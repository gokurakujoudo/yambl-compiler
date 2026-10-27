"""Compare persisted offset indexes against the previous full-table file reader."""

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from tests.storage.test_runtime import SOURCE
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.backends import FileBackend, Row, Selection, repository_contents
from yambl_compiler.storage.layout import build_layout
from yambl_compiler.storage.rendering import ddl

COUNT = 100_000
REPEATS = 3
WARM_LOOKUPS = 100
OUTPUT = Path(__file__).resolve().parents[2] / 'build/reports/latest/index-benchmark.json'


class Sample(TypedDict):
    """Measurements from one independent reader session."""

    first_seconds: float
    warm_seconds: float
    cached_seconds: float
    batch_seconds: float
    peak_bytes: int


def require_record(rows: list[Row], expected: str) -> None:
    """Require one matching result even when Python assertions are disabled.

    :param rows: Returned physical rows.
    :param expected: Requested ID.
    :returns: None.
    :raises ValueError: If the lookup returned a wrong number of rows or ID.
    """
    if len(rows) != 1 or rows[0].get('id') != expected:
        raise ValueError(f'index lookup returned an unexpected result for {expected}')


def run(
    output: Path, count: int = COUNT, repeats: int = REPEATS, lookups: int = WARM_LOOKUPS
) -> None:
    """Build an isolated fixture and record paired cold and warm measurements.

    :param output: Destination for the JSON measurements.
    :param count: Number of records in the fixture.
    :param repeats: Number of independent reader sessions per method.
    :param lookups: Number of distinct IDs queried in each warm pass.
    :returns: None.
    :raises ValueError: If fixture dimensions or a query result are invalid.
    """
    if count < 1 or repeats < 1 or lookups < 1 or lookups > count:
        raise ValueError('rows and repetitions must be positive; lookups must fit the rows')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'yambl-root.lclcfg').write_text("prefix: ''\n", encoding='utf-8')
        (root / 'main.yambl').write_text(SOURCE, encoding='utf-8')
        layout = build_layout(compile_repository(root))
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(ddl(layout))
            connection.executemany(
                'INSERT INTO tb_a (id,entity_type) VALUES (?,?)',
                ((f'a{i:06d}', 'A') for i in range(count)),
            )
            tables = {
                table.name: [
                    dict(row) for row in connection.execute('SELECT * FROM "' + table.name + '"')
                ]
                for table in layout.tables
            }
        for name, content in repository_contents(layout, tables).items():
            path = root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        metadata_path = root / 'repository.json'
        indexed_metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        legacy_metadata = {
            key: value for key, value in indexed_metadata.items() if key != 'indexes'
        }
        measurements: dict[str, object] = {}
        for label, metadata in [('legacy', legacy_metadata), ('indexed', indexed_metadata)]:
            metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
            samples: list[Sample] = []
            for _ in range(repeats):
                tracemalloc.start()
                try:
                    with closing(FileBackend(root, layout)) as backend:
                        started = time.perf_counter()
                        last = f'a{count - 1:06d}'
                        require_record(backend.rows('tb_a', {'id': last}), last)
                        first_seconds = time.perf_counter() - started
                        started = time.perf_counter()
                        for i in range(lookups):
                            target = f'a{i:06d}'
                            require_record(backend.rows('tb_a', {'id': target}), target)
                        warm_seconds = (time.perf_counter() - started) / lookups
                        started = time.perf_counter()
                        for i in range(lookups):
                            target = f'a{i:06d}'
                            require_record(backend.rows('tb_a', {'id': target}), target)
                        cached_seconds = (time.perf_counter() - started) / lookups
                        selections = [Selection((("id", f'a{i:06d}'),)) for i in range(lookups)]
                        started = time.perf_counter()
                        batch_rows = backend.rows_many('tb_a', selections)
                        batch_seconds = time.perf_counter() - started
                        if len(batch_rows) != lookups:
                            raise ValueError('index batch returned an unexpected number of groups')
                        for i, group in enumerate(batch_rows):
                            require_record(group, f'a{i:06d}')
                        _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                samples.append(
                    {
                        'first_seconds': first_seconds,
                        'warm_seconds': warm_seconds,
                        'cached_seconds': cached_seconds,
                        'batch_seconds': batch_seconds,
                        'peak_bytes': peak,
                    }
                )
            measurements[label] = {
                'samples': samples,
                'median_first_seconds': statistics.median(s['first_seconds'] for s in samples),
                'median_warm_seconds': statistics.median(s['warm_seconds'] for s in samples),
                'median_cached_seconds': statistics.median(s['cached_seconds'] for s in samples),
                'median_batch_seconds': statistics.median(s['batch_seconds'] for s in samples),
                'median_peak_bytes': statistics.median(s['peak_bytes'] for s in samples),
            }
        result = {
            'rows': count,
            'repeats': repeats,
            'warm_lookups_per_repeat': lookups,
            'table_bytes': (root / 'tables/tb_a.jsonl').stat().st_size,
            'index_bytes': (root / 'indexes/tb_a.jsonl').stat().st_size,
            'python': platform.python_version(),
            'platform': platform.platform(),
            'method': 'Same model and JSONL data; legacy ignores sidecar metadata. Each sample opens a fresh reader; tracemalloc begins before open. Both verify complete table on first read.',
            'measurements': measurements,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Measure file reader indexes in isolated stores.')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--rows', type=int, default=COUNT)
    parser.add_argument('--repeats', type=int, default=REPEATS)
    parser.add_argument('--lookups', type=int, default=WARM_LOOKUPS)
    args = parser.parse_args()
    run(args.output, args.rows, args.repeats, args.lookups)
