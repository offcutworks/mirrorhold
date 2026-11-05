"""What a finished run tells the user: one notice per run, from its records.

A run's records sort into three kinds the user chooses to hear about
(``jobs.model.Notifications``): completed; held (removals held, or a first run waiting
for confirmation); failed (failed, stopped or interrupted). A cancelled pair was the
user's own act and an absent drive only waits, so neither is announced. A notice names
the job, leads with its most urgent kind, and gives one line per announced destination
with what was done. A first run that waits says what the user is asked for: the go-ahead
for a first backup onto a folder that holds nothing, a review of what a folder already
holds (``HOLDS_CONTENT_REASON``), or a review that makes a shared destination's lost record
again (``SHARED_RECORD_LOST``).

A completed pair that left files unbacked-up that its previous run left too is a
fourth kind, ``not backed up``, announced under the failure choice (the maintainer's
decision, 2026-10-06): a backup that keeps missing a file is failing at that file. With
that choice off it is announced as completed, under the completion choice.

A line whose destination's drive was low on free space when the pair ended says so; a
remote destination's, at its remote, where its service reports its space.
A destination the run backed up whose full verification is due adds a line of its
own to the notice the run raises (the maintainer's decision, 2026-10-15): at every such run
until it is verified, and only in a notice the run raises anyway, so a run the user's choices
keep quiet stays quiet and the window's *Verified* column still tells it. A full verification
raises a notice of its own when it ends (``verification_notice``): differences found or a
failure under the failure choice, no difference under the completion choice; a stopped one
raises none. A repair of the copies a verification found to differ (``repair_notice``)
follows the same rule: every copy replaced under the completion choice, any it failed to
replace or left as it is, or a repair that failed, under the failure choice, a stopped one
none.

A notice that asks something of the user — a review of what was held, or a look at what
failed, was not backed up, differs or was left — says so (``Notice.asks``): it stays on the
screen until the user closes it (``toast``) and leaves a reminder (``mirrorhold.reminders``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import AbstractSet, Optional, Sequence, Tuple

from ..jobs.model import Notifications
from ..maintenance.repair import RepairRecord, RepairState
from ..maintenance.verification import VerificationRecord, VerificationState
from ..records.record import (ESCALATE_AFTER, FIRST_RUN_REASON, HOLDS_CONTENT_REASON, SHARED_RECORD_LOST, Outcome,
                              RunRecord)
from ..wording import agreeing, counted, number

COMPLETED, HELD, FAILED, NOT_BACKED_UP = "completed", "held", "failed", "not backed up"

_KIND = {Outcome.COMPLETED: COMPLETED, Outcome.HELD: HELD, Outcome.FAILED: FAILED, Outcome.STOPPED: FAILED,
         Outcome.INTERRUPTED: FAILED}
_URGENCY = (FAILED, NOT_BACKED_UP, HELD, COMPLETED)
_HEADLINE = {FAILED: "a backup did not finish", NOT_BACKED_UP: "some files are not being backed up",
             HELD: "a backup needs your review", COMPLETED: "backed up"}


@dataclass(frozen=True)
class Notice:
    title: str
    lines: Tuple[str, ...]
    asks: bool = False      # it asks something of the user: a review, or a look at what failed


def _wanted(kind: str, preferences: Notifications) -> bool:
    return {COMPLETED: preferences.on_completion, HELD: preferences.on_hold, FAILED: preferences.on_failure,
            NOT_BACKED_UP: preferences.on_failure}[kind]


def _kind(record: RunRecord, preferences: Notifications) -> Optional[str]:
    kind = _KIND.get(record.outcome)
    if kind == COMPLETED and record.repeatedly_unbacked and preferences.on_failure:
        return NOT_BACKED_UP
    return kind


def _count(n: int) -> str:
    return number(n, separated=True)


def _files(n: int, what: str = "") -> str:
    """"1 file", "2,000 new files": a count of files, ``what`` saying which."""
    return counted(n, f"{what} file" if what else "file", separated=True)


def _size(n: int) -> str:
    if n < 1024:
        return counted(n, "byte", separated=True)
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def _line(record: RunRecord, kind: str, remote: AbstractSet[int]) -> str:
    line = _outcome_line(record, kind)
    if record.low_on_space:
        where = "at its remote" if record.destination_index in remote else "on its drive"
        line += f" - only {record.free_percent:.0f} % free {where}"
    return line


def _outcome_line(record: RunRecord, kind: str) -> str:
    where = f"Destination {record.destination_index}"
    done = record.done
    if kind == NOT_BACKED_UP:
        repeated = record.repeatedly_unbacked
        example = repeated[0][0].rsplit("\\", 1)[-1]
        return (f"{where}: completed - {_files(len(repeated))} not backed up "
                f"for {counted(ESCALATE_AFTER, 'run')} in a row (for example {example})")
    if kind == COMPLETED:
        identical = (f", {_count(done['files_identical'])} already identical" if done.get("files_identical") else "")
        return (f"{where}: completed - {_files(done['files_copied'])} copied ({_size(done['bytes_copied'])})"
                f"{identical}, {_count(done['moved_to_versions'])} into versions")
    if kind == HELD and FIRST_RUN_REASON in record.reasons:
        if SHARED_RECORD_LOST in record.reasons:
            return f"{where}: its record of what to leave was lost - review it in MirrorHold"
        if HOLDS_CONTENT_REASON in record.reasons:
            return f"{where}: the folder already holds files - review its first run in MirrorHold"
        return f"{where}: no backup there yet - start the first backup in MirrorHold"
    if kind == HELD:
        return (f"{where}: removals held for your review; {_files(done['files_copied'], 'new and changed')} "
                f"copied")
    return f"{where}: {record.outcome.value}" + (f" - {record.detail}" if record.detail else "")


def due_line(index: int, last: Optional[str]) -> str:
    """"Destination 2: a full verification is due (last: 2026-07-01)": the local date its
    last finished pass began, or never."""
    when = "never"
    if last:
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        except ValueError:
            pass
    return f"Destination {index}: a full verification is due (last: {when})"


def notice_for(records: Sequence[RunRecord], preferences: Notifications,
               due: Sequence[Tuple[int, Optional[str]]] = (), *,
               remote: AbstractSet[int] = frozenset()) -> Optional[Notice]:
    """The notice a run's records call for under the user's choices, or None. ``due``: the
    destinations the run backed up whose full verification is due, (index, when its last
    finished pass began or None) - each a line after the outcomes, in a notice raised anyway.
    ``remote``: the indexes of the job's destinations that are a remote's folder, whose low
    free space is said to be at the remote rather than on a drive."""
    kinds = [(r, _kind(r, preferences)) for r in records]
    announced = [(r, k) for r, k in kinds if k is not None and _wanted(k, preferences)]
    if not announced:
        return None
    worst = next(kind for kind in _URGENCY if any(k == kind for _, k in announced))
    job = announced[0][0].job_name
    return Notice(f"{job}: {_HEADLINE[worst]}",
                  tuple(_line(r, k, remote) for r, k in announced) + tuple(due_line(i, last) for i, last in due),
                  asks=worst != COMPLETED)


def failure_notice(job_name: str, reason: str, preferences: Notifications) -> Optional[Notice]:
    """The notice for an automatic run that could not start at all."""
    if not preferences.on_failure:
        return None
    return Notice(f"{job_name}: an automatic backup could not start", (reason,), asks=True)


def verification_notice(record: Optional[VerificationRecord], preferences: Notifications) -> Optional[Notice]:
    """The notice a verification's end calls for under the user's choices, or None."""
    if record is None:
        return None
    where = f"Destination {record.destination_index}"
    what = "verification" if record.full else "verification of new copies"
    if record.state is VerificationState.COMPLETED and record.differ_total:
        if not preferences.on_failure:
            return None
        n = record.differ_total
        verb = agreeing(n, "differs", "differ")
        example = record.differ[0][0].rsplit("\\", 1)[-1] if record.differ else ""
        return Notice(f"{record.job_name}: {what} found {_files(n)} that {verb}",
                      (f"{where}: {_files(n)} {verb} from the source although size and "
                       f"date match" + (f" (for example {example})" if example else ""),), asks=True)
    if record.state is VerificationState.COMPLETED:
        if not preferences.on_completion:
            return None
        line = f"{where}: {_files(record.compared_files)} compared ({_size(record.compared_bytes)})"
        if record.unchanged_files:
            line += f"; {_count(record.unchanged_files)} verified earlier and unchanged, not read"
        return Notice(f"{record.job_name}: {what} found no differences", (line,))
    if record.state is VerificationState.FAILED and preferences.on_failure:
        return Notice(f"{record.job_name}: a {what} did not finish",
                      (f"{where}: {record.detail or 'it failed'}; Verify continues it",), asks=True)
    return None


def _copies(n: int) -> str:
    return counted(n, "copy", "copies", separated=True)


def repair_notice(record: Optional[RepairRecord], preferences: Notifications) -> Optional[Notice]:
    """The notice a repair's end calls for under the user's choices, or None: every copy it
    took up replaced under the completion choice; any it failed to replace or left as it is,
    or a repair that failed, under the failure choice; a stopped one raises none."""
    if record is None or record.state is RepairState.STOPPED:
        return None
    head = f"{record.job_name}, destination {record.destination_index}: {_copies(len(record.replaced))} replaced"
    if record.state is RepairState.COMPLETED and not record.left:
        return Notice(head, ()) if preferences.on_completion else None
    if not preferences.on_failure:
        return None
    if record.state is RepairState.COMPLETED:
        failed = f", {_count(record.failures)} failed" if record.failures else ""
        left = f", {_count(record.left_as_is)} left" if record.left_as_is else ""
        return Notice(f"{head}{failed}{left} - see History", (), asks=True)
    return Notice(f"{head}; the repair did not finish - see History", (record.detail,) if record.detail else (),
                  asks=True)
