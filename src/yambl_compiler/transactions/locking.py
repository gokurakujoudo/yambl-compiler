"""Serialize repository mutations across processes with a persistent lock file."""

import sqlite3
import threading
from pathlib import Path
from types import TracebackType
from typing import Self


class RepositoryLock:
    """Protect a repository with a reentrant, crash-released SQLite lock.

    The lock file is kept after release: unlinking it while another process is
    waiting could create two independent locks for the same repository.
    """

    def __init__(self, repository: Path) -> None:
        """Choose a stable sibling lock file for the resolved repository.

        :param repository: Resolved SQLite file or file-repository directory.
        :returns: None.
        """
        self.path = repository.with_name('.' + repository.name + '.yambl-lock')
        self._guard = threading.RLock()
        self._depth = 0
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        """Acquire the process lock, reusing it for nested calls in this thread.

        :returns: The acquired lock.
        :raises OSError: If the lock cannot be opened safely.
        :raises sqlite3.Error: If the lock cannot be acquired within 60 seconds.
        """
        self._guard.acquire()
        try:
            if self._depth == 0:
                if self.path.is_symlink() or self.path.is_junction():
                    raise OSError('repository lock must not be a symlink or junction')
                connection = sqlite3.connect(self.path, timeout=60, isolation_level=None)
                try:
                    connection.execute('BEGIN IMMEDIATE')
                except BaseException as error:
                    try:
                        connection.close()
                    except sqlite3.Error as close_error:
                        error.add_note(f'repository lock cleanup failed: {close_error}')
                    raise
                self._connection = connection
            self._depth += 1
            return self
        except BaseException:
            self._guard.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock without replacing an exception from the protected work.

        :param exc_type: Exception type from the protected work, when present.
        :param exc_value: Exception from the protected work, when present.
        :param traceback: Exception traceback from the protected work, when present.
        :returns: None; protected-work exceptions continue to propagate.
        """
        try:
            self._depth -= 1
            if self._depth == 0 and self._connection is not None:
                connection = self._connection
                self._connection = None
                try:
                    connection.close()
                except sqlite3.Error as error:
                    if exc_value is None:
                        raise
                    exc_value.add_note(f'repository lock release failed: {error}')
        finally:
            self._guard.release()
