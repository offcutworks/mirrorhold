"""What a scheduled task runs: one job's automatic run, unattended.

``run_scheduled`` is a schedule's: every destination, an absent one recorded as waiting
for its drive. ``run_on_arrival`` is an arrival's: it waits until the drives settle,
works out what arrived for this job (``arrival``) and runs only that, then looks again
and runs whatever arrived meanwhile — a second start of the task while it runs is
ignored by Task Scheduler, so this loop is how a drive attached during a long run is
still served. One arrival handler at a time per job holds the job's lock.
``run_at_logon`` is a logon's, for a job that runs on a schedule alone: a run a shutdown or
a crash cut short, whether it worked or waited for its turn, was started, so it is no
occurrence Task Scheduler missed, and without this it would wait for the schedule's next
occurrence, a month away for a monthly one. A logon runs what that occurrence would run,
once, and only when the job's newest record of some destination says so
(``records.record.cut_short``): nothing after an ordinary start of the computer, and nothing
for a run the user had paused or was stopping.

All three check the job first: a task left behind by an earlier configuration (the job
removed, its automatic runs switched off, or every destination of it switched off) runs
nothing, and a run never serves a destination switched off. All raise each run's
notice, with the destinations it backed up whose full verification is due; a run that
cannot start at all says why in a notice and in the job's ``automation/<job id>.log``
beside the store. A notice that asks something of the user also leaves the job's reminder
(``reminders``), which keeps the notification-area icon until the user opens the window.
Why Windows refused a notice, either kind, is kept in the result's ``problems``: the
process has no console, so the command line writes each to its diagnostic log, and the
run's records stay as they are.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from ..endpoints.cloud import SyncRoot
from ..endpoints.volumes import VolumeSource
from ..jobs.model import Job, Preferences
from ..jobs.store import ConfigStore, LoadStatus, StoreError
from ..locations import RuntimeLayout, runtime_layout
from ..notifications import announce_run, failure_notice, show
from ..notifications.toast import Notifier
from ..records import diagnostics
from ..records.admission import settle_interrupted
from ..records.lock import FileLock, remove_quietly
from ..records.record import RunRecord, cut_short, read_records
from ..records.status import is_run, observe_runs
from ..reminders import reminding
from ..run.runner import JobRunResult, RunError, RunOptions, run_job
from .arrival import (PresenceState, arrival_selection, arrivals, present_keys, read_state, settled_volumes,
                      system_boot_time, write_state)
from .tasks import ARRIVAL, SCHEDULE

MAX_ROUNDS = 5
"""Arrival rounds one start serves: a drive attached during a run is served by the next."""

Runner = Callable[..., JobRunResult]


@dataclass
class AutomaticResult:
    runs: List[JobRunResult] = field(default_factory=list)
    nothing_because: str = ""          # why nothing ran, when nothing did
    problems: List[str] = field(default_factory=list)    # why each notice Windows refused was not shown
    failure: str = ""                  # why it could not start, when it could not

    @property
    def records(self) -> List[RunRecord]:
        return [r for run in self.runs for r in run.records]


def _job(store: ConfigStore, job_id: str, kind: str):
    """(job, preferences, why not) from one load of the store."""
    loaded = store.load()
    if loaded.status in (LoadStatus.CORRUPT, LoadStatus.UNSUPPORTED):
        raise RunError(loaded.detail)
    job = loaded.contents.job(job_id)
    preferences = loaded.contents.preferences
    if job is None:
        return None, preferences, "the job no longer exists"
    return job, preferences, job.not_automatic(kind)


def _log_failure(store: ConfigStore, job_id: str, text: str) -> None:
    path = runtime_layout(store.path).automation_dir / f"{job_id}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(f"{stamp}\n{text}\n", encoding="utf-8")
    except OSError:
        pass


def _clear_failure(store: ConfigStore, job_id: str) -> None:
    remove_quietly(runtime_layout(store.path).automation_dir / f"{job_id}.log")


def _run(result: AutomaticResult, runner: Runner, store: ConfigStore, job: Job, preferences: Preferences,
         options: RunOptions, notifier: Optional[Notifier], volume_source: VolumeSource,
         sync_roots: Sequence[SyncRoot]) -> None:
    run = runner(store, job.job_id, options, volume_source=volume_source, sync_roots=sync_roots)
    result.runs.append(run)
    problem = announce_run(runtime_layout(store.path), job, run.records, preferences.notifications, notifier,
                           remind=reminding(store.path, job.job_id))
    if problem:
        result.problems.append(problem)


def _guarded(store: ConfigStore, job_id: str, notifier: Optional[Notifier], body: Callable[[], AutomaticResult]) \
        -> AutomaticResult:
    try:
        result = body()
    except Exception as exc:  # an automatic start has no one watching: it must say why it failed
        name, preferences = job_id, Preferences()
        try:
            loaded = store.load().contents
            job = loaded.job(job_id)
            name, preferences = (job.name if job else job_id), loaded.preferences
        except Exception:
            pass
        reason = str(exc) if isinstance(exc, (RunError, StoreError)) else f"{type(exc).__name__}: {exc}"
        _log_failure(store, job_id, traceback.format_exc())
        diagnostics.note_failure(f"the automatic run of job {job_id} could not start")
        problem = show(failure_notice(name, reason, preferences.notifications), notifier,
                       remind=reminding(store.path, job_id))
        return AutomaticResult(nothing_because=f"it could not start: {reason}", problems=[problem] if problem else [],
                               failure=reason)
    _clear_failure(store, job_id)
    return result


def run_scheduled(store: ConfigStore, job_id: str, *, volume_source: VolumeSource, sync_roots: Sequence[SyncRoot],
                  notifier: Optional[Notifier] = None, runner: Runner = run_job) -> AutomaticResult:
    def body() -> AutomaticResult:
        result = AutomaticResult()
        job, preferences, why_not = _job(store, job_id, SCHEDULE)
        if why_not:
            result.nothing_because = why_not
            return result
        _run(result, runner, store, job, preferences, RunOptions(unattended=True, automatic=SCHEDULE), notifier,
             volume_source, sync_roots)
        return result

    return _guarded(store, job_id, notifier, body)


def _cut_short(layout: RuntimeLayout, job: Job) -> bool:
    """Whether a run of ``job`` was cut short and nothing has come since: no run of it is in
    progress, and the newest record of one of its destinations says so."""
    if any(r.alive and is_run(r.status) and r.status.get("job_id") == job.job_id for r in observe_runs(layout)):
        return False
    newest = {record.marker_id: record for record in read_records(layout, job.job_id)}
    return any(cut_short(newest[d.marker_id]) for d in job.destinations if d.marker_id in newest)


def run_at_logon(store: ConfigStore, job_id: str, *, volume_source: VolumeSource, sync_roots: Sequence[SyncRoot],
                 notifier: Optional[Notifier] = None, runner: Runner = run_job) -> AutomaticResult:
    layout = runtime_layout(store.path)

    def body() -> AutomaticResult:
        result = AutomaticResult()
        job, preferences, why_not = _job(store, job_id, SCHEDULE)
        if why_not:
            result.nothing_because = why_not
            return result
        settle_interrupted(layout)
        if not _cut_short(layout, job):
            result.nothing_because = "no run of the job was cut short"
            return result
        _run(result, runner, store, job, preferences, RunOptions(unattended=True, automatic=SCHEDULE), notifier,
             volume_source, sync_roots)
        return result

    return _guarded(store, job_id, notifier, body)


def run_on_arrival(store: ConfigStore, job_id: str, *, volume_source: VolumeSource,
                   sync_roots: Sequence[SyncRoot], notifier: Optional[Notifier] = None, runner: Runner = run_job,
                   boot_time: Callable[[], datetime] = system_boot_time,
                   settle: Callable[[VolumeSource], Sequence] = settled_volumes,
                   sleep: Callable[[float], None] = time.sleep) -> AutomaticResult:
    layout = runtime_layout(store.path)

    def body() -> AutomaticResult:
        result = AutomaticResult()
        lock = FileLock(layout.automation_dir / f"{job_id}.lock")
        while not lock.try_acquire():
            sleep(1.0)
        try:
            boot = boot_time()
            volumes = settle(volume_source)
            for _round in range(MAX_ROUNDS):
                job, preferences, why_not = _job(store, job_id, ARRIVAL)
                if job is None:
                    result.nothing_because = why_not
                    return result
                present = present_keys(job, volumes)
                arrived = arrivals(read_state(layout, job_id), boot, present)
                write_state(layout, job_id, PresenceState(boot, present))
                selection = None if why_not else arrival_selection(job, present, arrived)
                if selection is None:
                    if not result.runs:
                        result.nothing_because = why_not or "no drive of this job arrived"
                    return result
                options = RunOptions(destinations=selection, unattended=True, automatic=ARRIVAL)
                _run(result, runner, store, job, preferences, options, notifier, volume_source, sync_roots)
                volumes = tuple(volume_source.scan().volumes)
            return result
        finally:
            lock.release()

    return _guarded(store, job_id, notifier, body)
