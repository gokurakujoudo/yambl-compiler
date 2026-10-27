"""Verify cross-process repository lock ownership and release."""

import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from yambl_compiler.transactions.locking import RepositoryLock

WORKER = r'''
import os, sys, time
from pathlib import Path
from yambl_compiler.transactions.locking import RepositoryLock
root = Path(sys.argv[1])
name = sys.argv[2]
(root / (name + '.attempt')).touch()
with RepositoryLock(root / 'data'):
    (root / (name + '.entered')).touch()
    if name == 'crash':
        os._exit(91)
    deadline = time.monotonic() + 30
    while not (root / (name + '.release')).exists():
        if time.monotonic() > deadline:
            raise TimeoutError('worker was not released')
        time.sleep(0.01)
'''


class RepositoryLockTests(unittest.TestCase):
    """Exercise real process locks rather than a mocked lock sequence."""

    def wait_for(self, path: Path, process: subprocess.Popen[str]) -> None:
        """Wait for a worker's explicit synchronization marker."""
        deadline = time.monotonic() + 30
        while not path.exists():
            if process.poll() is not None:
                self.fail(f'worker exited early: {process.communicate()}')
            if time.monotonic() > deadline:
                self.fail(f'worker did not create {path.name}')
            time.sleep(0.01)

    def test_processes_serialize_and_keep_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = subprocess.Popen([sys.executable, '-c', WORKER, str(root), 'first'], text=True)
            self.addCleanup(lambda: first.kill() if first.poll() is None else None)
            self.wait_for(root / 'first.entered', first)
            second = subprocess.Popen(
                [sys.executable, '-c', WORKER, str(root), 'second'], text=True
            )
            self.addCleanup(lambda: second.kill() if second.poll() is None else None)
            self.wait_for(root / 'second.attempt', second)
            with self.assertRaises(subprocess.TimeoutExpired):
                second.wait(timeout=0.3)
            self.assertFalse((root / 'second.entered').exists())
            (root / 'first.release').touch()
            self.assertEqual(first.wait(timeout=30), 0)
            self.wait_for(root / 'second.entered', second)
            (root / 'second.release').touch()
            self.assertEqual(second.wait(timeout=30), 0)
            self.assertTrue((root / '.data.yambl-lock').is_file())

    def test_crashed_process_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crash = subprocess.run(
                [sys.executable, '-c', WORKER, str(root), 'crash'], check=False, timeout=30
            )
            self.assertEqual(crash.returncode, 91)
            with RepositoryLock(root / 'data'):
                self.assertTrue((root / 'crash.entered').is_file())

    def test_nested_and_failure_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = RepositoryLock(Path(directory) / 'data')
            with self.assertRaisesRegex(RuntimeError, 'body failed'):
                with lock, lock:
                    raise RuntimeError('body failed')
            with lock:
                self.assertTrue(lock.path.is_file())

    def test_open_and_release_failures_keep_original_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = RepositoryLock(Path(directory) / 'data')
            connection = MagicMock(spec=sqlite3.Connection)
            connection.execute.side_effect = sqlite3.OperationalError('busy')
            connection.close.side_effect = sqlite3.OperationalError('cleanup failed')
            with patch(
                'yambl_compiler.transactions.locking.sqlite3.connect', return_value=connection
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'busy') as raised:
                    with lock:
                        self.fail('lock unexpectedly acquired')
            self.assertIn('cleanup failed', raised.exception.__notes__[0])
            connection.execute.side_effect = None
            with patch(
                'yambl_compiler.transactions.locking.sqlite3.connect', return_value=connection
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'cleanup failed'):
                    with lock:
                        pass
                with self.assertRaisesRegex(ValueError, 'body failed') as raised:
                    with lock:
                        raise ValueError('body failed')
            self.assertIn('cleanup failed', raised.exception.__notes__[0])
            with lock:
                self.assertTrue(lock.path.is_file())

    def test_rejects_linked_lock_path_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = RepositoryLock(Path(directory) / 'data')
            for method in ('is_symlink', 'is_junction'):
                with self.subTest(method=method), patch.object(Path, method, return_value=True):
                    with self.assertRaisesRegex(OSError, 'symlink or junction'):
                        with lock:
                            self.fail('linked lock was opened')
            with patch(
                'yambl_compiler.transactions.locking.sqlite3.connect', side_effect=OSError('denied')
            ):
                with self.assertRaisesRegex(OSError, 'denied'):
                    with lock:
                        self.fail('lock unexpectedly acquired')
            with lock:
                self.assertTrue(lock.path.is_file())


if __name__ == '__main__':
    unittest.main()
