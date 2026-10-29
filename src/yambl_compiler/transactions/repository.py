"""Open configured data stores and atomically publish recoverable transactions."""

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import ClassVar, Self, cast

from lclang.utils import SnowflakeGenerator

from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.yaplan.model import Plan
from yambl_compiler.languages.yatran.model import Transaction, TransactionGroup
from yambl_compiler.languages.yatran.syntax import valid_id
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.storage.backends import FileBackend, Row, SQLiteBackend, repository_contents
from yambl_compiler.storage.contract import decode, load_contract, loads
from yambl_compiler.storage.records import FILE_REVISION, Layout, quote
from yambl_compiler.storage.results import Success
from yambl_compiler.storage.runtime import Reader
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, prepare_rollback
from yambl_compiler.transactions.incremental import IncrementalData
from yambl_compiler.transactions.incremental_publication import prepare_files, publish_sqlite
from yambl_compiler.transactions.snapshot import SnapshotBackend
from yambl_compiler.transactions.validation import PlanValidator
from yambl_compiler.transactions.writing import dumps

LOCKS: dict[str, threading.RLock] = {}
LOCK_GUARD = threading.Lock()
LOGGER = logging.getLogger(__name__)
ID_GENERATOR = SnowflakeGenerator(worker_id=0)


@contextmanager
def measure(backend: str, operation: str, phase: str) -> Generator[None, None, None]:
    """Record elapsed time of one data operation phase, including failures.

    :param backend: Configured backend name.
    :param operation: Public operation being measured.
    :param phase: Phase within that operation.
    :yields: Control to the measured phase.
    """
    started = perf_counter()
    try:
        yield
    finally:
        LOGGER.info(
            'backend=%s operation=%s phase=%s elapsed_ms=%.3f',
            backend,
            operation,
            phase,
            (perf_counter() - started) * 1000,
        )


def read_plan(source: Path | Plan, operation: str = 'Plan validation') -> Plan:
    """Read a Plan once, or reuse an already parsed immutable Plan.

    :param source: Plan source file or parsed Plan.
    :param operation: Context used to identify a non-Plan input.
    :returns: Parsed Plan.
    :raises ValueError: If the source is not a Plan.
    """
    plan = source if isinstance(source, Plan) else parse(Path(source).read_text(encoding='utf-8'))
    if not isinstance(plan, Plan):
        raise ValueError(f'{operation} requires a Plan')
    return plan


@dataclass(frozen=True)
class FilePointer:
    """Identify the committed file generation and its transaction history.

    :param format_revision: File entry format revision.
    :param model_digest: Model semantic digest.
    :param generation: Current immutable generation ID.
    :param transactions: Committed transaction IDs in commit order.
    """

    format_revision: int
    model_digest: str
    generation: str
    transactions: tuple[str, ...]


class TransactionStatus(StrEnum):
    """Distinguish the outcomes of an already committed transaction."""

    COMMITTED = 'committed'
    CONFLICTS = 'conflicts'
    EXPORT_PENDING = 'export_pending'


@dataclass(frozen=True)
class TransactionReport:
    """Report committed effects separately from recoverable publication failures.

    :param id: Committed transaction ID.
    :param path: Managed transaction export path.
    :param status: Commit and export outcome.
    :param changes: Number of actual rollback units.
    :param warnings: Skipped conflict messages.
    :param publication_error: Export failure, empty when publication succeeded.
    """

    id: str
    path: Path
    status: TransactionStatus
    changes: int
    warnings: tuple[str, ...]
    publication_error: str = ''


def write_file(path: Path, content: str) -> None:
    """Write and synchronize a new managed file before it becomes authoritative.

    :param path: Destination in a prepared directory.
    :param content: UTF-8 text.
    :returns: None.
    """
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


class DataStore:
    """Provide the same data operations for configured SQLite and file stores."""

    reader_type: ClassVar[type[Reader]]
    schema_path: ClassVar[Path]

    def __init__(self, backend: str, path: Path, layout: Layout) -> None:
        """Retain one configuration and the process-local repository write lock.

        :param backend: sqlite or files.
        :param path: Resolved repository path.
        :param layout: Bundled model contract.
        :returns: None.
        """
        self.backend, self.path, self.layout = backend, path.resolve(), layout
        self.closed = False
        with LOCK_GUARD:
            key = os.path.normcase(str(self.path))
            self.lock = LOCKS.setdefault(key, threading.RLock())

    @classmethod
    async def configured(cls, config_path: Path) -> Self:
        """Evaluate configuration through lclang's public asynchronous API.

        :param config_path: Entry .lclcfg file.
        :returns: Configured, unopened store.
        :raises ValueError: On incompatible backend or path values.
        """
        from lclang.config import evaluate_config, load_config

        config_path = Path(config_path).resolve()
        config = await load_config(config_path)
        backend = await evaluate_config(config, 'backend')
        path = await evaluate_config(config, 'path')
        if not isinstance(backend, str) or backend not in ('sqlite', 'files'):
            raise ValueError('backend must be sqlite or files')
        if not isinstance(path, str) or not path:
            raise ValueError('path must be a nonempty string')
        layout = load_contract(cls.reader_type.model_path, cls.reader_type.contract_digest)
        return cls(backend, config_path.parent / path, layout)

    @classmethod
    async def open(cls, config_path: Path) -> Self:
        """Open a configured repository and recover committed transaction exports.

        :param config_path: Entry .lclcfg file.
        :returns: Open store.
        """
        store = await cls.configured(config_path)
        with measure(store.backend, 'open', 'validate_and_recover'), store.lock:
            backend = store.open_backend()
            backend.close()
            store.recover()
        return store

    @classmethod
    async def create(cls, config_path: Path) -> Self:
        """Initialize a new repository without overwriting any existing target.

        :param config_path: Entry .lclcfg file.
        :returns: Initialized open store.
        :raises FileExistsError: If the configured target already exists.
        """
        store = await cls.configured(config_path)
        with store.lock:
            if store.path.exists() or store.path.is_symlink():
                raise FileExistsError(store.path)
            with TemporaryDirectory(
                prefix=f'.{store.path.name}.create-', dir=store.path.parent
            ) as directory:
                prepared = cls(store.backend, Path(directory) / 'repository', store.layout)
                if store.backend == 'sqlite':
                    with closing(sqlite3.connect(prepared.path)) as connection, connection:
                        connection.execute('PRAGMA journal_mode=WAL')
                        connection.executescript(cls.schema_path.read_text(encoding='utf-8'))
                        connection.execute(
                            'CREATE TABLE tb_yambl_transactions (id TEXT PRIMARY KEY, document TEXT NOT NULL)'
                        )
                else:
                    prepared.path.mkdir()
                    tables: dict[str, list[Row]] = {
                        t.name: [
                            dict(zip((c.name for c in t.columns), row, strict=True))
                            for row in t.rows
                        ]
                        for t in store.layout.tables
                    }
                    from yambl_compiler.storage.records import LAYOUT_REVISION

                    tables['tb_yambl_metadata'] = [
                        {'key': 'layout_revision', 'value': str(LAYOUT_REVISION)},
                        {'key': 'model_digest', 'value': store.layout.model.model_digest},
                    ]
                    prepared.publish_files(Data(store.layout, tables), None)
                backend = prepared.open_backend()
                backend.close()
                prepared.path.rename(store.path)
        return store

    def ready(self) -> None:
        """Reject operations on a closed facade.

        :returns: None.
        :raises ValueError: If the facade is closed.
        """
        if self.closed:
            raise ValueError('data store is closed')

    def file_version(self) -> Path:
        """Resolve the authoritative immutable file generation.

        :returns: Validated generation directory.
        :raises ValueError: On invalid pointer metadata.
        """
        return self.path / 'versions' / self.file_pointer().generation

    def file_pointer(self) -> FilePointer:
        """Validate the file entry before using any of its paths.

        :returns: Typed committed generation and transaction history.
        :raises ValueError: On malformed or incompatible entry metadata.
        """
        metadata = cast(
            FilePointer,
            decode(loads((self.path / 'repository.json').read_text(encoding='utf-8')), FilePointer),
        )
        if (
            metadata.format_revision != FILE_REVISION
            or metadata.model_digest != self.layout.model.model_digest
        ):
            raise ValueError('file repository metadata does not match this model')
        for id_ in (metadata.generation, *metadata.transactions):
            if not valid_id(id_):
                raise ValueError('invalid file generation or transaction ID')
        if len(set(metadata.transactions)) != len(metadata.transactions):
            raise ValueError('duplicate committed transaction ID')
        if metadata.transactions and metadata.generation != metadata.transactions[-1]:
            raise ValueError('file generation is not the latest committed transaction')
        return metadata

    def snapshot(self) -> Data:
        """Load a detached candidate from one consistent backend snapshot.

        :returns: Complete candidate data.
        """
        self.ready()
        backend = self.open_backend()
        try:
            return Data(self.layout, {t.name: backend.rows(t.name, {}) for t in self.layout.tables})
        finally:
            backend.close()

    def open_backend(self) -> SQLiteBackend | FileBackend:
        """Pin a backend without loading business rows.

        :returns: Backend owned by the caller.
        """
        self.ready()
        if self.backend == 'sqlite':
            return SQLiteBackend(self.path, self.layout)
        return FileBackend(self.file_version(), self.layout)

    def read(self) -> Reader:
        """Create a stable read session independent of later commits.

        :returns: Generated reader with a pinned backend snapshot.
        """
        self.ready()
        with measure(self.backend, 'read', 'open_snapshot'), self.lock:
            return self.reader_type(self.open_backend(), self.layout)

    def transaction_directory(self) -> Path:
        """Locate the backend-independent transaction export directory.

        :returns: Managed export directory.
        """
        return (
            self.path.with_name(self.path.name + '.transactions')
            if self.backend == 'sqlite'
            else self.path / 'transactions'
        )

    def export(self, id_: str) -> Path:
        """Recover or regenerate a committed transaction file by ID.

        :param id_: Canonical transaction ID.
        :returns: Published .yatran path.
        :raises ValueError: If the transaction is not committed in this repository.
        """
        self.ready()
        if not valid_id(id_):
            raise ValueError('invalid transaction ID')
        with self.lock:
            if self.backend == 'sqlite':
                with closing(sqlite3.connect(self.path)) as connection:
                    row = connection.execute(
                        'SELECT document FROM tb_yambl_transactions WHERE id=?', (id_,)
                    ).fetchone()
                if row is None:
                    raise ValueError('unknown transaction')
                text = str(row[0])
            else:
                if id_ not in self.file_pointer().transactions:
                    raise ValueError('unknown transaction')
                text = (self.path / 'versions' / id_ / 'transaction.yatran').read_text(
                    encoding='utf-8'
                )
            transaction = parse(text)
            if (
                not isinstance(transaction, Transaction)
                or transaction.id != id_
                or transaction.plan.model != self.layout.model.model_digest
            ):
                raise ValueError('stored transaction identity does not match the repository')
            directory = self.transaction_directory()
            directory.mkdir(exist_ok=True)
            destination = directory / (id_ + '.yatran')
            temporary = directory / (id_ + '.pending')
            write_file(temporary, text)
            temporary.replace(destination)
            return destination

    def recover(self) -> None:
        """Republish committed exports after an interrupted file publication.

        :returns: None.
        """
        self.ready()
        with self.lock:
            if self.backend == 'sqlite':
                with closing(sqlite3.connect(self.path)) as connection:
                    ids = tuple(
                        str(row[0])
                        for row in connection.execute('SELECT id FROM tb_yambl_transactions')
                    )
            else:
                ids = self.file_pointer().transactions
            for id_ in ids:
                destination = self.transaction_directory() / (id_ + '.yatran')
                try:
                    transaction = parse(destination.read_text(encoding='utf-8'))
                    valid = (
                        isinstance(transaction, Transaction)
                        and transaction.id == id_
                        and transaction.plan.model == self.layout.model.model_digest
                    )
                except FileNotFoundError, ValueError:
                    valid = False
                if not valid:
                    self.export(id_)
            self.cleanup()

    def cleanup(self) -> None:
        """Remove obsolete snapshot files while retaining committed transaction records.

        Committed generations are retained because readers in this or another
        process may still use them. Unrecognized files and directories are left in place.

        :returns: None.
        :raises OSError: If managed files cannot be removed safely.
        """
        if self.backend != 'files':
            return
        with self.lock:
            self._cleanup_files()

    def _cleanup_files(self) -> None:
        """Remove uncommitted files while holding the in-process repository lock.

        :returns: None.
        :raises OSError: If managed files cannot be removed safely.
        """
        metadata = self.file_pointer()
        versions = self.path / 'versions'
        if versions.is_symlink() or versions.is_junction():
            raise OSError('cannot clean a linked versions directory')
        for version in versions.iterdir():
            if version.name == metadata.generation:
                continue
            if not valid_id(version.name) or not version.is_dir():
                continue
            tables = version / 'tables'
            indexes = version / 'indexes'
            if any(p.is_symlink() or p.is_junction() for p in (version, tables, indexes)):
                raise OSError('cannot clean a linked generation directory')
            if version.name in metadata.transactions:
                continue
            # The initial generation has no transaction ID and can also have readers.
            if not (version / 'transaction.yatran').exists():
                try:
                    backend = FileBackend(version, self.layout)
                except OSError, ValueError, TypeError:
                    pass
                else:
                    backend.close()
                    continue
            paths = [version / 'repository.json']
            paths.extend(tables / (table.name + '.jsonl') for table in self.layout.tables)
            paths.extend(indexes / (table.name + '.jsonl') for table in self.layout.tables)
            paths.append(version / 'transaction.yatran')
            for path in paths:
                if path.is_symlink() or path.is_junction():
                    raise OSError('cannot clean a linked generation file')
                path.unlink(missing_ok=True)
            if tables.exists() and not any(tables.iterdir()):
                tables.rmdir()
            if indexes.exists() and not any(indexes.iterdir()):
                indexes.rmdir()
            if not any(version.iterdir()):
                version.rmdir()
        (self.path / 'repository.pending').unlink(missing_ok=True)

    def publish_files(self, data: Data, transaction: Transaction | None) -> None:
        """Prepare immutable files, then atomically switch the root manifest.

        :param data: Validated candidate records.
        :param transaction: Commit record, or None for initial creation.
        :returns: None after the manifest commit point.
        """
        id_ = transaction.id if transaction is not None else str(ID_GENERATOR.next_id())
        version = self.path / 'versions' / id_
        version.mkdir(parents=True)
        contents = repository_contents(self.layout, data.tables)
        for name, content in contents.items():
            path = version / name
            path.parent.mkdir(exist_ok=True)
            write_file(path, content)
        pointer = self.path / 'repository.json'
        ids: list[str] = list(self.file_pointer().transactions) if pointer.exists() else []
        if transaction is not None:
            write_file(version / 'transaction.yatran', dumps(transaction))
            ids.append(id_)
        metadata = {
            'format_revision': FILE_REVISION,
            'model_digest': self.layout.model.model_digest,
            'generation': id_,
            'transactions': ids,
        }
        temporary = self.path / 'repository.pending'
        write_file(temporary, json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n')
        temporary.replace(pointer)

    def publish_incremental_files(self, data: IncrementalData, transaction: Transaction) -> None:
        """Prepare changed files and atomically publish an immutable generation.

        :param data: Validated candidate on the pinned file generation.
        :param transaction: Sealed transaction record.
        :returns: None after the root pointer commit point.
        """
        source = data.overlay.backend
        if not isinstance(source, FileBackend):
            raise ValueError('file publication requires a file snapshot')
        version = self.path / 'versions' / transaction.id
        prepare_files(source, version, self.layout, data.overlay.changes(), transaction)
        metadata = {
            'format_revision': FILE_REVISION,
            'model_digest': self.layout.model.model_digest,
            'generation': transaction.id,
            'transactions': [*self.file_pointer().transactions, transaction.id],
        }
        temporary = self.path / 'repository.pending'
        write_file(temporary, json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n')
        temporary.replace(self.path / 'repository.json')

    @contextmanager
    def mutable_candidate(self, operation: str) -> Generator[IncrementalData, None, None]:
        """Own a pinned candidate snapshot until its mutation has completed.

        :param operation: Public operation name for timing logs.
        :yields: Incremental candidate bound to one source snapshot.
        """
        with measure(self.backend, operation, 'snapshot'):
            backend = self.open_backend()
        try:
            yield IncrementalData(self.layout, backend)
        except BaseException as error:
            try:
                backend.close()
            except (OSError, sqlite3.Error, ValueError) as close_error:
                error.add_note(f'candidate snapshot close failed: {close_error}')
            raise
        else:
            try:
                backend.close()
            except (OSError, sqlite3.Error, ValueError) as error:
                LOGGER.warning('committed candidate snapshot close failed: %s', error)

    def commit(self, data: Data, transaction: Transaction) -> TransactionReport:
        """Validate and commit data together with its recoverable transaction record.

        :param data: Detached candidate.
        :param transaction: Sealed execution record.
        :returns: Explicit commit and publication outcome.
        """
        with measure(self.backend, 'commit', 'validate'):
            data.validate()
            if isinstance(data, IncrementalData):
                with self.reader_type(data.overlay, self.layout) as reader:
                    reader.validate_candidate(data)
                    changes = data.overlay.changes()
                    dictionaries = {
                        str(row['decodes_id'])
                        for table in ('tb_decodes', 'tb_decodes_entries')
                        for change in changes.get(table, ())
                        for row in (change.before, change.after)
                        if row is not None
                    }
                    removed: dict[str, set[str]] = {}
                    added: dict[str, set[str]] = {}
                    for change in changes.get('tb_decodes_entries', ()):
                        if change.before is not None:
                            row = change.before
                            removed.setdefault(str(row['decodes_id']), set()).add(str(row['code']))
                        if change.after is not None:
                            row = change.after
                            added.setdefault(str(row['decodes_id']), set()).add(str(row['code']))
                    for identity, values in removed.items():
                        values.difference_update(added.get(identity, set()))
                    if dictionaries:
                        dictionary_report = reader.validate_dictionary_references(
                            dictionaries, removed
                        )
                        if not dictionary_report.valid:
                            raise ValueError(
                                f'candidate dictionary validation failed: {dictionary_report}'
                            )
            else:
                with self.reader_type(SnapshotBackend(data.tables), self.layout) as reader:
                    report = reader.validate()
                    if not isinstance(report, Success) or not report.value.valid:
                        raise ValueError(f'candidate data validation failed: {report}')
        text = dumps(transaction)
        with measure(self.backend, 'commit', 'publish'):
            if isinstance(data, IncrementalData) and self.backend == 'sqlite':
                data.overlay.backend.close()
                publish_sqlite(self.path, self.layout, data.overlay.changes(), transaction)
            elif isinstance(data, IncrementalData):
                self.publish_incremental_files(data, transaction)
            elif self.backend == 'sqlite':
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    for table in reversed(self.layout.tables):
                        connection.execute('DELETE FROM ' + quote(table.name))
                    for table in self.layout.tables:
                        columns = tuple(c.name for c in table.columns)
                        connection.executemany(
                            'INSERT INTO '
                            + quote(table.name)
                            + ' ('
                            + ', '.join(quote(c) for c in columns)
                            + ') VALUES ('
                            + ', '.join('?' for _ in columns)
                            + ')',
                            [tuple(row[c] for c in columns) for row in data.tables[table.name]],
                        )
                    connection.execute(
                        'INSERT INTO tb_yambl_transactions VALUES (?, ?)', (transaction.id, text)
                    )
            else:
                self.publish_files(data, transaction)
        warnings = tuple(w for group in transaction.groups for w in group.warnings)
        for warning in warnings:
            LOGGER.warning('%s: %s', transaction.id, warning)
        count = sum(len(g.changes) for g in transaction.groups)
        try:
            with measure(self.backend, 'commit', 'export'):
                path = self.export(transaction.id)
        except (OSError, sqlite3.Error, ValueError) as error:
            return TransactionReport(
                transaction.id,
                self.transaction_directory() / (transaction.id + '.yatran'),
                TransactionStatus.EXPORT_PENDING,
                count,
                warnings,
                str(error),
            )
        try:
            self.cleanup()
        except (OSError, sqlite3.Error, ValueError) as error:
            LOGGER.warning('%s: obsolete snapshot cleanup pending: %s', transaction.id, error)
        return TransactionReport(
            transaction.id,
            path,
            TransactionStatus.CONFLICTS if warnings else TransactionStatus.COMMITTED,
            count,
            warnings,
        )

    @classmethod
    def validate_plan_syntax(cls, plan_path: Path | Plan) -> Plan:
        """Validate Plan syntax, names, coordinates and model-bound literal types.

        :param plan_path: UTF-8 .yaplan source file or already parsed Plan.
        :returns: Parsed Plan on success, without opening a repository.
        :raises ValueError: On malformed contents or a non-Plan document.
        :raises OSError: If the input file cannot be read.
        """
        plan = read_plan(plan_path)
        layout = load_contract(cls.reader_type.model_path, cls.reader_type.contract_digest)
        PlanValidator(layout).validate(plan)
        return plan

    def validate_plan_expectations(self, plan_path: Path | Plan) -> Plan:
        """Check expectations against the snapshot and prior ID-based write coordinates.

        :param plan_path: UTF-8 .yaplan source file or already parsed Plan.
        :returns: Parsed Plan after syntax and expectation validation; no data is written.
        :raises ValueError: On syntax, model, selector or expectation errors.
        :raises OSError: If input or repository data cannot be read.
        """
        self.ready()
        with measure(self.backend, 'validate_plan', 'parse_and_bind'):
            plan = read_plan(plan_path)
            PlanValidator(self.layout).validate(plan)
        with self.lock:
            with measure(self.backend, 'validate_plan', 'snapshot'):
                backend = self.open_backend()
            with closing(backend), measure(self.backend, 'validate_plan', 'expectations'):
                data = IncrementalData(self.layout, backend)
                Execution(data).validate_expectations(plan)
        return plan

    def apply(self, plan_path: Path | Plan) -> TransactionReport:
        """Apply an entire Plan using the configured backend.

        :param plan_path: .yaplan source file or already parsed Plan.
        :returns: Commit report.
        """
        self.ready()
        with self.lock:
            self.recover()
            with measure(self.backend, 'apply', 'parse_and_bind'):
                plan = read_plan(plan_path, 'apply')
                PlanValidator(self.layout).validate(plan)
            with self.mutable_candidate('apply') as data:
                with measure(self.backend, 'apply', 'execute'):
                    groups = Execution(data).prepare(plan)
                return self.finish(data, plan, groups)

    def finish(
        self, data: Data, plan: Plan, groups: tuple[TransactionGroup, ...], source: str = ''
    ) -> TransactionReport:
        """Seal execution metadata and publish the prepared candidate.

        :param data: Candidate data.
        :param plan: Original Plan.
        :param groups: Actual effects.
        :param source: Source transaction for rollback.
        :returns: Commit report.
        """
        if plan.model is None:
            plan = replace(plan, model=data.layout.model.model_digest)
        transaction = seal(
            Transaction(
                str(ID_GENERATOR.next_id()),
                datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                plan,
                groups,
                source,
            )
        )
        return self.commit(data, transaction)

    def rollback(
        self, transaction_path: Path, groups: tuple[str, ...] | None = None
    ) -> TransactionReport:
        """Rollback selected transaction groups while preserving later edits.

        :param transaction_path: .yatran execution record.
        :param groups: Selected group names, or all when omitted.
        :returns: Commit report including skipped conflicts.
        """
        self.ready()
        with self.lock:
            self.recover()
            transaction = parse(Path(transaction_path).read_text(encoding='utf-8'))
            if not isinstance(transaction, Transaction):
                raise ValueError('rollback requires a Transaction')
            with self.mutable_candidate('rollback') as data:
                with measure(self.backend, 'rollback', 'execute'):
                    result = prepare_rollback(data, transaction, groups)
                return self.finish(data, transaction.plan, result, transaction.id)

    def close(self) -> None:
        """Close the facade without invalidating detached read sessions.

        :returns: None.
        """
        self.closed = True

    def __enter__(self) -> Self:
        """Enter an open facade's resource scope.

        :returns: This facade.
        """
        self.ready()
        return self

    def __exit__(self, *args: object) -> None:
        """Close the facade at scope exit.

        :param args: Exception information supplied by the context manager.
        :returns: None.
        """
        self.close()
