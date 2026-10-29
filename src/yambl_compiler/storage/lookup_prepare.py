"""Prepare lookup indexes from projected, pinned repository tables."""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import InvalidOperation
from typing import cast

from yambl_compiler.ir.model import Lookup, LookupBinding
from yambl_compiler.languages.yambl.computation.evaluation import evaluate
from yambl_compiler.languages.yambl.computation.records import Expression
from yambl_compiler.storage.backends import Row, RowValidationError, Selection
from yambl_compiler.storage.batch import Batch, FieldRequest
from yambl_compiler.storage.computing import UNSET, Computing
from yambl_compiler.storage.configuration import Configuration
from yambl_compiler.storage.lookup import KeyField
from yambl_compiler.storage.records import actual_type, table_name
from yambl_compiler.storage.results import EntityRef, NonNormalResult, Status, Success
from yambl_compiler.storage.session import Session


def prepare[KeyT, IdT: str | int, MatchT](
    session: Session,
    owner: str,
    rule: Lookup,
    key_type: type[KeyT],
    id_type: type[IdT],
    inputs: Mapping[str, object],
    matcher_type: Callable[
        [type[KeyT], Sequence[KeyField], Sequence[tuple[tuple[object, ...], IdT]], int], MatchT
    ],
) -> Success[MatchT] | NonNormalResult:
    """Build a detached index for an entity branch from one repository snapshot.

    :param session: Pinned reader services used only while preparing.
    :param owner: Candidate entity identity.
    :param rule: Checked lookup contract.
    :param key_type: Generated key record class.
    :param id_type: Generated candidate ID type.
    :param inputs: Preparation parameters by their checked names.
    :param matcher_type: Explicit generated matcher class.
    :returns: Complete matcher or a located error without a partial index.
    """
    try:
        return _prepare(session, owner, rule, key_type, id_type, inputs, matcher_type)
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        InvalidOperation,
        ArithmeticError,
    ) as error:
        return NonNormalResult(Status.ERROR, f'lookup {owner}.{rule.name}: {error}')


def _prepare[KeyT, IdT: str | int, MatchT](
    session: Session,
    owner: str,
    rule: Lookup,
    key_type: type[KeyT],
    id_type: type[IdT],
    inputs: Mapping[str, object],
    matcher_type: Callable[
        [type[KeyT], Sequence[KeyField], Sequence[tuple[tuple[object, ...], IdT]], int], MatchT
    ],
) -> Success[MatchT]:
    """Read and index all paths after validating preparation inputs.

    :param session: Pinned reader services.
    :param owner: Candidate entity identity.
    :param rule: Checked lookup contract.
    :param key_type: Generated key record class.
    :param id_type: Generated candidate ID type.
    :param inputs: Preparation parameters.
    :param matcher_type: Explicit generated matcher class.
    :returns: Complete matcher.
    :raises ValueError: If a candidate or preparation input is invalid.
    """
    if set(inputs) - {item.name for item in rule.inputs}:
        raise ValueError('preparation parameters do not match lookup contract')
    parameters = {item.name: item for item in rule.inputs}
    normalized_inputs = dict(inputs)
    references = {item.name: item.value for item in rule.references}
    definitions = {item.identity: item for item in session.layout.model.definitions}
    batch = Batch(session)
    computing = Computing(session, batch, Configuration(batch), {})

    def resolve(name: str) -> object:
        """Resolve a lexical constant, dictionary member, or preparation input.

        :param name: Expression name or qualified member.
        :returns: Resolved value.
        """
        if name.startswith('input.'):
            return input_value(name.removeprefix('input.'))
        root, separator, member = name.partition('.')
        target = references[root]
        definition = definitions[target]
        if separator:
            return session.code_types[target](member)
        prior = references.copy()
        references.clear()
        references.update({item.name: item.value for item in definition.references})
        try:
            return evaluate(cast(Expression, definition.initializer), resolve, UNSET)
        finally:
            references.clear()
            references.update(prior)

    def input_value(name: str) -> object:
        """Bind and validate one input, evaluating a declared default once.

        :param name: Declared input name.
        :returns: Validated value or the omission sentinel.
        """
        parameter = parameters[name]
        value = normalized_inputs.get(name, UNSET)
        if value is UNSET and parameter.default is not None:
            value = evaluate(parameter.default, resolve, UNSET)
            if parameter.type.name == 'date' and isinstance(value, str):
                value = date.fromisoformat(value)
        if value is UNSET:
            if parameter.required:
                raise ValueError(
                    f'preparation parameters do not match lookup contract: missing {name}'
                )
            normalized_inputs[name] = value
            return value
        try:
            if parameter.type.name in ('float', 'datetime') or (
                parameter.type.target in definitions
                and definitions[parameter.type.target].kind == 'value'
            ):
                value = computing.boundary(parameter.type, value)
            else:
                if parameter.type.kind == 'reference' and isinstance(value, str):
                    value = EntityRef[object](parameter.type.target, value)
                value = session.scalar_rule(parameter.type).check(value)
        except (ValueError, TypeError, InvalidOperation) as error:
            raise ValueError(f'parameter {name}: {error}') from error
        normalized_inputs[name] = value
        return value

    for parameter in rule.inputs:
        input_value(parameter.name)
    fields: list[KeyField] = []
    for key in rule.keys:
        try:
            fields.append(KeyField(key.name, session.scalar_rule(key.type)))
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            raise ValueError(f'key {key.name}: {error}') from error
    layout = session.layout
    coordinates: dict[tuple[int, int], tuple[tuple[tuple[str, str], ...], date | None]] = {}
    for key_index, key in enumerate(rule.keys):
        for step_index, step in enumerate(key.steps):
            stored = layout.fields.get(step.access.identity)
            if stored is None:
                continue
            bound: dict[str, object] = {}
            for binding in step.bindings:
                try:
                    argument = (
                        evaluate(binding.expression, resolve, UNSET)
                        if not binding.input_name and not binding.fixed_kind and binding.expression
                        else binding_value(session, binding, normalized_inputs)
                    )
                    if argument is UNSET:
                        raise ValueError('missing required value')
                    if binding.type.name == 'code' and type(argument) is str:
                        argument = session.code_types[binding.type.arguments[0].target](argument)
                    if binding.type.name == 'string' and isinstance(argument, str):
                        argument = str(argument)
                    argument = session.scalar_rule(binding.type).check(argument)
                    bound[binding.name] = argument
                except (ValueError, TypeError, InvalidOperation) as error:
                    raise ValueError(
                        f'key {key.name}, parameter {binding.name}: {error}'
                    ) from error
            dimension_names = {dimension.key for dimension in stored.dimensions}
            dimensions = tuple(
                ('dim_' + dimension.key, str(bound[dimension.key]))
                for dimension in stored.dimensions
            )
            dates = [
                bound[binding.name]
                for binding in step.bindings
                if binding.name not in dimension_names
            ]
            when = cast(date | None, dates[0] if dates else None)
            coordinates[(key_index, step_index)] = (dimensions, when)
    roots = {layout.roots[owner]}
    columns: dict[str, set[str]] = {}
    sites: dict[str, set[tuple[str, str]]] = {}
    table_roots: dict[str, set[str]] = {}
    for key in rule.keys:
        for step in key.steps:
            implementation = session.implementations[step.access.implementation]
            if implementation.type.kind == 'reference':
                target_root = layout.roots[implementation.type.target]
                roots.add(target_root)
                sites.setdefault(table_name(target_root), set()).add(
                    (key.name, step.access.identity)
                )
            stored = layout.fields.get(step.access.identity)
            if stored is None or not stored.table:
                continue
            table_roots.setdefault(stored.table, set()).add(stored.root)
            sites.setdefault(stored.table, set()).add((key.name, step.access.identity))
            selected = columns.setdefault(stored.table, {'id'})
            selected.add(stored.state)
            selected.add(stored.value)
            selected.update('dim_' + dimension.key for dimension in stored.dimensions)
            selected.update(name for name, _ in stored.key_values)
            if implementation.category == 'scd':
                selected.update(('valid_from', 'valid_to'))
    for root in roots:
        builtin = layout.entities[root].source.origin == 'builtin'
        identifier = layout.ids[root].name if builtin else 'id'
        selected = columns.setdefault(table_name(root), set())
        table_roots.setdefault(table_name(root), set()).add(root)
        selected.add(identifier)
        if layout.discriminators[root]:
            selected.add('entity_type')
    sites.setdefault(table_name(layout.roots[owner]), set()).update(
        (key.name, 'candidate identities') for key in rule.keys
    )
    session.ready()
    tables: dict[str, list[Row]] = {}
    for table, selected in columns.items():
        try:
            tables[table] = session.backend.rows_many(
                table, [Selection(())], tuple(sorted(selected))
            )[0]
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            source = ', '.join(
                f'key {name}, field {field}' for name, field in sorted(sites.get(table, set()))
            )
            entity_text = ''
            if (
                isinstance(error, RowValidationError)
                and error.identifier is not None
                and len(table_roots.get(table, ())) == 1
            ):
                root = next(iter(table_roots[table]))
                try:
                    identity = session.decode(layout.ids[root].type, error.identifier)
                    entity_text = f', entity {identity}'
                except ValueError, TypeError, KeyError, InvalidOperation:
                    pass
            raise ValueError(f'table {table}, {source}{entity_text}: {error}') from error
    identities: dict[str, dict[str | int, EntityRef[object]]] = {}
    encoded_ids: dict[str, dict[str | int, object]] = {}
    for root in roots:
        builtin = layout.entities[root].source.origin == 'builtin'
        identifier = layout.ids[root].name if builtin else 'id'
        entries: dict[str | int, EntityRef[object]] = {}
        encoded: dict[str | int, object] = {}
        for row in tables[table_name(root)]:
            identity = cast(str | int, session.decode(layout.ids[root].type, row[identifier]))
            actual = actual_type(layout, root, row)
            if actual not in layout.entities or layout.roots[actual] != root:
                source = ', '.join(
                    f'key {name}, field {field}'
                    for name, field in sorted(sites.get(table_name(root), set()))
                )
                raise ValueError(
                    f'table {table_name(root)}, {source}, entity {identity}: invalid actual entity type'
                )
            if identity in entries:
                raise ValueError(f'table {table_name(root)}, entity {identity}: duplicate identity')
            entries[identity] = EntityRef(actual, identity)
            encoded[identity] = row[identifier]
        identities[root] = entries
        encoded_ids[root] = encoded
    indexes: dict[tuple[str, tuple[str, ...]], dict[tuple[object, ...], list[Row]]] = {}
    cached: dict[tuple[object, ...], object] = {}
    complete: list[tuple[tuple[object, ...], IdT]] = []
    skipped = 0
    for entity in identities[layout.roots[owner]].values():
        if not batch.is_type(entity.target, owner):
            continue
        values: list[object] = []
        for key_index, key in enumerate(rule.keys):
            current = entity
            for position, step in enumerate(key.steps):
                field = batch.implementation(current.target, step.access.identity)
                if field is None:
                    value: object = NonNormalResult(Status.NOT_DEFINED)
                elif field.category == 'id':
                    value = Success(current.id)
                else:
                    stored = layout.fields[step.access.identity]
                    dimensions, when = coordinates[(key_index, position)]
                    if not stored.table:
                        decoded = tuple(
                            (dimension.key, session.decode(dimension.type, value))
                            for dimension, (_, value) in zip(
                                stored.dimensions, dimensions, strict=True
                            )
                        )
                        value = batch.read(
                            (FieldRequest(current, step.access.identity, decoded, when),)
                        )[0]
                    else:
                        keys = (
                            ("id", encoded_ids[stored.root][current.id]),
                            *dimensions,
                            *stored.key_values,
                        )
                        if field.category == 'scd':
                            assert when is not None
                            selection = Selection(keys, 'scd', when.isoformat(), when.isoformat())
                        else:
                            selection = Selection(keys)
                        cache_key = (current, step.access.identity, selection)
                        if cache_key not in cached:
                            names = tuple(name for name, _ in selection.keys)
                            index_key = (stored.table, names)
                            if index_key not in indexes:
                                table_index: dict[tuple[object, ...], list[Row]] = {}
                                for row in tables[stored.table]:
                                    table_index.setdefault(
                                        tuple(row[name] for name in names), []
                                    ).append(row)
                                indexes[index_key] = table_index
                            matches = indexes[index_key].get(
                                tuple(value for _, value in selection.keys), []
                            )
                            try:
                                cached[cache_key] = session.point(stored, field, matches, when)
                            except (
                                ValueError,
                                TypeError,
                                KeyError,
                                IndexError,
                                InvalidOperation,
                            ) as error:
                                raise ValueError(
                                    f'key {key.name}, entity {entity.id}, field {step.access.identity}: {error}'
                                ) from error
                        value = cached[cache_key]
                if isinstance(value, NonNormalResult):
                    if value.status == Status.ERROR:
                        raise ValueError(
                            f'key {key.name}, entity {entity.id}, field {step.access.identity}: {value.message}'
                        )
                    break
                if not isinstance(value, Success):
                    raise ValueError(f'key {key.name}, entity {entity.id}: invalid field result')
                value = cast(Success[object], value)
                reference = value.value
                if isinstance(reference, EntityRef):
                    target = identities[layout.roots[reference.target]].get(reference.id)
                    if target is None or not batch.is_type(target.target, reference.target):
                        break
                    if position + 1 == len(key.steps):
                        values.append(EntityRef[object](key.type.target, reference.id))
                        continue
                    current = target
                    continue
                if position + 1 == len(key.steps):
                    values.append(
                        str(reference)
                        if key.type.name == 'string' and isinstance(reference, str)
                        else reference
                    )
                    continue
                else:
                    raise ValueError(f'key {key.name}, entity {entity.id}: expected a reference')
            else:
                continue
            break
        else:
            if type(entity.id) is not id_type:
                raise ValueError(f'invalid candidate ID type: {entity.id}')
            complete.append((tuple(values), cast(IdT, entity.id)))
            continue
        skipped += 1
    return Success(matcher_type(key_type, fields, complete, skipped))


def binding_value(session: Session, binding: LookupBinding, inputs: Mapping[str, object]) -> object:
    """Resolve one checked field parameter from an input or fixed value.

    :param session: Reader services for generated code classes.
    :param binding: Checked binding and type.
    :param inputs: Already validated preparation values.
    :returns: Typed field parameter value.
    """
    if binding.input_name:
        value = inputs[binding.input_name]
        return str(value) if binding.type.name == 'string' and type(value) is not str else value
    if binding.fixed_kind == 'date':
        return date.fromisoformat(binding.fixed_text)
    if binding.fixed_kind == 'code':
        return session.code_types[binding.fixed_target](binding.fixed_text)
    return binding.fixed_text
