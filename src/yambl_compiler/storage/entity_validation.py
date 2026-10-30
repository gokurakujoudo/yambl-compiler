"""Validate entity-level uniqueness against a reader's current snapshot."""

from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, cast

from yambl_compiler.storage.records import actual_type, table_name
from yambl_compiler.storage.results import EntityRef, NonNormalResult, Status, Success
from yambl_compiler.storage.validation import ValidationIssue

if TYPE_CHECKING:
    from yambl_compiler.ir.model import Type
    from yambl_compiler.storage.runtime import Reader


def value_key(value: object, type_: 'Type', reader: 'Reader') -> object:
    """Build a typed immutable key for a value returned by the reader.

    :param value: Normal field payload.
    :param type_: Actual field's declared contract.
    :param reader: Reader providing structured value declarations.
    :returns: Hashable equality key retaining type and reference identity.
    """
    if isinstance(value, EntityRef) or type_.kind == 'reference':
        return ('reference', type_.target, getattr(value, 'id', value))
    if is_dataclass(value) and not isinstance(value, type):
        declaration = next(
            item for item in reader._layout.model.definitions if item.identity == type_.target
        )
        types = {field.name: field.type for field in declaration.fields}
        return (
            'structured',
            type_.target,
            tuple(
                (field.name, value_key(getattr(value, field.name), types[field.name], reader))
                for field in fields(value)
            ),
        )
    return (type_.name, type_.target, value)


def validate_entities(reader: 'Reader') -> tuple[ValidationIssue, ...]:
    """Check each declared unique key over its entity and descendants.

    :param reader: Reader pinned to the full or candidate snapshot.
    :returns: Read failures and all duplicate identity groups.
    """
    layout = reader._layout
    keys = [key for definition in layout.model.definitions for key in definition.unique_keys]
    if not keys:
        return ()
    issues: list[ValidationIssue] = []
    for key in keys:
        root = layout.roots[key.entity]
        seen: dict[tuple[object, ...], list[str]] = {}
        for row in reader._backend.rows(table_name(root), {}):
            actual = actual_type(layout, root, row)
            ancestor = actual
            while ancestor != key.entity and ancestor in layout.entities:
                ancestor = layout.entities[ancestor].parent
            if ancestor != key.entity:
                continue
            identity = cast(str | int, reader._decode(layout.ids[root].type, row['id']))
            handle = reader.entity_types[actual](reader, actual, identity)
            results = [reader.read(handle, field, {}) for field in key.fields]
            reference = f'{actual}:{identity}'
            for field, result in zip(key.fields, results, strict=True):
                if isinstance(result, NonNormalResult) and result.status == Status.ERROR:
                    issues.append(
                        ValidationIssue(
                            field, f'unique_by read failed: {result.message}', (reference,)
                        )
                    )
            if not all(isinstance(result, Success) for result in results):
                continue
            values: list[object] = []
            for field, result in zip(key.fields, results, strict=True):
                assert isinstance(result, Success)
                implementation = reader._batch.implementation(actual, field)
                assert implementation is not None
                values.append(value_key(result.value, implementation.type, reader))
            seen.setdefault(tuple(values), []).append(reference)
        for identities in seen.values():
            if len(identities) > 1:
                source = key.source
                issues.append(
                    ValidationIssue(
                        key.entity,
                        f'unique_by {key.fields} at {source.path}:{source.line}:{source.column}',
                        tuple(sorted(identities)),
                    )
                )
    return tuple(issues)
