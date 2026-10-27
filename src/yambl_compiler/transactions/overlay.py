"""Keep candidate row changes separate from a pinned source snapshot."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from yambl_compiler.storage.backends import Backend, Row, Selection, validate_row
from yambl_compiler.storage.records import Layout


@dataclass(frozen=True)
class RowChange:
    """Describe the original and candidate versions of a physical row.

    :param key: Physical primary key in schema order.
    :param before: Original row, or None for insertion.
    :param after: Candidate row, or None for deletion.
    """

    key: tuple[object, ...]
    before: Row | None
    after: Row | None


class Overlay:
    """Read selected rows lazily and own detached mutable candidate records."""

    def __init__(self, layout: Layout, backend: Backend) -> None:
        """Bind one candidate to a caller-owned pinned backend.

        :param layout: Generated model and physical layout.
        :param backend: Immutable pinned source snapshot.
        :returns: None.
        """
        self.layout = layout
        self.backend = backend
        self.schema = {table.name: table for table in layout.tables}
        self.original: dict[str, dict[tuple[object, ...], Row | None]] = {}
        self.current: dict[str, dict[tuple[object, ...], Row | None]] = {}
        self.loaded: set[tuple[str, tuple[tuple[str, object], ...]]] = set()
        self.indexes: dict[
            tuple[str, tuple[str, ...]], dict[tuple[object, ...], set[tuple[object, ...]]]
        ] = {}

    def _index_key(self, table: str, key: tuple[object, ...]) -> None:
        """Add a new physical key to the indexes already built for its table.

        :param table: Physical table name.
        :param key: Schema-ordered primary key.
        :returns: None.
        """
        primary = self.schema[table].primary
        for (indexed_table, columns), index in self.indexes.items():
            if indexed_table == table:
                values = tuple(key[primary.index(column)] for column in columns)
                index.setdefault(values, set()).add(key)

    def key(self, table: str, row: Row) -> tuple[object, ...]:
        """Extract one physical row's ordered primary key.

        :param table: Physical table name.
        :param row: Candidate or source row.
        :returns: Key in schema order.
        """
        return tuple(row[name] for name in self.schema[table].primary)

    def select(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Select current candidate rows without loading unrelated source rows.

        :param table: Physical table name.
        :param keys: Equality filters accepted by the source backend.
        :returns: Candidate-owned matching rows.
        :raises ValueError: If a source row violates its physical contract.
        """
        original = self.original.setdefault(table, {})
        current = self.current.setdefault(table, {})
        query = (table, tuple(sorted(keys.items())))
        if query not in self.loaded:
            for row in self.backend.rows(table, keys):
                validate_row(self.layout, self.schema[table], row)
                key = self.key(table, row)
                if key not in original:
                    original[key] = dict(row)
                    current[key] = dict(row)
                    self._index_key(table, key)
            self.loaded.add(query)
        columns = tuple(sorted(keys))
        selected: Iterable[Row | None]
        if set(columns).issubset(self.schema[table].primary):
            index_key = (table, columns)
            if index_key not in self.indexes:
                self.indexes[index_key] = {}
                for key in current:
                    primary = self.schema[table].primary
                    values = tuple(key[primary.index(column)] for column in columns)
                    self.indexes[index_key].setdefault(values, set()).add(key)
            selected = (
                current[key]
                for key in self.indexes[index_key].get(
                    tuple(keys[column] for column in columns), ()
                )
            )
        else:
            selected = iter(current.values())
        return [
            row
            for row in selected
            if row is not None and all(row[column] == value for column, value in keys.items())
        ]

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Read detached rows through the storage backend protocol.

        :param table: Physical table name.
        :param keys: Equality filters.
        :returns: Detached candidate rows.
        """
        return [dict(row) for row in self.select(table, keys)]

    def rows_many(
        self, table: str, selections: Sequence[Selection], columns: tuple[str, ...] = ()
    ) -> list[list[Row]]:
        """Read multiple candidate selections with normal temporal semantics.

        :param table: Physical table name.
        :param selections: Equality and temporal selections.
        :param columns: Projected columns, or empty for complete records.
        :returns: Detached records grouped by selection.
        """
        result: list[list[Row]] = []
        for selection in selections:
            rows = self.select(table, dict(selection.keys))
            if selection.temporal == 'snapshot':
                rows = [row for row in rows if selection.start <= str(row['date']) <= selection.end]
            elif selection.temporal == 'scd':
                rows = [
                    row
                    for row in rows
                    if str(row['valid_from']) <= selection.end
                    and str(row['valid_to']) > selection.start
                ]
            result.append(
                [{column: row[column] for column in columns} for row in rows]
                if columns
                else [dict(row) for row in rows]
            )
        return result

    def close(self) -> None:
        """Close a reader view without closing the caller-owned source backend.

        :returns: None.
        """

    def add(self, table: str, row: Row) -> None:
        """Insert a row, rejecting an existing candidate primary key.

        :param table: Physical table name.
        :param row: New complete row, which is detached from the caller.
        :returns: None.
        :raises ValueError: If a record with the key already exists.
        """
        key = self.key(table, row)
        filters = dict(zip(self.schema[table].primary, key, strict=True))
        if self.select(table, filters):
            raise ValueError(f'duplicate record in {table}')
        is_new = key not in self.original[table]
        self.original[table].setdefault(key, None)
        self.current[table][key] = dict(row)
        if is_new:
            self._index_key(table, key)

    def remove(self, table: str, row: Row) -> None:
        """Delete a row already selected or inserted into this candidate.

        :param table: Physical table name.
        :param row: Candidate row to remove.
        :returns: None.
        :raises ValueError: If the row does not belong to this candidate.
        """
        key = self.key(table, row)
        if self.current.get(table, {}).get(key) is not row:
            raise ValueError('row does not belong to the candidate')
        self.current[table][key] = None

    def changes(self) -> dict[str, tuple[RowChange, ...]]:
        """Return detached changes and reject mutation of primary key columns.

        :returns: Changed tables and row before/after images.
        :raises ValueError: If a selected row's primary key was modified.
        """
        result: dict[str, tuple[RowChange, ...]] = {}
        for table, rows in self.current.items():
            changes: list[RowChange] = []
            for key, after in rows.items():
                if after is not None and self.key(table, after) != key:
                    raise ValueError(f'candidate primary key changed in {table}')
                before = self.original[table][key]
                if before != after:
                    changes.append(
                        RowChange(
                            key,
                            None if before is None else dict(before),
                            None if after is None else dict(after),
                        )
                    )
            if changes:
                result[table] = tuple(changes)
        return result
