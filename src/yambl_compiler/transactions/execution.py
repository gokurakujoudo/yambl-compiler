"""Build atomic candidate changes and preserve later edits during rollback."""

import json
from decimal import Decimal
from typing import cast

from yambl_compiler.ir.model import Field
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol, Value
from yambl_compiler.languages.shared.writing import literal
from yambl_compiler.languages.yaplan.model import Operation, Plan
from yambl_compiler.languages.yatran.model import Change, Image, Transaction, TransactionGroup
from yambl_compiler.storage.backends import Row
from yambl_compiler.storage.records import StoredField
from yambl_compiler.transactions.binding import Entity, object_items
from yambl_compiler.transactions.editing import Data


class Execution:
    """Apply one plan to a detached candidate using shared field codecs."""

    def __init__(self, data: Data) -> None:
        """Start with no write claims.

        :param data: Candidate snapshot.
        :returns: None.
        """
        self.data = data
        self.claims: dict[tuple[str, str], set[str]] = {}

    def claim(self, target: str, id_: Value, field: str) -> None:
        """Reject another write to an entity or its logical field.

        :param target: Entity type.
        :param id_: Business ID.
        :param field: Logical field or entity operation marker.
        :returns: None.
        :raises ValueError: If the Plan contains overlapping writes.
        """
        entity = self.data.catalog.entity(target)
        id_text = str(self.data.catalog.scalar(entity.id.type, id_))
        self.data.catalog.check_writable_identity(entity, id_text, deleting=field == '*')
        claims = self.claims.setdefault((entity.root, id_text), set())
        if field in claims or '*' in claims or (field == '*' and claims):
            raise ValueError(f'overlapping operations for {target}.{field}')
        claims.add(field)

    def validate_claims(self, plan: Plan) -> None:
        """Check independent entity and field writes across all plan groups.

        :param plan: Model-bound or unbound Plan.
        :returns: None without reading or changing instance data.
        :raises ValueError: If operations write the same entity or field more than once.
        """
        self.claims.clear()
        for group in plan.groups:
            for operation in group.operations:
                entity, field, _ = self.data.catalog.target(operation.target)
                if operation.scope:
                    owner = next((c for c in operation.scope if c.name == entity.id.name), None)
                    if owner is None or owner.end is not None or field is None:
                        raise ValueError('field scope requires owner ID')
                    self.claim(entity.definition.identity, owner.value, field.identity)
                    continue
                if entity.id.name not in operation.columns:
                    raise ValueError('entity operation requires its ID column')
                id_index = operation.columns.index(entity.id.name)
                for row in operation.rows:
                    if operation.kind in ('insert', 'delete'):
                        marker = '+' if operation.kind == 'insert' else '*'
                        self.claim(entity.definition.identity, row[id_index], marker)
                    if operation.kind != 'delete':
                        for name in operation.columns:
                            if name != entity.id.name:
                                current, _ = self.data.catalog.field(entity, name)
                                self.claim(
                                    entity.definition.identity, row[id_index], current.identity
                                )

    def field(self, operation: Operation) -> Change:
        """Execute one complete field scope and retain its before/after images.

        :param operation: Field replacement or deletion.
        :returns: Actual scope change.
        """
        entity, resolved, _ = self.data.catalog.target(operation.target)
        field = cast(Field, resolved)
        owner = next(c for c in operation.scope if c.name == entity.id.name)
        actual = self.data.actual(entity, owner.value)
        self.data.catalog.field(actual, field.name)
        before = self.data.image(operation.target, operation.scope)
        self.expect(operation, len(before.rows))
        if operation.kind == 'delete':
            self.data.clear(operation.target, operation.scope)
        elif operation.kind == 'replace':
            self.data.put(
                operation.target,
                operation.scope,
                Image(operation.target, operation.columns, operation.rows),
            )
        else:
            raise ValueError('field scopes require replace or delete')
        return Change(
            operation.target,
            operation.scope,
            (before,),
            (self.data.image(operation.target, operation.scope),),
        )

    def expect(self, operation: Operation, count: int) -> None:
        """Check the number of matched records before a mutation.

        :param operation: Requested mutation and optional expectation.
        :param count: Actual number of matching records.
        :returns: None.
        :raises ValueError: If the count violates the expectation.
        """
        if (operation.expect == 'one' and count != 1) or (operation.expect == 'any' and count < 1):
            raise ValueError(
                f'expect {operation.expect} matched {count} records: {operation.target}'
            )

    def matched_entity_rows(self, operation: Operation) -> tuple[tuple[Value, ...], ...]:
        """Select existing requested entity IDs without changing candidate data.

        :param operation: Entity deletion with an ID column.
        :returns: Requested rows whose IDs exist in this inheritance root.
        :raises ValueError: On missing ID columns or incompatible ID literals.
        """
        entity = self.data.catalog.entity(operation.target)
        if operation.columns != (entity.id.name,):
            raise ValueError('entity deletion accepts only the ID column')
        return tuple(
            values
            for values in operation.rows
            if self.data.select(
                entity.table,
                {entity.column: self.data.catalog.scalar(entity.id.type, values[0])},
            )
        )

    def validate_expectations(self, plan: Plan) -> None:
        """Check expectations using the snapshot and earlier planned identity rows.

        :param plan: Parsed Plan with an optional matching model digest.
        :returns: None; no candidate data or storage writes are performed.
        :raises ValueError: On model mismatch, invalid selectors or count mismatch.
        """
        if plan.model is not None and plan.model != self.data.layout.model.model_digest:
            raise ValueError('Plan model digest does not match the repository')
        planned: dict[str, dict[tuple[object, ...], Row]] = {}
        inserted: dict[tuple[str, str], Entity] = {}
        planned_codes: dict[str, set[str]] = {}
        from yambl_compiler.transactions.validation import PlanValidator

        validator = PlanValidator(self.data.layout)

        def available_codes(identity: str) -> set[str]:
            """Resolve planned dictionary membership or the original snapshot.

            :param identity: Qualified dictionary identity.
            :returns: Codes visible before the current Plan operation.
            """
            if identity in planned_codes:
                return planned_codes[identity]
            return self.data.dictionary_codes(identity)

        validator.catalog.code_members = available_codes

        def mark(table: str, row: Row) -> None:
            """Retain a planned row's identity and coordinates, not its payload.

            :param table: Physical table holding the row.
            :param row: Its identifying and coordinate values.
            :returns: None.
            """
            key = tuple(row[column] for column in self.data.schema[table].primary)
            planned.setdefault(table, {})[key] = row

        def mark_field(
            entity: Entity, field: Field, stored: StoredField, record: dict[str, Value]
        ) -> None:
            """Track a row created by a preceding field write.

            :param entity: Declared field owner.
            :param field: Model field.
            :param stored: Physical field layout.
            :param record: Field coordinates and value.
            :returns: None.
            """
            coordinates = self.data.coordinates(entity, field, stored)
            types = {entity.id.name: entity.id.type, **{d.key: d.type for d in stored.dimensions}}
            row: Row = dict(stored.key_values)
            for name, column in coordinates.items():
                value = record[name]
                row[column] = (
                    validator.catalog.scalar(types[name], value) if name in types else value
                )
            mark(stored.table, row)

        for group in plan.groups:
            for operation in group.operations:
                validator.operation(operation)
                entity, field, stored = self.data.catalog.target(operation.target)
                if operation.expect is not None:
                    if operation.scope and field is not None and stored is not None:
                        owner = next(c for c in operation.scope if c.name == entity.id.name)
                        id_text = str(self.data.catalog.scalar(entity.id.type, owner.value))
                        actual = inserted.get((entity.root, id_text))
                        if actual is not None:
                            ancestor = actual.definition
                            while (
                                ancestor.identity != entity.definition.identity
                                and ancestor.parent in self.data.catalog.definitions
                            ):
                                ancestor = self.data.catalog.definitions[ancestor.parent]
                            if ancestor.identity != entity.definition.identity:
                                raise ValueError('ID belongs to an incompatible entity type')
                            self.data.catalog.field(actual, field.name)
                        count = len(self.data.image(operation.target, operation.scope).rows)
                        additions = planned.get(stored.table, {})
                        if additions:
                            filters, interval = self.data.selection(
                                entity, field, stored, operation.scope
                            )
                            primary = self.data.schema[stored.table].primary
                            existing = {
                                tuple(row[key] for key in primary)
                                for row in self.data.select(stored.table, filters)
                                if self.data.matches(row, filters, interval, field.category)
                            }
                            count += sum(
                                key not in existing
                                and self.data.matches(row, filters, interval, field.category)
                                for key, row in additions.items()
                            )
                    else:
                        count = len(self.matched_entity_rows(operation))
                    self.expect(operation, count)
                if operation.scope and field is not None and stored is not None:
                    if operation.kind == 'replace':
                        constants = {c.name: c.value for c in operation.scope if c.end is None}
                        for row in operation.rows:
                            mark_field(
                                entity,
                                field,
                                stored,
                                {**constants, **dict(zip(operation.columns, row, strict=True))},
                            )
                elif operation.kind in ('insert', 'update'):
                    for row in operation.rows:
                        record = dict(zip(operation.columns, row, strict=True))
                        id_ = record[entity.id.name]
                        if operation.kind == 'insert':
                            value = self.data.catalog.scalar(entity.id.type, id_)
                            inserted[(entity.root, str(value))] = entity
                            mark(entity.table, {entity.column: value})
                        for name in operation.columns:
                            if name != entity.id.name:
                                current, binding = self.data.catalog.field(entity, name)
                                mark_field(entity, current, binding, record)
                if operation.target == 'builtins.Decodes.entries':
                    dictionary = next(c.value for c in operation.scope if c.name == 'decodes_id')
                    if isinstance(dictionary, str) and dictionary in self.data.catalog.definitions:
                        replacement_value = (
                            operation.rows[0][operation.columns.index('value')]
                            if operation.kind == 'replace' and operation.rows
                            else ()
                        )
                        planned_codes[dictionary] = (
                            {str(object_items(item)['code']) for item in replacement_value}
                            if isinstance(replacement_value, tuple)
                            else set()
                        )

    def entities(self, operation: Operation) -> tuple[Change, ...]:
        """Execute a batch of ID-based entity mutations.

        :param operation: Entity insertion, field update or deletion.
        :returns: One entity change or individual field changes for each row.
        """
        entity = self.data.catalog.entity(operation.target)
        if operation.kind == 'delete' and operation.columns != (entity.id.name,):
            raise ValueError('entity deletion accepts only the ID column')
        selected = operation.rows
        if operation.kind == 'delete':
            selected = self.matched_entity_rows(operation)
            self.expect(operation, len(selected))
        changes: list[Change] = []
        for values in selected:
            record = dict(zip(operation.columns, values, strict=True))
            id_ = record.pop(entity.id.name)
            scope = (Condition(entity.id.name, id_),)
            if operation.kind == 'insert':
                self.data.insert(entity, id_)
                actual = entity
            else:
                actual = self.data.actual(entity, id_)
            if operation.kind == 'delete':
                entity_before = self.data.entity_images(actual, id_)
                self.data.delete(actual, id_, operation.owned_data)
                changes.append(Change(actual.definition.identity, scope, entity_before, ()))
                continue
            for name, value in record.items():
                field, stored = self.data.catalog.field(actual, name)
                if (
                    stored.dimensions
                    or field.category in ('snapshot', 'scd')
                    or (stored.members and operation.kind != 'insert')
                ):
                    raise ValueError('coordinate and collection fields require a field scope')
                target = actual.definition.identity + '.' + name
                before = self.data.image(target, scope)
                self.data.put(target, scope, Image(target, ('value',), ((value,),)))
                if operation.kind == 'update':
                    changes.append(
                        Change(target, scope, (before,), (self.data.image(target, scope),))
                    )
            if operation.kind == 'insert':
                changes.append(
                    Change(
                        actual.definition.identity, scope, (), self.data.entity_images(actual, id_)
                    )
                )
            elif operation.kind != 'update':
                raise ValueError('invalid entity operation')
        return tuple(changes)

    def apply(self, plan: Plan) -> tuple[TransactionGroup, ...]:
        """Apply all groups to the candidate, preserving the original boundaries.

        :param plan: Model-bound Plan.
        :returns: Actual changes grouped by Plan group.
        :raises ValueError: On model mismatch or invalid operations.
        """
        result = self.prepare(plan)
        self.data.validate()
        return result

    def prepare(self, plan: Plan) -> tuple[TransactionGroup, ...]:
        """Prepare unvalidated candidate changes without publishing them.

        The caller must validate the final candidate before it is published.

        :param plan: Model-bound or unbound Plan.
        :returns: Actual changes grouped by Plan group.
        :raises ValueError: On model mismatch or invalid operations.
        """
        if plan.model is not None and plan.model != self.data.layout.model.model_digest:
            raise ValueError('Plan model digest does not match the repository')
        self.validate_claims(plan)
        groups: list[TransactionGroup] = []
        for group in plan.groups:
            changes: list[Change] = []
            for operation in group.operations:
                if operation.scope:
                    changes.append(self.field(operation))
                else:
                    changes.extend(self.entities(operation))
            groups.append(TransactionGroup(group.name, tuple(changes)))
        return tuple(groups)


def semantic_value(value: Value) -> str:
    """Encode typed values with unordered collection and mapping semantics.

    :param value: Business literal from a validated data image.
    :returns: Stable comparison key, distinguishing booleans from numbers.
    """
    if isinstance(value, bool):
        return 'bool:' + literal(value)
    if isinstance(value, (int, Decimal)):
        return 'number:' + literal(Decimal(value))
    if isinstance(value, tuple):
        return 'collection:' + json.dumps(sorted(semantic_value(item) for item in value))
    if isinstance(value, ObjectValue):
        return 'object:' + json.dumps(
            sorted(
                (semantic_value(key.name if isinstance(key, Symbol) else key), semantic_value(item))
                for key, item in value.items
            )
        )
    return ('symbol:' if isinstance(value, Symbol) else 'string:') + literal(value)


def comparable(image: Image) -> tuple[tuple[str, ...], ...]:
    """Canonicalize unordered records and equivalent adjacent SCD fragments.

    :param image: Typed business data image.
    :returns: Sorted semantic records.
    """
    if 'valid_from' not in image.columns:
        return tuple(sorted(tuple(semantic_value(v) for v in row) for row in image.rows))
    start, end = image.columns.index('valid_from'), image.columns.index('valid_to')
    groups: dict[tuple[str, ...], list[tuple[Value, ...]]] = {}
    for row in image.rows:
        key = tuple(semantic_value(v) for i, v in enumerate(row) if i not in (start, end))
        groups.setdefault(key, []).append(row)
    merged: list[tuple[Value, ...]] = []
    for rows in groups.values():
        pieces: list[tuple[Value, ...]] = []
        for row in sorted(rows, key=lambda r: str(r[start])):
            if pieces and pieces[-1][end] == row[start]:
                previous = list(pieces[-1])
                previous[end] = row[end]
                pieces[-1] = tuple(previous)
            else:
                pieces.append(row)
        merged.extend(pieces)
    return tuple(sorted(tuple(semantic_value(v) for v in row) for row in merged))


def same(left: tuple[Image, ...], right: tuple[Image, ...]) -> bool:
    """Compare full images independently of table row ordering.

    :param left: Current business data.
    :param right: Recorded transaction data.
    :returns: Whether both images represent identical data.
    """
    return len(left) == len(right) and all(
        a.target == b.target and a.columns == b.columns and comparable(a) == comparable(b)
        for a, b in zip(
            sorted(left, key=lambda i: i.target), sorted(right, key=lambda i: i.target), strict=True
        )
    )


def rollback(
    data: Data, transaction: Transaction, selected: tuple[str, ...] | None = None
) -> tuple[TransactionGroup, ...]:
    """Reverse selected actual changes while retaining conflicting later data.

    :param data: Detached candidate snapshot.
    :param transaction: Validated source execution record.
    :param selected: Unique group names, or None for every recorded group.
    :returns: Actual reverse changes and conflict warnings.
    :raises ValueError: On invalid selection or model mismatch.
    """
    result = prepare_rollback(data, transaction, selected)
    data.validate()
    return result


def restore_image(data: Data, target: str, scope: tuple[Condition, ...], image: Image) -> None:
    """Restore an image while deferring code membership to final candidate validation.

    :param data: Detached rollback candidate.
    :param target: Field target.
    :param scope: Recorded field scope.
    :param image: Original data image.
    :returns: None.
    """
    original = data.catalog.code_members
    data.catalog.code_members = lambda _: None
    try:
        data.put(target, scope, image)
    finally:
        data.catalog.code_members = original


def prepare_rollback(
    data: Data, transaction: Transaction, selected: tuple[str, ...] | None = None
) -> tuple[TransactionGroup, ...]:
    """Prepare an unvalidated rollback without publishing it.

    The caller must validate the final candidate before it is published.

    :param data: Detached candidate snapshot.
    :param transaction: Validated source execution record.
    :param selected: Unique group names, or None for every recorded group.
    :returns: Actual reverse changes and conflict warnings.
    :raises ValueError: On invalid selection or model mismatch.
    """
    if transaction.plan.model != data.layout.model.model_digest:
        raise ValueError('Transaction model digest does not match the repository')
    names = {g.name for g in transaction.groups}
    if selected is not None and (len(set(selected)) != len(selected) or set(selected) - names):
        raise ValueError('unknown or duplicate rollback group')
    result: list[TransactionGroup] = []
    for group in reversed(transaction.groups):
        if selected is not None and group.name not in selected:
            continue
        changes: list[Change] = []
        warnings: list[str] = []
        for change in reversed(group.changes):
            entity, field, _ = data.catalog.target(change.target)
            id_ = next(c.value for c in change.scope if c.name == entity.id.name)
            encoded = data.catalog.scalar(entity.id.type, id_)
            data.catalog.check_writable_identity(entity, str(encoded), deleting=field is None)
            exists = bool(data.select(entity.table, {entity.column: encoded}))
            if exists:
                try:
                    actual = data.actual(entity, id_)
                    if field is not None:
                        data.catalog.field(actual, field.name)
                except ValueError:
                    warnings.append(f'{change.target}: later entity type retained')
                    continue
            if field is None:
                current = data.entity_images(entity, id_) if exists else ()
            else:
                current = (data.image(change.target, change.scope),) if exists else ()
            if not same(current, change.after):
                warnings.append(f'{change.target}: later or missing data retained')
                continue
            if field is None:
                if exists:
                    data.delete(entity, id_, True)
                if change.before:
                    restored = data.catalog.entity(change.before[0].target)
                    data.insert(restored, id_)
                    for image in change.before[1:]:
                        restore_image(data, image.target, change.scope, image)
                after = data.entity_images(entity, id_) if change.before else ()
            else:
                if len(change.before) != 1:
                    raise ValueError('field rollback requires one before image')
                restore_image(data, change.target, change.scope, change.before[0])
                after = (data.image(change.target, change.scope),)
            changes.append(Change(change.target, change.scope, current, after))
        result.append(TransactionGroup(group.name, tuple(changes), tuple(warnings)))
    return tuple(result)
