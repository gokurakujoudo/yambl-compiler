"""Batch identity and field reads while retaining per-contribution outcomes."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from yambl_compiler.ir.model import Field
from yambl_compiler.storage.backends import Row, Selection
from yambl_compiler.storage.records import STATE_CODES, StoredField, actual_type, table_name
from yambl_compiler.storage.results import (
    EntityRef,
    NonNormalResult,
    ReadResult,
    Status,
    Success,
)
from yambl_compiler.storage.session import Session


@dataclass(frozen=True)
class FieldRequest:
    """Identify a typed point lookup without constructing an entity handle.

    :param owner: Resolved actual entity identity.
    :param identity: Logical field qualified name.
    :param dimensions: Explicit dimension values.
    :param when: Temporal point, absent for static/config fields.
    """

    owner: EntityRef[object]
    identity: str
    dimensions: tuple[tuple[str, object], ...] = ()
    when: date | None = None


class Batch:
    """Share a reader's pinned backend and cache normalized dependency requests."""

    def __init__(self, reader: Session) -> None:
        """Create a session-local batch facade.

        :param reader: Owning reader and validated storage contract.
        :returns: None.
        """
        self.reader = reader
        self.identities: dict[EntityRef[object], ReadResult[EntityRef[object]]] = {}
        self.points: dict[tuple[object, ...], ReadResult[object]] = {}
        self.full: dict[tuple[EntityRef[object], str], tuple[Row, ...]] = {}

    def entities(
        self,
        references: Sequence[EntityRef[object]],
        missing: Status = Status.ENTITY_NOT_FOUND,
    ) -> tuple[ReadResult[EntityRef[object]], ...]:
        """Resolve unique IDs in batches while preserving request multiplicity.

        :param references: Declared target types and IDs.
        :param missing: Status for absent targets at this API boundary.
        :returns: Actual entity references or located non-normal outcomes.
        """
        reader = self.reader
        resolved = dict(self.identities)
        groups: dict[str, list[EntityRef[object]]] = {}
        for ref in dict.fromkeys(references):
            if ref not in self.identities:
                root = reader.layout.roots[ref.target]
                groups.setdefault(root, []).append(ref)
        if groups:
            reader.ready()
        for root, refs in groups.items():
            builtin = reader.layout.entities[root].source.origin == "builtin"
            id_name = reader.layout.ids[root].name if builtin else "id"
            selections = [
                Selection(((id_name, reader.encode(reader.layout.ids[root].type, ref.id)),))
                for ref in refs
            ]
            columns = (id_name, "entity_type") if reader.layout.discriminators[root] else (id_name,)
            rows = reader.backend.rows_many(table_name(root), selections, columns)
            for ref, group in zip(refs, rows, strict=True):
                result: ReadResult[EntityRef[object]] = NonNormalResult(Status.ENTITY_NOT_FOUND)
                if len(group) > 1:
                    result = NonNormalResult(Status.ERROR, "duplicate identity record")
                elif group:
                    actual = actual_type(reader.layout, root, group[0])
                    if actual not in reader.layout.entities or reader.layout.roots[actual] != root:
                        result = NonNormalResult(Status.ERROR, "invalid actual entity type")
                    elif self.is_type(actual, ref.target):
                        result = Success(EntityRef(actual, ref.id))
                if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                    self.identities[ref] = result
                resolved[ref] = result
        return tuple(
            (
                NonNormalResult(missing, r.message, r.issues)
                if isinstance(r, NonNormalResult) and r.status == Status.ENTITY_NOT_FOUND
                else r
            )
            for ref in references
            for r in (resolved[ref],)
        )

    def is_type(self, actual: str, expected: str) -> bool:
        """Check an entity's actual type against a declared reference target.

        :param actual: Actual qualified type.
        :param expected: Required type.
        :returns: Whether the entity belongs to the required inheritance branch.
        """
        while actual != expected and actual in self.reader.layout.entities:
            actual = self.reader.layout.entities[actual].parent
        return actual == expected

    def implementation(self, actual: str, identity: str) -> Field | None:
        """Select the actual implementation without allocating a handle.

        :param actual: Actual entity type.
        :param identity: Logical field identity.
        :returns: Applicable declaration, or None.
        """
        entity = self.reader.layout.entities[actual]
        return next(
            (
                self.reader.implementations[e.implementation]
                for e in entity.effective
                if e.identity == identity
            ),
            None,
        )

    def selection(self, request: FieldRequest, stored: StoredField, field: Field) -> Selection:
        """Normalize a point request to exact physical keys and date bounds.

        :param request: Point lookup.
        :param stored: Shared physical mapping.
        :param field: Actual implementation.
        :returns: Parameterized backend selection.
        :raises ValueError: On invalid dimensions or temporal input.
        """
        dims = dict(request.dimensions)
        if len(dims) != len(request.dimensions) or set(dims) != {d.key for d in stored.dimensions}:
            raise ValueError("all declared dimensions must be supplied exactly once")
        keys = [
            (
                "id",
                self.reader.encode(self.reader.layout.ids[stored.root].type, request.owner.id),
            ),
            *(("dim_" + d.key, self.reader.encode(d.type, dims[d.key])) for d in stored.dimensions),
            *stored.key_values,
        ]
        if field.category in ("snapshot", "scd"):
            if type(request.when) is not date:
                raise ValueError("time field requires a date")
            when = request.when.isoformat()
            return Selection(tuple(keys), field.category, when, when)
        return Selection(tuple(keys))

    def read(self, requests: Sequence[FieldRequest]) -> tuple[ReadResult[object], ...]:
        """Read unique physical points, grouping table and collection queries.

        :param requests: Point requests in contribution order.
        :returns: Detached field values in the same order.
        """
        reader = self.reader
        results: list[ReadResult[object]] = [NonNormalResult(Status.NOT_DEFINED) for _ in requests]
        groups: dict[
            str,
            dict[
                tuple[object, ...],
                tuple[FieldRequest, StoredField, Field, Selection, list[int]],
            ],
        ] = {}
        for i, request in enumerate(requests):
            field = self.implementation(request.owner.target, request.identity)
            if field is None:
                continue
            if field.category == "id":
                results[i] = Success(request.owner.id)
                continue
            if field.builtin:
                results[i] = Success(
                    request.owner.target
                    if field.name == "entity_type"
                    else reader.layout.roots[request.owner.target]
                )
                continue
            stored = reader.layout.fields[request.identity]
            selection = self.selection(request, stored, field)
            key: tuple[object, ...] = (
                request.owner.target,
                request.identity,
                selection,
            )
            if key in self.points:
                results[i] = self.points[key]
                continue
            complete = self.full.get((request.owner, request.identity))
            if complete is not None:
                cached_rows = [
                    dict(row) for row in complete if all(row[k] == v for k, v in selection.keys)
                ]
                results[i] = reader.point(stored, field, cached_rows, request.when)
                continue
            group = groups.setdefault(stored.table, {})
            if key not in group:
                group[key] = (request, stored, field, selection, [])
            group[key][-1].append(i)
        if groups:
            reader.ready()
        for table, group in groups.items():
            entries = list(group.items())
            columns = {"id"}
            for _, (_, stored, field, _, _) in entries:
                columns.add(stored.state)
                if stored.value:
                    columns.add(stored.value)
                columns.update(k for k, _ in stored.key_values)
                columns.update("dim_" + d.key for d in stored.dimensions)
                if field.category == "snapshot":
                    columns.add("date")
                elif field.category == "scd":
                    columns.update(("valid_from", "valid_to"))
            rows = reader.backend.rows_many(
                table, [entry[1][3] for entry in entries], tuple(sorted(columns))
            )
            self.collections(
                [
                    (stored, field, row)
                    for (_, (_, stored, field, _, _)), row_group in zip(entries, rows, strict=True)
                    for row in row_group
                ]
            )
            for (key, (request, stored, field, _, positions)), matching in zip(
                entries, rows, strict=True
            ):
                result = reader.point(stored, field, matching, request.when)
                if not isinstance(result, NonNormalResult) or result.status != Status.ERROR:
                    self.points[key] = result
                for position in positions:
                    results[position] = result
        return tuple(results)

    def collections(self, records: Sequence[tuple[StoredField, Field, Row]]) -> None:
        """Prefetch collection members once per table rather than once per row.

        :param records: Point or candidate header records.
        :returns: None; attaches decoded collection outcomes to detached headers.
        """
        groups: dict[str, list[tuple[StoredField, Field, Row, Selection]]] = {}
        for stored, field, row in records:
            if not stored.members or row[stored.state] != STATE_CODES['VALUE']:
                continue
            keys = [
                "id",
                *("dim_" + d.key for d in stored.dimensions),
                *(k for k, _ in stored.key_values),
            ]
            if field.category == "snapshot":
                keys.append("date")
            elif field.category == "scd":
                keys.append("valid_from")
            selection = Selection(tuple((key, row[key]) for key in keys))
            groups.setdefault(stored.members, []).append((stored, field, row, selection))
        for table, entries in groups.items():
            members = self.reader.backend.rows_many(
                table, [entry[3] for entry in entries], ("member_id",)
            )
            for (_, field, row, _), group in zip(entries, members, strict=True):
                try:
                    row["_result"] = Success(
                        frozenset(
                            self.reader.decode(field.type.arguments[0], member["member_id"])
                            for member in group
                        )
                    )
                except (ValueError, TypeError, KeyError) as error:
                    row["_result"] = NonNormalResult(Status.ERROR, str(error))
