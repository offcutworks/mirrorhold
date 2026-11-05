"""The scheduled tasks a configuration calls for, as Task Scheduler definitions.

Per job with automatic runs switched on, at most two tasks, each named by the job's
identifier and what starts it:

- ``<job id>-arrival`` (the job runs when one of its drives is attached): an event
  trigger on a volume device being started or removed, and a logon trigger. Measured
  on the development machine, ``Microsoft-Windows-Kernel-PnP/Configuration`` logs a
  volume device's start (410) when a drive is attached and its removal (420) when it goes,
  each naming the volume device class, for every file system — but not at an ordinary
  start of the computer, when the drives that stayed attached are simply there. The
  logon trigger is therefore what makes "the computer starts with the drive attached"
  an arrival. Removals start the task too, so that the core notices a drive is gone
  and counts it as arriving when it comes back (``automation.arrival``).
- ``<job id>-schedule``: calendar triggers — daily, on chosen weekdays, or on a day of
  every month — one for each time of a schedule's list, or one repeating every N hours
  across its window; a missed occurrence runs at the next opportunity
  (``StartWhenAvailable``). An ``every Nh`` schedule registered while one of its days is
  under way also has a one-off trigger for that day's runs still to come, which would
  otherwise wait for the next day: a calendar trigger's first run fixes the time of day it
  repeats at, so it cannot start partway through the day.

Every task starts the installed copy's core — the source installation's copy, or the
portable release set up — with its windowless interpreter, in its folder, against the
store the installation serves (``invocation`` composes the command), and runs only
while the user is logged on, with the user's own rights: no password is stored, and a
notice can reach the user's desktop. A second start while a task's run is going is
ignored (``IgnoreNew``), which coalesces a burst of events into one run; there is no
time limit, because a first backup of a large drive takes hours.

A task's description ends with a fingerprint of everything its definition depends on,
so reconciliation re-registers only a task whose definition changed — re-registering
would reset a schedule's missed-run catch-up.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional
from xml.sax.saxutils import escape

from ..installation.manifest import InstalledCopy
from ..invocation import core_arguments
from ..jobs.model import Job
from ..jobs.schedule import LAST, MONTHLY, Schedule, parse_schedule

TASK_FOLDER = "\\MirrorHold"
ARRIVAL, SCHEDULE = "arrival", "schedule"
DEFINITION_FORMAT = 2
"""Raised whenever the generated definition changes, so every task is re-registered once."""

VOLUME_CLASS = "{71a27cdd-812a-11d0-bec7-08002be2092f}"
PNP_CHANNEL = "Microsoft-Windows-Kernel-PnP/Configuration"
VOLUME_STARTED, VOLUME_REMOVED = 410, 420
EVENT_DELAY = "PT10S"
LOGON_DELAY = "PT1M"
TASK_PRIORITY = 6
"""Normal priority: a backup the user is waiting for should not crawl behind other work."""

_WEEKDAY_ELEMENTS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_ELEMENTS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                   "October", "November", "December")
_LAST_DAY = "Last"
"""Task Scheduler's last day of the month; measured on the development machine, only this
spelling validates."""
_STAMP = "%Y-%m-%dT%H:%M:%S"
FINGERPRINT_PREFIX = "Definition "


def current_user() -> str:
    """The logged-on user, as ``DOMAIN\\name``, whom the tasks run as."""
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


@dataclass(frozen=True)
class Launcher:
    """How a task starts the installed core."""

    interpreter: Path
    root: Path
    store: Path
    user: str

    @classmethod
    def for_installation(cls, copy: InstalledCopy, user: Optional[str] = None) -> "Launcher":
        return cls(copy.interpreter, copy.root, copy.store, user or current_user())


@dataclass(frozen=True)
class TaskSpec:
    name: str
    job_id: str
    job_name: str
    kind: str                       # ARRIVAL or SCHEDULE
    schedule: Optional[str]         # the canonical schedule text of a SCHEDULE task
    launcher: Launcher

    @property
    def arguments(self) -> str:
        return subprocess.list2cmdline(core_arguments("--store", self.launcher.store, "automatic", self.job_id,
                                                      "--by", self.kind))

    @property
    def fingerprint(self) -> str:
        parts = (str(DEFINITION_FORMAT), self.kind, self.job_id, self.job_name, self.schedule or "",
                 str(self.launcher.interpreter), str(self.launcher.root), self.arguments, self.launcher.user)
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]

    @property
    def description(self) -> str:
        if self.kind == ARRIVAL:
            what = (f'MirrorHold runs the job "{self.job_name}" when one of its drives is attached, or the '
                    "computer starts with it attached.")
        else:
            what = f'MirrorHold runs the job "{self.job_name}": {parse_schedule(self.schedule).describe()}.'
        return f"{what}\n{FINGERPRINT_PREFIX}{self.fingerprint}"


def task_name(job_id: str, kind: str) -> str:
    return f"{job_id}-{kind}"


def fingerprint_of(description: str) -> Optional[str]:
    """The fingerprint a registered task's description carries, if any."""
    for line in reversed((description or "").splitlines()):
        if line.startswith(FINGERPRINT_PREFIX):
            return line[len(FINGERPRINT_PREFIX):].strip()
    return None


def desired_tasks(jobs: Iterable[Job], launcher: Launcher) -> Dict[str, TaskSpec]:
    """Every task the jobs call for: none for a job without automatic runs, or one whose
    automatic runs are paused."""
    wanted: Dict[str, TaskSpec] = {}
    for job in jobs:
        if not job.enabled:
            continue
        if job.triggers.on_arrival:
            name = task_name(job.job_id, ARRIVAL)
            wanted[name] = TaskSpec(name, job.job_id, job.name, ARRIVAL, None, launcher)
        if job.triggers.schedule is not None:
            name = task_name(job.job_id, SCHEDULE)
            wanted[name] = TaskSpec(name, job.job_id, job.name, SCHEDULE, job.triggers.schedule, launcher)
    return wanted


def volume_event_query() -> str:
    """The event subscription of an arrival task: a volume device started or removed."""
    select = (f"*[System[Provider[@Name='Microsoft-Windows-Kernel-PnP'] and "
              f"(EventID={VOLUME_STARTED} or EventID={VOLUME_REMOVED})]] and "
              f"*[EventData[Data[@Name='ClassGuid']='{VOLUME_CLASS}']]")
    return (f'<QueryList><Query Id="0" Path="{PNP_CHANNEL}">'
            f'<Select Path="{PNP_CHANNEL}">{escape(select)}</Select></Query></QueryList>')


def _triggers(spec: TaskSpec, now: datetime) -> str:
    user = escape(spec.launcher.user)
    if spec.kind == ARRIVAL:
        return ("<EventTrigger><Enabled>true</Enabled>"
                f"<Subscription>{escape(volume_event_query())}</Subscription>"
                f"<Delay>{EVENT_DELAY}</Delay></EventTrigger>"
                f"<LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId>"
                f"<Delay>{LOGON_DELAY}</Delay></LogonTrigger>")
    schedule = parse_schedule(spec.schedule)
    by = _calendar(schedule)
    if schedule.every is None:
        return "".join(f"<CalendarTrigger><StartBoundary>{schedule.next_at(minute, now).strftime(_STAMP)}"
                       f"</StartBoundary><Enabled>true</Enabled>{by}</CalendarTrigger>"
                       for minute in schedule.times)
    start = schedule.next_at(schedule.first_minute, now).strftime(_STAMP)
    triggers = (f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>"
                f"{_repetition(schedule.every, schedule.span)}{by}</CalendarTrigger>")
    under_way = schedule.day_under_way(now)
    if under_way is None:
        return triggers
    first, last = under_way
    rest = _repetition(schedule.every, int((last - first).total_seconds()) // 60)
    return (f"<TimeTrigger><StartBoundary>{first.strftime(_STAMP)}</StartBoundary><Enabled>true</Enabled>"
            f"{rest}</TimeTrigger>{triggers}")


def _calendar(schedule: Schedule) -> str:
    """The days a calendar trigger fires on."""
    if schedule.kind == MONTHLY:
        day = _LAST_DAY if schedule.month_day == LAST else str(schedule.month_day)
        months = "".join(f"<{m} />" for m in _MONTH_ELEMENTS)
        return f"<ScheduleByMonth><DaysOfMonth><Day>{day}</Day></DaysOfMonth><Months>{months}</Months></ScheduleByMonth>"
    if schedule.is_daily:
        return "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    days = "".join(f"<{_WEEKDAY_ELEMENTS[d]} />" for d in schedule.weekdays)
    return f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"


def _repetition(hours: int, span: int) -> str:
    """Runs every ``hours`` for ``span`` minutes after a trigger's first, the last one
    included: the duration reaches a minute past it, so whether Task Scheduler starts an
    occurrence falling exactly at a duration's end never matters. Nothing for one run."""
    if span <= 0:
        return ""
    return (f"<Repetition><Interval>PT{hours}H</Interval><Duration>PT{span + 1}M</Duration>"
            "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")


def task_xml(spec: TaskSpec, now: datetime) -> str:
    """The task's definition in Task Scheduler's XML schema (version 1.2). ``now`` (a
    naive local time) fixes a schedule's first occurrence, always in the future, so a
    task registered after its time of day does not run at once."""
    launcher = spec.launcher
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<RegistrationInfo><Author>MirrorHold</Author><Description>{escape(spec.description)}</Description>"
        "</RegistrationInfo>"
        f"<Triggers>{_triggers(spec, now)}</Triggers>"
        f'<Principals><Principal id="Author"><UserId>{escape(launcher.user)}</UserId>'
        "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<AllowHardTerminate>true</AllowHardTerminate>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
        "<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>"
        "<AllowStartOnDemand>true</AllowStartOnDemand>"
        "<Enabled>true</Enabled>"
        "<Hidden>false</Hidden>"
        "<RunOnlyIfIdle>false</RunOnlyIfIdle>"
        "<WakeToRun>false</WakeToRun>"
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
        f"<Priority>{TASK_PRIORITY}</Priority>"
        "</Settings>"
        '<Actions Context="Author"><Exec>'
        f"<Command>{escape(str(launcher.interpreter))}</Command>"
        f"<Arguments>{escape(spec.arguments)}</Arguments>"
        f"<WorkingDirectory>{escape(str(launcher.root))}</WorkingDirectory>"
        "</Exec></Actions>"
        "</Task>"
    )
