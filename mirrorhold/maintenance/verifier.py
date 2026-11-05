"""Running a verification of one destination, in a core process of its own.

``verify_destination`` is what the command line's ``verify`` executes; the GUI starts it
through ``run.launch.start_verification``. A run judges change by size and modification
time, so a copy damaged on its drive keeps both and no run copies it again; this is the
one check that compares content. It writes nothing on either side but its own
records. A full pass reads every file the same size and time on both sides; a pass of
new copies only those its baseline — the latest list of verified copies
(``maintenance.verified``) — does not hold as they are now, the others counted as
unchanged and not read. Both kinds are admitted, give way and are controlled alike.

A verification publishes its live status from the moment it starts (``records.status``,
kind "verification"), through which callers pause, resume and stop it. Then:

1. **Its turn** (``records.admission``), like a run's: it claims its source and its
   destination, reading both and writing neither, and the drives they lie on, and works
   at the same time as every run and verification that writes neither — a verification
   of another destination included — and, while the user's choice takes drives one at a
   time, that uses neither drive; from one snapshot of the configuration taken as it is
   admitted.
2. **The pair**: the source must resolve to one present volume, and the destination to one
   too — or, a remote's folder, be reached through one engine session on the configuration
   the preferences name (``remotes.reach``), opened once the verification's turn comes and
   closed when it gives way or ends — and the destination's marker must match; otherwise
   the verification cannot start (``VerifyError``). How its copies are compared is then
   asked of the destination (``compare.measure_of``).
3. **The pass**: the destination's unfinished pass of the kind asked for is continued from
   its cursor (``verification.resumable``) unless a restart was asked for — its identifier
   is this process's, chosen before its turn is waited for; two verifications of one
   destination conflict, whatever their kinds, so they never run at once. A pass of new
   copies begun where no list of verified copies is kept is a full pass: every file is new
   to it.
4. **Both sides listed** with the job's exclusions (``planning.listing``); every entry
   after the cursor sorted in key order into compared, changed, missing, extra or
   unreadable (``verification``). A shared destination (``endpoints.shared``) is listed
   as its record says its job has backed it up (``planning.sharing.recorded_scope``):
   beneath the names the job owns, less what it leaves where it is, which is never
   counted as extra or compared. A name the source gained since the last run, and a left
   entry the source holds something at now, are not the job's until a run claims them:
   the source's files there count as not backed up yet, and what the destination holds
   there — perhaps the user's own — is not compared. A shared destination whose record
   cannot be used is not verified.
5. **Each file compared**: on a drive both copies read in full, each side on a thread of
   its own so the slower drive sets the pace, SHA-256 digests compared. On a remote, by the
   fingerprint its service keeps against the source's of that kind, the source alone read;
   on an encrypted remote by the engine's check of the encrypted form, files handed to it
   in batches (``CHECK_FILES``, ``CHECK_BYTES``, ``CHECK_ENTRIES``) whose entries the cursor
   passes only once the batch is checked — a pass continued after its measure changed
   begins anew; on a remote that allows neither, and for a copy listed with no
   fingerprint, both copies read (``compare``). A source file that changed
   while it was read is counted as changed, not as a difference. One the baseline holds,
   in a pass of new copies, is counted as unchanged instead. Each found the same, or
   unchanged, goes into the pass's own list. The record is saved every few seconds, the
   list appended to first, so a shutdown loses little; the window reads it as often, and a
   save another program's hold refuses (``exchange``) waits for the next save
   rather than ending the pass. A finished pass's list is sorted and kept, and the
   destination's older lists removed.

The entries are made one at a time, in key order, from the two listings
(``pass_items``): a pass over millions of files keeps no second copy of what its
listings hold.

Pause holds both readers between two chunks. A stop ends the sitting within
``STOP_PATIENCE`` — nothing is written, so there is nothing in flight to finish — and
keeps every file already handled; the file being compared is compared again next time.
A read that does not return (a drive that hangs) is given up after that patience: the
readers are threads the process does not wait for, and the read is asked to cancel.

**A side that goes away is waited for, and ends the sitting only when it does not come
back.** A file that cannot be read is counted as unreadable — but when its side's folder
is gone, its drive was removed or reset, and every file after it would read the same way
and the pass would end as if complete. So a read that fails is followed by a look at
both folders; one gone — a remote whose engine no longer answers for its folder among
them — is looked for again for ``SIDE_PATIENCE``: back, the file is read again and the
pass goes on; still gone, the sitting ends as failed (``VerifyError``), the file counted
nowhere, and the next verification continues from there.

What the process says, and a failure no one foresaw with its traceback, goes to the
diagnostic log the command line opens (``records.diagnostics``). While it works — admitted and
not paused — it keeps the computer from falling asleep by itself and has Windows' shutdown screen
name it when the user's choices say so (``power``).

**A verification gives way to backups.** Backups are the product's purpose and a
verification is resumable by design, so whenever a run that conflicts with it waits —
one that writes what it reads, or one that uses a drive it uses and takes turns on that
drive — held back by verifications alone (``records.admission.gives_way``), the verification —
paused or not — saves its progress, gives its claim up and waits for its turn again,
behind that run; then it lists both sides again and continues from its cursor. A run it
does not conflict with works beside it.
"""
from __future__ import annotations

import hashlib
import heapq
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Tuple

from ..endpoints.endpoint import RemoteEndpoint
from ..endpoints.marker import MarkerState, marker_check
from ..endpoints.resolution import Presence, Resolution, resolve
from ..endpoints.shared import SharedError, SharedRecord, read_shared
from ..endpoints.volumes import VolumeSource
from ..engine.rclone import CryptCheck, open_source_file
from ..exchange import FileBusy
from ..jobs.exclusions import EntryFilter
from ..jobs.model import Job
from ..jobs.store import ConfigStore, LoadStatus, StoreBusy, StoreContents
from ..locations import RuntimeLayout, runtime_layout
from ..planning.estimate import MiB, estimate
from ..planning.listing import ListingError, Side, SkipReason, TreeListing, list_tree, parent_keys, path_key
from ..planning.plan import time_window_ns
from ..planning.sharing import recorded_scope
from ..power import Hold, System, verification_reason
from ..records import diagnostics
from ..records.admission import ADMISSION_INTERVAL, activity_claim, admit, gives_way, withdraw
from ..records.record import read_records, throughputs, utc_now
from ..records.status import (KIND_VERIFICATION, LiveStatus, Phase, StatusBoard, engine_list_path, is_verification,
                              observe_runs)
from ..remotes.facts import Reach, SetupState
from ..remotes.reach import Reached, reach
from ..run.control import RunControl, StatusPump
from ..storage.destination import DestinationStorage, StorageError
from ..storage.drive import DriveStorage
from ..tray import offer as offer_tray
from . import verified
from .compare import encrypted_outcome, hasher, measure_of
from .verification import (FILE_SECONDS, Measure, VerificationMode, VerificationRecord, VerificationState,
                           read_verifications, resumable, verification_throughputs, write_verification)

CHUNK_BYTES = 4 * MiB
SAVE_INTERVAL = 5.0
"""Seconds between two saves of a pass's progress."""
SAVE_PATIENCE = 1.0
"""Seconds a save of progress waits for another program's hold before it leaves the
save to the next one."""
SAVE_AGAIN = 1.0
"""Seconds before a save of progress that did not land is tried again."""
END_SAVE_PATIENCE = 30.0
"""Seconds the save that ends a sitting waits for another program's hold."""
GIVE_WAY_INTERVAL = 2.0
"""Seconds between two looks for a run waiting for what the verification holds."""
SIDE_PATIENCE = 60.0
"""Seconds a side whose folder went away is looked for before the sitting ends: a USB
drive that resets, or a pool that re-mounts, comes back well within it."""
SIDE_LOOK = 1.0
"""Seconds between two looks for a side that went away."""
STOP_PATIENCE = 10.0
"""Seconds a stop, or giving way, waits for a read in progress before giving it up."""
RATE_WINDOW = 10.0
CHECK_FILES = 64
CHECK_BYTES = 256 * MiB
"""The most files, and about the most bytes, handed to one check of an encrypted remote:
what a stop or giving way leaves to be checked again, beside one process's start for the
batch (about a third of a second, measured)."""
CHECK_ENTRIES = 4096
"""The most entries a batch on an encrypted remote holds, checked files or not, so the
cursor and the saved progress move through a run of entries no check is needed for — the
copies a pass of new copies leaves unread above all."""

COMPARE, CHANGED, MISSING, EXTRA, UNREADABLE = "compare", "changed", "missing", "extra", "unreadable"
_AGAIN = "again"

Say = Callable[[str], None]


class VerifyError(Exception):
    """A verification that cannot start, or cannot go on: its pair is not usable."""


class _Stop(Exception):
    def __init__(self, abandoned: str = "", seconds: float = 0.0):
        super().__init__(abandoned)
        self.abandoned = abandoned          # the file whose read was given up, as the pass names it
        self.seconds = seconds

    def detail(self) -> str:
        if not self.abandoned:
            return "stopped by you"
        name = self.abandoned.rsplit("\\", 1)[-1]
        return (f"stopped by you; {name}, which was being read, did not answer within {self.seconds:.0f} seconds "
                "and is compared again next time")


class _GiveWay(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Item:
    """One entry of a pass, in key order: what it is and how it is handled."""

    key: str
    kind: str
    path: str                     # as the source spells it, else as the destination does
    size: int = 0
    destination_path: str = ""    # as the destination spells it, for a file compared
    mtime_ns: int = 0
    why: str = ""
    destination_mtime_ns: int = 0  # for a file compared

    def fingerprint(self) -> int:
        """A file compared, as its list of verified copies knows it (``verified``)."""
        return verified.fingerprint(self.key, self.size, self.mtime_ns, self.destination_mtime_ns)


def pass_items(source: TreeListing, destination: TreeListing, window_ns: int) -> Iterator[Item]:
    """Every entry of the two listings, in key order, with how a pass handles it — made one
    at a time from the listings, so a pass holds no second copy of them."""
    skipped: Dict[str, Item] = {}
    unlistable: Dict[str, str] = {}
    for side, listing in (("source", source), ("destination", destination)):
        for skip in listing.skipped:
            if side == "source" and skip.reason in (SkipReason.PLACEHOLDER, SkipReason.LINK):
                continue                    # left out by design, as a run leaves them
            key = path_key(skip.path)
            if skip.reason is SkipReason.UNLISTABLE:
                unlistable[key] = side
            why = {SkipReason.UNLISTABLE: "folder could not be listed", SkipReason.UNREADABLE: "could not be read",
                   SkipReason.LINK: "is a link or junction"}.get(skip.reason, skip.reason.value)
            skipped[key] = Item(key, UNREADABLE, skip.path, why=f"the {side} {why}"
                                + (f": {skip.detail}" if skip.detail else ""))
    previous = None
    for key in heapq.merge(sorted(source.files), sorted(destination.files), sorted(skipped)):
        if key == previous:
            continue
        previous = key
        item = skipped.get(key)
        if item is None:
            entry, other = source.files.get(key), destination.files.get(key)
            if other is None:
                item = Item(key, MISSING, entry.path, entry.size)
            elif entry is None:
                item = Item(key, EXTRA, other.path, other.size)
            elif entry.size == other.size and abs(entry.mtime_ns - other.mtime_ns) <= window_ns:
                item = Item(key, COMPARE, entry.path, entry.size, other.path, entry.mtime_ns,
                            destination_mtime_ns=other.mtime_ns)
            else:
                item = Item(key, CHANGED, entry.path, entry.size)
            if unlistable and item.kind in (MISSING, EXTRA):
                side = next((unlistable[p] for p in parent_keys(key) if p in unlistable), None)
                if side is not None:
                    item = Item(key, UNREADABLE, item.path, why=f"beneath a {side} folder that could not be listed")
        yield item


Opener = Callable[[], BinaryIO]
"""Opens one side's copy of a file for reading from its start."""


def _digest(opener: Opener, slot: int, progress: List[int], abort: threading.Event, hold: threading.Event,
            chunk: int, pace: Optional[Callable[[], None]], make=hashlib.sha256) -> Optional[str]:
    """The digest of a file read in full, of the kind ``make`` computes, in lower-case hex;
    None when aborted."""
    digest = make()
    with opener() as handle:
        while True:
            while hold.is_set() and not abort.is_set():
                time.sleep(0.05)
            if abort.is_set():
                return None
            data = handle.read(chunk)
            if not data:
                return digest.hexdigest()
            digest.update(data)
            progress[slot] += len(data)
            if pace is not None:
                pace()


class _Read(threading.Thread):
    """One side's read of one file, on a thread the process does not wait for: a read that
    never returns — a drive that hangs — keeps no one from stopping. ``work``, when given,
    replaces the read: it returns the digest itself (a fingerprint the engine computes),
    None when it gave up because ``abort`` was set."""

    def __init__(self, opener: Opener, slot: int, progress: List[int], abort: threading.Event,
                 hold: threading.Event, chunk: int, pace: Optional[Callable[[], None]], ended: threading.Event,
                 make=hashlib.sha256, work: Optional[Callable[[threading.Event], Optional[str]]] = None):
        super().__init__(name="verify-read", daemon=True)
        self._arguments = (opener, slot, progress, abort, hold, chunk, pace, make)
        self._work, self._abort = work, abort
        self._ended = ended
        self.digest: Optional[str] = None
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.digest = self._work(self._abort) if self._work is not None else _digest(*self._arguments)
        except BaseException as exc:  # handed to the pass, which decides
            self.error = exc
        finally:
            self._ended.set()

    def cancel_io(self) -> None:
        """Ask Windows to cancel the read this thread is blocked in, if it is."""
        if sys.platform != "win32" or not self.is_alive() or self.native_id is None:
            return
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenThread.restype = ctypes.c_void_p
        k32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenThread(0x0001, 0, self.native_id)        # THREAD_TERMINATE, which the cancel needs
        if handle:
            k32.CancelSynchronousIo(handle)
            k32.CloseHandle(handle)


def _stopped_answering(reason: str) -> str:
    """Why a sitting on a remote ended when its engine no longer answered for it."""
    return f"the remote stopped answering: {reason}; the next verification continues from here"


def _load(store: ConfigStore) -> StoreContents:
    result = store.load()
    if result.status in (LoadStatus.UNSUPPORTED, LoadStatus.CORRUPT):
        raise VerifyError(result.detail)
    return result.contents


def _live_verification(layout: RuntimeLayout, marker_id: str) -> bool:
    return any(r.alive and is_verification(r.status) and r.status.get("marker_id") == marker_id
               for r in observe_runs(layout))


class _Verifier:
    def __init__(self, store: ConfigStore, job: Job, index: int, volume_source: VolumeSource, say: Say,
                 verification_id: str, chunk: int, pace, after_item, side_patience: float, stop_patience: float,
                 power: Optional[System] = None, mode: VerificationMode = VerificationMode.FULL,
                 asked: VerificationMode = VerificationMode.FULL):
        """``mode``: the kind of pass to begin, or that the pass continued is; ``asked``: the
        kind the caller asked for, new copies where none is kept to build on reading as full."""
        self.store = store
        self.mode = mode
        self.asked = asked
        self.layout = runtime_layout(store.path)
        self.job = job
        self.index = index
        self.marker_id = job.destinations[index - 1].marker_id
        self.volume_source = volume_source
        self.say = say
        self.chunk = chunk
        self.pace = pace
        self.after_item = after_item
        self.side_patience = side_patience
        self.stop_patience = stop_patience
        self.verification_id = verification_id
        self.status = LiveStatus(verification_id, os.getpid(), job.job_id, job.name, "manual", utc_now(),
                                 pair_index=index, pair_total=1, marker_id=self.marker_id,
                                 destination=f"destination {index}", pair_started=utc_now(), kind=KIND_VERIFICATION,
                                 verify=self._progress_document())
        self.board = StatusBoard(self.layout, self.status)
        self.control = RunControl(self.status)
        self.hold = Hold(verification_reason(job.name), lambda: self.status.admitted and not self.status.paused,
                         lambda: _load(store).preferences.activities, system=power)
        self.pump = StatusPump(self.board, self.control, on_tick=self.hold.tick)
        self.record: Optional[VerificationRecord] = None
        self.baseline: Optional[verified.VerifiedCopies] = None   # a pass of new copies: the copies left unread
        self.verified: Optional[verified.ListWriter] = None       # the copies this pass finds the same
        self.source_root: Optional[Path] = None
        self.destination: Optional[DestinationStorage] = None
        self.shared: Optional[SharedRecord] = None     # a shared destination's record: what its job owns there
        self.preferences = None                        # the user's choices as the verification was admitted
        self.remotes: Optional[Reached] = None         # the session a remote destination is reached through
        self.measure, self.kind = Measure.READ, None   # how copies are compared, and the fingerprint kind
        self.fingerprints: Dict[str, str] = {}         # a fingerprint pass: key -> what the service keeps
        self.window_ns = 0
        self._looked = 0.0
        self._saved = 0.0
        self._samples: List[Tuple[float, int]] = []

    # --- status ------------------------------------------------------------------------

    def _progress_document(self) -> dict:
        return {"files": 0, "bytes": 0, "files_done": 0, "bytes_done": 0, "differ": 0, "listing": None,
                "gave_way": False, "resumed": False, "estimate": None, "mode": self.mode.value}

    def _progress(self, in_flight: int = 0) -> None:
        record, verify = self.record, self.status.verify
        verify.update(files=record.files_total, bytes=record.bytes_total, files_done=record.compared_files,
                      bytes_done=record.compared_bytes + in_flight, differ=record.differ_total)
        now = time.monotonic()
        done = verify["bytes_done"]
        self._samples = [(t, b) for t, b in self._samples if now - t <= RATE_WINDOW] + [(now, done)]
        first_t, first_b = self._samples[0]
        rate = (done - first_b) / (now - first_t) if now - first_t >= 1.0 else 0.0
        self.status.rate = rate
        self.status.eta_seconds = (max(0, record.bytes_total - done) / rate) if rate > 0 else None

    # --- waiting and giving way ----------------------------------------------------------

    def _admit(self) -> bool:
        """Wait for this verification's turn (``records.admission``), the job and the user's
        choices as the store holds them at each attempt — a job file another program holds
        is read again at the next — and the drives as they are then: taken in turn where the
        choice and the drive's kind say so, and what it asks of the computer while it works. False when
        stopped."""
        self.status.phase, self.status.paused = Phase.WAITING, False
        while self.control.wanted.cancel is None:
            try:
                contents = _load(self.store)
            except StoreBusy:
                self.control.wait_change(ADMISSION_INTERVAL)
                continue
            job = self._pair_in(contents)
            claim = activity_claim(job, [self.index], self._volumes(), contents.preferences.activities,
                                   verifying=True)
            if admit(self.board, claim).admitted:
                self.job, self.preferences = job, contents.preferences
                self.hold.choose(contents.preferences.activities)
                return True
            self.control.wait_change(ADMISSION_INTERVAL)
        return False

    def _pair_in(self, contents: StoreContents) -> Job:
        """The job as ``contents`` hold it; VerifyError when it no longer has this pair."""
        job = contents.job(self.job.job_id)
        if job is None:
            raise VerifyError("the job was removed while this verification waited")
        if len(job.destinations) < self.index or job.destinations[self.index - 1].marker_id != self.marker_id:
            raise VerifyError("the job's destinations changed while this verification waited")
        return job

    def _should_give_way(self) -> bool:
        now = time.monotonic()
        if now - self._looked < GIVE_WAY_INTERVAL:
            return False
        self._looked = now
        return gives_way(self.layout, self.status)

    def _between(self) -> None:
        """Between two entries: wait while paused; stop or give way when asked."""
        while True:
            wanted = self.control.wanted
            if wanted.cancel is not None:
                raise _Stop()
            if self._should_give_way():
                raise _GiveWay()
            if not wanted.paused:
                self.status.paused = False
                return
            self.status.paused = True
            self.control.wait_change(0.5)

    def _listed(self, entries: int) -> None:
        self.status.listed = entries
        self._between()

    # --- the pair ----------------------------------------------------------------------

    def _volumes(self):
        """The volumes present now; VerifyError when they cannot be scanned."""
        try:
            return self.volume_source.scan().volumes
        except Exception as exc:  # a scan that fails is a reason in words, its details in the log
            diagnostics.note_failure(f"verification {self.verification_id}: the drives could not be checked")
            raise VerifyError(f"the drives could not be checked: {type(exc).__name__}: {exc}") from None

    @staticmethod
    def _present(which: str, resolution: Resolution) -> None:
        if resolution.presence is Presence.ABSENT:
            raise VerifyError(f"the {which}'s drive is not attached")
        if resolution.presence is Presence.AMBIGUOUS:
            raise VerifyError(f"two attached drives match the {which}")

    def _reach(self, remote: RemoteEndpoint) -> DestinationStorage:
        """The remote folder's storage through a session opened now; VerifyError, with why,
        when the configuration cannot be opened or the folder cannot be reached."""
        self._close_remote()
        self.remotes = reach(self.preferences, self.store.path, [remote])
        setup = self.remotes.setup
        if setup.state is not SetupState.READY:
            raise VerifyError(setup.reason)
        facts = self.remotes.facts(remote)
        if facts is None:
            raise VerifyError(f"the rclone configuration holds no remote '{remote.remote}'")
        if facts.reach is Reach.UNREACHABLE:
            raise VerifyError(f"{remote.describe()} cannot be reached: {facts.reason}; verify again later")
        storage = self.remotes.storage(remote)
        if storage is None:
            raise VerifyError(f"{remote.describe()} cannot be used: "
                              f"{facts.reason or 'a file stands where its folder is named'}")
        return storage

    def _close_remote(self) -> None:
        if self.remotes is not None:
            self.remotes.close()
            self.remotes = None

    def _resolve(self) -> None:
        volumes = self._volumes()
        destination = self.job.destinations[self.index - 1]
        source = resolve(self.job.source.endpoint, volumes)
        self._present("source", source)
        if destination.remote is not None:
            storage = self._reach(destination.remote)
        else:
            target = resolve(destination.endpoint, volumes)
            self._present("destination", target)
            storage = DriveStorage(target.location, target.volume)
        try:
            state, marker = marker_check(storage, self.job.job_id, self.marker_id)
        except StorageError as exc:          # only a remote raises it: it stopped answering
            raise VerifyError(_stopped_answering(exc.strerror)) from None
        if state is not MarkerState.MATCH:
            raise VerifyError("the destination has no marker of this job yet: its first run has not been confirmed"
                              if state is MarkerState.MISSING else
                              "the destination's marker does not match this job")
        self.shared = None
        if marker.shared:
            try:
                self.shared = read_shared(storage, self.job.job_id, self.marker_id)
            except SharedError as exc:
                raise VerifyError(f"the destination's record of what to leave where it is cannot be used: {exc}") \
                    from None
            except StorageError as exc:      # only a remote raises it: it stopped answering
                raise VerifyError(_stopped_answering(exc.strerror)) from None
        self.source_root, self.destination = source.location, storage
        self.window_ns = max(time_window_ns(source.volume.file_system), storage.time_window_ns)
        try:
            self.measure, self.kind = measure_of(storage, source.location,
                                                 engine_list_path(self.layout, self.verification_id))
        except OSError as exc:
            raise VerifyError(_stopped_answering(exc.strerror or str(exc))) from None
        self.status.destination = f"destination {self.index} at {storage.describe()}"

    def _begin(self, restart: bool) -> None:
        job = self.job
        self._resolve()
        records = read_verifications(self.layout, job.job_id)
        mine = [r for r in records if r.verification_id == self.verification_id]
        continuing = bool(mine) and not restart
        if continuing and (mine[-1].measure, mine[-1].fingerprint) != (self.measure, self.kind):
            # A pass is compared by one measure: its counts and its rate mean nothing mixed.
            ended = mine[-1]
            ended.state, ended.updated = VerificationState.FAILED, utc_now()
            ended.detail = "how the destination's copies are compared changed since it began; a new pass began"
            try:
                write_verification(self.layout, ended, patience=END_SAVE_PATIENCE)
            except OSError:
                diagnostics.note_failure(f"verification {self.verification_id}: its earlier pass could not be ended")
            continuing = False
        if continuing:
            record = mine[-1]
            record.sittings += 1
            record.state, record.detail, record.updated = VerificationState.RUNNING, "", utc_now()
            self.status.verify["resumed"] = True
        else:
            now = utc_now()
            record = VerificationRecord(self.verification_id, job.job_id, job.name, self.marker_id, self.index, now,
                                        now, mode=self.mode)
        if record.mode is VerificationMode.NEW_COPIES:
            found = verified.baseline(self.layout, records, self.marker_id, prefer=record.baseline)
            if found is not None:
                record.baseline, self.baseline = found[0].verification_id, found[1]
            elif not continuing:
                record.mode = VerificationMode.FULL       # its list went while it waited: every file is new to it
        self.mode = record.mode
        self.status.verify["mode"] = record.mode.value
        record.measure, record.fingerprint = self.measure, self.kind
        self.verified = verified.ListWriter(self.layout, record, continuing=continuing)
        self.record = record

    def _save(self, *, finished: bool = False) -> None:
        """Save the pass's progress. A save another program's hold refuses is left to the
        next one, a moment later: the window reads the record as often as it is saved, and
        a refused save must not end the pass. The list of verified copies is appended to
        first, so the cursor saved never passes what it holds."""
        self.verified.flush()
        self.record.updated = utc_now()
        try:
            write_verification(self.layout, self.record, self.destination if finished else None,
                               patience=SAVE_PATIENCE)
        except FileBusy:
            diagnostics.note(f"verification {self.verification_id}: its progress could not be saved now; "
                             "it is saved a moment later")
            self._saved = time.monotonic() - SAVE_INTERVAL + SAVE_AGAIN
            return
        self._saved = time.monotonic()

    def _save_end(self, *, finished: bool = False) -> bool:
        """The save that ends a sitting, borne longer; one that still cannot be made is
        said in the log, the record keeping its last saved progress for the next sitting.
        False then."""
        if self.verified is not None:
            self.verified.flush()
        self.record.updated = utc_now()
        try:
            write_verification(self.layout, self.record, self.destination if finished else None,
                               patience=END_SAVE_PATIENCE)
        except OSError:
            diagnostics.note_failure(f"verification {self.verification_id}: its record could not be saved")
            return False
        return True

    def _keep_list(self) -> None:
        """A finished pass's list of verified copies sorted and kept — only once its record
        says it finished, so a pass continued after a save that failed appends to the same
        partial list — and the destination's other lists removed."""
        if not self.verified.finish():
            diagnostics.note(f"verification {self.verification_id}: its list of verified copies could not be "
                             "sorted; it is read as it was written")
            return
        live = {r.run_id for r in observe_runs(self.layout) if r.alive and r.run_id != self.verification_id}
        verified.prune(self.layout, read_verifications(self.layout, self.job.job_id), self.record, live)

    # --- one sitting ---------------------------------------------------------------------

    def _list(self) -> Tuple[TreeListing, TreeListing]:
        entry_filter = EntryFilter.for_source(self.job.source)
        destination = self.job.destinations[self.index - 1]
        self.status.phase, self.status.listed = Phase.LISTING, 0
        try:
            self.status.verify["listing"] = "source"
            source = list_tree(self.source_root, Side.SOURCE, drive_root=self.job.source.endpoint.is_drive_root,
                               entry_filter=entry_filter, progress=self._listed)
            self.status.verify["listing"], self.status.listed = "destination", 0
            record = self.shared
            target = self.destination.list_tree(
                drive_root=destination.endpoint.is_drive_root, entry_filter=entry_filter, progress=self._listed,
                scope=recorded_scope(record.own, record.left) if record is not None else None,
                fingerprint=self.kind if self.measure is Measure.FINGERPRINT else None)
        except ListingError as exc:
            raise VerifyError(str(exc)) from None
        self.fingerprints = target.fingerprints
        self.status.verify["listing"] = None
        if not source.present:
            raise VerifyError(f"the source folder {self.source_root} does not exist")
        return source, target

    def _rates(self) -> List[float]:
        """The rates the estimate takes: this destination's earlier passes compared by this
        pass's measure. A remote read by download with none takes the rates its runs copied
        onto it at (the maintainer's decision, 2026-10-29) — the network sets both — and the
        estimate's stated default where those are none either; a pass compared by fingerprint
        reads the source drive alone, as the default assumes."""
        samples = verification_throughputs(read_verifications(self.layout, self.job.job_id), self.marker_id,
                                           self.record.measure)
        if not samples and self.record.measure is Measure.READ \
                and self.job.destinations[self.index - 1].remote is not None:
            samples = throughputs(read_records(self.layout, self.job.job_id), self.marker_id)
        return samples

    def _estimate(self, files: int, remaining: int, listing_seconds: float) -> None:
        guess = estimate(remaining, listing_seconds, self._rates(), files=files, file_seconds=FILE_SECONDS)
        self.status.verify["estimate"] = {"bytes": remaining, "files": files, "shortest": guess.shortest_seconds,
                                          "longest": guess.longest_seconds, "samples": guess.samples,
                                          "low_rate": guess.low_rate, "high_rate": guess.high_rate,
                                          "low_file_seconds": guess.low_file_seconds,
                                          "high_file_seconds": guess.high_file_seconds,
                                          "listing_seconds": listing_seconds}

    def _sitting(self) -> None:
        source, target = self._list()
        record = self.record
        record.listing_seconds = (source.seconds, target.seconds)

        def remaining() -> Iterator[Item]:
            return (i for i in pass_items(source, target, self.window_ns)
                    if record.cursor is None or i.key > record.cursor)

        files = size = 0
        for item in remaining():
            if item.kind == COMPARE and not self._unchanged(item):
                files, size = files + 1, size + item.size
        record.files_total = record.compared_files + files
        record.bytes_total = record.compared_bytes + size
        self._estimate(files, size, source.seconds + target.seconds)
        self.status.phase = Phase.VERIFYING
        self._progress()
        self._save()
        # Every entry is handled at once, but on an encrypted remote, whose files the engine
        # checks in batches: there entries wait until enough files are to be checked.
        batch: List[Item] = []
        files = size = 0
        for item in remaining():
            self._between()
            batch.append(item)
            if self._checked(item):
                files, size = files + 1, size + item.size
            if record.measure is not Measure.ENCRYPTED or files >= CHECK_FILES or size >= CHECK_BYTES \
                    or len(batch) >= CHECK_ENTRIES:
                self._settle(batch)
                batch, files, size = [], 0, 0
        if batch:
            self._settle(batch)

    def _checked(self, item: Item) -> bool:
        """Whether ``item`` goes to the engine's check of the encrypted form."""
        return self.record.measure is Measure.ENCRYPTED and item.kind == COMPARE and not self._unchanged(item)

    def _settle(self, batch: List[Item]) -> None:
        """Handle the entries of ``batch`` in key order, the cursor passing each once it is
        handled; on an encrypted remote its files are checked first, all together."""
        record = self.record
        outcomes: Dict[str, Tuple[str, str]] = {}
        checked = [i for i in batch if self._checked(i)]
        if checked:
            outcomes = self._check_encrypted(checked)
        for item in batch:
            self._handle(item, outcomes.get(item.key))
            record.cursor = item.key
            self._progress()
            if self.after_item is not None:
                self.after_item(item.path)
            if time.monotonic() - self._saved >= SAVE_INTERVAL:
                self._save()

    def _unchanged(self, item: Item, fingerprint: Optional[int] = None) -> bool:
        """A file compared that the baseline holds as it is now: a pass of new copies leaves it unread."""
        if self.baseline is None:
            return False
        return (item.fingerprint() if fingerprint is None else fingerprint) in self.baseline

    def _handle(self, item: Item, checked: Optional[Tuple[str, str]] = None) -> None:
        """Count ``item`` in the record; a file to compare is compared now, unless its
        outcome is ``checked`` already (the engine's check of an encrypted remote)."""
        record = self.record
        if item.kind == CHANGED:
            record.changed_total += 1
        elif item.kind == MISSING:
            record.missing_total += 1
        elif item.kind == EXTRA:
            record.extra_total += 1
        elif item.kind == UNREADABLE:
            record.add_unreadable(item.path, item.why)
        elif self._unchanged(item, fingerprint := item.fingerprint()):
            record.unchanged_files += 1
            record.unchanged_bytes += item.size
            self.verified.add(fingerprint)
        else:
            if checked is not None:
                outcome, why = checked
            else:
                outcome, why = self._compare(item)
                while outcome == _AGAIN:          # a side went away during the read and came back
                    outcome, why = self._compare(item)
            if outcome == CHANGED:
                record.changed_total += 1
            elif outcome == UNREADABLE:
                record.add_unreadable(item.path, why)
            else:
                record.compared_files += 1
                record.compared_bytes += item.size
                if record.measure is Measure.FINGERPRINT and item.key not in self.fingerprints:
                    record.read_files += 1
                    record.read_bytes += item.size
                if outcome == "differ":
                    record.add_differ(item.path, item.size)
                    self.say(f"differs: {item.path}")
                else:
                    self.verified.add(fingerprint)

    def _compare(self, item: Item) -> Tuple[str, str]:
        """("same" | "differ" | CHANGED | UNREADABLE | _AGAIN, why). Raises _Stop or
        _GiveWay, the file then counted nowhere: it is compared again. In a pass by
        fingerprint, a copy the service listed with one is compared with the source's of that
        kind, the source alone read — by the engine for a kind not computed here; any other is
        read on both sides."""
        source = self.source_root / item.path
        kept = self.fingerprints.get(item.key) if self.measure is Measure.FINGERPRINT else None
        abort, hold, ended = threading.Event(), threading.Event(), threading.Event()
        if kept is None:
            openers = (lambda: open_source_file(source), lambda: self.destination.open_file(item.destination_path))
            progress = [0, 0]
            readers = [_Read(opener, slot, progress, abort, hold, self.chunk, self.pace, ended)
                       for slot, opener in enumerate(openers)]
        else:
            make = hasher(self.kind)
            work = None if make is not None else \
                (lambda stop: self.destination.fingerprint_local(source, self.kind, give_up=stop.is_set))
            progress = [0]
            readers = [_Read(lambda: open_source_file(source), 0, progress, abort, hold, self.chunk, self.pace, ended,
                             make or hashlib.sha256, work)]
        for reader in readers:
            reader.start()
        reason, asked = None, 0.0
        self.status.in_flight = [[item.path, item.size, 0]]
        last = time.monotonic()
        try:
            while True:
                ended.wait(0.25)
                ended.clear()
                now = time.monotonic()
                if not hold.is_set():
                    self.record.seconds += now - last
                last = now
                if not any(r.is_alive() for r in readers):
                    break
                wanted = self.control.wanted
                if reason is None and wanted.cancel is not None:
                    reason = _Stop
                elif reason is None and self._should_give_way():
                    reason = _GiveWay
                if reason is not None:
                    if not abort.is_set():
                        abort.set()
                        asked = now
                    elif now - asked >= self.stop_patience:
                        for reader in readers:
                            reader.cancel_io()
                        diagnostics.note(f"verification {self.verification_id}: the read of {item.path} did not "
                                         f"answer within {self.stop_patience:.0f} seconds and was given up")
                        if reason is _Stop:
                            raise _Stop(item.path, self.stop_patience)
                        break
                if wanted.paused and reason is None:
                    hold.set()
                    self.status.paused = True
                else:
                    hold.clear()
                    self.status.paused = False
                self.status.in_flight = [[item.path, item.size, min(progress)]]
                self._progress(min(progress))
        finally:
            self.status.in_flight = []
        if reason is not None:
            raise reason()
        for side, reader in zip(("source", "destination"), readers):
            if reader.error is not None:
                if self._sides_back():
                    return _AGAIN, ""
                failure = reader.error
                text = getattr(failure, "strerror", None) or str(failure)
                return UNREADABLE, f"the {side} copy could not be read: {text}"
        try:
            now = os.stat(source)
        except OSError as exc:
            if self._sides_back():
                return _AGAIN, ""
            return UNREADABLE, f"the source copy could not be read: {exc.strerror or exc}"
        if now.st_size != item.size or abs(now.st_mtime_ns - item.mtime_ns) > self.window_ns:
            return CHANGED, ""
        first = readers[0].digest
        second = kept if kept is not None else readers[1].digest
        return ("same" if first == second else "differ"), ""

    def _check_encrypted(self, items: List[Item]) -> Dict[str, Tuple[str, str]]:
        """The outcome of each of ``items`` by the engine's check of the encrypted form, by
        key, as ``_compare`` gives one. Pause suspends the check; a stop or giving way ends it,
        ``_Stop`` or ``_GiveWay`` then raised and every one of ``items`` counted nowhere: they
        are checked again. A check that reported nothing and ended with an error is followed by
        a look at both folders, as a read that fails is, and made again when they are back."""
        while True:
            try:
                check = self.destination.check_encrypted(self.source_root, [i.path for i in items],
                                                         list_path=engine_list_path(self.layout,
                                                                                    self.verification_id))
            except OSError as exc:
                if self._sides_back():
                    continue
                return {i.key: (UNREADABLE, f"the engine could not check it: {exc.strerror or exc}") for i in items}
            report = self._watch(check, items)
            if report.checked or report.exit_code == 0 or not self._sides_back():
                break
        found: Dict[str, Tuple[str, str]] = {}
        for item in items:
            source = self.source_root / item.path
            same, why = encrypted_outcome(report, item.path, source)
            if same is None:
                found[item.key] = (UNREADABLE, why)
                continue
            try:
                now = os.stat(source)
            except OSError as exc:
                found[item.key] = (UNREADABLE, f"the source copy could not be read: {exc.strerror or exc}")
                continue
            if now.st_size != item.size or abs(now.st_mtime_ns - item.mtime_ns) > self.window_ns:
                found[item.key] = (CHANGED, "")
            else:
                found[item.key] = ("same" if same else "differ", "")
        return found

    def _watch(self, check: CryptCheck, items: List[Item]):
        """Follow a check of an encrypted remote to its end, as ``_compare`` follows a read."""
        total = sum(i.size for i in items)
        sizes = {i.path: i.size for i in items}
        reason = None
        last = time.monotonic()
        try:
            while check.poll() is None:
                time.sleep(0.25)
                now = time.monotonic()
                wanted = self.control.wanted
                if wanted.cancel is not None:
                    reason = _Stop
                elif self._should_give_way():
                    reason = _GiveWay
                if reason is not None:
                    check.stop()
                    break
                if wanted.paused:
                    check.suspend()
                    self.status.paused = True
                else:
                    check.resume()
                    self.status.paused = False
                    self.record.seconds += now - last
                last = now
                done = sum(sizes.get(path, 0) for path in check.checked)
                self.status.in_flight = [[items[0].path, total, done]]
                self._progress(done)
            else:
                self.record.seconds += time.monotonic() - last
        finally:
            self.status.in_flight = []
            report = check.report(timeout=self.stop_patience)
        if reason is not None:
            raise reason()
        return report

    def _gone(self) -> Optional[Tuple[str, str]]:
        """The first side whose folder is not there — a remote's whose engine does not answer
        for it included — or None."""
        if not self.source_root.is_dir():
            return "source", str(self.source_root)
        try:
            if not self.destination.is_folder():
                return "destination", self.destination.describe()
        except OSError as exc:
            return "destination", f"{self.destination.describe()} ({exc.strerror or exc})"
        return None

    def _sides_back(self) -> bool:
        """After a read failed: False when both sides' folders are there — the file itself
        could not be read. A side's folder that is gone, as when its drive is removed or
        resets, is looked for for ``side_patience`` seconds: True once it is back, the file
        to be read again; VerifyError when it is not — the file is then not unreadable,
        and neither would be the rest. A stop ends the wait."""
        gone = self._gone()
        if gone is None:
            return False
        which, root = gone
        diagnostics.note(f"verification {self.verification_id}: the {which} folder {root} went away; waiting up to "
                         f"{self.side_patience:.0f} seconds for it to come back")
        deadline = time.monotonic() + self.side_patience
        while gone is not None:
            if self.control.wanted.cancel is not None:
                raise _Stop()
            if time.monotonic() >= deadline:
                which, root = gone
                if which == "destination" and self.destination.local_folder is None:
                    raise VerifyError(_stopped_answering(root))
                raise VerifyError(f"the {which} folder {root} went away and did not come back within "
                                  f"{self.side_patience:.0f} seconds: was its drive removed?")
            self.control.wait_change(min(SIDE_LOOK, max(0.0, deadline - time.monotonic())))
            gone = self._gone()
        diagnostics.note(f"verification {self.verification_id}: the {which} folder came back; the file is read again")
        return True

    # --- the whole verification ------------------------------------------------------------

    def execute(self, restart: bool) -> Optional[VerificationRecord]:
        try:
            self.board.open()
        except RuntimeError:
            raise VerifyError("this destination is being verified already") from None
        offer_tray(self.layout, self.store.path)
        self.pump.start()
        diagnostics.note(f"verification {self.verification_id}: destination {self.index} of '{self.job.name}' "
                         f"({self.job.job_id})")
        diagnostics.mark(diagnostics.ACTIVITY, id=self.verification_id, kind=KIND_VERIFICATION)
        try:
            if not self._admit():
                return None
            self._begin(restart)
            if self.asked is VerificationMode.NEW_COPIES and self.record.full:
                self.say(f"no list of verified copies is kept for destination {self.index}: "
                         + ("its unfinished full verification is continued" if self.status.verify["resumed"]
                            else "every file is read, as a full verification"))
            what = "destination" if self.record.full else "the new copies of destination"
            self.say(f"verifying {what} {self.index} of '{self.job.name}'{self._how()}")
            while True:
                try:
                    self._sitting()
                    break
                except _GiveWay:
                    self._save()
                    self.status.phase, self.status.paused = Phase.WAITING, False
                    self.status.verify["gave_way"] = True
                    self._close_remote()
                    withdraw(self.board)
                    self.say("a backup that writes what this verification reads, or uses a drive it uses, is "
                             "waiting: giving way, to continue after it")
                    if not self._admit():
                        raise _Stop() from None
                    self.status.verify["gave_way"] = False
                    self._resolve()
            self.record.state, self.record.ended = VerificationState.COMPLETED, utc_now()
            if self._save_end(finished=True):
                self._keep_list()
        except _Stop as stop:
            self._end(VerificationState.STOPPED, stop.detail())
        except KeyboardInterrupt:
            self._end(VerificationState.INTERRUPTED, "interrupted at the console")
            raise
        except VerifyError as exc:
            if self.record is None:
                raise
            diagnostics.note(f"verification {self.verification_id}: {exc}")
            self._end(VerificationState.FAILED, str(exc))
        except Exception as exc:  # a verification that fails unexpectedly keeps its progress and says why
            diagnostics.note_failure(f"verification {self.verification_id} failed unexpectedly")
            if self.record is None:
                raise
            self._end(VerificationState.FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            self._close_remote()
            self.pump.stop()
            self.hold.release()
            self.board.close()
        if self.record is not None:
            self.say(self.record.describe())
        return self.record

    def _how(self) -> str:
        """How a remote destination's copies are compared, as the start of a verification
        says it; nothing for a drive's."""
        if self.destination is None or self.destination.local_folder is not None:
            return ""
        where = f" ({self.destination.describe()})"
        if self.measure is Measure.FINGERPRINT:
            return f"{where} by the {self.kind} fingerprints its service keeps; nothing is downloaded"
        if self.measure is Measure.ENCRYPTED:
            kind = f"{self.kind} fingerprints" if self.kind else "fingerprints"
            return (f"{where} by its encrypted form against the {kind} the service beneath keeps; only each file's "
                    f"first bytes are read there")
        return f"{where}: its service keeps no fingerprint, so both copies of every file are read, the destination's " \
               f"by download"

    def _end(self, state: VerificationState, detail: str) -> None:
        if self.record is None:
            return
        self.record.state, self.record.detail = state, detail
        self._save_end()


def verify_destination(store: ConfigStore, job_id: str, index: Optional[int] = None, *, volume_source: VolumeSource,
                       marker: Optional[str] = None, restart: bool = False,
                       mode: VerificationMode = VerificationMode.FULL,
                       say: Optional[Say] = None, chunk_bytes: int = CHUNK_BYTES,
                       pace: Optional[Callable[[], None]] = None,
                       after_item: Optional[Callable[[str], None]] = None,
                       side_patience: float = SIDE_PATIENCE,
                       stop_patience: float = STOP_PATIENCE,
                       power: Optional[System] = None) -> Optional[VerificationRecord]:
    """Verify destination ``index`` (1-based) of a job — or the one whose marker is
    ``marker``, found in the job as this verification reads it — a pass of ``mode``: continue its
    unfinished pass of that kind, or with ``restart`` begin a new one. A new pass of new
    copies where no list of verified copies is kept is a full pass, and continues the
    destination's unfinished full pass unless ``restart``. Returns the pass's record, or
    None when stopped before it began. Raises VerifyError when the verification cannot
    start, StoreBusy when another program kept the job file.

    ``pace`` is called after every chunk read and ``after_item`` after every entry
    handled; ``side_patience`` and ``stop_patience`` stand in for ``SIDE_PATIENCE`` and
    ``STOP_PATIENCE`` (tests); ``power``: what keeps the computer awake and names the
    verification at shutdown while it works, Windows' own unless given."""
    layout = runtime_layout(store.path)
    contents = _load(store)
    job = contents.job(job_id)
    if job is None:
        raise VerifyError(f"no job {job_id}")
    try:
        index = job.destination_number(index, marker)
    except ValueError as exc:
        raise VerifyError(str(exc)) from None
    marker_id = job.destinations[index - 1].marker_id
    if _live_verification(layout, marker_id):
        raise VerifyError("this destination is being verified already")
    records = read_verifications(layout, job_id)
    live = {r.run_id for r in observe_runs(layout) if r.alive}
    begun = mode
    if mode is VerificationMode.NEW_COPIES and (restart or resumable(records, marker_id, live, mode) is None) \
            and verified.kept(layout, records, marker_id) is None:
        begun = VerificationMode.FULL
    previous = None if restart else resumable(records, marker_id, live, begun)
    verification_id = previous.verification_id if previous is not None else uuid.uuid4().hex
    verifier = _Verifier(store, job, index, volume_source, say or (lambda text: None), verification_id, chunk_bytes,
                         pace, after_item, side_patience, stop_patience, power,
                         previous.mode if previous is not None else begun, mode)
    return verifier.execute(restart)
