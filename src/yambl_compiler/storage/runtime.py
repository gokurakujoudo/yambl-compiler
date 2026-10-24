"""Read generated SQLite layouts through explicit session-bound handles."""

import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar, Self, cast

from yambl_compiler.ir.application import load_ir
from yambl_compiler.ir.model import Field, Type
from yambl_compiler.storage.layout import (
    LAYOUT_REVISION,
    Layout,
    StoredField,
    build_layout,
    quote,
    table_name,
)
from yambl_compiler.storage.results import EntityRef, NonNormalResult, ReadResult, Status, Success


class EntityHandle:
    """Keep an explicit link to the owning read session."""

    def __init__(self, reader: Reader, identity: str, id_value: str | int) -> None:
        """Initialize a checked handle.

        :param reader: Owning session.
        :param identity: Actual entity type.
        :param id_value: Validated ID.
        :returns: None.
        """
        self.reader = reader
        self.entity_type = identity
        self.id = id_value


class Reader:
    """Own a SQLite read transaction and generated model bindings."""

    model_path: ClassVar[Path]
    entity_types: ClassVar[dict[str, type[EntityHandle]]] = {}
    code_types: ClassVar[dict[str, type[str]]] = {}

    def __init__(self, connection: sqlite3.Connection, layout: Layout) -> None:
        """Take ownership of an opened connection.

        :param connection: Read-only connection with a pinned transaction.
        :param layout: Validated bundled model layout.
        :returns: None.
        """
        self._connection = connection
        self._layout = layout
        self._closed = False
        self._implementations = {
            f.implementation: f for d in layout.model.definitions for f in d.fields
        }

    @classmethod
    def open(cls, path: Path) -> ReadResult[Self]:
        """Open a matching database in a consistent read transaction.

        :param path: Existing SQLite database.
        :returns: Session or ERROR; never creates a database.
        """
        connection: sqlite3.Connection | None = None
        try:
            layout = build_layout(load_ir(cls.model_path))
            connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN')
            metadata = dict(
                connection.execute('SELECT key, value FROM tb_yambl_metadata').fetchall()
            )
            if metadata != {
                'layout_revision': str(LAYOUT_REVISION),
                'model_digest': layout.model.model_digest,
            }:
                raise ValueError(
                    'database model or storage revision does not match the generated package'
                )
            return Success(cls(connection, layout))
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            if connection is not None:
                connection.close()
            return NonNormalResult(Status.ERROR, str(error))

    def close(self) -> ReadResult[None]:
        """Release this session; existing handles become unusable.

        :returns: Success or ERROR if closing fails.
        """
        try:
            self._connection.close()
            self._closed = True
            return Success(None)
        except sqlite3.Error as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _ready(self) -> None:
        """Check session lifetime before all operations.

        :returns: None.
        :raises ValueError: If the session has closed.
        """
        if self._closed:
            raise ValueError('reader is closed')

    def _decode(self, type_: Type, value: object) -> object:
        """Decode and validate one stored scalar.

        :param type_: Resolved semantic type.
        :param value: SQLite payload.
        :returns: Typed value or reference.
        :raises ValueError: If the stored value violates its contract.
        """
        if type_.kind == 'reference':
            id_type = self._layout.ids[self._layout.roots[type_.target]].type
            if type_.exact is not None and str(value) != type_.exact.text:
                raise ValueError('reference violates exact constraint')
            return EntityRef[EntityHandle](
                type_.target, cast(str | int, self._decode(id_type, value))
            )
        result: object
        if type_.name == 'bool':
            if type(value) is not int or value not in (0, 1):
                raise ValueError('invalid boolean payload')
            result = bool(value)
        else:
            if not isinstance(value, str):
                raise ValueError('expected TEXT payload')
            if type_.name in ('integer', 'snowflake_id'):
                result = int(value)
                if str(result) != value:
                    raise ValueError('noncanonical integer')
            elif type_.name == 'decimal':
                result = Decimal(value)
                if not result.is_finite():
                    raise ValueError('nonfinite decimal')
                params = {p.name: int(p.value) for p in type_.parameters}
                if params:
                    _, digits, exponent = result.as_tuple()
                    scale = max(0, -cast(int, exponent))
                    whole = max(0, len(digits) + cast(int, exponent))
                    if scale > params['scale'] or whole > params['precision'] - params['scale']:
                        raise ValueError('decimal exceeds declared precision or scale')
            elif type_.name == 'date':
                result = date.fromisoformat(value)
                if result.isoformat() != value:
                    raise ValueError('noncanonical date')
            elif type_.name == 'code':
                target = type_.arguments[0].target
                definition = next(
                    (d for d in self._layout.model.definitions if d.identity == target), None
                )
                if definition is not None and value not in {e.code for e in definition.entries}:
                    raise ValueError(f'unknown code for {target}')
                result = self.code_types[target](value)
            else:
                result = value
        if (
            type_.exact is not None
            and (str(result).lower() if isinstance(result, bool) else str(result))
            != type_.exact.text
        ):
            raise ValueError('value violates exact constraint')
        return result

    def _encode(self, type_: Type, value: object) -> str | int:
        """Validate a caller's key and encode its SQLite representation.

        :param type_: Declared ID or dimension type.
        :param value: Caller value.
        :returns: SQL parameter value.
        :raises ValueError: On invalid input type or constraint.
        """
        if type_.kind == 'reference':
            if not isinstance(value, EntityRef) or value.target != type_.target:
                raise ValueError('dimension requires a reference of the declared type')
            return self._encode(self._layout.ids[self._layout.roots[type_.target]].type, value.id)
        if type_.name == 'bool':
            valid = type(value) is bool
            encoded: str | int = int(cast(bool, value)) if valid else 0
        elif type_.name in ('integer', 'snowflake_id'):
            valid = type(value) is int
            encoded = str(value)
        elif type_.name == 'decimal':
            valid = isinstance(value, Decimal)
            encoded = str(value)
        elif type_.name == 'date':
            valid = type(value) is date
            encoded = cast(date, value).isoformat() if valid else ''
        elif type_.name == 'code':
            valid = isinstance(value, self.code_types[type_.arguments[0].target])
            encoded = str(value)
        else:
            valid = type(value) is str
            encoded = str(value)
        if not valid:
            raise ValueError(f'invalid input for {type_.name}')
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
            rows = self._connection.execute(
                f'SELECT entity_type FROM {quote(table_name(root))} WHERE id = ?',
                (encoded,),
            ).fetchall()
            if not rows:
                return NonNormalResult(Status.NO_ENTRY)
            actual = str(rows[0][0])
            if actual not in self._layout.entities or self._layout.roots[actual] != root:
                raise ValueError('invalid actual entity type')
            ancestor = actual
            while ancestor != entity and ancestor in self._layout.entities:
                ancestor = self._layout.entities[ancestor].parent
            if ancestor != entity:
                return NonNormalResult(Status.NO_ENTRY)
            return Success(self.entity_types[actual](self, actual, cast(str | int, id_value)))
        except (
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
            return NonNormalResult(Status.ERROR, 'ids must be a list')
        result = [self.get(entity, value) for value in ids]
        errors = [r for r in result if isinstance(r, NonNormalResult) and r.status == Status.ERROR]
        if errors:
            return errors[0]
        if self._closed:
            return NonNormalResult(Status.ERROR, 'reader is closed')
        return Success(result)

    def resolve[T](self, ref: EntityRef[T]) -> ReadResult[T]:
        """Resolve a typed reference explicitly.

        :param ref: Reference returned by this model's reads.
        :returns: Target handle or ENTITY_NOT_FOUND; other errors retain ERROR.
        """
        if not isinstance(cast(object, ref), EntityRef):
            return NonNormalResult(Status.ERROR, 'expected EntityRef')
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
        self._ready()
        if handle.reader is not self:
            raise ValueError('handle belongs to a different reader')
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
    ) -> list[sqlite3.Row]:
        """Fetch all records for one ID and exact dimension tuple.

        :param handle: Checked owner.
        :param stored: Physical field mapping.
        :param dimensions: Required named dimensions.
        :returns: Detached SQLite rows.
        :raises ValueError: If dimension keys or values are invalid.
        """
        if set(dimensions) != {d.key for d in stored.dimensions}:
            raise ValueError('all declared dimensions must be supplied exactly once')
        keys = [
            'id',
            *('dim_' + d.key for d in stored.dimensions),
            *(k for k, _ in stored.key_values),
        ]
        params = [
            self._encode(self._layout.ids[stored.root].type, handle.id),
            *(self._encode(d.type, dimensions[d.key]) for d in stored.dimensions),
            *(v for _, v in stored.key_values),
        ]
        query = f'SELECT * FROM {quote(stored.table)} WHERE ' + ' AND '.join(
            quote(k) + ' = ?' for k in keys
        )
        return cast(list[sqlite3.Row], self._connection.execute(query, params).fetchall())

    def _value(
        self, stored: StoredField, field: Field, row: sqlite3.Row | None
    ) -> ReadResult[object]:
        """Decode one record and apply only config missing-record defaults.

        :param stored: Physical field mapping.
        :param field: Actual implementation and constraints.
        :param row: Selected record or missing.
        :returns: Detached field result.
        """
        try:
            state = str(row[stored.state]) if row is not None else 'NO_ENTRY'
            if state == 'NO_ENTRY' and field.category == 'config' and field.default is not None:
                raw: object = (
                    int(field.default.text == 'true')
                    if field.type.name == 'bool'
                    else field.default.text
                )
                return Success(self._decode(field.type, raw), 'model_default')
            if state in ('NO_ENTRY', 'EMPTY', 'NOT_APPLICABLE'):
                return NonNormalResult(Status(state))
            if state != 'VALUE' or row is None:
                raise ValueError('invalid stored field state')
            if stored.members:
                keys = [
                    'id',
                    *('dim_' + d.key for d in stored.dimensions),
                    *(k for k, _ in stored.key_values),
                ]
                if field.category == 'snapshot':
                    keys.append('date')
                if field.category == 'scd':
                    keys.append('valid_from')
                query = f'SELECT member_id FROM {quote(stored.members)} WHERE ' + ' AND '.join(
                    quote(k) + ' = ?' for k in keys
                )
                members = self._connection.execute(query, tuple(row[k] for k in keys)).fetchall()
                return Success(
                    frozenset(
                        self._decode(field.type.arguments[0], cast(object, r[0])) for r in members
                    )
                )
            return Success(self._decode(field.type, cast(object, row[stored.value])))
        except (ValueError, TypeError, KeyError, IndexError, InvalidOperation) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def _point(
        self, stored: StoredField, field: Field, rows: list[sqlite3.Row], when: date | None
    ) -> ReadResult[object]:
        """Select the point record, rejecting ambiguous SCD periods.

        :param stored: Physical field.
        :param field: Actual implementation.
        :param rows: Records with fixed ID and dimensions.
        :param when: Time coordinate, absent for static/config.
        :returns: Point result.
        """
        selected = rows
        if field.category == 'snapshot':
            selected = [r for r in rows if date.fromisoformat(str(r['date'])) == when]
        elif field.category == 'scd':
            selected = [
                r
                for r in rows
                if date.fromisoformat(str(r['valid_from']))
                <= cast(date, when)
                < date.fromisoformat(str(r['valid_to']))
            ]
        if len(selected) > 1:
            return NonNormalResult(Status.ERROR, 'overlapping records')
        return self._value(stored, field, selected[0] if selected else None)

    def read(
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
            field = self._field(handle, identity)
            if field is None:
                return NonNormalResult(Status.NOT_DEFINED)
            if field.category in ('snapshot', 'scd') and type(when) is not date:
                raise ValueError('time field requires a date')
            stored = self._layout.fields[identity]
            return self._point(stored, field, self._rows(handle, stored, dimensions), when)
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def read_range(
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
            field = self._field(handle, identity)
            if field is None:
                return NonNormalResult(Status.NOT_DEFINED)
            if type(start) is not date or type(end) is not date or end < start:
                raise ValueError('range requires dates with start <= end')
            if field.category not in ('snapshot', 'scd'):
                raise ValueError('range requires a time field')
            stored = self._layout.fields[identity]
            rows = self._rows(handle, stored, dimensions)
            if start == end:
                return Success([])
            dates = {
                date.fromisoformat(str(row[key]))
                for row in rows
                for key in (
                    ('date',) if field.category == 'snapshot' else ('valid_from', 'valid_to')
                )
            }
            if field.category == 'scd':
                dates.add(start)
            return Success(
                [
                    (d, self._point(stored, field, rows, d))
                    for d in sorted(dates)
                    if start <= d < end
                ]
            )
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))
