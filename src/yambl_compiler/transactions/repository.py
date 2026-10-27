"""Open configured data stores and atomically publish recoverable transactions."""

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar, Literal, Self, cast
from uuid import UUID, uuid4

from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.yaplan.model import Plan
from yambl_compiler.languages.yatran.model import Transaction, TransactionGroup
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.storage.backends import FileBackend, Row, SQLiteBackend, repository_contents
from yambl_compiler.storage.contract import decode, load_contract, loads
from yambl_compiler.storage.records import Layout, quote
from yambl_compiler.storage.results import Success
from yambl_compiler.storage.runtime import Reader
from yambl_compiler.transactions.editing import Data
from yambl_compiler.transactions.execution import Execution, rollback
from yambl_compiler.transactions.snapshot import SnapshotBackend
from yambl_compiler.transactions.validation import PlanValidator
from yambl_compiler.transactions.writing import dumps

LOCKS: dict[str, threading.RLock] = {}
LOCK_GUARD = threading.Lock()
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilePointer:
    """Identify the committed file generation and its transaction history.

    :param format_revision: File entry format revision.
    :param model_digest: Model semantic digest.
    :param generation: Current immutable generation UUID.
    :param transactions: Committed transaction UUIDs in commit order.
    """

    format_revision: int
    model_digest: str
    generation: str
    transactions: tuple[str, ...]


@dataclass(frozen=True)
class TransactionReport:
    """Report committed effects separately from recoverable publication failures.

    :param id: Committed transaction UUID.
    :param path: Managed transaction export path.
    :param status: Commit and export outcome.
    :param changes: Number of actual rollback units.
    :param warnings: Skipped conflict messages.
    :param publication_error: Export failure, empty when publication succeeded.
    """

    id: str
    path: Path
    status: Literal['committed', 'committed_with_warnings', 'publication_pending']
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
            self.lock = LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

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
        with store.lock:
            store.snapshot()
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
                prepared.snapshot()
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
        if metadata.format_revision != 2 or metadata.model_digest != self.layout.model.model_digest:
            raise ValueError('file repository metadata does not match this model')
        for id_ in (metadata.generation, *metadata.transactions):
            if str(UUID(id_)) != id_:
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
        backend = (
            SQLiteBackend(self.path, self.layout)
            if self.backend == 'sqlite'
            else FileBackend(self.file_version(), self.layout)
        )
        try:
            return Data(self.layout, {t.name: backend.rows(t.name, {}) for t in self.layout.tables})
        finally:
            backend.close()

    def read(self) -> Reader:
        """Create a stable read session independent of later commits.

        :returns: Generated reader with a pinned detached snapshot.
        """
        self.ready()
        with self.lock:
            data = self.snapshot()
        return self.reader_type(SnapshotBackend(data.tables), self.layout)

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
        """Recover or regenerate a committed transaction file by UUID.

        :param id_: Canonical transaction UUID.
        :returns: Published .yatran path.
        :raises ValueError: If the transaction is not committed in this repository.
        """
        self.ready()
        if str(UUID(id_)) != id_:
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

        Read sessions own detached data, so they do not reference generation files.
        Unrecognized files and directories are left in place.

        :returns: None.
        :raises OSError: If managed files cannot be removed safely.
        """
        if self.backend != 'files':
            return
        metadata = self.file_pointer()
        versions = self.path / 'versions'
        if versions.is_symlink() or versions.is_junction():
            raise OSError('cannot clean a linked versions directory')
        for version in versions.iterdir():
            if version.name == metadata.generation:
                continue
            try:
                canonical = str(UUID(version.name)) == version.name
            except ValueError:
                canonical = False
            if not canonical or not version.is_dir():
                continue
            tables = version / 'tables'
            if any(p.is_symlink() or p.is_junction() for p in (version, tables)):
                raise OSError('cannot clean a linked generation directory')
            paths = [version / 'repository.json']
            paths.extend(tables / (table.name + '.jsonl') for table in self.layout.tables)
            if version.name not in metadata.transactions:
                paths.append(version / 'transaction.yatran')
            for path in paths:
                if path.is_symlink() or path.is_junction():
                    raise OSError('cannot clean a linked generation file')
                path.unlink(missing_ok=True)
            if tables.exists() and not any(tables.iterdir()):
                tables.rmdir()
            if not any(version.iterdir()):
                version.rmdir()
        (self.path / 'repository.pending').unlink(missing_ok=True)

    def publish_files(self, data: Data, transaction: Transaction | None) -> None:
        """Prepare immutable files, then atomically switch the root manifest.

        :param data: Validated candidate records.
        :param transaction: Commit record, or None for initial creation.
        :returns: None after the manifest commit point.
        """
        id_ = transaction.id if transaction is not None else str(uuid4())
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
            'format_revision': 2,
            'model_digest': self.layout.model.model_digest,
            'generation': id_,
            'transactions': ids,
        }
        temporary = self.path / 'repository.pending'
        write_file(temporary, json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n')
        temporary.replace(pointer)

    def commit(self, data: Data, transaction: Transaction) -> TransactionReport:
        """Validate and commit data together with its recoverable transaction record.

        :param data: Detached candidate.
        :param transaction: Sealed execution record.
        :returns: Explicit commit and publication outcome.
        """
        data.validate()
        reader = self.reader_type(SnapshotBackend(data.tables), self.layout)
        try:
            report = reader.validate()
            if not isinstance(report, Success) or not report.value.valid:
                raise ValueError(f'candidate data validation failed: {report}')
        finally:
            reader.close()
        text = dumps(transaction)
        if self.backend == 'sqlite':
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
            path = self.export(transaction.id)
        except OSError as error:
            return TransactionReport(
                transaction.id,
                self.transaction_directory() / (transaction.id + '.yatran'),
                'publication_pending',
                count,
                warnings,
                str(error),
            )
        try:
            self.cleanup()
        except OSError as error:
            LOGGER.warning('%s: obsolete snapshot cleanup pending: %s', transaction.id, error)
        return TransactionReport(
            transaction.id,
            path,
            'committed_with_warnings' if warnings else 'committed',
            count,
            warnings,
        )

    @classmethod
    def validate_plan_syntax(cls, plan_path: Path) -> Plan:
        """Validate Plan syntax, names, coordinates and model-bound literal types.

        :param plan_path: UTF-8 .yaplan source file.
        :returns: Parsed Plan on success, without opening a repository.
        :raises ValueError: On malformed contents or a non-Plan document.
        :raises OSError: If the input file cannot be read.
        """
        plan = parse(Path(plan_path).read_text(encoding='utf-8'))
        if not isinstance(plan, Plan):
            raise ValueError('Plan validation requires a Plan')
        layout = load_contract(cls.reader_type.model_path, cls.reader_type.contract_digest)
        PlanValidator(layout).validate(plan)
        return plan

    def validate_plan_expectations(self, plan_path: Path) -> Plan:
        """Check Plan expectations against one unchanged repository snapshot.

        :param plan_path: UTF-8 .yaplan source file.
        :returns: Parsed Plan after syntax and expectation validation.
        :raises ValueError: On syntax, model, selector or expectation errors.
        :raises OSError: If input or repository data cannot be read.
        """
        self.ready()
        plan = self.validate_plan_syntax(plan_path)
        with self.lock:
            Execution(self.snapshot()).validate_expectations(plan)
        return plan

    def apply(self, plan_path: Path) -> TransactionReport:
        """Apply an entire Plan using the configured backend.

        :param plan_path: .yaplan source file.
        :returns: Commit report.
        """
        self.ready()
        with self.lock:
            self.recover()
            plan = parse(Path(plan_path).read_text(encoding='utf-8'))
            if not isinstance(plan, Plan):
                raise ValueError('apply requires a Plan')
            data = self.snapshot()
            groups = Execution(data).apply(plan)
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
        transaction = seal(
            Transaction(
                str(uuid4()),
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
            data = self.snapshot()
            result = rollback(data, transaction, groups)
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
