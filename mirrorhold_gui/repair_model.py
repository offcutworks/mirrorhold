"""Replacing the copies a verification found to differ, and what a repair did, in words. No toolkit.

A full verification reports the copies that differ from the source and repairs nothing: it
cannot tell which copy is damaged. *Replace backup copies...* — on a verification's details,
and in the right-click menu of a destination whose latest finished verification found
copies that differ — is the user saying the source's copies are the good ones. It asks one
question first (``replace_question``); the repair then runs as a core process of its own
(``session.start_repair``, ``mirrorhold.maintenance.repairer``), shows in the Runs tab, and
lands in History, whose row opens its details (``repair_details``). A copy a repair took up
ends replaced, failed — not copied, its new copy not read back identical, or the repair's
process ended before it was done with it — or left as it is, and every word here keeps
the three apart: a failure is never called a copy left as it is, nor a repair that
replaced nothing one that replaced copies. A verification's details
say which of its copies a repair has replaced since, and of the rest which failed, which
were left as they are and which no repair has reached yet (``repaired_line``). A remote's
new copies are checked as its verification compares them, not read back, and the words say so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Optional, Sequence, Tuple

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.maintenance.repair import (FAILURES, UNSETTLED_WORDS, RepairRecord, RepairState, effective_state,
                                           left_words, of_pass, pending, replaced_keys, unsettled)
from mirrorhold.maintenance.verification import VerificationRecord
from mirrorhold.planning.listing import path_key
from mirrorhold.wording import agreeing, counted

from .formatting import count, files, local_time, sentence
from .jobs_model import REPAIRING, Place, Root, place_under
from .preview_model import ListedFile

REPLACE_HINT = ("A file changed since the verification, gone from either side, or whose source copy lost the "
                "file-format signature its backup copy has, is left as it is.")


def _copies(n: int) -> str:
    return counted(n, "copy", "copies", separated=True)


@dataclass(frozen=True)
class ReplaceQuestion:
    title: str
    text: str
    versions: str = ""       # how long versions are kept, while the job's version cleanup is on
    unlisted: str = ""       # what the verification found beyond what it listed
    hint: str = REPLACE_HINT


def _confirmed(remote: bool) -> str:
    """How a replaced copy was found good."""
    return "each found identical to the source" if remote else "each read back identical to the source"


def replace_question(job_name: str, index: int, name: str, verification: VerificationRecord,
                     repairs: Sequence[RepairRecord], keep_days: Optional[int], *,
                     remote: bool = False) -> ReplaceQuestion:
    """The question before a repair of destination ``index`` starts: the copies of
    ``verification`` no repair replaced yet. ``keep_days``: the job's version cleanup, None
    while it is off; ``remote``: the destination is a remote's folder."""
    n = len(pending(verification, repairs))
    title = f"Replace backup copies: {job_name}, destination {index}" + (f' "{name}"' if name else "")
    text = (f"Replace the backup copies of {files(n)} that {agreeing(n, 'differs', 'differ')} from the source? "
            "Each backup copy moves into this destination's versions, the source's copy is copied in its place, "
            + ("and the new copy is checked against the source the way this destination is verified." if remote
               else "and both are read back to check they now match."))
    versions = (f"Versions are kept {counted(keep_days, 'day')}: if a source copy turns out to be the damaged one, "
                "take the backup copy out of versions before then.") if keep_days else ""
    listed, total = len(verification.differ), verification.differ_total
    unlisted = (f"The verification listed {count(listed)} of the {files(total)} that "
                f"{agreeing(total, 'differs', 'differ')}; verify again from the beginning afterwards to find the "
                "rest.") if total > listed else ""
    return ReplaceQuestion(title, text, versions, unlisted)


def _when(record: RepairRecord) -> str:
    """When a repair ended, or began while it has not."""
    return local_time(record.ended or record.started)


def _several(records: Sequence[RepairRecord]) -> str:
    """'2 repairs, the last 2026-10-17 14:20'."""
    return f"{counted(len(records), 'repair')}, the last {_when(records[-1])}"


def repaired_line(verification: VerificationRecord, repairs: Sequence[RepairRecord],
                  live: Collection[str] = (), *, remote: bool = False) -> str:
    """'Replaced 2026-10-17 14:20: 2 of these, each read back identical to the source; 1 failed
    and 1 left as it is (see their details in History); 2 not reached by a repair yet.', or ""
    when no repair took the pass's copies up. Where the pass's repairs replaced none it opens
    'Repair 2026-10-17 14:20: none of these replaced', never with "Replaced". The time is that
    of the last repair the opening speaks of — of those that replaced a copy of the pass, or of
    every repair of it where none did — and where they are several the line says so, the count
    being all of theirs: 'Replaced by 2 repairs, the last 2026-10-17 14:20: 3 of these, ...',
    '2 repairs, the last 2026-10-17 14:20: none of these replaced'. A copy not
    replaced counts as failed once any repair of the pass failed on it, or ended before it
    was done with it (``unsettled``) —
    its backup copy may lie in that repair's versions, whatever a later repair found — else
    as left as it is when a repair left it; one no repair names, a stop or a failure kept
    every repair from reaching, or a repair in progress has not finished with. ``live``: the
    activities in progress."""
    mine = of_pass(repairs, verification.verification_id)
    if not mine:
        return ""
    done = replaced_keys(repairs, verification.verification_id)
    listed = {path_key(path) for path, _size in verification.differ}
    replaced = sum(1 for path, _size in verification.differ if path_key(path) in done)
    replacers = [r for r in mine if any(path_key(path) in listed for path, _size in r.replaced)]
    failures = {path_key(path) for r in mine for path, reason, _detail in r.left if reason in FAILURES}
    failures |= {path_key(path) for r in mine for path, _size in unsettled(r, live)}
    reached = {path_key(path) for r in mine for path, _reason, _detail in r.left}
    todo = [path_key(path) for path, _size in pending(verification, repairs)]
    failed = sum(1 for key in todo if key in failures)
    left = sum(1 for key in todo if key in reached and key not in failures)
    unreached = len(todo) - failed - left
    if replaced:
        opening = f"Replaced by {_several(replacers)}" if len(replacers) > 1 else f"Replaced {_when(replacers[-1])}"
        text = f"{opening}: {count(replaced)} of these, {_confirmed(remote)}"
    else:
        opening = _several(mine) if len(mine) > 1 else f"Repair {_when(mine[-1])}"
        text = f"{opening}: none of these replaced"
    named = [f"{count(failed)} failed"] if failed else []
    if left:
        named.append(f"{count(left)} left as {agreeing(left, 'it is', 'they are')}")
    if named:
        text += f"; {' and '.join(named)} (see {agreeing(failed + left, 'its', 'their')} details in History)"
    if unreached:
        text += f"; {count(unreached)} not reached by a repair yet"
    return text + "."


def result_words(record: RepairRecord, live: Collection[str] = ()) -> str:
    """A repair's result as History shows it."""
    state = effective_state(record, live)
    if state is RepairState.RUNNING:
        return REPAIRING
    if state is RepairState.COMPLETED:
        text = "Copies replaced" if record.replaced else "No copy replaced"
        if record.failures:
            text += f", {count(record.failures)} failed"
        if record.left_as_is:
            text += f", {count(record.left_as_is)} left"
        return text
    return {RepairState.STOPPED: "Stopped", RepairState.FAILED: "Failed",
            RepairState.INTERRUPTED: "Interrupted"}[state]


def summary(record: RepairRecord, live: Collection[str] = (), *, remote: bool = False) -> str:
    """The first line of a repair's details: 'Replaced 2 of the 3 copies the verification of
    2026-10-17 14:02 found to differ; each read back identical to the source.' A repair that
    replaced none says so, never "Replaced 0": 'None of the 3 copies ... was replaced.', 'The
    copy ... was not replaced.', or 'No copy was replaced.' where it took none up."""
    replaced, taken = len(record.replaced), record.taken
    found = f"the verification of {local_time(record.verification_started)} found to differ"
    if replaced:
        text = f"Replaced {count(replaced)} of the {_copies(taken)} {found}; {_confirmed(remote)}."
    elif taken > 1:
        text = f"None of the {_copies(taken)} {found} was replaced."
    elif taken == 1:
        text = f"The copy {found} was not replaced."
    else:
        text = "No copy was replaced."
    state = effective_state(record, live)
    lead = {RepairState.RUNNING: "In progress: ", RepairState.STOPPED: "Stopped by you: ",
            RepairState.INTERRUPTED: "Interrupted (a shutdown, a crash or a closed console): ",
            RepairState.FAILED: f"Did not finish: {record.detail or 'it failed'}. "}.get(state, "")
    return lead + (text if not lead or lead.endswith(". ") else text[0].lower() + text[1:])


@dataclass(frozen=True)
class RepairDetails:
    title: str
    summary: str
    left: Tuple[ListedFile, ...]              # the copies left as they are, with why
    versions_path: Optional[Place] = None
    failed: Tuple[ListedFile, ...] = ()       # the copies it failed to replace, with why


def repair_details(record: RepairRecord, destination_root: Optional[Root], destination_name: str = "",
                   live: Collection[str] = ()) -> RepairDetails:
    title = (f"Repair {local_time(record.started)} - {record.job_name}, destination {record.destination_index}"
             + (f' ("{destination_name}")' if destination_name else ""))
    remote = isinstance(destination_root, RemoteEndpoint)
    failed = tuple(ListedFile(path, left_words(reason, detail, remote=remote)) for path, reason, detail in record.left
                   if reason in FAILURES) + tuple(ListedFile(path, UNSETTLED_WORDS) for path, _size in
                                                  unsettled(record, live))
    left = tuple(ListedFile(path, left_words(reason, detail, remote=remote)) for path, reason, detail in record.left
                 if reason not in FAILURES)
    batch = record.versions_batch
    path = place_under(destination_root, batch) if batch and destination_root is not None else None
    return RepairDetails(title, sentence(summary(record, live, remote=remote)), left, path, failed)
