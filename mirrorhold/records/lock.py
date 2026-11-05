"""File locks the operating system releases when their holder ends, however it ends.

Three uses: each run's liveness lock, by which any reader tells a run in progress from
one whose process died — a crash, a shutdown, a killed process — without trusting a
recorded process number that the system may since have given to another program; the
admission lock, held for the moment a decision about which runs may work at once takes
(``records.admission``); and the snapshot lock, held while a run makes or removes a
volume snapshot (``snapshots.reading``). The last two are held through ``holding``.

Anyone may look at a liveness lock at the moment its owner releases and removes it, so a
look (``is_held_elsewhere``) opens only a file that is there, sharing its deletion — it
never creates the file again behind its owner, and never stands in the way of the
removal — and answers "ended" when the file is gone or is being removed: never an
exception.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ..exchange import open_shared, remove as _remove_file


class FileLock:
    """An exclusive lock on the first byte of ``path``, held until ``release`` or until
    the holding process ends."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self, *, remove: bool = False) -> None:
        """Let the lock go; with ``remove``, the file too, waiting a moment for a look
        that has it open."""
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None
        if remove:
            _remove_file(self.path)


HOLD_POLL = 0.02
"""Seconds between two tries for a lock another holder has, in ``holding``."""


@contextmanager
def holding(path: Path) -> Iterator[None]:
    """Hold the lock on ``path`` for the block, waiting while another holder has it. For
    locks held only for a moment: the wait has no bound."""
    lock = FileLock(path)
    while not lock.try_acquire():
        time.sleep(HOLD_POLL)
    try:
        yield
    finally:
        lock.release()


LOOK_PATIENCE = 0.2
"""Seconds a look at a lock being removed tries again before it answers "ended"."""


def is_held_elsewhere(path: Path) -> Optional[bool]:
    """True when another holder has the lock, False when nobody has, None when the file
    does not exist or its owner is removing it."""
    deadline = time.monotonic() + LOOK_PATIENCE
    while True:
        try:
            handle = open_shared(Path(path))
        except FileNotFoundError:
            return None
        except PermissionError:               # being removed: its owner has ended
            if time.monotonic() >= deadline:
                return None
            time.sleep(HOLD_POLL)
            continue
        with handle:
            try:
                _lock(handle)
            except OSError:
                return True
            _unlock(handle)
            return False


if sys.platform == "win32":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
