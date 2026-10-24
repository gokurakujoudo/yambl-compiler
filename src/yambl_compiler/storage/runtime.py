"""Read generated SQLite layouts through explicit session-bound handles."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from dataclasses import field as data_field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar, Self, cast

from yambl_compiler.ir.model import Field, Type
from yambl_compiler.storage.backends import Backend, FileBackend, Row, SQLiteBackend
from yambl_compiler.storage.contract import load_contract
from yambl_compiler.storage.records import (
    Layout,
    StoredField,
    table_name,
)
from yambl_compiler.storage.results import (
    EntityRef,
    NonNormalResult,
    ReadResult,
    Status,
    Success,
)
from yambl_compiler.storage.validation import ValidationReport


def has_error(value: object) -> bool:
    """Find errors nested in field results, records or interval sequences.

    :param value: Detached result data.
    :returns: Whether caching would retain an ERROR.
    """
    if isinstance(value, NonNormalResult):
        return value.status == Status.ERROR
    if isinstance(value, Success):
        return has_error(cast(Success[object], value).value)
    if isinstance(value, (tuple, list)):
        return any(has_error(item) for item in cast(tuple[object, ...] | list[object], value))
    if is_dataclass(value):
        return has_error(cast(object, getattr(value, "value", None)))
    return False


@dataclass(frozen=True, eq=False)
class EntityHandle:
    """Retain immutable identity and privately cached lazy field data.

    :param reader: Owning read session.
    :param entity_type: Actual qualified entity type.
    :param id: Typed entity ID.
    """

    reader: Reader = data_field(repr=False)
    entity_type: str
    id: str | int


class Reader:
    """Own a SQLite read transaction and generated model bindings."""

    model_path: ClassVar[Path]
    contract_digest: ClassVar[str]
    record_types: ClassVar[dict[str, Callable[..., object]]] = {}
    entity_types: ClassVar[dict[str, type[EntityHandle]]] = {}
    code_types: ClassVar[dict[str, type[str]]] = {}

    def __init__(self, backend: Backend, layout: Layout) -> None:
        """Take ownership of an opened connection.

        :param backend: Read-only storage session with pinned metadata.
        :param layout: Validated bundled model layout.
        :returns: None.
        """
        self._backend = backend
        self._layout = layout
        self._closed = False
        self._cache: dict[EntityHandle, dict[tuple[object, ...], ReadResult[object]]] = {}
        self._full: dict[EntityHandle, dict[str, tuple[Row, ...]]] = {}
        self._handles: dict[tuple[str, str | int], EntityHandle] = {}
        self._implementations = {
            f.implementation: f for d in layout.model.definitions for f in d.fields
        }

    @classmethod
    def open(cls, path: Path) -> ReadResult[Self]:
        """Open a matching database in a consistent read transaction.

        :param path: Existing SQLite database.
        :returns: Session or ERROR; never creates a database.
        """
        return cls.open_sqlite(path)

    @classmethod
    def open_sqlite(cls, path: Path) -> ReadResult[Self]:
        """Open an existing SQLite snapshot.

        :param path: Existing SQLite database.
        :returns: Reader or ERROR without creating a database.
        """
        return cls._open(path, SQLiteBackend)

    @classmethod
    def open_files(cls, path: Path) -> ReadResult[Self]:
        """Open an existing repository without reading its table contents.

        :param path: JSONL repository directory.
        :returns: Reader or ERROR.
        """
        return cls._open(path, FileBackend)

    @classmethod
    def _open(cls, path: Path, factory: Callable[[Path, Layout], Backend]) -> ReadResult[Self]:
        """Load the pinned contract and acquire a backend.

        :param path: Database or repository path.
        :param factory: Backend constructor.
        :returns: Reader or ERROR.
        """
        try:
            layout = load_contract(cls.model_path, cls.contract_digest)
            return Success(cls(factory(path, layout), layout))
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def close(self) -> ReadResult[None]:
        """Release this session; cached handle data remains available.

        :returns: Success or ERROR if closing fails.
        """
        try:
            self._backend.close()
            self._closed = True
            return Success(None)
        except (OSError, sqlite3.Error, ValueError) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _ready(self) -> None:
        """Check session lifetime before all operations.

        :returns: None.
        :raises ValueError: If the session has closed.
        """
        if self._closed:
            raise ValueError("reader is closed")

    def _cached(
        self,
        handle: EntityHandle,
        identity: str,
        dimensions: dict[str, object],
        coordinates: tuple[object, ...],
        operation: Callable[[], ReadResult[object]],
    ) -> ReadResult[object]:
        """Cache successful and deterministic field outcomes by typed coordinates.

        :param handle: Owning immutable entity.
        :param identity: Logical field.
        :param dimensions: Explicit dimension values.
        :param coordinates: Query kind and time arguments.
        :param operation: Query used on a cache miss.
        :returns: A cached or newly read result; ERROR is never cached.
        """
        try:
            if self._field(handle, identity) is None:
                return NonNormalResult(Status.NOT_DEFINED)
            stored = self._layout.fields[identity]
            if set(dimensions) != {d.key for d in stored.dimensions}:
                raise ValueError("all declared dimensions must be supplied exactly once")
            key = (
                identity,
                *coordinates,
                *(self._encode(d.type, dimensions[d.key]) for d in stored.dimensions),
            )
            if key not in self._cache.setdefault(handle, {}):
                result = operation()
                if has_error(result):
                    return result
                self._cache.setdefault(handle, {})[key] = result
            result = self._cache.setdefault(handle, {})[key]
            if isinstance(result, Success):
                payload = result.value
                if isinstance(payload, list):
                    return Success(list(cast(list[object], payload)), result.origin)
            return result
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def read(
        self,
        handle: EntityHandle,
        identity: str,
        dimensions: dict[str, object],
        when: date | None = None,
    ) -> ReadResult[object]:
        """Read a point, reusing an exact query or complete property snapshot.

        :param handle: Owner entity.
        :param identity: Logical field.
        :param dimensions: Explicit dimensions.
        :param when: Date for time fields.
        :returns: Field result, including defaults and non-normal states.
        """
        return self._cached(
            handle,
            identity,
            dimensions,
            ("point", type(when), when),
            lambda: self._read(handle, identity, dimensions, when),
        )

    def read_range(
        self,
        handle: EntityHandle,
        identity: str,
        dimensions: dict[str, object],
        start: date,
        end: date,
    ) -> ReadResult[list[tuple[date, ReadResult[object]]]]:
        """Read an interval and detach the returned list from its cache.

        :param handle: Owner entity.
        :param identity: Logical time field.
        :param dimensions: Explicit dimensions.
        :param start: Inclusive start.
        :param end: Exclusive end.
        :returns: Ordered point results or an outer ERROR.
        """
        return cast(
            ReadResult[list[tuple[date, ReadResult[object]]]],
            self._cached(
                handle,
                identity,
                dimensions,
                ("range", type(start), start, type(end), end),
                lambda: cast(
                    ReadResult[object],
                    self._read_range(handle, identity, dimensions, start, end),
                ),
            ),
        )

    def read_all(self, handle: EntityHandle, identity: str) -> ReadResult[object]:
        """Read every coordinate of one property into an immutable snapshot.

        :param handle: Owner entity.
        :param identity: Logical field.
        :returns: Scalar result or a tuple of generated typed records.
        """
        try:
            field = self._field(handle, identity)
            if field is None:
                return NonNormalResult(Status.NOT_DEFINED)
            stored = self._layout.fields[identity]
            if field.category in ("static", "config") and not stored.dimensions:
                return self.read(handle, identity, {})
            key = (identity, "all")
            if key in self._cache.setdefault(handle, {}):
                return self._cache.setdefault(handle, {})[key]
            self._ready()
            rows = self._backend.rows(
                stored.table,
                {
                    "id": self._encode(self._layout.ids[stored.root].type, handle.id),
                    **dict(stored.key_values),
                },
            )
            columns = [
                *("dim_" + d.key for d in stored.dimensions),
                *(
                    ("date",)
                    if field.category == "snapshot"
                    else ("valid_from", "valid_to") if field.category == "scd" else ()
                ),
            ]
            rows.sort(key=lambda row: tuple(str(row[c]) for c in columns))
            seen: set[tuple[object, ...]] = set()
            records: list[object] = []
            for row in rows:
                args = {d.key: self._decode(d.type, row["dim_" + d.key]) for d in stored.dimensions}
                for name in columns[len(stored.dimensions) :]:
                    raw = str(row[name])
                    parsed = date.fromisoformat(raw)
                    if parsed.isoformat() != raw:
                        raise ValueError("invalid time coordinate")
                    time_key = (
                        next(
                            (
                                b.key
                                for c in field.decorators
                                if c.target == "builtins.temporal"
                                for b in c.bound
                            ),
                            "as_of_date",
                        )
                        if name == "date"
                        else name
                    )
                    args[time_key] = parsed
                if (
                    field.category == "scd"
                    and not "1900-01-01"
                    <= str(row["valid_from"])
                    < str(row["valid_to"])
                    <= "2999-12-31"
                ):
                    raise ValueError("invalid SCD interval")
                coordinate = tuple(row[c] for c in columns)
                if coordinate in seen:
                    raise ValueError("duplicate property coordinate")
                seen.add(coordinate)
                value = self._value(stored, field, row)
                row["_result"] = value
                records.append(self.record_types[identity](**args, value=value))
            result = Success[object](tuple(records))
            if not has_error(result):
                self._cache.setdefault(handle, {})[key] = result
                self._full.setdefault(handle, {})[identity] = tuple(rows)
            return result
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def validate(self) -> ReadResult[ValidationReport]:
        """Explicitly validate all stored data and same-field reference cycles.

        :returns: Immutable report or ERROR on repository access failure.
        """
        from yambl_compiler.storage.validation import validate_data

        try:
            self._ready()
            data = {t.name: self._backend.rows(t.name, {}) for t in self._layout.tables}
            return validate_data(
                self._layout,
                data,
                lambda actual, identity: self._field(
                    self.entity_types[actual](self, actual, ""), identity
                ),
                self._value,
            )
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _decode(self, type_: Type, value: object) -> object:
        """Decode and validate one stored scalar.

        :param type_: Resolved semantic type.
        :param value: SQLite payload.
        :returns: Typed value or reference.
        :raises ValueError: If the stored value violates its contract.
        """
        if type_.kind == "reference":
            id_type = self._layout.ids[self._layout.roots[type_.target]].type
            if type_.exact is not None and str(value) != type_.exact.text:
                raise ValueError("reference violates exact constraint")
            return EntityRef[EntityHandle](
                type_.target, cast(str | int, self._decode(id_type, value))
            )
        result: object
        if type_.name == "bool":
            if type(value) is not int or value not in (0, 1):
                raise ValueError("invalid boolean payload")
            result = bool(value)
        else:
            if not isinstance(value, str):
                raise ValueError("expected TEXT payload")
            if type_.name in ("integer", "snowflake_id"):
                result = int(value)
                if str(result) != value:
                    raise ValueError("noncanonical integer")
            elif type_.name == "decimal":
                result = Decimal(value)
                if not result.is_finite():
                    raise ValueError("nonfinite decimal")
                params = {p.name: int(p.value) for p in type_.parameters}
                if params:
                    _, digits, exponent = result.as_tuple()
                    scale = max(0, -cast(int, exponent))
                    whole = max(0, len(digits) + cast(int, exponent))
                    if scale > params["scale"] or whole > params["precision"] - params["scale"]:
                        raise ValueError("decimal exceeds declared precision or scale")
            elif type_.name == "date":
                result = date.fromisoformat(value)
                if result.isoformat() != value:
                    raise ValueError("noncanonical date")
            elif type_.name == "code":
                target = type_.arguments[0].target
                definition = next(
                    (d for d in self._layout.model.definitions if d.identity == target),
                    None,
                )
                if definition is not None and value not in {e.code for e in definition.entries}:
                    raise ValueError(f"unknown code for {target}")
                result = self.code_types[target](value)
            else:
                result = value
        if (
            type_.exact is not None
            and (str(result).lower() if isinstance(result, bool) else str(result))
            != type_.exact.text
        ):
            raise ValueError("value violates exact constraint")
        return result

    def _encode(self, type_: Type, value: object) -> str | int:
        """Validate a caller's key and encode its SQLite representation.

        :param type_: Declared ID or dimension type.
        :param value: Caller value.
        :returns: SQL parameter value.
        :raises ValueError: On invalid input type or constraint.
        """
        if type_.kind == "reference":
            if not isinstance(value, EntityRef) or value.target != type_.target:
                raise ValueError("dimension requires a reference of the declared type")
            return self._encode(self._layout.ids[self._layout.roots[type_.target]].type, value.id)
        if type_.name == "bool":
            valid = type(value) is bool
            encoded: str | int = int(cast(bool, value)) if valid else 0
        elif type_.name in ("integer", "snowflake_id"):
            valid = type(value) is int
            encoded = str(value)
        elif type_.name == "decimal":
            valid = isinstance(value, Decimal)
            encoded = str(value)
        elif type_.name == "date":
            valid = type(value) is date
            encoded = cast(date, value).isoformat() if valid else ""
        elif type_.name == "code":
            valid = isinstance(value, self.code_types[type_.arguments[0].target])
            encoded = str(value)
        else:
            valid = type(value) is str
            encoded = str(value)
        if not valid:
            raise ValueError(f"invalid input for {type_.name}")
        self._decode(type_, encoded)
        return encoded

    def get(self, entity: str, id_value: object) -> ReadResult[EntityHandle]:
        """Fetch one identity and return its actual subtype handle.

        :param entity: Requested qualified entity type.
        :param id_value: Entity ID.
        :returns: Handle, NO_ENTRY or ERROR.
        """
        try:
            self._ready()
            root = self._layout.roots[entity]
            encoded = self._encode(self._layout.ids[root].type, id_value)
            rows = self._backend.rows(table_name(root), {"id": encoded})
            if not rows:
                return NonNormalResult(Status.NO_ENTRY)
            actual = str(rows[0]["entity_type"])
            if actual not in self._layout.entities or self._layout.roots[actual] != root:
                raise ValueError("invalid actual entity type")
            ancestor = actual
            while ancestor != entity and ancestor in self._layout.entities:
                ancestor = self._layout.entities[ancestor].parent
            if ancestor != entity:
                return NonNormalResult(Status.NO_ENTRY)
            key = (actual, cast(str | int, id_value))
            if key not in self._handles:
                self._handles[key] = self.entity_types[actual](self, *key)
            return Success(self._handles[key])
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def get_many(
        self, entity: str, ids: list[str | int]
    ) -> ReadResult[list[ReadResult[EntityHandle]]]:
        """Read IDs in input order, preserving duplicate positions.

        :param entity: Requested type.
        :param ids: Ordered IDs.
        :returns: Ordered outcomes, or a whole-query ERROR.
        """
        if not isinstance(cast(object, ids), list):
            return NonNormalResult(Status.ERROR, "ids must be a list")
        result = [self.get(entity, value) for value in ids]
        errors = [r for r in result if isinstance(r, NonNormalResult) and r.status == Status.ERROR]
        if errors:
            return errors[0]
        if self._closed:
            return NonNormalResult(Status.ERROR, "reader is closed")
        return Success(result)

    def resolve[T](self, ref: EntityRef[T]) -> ReadResult[T]:
        """Resolve a typed reference explicitly.

        :param ref: Reference returned by this model's reads.
        :returns: Target handle or ENTITY_NOT_FOUND; other errors retain ERROR.
        """
        if not isinstance(cast(object, ref), EntityRef):
            return NonNormalResult(Status.ERROR, "expected EntityRef")
        result = self.get(ref.target, ref.id)
        if isinstance(result, NonNormalResult) and result.status == Status.NO_ENTRY:
            return NonNormalResult(Status.ENTITY_NOT_FOUND)
        return cast(ReadResult[T], result)

    def _field(self, handle: EntityHandle, identity: str) -> Field | None:
        """Select the actual implementation for a logical field.

        :param handle: Actual entity handle.
        :param identity: Logical field identity.
        :returns: Applicable implementation, or None.
        """
        if handle.reader is not self:
            raise ValueError("handle belongs to a different reader")
        entity = self._layout.entities[handle.entity_type]
        return next(
            (
                self._implementations[e.implementation]
                for e in entity.effective
                if e.identity == identity
            ),
            None,
        )

    def _rows(
        self, handle: EntityHandle, stored: StoredField, dimensions: dict[str, object]
    ) -> list[Row]:
        """Fetch all records for one ID and exact dimension tuple.

        :param handle: Checked owner.
        :param stored: Physical field mapping.
        :param dimensions: Required named dimensions.
        :returns: Detached SQLite rows.
        :raises ValueError: If dimension keys or values are invalid.
        """
        keys = [
            "id",
            *("dim_" + d.key for d in stored.dimensions),
            *(k for k, _ in stored.key_values),
        ]
        params = [
            self._encode(self._layout.ids[stored.root].type, handle.id),
            *(self._encode(d.type, dimensions[d.key]) for d in stored.dimensions),
            *(v for _, v in stored.key_values),
        ]
        filters = dict(zip(keys, params, strict=True))
        if stored.field.identity in self._full.setdefault(handle, {}):
            return [
                dict(row)
                for row in self._full.setdefault(handle, {})[stored.field.identity]
                if all(row[k] == v for k, v in filters.items())
            ]
        self._ready()
        return self._backend.rows(stored.table, filters)

    def _value(self, stored: StoredField, field: Field, row: Row | None) -> ReadResult[object]:
        """Decode one record and apply only config missing-record defaults.

        :param stored: Physical field mapping.
        :param field: Actual implementation and constraints.
        :param row: Selected record or missing.
        :returns: Detached field result.
        """
        try:
            if row is not None and "_result" in row:
                return cast(ReadResult[object], row["_result"])
            state = str(row[stored.state]) if row is not None else "NO_ENTRY"
            if state == "NO_ENTRY" and field.category == "config" and field.default is not None:
                raw: object = (
                    int(field.default.text == "true")
                    if field.type.name == "bool"
                    else field.default.text
                )
                return Success(self._decode(field.type, raw), "model_default")
            if state in ("NO_ENTRY", "EMPTY", "NOT_APPLICABLE"):
                return NonNormalResult(Status(state))
            if state != "VALUE" or row is None:
                raise ValueError("invalid stored field state")
            if stored.members:
                keys = [
                    "id",
                    *("dim_" + d.key for d in stored.dimensions),
                    *(k for k, _ in stored.key_values),
                ]
                if field.category == "snapshot":
                    keys.append("date")
                if field.category == "scd":
                    keys.append("valid_from")
                members = self._backend.rows(stored.members, {k: row[k] for k in keys})
                return Success(
                    frozenset(
                        self._decode(field.type.arguments[0], r["member_id"]) for r in members
                    )
                )
            return Success(self._decode(field.type, row[stored.value]))
        except (ValueError, TypeError, KeyError, IndexError, InvalidOperation) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _point(
        self, stored: StoredField, field: Field, rows: list[Row], when: date | None
    ) -> ReadResult[object]:
        """Select the point record, rejecting ambiguous SCD periods.

        :param stored: Physical field.
        :param field: Actual implementation.
        :param rows: Records with fixed ID and dimensions.
        :param when: Time coordinate, absent for static/config.
        :returns: Point result.
        """
        selected = rows
        if field.category == "snapshot":
            selected = [r for r in rows if date.fromisoformat(str(r["date"])) == when]
        elif field.category == "scd":
            selected = [
                r
                for r in rows
                if date.fromisoformat(str(r["valid_from"]))
                <= cast(date, when)
                < date.fromisoformat(str(r["valid_to"]))
            ]
        if len(selected) > 1:
            return NonNormalResult(Status.ERROR, "overlapping records")
        return self._value(stored, field, selected[0] if selected else None)

    def _read(
        self,
        handle: EntityHandle,
        identity: str,
        dimensions: dict[str, object],
        when: date | None = None,
    ) -> ReadResult[object]:
        """Read a scalar, collection or exact time point.

        :param handle: Owner handle.
        :param identity: Logical field.
        :param dimensions: Explicit dimension values.
        :param when: Date for snapshot/SCD fields.
        :returns: Field outcome; expected failures are ERROR values.
        """
        try:
            field = cast(Field, self._field(handle, identity))
            if field.category in ("snapshot", "scd") and type(when) is not date:
                raise ValueError("time field requires a date")
            stored = self._layout.fields[identity]
            return self._point(stored, field, self._rows(handle, stored, dimensions), when)
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _read_range(
        self,
        handle: EntityHandle,
        identity: str,
        dimensions: dict[str, object],
        start: date,
        end: date,
    ) -> ReadResult[list[tuple[date, ReadResult[object]]]]:
        """Read sparse snapshots or SCD start state and changes.

        :param handle: Owner handle.
        :param identity: Logical time field.
        :param dimensions: Explicit dimensions.
        :param start: Inclusive date.
        :param end: Exclusive date.
        :returns: Complete ordered results or outer ERROR, never partial output.
        """
        try:
            field = cast(Field, self._field(handle, identity))
            if type(start) is not date or type(end) is not date or end < start:
                raise ValueError("range requires dates with start <= end")
            if field.category not in ("snapshot", "scd"):
                raise ValueError("range requires a time field")
            stored = self._layout.fields[identity]
            rows = self._rows(handle, stored, dimensions)
            if start == end:
                return Success([])
            dates = {
                date.fromisoformat(str(row[key]))
                for row in rows
                for key in (
                    ("date",) if field.category == "snapshot" else ("valid_from", "valid_to")
                )
            }
            if field.category == "scd":
                dates.add(start)
            return Success(
                [
                    (d, self._point(stored, field, rows, d))
                    for d in sorted(dates)
                    if start <= d < end
                ]
            )
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))
