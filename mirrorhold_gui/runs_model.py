"""The Runs tab: every run and verification in progress, in words, and what can be asked of it. No toolkit.

A run's status document (``mirrorhold.records.status``) is published by the run's own
process about twice a second, whoever started it; ``run_panel`` reads one into what a
row shows. The window asks a run to pause, resume or cancel only through
``records.status.request`` — the run reconciles — and never reaches the engine.

Files the engine compared and found identical count as done in a run's row, with their
bytes, beside the files copied; the row's note says how many there were, and while the
engine compares and copies nothing, its in-flight line names the files being compared.

A full verification publishes through the same channel (kind "verification") and shows
as a row of its own: files and bytes compared against its listing, the estimate it gave
when it began, the files found to differ so far; it can be paused, resumed and stopped,
and waits while a run it gave way to works. A repair of the copies a verification found
to differ (kind "repair") shows as a row of its own too: the copies copied of those it
replaces; it can be paused, resumed and stopped after the files in flight.

A waiting row names the job whose run or verification it waits for, and says whether
that one is paused or waits too (``mirrorhold.records.admission``) — or, for one whose
status the waiting one cannot read now, says so, naming it by the last status the waiting
one read of it where it read one; its note says the drive they share, when that alone is
why, and its place in line. An activity of this copy that names nothing it waits for is
starting when it has not yet made its first attempt at its turn, or has just been
admitted and not yet begun its first step (``admission.starting``); one that has asked
and names nothing — a verification giving way, or one whose admission could not be
published, each until its next attempt — waits for its turn. A status that names nothing
and says nothing of its turn — one published by an older copy of the tool — waits for
the run in progress, as that copy's runs did.

A live activity whose status cannot be read now has a row too, titled as the window last
read it (by its identifier where it read nothing): it holds its turn, so whatever asks
for its turn waits for it, and neither its progress nor a request can be shown or made
from here until it can be read again.

A row's title names its job, the destination it serves and where that lies; a
verification's and a repair's begin with what they are, and a run serving every one of
its job's several destinations says which of them it is on ("Photos -> 1 of 2"); a run
of some of them names the destination alone. The rows stand in line
order (``live_panels``): the activities at work or paused first, then the waiting ones
by their place in line, each group in the order the activities joined the line.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mirrorhold.jobs.model import Job
from mirrorhold.maintenance.verification import VerificationMode
from mirrorhold.records.admission import WaitedFor, line_words, starting
from mirrorhold.records.status import ANY_KIND, ANY_KINDS, KIND_REPAIR, KIND_VERIFICATION, UNREADABLE, CancelMode, \
    ObservedRun, Phase, is_repair, is_run, is_verification, line_order, place_of
from mirrorhold.wording import agreeing, counted

from .formatting import count, duration, files, joined, rate, size
from .session import LaunchProblem

STARTING = "starting"
"""A row's phase while its activity has not begun waiting for anything, nor begun its work."""

TURN = "waiting for its turn"
"""A row's phase while its activity has asked for its turn and names nothing it waits for."""

_BEFORE_PLAN = (Phase.SNAPSHOT.value, Phase.CLEANING.value, Phase.LISTING.value, Phase.PLANNING.value)
"""A run's steps before it has a plan: its counts would read 0 of 0."""


@dataclass(frozen=True)
class RunPanel:
    run_id: str
    title: str
    phase: str
    fraction: Optional[float]          # None: no measurable progress yet (an indeterminate bar)
    counts: str
    in_flight: str
    waiting: bool
    paused: bool
    cancelling: Optional[CancelMode]
    can_pause: bool
    can_resume: bool
    can_cancel: bool                   # the "Cancel..." button ("Stop..." for a verification or a repair)
    can_stop_now: bool                 # the "Stop now..." button of a graceful cancel in progress
    verification: bool = False
    note: str = ""                     # a verification's estimate; files found identical; why a waiting one waits
    repair: bool = False
    readable: bool = True              # False: its status cannot be read now, so no progress is shown


def pause_words(panel: RunPanel) -> str:
    """What a row's pause button, and the icon's entry for it, say."""
    return "Resume" if panel.paused else "Pause"


def can_ask_pause(panel: RunPanel) -> bool:
    return panel.can_pause or panel.can_resume


def cancel_words(panel: RunPanel) -> str:
    """What a row's cancel button, and the icon's entry for it, say: a verification or a
    repair is stopped, a graceful cancel in progress can be made to stop now."""
    return "Stop..." if panel.verification or panel.repair else "Stop now..." if panel.can_stop_now else "Cancel..."


def can_ask_cancel(panel: RunPanel) -> bool:
    return panel.can_cancel or panel.can_stop_now


def _where(status: dict) -> str:
    destination = status.get("destination") or ""
    return destination.split(" at ", 1)[1] if " at " in destination else ""


def whom(other: WaitedFor) -> str:
    """"'Photos'", "a verification of 'Photos'", "'Photos' (its status cannot be read now)", "a
    run, verification or repair whose status cannot be read now": the activity a waiting one
    waits for, as the Runs tab and the job list name it."""
    if not other.kind:
        return f"a {ANY_KIND} whose status cannot be read now"
    what = {KIND_VERIFICATION: f"a verification of '{other.job_name}'",
            KIND_REPAIR: f"a repair of '{other.job_name}'"}.get(other.kind, f"'{other.job_name}'")
    return what if other.readable else f"{what} ({UNREADABLE})"


def waited_for_words(status: dict) -> str:
    """"'Photos' (paused)", "a verification of 'Photos'": what a waiting row waits for, or ""
    when its status names nothing."""
    other = WaitedFor.from_status(status)
    if other is None:
        return ""
    return whom(other) + (" (paused)" if other.paused else " (waiting too)" if other.waiting else "")


def waiting_note(status: dict) -> str:
    """'Both use drive I:. 2nd in line.': a waiting row's note — the drive it shares with
    the one it waits for, when that alone is why, and its place in line; "" when its
    status names neither."""
    other = WaitedFor.from_status(status)
    parts = [f"Both use {other.drive}."] if other is not None and other.drive else []
    if place_of(status):
        parts.append(line_words(place_of(status)).capitalize() + ".")
    return " ".join(parts)


def waits(status: dict) -> bool:
    """Whether an activity waits for its turn: in the waiting phase and not starting."""
    return status.get("phase") == Phase.WAITING.value and not starting(status)


def _waiting_phase(status: dict) -> str:
    """A waiting row's phase: whom it waits for; starting; its turn, for one of this copy that
    has asked and names nothing; or, for an older copy's status naming nothing, the run in
    progress."""
    other = waited_for_words(status)
    if other:
        return f"waiting for {other}"
    if starting(status):
        return STARTING
    return TURN if isinstance(status.get("admitted"), bool) else "waiting for the run in progress"


def _entries(n: int) -> str:
    return counted(n, "entry", "entries", separated=True)


def _phase_words(status: dict) -> str:
    phase = status.get("phase")
    if phase == Phase.WAITING.value:
        return _waiting_phase(status)
    if phase == Phase.LISTING.value:
        side = "the source" if not status.get("pair_index") else "the destination"
        listed = int(status.get("listed") or 0)
        return f"listing {side}" + (f"   {_entries(listed)}" if listed else "")
    total = int(status.get("to_examine") or 0)
    if phase == Phase.PLANNING.value and total:
        return (f"planning   {count(int(status.get('examined') or 0))} of "
                f"{counted(total, 'changed file', separated=True)} read")
    if phase == Phase.CHECKING.value:
        words = "checking changed files can be read"
        return words + (f"   {count(int(status.get('examined') or 0))} of {count(total)}" if total else "")
    return {Phase.SNAPSHOT.value: "taking a snapshot of the source", Phase.PLANNING.value: "planning",
            Phase.CLEANING.value: "removing old versions",
            Phase.RENAMING.value: "renaming", Phase.MOVING.value: "moving into versions",
            Phase.COPYING.value: "copying", Phase.FINISHING.value: "finishing",
            Phase.RELEASING.value: "removing the snapshot"}.get(phase, str(phase or ""))


def in_flight_left(status: dict) -> str:
    """'2 files in flight, 1.8 GiB left' — what a graceful cancel waits for."""
    flying = status.get("in_flight") or []
    left = sum(max(0, int(total) - int(got)) for _, total, got in flying)
    if not flying:
        return "no file is being copied"
    return f"{files(len(flying))} in flight, {size(left)} left"


Places = Mapping[str, Mapping[str, int]]
"""Each job's destinations' numbers as the job stands now, by job and marker
(``destination_places``)."""

NO_PLACES: Places = {}


def destination_places(jobs: Iterable[Job]) -> Dict[str, Dict[str, int]]:
    """The number of each job's destinations, by job and marker: 1 for the first."""
    return {job.job_id: {d.marker_id: n for n, d in enumerate(job.destinations, 1)} for job in jobs}


def destination_number(status: dict, places: Places = NO_PLACES) -> Optional[int]:
    """The number of the destination an activity works on as its job stands now. An
    activity numbers its destinations as the job stood when it began, and an edit of the
    job while it works may move or remove one, so the number is found by the marker its
    status names; the status's own number where the job is not in ``places`` or no marker
    is named; None where it works on no destination yet, or on one the job no longer has."""
    pair = status.get("pair_index")
    if not pair:
        return None
    numbers = places.get(status.get("job_id"))
    marker = status.get("marker_id")
    if numbers is None or not marker:
        return pair
    return numbers.get(marker)


def of_total(status: dict, number: int) -> str:
    """' of 2' after the number of the destination a run is on, when the run serves every
    destination of its job and ``number`` is the one the run gave itself: the count is then
    the job's. Nothing for a run of some destinations, whose count is not the job's; for a
    job edited since the run began, whose numbers and count no longer agree; for a single
    destination; and for a verification or a repair."""
    total = status.get("pair_total")
    if not (is_run(status) and number == status.get("pair_index") and isinstance(total, int)
            and not isinstance(total, bool) and total > 1):
        return ""
    selected, markers = status.get("selected") or [], status.get("markers") or []
    if selected and len(selected) != len(markers):     # none selected: every one, as an older copy publishes
        return ""
    return f" of {total}"


def _title(st: dict, places: Places = NO_PLACES) -> str:
    """'Photos -> 1 of 2  F:\\Backups', 'Photos -> 3  F:\\Backups', 'Verification: Photos -> 1
    F:\\Backups', 'Verification (new copies): Photos -> 1  F:\\Backups', 'Repair: Photos -> 1' —
    the destination numbered as its job stands now (``destination_number``), and the job's
    count where it agrees (``of_total``)."""
    if is_verification(st):
        new_copies = (st.get("verify") or {}).get("mode") == VerificationMode.NEW_COPIES.value
        kind = "Verification (new copies): " if new_copies else "Verification: "
    else:
        kind = "Repair: " if is_repair(st) else ""
    title = f"{kind}{st.get('job_name', '')}"
    if st.get("pair_index"):
        number = destination_number(st, places)
        if number is not None:
            title += f" -> {number}{of_total(st, number)}"
        where = _where(st)
        if where:
            title += f"  {where}"
    return title


def _milliseconds(seconds: float) -> str:
    return f"{seconds * 1000:g} ms"


def estimate_words(estimate: Optional[dict]) -> str:
    """'Expected at start: 13 s to 1 min (default for a USB 3 hard disk, 30.0 MiB/s to 150.0
    MiB/s, 2.5 ms to 15 ms a file); listing took 0 s' — the cost of each file named when the
    estimate counted one."""
    if not estimate:
        return ""
    low, high = duration(estimate["shortest"]), duration(estimate["longest"])
    span = low if low == high else f"{low} to {high}"
    rates = f"{rate(estimate['low_rate'])} to {rate(estimate['high_rate'])}"
    if estimate.get("high_file_seconds"):
        rates += (f", {_milliseconds(estimate.get('low_file_seconds') or 0.0)} to "
                  f"{_milliseconds(estimate['high_file_seconds'])} a file")
    samples = int(estimate.get("samples") or 0)
    basis = (f"from this destination's last {counted(samples, 'verification')}, {rates}" if samples
             else f"default for a USB 3 hard disk, {rates}")
    if not estimate.get("bytes") and not estimate.get("files"):
        return f"Nothing to compare; listing took {duration(estimate['listing_seconds'])}"
    return f"Expected at start: {span} ({basis}); listing took {duration(estimate['listing_seconds'])}"


def verification_panel(observed: ObservedRun, places: Places = NO_PLACES) -> RunPanel:
    st = observed.status
    verify = st.get("verify") or {}
    phase = st.get("phase")
    waiting = phase == Phase.WAITING.value
    paused = bool(st.get("paused"))
    if waiting:
        other = waited_for_words(st)
        if verify.get("gave_way"):
            words = f"gave way, waiting for {other}" if other else "waiting: a backup runs first"
        else:
            words = _waiting_phase(st)
    elif paused:
        words = "paused"
    elif phase == Phase.LISTING.value:
        listed = int(st.get("listed") or 0)
        words = f"listing the {verify.get('listing') or 'source'}" + (f"   {_entries(listed)}" if listed else "")
    else:
        words = "verifying"
    total, done = int(verify.get("bytes") or 0), int(verify.get("bytes_done") or 0)
    verifying = phase == Phase.VERIFYING.value
    fraction = min(1.0, done / total) if verifying and total else None
    counts = ""
    if verifying or (paused and verify.get("files")):
        parts = [f"{count(int(verify.get('files_done') or 0))} of {files(int(verify.get('files') or 0))}",
                 f"{size(done)} of {size(total)}"]
        if verifying and not paused and st.get("rate"):
            parts.append(rate(float(st["rate"])))
            if st.get("eta_seconds") is not None:
                parts.append(f"about {duration(float(st['eta_seconds']))} left")
        if paused:
            parts.append("resumes where it stopped")
        counts = "   ".join(parts)
    differ = int(verify.get("differ") or 0)
    flight = ""
    if verifying or paused:
        flight = f"Differ so far: {files(differ)}" if differ else "No difference so far"
    live = observed.alive
    return RunPanel(observed.run_id, _title(st, places), words, fraction, counts, flight, waiting, paused, None,
                    can_pause=live and not waiting and not paused, can_resume=live and paused,
                    can_cancel=live, can_stop_now=False, verification=True,
                    note=waiting_note(st) if waiting else estimate_words(verify.get("estimate")))


def repair_panel(observed: ObservedRun, places: Places = NO_PLACES) -> RunPanel:
    st = observed.status
    phase = st.get("phase")
    waiting = phase == Phase.WAITING.value
    paused = bool(st.get("paused"))
    cancelling = CancelMode(st["cancelling"]) if st.get("cancelling") else None
    planned, done = st.get("planned") or {}, st.get("done") or {}
    total, copied = int(planned.get("files_to_copy", 0)), int(done.get("files_copied", 0))
    if waiting:
        words = _waiting_phase(st)
    elif cancelling is not None:
        words = "stopping after the files in flight"
    elif paused:
        words = "paused"
    else:
        words = "replacing copies"
    counts = f"{count(copied)} of {count(total)}" if total and not waiting else ""
    size_total = int(planned.get("bytes_to_copy", 0))
    copying = phase == Phase.COPYING.value
    fraction = min(1.0, int(done.get("bytes_copied", 0)) / size_total) if copying and size_total else None
    live = observed.alive
    return RunPanel(observed.run_id, _title(st, places), words, fraction, counts, "", waiting, paused, cancelling,
                    can_pause=live and not waiting and not paused and cancelling is None,
                    can_resume=live and paused and cancelling is None, can_cancel=live and cancelling is None,
                    can_stop_now=False, note=waiting_note(st) if waiting else "", repair=True)


def run_panel(observed: ObservedRun, places: Places = NO_PLACES) -> RunPanel:
    """The row of a live activity whose status was read, its destination numbered as
    ``places`` says its job stands now."""
    st = observed.status
    if is_verification(st):
        return verification_panel(observed, places)
    if is_repair(st):
        return repair_panel(observed, places)
    title = _title(st, places)
    waiting = st.get("phase") == Phase.WAITING.value
    paused = bool(st.get("paused"))
    cancelling = CancelMode(st["cancelling"]) if st.get("cancelling") else None
    planned, done = st.get("planned") or {}, st.get("done") or {}
    identical = int(done.get("files_identical", 0))
    files_done = int(done.get("files_copied", 0)) + identical
    total_bytes = int(planned.get("bytes_to_copy", 0))
    copied = int(done.get("bytes_copied", 0)) + int(done.get("bytes_identical", 0))
    copying = st.get("phase") == Phase.COPYING.value
    fraction = min(1.0, copied / total_bytes) if copying and total_bytes else None

    if cancelling is CancelMode.GRACEFUL:
        phase = "cancelling: finishing the files in flight"
    elif cancelling is CancelMode.IMMEDIATE:
        phase = "stopping now"
    elif paused:
        phase = "paused"
    else:
        phase = _phase_words(st)

    flying = st.get("in_flight") or []
    if waiting:
        counts = ""
    else:
        parts = [] if st.get("phase") in _BEFORE_PLAN else [
            f"{count(files_done)} of {files(int(planned.get('files_to_copy', 0)))}",
            f"{size(copied)} of {size(total_bytes)}"]
        if copying and not paused and st.get("rate"):
            parts.append(rate(float(st["rate"])))
            if st.get("eta_seconds") is not None:
                parts.append(f"about {duration(float(st['eta_seconds']))} left")
        if paused:
            parts.append("resumes where it stopped")
        counts = "   ".join(parts)
    if cancelling is CancelMode.GRACEFUL:
        counts = (f"Waiting for {in_flight_left(st)}; no new file starts" if flying
                  else "No file is being copied; stopping")

    in_flight = ""
    comparing = st.get("comparing") or []
    if flying and not waiting:
        shown = ", ".join(f"{str(name).rsplit(chr(92), 1)[-1]} ({_percent(got, total)})"
                          for name, total, got in flying[:3])
        more = f" and {len(flying) - 3} more" if len(flying) > 3 else ""
        in_flight = f"In flight: {shown}{more}"
    elif comparing and copying and not paused:
        shown = ", ".join(str(name).rsplit("\\", 1)[-1] for name in comparing[:3])
        more = f" and {len(comparing) - 3} more" if len(comparing) > 3 else ""
        in_flight = f"Comparing with the backup copy: {shown}{more}"

    live = observed.alive
    return RunPanel(observed.run_id, title, phase, fraction, counts, in_flight, waiting, paused, cancelling,
                    can_pause=live and not waiting and not paused and cancelling is None,
                    can_resume=live and paused and cancelling is None,
                    can_cancel=live and cancelling is None,
                    can_stop_now=live and cancelling is CancelMode.GRACEFUL,
                    note=waiting_note(st) if waiting else identical_words(identical))


def identical_words(files_identical: int) -> str:
    """'1,234 files were already identical: only their date was updated', or ""."""
    if not files_identical:
        return ""
    return (f"{files(files_identical)} {agreeing(files_identical, 'was', 'were')} already identical: only "
            f"{agreeing(files_identical, 'its', 'their')} date was updated")


def _percent(got, total) -> str:
    total = int(total)
    return f"{int(100 * int(got) / total)} %" if total else "0 %"


REQUEST_TITLES = {"pause": "Not paused", "resume": "Not resumed", "cancel": "Not cancelled", "stop": "Not stopped"}
"""The title of what the window says when a request did not reach its run, by what was asked."""


def not_reached_words(verification: bool, reason: str = "", *, repair: bool = False) -> str:
    """Why a request did not reach its run, verification or repair: another program kept its
    control file open past the patience, or ``reason``, the system's own."""
    what = "verification" if verification else "repair" if repair else "run"
    if reason:
        return f"The request did not reach the {what}: {reason}."
    return f"The request did not reach the {what}: another program kept its control file open. Try again."


def _counts(runs: int, verifications: int, repairs: int = 0) -> List[str]:
    """['2 runs', '1 verification']: the activities counted, each kind present named once."""
    return [counted(n, word) for n, word in ((runs, "run"), (verifications, "verification"), (repairs, "repair")) if n]


def _kinds(runs: int, verifications: int, repairs: int = 0) -> str:
    """'2 runs and 1 verification': the activities counted, each kind named once."""
    return joined(_counts(runs, verifications, repairs))


def kind_counts(runs: Sequence[ObservedRun]) -> Tuple[int, int, int, int]:
    """(runs, verifications, repairs, unknown): the live activities by kind, waiting ones
    included; one whose status cannot be read now by the last status the window read of it,
    ``unknown`` counting those it never read."""
    counts = [0, 0, 0, 0]
    for observed in runs:
        if not observed.alive:
            continue
        status = observed.status if observed.readable else observed.last
        if status is None:
            counts[3] += 1
        else:
            counts[1 if is_verification(status) else 2 if is_repair(status) else 0] += 1
    return counts[0], counts[1], counts[2], counts[3]


def closing_question(runs: int, verifications: int, repairs: int = 0, unknown: int = 0) -> str:
    """What closing the window asks while runs, verifications or repairs are in progress,
    waiting ones included; ``unknown``, those whose kind is not known — their status cannot
    be read now and the window never read it — said last, as "1 run, verification or repair
    whose status cannot be read now"."""
    total = runs + verifications + repairs + unknown
    parts = _counts(runs, verifications, repairs)
    if unknown:
        parts.append(f"{counted(unknown, ANY_KIND, ANY_KINDS)} whose status cannot be read now")
    return (f"{joined(parts)} {agreeing(total, 'is', 'are')} in progress. "
            f"{agreeing(total, 'It continues', 'They continue')} without this window; reopen MirrorHold to watch or "
            f"control {agreeing(total, 'it', 'them')}.")


def unreadable_words(runs: Sequence[ObservedRun]) -> str:
    """"the status of 1 repair cannot be read now": the status bar's words for the live
    activities whose status cannot be read now. Each is named by its kind as the window last
    read it, like the Runs tab's title and the closing question's count; one never read is a
    "run, verification or repair". "" when there is none."""
    counts = kind_counts([r for r in runs if not r.readable])
    parts = _counts(*counts[:3])
    if counts[3]:
        parts.append(counted(counts[3], ANY_KIND, ANY_KINDS))
    return f"the status of {joined(parts)} cannot be read now" if parts else ""


def not_started_words(problems: Sequence[LaunchProblem]) -> Tuple[str, str]:
    """(title, text) the window shows for the runs, verifications and repairs it started that
    ended without running, all in one: 'Run not started' for one, '3 runs not started' with a
    line per job for several, each line naming its kind when they are not all of one."""
    if len(problems) == 1:
        p = problems[0]
        return f"{p.what.capitalize()} not started", f"The {p.what} of \"{p.job_name}\" could not start: {p.message}"
    whats = [p.what for p in problems]
    title = _kinds(whats.count("run"), whats.count("verification"), whats.count("repair")) + " not started"
    mixed = len(set(whats)) > 1
    lines = [f"{p.job_name}" + (f" ({p.what})" if mixed else "") + f": {p.message}" for p in problems]
    return title, "\n".join(lines)


def unreadable_panel(observed: ObservedRun, places: Places = NO_PLACES) -> RunPanel:
    """The row of a live activity whose status cannot be read now: titled as the window last
    read it, by its identifier where it read nothing; no progress, and nothing to ask of it."""
    last = observed.last
    title = _title(last, places) if last else f"{ANY_KIND.capitalize()} {observed.run_id[:12]}"
    return RunPanel(observed.run_id, title, UNREADABLE, None, "", "", False, False, None, can_pause=False,
                    can_resume=False, can_cancel=False, can_stop_now=False,
                    verification=bool(last) and is_verification(last), repair=bool(last) and is_repair(last),
                    readable=False)


def live_panels(runs: Sequence[ObservedRun], places: Places = NO_PLACES) -> List[RunPanel]:
    """A panel per live run, in line order (``records.status.line_order``, the order the
    command line's ``status`` lists them in too), one whose status cannot be read now among
    those at work, as it holds its turn, each destination numbered as ``places`` says its job
    stands now. A run whose process has ended is left to the next run to record as
    interrupted."""
    return [run_panel(r, places) if r.readable else unreadable_panel(r, places)
            for r in sorted((r for r in runs if r.alive), key=line_order)]
