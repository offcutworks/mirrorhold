"""Groups of jobs, and acting on several jobs at once, in words. No toolkit.

A group is a name the user keeps jobs together under (``mirrorhold.jobs.model.Group``,
held by the store beside the jobs); it changes nothing about how any of them runs. The
job list shows each group above its jobs (``jobs_model.arrange``), and what is selected
there decides what the window offers (``Selection``):

- one job, or one of its destinations: the job's and the destination's own actions;
- one group: run every job in it, verify its jobs' destinations, rename it, delete it
  (its jobs stay, in no group);
- several items: run every job they reach, verify every destination they reach, and
  remove them when every item is a job.

*Run now* runs what the selection names (``Selection.chosen``): a job whose own row or
whose group's row is selected runs whole, and a job reached only through rows of its
destinations runs those destinations alone — so a destination selected is run, never its
job's others. A run of some destinations sees the job through them (``JobRow.serving``):
the first backups it confirms and the reviews it holds are theirs.

Running several jobs asks once first (``run_several``): which start, and of a job run
for some destinations which, which run already and are left alone, which first backups
into folders that hold nothing yet the answer confirms, and which need the user's review
— their run holds what waits for a confirmation, as *Run now* on one job does. They
start together and are admitted like any run: those that conflict wait their turn, and
so do those using one drive unless the preferences let them share its speed, as the
question says.

*Verify...* reaches a selected destination itself and every destination of a selected
job or group (``Selection.destinations``). One destination reached is the Verify dialog's
own (``verify_model``); several ask once first (``verify_several``), answered with their
new copies only or a full verification: which are verified, which have no list of verified
copies to build on and are read in full either way, whose unfinished pass each answer
continues, which cannot be verified now and why, and what the choice for one drive means.
Each is a verification of its own, started together and admitted like any; an unfinished
pass of the kind chosen is always continued here — starting one again from the beginning
is the one destination's dialog's choice.

A group, or a job within its group or among the jobs in no group, moves up or down one
place at a time (``reordered``): the order is the job file's (``StoreContents.in_order``),
so the window, the command line and every copy read it alike. Groups stay above the jobs
in no group, and a job leaves its group only through *Move to group*.

Which groups the user closed in the list is the GUI's own remembered state
(``Preferences.gui_state``), kept under one key (``collapsed_groups``, ``with_collapsed``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Collection, Dict, Mapping, Optional, Sequence, Set, Tuple

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.marker import MarkerState
from mirrorhold.jobs.model import MAX_NAME_LENGTH, DriveTurns
from mirrorhold.jobs.store import StoreContents
from mirrorhold.wording import agreeing, counted

from .formatting import joined
from .jobs_model import DestinationRow, GroupRow, JobRow
from .verify_model import GIVES_WAY

COLLAPSED_KEY = "collapsed_groups"
"""The key of the GUI's remembered state that lists the groups closed in the job list."""

NEW_GROUP_NOTE = "Right-click jobs to move them into a group."

SHOWN_NAMES = 10


@dataclass(frozen=True)
class Selection:
    """What is selected in the job list, in the list's order, every job it reaches, and
    every destination it reaches."""

    items: Tuple[object, ...] = ()
    reached: Tuple[JobRow, ...] = ()
    destinations: Tuple[DestinationRow, ...] = ()   # a selected destination; a selected job's or group's every one

    @classmethod
    def of(cls, items: Sequence[object], by_iid: Mapping[str, object]) -> "Selection":
        """``items``: the selected rows; ``by_iid``: every row of the list by its identifier,
        in the list's order, through which a destination reaches its job."""
        reached: Dict[str, JobRow] = {}
        targets: Dict[str, DestinationRow] = {}
        for item in items:
            if isinstance(item, GroupRow):
                jobs: Tuple[JobRow, ...] = item.jobs
                rows: Tuple[DestinationRow, ...] = tuple(d for job in item.jobs for d in job.destinations)
            elif isinstance(item, JobRow):
                jobs, rows = (item,), item.destinations
            elif isinstance(item, DestinationRow):
                job = by_iid.get(f"job:{item.job_id}")
                jobs, rows = ((job,) if isinstance(job, JobRow) else ()), (item,)
            else:
                jobs, rows = (), ()
            for job in jobs:
                reached.setdefault(job.job_id, job)
            for row in rows:
                targets.setdefault(row.iid, row)
        order = {iid: n for n, iid in enumerate(by_iid)}
        destinations = tuple(sorted(targets.values(), key=lambda d: order.get(d.iid, len(order))))
        return cls(tuple(items), tuple(reached.values()), destinations)

    @property
    def single_group(self) -> Optional[GroupRow]:
        item = self.items[0] if len(self.items) == 1 else None
        return item if isinstance(item, GroupRow) else None

    @property
    def single_destination(self) -> Optional[DestinationRow]:
        item = self.items[0] if len(self.items) == 1 else None
        return item if isinstance(item, DestinationRow) else None

    @property
    def single_job(self) -> Optional[JobRow]:
        """The job of a selection of one job, or of one of its destinations."""
        if len(self.items) == 1 and isinstance(self.items[0], (JobRow, DestinationRow)) and self.reached:
            return self.reached[0]
        return None

    @property
    def startable(self) -> Tuple[JobRow, ...]:
        """The jobs *Run now* starts: every job reached that is not running."""
        return tuple(job for job in self.reached if not job.running)

    @property
    def chosen(self) -> Dict[str, Tuple[str, ...]]:
        """The destinations *Run now* runs of each job reached only through rows of its
        destinations, by job: their markers, in the job's order — the destination itself,
        whatever number the job gives it when the run starts. A job whose own row or whose
        group's row is selected is not here: it runs whole."""
        whole = {job.job_id for item in self.items if isinstance(item, GroupRow) for job in item.jobs}
        whole |= {item.job_id for item in self.items if isinstance(item, JobRow)}
        chosen: Dict[str, Tuple[str, ...]] = {}
        for destination in self.destinations:
            if destination.job_id not in whole:
                chosen[destination.job_id] = chosen.get(destination.job_id, ()) + (destination.marker_id,)
        return chosen

    @property
    def verifiable(self) -> Tuple[DestinationRow, ...]:
        """The destinations *Verify...* can start now: every destination reached that can be
        verified."""
        return tuple(d for d in self.destinations if d.can_verify)

    @property
    def removable(self) -> Tuple[JobRow, ...]:
        """The jobs *Remove...* removes at once: several items, every one a job."""
        if len(self.items) > 1 and all(isinstance(item, JobRow) for item in self.items):
            return tuple(self.items)
        return ()

    @property
    def movable(self) -> Tuple[str, ...]:
        """The jobs *Move to group* moves: every job reached, unless a group is selected."""
        if any(isinstance(item, GroupRow) for item in self.items):
            return ()
        return tuple(job.job_id for job in self.reached)


def reordered(contents: StoreContents, selection: Selection, step: int) -> Optional[StoreContents]:
    """The job file with the selected group, or the selected job (itself or through one of
    its destinations), moved ``step`` places within its level; None when the selection is
    anything else or nothing lies that way."""
    try:
        if selection.single_group is not None:
            return contents.with_group_moved(selection.single_group.group_id, step)
        if selection.single_job is not None:
            return contents.with_job_moved(selection.single_job.job_id, step)
    except ContractError:
        pass
    return None


def _names_of(names: Sequence[str]) -> str:
    """'A, B, C', the first SHOWN_NAMES then 'and 3 more'."""
    shown = ", ".join(names[:SHOWN_NAMES])
    more = len(names) - SHOWN_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


def _names(jobs: Sequence[JobRow]) -> str:
    return _names_of([job.name for job in jobs])


def _jobs(n: int) -> str:
    return counted(n, "job")


@dataclass(frozen=True)
class RunSeveral:
    """The question *Run now* asks before it starts several jobs, and what it starts."""

    title: str
    text: str
    button: str
    start: Tuple[str, ...]          # job identifiers, in the list's order
    first_backup: Tuple[str, ...] = ()   # those started with the go-ahead for their first backups
    destinations: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)   # those started for some destinations:
                                                                                # their markers; the rest run whole


def _first_backups(jobs: Sequence[JobRow]) -> str:
    """'Photos (E:\\Backups\\Photos), Music (F:\\)', the first SHOWN_NAMES jobs."""
    shown = ", ".join(f"{job.name} ({', '.join(d.folder for d in job.first_backups)})" for job in jobs[:SHOWN_NAMES])
    more = len(jobs) - SHOWN_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


def _on_one_drive(turns: DriveTurns, what: str) -> str:
    """What the question for several says of ``what`` ("jobs", "verifications") sharing a
    drive, under the preference for activities on one drive."""
    if turns is DriveTurns.AT_THE_SAME_TIME:
        return f"They run at the same time; {what} that use the same drive share its speed."
    if turns is DriveTurns.ONE_AT_A_TIME:
        return f"{what.capitalize()} that use the same drive run one at a time; the others run at the same time."
    return (f"{what.capitalize()} that use the same drive run one at a time unless it is a solid-state or cloud "
            f"drive, as Windows reports or as set in Preferences; the others run at the same time.")


def _run_name(job: JobRow, chosen: Mapping[str, Tuple[str, ...]]) -> str:
    """'Photos', 'Recipes (destination 4)', 'Recipes (destinations 2 and 3)': a job as the question
    names what starts of it, ``job`` as the run sees it (``JobRow.serving``)."""
    if job.job_id not in chosen:
        return job.name
    indexes = [d.index for d in job.destinations]
    which = ("destination " if len(indexes) == 1 else "destinations ") + joined([str(i) for i in indexes])
    return f"{job.name} ({which})"


def run_several(jobs: Sequence[JobRow], group: Optional[str] = None, *,
                turns: DriveTurns = DriveTurns.AUTOMATIC,
                chosen: Optional[Mapping[str, Tuple[str, ...]]] = None) -> RunSeveral:
    """``jobs``: every job reached, in the list's order; ``group``: the group's name when
    one group is selected; ``turns``: the preference for activities on one drive, which
    decides what the question says of jobs sharing one; ``chosen``: the destinations run of
    each job run for some (``Selection.chosen``), every other job running whole."""
    chosen = chosen or {}
    jobs = [job.serving(chosen.get(job.job_id)) for job in jobs]
    start = [job for job in jobs if not job.running]
    running = [job for job in jobs if job.running]
    firsts = [job for job in start if job.first_backups]
    review = [job for job in start if job.needs_other_review]
    title = f'Run {_jobs(len(start))} of "{group}"?' if group else f"Run {_jobs(len(start))}?"
    lines = [f"Starts: {_names_of([_run_name(job, chosen) for job in start])}."]
    if running:
        lines.append(f"Running already, not started again: {_names(running)}.")
    if firsts:
        lines.append(f"First backups, into folders that hold nothing yet: {_first_backups(firsts)}. "
                     "Starting confirms them.")
    if review:
        lines.append(f"{_names(review)} {agreeing(len(review), 'needs', 'need')} your review: "
                     f"{agreeing(len(review), 'its run holds', 'their runs hold')} what waits for your confirmation.")
    if len(start) > 1:
        lines.append(_on_one_drive(turns, "jobs"))
    return RunSeveral(title, "\n".join(lines), f"Run {_jobs(len(start))}", tuple(job.job_id for job in start),
                      tuple(job.job_id for job in firsts),
                      {job.job_id: chosen[job.job_id] for job in start if job.job_id in chosen})


@dataclass(frozen=True)
class VerifySeveral:
    """The question *Verify...* asks before it starts verifications of several destinations,
    and what it starts."""

    title: str
    text: str
    start: Tuple[Tuple[str, int], ...]   # (job identifier, destination index), in the list's order
    new_copies_button: str = "New copies only"
    full_button: str = "Full verification"
    full_default: bool = False           # the full verification is the default answer: one is due, or a
                                         # destination has no list of verified copies to build on


def _destinations(n: int) -> str:
    return counted(n, "destination")


def _and(names: Sequence[str]) -> str:
    """'A', 'A and B', 'A, B and C', the first SHOWN_NAMES then 'and 3 more'."""
    more = len(names) - SHOWN_NAMES
    return joined(list(names[:SHOWN_NAMES]) + ([f"{more} more"] if more > 0 else []))


def not_verified_why(destination: DestinationRow, job: Optional[JobRow]) -> str:
    """Why a destination reached cannot be verified now, in a few words; "" when it can."""
    if destination.can_verify:
        return ""
    if destination.verifying:
        return "it is being verified"
    if destination.drive == "two drives match":
        return "two drives match it"
    if destination.location is None and destination.remote is None:
        return "its drive is not attached"
    if job is not None and job.drive == "two drives match":
        return "two drives match the source"
    if job is not None and job.drive != "present":
        return "the source's drive is not attached"
    return {MarkerState.MISSING: "its first run is not confirmed yet",
            MarkerState.MISMATCH: "the folder belongs to another job",
            MarkerState.UNREADABLE: "its marker cannot be read"}.get(destination.marker,
                                                                    "its drive has not been checked yet")


def verify_several(destinations: Sequence[DestinationRow], jobs: Mapping[str, JobRow], name: Optional[str] = None,
                   *, turns: DriveTurns = DriveTurns.AUTOMATIC,
                   kept: Collection[Tuple[str, int]] = ()) -> VerifySeveral:
    """``destinations``: every destination reached, in the list's order; ``jobs``: the list's
    jobs by identifier; ``name``: the group's or the job's name when one is selected;
    ``turns``: the preference for activities on one drive, which decides what the question
    says of verifications sharing one; ``kept``: the destinations, as (job
    identifier, destination index), with a list of verified copies to build on."""
    def label(d: DestinationRow) -> str:
        job = jobs.get(d.job_id)
        return f"{job.name if job is not None else d.job_id} {d.index}"

    start = [d for d in destinations if d.can_verify]
    left = [(d, not_verified_why(d, jobs.get(d.job_id))) for d in destinations if not d.can_verify]
    unlisted = [d for d in start if (d.job_id, d.index) not in kept]
    n = len(start)
    title = f'Verify {_destinations(n)} of "{name}"?' if name else f"Verify {_destinations(n)}?"
    lines = [f"Verifies: {_names_of([label(d) for d in start])}."] if start else []
    if start:
        line = "New copies only reads what was backed up or changed since it was last verified"
        if unlisted:
            many = len(unlisted)
            line += (f"; {_and([label(d) for d in unlisted])} {agreeing(many, 'has', 'have')} no list of verified "
                     f"copies yet and {agreeing(many, 'is', 'are')} read in full")
        lines.append(line + ".")
    for answer, full in (("A full verification", True), ("New copies only", False)):
        continuing = [d for d in start if d.unfinished is not None and d.unfinished.full is full]
        if continuing:
            lines.append(f"{answer} continues the unfinished {agreeing(len(continuing), 'one', 'ones')} of "
                         f"{_and([label(d) for d in continuing])}.")
    if left:
        lines.append("Not verified: " + _names_of([f"{label(d)} ({why})" for d, why in left]) + ".")
    if n > 1:
        lines.append(_on_one_drive(turns, "verifications"))
        lines.append("A backup that is due runs first: the verifications step aside and continue.")
    elif n:
        lines.append(GIVES_WAY)
    return VerifySeveral(title, "\n".join(lines), tuple((d.job_id, d.index) for d in start),
                         full_default=bool(unlisted) or any(d.verification_due for d in start))


def delete_group_question(group: GroupRow) -> Tuple[str, str]:
    """(title, text) asked before a group is deleted."""
    n = len(group.jobs)
    stay = f"Its {_jobs(n)} {agreeing(n, 'stays', 'stay')}, in no group." if n else "It holds no job."
    return "Delete group", f'Delete the group "{group.name}"? {stay} Nothing changes on any drive.'


def remove_jobs_question(jobs: Sequence[JobRow]) -> Tuple[str, str]:
    """(title, text) asked before several jobs are removed at once."""
    return "Remove jobs", (f"Remove {_jobs(len(jobs))}: {_names(jobs)}?\nNothing is deleted on any drive: their "
                           "destinations keep their files, versions and run records.")


def group_name_problem(contents: StoreContents, name: str, besides: Optional[str] = None) -> str:
    """Why ``name`` cannot name a group, or "" when it can; ``besides``: the group being
    renamed, which may keep its own name."""
    text = name.strip()
    if not text:
        return "A group needs a name."
    if len(text) > MAX_NAME_LENGTH:
        return f"A name has at most {MAX_NAME_LENGTH} characters."
    other = contents.group_named(text)
    if other is not None and other.group_id != besides:
        return f'A group is already called "{other.name}".'
    return ""


def collapsed_groups(gui_state: Mapping[str, Any]) -> Set[str]:
    """The groups closed in the job list, as remembered; anything else under the key is
    passed over."""
    value = gui_state.get(COLLAPSED_KEY)
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def with_collapsed(gui_state: Mapping[str, Any], collapsed: Collection[str]) -> Dict[str, Any]:
    """The GUI's remembered state holding ``collapsed`` as the groups closed."""
    state = dict(gui_state)
    if collapsed:
        state[COLLAPSED_KEY] = sorted(collapsed)
    else:
        state.pop(COLLAPSED_KEY, None)
    return state
