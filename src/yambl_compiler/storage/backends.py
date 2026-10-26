"""Read immutable row snapshots from SQLite or lazily loaded JSONL tables."""

import hashlib
import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, cast

from yambl_compiler.storage.contract import loads
from yambl_compiler.storage.records import LAYOUT_REVISION, Layout, Table, quote

type Row = dict[str, object]


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
    for column in table.columns:
        value = row[column.name]
        if value is None:
            if not column.nullable:
                raise ValueError(f"NULL in {column.name}")
        elif type(value) is not (int if column.sql_type == "INTEGER" else str):
            raise ValueError(f"invalid column type: {column.name}")
        if column.name.endswith("__state") or column.name == "state":
            if value not in ("VALUE", "NO_ENTRY", "EMPTY", "NOT_APPLICABLE"):
                raise ValueError("invalid stored field state")
    if "valid_from" in table.primary and "valid_to" in row:
        start, end = str(row["valid_from"]), str(row["valid_to"])
        if not "1900-01-01" <= start < end <= "2999-12-31" or any(
            date.fromisoformat(v).isoformat() != v for v in (start, end)
        ):
            raise ValueError("invalid SCD interval")
    if "date" in table.primary and date.fromisoformat(str(row["date"])).isoformat() != row["date"]:
        raise ValueError("invalid snapshot date")
    fields = [f for f in layout.fields.values() if f.table == table.name]
    if table.name in ('tb_config_group_when', 'tb_config_group_then_set'):
        envelope = loads(str(row['value']))
        if (
            not isinstance(envelope, dict)
            or not isinstance(envelope.get('type'), dict)
            or 'value' not in envelope
        ):
            raise ValueError('invalid typed mapping envelope')
    if "field" in row and fields and row["field"] not in table.fields:
        raise ValueError("unknown logical field")
    for stored in fields:
        if any(row[k] != v for k, v in stored.key_values):
            continue
        normal = row[stored.state] == "VALUE"
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
    contents = {
        f"tables/{t.name}.jsonl": "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for r in tables[t.name]
        )
        for t in layout.tables
    }
    metadata = {
        "format_revision": 2,
        "layout_revision": LAYOUT_REVISION,
        "model_digest": layout.model.model_digest,
        "tables": {
            t.name: hashlib.sha256(contents[f"tables/{t.name}.jsonl"].encode()).hexdigest()
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
            or set(metadata) != {"format_revision", "layout_revision", "model_digest", "tables"}
            or type(metadata["format_revision"]) is not int
            or metadata["format_revision"] != 2
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
        self.cache: dict[str, list[Row]] = {}
        self.indexes: dict[tuple[str, tuple[str, ...]], dict[tuple[object, ...], list[Row]]] = {}
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

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Load, validate and index a table on its first use.

        :param table: Trusted table name.
        :param keys: Equality filters.
        :returns: Detached matching row dictionaries.
        :raises ValueError: On corrupt rows, duplicate keys or changed content.
        :raises OSError: On file access failure.
        """
        self._check()
        self._load(table)
        return self._select(table, tuple(keys.items()))

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
                try:
                    data = loads(raw.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("expected row object")
                    row = cast(Row, data)
                    validate_row(self.layout, self.tables[table], row)
                    key = tuple(row[k] for k in self.tables[table].primary)
                    if key in seen:
                        raise ValueError("duplicate primary key")
                    seen.add(key)
                    rows.append(row)
                except ValueError as error:
                    raise ValueError(f"{path}:{line}: {table}: {error}") from error
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
        self.time_indexes.clear()
