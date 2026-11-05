"""Version cleanup: whole versions batches older than a job's days, deleted.

Every removal or replacement a run or a repair makes on a destination is a move into its
versions batch, ``.mirrorhold/versions/<UTC start>/``, named by when that pair began to be
applied or the repair its copies (``run.apply.VersionsBatch``). A job may opt in to deleting
batches older than N days. That deletion is the only permanent one the tool performs,
so it is fenced on every side:

- only in a destination whose marker names this job and this destination, read again
  immediately before anything is deleted;
- only inside the management folder's ``versions`` folder, each of the two a real
  folder — one that is a link or junction is not entered;
- only folders named as a run names a batch, dated by that name (in UTC; the name is
  the batch's own date and needs no record that could be lost): an
  entry not named so is left alone and reported, and a batch is expired when its date
  lies more than ``keep_days`` days before now;
- never through a reparse point: a link or junction inside a batch is removed as a
  link and what it points at is not touched; a batch that is itself a link is left
  alone and reported.

A batch is deleted deepest first, its folder last; a file that cannot be deleted keeps
the rest of its batch in place, reported, and the next run tries again. On a drive,
read-only files and folders — a copy keeps Windows' read-only attribute — are made
writable first, and folders are entered by their extended-length spelling, so one whose
name ends in a space or a period is measured and deleted like any other
(``storage.drive``). ``plan_cleanup`` measures the same selection without deleting, for
the preview. Every destination is reached through the storage seam (``storage``): a
folder of this computer given as a path is a drive's. On a remote a batch is deleted whole
by the engine, after the same fences; a remote that stops answering while its marker or the
batches are read leaves them all, said as a problem of the remote. A service that keeps
what is deleted in a trash, or as older versions, frees its space only when it removes them
itself: the report says it may (``CleanupReport.trash``), for the preview and the record to
say so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from ..endpoints.marker import MANAGEMENT_DIR, MarkerState, check_marker
from ..records.record import MAX_PROBLEMS, VersionsCleaned, capped
from ..storage.destination import StorageError
from ..storage.drive import destination_storage

VERSIONS_DIR = "versions"
VERSIONS = f"{MANAGEMENT_DIR}\\{VERSIONS_DIR}"
"""The destination's versions folder, relative to the destination."""
BATCH_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z(?:-\d+)?$")
"""What ``run.apply.VersionsBatch`` names a batch: when its moves began, in UTC, and a
counter when two batches began in the same second."""

def batch_time(name: str) -> Optional[datetime]:
    """The UTC time a batch's name carries, or None for a name no run gives a batch."""
    match = BATCH_NAME.match(name)
    if match is None:
        return None
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class ExpiredBatch:
    name: str
    started: datetime
    files: int
    bytes: int


@dataclass(frozen=True)
class CleanupReport:
    """The expired batches of one destination: what a run deleted (``deleted``), or in a
    preview what it would delete. ``trash``: the destination may keep what is deleted until
    it removes it itself (``DestinationStorage.may_keep_deleted``), so the space is not
    freed at once."""

    keep_days: int
    batches: Tuple[ExpiredBatch, ...] = ()
    deleted: bool = False
    problems: Tuple[str, ...] = ()
    trash: bool = False

    @property
    def files(self) -> int:
        return sum(b.files for b in self.batches)

    @property
    def bytes(self) -> int:
        return sum(b.bytes for b in self.batches)

    def cleaned(self) -> VersionsCleaned:
        """What a run's record keeps of it."""
        names, total = capped(b.name for b in self.batches)
        return VersionsCleaned(self.keep_days, names, total, self.files, self.bytes, self.problems[:MAX_PROBLEMS],
                               self.trash)


def versions_problem(storage) -> Optional[str]:
    """Why the destination's versions folder must not be entered — it, or the management
    folder it lies in, is a link or not a folder, or cannot be read — or None."""
    for folder in (MANAGEMENT_DIR, VERSIONS):
        problem = storage.folder_problem(folder)
        if problem:
            return problem
    return None


def expired_batches(destination, keep_days: int,
                    now: Optional[datetime] = None) -> Tuple[Tuple[ExpiredBatch, ...], Tuple[str, ...]]:
    """The destination's batches older than ``keep_days`` days, oldest first, and what was
    left alone and why."""
    storage = destination_storage(destination)
    now = now or datetime.now(timezone.utc)
    problem = versions_problem(storage)
    if problem:
        return (), (problem,)
    if not storage.is_folder(VERSIONS):
        return (), ()
    cutoff = now - timedelta(days=keep_days)
    found: List[ExpiredBatch] = []
    problems: List[str] = []
    for entry in sorted(storage.entries(VERSIONS), key=lambda e: e.name):
        if entry.problem:
            problems.append(f"{entry.name} cannot be read: {entry.problem}; left alone")
            continue
        when = batch_time(entry.name)
        if entry.is_link or not entry.is_dir or when is None:
            problems.append(f"{entry.name} is not a versions batch; left alone")
            continue
        if when < cutoff:
            files, size = storage.measure(f"{VERSIONS}\\{entry.name}")
            found.append(ExpiredBatch(entry.name, when, files, size))
    return tuple(sorted(found, key=lambda b: (b.started, b.name))), tuple(problems)


def plan_cleanup(destination, keep_days: int, now: Optional[datetime] = None) -> CleanupReport:
    """What a run's cleanup would delete now; nothing is deleted."""
    storage = destination_storage(destination)
    batches, problems = _expired(storage, keep_days, now)
    return CleanupReport(keep_days, batches, False, problems, storage.may_keep_deleted)


def _expired(storage, keep_days: int, now: Optional[datetime]) -> Tuple[Tuple[ExpiredBatch, ...], Tuple[str, ...]]:
    """``expired_batches``, a remote that stops answering while they are read leaving them
    all, said as a problem: nothing is deleted on what could not be read whole."""
    try:
        return expired_batches(storage, keep_days, now)
    except StorageError as exc:
        return (), (f"the versions batches cannot be read now: {exc.strerror}; the next run tries again",)


def clean_versions(destination, keep_days: int, *, job_id: str, marker_id: str,
                   now: Optional[datetime] = None, stop: Optional[Callable[[], bool]] = None) -> CleanupReport:
    """Delete the destination's batches older than ``keep_days`` days. ``stop`` is asked
    before each batch; True leaves the rest for the next run (a cancel)."""
    storage = destination_storage(destination)
    try:
        state = check_marker(storage, job_id, marker_id)
    except StorageError as exc:              # only a remote raises it: it stopped answering
        return CleanupReport(keep_days, (), True, (f"the remote stopped answering: {exc.strerror}; nothing was deleted "
                                                   f"and the next run tries again",))
    if state is not MarkerState.MATCH:
        return CleanupReport(keep_days, (), True, ("the destination's marker does not match this job; nothing was "
                                                   "deleted",))
    batches, problems = _expired(storage, keep_days, now)
    deleted: List[ExpiredBatch] = []
    found = list(problems)
    for batch in batches:
        if stop is not None and stop():
            break
        relative = f"{VERSIONS}\\{batch.name}"
        problem = storage.folder_problem(relative)
        if problem:
            found.append(problem)
            continue
        failure = storage.delete_tree(relative)
        if failure:
            found.append(f"{batch.name}: {failure}; the rest of the batch is kept and the next run tries again")
        else:
            deleted.append(batch)
    return CleanupReport(keep_days, tuple(deleted), True, tuple(found), storage.may_keep_deleted)
