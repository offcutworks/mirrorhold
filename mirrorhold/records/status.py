"""Live status of runs in progress, and the channel through which callers control them.

Every run, whoever started it, keeps three files in ``<layout>/status/`` named by its
run identifier:

- ``<run>.lock`` — its liveness lock, held for the run's whole life and released by the
  operating system when the process ends, so a reader tells a live run from a dead one
  without trusting a process number;
- ``<run>.json`` — its status, rewritten atomically about twice a second: job, phase,
  the destinations it serves (none named: every one, as an older copy publishes) and,
  for a run, the marker of each destination by the number it gives it — the job's order
  when the run began, which an edit of the job since may have changed, so an observer
  finds a destination by its marker, never by its number — the pair in progress, how
  many entries the listing in progress has seen, how many of the changed files the step
  in progress has examined (their signatures read while planning, each opened before
  the copy), the plan's totals and what is done against them (files found identical
  included), the files in flight and the files the engine is comparing, rate and time left, and whether it is paused or cancelling; and what
  admission needs (``records.admission``): the folders it reads and writes and the
  drives they lie on, whether it holds them, what it waits for and its place in line,
  and what its gate let it write on its destination's drive;
- ``<run>.control`` — the state its callers want: paused or not, and whether to cancel
  (gracefully or at once). A caller writes the wanted state; the run reconciles to it.
  Wanted state rather than a queue of commands, so two callers compose and a repeated
  request changes nothing. A request reads, changes and writes it while holding
  ``<run>.request``, a lock of requests alone, there only while one holds it and named so
  that no look takes it for a run's liveness lock: two callers at one instant then each
  keep the other's change, and since only requests write the control file, a request
  never meets it being renamed over.

When a run ends it writes its records and then removes all three. It holds its requests'
turn from before its status goes until its liveness lock does, and a request that
holds the turn writes only while that lock is held: a request made as the run ends either
lands before the control file is removed or finds the run ended and writes nothing, so no
control file outlives its run. A status file whose
lock is free belongs to a run that died; ``reap_interrupted`` turns it into an
interrupted record for the pair it was working on. The lock is taken before the first
status is written and given up after the last is removed, and a look in the folder can
meet a status at the instant it is renamed over and find no file (measured on the
development machine: about one look in a hundred thousand while another process
replaced its status as fast as it could). So a run is found by its lock as well as by
its status: a held lock whose status is not there is looked at again for a moment, and
is then a live run whose status cannot be read — never a run that ended. An observer
that looks again and again (the window, a waiting activity) names such a run by the last
status it read of it (``LastRead``), and by its identifier alone where it read none.

Every one of these files is written and read through ``mirrorhold.exchange``, so a reader
never stands in the publisher's way, nor a publisher in a reader's. A file another
program holds is borne for the exchange's patience; past it, a status that cannot be
read while its lock is held is observed as alive and unreadable — its claim unknown,
so admission takes it to conflict with everything — never as a run that ended; a
control file that cannot be read, or that is not there for the moment a request renames
a new one over it, leaves what its run wants as it was; and a request
that cannot be written is reported to whoever made it.

A full verification (``maintenance.verifier``) publishes through the same channel, with
``kind`` "verification" and its progress under ``verify``, so every caller watches and
controls it as it does a run. A verification that died leaves no run record: its own
record keeps its progress, and the next verification of that destination continues it.
A repair (``maintenance.repairer``) publishes with ``kind`` "repair", the copies it
replaces counted as a run counts its copies; one that died leaves no run record either,
its own record reading as interrupted.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..locations import RuntimeLayout
from .. import exchange
from .lock import FileLock, is_held_elsewhere
from .record import DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, Outcome, RunRecord, json_text, utc_now, write_record

STATUS_FORMAT = "mirrorhold-run-status"

KIND_RUN = "run"
KIND_VERIFICATION = "verification"
KIND_REPAIR = "repair"


class Phase(str, Enum):
    WAITING = "waiting"            # for its turn: an activity it conflicts with works or goes first
    SNAPSHOT = "taking a snapshot"  # of the source's volume, before the source is listed
    CLEANING = "removing old versions"  # the job's version cleanup, before the destination is listed
    LISTING = "listing"
    VERIFYING = "verifying"        # a full verification comparing contents
    PLANNING = "planning"
    RENAMING = "renaming"
    MOVING = "moving into versions"
    CHECKING = "checking changed files"   # each opened the engine's way before the copy
    COPYING = "copying"
    FINISHING = "finishing"
    RELEASING = "removing the snapshot"   # after the last pair, whatever its outcome


class CancelMode(str, Enum):
    GRACEFUL = "graceful"          # start nothing new; let the files in flight finish
    IMMEDIATE = "immediate"        # stop the copies in flight at once


@dataclass(frozen=True)
class Wanted:
    """What callers want of a run."""

    paused: bool = False
    cancel: Optional[CancelMode] = None

    def to_document(self) -> dict:
        return {"paused": self.paused, "cancel": self.cancel.value if self.cancel else None}

    @classmethod
    def from_document(cls, doc: Any) -> "Wanted":
        if not isinstance(doc, dict):
            return cls()
        cancel = doc.get("cancel")
        try:
            mode = CancelMode(cancel) if cancel else None
        except ValueError:
            mode = None
        return cls(bool(doc.get("paused")), mode)


def queue_time() -> str:
    """Now in UTC to the microsecond: when an activity joined the line for its turn."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class LiveStatus:
    """The status a run publishes. Mutable: the run updates it in place."""

    run_id: str
    pid: int
    job_id: str
    job_name: str
    trigger: str
    started: str
    phase: Phase = Phase.WAITING
    paused: bool = False
    cancelling: Optional[CancelMode] = None
    pair_index: int = 0
    pair_total: int = 0
    marker_id: Optional[str] = None
    destination: str = ""
    pair_started: Optional[str] = None
    engine_version: Optional[str] = None
    engine_pid: Optional[int] = None
    listed: int = 0                                        # entries the listing in progress has seen
    examined: int = 0                                      # changed files the step in progress has examined
    to_examine: int = 0                                    # of these; 0 outside such a step
    planned: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(PLANNED_KEYS, 0))
    done: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(DONE_KEYS, 0))
    in_flight: List[List] = field(default_factory=list)   # [name, size, bytes]
    comparing: List[str] = field(default_factory=list)    # files the engine is comparing now
    rate: float = 0.0                                      # bytes per second, copied or found identical
    eta_seconds: Optional[float] = None
    finished: List[List] = field(default_factory=list)    # [destination index, outcome]
    selected: List[int] = field(default_factory=list)      # a run's destination indexes; empty: every one
    markers: List[str] = field(default_factory=list)       # a run's destinations' markers, index 1 first
    kind: str = KIND_RUN                                   # KIND_RUN, KIND_VERIFICATION or KIND_REPAIR
    verify: Optional[Dict[str, Any]] = None                # a verification's progress (maintenance.verifier)
    queued: str = field(default_factory=queue_time)        # its place in line for its turn
    claim: Optional[Dict[str, Any]] = None                 # the folders it reads and writes (records.admission)
    admitted: bool = False                                 # it holds its claim: working, or paused
    waiting_for: Optional[Dict[str, Any]] = None           # while it waits: the activity it waits for
    place: int = 0                                         # while it waits: its place in line, 1 the next to go
    writing: Optional[Dict[str, Any]] = None               # the pair's drive and what its gate let it copy

    def to_document(self) -> dict:
        return {"format": STATUS_FORMAT, "run_id": self.run_id, "pid": self.pid, "job_id": self.job_id,
                "job_name": self.job_name, "trigger": self.trigger, "started": self.started,
                "phase": self.phase.value, "paused": self.paused,
                "cancelling": self.cancelling.value if self.cancelling else None,
                "pair_index": self.pair_index, "pair_total": self.pair_total, "marker_id": self.marker_id,
                "destination": self.destination, "pair_started": self.pair_started,
                "engine_version": self.engine_version, "engine_pid": self.engine_pid, "listed": self.listed,
                "examined": self.examined, "to_examine": self.to_examine,
                "planned": dict(self.planned), "done": dict(self.done), "in_flight": [list(f) for f in self.in_flight],
                "comparing": list(self.comparing),
                "rate": self.rate, "eta_seconds": self.eta_seconds, "finished": [list(f) for f in self.finished],
                "selected": list(self.selected), "markers": list(self.markers), "kind": self.kind,
                "verify": dict(self.verify) if self.verify is not None else None,
                "queued": self.queued, "claim": self.claim, "admitted": self.admitted,
                "waiting_for": dict(self.waiting_for) if self.waiting_for is not None else None,
                "place": self.place,
                "writing": dict(self.writing) if self.writing is not None else None,
                "updated": utc_now()}


def is_verification(status: dict) -> bool:
    """Whether a status document is a full verification's rather than a run's."""
    return status.get("kind") == KIND_VERIFICATION


def is_repair(status: dict) -> bool:
    """Whether a status document is a repair's (``maintenance.repairer``)."""
    return status.get("kind") == KIND_REPAIR


def is_run(status: dict) -> bool:
    """Whether a status document is a run's: one that names no kind, as an older copy's
    may, is a run."""
    return status.get("kind", KIND_RUN) == KIND_RUN


def _paths(layout: RuntimeLayout, run_id: str) -> Dict[str, Path]:
    folder = layout.status_dir
    return {"status": folder / f"{run_id}.json", "lock": folder / f"{run_id}.lock",
            "control": folder / f"{run_id}.control", "request": folder / f"{run_id}.request",
            "files": folder / f"{run_id}.files"}


def engine_list_path(layout: RuntimeLayout, run_id: str) -> Path:
    """Where a run's engine file list lives: beside its status, among the private files."""
    return _paths(layout, run_id)["files"]


def _write_atomically(path: Path, doc: dict, *, patience: float, staging: Optional[Path] = None) -> None:
    exchange.write_text(path, json_text(doc), patience=patience, staging=staging)


def _read_json(path: Path, *, patience: float = exchange.READ_PATIENCE) -> Optional[dict]:
    """The document, or None when the file is not there or holds no document. FileBusy
    when it stayed refused for ``patience`` seconds."""
    try:
        doc = json.loads(exchange.read_text(path, patience=patience))
    except FileNotFoundError:
        return None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


CONTROL_PATIENCE = 0.5
"""Seconds a run's status pump waits for its control file before it keeps what its run
was told last; the pump looks again twice a second."""

REQUEST_PATIENCE = 5.0
"""Seconds a request from the window or the command line waits for its turn and for the
run's control file before it is reported as not written."""

REQUEST_TURN_POLL = 0.02
"""Seconds between two tries for a request's turn another request holds."""

CLOSE_PATIENCE = 5.0
"""Seconds an ending run waits for a reader to let go of its status file: a status left
behind with its lock free would read as a run that died, and be recorded as interrupted."""

CLOSE_TURN_PATIENCE = 2 * REQUEST_PATIENCE + 1.0
"""Seconds an ending run waits for a request at work to let go of its requests' turn: a
request holds it for a read and a write of the control file, each borne for its patience."""


class StatusBoard:
    """The publishing side, owned by one run.

    Its process publishes from two threads (the run and its status pump), one at a time,
    so a document taken earlier never lands over a later one. ``last_read`` is what its
    attempts at its turn last read of the other activities (``records.admission``)."""

    def __init__(self, layout: RuntimeLayout, status: LiveStatus):
        self.layout = layout
        self.status = status
        self._paths = _paths(layout, status.run_id)
        self._lock = FileLock(self._paths["lock"])
        self._publishing = threading.Lock()
        self.last_read = LastRead()

    def open(self) -> None:
        if not self._lock.try_acquire():
            raise RuntimeError(f"run {self.status.run_id} is already live")
        self.publish(patience=exchange.WRITE_PATIENCE)

    def publish(self, *, patience: float = 0.0) -> None:
        """Write the status now. A file another program holds is tried again for
        ``patience`` seconds (none: one try, the status pump's next tick being the next);
        FileBusy then reaches the caller."""
        with self._publishing:
            _write_atomically(self._paths["status"], self.status.to_document(), patience=patience)

    def wanted(self) -> Optional[Wanted]:
        """What callers want of this run; None when its control file cannot be read now,
        so that what was wanted last stands. A control file that is not there is one: once
        the first request writes it, it stays until the run ends, so a look that finds none
        later met the moment a request renames a new one over it (``exchange``); before the
        first request, nothing was wanted last."""
        try:
            doc = json.loads(exchange.read_text(self._paths["control"], patience=CONTROL_PATIENCE))
        except OSError:                    # FileBusy, or not there
            return None
        except ValueError:                 # not a document: nothing wanted
            doc = None
        return Wanted.from_document(doc)

    def close(self) -> None:
        """Remove the run's files, its status first and its liveness lock last. The
        requests' turn is taken before any of them goes and held until the lock has gone,
        so a request either lands before the control file is removed or finds the run
        ended (``request``); while the run waits for a request at work its status stays,
        so no look meets a held lock without a status, which would read as a live run whose
        status cannot be read. A request that keeps the turn past its patience is not waited
        for further: the run ends, and that request alone may leave its control file."""
        turn = FileLock(self._paths["request"])
        deadline = time.monotonic() + CLOSE_TURN_PATIENCE
        while not turn.try_acquire() and time.monotonic() < deadline:
            time.sleep(REQUEST_TURN_POLL)
        for name in ("status", "control", "files"):
            exchange.remove(self._paths[name], patience=CLOSE_PATIENCE)
        for left in self.layout.status_dir.glob(f"{self.status.run_id}.*.writing"):
            exchange.remove(left, patience=0.0)
        self._lock.release(remove=True)
        turn.release(remove=True)


@dataclass(frozen=True)
class ObservedRun:
    status: dict
    alive: bool
    readable: bool = True       # False: alive, its status refused for the exchange's patience
    last: Optional[dict] = None  # one not readable: the last status its observer read of it (``LastRead``)

    @property
    def run_id(self) -> str:
        return self.status.get("run_id", "")


UNREADABLE = "its status cannot be read now"
"""What the window and the command line say of a live activity whose status cannot be read now."""

ANY_KIND = "run, verification or repair"
ANY_KINDS = "runs, verifications or repairs"
"""What the window and the command line call a live activity whose kind is not known: one
whose status cannot be read now and that its observer never read."""


def _unreadable(run_id: str) -> dict:
    """What is known of a live run whose status cannot be read now: its identifier. It
    names no claim, so admission takes it to conflict with every other activity."""
    return {"format": STATUS_FORMAT, "run_id": run_id}


class LastRead:
    """The last status one observer read of each activity it still finds, kept across its
    looks: what names an activity whose status it cannot read now, which its status alone
    cannot. Only what an activity is, fixed for its life, is taken from it — its kind, its
    job and the destinations it serves (the window's job list also keeps those a run has
    finished, which only grow); what the activity claims, whether it holds its turn and how
    far it has come are never taken from an older status. An observer that looks once — the
    command line's ``status`` — keeps none. Used by one thread at a time."""

    def __init__(self) -> None:
        self._read: Dict[str, dict] = {}

    def look(self, runs: List[ObservedRun]) -> List[ObservedRun]:
        """``runs`` with each one not readable carrying the last status read of it, if any;
        what is remembered of an activity no longer found is forgotten."""
        self._read = {r.run_id: (r.status if r.readable else self._read[r.run_id]) for r in runs
                      if r.readable or r.run_id in self._read}
        return [r if r.readable else ObservedRun(r.status, r.alive, False, self._read.get(r.run_id)) for r in runs]


MISSING_PATIENCE = 0.2
"""Seconds a status that is not there while its run's lock is held is looked for again:
the lock comes before the first status and goes after the last, and a status is renamed
over at every publication."""

MISSING_LOOK = 0.02
"""Seconds between two looks for such a status."""


def _observe(layout: RuntimeLayout, run_id: str) -> Optional[ObservedRun]:
    """The run ``run_id`` as its files say now; None when it has no status and its lock is
    not held (a run that ended, or one whose files are being reaped)."""
    paths = _paths(layout, run_id)
    deadline = None
    while True:
        try:
            doc = _read_json(paths["status"])
        except OSError:                    # FileBusy above all: not a run that ended
            if is_held_elsewhere(paths["lock"]) is True:
                return ObservedRun(_unreadable(run_id), True, False)
            return None
        if doc is not None:
            if doc.get("format") != STATUS_FORMAT:
                return None
            alive = is_held_elsewhere(_paths(layout, doc.get("run_id", ""))["lock"]) is True
            return ObservedRun(doc, alive)
        if is_held_elsewhere(paths["lock"]) is not True:
            return None
        if deadline is None:
            deadline = time.monotonic() + MISSING_PATIENCE
        elif time.monotonic() >= deadline:
            return ObservedRun(_unreadable(run_id), True, False)
        time.sleep(MISSING_LOOK)


def observe_runs(layout: RuntimeLayout, last_read: Optional[LastRead] = None) -> List[ObservedRun]:
    """Every run with a status file or a liveness lock: live ones, and dead ones not yet
    reaped. Oldest first.

    A status that stays refused while its lock is held is observed as alive and not
    ``readable``, and so is one that stays missing while its lock is held; while its lock
    is free it is left to a later look. With ``last_read``, the observer's memory across
    its looks, one not readable carries the last status that observer read of it."""
    folder = layout.status_dir
    if not folder.is_dir():
        return last_read.look([]) if last_read is not None else []
    run_ids = {path.stem for pattern in ("*.json", "*.lock") for path in folder.glob(pattern)}
    runs = [observed for observed in (_observe(layout, run_id) for run_id in sorted(run_ids))
            if observed is not None]
    runs = sorted(runs, key=lambda r: r.status.get("started", ""))
    return last_read.look(runs) if last_read is not None else runs


def place_of(status: Mapping[str, Any]) -> int:
    """A waiting activity's place in line as its status publishes it; 0 when it names none."""
    place = status.get("place")
    return place if isinstance(place, int) and not isinstance(place, bool) and place > 0 else 0


def line_order(observed: ObservedRun) -> Tuple[int, int, str, str]:
    """Where an activity stands in line, as a sort key: those at work or paused first, then
    the waiting ones by their place in line, one naming no place last; each in the order it
    joined the line (to the microsecond; an older copy's status says only when it started,
    to the second, read as that second's start so that it compares with the microseconds).
    The activities a group or several items start share their start's second, so when they
    started says nothing of their order."""
    st = observed.status
    when = str(st.get("queued") or st.get("started") or "")
    if when.endswith("Z") and "." not in when:
        when = when[:-1] + ".000000Z"
    if st.get("phase") != Phase.WAITING.value:
        return 0, 0, when, observed.run_id
    return 1, place_of(st) or 1 << 30, when, observed.run_id


def is_live(layout: RuntimeLayout, run_id: str) -> bool:
    """Whether the run (or verification) ``run_id`` is in progress: its liveness lock is held."""
    return is_held_elsewhere(_paths(layout, run_id)["lock"]) is True


class NoSuchRun(Exception):
    pass


def find_run(layout: RuntimeLayout, key: Optional[str]) -> ObservedRun:
    """The live run whose identifier starts with ``key``, its status readable or not: a
    request reaches a run through its control file, whatever keeps its status from being
    read. With no key, the only live run whose status can be read that is not waiting, else
    the only such run; one whose status cannot be read is reached only by its identifier."""
    everyone = [r for r in observe_runs(layout) if r.alive]
    live = [r for r in everyone if r.readable]
    if key:
        found = [r for r in everyone if r.run_id.startswith(key)]
    else:
        working = [r for r in live if r.status.get("phase") != Phase.WAITING.value]
        found = working if len(working) == 1 else live
    if not found and not key and len(everyone) > len(live):
        raise NoSuchRun("no run in progress whose status can be read; the status of "
                        + ", ".join(r.run_id[:12] for r in everyone if not r.readable)
                        + " cannot be read now: name it to reach it")
    if not found:
        raise NoSuchRun("no run in progress" + (f" matches '{key}'" if key else ""))
    if len(found) > 1:
        raise NoSuchRun("more than one run is in progress; name one: " + ", ".join(r.run_id[:12] for r in found))
    return found[0]


def request(layout: RuntimeLayout, run_id: str, *, pause: Optional[bool] = None,
            cancel: Optional[CancelMode] = None, patience: float = REQUEST_PATIENCE) -> Wanted:
    """Change what is wanted of a run. A cancel is never downgraded: an immediate cancel
    stays immediate. Requests to one run take turns (``<run>.request``, removed as each lets
    it go), so what one wanted is never dropped by another made at the same instant; an
    ending run holds that turn while it removes its files (``StatusBoard.close``).
    FileBusy when the turn or the run's control file stayed held for ``patience`` seconds:
    the request did not land. NoSuchRun when the run has ended by the time the request has
    its turn: nothing is written."""
    paths = _paths(layout, run_id)
    path = paths["control"]
    turn = FileLock(paths["request"])
    deadline = time.monotonic() + patience
    while not turn.try_acquire():
        if time.monotonic() >= deadline:
            raise exchange.FileBusy(paths["request"], reading=False)
        time.sleep(REQUEST_TURN_POLL)
    try:
        if is_held_elsewhere(paths["lock"]) is not True:
            raise NoSuchRun(f"run {run_id[:12]} has ended")
        current = Wanted.from_document(_read_json(path, patience=patience))
        paused = current.paused if pause is None else pause
        mode = current.cancel
        if cancel is not None and mode is not CancelMode.IMMEDIATE:
            mode = cancel
        wanted = Wanted(paused, mode)
        _write_atomically(path, wanted.to_document(), patience=patience, staging=exchange.private_staging(path))
    finally:
        turn.release(remove=True)
    return wanted


def reap_interrupted(layout: RuntimeLayout) -> List[RunRecord]:
    """Record every dead run as interrupted (for the pair it was working on) and remove its
    status files; a dead verification's or repair's are removed with no run record. A live
    run's files are never touched. Called as an activity is admitted, under the admission
    lock (``records.admission``), so no dead run is recorded twice; and a dead run is
    recorded only once its status file is gone, so a status another program holds is left
    to a later admission rather than recorded at each."""
    reaped = []
    for observed in observe_runs(layout):
        if observed.alive or not observed.readable:
            continue
        doc = observed.status
        paths = _paths(layout, observed.run_id)
        if not exchange.remove(paths["status"]):
            continue
        if is_run(doc) and doc.get("pair_index") and doc.get("marker_id") and doc.get("pair_started"):
            planned = {k: int((doc.get("planned") or {}).get(k, 0)) for k in PLANNED_KEYS}
            done = {k: int((doc.get("done") or {}).get(k, 0)) for k in DONE_KEYS}
            left = dict.fromkeys(LEFT_KEYS, 0)
            left["files_to_copy"] = max(0, planned["files_to_copy"] - done["files_copied"] - done["files_identical"])
            left["bytes_to_copy"] = max(0, planned["bytes_to_copy"] - done["bytes_copied"] - done["bytes_identical"])
            left["removals"] = max(0, planned["deleted_files"] + planned["changed_files"] - done["moved_to_versions"]
                                   - done["files_identical"])
            left["renames"] = max(0, planned["renames"] - done["renamed"])
            record = RunRecord(observed.run_id, doc.get("job_id", ""), doc.get("job_name", ""), doc["marker_id"],
                               int(doc["pair_index"]), doc.get("trigger", ""), doc["pair_started"],
                               doc.get("updated") or utc_now(), Outcome.INTERRUPTED, ("process-ended",),
                               "the run's process ended before the run did (shutdown, unplugging or a crash); "
                               "the next run converges", doc.get("engine_version"), planned, done, left)
            write_record(layout, record)
            reaped.append(record)
        for name in ("control", "request", "files", "lock"):
            exchange.remove(paths[name], patience=0.0)
    return reaped
