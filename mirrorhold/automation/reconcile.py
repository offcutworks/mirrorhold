"""Bringing Task Scheduler into line with the configuration — the only way tasks change.

``reconcile`` makes the tool's task folder hold exactly the desired tasks: it registers
a missing one, re-registers one whose fingerprint differs, leaves an unchanged one
alone, and removes every other task in the folder. ``apply_automation`` is what every
caller runs after the jobs change (the command line, the GUI, the installer): it works
out the desired tasks from the installation that serves this store — the source
installation's copy, or the portable release whose setup record sits beside the store —
and does nothing, not even open Task Scheduler, when none does, because a task must
never run a working copy (an arrival during development would run half-edited code).
Whichever copy saves the jobs reconciles toward that installation, so a copy that is not
the one installed never takes the tasks over by saving. A portable release that was
moved or deleted after it was set up leaves the tasks as they are (``GONE``) until a copy
is set up again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional, Tuple

from ..installation.manifest import InstalledCopy, read_installation
from ..jobs.model import Job
from ..locations import PROJECT_ROOT, CopyKind, copy_kind, install_dir, runtime_layout
from ..wording import counted
from .scheduler import Scheduler, SchedulerError, WindowsScheduler
from .tasks import Launcher, TaskSpec, desired_tasks, fingerprint_of, task_xml


@dataclass(frozen=True)
class ReconcileResult:
    registered: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    unchanged: Tuple[str, ...] = ()

    @property
    def tasks(self) -> int:
        return len(self.registered) + len(self.unchanged)


def reconcile(scheduler: Scheduler, desired: Mapping[str, TaskSpec], now: Optional[datetime] = None) \
        -> ReconcileResult:
    """Make the scheduler's folder hold exactly ``desired``. Raises SchedulerError."""
    now = now or datetime.now()
    existing = {t.name: t for t in scheduler.tasks()}
    registered, unchanged, removed = [], [], []
    for name, spec in sorted(desired.items()):
        current = existing.get(name)
        if current is not None and fingerprint_of(current.description) == spec.fingerprint:
            unchanged.append(name)
            continue
        scheduler.register(name, task_xml(spec, now))
        registered.append(name)
    for name in sorted(set(existing) - set(desired)):
        scheduler.delete(name)
        removed.append(name)
    return ReconcileResult(tuple(registered), tuple(removed), tuple(unchanged))


def remove_all(scheduler: Scheduler) -> ReconcileResult:
    """Remove every task in the folder and the folder itself. Raises SchedulerError."""
    result = reconcile(scheduler, {})
    scheduler.remove_folder()
    return result


class AutomationState(str, Enum):
    NOT_INSTALLED = "not-installed"     # nothing to register with: no task may exist
    OTHER_STORE = "other-store"         # the installed copy serves another job file; left alone
    GONE = "gone"                       # the portable release set up is no longer where it was; left alone
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class AutomationOutcome:
    state: AutomationState
    result: Optional[ReconcileResult] = None
    problem: str = ""
    installation: Optional[InstalledCopy] = None

    @property
    def ok(self) -> bool:
        return self.state is not AutomationState.FAILED

    def describe(self) -> str:
        if self.state is AutomationState.NOT_INSTALLED:
            return "automatic runs: not installed, so no job runs by itself"
        if self.state is AutomationState.OTHER_STORE:
            return "automatic runs: the installed copy serves another job file; its tasks were left alone"
        if self.state is AutomationState.GONE:
            return (f"automatic runs cannot start: the copy of MirrorHold they use is gone ({self.installation.root});"
                    " set up a copy to run them")
        if self.state is AutomationState.FAILED:
            return f"automatic runs could not be updated: {self.problem}"
        r = self.result
        return (f"automatic runs: {counted(r.tasks, 'scheduled task')} "
                f"({len(r.registered)} registered, {len(r.removed)} removed)")


def current_installation(store_path: Path, source: Path = PROJECT_ROOT) -> Optional[InstalledCopy]:
    """The installation the copy at ``source`` reconciles toward: in a portable release, the
    setup record beside ``store_path``; elsewhere, the source installation's copy."""
    if copy_kind(source) is CopyKind.PORTABLE:
        copy = read_installation(runtime_layout(store_path).root)
        return copy if copy is not None and copy.portable else None
    return read_installation(install_dir(source))


def apply_automation(store_path: Path, jobs: Iterable[Job], *, root: Optional[Path] = None,
                     scheduler: Optional[Scheduler] = None, now: Optional[datetime] = None,
                     user: Optional[str] = None, source: Path = PROJECT_ROOT) -> AutomationOutcome:
    """Register exactly the tasks ``jobs`` call for, pointing at the installation recorded
    in ``root`` (default: the one the copy at ``source`` reconciles toward), when it serves
    ``store_path``. Never raises."""
    copy = read_installation(root) if root is not None else current_installation(store_path, source)
    if copy is None:
        return AutomationOutcome(AutomationState.NOT_INSTALLED)
    if not copy.serves(store_path):
        return AutomationOutcome(AutomationState.OTHER_STORE, installation=copy)
    if not copy.present:
        return AutomationOutcome(AutomationState.GONE, installation=copy)
    desired = desired_tasks(jobs, Launcher.for_installation(copy, user))
    try:
        result = reconcile(scheduler or WindowsScheduler(), desired, now)
    except SchedulerError as exc:
        return AutomationOutcome(AutomationState.FAILED, problem=str(exc), installation=copy)
    return AutomationOutcome(AutomationState.APPLIED, result, installation=copy)
