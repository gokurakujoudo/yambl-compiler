"""Measure single-record commits in isolated, large generated repositories."""

import argparse
import asyncio
import gc
import hashlib
import importlib
import json
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Protocol, cast

from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import NonNormalResult, Partial, Success
from yambl_compiler.storage.runtime import Reader
from yambl_compiler.transactions.repository import DataStore
from yambl_compiler.transactions.writing import dumps

SOURCE = "entity BenchRecord { id key: string static attr label: string }\n"
OUTPUT = Path(__file__).resolve().parents[2] / 'build/reports/latest/write-benchmark.json'


class GeneratedModule(Protocol):
    """Describe the generated interfaces used by this benchmark."""

    Success: type[Success[object]]
    BenchData: type[DataStore]


@contextmanager
def reader_session(module: GeneratedModule, store: DataStore) -> Generator[Reader, None, None]:
    """Close readers from either the current or preceding generated package.

    :param module: Generated package.
    :param store: Open data interface.
    :yields: Open reader.
    :raises ValueError: If reader closure reports an error without an earlier failure.
    """
    reader = store.read()
    try:
        yield reader
    finally:
        earlier = sys.exception()
        try:
            closed = reader.close()
        except Exception as error:
            if earlier is None:
                raise
            earlier.add_note(f'reader close failed: {error}')
        else:
            if not isinstance(closed, module.Success):
                message = f'reader close failed: {closed}'
                if earlier is None:
                    raise ValueError(message)
                earlier.add_note(message)


def fingerprint(root: Path) -> str:
    """Hash sorted relative names and content while excluding Python caches.

    :param root: Generated package directory.
    :returns: Content fingerprint.
    """
    result = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if (
            path.is_file()
            and '__pycache__' not in path.parts
            and path.suffix not in ('.pyc', '.pyo')
        ):
            for value in (path.relative_to(root).as_posix().encode('utf-8'), path.read_bytes()):
                result.update(len(value).to_bytes(8, 'big'))
                result.update(value)
    return result.hexdigest()


def observed(module: GeneratedModule, store: DataStore, key: str) -> dict[str, object]:
    """Read one generated entity and field without retaining a read session.

    :param module: Generated package.
    :param store: Open generated data interface.
    :param key: Entity ID to read.
    :returns: Observed status and, for normal values, the value.
    :raises ValueError: If the entity cannot be found.
    """
    with reader_session(module, store) as reader:
        found = reader.get('BenchRecord', key)
        if not isinstance(found, module.Success):
            raise ValueError(f'fixture entity missing: {found}')
        value = reader.read(found.value, 'BenchRecord.label', {})
        if isinstance(value, module.Success):
            return {'status': 'VALUE', 'value': value.value}
        return {'status': str(cast(NonNormalResult | Partial[object], value).status)}


def validate_store(module: GeneratedModule, store: DataStore, backend: str) -> None:
    """Require the generated reader's full data validation to succeed.

    :param module: Generated package.
    :param store: Open generated data interface.
    :param backend: Backend name in failure diagnostics.
    :returns: None.
    :raises ValueError: If the full data check fails.
    """
    with reader_session(module, store) as reader:
        validity = reader.validate()
        if not isinstance(validity, module.Success) or not validity.value.valid:
            raise ValueError(f'{backend} data validation failed: {validity}')


def measure[T](action: Callable[[], T]) -> tuple[T, dict[str, object]]:
    """Measure one real operation; tracemalloc captures Python allocations only.

    :param action: Operation to time.
    :returns: Operation result and measurement.
    """
    tracemalloc.start()
    start = time.perf_counter()
    try:
        report = action()
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return report, {'seconds': elapsed, 'tracemalloc_peak_bytes': peak}


async def run(rows: int, backend_names: tuple[str, ...]) -> dict[str, object]:
    """Build valid fixtures, verify them, then time generated API transactions.

    :param rows: Number of initial entity records.
    :param backend_names: Backends to measure in order.
    :returns: Report with inputs, measurements and transaction records.
    :raises ValueError: If the requested size, fixture or transaction is invalid.
    """
    if rows < 1:
        raise ValueError('rows must be positive')
    with tempfile.TemporaryDirectory(prefix='yambl-write-benchmark-') as directory:
        root = Path(directory)
        model_root = root / 'model'
        model_root.mkdir()
        (model_root / 'yambl-root.lclcfg').write_text("prefix: ''\n", encoding='utf-8')
        (model_root / 'main.yambl').write_text(SOURCE, encoding='utf-8')
        model = compile_repository(model_root)
        package_root = root / 'generated'
        layout = generate(model, package_root, 'bench_model')
        sys.path.insert(0, str(package_root))
        try:
            module = cast(GeneratedModule, importlib.import_module('bench_model'))
        finally:
            sys.path.pop(0)
        key = f'r{rows - 1:08d}'
        plan = Plan(
            'single_write',
            model.model_digest,
            (
                Group(
                    'change',
                    (Operation('update', 'BenchRecord', ('key', 'label'), ((key, 'changed'),)),),
                ),
            ),
        )
        plan_text = dumps(plan)
        plan_path = root / 'single.yaplan'
        plan_path.write_text(plan_text, encoding='utf-8', newline='\n')
        stores: dict[str, DataStore] = {}
        setup_started = time.perf_counter()
        try:
            for backend in backend_names:
                config = root / f'{backend}.lclcfg'
                config.write_text(
                    f"backend: '{backend}'\npath: '{backend}.data'\n", encoding='utf-8'
                )
                stores[backend] = await module.BenchData.create(config)
            # Fixture construction deliberately uses the generated SQL schema and
            # layout. The measured writes below use only the generated API.
            with closing(sqlite3.connect(':memory:')) as fixture:
                fixture.row_factory = sqlite3.Row
                fixture.executescript((package_root / 'schema.sql').read_text(encoding='utf-8'))
                fixture.executemany(
                    'INSERT INTO tb_bench_record (id) VALUES (?)',
                    ((f'r{i:08d}',) for i in range(rows)),
                )
                tables = {
                    table.name: [
                        dict(row) for row in fixture.execute('SELECT * FROM "' + table.name + '"')
                    ]
                    for table in layout.tables
                }
            if 'sqlite' in stores:
                with closing(sqlite3.connect(stores['sqlite'].path)) as connection, connection:
                    connection.executemany(
                        'INSERT INTO tb_bench_record (id) VALUES (?)',
                        ((f'r{i:08d}',) for i in range(rows)),
                    )
                    if (
                        connection.execute('SELECT COUNT(*) FROM tb_bench_record').fetchone()[0]
                        != rows
                    ):
                        raise ValueError(
                            'SQLite fixture row count does not match the requested size'
                        )
            if 'files' in stores:
                version = stores['files'].file_version()
                for name, content in repository_contents(layout, tables).items():
                    destination = version / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding='utf-8', newline='\n')
                with (version / 'tables/tb_bench_record.jsonl').open(encoding='utf-8') as records:
                    if sum(1 for _ in records) != rows:
                        raise ValueError('file fixture row count does not match the requested size')
            del tables
            gc.collect()
            setup_seconds = time.perf_counter() - setup_started
            results: dict[str, object] = {}
            for backend, store in stores.items():
                validate_store(module, store, backend)
                before = observed(module, store, key)
                if before != {'status': 'NO_ENTRY'}:
                    raise ValueError(f'{backend} unexpected initial value: {before}')
                applied, apply = measure(lambda: store.apply(plan_path))
                after = observed(module, store, key)
                rolled, rollback = measure(lambda: store.rollback(applied.path))
                restored = observed(module, store, key)
                if (str(applied.status), str(rolled.status)) != ('committed', 'committed'):
                    raise ValueError(f'{backend} transaction did not commit cleanly')
                if after != {'status': 'VALUE', 'value': 'changed'} or restored != before:
                    raise ValueError(f'{backend} unexpected apply or rollback result')
                validate_store(module, store, backend)
                apply['transaction'] = {
                    'id': applied.id,
                    'status': str(applied.status),
                    'changes': applied.changes,
                    'path': str(applied.path),
                    'warnings': list(applied.warnings),
                    'text': applied.path.read_text(encoding='utf-8'),
                }
                rollback['transaction'] = {
                    'id': rolled.id,
                    'status': str(rolled.status),
                    'changes': rolled.changes,
                    'path': str(rolled.path),
                    'warnings': list(rolled.warnings),
                    'text': rolled.path.read_text(encoding='utf-8'),
                }
                results[backend] = {
                    'fixture_validation': 'valid',
                    'post_rollback_validation': 'valid',
                    'before': before,
                    'after_apply': after,
                    'after_rollback': restored,
                    'apply': apply,
                    'rollback': rollback,
                }
            return {
                'rows': rows,
                'backends': list(backend_names),
                'python': sys.version,
                'platform': platform.platform(),
                'model_source': SOURCE,
                'model_digest': model.model_digest,
                'compiler_version': model.compiler_version,
                'generated_package_sha256': fingerprint(package_root / 'bench_model'),
                'package_hash_format': 'sha256-length-prefixed-path-and-content-v1',
                'benchmark_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'plan_sha256': hashlib.sha256(plan_text.encode('utf-8')).hexdigest(),
                'setup_seconds': setup_seconds,
                'method': 'Fixture rows are constructed directly using generated SQL schema and storage layout in temporary stores; generated Reader validates each fixture. Only generated Data.apply and Data.rollback are timed, each with tracemalloc; its peak reports Python allocations and tracing affects execution time. Temporary stores and transaction paths are removed after results are captured.',
                'results': results,
            }
        finally:
            for store in stores.values():
                store.close()
            for name in tuple(sys.modules):
                if name == 'bench_model' or name.startswith('bench_model.'):
                    del sys.modules[name]


def main() -> None:
    """Run an isolated benchmark and save its JSON report.

    :returns: None after writing the report.
    """
    parser = argparse.ArgumentParser(
        description='Measure a single write against generated backends.'
    )
    parser.add_argument('--rows', type=int, default=100_000)
    parser.add_argument('--backend', choices=('files', 'sqlite', 'both'), default='both')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    backends = ('files', 'sqlite') if args.backend == 'both' else (args.backend,)
    result = asyncio.run(run(args.rows, backends))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(args.output)


if __name__ == '__main__':
    main()
