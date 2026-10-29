"""Exercise large stores through a generated package in a clean process."""

import os
import subprocess
import sys

from tests.support import FilesystemCase
from yambl_compiler.ir import compile_repository
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.transactions.writing import dumps

SOURCE = 'entity Node { id key: string static attr text: string }\n'
COUNT = 100_000
WORKER = r'''
import asyncio
import gc
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

root = Path(sys.argv[1])
count = int(sys.argv[2])
sys.path.insert(0, str(root / 'generated'))
import sample_model as model
import sample_model.transactions.repository as repository
from sample_model._backends import FileBackend, SQLiteBackend
from sample_model.transactions.editing import Data
from sample_model.transactions.incremental import IncrementalData

assert not any(name == 'yambl_compiler' or name.startswith('yambl_compiler.') for name in sys.modules)
configs = {}
for backend in ('files', 'sqlite'):
    config = root / (backend + '.lclcfg')
    config.write_text("backend: '" + backend + "'\npath: '" + backend + ".data'\n", encoding='utf-8')
    configs[backend] = config

with asyncio.run(model.SampleData.create(configs['sqlite'])) as sqlite_store:
    with closing(sqlite3.connect(sqlite_store.path)) as connection, connection:
        connection.executemany(
            'INSERT INTO tb_node (id) VALUES (?)',
            ((f'n{i:06d}',) for i in range(count)),
        )
        connection.row_factory = sqlite3.Row
        tables = {
            table.name: [dict(row) for row in connection.execute('SELECT * FROM "' + table.name + '"')]
            for table in sqlite_store.layout.tables
        }
with asyncio.run(model.SampleData.create(configs['files'])) as file_store:
    file_store.publish_files(Data(file_store.layout, tables), None)
del tables
gc.collect()

def inspect(store):
    with store.read() as reader:
        changed = reader.get_node('n050000')
        untouched = reader.get_node('n000000')
        assert isinstance(changed, model.Success)
        assert isinstance(untouched, model.Success)
        return changed.value.read_text(), untouched.value.read_text(), reader.validate()

def verify_all(store, backend, expected):
    if backend == 'sqlite':
        with closing(sqlite3.connect(store.path)) as connection:
            total = connection.execute('SELECT COUNT(*) FROM tb_node').fetchone()[0]
            bad = connection.execute(
                "SELECT COUNT(*) FROM tb_node WHERE id != ? AND "
                "(text__state != 1 OR text IS NOT NULL)", ('n050000',)
            ).fetchone()[0]
            target = connection.execute(
                'SELECT text__state,text FROM tb_node WHERE id=?', ('n050000',)
            ).fetchone()
        assert total == count and bad == 0
        assert target == ((0, 'updated') if expected else (1, None))
    else:
        ids = set()
        with (store.file_version() / 'tables' / 'tb_node.jsonl').open('rb') as rows:
            for line in rows:
                row = json.loads(line)
                assert row['id'] not in ids
                ids.add(row['id'])
                changed = row['id'] == 'n050000' and expected
                assert row['text__state'] == (0 if changed else 1)
                assert row['text'] == ('updated' if changed else None)
        assert len(ids) == count and 'n050000' in ids

result = {}
for backend in ('files', 'sqlite'):
    with asyncio.run(model.SampleData.open(configs[backend])) as store:
        assert hasattr(repository, 'prepare_files' if backend == 'files' else 'publish_sqlite'), (
            'incremental publisher is not connected to repository'
        )
        publisher = 'prepare_files' if backend == 'files' else 'publish_sqlite'
        original_init = IncrementalData.__init__
        init_calls = []
        def tracked_init(self, *args, **kwargs):
            init_calls.append(1)
            original_init(self, *args, **kwargs)
        def forbidden_snapshot():
            raise AssertionError('full snapshot is forbidden during incremental operations')
        backend_type = FileBackend if backend == 'files' else SQLiteBackend
        original_rows = backend_type.rows
        original_many = backend_type.rows_many
        def guarded_rows(self, table, keys):
            if table == 'tb_node' and not keys:
                raise AssertionError('full table rows are forbidden during incremental operations')
            return original_rows(self, table, keys)
        def guarded_many(self, table, selections, columns=()):
            if table == 'tb_node' and any(not selection.keys for selection in selections):
                raise AssertionError('full table batch is forbidden during incremental operations')
            return original_many(self, table, selections, columns)
        def incrementally(action):
            with patch.object(backend_type, 'rows', guarded_rows), \
                 patch.object(backend_type, 'rows_many', guarded_many):
                return action()

        with patch.object(store, 'snapshot', side_effect=forbidden_snapshot), \
             patch.object(IncrementalData, '__init__', tracked_init), \
             patch.object(repository, publisher, wraps=getattr(repository, publisher)) as publish:
            incrementally(lambda: store.validate_plan_expectations(root / 'change.yaplan'))
            after_validate = len(init_calls)
            applied = incrementally(lambda: store.apply(root / 'change.yaplan'))
            after_apply = len(init_calls)
            first_publications = publish.call_count
            assert applied.status == 'committed' and applied.path.is_file() and applied.changes == 1
            changed, untouched, check = inspect(store)
            assert isinstance(changed, model.Success) and changed.value == 'updated'
            assert untouched.status == model.Status.NO_ENTRY
            assert isinstance(check, model.Success) and check.value.valid
            verify_all(store, backend, True)
            reverted = incrementally(lambda: store.rollback(applied.path))
            changed, untouched, check = inspect(store)
            assert changed.status == model.Status.NO_ENTRY and untouched.status == model.Status.NO_ENTRY
            assert isinstance(check, model.Success) and check.value.valid
            verify_all(store, backend, False)
            assert reverted.status == 'committed' and reverted.path.is_file() and reverted.id != applied.id
            assert after_validate >= 1 and after_apply > after_validate and len(init_calls) > after_apply
            assert first_publications >= 1 and publish.call_count > first_publications
            result[backend] = {'candidate_inits': len(init_calls), 'publications': publish.call_count,
                               'apply': applied.status, 'rollback': reverted.status}
        if backend == 'sqlite':
            with closing(sqlite3.connect(store.path)) as connection:
                actual_count = connection.execute('SELECT COUNT(*) FROM tb_node').fetchone()[0]
        else:
            with (store.file_version() / 'tables' / 'tb_node.jsonl').open('rb') as rows:
                actual_count = sum(1 for _ in rows)
        assert actual_count == count
assert not any(name == 'yambl_compiler' or name.startswith('yambl_compiler.') for name in sys.modules)
print(json.dumps({'rows': count, 'backends': result}, sort_keys=True))
'''


class LargeProcessTests(FilesystemCase):
    """Require incremental validation and publication without a full snapshot."""

    def test_generated_package_large_apply_and_rollback(self):
        self.write('yambl-root.lclcfg', "prefix: ''\n")
        self.write('model.yambl', SOURCE)
        layout = generate(compile_repository(self.root), self.root / 'generated', 'sample_model')
        plan = Plan(
            'change',
            layout.model.model_digest,
            (
                Group(
                    'g', (Operation('update', 'Node', ('key', 'text'), (('n050000', 'updated'),)),)
                ),
            ),
        )
        self.write('change.yaplan', dumps(plan))
        worker = self.write('worker.py', WORKER)
        env = os.environ.copy()
        env['PYTHONPATH'] = str(self.root / 'generated')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONUTF8'] = '1'
        env['PYTHONOPTIMIZE'] = '0'
        process = subprocess.run(
            [sys.executable, str(worker), str(self.root), str(COUNT)],
            cwd=self.root,
            env=env,
            encoding='utf-8',
            capture_output=True,
            timeout=300,
        )
        self.assertEqual(process.returncode, 0, process.stdout + '\n' + process.stderr)
        self.assertIn('"rows": 100000', process.stdout)
