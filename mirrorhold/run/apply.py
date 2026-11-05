"""Applying a judged plan to one pair: the only code that changes a destination.

Order, each step a safe point for pause and cancel:

1. **The engine's leftovers** (``plan.temporaries``) are removed — the one deletion
   besides version cleanup the tool performs, and only on a destination it already
   owned before this run.
2. **Renames** — folders whose name differs in case, then files (moved or renamed at the
   source, or differing in case alone) — are made on the destination, so nothing renamed
   is copied again. A rename whose new name is taken, or whose file is gone, is skipped
   and reported; nothing is overwritten.
3. **Removals** — destination-only folders whole, then destination-only files — move
   into this run's versions batch, keeping their hierarchy. A destination-only folder
   that holds no file at all (no file, no link, nothing hidden), as a reorganisation
   by renames leaves the old folders, is removed instead: it carries no data, and a
   batch of empty folders would only clutter the versions.
4. **Copies** — the engine copies the new files and, unless removals are held, the
   changed ones, moving each destination file it replaces into the same batch. A
   changed file the engine could not open would lose its destination copy (measured),
   so every changed file is first opened the engine's way and left out if that fails
   (phase *checking changed files*, counted and a safe point per file); should one
   still fail in the engine, its copy is moved back out of the batch. A changed file
   whose contents the engine finds identical only has its time set: it counts as done
   and nothing moves into the batch. When the engine has gone through its whole list,
   every listed file it reported neither copied, nor identical, nor failed is recorded
   as skipped, so a run never reads as complete over a file the engine passed over.
5. **Folders** that exist only at the source are created.

While removals are held, only 4 (new files) and 5 run. Steps 2 and 3 are single
renames on one volume, atomic each; the engine writes under a temporary name. So an
interruption anywhere leaves every file under its final name complete, and the next
run's plan converges with no repair step. Paths a run cannot carry under their own name
never reach any step: the plan leaves them out (``PairPlan.left_out``).

Every step reaches the destination through the storage seam (``storage``): each rename, move,
removal and folder it asks for is the destination kind's to perform, and the engine is handed
the destination and the batch where that kind locates them. A folder of this computer given
as a path is a drive's. A remote that stops answering partway (``StorageError`` from a step
that does not handle the item alone) ends the steps there: what was done stays counted and
``ApplyResult.destination_failure`` says why, and the next run converges, as after an
interruption. On a drive the same steps fail item by item, as they always have.

A graceful cancel while the engine works starts no new file; once no file is in flight
the engine is ended (``Transfer.end_stop``), since its comparisons would otherwise run on
to the end of the list.

An engine that makes no progress at all — no byte copied, no file checked, nothing found
identical or failed, nothing read or written by its process — for ``STALL_LIMIT`` while not
paused is ended at once and the pair fails, saying why: a run holds its drives' turn and may
keep the computer awake while it follows its engine, and an engine was seen to stop
mid-comparison for hours, its remote control still answering. The next scheduled or arrival
run tries again. Its remote control not answering counts as no progress; time the computer
spent asleep does not count. Under a cancel already asked for, the engine is ended the same
way and the pair stays cancelled, the reason among its errors.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..endpoints.marker import MANAGEMENT_DIR
from ..engine.rclone import Engine, EngineError, StopKind, Transfer, TransferStats, source_file_unreadable
from ..planning.listing import parent_keys, path_key
from ..planning.plan import PairPlan
from ..records.status import CancelMode, LiveStatus, Phase
from ..storage.destination import StorageError
from ..storage.drive import destination_storage
from .control import RunControl, StatusPump
from .gate import GateDecision

VERSIONS_DIR = "versions"
RENAME_PASSES = 3
"""A rename may wait on another one moving a file out of its way; this many passes settle it."""
PASSED_OVER = "not copied: the copy engine passed over it without an error"
STALL_LIMIT = 30 * 60.0
"""Seconds an engine may make no progress at all before it is ended (the maintainer's
decision of 2026-10-21); a very slow destination — a network share, a disk that stalls for
long — would need an hour or more."""
STALL_LOOK = 10.0
"""Seconds between two readings of the engine's figures for progress."""
STALL_GAP = 60.0
"""The most one turn of the follow loop adds to the time without progress. A turn takes a
fraction of a second, or a few seconds while the engine is slow to answer; a longer gap is
the computer asleep, which the monotonic clock counts on Windows."""


class VersionsBatch:
    """The folder of everything a run's pair, or a repair, moves out of the mirror, named
    in UTC by ``started`` — when the pair began to be applied, or the repair its copies — so
    batches sort by time. Created by the first move into it. ``destination_root``: the
    destination, a storage (``storage``) or a folder of this computer; ``folder`` is the
    batch relative to the destination, ``relative`` the same as a ``Path`` (as records name
    it), and ``path`` the batch as the engine is handed it for the files a copy replaces."""

    def __init__(self, destination_root, started: datetime):
        base = started.strftime("%Y-%m-%dT%H-%M-%SZ")
        self.storage = destination_storage(destination_root)
        versions = f"{MANAGEMENT_DIR}\\{VERSIONS_DIR}"
        name, n = base, 1
        while self.storage.exists(f"{versions}\\{name}"):
            n += 1
            name = f"{base}-{n}"
        self.relative = Path(MANAGEMENT_DIR) / VERSIONS_DIR / name
        self.folder = f"{versions}\\{name}"
        self.path = self.storage.locate(self.folder)

    def inside(self, relative: str) -> str:
        """``relative``'s place in the batch, relative to the destination."""
        return f"{self.folder}\\{relative}"

    def move_in(self, relative: str) -> None:
        self.storage.move(relative, self.inside(relative))

    def move_back(self, relative: str) -> None:
        self.storage.move(self.inside(relative), relative, parents=False)

    def holds(self, relative: str) -> bool:
        """Whether anything is at ``relative``'s place in the batch."""
        return self.storage.exists(self.inside(relative))

    @property
    def exists(self) -> bool:
        return self.storage.is_folder(self.folder)


@dataclass
class ApplyResult:
    renamed: int = 0
    moved_to_versions: int = 0
    temporaries_removed: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    files_identical: int = 0      # changed files the engine found identical and brought up to date
    bytes_identical: int = 0
    restored: int = 0
    directories_created: int = 0
    cancelled: Optional[CancelMode] = None
    skipped: List[Tuple[str, str]] = field(default_factory=list)   # (path, why it was not applied)
    errors: List[str] = field(default_factory=list)
    engine_failure: Optional[str] = None     # the engine ended without being asked to
    destination_failure: Optional[str] = None   # a remote stopped answering partway, with why
    transfer_seconds: float = 0.0
    versions_batch: Optional[str] = None
    removals_done: int = 0        # files moved into versions, by the core or the engine


class _Cancelled(Exception):
    pass


def _files_under(plan: PairPlan) -> Dict[str, int]:
    """How many of the plan's deleted files each deleted folder holds, by the folder as spelled."""
    return Counter(plan.moved_with_folders().values())


def apply_pair(plan: PairPlan, decision: GateDecision, *, source_root: Path, destination_root,
               batch: VersionsBatch, engine: Engine, control: RunControl, pump: StatusPump, list_path: Path,
               bandwidth_limit: Optional[str] = None) -> ApplyResult:
    """``destination_root``: the destination, a storage (``storage``) or a folder of this
    computer, which every change reaches it through."""
    result = ApplyResult()
    status: LiveStatus = pump.board.status
    source_root, destination = Path(source_root), destination_storage(destination_root)

    def step() -> None:
        cancel = control.checkpoint()
        if cancel is not None:
            result.cancelled = cancel
            raise _Cancelled

    try:
        destination.make_folder()
        _remove_temporaries(plan, destination, result, status, step)
        if decision.applies_removals:
            _rename(plan, destination, result, status, step)
            _remove(plan, destination, batch, result, status, step)
        _copy(plan, decision, source_root, destination, batch, engine, control, pump, list_path,
              bandwidth_limit, result, step)
        if result.cancelled is None:
            for relative in plan.dirs_to_create:
                if not destination.is_folder(relative):
                    try:
                        destination.make_folder(relative)
                        result.directories_created += 1
                    except OSError as exc:
                        result.skipped.append((relative, f"folder could not be created: {exc.strerror or exc}"))
    except _Cancelled:
        pass
    except StorageError as exc:
        result.destination_failure = exc.strerror
    try:
        kept = batch.exists
    except StorageError:
        kept = result.moved_to_versions > 0       # the remote cannot say: anything moved there made it
    if kept:
        result.versions_batch = str(batch.relative)
    return result


def _remove_temporaries(plan, destination, result, status, step) -> None:
    for relative in plan.temporaries:
        step()
        try:
            destination.remove_file(relative)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result.errors.append(f"{relative}: the engine's leftover could not be removed: {exc.strerror or exc}")
            continue
        result.temporaries_removed += 1
        status.done["temporaries_removed"] = result.temporaries_removed


def _rename(plan, destination, result, status, step) -> None:
    status.phase = Phase.RENAMING
    for old, new in plan.dir_case_renames:
        step()
        try:
            destination.move(old, new, parents=False)
        except OSError as exc:
            result.skipped.append((new, f"folder could not be renamed: {exc.strerror or exc}"))
    pending = list(plan.renames)
    failures = []
    for _ in range(RENAME_PASSES):
        waiting = []
        for rename in pending:
            step()
            if not destination.exists(rename.old):
                result.skipped.append((rename.new, "the file to rename is no longer on the destination"))
                continue
            if not rename.case_only and destination.exists(rename.new):
                result.skipped.append((rename.new, "a file already holds the new name; nothing was overwritten"))
                continue
            try:
                destination.move(rename.old, rename.new)
            except OSError as exc:
                waiting.append((rename, exc))
                continue
            result.renamed += 1
            status.done["renamed"] = result.renamed
        progress = len(waiting) < len(pending)
        pending, failures = [r for r, _ in waiting], waiting
        if not pending or not progress:
            break
    for rename, exc in failures:
        result.skipped.append((rename.new, f"could not be renamed: {exc.strerror or exc}"))


def _remove(plan, destination, batch, result, status, step) -> None:
    status.phase = Phase.MOVING
    counts = _files_under(plan)
    moved: Set[str] = set()
    for folder in plan.deleted_dirs:
        step()
        if destination.remove_empty_tree(folder):
            moved.add(path_key(folder))
            continue
        try:
            batch.move_in(folder)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result.errors.append(f"{folder}: could not be moved into versions: {exc.strerror or exc}")
            continue
        moved.add(path_key(folder))
        result.moved_to_versions += counts.get(folder, 0)
        result.removals_done += counts.get(folder, 0)
        status.done["moved_to_versions"] = result.moved_to_versions
    for entry in plan.deleted:
        if any(p in moved for p in parent_keys(entry.key)):
            continue
        step()
        try:
            batch.move_in(entry.path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result.errors.append(f"{entry.path}: could not be moved into versions: {exc.strerror or exc}")
            continue
        result.moved_to_versions += 1
        result.removals_done += 1
        status.done["moved_to_versions"] = result.moved_to_versions


def _copy(plan, decision, source_root, destination, batch, engine, control, pump, list_path,
          bandwidth_limit, result, step) -> None:
    status = pump.board.status
    sizes = {e.path: e.size for e in plan.new}
    changed = []
    if decision.applies_removals and plan.changed:
        status.phase, status.examined, status.to_examine = Phase.CHECKING, 0, len(plan.changed)
        for n, change in enumerate(plan.changed):
            status.examined = n
            step()
            reason = source_file_unreadable(source_root / change.source.path)
            if reason is not None:
                result.skipped.append((change.source.path, f"could not be read now ({reason}); "
                                                           "its previous copy stays on the destination"))
            else:
                changed.append(change)
        status.examined, status.to_examine = 0, 0
    sizes.update((c.source.path, c.source.size) for c in changed)
    files = [e.path for e in plan.new] + [c.source.path for c in changed]
    if not files:
        return
    step()
    status.phase = Phase.COPYING
    try:
        transfer = Transfer(engine, source_root, destination.locate(), files, backup_dir=batch.path,
                            list_path=list_path, bandwidth_limit=bandwidth_limit, sizes=sizes,
                            configuration=destination.configuration)
    except EngineError as exc:
        result.engine_failure = str(exc)
        return
    pump.watch(transfer, len(files), sum(sizes.values()))
    followed = False
    stalled: Optional[str] = None
    try:
        stalled = _follow(transfer, control, status, result)
        followed = True
    finally:
        if not followed:
            try:
                transfer.stop_now()
            except EngineError:
                pass
        report = transfer.report()
        pump.watch(None)
    result.files_copied = len(report.copied)
    result.bytes_copied = report.bytes_copied
    result.files_identical = len(report.identical)
    result.bytes_identical = report.bytes_identical
    status.done.update(files_copied=result.files_copied, bytes_copied=result.bytes_copied,
                       files_identical=result.files_identical, bytes_identical=result.bytes_identical)
    result.transfer_seconds = report.seconds
    result.moved_to_versions += len(report.moved_to_backup)
    result.removals_done += len(report.moved_to_backup)
    result.errors.extend(report.errors)
    copied = {path_key(p) for p in report.copied}
    identical = {path_key(p) for p in report.identical}
    for path, message in report.failed.items():
        result.skipped.append((path, message))
    if report.stop is StopKind.NONE and report.ended_as_expected:
        accounted = copied | identical | {path_key(p) for p in report.failed}
        result.skipped.extend((path, PASSED_OVER) for path in files if path_key(path) not in accounted)
    for change in changed:
        if change.source.key in copied or change.source.key in identical:
            continue
        if not destination.exists(change.destination.path) and batch.holds(change.destination.path):
            try:
                batch.move_back(change.destination.path)
            except OSError as exc:
                result.errors.append(f"{change.destination.path}: its previous copy could not be moved back out "
                                     f"of versions: {exc.strerror or exc}")
                continue
            result.restored += 1
            result.moved_to_versions -= 1
            result.removals_done -= 1
    status.done["moved_to_versions"] = result.moved_to_versions
    if stalled is not None:
        result.engine_failure = stalled
    elif report.stop is not StopKind.NONE:
        result.cancelled = CancelMode.IMMEDIATE if report.stop is StopKind.IMMEDIATE else CancelMode.GRACEFUL
    elif not report.ended_as_expected:
        detail = "; ".join(report.errors[:3]) or "no message"
        result.engine_failure = f"the engine ended with status {report.exit_code}: {detail}"


def _progress(stats: Optional[TransferStats]) -> Optional[Tuple[int, ...]]:
    """What changes whenever the engine moves on; None when it could not be read."""
    if stats is None:
        return None
    return stats.bytes, stats.transfers, stats.checks, stats.errors, stats.identical, stats.io_bytes


def _follow(transfer: Transfer, control: RunControl, status: LiveStatus, result: ApplyResult, *,
            clock: Callable[[], float] = time.monotonic) -> Optional[str]:
    """Reconcile the running engine with what callers want until it ends. A graceful
    cancel ends the engine as soon as no file is in flight. Returns why the engine was
    ended for making no progress; None when it was not, or when a cancel had been asked for
    (the reason then among ``result.errors``)."""
    applied_pause = False
    applied_cancel: Optional[CancelMode] = None
    ending = False
    seen: Optional[Tuple[int, ...]] = None
    still = 0.0                       # seconds followed without progress
    last = looked = clock()
    while transfer.poll() is None:
        now = clock()
        still, last = still + min(now - last, STALL_GAP), now
        if transfer.suspended:
            still = 0.0
        elif now - looked >= STALL_LOOK:
            looked = now
            progress = _progress(transfer.stats())
            if progress is not None and progress != seen:
                seen, still = progress, 0.0
            elif still >= STALL_LIMIT:
                transfer.stop_now()
                why = (f"the engine made no progress for {round(STALL_LIMIT / 60)} minutes (no byte copied, "
                       "no file checked) and was ended; the next run tries again")
                if applied_cancel is None:
                    return why
                result.errors.append(why)
                return None
        wanted = control.wanted
        if wanted.cancel is not None and wanted.cancel is not applied_cancel:
            status.cancelling, status.paused = wanted.cancel, False
            applied_cancel, applied_pause = wanted.cancel, False
            try:
                if wanted.cancel is CancelMode.IMMEDIATE:
                    transfer.stop_now()
                else:
                    transfer.stop_gracefully()
            except EngineError as exc:
                result.errors.append(f"{exc}; stopping at once instead")
                transfer.stop_now()
        elif wanted.cancel is None and wanted.paused != applied_pause:
            applied_pause = wanted.paused
            try:
                transfer.suspend() if wanted.paused else transfer.resume()
                status.paused = wanted.paused
            except EngineError as exc:
                result.errors.append(f"the engine could not be {'paused' if wanted.paused else 'resumed'}: {exc}")
        if applied_cancel is CancelMode.GRACEFUL and not ending:
            stats = transfer.stats()
            if stats is not None and not stats.in_flight:
                ending = True
                try:
                    transfer.end_stop()
                except EngineError as exc:
                    result.errors.append(f"{exc}; stopping at once instead")
                    transfer.stop_now()
        control.wait_change(0.2)
