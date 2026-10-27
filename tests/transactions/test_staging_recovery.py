"""Recover file generations interrupted at each preparation boundary."""

import subprocess
import sys
import unittest

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Status
from yambl_compiler.transactions.writing import dumps

WORKER = '''
import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import patch
from tests.support import load_generated

root, config, plan, phase = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
module = load_generated(root / 'generated' / 'sample_model', 'sample_model')
store = asyncio.run(module.SampleData.open(config))
original_mkdir, original_open = Path.mkdir, Path.open
original_prepare = store.commit.__globals__['prepare_files']

def marker(version):
    (version / 'user-file.txt').write_text('preserve me', encoding='utf-8')

def mkdir(path, *args, **kwargs):
    result = original_mkdir(path, *args, **kwargs)
    if phase == 'empty' and path.parent.name == 'versions':
        marker(path)
        os._exit(93)
    return result

def open_file(path, *args, **kwargs):
    if phase == 'partial' and path.name == 'transaction.yatran' and args and args[0] == 'x':
        with original_open(path, *args, **kwargs) as output:
            output.write('incomplete transaction')
            output.flush()
            os.fsync(output.fileno())
        marker(path.parent)
        os._exit(93)
    return original_open(path, *args, **kwargs)

def prepare(*args, **kwargs):
    result = original_prepare(*args, **kwargs)
    if phase == 'complete':
        marker(args[1])
        os._exit(93)
    return result

with patch.object(Path, 'mkdir', mkdir), patch.object(Path, 'open', open_file):
    with patch.dict(store.commit.__globals__, {'prepare_files': prepare}):
        store.apply(plan)
raise RuntimeError('staging crash point was not reached')
'''


class StagingRecoveryTests(CatalogCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')
        plan = Plan(
            'insert',
            self.catalog.layout.model.model_digest,
            (Group('g', (Operation('insert', 'A', ('key',), (('a',),)),)),),
        )
        self.plan_path = self.write('plan.yaplan', dumps(plan))

    async def test_unpublished_generation_cleanup_preserves_initial_and_unknown_files(self):
        for phase in ('empty', 'partial', 'complete'):
            with self.subTest(phase=phase):
                config = self.write(f'{phase}.lclcfg', f"backend: 'files'\npath: '{phase}.data'\n")
                store = await self.module.SampleData.create(config)
                self.addCleanup(store.close)
                initial = store.file_version()
                with store.read() as old_reader:
                    process = subprocess.run(
                        [
                            sys.executable,
                            '-c',
                            WORKER,
                            str(self.root),
                            str(config),
                            str(self.plan_path),
                            phase,
                        ],
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(process.returncode, 93, process.stdout + process.stderr)
                    versions = [
                        path for path in (store.path / 'versions').iterdir() if path != initial
                    ]
                    self.assertEqual(len(versions), 1)
                    staged = versions[0]
                    self.assertEqual(
                        (staged / 'user-file.txt').read_text(encoding='utf-8'), 'preserve me'
                    )
                    if phase == 'empty':
                        self.assertFalse((staged / 'transaction.yatran').exists())
                    elif phase == 'partial':
                        self.assertEqual(
                            (staged / 'transaction.yatran').read_text(encoding='utf-8'),
                            'incomplete transaction',
                        )
                    else:
                        self.assertTrue((staged / 'repository.json').exists())
                        self.assertTrue((staged / 'transaction.yatran').exists())
                    store.recover()
                    self.assertTrue(initial.exists())
                    self.assertEqual(store.file_version(), initial)
                    self.assertEqual(old_reader.get_a('a').status, Status.NO_ENTRY)
                    self.assertEqual([path.name for path in staged.iterdir()], ['user-file.txt'])
                    self.assertEqual(list(store.transaction_directory().glob('*.yatran')), [])
