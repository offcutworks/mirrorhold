"""The Verify... dialog, a verification's place in the history, and its details, in words. No toolkit.

A full verification (``mirrorhold.maintenance``) reads both copies of every file of
one destination in full and compares their contents, to find a copy damaged on its
drive, which no run would ever copy again; a verification of new copies reads only the
copies no earlier pass found the same as they are now, the routine check after backups.
Each runs as a core process of its own, started by the window
(``session.start_verification``) and shown in the Runs tab. The dialog offers both, and
to continue the destination's unfinished pass; it says when that pass began — a pass that
continues counts as a verification from that day — and that the files it found to differ
stay listed if it continues, so the user can tell when starting again is the better
choice, and that new copies are read in full where no list of verified copies is kept.
It selects at first what the destination needs: continuing, else a full verification
when one is due or nothing is kept to build on, else the new copies. A remote's copies are
compared without being read wherever the service allows (``maintenance.compare``), which the
dialog says, and a finished pass's details say how they were.

What a verification found is reported, never repaired by the verification: it cannot tell
which copy is the damaged one, so the details say so and offer the user's own answer —
replacing the backup copies with the source's (``repair_model``) — and say which of the
copies a repair has replaced since.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Optional, Sequence, Tuple

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.maintenance.repair import RepairRecord
from mirrorhold.maintenance.verification import Measure, VerificationMode, VerificationRecord, VerificationState, \
    effective_state
from mirrorhold.wording import agreeing, counted

from .formatting import count, duration, files, local_time, size
from .jobs_model import DestinationRow, Place, Root, place_under, verified_words
from .preview_model import ListedFile
from .repair_model import repaired_line

INTRO = ("Compares the backup's copies with the source's, reading both in full, to find copies damaged on a drive. "
         "It changes nothing on either drive.")
INTRO_REMOTE = ("Compares the backup's copies with the source's by the fingerprints the service keeps, reading both "
                "copies where it keeps none. It changes nothing on either side.")
GIVES_WAY = "A backup that is due runs first: the verification steps aside and continues."
NEW_COPIES = "New copies only: the files backed up or changed since they were last verified"
FULL = "Full verification: every file, to find copies damaged on the drive since they were written"
FULL_REMOTE = "Full verification: every file, to find copies damaged on the service since they were written"
NO_LIST = ("No list of verified copies is kept for this destination yet: this time every file is read, and it "
           "counts as a full verification.")
ADVICE = ("MirrorHold cannot tell which copy is damaged. If the source's copies are the good ones, replace the "
          "backup's: each backup copy moves into versions first.")

CONTINUE, CHOOSE_NEW, CHOOSE_FULL = "continue", "new", "full"
"""The dialog's choices."""


@dataclass(frozen=True)
class VerifyDialog:
    title: str
    intro: str
    last: str
    resume: str             # the choice to continue the unfinished pass; "" when there is none
    note: str
    resume_note: str = ""   # when that pass began, and what continuing it keeps
    why: str = ""           # why that pass did not finish, when it failed
    new_last: str = ""      # when its new copies were last verified, when that was after its last full verification
    new_copies: str = NEW_COPIES
    new_copies_note: str = ""   # that every file is read, where no list of verified copies is kept
    full: str = FULL
    choice: str = CHOOSE_NEW    # selected at first


def _kind(record: VerificationRecord) -> str:
    return "verification" if record.full else "verification of new copies"


def verify_dialog(job_name: str, row: DestinationRow, reminder_days: int, unfinished: Optional[VerificationRecord],
                  *, list_kept: bool = False) -> VerifyDialog:
    """``unfinished``: the destination's pass to continue (``verification.unfinished``);
    ``list_kept``: whether a list of verified copies is kept for it to build on."""
    title = f"Verify: {job_name}, destination {row.index}" + (f' "{row.name}"' if row.name else "")
    last = row.verified.replace(" - due", "")
    last_text = f"Last full verification: {last}" + (", due now" if row.verification_due else "")
    last_text += f". Reminder every {counted(reminder_days, 'day')}."
    latest = row.last_verification
    new_last = (f"New copies last verified: {verified_words(latest.started, False)}."
                if latest is not None and not latest.full else "")
    resume = resume_note = why = ""
    if unfinished is not None and unfinished.state is VerificationState.FAILED and unfinished.detail:
        why = f"Why it did not finish: {unfinished.detail}"
    if unfinished is not None:
        how = {VerificationState.STOPPED: "stopped", VerificationState.FAILED: "that did not finish"}.get(
            unfinished.state, "interrupted")
        kind = "full verification" if unfinished.full else "verification of new copies"
        resume = (f"Continue the {kind} {how} {local_time(unfinished.updated)} "
                  f"({count(unfinished.compared_files)} of {files(unfinished.files_total)})")
        resume_note = f"It began {local_time(unfinished.started)}; continued, it counts as a verification from then."
        differ = unfinished.differ_total
        if differ:
            resume_note += (f" The {files(differ)} it found to differ {agreeing(differ, 'stays', 'stay')} listed; "
                            f"start again once you have dealt with {agreeing(differ, 'it', 'them')}.")
    if unfinished is not None:
        choice = CONTINUE
    elif row.verification_due or not list_kept:
        choice = CHOOSE_FULL
    else:
        choice = CHOOSE_NEW
    remote = row.remote is not None
    return VerifyDialog(title, INTRO_REMOTE if remote else INTRO, last_text, resume, GIVES_WAY, resume_note, why,
                        new_last, new_copies_note="" if list_kept else NO_LIST, full=FULL_REMOTE if remote else FULL,
                        choice=choice)


def verify_request(choice: str, unfinished: Optional[VerificationRecord], *,
                   list_kept: bool = True) -> Tuple[bool, bool]:
    """(new copies, restart): what the dialog's ``choice`` starts. Continuing takes the
    unfinished pass up as it is; either other choice begins a pass of its kind from the
    beginning when the pass offered for continuing is of that kind, leaving one of the other
    kind to be continued. New copies where no list is kept (``list_kept`` False) are a full
    verification from the beginning, as the dialog says: every file is read."""
    if choice == CONTINUE and unfinished is not None:
        return not unfinished.full, False
    new_copies = choice == CHOOSE_NEW and list_kept
    kind = VerificationMode.NEW_COPIES if new_copies else VerificationMode.FULL
    return new_copies, unfinished is not None and unfinished.mode is kind


_STATE_WORDS = {VerificationState.STOPPED: "stopped", VerificationState.INTERRUPTED: "interrupted",
                VerificationState.FAILED: "failed"}


def result_words(record: VerificationRecord, live: Collection[str] = ()) -> str:
    state = effective_state(record, live)
    if state is VerificationState.COMPLETED:
        lead = "Verified" if record.full else "New copies verified"
        if not record.differ_total:
            return f"{lead}: no differences"
        return f"{lead}: {count(record.differ_total)} {agreeing(record.differ_total, 'differs', 'differ')}"
    if state is VerificationState.RUNNING:
        return "Verifying" if record.full else "Verifying new copies"
    return f"{_kind(record).capitalize()} {_STATE_WORDS[state]}"


def not_compared_words(record: VerificationRecord) -> str:
    """'Not compared: 12 changed since the last run (the next run copies them); ...' or ""."""
    parts = []
    if record.changed_total:
        parts.append(f"{count(record.changed_total)} changed since the last run (the next run copies "
                     f"{agreeing(record.changed_total, 'it', 'them')})")
    if record.missing_total:
        parts.append(f"{count(record.missing_total)} not backed up yet")
    if record.unreadable_total:
        parts.append(f"{count(record.unreadable_total)} could not be read")
    text = "Not compared: " + "; ".join(parts) + "." if parts else ""
    if record.extra_total:
        text += (" " if text else "") + (f"{files(record.extra_total)} only on the destination: the next run moves "
                                         f"{agreeing(record.extra_total, 'it', 'them')} into versions.")
    return text


@dataclass(frozen=True)
class VerificationDetails:
    title: str
    summary: str
    differ_line: str
    differ: Tuple[ListedFile, ...]
    differ_total: int
    advice: str
    not_compared: str
    unreadable: Tuple[ListedFile, ...] = ()
    unreadable_total: int = 0
    destination_path: Optional[Place] = None
    repaired: str = ""                  # which of the copies that differ a repair replaced since


def _new_copies(n: int) -> str:
    return counted(n, "new copy", "new copies", separated=True)


def _compared_how(record: VerificationRecord) -> str:
    """How a remote's copies were compared, as a clause of the first line; "" for a pass that
    read them, as every pass of a drive does."""
    kind = record.fingerprint.upper() + " " if record.fingerprint else ""
    text = ""
    if record.measure is Measure.FINGERPRINT:
        text = f"; by their {kind}fingerprints, nothing downloaded"
    elif record.measure is Measure.ENCRYPTED:
        text = f"; by their encrypted form against the service's {kind}fingerprints"
    n = record.read_files
    if n:
        text += (f"; {files(n)} read in full ({size(record.read_bytes)}): the service kept no fingerprint for "
                 f"{agreeing(n, 'it', 'them')}")
    return text


def verification_summary(record: VerificationRecord, live: Collection[str] = ()) -> str:
    """The first line of a verification's details."""
    state = effective_state(record, live)
    what = files(record.compared_files) if record.full else _new_copies(record.compared_files)
    if state is VerificationState.COMPLETED:
        text = f"Finished: {what} compared ({size(record.compared_bytes)}) in {duration(record.seconds)}"
        if record.sittings > 1:
            text += f", over {counted(record.sittings, 'sitting')}"
        if record.unchanged_files:
            text += f"; {count(record.unchanged_files)} verified earlier and unchanged, not read"
        return text + _compared_how(record) + "."
    total = files(record.files_total) if record.full else _new_copies(record.files_total)
    progress = f"{count(record.compared_files)} of {total} compared"
    if state is VerificationState.RUNNING:
        return f"In progress: {progress}."
    lead = {VerificationState.STOPPED: "Stopped by you",
            VerificationState.INTERRUPTED: "Interrupted (a shutdown, a crash or a closed console)",
            VerificationState.FAILED: f"Did not finish: {record.detail or 'it failed'}"}[state]
    return f"{lead}: {progress}; Verify... continues from there."


def verification_details(record: VerificationRecord, destination_root: Optional[Root], destination_name: str = "",
                         live: Collection[str] = (), repairs: Sequence[RepairRecord] = ()) -> VerificationDetails:
    """``repairs``: the job's repair records, from which the details say what was replaced since."""
    title = (f"{_kind(record).capitalize()} {local_time(record.started)} - {record.job_name}, "
             f"destination {record.destination_index}" + (f' ("{destination_name}")' if destination_name else ""))
    differ = tuple(ListedFile(path, "differs from the source", size) for path, size in record.differ)
    if record.differ_total:
        n = record.differ_total
        line = (f"{files(n)} {agreeing(n, 'differs', 'differ')} from the source although "
                f"{agreeing(n, 'its', 'their')} size and date are the same:")
    elif record.compared_files:
        line = "No file differs from the source."
    else:
        line = ""
    unreadable = tuple(ListedFile(path, why) for path, why in record.unreadable)
    place = place_under(destination_root) if destination_root is not None else None
    remote = isinstance(destination_root, RemoteEndpoint)
    return VerificationDetails(title, verification_summary(record, live), line, differ, record.differ_total,
                               ADVICE if record.differ_total else "", not_compared_words(record), unreadable,
                               record.unreadable_total, place, repaired_line(record, repairs, live, remote=remote))
