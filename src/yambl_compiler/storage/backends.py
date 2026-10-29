"""Read immutable row snapshots from SQLite or lazily loaded JSONL tables."""

import hashlib
import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, cast

from yambl_compiler.storage.contract import loads
from yambl_compiler.storage.records import (
    FILE_REVISION,
    LAYOUT_REVISION,
    STATE_CODES,
    Layout,
    Table,
    quote,
)

type Row = dict[str, object]


class RowValidationError(ValueError):
    """Retain an untrusted row identifier alongside the original diagnostic."""

    def __init__(self, message: str, identifier: object | None) -> None:
        """Attach the raw identifier without treating it as validated.

        :param message: Original diagnostic.
        :param identifier: Untrusted ID payload or None.
        :returns: None.
        """
        super().__init__(message)
        self.identifier = identifier


@dataclass(frozen=True)
class Selection:
    """Describe one equality and optional temporal selection in a batch.

    :param keys: Trusted column names and encoded scalar values.
    :param temporal: No time filter, snapshot dates or intersecting SCD intervals.
    :param start: Inclusive ISO lower date bound.
    :param end: Inclusive ISO upper date bound.
    """

    keys: tuple[tuple[str, object], ...]
    temporal: Literal["", "snapshot", "scd"] = ""
    start: str = ""
    end: str = ""


class Backend(Protocol):
    """Provide row selection without business value interpretation."""

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Read detached matching rows.

        :param table: Trusted physical table name.
        :param keys: Equality filters, empty for a complete table.
        :returns: Matching detached records.
        """
        ...

    def close(self) -> None:
        """Release resources.

        :returns: None.
        """
        ...

    def rows_many(
        self, table: str, selections: Sequence[Selection], columns: tuple[str, ...] = ()
    ) -> list[list[Row]]:
        """Read a batch without one backend operation per entity.

        :param table: Trusted physical table.
        :param selections: Independent requests, retaining duplicates and order.
        :param columns: Projection, or empty for all columns.
        :returns: Detached row groups in request order.
        """
        ...


class SQLiteBackend:
    """Own one read-only SQLite transaction."""

    def __init__(self, path: Path, layout: Layout) -> None:
        """Open and pin the expected database.

        :param path: Existing SQLite file.
        :param layout: Generated contract.
        :returns: None.
        :raises sqlite3.Error: On database access failure.
        :raises ValueError: On incompatible metadata.
        """
        self.connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("BEGIN")
            metadata = dict(self.connection.execute("SELECT key, value FROM tb_yambl_metadata"))
            if metadata != {
                "layout_revision": str(LAYOUT_REVISION),
                "model_digest": layout.model.model_digest,
            }:
                raise ValueError(
                    "database model or storage revision does not match the generated package"
                )
        except BaseException:
            self.connection.close()
            raise

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Select rows using parameterized equality filters.

        :param table: Trusted physical table name.
        :param keys: Equality filters.
        :returns: Detached row dictionaries.
        """
        where = " WHERE " + " AND ".join(quote(k) + " = ?" for k in keys) if keys else ""
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM " + quote(table) + where, tuple(keys.values())
            )
        ]

    def close(self) -> None:
        """Close the transaction and connection.

        :returns: None.
        """
        self.connection.close()

    def rows_many(
        self, table: str, selections: Sequence[Selection], columns: tuple[str, ...] = ()
    ) -> list[list[Row]]:
        """Join parameter batches to a table within SQLite's variable limit.

        :param table: Trusted physical table.
        :param selections: Equality and date-window requests.
        :param columns: Required payload columns, or all when empty.
        :returns: Detached rows grouped by original request index.
        """
        result: list[list[Row]] = [[] for _ in selections]
        groups: dict[tuple[tuple[str, ...], str], list[int]] = {}
        for i, selection in enumerate(selections):
            groups.setdefault((tuple(k for k, _ in selection.keys), selection.temporal), []).append(
                i
            )
        limit = self.connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        for (keys, temporal), indexes in groups.items():
            width = 1 + len(keys) + (2 if temporal else 0)
            chunk_size = limit // width
            if not chunk_size:
                raise ValueError("SQLite parameter limit cannot accommodate one batch request")
            labels = [
                "request",
                *(f"k{i}" for i in range(len(keys))),
                *(["start", "end"] if temporal else []),
            ]
            conditions = [f't.{quote(k)} = q.{quote(f"k{i}")}' for i, k in enumerate(keys)]
            if temporal == "snapshot":
                conditions.extend(['t."date" >= q."start"', 't."date" <= q."end"'])
            elif temporal == "scd":
                conditions.extend(['t."valid_from" <= q."end"', 't."valid_to" > q."start"'])
            projection = ", ".join("t." + quote(c) for c in columns) if columns else "t.*"
            for offset in range(0, len(indexes), chunk_size):
                chunk = indexes[offset : offset + chunk_size]
                parameters: list[object] = []
                for i in chunk:
                    selection = selections[i]
                    parameters.extend([i, *(v for _, v in selection.keys)])
                    if temporal:
                        parameters.extend([selection.start, selection.end])
                values = ", ".join("(" + ", ".join("?" for _ in labels) + ")" for _ in chunk)
                sql = (
                    "WITH q ("
                    + ", ".join(quote(k) for k in labels)
                    + ") AS (VALUES "
                    + values
                    + ") "
                    + 'SELECT q."request" AS "__yambl_request", '
                    + projection
                    + " FROM q JOIN "
                    + quote(table)
                    + " AS t ON "
                    + (" AND ".join(conditions) or "1")
                )
                for raw in self.connection.execute(sql, parameters):
                    row = dict(raw)
                    request = int(row.pop("__yambl_request"))
                    result[request].append(row)
        return result


def validate_row(layout: Layout, table: Table, row: Row) -> None:
    """Check physical scalar columns, states and payload lanes.

    :param layout: Generated storage contract.
    :param table: Expected table.
    :param row: Untrusted JSONL or SQLite row.
    :returns: None for a structurally valid record.
    :raises ValueError: On malformed columns, coordinates or state/value combinations.
    """
    if set(row) != {c.name for c in table.columns}:
        raise ValueError("missing or unknown columns")
    fields = [f for f in layout.fields.values() if f.table == table.name]
    states = {f.state for f in fields}
    for column in table.columns:
        value = row[column.name]
        if value is None:
            if not column.nullable:
                raise ValueError(f"NULL in {column.name}")
        elif type(value) is not (int if column.sql_type == "INTEGER" else str):
            raise ValueError(f"invalid column type: {column.name}")
        if column.name in states:
            if value not in STATE_CODES.values():
                raise ValueError("invalid stored field state")
    if "valid_from" in table.primary and "valid_to" in row:
        start, end = str(row["valid_from"]), str(row["valid_to"])
        if not "1900-01-01" <= start < end <= "2999-12-31" or any(
            date.fromisoformat(v).isoformat() != v for v in (start, end)
        ):
            raise ValueError("invalid SCD interval")
    if "date" in table.primary and date.fromisoformat(str(row["date"])).isoformat() != row["date"]:
        raise ValueError("invalid snapshot date")
    if table.name in ('tb_config_group_when', 'tb_config_group_then_set'):
        envelope = loads(str(row['value']))
        if (
            not isinstance(envelope, dict)
            or not isinstance(envelope.get('type'), dict)
            or 'value' not in envelope
        ):
            raise ValueError('invalid typed mapping envelope')
    if (
        "field" in {key for f in fields for key, _ in f.key_values}
        and row["field"] not in table.fields
    ):
        raise ValueError("unknown logical field")
    for stored in fields:
        if any(row[k] != v for k, v in stored.key_values):
            continue
        normal = row[stored.state] == STATE_CODES['VALUE']
        if stored.value:
            payload = row[stored.value]
            if normal != (payload is not None):
                raise ValueError("invalid state/payload combination")
            if normal and stored.field.type.name == "bool" and payload not in (0, 1):
                raise ValueError("invalid boolean payload")
        if stored.key_values:
            if any(
                row[c.name] is not None
                for c in table.columns
                if c.name not in ("id", "field", "state", stored.value)
            ):
                raise ValueError("multiple or wrong payload lanes")


def repository_contents(layout: Layout, tables: Mapping[str, list[Row]]) -> dict[str, str]:
    """Render a JSONL snapshot and its content-addressed metadata.

    :param layout: Generated storage contract.
    :param tables: Complete table records from a template or example initializer.
    :returns: Relative filenames and UTF-8 text.
    """
    contents: dict[str, str] = {}
    for table in layout.tables:
        rows: list[str] = []
        offsets: list[str] = []
        offset = 0
        for row in tables[table.name]:
            text = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            length = len(text.encode("utf-8"))
            item = [*[row[key] for key in table.primary], row.get("valid_to"), offset, length]
            offsets.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            rows.append(text)
            offset += length
        contents[f"tables/{table.name}.jsonl"] = "".join(rows)
        contents[f"indexes/{table.name}.jsonl"] = "".join(offsets)
    metadata = {
        "format_revision": FILE_REVISION,
        "layout_revision": LAYOUT_REVISION,
        "model_digest": layout.model.model_digest,
        "tables": {
            t.name: hashlib.sha256(contents[f"tables/{t.name}.jsonl"].encode()).hexdigest()
            for t in layout.tables
        },
        "indexes": {
            t.name: hashlib.sha256(contents[f"indexes/{t.name}.jsonl"].encode()).hexdigest()
            for t in layout.tables
        },
    }
    contents["repository.json"] = json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
    return contents


class FileBackend:
    """Load tables on demand and reject changes to the pinned repository."""

    def __init__(self, path: Path, layout: Layout) -> None:
        """Validate metadata and record file identities without loading tables.

        :param path: Existing JSONL repository directory.
        :param layout: Generated contract.
        :returns: None.
        :raises ValueError: On invalid metadata or file names.
        :raises OSError: On missing or inaccessible files.
        """
        self.path, self.layout = Path(path).resolve(), layout
        self.tables = {t.name: t for t in layout.tables}
        self.files = {t: self.path / "tables" / (t + ".jsonl") for t in self.tables}
        self.files["repository"] = self.path / "repository.json"
        for file in self.files.values():
            if not file.resolve().is_relative_to(self.path) or file.is_symlink():
                raise ValueError("repository file escapes root or is a symlink")
        self.stamps = self._stamps()
        metadata = loads(self.files["repository"].read_text(encoding="utf-8"))
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            not in (
                {"format_revision", "layout_revision", "model_digest", "tables"},
                {"format_revision", "layout_revision", "model_digest", "tables", "indexes"},
            )
            or type(metadata["format_revision"]) is not int
            or metadata["format_revision"] != FILE_REVISION
            or type(metadata["layout_revision"]) is not int
            or metadata["layout_revision"] != LAYOUT_REVISION
            or metadata["model_digest"] != layout.model.model_digest
        ):
            raise ValueError("file repository model or revision does not match generated package")
        hashes = metadata["tables"]
        if (
            not isinstance(hashes, dict)
            or set(hashes) != set(self.tables)
            or any(
                not isinstance(v, str)
                or len(v) != 64
                or any(c not in "0123456789abcdef" for c in v)
                for v in hashes.values()
            )
        ):
            raise ValueError("invalid table digest manifest")
        self.hashes = cast(dict[str, str], hashes)
        index_hashes = metadata.get("indexes", {})
        if (
            not isinstance(index_hashes, dict)
            or ("indexes" in metadata and set(index_hashes) != set(self.tables))
            or any(
                not isinstance(v, str)
                or len(v) != 64
                or any(c not in "0123456789abcdef" for c in v)
                for v in index_hashes.values()
            )
        ):
            raise ValueError("invalid index digest manifest")
        self.index_hashes = cast(dict[str, str], index_hashes)
        if self.index_hashes:
            original = self.stamps
            for table in self.tables:
                file = self.path / "indexes" / (table + ".jsonl")
                if not file.resolve().is_relative_to(self.path) or file.is_symlink():
                    raise ValueError("repository index escapes root or is a symlink")
                self.files["index:" + table] = file
            self.stamps = self._stamps()
            if any(self.stamps[name] != stamp for name, stamp in original.items()):
                raise ValueError("file repository changed while opening")
        self.cache: dict[str, list[Row]] = {}
        self.indexes: dict[tuple[str, tuple[str, ...]], dict[tuple[object, ...], list[Row]]] = {}
        self.offsets: dict[str, list[Row]] = {}
        self.row_cache: OrderedDict[tuple[str, tuple[int, int, int], int], Row] = OrderedDict()
        self.time_indexes: dict[
            tuple[str, tuple[tuple[str, object], ...], str], tuple[list[str], list[Row]]
        ] = {}
        self.failed = False
        self._check()

    def _stamps(self) -> dict[str, tuple[int, int, int]]:
        """Read identity, size and timestamp for all pinned files.

        :returns: File signatures.
        """
        return {
            name: (s.st_ino, s.st_size, s.st_mtime_ns)
            for name, file in self.files.items()
            for s in (file.stat(),)
        }

    def _check(self) -> None:
        """Reject further reads after any repository change.

        :returns: None for the original snapshot.
        :raises ValueError: On a changed or invalidated repository.
        """
        try:
            changed = self.failed or self._stamps() != self.stamps
        except OSError:
            self.failed = True
            raise
        if changed:
            self.failed = True
            raise ValueError("file repository changed; reopen the reader")

    def check_snapshot(self) -> None:
        """Verify that the pinned repository still names the same files.

        :returns: None for an unchanged source snapshot.
        :raises ValueError: If the source snapshot changed.
        :raises OSError: If a source file cannot be inspected.
        """
        self._check()

    def validate_indexes(self) -> None:
        """Check every persisted index against its table when indexes exist.

        :returns: None for a valid indexed or legacy unindexed snapshot.
        :raises ValueError: If an index or table fails integrity validation.
        :raises OSError: If a file cannot be read.
        """
        self._check()
        if self.index_hashes:
            for table in self.tables:
                self._load_offsets(table)

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Load, validate and index a table on its first use.

        :param table: Trusted table name.
        :param keys: Equality filters.
        :returns: Detached matching row dictionaries.
        :raises ValueError: On corrupt rows, duplicate keys or changed content.
        :raises OSError: On file access failure.
        """
        self._check()
        if keys and self._indexed(table, tuple(keys)):
            return self._read_offsets(table, self._select_offsets(table, tuple(keys.items())))
        self._load(table)
        return self._select(table, tuple(keys.items()))

    def _indexed(self, table: str, columns: tuple[str, ...]) -> bool:
        """Select the persistent index when its keys cover the requested equality filters.

        :param table: Physical table name.
        :param columns: Requested equality columns.
        :returns: Whether offsets can satisfy the filters.
        """
        return bool(self.index_hashes) and set(columns).issubset(self.tables[table].primary)

    def _load_offsets(self, table: str) -> None:
        """Verify a table and load only its primary keys and byte offsets.

        :param table: Physical table name.
        :returns: None; the validated index is cached for this snapshot.
        :raises ValueError: On malformed index, contents, or digest.
        """
        if table in self.offsets:
            return
        index_path = self.files["index:" + table]
        content = index_path.read_bytes()
        self._check()
        if hashlib.sha256(content).hexdigest() != self.index_hashes[table]:
            self.failed = True
            raise ValueError(f"{index_path}: index digest mismatch")
        table_path = self.files[table]
        digest = hashlib.sha256()
        with table_path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        self._check()
        if digest.hexdigest() != self.hashes[table]:
            self.failed = True
            raise ValueError(f"{table_path}: table digest mismatch")
        entries: list[Row] = []
        seen: set[tuple[object, ...]] = set()
        offset = 0
        primary = self.tables[table].primary
        has_end = any(column.name == "valid_to" for column in self.tables[table].columns)
        for line, raw in enumerate(content.splitlines(), 1):
            try:
                item: object = json.loads(raw.decode("utf-8"))
                if not isinstance(item, list):
                    raise ValueError("invalid index record")
                values = cast(list[object], item)
                if len(values) != len(primary) + 3:
                    raise ValueError("invalid index record")
                key = tuple(values[: len(primary)])
                end, start, length = values[-3:]
                if (
                    any(type(value) not in (str, int) for value in key)
                    or (end is not None and not isinstance(end, str))
                    or (has_end != (end is not None))
                    or type(start) is not int
                    or type(length) is not int
                    or start != offset
                    or length <= 0
                    or key in seen
                ):
                    raise ValueError("invalid offset or duplicate index key")
                offset += length
                seen.add(key)
                entry: Row = dict(zip(primary, key, strict=True))
                entry.update(_offset=start, _length=length)
                if end is not None:
                    entry["valid_to"] = end
                entries.append(entry)
            except (ValueError, TypeError) as error:
                raise ValueError(f"{table_path}:{line}: {table}: {error}") from error
        if offset != self.stamps[table][1]:
            raise ValueError(f"{table_path}: index does not cover table")
        with table_path.open("rb") as source:
            for line, entry in enumerate(entries, 1):
                raw = source.read(cast(int, entry["_length"]))
                self._decode_offset(table, entry, raw, line)
        self._check()
        self.offsets[table] = entries

    def _select_offsets(self, table: str, keys: tuple[tuple[str, object], ...]) -> list[Row]:
        """Find offset entries by their indexed primary-key columns.

        :param table: Physical table name.
        :param keys: Equality conditions.
        :returns: Matching offset entries.
        """
        self._load_offsets(table)
        columns = tuple(k for k, _ in keys)
        index_key = ("offset:" + table, columns)
        if index_key not in self.indexes:
            index: dict[tuple[object, ...], list[Row]] = {}
            for row in self.offsets[table]:
                index.setdefault(tuple(row[k] for k in columns), []).append(row)
            self.indexes[index_key] = index
        return self.indexes[index_key].get(tuple(v for _, v in keys), [])

    def _read_offsets(self, table: str, entries: Sequence[Row]) -> list[Row]:
        """Read and validate only selected records from a verified table.

        :param table: Physical table name.
        :param entries: Selected primary keys and byte offsets.
        :returns: Detached physical records.
        :raises ValueError: On corrupt records or mismatched index keys.
        """
        found: dict[tuple[str, tuple[int, int, int], int], Row] = {}
        pending: dict[tuple[str, tuple[int, int, int], int], Row] = {}
        for entry in entries:
            key = (table, self.stamps[table], cast(int, entry["_offset"]))
            cached = self.row_cache.get(key)
            if cached is None:
                pending[key] = entry
            else:
                self.row_cache.move_to_end(key)
                found[key] = cached
        path = self.files[table]
        if pending:
            with path.open("rb") as source:
                for key, entry in pending.items():
                    source.seek(key[2])
                    raw = source.read(cast(int, entry["_length"]))
                    found[key] = self._decode_offset(table, entry, raw, None)
        self._check()
        for key in pending:
            self.row_cache[key] = found[key]
            if len(self.row_cache) > 1024:
                self.row_cache.popitem(last=False)
        return [
            dict(found[(table, self.stamps[table], cast(int, entry["_offset"]))])
            for entry in entries
        ]

    def _decode_offset(self, table: str, entry: Row, raw: bytes, line: int | None) -> Row:
        """Validate one indexed record against its primary key and interval.

        :param table: Physical table name.
        :param entry: Indexed primary key and byte location.
        :param raw: Complete encoded JSONL record.
        :param line: Record line number when streaming the entire table.
        :returns: Decoded physical row.
        :raises ValueError: On malformed or inconsistent data.
        """
        try:
            value = loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("expected row object")
            row = cast(Row, value)
            validate_row(self.layout, self.tables[table], row)
            if any(row[key] != entry[key] for key in self.tables[table].primary):
                raise ValueError("row does not match index")
            if "valid_to" in entry and row.get("valid_to") != entry["valid_to"]:
                raise ValueError("interval does not match index")
            return row
        except (ValueError, TypeError, KeyError) as error:
            location = f":{line}" if line is not None else ""
            raise ValueError(f"{self.files[table]}{location}: {table}: {error}") from error

    def _load(self, table: str) -> None:
        """Load and validate a table once per pinned session.

        :param table: Trusted physical table.
        :returns: None; populates the immutable row snapshot.
        """
        if table not in self.cache:
            path = self.files[table]
            content = path.read_bytes()
            self._check()
            if hashlib.sha256(content).hexdigest() != self.hashes[table]:
                self.failed = True
                raise ValueError(f"{path}: table digest mismatch")
            rows: list[Row] = []
            seen: set[tuple[object, ...]] = set()
            for line, raw in enumerate(content.splitlines(), 1):
                identifier: object | None = None
                try:
                    data = loads(raw.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("expected row object")
                    row = cast(Row, data)
                    identifier = row.get(self.tables[table].primary[0])
                    validate_row(self.layout, self.tables[table], row)
                    key = tuple(row[k] for k in self.tables[table].primary)
                    if key in seen:
                        raise ValueError("duplicate primary key")
                    seen.add(key)
                    rows.append(row)
                except ValueError as error:
                    raise RowValidationError(
                        f"{path}:{line}: {table}: {error}", identifier
                    ) from error
            self.cache[table] = rows

    def _select(self, table: str, keys: tuple[tuple[str, object], ...]) -> list[Row]:
        """Use a shared equality index without repeating repository stat calls.

        :param table: Loaded table.
        :param keys: Encoded equality filters.
        :returns: Detached matching rows.
        """
        columns = tuple(k for k, _ in keys)
        index_key = (table, columns)
        if index_key not in self.indexes:
            index: dict[tuple[object, ...], list[Row]] = {}
            for row in self.cache[table]:
                index.setdefault(tuple(row[k] for k in columns), []).append(row)
            self.indexes[index_key] = index
        return [dict(r) for r in self.indexes[index_key].get(tuple(v for _, v in keys), [])]

    def rows_many(
        self, table: str, selections: Sequence[Selection], columns: tuple[str, ...] = ()
    ) -> list[list[Row]]:
        """Check the snapshot once and share loaded tables and equality indexes.

        :param table: Trusted physical table.
        :param selections: Independent equality and temporal selections.
        :param columns: Projection, empty for complete rows.
        :returns: Detached rows in request order.
        """
        if not selections:
            return []
        self._check()
        if all(
            selection.keys and self._indexed(table, tuple(key for key, _ in selection.keys))
            for selection in selections
        ):
            selected: list[list[Row]] = []
            for selection in selections:
                entries = self._select_offsets(table, selection.keys)
                if selection.temporal:
                    index_key = ("offset:" + table, selection.keys, selection.temporal)
                    if index_key not in self.time_indexes:
                        column = "date" if selection.temporal == "snapshot" else "valid_from"
                        ordered = sorted(entries, key=lambda row: str(row[column]))
                        self.time_indexes[index_key] = (
                            [str(row[column]) for row in ordered],
                            ordered,
                        )
                    dates, ordered = self.time_indexes[index_key]
                    end = bisect_right(dates, selection.end)
                    if selection.temporal == "snapshot":
                        entries = ordered[bisect_left(dates, selection.start) : end]
                    else:
                        entries = [r for r in ordered[:end] if str(r["valid_to"]) > selection.start]
                selected.append(entries)
            unique: dict[object, Row] = {}
            for entries in selected:
                for entry in entries:
                    unique.setdefault(entry["_offset"], entry)
            records = self._read_offsets(table, list(unique.values()))
            by_offset = dict(zip(unique, records, strict=True))
            return [
                [
                    (
                        {column: by_offset[entry["_offset"]][column] for column in columns}
                        if columns
                        else dict(by_offset[entry["_offset"]])
                    )
                    for entry in entries
                ]
                for entries in selected
            ]
        self._load(table)
        result: list[list[Row]] = []
        for selection in selections:
            if selection.temporal:
                index_key = (table, selection.keys, selection.temporal)
                if index_key not in self.time_indexes:
                    time_column = "date" if selection.temporal == "snapshot" else "valid_from"
                    ordered = sorted(
                        self._select(table, selection.keys),
                        key=lambda row: str(row[time_column]),
                    )
                    self.time_indexes[index_key] = (
                        [str(row[time_column]) for row in ordered],
                        ordered,
                    )
                dates, ordered = self.time_indexes[index_key]
                end = bisect_right(dates, selection.end)
                if selection.temporal == "snapshot":
                    rows = ordered[bisect_left(dates, selection.start) : end]
                else:
                    rows = [r for r in ordered[:end] if str(r["valid_to"]) > selection.start]
            else:
                rows = self._select(table, selection.keys)
            result.append(
                [{c: r[c] for c in columns} for r in rows] if columns else [dict(r) for r in rows]
            )
        return result

    def close(self) -> None:
        """Discard table indexes and invalidate future backend reads.

        :returns: None.
        """
        self.failed = True
        self.cache.clear()
        self.indexes.clear()
        self.offsets.clear()
        self.row_cache.clear()
        self.time_indexes.clear()
