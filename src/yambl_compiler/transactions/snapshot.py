"""Expose detached candidate records through the existing read backend protocol."""

from collections.abc import Mapping, Sequence

from yambl_compiler.storage.backends import Row, Selection


class SnapshotBackend:
    """Read immutable copies of a complete physical data snapshot."""

    def __init__(self, tables: dict[str, list[Row]]) -> None:
        """Detach all row dictionaries from candidate mutations.

        :param tables: Complete physical tables.
        :returns: None.
        """
        self.tables = {name: tuple(dict(row) for row in rows) for name, rows in tables.items()}

    def rows(self, table: str, keys: Mapping[str, object]) -> list[Row]:
        """Read matching detached rows.

        :param table: Trusted table name.
        :param keys: Equality filters.
        :returns: New dictionaries for matching records.
        """
        return [
            dict(row) for row in self.tables[table] if all(row[k] == v for k, v in keys.items())
        ]

    def rows_many(
        self, table: str, selections: Sequence[Selection], columns: tuple[str, ...] = ()
    ) -> list[list[Row]]:
        """Match existing batch read semantics, including inclusive snapshot bounds.

        :param table: Trusted table name.
        :param selections: Independent requests.
        :param columns: Optional projection.
        :returns: Detached rows grouped in request order.
        """
        result: list[list[Row]] = []
        for selection in selections:
            rows = self.rows(table, dict(selection.keys))
            if selection.temporal == 'snapshot':
                rows = [r for r in rows if selection.start <= str(r['date']) <= selection.end]
            elif selection.temporal == 'scd':
                rows = [
                    r
                    for r in rows
                    if str(r['valid_from']) <= selection.end
                    and str(r['valid_to']) > selection.start
                ]
            result.append([{k: row[k] for k in columns} for row in rows] if columns else rows)
        return result

    def close(self) -> None:
        """Release a detached snapshot; existing copies need no I/O cleanup.

        :returns: None.
        """
