"""Read generated SQLite layouts through explicit session-bound handles."""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from dataclasses import field as data_field
from datetime import date
from decimal import InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Protocol, Self, cast

from yambl_compiler.ir.model import Field, LogicalField, Type
from yambl_compiler.storage.backends import (
    Backend,
    FileBackend,
    Row,
    Selection,
    SQLiteBackend,
)
from yambl_compiler.storage.batch import Batch, FieldRequest
from yambl_compiler.storage.computing import Computing, Frame, Program, Vector
from yambl_compiler.storage.configuration import Configuration
from yambl_compiler.storage.contract import decode, load_contract, loads
from yambl_compiler.storage.records import (
    STATE_CODES,
    STATE_NAMES,
    Layout,
    StoredField,
    actual_type,
    table_name,
)
from yambl_compiler.storage.results import (
    EntityRef,
    NonNormalResult,
    ReadResult,
    Status,
    Success,
)
from yambl_compiler.storage.scalars import (
    ScalarRule,
    decode_scalar,
    encode_input,
    reference_matches,
)
from yambl_compiler.storage.session import Session
from yambl_compiler.storage.validation import (
    ValidationIssue,
    ValidationReport,
    validate_dictionaries,
)


class CandidateValidator(Protocol):
    """Validate changed business fields using the owning reader's decoders."""

    def validate_business(
        self,
        field: Callable[[str, str], Field | None],
        read: Callable[[StoredField, Field, Row], ReadResult[object]],
    ) -> None:
        """Check changed business data and references.

        :param field: Resolve an actual field implementation.
        :param read: Decode a candidate field value.
        :returns: None.
        """
        ...


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

    @property
    def root_entity_type(self) -> str:
        """Return the qualified inheritance root of this instance.

        :returns: The actual instance's root entity type.
        """
        return self.reader.root_type(self.entity_type)

    def read_entity_type(self) -> ReadResult[str]:
        """Return the actual qualified entity type as a field result.

        :returns: Actual entity type.
        """
        return Success(self.entity_type)

    def read_root_entity_type(self) -> ReadResult[str]:
        """Return the qualified inheritance root as a field result.

        :returns: Root entity type.
        """
        return Success(self.root_entity_type)


class Reader:
    """Own a SQLite read transaction and generated model bindings."""

    model_path: ClassVar[Path]
    contract_digest: ClassVar[str]
    record_types: ClassVar[dict[str, Callable[..., object]]] = {}
    entity_types: ClassVar[dict[str, type[EntityHandle]]] = {}
    code_types: ClassVar[dict[str, type[str]]] = {}
    calculation_programs: ClassVar[dict[str, Program]] = {}

    def root_type(self, entity_type: str) -> str:
        """Find the inheritance root of a known entity type.

        :param entity_type: Qualified entity type.
        :returns: Qualified root entity type.
        """
        return self._layout.roots[entity_type]

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
        self._dictionary_cache: dict[str, frozenset[str]] = {}
        self._model_identities = frozenset(d.identity for d in layout.model.definitions)
        self._handles: dict[tuple[str, str | int], EntityHandle] = {}
        self._implementations = {
            f.implementation: f for d in layout.model.definitions for f in d.fields
        }
        self._session = Session(
            layout,
            backend,
            self._implementations,
            self.code_types,
            self._ready,
            self._encode,
            self._decode,
            self._point,
            self._value,
            self._scalar_rule,
        )
        self._batch = Batch(self._session)
        self._configuration = Configuration(self._batch)
        self._computing = Computing(
            self._session, self._batch, self._configuration, self.calculation_programs
        )

    def calculate(
        self,
        handle: EntityHandle,
        identity: str,
        parameters: Mapping[str, object],
        best_effort: bool = False,
    ) -> ReadResult[object]:
        """Calculate one field using the same dependency batches as a many call.

        :param handle: Existing entity handle in this session.
        :param identity: Logical computed field identity.
        :param parameters: Explicit inferred parameter values.
        :param best_effort: Whether to retain valid contributions as PARTIAL.
        :returns: Computed payload or a native non-normal outcome.
        """
        if handle.reader is not self:
            return NonNormalResult(Status.ERROR, "handle belongs to a different reader")
        result = self.calculate_many(
            handle.entity_type, identity, ((handle.id, parameters, best_effort),)
        )
        return result.value[0] if isinstance(result, Success) else cast(ReadResult[object], result)

    def calculate_many(
        self,
        target: str,
        identity: str,
        requests: Sequence[tuple[str | int, Mapping[str, object], bool]],
    ) -> ReadResult[tuple[ReadResult[object], ...]]:
        """Execute a generated computation for an ordered request batch.

        :param target: Requested entity type.
        :param identity: Logical computed field identity.
        :param requests: IDs, independent parameters and best-effort choices.
        :returns: Ordered per-request results, or outer ERROR on backend failure.
        """
        try:
            lanes = tuple((i,) for i in range(len(requests)))
            source: Vector = {}
            refs: list[EntityRef[object]] = []
            valid: list[tuple[int, ...]] = []
            root = self._layout.roots[target]
            for lane, (id_value, _, mode) in zip(lanes, requests, strict=True):
                try:
                    self._encode(self._layout.ids[root].type, id_value)
                    if type(mode) is not bool:
                        raise ValueError("best_effort requires bool")
                    refs.append(EntityRef(target, id_value))
                    valid.append(lane)
                except (ValueError, TypeError) as error:
                    source[lane] = NonNormalResult(Status.ERROR, str(error))
            owners = self._batch.entities(refs, Status.NO_ENTRY)
            source.update(
                (lane, cast(ReadResult[object], owner))
                for lane, owner in zip(valid, owners, strict=True)
            )
            inputs: dict[str, Vector] = {}
            for i, (_, parameters, _) in enumerate(requests):
                for name, value in parameters.items():
                    inputs.setdefault(name, {})[(i,)] = Success(value)
            from yambl_compiler.storage.computing import UNSET

            for vector in inputs.values():
                for lane in lanes:
                    vector.setdefault(lane, Success(UNSET))
            modes = tuple(request[2] for request in requests)
            frame = Frame(lanes, source, inputs, modes)
            values = self._computing.compute(frame, source, identity, {})
            return Success(tuple(values[lane] for lane in lanes), "computation")
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            sqlite3.Error,
            InvalidOperation,
        ) as error:
            return NonNormalResult(Status.ERROR, str(error))

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

    def close(self) -> Success[None] | NonNormalResult:
        """Release this session; cached handle data remains available.

        :returns: Success or ERROR if closing fails.
        """
        try:
            self._backend.close()
            self._closed = True
            return Success(None)
        except (OSError, sqlite3.Error, ValueError) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def __enter__(self) -> Self:
        """Enter an open reader's resource scope.

        :returns: This reader.
        :raises ValueError: If the reader is closed.
        """
        self._ready()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the reader and expose a close failure without hiding a body error.

        :param exc_type: Body exception type, if the body failed.
        :param exc_value: Body exception, if the body failed.
        :param traceback: Body exception traceback, if the body failed.
        :returns: None; body exceptions continue to propagate.
        :raises RuntimeError: If closing fails after a successful body.
        """
        result = self.close()
        if not isinstance(result, Success):
            message = f'reader close failed: {result.message}'
            if exc_value is not None:
                exc_value.add_note(message)
            else:
                raise RuntimeError(message)

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
                    return Success(list(cast(list[object], payload)), result.origin, result.groups)
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
        try:
            field = self._field(handle, identity)
            if field is not None and field.builtin:
                if dimensions or when is not None:
                    raise ValueError("built-in type fields do not accept coordinates")
                request = FieldRequest(EntityRef(handle.entity_type, handle.id), identity)
                return self._batch.read((request,))[0]
        except (ValueError, KeyError) as error:
            return NonNormalResult(Status.ERROR, str(error))
        result = self._cached(
            handle,
            identity,
            dimensions,
            ("point", type(when), when),
            lambda: self._read(handle, identity, dimensions, when),
        )
        if not has_error(result) and identity in self._layout.fields:
            field = self._batch.implementation(handle.entity_type, identity)
            if field is not None:
                request = FieldRequest(
                    EntityRef(handle.entity_type, handle.id),
                    identity,
                    tuple(dimensions.items()),
                    when,
                )
                selection = self._batch.selection(request, self._layout.fields[identity], field)
                self._batch.points[(handle.entity_type, identity, selection)] = result
        return result

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
            if not stored.table:
                return self._reverse_all(handle, identity, field, stored)
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
                self._batch.full[(EntityRef(handle.entity_type, handle.id), identity)] = tuple(rows)
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
            if isinstance(self._backend, FileBackend):
                self._backend.validate_indexes()
            data = {t.name: self._backend.rows(t.name, {}) for t in self._layout.tables}
            report = validate_data(
                self._layout,
                data,
                lambda actual, identity: self._field(
                    self.entity_types[actual](self, actual, ""), identity
                ),
                self._value,
            )
            dictionaries = {
                d.identity for d in self._layout.model.definitions if d.kind == 'decodes'
            }
            dictionary_report = self.validate_dictionary_references(dictionaries)
            issues = tuple(dict.fromkeys((*report.value.issues, *dictionary_report.issues)))
            try:
                self._configuration.load()
            except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                return Success(
                    ValidationReport((*issues, ValidationIssue('tb_config_group', str(error))))
                )
            return Success(ValidationReport(issues))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            return NonNormalResult(Status.ERROR, str(error))

    def validate_candidate(self, candidate: CandidateValidator) -> None:
        """Validate changed business data using this reader's pinned snapshot.

        The candidate and reader must refer to the same candidate snapshot.

        :param candidate: Candidate with changed business records.
        :returns: None.
        :raises ValueError: If changed business data is invalid.
        """
        self._ready()
        candidate.validate_business(self._batch.implementation, self._value)

    def validate_dictionary_references(
        self, dictionaries: set[str], removed: dict[str, set[str]] | None = None
    ) -> ValidationReport:
        """Check selected dictionary contracts and their stored references.

        :param dictionaries: Local dictionary identities affected by the candidate.
        :param removed: Removed codes for mapping contracts widened to string.
        :returns: Violations in this reader's pinned snapshot.
        """
        self._ready()
        issues = list(validate_dictionaries(self._layout, self._backend.rows, dictionaries))
        logicals = {f.identity: f for f in self._layout.model.fields}
        owner_rows: dict[str, dict[str, str]] = {}
        for identity, stored in self._layout.fields.items():
            if (
                not stored.table
                or stored.root not in self._layout.entities
                or self._layout.entities[stored.root].source.origin != 'model'
            ):
                continue
            logical = logicals[identity]
            implementations = [self._implementations[name] for name in logical.implementations]
            dimensions = [
                d
                for d in stored.dimensions
                if d.type.name == 'code' and d.type.arguments[0].target in dictionaries
            ]
            has_code = any(
                f.type.name == 'code' and f.type.arguments[0].target in dictionaries
                for f in implementations
            )
            if not dimensions and not has_code:
                continue
            if stored.root not in owner_rows:
                owner_rows[stored.root] = {
                    str(row['id']): actual_type(self._layout, stored.root, row)
                    for row in self._backend.rows(table_name(stored.root), {})
                }
            for row in self._backend.rows(stored.table, dict(stored.key_values)):
                owner = str(row['id'])
                actual = owner_rows[stored.root].get(owner)
                if actual not in self._layout.entities:
                    continue
                field = self._batch.implementation(actual, identity)
                if field is None:
                    continue
                try:
                    for dimension in dimensions:
                        self._decode(dimension.type, row['dim_' + dimension.key])
                    if (
                        field.type.name == 'code'
                        and field.type.arguments[0].target in dictionaries
                        and row[stored.state] == STATE_CODES['VALUE']
                    ):
                        self._decode(field.type, row[stored.value])
                except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                    issues.append(ValidationIssue(identity, str(error), (owner,)))
        self._validate_dictionary_mappings(dictionaries, removed or {}, logicals, issues)
        return ValidationReport(tuple(issues))

    def _validate_dictionary_mappings(
        self,
        dictionaries: set[str],
        removed: dict[str, set[str]],
        logicals: dict[str, LogicalField],
        issues: list[ValidationIssue],
    ) -> None:
        """Check affected ConfigGroup mapping values using their field contracts.

        :param dictionaries: Local dictionaries being checked.
        :param removed: Removed codes by dictionary.
        :param logicals: Logical field contracts by identity.
        :param issues: Destination for located failures.
        :returns: None.
        """
        headers = {
            str(row['config_group_id']): row for row in self._backend.rows('tb_config_group', {})
        }
        for table in ('tb_config_group_when', 'tb_config_group_then_set'):
            for row in self._backend.rows(table, {}):
                identity = str(row['field'])
                logical = logicals.get(identity)
                if logical is None:
                    continue
                type_ = logical.type
                direct = type_.name == 'code' and type_.arguments[0].target in dictionaries
                potential = {
                    self._implementations[name].type.arguments[0].target
                    for name in logical.implementations
                    if self._implementations[name].type.name == 'code'
                    and self._implementations[name].type.arguments[0].target in removed
                }
                if not direct and not potential:
                    continue
                group = str(row['config_group_id'])
                try:
                    envelope = loads(str(row['value']))
                    if not isinstance(envelope, dict) or set(envelope) != {'type', 'value'}:
                        raise ValueError('invalid ConfigGroup typed value envelope')
                    if decode(envelope['type'], Type) != type_:
                        raise ValueError(
                            'ConfigGroup mapping value type disagrees with field contract'
                        )
                    if direct:
                        self._configuration.value(type_, envelope['value'])
                    else:
                        target = str(headers[group]['target_entity_type'])
                        for actual in logical.applicable:
                            if not self._batch.is_type(actual, target):
                                continue
                            implementation = self._batch.implementation(actual, identity)
                            if implementation is not None and implementation.type.name == 'code':
                                dictionary = implementation.type.arguments[0].target
                                if envelope['value'] in removed.get(dictionary, set()):
                                    raise ValueError(f'unknown code for {dictionary}')
                except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                    issues.append(ValidationIssue(table, str(error), (group,)))

    def _decode(self, type_: Type, value: object) -> object:
        """Decode and validate one stored scalar.

        :param type_: Resolved semantic type.
        :param value: SQLite payload.
        :returns: Typed value or reference.
        :raises ValueError: If the stored value violates its contract.
        """
        if type_.kind == "reference":
            id_type = self._id_type(type_.target)
            if not reference_matches(type_, value):
                raise ValueError("reference violates exact constraint")
            return EntityRef[EntityHandle](
                type_.target, cast(str | int, self._decode(id_type, value))
            )
        if type_.name == 'code' and isinstance(value, str):
            target = type_.arguments[0].target
            return decode_scalar(
                type_, value, self.code_types[target], self._dictionary_codes(target)
            )
        return decode_scalar(type_, value)

    def _scalar_rule(self, type_: Type) -> ScalarRule:
        """Freeze a field's validation dependencies for later detached use.

        :param type_: Validated scalar or reference contract.
        :returns: Rule containing no reader or backend references.
        :raises ValueError: If a required dictionary is invalid.
        """
        if type_.kind == 'reference':
            return ScalarRule(type_, reference_id=self._id_type(type_.target))
        if type_.name != 'code':
            return ScalarRule(type_)
        target = type_.arguments[0].target
        return ScalarRule(type_, self.code_types[target], self._dictionary_codes(target))

    def _dictionary_codes(self, target: str) -> frozenset[str] | None:
        """Load declared dictionary members once for this reader snapshot.

        :param target: Dictionary identity.
        :returns: Known members, or None for an external dictionary.
        :raises ValueError: If a declared dictionary has invalid state.
        """
        if target not in self._model_identities:
            return None
        if target not in self._dictionary_cache:
            self._ready()
            selection = [Selection((("decodes_id", target),))]
            headers = self._backend.rows_many("tb_decodes", selection, ("entries__state",))[0]
            if len(headers) != 1 or headers[0]["entries__state"] != STATE_CODES['VALUE']:
                raise ValueError(f"invalid dictionary state for {target}")
            self._dictionary_cache[target] = frozenset(
                str(row["code"])
                for row in self._backend.rows_many("tb_decodes_entries", selection, ("code",))[0]
            )
        return self._dictionary_cache[target]

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
            return self._encode(self._id_type(type_.target), value.id)
        code_type = self.code_types[type_.arguments[0].target] if type_.name == 'code' else None
        encoded = encode_input(type_, value, code_type)
        self._decode(type_, encoded)
        return encoded

    def _id_type(self, target: str) -> Type:
        """Find business or embedded builtin reference ID contracts.

        :param target: Qualified entity identity.
        :returns: Declared scalar ID type.
        :raises ValueError: If no unique ID contract exists.
        """
        if target not in self._layout.roots:
            raise ValueError("reference requires one identifiable ID field")
        return self._layout.ids[self._layout.roots[target]].type

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
            builtin = self._layout.entities[root].source.origin == "builtin"
            id_name = self._layout.ids[root].name if builtin else "id"
            rows = self._backend.rows(table_name(root), {id_name: encoded})
            if not rows:
                return NonNormalResult(Status.NO_ENTRY)
            actual = actual_type(self._layout, root, rows[0])
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
            self._batch.identities[EntityRef(entity, cast(str | int, id_value))] = Success(
                EntityRef(*key)
            )
            self._batch.identities[EntityRef(*key)] = Success(EntityRef(*key))
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
        try:
            self._ready()
            root = self._layout.roots[entity]
            builtin = self._layout.entities[root].source.origin == "builtin"
            id_name = self._layout.ids[root].name if builtin else "id"
            selections = [
                Selection(((id_name, self._encode(self._layout.ids[root].type, value)),))
                for value in ids
            ]
            columns = (id_name, "entity_type") if self._layout.discriminators[root] else (id_name,)
            batches = self._backend.rows_many(table_name(root), selections, columns)
            result: list[ReadResult[EntityHandle]] = []
            for value, rows in zip(ids, batches, strict=True):
                if not rows:
                    result.append(NonNormalResult(Status.NO_ENTRY))
                    continue
                actual = actual_type(self._layout, root, rows[0])
                if actual not in self._layout.entities or self._layout.roots[actual] != root:
                    raise ValueError("invalid actual entity type")
                ancestor = actual
                while ancestor != entity and ancestor in self._layout.entities:
                    ancestor = self._layout.entities[ancestor].parent
                if ancestor != entity:
                    result.append(NonNormalResult(Status.NO_ENTRY))
                    continue
                key = (actual, value)
                if key not in self._handles:
                    self._handles[key] = self.entity_types[actual](self, *key)
                self._batch.identities[EntityRef(entity, value)] = Success(EntityRef(*key))
                self._batch.identities[EntityRef(*key)] = Success(EntityRef(*key))
                result.append(Success(self._handles[key]))
            return Success(result)
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
            state = STATE_NAMES.get(cast(int, row[stored.state])) if row is not None else "NO_ENTRY"
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
            if not stored.table:
                complete = self._cache.get(handle, {}).get((identity, 'all'))
                if isinstance(complete, Success):
                    for record in cast(tuple[object, ...], complete.value):
                        if any(getattr(record, key) != value for key, value in dimensions.items()):
                            continue
                        if field.category == 'snapshot':
                            time_key = next(
                                (
                                    b.key
                                    for c in field.decorators
                                    if c.target == 'builtins.temporal'
                                    for b in c.bound
                                ),
                                'as_of_date',
                            )
                            if getattr(record, time_key) != when:
                                continue
                        elif field.category == 'scd' and not getattr(record, 'valid_from') <= cast(
                            date, when
                        ) < getattr(record, 'valid_to'):
                            continue
                        return cast(ReadResult[object], getattr(record, 'value'))
                    return (
                        Success(frozenset[object]())
                        if field.type.name == 'collection'
                        else NonNormalResult(Status.NO_ENTRY)
                    )
                request = FieldRequest(
                    EntityRef(handle.entity_type, handle.id),
                    identity,
                    tuple(dimensions.items()),
                    when,
                )
                return self._batch.read((request,))[0]
            if field.category == "config":
                request = FieldRequest(EntityRef(handle.entity_type, handle.id), identity)
                return self._configuration.read((request,))[0]
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
            if not stored.table:
                return self._reverse_range(handle, identity, field, stored, dimensions, start, end)
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

    def _reverse_all(
        self, handle: EntityHandle, identity: str, field: Field, stored: StoredField
    ) -> Success[object]:
        """Build complete derived coordinates from the forward relationship records.

        :param handle: Reverse target entity.
        :param identity: Reverse logical field identity.
        :param field: Effective reverse declaration.
        :param stored: Reverse coordinate contract.
        :returns: Immutable generated coordinate records.
        """
        key = (identity, 'all')
        cached = self._cache.get(handle, {}).get(key)
        if isinstance(cached, Success):
            return cached
        call = next(c for c in field.decorators if c.target == 'builtins.reverse_relationship')
        forward_id = next(
            b.type.exact.text for b in call.bound if b.name == 'target' and b.type.exact
        )
        forward = self._layout.fields[forward_id]
        self._ready()
        rows = self._backend.rows(forward.table, dict(forward.key_values))
        groups: dict[tuple[object, ...], set[date]] = {}
        for row in rows:
            dimensions = tuple(self._decode(d.type, row['dim_' + d.key]) for d in stored.dimensions)
            points = groups.setdefault(dimensions, set())
            if field.category == 'snapshot':
                points.add(date.fromisoformat(str(row['date'])))
            elif field.category == 'scd':
                points.update(
                    date.fromisoformat(str(row[key])) for key in ('valid_from', 'valid_to')
                )
        requests: list[FieldRequest] = []
        coordinates: list[tuple[tuple[object, ...], date | None, date | None]] = []
        for dimensions, points in sorted(groups.items(), key=lambda item: tuple(map(str, item[0]))):
            ordered = sorted(points)
            ranges: list[tuple[date | None, date | None]]
            if field.category == 'scd':
                ranges = [(left, right) for left, right in zip(ordered, ordered[1:])]
            elif field.category == 'snapshot':
                ranges = [(point, None) for point in ordered]
            else:
                ranges = [(None, None)]
            for start, end in ranges:
                requests.append(
                    FieldRequest(
                        EntityRef(handle.entity_type, handle.id),
                        identity,
                        tuple(
                            (d.key, value)
                            for d, value in zip(stored.dimensions, dimensions, strict=True)
                        ),
                        start,
                    )
                )
                coordinates.append((dimensions, start, end))
        records: list[object] = []
        time_key = next(
            (b.key for c in field.decorators if c.target == 'builtins.temporal' for b in c.bound),
            'as_of_date',
        )
        for (dimensions, start, end), value in zip(
            coordinates, self._batch.read(requests), strict=True
        ):
            if isinstance(value, NonNormalResult) and value.status == Status.NO_ENTRY:
                continue
            if isinstance(value, Success) and value.value == frozenset():
                continue
            args = {d.key: item for d, item in zip(stored.dimensions, dimensions, strict=True)}
            if field.category == 'snapshot':
                args[time_key] = start
            elif field.category == 'scd':
                args['valid_from'], args['valid_to'] = start, end
            records.append(self.record_types[identity](**args, value=value))
        result: Success[object] = Success(tuple(records))
        if not has_error(result):
            self._cache.setdefault(handle, {})[key] = result
        return result

    def _reverse_range(
        self,
        handle: EntityHandle,
        identity: str,
        field: Field,
        stored: StoredField,
        dimensions: dict[str, object],
        start: date,
        end: date,
    ) -> ReadResult[list[tuple[date, ReadResult[object]]]]:
        """Select derived snapshot dates or SCD changes in an interval.

        :param handle: Reverse target entity.
        :param identity: Reverse logical field identity.
        :param field: Effective reverse declaration.
        :param stored: Reverse coordinate contract.
        :param dimensions: Exact dimension coordinates.
        :param start: Inclusive interval start.
        :param end: Exclusive interval end.
        :returns: Ordered derived states at relevant dates.
        """
        if start == end:
            return Success([])
        full = self._reverse_all(handle, identity, field, stored)
        time_key = next(
            (b.key for c in field.decorators if c.target == 'builtins.temporal' for b in c.bound),
            'as_of_date',
        )
        points: set[date] = {start} if field.category == 'scd' else set()
        for record in cast(tuple[object, ...], full.value):
            if any(getattr(record, d.key) != dimensions[d.key] for d in stored.dimensions):
                continue
            names = (time_key,) if field.category == 'snapshot' else ('valid_from', 'valid_to')
            points.update(cast(date, getattr(record, name)) for name in names)
        ordered = sorted(point for point in points if start <= point < end)
        return Success(
            [(point, self.read(handle, identity, dimensions, point)) for point in ordered]
        )
