"""Measure lookup preparation and matching on temporary SQLite and JSONL stores."""

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from contextlib import closing
from datetime import date
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from tests.support import load_generated
from yambl_compiler.ir import compile_repository
from yambl_compiler.storage.backends import repository_contents
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import NonNormalResult, Status, Success

OUTPUT = Path(__file__).resolve().parents[2] / 'build/reports/latest/lookup-benchmark.json'
SEED = 29102026
PROJECT = Path(__file__).resolve().parents[2]
MODEL = '''entity Issuer { id id: string attr name: string }
entity Security {
    id id: string
    attr label: string
    @sparse('lookup_sparse') attr market: string
    @dense('lookup_dense') attr sector: string
    attr issuer: Issuer
    dimension provider { value provider: string }
    dimension agency { value agency: string }
    @dimension(provider) @dimension(agency) @temporal(day: date) scd attr rating: string
}
'''
RULES = '''extend Security {
    lookup by_static { label = self.label::value() market = self.market::value() sector = self.sector::value() }
    lookup by_scd { rating = self.rating::value() }
    lookup by_ref { label = self.label::value() issuer = self.issuer::value().name::value() }
}
'''


def source_fingerprint() -> str:
    """Hash compiler sources, language resources and this benchmark.

    :returns: SHA-256 of relative paths and their contents.
    """
    source = PROJECT / 'src/yambl_compiler'
    paths = [
        path
        for path in source.rglob('*')
        if path.is_file() and path.suffix in ('.py', '.lark', '.yambl', '.json', '.lclcfg')
    ]
    paths.extend(
        (Path(__file__).resolve(), PROJECT / 'tests/support.py', PROJECT / 'pyproject.toml')
    )
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(PROJECT).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def label_for(index: int) -> str:
    """Return a fixture label shared by pairs or used by one entity.

    :param index: Zero-based entity index.
    :returns: Deterministic label.
    """
    return f"label-{index // 3}-{'pair' if index % 3 < 2 else 'single'}"


def fixture(root: Path, count: int, backends: tuple[str, ...]):
    """Create one model and temporary stores for the requested backends.

    :param root: Empty temporary directory.
    :param count: Number of securities.
    :param backends: Storage backends to prepare.
    :returns: Generated package, store paths, issuer count and model digest.
    """
    (root / 'yambl-root.lclcfg').write_text("prefix: ''\n", encoding='utf-8')
    (root / 'model.yambl').write_text(MODEL + RULES, encoding='utf-8')
    model = compile_repository(root)
    output = root / 'generated'
    layout = generate(model, output, 'benchmark_model')
    package = load_generated(output / 'benchmark_model', 'benchmark_model')
    issuer_count = max(1, count // 100)
    database = root / 'store.sqlite'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript((output / 'schema.sql').read_text(encoding='utf-8'))
        connection.executemany(
            "INSERT INTO tb_issuer (id,name__state,name) VALUES (?, 0, ?)",
            ((str(i), f'Issuer-{i}') for i in range(issuer_count)),
        )
        connection.executemany(
            "INSERT INTO tb_security (id,label__state,label,issuer__state,issuer) VALUES (?, 0, ?, 0, ?)",
            ((str(i), label_for(i), str((i // 3) % issuer_count)) for i in range(count)),
        )
        connection.executemany(
            "INSERT INTO tb_security_lookup_sparse (id,field,state,string) VALUES (?, 'Security.market', 0, 'market')",
            ((str(i),) for i in range(count)),
        )
        connection.executemany(
            "INSERT INTO tb_security_lookup_dense (id,sector__state,sector) VALUES (?, 0, 'sector')",
            ((str(i),) for i in range(count)),
        )
        connection.executemany(
            "INSERT INTO tb_security_rating (id,dim_agency,dim_provider,valid_from,valid_to,rating__state,rating) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                (str(i), agency, provider, start, end, f'rating-{i % 10}')
                for i in range(count)
                for agency, provider in (('agency', 'provider'), ('other', 'other'))
                for start, end in (
                    ('2024-01-01', '2025-01-01'),
                    ('2025-01-01', '2026-01-01'),
                    ('2026-01-01', '2027-01-01'),
                )
            ),
        )
        connection.commit()
        if 'files' in backends:
            connection.row_factory = sqlite3.Row
            rows = {
                table.name: [
                    dict(row) for row in connection.execute(f'SELECT * FROM "{table.name}"')
                ]
                for table in layout.tables
            }
            files = root / 'files'
            for relative, content in repository_contents(layout, rows).items():
                destination = files / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding='utf-8', newline='\n')
    paths = {'sqlite': database, 'files': root / 'files'}
    return package, paths, issuer_count, model.model_digest


def open_reader(reader_type, backend: str, path: Path):
    """Open the selected backend.

    :param reader_type: Generated reader class.
    :param backend: Backend name.
    :param path: Store location.
    :returns: Open reader.
    :raises ValueError: If the store cannot be opened.
    """
    result = reader_type.open_sqlite(path) if backend == 'sqlite' else reader_type.open_files(path)
    if not isinstance(result, Success):
        raise ValueError(f'open {backend}: {result}')
    return result.value


def run_prepare(reader, rule_name, inputs):
    """Call the generated preparation method.

    :param reader: Open generated reader.
    :param rule_name: Lookup rule name.
    :param inputs: Required coordinates.
    :returns: Detached matcher.
    :raises ValueError: If preparation fails.
    """
    result = getattr(reader, 'prepare_security_' + rule_name)(**inputs)
    if not isinstance(result, Success):
        raise ValueError(f'prepare {rule_name}: {result}')
    return result.value


def keys(key_type, scenario: str, count: int, batch: int, issuer_count: int):
    """Construct a deterministic mix of matches, duplicates and misses.

    :param key_type: Generated input record class.
    :param scenario: Rule category.
    :param count: Number of securities.
    :param batch: Input record count.
    :param issuer_count: Number of shared issuers.
    :returns: Ordered key records.
    """
    randomizer = random.Random(SEED + batch)
    result = []
    for position in range(batch):
        if position % 10 == 1:
            result.append(result[-1])
            continue
        index = randomizer.randrange(count)
        label = label_for(index) if position % 5 else 'missing'
        if scenario == 'static':
            result.append(key_type(label=label, market='market', sector='sector'))
        elif scenario == 'scd':
            result.append(key_type(rating=f'rating-{index % 10}' if position % 5 else 'missing'))
        else:
            result.append(key_type(label=label, issuer=f'Issuer-{(index // 3) % issuer_count}'))
    return result


def verify(matcher, key_type, ambiguous_type, scenario: str, count: int):
    """Check a known match and a miss before retaining timings.

    :param matcher: Prepared matcher.
    :param key_type: Generated input record class.
    :param ambiguous_type: Generated ambiguous-result class.
    :param scenario: Rule category.
    :param count: Number of candidates.
    :returns: None.
    :raises ValueError: If the known results or statistics differ.
    """
    first = (
        key_type(label='label-0-pair', market='market', sector='sector')
        if scenario == 'static'
        else (
            key_type(rating='rating-0')
            if scenario == 'scd'
            else key_type(label='label-0-pair', issuer='Issuer-0')
        )
    )
    missing = (
        key_type(label='missing', market='market', sector='sector')
        if scenario == 'static'
        else (
            key_type(rating='missing')
            if scenario == 'scd'
            else key_type(label='missing', issuer='Issuer-0')
        )
    )
    ids = tuple(
        sorted(str(i) for i in (range(0, count, 10) if scenario == 'scd' else range(min(count, 2))))
    )
    expected = Success(ids[0]) if len(ids) == 1 else ambiguous_type(ids)
    if matcher.stats.scanned_entities != count or matcher.stats.indexed_entities != count:
        raise ValueError(f'{scenario}: unexpected matcher statistics')
    if matcher.match_many([first, missing]) != Success(
        (expected, NonNormalResult(Status.NO_ENTRY))
    ):
        raise ValueError(f'{scenario}: reference results differ')


def measured_prepare(reader, rule_name, matcher_type, inputs):
    """Time preparation and its actual backend row calls.

    :param reader: Open reader.
    :param rule_name: Lookup rule name.
    :param matcher_type: Generated matcher class.
    :param inputs: Required coordinates.
    :returns: Matcher, total, row-call and index-build seconds, and table call counts.
    """
    elapsed = 0.0
    index_seconds = 0.0
    counts = Counter()
    original = reader._backend.rows_many

    def counted(table, selections, columns=()):
        """Measure one real backend call.

        :param table: Physical table name.
        :param selections: Row selections.
        :param columns: Projected columns.
        :returns: Real backend results.
        """
        nonlocal elapsed
        started = time.perf_counter()
        try:
            return original(table, selections, columns)
        finally:
            elapsed += time.perf_counter() - started
            counts[table] += 1

    original_init = matcher_type.__init__

    def index_init(instance, record_type, fields, rows, skipped):
        """Time creation of the final index from resolved candidate keys.

        :param instance: Matcher being initialized.
        :param record_type: Input record class.
        :param fields: Input validation rules.
        :param rows: Resolved candidate keys and IDs.
        :param skipped: Number of skipped candidates.
        :returns: None.
        """
        nonlocal index_seconds
        started = time.perf_counter()
        try:
            original_init(instance, record_type, fields, rows, skipped)
        finally:
            index_seconds += time.perf_counter() - started

    with (
        patch.object(reader._backend, 'rows_many', side_effect=counted),
        patch.object(matcher_type, '__init__', index_init),
    ):
        started = time.perf_counter()
        matcher = run_prepare(reader, rule_name, inputs)
        seconds = time.perf_counter() - started
    return matcher, seconds, elapsed, index_seconds, dict(counts)


def io_sample(
    reader_type, backend, path, rule_name, key_type, ambiguous_type, inputs, scenario, count
):
    """Count real SQL statements or JSONL table bytes in a separate preparation.

    :param reader_type: Generated reader class.
    :param backend: Backend name.
    :param path: Store path.
    :param rule_name: Lookup rule name.
    :param key_type: Input record type.
    :param ambiguous_type: Generated ambiguous-result class.
    :param inputs: Required coordinates.
    :param scenario: Rule category.
    :param count: Number of candidates.
    :returns: SQL statement and JSONL table-byte counts where available.
    """
    reader = open_reader(reader_type, backend, path)
    statements: list[str] = []
    table_bytes: Counter[str] = Counter()
    original = Path.read_bytes

    def counted(file):
        """Count bytes returned by the real table-file read.

        :param file: File being read.
        :returns: Original file contents.
        """
        content = original(file)
        if file.parent.name == 'tables' and file.suffix == '.jsonl':
            table_bytes[file.name] += len(content)
        return content

    try:
        if backend == 'sqlite':
            reader._backend.connection.set_trace_callback(statements.append)
        with patch.object(Path, 'read_bytes', counted):
            matcher = run_prepare(reader, rule_name, inputs)
        reader.close()
        verify(matcher, key_type, ambiguous_type, scenario, count)
        return {
            'sql_select_statements': (
                sum(
                    statement.lstrip().upper().startswith(('SELECT', 'WITH'))
                    for statement in statements
                )
                if backend == 'sqlite'
                else None
            ),
            'jsonl_table_bytes': dict(table_bytes) if backend == 'files' else None,
        }
    finally:
        if not reader._closed:
            reader.close()


def memory_sample(reader_type, backend, path, rule_name, inputs, records):
    """Measure Python allocation peaks separately from timing.

    :param reader_type: Generated reader class.
    :param backend: Backend name.
    :param path: Store path.
    :param rule_name: Lookup rule name.
    :param inputs: Required coordinates.
    :param records: Preconstructed input batch for repeated matching.
    :returns: Traced allocation measurements.
    """
    gc.collect()
    tracemalloc.start()
    try:
        reader = open_reader(reader_type, backend, path)
        opened, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        matcher = run_prepare(reader, rule_name, inputs)
        before_close, peak = tracemalloc.get_traced_memory()
        reader.close()
        del reader
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
        if matcher.stats.scanned_entities == 0:
            raise ValueError('memory sample has no candidates')
        for _ in range(10):
            result = matcher.match_many(records)
            if not isinstance(result, Success) or len(result.value) != len(records):
                raise ValueError('memory sample batch failed')
            del result
        gc.collect()
        after_batches, _ = tracemalloc.get_traced_memory()
        return {
            'reader_open_bytes': opened,
            'prepare_peak_traced_bytes': peak,
            'prepare_peak_above_open_bytes': max(0, peak - opened),
            'before_reader_close_bytes': before_close,
            'after_reader_close_bytes': retained,
            'released_on_reader_close_bytes': max(0, before_close - retained),
            'after_ten_batches_bytes': after_batches,
        }
    finally:
        tracemalloc.stop()


def run(counts, batch_sizes, backends, repeats):
    """Run the requested benchmark matrix in temporary stores.

    :param counts: Entity counts.
    :param batch_sizes: Input counts per batch.
    :param backends: Backend names.
    :param repeats: Timing repetitions per scenario.
    :returns: Measured samples and environment metadata.
    """
    fingerprint = source_fingerprint()
    samples = []
    for count in counts:
        with tempfile.TemporaryDirectory(prefix='yambl-lookup-benchmark-') as temporary:
            package, paths, issuer_count, model_digest = fixture(Path(temporary), count, backends)
            reader_type = package.BenchmarkReader
            for backend in backends:
                for scenario, rule_name, key_type, matcher_type, inputs in (
                    (
                        'static',
                        'by_static',
                        package.SecurityByStaticKey,
                        package.SecurityByStaticMatcher,
                        {},
                    ),
                    (
                        'scd',
                        'by_scd',
                        package.SecurityByScdKey,
                        package.SecurityByScdMatcher,
                        {'day': date(2025, 6, 1), 'provider': 'provider', 'agency': 'agency'},
                    ),
                    (
                        'reference',
                        'by_ref',
                        package.SecurityByRefKey,
                        package.SecurityByRefMatcher,
                        {},
                    ),
                ):
                    cold, warm, matching = [], [], []
                    for _ in range(repeats):
                        reader = open_reader(reader_type, backend, paths[backend])
                        try:
                            matcher, total, io, index, table_calls = measured_prepare(
                                reader, rule_name, matcher_type, inputs
                            )
                            verify(matcher, key_type, package.AmbiguousMatch, scenario, count)
                            cold.append(
                                {
                                    'seconds': total,
                                    'backend_rows_seconds': io,
                                    'resolution_and_candidate_keys_seconds': max(
                                        0.0, total - io - index
                                    ),
                                    'index_build_seconds': index,
                                    'table_calls': table_calls,
                                }
                            )
                            matcher, total, io, index, table_calls = measured_prepare(
                                reader, rule_name, matcher_type, inputs
                            )
                            verify(matcher, key_type, package.AmbiguousMatch, scenario, count)
                            warm.append(
                                {
                                    'seconds': total,
                                    'backend_rows_seconds': io,
                                    'resolution_and_candidate_keys_seconds': max(
                                        0.0, total - io - index
                                    ),
                                    'index_build_seconds': index,
                                    'table_calls': table_calls,
                                }
                            )
                        finally:
                            reader.close()
                        for batch in batch_sizes:
                            started = time.perf_counter()
                            records = keys(key_type, scenario, count, batch, issuer_count)
                            key_seconds = time.perf_counter() - started
                            started = time.perf_counter()
                            outcome = matcher.match_many(records)
                            match_seconds = time.perf_counter() - started
                            if not isinstance(outcome, Success) or len(outcome.value) != batch:
                                raise ValueError(f'{scenario}: invalid batch result')
                            distribution: Counter[str] = Counter()
                            for item in outcome.value:
                                if isinstance(item, Success):
                                    distribution['unique'] += 1
                                elif isinstance(item, package.AmbiguousMatch):
                                    distribution['ambiguous'] += 1
                                elif (
                                    isinstance(item, NonNormalResult)
                                    and item.status == Status.NO_ENTRY
                                ):
                                    distribution['missing'] += 1
                                else:
                                    raise ValueError(
                                        f'{scenario}: unexpected match outcome: {item}'
                                    )
                            matching.append(
                                {
                                    'batch_size': batch,
                                    'key_construction_and_sampling_seconds': key_seconds,
                                    'match_seconds': match_seconds,
                                    'end_to_end_seconds': key_seconds + match_seconds,
                                    'records_per_second': batch / match_seconds,
                                    'outcomes': dict(distribution),
                                    'repeated_inputs': batch - len(set(records)),
                                }
                            )
                    io = io_sample(
                        reader_type,
                        backend,
                        paths[backend],
                        rule_name,
                        key_type,
                        package.AmbiguousMatch,
                        inputs,
                        scenario,
                        count,
                    )
                    memory = memory_sample(
                        reader_type,
                        backend,
                        paths[backend],
                        rule_name,
                        inputs,
                        keys(key_type, scenario, count, max(batch_sizes), issuer_count),
                    )
                    samples.append(
                        {
                            'entities': count,
                            'model_digest': model_digest,
                            'lookup_ir_digest': model_digest,
                            'backend': backend,
                            'scenario': scenario,
                            'issuer_count': issuer_count,
                            'scd_periods': 3,
                            'dimensions': 2,
                            'dimension_combinations': 2,
                            'cold': cold,
                            'warm': warm,
                            'matching': matching,
                            'io': io,
                            'memory': memory,
                            'cold_median_seconds': statistics.median(x['seconds'] for x in cold),
                            'warm_median_seconds': statistics.median(x['seconds'] for x in warm),
                            'preparation_medians': {
                                temperature: {
                                    metric: statistics.median(item[metric] for item in observations)
                                    for metric in (
                                        'seconds',
                                        'backend_rows_seconds',
                                        'resolution_and_candidate_keys_seconds',
                                        'index_build_seconds',
                                    )
                                }
                                for temperature, observations in (('cold', cold), ('warm', warm))
                            },
                            'match_median_seconds': {
                                str(batch): statistics.median(
                                    x['match_seconds'] for x in matching if x['batch_size'] == batch
                                )
                                for batch in batch_sizes
                            },
                            'batch_medians': {
                                str(batch): {
                                    metric: statistics.median(
                                        item[metric]
                                        for item in matching
                                        if item['batch_size'] == batch
                                    )
                                    for metric in (
                                        'key_construction_and_sampling_seconds',
                                        'match_seconds',
                                        'end_to_end_seconds',
                                        'records_per_second',
                                    )
                                }
                                for batch in batch_sizes
                            },
                        }
                    )
    if source_fingerprint() != fingerprint:
        raise RuntimeError('compiler sources changed during the benchmark; discard these samples')
    return {
        'method': 'This benchmark calls generated preparation methods and uses generated key and matcher classes. Timing, I/O counts and traced allocations use separate runs. Row-call time includes backend integrity checks. JSONL byte counts measure table reads through Path.read_bytes; memory totals include Python tracing and runtime bookkeeping.',
        'python': sys.version,
        'platform': platform.platform(),
        'processor': platform.processor(),
        'logical_cpus': os.cpu_count(),
        'compiler_version': version('yambl-compiler'),
        'dependencies': {name: version(name) for name in ('lclang', 'lark', 'jsonschema')},
        'source_sha256': fingerprint,
        'seed': SEED,
        'counts': list(counts),
        'batch_sizes': list(batch_sizes),
        'backends': list(backends),
        'repeats': repeats,
        'samples': samples,
    }


def main():
    """Run the selected matrix and publish a JSON report after success.

    :returns: None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', nargs='+', type=int, default=[10000, 100000, 300000])
    parser.add_argument('--batch-sizes', nargs='+', type=int, default=[1000, 10000])
    parser.add_argument(
        '--backends', nargs='+', choices=('sqlite', 'files'), default=['sqlite', 'files']
    )
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    if any(value < 1 for value in (*args.counts, *args.batch_sizes)) or args.repeats < 1:
        parser.error('counts, batch sizes and repeats must be positive')
    report = run(args.counts, args.batch_sizes, tuple(args.backends), args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + '.tmp')
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n'
    )
    os.replace(temporary, args.output)
    print(args.output)


if __name__ == '__main__':
    main()
