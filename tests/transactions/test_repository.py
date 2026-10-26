"""Exercise unified configured data access and durable transaction exports."""

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success
from yambl_compiler.transactions.model import Group, Operation, Plan
from yambl_compiler.transactions.repository import DataStore
from yambl_compiler.transactions.syntax import parse
from yambl_compiler.transactions.writing import dumps

CRASH_WORKER = '''
import asyncio
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from tests.support import load_generated
from yambl_compiler.transactions import repository

root, config, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
module = load_generated(root / 'generated' / 'sample_model', 'sample_model')
class Store(repository.DataStore):
    reader_type = module.SampleReader
    schema_path = root / 'generated' / 'schema.sql'
store = asyncio.run(Store.open(config))
original_write = repository.write_file
original_replace = Path.replace
original_unlink = Path.unlink
original_connect = sqlite3.connect

def write(path, content):
    original_write(path, content)
    if phase == 'prepare' and path.name == 'transaction.yatran':
        os._exit(93)

def replace(path, target):
    if path.name == 'repository.pending' and phase == 'before_commit':
        os._exit(93)
    result = original_replace(path, target)
    if path.name == 'repository.pending' and phase == 'after_commit':
        os._exit(93)
    return result

def unlink(path, *args, **kwargs):
    result = original_unlink(path, *args, **kwargs)
    if phase == 'cleanup' and path.name == 'repository.json':
        os._exit(93)
    return result

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        if phase == 'before_commit':
            os._exit(93)
        result = super().__exit__(*args)
        if phase == 'after_commit':
            os._exit(93)
        return result

def connect(*args, **kwargs):
    return original_connect(*args, **kwargs, factory=Connection)

def export(id_):
    os._exit(93)

with patch.object(repository, 'write_file', write), patch.object(Path, 'replace', replace), patch.object(Path, 'unlink', unlink):
    with patch.object(sqlite3, 'connect', connect):
        if phase == 'export':
            store.export = export
        store.apply(root / 'demo.yaplan')
raise RuntimeError('crash boundary was not reached')
'''


class RepositoryTests(CatalogCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')

        class Store(DataStore):
            reader_type = self.module.SampleReader
            schema_path = output / 'schema.sql'

        self.store_type = Store
        self.plan = Plan(
            'demo',
            self.catalog.layout.model.model_digest,
            (
                Group(
                    'entities', (Operation('insert', 'A', ('key', 'text'), (('a', 'original'),)),)
                ),
            ),
        )
        self.plan_path = self.write('demo.yaplan', dumps(self.plan))

    async def configured(self, backend, suffix=''):
        config = self.write(
            f'{backend}{suffix}.lclcfg', f"backend: '{backend}'\npath: '{backend}{suffix}.data'\n"
        )
        return await self.store_type.create(config), config

    async def test_same_interface_and_pinned_reader_for_both_backends(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, config = await self.configured(backend)
                self.addCleanup(store.close)
                initial = store.apply(self.plan_path)
                self.assertEqual(initial.status, 'committed')
                self.assertTrue(initial.path.is_file())
                old_reader = store.read()
                self.addCleanup(old_reader.close)
                patch_plan = Plan(
                    'change',
                    self.plan.model,
                    (
                        Group(
                            'labels', (Operation('update', 'A', ('key', 'text'), (('a', 'new'),)),)
                        ),
                    ),
                )
                path = self.write('change.yaplan', dumps(patch_plan))
                changed = store.apply(path)
                reader = store.read()
                self.addCleanup(reader.close)
                self.assertEqual(reader.get_a('a').value.text, Success('new'))
                self.assertEqual(old_reader.get_a('a').value.text, Success('original'))
                reverted = store.rollback(changed.path)
                self.assertEqual(reverted.status, 'committed')
                restored = store.read()
                self.addCleanup(restored.close)
                self.assertEqual(restored.get_a('a').value.text, Success('original'))
                self.assertEqual(parse(reverted.path.read_text()).source, changed.id)
                reopened = await self.store_type.open(config)
                reopened.close()
                with self.assertRaises(FileExistsError):
                    await self.store_type.create(config)

    async def test_export_failure_is_committed_and_recovers_without_reapply(self):
        for backend in ('sqlite', 'files'):
            store, config = await self.configured(backend)
            self.addCleanup(store.close)
            with patch.object(store, 'export', side_effect=OSError('publication failed')):
                report = store.apply(self.plan_path)
            self.assertEqual(report.status, 'publication_pending')
            self.assertFalse(report.path.exists())
            reopened = await self.store_type.open(config)
            self.addCleanup(reopened.close)
            self.assertEqual(parse(report.path.read_text()).id, report.id)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                reopened.apply(self.plan_path)
            self.assertEqual(reopened.export(report.id), report.path)

    async def test_config_types_paths_and_closed_facade(self):
        for text in ("backend: 'unknown'\npath: 'data'", "backend: 'sqlite'\npath: 1"):
            with self.assertRaises(ValueError):
                await self.store_type.configured(self.write('bad.lclcfg', text))
        store, _ = await self.configured('files')
        self.assertEqual(store.path, self.root / 'files.data')
        with store:
            self.assertIs(store.__enter__(), store)
        with self.assertRaisesRegex(ValueError, 'closed'):
            store.read()

    async def test_file_pointer_rejects_invalid_history_without_writes(self):
        store, config = await self.configured('files')
        self.addCleanup(store.close)
        report = store.apply(self.plan_path)
        pointer = store.path / 'repository.json'
        original = pointer.read_text(encoding='utf-8')
        metadata = json.loads(original)
        cases = [
            ('[]', 'expected object'),
            (original.replace('"format_revision":2', '"format_revision":true'), 'scalar type'),
            (original.replace('"format_revision":2', '"format_revision":2.0'), 'JSON number'),
            ('{"generation":"x",' + original[1:], 'duplicate JSON key'),
        ]
        for changes, message in (
            ({'transactions': [1]}, 'scalar type'),
            ({'transactions': ['../outside']}, 'UUID'),
            ({'transactions': [report.id, report.id]}, 'duplicate committed'),
            ({'generation': str(uuid4())}, 'latest committed'),
            ({'format_revision': 1}, 'does not match'),
            ({'model_digest': '0' * 64}, 'does not match'),
            ({'extra': 1}, 'unknown fields'),
        ):
            cases.append((json.dumps(metadata | changes), message))
        try:
            for content, message in cases:
                with self.subTest(content=content):
                    pointer.write_text(content, encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, message):
                        await self.store_type.open(config)
                    self.assertEqual(pointer.read_text(encoding='utf-8'), content)
                    self.assertEqual(parse(report.path.read_text()).id, report.id)
        finally:
            pointer.write_text(original, encoding='utf-8')
        reader = store.read()
        self.addCleanup(reader.close)
        self.assertEqual(reader.get_a('a').value.text, Success('original'))

    async def test_recovery_repairs_exports_before_next_write(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                self.addCleanup(store.close)
                initial = store.apply(self.plan_path)
                original = initial.path.read_bytes()
                for damaged in ('broken', dumps(self.plan)):
                    initial.path.write_text(damaged, encoding='utf-8')
                    store.recover()
                    self.assertEqual(initial.path.read_bytes(), original)
                initial.path.unlink()
                empty = Plan('empty', self.plan.model, (Group('all', ()),))
                empty_path = self.write('empty.yaplan', dumps(empty))
                with patch.object(store, 'export', side_effect=OSError('still unavailable')):
                    with self.assertRaisesRegex(OSError, 'still unavailable'):
                        store.apply(empty_path)
                self.assertEqual(store.read().get_a('a').value.text, Success('original'))
                report = store.apply(empty_path)
                self.assertEqual(report.status, 'committed')
                self.assertEqual(report.changes, 0)
                self.assertEqual(initial.path.read_bytes(), original)
                initial.path.write_text('broken', encoding='utf-8')
                undone = store.rollback(initial.path)
                self.assertEqual(undone.status, 'committed')
                self.assertEqual(initial.path.read_bytes(), original)

    async def test_precommit_failure_leaves_data_unchanged(self):
        for backend in ('sqlite', 'files'):
            store, _ = await self.configured(backend)
            self.addCleanup(store.close)
            before = store.snapshot().tables
            invalid = Plan(
                'invalid',
                self.plan.model,
                (
                    Group(
                        'all',
                        (
                            Operation('insert', 'A', ('key',), (('a',),)),
                            Operation('update', 'A', ('key', 'count'), (('a', 'wrong'),)),
                        ),
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, 'integer'):
                store.apply(self.write('invalid.yaplan', dumps(invalid)))
            self.assertEqual(store.snapshot().tables, before)

    async def test_cleanup_preserves_history_and_unknown_files(self):
        store, config = await self.configured('files')
        self.addCleanup(store.close)
        initial_version = store.file_version()
        marker = initial_version / 'keep.txt'
        marker.write_text('user file', encoding='utf-8')
        old_reader = store.read()
        self.addCleanup(old_reader.close)
        report = store.apply(self.plan_path)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'user file')
        self.assertFalse((initial_version / 'tables').exists())
        self.assertFalse((initial_version / 'repository.json').exists())
        self.assertEqual(old_reader.get_a('a').status, Status.NO_ENTRY)
        reader = store.read()
        self.addCleanup(reader.close)
        self.assertEqual(reader.get_a('a').value.text, Success('original'))
        committed_version = store.file_version()
        original = report.path.read_bytes()
        store.rollback(report.path)
        self.assertEqual(
            list(committed_version.iterdir()), [committed_version / 'transaction.yatran']
        )
        report.path.unlink()
        reopened = await self.store_type.open(config)
        self.addCleanup(reopened.close)
        self.assertEqual(report.path.read_bytes(), original)

    async def test_cleanup_failure_keeps_commit_successful_and_retries_on_open(self):
        store, config = await self.configured('files')
        self.addCleanup(store.close)
        old_version = store.file_version()
        unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path == old_version / 'repository.json':
                raise OSError('cleanup unavailable')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', failing_unlink):
            with self.assertLogs('yambl_compiler.transactions.repository', level='WARNING') as logs:
                report = store.apply(self.plan_path)
        self.assertEqual(report.status, 'committed')
        self.assertEqual(parse(report.path.read_text()).plan, self.plan)
        self.assertIn('cleanup unavailable', logs.output[0])
        self.assertTrue(old_version.exists())
        reopened = await self.store_type.open(config)
        self.addCleanup(reopened.close)
        self.assertFalse(old_version.exists())
        reader = reopened.read()
        self.addCleanup(reader.close)
        self.assertEqual(reader.get_a('a').value.text, Success('original'))

    async def test_process_termination_at_commit_boundaries(self):
        for backend in ('sqlite', 'files'):
            phases = ('before_commit', 'after_commit', 'export')
            if backend == 'files':
                phases = ('prepare', *phases, 'cleanup')
            for phase in phases:
                with self.subTest(backend=backend, phase=phase):
                    store, config = await self.configured(backend, phase)
                    self.addCleanup(store.close)
                    before = store.snapshot().tables
                    child = await asyncio.to_thread(
                        subprocess.run,
                        [sys.executable, '-c', CRASH_WORKER, str(self.root), str(config), phase],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(child.returncode, 93, child.stdout + child.stderr)
                    reopened = await self.store_type.open(config)
                    self.addCleanup(reopened.close)
                    documents = list(reopened.transaction_directory().glob('*.yatran'))
                    if backend == 'files':
                        for version in (reopened.path / 'versions').iterdir():
                            if version != reopened.file_version():
                                self.assertEqual(
                                    list(version.iterdir()), [version / 'transaction.yatran']
                                )
                        self.assertFalse((reopened.path / 'repository.pending').exists())
                    if phase in ('prepare', 'before_commit'):
                        self.assertEqual(reopened.snapshot().tables, before)
                        self.assertEqual(documents, [])
                        report = reopened.apply(self.plan_path)
                        self.assertEqual(report.status, 'committed')
                    else:
                        self.assertEqual(len(documents), 1)
                        transaction = parse(documents[0].read_text(encoding='utf-8'))
                        self.assertEqual(transaction.plan, self.plan)
                        reader = reopened.read()
                        self.addCleanup(reader.close)
                        self.assertEqual(reader.get_a('a').value.text, Success('original'))
