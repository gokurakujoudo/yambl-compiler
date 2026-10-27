"""Exercise the large-write benchmark with small, real backend fixtures."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.performance import index_benchmark, write_benchmark
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.yatran.model import Transaction
from yambl_compiler.storage.results import NonNormalResult, Status, Success
from yambl_compiler.storage.validation import ValidationIssue, ValidationReport

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / 'tests/performance/write_benchmark.py'


class WriteBenchmarkTests(unittest.TestCase):
    """Check CLI output, generated transactions and cleanup in isolation."""

    def test_real_backends_validate_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(PROJECT)
            environment['PYTHONUTF8'] = '1'
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            process = subprocess.run(
                [sys.executable, '-B', str(SCRIPT), '--rows', '10', '--output', str(output)],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=120,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['rows'], 10)
            self.assertEqual(report['backends'], ['files', 'sqlite'])
            self.assertEqual(len(report['generated_package_sha256']), 64)
            for backend, result in report['results'].items():
                with self.subTest(backend=backend):
                    self.assertEqual(result['fixture_validation'], 'valid')
                    self.assertEqual(result['post_rollback_validation'], 'valid')
                    self.assertEqual(result['before'], {'status': 'NO_ENTRY'})
                    self.assertEqual(result['after_apply'], {'status': 'VALUE', 'value': 'changed'})
                    self.assertEqual(result['after_rollback'], result['before'])
                    applied = result['apply']['transaction']
                    rolled = result['rollback']['transaction']
                    self.assertEqual(
                        (applied['status'], rolled['status']), ('committed', 'committed')
                    )
                    self.assertEqual((applied['changes'], rolled['changes']), (1, 1))
                    self.assertEqual((applied['warnings'], rolled['warnings']), ([], []))
                    self.assertFalse(Path(applied['path']).exists())
                    apply_transaction, rollback_transaction = parse(applied['text']), parse(
                        rolled['text']
                    )
                    self.assertIsInstance(apply_transaction, Transaction)
                    self.assertIsInstance(rollback_transaction, Transaction)
                    self.assertEqual(apply_transaction.id, applied['id'])
                    self.assertEqual(rollback_transaction.source, applied['id'])
                    self.assertEqual(apply_transaction.plan.model, report['model_digest'])
                    self.assertGreater(result['apply']['seconds'], 0)
                    self.assertGreater(result['rollback']['tracemalloc_peak_bytes'], 0)

    def test_invalid_count_does_not_create_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(PROJECT)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            process = subprocess.run(
                [sys.executable, '-B', str(SCRIPT), '--rows', '0', '--output', str(output)],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=120,
                check=False,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('rows must be positive', process.stderr)
            self.assertFalse(output.exists())

    def test_generated_reader_failure_boundaries(self):
        module = SimpleNamespace(Success=Success)
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.close.return_value = Success(None)
        store = SimpleNamespace(read=lambda: reader)
        reader.get.return_value = NonNormalResult(Status.ENTITY_NOT_FOUND)
        with self.assertRaisesRegex(ValueError, 'fixture entity missing'):
            write_benchmark.observed(module, store, 'missing')
        reader.get.return_value = Success('handle')
        reader.read.return_value = NonNormalResult(Status.EMPTY)
        self.assertEqual(write_benchmark.observed(module, store, 'id'), {'status': 'EMPTY'})
        for validity in (
            NonNormalResult(Status.ERROR),
            Success(ValidationReport((ValidationIssue('field', 'bad'),))),
        ):
            reader.validate.return_value = validity
            with (
                self.subTest(validity=validity),
                self.assertRaisesRegex(ValueError, 'validation failed'),
            ):
                write_benchmark.validate_store(module, store, 'files')

    def test_reader_lifecycle_supports_old_readers_and_preserves_close_failures(self):
        module = SimpleNamespace(Success=Success)

        class OldReader:
            def get(self, *args):
                return Success('handle')

            def read(self, *args):
                return Success('value')

            def close(self):
                return Success(None)

        self.assertEqual(
            write_benchmark.observed(module, SimpleNamespace(read=OldReader), 'id'),
            {'status': 'VALUE', 'value': 'value'},
        )
        reader = MagicMock()
        store = SimpleNamespace(read=lambda: reader)
        reader.get.return_value = Success('handle')
        reader.read.return_value = Success('value')
        reader.close.return_value = NonNormalResult(Status.ERROR, 'close failed')
        with self.assertRaisesRegex(ValueError, 'reader close failed'):
            write_benchmark.observed(module, store, 'id')
        reader.get.return_value = NonNormalResult(Status.ENTITY_NOT_FOUND)
        with self.assertRaisesRegex(ValueError, 'fixture entity missing') as raised:
            write_benchmark.observed(module, store, 'id')
        self.assertIn('reader close failed', raised.exception.__notes__[0])
        reader.close.side_effect = OSError('close raised')
        reader.get.return_value = Success('handle')
        with self.assertRaisesRegex(OSError, 'close raised'):
            write_benchmark.observed(module, store, 'id')
        reader.get.return_value = NonNormalResult(Status.ENTITY_NOT_FOUND)
        with self.assertRaisesRegex(ValueError, 'fixture entity missing') as raised:
            write_benchmark.observed(module, store, 'id')
        self.assertIn('close raised', raised.exception.__notes__[0])

    def test_one_backend_commands_and_direct_index_import(self):
        self.assertEqual(index_benchmark.COUNT, 100_000)
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(PROJECT)
            for backend in ('sqlite', 'files'):
                output = Path(directory) / (backend + '.json')
                process = subprocess.run(
                    [
                        sys.executable,
                        '-B',
                        str(SCRIPT),
                        '--rows',
                        '1',
                        '--backend',
                        backend,
                        '--output',
                        str(output),
                    ],
                    cwd=PROJECT,
                    env=environment,
                    capture_output=True,
                    encoding='utf-8',
                    timeout=120,
                    check=False,
                )
                with self.subTest(backend=backend):
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    self.assertEqual(
                        list(json.loads(output.read_text(encoding='utf-8'))['results']), [backend]
                    )

    def test_index_benchmark_runs_queries_with_python_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'index.json'
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(PROJECT)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            process = subprocess.run(
                [
                    sys.executable,
                    '-O',
                    str(PROJECT / 'tests/performance/index_benchmark.py'),
                    '--rows',
                    '4',
                    '--repeats',
                    '1',
                    '--lookups',
                    '2',
                    '--output',
                    str(output),
                ],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=120,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(
                (report['rows'], report['repeats'], report['warm_lookups_per_repeat']), (4, 1, 2)
            )
            for name in ('legacy', 'indexed'):
                self.assertGreater(report['measurements'][name]['median_warm_seconds'], 0)

    def test_index_rejects_wrong_results_and_invalid_dimensions(self):
        index_benchmark.require_record([{'id': 'a'}], 'a')
        for rows in ([], [{'id': 'b'}], [{'id': 'a'}, {'id': 'a'}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'unexpected result'):
                index_benchmark.require_record(rows, 'a')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'index.json'
            for count, repeats, lookups in ((0, 1, 1), (1, 0, 1), (1, 1, 0), (1, 1, 2)):
                with self.subTest(count=count, repeats=repeats, lookups=lookups):
                    with self.assertRaisesRegex(ValueError, 'must be positive'):
                        index_benchmark.run(output, count, repeats, lookups)
            with patch.object(index_benchmark.FileBackend, 'rows_many', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'unexpected number of groups'):
                    index_benchmark.run(output, 2, 1, 1)
            self.assertFalse(tracemalloc.is_tracing())
            self.assertFalse(output.exists())

    def test_unexpected_results_are_rejected(self):
        scenarios = (
            ([{'status': 'VALUE'}], 'unexpected initial value'),
            (
                [{'status': 'NO_ENTRY'}, {'status': 'EMPTY'}, {'status': 'NO_ENTRY'}],
                'unexpected apply or rollback',
            ),
        )
        for observations, message in scenarios:
            with (
                self.subTest(message=message),
                patch.object(write_benchmark, 'observed', side_effect=observations),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    asyncio.run(write_benchmark.run(1, ('sqlite',)))
        original = write_benchmark.measure

        def conflicted(action):
            report, timings = original(action)
            return replace(report, status='conflicts'), timings

        with patch.object(write_benchmark, 'measure', side_effect=conflicted):
            with self.assertRaisesRegex(ValueError, 'did not commit cleanly'):
                asyncio.run(write_benchmark.run(1, ('sqlite',)))

    def test_fixture_row_count_mismatch_is_rejected(self):
        original_connect = sqlite3.connect

        class WrongCount(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'SELECT COUNT(*) FROM tb_bench_record':
                    return super().execute('SELECT -1')
                return super().execute(sql, parameters)

        def connect(database, *args, **kwargs):
            if str(database).endswith('sqlite.data'):
                kwargs['factory'] = WrongCount
            return original_connect(database, *args, **kwargs)

        with patch.object(sqlite3, 'connect', side_effect=connect):
            with self.assertRaisesRegex(ValueError, 'SQLite fixture row count'):
                asyncio.run(write_benchmark.run(1, ('sqlite',)))
        original_contents = write_benchmark.repository_contents

        def corrupt_table(layout, tables):
            contents = original_contents(layout, tables)
            contents['tables/tb_bench_record.jsonl'] += '{}\n'
            return contents

        with patch.object(write_benchmark, 'repository_contents', side_effect=corrupt_table):
            with self.assertRaisesRegex(ValueError, 'file fixture row count'):
                asyncio.run(write_benchmark.run(1, ('files',)))


if __name__ == '__main__':
    unittest.main()
