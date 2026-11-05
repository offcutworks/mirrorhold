"""Replacing the copies a full verification found to differ, in a core process of its own.

``repair_destination`` is what the command line's ``repair`` executes; the GUI starts it
through ``run.launch.start_repair``. It is the user's act: a verification cannot tell which
copy is damaged and repairs nothing (``verifier``), and a repair is how the user says the
source's copies are the good ones. Nothing is destroyed: every backup copy it replaces
moves into the destination's versions first, as a run's replaced files do.

A repair publishes its live status from the moment it starts (``records.status``, kind
"repair"), through which callers pause and stop it. Then:

1. **What it takes up**: the destination's latest finished verification pass, and of the
   copies that pass listed as differing, those no earlier repair of it replaced
   (``repair.pending``). A destination being verified, or whose latest finished pass found
   no copy that differs, or whose listed copies were all replaced, is refused
   (``RepairError``). It reads that pass again once its turn comes, and a verification
   that gave way to it meanwhile, unfinished, is never what it takes up.
2. **Its turn** (``records.admission``), claimed as a run of that one destination claims:
   its source read, its destination written, and their drives; it goes before waiting
   verifications, and a working one gives way to it.
3. **The pair**: the source must resolve to one present volume, and the destination to one
   too — or, a remote's folder, be reached through one engine session on the configuration
   the preferences name (``remotes.reach``), kept for the repair's length — and the
   destination's marker must match; a shared destination's record must be usable
   (``endpoints.shared``). The record of the repair is written before anything is changed
   (``repair``).
4. **The source read as a run reads it**: from a volume snapshot made for the repair when
   the job's choice and the drive allow one, else as it is (``snapshots.reading``).
5. **Each copy checked** before it is touched. It is left as it is, with why
   (``repair.LeftReason``), when: the job excludes it now; on a shared destination it lies
   outside what the job owns or in what the job leaves; it lies in a folder no run moves or
   writes into (``run.runner.protected_folders``) — a synchronised folder, another job's
   source, a folder Windows or this tool needs; either side's path goes through a
   link or junction; it is gone from either side; the source copy is a cloud-only file;
   either side changed size or time since the verification (the next run copies it); a
   copy cannot be read; or the backup copy begins with a known file-format signature the
   source copy lacks — the mark encryption leaves (``planning.signatures``), when replacing
   would put a damaged original over a good backup.
6. **Room**: the copies' bytes, with the gate's margin, must fit the destination's free
   space less what other activities still have to write there, judged under the admission
   lock like a run's (``run.gate.space_needed``); the backup copies move into versions on
   the same drive, freeing nothing. A remote's space is its service's, where it reports one,
   and nothing else writes there meanwhile: every activity that reaches a remote waits for
   this one. Otherwise the repair fails, nothing changed.
7. **The replacement**: the record names a versions batch of the repair's own
   (``run.apply.VersionsBatch``) and every copy whose backup copy is to move into it
   (``RepairRecord.replacing``), and is saved; then each backup copy moves into the batch,
   and the engine copies the source's copies in their place (``run.apply.apply_pair``, each
   a new file now). A copy the engine did not complete has its backup copy moved back out of
   the batch — also when the copying ends by an error or an interruption, the copies the
   engine did complete then recorded as not read back. A backup copy that cannot be moved
   back is recorded as in versions. Each copy leaves ``replacing`` as it is settled, so none
   is ever in the batch unnamed for the job's version cleanup to delete: a process that dies
   — a kill, a crash, a power cut, a second Ctrl+C during that clean-up — leaves the copies
   it had not settled named there (``repair.unsettled``). A copy whose new copy a remote that
   stopped answering cannot say is in place stays named there too.
8. **Read back**, by the measure a verification of the destination compares by
   (``compare``): on a drive both copies of each replaced file are read in full and their
   digests compared; on a remote the fingerprint its service keeps for the new copy against
   the source's, or the engine's check of the encrypted form, a copy with neither read on
   both sides. One that still differs is recorded so — the destination drive may be failing,
   or the service did not keep it as it was sent.

A stop ends the repair at the next safe point: before the copies start, or, while the
engine copies, after the files in flight. The copies already replaced stay replaced and are
read back; the rest stay as they are and pending for the next repair.

What the process says, and a failure no one foresaw with its traceback, goes to the
diagnostic log the command line opens (``records.diagnostics``). While it works it keeps
the computer from falling asleep by itself and has Windows' shutdown screen name it when the
user's choices say so (``power``).
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Collection, List, Optional, Sequence, Tuple

from ..endpoints.cloud import SyncRoot
from ..endpoints.endpoint import RemoteEndpoint
from ..endpoints.marker import MarkerState, marker_check
from ..endpoints.resolution import Presence, Resolution, resolve
from ..endpoints.shared import SharedError, SharedRecord, read_shared
from ..endpoints.volumes import VolumeSource
from ..engine.rclone import Engine, EngineError, open_engine, open_source_file
from ..jobs.exclusions import EntryFilter, is_cloud_placeholder, is_link
from ..jobs.model import Job
from ..jobs.store import ConfigStore, LoadStatus, StoreBusy, StoreContents
from ..jobs.validation import Contained, ValidationContext
from ..locations import RuntimeLayout, runtime_layout
from ..planning.listing import Entry, enumerable, parent_keys, path_key
from ..planning.plan import PairPlan, left_out, time_window_ns
from ..planning.signatures import LEADING_BYTES, lost_signature
from ..power import Hold, System, repair_reason
from ..records import diagnostics
from ..records.admission import ADMISSION_INTERVAL, PUBLISH_PATIENCE, activity_claim, admit
from ..remotes.facts import Reach, SetupState
from ..remotes.reach import Reached, reach
from ..records.lock import holding
from ..records.record import DONE_KEYS, PLANNED_KEYS, utc_now
from ..records.status import KIND_REPAIR, LiveStatus, Phase, StatusBoard, engine_list_path, observe_runs
from ..run.apply import VersionsBatch, apply_pair
from ..run.control import RunControl, StatusPump
from ..run.gate import GateDecision, Verdict, space_needed
from ..run.runner import protected_folders, reserved_on
from ..snapshots.provider import SnapshotProvider, default_snapshot_provider
from ..snapshots.reading import SourceSnapshot
from ..storage.destination import DestinationStorage, StorageError, StoredEntry
from ..storage.drive import DriveStorage
from ..tray import offer as offer_tray
from ..wording import agreeing, counted
from .compare import encrypted_outcome, hasher, measure_of
from .repair import LeftReason, RepairRecord, RepairState, left_words, pending, read_repairs, write_repair
from .verification import Measure, VerificationRecord, latest_finished, read_verifications

CHUNK_BYTES = 4 * 1024 * 1024

Say = Callable[[str], None]
Left = Tuple[LeftReason, str]


class RepairError(Exception):
    """A repair that cannot start: nothing to take up, or its pair is not usable."""


class _Failed(Exception):
    """A repair that cannot go on once its record is written: it ends as failed."""


class _Stop(Exception):
    pass


def _load(store: ConfigStore) -> StoreContents:
    result = store.load()
    if result.status in (LoadStatus.UNSUPPORTED, LoadStatus.CORRUPT):
        raise RepairError(result.detail)
    return result.contents


def _local_date(text: str) -> str:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return text


def taken_up(layout: RuntimeLayout, job_id: str, marker_id: str,
             live: Collection[str]) -> Tuple[VerificationRecord, Tuple[Tuple[str, int], ...]]:
    """The destination's latest finished verification pass and the copies a repair of it
    takes up; RepairError when there is none to take up, or while a pass of the destination
    is in progress. ``live``: the activities in progress.

    A pass in progress, stopped, interrupted or failed is never taken up, nor one that gave
    way to this repair while it waited: what it found so far cannot stand for the
    destination, and the window's *Copies differ* reads the latest finished pass alike."""
    records = read_verifications(layout, job_id)
    if any(r.marker_id == marker_id and r.verification_id in live for r in records):
        raise RepairError("a verification of the destination is in progress")
    found = latest_finished(records, marker_id)
    if found is None or not found.differ:
        raise RepairError("the destination's latest finished verification found no copy that differs")
    todo = pending(found, read_repairs(layout, job_id))
    if not todo:
        raise RepairError("every copy that verification listed was replaced already")
    return found, todo


def _digest(opener: Callable[[], BinaryIO], chunk: int, make=hashlib.sha256) -> str:
    """The digest of a file read in full, as ``opener`` opens it, of the kind ``make``
    computes, in lower-case hex."""
    digest = make()
    with opener() as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                return digest.hexdigest()
            digest.update(data)


def _stopped_answering(reason: str) -> str:
    """Why a repair on a remote ended when its engine no longer answered for it."""
    return f"the remote stopped answering: {reason}"


def _head(path: Path) -> bytes:
    with open_source_file(path) as handle:
        return handle.read(LEADING_BYTES)


class _Repairer:
    def __init__(self, store: ConfigStore, job: Job, index: int, volume_source: VolumeSource,
                 sync_roots: Sequence[SyncRoot], say: Say, snapshots: SnapshotProvider, chunk: int,
                 power: Optional[System]):
        self.store = store
        self.layout = runtime_layout(store.path)
        self.job = job
        self.index = index
        self.marker_id = job.destinations[index - 1].marker_id
        self.volume_source = volume_source
        self.sync_roots = tuple(sync_roots)
        self.protected: Tuple[Contained, ...] = ()
        self.say = say
        self.chunk = chunk
        self.repair_id = uuid.uuid4().hex
        self.status = LiveStatus(self.repair_id, os.getpid(), job.job_id, job.name, "manual", utc_now(),
                                 pair_index=index, pair_total=1, marker_id=self.marker_id,
                                 destination=f"destination {index}", pair_started=utc_now(), selected=[index],
                                 kind=KIND_REPAIR)
        self.board = StatusBoard(self.layout, self.status)
        self.control = RunControl(self.status)
        self.hold = Hold(repair_reason(job.name), lambda: self.status.admitted and not self.status.paused,
                         lambda: _load(store).preferences.activities, system=power)
        self.pump = StatusPump(self.board, self.control, on_tick=self.hold.tick)
        self.snapshot = SourceSnapshot(self.layout, self.repair_id, snapshots)
        self.record: Optional[RepairRecord] = None
        self.engine: Optional[Engine] = None
        self.source_resolution: Optional[Resolution] = None
        self.source_root: Optional[Path] = None
        self.destination: Optional[DestinationStorage] = None
        self.shared: Optional[SharedRecord] = None
        self.remotes: Optional[Reached] = None         # the session a remote destination is reached through
        self.measure, self.kind = Measure.READ, None   # how a replaced copy is read back
        self.window_ns = 0
        self.entry_filter = EntryFilter.for_source(job.source)
        self._began = time.monotonic()

    # --- its turn and its pair -------------------------------------------------------------

    def _admit(self) -> Optional[StoreContents]:
        """Wait for the repair's turn, claimed as a run of its one destination, the job and
        the user's choices as the store holds them at each attempt. None when stopped."""
        self.status.phase, self.status.paused = Phase.WAITING, False
        while self.control.wanted.cancel is None:
            try:
                contents = _load(self.store)
            except StoreBusy:
                self.control.wait_change(ADMISSION_INTERVAL)
                continue
            job = contents.job(self.job.job_id)
            if job is None:
                raise RepairError("the job was removed while this repair waited")
            if len(job.destinations) < self.index or job.destinations[self.index - 1].marker_id != self.marker_id:
                raise RepairError("the job's destinations changed while this repair waited")
            claim = activity_claim(job, [self.index], self._volumes(), contents.preferences.activities)
            if admit(self.board, claim).admitted:
                self.job = job
                self.entry_filter = EntryFilter.for_source(job.source)
                self.hold.choose(contents.preferences.activities)
                return contents
            self.control.wait_change(ADMISSION_INTERVAL)
        return None

    def _volumes(self):
        try:
            return self.volume_source.scan().volumes
        except Exception as exc:  # a scan that fails is a reason in words, its details in the log
            diagnostics.note_failure(f"repair {self.repair_id}: the drives could not be checked")
            raise RepairError(f"the drives could not be checked: {type(exc).__name__}: {exc}") from None

    @staticmethod
    def _present(which: str, resolution: Resolution) -> None:
        if resolution.presence is Presence.ABSENT:
            raise RepairError(f"the {which}'s drive is not attached")
        if resolution.presence is Presence.AMBIGUOUS:
            raise RepairError(f"two attached drives match the {which}")

    def _reach(self, contents: StoreContents, remote: RemoteEndpoint) -> DestinationStorage:
        """The remote folder's storage through a session opened for the repair's length;
        RepairError, with why, when the configuration cannot be opened or the folder cannot be
        reached."""
        self.remotes = reach(contents.preferences, self.store.path, [remote])
        setup = self.remotes.setup
        if setup.state is not SetupState.READY:
            raise RepairError(setup.reason)
        facts = self.remotes.facts(remote)
        if facts is None:
            raise RepairError(f"the rclone configuration holds no remote '{remote.remote}'")
        if facts.reach is Reach.UNREACHABLE:
            raise RepairError(f"{remote.describe()} cannot be reached: {facts.reason}; repair again later")
        storage = self.remotes.storage(remote)
        if storage is None:
            raise RepairError(f"{remote.describe()} cannot be used: "
                              f"{facts.reason or 'a file stands where its folder is named'}")
        return storage

    def _begin(self, contents: StoreContents) -> Tuple[VerificationRecord, Tuple[Tuple[str, int], ...]]:
        """Resolve the pair, read what the repair takes up and write its record; RepairError
        while nothing is written."""
        volumes = self._volumes()
        destination = self.job.destinations[self.index - 1]
        source = resolve(self.job.source.endpoint, volumes)
        self._present("source", source)
        if destination.remote is not None:
            storage = self._reach(contents, destination.remote)
        else:
            target = resolve(destination.endpoint, volumes)
            self._present("destination", target)
            storage = DriveStorage(target.location, target.volume)
        try:
            state, marker = marker_check(storage, self.job.job_id, self.marker_id)
        except StorageError as exc:          # only a remote raises it: it stopped answering
            raise RepairError(_stopped_answering(exc.strerror)) from None
        if state is not MarkerState.MATCH:
            raise RepairError("the destination's marker does not match this job")
        if marker.shared:
            try:
                self.shared = read_shared(storage, self.job.job_id, self.marker_id)
            except SharedError as exc:
                raise RepairError(f"the destination's record of what to leave where it is cannot be used: {exc}") \
                    from None
            except StorageError as exc:      # only a remote raises it: it stopped answering
                raise RepairError(_stopped_answering(exc.strerror)) from None
        others = tuple(j for j in contents.jobs if j.job_id != self.job.job_id)
        context = ValidationContext.on_this_computer(volumes, self.sync_roots, others, self.store.path)
        self.protected = protected_folders(self.job, self.index, context)
        found, todo = taken_up(self.layout, self.job.job_id, self.marker_id, ())
        try:
            self.engine = open_engine(contents.preferences.engine_path)
        except EngineError as exc:
            raise RepairError(f"the sync engine cannot be used: {exc}") from None
        self.source_resolution, self.destination = source, storage
        self.window_ns = max(time_window_ns(source.volume.file_system), storage.time_window_ns)
        try:
            self.measure, self.kind = measure_of(storage, source.location, engine_list_path(self.layout,
                                                                                             self.repair_id))
        except OSError as exc:
            raise RepairError(_stopped_answering(exc.strerror or str(exc))) from None
        self.status.destination = f"destination {self.index} at {storage.describe()}"
        now = utc_now()
        self.record = RepairRecord(self.repair_id, self.job.job_id, self.job.name, self.marker_id, self.index,
                                   found.verification_id, found.started, now, now, taken=len(todo),
                                   differ_total=found.differ_total)
        self._save()
        return found, todo

    # --- the record --------------------------------------------------------------------------

    def _save(self, *, final: bool = False) -> None:
        record = self.record
        record.updated = utc_now()
        record.seconds = time.monotonic() - self._began
        try:
            write_repair(self.layout, record, self.destination if final else None)
        except OSError:
            diagnostics.note_failure(f"repair {self.repair_id}: its record could not be saved")

    def _left(self, path: str, reason: LeftReason, detail: str = "") -> None:
        self.record.left.append((path, reason, detail))
        self._settled(path)
        self.say(f"left: {path} - {left_words(reason, detail, remote=self.destination.local_folder is None)}")

    def _settled(self, path: str) -> None:
        """``path`` leaves ``replacing``: its outcome is recorded, or its backup copy is back
        in its place."""
        self.record.replacing = [r for r in self.record.replacing if r[0] != path]

    # --- checking each copy --------------------------------------------------------------------

    def _step(self) -> None:
        if self.control.checkpoint() is not None:
            raise _Stop()

    def _stat(self, root: Path, parts: Sequence[str], side: str) -> Tuple[Optional[os.stat_result], Optional[Left]]:
        """The source's file at ``parts`` beneath ``root``, or why it is left: a link on the
        way, gone, not a file, or not readable."""
        missing = LeftReason.MISSING_SOURCE if side == "source" else LeftReason.MISSING_DESTINATION
        where = enumerable(str(root))
        info = None
        for depth in range(1, len(parts) + 1):
            where = os.path.join(where, parts[depth - 1])
            try:
                info = os.lstat(where)
            except FileNotFoundError:
                return None, (missing, "")
            except OSError as exc:
                return None, (LeftReason.UNREADABLE, f"the {side} copy: {exc.strerror or exc}")
            if is_link(getattr(info, "st_file_attributes", 0), getattr(info, "st_reparse_tag", 0)):
                return None, (LeftReason.LINK, "")
        if not os.path.isfile(where):
            return None, (missing, "")
        return info, None

    def _stored(self, parts: Sequence[str]) -> Tuple[Optional[StoredEntry], Optional[Left]]:
        """The destination's file at ``parts``, or why it is left, as ``_stat`` says it."""
        try:
            found = self.destination.file_entry(parts)
        except OSError as exc:
            return None, (LeftReason.UNREADABLE, f"the destination copy: {exc.strerror or exc}")
        if found is None:
            return None, (LeftReason.MISSING_DESTINATION, "")
        if found.is_link:
            return None, (LeftReason.LINK, "")
        if found.is_dir:
            return None, (LeftReason.MISSING_DESTINATION, "")
        return found, None

    def _check(self, path: str) -> Optional[Left]:
        """None when the copy at ``path`` is to be replaced; else why it is left."""
        if self.entry_filter.hiding(path, False) is not None:
            return LeftReason.EXCLUDED, ""
        parts = path.split("\\")
        key = path_key(path)
        if self.shared is not None:
            owned = {path_key(name) for name in self.shared.own}
            leaving = {path_key(p) for p in self.shared.left}
            if path_key(parts[0]) not in owned or key in leaving or any(p in leaving for p in parent_keys(key)):
                return LeftReason.NOT_THE_JOBS, ""
        lineage = {key, *parent_keys(key)}
        folder = next((f for f in self.protected if not f.relative or path_key(f.relative) in lineage), None)
        if folder is not None:
            return LeftReason.PROTECTED, folder.words
        cannot = left_out(path)
        if cannot is not None:
            return LeftReason.NOT_COPIED, cannot
        source, why = self._stat(self.source_root, parts, "source")
        if why is not None:
            return why
        if is_cloud_placeholder(getattr(source, "st_file_attributes", 0)):
            return LeftReason.CLOUD_ONLY, ""
        destination, why = self._stored(parts)
        if why is not None:
            return why
        if source.st_size != destination.size or abs(source.st_mtime_ns - destination.mtime_ns) > self.window_ns:
            return LeftReason.CHANGED, ""
        try:
            backup = self.destination.read_head(path, LEADING_BYTES)
        except OSError as exc:
            return LeftReason.UNREADABLE, f"the destination copy: {exc.strerror or exc}"
        try:
            original = _head(self.source_root / path)
        except OSError as exc:
            return LeftReason.UNREADABLE, f"the source copy: {exc.strerror or exc}"
        if lost_signature(backup, original):
            return LeftReason.LOST_SIGNATURE, ""
        return None

    # --- the repair ------------------------------------------------------------------------------

    def _room(self, files: int, size: int) -> None:
        """Judge the destination's free space for ``size`` bytes in ``files`` files, as a
        run's gate does, and publish what the repair will write there; _Failed when it does
        not fit."""
        needed = space_needed(size, files)
        if self.job.destinations[self.index - 1].remote is not None:
            try:
                free = self.destination.free_bytes()
            except OSError as exc:
                raise _Failed(_stopped_answering(exc.strerror or str(exc))) from None
            if free is not None and needed > free:
                raise _Failed(f"the repair needs {counted(needed, 'byte')} with its margin and the remote has "
                              f"{counted(free, 'byte')} free")
            return
        with holding(self.layout.admission_lock):
            now = resolve(self.job.destinations[self.index - 1].endpoint, self._volumes())
            if now.presence is not Presence.PRESENT:
                raise _Failed("the destination's drive is not attached")
            identity = now.volume.identity
            reserved = reserved_on(self.layout, identity, besides=self.repair_id)
            if needed > now.volume.free_bytes - reserved:
                others = (f", {counted(reserved, 'byte')} of them still to be written by other activities"
                          if reserved else "")
                raise _Failed(f"the repair needs {counted(needed, 'byte')} with its margin and the destination has "
                              f"{counted(now.volume.free_bytes, 'byte')} free{others}")
            self.status.writing = {"volume": identity.to_document(), "files": files, "bytes": size}
            self.board.publish(patience=PUBLISH_PATIENCE)

    def _settle(self, batch: VersionsBatch, moved: Sequence[Tuple[str, int]],
                not_copied: Optional[Callable[[str], str]]) -> List[Tuple[str, int]]:
        """Of the copies whose backup copy moved into ``batch``, return those whose new copy is
        in place, and move each other's backup copy back. A backup copy that cannot be moved
        back is recorded as in versions, whatever ended the copying; one moved back is
        recorded as not copied, with ``not_copied``'s why, or, with None — a stop, a failure —
        left unrecorded and pending, its backup copy where it was. Those returned stay in
        ``replacing`` until read back. A copy a remote that stopped answering cannot say is in
        place stays in ``replacing`` too, pending as the copies of a repair that died are."""
        try:
            self.record.versions_batch = str(batch.relative) if batch.exists else None
        except OSError:
            self.record.versions_batch = str(batch.relative)     # it may hold what moved
        copied: List[Tuple[str, int]] = []
        for path, size in moved:
            try:
                there = self.destination.exists(path)
            except OSError as exc:
                diagnostics.note(f"repair {self.repair_id}: whether the new copy of {path} is in place cannot be "
                                 f"told now: {exc.strerror or exc}")
                continue
            if there:
                copied.append((path, size))
                continue
            try:
                batch.move_back(path)
            except OSError as exc:
                why = exc.strerror or str(exc)
                diagnostics.note(f"repair {self.repair_id}: the backup copy of {path} could not be moved back out of "
                                 f"versions: {why}")
                self._left(path, LeftReason.IN_VERSIONS, why)
                continue
            if not_copied is not None:
                self._left(path, LeftReason.NOT_COPIED, not_copied(path))
            else:
                self._settled(path)
        return copied

    def _repair(self, todo: Sequence[Tuple[str, int]]) -> None:
        record, status = self.record, self.status
        status.phase = Phase.SNAPSHOT
        read = self.snapshot.take(self.job, self.source_resolution.volume, needed=True)
        self.say(f"source {read.describe()}")
        self.source_root = self.snapshot.root(self.job.source.endpoint.path) or self.source_resolution.location
        status.phase, status.examined, status.to_examine = Phase.CHECKING, 0, len(todo)
        replacing: List[Tuple[str, int]] = []
        for n, (path, size) in enumerate(todo):
            status.examined = n
            self._step()
            why = self._check(path)
            if why is not None:
                self._left(path, *why)
            else:
                replacing.append((path, size))
        status.examined, status.to_examine = 0, 0
        if not replacing:
            return
        total = sum(size for _path, size in replacing)
        status.planned = dict.fromkeys(PLANNED_KEYS, 0)
        status.planned.update(files_to_copy=len(replacing), bytes_to_copy=total)
        status.done = dict.fromkeys(DONE_KEYS, 0)
        self._step()
        self._room(len(replacing), total)
        status.phase = Phase.MOVING
        batch = VersionsBatch(self.destination, datetime.now(timezone.utc))
        # Named and saved before the first move, should the process die from here on: a
        # record read after any death says which backup copies may lie in which batch.
        record.versions_batch, record.replacing = str(batch.relative), list(replacing)
        self._save()
        moved: List[Tuple[str, int]] = []
        try:
            for path, size in replacing:
                try:
                    batch.move_in(path)
                except FileNotFoundError:
                    self._left(path, LeftReason.MISSING_DESTINATION)
                    continue
                except OSError as exc:
                    self._left(path, LeftReason.NOT_COPIED, f"its backup copy could not be moved into versions: "
                                                            f"{exc.strerror or exc}")
                    continue
                moved.append((path, size))
            self._save()
            plan = PairPlan(new=tuple(Entry(path, size, 0) for path, size in moved))
            result = apply_pair(plan, GateDecision(Verdict.PROCEED), source_root=self.source_root,
                                destination_root=self.destination, batch=batch, engine=self.engine,
                                control=self.control, pump=self.pump,
                                list_path=engine_list_path(self.layout, self.repair_id))
        except BaseException as exc:
            # Whatever ends the repair here, no backup copy it moved stays in versions unnamed:
            # those whose new copy is not in place go back, and those whose new copy is are
            # recorded unread, pending for the next repair. What was moved is read from the
            # batch itself, not from ``moved``, which an interruption can leave a move behind;
            # a copy not moved leaves ``replacing``, its backup copy where it was. A death
            # during this leaves the copies not yet settled named in ``replacing``.
            ending = ("the repair failed before it was read back" if isinstance(exc, Exception)
                      else "the repair was interrupted before it was read back")
            moved = [(path, size) for path, size in replacing if self._may_hold(batch, path)]
            held = {path for path, _size in moved}
            record.replacing = [r for r in record.replacing if r[0] in held]
            for path, _size in self._settle(batch, moved, None):
                self._left(path, LeftReason.NOT_READ_BACK, ending)
            raise
        failed = {path_key(path): why for path, why in result.skipped}
        not_copied = None if result.cancelled is not None else \
            (lambda path: failed.get(path_key(path)) or result.engine_failure or result.destination_failure or "")
        copied = self._settle(batch, moved, not_copied)
        record.bytes_copied = result.bytes_copied
        self._save()
        status.phase = Phase.VERIFYING
        for (path, size), (same, why) in zip(copied, self._read_back([p for p, _s in copied])):
            if why:
                self._left(path, LeftReason.NOT_READ_BACK, why)
                continue
            if not same:
                self._left(path, LeftReason.STILL_DIFFERS)
                continue
            record.replaced.append((path, size))
            self._settled(path)
            self.say(f"replaced: {path}")
        if result.cancelled is not None:
            raise _Stop()

    @staticmethod
    def _may_hold(batch: VersionsBatch, path: str) -> bool:
        """Whether something may be at ``path``'s place in the batch: True when the
        destination cannot say, so the copy stays named for the version cleanup."""
        try:
            return batch.holds(path)
        except OSError:
            return True

    def _read_back(self, paths: Sequence[str]) -> List[Tuple[bool, str]]:
        """For each of ``paths``, replaced: (whether the new copy is the source's, why it could
        not be told — "" when it could), by the measure a verification of the destination
        compares by: both copies read, the fingerprint the service keeps against the source's
        of that kind (both read where the service keeps none for the copy), or the engine's
        check of the encrypted form."""
        if self.measure is Measure.ENCRYPTED and paths:
            return self._read_back_encrypted(paths)
        found = []
        for path in paths:
            source = self.source_root / path
            try:
                kept = self.destination.fingerprint(path, self.kind) if self.measure is Measure.FINGERPRINT else None
                if kept is None:
                    same = _digest(lambda: open_source_file(source), self.chunk) == \
                        _digest(lambda: self.destination.open_file(path), self.chunk)
                else:
                    make = hasher(self.kind)
                    mine = _digest(lambda: open_source_file(source), self.chunk, make) if make is not None else \
                        self.destination.fingerprint_local(source, self.kind)
                    same = mine == kept
            except OSError as exc:
                found.append((False, exc.strerror or str(exc)))
                continue
            found.append((same, ""))
        return found

    def _read_back_encrypted(self, paths: Sequence[str]) -> List[Tuple[bool, str]]:
        try:
            check = self.destination.check_encrypted(self.source_root, paths,
                                                     list_path=engine_list_path(self.layout, self.repair_id))
        except OSError as exc:
            return [(False, exc.strerror or str(exc))] * len(paths)
        report = check.report()
        found = []
        for path in paths:
            same, why = encrypted_outcome(report, path, self.source_root / path)
            found.append((bool(same), why))
        return found

    # --- the whole repair -----------------------------------------------------------------------

    def execute(self) -> Optional[RepairRecord]:
        try:
            self.board.open()
        except RuntimeError:
            raise RepairError("this repair is already in progress") from None
        offer_tray(self.layout, self.store.path)
        self.pump.start()
        diagnostics.note(f"repair {self.repair_id}: destination {self.index} of '{self.job.name}' ({self.job.job_id})")
        diagnostics.mark(diagnostics.ACTIVITY, id=self.repair_id, kind=KIND_REPAIR)
        try:
            contents = self._admit()
            if contents is None:
                return None
            found, todo = self._begin(contents)
            self.say(f"replacing the backup copies of {counted(len(todo), 'file')} that "
                     f"{agreeing(len(todo), 'differs', 'differ')} (verification of {_local_date(found.started)})")
            self._repair(todo)
            self.record.state = RepairState.COMPLETED
        except _Stop:
            self.record.state, self.record.detail = RepairState.STOPPED, "stopped by you"
        except _Failed as exc:
            diagnostics.note(f"repair {self.repair_id}: {exc}")
            self.record.state, self.record.detail = RepairState.FAILED, str(exc)
        except KeyboardInterrupt:
            if self.record is not None:
                self.record.state, self.record.detail = RepairState.INTERRUPTED, "interrupted at the console"
                self._end()
            raise
        except RepairError as exc:
            if self.record is None:
                raise
            diagnostics.note(f"repair {self.repair_id}: {exc}")
            self.record.state, self.record.detail = RepairState.FAILED, str(exc)
        except Exception as exc:  # a repair that fails unexpectedly says why and keeps what it did
            diagnostics.note_failure(f"repair {self.repair_id} failed unexpectedly")
            if self.record is None:
                raise
            self.record.state, self.record.detail = RepairState.FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            self._release()
        self._end()
        self.say(self.record.describe())
        return self.record

    def _end(self) -> None:
        self.record.ended = utc_now()
        self._save(final=True)

    def _release(self) -> None:
        if self.remotes is not None:
            self.remotes.close()
            self.remotes = None
        if self.snapshot.snapshot is not None:
            self.status.phase = Phase.RELEASING
            problem = self.snapshot.release()
            if problem:
                self.say(problem)
        self.pump.stop()
        self.hold.release()
        self.board.close()


def repair_destination(store: ConfigStore, job_id: str, index: Optional[int] = None, *, volume_source: VolumeSource,
                       marker: Optional[str] = None, sync_roots: Sequence[SyncRoot] = (), say: Optional[Say] = None,
                       snapshots: Optional[SnapshotProvider] = None, chunk_bytes: int = CHUNK_BYTES,
                       power: Optional[System] = None) -> Optional[RepairRecord]:
    """Replace the copies a destination's latest finished verification found to differ, as
    the module says: destination ``index`` (1-based) of a job, or the one whose marker is
    ``marker``, found in the job as this repair reads it. Returns the repair's record, or None
    when stopped before its turn came. Raises RepairError when the repair cannot start, StoreBusy when another
    program kept the job file.

    ``sync_roots``: the folders cloud clients synchronise on this computer, whose copies the
    repair leaves as they are — none unless given, so a caller repairing on this computer
    passes them; ``snapshots``: where volume snapshots come from, Windows' own unless given;
    ``power``: what keeps the computer awake and names the repair at shutdown while it works,
    Windows' own unless given."""
    layout = runtime_layout(store.path)
    contents = _load(store)
    job = contents.job(job_id)
    if job is None:
        raise RepairError(f"no job {job_id}")
    try:
        index = job.destination_number(index, marker)
    except ValueError as exc:
        raise RepairError(str(exc)) from None
    taken_up(layout, job_id, job.destinations[index - 1].marker_id,
             {r.run_id for r in observe_runs(layout) if r.alive})
    repairer = _Repairer(store, job, index, volume_source, sync_roots, say or (lambda text: None),
                         snapshots or default_snapshot_provider(), chunk_bytes, power)
    return repairer.execute()
