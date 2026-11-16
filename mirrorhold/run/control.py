"""The running core's side of run control, and the thread that keeps its status current.

Callers never reach the engine: they write what they want of a run (``records.status``),
and ``StatusPump`` — a thread of the run's own process — reads it twice a second into
``RunControl``. The run reconciles at safe points: between two steps of its own work
(``checkpoint``), and while the engine copies, by suspending, resuming or stopping the
engine through its adapter. A paused activity uses no drive, and others may have begun on
its drives meanwhile, so one asked to work again stays paused until it has them back
(``pause_wanted``). The pump also takes the engine's live statistics into the published
status, except while the engine is suspended; a place in line a caller moved it to reaches
its status as it reads what they want.

What the status counts as done while the engine works is what it processed: the files
it copied and the files it compared and found identical, with their bytes. The rate is
those bytes over the time the engine worked unpaused, and the time left
(``time_left``) is computed from what the transfer was handed — not from the engine's
own estimate, which covers only the files it has queued.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ..engine.rclone import Transfer, TransferStats
from ..records.admission import ADMISSION_INTERVAL, stay_paused, take_back
from ..records.status import CancelMode, LiveStatus, StatusBoard, Wanted

PUBLISH_INTERVAL = 0.5

MIN_RATE_SECONDS = 3.0
"""Seconds of unpaused work before a rate and a time left are published: the first
seconds measure the engine starting, not its pace."""


def time_left(files_left: int, bytes_left: int, files_done: int, bytes_done: int,
              seconds: float) -> Optional[float]:
    """Seconds left at the pace measured so far: the larger of the bytes left over the byte
    rate and the files left over the file rate — many small files are bound by the count,
    a few large ones by the bytes. None before ``MIN_RATE_SECONDS`` or anything done."""
    if seconds < MIN_RATE_SECONDS or (files_done <= 0 and bytes_done <= 0):
        return None
    estimates = []
    if files_done > 0:
        estimates.append(max(0, files_left) * seconds / files_done)
    if bytes_done > 0:
        estimates.append(max(0, bytes_left) * seconds / bytes_done)
    return max(estimates)


class RunControl:
    """What callers want of an activity as it sees it. ``board``: its status board, through
    which a paused one takes its drives back (``records.admission.take_back``); without one a
    pause lifts at once."""

    def __init__(self, status: LiveStatus, board: Optional[StatusBoard] = None):
        self._status = status
        self._board = board
        self._wanted = Wanted()
        self._changed = threading.Condition()
        self._next_attempt = 0.0

    @property
    def wanted(self) -> Wanted:
        with self._changed:
            return self._wanted

    def update(self, wanted: Wanted) -> None:
        with self._changed:
            if wanted != self._wanted:
                self._wanted = wanted
                if wanted.queued is not None:
                    self._status.queued = wanted.queued
                self._changed.notify_all()

    @property
    def cancel(self) -> Optional[CancelMode]:
        return self.wanted.cancel

    def wait_change(self, timeout: float) -> None:
        with self._changed:
            self._changed.wait(timeout)

    def pause_wanted(self) -> bool:
        """Whether the activity is to be paused now: a pause is wanted, or it is paused,
        asked to work again and its drives are not back yet. The caller pauses or works, and
        publishes which; one that may work again is no longer paused by then. A cancel
        outranks both."""
        wanted = self.wanted
        if wanted.cancel is None and not wanted.paused and self._status.paused:
            return not self._take_back()
        stay_paused(self._status)
        return wanted.cancel is None and wanted.paused

    def _take_back(self) -> bool:
        if self._board is None:
            self._status.paused = False
            return True
        now = time.monotonic()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + ADMISSION_INTERVAL
        return take_back(self._board)

    def checkpoint(self) -> Optional[CancelMode]:
        """Between two steps of the core's own work: wait while the activity is to be
        paused, and answer whether a cancel is wanted."""
        while self.pause_wanted():
            self._status.paused = True
            self.wait_change(PUBLISH_INTERVAL)
        self._status.paused = False
        cancel = self.wanted.cancel
        if cancel is not None:
            self._status.cancelling = cancel
        return cancel


class StatusPump(threading.Thread):
    """Publishes the run's status and feeds callers' wishes to its ``RunControl``."""

    def __init__(self, board: StatusBoard, control: RunControl,
                 on_tick: Optional[Callable[[], None]] = None):
        super().__init__(name="run-status", daemon=True)
        self.board = board
        self.control = control
        self._transfer: Optional[Transfer] = None
        self._base = (0, 0, 0, 0)
        self._handed = (0, 0)
        self._worked = 0.0
        self._last: Optional[float] = None
        self._on_tick = on_tick
        self._halt = threading.Event()

    def watch(self, transfer: Optional[Transfer], files: int = 0, size: int = 0) -> None:
        """Take live figures from ``transfer`` (None: stop), counting on from what the
        status shows as done when it starts. ``files`` and ``size`` (bytes): what the
        transfer was handed, which the time left is measured against."""
        status = self.board.status
        done = status.done
        self._base = (done["files_copied"], done["bytes_copied"], done["files_identical"], done["bytes_identical"])
        self._handed = (files, size)
        self._worked, self._last = 0.0, None
        self._transfer = transfer
        status.engine_pid = transfer.pid if transfer is not None else None
        if transfer is None:
            status.in_flight, status.comparing, status.rate, status.eta_seconds = [], [], 0.0, None

    def run(self) -> None:
        while not self._halt.is_set():
            self.tick()
            self._halt.wait(PUBLISH_INTERVAL)

    def tick(self) -> None:
        wanted = self.board.wanted()
        if wanted is not None:                 # None: the control file is held or not there; what was wanted stands
            self.control.update(wanted)
        transfer = self._transfer
        if transfer is not None:
            now = time.monotonic()
            if self._last is not None and not transfer.suspended:
                self._worked += now - self._last
            self._last = now
            stats = transfer.stats()
            if stats is not None and transfer is self._transfer:
                self._take(stats)
        if self._on_tick is not None:
            self._on_tick()
        try:
            self.board.publish()
        except OSError:
            pass

    def _take(self, stats: TransferStats) -> None:
        status = self.board.status
        status.in_flight = [[f.name, f.size, f.bytes] for f in stats.in_flight]
        status.comparing = list(stats.checking)
        base = self._base
        status.done["files_copied"] = base[0] + stats.transfers
        status.done["bytes_copied"] = base[1] + stats.bytes
        status.done["files_identical"] = base[2] + stats.identical
        status.done["bytes_identical"] = base[3] + stats.identical_bytes
        files_done, bytes_done = stats.transfers + stats.identical, stats.bytes + stats.identical_bytes
        worked = self._worked
        status.rate = bytes_done / worked if worked >= MIN_RATE_SECONDS else 0.0
        status.eta_seconds = time_left(self._handed[0] - files_done, self._handed[1] - bytes_done,
                                       files_done, bytes_done, worked)

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=10)
