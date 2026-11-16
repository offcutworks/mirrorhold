"""Admission: which runs, full verifications and repairs work at the same time.

Runs, full verifications and repairs — activities — work at the same time unless they
conflict. Each claims, before it starts, the folders it reads and the folders it writes:
a run reads its source and writes each selected destination present when it is first
admitted that it has not finished (*A run's destinations one at a time*, below);
a verification reads its source and its destination and writes nothing; a repair
(``maintenance.repairer``) claims as a run of its one destination does. Two activities
conflict when a folder one writes and a folder the other reads or writes lie on one
volume identity with one containing the other, paths compared case-insensitively
(``Endpoint.overlaps``). Beyond the folders, two runs of one job always conflict, so a
job's destinations are written one after another whoever started the runs, and two
verifications of one destination conflict, as both would continue its one pass. An
activity whose claim cannot be read — one started by an older copy of the tool, which
publishes none — conflicts with every other. One of this copy that has published its
first status but not yet made its first attempt, and so claims nothing, has asked for
nothing and holds no one back (``before_first_attempt``): the activities a group or
several items start at once would otherwise each wait one attempt for the others. Nor does
a run that stopped waiting without a turn — cancelled, or no longer to run by itself — and
withdrew its claim as it ends.

**Turns on a drive.** A claim also names the drives its present folders lie on
(``VolumeInfo.drive``: a physical disk, or a volume no single disk carries; a run's, those
of its source and of the destination it works on alone, below), each with
the words a person knows it by and its kind — what the user says the drive is
(``Activities.drive_kinds``, read from the job file with the choice), else what its disk
reports (``VolumeInfo.drive_kind``), decided here alone (``_drives``) — and the user's
choice of when activities take turns on a drive (``Activities.drive_turns``, read from
the job file at each attempt): automatic, by default, takes turns on a drive with a seek
penalty or of a kind not known and none on a drive with no seek penalty or a cloud drive,
which has no heads to share (``SHARED_KINDS``); or always; or never. On a drive it takes
turns on, an activity also conflicts with every activity that uses that drive, to read or
to write; on any other, two activities work at the same time and share its rate. The
choice and the drive's kind are the waiting activity's own, as its claim names them: an
activity admitted under one choice keeps its drives, and a newcomer judges by what it read.
A claim that names no drives — an older copy's — is compared by its folders alone; one
that names drives without their kind or the choice — also an older copy's — takes turns on
all of them or on none, as its one choice says, and a claim keeps publishing that one
choice for an older copy to read: on when it takes turns on any of its drives. An older
copy reads a cloud drive's kind as not known, so it takes turns there.

**Turns on the remotes.** A remote's folder is claimed whenever its destination is
selected — whether it can be reached is known only once the engine asks — and conflicts by
remote and folder like any other (``RemoteEndpoint.overlaps``). Beyond that, any two
activities that both reach a remote take turns, whatever the choice for drives: every
remote is reached through one rclone configuration, which the engine rewrites when it
refreshes a service's sign-in, and two engines refreshing one configuration at once could
lose a fresh sign-in where the service lets each be used only once. A claim says whether
it reaches a remote now (``Claim.remote``): a run names the remote folders of the
destinations it has yet to reach, but reaches a remote only while it works on one of them.
A claim that does not say — an older copy's — reaches one whenever it names a remote's
folder, and an older copy that knows remote folders reads every claim so: it takes turns on
the remotes with a run for as long as that run has a remote destination left. A copy older
still reads a claim naming a remote's folder as one it cannot read, and so waits for it. A
waiting activity names that configuration where a drive would stand (``CONFIGURATION``),
when it alone is why.

An activity publishes its claim in its live status (``records.status``) from its first
attempt, and starts when, under the admission lock (``RuntimeLayout.admission_lock``,
held for the moment a decision takes), no admitted activity conflicts with it and no
waiting activity it conflicts with goes first: a run waiting between two of its
destinations goes before every activity that has not begun (below), then a waiting run or
repair before any verification, and otherwise the one queued first goes first — the user
may move a waiting run or repair to the front (``records.status.run_next``), which changes
when it counts as queued and nothing else. Admitted, it holds what it claimed until it
ends, or a run until it is done with the destination it works on; one that waits holds
nothing — one taking its drives back after a pause aside (below) — and the strict order
means none waits for ever behind a later one. The decision is published before the lock is
released, so the next decision sees it, and every dead activity is reaped in the same
moment, so none is recorded twice. A waiting activity publishes the one it waits for —
naming the drive they share when the drive alone is why — and its place in line: one
more than the waiting activities it conflicts with that go first, a run that has begun
counted only in the place of another that has, since none that has not can pass it. One
it waits for whose status it cannot read now it names by the last status its attempts read
of it, saying that it cannot read it now.

**A run's destinations one at a time** (the maintainer's decision of 2026-11-10). A run
holds a destination's drive only while it works on that destination. Its claim names the
folders of every destination it has not finished, so that nothing else writes there or
reads there before it has, but the drives of its source and of the one destination it
works on alone, so what shares only another of its drives works beside it; and it reaches
the remotes only while that destination is a remote's folder (``run_claim``'s ``working``).
Done there, it gives up everything and asks again in the same moment, under the lock: it
is admitted on the first of the destinations it has not finished, by number, that nothing
holds back, a held one left for later (``admit``'s ``any_of``), and otherwise waits,
claiming them all as one waiting for its first turn does. A run waiting so has begun
(``Activity.begun``): its source is listed, and another writer on a destination it has yet
to reach — another run of its job, a repair there — would leave it writing there from an
older listing. So it goes before every activity that has not begun, wherever their places
in line, as one taking its drives back after a pause does (below). None waits longer for
it than it would behind a run holding all of its claim to its end, and the order is
still strict — begun first, then runs and repairs before verifications, then the one
queued first — so it adds no way to wait for ever.

**A pause frees the drives** (the maintainer's decision of 2026-11-07). A paused activity
uses no drive, so nothing takes turns with it: those that share only a drive with it go
ahead (``Activity.holds_drives``). It keeps its folders, and its turn on the remotes: the plan
it holds would not survive another writer there, and its engine, suspended, still holds a
sign-in. Asked to work again it takes its drives back (``take_back``): it may once nothing
at work uses a drive it takes turns on, and until then it stays paused, says whom it waits
for (``resuming``) and holds its drives against everyone not yet admitted, so it keeps its
place ahead of them. It waits only for activities at work, and those wait for nothing it
holds, so this adds no way to wait for ever: of two asked to work again at one instant,
the lock lets one go first, and the other then finds it at work.

A working verification gives way to a waiting run or repair it conflicts with that
nothing but verifications holds back (``gives_way``), judged as that one judges its own
turn: writing the backup first, and only to the activities it actually blocks. A run or a
repair taking its drives back counts as waiting here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..contract_errors import ContractError
from ..endpoints.endpoint import AnyEndpoint, RemoteEndpoint, endpoint_from_document
from ..endpoints.resolution import Presence, resolve
from ..endpoints.volumes import DriveKind, VolumeInfo, drive_name
from ..jobs.model import Activities, Destination, DriveMark, DriveTurns, Job
from ..locations import RuntimeLayout
from .lock import holding
from .status import (KIND_RUN, KIND_VERIFICATION, LastRead, LiveStatus, ObservedRun, Phase, StatusBoard, observe_runs,
                     place_of, reap_interrupted)

ADMISSION_INTERVAL = 0.5
"""Seconds between two attempts of a waiting activity, unless its control changes first."""

PUBLISH_PATIENCE = 2.0
"""Seconds an admission's publication is tried again while another program holds the status file."""

CONFIGURATION = "the rclone configuration"
"""What two activities that reach a remote share, named as a shared drive is."""

SHARED_KINDS = (DriveKind.SOLID, DriveKind.CLOUD)
"""The kinds of drive the automatic choice takes no turns on: those with no heads to seek."""


@dataclass(frozen=True)
class ClaimedDrive:
    """A drive a claim names: what it is (``VolumeInfo.drive``), how a person knows it as
    the claim is made ("drive I:", ``endpoints.volumes.drive_name``), and its kind as the
    claim read it (``VolumeInfo.drive_kind``)."""

    drive: str
    name: str
    kind: DriveKind = DriveKind.UNKNOWN

    def to_document(self) -> dict:
        return {"drive": self.drive, "name": self.name, "kind": self.kind.value}

    @classmethod
    def from_document(cls, doc: Any) -> "ClaimedDrive":
        """A claimed drive as a status publishes it. One naming no kind (an older copy's), or
        a kind this copy does not know (a newer copy's), is of a kind not known."""
        kind = doc.get("kind", DriveKind.UNKNOWN.value)
        if not isinstance(doc["drive"], str) or not isinstance(doc["name"], str) or not isinstance(kind, str):
            raise TypeError("a claimed drive is three strings")
        return cls(doc["drive"], doc["name"], next((k for k in DriveKind if k.value == kind), DriveKind.UNKNOWN))


@dataclass(frozen=True)
class Claim:
    """The folders an activity reads and the folders it writes, the drives they lie on,
    when it takes turns on a drive, and whether it reaches a remote now: None where the
    claim does not say (an older copy's), when it does whenever it names a remote's folder."""

    reads: Tuple[AnyEndpoint, ...] = ()
    writes: Tuple[AnyEndpoint, ...] = ()
    drives: Tuple[ClaimedDrive, ...] = ()
    turns: DriveTurns = DriveTurns.AT_THE_SAME_TIME
    remote: Optional[bool] = None

    def __post_init__(self):
        if not isinstance(self.turns, DriveTurns):
            raise TypeError(f"a claim's turns are a DriveTurns, not {self.turns!r}")

    @property
    def reaches_remote(self) -> bool:
        """Whether the activity reaches a remote now, through the one rclone configuration."""
        if self.remote is not None:
            return self.remote
        return any(isinstance(e, RemoteEndpoint) for e in self.reads + self.writes)

    def takes_turns_on(self, drive: ClaimedDrive) -> bool:
        """Whether the activity takes turns on ``drive``: by its kind when automatic — on any
        drive not known to have no heads to seek (``SHARED_KINDS``) — otherwise as the choice
        says."""
        if self.turns is DriveTurns.AUTOMATIC:
            return drive.kind not in SHARED_KINDS
        return self.turns is DriveTurns.ONE_AT_A_TIME

    def to_document(self) -> dict:
        # "one_per_drive" is what an older copy reads: one choice for all of a claim's drives.
        doc = {"reads": [e.to_document() for e in self.reads], "writes": [e.to_document() for e in self.writes],
               "drives": [d.to_document() for d in self.drives], "turns": self.turns.value,
               "one_per_drive": any(self.takes_turns_on(d) for d in self.drives)}
        if self.remote is not None:
            doc["remote"] = self.remote
        return doc

    @classmethod
    def from_document(cls, doc: Any) -> Optional["Claim"]:
        """The claim a status publishes; None when it holds none that can be read. One that
        names no drives (an older copy's) names none; one that names no turns (an older
        copy's too) takes turns on all its drives or on none, as its one choice says; one
        that does not say whether it reaches a remote now (an older copy's too) does whenever
        it names a remote's folder."""
        try:
            reads = tuple(endpoint_from_document(e) for e in doc["reads"])
            writes = tuple(endpoint_from_document(e) for e in doc["writes"])
            drives = tuple(ClaimedDrive.from_document(d) for d in doc.get("drives", ()))
            if "turns" in doc:
                turns = next((t for t in DriveTurns if t.value == doc["turns"]), None)
            else:
                one_per_drive = doc.get("one_per_drive", False)
                turns = ({True: DriveTurns.ONE_AT_A_TIME, False: DriveTurns.AT_THE_SAME_TIME}.get(one_per_drive)
                         if isinstance(one_per_drive, bool) else None)
            if turns is None:
                return None
            remote = doc.get("remote")
            if remote is not None and not isinstance(remote, bool):
                return None
            return cls(reads, writes, drives, turns, remote)
        except (ContractError, KeyError, TypeError, AttributeError):
            return None

    def turn_drive(self, other: "Claim") -> Optional[ClaimedDrive]:
        """The first of this claim's drives the other claim names too and this one takes
        turns on; None when none."""
        theirs = {d.drive for d in other.drives}
        return next((d for d in self.drives if d.drive in theirs and self.takes_turns_on(d)), None)


def _drives(endpoints: Sequence[AnyEndpoint], volumes: Sequence[VolumeInfo],
            marks: Sequence[DriveMark]) -> Tuple[ClaimedDrive, ...]:
    """The drives of those of ``endpoints`` present among ``volumes``, each once, of the kind
    ``marks`` says, else of the kind its disk reports. A remote's folder lies on no drive."""
    marked = {mark.drive: mark.kind for mark in marks}
    found: Dict[str, ClaimedDrive] = {}
    for endpoint in endpoints:
        resolution = resolve(endpoint, volumes)
        if resolution.presence is Presence.PRESENT and resolution.volume.drive not in found:
            drive = resolution.volume.drive
            found[drive] = ClaimedDrive(drive, drive_name(volumes, drive),
                                        marked.get(drive, resolution.volume.drive_kind))
    return tuple(found.values())


def worked_on(destination: Destination, volumes: Sequence[VolumeInfo]) -> bool:
    """Whether a run claims ``destination`` and works on it at a turn of its own: a remote's
    folder, whose presence only the engine can tell, or a drive's present among ``volumes``.
    Any other is recorded as it stands, needing no turn (``run.runner``)."""
    return destination.remote is not None or resolve(destination.endpoint, volumes).presence is Presence.PRESENT


def _written(job: Job, indexes: Sequence[int], volumes: Sequence[VolumeInfo]) -> Tuple[AnyEndpoint, ...]:
    """The folders of the destinations ``indexes`` (1-based) a run writes (``worked_on``)."""
    return tuple(job.destinations[i - 1].endpoint for i in indexes if worked_on(job.destinations[i - 1], volumes))


def run_claim(job: Job, indexes: Sequence[int], volumes: Sequence[VolumeInfo], *,
              turns: DriveTurns = DriveTurns.AT_THE_SAME_TIME, marks: Sequence[DriveMark] = (),
              working: Optional[int] = None) -> Claim:
    """A run's claim: its source read, each selected destination (1-based) present among
    ``volumes`` — or a remote's folder, whose presence only the engine can tell — written,
    and the drives of those present, of the kinds ``marks`` says. With ``working``, one of
    ``indexes``, the claim of a run working on that destination: the same folders, but the
    drives of its source and of that destination alone, and reaching a remote only when that
    destination is a remote's folder."""
    source = job.source.endpoint
    writes = _written(job, indexes, volumes)
    if working is None:
        return Claim((source,), writes, _drives((source,) + writes, volumes, marks), turns)
    if working not in indexes:
        raise ValueError(f"destination {working} is not among those claimed")
    return Claim((source,), writes, _drives((source,) + _written(job, (working,), volumes), volumes, marks), turns,
                 job.destinations[working - 1].remote is not None)


def verification_claim(job: Job, index: int, volumes: Sequence[VolumeInfo] = (), *,
                       turns: DriveTurns = DriveTurns.AT_THE_SAME_TIME, marks: Sequence[DriveMark] = ()) -> Claim:
    """A verification's claim: its source and its destination (1-based) read, nothing
    written, and the drives of those present among ``volumes``, of the kinds ``marks`` says."""
    reads = (job.source.endpoint, job.destinations[index - 1].endpoint)
    return Claim(reads, (), _drives(reads, volumes, marks), turns)


def activity_claim(job: Job, indexes: Sequence[int], volumes: Sequence[VolumeInfo], activities: Activities, *,
                   verifying: bool = False, working: Optional[int] = None) -> Claim:
    """The claim of an activity of ``job`` under the job file's ``activities``: a run (or a
    repair) of the destinations ``indexes`` — working on ``working`` among them, when given
    (``run_claim``) — or with ``verifying`` a verification of its one destination — so every
    activity reads the choice and the drives' marks the same way."""
    if verifying:
        (index,) = indexes
        return verification_claim(job, index, volumes, turns=activities.drive_turns, marks=activities.drive_kinds)
    return run_claim(job, indexes, volumes, turns=activities.drive_turns, marks=activities.drive_kinds,
                     working=working)


@dataclass(frozen=True)
class WaitedFor:
    """The activity a waiting one waits for, as the waiting one's status names it. One whose
    status the waiting one cannot read now is named by the last status it read of it: its
    kind and job, both "" when it read none, and nothing of whether it is paused or waits."""

    run_id: str
    kind: str
    job_name: str
    paused: bool = False
    waiting: bool = False             # it waits for its own turn too
    drive: str = ""                   # what they share, when that alone is why: "drive I:", ``CONFIGURATION``
    readable: bool = True             # False: its status cannot be read now

    def to_document(self) -> dict:
        return {"run_id": self.run_id, "kind": self.kind, "job_name": self.job_name, "paused": self.paused,
                "waiting": self.waiting, "drive": self.drive, "readable": self.readable}

    @classmethod
    def from_status(cls, status: Mapping[str, Any]) -> Optional["WaitedFor"]:
        """What a status says it waits for; None when it names nothing. A status that does
        not say whether it could read the other's, as an older copy's, read it."""
        doc = status.get("waiting_for")
        if not isinstance(doc, dict) or not doc.get("run_id"):
            return None
        readable = doc.get("readable") is not False
        return cls(str(doc["run_id"]), str(doc.get("kind") or (KIND_RUN if readable else "")),
                   str(doc.get("job_name") or ""), bool(doc.get("paused")), bool(doc.get("waiting")),
                   str(doc.get("drive") or ""), readable)


def line_words(place: int) -> str:
    """'next in line', '2nd in line', '3rd in line', '11th in line': a waiting activity's place."""
    if place <= 1:
        return "next in line"
    suffix = "th" if 10 <= place % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
    return f"{place}{suffix} in line"


@dataclass(frozen=True)
class Activity:
    """A live run or verification, as admission reads it from its status."""

    run_id: str
    kind: str
    job_id: str
    job_name: str
    marker_id: str
    queued: str
    admitted: bool
    paused: bool
    claim: Optional[Claim]
    readable: bool = True            # False: its status cannot be read now
    last_kind: str = ""              # one not readable: its kind in the last status read of it, "" for none
    resuming: bool = False           # paused, asked to work again, its drives not back yet
    begun: bool = False              # a run that has finished a destination: its source is listed

    @classmethod
    def from_observed(cls, observed: ObservedRun) -> "Activity":
        """An activity as its observer finds it. One whose status cannot be read now is read
        from what is known of it — its identifier, which holds its turn and claims nothing
        that can be read, so that it conflicts with every other — and named, never judged,
        by the last status this observer read of it (``records.status.LastRead``)."""
        activity = cls.from_status(observed.status)
        if observed.readable:
            return activity
        last = observed.last or {}
        return replace(activity, job_name=str(last.get("job_name") or ""), readable=False,
                       last_kind=str(last.get("kind") or KIND_RUN) if last else "")

    @classmethod
    def from_status(cls, doc: Mapping[str, Any]) -> "Activity":
        """An activity as its status says. A run has begun once it names a destination it has
        finished: a run records its destinations only once admitted, so one that waits naming
        one waits between two of them — which a run of an older copy never does."""
        admitted = doc.get("admitted")
        if not isinstance(admitted, bool):        # an older copy of the tool: working unless waiting
            admitted = doc.get("phase") != Phase.WAITING.value
        claim = doc.get("claim")
        kind = str(doc.get("kind") or KIND_RUN)
        finished = doc.get("finished")
        return cls(str(doc.get("run_id") or ""), kind, str(doc.get("job_id") or ""),
                   str(doc.get("job_name") or ""), str(doc.get("marker_id") or ""),
                   str(doc.get("queued") or doc.get("started") or ""), admitted, bool(doc.get("paused")),
                   Claim.from_document(claim) if claim is not None else None, resuming=bool(doc.get("resuming")),
                   begun=kind == KIND_RUN and isinstance(finished, list) and bool(finished))

    @property
    def order(self) -> Tuple[str, str]:
        """Its place in line: when it was queued, the identifier breaking a tie."""
        return self.queued, self.run_id

    @property
    def at_work(self) -> bool:
        return self.admitted and not self.paused

    @property
    def holds_drives(self) -> bool:
        """Whether the drives it takes turns on are its own now: admitted, and not paused
        unless it has asked for them back."""
        return self.at_work or (self.admitted and self.resuming)

    def waited_for(self, drive: str = "") -> WaitedFor:
        if not self.readable:
            return WaitedFor(self.run_id, self.last_kind, self.job_name, readable=False)
        return WaitedFor(self.run_id, self.kind, self.job_name, self.paused and not self.resuming,
                         not self.admitted or self.resuming, drive)


def _touches(writes: Sequence[AnyEndpoint], claim: Claim) -> bool:
    return any(w.overlaps(e) for w in writes for e in claim.reads + claim.writes)


def conflict(a: Activity, b: Activity, *, by_drive: bool = False) -> bool:
    """Whether two activities may not work at the same time: over their folders, one job
    or one destination, both reaching a remote, and with ``by_drive`` over a drive both use
    that ``a`` — the one judging — takes turns on."""
    if a.claim is None or b.claim is None:
        return True
    if a.claim.reaches_remote and b.claim.reaches_remote:
        return True
    if _one_work(a, b):
        return True
    return by_drive and a.claim.turn_drive(b.claim) is not None


def _one_work(a: Activity, b: Activity) -> bool:
    """Whether two activities with claims conflict over their work: one job, one destination
    or their folders, whatever they use besides."""
    if a.kind == b.kind == KIND_RUN and a.job_id == b.job_id:
        return True
    if a.kind == b.kind == KIND_VERIFICATION and a.marker_id and a.marker_id == b.marker_id:
        return True
    return _touches(a.claim.writes, b.claim) or _touches(b.claim.writes, a.claim)


def goes_first(waiting: Activity, other: Activity) -> bool:
    """Whether ``waiting`` goes before ``other`` when the two conflict and both wait: a run
    that has begun before any activity that has not; then a run or a repair before any
    verification; otherwise the one queued first. One order for every pair, so no two
    waiting activities each wait for the other."""
    if waiting.begun != other.begun:
        return waiting.begun
    if (waiting.kind == KIND_VERIFICATION) != (other.kind == KIND_VERIFICATION):
        return other.kind == KIND_VERIFICATION
    return waiting.order < other.order


def _ahead(me: Activity, others: Sequence[Activity]) -> List[Activity]:
    """The waiting activities ``me`` conflicts with, by its own choice, that go first."""
    return sorted((o for o in others if not o.admitted and conflict(me, o, by_drive=True)
                   and goes_first(o, me)), key=lambda o: o.order)


def blockers(me: Activity, others: Sequence[Activity]) -> List[Activity]:
    """What keeps ``me`` waiting, the first named first: every admitted activity it
    conflicts with, then every waiting one it conflicts with that goes first — a drive
    both use counting when ``me`` takes turns on it and the other holds its drives. One
    taking its drives back holds its place already, and waits only for what works on them."""
    if me.resuming:
        return sorted((o for o in others if o.at_work and conflict(me, o, by_drive=True)), key=lambda o: o.order)
    held = sorted((o for o in others if o.admitted and conflict(me, o, by_drive=o.holds_drives)),
                  key=lambda o: o.order)
    return held + _ahead(me, others)


def place_in_line(me: Activity, others: Sequence[Activity]) -> int:
    """Where ``me`` stands while it waits: 1 when no waiting activity it conflicts with goes
    first, one more for each that does — a run that has begun counted only in the place of
    another that has: none that has not can pass it, so it stands as one at work does, and
    a run moved to the front is first among those that can."""
    return len([o for o in _ahead(me, others) if o.begun == me.begun]) + 1


def _shared_drive(me: Activity, other: Activity) -> str:
    """What ``me`` and ``other`` both use, by name, when that alone is why ``me`` waits for
    it — a drive, or the rclone configuration of two that reach a remote; "" otherwise."""
    if me.claim is None or other.claim is None or _one_work(me, other):
        return ""
    if me.claim.reaches_remote and other.claim.reaches_remote:
        return CONFIGURATION
    shared = me.claim.turn_drive(other.claim)
    return shared.name if shared is not None else ""


def before_first_attempt(status: Mapping[str, Any]) -> bool:
    """Whether a status is an activity of this copy that asks for no turn: it says it holds
    no turn — which an older copy's status never says — and claims nothing. Its first status
    is published before its first attempt, which publishes its claim; a run that stops
    waiting without a turn withdraws its claim as it ends (``run.runner``)."""
    return status.get("admitted") is False and status.get("claim") is None


def starting(status: Mapping[str, Any]) -> bool:
    """Whether a waiting status is an activity of this copy starting: it names neither what
    it waits for nor a place in line, and it is before its first attempt at its turn
    (``before_first_attempt``) or just admitted and not yet at its first step. One that has
    asked and names neither — a verification giving way, until its next attempt, or one
    whose admission could not be published — is waiting for its turn, not starting."""
    if WaitedFor.from_status(status) is not None or place_of(status):
        return False
    return before_first_attempt(status) or status.get("admitted") is True


def live_activities(layout: RuntimeLayout, last_read: Optional[LastRead] = None) -> List[Activity]:
    """Every run, verification and repair in progress that has asked for its turn, admitted
    or waiting. One ``before_first_attempt`` holds no one back: its first attempt judges it
    against the others, as every attempt does. ``last_read`` names those whose status
    cannot be read now (``Activity.from_observed``)."""
    return [Activity.from_observed(r) for r in observe_runs(layout, last_read)
            if r.alive and not before_first_attempt(r.status)]


@dataclass(frozen=True)
class Turn:
    """The outcome of one attempt: admitted, or what the activity waits for."""

    admitted: bool
    waits_for: Optional[Activity] = None
    chosen: int = 0                       # admitted: which of ``admit``'s ``any_of``, when given


def admit(board: StatusBoard, claim: Claim, *, any_of: Sequence[Claim] = ()) -> Turn:
    """One attempt at the activity's turn, under the admission lock. When nothing holds its
    claim back it is admitted with it; with ``any_of`` — parts of ``claim``, a run's claims
    each working on one of its destinations, in the order they are to be tried — it is
    admitted with the first of them nothing holds back instead. Admitted, the claim it holds
    is published before the lock is released, or it is not admitted at all, and every dead
    activity is reaped; otherwise ``claim`` is published with what it waits for, the drive
    they share when that alone is why, and its place in line. An activity admitted before
    — a run between two of its destinations — holds nothing from the moment it asks."""
    layout, status = board.layout, board.status
    with holding(layout.admission_lock):
        others: Optional[List[Activity]] = None
        for position, offered in enumerate(any_of or (claim,)):
            status.claim = offered.to_document()
            me = Activity.from_status(status.to_document())
            if others is None:
                others = [a for a in live_activities(layout, board.last_read) if a.run_id != me.run_id]
            if blockers(me, others):
                continue
            status.admitted, status.waiting_for, status.place = True, None, 0
            try:
                board.publish(patience=PUBLISH_PATIENCE)
            except OSError:
                status.admitted, status.claim = False, claim.to_document()
                return Turn(False)
            reap_interrupted(layout)
            return Turn(True, chosen=position)
        status.admitted, status.claim = False, claim.to_document()
        me = Activity.from_status(status.to_document())
        found = blockers(me, others)
        status.waiting_for = found[0].waited_for(_shared_drive(me, found[0])).to_document() if found else None
        status.place = place_in_line(me, others)
        _publish_quietly(board)
        return Turn(False, found[0] if found else None)


def take_back(board: StatusBoard) -> bool:
    """One attempt, under the admission lock, of a paused activity asked to work again:
    whether it may. It may once nothing at work uses a drive it takes turns on, no longer
    paused as the lock is released, so the next decision sees it. Until then it asks for its
    drives back and publishes what it waits for, the drive they share when that alone is why."""
    layout, status = board.layout, board.status
    with holding(layout.admission_lock):
        status.resuming = True
        me = Activity.from_status(status.to_document())
        found = blockers(me, [a for a in live_activities(layout, board.last_read) if a.run_id != me.run_id])
        if found:
            status.waiting_for = found[0].waited_for(_shared_drive(me, found[0])).to_document()
            _publish_quietly(board)
            return False
        status.paused, status.resuming, status.waiting_for = False, False, None
        try:
            board.publish(patience=PUBLISH_PATIENCE)
        except OSError:
            status.paused, status.resuming = True, True
            return False
        return True


def stay_paused(status: LiveStatus) -> None:
    """Stop asking for the drives back: a pause or a cancel came first."""
    status.resuming, status.waiting_for = False, None


def settle_interrupted(layout: RuntimeLayout) -> None:
    """Record every dead activity now, under the admission lock, as an admission does."""
    with holding(layout.admission_lock):
        reap_interrupted(layout)


def withdraw(board: StatusBoard) -> None:
    """Give the claim up and wait again (a verification giving way). Published at once when
    it can be; otherwise the status pump publishes it a moment later, which keeps others
    waiting that moment longer and nothing worse."""
    stay_paused(board.status)
    board.status.admitted, board.status.place = False, 0
    _publish_quietly(board)


def _publish_quietly(board: StatusBoard) -> None:
    try:
        board.publish(patience=PUBLISH_PATIENCE)
    except OSError:
        pass                                # the status pump publishes the same state shortly


def gives_way(layout: RuntimeLayout, status: LiveStatus) -> bool:
    """Whether a working verification should give way: a run or a repair that conflicts with
    it waits, has asked for its folders, and is held back by verifications alone — each
    judged as it judges its own turn, a drive both use counting when it takes turns on
    it. One taking its drives back waits too."""
    me = Activity.from_status(status.to_document())
    everyone = [a for a in live_activities(layout) if a.run_id != me.run_id] + [me]
    for run in everyone:
        if run.kind == KIND_VERIFICATION or (run.admitted and not run.resuming) or run.claim is None \
                or not conflict(run, me, by_drive=me.holds_drives):
            continue
        held = blockers(run, [a for a in everyone if a.run_id != run.run_id])
        if held and all(a.kind == KIND_VERIFICATION for a in held):
            return True
    return False


__all__ = ["ADMISSION_INTERVAL", "Activity", "Claim", "ClaimedDrive", "PUBLISH_PATIENCE", "Turn", "WaitedFor",
           "activity_claim", "admit", "before_first_attempt", "blockers", "conflict", "gives_way", "goes_first",
           "line_words", "live_activities", "place_in_line", "run_claim", "settle_interrupted", "starting",
           "stay_paused", "take_back", "verification_claim", "withdraw", "worked_on"]
