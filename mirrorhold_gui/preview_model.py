"""The preview: running one off the window's thread, and reading its result in words. No toolkit.

``PreviewWorker`` runs the core's ``preview_job`` — of the destinations a selection
names, or of every one — on a thread of its own and reports
where it is; cancelling makes the preview's progress callback raise, which ends it
before a ticket is written — nothing has changed anywhere. The preview runs in the GUI
process, through the core's library seam: it only lists and reads, and its result (the
plan of every pair) is what the confirmation dialog shows.

``summarise`` turns a ``JobPreview`` into what the dialog shows: per destination, the
first line (ready, first run, adoption, held — with the reason — or why it cannot run),
the quantities (the old version batches the run will delete permanently among them,
when the job's cleanup is on), the estimate with its basis, the removals grouped by
folder with samples — a folder that moves into versions whole is one group, so a removed
tree reads by its own name rather than by its largest subfolder — the removals that
reappeared after being moved into versions grouped apart from the others, by the same
rule, with when they were moved (``PairSummary.back_groups``, ``back_line``), and the
lists behind each "Show all", and the job's notices beside the destination
(``PairSummary.notices``: a cloud client that keeps a copy of the folder and may put back
what a run moves into versions, or one whose folders could not be read); and for the
whole preview, which run buttons apply. Confirming applies exactly what the preview
listed: the run started with its ticket re-lists, and any removal not in the ticket is
held again.

A first run into a folder that already holds what the source does not offers two
answers (``PairSummary.choices``), each with the destination as it would run by it:
leave that where it is — the preview's own choice — or move it into versions, which is
shown but not offered where the folder holds one the core protects (``withheld``). When
the user answers otherwise than the preview did, ``ticket_to_run`` has the core write the
ticket that confirms the answer (``runner.ticket_for``); the run is started as always.
A shared destination whose record was lost is shown as such a first run with leaving its
only answer, moving shown but not offered: confirming it makes the record again.

Changed files of the same size on both sides are compared first; those found identical
only have their date updated and keep their copy, which the quantities and an adoption's
first line say. Source files whose names a run cannot keep are listed with the files
left out at the source.

A line under the source says how the run will read it (``reading_words``): as it is, and
why, where the job's choice or the drive's kind settles it; otherwise from a snapshot when
the run can make one as it starts. Whether Windows then lets it is the run's to find out:
the preview asks Windows nothing about snapshots.

A remote destination reads as a drive's does, but for what only a service can say: a remote
that could not be reached (the next run tries again), free space it does not report (not
judged), what it may keep of deleted old versions; and what a remote's notices say — that it
does not encrypt, above all — shows even when the destination cannot run.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from mirrorhold.endpoints.resolution import Presence
from mirrorhold.endpoints.volumes import FAT_MAX_FILE_BYTES
from mirrorhold.jobs.model import DEFAULT_SHARE
from mirrorhold.jobs.validation import REMOTE_UNREACHABLE
from mirrorhold.planning.estimate import Estimate, MiB
from mirrorhold.planning.plan import PairPlan, left_out
from mirrorhold.planning.sharing import Sharing
from mirrorhold.records.record import Outcome
from mirrorhold.records.status import Phase
from mirrorhold.run.confirmation import Content
from mirrorhold.run.gate import GateDecision, Hold, Stop, Verdict, share_words
from mirrorhold.run.put_back import PutBack
from mirrorhold.run.runner import (Alternative, JobPreview, PairPreview, PreviewProgress, preview_job, ticket_for,
                                   unreached_reason)
from mirrorhold.snapshots.reading import drive_name, settled_without_asking
from mirrorhold.wording import agreeing, counted

from .formatting import count, duration, files, sentence, size
from .jobs_model import Tone, describe_folder, describe_remote, live_reading_words, reason_words
from .session import GuiSession

SHOWN_FOLDERS = 3
SAMPLES = 2
REAPPEARED = "reappeared"
"""Why a removal that reappeared after being moved into versions is listed."""


class PreviewCancelled(Exception):
    """Raised inside a preview to end it."""


class WorkerState(str, Enum):
    WORKING = "working"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PreviewWorker:
    """One preview on a thread of its own, of the destinations whose markers are in
    ``destinations`` — found by marker in the job as the preview reads it, after the window
    has checked the job file still holds them (``GuiSession.check_chosen``) — or of every
    one. The window polls ``state`` and ``progress``."""

    def __init__(self, session: GuiSession, job_id: str, *, destinations: Optional[Sequence[str]] = None,
                 preview: Callable[..., JobPreview] = preview_job):
        self.session = session
        self.job_id = job_id
        self.destinations = tuple(destinations) if destinations is not None else None
        self._preview = preview
        self._cancel = threading.Event()
        self.state = WorkerState.WORKING
        self.progress = PreviewProgress(Phase.LISTING)
        self.result: Optional[JobPreview] = None
        self.error = ""
        self._thread = threading.Thread(target=self._work, name="mirrorhold-preview", daemon=True)

    def start(self) -> "PreviewWorker":
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def _report(self, progress: PreviewProgress) -> None:
        if self._cancel.is_set():
            raise PreviewCancelled()
        self.progress = progress

    def _work(self) -> None:
        try:
            if self.destinations is not None:
                self.session.check_chosen(self.job_id, self.destinations)
            self.result = self._preview(self.session.store, self.job_id, volume_source=self.session.volume_source(),
                                        sync_roots=self.session.sync_roots(), markers=self.destinations,
                                        progress=self._report)
            self.state = WorkerState.DONE
        except PreviewCancelled:
            self.state = WorkerState.CANCELLED
        except Exception as exc:  # shown in the dialog: a preview that fails says why
            self.error = f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc)
            self.state = WorkerState.FAILED


def progress_words(progress: PreviewProgress, preview_source: str, destinations: Sequence[str]) -> str:
    """'Listing the source  E:\\ "Media"', 'Listing destination 1 ...', 'Planning ...',
    'Measuring old versions of destination 1 ...'."""
    if progress.destination == 0:
        return f"Listing the source  {preview_source}"
    where = destinations[progress.destination - 1] if progress.destination <= len(destinations) else ""
    verb = {Phase.LISTING: "Listing", Phase.CLEANING: "Measuring old versions of"}.get(progress.phase, "Planning")
    return f"{verb} destination {progress.destination}  {where}".rstrip()


# --- the result in words ----------------------------------------------------------------

class PairState(str, Enum):
    READY = "ready"
    FIRST_RUN = "first run"
    ADOPTION = "adoption"
    HELD = "held"
    CANNOT_RUN = "cannot run"
    ABSENT = "absent"


@dataclass(frozen=True)
class ListedFile:
    path: str
    why: str
    size: Optional[int] = None


@dataclass(frozen=True)
class FolderGroup:
    folder: str                          # relative, with a trailing backslash; "" for the top level
    files: Tuple[ListedFile, ...]
    whole: bool = False                  # the folder itself moves into versions, every file beneath it

    @property
    def label(self) -> str:
        return self.folder or "(top level)"

    @property
    def samples(self) -> Tuple[str, ...]:
        return tuple(self.name(f) for f in self.files[:SAMPLES])

    @property
    def detail(self) -> str:
        """'whole folder deleted at the source', or 'for example a.txt, b.txt'."""
        if self.whole:
            return f"whole folder {self.files[0].why}"
        return "for example " + ", ".join(self.samples)

    def name(self, item: ListedFile) -> str:
        """A file's path within the group's folder: its name, or for a whole folder the
        subfolders beneath it too."""
        return "\\".join(item.path.split("\\")[self.folder.count("\\"):])


@dataclass(frozen=True)
class Quantity:
    label: str
    files: str
    size: str
    detail: str
    listing: str = ""                    # the key of the list "Details..." opens; "" for none


@dataclass(frozen=True)
class PairSummary:
    index: int
    title: str
    state: PairState
    headline: str
    tone: Tone
    quantities: Tuple[Quantity, ...] = ()
    time: str = ""
    groups: Tuple[FolderGroup, ...] = ()
    lists: Dict[str, Tuple[ListedFile, ...]] = field(default_factory=dict)
    removals: int = 0
    note: str = ""                       # beneath the quantities: names in the source that met what was there
    choices: Tuple["Choice", ...] = ()   # a first run into a folder holding what the source does not
    content: Optional[Content] = None    # the choice the preview planned, and its ticket confirms
    notices: Tuple[str, ...] = ()        # the job's notices beside this destination, to read before confirming
    back_groups: Tuple[FolderGroup, ...] = ()   # the removals that reappeared after being moved into versions
    back_line: str = ""                  # when those were moved, and what may have put them back

    @property
    def runs(self) -> bool:
        """Whether a run would do anything for this pair."""
        return self.state in (PairState.READY, PairState.FIRST_RUN, PairState.ADOPTION, PairState.HELD)

    @property
    def needs_review(self) -> bool:
        return self.state in (PairState.FIRST_RUN, PairState.ADOPTION, PairState.HELD)

    @property
    def hidden_groups(self) -> int:
        return max(0, len(self.groups) - SHOWN_FOLDERS)

    @property
    def hidden_back_groups(self) -> int:
        return max(0, len(self.back_groups) - SHOWN_FOLDERS)


@dataclass(frozen=True)
class Choice:
    """One answer to what a first run does with what its folder holds and the source does
    not, and the destination as it would run by that answer."""

    content: Content
    label: str
    detail: str
    offered: bool
    pair: PairSummary


@dataclass(frozen=True)
class Actions:
    """The preview's run buttons: which exist, which is primary, and why none can run."""

    review: bool                         # something needs the user's review
    apply_all: bool                      # "Run and apply everything shown"
    keep_held: bool                      # "Run, keep held removals waiting"
    run_now: bool                        # "Run now" (nothing needs review)
    blocked: str = ""                    # why no run can start
    first_run_waits: bool = False        # a first run does nothing under "keep held"


@dataclass(frozen=True)
class PreviewSummary:
    job_id: str
    source_line: str
    pairs: Tuple[PairSummary, ...]
    actions: Actions
    ticket_id: Optional[str] = None
    reading: str = ""                    # how the run will read the source; "" when nothing would run


def _title(item: PairPreview) -> str:
    pair = item.assessment
    remote = pair.destination.remote
    where = (describe_remote(remote) if remote is not None
             else describe_folder(pair.resolution) if pair.resolution is not None else "")
    return f"Destination {pair.index}" + (f" - {where}" if where else "")


def _groups(plan: PairPlan, first_run: bool, back: FrozenSet[str] = frozenset()) \
        -> Tuple[Tuple[FolderGroup, ...], Tuple[FolderGroup, ...]]:
    """The removals by folder: those that did not reappear, then those that reappeared after
    being moved into versions (``back``, their comparison keys), never in one group; at a
    first run, what the folder held that is not in the source, rather than what was deleted
    at it. A folder the run moves into versions whole (``PairPlan.deleted_dirs``) is one
    group of every file beneath it — split by whether its files reappeared, should only some
    have, and then neither part reads as the whole folder; any other removal is grouped by
    the folder that holds it."""
    gone = "not in the source" if first_run else "deleted at the source"
    moved = plan.moved_with_folders()
    by_folder: Dict[Tuple[str, bool], List[ListedFile]] = defaultdict(list)
    for entry in plan.deleted:
        folder = moved[entry.key] + "\\" if entry.key in moved else _folder(entry.path)
        again = entry.key in back
        by_folder[(folder, again)].append(ListedFile(entry.path, REAPPEARED if again else gone, entry.size))
    for change in plan.changed:
        by_folder[(_folder(change.destination.path), False)].append(
            ListedFile(change.destination.path, "replaced by a newer version", change.destination.size))
    others = {e.key for e in plan.deleted} - back
    whole = {(folder + "\\", again) for again, keys in ((False, others), (True, back))
             for folder in plan.whole_folders(keys, moved)}
    sides: Tuple[List[FolderGroup], List[FolderGroup]] = ([], [])
    for (folder, again), items in by_folder.items():
        sides[again].append(FolderGroup(folder, tuple(sorted(items, key=lambda f: f.path.casefold())),
                                        (folder, again) in whole))
    return tuple(tuple(sorted(side, key=lambda g: (-len(g.files), g.folder.casefold()))) for side in sides)


def _back_line(back: PutBack) -> str:
    """When the files that reappeared were moved into versions, in this computer's time, and
    the cloud client the destination's notice names as keeping a copy of it."""
    moment = back.moved.astimezone()
    line = f"These reappeared after being moved into versions on {moment:%Y-%m-%d} at {moment:%H:%M}"
    if back.keeper:
        return f"{line}: {back.keeper}, which keeps a copy of this folder, may have put them back."
    return f"{line}."


def _folder(path: str) -> str:
    return path.rsplit("\\", 1)[0] + "\\" if "\\" in path else ""


def _estimate_words(estimate: Optional[Estimate], remote: bool = False) -> str:
    if estimate is None:
        return ""
    rates = f"{estimate.low_rate / MiB:.0f}-{estimate.high_rate / MiB:.0f} MiB/s"
    if estimate.samples:
        basis = f"from this {'pair' if remote else 'drive pair'}'s last {counted(estimate.samples, 'run')}, {rates}"
    elif remote:
        basis = f"a stated default, {rates}; no earlier run on this pair to measure"
    else:
        basis = f"a default for a USB 3 hard disk, {rates}; no earlier run on this pair to measure"
    listing = f"listing took {duration(estimate.listing_seconds)} on top"
    if not estimate.bytes_to_copy:
        return f"Nothing to copy; {listing}"
    low, high = duration(estimate.shortest_seconds), duration(estimate.longest_seconds)
    span = low if low == high else f"{low} to {high}"
    return f"{span} to copy {size(estimate.bytes_to_copy)} ({basis}); {listing}"


def _hold_words(decision, plan: PairPlan, holds) -> str:
    parts = []
    for hold in decision.holds:
        if hold is Hold.ABOVE_THRESHOLD:
            parts.append(f"more files would move into versions than this job allows in one run (the larger of "
                         f"{files(holds.count_floor)} and {holds.percent:g} % of "
                         f"{count(plan.destination_files)} = {count(int(decision.threshold))})")
        elif hold is Hold.SHARE_OF_DESTINATION:
            own = "" if holds.share == DEFAULT_SHARE else f" (this job holds at {holds.share:g} %)"
            parts.append(f"{share_words(holds.share)} of this destination's files would move into versions{own}. "
                         "This is what an empty or wrong source looks like")
        elif hold is Hold.LOST_SIGNATURES:
            lost = sorted(plan.signature_losses)
            names = ", ".join(p.rsplit("\\", 1)[-1] for p in _spelled(plan, lost)[:SAMPLES])
            n = len(lost)
            parts.append(f"{counted(n, 'changed file', separated=True)} no longer {agreeing(n, 'starts', 'start')} "
                         f"like {agreeing(n, 'its', 'their')} file type (what encryption leaves), for example {names}")
        elif hold is Hold.NOT_CONFIRMED:
            parts.append("removals appeared that the confirmed preview did not list")
    return "REMOVALS HELD - " + "; ".join(parts) + "."


def _spelled(plan: PairPlan, keys: Sequence[str]) -> List[str]:
    """Destination paths as spelled on disk, for comparison keys of changed files."""
    spelled = {c.destination.key: c.destination.path for c in plan.changed}
    return [spelled.get(k, k) for k in keys]


def _cannot_run(item: PairPreview) -> PairSummary:
    pair = item.assessment
    title = _title(item)
    notices = tuple(_sentence(n) for n in item.notices) if pair.destination.remote is not None else ()
    if pair.outcome is Outcome.ABSENT:
        if "source-absent" in pair.reasons:
            return PairSummary(pair.index, title, PairState.ABSENT, "The source's drive is not attached.",
                               Tone.NORMAL, notices=notices)
        if REMOTE_UNREACHABLE in pair.reasons:
            why = unreached_reason(pair.detail)
            return PairSummary(pair.index, title, PairState.ABSENT, "Could not be reached - the next run tries again"
                               + (f" ({why})." if why else "."), Tone.NORMAL, notices=notices)
        return PairSummary(pair.index, title, PairState.ABSENT, "Not attached - waits until its drive is attached.",
                           Tone.NORMAL)
    reasons = pair.reasons or ("see the history",)
    if "volume-ambiguous" in reasons:
        # A pair is resolved only after its source, so no resolution means the source was the one.
        which = "destination" if pair.resolution is not None else "source"
        text = f"two attached drives match this {which} (a cloned disk?)"
    elif "marker-mismatch" in reasons:
        text = "this folder belongs to another job"
    else:
        text = reason_words(reasons[0])
        if pair.detail:
            text += f" ({pair.detail})"
    return PairSummary(pair.index, title, PairState.CANNOT_RUN, f"CANNOT RUN - {text}.", Tone.PROBLEM, notices=notices)


def _sentence(text: str) -> str:
    """A core message as a sentence: capitalised, ending in a period."""
    return sentence(text) + ("" if text.endswith(".") else ".")


def _names(items: Sequence[str]) -> str:
    """'Windows, Users, Program Files and 4 more'."""
    return ", ".join(items[:SHOWN_FOLDERS]) + (f" and {len(items) - SHOWN_FOLDERS} more"
                                               if len(items) > SHOWN_FOLDERS else "")


def _and(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _meeting_note(sharing: Sharing) -> str:
    """What happens where a source's top-level name met what the folder already held."""
    if not sharing.meeting:
        return ""
    names = [name for name, _ in sharing.meeting]
    many = len(names)
    left = sum(n for _, n in sharing.meeting)
    return (f"{_and(names)} {agreeing(many, 'is', 'are')} in the source and already here: the source's files are "
            f"backed up into {agreeing(many, 'it', 'them')}, the {files(left)} there that the source does not have "
            f"{agreeing(left, 'stays as it is', 'stay as they are')}, and what you put into "
            f"{agreeing(many, 'it', 'them')} yourself from now on moves into versions at the next run.")


LEAVE_DETAIL = ("None of them is moved or changed, now or by any later run, and neither is anything put beside this "
                "job's own folders and files later.")
REBUILD_NOTE = ("Files this job backed up here whose source copies are gone, if any, are among them: they stay as "
                "they are from now on.")     # the lost record cannot tell whether there are any


def _choice_headline(alternatives: Sequence[Alternative]) -> str:
    left = next(a.sharing for a in alternatives if a.content is Content.LEAVE)
    n = left.left_files
    what = f"{files(n)} there {agreeing(n, 'is', 'are')} not in the source" if n else \
        "folders or links there are not in the source"
    return f"FIRST RUN INTO A FOLDER THAT ALREADY HOLDS FILES OR FOLDERS - {what}. Choose what happens to them:"


def _choice(item: PairPreview, alternative: Alternative, holds) -> Choice:
    pair = item.assessment
    summary = _summary(item, alternative.plan, alternative.decision, alternative.sharing, holds)
    if alternative.content is Content.LEAVE:
        return Choice(Content.LEAVE, "Leave them where they are", LEAVE_DETAIL, True, summary)
    if pair.withheld:
        return Choice(Content.MOVE, "Move them into versions - not offered:", f"{pair.withheld}.", False, summary)
    return Choice(Content.MOVE, "Move them into versions",
                  "The folder becomes this job's alone: they move into versions now, and anything put there later "
                  "moves into versions at the next run.", True, summary)


def _rebuild(item: PairPreview, holds) -> PairSummary:
    """A shared destination whose record was lost: leaving what it holds, its only answer,
    makes the record again; moving is shown, not offered."""
    pair = item.assessment
    summary = _summary(item, pair.plan, pair.decision, pair.sharing, holds)
    n = pair.sharing.left_files if pair.sharing is not None else 0
    what = (f" {files(n)} there {agreeing(n, 'is', 'are')} not in the source." if n else
            " Folders or links there are not in the source." if pair.sharing is not None
            and pair.sharing.leaves_anything else "")
    leave = Choice(Content.LEAVE, "Leave them where they are", LEAVE_DETAIL + " " + REBUILD_NOTE, True, summary)
    move = Choice(Content.MOVE, "Move them into versions - not offered:", f"{pair.withheld}.", False, summary)
    return replace(summary, headline="THE RECORD OF WHAT THIS FOLDER KEEPS AS IT IS WAS LOST - its first run left "
                                     f"what the folder held where it is.{what}",
                   choices=(leave, move), content=Content.LEAVE)


def summarise_pair(item: PairPreview, holds) -> PairSummary:
    pair = item.assessment
    if pair.plan is None or pair.decision is None:
        return _cannot_run(item)
    if pair.rebuild:
        return _rebuild(item, holds)
    if not pair.alternatives:
        return _summary(item, pair.plan, pair.decision, pair.sharing, holds)
    choices = tuple(_choice(item, alternative, holds) for alternative in pair.alternatives)
    chosen = next(c for c in choices if c.content is pair.content)
    return replace(chosen.pair, headline=_choice_headline(pair.alternatives), choices=choices, content=pair.content)


def _summary(item: PairPreview, plan: PairPlan, decision: GateDecision, sharing: Optional[Sharing],
             holds) -> PairSummary:
    """The destination as it would run by ``plan`` and ``decision``; ``sharing``: the job's
    part of a shared destination, and what it leaves."""
    pair = item.assessment
    title = _title(item)
    remote = pair.destination.remote is not None
    lists: Dict[str, Tuple[ListedFile, ...]] = {}
    back = item.put_back.keys if item.put_back is not None else frozenset()
    groups, back_groups = _groups(plan, pair.first_run, back)
    lists["removals"] = tuple(f for g in groups + back_groups for f in g.files)
    lists["kept"] = tuple(ListedFile(e.path, "named so this tool cannot move it" if left_out(e.path) is not None
                                     else "beneath a source folder that could not be read", e.size)
                          for e in plan.kept)
    lists["skipped"] = tuple(ListedFile(s.path, s.detail or s.reason.value) for s in
                             tuple(plan.source_skipped) + tuple(plan.blocked) + tuple(plan.left_out))
    lists["lost"] = tuple(ListedFile(p, "no longer starts like its file type")
                          for p in _spelled(plan, sorted(plan.signature_losses)))
    too_large = [e for e in plan.new if e.size > FAT_MAX_FILE_BYTES] + \
                [c.source for c in plan.changed if c.source.size > FAT_MAX_FILE_BYTES]
    lists["too_large"] = tuple(ListedFile(e.path, "4 GiB or more", e.size) for e in too_large)

    if decision.verdict is Verdict.STOP:
        if decision.stop is Stop.FAT32_LARGE_FILE:
            headline = (f"CANNOT RUN - {files(len(too_large))} of 4 GiB or more cannot be stored on "
                        f"{pair.resolution.volume.file_system}.")
        else:
            headline = (f"CANNOT RUN - needs {size(decision.bytes_needed)} with its margin; "
                        f"{size(decision.free_bytes)} free.")
        state, tone = PairState.CANNOT_RUN, Tone.PROBLEM
    elif decision.verdict is Verdict.AWAIT_CONFIRMATION:
        if decision.adoption:
            gone, changed = len(plan.deleted), len(plan.changed)
            headline = (f"FIRST RUN INTO A FOLDER THAT ALREADY HOLDS FILES OR FOLDERS - confirming adopts it: "
                        f"{files(gone)} that {agreeing(gone, 'is', 'are')} not in the source "
                        f"{agreeing(gone, 'moves', 'move')} into versions; {files(changed)} "
                        f"{agreeing(changed, 'is', 'are')} replaced "
                        f"({agreeing(changed, 'its old copy', 'their old copies')} into versions).")
            if plan.same_size_changed:
                same = plan.same_size_changed
                headline += (f" Of these, {count(same)} {agreeing(same, 'has', 'have')} the same size as "
                             f"{agreeing(same, 'its', 'their')} copy: " +
                             agreeing(same, "it is compared first, and only has its date updated if found identical.",
                                      "they are compared first, and those found identical only have their date "
                                      "updated."))
            state = PairState.ADOPTION
        else:
            headline = ("FIRST RUN - nothing has been written to this folder yet. Confirm to start mirroring "
                        "here.")
            state = PairState.FIRST_RUN
        tone = Tone.ATTENTION
    elif decision.verdict is Verdict.ADDITIONS_ONLY:
        headline, state, tone = _hold_words(decision, plan, holds), PairState.HELD, Tone.ATTENTION
    elif plan.nothing_to_do and not plan.left_out:
        headline, state, tone = "Ready - nothing to copy or move; this destination matches the source.", \
            PairState.READY, Tone.NORMAL
    else:
        headline, state, tone = "Ready - nothing needs your review.", PairState.READY, Tone.NORMAL

    same_size = (f"; {count(plan.same_size_changed)} changed of the same size "
                 f"{agreeing(plan.same_size_changed, 'is', 'are')} compared first, and only dated where identical"
                 if plan.same_size_changed else "")
    if pair.first_run:
        into = ", ".join(([f"{count(len(plan.deleted))} not in the source"] if plan.deleted else [])
                         + [f"{count(len(plan.changed))} replaced by the source's version"])
    else:
        again = sum(len(g.files) for g in back_groups)
        into = (f"{count(len(plan.deleted))} deleted at the source"
                + (f" ({count(again)} of them {REAPPEARED})" if again else "")
                + f", {count(len(plan.changed))} replaced by a newer version")
    quantities = [
        Quantity("To copy", files(plan.files_to_copy), size(plan.bytes_to_copy),
                 f"{count(len(plan.new))} new, {count(len(plan.changed))} changed{same_size}"),
        Quantity("Into versions", files(plan.removal_count), "", into, "removals" if plan.removal_count else ""),
        Quantity("Renamed", files(len(plan.renames)), "", "recognised; nothing copied again"),
    ]
    note = ""
    if sharing is not None and pair.first_run and sharing.leaves_anything:
        lists["left"] = tuple(ListedFile(path, "not in the source; left as it is") for path in sharing.left_items)
        quantities.insert(0, Quantity("Left as they are", files(sharing.left_files), "", _names(sharing.left_items),
                                      "left"))
        note = _meeting_note(sharing)
    if plan.kept:
        unmovable = any(left_out(e.path) is not None for e in plan.kept)
        quantities.append(Quantity("Left as they are", files(len(plan.kept)), "",
                                   "beneath source folders that could not be read"
                                   + (", or named so this tool cannot move them" if unmovable else ""), "kept"))
    if lists["skipped"]:
        quantities.append(Quantity("Left out at the source", files(len(lists["skipped"])), "",
                                   "cloud-only files, links, entries that could not be read and names this tool cannot "
                                   "keep" if plan.left_out
                                   else "cloud-only files, links and entries that could not be read", "skipped"))
    if plan.temporaries:
        quantities.append(Quantity("Unfinished copies", files(len(plan.temporaries)), "",
                                   "left by an interrupted run; removed"))
    cleanup = pair.cleanup
    if cleanup is not None and cleanup.batches:
        quantities.append(Quantity("Old versions", counted(len(cleanup.batches), "batch", "batches", separated=True),
                                   size(cleanup.bytes),
                                   f"older than {counted(cleanup.keep_days, 'day')}: deleted permanently by this run"
                                   + ("; the service may keep them until it removes them" if cleanup.trash else "")))
    if remote and decision.free_bytes is None:
        quantities.append(Quantity("Free space", "", "", "the service reports none, so it is not checked"))
    if plan.signature_losses:
        quantities.append(Quantity("Lost their file type", files(len(plan.signature_losses)), "",
                                   "changed files that no longer start like their file type", "lost"))
    if decision.stop is Stop.FAT32_LARGE_FILE:
        quantities.append(Quantity("Too large for FAT32", files(len(too_large)), "", "4 GiB or more", "too_large"))
    return PairSummary(pair.index, title, state, headline, tone, tuple(quantities),
                       _estimate_words(item.estimate, remote),
                       groups, lists, plan.removal_count, note, notices=tuple(_sentence(n) for n in item.notices),
                       back_groups=back_groups, back_line=_back_line(item.put_back) if back_groups else "")


def ticket_to_run(session: GuiSession, preview: JobPreview, summary: "PreviewSummary",
                  chosen: Mapping[int, Content]) -> Optional[str]:
    """The ticket a run confirming ``summary`` takes, ``chosen`` naming each answer the user
    gave by destination index: the preview's own ticket when every answer is the one it
    planned, else one the core writes for the answers given (``runner.ticket_for``)."""
    planned = {p.index: p.content for p in summary.pairs if p.choices}
    changed = {index: content for index, content in chosen.items()
               if index in planned and planned[index] is not content}
    if not changed:
        return summary.ticket_id
    ticket = ticket_for(session.store, preview, changed)
    return ticket.ticket_id if ticket is not None else None


def summarise(preview: JobPreview) -> PreviewSummary:
    if preview.source is not None:
        where = describe_folder(preview.source)
        if preview.source_problem:
            source_line = f"Source {where} - {preview.source_problem}"
        else:
            source_line = (f"Source {where} - {files(preview.source_files)}, listed in "
                           f"{duration(preview.source_seconds)}")
    else:
        source_line = f"Source - {preview.source_problem}" if preview.source_problem else "Source"
    pairs = tuple(summarise_pair(item, preview.job.holds) for item in preview.pairs)
    review = any(p.needs_review for p in pairs)
    runnable = [p for p in pairs if p.runs]
    blocked = ""
    if not runnable:
        if preview.source is not None and preview.source.presence is Presence.ABSENT:
            blocked = "Nothing can run now: the source's drive is not attached."
        elif all(p.state is PairState.ABSENT for p in pairs):
            blocked = ("Nothing can run now: no destination's drive is attached or its remote reached."
                       if any(item.assessment.destination.remote is not None for item in preview.pairs)
                       else "Nothing can run now: no destination's drive is attached.")
        else:
            blocked = "Nothing can run now: see each destination above."
    keep_held = review and any(p.state in (PairState.READY, PairState.HELD) for p in pairs)
    actions = Actions(review=review, apply_all=review and bool(runnable) and preview.ticket is not None,
                      keep_held=keep_held, run_now=(not review) and bool(runnable), blocked=blocked,
                      first_run_waits=any(p.state in (PairState.FIRST_RUN, PairState.ADOPTION) for p in pairs))
    return PreviewSummary(preview.job.job_id, source_line, pairs, actions,
                          preview.ticket.ticket_id if preview.ticket else None, reading_words(preview))


def reading_words(preview: JobPreview) -> str:
    """How the run will read the source, as far as is known without asking Windows: as it
    is, and why, or from a snapshot when it can make one as it starts. "" while the
    source's drive is not attached, or no destination's drive is and no remote was reached."""
    source = preview.source
    if source is None or source.presence is not Presence.PRESENT:
        return ""
    if not any(item.assessment.storage is not None or (item.assessment.resolution is not None
                                                       and item.assessment.resolution.presence is Presence.PRESENT)
               for item in preview.pairs):
        return ""
    settled = settled_without_asking(preview.job, source.volume)
    if settled is None:
        return f"The run reads the source from a snapshot of {drive_name(source.volume)} when it can make one as it " \
               "starts."
    return f"The run reads the source as it is: {live_reading_words(settled.reason)}."
