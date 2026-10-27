"""Prepare incremental file generations or publish SQLite row changes."""

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from yambl_compiler.languages.yatran.model import Transaction
from yambl_compiler.storage.backends import FileBackend, Row
from yambl_compiler.storage.contract import loads
from yambl_compiler.storage.records import LAYOUT_REVISION, Layout, Table, quote
from yambl_compiler.transactions.overlay import RowChange
from yambl_compiler.transactions.writing import dumps

LOGGER = logging.getLogger(__name__)


def _link(source: Path, destination: Path) -> None:
    """Reuse an immutable file using a hard link in the new generation.

    :param source: Existing file in the pinned generation.
    :param destination: File in the new generation.
    :returns: None.
    :raises OSError: If the filesystem or process cannot create a hard link.
    """
    try:
        os.link(source, destination)
    except OSError as error:
        raise OSError(f'file backend requires hard links: {source}: {error}') from error


def _stream_table(
    source: FileBackend, destination: Path, table: Table, changes: Sequence[RowChange]
) -> tuple[str, str]:
    """Stream a merged table and its byte-offset index without retaining all rows.

    :param source: Pinned source generation.
    :param destination: Newly prepared generation directory.
    :param table: Physical table schema.
    :param changes: Changed row images for this table.
    :returns: SHA-256 digests of the table and its index.
    :raises ValueError: If a changed row no longer matches its source image.
    """
    pending = {change.key: change for change in changes}
    if len(pending) != len(changes):
        raise ValueError(f'duplicate changed primary key in {table.name}')
    table_hash = hashlib.sha256()
    index_hash = hashlib.sha256()
    original_hash = hashlib.sha256()
    offset = 0
    target = destination / 'tables' / (table.name + '.jsonl')
    index = destination / 'indexes' / (table.name + '.jsonl')
    with (
        source.files[table.name].open('rb') as existing,
        target.open('xb') as output,
        index.open('xb') as offsets,
    ):

        def write(row: Row) -> None:
            """Append a canonical row and matching offset entry.

            :param row: Complete physical row.
            :returns: None.
            """
            nonlocal offset
            raw = (
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
            ).encode('utf-8')
            entry = [*[row[key] for key in table.primary], row.get('valid_to'), offset, len(raw)]
            indexed = (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode(
                'utf-8'
            )
            output.write(raw)
            offsets.write(indexed)
            table_hash.update(raw)
            index_hash.update(indexed)
            offset += len(raw)

        for raw in existing:
            original_hash.update(raw)
            value = loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError(f'invalid source row in {table.name}')
            row = cast(Row, value)
            key = tuple(row[column] for column in table.primary)
            change = pending.pop(key, None)
            if change is None:
                write(row)
            else:
                if change.before is None or change.before != row:
                    raise ValueError(f'original row changed in {table.name}')
                if change.after is not None:
                    write(change.after)
        for change in pending.values():
            if change.before is not None:
                raise ValueError(f'original row missing in {table.name}')
            if change.after is not None:
                write(change.after)
        output.flush()
        offsets.flush()
        os.fsync(output.fileno())
        os.fsync(offsets.fileno())
    if original_hash.hexdigest() != source.hashes[table.name]:
        raise ValueError(f'source table digest mismatch: {table.name}')
    return table_hash.hexdigest(), index_hash.hexdigest()


def _index_legacy(source: FileBackend, destination: Path, table: Table) -> str:
    """Build an index for an unchanged legacy table while retaining its original bytes.

    :param source: Pinned unindexed generation.
    :param destination: Newly prepared generation directory.
    :param table: Physical table schema.
    :returns: SHA-256 digest of the generated index.
    :raises ValueError: If the source table does not match its stored digest.
    """
    source_hash = hashlib.sha256()
    index_hash = hashlib.sha256()
    offset = 0
    with (
        source.files[table.name].open('rb') as existing,
        (destination / 'indexes' / (table.name + '.jsonl')).open('xb') as output,
    ):
        for raw in existing:
            source_hash.update(raw)
            value = loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError(f'invalid source row in {table.name}')
            row = cast(Row, value)
            entry = [*[row[key] for key in table.primary], row.get('valid_to'), offset, len(raw)]
            indexed = (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode(
                'utf-8'
            )
            output.write(indexed)
            index_hash.update(indexed)
            offset += len(raw)
        output.flush()
        os.fsync(output.fileno())
    if source_hash.hexdigest() != source.hashes[table.name]:
        raise ValueError(f'source table digest mismatch: {table.name}')
    return index_hash.hexdigest()


def prepare_files(
    source: FileBackend,
    destination: Path,
    layout: Layout,
    changes: Mapping[str, Sequence[RowChange]],
    transaction: Transaction | None = None,
) -> None:
    """Prepare an unpublished immutable generation from affected physical rows.

    The caller owns the writer lock, publishes the transaction record and atomically
    switches the root pointer only after this function succeeds.

    :param source: Pinned file-repository generation.
    :param destination: New, nonexistent generation directory.
    :param layout: Generated physical schema.
    :param changes: Changed physical rows by table.
    :param transaction: Optional sealed transaction written before generation files.
    :returns: None after all generation files have been synchronized.
    :raises ValueError: If changed rows do not match the source snapshot.
    :raises OSError: If generation preparation fails.
    """
    tables = {table.name: table for table in layout.tables}
    if set(changes) - tables.keys():
        raise ValueError('unknown changed table')
    source.check_snapshot()
    destination.mkdir(parents=True)
    if transaction is not None:
        with (destination / 'transaction.yatran').open(
            'x', encoding='utf-8', newline='\n'
        ) as output:
            output.write(dumps(transaction))
            output.flush()
            os.fsync(output.fileno())
    (destination / 'tables').mkdir()
    (destination / 'indexes').mkdir()
    hashes: dict[str, str] = {}
    indexes: dict[str, str] = {}
    for table in layout.tables:
        if table.name in changes:
            hashes[table.name], indexes[table.name] = _stream_table(
                source, destination, table, changes.get(table.name, ())
            )
        else:
            _link(source.files[table.name], destination / 'tables' / (table.name + '.jsonl'))
            hashes[table.name] = source.hashes[table.name]
            if source.index_hashes:
                _link(
                    source.files['index:' + table.name],
                    destination / 'indexes' / (table.name + '.jsonl'),
                )
                indexes[table.name] = source.index_hashes[table.name]
            else:
                indexes[table.name] = _index_legacy(source, destination, table)
    source.check_snapshot()
    metadata = {
        'format_revision': 2,
        'layout_revision': LAYOUT_REVISION,
        'model_digest': layout.model.model_digest,
        'tables': hashes,
        'indexes': indexes,
    }
    with (destination / 'repository.json').open('x', encoding='utf-8', newline='\n') as output:
        json.dump(metadata, output, sort_keys=True, separators=(',', ':'))
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def publish_sqlite(
    path: Path, layout: Layout, changes: Mapping[str, Sequence[RowChange]], transaction: Transaction
) -> None:
    """Atomically publish validated affected rows and their transaction record.

    The caller must hold the repository writer lock and close its pinned read
    connection before calling. Unaffected physical rows are neither read nor copied.

    :param path: Existing SQLite data repository.
    :param layout: Generated physical schema.
    :param changes: Physical table changes with original and final images.
    :param transaction: Sealed transaction record for recovery and rollback.
    :returns: None after the transaction commits.
    :raises ValueError: If an original row disappeared or an unknown table is supplied.
    :raises sqlite3.Error: If publication fails before the commit completes.
    """
    tables = {table.name: table for table in layout.tables}
    if set(changes) - tables.keys():
        raise ValueError('unknown changed table')
    connection = sqlite3.connect(path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        for table in reversed(layout.tables):
            where = ' AND '.join(quote(column) + ' = ?' for column in table.primary)
            for change in changes.get(table.name, ()):
                if change.before is not None:
                    cursor = connection.execute(
                        'DELETE FROM ' + quote(table.name) + ' WHERE ' + where, change.key
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(f'original row missing in {table.name}')
        for table in layout.tables:
            columns = tuple(column.name for column in table.columns)
            sql = (
                'INSERT INTO '
                + quote(table.name)
                + ' ('
                + ', '.join(quote(column) for column in columns)
                + ') VALUES ('
                + ', '.join('?' for _ in columns)
                + ')'
            )
            for change in changes.get(table.name, ()):
                if change.after is not None:
                    connection.execute(sql, tuple(change.after[column] for column in columns))
        connection.execute(
            'INSERT INTO tb_yambl_transactions VALUES (?, ?)', (transaction.id, dumps(transaction))
        )
        connection.commit()
    except BaseException as error:
        try:
            connection.close()
        except sqlite3.Error as close_error:
            error.add_note(f'SQLite connection close failed: {close_error}')
        raise
    try:
        connection.close()
    except sqlite3.Error:
        LOGGER.warning(
            '%s: SQLite connection close failed after commit', transaction.id, exc_info=True
        )
