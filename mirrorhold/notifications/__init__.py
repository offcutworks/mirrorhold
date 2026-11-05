"""
Notifications — telling the user how a run ended, with the GUI closed.

Background
----------
Runs happen when nobody watches: a backup drive is attached, a schedule comes round,
the window that started a run was closed. Each run's core process therefore raises one
notice when it ends — completed, removals held (or a first run waiting), failed, or
files not backed up for two runs in a row (under the failure choice) — with the counts,
and with a destination's drive named when it was low on free space, subject to the
user's choices in the preferences, and with a line for each destination it backed up whose
full verification is due. A full verification, which can last hours, raises one when it
ends too: differences found, none found, or a failure; and so does a repair of the copies
a verification found to differ: every copy replaced, some left, or a failure. A notice that
asks something of the user (``Notice.asks``) stays on the screen until the user closes it,
and leaves a reminder the notification-area icon keeps showing until the user opens the
window (``mirrorhold.reminders``).

Structure
---------
    message.py   notice_for(): a run's records, the user's choices and the destinations
                 due a verification -> a Notice (title, one line per announced
                 destination, one per destination due) or none;
                 failure_notice() for an automatic run that could not start;
                 verification_notice() for a verification's end; repair_notice()
                 for a repair's end.
    toast.py     ToastNotifier (a notice as a Windows notification), SilentNotifier,
                 default_notifier().
    windows_runtime.py
                 show(): a toast handed to Windows through the Windows Runtime from
                 this process, with ctypes; check(): the same without showing it.
    __init__.py  announce_run(), announce_verification() and announce_repair(): the one
                 call a run's, a verification's or a repair's process makes when it ends;
                 announce(), the run's notice from what announce_run() gathers.

Architecture
------------
The command line's ``run``, ``verify`` and ``repair`` and the scheduler's ``automatic``
entry call ``announce_run`` (or ``announce_verification``, ``announce_repair``) when their work
returns; the GUI never raises a notice, because every run, verification and repair it
starts is a core process of its own. What to say (``message``) is pure and tested without Windows; how to show it
(``toast``) is a seam a caller replaces, and so is where a notice that asks something leaves its
reminder (``remind``): the caller binds it to the store and the job, which this package does not
know. The process that raises a notice hands it to
Windows itself (``windows_runtime``) and starts no other program: a hidden shell given an
encoded script is what behaviour-based antivirus watches for, so a notice is never shown
through one.

Design patterns
---------------
- One notice per run, never one per file or per event.
- An injected notifier: tests and the in-process command line pass their own; only
  the real entry point builds the Windows one.

Caveats
-------
- A notification under MirrorHold's own name needs its Start menu entry, which the
  installation creates; before that, notices appear under Windows PowerShell's name.
- ``MIRRORHOLD_NOTIFICATIONS=off`` silences a process and the runs it starts; the
  test suites rely on it.
- A notice that cannot be shown is not a failed run: ``announce`` reports it to the
  caller and the run's records stand.
- A file name holding an unpaired surrogate, which neither the toast's XML nor a Windows
  string can carry, is shown with the replacement character.
- The verification reminder reaches a notice only through a run that backs the destination
  up and raises a notice anyway: no process of the tool runs to tell it otherwise, and a
  notice of its own after every run would be one more each day for a daily schedule.
"""
from __future__ import annotations

from typing import AbstractSet, Callable, Optional, Sequence, Tuple

from ..jobs.model import Job, Notifications
from ..locations import RuntimeLayout
from ..maintenance.repair import RepairRecord
from ..maintenance.verification import VerificationRecord, due_after_run
from ..records.record import RunRecord
from .message import Notice, failure_notice, notice_for, repair_notice, verification_notice
from .toast import NotificationError, Notifier

Remind = Callable[[Notice], None]
"""What a raised notice is handed to, to leave its reminder (``mirrorhold.reminders.reminding``)."""


def announce(records: Sequence[RunRecord], preferences: Notifications, notifier: Optional[Notifier],
             due: Sequence[Tuple[int, Optional[str]]] = (), *, remote: AbstractSet[int] = frozenset(),
             remind: Optional[Remind] = None) -> str:
    """Show the notice a run's records call for, with ``due``
    (``maintenance.verification.due_after_run``) as its last lines; ``remote``: the indexes
    of the job's remote destinations (``notice_for``); ``remind``: as ``show``. Returns why it
    could not be shown, or ""."""
    if notifier is None:
        return ""
    notice = notice_for(records, preferences, due, remote=remote)
    return show(notice, notifier, remind=remind)


def announce_run(layout: RuntimeLayout, job: Job, records: Sequence[RunRecord], preferences: Notifications,
                 notifier: Optional[Notifier], *, remind: Optional[Remind] = None) -> str:
    """``announce`` for one run of ``job``: with the destinations it backed up whose full
    verification is due, and a remote's low free space said at its remote. Returns why the
    notice could not be shown, or ""."""
    if notifier is None:
        return ""
    due = due_after_run(layout, job.verification_reminder_days, records)
    remote = frozenset(i for i, d in enumerate(job.destinations, 1) if d.remote is not None)
    return announce(records, preferences, notifier, due, remote=remote, remind=remind)


def announce_verification(record: Optional[VerificationRecord], preferences: Notifications,
                          notifier: Optional[Notifier], *, remind: Optional[Remind] = None) -> str:
    """Show the notice a verification's end calls for. Returns why it could not be shown, or ""."""
    if notifier is None:
        return ""
    return show(verification_notice(record, preferences), notifier, remind=remind)


def announce_repair(record: Optional[RepairRecord], preferences: Notifications, notifier: Optional[Notifier], *,
                    remind: Optional[Remind] = None) -> str:
    """Show the notice a repair's end calls for. Returns why it could not be shown, or ""."""
    if notifier is None:
        return ""
    return show(repair_notice(record, preferences), notifier, remind=remind)


def show(notice: Optional[Notice], notifier: Optional[Notifier], *, remind: Optional[Remind] = None) -> str:
    """Show ``notice``, then hand it to ``remind`` (``mirrorhold.reminders.reminding``), whether or
    not Windows took it. Returns why it could not be shown, or ""."""
    if notice is None or notifier is None:
        return ""
    try:
        notifier.show(notice)
    except NotificationError as exc:
        problem = str(exc)
    else:
        problem = ""
    if remind is not None:
        remind(notice)
    return problem


__all__ = ["Notice", "Notifier", "announce", "announce_repair", "announce_run", "announce_verification",
           "failure_notice", "notice_for", "repair_notice", "show", "verification_notice"]
