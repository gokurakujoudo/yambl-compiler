"""Select available values using named dimensions and bounded temporal windows."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import cast

from yambl_compiler.ir.model import Dimension, Field
from yambl_compiler.storage.backends import Row, Selection
from yambl_compiler.storage.batch import Batch, FieldRequest
from yambl_compiler.storage.records import STATE_CODES, STATE_NAMES
from yambl_compiler.storage.results import NonNormalResult, ReadResult, Status, Success


@dataclass(frozen=True)
class BestRequest:
    """Carry a normalized automatic-selection request.

    :param point: Owner, field, explicit dimension values and requested date.
    :param hierarchies: Explicit selector IDs; declaration defaults apply when omitted.
    :param fallback_days: Effective per-instance look-back; zero means same date.
    """

    point: FieldRequest
    hierarchies: tuple[tuple[str, str], ...] = ()
    fallback_days: int = 0


class Available:
    """Batch hierarchy metadata and field candidates using a pinned session."""

    def __init__(self, batch: Batch) -> None:
        """Retain shared field decoding and hierarchy caches.

        :param batch: Owning batch context.
        :returns: None.
        """
        self.batch = batch
        self.hierarchies: dict[str, ReadResult[tuple[str, ...]]] = {}
        self.cache: dict[BestRequest, ReadResult[object]] = {}

    def dimensions(self, field: Field) -> tuple[Dimension, ...]:
        """Find original dimension declarations for an effective implementation.

        :param field: Actual field implementation.
        :returns: Attached dimensions in metadata order.
        """
        definitions = {d.identity: d for d in self.batch.reader.layout.model.definitions}
        owner = field.implementation.rsplit(".", 1)[0]
        visible: dict[str, Dimension] = {}
        while owner in definitions:
            definition = definitions[owner]
            for dimension in definition.dimensions:
                visible.setdefault(dimension.name, dimension)
            owner = definition.parent
        return tuple(
            visible[c.arguments[0].expression.name]
            for c in field.decorators
            if c.target == "builtins.dimension"
        )

    def hierarchy(self, names: Sequence[str]) -> dict[str, ReadResult[tuple[str, ...]]]:
        """Resolve ordered source codes for all unique hierarchy IDs together.

        :param names: Hierarchy IDs needed by this batch.
        :returns: Ordered values or native lookup outcomes for every ID.
        """
        missing = [name for name in dict.fromkeys(names) if name not in self.hierarchies]
        results = dict(self.hierarchies)
        if not missing:
            return results
        reader = self.batch.reader
        reader.ready()
        selections = [Selection((("hierarchy_id", name),)) for name in missing]
        headers = reader.backend.rows_many("tb_hierarchy", selections)
        entries = reader.backend.rows_many("tb_hierarchy_entries", selections)
        for name, header, members in zip(missing, headers, entries, strict=True):
            result: ReadResult[tuple[str, ...]]
            if not header:
                result = NonNormalResult(Status.ENTITY_NOT_FOUND, f"Hierarchy not found: {name}")
            elif len(header) != 1:
                result = NonNormalResult(Status.ERROR, "duplicate Hierarchy identity")
            elif header[0]["hierarchy_entries__state"] != STATE_CODES['VALUE']:
                state = STATE_NAMES.get(cast(int, header[0]["hierarchy_entries__state"]))
                result = (
                    NonNormalResult(Status(state))
                    if state in ("NO_ENTRY", "EMPTY", "NOT_APPLICABLE")
                    else NonNormalResult(Status.ERROR, "invalid Hierarchy state")
                )
            else:
                try:
                    orders = [int(str(row["ordering"])) for row in members]
                    values = [str(row["entry_value"]) for row in members]
                    if any(
                        str(order) != row["ordering"]
                        for order, row in zip(orders, members, strict=True)
                    ) or len(set(orders)) != len(orders):
                        raise ValueError(
                            "Hierarchy ordering must contain unique canonical integers"
                        )
                    result = Success(
                        tuple(value for _, value in sorted(zip(orders, values, strict=True)))
                    )
                except (ValueError, KeyError, TypeError) as error:
                    result = NonNormalResult(Status.ERROR, str(error))
            results[name] = result
            if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                self.hierarchies[name] = result
        return results

    def read(self, requests: Sequence[BestRequest]) -> tuple[ReadResult[object], ...]:
        """Choose date before source priority and stop decoding after a winner.

        :param requests: Independent bounded selection requests.
        :returns: Values, NO_ENTRY, NO_DATA or preserved errors in input order.
        """
        results: dict[BestRequest, ReadResult[object]] = {}
        pending = [r for r in dict.fromkeys(requests) if r not in self.cache]
        specs: dict[BestRequest, tuple[Field, tuple[Dimension, ...], Dimension | None, str]] = {}
        needed: list[str] = []
        for request in pending:
            field = self.batch.implementation(request.point.owner.target, request.point.identity)
            if field is None:
                results[request] = NonNormalResult(Status.NOT_DEFINED)
                continue
            if type(request.fallback_days) is not int or request.fallback_days < 0:
                results[request] = NonNormalResult(
                    Status.ERROR, "fallback_days requires a nonnegative integer"
                )
                continue
            dimensions = self.dimensions(field)
            explicit = dict(request.point.dimensions)
            automatic = [d for d in dimensions if d.value_name not in explicit]
            if len(automatic) > 1 or any(not d.hierarchy_name for d in automatic):
                results[request] = NonNormalResult(
                    Status.ERROR,
                    "provide every dimension value except at most one Hierarchy selection",
                )
                continue
            selected = automatic[0] if automatic else None
            hierarchy_id = ""
            if selected:
                hierarchy_id = dict(request.hierarchies).get(
                    selected.hierarchy_name,
                    (selected.hierarchy_default.value if selected.hierarchy_default else ""),
                )
                if not hierarchy_id:
                    results[request] = NonNormalResult(
                        Status.ERROR,
                        f"provide {selected.value_name} when no {selected.hierarchy_name} is available",
                    )
                    continue
                needed.append(hierarchy_id)
            specs[request] = (field, dimensions, selected, hierarchy_id)
        hierarchies = self.hierarchy(needed)
        candidates: dict[BestRequest, list[tuple[int, Row]]] = {r: [] for r in specs}
        groups: dict[str, list[tuple[BestRequest, int, Selection]]] = {}
        reader = self.batch.reader
        for request, (field, _, selected, hierarchy_id) in specs.items():
            hierarchy = hierarchies.get(hierarchy_id, Success(("",)))
            if isinstance(hierarchy, NonNormalResult):
                results[request] = hierarchy
                continue
            codes = hierarchy.value
            stored = reader.layout.fields[request.point.identity]
            for priority, code in enumerate(codes):
                dims = dict(request.point.dimensions)
                if selected is not None:
                    dims[selected.value_name] = reader.decode(selected.type, code)
                point = FieldRequest(
                    request.point.owner,
                    request.point.identity,
                    tuple(sorted(dims.items())),
                    request.point.when,
                )
                selection = self.batch.selection(point, stored, field)
                if field.category == "snapshot":
                    assert request.point.when is not None
                    ordinal = max(
                        date.min.toordinal(),
                        request.point.when.toordinal() - request.fallback_days,
                    )
                    selection = Selection(
                        selection.keys,
                        "snapshot",
                        date.fromordinal(ordinal).isoformat(),
                        request.point.when.isoformat(),
                    )
                complete = self.batch.full.get((request.point.owner, request.point.identity))
                if complete is not None:
                    for row in complete:
                        if not all(row[k] == v for k, v in selection.keys):
                            continue
                        if (
                            field.category == "snapshot"
                            and not selection.start <= str(row["date"]) <= selection.end
                        ):
                            continue
                        if field.category == "scd" and not str(
                            row["valid_from"]
                        ) <= selection.start < str(row["valid_to"]):
                            continue
                        candidates[request].append((priority, dict(row)))
                else:
                    groups.setdefault(stored.table, []).append((request, priority, selection))
        if groups:
            reader.ready()
        for table, entries in groups.items():
            unique = list(dict.fromkeys(selection for _, _, selection in entries))
            columns = {"id"}
            for request, _, _ in entries:
                stored = reader.layout.fields[request.point.identity]
                columns.add(stored.state)
                if stored.value:
                    columns.add(stored.value)
                columns.update(k for k, _ in stored.key_values)
                columns.update("dim_" + d.key for d in stored.dimensions)
                category = specs[request][0].category
                if category == "snapshot":
                    columns.add("date")
                elif category == "scd":
                    columns.update(("valid_from", "valid_to"))
            rows = dict(
                zip(
                    unique,
                    reader.backend.rows_many(table, unique, tuple(sorted(columns))),
                    strict=True,
                )
            )
            for request, priority, selection in entries:
                candidates[request].extend((priority, row) for row in rows[selection])
        winners: list[tuple[BestRequest, Row]] = []
        for request, records in candidates.items():
            if request in results:
                continue
            field = specs[request][0]
            stored = reader.layout.fields[request.point.identity]
            if field.category == "snapshot":
                records.sort(
                    key=lambda pair: (
                        -date.fromisoformat(str(pair[1]["date"])).toordinal(),
                        pair[0],
                    )
                )
            else:
                records.sort(key=lambda pair: pair[0])
            if field.category == "scd" and len({rank for rank, _ in records}) != len(records):
                results[request] = NonNormalResult(Status.ERROR, "overlapping SCD records")
                continue
            found = False
            for _, row in records:
                state = row[stored.state]
                if state == STATE_CODES['NO_ENTRY']:
                    continue
                found = True
                if state in (STATE_CODES['EMPTY'], STATE_CODES['NOT_APPLICABLE']):
                    continue
                winners.append((request, row))
                break
            else:
                results[request] = NonNormalResult(Status.NO_DATA if found else Status.NO_ENTRY)
        self.batch.collections(
            [
                (reader.layout.fields[r.point.identity], specs[r][0], row)
                for r, row in winners
                if "_result" not in row
            ]
        )
        for request, row in winners:
            results[request] = reader.value(
                reader.layout.fields[request.point.identity], specs[request][0], row
            )
        for request, result in results.items():
            if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                self.cache[request] = result
        return tuple(
            results.get(r, self.cache.get(r, NonNormalResult(Status.ERROR))) for r in requests
        )
