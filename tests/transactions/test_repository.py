"""Exercise unified configured data access and durable transaction exports."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lclang.error import LclError, render_error
from lclang.utils import SnowflakeGenerator

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.shared.documents import parse
from yambl_compiler.languages.shared.model import Condition, ObjectValue, Symbol
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.languages.yatran.writing import seal
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status, Success
from yambl_compiler.transactions.writing import dumps

CRASH_WORKER = '''
import asyncio
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from uuid import uuid4
from tests.support import load_generated
from yambl_compiler.transactions import repository
from yambl_compiler.transactions import incremental_publication as publication

root, config, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
module = load_generated(root / 'generated' / 'sample_model', 'sample_model')
class Store(repository.DataStore):
    reader_type = module.SampleReader
    schema_path = root / 'generated' / 'schema.sql'
store = asyncio.run(Store.open(config))
original_replace = Path.replace
original_unlink = Path.unlink
original_connect = sqlite3.connect
original_prepare = repository.prepare_files
original_fsync = os.fsync

def fsync(fd):
    original_fsync(fd)
    if phase == 'prepare':
        os._exit(93)

def prepare(*args, **kwargs):
    with patch.object(publication.os, 'fsync', fsync):
        result = original_prepare(*args, **kwargs)
    if phase == 'cleanup':
        orphan = store.path / 'versions' / str(uuid4())
        (orphan / 'tables').mkdir(parents=True)
        (orphan / 'repository.json').write_text('unpublished', encoding='utf-8')
    return result

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
    def commit(self):
        assert self.execute('SELECT COUNT(*) FROM tb_yambl_transactions').fetchone()[0] == 1
        assert self.execute("SELECT text FROM tb_a WHERE id='a'").fetchone()[0] == 'original'
        if phase == 'before_commit':
            os._exit(93)
        result = super().commit()
        if phase == 'after_commit':
            os._exit(93)
        return result

def connect(*args, **kwargs):
    return original_connect(*args, **kwargs, factory=Connection)

def export(id_):
    os._exit(93)

with patch.object(repository, 'prepare_files', prepare), patch.object(Path, 'replace', replace), patch.object(Path, 'unlink', unlink):
    with patch.object(sqlite3, 'connect', connect):
        if phase == 'export':
            store.export = export
        store.apply(root / 'demo.yaplan')
raise RuntimeError('crash boundary was not reached')
'''

CREATE_CRASH_WORKER = '''
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
original_rename, original_write, original_connect = Path.rename, repository.write_file, sqlite3.connect
def rename(path, target):
    if phase == 'before_commit': os._exit(94)
    result = original_rename(path, target)
    if phase == 'after_commit': os._exit(94)
    return result
def write(path, content):
    original_write(path, content)
    if phase == 'prepare': os._exit(94)
class Connection(sqlite3.Connection):
    def executescript(self, script):
        result = super().executescript(script)
        if phase == 'prepare': os._exit(94)
        return result
def connect(*args, **kwargs):
    return original_connect(*args, **kwargs, factory=Connection)
with patch.object(Path, 'rename', rename), patch.object(repository, 'write_file', write), patch.object(sqlite3, 'connect', connect):
    asyncio.run(module.SampleData.create(config))
raise RuntimeError('initialization crash boundary not reached')
'''


class RepositoryTests(CatalogCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')

        self.store_type = self.module.SampleData
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

    async def test_snowflake_ids_advance_after_generator_restart(self):
        runtime = sys.modules[self.store_type.__mro__[1].__module__]
        clock_ns = (1_704_067_200_000 + 86_400_000) * 1_000_000
        with patch('lclang.utils.snowflake_id.time_ns', return_value=clock_ns):
            for backend in ('sqlite', 'files'):
                with self.subTest(backend=backend):
                    with patch.object(runtime, 'ID_GENERATOR', SnowflakeGenerator(worker_id=0)):
                        store, config = await self.configured(backend)
                        self.addCleanup(store.close)
                        if backend == 'files':
                            initial = json.loads((store.path / 'repository.json').read_text())
                            self.assertTrue(initial['generation'].isdecimal())
                        first = store.apply(self.plan_path)
                    with patch.object(runtime, 'ID_GENERATOR', SnowflakeGenerator(worker_id=0)):
                        second = store.rollback(first.path)
                    self.assertTrue(first.id.isdecimal())
                    self.assertTrue(second.id.isdecimal())
                    self.assertGreater(int(second.id), int(first.id))
                    self.assertEqual(parse(second.path.read_text()).source, first.id)
                    reopened = await self.store_type.open(config)
                    reopened.close()

    async def test_snowflake_id_rejects_clock_behind_repository_watermark(self):
        runtime = sys.modules[self.store_type.__mro__[1].__module__]
        now_ns = (1_704_067_200_000 + 86_400_000) * 1_000_000
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                with (
                    patch('lclang.utils.snowflake_id.time_ns', return_value=now_ns),
                    patch.object(runtime, 'ID_GENERATOR', SnowflakeGenerator(worker_id=0)),
                ):
                    store, _ = await self.configured(backend)
                    self.addCleanup(store.close)
                    first = store.apply(self.plan_path)
                with (
                    patch('lclang.utils.snowflake_id.time_ns', return_value=now_ns - 1_000_000),
                    patch.object(runtime, 'ID_GENERATOR', SnowflakeGenerator(worker_id=0)),
                    self.assertRaisesRegex(ValueError, 'cannot advance beyond repository IDs'),
                ):
                    store.rollback(first.path)
                with closing(store.read()) as reader:
                    self.assertEqual(reader.get_a('a').value.text, Success('original'))

    async def test_expect_failure_is_atomic_and_inline_insert_round_trips(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                self.addCleanup(store.close)
                store.apply(self.plan_path)
                insert = Operation(
                    'insert',
                    'A',
                    ('key', 'text', 'members'),
                    (('b', 'second', (1, 2)), ('c', 'third', ())),
                )
                failure = Operation(
                    'delete', 'A.price', (), (), (Condition('key', 'a'),), expect='any'
                )
                path = self.write(
                    'batch.yaplan',
                    dumps(replace(self.plan, groups=(Group('A', (insert, failure)),))),
                )
                with self.assertRaisesRegex(ValueError, 'expect any matched 0'):
                    store.apply(path)
                with closing(store.read()) as reader:
                    self.assertEqual(reader.get_a('b').status, Status.NO_ENTRY)
                    self.assertEqual(reader.get_a('a').value.text, Success('original'))
                path.write_text(
                    dumps(replace(self.plan, groups=(Group('A', (insert,)),))), encoding='utf-8'
                )
                result = store.apply(path)
                with closing(store.read()) as reader:
                    self.assertEqual(reader.get_a('b').value.text, Success('second'))
                    self.assertEqual(len(reader.get_a('b').value.members.value), 2)
                store.rollback(result.path)
                with closing(store.read()) as reader:
                    self.assertEqual(reader.get_a('b').status, Status.NO_ENTRY)

    async def test_two_plan_validation_levels_do_not_write(self):
        self.assertEqual(self.store_type.validate_plan_syntax(self.plan_path).name, 'demo')
        invalid = self.write('invalid.yaplan', 'not a plan')
        with self.assertRaises(ValueError):
            self.store_type.validate_plan_syntax(invalid)
        invalid.write_text(
            dumps(
                replace(
                    self.plan, groups=(Group('g', (Operation('insert', 'A', ('key', 'bad'), ()),)),)
                )
            ),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'not writable'):
            self.store_type.validate_plan_syntax(invalid)
        for backend in ('sqlite', 'files'):
            store, _ = await self.configured(backend)
            self.addCleanup(store.close)
            initial = store.apply(self.plan_path)
            with self.assertRaisesRegex(ValueError, 'requires a Plan'):
                self.store_type.validate_plan_syntax(initial.path)
            before = store.snapshot().tables
            files = {p.name: p.read_bytes() for p in store.transaction_directory().iterdir()}
            for op, passes in (
                (
                    Operation('delete', 'A.text', (), (), (Condition('key', 'a'),), expect='one'),
                    True,
                ),
                (
                    Operation('delete', 'A.price', (), (), (Condition('key', 'a'),), expect='any'),
                    False,
                ),
                (Operation('delete', 'A', ('key',), (('a',),), expect='one'), True),
                (Operation('delete', 'A', ('key',), (('absent',),), expect='any'), False),
                (Operation('update', 'A', ('key', 'text'), (('absent', 'name'),)), True),
            ):
                plan = replace(self.plan, groups=(Group('g', (op,)),))
                path = self.write('validate.yaplan', dumps(plan))
                self.assertEqual(
                    store.validate_plan_syntax(path).groups[0].operations[0].target, op.target
                )
                if passes:
                    self.assertEqual(store.validate_plan_expectations(path).name, plan.name)
                else:
                    with self.assertRaisesRegex(ValueError, 'matched 0'):
                        store.validate_plan_expectations(path)
            self.assertEqual(store.snapshot().tables, before)
            self.assertEqual(
                {p.name: p.read_bytes() for p in store.transaction_directory().iterdir()}, files
            )

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

    async def test_open_and_read_do_not_materialize_all_tables(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                created, config = await self.configured(backend)
                created.close()
                with patch.object(self.store_type, 'snapshot', side_effect=AssertionError('eager')):
                    store = await self.store_type.open(config)
                    self.addCleanup(store.close)
                    with store.read() as reader:
                        self.assertEqual(reader.get_a('absent').status, Status.NO_ENTRY)

    async def test_reuse_parsed_plan_and_bind_optional_digest_on_commit(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                self.addCleanup(store.close)
                source = self.write(
                    f'{backend}-unbound.yaplan', dumps(replace(self.plan, model=None))
                )
                parsed = self.store_type.validate_plan_syntax(source)
                self.assertIsNone(parsed.model)
                source.unlink()
                with self.assertLogs(self.store_type.apply.__module__, level='INFO') as logs:
                    self.assertEqual(store.validate_plan_expectations(parsed).name, 'demo')
                    report = store.apply(parsed)
                self.assertIs(report.status, self.module.TransactionStatus.COMMITTED)
                self.assertEqual(
                    parse(report.path.read_text(encoding='utf-8')).plan.model, self.plan.model
                )
                self.assertTrue(any('phase=parse_and_bind' in line for line in logs.output))
                self.assertTrue(any('phase=execute' in line for line in logs.output))
                self.assertTrue(any('phase=publish' in line for line in logs.output))
                undone = store.rollback(report.path)
                self.assertIs(undone.status, self.module.TransactionStatus.COMMITTED)
                self.assertIs(
                    store.rollback(undone.path).status, self.module.TransactionStatus.COMMITTED
                )
                with self.assertRaisesRegex(ValueError, 'digest|model'):
                    store.apply(replace(parsed, model='0' * 64))

    async def test_validation_reader_close_failure_prevents_commit(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                self.addCleanup(store.close)
                candidate_type = self.store_type.commit.__globals__['IncrementalData']
                overlay_type = candidate_type.__init__.__globals__['Overlay']
                with patch.object(overlay_type, 'close', side_effect=OSError('close failed')):
                    with self.assertRaisesRegex(RuntimeError, 'reader close failed: close failed'):
                        store.apply(self.plan_path)
                with store.read() as reader:
                    self.assertEqual(reader.get_a('a').status, Status.NO_ENTRY)

    async def test_expect_after_prior_insert_by_id_on_both_backends(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                self.addCleanup(store.close)
                inserted = Operation('insert', 'A', ('key',), (('b',),))
                field = Operation(
                    'replace',
                    'A.text',
                    ('value',),
                    (('new',),),
                    (Condition('key', 'b'),),
                    expect='one',
                )
                plan = replace(
                    self.plan,
                    groups=(Group('insert', (inserted,)), Group('replace', (field,))),
                )
                source = self.write(f'{backend}-insert-expect.yaplan', dumps(plan))
                checked = store.validate_plan_expectations(source)
                self.assertEqual(checked.name, plan.name)
                with store.read() as reader:
                    self.assertEqual(reader.get_a('b').status, Status.NO_ENTRY)
                report = store.apply(source)
                with store.read() as reader:
                    self.assertEqual(reader.get_a('b').value.read_text(), Success('new'))
                store.rollback(report.path)
                with store.read() as reader:
                    self.assertEqual(reader.get_a('b').status, Status.NO_ENTRY)

                temporal = Operation(
                    'replace',
                    'A.price',
                    ('day', 'source', 'value'),
                    (),
                    (Condition('key', 'c'),),
                    expect='any',
                )
                absent = replace(
                    self.plan,
                    groups=(
                        Group('insert', (replace(inserted, rows=(('c',),)),)),
                        Group('replace', (temporal,)),
                    ),
                )
                source = self.write(f'{backend}-missing-expect.yaplan', dumps(absent))
                with self.assertRaisesRegex(ValueError, 'expect any matched 0'):
                    store.validate_plan_expectations(source)
                with store.read() as reader:
                    self.assertEqual(reader.get_a('c').status, Status.NO_ENTRY)

    async def test_old_reader_loads_its_snapshot_after_another_process_commits(self):
        worker = '''
import asyncio, sys
from pathlib import Path
from tests.support import load_generated
root, config, plan = map(Path, sys.argv[1:])
module = load_generated(root / 'generated' / 'sample_model', 'sample_model')
with asyncio.run(module.SampleData.open(config)) as store:
    report = store.apply(plan)
    assert report.status == module.TransactionStatus.COMMITTED, report
'''
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, config = await self.configured(backend)
                self.addCleanup(store.close)
                store.apply(self.plan_path)
                reader = store.read()
                self.addCleanup(reader.close)
                updated = replace(
                    self.plan,
                    groups=(
                        Group(
                            'update', (Operation('update', 'A', ('key', 'text'), (('a', 'new'),)),)
                        ),
                    ),
                )
                path = self.write(f'{backend}-external.yaplan', dumps(updated))
                child = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, '-c', worker, str(self.root), str(config), str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                # The field has not been loaded before the other process committed.
                self.assertEqual(reader.get_a('a').value.read_text(), Success('original'))
                with store.read() as fresh:
                    self.assertEqual(fresh.get_a('a').value.read_text(), Success('new'))

    async def test_writes_and_recovery_serialize_per_repository(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                writer, config = await self.configured(backend)
                rollback_store = await self.store_type.open(config)
                recovery_store = await self.store_type.open(config)
                independent, _ = await self.configured(backend, '-independent')
                for store in (writer, rollback_store, recovery_store, independent):
                    self.addCleanup(store.close)
                self.assertIs(writer.lock, rollback_store.lock)
                self.assertIs(writer.lock, recovery_store.lock)
                self.assertIsNot(writer.lock, independent.lock)
                writer.apply(self.plan_path)
                changed_plan = replace(
                    self.plan,
                    groups=(
                        Group(
                            'text',
                            (Operation('update', 'A', ('key', 'text'), (('a', 'changed'),)),),
                        ),
                    ),
                )
                changed = writer.apply(self.write('text.yaplan', dumps(changed_plan)))
                source = self.write('undo.yatran', changed.path.read_text())
                count_plan = replace(
                    self.plan,
                    groups=(
                        Group('count', (Operation('update', 'A', ('key', 'count'), (('a', 7),)),)),
                    ),
                )
                count_path = self.write('count.yaplan', dumps(count_plan))
                committing, release, restored_export = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                rollback_started, recovery_started = threading.Event(), threading.Event()
                original_commit, original_export = writer.commit, self.store_type.export

                def held_commit(data, transaction):
                    committing.set()
                    if not release.wait(10):
                        raise TimeoutError('test did not release the pending commit')
                    return original_commit(data, transaction)

                def observed_export(store, id_):
                    restored_export.set()
                    return original_export(store, id_)

                def undo():
                    rollback_started.set()
                    return rollback_store.rollback(source)

                def recover():
                    recovery_started.set()
                    recovery_store.recover()

                with ThreadPoolExecutor(max_workers=4) as pool:
                    with (
                        patch.object(writer, 'commit', held_commit),
                        patch.object(self.store_type, 'export', observed_export),
                    ):
                        applying = pool.submit(writer.apply, count_path)
                        try:
                            self.assertTrue(committing.wait(10))
                            changed.path.unlink()
                            restored_export.clear()
                            undoing = pool.submit(undo)
                            recovering = pool.submit(recover)
                            self.assertTrue(rollback_started.wait(10))
                            self.assertTrue(recovery_started.wait(10))
                            self.assertFalse(restored_export.wait(0.1))
                            self.assertFalse(undoing.done())
                            self.assertFalse(recovering.done())
                            separate = pool.submit(independent.apply, self.plan_path).result(
                                timeout=10
                            )
                            self.assertEqual(separate.status, 'committed')
                            self.assertFalse(changed.path.exists())
                        finally:
                            release.set()
                        applied = applying.result(timeout=10)
                        undone = undoing.result(timeout=10)
                        recovering.result(timeout=10)
                self.assertEqual(applied.status, 'committed')
                self.assertEqual(undone.status, 'committed')
                self.assertEqual(changed.path.read_text(), source.read_text())
                with closing(writer.read()) as reader:
                    entity = reader.get_a('a').value
                    self.assertEqual(entity.text, Success('original'))
                    self.assertEqual(entity.count, Success(7))
                    self.assertTrue(reader.validate().value.valid)

    async def test_scd_collection_deletion_round_trip_on_both_backends(self):
        scope = (Condition('key', 'a'),)
        initial = Plan(
            'owners',
            self.plan.model,
            (
                Group(
                    'initial',
                    (
                        Operation('insert', 'A', ('key',), (('a',),)),
                        Operation('insert', 'Other', ('number',), ((1,), (2,))),
                        Operation(
                            'replace',
                            'A.owners',
                            ('valid_from', 'valid_to', 'value'),
                            (('2026-01-01', '2026-02-01', (1, 2)),),
                            scope,
                        ),
                    ),
                ),
            ),
        )
        deletion = Plan(
            'remove',
            self.plan.model,
            (
                Group(
                    'window',
                    (
                        Operation(
                            'delete',
                            'A.owners',
                            (),
                            (),
                            (*scope, Condition('day', '2026-01-10', '2026-01-20')),
                        ),
                    ),
                ),
            ),
        )
        initial_path = self.write('owners.yaplan', dumps(initial))
        deletion_path = self.write('remove.yaplan', dumps(deletion))
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, config = await self.configured(backend)
                with store:
                    self.assertEqual(store.apply(initial_path).status, 'committed')
                    removed = store.apply(deletion_path)
                    self.assertEqual(removed.status, 'committed')
                    change = parse(removed.path.read_text()).groups[0].changes[0]
                    self.assertEqual(
                        change.before[0].rows, (('a', '2026-01-10', '2026-01-20', (1, 2)),)
                    )
                    self.assertEqual(change.after[0].rows, ())
                with await self.store_type.open(config) as reopened:
                    with closing(reopened.read()) as reader:
                        records = reader.get_a('a').value.owners
                        self.assertIsInstance(records, Success)
                        self.assertEqual(len(records.value), 2)
                        self.assertTrue(reader.validate().value.valid)
                    restored = reopened.rollback(removed.path)
                    self.assertEqual(restored.status, 'committed')
                    change = parse(restored.path.read_text()).groups[0].changes[0]
                    self.assertEqual(
                        change.after[0].rows, (('a', '2026-01-10', '2026-01-20', (1, 2)),)
                    )
                    redone = reopened.rollback(restored.path)
                    self.assertEqual(redone.status, 'committed')
                    with closing(reopened.read()) as reader:
                        self.assertEqual(len(reader.get_a('a').value.owners.value), 2)
                        self.assertTrue(reader.validate().value.valid)

    async def test_partial_conflicts_commit_report_and_warning_on_both_backends(self):
        for backend in ('sqlite', 'files'):
            store, _ = await self.configured(backend)
            self.addCleanup(store.close)
            store.apply(self.plan_path)
            changed = Plan(
                'change',
                self.plan.model,
                (
                    Group(
                        'patch',
                        (
                            Operation(
                                'update', 'A', ('key', 'text', 'count'), (('a', 'updated', 2),)
                            ),
                        ),
                    ),
                ),
            )
            transaction = store.apply(self.write('patch.yaplan', dumps(changed)))
            later = Plan(
                'later',
                self.plan.model,
                (Group('later', (Operation('update', 'A', ('key', 'text'), (('a', 'later'),)),)),),
            )
            store.apply(self.write('later.yaplan', dumps(later)))
            with self.assertLogs(
                'yambl_compiler.transactions.repository', level='WARNING'
            ) as messages:
                reversed_report = store.rollback(transaction.path)
            self.assertEqual(reversed_report.status, 'conflicts')
            self.assertIs(reversed_report.status, self.module.TransactionStatus.CONFLICTS)
            self.assertEqual(reversed_report.changes, 1)
            self.assertEqual(len(reversed_report.warnings), 1)
            self.assertIn('A.text', messages.output[0])
            recorded = parse(reversed_report.path.read_text(encoding='utf-8'))
            self.assertEqual(recorded.source, transaction.id)
            self.assertTrue(recorded.time.endswith('Z'))
            self.assertEqual(recorded.groups[0].warnings, reversed_report.warnings)
            with closing(store.read()) as reader:
                self.assertEqual(reader.get_a('a').value.text, Success('later'))
                self.assertEqual(reader.get_a('a').value.count.status, Status.NO_ENTRY)
            redo = store.rollback(reversed_report.path)
            self.assertEqual((redo.status, redo.changes, redo.warnings), ('committed', 1, ()))
            with closing(store.read()) as reader:
                self.assertEqual(reader.get_a('a').value.text, Success('later'))
                self.assertEqual(reader.get_a('a').value.count, Success(2))

    async def test_invalid_candidate_constraints_leave_both_backends_unchanged(self):
        owner = (Condition('key', 'a'),)
        scope = (*owner, Condition('source', 'Vendor'))
        period = (*owner, Condition('day', '2026-01-01', '2026-02-01'))
        operations = [
            Operation('replace', 'A.price', ('day', 'value'), (), (Condition('source', 'Vendor'),)),
            Operation('delete', 'A.price', (), (), (*scope, Condition('day', '2026-01-01'))),
            Operation(
                'delete', 'A.price', (), (), (*scope, Condition('day', '2026-02-01', '2026-01-01'))
            ),
            Operation('delete', 'A.price', (), (), (*scope, Condition('unknown', 'x'))),
            Operation('replace', 'A.price', ('day',), (('2026-01-01',),), scope),
            Operation('replace', 'A.price', ('day', 'value'), ((1, 1),), scope),
            Operation('replace', 'A.price', ('day', 'value'), (('2026-2-1', 1),), scope),
            Operation(
                'replace',
                'A.price',
                ('day', 'value'),
                (('2026-02-01', 1),),
                (*scope, Condition('day', '2026-01-01', '2026-02-01')),
            ),
            Operation(
                'replace',
                'A.price',
                ('day', 'value'),
                (('2026-01-01', 1), ('2026-01-01', 2)),
                scope,
            ),
            Operation(
                'replace',
                'A.outstanding',
                ('valid_from', 'valid_to', 'value'),
                (('2025-12-01', '2026-01-15', 1),),
                period,
            ),
            Operation(
                'replace',
                'A.outstanding',
                ('valid_from', 'valid_to', 'value'),
                (('2026-01-01', '2026-02-01', 1), ('2026-01-10', '2026-03-01', 2)),
                owner,
            ),
            Operation('replace', 'A.members', ('value',), ((1,),), owner),
            Operation('replace', 'A.members', ('value',), (((1, 1),),), owner),
            Operation('update', 'A', ('text',), (('missing ID',),)),
            Operation('delete', 'A', ('key', 'count'), (('a', 1),)),
            Operation('update', 'A', ('key', 'members'), (('a', (1,)),)),
            Operation('insert', 'builtins.ConfigGroup', ('config_group_id',), (('incomplete',),)),
        ]
        invalid_groups = [(operation,) for operation in operations]
        for target, when, then_set in [
            ('builtins.Decodes', ObjectValue(()), ObjectValue(())),
            ('A', ObjectValue(((Symbol('Other.label'), 'outside'),)), ObjectValue(())),
            ('A', ObjectValue(((Symbol('A.limit'), 1),)), ObjectValue(())),
            ('A', ObjectValue(()), ObjectValue(((Symbol('A.peers'), 1),))),
            ('A', ObjectValue(()), ObjectValue(((Symbol('A.limit'), True),))),
            ('A', (), ObjectValue(())),
        ]:
            config_scope = (Condition('config_group_id', 'invalid'),)
            invalid_groups.append(
                (
                    Operation(
                        'insert',
                        'builtins.ConfigGroup',
                        ('config_group_id', 'entity_type', 'priority'),
                        (('invalid', Symbol(target), 1),),
                    ),
                    Operation(
                        'replace', 'builtins.ConfigGroup.when', ('value',), ((when,),), config_scope
                    ),
                    Operation(
                        'replace',
                        'builtins.ConfigGroup.then_set',
                        ('value',),
                        ((then_set,),),
                        config_scope,
                    ),
                )
            )
        for entries in [
            1,
            (ObjectValue(((Symbol('code'), 'CNY'),)),),
            (
                ObjectValue(
                    (
                        (Symbol('code'), 'CNY'),
                        (Symbol('decode'), 'Yuan'),
                        (Symbol('ordering'), True),
                    )
                ),
            ),
        ]:
            invalid_groups.append(
                (
                    Operation(
                        'replace',
                        'builtins.Decodes.entries',
                        ('value',),
                        ((entries,),),
                        (Condition('decodes_id', 'Currencies'),),
                    ),
                )
            )
        for backend in ('sqlite', 'files'):
            store, _ = await self.configured(backend)
            self.addCleanup(store.close)
            initialized = store.apply(self.plan_path)
            self.assertEqual(initialized.status, 'committed', initialized.publication_error)
            baseline = store.snapshot().tables
            exported = sorted(store.transaction_directory().glob('*.yatran'))
            for operations in invalid_groups:
                with self.subTest(backend=backend, operations=operations):
                    plan = Plan(
                        'invalid',
                        self.plan.model,
                        (
                            Group(
                                'candidate',
                                (
                                    Operation('update', 'A', ('key', 'count'), (('a', 99),)),
                                    *operations,
                                ),
                            ),
                        ),
                    )
                    with self.assertRaises(ValueError):
                        store.apply(self.write('invalid.yaplan', dumps(plan)))
                    self.assertEqual(store.snapshot().tables, baseline)
                    self.assertEqual(
                        sorted(store.transaction_directory().glob('*.yatran')), exported
                    )

    async def test_export_failure_is_committed_and_recovers_without_reapply(self):
        for backend in ('sqlite', 'files'):
            for failure in (OSError, sqlite3.OperationalError, ValueError):
                with self.subTest(backend=backend, failure=failure.__name__):
                    store, config = await self.configured(backend, failure.__name__)
                    self.addCleanup(store.close)
                    with patch.object(store, 'export', side_effect=failure('publication failed')):
                        report = store.apply(self.plan_path)
                    self.assertIs(report.status, self.module.TransactionStatus.EXPORT_PENDING)
                    self.assertEqual(report.publication_error, 'publication failed')
                    self.assertFalse(report.path.exists())
                    reopened = await self.store_type.open(config)
                    self.addCleanup(reopened.close)
                    self.assertEqual(parse(report.path.read_text(encoding='utf-8')).id, report.id)
                    with self.assertRaisesRegex(ValueError, 'already exists'):
                        reopened.apply(self.plan_path)
                    self.assertEqual(reopened.export(report.id), report.path)

    async def test_cleanup_failure_does_not_hide_commit_or_create_conflicts(self):
        for backend in ('sqlite', 'files'):
            for failure in (OSError, sqlite3.OperationalError, ValueError):
                with self.subTest(backend=backend, failure=failure.__name__):
                    store, config = await self.configured(backend, failure.__name__)
                    self.addCleanup(store.close)
                    cleanup = store.cleanup
                    calls = 0

                    def fail_after_recovery():
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise failure('cleanup failed')
                        cleanup()

                    with patch.object(store, 'cleanup', side_effect=fail_after_recovery):
                        with self.assertLogs(
                            self.store_type.apply.__module__, level='WARNING'
                        ) as logs:
                            report = store.apply(self.plan_path)
                    self.assertEqual(calls, 2)
                    self.assertIs(report.status, self.module.TransactionStatus.COMMITTED)
                    self.assertEqual(report.warnings, ())
                    self.assertTrue(any('cleanup failed' in line for line in logs.output))
                    self.assertEqual(parse(report.path.read_text(encoding='utf-8')).id, report.id)
                    reopened = await self.store_type.open(config)
                    self.addCleanup(reopened.close)
                    with self.assertRaisesRegex(ValueError, 'already exists'):
                        reopened.apply(self.plan_path)

    async def test_committed_report_survives_failed_writer_lock_release(self):
        original_connect = sqlite3.connect
        for backend in ('sqlite', 'files'):
            for operation in ('apply', 'rollback'):
                with self.subTest(backend=backend, operation=operation):
                    store, config = await self.configured(backend, operation)
                    self.addCleanup(store.close)
                    source = store.apply(self.plan_path) if operation == 'rollback' else None
                    held = []

                    class FailedClose(sqlite3.Connection):
                        def close(self):
                            raise sqlite3.OperationalError('lock release failed')

                    def connect(database, *args, **kwargs):
                        if Path(database) == store.writer_lock.path:
                            kwargs['factory'] = FailedClose
                        connection = original_connect(database, *args, **kwargs)
                        if isinstance(connection, FailedClose):
                            held.append(connection)
                        return connection

                    def probe():
                        return subprocess.run(
                            [
                                sys.executable,
                                '-c',
                                'import sqlite3,sys; '
                                'c=sqlite3.connect(sys.argv[1],timeout=0); '
                                'c.execute("BEGIN IMMEDIATE"); c.close()',
                                str(store.writer_lock.path),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            check=False,
                        )

                    try:
                        with patch.object(sqlite3, 'connect', side_effect=connect):
                            with self.assertLogs(
                                self.store_type.apply.__module__, level='WARNING'
                            ) as logs:
                                report = (
                                    store.apply(self.plan_path)
                                    if source is None
                                    else store.rollback(source.path)
                                )
                        self.assertIs(report.status, self.module.TransactionStatus.COMMITTED)
                        self.assertTrue(report.path.exists())
                        self.assertEqual(
                            parse(report.path.read_text(encoding='utf-8')).id, report.id
                        )
                        self.assertTrue(any('lock release failed' in line for line in logs.output))
                        self.assertTrue(any('lock may remain held' in line for line in logs.output))
                        rows = store.snapshot().tables['tb_a']
                        self.assertEqual(
                            [row['id'] for row in rows], ['a'] if source is None else []
                        )
                        self.assertNotEqual(probe().returncode, 0)
                    finally:
                        for connection in held:
                            sqlite3.Connection.close(connection)
                    self.assertEqual(probe().returncode, 0)
                    reopened = subprocess.run(
                        [
                            sys.executable,
                            '-c',
                            'import asyncio,sys; from pathlib import Path; '
                            'from tests.support import load_generated; '
                            'm=load_generated(Path(sys.argv[1]),"sample_model"); '
                            's=asyncio.run(m.SampleData.open(Path(sys.argv[2]))); '
                            'print(",".join(row["id"] for row in s.snapshot().tables["tb_a"])); '
                            's.close()',
                            str(self.root / 'generated' / 'sample_model'),
                            str(config),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    self.assertEqual(reopened.returncode, 0, reopened.stderr)
                    self.assertEqual(reopened.stdout.strip(), 'a' if source is None else '')

                    before = store.snapshot().tables
                    exports = sorted(store.transaction_directory().glob('*.yatran'))
                    for failure in (sqlite3.OperationalError, ValueError):
                        held.clear()
                        try:
                            with (
                                patch.object(sqlite3, 'connect', side_effect=connect),
                                patch.object(
                                    store,
                                    'open_backend',
                                    side_effect=failure('candidate read failed'),
                                ),
                                self.assertRaisesRegex(failure, 'candidate read failed') as raised,
                            ):
                                if source is None:
                                    store.apply(self.plan_path)
                                else:
                                    store.rollback(source.path)
                            self.assertTrue(
                                any(
                                    'lock release failed' in note
                                    for note in raised.exception.__notes__
                                )
                            )
                        finally:
                            for connection in held:
                                sqlite3.Connection.close(connection)
                        self.assertEqual(store.snapshot().tables, before)
                        self.assertEqual(
                            sorted(store.transaction_directory().glob('*.yatran')), exports
                        )

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

    async def test_configuration_composition_evaluation_and_entry_relative_path(self):
        self.write('defaults/base.lclcfg', "backend: 'files'\nstem: 'base'\npath: f'{stem}.data'\n")
        config = self.write(
            'entry.lclcfg', "using 'defaults/base.lclcfg'\nbackend: 'sqlite'\nstem: 'composed'\n"
        )
        store = await self.store_type.create(config)
        self.addCleanup(store.close)
        self.assertEqual(store.path, self.root / 'composed.data')
        self.assertEqual(store.backend, 'sqlite')
        self.assertFalse((self.root / 'defaults/composed.data').exists())
        config.write_text("using 'defaults/base.lclcfg'\nstem: 'switched'\n", encoding='utf-8')
        self.assertEqual(store.backend, 'sqlite')
        self.assertEqual(store.path, self.root / 'composed.data')
        other = await self.store_type.create(config)
        self.addCleanup(other.close)
        self.assertEqual((other.backend, other.path), ('files', self.root / 'switched.data'))
        self.write('defaults/bad.lclcfg', 'backend: [\n')
        bad = self.write('invalid.lclcfg', "using 'defaults/bad.lclcfg'\npath: 'never.data'\n")
        with self.assertRaises(LclError) as caught:
            await self.store_type.create(bad)
        self.assertIn('bad.lclcfg', render_error(caught.exception))
        self.assertFalse((self.root / 'never.data').exists())
        for body, missing in [("path: 'never.data'", 'backend'), ("backend: 'files'", 'path')]:
            with self.subTest(missing=missing), self.assertRaises(LclError) as absent:
                await self.store_type.create(self.write('missing.lclcfg', body))
            self.assertIn(missing, render_error(absent.exception))
        cycle = self.write('cycle.lclcfg', "using 'cycle.lclcfg'\n")
        with self.assertRaises(LclError) as circular:
            await self.store_type.create(cycle)
        self.assertIn('cycle.lclcfg', render_error(circular.exception))

    async def test_creation_failure_removes_staging_and_allows_retry(self):
        for backend in ('sqlite', 'files'):
            config = self.write(
                f'init-{backend}.lclcfg', f"backend: '{backend}'\npath: 'init-{backend}.data'\n"
            )
            target = self.root / f'init-{backend}.data'
            failure = (
                patch.object(
                    self.store_type, 'schema_path', self.write('broken.sql', 'INVALID SQL;')
                )
                if backend == 'sqlite'
                else patch(
                    'yambl_compiler.transactions.repository.write_file',
                    side_effect=OSError('prepare failed'),
                )
            )
            with failure:
                with self.assertRaises((sqlite3.Error, OSError)):
                    await self.store_type.create(config)
            self.assertFalse(target.exists())
            self.assertEqual(list(self.root.glob(f'.{target.name}.create-*')), [])
            with patch.object(Path, 'rename', side_effect=OSError('initial publication failed')):
                with self.assertRaisesRegex(OSError, 'initial publication failed'):
                    await self.store_type.create(config)
            self.assertFalse(target.exists())
            self.assertEqual(list(self.root.glob(f'.{target.name}.create-*')), [])
            store = await self.store_type.create(config)
            self.addCleanup(store.close)
            store.apply(self.plan_path)
            with closing(store.read()) as reader:
                self.assertEqual(reader.get_a('a').value.text, Success('original'))

    async def test_process_exit_during_initialization_is_atomic(self):
        for backend in ('sqlite', 'files'):
            for phase in ('prepare', 'before_commit', 'after_commit'):
                with self.subTest(backend=backend, phase=phase):
                    name = f'init-{backend}-{phase}'
                    config = self.write(
                        name + '.lclcfg', f"backend: '{backend}'\npath: '{name}.data'\n"
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            '-c',
                            CREATE_CRASH_WORKER,
                            str(self.root),
                            str(config),
                            phase,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 94, result.stderr)
                    target = self.root / (name + '.data')
                    self.assertEqual(target.exists(), phase == 'after_commit')
                    store = await (
                        self.store_type.open(config)
                        if target.exists()
                        else self.store_type.create(config)
                    )
                    self.addCleanup(store.close)
                    report = store.apply(self.plan_path)
                    self.assertEqual(report.status, 'committed')
                    with closing(store.read()) as reader:
                        self.assertEqual(reader.get_a('a').value.text, Success('original'))

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
            ({'transactions': ['ABCDEFAB-1234-1234-1234-123456789ABC']}, 'invalid file generation'),
            ({'transactions': ['../outside']}, 'invalid file generation'),
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
                with store.read() as reader:
                    self.assertEqual(reader.get_a('a').value.text, Success('original'))
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

    async def test_wrong_document_kind_and_model_leave_data_unchanged(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                with store:
                    report = store.apply(self.plan_path)
                    original = parse(report.path.read_text())
                    wrong_plan = replace(self.plan, model='0' * 64)
                    wrong_transaction = seal(replace(original, plan=wrong_plan))
                    operations = (
                        (store.apply, report.path, 'apply requires a Plan'),
                        (store.rollback, self.plan_path, 'rollback requires a Transaction'),
                        (
                            store.apply,
                            self.write('wrong.yaplan', dumps(wrong_plan)),
                            'Plan model digest',
                        ),
                        (
                            store.rollback,
                            self.write('wrong.yatran', dumps(wrong_transaction)),
                            'Transaction model digest',
                        ),
                    )
                    before = store.snapshot().tables
                    exports = {
                        p.name: p.read_bytes()
                        for p in store.transaction_directory().glob('*.yatran')
                    }
                    for operation, path, message in operations:
                        with self.subTest(operation=message):
                            with self.assertRaisesRegex(ValueError, message):
                                operation(path)
                            self.assertEqual(store.snapshot().tables, before)
                            self.assertEqual(
                                {
                                    p.name: p.read_bytes()
                                    for p in store.transaction_directory().glob('*.yatran')
                                },
                                exports,
                            )

    async def test_export_rejects_unknown_ids_and_mismatched_durable_records(self):
        for backend in ('sqlite', 'files'):
            with self.subTest(backend=backend):
                store, _ = await self.configured(backend)
                with store:
                    report = store.apply(self.plan_path)
                    original_text = report.path.read_text()
                    original = parse(original_text)
                    before = store.snapshot().tables
                    for id_, message in (
                        ('ABCDEFAB-1234-1234-1234-123456789ABC', 'invalid transaction ID'),
                        (str(uuid4()), 'unknown transaction'),
                    ):
                        with self.assertRaisesRegex(ValueError, message):
                            store.export(id_)
                    mismatched = (
                        self.plan,
                        seal(replace(original, id=str(uuid4()))),
                        seal(replace(original, plan=replace(self.plan, model='0' * 64))),
                    )
                    for document in mismatched:
                        content = dumps(document)
                        if backend == 'sqlite':
                            with closing(sqlite3.connect(store.path)) as db, db:
                                db.execute(
                                    'UPDATE tb_yambl_transactions SET document=? WHERE id=?',
                                    (content, report.id),
                                )
                        else:
                            (store.path / 'versions' / report.id / 'transaction.yatran').write_text(
                                content, encoding='utf-8'
                            )
                        with self.assertRaisesRegex(ValueError, 'stored transaction identity'):
                            store.export(report.id)
                        self.assertEqual(report.path.read_text(), original_text)
                        self.assertEqual(store.snapshot().tables, before)
                    if backend == 'sqlite':
                        with closing(sqlite3.connect(store.path)) as db, db:
                            db.execute(
                                'UPDATE tb_yambl_transactions SET document=? WHERE id=?',
                                (original_text, report.id),
                            )
                    else:
                        (store.path / 'versions' / report.id / 'transaction.yatran').write_text(
                            original_text, encoding='utf-8'
                        )
                    report.path.unlink()
                    self.assertEqual(store.export(report.id).read_text(), original_text)

    async def test_cleanup_refuses_linked_paths_and_retains_unrecognized_entries(self):
        store, _ = await self.configured('files')
        with store:
            versions = store.path / 'versions'
            unknown = versions / 'not-a-generation'
            unknown.mkdir()
            marker = unknown / 'keep.txt'
            marker.write_text('retained', encoding='utf-8')
            noncanonical = versions / 'ABCDEFAB-1234-1234-1234-123456789ABC'
            noncanonical.mkdir()
            regular_file = versions / str(uuid4())
            regular_file.write_text('retained', encoding='utf-8')
            old = versions / str(uuid4())
            tables = old / 'tables'
            table = tables / (store.layout.tables[0].name + '.jsonl')
            manifest = old / 'repository.json'
            for linked in (versions, old, tables, manifest, table):
                tables.mkdir(parents=True, exist_ok=True)
                manifest.write_text('manifest sentinel', encoding='utf-8')
                table.write_text('table sentinel', encoding='utf-8')
                with self.subTest(linked=linked):
                    # Simulate the OS link flag without requiring symlink privileges.
                    with patch.object(Path, 'is_junction', lambda path: path == linked):
                        with self.assertRaisesRegex(OSError, 'cannot clean a linked'):
                            store.cleanup()
                    self.assertTrue(linked.exists())
                    self.assertEqual(table.read_text(), 'table sentinel')
            store.cleanup()
            self.assertFalse(old.exists())
            self.assertEqual(marker.read_text(), 'retained')
            self.assertTrue(noncanonical.is_dir())
            self.assertEqual(regular_file.read_text(), 'retained')
            with closing(store.read()) as reader:
                self.assertTrue(reader.validate().value.valid)

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
        self.assertTrue((initial_version / 'tables').exists())
        self.assertTrue((initial_version / 'repository.json').exists())
        self.assertEqual(old_reader.get_a('a').status, Status.NO_ENTRY)
        reader = store.read()
        self.addCleanup(reader.close)
        self.assertEqual(reader.get_a('a').value.text, Success('original'))
        committed_version = store.file_version()
        original = report.path.read_bytes()
        store.rollback(report.path)
        self.assertTrue((committed_version / 'transaction.yatran').is_file())
        self.assertTrue((committed_version / 'repository.json').is_file())
        self.assertTrue((committed_version / 'tables').is_dir())
        report.path.unlink()
        reopened = await self.store_type.open(config)
        self.addCleanup(reopened.close)
        self.assertEqual(report.path.read_bytes(), original)

    async def test_cleanup_handles_incomplete_generations_without_removing_unknown_files(self):
        store, _ = await self.configured('files')
        self.addCleanup(store.close)
        versions = store.path / 'versions'
        no_tables = versions / str(uuid4())
        no_tables.mkdir()
        (no_tables / 'repository.json').write_text('unpublished', encoding='utf-8')
        (no_tables / 'transaction.yatran').write_text('unpublished', encoding='utf-8')
        empty_indexes = versions / str(uuid4())
        (empty_indexes / 'indexes').mkdir(parents=True)
        (empty_indexes / 'transaction.yatran').write_text('unpublished', encoding='utf-8')
        with_unknown = versions / str(uuid4())
        tables = with_unknown / 'tables'
        indexes = with_unknown / 'indexes'
        tables.mkdir(parents=True)
        indexes.mkdir()
        (with_unknown / 'transaction.yatran').write_text('unpublished', encoding='utf-8')
        (with_unknown / 'repository.json').write_text('unpublished', encoding='utf-8')
        table_user_file = tables / 'user-file.txt'
        index_user_file = indexes / 'user-index.txt'
        table_user_file.write_text('table data to retain', encoding='utf-8')
        index_user_file.write_text('index data to retain', encoding='utf-8')
        managed = tables / (store.layout.tables[0].name + '.jsonl')
        managed.write_text('unpublished', encoding='utf-8')
        before = store.snapshot().tables

        store.cleanup()

        self.assertFalse(no_tables.exists())
        self.assertFalse(empty_indexes.exists())
        self.assertFalse(managed.exists())
        self.assertFalse((with_unknown / 'transaction.yatran').exists())
        self.assertEqual(table_user_file.read_text(encoding='utf-8'), 'table data to retain')
        self.assertEqual(index_user_file.read_text(encoding='utf-8'), 'index data to retain')
        self.assertEqual(store.snapshot().tables, before)

    async def test_cleanup_failure_keeps_commit_successful_and_retries_on_open(self):
        store, config = await self.configured('files')
        self.addCleanup(store.close)
        old_version = store.file_version()
        orphan = store.path / 'versions' / str(uuid4())
        unlink = Path.unlink
        cleanup = store.cleanup
        calls = 0

        def cleanup_with_orphan():
            nonlocal calls
            calls += 1
            if calls == 2:
                (orphan / 'tables').mkdir(parents=True)
                (orphan / 'repository.json').write_text('unpublished', encoding='utf-8')
            cleanup()

        def failing_unlink(path, *args, **kwargs):
            if path == orphan / 'repository.json':
                raise OSError('cleanup unavailable')
            return unlink(path, *args, **kwargs)

        with (
            patch.object(Path, 'unlink', failing_unlink),
            patch.object(store, 'cleanup', cleanup_with_orphan),
        ):
            with self.assertLogs(self.store_type.apply.__module__, level='WARNING') as logs:
                report = store.apply(self.plan_path)
        self.assertEqual(report.status, 'committed')
        self.assertEqual(parse(report.path.read_text()).plan, self.plan)
        self.assertIn('cleanup unavailable', logs.output[0])
        self.assertTrue(old_version.exists())
        reopened = await self.store_type.open(config)
        self.addCleanup(reopened.close)
        self.assertTrue(old_version.exists())
        self.assertFalse(orphan.exists())
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
                            self.assertTrue((version / 'repository.json').is_file())
                            self.assertTrue((version / 'tables').is_dir())
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
