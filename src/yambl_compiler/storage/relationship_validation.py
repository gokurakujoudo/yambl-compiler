"""Validate forward relationship cardinalities on a data snapshot."""

from collections.abc import Callable, Mapping
from itertools import combinations
from typing import cast

from yambl_compiler.ir.model import Field
from yambl_compiler.storage.backends import Row
from yambl_compiler.storage.records import Layout, StoredField, actual_type, table_name
from yambl_compiler.storage.results import EntityRef, NonNormalResult, ReadResult
from yambl_compiler.storage.validation import ValidationIssue


def validate_relationships(
    layout: Layout,
    select: Callable[[str, Mapping[str, object]], list[Row]],
    field: Callable[[str, str], Field | None],
    read: Callable[[StoredField, Field, Row], ReadResult[object]],
) -> tuple[ValidationIssue, ...]:
    """Check required values and target uniqueness in a complete candidate.

    :param layout: Validated model and physical storage contract.
    :param select: Candidate row selection on a single snapshot.
    :param field: Resolve an actual entity's field implementation.
    :param read: Decode a stored row, including collection members.
    :returns: Cardinality violations with their field and coordinate.
    """
    issues: list[ValidationIssue] = []
    for identity, stored in sorted(layout.fields.items()):
        relation = next(
            (call for call in stored.field.decorators if call.target == 'builtins.relationship'),
            None,
        )
        if relation is None or not stored.table:
            continue
        bounds = {item.name: item.type.exact.text for item in relation.bound if item.type.exact}
        owners = {
            str(row['id']): actual_type(layout, stored.root, row)
            for row in select(table_name(stored.root), {})
        }
        rows = [
            row for row in select(stored.table, dict(stored.key_values)) if str(row['id']) in owners
        ]
        if bounds['y'] != 'maybe' and stored.field.category == 'static' and not stored.dimensions:
            present = {str(row['id']) for row in rows}
            for owner, actual in owners.items():
                if owner not in present and field(actual, identity) is not None:
                    issues.append(
                        ValidationIssue(identity, 'relationship requires a value', (owner,))
                    )
        groups: dict[
            tuple[tuple[tuple[str, str], ...], str, str, str], list[tuple[str, str, str]]
        ] = {}
        for row in rows:
            owner = str(row['id'])
            implementation = field(owners[owner], identity)
            if implementation is None:
                continue
            value = read(stored, implementation, row)
            dimensions = tuple((d.key, str(row['dim_' + d.key])) for d in stored.dimensions)
            snapshot = str(row['date']) if implementation.category == 'snapshot' else ''
            start, end = (
                (str(row['valid_from']), str(row['valid_to']))
                if implementation.category == 'scd'
                else ('', '')
            )
            if isinstance(value, NonNormalResult):
                if bounds['y'] != 'maybe':
                    issues.append(
                        ValidationIssue(
                            identity,
                            'relationship requires a value',
                            (owner,),
                            dimensions,
                            snapshot or start,
                            end,
                        )
                    )
                continue
            payload: object = value.value
            refs = (
                cast(frozenset[object], payload) if isinstance(payload, frozenset) else (payload,)
            )
            if bounds['y'] == 'many' and not refs:
                issues.append(
                    ValidationIssue(
                        identity,
                        'relationship requires a nonempty collection',
                        (owner,),
                        dimensions,
                        snapshot or start,
                        end,
                    )
                )
            if bounds['x'] == 'one':
                for ref in refs:
                    ref = cast(EntityRef[object], ref)
                    key = (dimensions, snapshot, layout.roots[ref.target], str(ref.id))
                    groups.setdefault(key, []).append((owner, start, end))
        for (dimensions, snapshot, _, target), records in sorted(groups.items()):
            for left, right in combinations(sorted(set(records)), 2):
                if left[0] == right[0]:
                    continue
                start = max(left[1], right[1])
                end = min(left[2], right[2]) if left[2] else ''
                if end and start >= end:
                    continue
                issues.append(
                    ValidationIssue(
                        identity,
                        'relationship target has multiple sources',
                        (left[0], right[0], target),
                        dimensions,
                        snapshot or start,
                        end,
                    )
                )
    return tuple(issues)
