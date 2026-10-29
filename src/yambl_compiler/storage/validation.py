"""Check stored field values and coordinate-specific instance reference cycles."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from yambl_compiler.ir.model import Field
from yambl_compiler.storage.backends import Row, validate_row
from yambl_compiler.storage.records import (
    STATE_CODES,
    Layout,
    StoredField,
    actual_type,
    mapped_field,
    table_name,
)
from yambl_compiler.storage.results import (
    EntityRef,
    NonNormalResult,
    ReadResult,
    Status,
    Success,
)


@dataclass(frozen=True)
class ValidationIssue:
    """Describe one data error or reference-cycle witness.

    :param field: Logical field or physical table.
    :param message: Human-readable diagnostic.
    :param ids: Deterministic closed cycle path, or affected record IDs.
    :param dimensions: Canonical dimension key/value pairs.
    :param start: Snapshot date or inclusive SCD start, empty for static data.
    :param end: Exclusive SCD end, empty otherwise.
    """

    field: str
    message: str
    ids: tuple[str, ...] = ()
    dimensions: tuple[tuple[str, str], ...] = ()
    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class ValidationReport:
    """Return immutable data diagnostics.

    :param issues: All discovered data violations.
    """

    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        """Report whether the data passed validation.

        :returns: True when no issues were found.
        """
        return not self.issues


def cycles(edges: set[tuple[str, str]]) -> list[tuple[str, ...]]:
    """Find one closed path per cyclic strongly connected component.

    :param edges: Same-field instance reference edges.
    :returns: Deterministically ordered cycle witnesses, including self-loops.
    """
    graph: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
        reverse.setdefault(target, set()).add(source)
        reverse.setdefault(source, set())
    seen: set[str] = set()
    order: list[str] = []
    for root in sorted(graph):
        stack = [(root, False)]
        while stack:
            node, finished = stack.pop()
            if finished:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in sorted(graph[node], reverse=True))
    seen.clear()
    result: list[tuple[str, ...]] = []
    for root in reversed(order):
        if root in seen:
            continue
        component: set[str] = set()
        pending = [root]
        while pending:
            node = pending.pop()
            if node not in seen:
                seen.add(node)
                component.add(node)
                pending.extend(reverse[node])
        if len(component) == 1 and (root, root) not in edges:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        node = min(component)
        while node not in positions:
            positions[node] = len(path)
            path.append(node)
            node = min(graph[node] & component)
        result.append(tuple([*path[positions[node] :], node]))
    return sorted(result)


def validate_dictionaries(
    layout: Layout,
    select: Callable[[str, Mapping[str, object]], list[Row]],
    dictionaries: set[str],
) -> tuple[ValidationIssue, ...]:
    """Check immutable model entries against a supplied data snapshot.

    :param layout: Generated model contract.
    :param select: Row selection on the same snapshot being validated.
    :param dictionaries: Qualified dictionary identities to inspect.
    :returns: Missing, changed or duplicate entry diagnostics.
    """
    issues: list[ValidationIssue] = []
    for definition in layout.model.definitions:
        if definition.kind != 'decodes' or definition.identity not in dictionaries:
            continue
        identity = definition.identity
        headers = select('tb_decodes', {'decodes_id': identity})
        if len(headers) != 1 or headers[0].get('entries__state') != STATE_CODES['VALUE']:
            issues.append(
                ValidationIssue(identity, 'model dictionary missing or entries not VALUE')
            )
            continue
        rows = select('tb_decodes_entries', {'decodes_id': identity})
        entries = {str(row['code']): (str(row['decode']), str(row['ordering'])) for row in rows}
        if len(entries) != len(rows):
            issues.append(ValidationIssue(identity, 'duplicate dictionary code'))
        expected = {entry.code: (entry.decode, entry.ordering) for entry in definition.entries}
        for code, value in expected.items():
            if entries.get(code) != value:
                issues.append(ValidationIssue(identity, f'model dictionary entry changed: {code}'))
        if (
            definition.derived or any(c.target == 'builtins.flags' for c in definition.decorators)
        ) and entries.keys() != expected.keys():
            issues.append(ValidationIssue(identity, 'read-only dictionary entries changed'))
    return tuple(issues)


def validate_data(
    layout: Layout,
    data: dict[str, list[Row]],
    select: Callable[[str, str], Field | None],
    read: Callable[[StoredField, Field, Row], ReadResult[object]],
) -> Success[ValidationReport]:
    """Scan both backends explicitly without changing normal lazy-read behavior.

    :param layout: Generated storage contract.
    :param data: Complete physical row snapshot.
    :param select: Resolve the actual field implementation.
    :param read: Decode one physical field row.
    :returns: Data report for the supplied physical snapshot.
    """
    issues: list[ValidationIssue] = []
    issues.extend(
        validate_dictionaries(
            layout,
            lambda name, keys: [
                row for row in data[name] if all(row[key] == value for key, value in keys.items())
            ],
            {d.identity for d in layout.model.definitions if d.kind == 'decodes'},
        )
    )
    tables = {t.name: t for t in layout.tables}
    for table in layout.tables:
        for row in data[table.name]:
            try:
                validate_row(layout, table, row)
            except ValueError as error:
                issues.append(ValidationIssue(table.name, str(error), (str(row.get("id", "")),)))
        for parent in table.parents:
            keys = tables[parent].primary
            owners = {tuple(row[k] for k in keys) for row in data[parent]}
            for row in data[table.name]:
                if tuple(row[k] for k in keys) not in owners:
                    issues.append(
                        ValidationIssue(
                            table.name, 'missing collection owner', tuple(str(row[k]) for k in keys)
                        )
                    )
        if 'valid_to' in {column.name for column in table.columns}:
            overlap_groups: dict[tuple[object, ...], list[tuple[str, str]]] = {}
            for row in data[table.name]:
                key = tuple(row[column] for column in table.primary if column != 'valid_from')
                overlap_groups.setdefault(key, []).append(
                    (str(row['valid_from']), str(row['valid_to']))
                )
            for key, intervals in overlap_groups.items():
                ordered = sorted(intervals)
                if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
                    issues.append(
                        ValidationIssue(
                            table.name,
                            'overlapping SCD intervals',
                            tuple(str(value) for value in key),
                        )
                    )
    mappings: dict[str, dict[object, list[Row]]] = {}
    for name in ('tb_config_group_when', 'tb_config_group_then_set'):
        by_owner: dict[object, list[Row]] = {}
        for member in data[name]:
            by_owner.setdefault(member['config_group_id'], []).append(member)
        mappings[name] = by_owner
    for row in data['tb_config_group']:
        id_ = str(row['config_group_id'])
        if any(
            row[name + '__state'] != STATE_CODES['VALUE']
            for name in ('target_entity_type', 'priority', 'when', 'then_set')
        ):
            issues.append(
                ValidationIssue(
                    'tb_config_group',
                    'ConfigGroup requires target_entity_type, priority, when and then_set',
                    (id_,),
                )
            )
            continue
        target = str(row['target_entity_type'])
        if target not in layout.roots:
            issues.append(
                ValidationIssue(
                    'tb_config_group', 'ConfigGroup target must be a business entity', (id_,)
                )
            )
            continue
        for name in ('tb_config_group_when', 'tb_config_group_then_set'):
            for member in mappings[name].get(row['config_group_id'], ()):
                mapped = mapped_field(layout, str(member['field']))
                if mapped is None or mapped[1] != layout.roots[target]:
                    issues.append(
                        ValidationIssue(
                            name,
                            'ConfigGroup field is outside its target inheritance tree',
                            (id_,),
                        )
                    )
    groups: dict[tuple[str, tuple[tuple[str, str], ...], str], list[tuple[str, str, str, str]]] = {}
    for identity, stored in layout.fields.items():
        if (
            stored.root not in layout.entities
            or layout.entities[stored.root].source.origin == "builtin"
        ):
            continue
        identities = {
            str(r["id"]): actual_type(layout, stored.root, r) for r in data[table_name(stored.root)]
        }
        for row in data[stored.table]:
            if any(row[k] != v for k, v in stored.key_values):
                continue
            source = str(row["id"])
            actual = identities.get(source)
            if actual not in layout.entities or layout.roots[actual] != stored.root:
                issues.append(
                    ValidationIssue(identity, "missing or invalid owner entity", (source,))
                )
                continue
            field = select(actual, identity)
            if field is None:
                continue
            value = read(stored, field, row)
            if isinstance(value, NonNormalResult):
                if value.status == Status.ERROR:
                    issues.append(ValidationIssue(identity, value.message, (source,)))
                continue
            refs = (
                cast(frozenset[object], value.value)
                if isinstance(value.value, frozenset)
                else (value.value,)
            )
            for ref in refs:
                if (
                    not isinstance(ref, EntityRef)
                    or layout.roots[ref.target] != stored.root
                    or str(ref.id) not in identities
                ):
                    continue
                target = identities[str(ref.id)]
                while target in layout.entities and target != ref.target:
                    target = layout.entities[target].parent
                if target != ref.target:
                    continue
                dimensions = tuple((d.key, str(row["dim_" + d.key])) for d in stored.dimensions)
                snapshot = str(row["date"]) if field.category == "snapshot" else ""
                start, end = (
                    (str(row["valid_from"]), str(row["valid_to"]))
                    if field.category == "scd"
                    else ("", "")
                )
                groups.setdefault((identity, dimensions, snapshot), []).append(
                    (source, str(ref.id), start, end)
                )
    for (identity, dimensions, snapshot), records in sorted(groups.items()):
        boundaries = sorted({d for _, _, start, end in records for d in (start, end) if d})
        periods = list(zip(boundaries, boundaries[1:])) if boundaries else [(snapshot, "")]
        for start, end in periods:
            edges = {(a, b) for a, b, lo, hi in records if not lo or lo <= start < hi}
            for path in cycles(edges):
                issues.append(
                    ValidationIssue(
                        identity,
                        "same-field reference cycle",
                        path,
                        dimensions,
                        start,
                        end,
                    )
                )
    return Success(ValidationReport(tuple(issues)))
