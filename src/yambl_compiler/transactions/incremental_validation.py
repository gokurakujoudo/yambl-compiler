"""Check reference cycles reachable from changed candidate records."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import cast

from yambl_compiler.ir.model import Field
from yambl_compiler.storage.backends import Row
from yambl_compiler.storage.records import Layout, StoredField, table_name
from yambl_compiler.storage.results import EntityRef, NonNormalResult, ReadResult, Status
from yambl_compiler.storage.validation import ValidationIssue, ValidationReport, cycles


def _periods(
    records: list[tuple[str, str, str, str]], snapshot: str
) -> Iterator[tuple[str, str, set[tuple[str, str]]]]:
    """Stream active edges across the distinct half-open temporal intervals.

    :param records: Source, target and optional SCD bounds for one field coordinate.
    :param snapshot: Snapshot date for a non-SCD coordinate.
    :yields: Inclusive start, exclusive end and the active edge set.
    """
    boundaries = sorted({d for _, _, start, end in records for d in (start, end) if d})
    all_edges = {(a, b) for a, b, _, _ in records}
    if not boundaries:
        yield snapshot, '', all_edges
        return
    if not cycles(all_edges):
        return
    events: dict[str, list[tuple[tuple[str, str], int]]] = {}
    counts: dict[tuple[str, str], int] = {}
    for source, target, start, end in records:
        edge = (source, target)
        events.setdefault(start, []).append((edge, 1))
        events.setdefault(end, []).append((edge, -1))
    for start, end in zip(boundaries, boundaries[1:]):
        for edge, delta in events.get(start, ()):
            counts[edge] = counts.get(edge, 0) + delta
            if counts[edge] == 0:
                del counts[edge]
        yield start, end, set(counts)


def validate_affected_cycles(
    layout: Layout,
    select: Callable[[str, Mapping[str, object]], list[Row]],
    field: Callable[[str, str], Field | None],
    read: Callable[[StoredField, Field, Row], ReadResult[object]],
    changed: Mapping[str, Sequence[Row]],
) -> ValidationReport:
    """Find cycles affected by changed records in an otherwise validated candidate.

    The candidate lookup and reader must see the same overlay, including collection
    members. ``changed`` contains both before and after images, including inserted
    identities. Unrelated existing corruption requires a separate full check.

    :param layout: Generated model and storage contract.
    :param select: Candidate rows matching exact equality keys.
    :param field: Resolve the logical field on an actual entity type.
    :param read: Decode one candidate row with its actual field implementation.
    :param changed: Physical tables mapped to changed before and after rows.
    :returns: Cycle diagnostics reachable from the changed owners.
    :raises ValueError: If a reachable candidate identity or value is invalid.
    """
    issues: list[ValidationIssue] = []
    for identity, stored in sorted(layout.fields.items()):
        if (
            stored.root not in layout.entities
            or layout.entities[stored.root].source.origin != 'model'
        ):
            continue
        kind = stored.field.type.kind
        if kind not in ('reference', 'collection'):
            continue
        tables = {table_name(stored.root), stored.table, stored.members} - {''}
        pending = {
            str(row['id']) for table in tables for row in changed.get(table, ()) if 'id' in row
        }
        if not pending:
            continue
        seen: set[str] = set()
        actuals: dict[str, str | None] = {}

        def actual(id_: str) -> str | None:
            """Resolve an ID in the same business tree.

            :param id_: Canonical physical ID.
            :returns: Actual type, or None when the target is absent.
            :raises ValueError: If an identity is duplicated or has an invalid type.
            """
            if id_ not in actuals:
                rows = select(table_name(stored.root), {'id': id_})
                if len(rows) > 1:
                    raise ValueError('duplicate identity record')
                found = str(rows[0]['entity_type']) if rows else None
                if found is not None and (
                    found not in layout.entities or layout.roots[found] != stored.root
                ):
                    raise ValueError('invalid actual entity type')
                actuals[id_] = found
            return actuals[id_]

        groups: dict[tuple[tuple[tuple[str, str], ...], str], list[tuple[str, str, str, str]]] = {}
        while pending:
            owner = pending.pop()
            if owner in seen:
                continue
            seen.add(owner)
            owner_type = actual(owner)
            if owner_type is None:
                continue
            implementation = field(owner_type, identity)
            if implementation is None:
                continue
            keys: dict[str, object] = {'id': owner, **dict(stored.key_values)}
            for row in select(stored.table, keys):
                value = read(stored, implementation, row)
                if isinstance(value, NonNormalResult):
                    if value.status == Status.ERROR:
                        raise ValueError(value.message)
                    continue
                refs = (
                    cast(frozenset[object], value.value)
                    if isinstance(value.value, frozenset)
                    else (value.value,)
                )
                for ref in refs:
                    if (
                        not isinstance(ref, EntityRef)
                        or layout.roots.get(ref.target) != stored.root
                    ):
                        continue
                    target = str(ref.id)
                    target_type = actual(target)
                    ancestor = target_type
                    while ancestor in layout.entities and ancestor != ref.target:
                        ancestor = layout.entities[ancestor].parent
                    if ancestor != ref.target:
                        continue
                    dimensions = tuple((d.key, str(row['dim_' + d.key])) for d in stored.dimensions)
                    snapshot = str(row['date']) if implementation.category == 'snapshot' else ''
                    start, end = (
                        (str(row['valid_from']), str(row['valid_to']))
                        if implementation.category == 'scd'
                        else ('', '')
                    )
                    groups.setdefault((dimensions, snapshot), []).append(
                        (owner, target, start, end)
                    )
                    pending.add(target)
        for (dimensions, snapshot), records in sorted(groups.items()):
            for start, end, edges in _periods(records, snapshot):
                for path in cycles(edges):
                    issues.append(
                        ValidationIssue(
                            identity, 'same-field reference cycle', path, dimensions, start, end
                        )
                    )
    return ValidationReport(tuple(issues))
