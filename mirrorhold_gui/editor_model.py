"""The job editor's state and its checks. No toolkit.

A ``JobDraft`` holds what the editor shows; ``check`` turns it into a job and runs the
core's own creation-time validation on it, so the GUI never re-implements a rule: the
editor shows the core's findings and saves only when nothing is refused.

What the editor offers is every setting of the job contract: the name, the source,
whether the source is read from a snapshot of its drive when possible, the destinations,
the two default exclusion categories, the user's own patterns, the holds (the count floor
and percentage, and the share hold, which the user may switch off for a source of a few
files, where one edit is already half), when the job runs by itself — when one of its
drives is attached, on a schedule (daily, on chosen weekdays or on a day of the month; at
times listed or every few hours, inside a window or not), and a switch that pauses both
while keeping them (the job's enabled switch, shown the other way round) — and its versions
and verification: keep every version, or
delete versions batches older than a number of days, and the interval of the reminder
to verify every destination fully. A new job runs only when the user starts it and
keeps every version. The snapshot choice is offered only for a drive that can be
snapshotted; for another it is shown, disabled, and kept as stored.

Deleting old versions is the only permanent deletion the tool performs, so a save that
switches it on or lowers its days asks first (``cleanup_question``).

Two rules come from what a job's destinations already hold: the source of a job that
has run is not editable (a new source would turn its next run into replacing every
destination's contents), and a destination is never moved in place (its marker belongs
to its folder) — it is removed and another one added.

A folder is typed or chosen, and a typed one is read as the command line's ``add`` reads
its paths (``endpoint_from_path``), so a wrong path is refused with the same words. The
source must exist, since it is what runs read: a source not there is a mistyped path. A
destination may not exist yet: its first backup makes it.

A destination may be a remote's folder instead (``add_remote_destination``). What the core
judges of one comes from the engine, which ``check`` is handed (``remote_setup``), never
asks, since it runs at every keystroke: the editor asks off its thread, and until the answer
comes the job cannot be saved (``asking``), so that a remote the core refuses is refused when
the job is made.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.cloud import SyncRoot
from mirrorhold.endpoints.endpoint import AnyEndpoint, Endpoint, RemoteEndpoint
from mirrorhold.endpoints.resolution import EndpointError, Presence, endpoint_from_path, resolve
from mirrorhold.endpoints.volumes import VolumeInfo
from mirrorhold.jobs.model import (DEFAULT_REMINDER_DAYS, DEFAULT_SHARE, DefaultExclusions, Destination,
                                   HoldThresholds, Job, Triggers, VersionCleanup, new_job)
from mirrorhold.jobs.schedule import (DAILY, EVERY_HOURS, LAST, MONTH_DAYS, MONTHLY, WEEKLY, Schedule, ScheduleError,
                                      Window, clock, parse_minute, parse_schedule, parse_times)
from mirrorhold.jobs.validation import REMOTE_UNREACHABLE, Severity, ValidationContext, validate_job
from mirrorhold.remotes.facts import RemoteSetup
from mirrorhold.wording import counted

from .formatting import sentence, size
from .jobs_model import describe_folder
from .remote_model import window_reason


REFUSED, WARNING, NOTE = "Refused", "Warning", "Note"
"""How a check reads: validation's three severities; an input problem reads as refused."""

DEFAULT_SCHEDULE_TIME = "21:00"
DEFAULT_KEEP_DAYS = 90
"""The days offered when version cleanup is switched on; the contract has no default,
cleanup being off until the user chooses."""
CLEANUP_NOTE = ("Deleting is permanent. It happens during this job's runs, only inside the versions folder of a "
                "destination whose marker matches.")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SCHEDULE_KINDS = {DAILY: "Daily", WEEKLY: "Weekly", MONTHLY: "Monthly"}
"""The days a schedule runs on, as the editor names them."""
LAST_DAY_CHOICE = "last day"
MONTH_DAY_CHOICES = (*(str(d) for d in MONTH_DAYS), LAST_DAY_CHOICE)
EVERY_CHOICES = tuple(str(n) for n in EVERY_HOURS)
DEFAULT_EVERY_HOURS = "2"
DEFAULT_WINDOW = ("08:00", "22:00")

SOURCE_PROMPT = "Type or choose the folder or drive to back up."
SOURCE_MISSING = "this folder does not exist"
NEW_FOLDER_NOTE = "A folder that does not exist yet is made by the first backup."
ASKING_REMOTE = "Asking the engine about the remote folders..."

SNAPSHOT_NOTE = ("Files are read from a moment's copy of the drive, so files that programs keep open are backed up "
                 "too. When a snapshot cannot be made, files are read as they are and the run's details say why.")
NO_SNAPSHOT_NOTE = "This drive cannot be snapshotted."

_NOT_LISTED = frozenset({"size-checks-deferred"})
"""Findings the editor leaves out of *Checks*: the editor never surveys the source, so
this one holds for every job and says nothing about the one being edited."""


@dataclass(frozen=True)
class Check:
    level: str          # REFUSED, WARNING or NOTE
    text: str
    code: str = ""


@dataclass(frozen=True)
class DraftDestination:
    endpoint: AnyEndpoint
    marker_id: Optional[str] = None      # None: added in this editor; a new marker identity on save


@dataclass(frozen=True)
class CheckResult:
    job: Optional[Job]
    checks: Tuple[Check, ...]
    asking: bool = False            # the engine has not answered yet

    @property
    def can_save(self) -> bool:
        return self.job is not None and not self.asking and not any(c.level == REFUSED for c in self.checks)


@dataclass
class JobDraft:
    name: str = ""
    source: Optional[Endpoint] = None
    source_editable: bool = True
    read_from_snapshot: bool = True
    destinations: List[DraftDestination] = field(default_factory=list)
    sync_client_artefacts: bool = True
    transient_editor_files: bool = True
    patterns_text: str = ""
    count_floor_text: str = str(HoldThresholds().count_floor)
    percent_text: str = field(default_factory=lambda: _percent_text(HoldThresholds().percent))
    share_on: bool = True                                       # the share hold; off keeps share_text
    share_text: str = field(default_factory=lambda: _percent_text(DEFAULT_SHARE))
    on_arrival: bool = False
    scheduled: bool = False
    days_kind: str = DAILY                                     # DAILY, WEEKLY or MONTHLY
    schedule_days: Set[int] = field(default_factory=set)      # 0 Monday ... 6 Sunday; weekly only
    month_day_text: str = MONTH_DAY_CHOICES[0]                 # one of MONTH_DAY_CHOICES; monthly only
    repeating: bool = False                                    # every N hours rather than at the times listed
    schedule_time_text: str = DEFAULT_SCHEDULE_TIME            # one or more times, separated by commas
    every_text: str = DEFAULT_EVERY_HOURS
    window_on: bool = False                                    # every N hours only inside the window
    window_start_text: str = DEFAULT_WINDOW[0]
    window_end_text: str = DEFAULT_WINDOW[1]
    paused: bool = False
    cleanup: bool = False                                      # delete versions batches older than keep_days_text
    keep_days_text: str = str(DEFAULT_KEEP_DAYS)
    reminder_days_text: str = str(DEFAULT_REMINDER_DAYS)
    base: Optional[Job] = None

    @classmethod
    def from_job(cls, job: Job, *, source_editable: bool) -> "JobDraft":
        draft = cls(name=job.name, source=job.source.endpoint, source_editable=source_editable,
                    read_from_snapshot=job.source.read_from_snapshot,
                    destinations=[DraftDestination(d.endpoint, d.marker_id) for d in job.destinations],
                    sync_client_artefacts=job.source.default_exclusions.sync_client_artefacts,
                    transient_editor_files=job.source.default_exclusions.transient_editor_files,
                    patterns_text="\n".join(job.source.exclusions),
                    count_floor_text=str(job.holds.count_floor), percent_text=_percent_text(job.holds.percent),
                    share_on=job.holds.share is not None,
                    share_text=_percent_text(DEFAULT_SHARE if job.holds.share is None else job.holds.share),
                    on_arrival=job.triggers.on_arrival, paused=not job.enabled,
                    cleanup=job.version_cleanup.enabled,
                    keep_days_text=str(job.version_cleanup.keep_days or DEFAULT_KEEP_DAYS),
                    reminder_days_text=str(job.verification_reminder_days), base=job)
        if job.triggers.schedule is not None:
            draft._show_schedule(parse_schedule(job.triggers.schedule))
        return draft

    def _show_schedule(self, schedule: Schedule) -> None:
        self.scheduled, self.days_kind = True, schedule.kind
        if schedule.kind == WEEKLY:
            self.schedule_days = set(schedule.weekdays)
        if schedule.kind == MONTHLY:
            self.month_day_text = LAST_DAY_CHOICE if schedule.month_day == LAST else str(schedule.month_day)
        self.repeating = schedule.every is not None
        if schedule.every is None:
            self.schedule_time_text = ", ".join(clock(t) for t in schedule.times)
        else:
            self.every_text = str(schedule.every)
            self.window_on = schedule.window is not None
            if schedule.window is not None:
                self.window_start_text, self.window_end_text = clock(schedule.window.start), clock(schedule.window.end)

    @property
    def automatic_chosen(self) -> bool:
        """Whether the job would run by itself: the pause switch applies only then."""
        return self.on_arrival or self.scheduled

    # --- editing ---------------------------------------------------------------------

    def set_source(self, path: str, volumes: Sequence[VolumeInfo]) -> None:
        """Raises EndpointError for a path on no present local volume, and for one where
        nothing exists."""
        if not self.source_editable:
            raise EndpointError("the source of a job that has run cannot change; make a new job instead")
        source = endpoint_from_path(path, volumes)
        if not os.path.exists(path):        # a file is left to validation, which says it is not a folder
            raise EndpointError(f"{path}: {SOURCE_MISSING}")
        self.source = source

    def clear_source(self) -> None:
        """No source: the source's field is empty or holds a wrong path."""
        if self.source_editable:
            self.source = None

    def add_destination(self, path: str, volumes: Sequence[VolumeInfo]) -> None:
        """Raises EndpointError for a path on no present local volume; a folder not made
        yet is taken."""
        self.destinations.append(DraftDestination(endpoint_from_path(path, volumes)))

    def add_remote_destination(self, remote: str, folder: str) -> None:
        """Raises EndpointError for a remote's name or a folder no remote can have; a folder
        not made yet is taken."""
        try:
            self.destinations.append(DraftDestination(RemoteEndpoint(remote, folder)))
        except ContractError as exc:
            raise EndpointError(str(exc)) from None

    @property
    def remote_endpoints(self) -> Tuple[RemoteEndpoint, ...]:
        return tuple(d.endpoint for d in self.destinations if isinstance(d.endpoint, RemoteEndpoint))

    def remove_destination(self, position: int) -> None:
        del self.destinations[position]

    def move_destination(self, position: int, step: int) -> None:
        target = position + step
        if 0 <= target < len(self.destinations):
            items = self.destinations
            items[position], items[target] = items[target], items[position]

    @property
    def patterns(self) -> Tuple[str, ...]:
        return tuple(line.strip() for line in self.patterns_text.splitlines() if line.strip())

    # --- checking --------------------------------------------------------------------

    def _input_checks(self) -> Tuple[List[Check], Optional[HoldThresholds]]:
        problems: List[Check] = []
        if not self.name.strip():
            problems.append(Check(REFUSED, "The job needs a name.", "input-name"))
        if self.source is None:
            problems.append(Check(REFUSED, "Choose the source folder.", "input-source"))
        if not self.destinations:
            problems.append(Check(REFUSED, "Add at least one destination.", "input-destinations"))
        holds = None
        share_usable, share = self._share(problems)
        try:
            floor = int(self.count_floor_text.strip())
            percent = float(self.percent_text.strip())
            if not math.isfinite(percent):
                raise ValueError
            checked = HoldThresholds(floor, _plain(percent), share if share_usable else DEFAULT_SHARE)
            holds = checked if share_usable else None
        except ValueError:
            problems.append(Check(REFUSED, "The hold values must be a whole number of files and a percentage.",
                                  "input-holds"))
        except ContractError:
            problems.append(Check(REFUSED, "Hold removals above at least 0 files and a percentage above 0 and at "
                                           "most 100.", "input-holds"))
        return problems, holds

    def _share(self, problems: List[Check]) -> Tuple[bool, Optional[float]]:
        """Whether the share hold's input can be used, and the share (None: switched off);
        a share that cannot is a problem added."""
        if not self.share_on:
            return True, None
        try:
            share = float(self.share_text.strip())
        except ValueError:
            share = math.nan
        if not math.isfinite(share) or not 0 < share <= 100:
            problems.append(Check(REFUSED, "The share must be a percentage above 0 and at most 100.", "input-share"))
            return False, None
        return True, _plain(share)

    def _maintenance(self, problems: List[Check]) -> Tuple[Optional[VersionCleanup], Optional[int]]:
        """The version cleanup and the reminder interval, or None for one that is not valid
        (a problem added)."""
        cleanup = VersionCleanup()
        if self.cleanup:
            days = _whole_number(self.keep_days_text)
            if days is None:
                problems.append(Check(REFUSED, "Old versions are deleted after a whole number of days, at least 1.",
                                      "input-keep-days"))
                cleanup = None
            else:
                cleanup = VersionCleanup(days)
        reminder = _whole_number(self.reminder_days_text)
        if reminder is None:
            problems.append(Check(REFUSED, "The verification reminder needs a whole number of days, at least 1.",
                                  "input-reminder-days"))
        return cleanup, reminder

    def _schedule(self, problems: List[Check]) -> Optional[str]:
        """The schedule's text, or None when not scheduled or not valid (a problem added)."""
        if not self.scheduled:
            return None
        found = len(problems)
        weekdays, month_day = None, None
        if self.days_kind == WEEKLY:
            weekdays = tuple(self.schedule_days)
            if not weekdays:
                problems.append(Check(REFUSED, "Choose at least one day for the weekly schedule.",
                                      "input-schedule-days"))
        elif self.days_kind == MONTHLY:
            if self.month_day_text not in MONTH_DAY_CHOICES:
                problems.append(Check(REFUSED, "Choose a day of the month from 1 to 28, or the last day.",
                                      "input-schedule-month-day"))
            else:
                month_day = LAST if self.month_day_text == LAST_DAY_CHOICE else int(self.month_day_text)
        times, every, window = (), None, None
        if not self.repeating:
            try:
                times = parse_times(self.schedule_time_text)
            except ScheduleError:
                problems.append(Check(REFUSED, "The schedule needs times of day such as 21:00, separated by commas.",
                                      "input-schedule-time"))
        else:
            every = _whole_number(self.every_text)
            if every not in EVERY_HOURS:
                problems.append(Check(REFUSED, "Every takes a whole number of hours from 1 to 12.",
                                      "input-schedule-every"))
            if self.window_on:
                try:
                    window = Window(parse_minute(self.window_start_text), parse_minute(self.window_end_text))
                except ScheduleError:
                    problems.append(Check(REFUSED, "The hours to run between need two different times of day such "
                                                   "as 08:00 and 22:00.", "input-schedule-window"))
        if len(problems) > found:
            return None
        try:
            return Schedule(weekdays, month_day, times, every, window).text()
        except ScheduleError as exc:              # a rule of the grammar itself, such as its most times a day
            problems.append(Check(REFUSED, sentence(f"{exc}."), "input-schedule"))
            return None

    def build(self) -> Tuple[Optional[Job], List[Check]]:
        problems, holds = self._input_checks()
        schedule = self._schedule(problems)
        cleanup, reminder = self._maintenance(problems)
        if problems:
            return None, problems
        defaults = DefaultExclusions(self.sync_client_artefacts, self.transient_editor_files)
        triggers, enabled = Triggers(self.on_arrival, schedule), not self.paused
        try:
            if self.base is None:
                job = new_job(self.name, self.source, [d.endpoint for d in self.destinations],
                              read_from_snapshot=self.read_from_snapshot, default_exclusions=defaults,
                              exclusions=self.patterns, holds=holds, triggers=triggers, enabled=enabled,
                              version_cleanup=cleanup, verification_reminder_days=reminder)
                return job, []
            destinations = tuple(Destination(d.endpoint, d.marker_id) if d.marker_id else Destination(d.endpoint)
                                 for d in self.destinations)
            source = replace(self.base.source, endpoint=self.source, read_from_snapshot=self.read_from_snapshot,
                             default_exclusions=defaults, exclusions=self.patterns)
            return replace(self.base, name=self.name.strip(), source=source, destinations=destinations,
                           holds=holds, triggers=triggers, enabled=enabled, version_cleanup=cleanup,
                           verification_reminder_days=reminder), []
        except ContractError as exc:
            return None, [Check(REFUSED, sentence(f"{exc}."), "input-contract")]


def check(draft: JobDraft, volumes: Sequence[VolumeInfo], sync_roots: Sequence[SyncRoot],
          other_jobs: Sequence[Job], store_path: Optional[Path] = None, *,
          remote_setup: Optional[RemoteSetup] = None, asking: bool = False) -> CheckResult:
    """The draft as a job, and every check on it: input problems, then the core's
    findings, refused first, but for those that say nothing about this job
    (``_NOT_LISTED``). ``store_path``: the job file in use — with it, the checks look at
    this computer too (``ValidationContext.on_this_computer``). ``remote_setup``: what the
    engine last said of the remote destinations; ``asking``: it is being asked again, so
    that what it has not said yet is said to wait, and the job cannot be saved yet."""
    job, problems = draft.build()
    if job is None:
        return CheckResult(None, tuple(problems))
    others = tuple(j for j in other_jobs if j.job_id != job.job_id)
    context = (ValidationContext.on_this_computer(volumes, sync_roots, others, store_path, remote_setup=remote_setup)
               if store_path is not None
               else ValidationContext(tuple(volumes), tuple(sync_roots), others, remote_setup=remote_setup))
    report = validate_job(job, context, None)
    order = {Severity.REFUSED: 0, Severity.WARNING: 1, Severity.NOTICE: 2}
    level = {Severity.REFUSED: REFUSED, Severity.WARNING: WARNING, Severity.NOTICE: NOTE}
    checks = [Check(level[f.severity], sentence(f"{f.subject}: {window_reason(f.message)}."), f.code)
              for f in sorted(report.findings, key=lambda f: order[f.severity]) if f.code not in _NOT_LISTED
              and not (asking and f.code == REMOTE_UNREACHABLE)]
    if asking:
        checks.append(Check(NOTE, ASKING_REMOTE, "asking-remote"))
    return CheckResult(job, tuple(checks), asking)


def cleanup_question(job: Job, base: Optional[Job]) -> Optional[str]:
    """The question a save asks first when it switches version cleanup on or lowers its
    days — old versions are then deleted permanently — or None."""
    days = job.version_cleanup.keep_days
    if days is None:
        return None
    before = base.version_cleanup.keep_days if base is not None else None
    if before is not None and days >= before:
        return None
    return (f"From the next run on, version batches older than {counted(days, 'day')} are deleted "
            f"permanently from this job's destinations.")


def typed_path(text: str) -> str:
    """A folder as the user typed or pasted it, normalised as a chosen one is: the spaces
    around it and one pair of double quotes dropped — Explorer's *Copy as path* adds
    them; "" for none."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return os.path.normpath(text) if text else ""


def path_problem(error: EndpointError) -> str:
    """What is wrong with a typed or chosen path, as a line beneath its field."""
    return sentence(f"{error}.")


def _whole_number(text: str) -> Optional[int]:
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value >= 1 else None


def snapshot_choice(draft: JobDraft, volumes: Sequence[VolumeInfo]) -> Tuple[bool, str]:
    """Whether the snapshot choice can be changed, and what to say of it — what it does,
    or why it is disabled: a source on a present drive that cannot be snapshotted disables
    it; an absent drive does not, since what it is cannot be read until it is attached."""
    if draft.source is not None:
        resolution = resolve(draft.source, volumes)
        if resolution.presence is Presence.PRESENT and not resolution.volume.snapshot_eligible:
            return False, NO_SNAPSHOT_NOTE
    return True, SNAPSHOT_NOTE


def endpoint_line(endpoint: AnyEndpoint, volumes: Sequence[VolumeInfo],
                  remote_setup: Optional[RemoteSetup] = None) -> Tuple[str, str, str]:
    """(folder, drive, free) for an endpoint in the editor's lists; ``remote_setup``: what
    the engine said of a remote's, whose free space only it knows."""
    if isinstance(endpoint, RemoteEndpoint):
        facts = remote_setup.facts.get(endpoint) if remote_setup is not None else None
        return endpoint.describe(), "remote", size(facts.space[0]) if facts is not None and facts.space else ""
    resolution = resolve(endpoint, volumes)
    if resolution.presence is Presence.PRESENT:
        volume = resolution.volume
        drive = (f'"{volume.label}", ' if volume.label else "") + volume.file_system
        if endpoint.is_drive_root:
            drive += " - a drive root"
        return str(resolution.location), drive, size(volume.free_bytes)
    if resolution.presence is Presence.AMBIGUOUS:
        return describe_folder(resolution), "two drives match", ""
    return describe_folder(resolution), "not attached", ""


def _percent_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _plain(value: float) -> float:
    """A whole percentage as an integer, as the job file keeps one."""
    return int(value) if value.is_integer() else value
