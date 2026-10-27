"""Exercise durable writes by independent processes against the same repository."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from tests.support import load_generated
from tests.transactions.test_binding import CatalogCase
from tests.transactions.test_locking import WORKER as LOCK_WORKER
from yambl_compiler.languages.yaplan.model import Group, Operation, Plan
from yambl_compiler.storage.generation import generate
from yambl_compiler.storage.results import Success
from yambl_compiler.transactions.writing import dumps

WORKER = r'''
import asyncio, sys, time
from pathlib import Path
root, config, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root / 'generated'))
import sample_model as model
from sample_model.transactions.incremental import IncrementalData
from sample_model.transactions.locking import RepositoryLock
original_enter = RepositoryLock.__enter__
def enter(self):
    (root / (name + '.lock_attempt')).touch()
    return original_enter(self)
RepositoryLock.__enter__ = enter
(root / (name + '.starting')).touch()
store = asyncio.run(model.SampleData.open(config))
original = IncrementalData.select
entered = False
def gated(self, table, keys):
    global entered
    rows = original(self, table, keys)
    if not entered:
        entered = True
        (root / (name + '.candidate')).touch()
        if name == 'first':
            deadline = time.monotonic() + 30
            while not (root / 'first.release').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('first writer was not released')
                time.sleep(0.01)
    return rows
IncrementalData.select = gated
try:
    print(store.apply(root / (name + '.yaplan')).status, flush=True)
finally:
    store.close()
'''

CLEANUP_WORKER = r'''
import asyncio, sys, time
from pathlib import Path
root, config = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / 'generated'))
import sample_model as model
store = asyncio.run(model.SampleData.open(config))
(root / 'cleaner.ready').touch()
deadline = time.monotonic() + 30
while not (root / 'writer.prepared').exists():
    if time.monotonic() > deadline:
        raise TimeoutError('writer did not prepare the generation')
    time.sleep(0.01)
(root / 'cleaner.starting').touch()
try:
    store.cleanup()
    (root / 'cleaner.done').touch()
finally:
    store.close()
'''

PUBLISH_WORKER = r'''
import asyncio, sys, time
from pathlib import Path
root, config = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / 'generated'))
import sample_model as model
import sample_model.transactions.repository as repository
store = asyncio.run(model.SampleData.open(config))
original = repository.write_file
def gated(path, content):
    original(path, content)
    if path.name == 'repository.pending':
        (root / 'writer.prepared').touch()
        deadline = time.monotonic() + 30
        while not (root / 'writer.release').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('writer was not released')
            time.sleep(0.01)
repository.write_file = gated
try:
    print(store.apply(root / 'first.yaplan').status, flush=True)
finally:
    store.close()
'''


class ConcurrentRepositoryTests(CatalogCase):
    """Two writers must not publish snapshots based on the same old state."""

    def setUp(self):
        super().setUp()
        output = self.root / 'generated'
        generate(self.catalog.layout.model, output, 'sample_model')
        self.module = load_generated(output / 'sample_model', 'sample_model')
        for name in ('first', 'second'):
            plan = Plan(
                name,
                self.catalog.layout.model.model_digest,
                (Group('main', (Operation('insert', 'A', ('key', 'text'), ((name, name),)),)),),
            )
            self.write(name + '.yaplan', dumps(plan))

    def wait_for(self, path: Path, process: subprocess.Popen[str]) -> None:
        """Wait for a process barrier while checking for an early failure."""
        deadline = time.monotonic() + 30
        while not path.exists():
            if process.poll() is not None:
                self.fail(f'writer exited early: {process.communicate()}')
            if time.monotonic() > deadline:
                self.fail(f'writer did not reach {path.name}')
            time.sleep(0.01)

    def test_two_processes_preserve_both_commits(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                config = self.write(
                    backend + '.lclcfg', f"backend: '{backend}'\npath: '{backend}.data'\n"
                )
                asyncio.run(self.module.SampleData.create(config)).close()
                observer = asyncio.run(self.module.SampleData.open(config))
                self.addCleanup(observer.close)
                old_reader = observer.read()
                self.addCleanup(old_reader.close)
                for name in (
                    'first.starting',
                    'first.lock_attempt',
                    'first.candidate',
                    'first.release',
                    'second.starting',
                    'second.lock_attempt',
                    'second.candidate',
                ):
                    (self.root / name).unlink(missing_ok=True)
                processes = []
                try:

                    def start(name):
                        process = subprocess.Popen(
                            [sys.executable, '-c', WORKER, str(self.root), str(config), name],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        processes.append(process)
                        return process

                    first = start('first')
                    self.wait_for(self.root / 'first.candidate', first)
                    second = start('second')
                    self.wait_for(self.root / 'second.lock_attempt', second)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        second.wait(timeout=0.3)
                    self.assertFalse((self.root / 'second.candidate').exists())
                    (self.root / 'first.release').touch()
                    for process in (first, second):
                        out, err = process.communicate(timeout=30)
                        self.assertEqual(process.returncode, 0, err)
                        self.assertEqual(out.strip(), 'committed')
                    self.assertNotIsInstance(old_reader.get_a('first'), Success)
                    self.assertNotIsInstance(old_reader.get_a('second'), Success)
                    with asyncio.run(self.module.SampleData.open(config)) as data:
                        with data.read() as reader:
                            self.assertIsInstance(reader.get_a('first'), Success)
                            self.assertIsInstance(reader.get_a('second'), Success)
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                            process.wait()

    def test_cleanup_waits_for_in_progress_file_publication(self):
        config = self.write('cleanup.lclcfg', "backend: 'files'\npath: 'cleanup.data'\n")
        asyncio.run(self.module.SampleData.create(config)).close()
        processes = []
        try:

            def start(source):
                process = subprocess.Popen(
                    [sys.executable, '-c', source, str(self.root), str(config)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append(process)
                return process

            cleaner = start(CLEANUP_WORKER)
            self.wait_for(self.root / 'cleaner.ready', cleaner)
            writer = start(PUBLISH_WORKER)
            self.wait_for(self.root / 'writer.prepared', writer)
            self.wait_for(self.root / 'cleaner.starting', cleaner)
            with self.assertRaises(subprocess.TimeoutExpired):
                cleaner.wait(timeout=0.3)
            self.assertFalse((self.root / 'cleaner.done').exists())
            (self.root / 'writer.release').touch()
            out, err = writer.communicate(timeout=30)
            self.assertEqual(writer.returncode, 0, err)
            self.assertEqual(out.strip(), 'committed')
            out, err = cleaner.communicate(timeout=30)
            self.assertEqual(cleaner.returncode, 0, out + err)
            self.assertTrue((self.root / 'cleaner.done').exists())
            with asyncio.run(self.module.SampleData.open(config)) as data:
                with data.read() as reader:
                    self.assertIsInstance(reader.get_a('first'), Success)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_sqlite_hardlink_alias_is_rejected_without_write(self):
        config = self.write('alias.lclcfg', "backend: 'sqlite'\npath: 'alias.data'\n")
        data = asyncio.run(self.module.SampleData.create(config))
        self.addCleanup(data.close)
        before = data.path.read_bytes()
        alias = self.root / 'second-link.data'
        try:
            os.link(data.path, alias)
        except OSError as error:
            self.skipTest(f'filesystem cannot create hard links: {error}')
        alias_config = self.write(
            'second-link.lclcfg', "backend: 'sqlite'\npath: 'second-link.data'\n"
        )
        with self.assertRaisesRegex(ValueError, 'hard-link aliases'):
            asyncio.run(self.module.SampleData.open(alias_config))
        with self.assertRaisesRegex(ValueError, 'hard-link aliases'):
            data.apply(self.root / 'first.yaplan')
        self.assertEqual(data.path.read_bytes(), before)

    def test_lock_timeout_does_not_commit_or_change_business_data(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                directory = self.root / backend
                directory.mkdir()
                config = directory / 'config.lclcfg'
                config.write_text(f"backend: '{backend}'\npath: 'data'\n", encoding='utf-8')
                data = asyncio.run(self.module.SampleData.create(config))
                self.addCleanup(data.close)
                original_connect = sqlite3.connect
                holder = subprocess.Popen(
                    [sys.executable, '-c', LOCK_WORKER, str(directory), 'first'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    self.wait_for(directory / 'first.entered', holder)

                    def short_connect(*args, **kwargs):
                        kwargs['timeout'] = 0.2
                        return original_connect(*args, **kwargs)

                    with patch(
                        'yambl_compiler.transactions.locking.sqlite3.connect',
                        side_effect=short_connect,
                    ):
                        with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                            data.apply(self.root / 'first.yaplan')
                    with data.read() as reader:
                        self.assertNotIsInstance(reader.get_a('first'), Success)
                    self.assertFalse(data.transaction_directory().exists())
                    (directory / 'first.release').touch()
                    out, err = holder.communicate(timeout=30)
                    self.assertEqual(holder.returncode, 0, out + err)
                finally:
                    if holder.poll() is None:
                        holder.kill()
                        holder.wait()
