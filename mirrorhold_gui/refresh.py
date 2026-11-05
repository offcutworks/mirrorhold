"""What the main window shows, observed off the window's thread. No toolkit.

Checking a drive can block — a sleeping USB disk spins up before it answers — so the
volume scan, the destinations' markers and the run records are read by ``Refresher``, a
background thread, every few seconds, and handed to the window as an ``Observation``.
The window only ever reads the latest one.

To leave backup drives alone, a marker is read again only when its destination has just
become present, when asked (``refresh_now(markers=True)``, after a run ended or the jobs
changed), when its job's run records changed — a run serving several destinations
records each as it finishes it, a first backup having written its destination's marker
while the run goes on to the next — or once a minute; run, verification and repair
records are read again when their folder changed, or when the last read passed over a
file another program held, so a record lands in the window as soon as it can be read.
A destination without a marker has its folder's top level looked at whenever its marker
is read (``storage.DestinationStorage.occupancy``), so the window knows whether its first
run waits only for the go-ahead of a first backup. Both are read through the destination's
storage (``storage``), a drive's here. The activities' logs are followed too
(``records.diagnostics.LogWatch``), so a start that left no record shows in the window.

A look that fails is not the end of looking: the window keeps what it saw last, the
observation says why the look failed (``failed``), and the next look tries again.

The runs' status files — every run, verification and repair in progress — are read by
``RunWatcher``, a thread of its own, every half second: a status another program holds
is waited for there, for the exchange's patience, and the window never stops answering
for it.

The two looks meet in the job list. As the window sees an activity end it asks the
refresher to read the markers again; until a look begun after that request succeeds
(``Observation.answered``), the list shows the ended activity as it was last seen at
work (``Settling``), not the destinations as they were before it began — a job whose
first backup has just been made would otherwise offer its first backup again. A look
that fails reads nothing, so it settles nothing; the status bar says since when.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from mirrorhold.endpoints.marker import MarkerState, check_marker
from mirrorhold.endpoints.resolution import Presence, resolve
from mirrorhold.endpoints.volumes import VolumeInfo, VolumeSource
from mirrorhold.jobs.exclusions import EntryFilter
from mirrorhold.jobs.store import StoreContents
from mirrorhold.locations import RuntimeLayout
from mirrorhold.maintenance.repair import RepairRecord, read_repairs
from mirrorhold.maintenance.verification import VerificationRecord, read_verifications
from mirrorhold.planning.listing import Occupancy
from mirrorhold.records.diagnostics import ActivityLog, LogWatch
from mirrorhold.records.record import RunRecord, read_records
from mirrorhold.records.status import LastRead, ObservedRun, observe_runs
from mirrorhold.storage.drive import DriveStorage

MARKER_MAX_AGE = 60.0
"""Seconds after which a present destination's marker is read again regardless."""

RecordStamp = Optional[Tuple[int, int]]


@dataclass(frozen=True)
class Observation:
    volumes: Tuple[VolumeInfo, ...] = ()
    unavailable: Tuple[Tuple[str, str], ...] = ()
    markers: Dict[Tuple[str, str], MarkerState] = field(default_factory=dict)
    records: Dict[str, Tuple[RunRecord, ...]] = field(default_factory=dict)
    taken: float = 0.0                       # time.time() of the observation
    error: str = ""                          # why the drives could not be checked, if they could not
    marker_times: Dict[Tuple[str, str], float] = field(default_factory=dict)
    record_stamps: Dict[str, RecordStamp] = field(default_factory=dict)
    verifications: Dict[str, Tuple[VerificationRecord, ...]] = field(default_factory=dict)
    verification_stamps: Dict[str, RecordStamp] = field(default_factory=dict)
    occupancy: Dict[Tuple[str, str], Occupancy] = field(default_factory=dict)   # present destinations without a marker
    logs: Tuple[ActivityLog, ...] = ()       # the activities' logs, read back, oldest first
    failed: str = ""                         # why the latest look failed; what is here is the look before
    repairs: Dict[str, Tuple[RepairRecord, ...]] = field(default_factory=dict)
    repair_stamps: Dict[str, RecordStamp] = field(default_factory=dict)
    answered: int = 0                        # the latest ``Refresher.refresh_now`` a successful look began after
    # Each job's run-records folder as measured, None while it does not exist. Unlike ``record_stamps``,
    # a record another program held does not clear it, so such a record does not have the job's
    # markers read again at every look.
    record_folders: Dict[str, RecordStamp] = field(default_factory=dict)


def _folder_stamp(folder) -> RecordStamp:
    try:
        info = folder.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _record_stamp(layout: RuntimeLayout, job_id: str) -> RecordStamp:
    return _folder_stamp(layout.records_dir / job_id)


def observe(contents: StoreContents, layout: RuntimeLayout, volume_source: VolumeSource,
            previous: Optional[Observation] = None, *, reread_markers: bool = False,
            watch: Optional[LogWatch] = None) -> Observation:
    """One observation; ``previous`` lets unchanged markers and records be reused;
    ``watch`` follows the logs, which are left out without one."""
    previous = previous or Observation()
    now = time.time()
    try:
        scan = volume_source.scan()
        volumes, unavailable, error = scan.volumes, scan.unavailable, ""
    except Exception as exc:  # a scan that fails is shown, and the last known volumes kept
        volumes, unavailable, error = previous.volumes, previous.unavailable, f"{type(exc).__name__}: {exc}"
    previously_present = {v.identity for v in previous.volumes}
    markers: Dict[Tuple[str, str], MarkerState] = {}
    marker_times: Dict[Tuple[str, str], float] = {}
    held: Dict[Tuple[str, str], Occupancy] = {}
    records: Dict[str, Tuple[RunRecord, ...]] = {}
    stamps: Dict[str, RecordStamp] = {}
    checks: Dict[str, Tuple[VerificationRecord, ...]] = {}
    check_stamps: Dict[str, RecordStamp] = {}
    repairs: Dict[str, Tuple[RepairRecord, ...]] = {}
    repair_stamps: Dict[str, RecordStamp] = {}
    folders: Dict[str, RecordStamp] = {}
    for job in contents.jobs:
        stamp = folders[job.job_id] = _record_stamp(layout, job.job_id)
        recorded = job.job_id in previous.record_folders and previous.record_folders[job.job_id] != stamp
        for destination in job.destinations:
            resolution = resolve(destination.endpoint, volumes)
            if resolution.presence is not Presence.PRESENT:
                continue
            key = (job.job_id, destination.marker_id)
            fresh = (not reread_markers and not recorded and key in previous.markers
                     and destination.endpoint.volume in previously_present
                     and now - previous.marker_times.get(key, 0.0) < MARKER_MAX_AGE)
            if fresh:
                markers[key], marker_times[key] = previous.markers[key], previous.marker_times[key]
                if key in previous.occupancy:
                    held[key] = previous.occupancy[key]
            else:
                storage = DriveStorage(resolution.location, resolution.volume)
                markers[key], marker_times[key] = check_marker(storage, job.job_id, destination.marker_id), now
                if markers[key] is MarkerState.MISSING:
                    held[key] = storage.occupancy(drive_root=destination.endpoint.is_drive_root,
                                                  entry_filter=EntryFilter.for_source(job.source))
        if stamp is not None and previous.record_stamps.get(job.job_id) == stamp and job.job_id in previous.records:
            records[job.job_id] = previous.records[job.job_id]
        else:
            busy: List[Path] = []
            records[job.job_id] = tuple(read_records(layout, job.job_id, busy=busy))
            stamp = None if busy else stamp            # one held past the patience: read the folder again
        stamps[job.job_id] = stamp
        stamp = _folder_stamp(layout.verifications_dir / job.job_id)
        if stamp is not None and previous.verification_stamps.get(job.job_id) == stamp \
                and job.job_id in previous.verifications:
            checks[job.job_id] = previous.verifications[job.job_id]
        else:
            busy = []
            checks[job.job_id] = tuple(read_verifications(layout, job.job_id, busy=busy))
            stamp = None if busy else stamp
        check_stamps[job.job_id] = stamp
        stamp = _folder_stamp(layout.repairs_dir / job.job_id)
        if stamp is not None and previous.repair_stamps.get(job.job_id) == stamp and job.job_id in previous.repairs:
            repairs[job.job_id] = previous.repairs[job.job_id]
        else:
            busy = []
            repairs[job.job_id] = tuple(read_repairs(layout, job.job_id, busy=busy))
            stamp = None if busy else stamp
        repair_stamps[job.job_id] = stamp
    logs = watch.look() if watch is not None else ()
    return Observation(tuple(volumes), tuple(unavailable), markers, records, now, error, marker_times, stamps, checks,
                       check_stamps, held, logs, repairs=repairs, repair_stamps=repair_stamps, record_folders=folders)


class Refresher:
    """Observes every ``interval`` seconds, and at once when asked, on a thread of its
    own; ``publish`` receives each observation on that thread."""

    def __init__(self, layout: RuntimeLayout, volume_source: VolumeSource, publish: Callable[[Observation], None],
                 *, interval: float = 4.0):
        self.layout = layout
        self.volume_source = volume_source
        self.publish = publish
        self.interval = interval
        self._contents = StoreContents()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._reread = False
        self._requests = 0
        self._thread: Optional[threading.Thread] = None
        self._watch = LogWatch(layout)
        self.latest: Optional[Observation] = None

    def set_contents(self, contents: StoreContents) -> None:
        with self._lock:
            self._contents = contents
        self.refresh_now(markers=True)

    def refresh_now(self, *, markers: bool = False) -> int:
        """Look again at once, reading every present destination's marker again when
        ``markers``. Returns the request's number, which the first look begun after it
        that succeeds answers (``Observation.answered``); a look that fails answers what
        the look before it did, having read nothing."""
        with self._lock:
            self._reread = self._reread or markers
            self._requests += 1
            request = self._requests
        self._wake.set()
        return request

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mirrorhold-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                contents, reread, self._reread = self._contents, self._reread, False
                answered = self._requests
                self._wake.clear()
            try:
                observation = replace(observe(contents, self.layout, self.volume_source, self.latest,
                                              reread_markers=reread, watch=self._watch), answered=answered)
                self.latest = observation
            except Exception as exc:  # the window keeps what it saw last and says why it sees nothing newer
                observation = replace(self.latest or Observation(), failed=f"{type(exc).__name__}: {exc}")
                with self._lock:
                    self._reread = self._reread or reread
            if not self._stop.is_set():
                self.publish(observation)
            self._wake.wait(self.interval)


@dataclass(frozen=True)
class RunsLook:
    """One reading of every run's status (``records.status.observe_runs``)."""
    runs: Tuple[ObservedRun, ...] = ()
    taken: float = 0.0                       # time.time() as the look began
    failed: str = ""                         # why the look failed; ``runs`` then holds nothing


class RunWatcher:
    """Reads every run's status every ``interval`` seconds, and at once when asked, on a
    thread of its own; ``publish`` receives each look on that thread. It keeps the last
    status it read of each run, which names one whose status it cannot read now
    (``records.status.LastRead``). ``observer`` reads them instead (tests)."""

    def __init__(self, layout: RuntimeLayout, publish: Callable[[RunsLook], None], *, interval: float = 0.5,
                 observer: Optional[Callable[[RuntimeLayout], Sequence[ObservedRun]]] = None):
        self.layout = layout
        self.publish = publish
        self.interval = interval
        last_read = LastRead()
        self._observer = observer or (lambda runs_layout: observe_runs(runs_layout, last_read))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def look_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mirrorhold-runs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            taken = time.time()
            try:
                look = RunsLook(tuple(self._observer(self.layout)), taken)
            except Exception as exc:  # the window keeps the runs it saw last and says why it sees nothing newer
                look = RunsLook((), taken, f"{type(exc).__name__}: {exc}")
            if not self._stop.is_set():
                self.publish(look)
            self._wake.wait(self.interval)


def ended_runs(before: Sequence[ObservedRun], now: Sequence[ObservedRun]) -> Tuple[ObservedRun, ...]:
    """The activities at work in ``before`` that no longer are in ``now``, as last seen at work."""
    alive = {r.run_id for r in now if r.alive}
    return tuple(r for r in before if r.alive and r.run_id not in alive)


class Settling:
    """The activities that ended before the refresher looked at what they left: each is
    held, as last seen at work, until a look answering the request made as it ended
    arrives. The window's thread alone uses it."""

    def __init__(self):
        self._held: Dict[str, Tuple[ObservedRun, int]] = {}

    def hold(self, ended: Iterable[ObservedRun], request: int) -> None:
        """Hold ``ended`` until a look answers ``request`` (``Refresher.refresh_now``)."""
        for run in ended:
            self._held[run.run_id] = (run, request)

    def settle(self, answered: int) -> None:
        """Let go of every activity a look that answered ``answered`` shows as it left things."""
        self._held = {run_id: held for run_id, held in self._held.items() if held[1] > answered}

    def listed(self, runs: Sequence[ObservedRun]) -> Tuple[ObservedRun, ...]:
        """The activities the job list shows: ``runs``, each held one as last seen at work."""
        return (tuple(r for r in runs if r.run_id not in self._held)
                + tuple(run for run, _request in self._held.values()))
