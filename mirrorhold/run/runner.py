"""Running and previewing a job: every selected pair, one after another.

``run_job`` is what the command line's ``run`` executes, in a core process of its own
whoever asked for it. It publishes a live status from the moment it starts and waits
for its turn (``records.admission``): it works at the same time as every run and
verification it does not conflict with, and claims, from one snapshot of the
configuration and one scan of the volumes taken as it is admitted, its source and the
selected destinations present then, and the drives they lie on — taking turns on those the
user's choice and each drive's kind say to. Destinations asked for — by number, in the job
as it stood when the run began, or by marker — are kept by marker: a job reordered while the
run waits changes nothing of which it serves, and one of them removed meanwhile stops the
run. Admitted, it
works with that configuration — the job re-validated against it — and those volumes;
removes any volume snapshot a run that died left; decides how the source is read — from
a volume snapshot made for this run, or as it is (``snapshots.reading``) — lists the
source once, and then for each pair:
resolves and verifies the destination, deletes its expired versions batches when the
job's version cleanup is on (``maintenance.cleanup``), lists it, plans, signs off the
plan at the safety gate — the destination's free space measured then, so what the
cleanup freed and what other runs wrote are in it, with what other runs in progress
still have to write there counted as used, under the admission lock — applies what the
gate allows, and writes the pair's record. Pairs are independent: an absent, stopped or
failed pair does not stop the next one; a cancel does.

A destination that is a remote's folder is reached through one engine session the run
opens once it is admitted, and a preview once it starts (``remotes.reach``), kept until it
ends, so an encrypted configuration is unlocked once; the job is validated with what the
engine said of each selected remote folder. A remote that cannot be reached is recorded
absent and waits (``remote-unreachable``), like a drive not attached; one the validation
refuses — no configuration, one that cannot be opened or unlocked, a service that refused,
no such remote, a file in the folder's place, below the capability floor — stops untouched
with the validation's own words. Reached, the pair runs as onto a drive, through its
storage: its free space judged where the service reports one and not judged where it
reports none (``gate.Space``), no other activity writing there meanwhile, since every
activity that reaches a remote takes its turn (``records.admission``). A remote listing
names the plan cannot tell apart stops the pair untouched (``NAMES_AMBIGUOUS``). A remote that
stops answering fails the pair (``REMOTE_FAILED``): as its marker or its record of what to
leave is read, its changed files' leading bytes read or its space asked, before any of the
plan is applied, it says so; once the run writes there — its records, its
marker, the job's definition, the plan — what was done is recorded and the next run converges.
The volume snapshot, if one was made, is removed when the run ends, whatever its outcome.
While it works — admitted and not paused — it keeps the computer from falling asleep by
itself and has Windows' shutdown screen name it when the user's choices say so (``power``).

A pair's first run is confirmed by the user alone (``run.gate``): by the ticket of a
preview the user confirmed (``RunOptions.ticket_id``), or, onto a destination that holds
nothing when the run lists it, by the user's go-ahead for a first backup
(``RunOptions.first_backup``); an unattended run confirms nothing.

A first run onto a destination that already holds what the source does not is planned
both ways by its preview (``PairAssessment.alternatives``): leaving that where it is —
the default — or moving it into versions; the ticket carries the user's choice
(``confirmation.Content``). Left, the destination is **shared**: its marker says so, a
record beside it names what the job owns (``endpoints.shared``), and that run and every
later one list, plan and apply only the job's part of it (``planning.sharing``), the
record updated before anything is applied. Moved — or when there was nothing to leave —
the destination is the job's whole.

A shared destination whose record is lost (``endpoints.shared.SharedLost``) no longer says
what is the job's there, so its run is a first run again (``PairAssessment.rebuild``): it
leaves what the source does not hold where it is — moving is not offered, the folder's
first run having left it — and waits, as any first run does, for the user to confirm it in
a preview. Confirmed, it moves what is left of the lost record into its versions batch and
writes the record anew. What the job backed up there whose source copies are gone since
is then left too. A record another program holds stops the run untouched; it is read
again next time.

Some folders inside a destination are never moved or written into (``protected_folders``):
a synchronised folder, another job's source, a folder Windows or this tool needs. Such a
destination's first run is offered only to leave. A run stops at one, nothing touched,
when the destination is the job's whole, when its first run was confirmed to move, or
when the folder lies beneath a top-level name the job owns or the source holds — unless
the job's exclusions hide it, the folder itself or a folder holding it
(``EntryFilter.hiding``). A hidden one neither listing sees, and no folder holding it
moves into versions whole: its other contents move one by one and it stays
(``build_plan``'s ``leave``). It stops a run only where the source holds a file at its
path or at a folder holding it, which a copy could put there only by moving it (the
maintainer's decisions of 2026-10-11 and 2026-10-21).

Pause and cancel are honoured at every safe point, the longest steps before the copy
included: after every folder of either listing, before each changed file's signature
is read, and before each changed file is opened ahead of the copy (``run.apply``) — each
publishing how far it has come. A cancel there records the pair, and the pairs not
started, as cancelled.

Each record also carries how the source was read and which skipped files went
unbacked-up, with the runs in a row each has — those the plan left out because a run
cannot carry their names among them — what the cleanup deleted, and the free
space of the destination's drive when the pair ended (``records.record``).

``preview_job`` runs the same assessment — resolution, marker, listings, plan, gate —
without waiting for a turn, without a volume snapshot and without changing anything: the
cleanup is measured, not made, and the gate counts what it would free on the free space
measured as the preview began. It adds the duration
estimate and, beside each destination, the job's notices a user should read before
confirming its removals (``validation.PREVIEW_NOTICES``: a cloud client that keeps a copy
of the folder and may put back what a run moves into versions, or one whose folders could
not be read); at a destination that is the job's and past its first run, the removals that
reappeared after being moved into its newest versions batch (``put_back``), which move into
versions again; and
saves a confirmation ticket naming each pair's removals. It reports where it is to an
optional ``progress`` callback, through which a caller (the GUI) also cancels it.
"""
from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (Callable, ContextManager, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Set,
                    Tuple, Union)

from ..contract_errors import ContractError
from ..endpoints.cloud import SyncKind, SyncRoot, sync_root_endpoints
from ..endpoints.endpoint import VolumeIdentity
from ..endpoints.marker import MANAGEMENT_DIR, Marker, MarkerState, marker_check, write_marker
from ..endpoints.resolution import Presence, Resolution, resolve
from ..endpoints.shared import SHARED_FILE, SharedError, SharedLost, SharedRecord, read_shared, write_shared
from ..endpoints.system import system_folder_endpoints
from ..endpoints.volumes import VolumeInfo, VolumeSource
from ..engine.rclone import Engine, EngineError, open_engine, open_source_file
from ..jobs.exclusions import EntryFilter
from ..jobs.model import Destination, Job
from ..jobs.store import ConfigStore, LoadStatus, StoreBusy, StoreContents
from ..jobs.validation import (DESTINATION_IN_CLOUD_COPY, REMOTE_UNREACHABLE, Contained, Finding, Severity,
                               ValidationContext, ValidationReport, contained_folders, preview_notices,
                               relative_inside, validate_job)
from ..locations import RuntimeLayout, runtime_layout
from ..maintenance.cleanup import CleanupReport, clean_versions, plan_cleanup
from ..planning.estimate import Estimate, estimate
from ..planning.listing import (ListingError, ListingProgress, Side, SkipReason, TreeListing, list_tree, parent_keys,
                                path_key)
from ..planning.plan import PairPlan, build_plan, time_window_ns
from ..planning.sharing import Sharing, listing_scope, share, top_names
from ..planning.signatures import LEADING_BYTES, Progress as SignatureProgress, assess_signatures
from ..power import Hold, System, run_reason
from ..remotes.facts import Reach, RemoteSetup
from ..remotes.reach import Reached, reach
from ..records import diagnostics
from ..records.admission import ADMISSION_INTERVAL, PUBLISH_PATIENCE, activity_claim, admit
from ..records.lock import holding
from ..records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, SHARED_RECORD_LOST, Outcome, RunRecord, SourceRead,
                              capped, last_counted, read_records, throughputs, unbacked_streaks, utc_now,
                              write_record)
from ..records.status import (KIND_RUN, CancelMode, LiveStatus, Phase, StatusBoard, engine_list_path,
                              observe_runs)
from ..snapshots.provider import SnapshotProvider, default_snapshot_provider
from ..snapshots.reading import SourceSnapshot, remove_leftovers
from ..storage.destination import DestinationStorage, StorageError, raise_if_unanswered
from ..storage.drive import DriveStorage
from ..tray import offer as offer_tray
from ..wording import counted
from .apply import ApplyResult, VersionsBatch, apply_pair
from .confirmation import (Content, PairConfirmation, Ticket, TicketError, discard_ticket, load_ticket, new_ticket,
                           save_ticket)
from .control import RunControl, StatusPump
from .gate import GateDecision, Space, Verdict, judge, space_needed
from .put_back import PutBack, put_back

DEFINITION_FILE = "job.json"
"""The self-describing copy of a destination's job, in its management folder."""

SYSTEM_FOLDER_CODE = "destination-contains-system-folder"
"""A pair stopped because its run would move or write into a folder Windows or this tool needs."""
SHARED_RECORD_UNREADABLE = "shared-record-unreadable"
"""A shared destination stopped, untouched, because its record cannot be read now."""
REBUILD_WITHHELD = "this folder's first run left what it held where it is"
"""Why a run making a lost record again does not offer to move what the folder holds."""

UNBACKED_SOURCE_SKIPS = (SkipReason.UNREADABLE, SkipReason.UNLISTABLE)
"""Source entries skipped for a reason a later run can overcome; a cloud-only file and a
link are left out by design and never go unbacked-up."""

Say = Callable[[str], None]
SourceReader = Callable[[Resolution], Tuple[Optional[SourceRead], Path]]
"""How a run reads its present source: what it records, and the folder it lists."""
VersionsCare = Callable[[Job, Destination, DestinationStorage], CleanupReport]
"""What happens to a verified destination's expired versions batches: deleted by a run,
measured by a preview."""
Room = Callable[["PairAssessment"], ContextManager[Tuple[Union[VolumeInfo, Space], int]]]
"""The destination's drive — or a remote's space — as the gate judges its free space, and
the bytes other runs still have to write there; held for as long as the judgement takes,
and its exit runs once the pair's decision is made."""


NAMES_AMBIGUOUS = "destination-names-ambiguous"
"""A remote pair stopped, untouched, because its remote lists names the plan cannot tell
apart: two differing by case alone, or one name held twice in a folder."""
REMOTE_FAILED = "remote-failed"
"""A remote pair failed because its remote stopped answering, as the pair was assessed or
applied."""


class RunError(Exception):
    """A run or a preview that cannot start at all."""


class RunCancelled(Exception):
    """Raised by a run's own progress callbacks when a cancel is wanted: ends a listing or
    the signature reads, and is recorded as the pair's cancellation."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


AUTOMATIC_TRIGGERS = ("arrival", "schedule")
"""What can start a run by itself; such a run is unattended and named so in its records."""


@dataclass(frozen=True)
class RunOptions:
    destinations: Optional[Tuple[int, ...]] = None   # 1-based, in the job's order; None: every one
    ticket_id: Optional[str] = None
    unattended: bool = False
    bandwidth_limit: Optional[str] = None            # handed to the engine; a property of this run only
    automatic: Optional[str] = None                  # "arrival" or "schedule": what started this unattended run
    first_backup: bool = False                       # the go-ahead for first runs onto destinations holding nothing
    markers: Optional[Tuple[str, ...]] = None        # the destinations by marker, instead of by number

    @property
    def trigger(self) -> str:
        """How the run was started, as its status and records name it: "manual",
        "unattended", "arrival" or "schedule"."""
        return self.automatic or ("unattended" if self.unattended else "manual")


@dataclass(frozen=True)
class JobRunResult:
    run_id: str
    records: Tuple[RunRecord, ...] = ()
    disabled: bool = False                            # an unattended run of a disabled job did nothing


@dataclass(frozen=True)
class Alternative:
    """One way a first run may treat what its destination already holds and the source
    does not: the plan by it, the gate's decision on that plan and, when it is left, the
    job's part of the destination."""

    content: Content
    plan: PairPlan
    decision: GateDecision
    sharing: Optional[Sharing] = None


@dataclass
class PairAssessment:
    """One pair, resolved and planned and judged, or the reason it could not be."""

    index: int
    destination: Destination
    resolution: Optional[Resolution] = None
    storage: Optional[DestinationStorage] = None      # the destination's files, once it resolved present
    marker: Optional[MarkerState] = None
    listing: Optional[TreeListing] = None
    plan: Optional[PairPlan] = None
    decision: Optional[GateDecision] = None
    outcome: Optional[Outcome] = None                 # set when the pair ends before applying
    reasons: Tuple[str, ...] = ()
    detail: str = ""
    cleanup: Optional[CleanupReport] = None           # the job's version cleanup: deleted (a run), or measured
    sharing: Optional[Sharing] = None                 # a shared destination's: the job's part, its record after the run
    content: Optional[Content] = None                 # a previewed first run's choice: what ``plan`` is by
    alternatives: Tuple[Alternative, ...] = ()        # a preview's, for a first run onto what the source does not hold
    withheld: str = ""                                # why moving that into versions is not offered, as a clause
    rebuild: str = ""                                 # why a shared destination's record is lost, when it is

    @property
    def root(self) -> Optional[Path]:
        """Where the destination resolved, as words name it; its files are reached only
        through ``storage``."""
        return self.resolution.location if self.resolution is not None else None

    @property
    def first_run(self) -> bool:
        """No marker yet, or a shared destination whose record was lost: either way what
        the folder holds is judged afresh, and the run waits for the user's confirmation."""
        return self.marker is MarkerState.MISSING or bool(self.rebuild)


@dataclass(frozen=True)
class PairPreview:
    assessment: PairAssessment
    estimate: Optional[Estimate] = None
    notices: Tuple[str, ...] = ()        # what the user should read before confirming (``preview_notices``)
    put_back: Optional[PutBack] = None   # the removals that reappeared after being moved into versions


@dataclass(frozen=True)
class JobPreview:
    job: Job
    pairs: Tuple[PairPreview, ...]
    ticket: Optional[Ticket] = None
    source_problem: str = ""
    source: Optional[Resolution] = None
    source_files: int = 0                 # files the source listing found, exclusions applied
    source_seconds: float = 0.0           # how long the source listing took


@dataclass(frozen=True)
class PreviewProgress:
    """Where a preview is: the phase, the destination (0 while the source is listed) and,
    while listing, the entries seen so far — while planning, the changed files whose
    signatures were read."""

    phase: Phase
    destination: int = 0
    entries: int = 0


# --- shared assessment ---------------------------------------------------------------

def _load(store: ConfigStore) -> StoreContents:
    result = store.load()
    if result.status in (LoadStatus.UNSUPPORTED, LoadStatus.CORRUPT):
        raise RunError(result.detail)
    return result.contents


def _job(contents: StoreContents, job_id: str) -> Job:
    job = contents.job(job_id)
    if job is None:
        raise RunError(f"no job {job_id}")
    return job


def _selected(job: Job, wanted: Optional[Sequence[int]], markers: Optional[Sequence[str]] = None) -> List[int]:
    """The numbers of the destinations asked for — by number or by marker, in the job as it
    stands — or of every one when neither is given."""
    if wanted is not None and markers is not None:
        raise RunError("destinations are named by number or by marker, not both")
    if wanted is None and markers is None:
        return list(range(1, len(job.destinations) + 1))
    try:
        numbers = ([job.destination_number(marker=marker) for marker in markers] if markers is not None
                   else [job.destination_number(n) for n in wanted])
    except ValueError as exc:
        raise RunError(str(exc)) from None
    return sorted(set(numbers))


def _markers(job: Job) -> List[str]:
    """The markers of the job's destinations, destination 1 first: what each number a run's
    status gives names, whatever the job's order becomes while the run works."""
    return [destination.marker_id for destination in job.destinations]


def _refused(report: ValidationReport, subject: str) -> Tuple[Finding, ...]:
    pattern = re.compile(rf"\b{re.escape(subject)}\b")
    return tuple(f for f in report.findings if f.severity is Severity.REFUSED and pattern.search(f.subject))


def _refusals(report: ValidationReport, subject: str) -> Tuple[str, ...]:
    return tuple(f.code for f in _refused(report, subject))


def _read_head(path: Path) -> Optional[bytes]:
    try:
        with open_source_file(path) as handle:
            return handle.read(LEADING_BYTES)
    except OSError:
        return None


def _stored_head(storage: DestinationStorage) -> Callable[[str], Optional[bytes]]:
    """A reader of the leading bytes of the destination's files, by their relative paths: None
    for a file that cannot be read, which is not judged; a remote's ``StorageError`` when the
    remote stopped answering (``storage.destination.raise_if_unanswered``), never taken for
    such a file."""
    def read(relative: str) -> Optional[bytes]:
        try:
            return storage.read_head(relative, LEADING_BYTES)
        except OSError as exc:
            raise_if_unanswered(storage, relative, exc)
            return None
    return read


@dataclass
class _Source:
    resolution: Resolution
    listing: Optional[TreeListing]
    outcome: Optional[Outcome] = None
    reasons: Tuple[str, ...] = ()
    detail: str = ""
    root: Optional[Path] = None              # the folder listed and copied from: live, or in a snapshot
    read: Optional[SourceRead] = None        # how it was read; None when it was not, and in a preview


def _assess_source(job: Job, volumes: Sequence[VolumeInfo], report: ValidationReport,
                   listed: Optional[ListingProgress] = None, reader: Optional[SourceReader] = None) -> _Source:
    resolution = resolve(job.source.endpoint, volumes)
    if resolution.presence is Presence.ABSENT:
        return _Source(resolution, None, Outcome.ABSENT, ("source-absent",), "the source's volume is not present")
    if resolution.presence is Presence.AMBIGUOUS:
        return _Source(resolution, None, Outcome.STOPPED, ("volume-ambiguous",), resolution.describe())
    refused = _refusals(report, "source")
    if refused:
        return _Source(resolution, None, Outcome.STOPPED, refused, "the job is refused at its source")
    read, root = reader(resolution) if reader is not None else (None, resolution.location)
    try:
        listing = list_tree(root, Side.SOURCE, drive_root=job.source.endpoint.is_drive_root,
                            entry_filter=EntryFilter.for_source(job.source), progress=listed)
    except ListingError as exc:
        return _Source(resolution, None, Outcome.STOPPED, ("source-unlistable",), str(exc), root, read)
    except RunCancelled as stop:
        return _Source(resolution, None, Outcome.CANCELLED, ("cancelled",), stop.detail, root, read)
    if not listing.present:
        return _Source(resolution, None, Outcome.STOPPED, ("source-missing",),
                       f"the source folder {resolution.location} does not exist", root, read)
    return _Source(resolution, listing, root=root, read=read)


def _freed(volume: VolumeInfo, cleanup: Optional[CleanupReport]) -> VolumeInfo:
    """The destination's volume measured before its cleanup, as the gate judges it: with the
    space the cleanup would free (a preview) or freed (a run whose drive cannot be measured
    again) counted as free. Files' logical sizes are counted, so the cluster slack they
    free too is margin."""
    if cleanup is None or not cleanup.bytes:
        return volume
    return replace(volume, free_bytes=volume.free_bytes + cleanup.bytes)


def _remote_space(storage: DestinationStorage, cleanup: Optional[CleanupReport] = None) -> Space:
    """A remote's room as the gate judges it: the free bytes its service reports now, with
    what ``cleanup`` would free counted as free unless the service may keep what is deleted
    (``CleanupReport.trash``); not judged where it reports none or answers that it cannot.
    ``StorageError`` when the service could not be reached or the engine gave no answer: a
    space that could not be asked is never taken for one that is unknown."""
    free = storage.free_bytes()
    if free is None:
        return Space(None)
    freed = cleanup.bytes if cleanup is not None and not cleanup.trash else 0
    return Space(free + freed)


def _measured(pair: "PairAssessment") -> Union[VolumeInfo, Space]:
    """The destination as a gate without a ``Room`` judges it: its volume as resolved, or a
    remote's space asked of its service now (``_remote_space``), with what the cleanup would
    free counted as free."""
    if pair.resolution is not None:
        return _freed(pair.resolution.volume, pair.cleanup)
    return _remote_space(pair.storage, pair.cleanup)


def _reach_remote(pair: "PairAssessment", report: ValidationReport, remotes: Optional[Reached]) -> bool:
    """Reach a remote pair's folder: its storage set and True, or its outcome set and False —
    absent while the service cannot be reached, stopped with the validation's own words
    where the validation refuses it."""
    remote = pair.destination.remote
    facts = remotes.facts(remote) if remotes is not None else None
    if facts is not None and facts.reach is Reach.UNREACHABLE:
        pair.outcome, pair.reasons = Outcome.ABSENT, (REMOTE_UNREACHABLE,)
        pair.detail = f"{remote.describe()}{_UNREACHED}{facts.reason}{_NEXT_RUN}"
        return False
    refused = _refused(report, f"destination {pair.index}")
    if refused:
        pair.outcome, pair.reasons = Outcome.STOPPED, tuple(f.code for f in refused)
        pair.detail = "; ".join(f.message for f in refused)
        return False
    pair.storage = remotes.storage(remote) if remotes is not None else None
    if pair.storage is None:                 # the validation refuses whatever leaves it unreached
        pair.outcome, pair.reasons = Outcome.ABSENT, (REMOTE_UNREACHABLE,)
        pair.detail = f"{remote.describe()} was not reached; the next run tries again"
        return False
    return True


def _encrypts(pair: "PairAssessment") -> Optional[bool]:
    """Whether the remote the pair reached encrypts; None for a drive or a remote not reached."""
    return pair.storage.encrypted if pair.destination.remote is not None and pair.storage is not None else None


_UNREACHED, _NEXT_RUN = " cannot be reached: ", "; the next run tries again"


def unreached_reason(detail: str) -> str:
    """The service's reason in a record's detail for a remote it could not reach; "" when it
    gives none."""
    return detail.partition(_UNREACHED)[2].removesuffix(_NEXT_RUN)


def _remote_failed(reason: str) -> str:
    """Why a remote pair failed once the run wrote there, as its record says it."""
    return (f"the remote stopped answering while the run worked: {reason}; what was done is recorded and the next run "
            f"converges")


def _fail_unworked(pair: "PairAssessment", failure: StorageError) -> "PairAssessment":
    """``pair`` failed because its remote stopped answering as it was assessed, before any of
    its plan was applied. A version cleanup may have run by then; its record says what it
    deleted."""
    pair.outcome, pair.reasons = Outcome.FAILED, (REMOTE_FAILED,)
    pair.detail = f"the remote stopped answering before any of the plan was applied: {failure.strerror}{_NEXT_RUN}"
    return pair


def _ambiguous(listing: TreeListing) -> str:
    """The stop's words for a remote listing whose names the plan cannot tell apart."""
    (first, second), more = listing.ambiguous[0], len(listing.ambiguous) - 1
    others = f" (and {counted(more, 'other such pair')})" if more else ""
    return (f"the remote holds names this tool cannot tell apart: {first} and {second}{others}; rename or remove one "
            f"of them on the service")


def _assess_pair(job: Job, index: int, source: _Source, volumes: Sequence[VolumeInfo], report: ValidationReport,
                 confirmation: Optional[PairConfirmation], on_phase: Callable[[Phase], None] = lambda p: None,
                 listed: Optional[ListingProgress] = None, versions: Optional[VersionsCare] = None,
                 examined: Optional[SignatureProgress] = None, room: Optional[Room] = None,
                 first_backup: bool = False, protected: Sequence[Contained] = (),
                 choose: Optional[Content] = None, remotes: Optional[Reached] = None) -> PairAssessment:
    """``listed`` and ``examined`` follow the destination's listing and the signature
    reads; a ``RunCancelled`` either raises ends the pair as cancelled. ``room`` gives the
    gate the destination's drive; without it the gate judges the volume as resolved, or a
    remote's space as its service reports it, with what the cleanup frees counted as free.
    ``first_backup``: the user's go-ahead for a first run onto this destination, should it
    hold nothing. ``protected``: the folders inside the destination no run may move or
    write into (``protected_folders``). ``choose``: a preview's — a first run onto what the
    source does not hold is planned both ways, ``plan`` by this choice unless moving is not
    offered. ``remotes``: the session a remote destination is reached through, and what the
    engine said of it."""
    destination = job.destinations[index - 1]
    pair = PairAssessment(index, destination)
    if source.outcome is not None:
        pair.outcome, pair.reasons, pair.detail = source.outcome, source.reasons, source.detail
        return pair
    if destination.remote is not None:
        if not _reach_remote(pair, report, remotes):
            return pair
    else:
        pair.resolution = resolve(destination.endpoint, volumes)
        if pair.resolution.presence is Presence.ABSENT:
            pair.outcome, pair.reasons, pair.detail = Outcome.ABSENT, ("destination-absent",), "not present"
            return pair
        if pair.resolution.presence is Presence.AMBIGUOUS:
            pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, ("volume-ambiguous",), \
                pair.resolution.describe()
            return pair
        pair.storage = DriveStorage(pair.resolution.location, pair.resolution.volume)
        refused = _refusals(report, f"destination {index}")
        if refused:
            pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, refused, \
                "the job is refused at this destination"
            return pair
    try:
        pair.marker, marker = marker_check(pair.storage, job.job_id, destination.marker_id)
    except StorageError as exc:              # only a remote raises it: it stopped answering
        return _fail_unworked(pair, exc)
    if pair.marker in (MarkerState.MISMATCH, MarkerState.UNREADABLE):
        pair.outcome, pair.reasons = Outcome.STOPPED, (f"marker-{pair.marker.value}",)
        pair.detail = ("this folder belongs to another job or another destination"
                       if pair.marker is MarkerState.MISMATCH else "the destination's marker cannot be read")
        return pair
    record = None
    if marker is not None and marker.shared:
        try:
            record = read_shared(pair.storage, job.job_id, destination.marker_id)
        except SharedLost as exc:
            pair.rebuild = str(exc)
        except SharedError as exc:
            pair.outcome, pair.reasons = Outcome.STOPPED, (SHARED_RECORD_UNREADABLE,)
            pair.detail = f"its record of what to leave where it is cannot be used: {exc}"
            return pair
        except StorageError as exc:          # only a remote raises it: it stopped answering
            return _fail_unworked(pair, exc)
    granted, content = None, None
    if (confirmation is not None and confirmation.first_run == pair.first_run
            and not (pair.rebuild and confirmation.content is Content.MOVE)):
        granted, content = confirmation.removals, confirmation.content
    whole = (pair.marker is MarkerState.MATCH and record is None and not pair.rebuild) or content is Content.MOVE
    entry_filter = EntryFilter.for_source(job.source)
    hit, hidden = _protection(protected, source.listing, record.own if record is not None else (), whole,
                              entry_filter)
    if hit is not None:
        pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, (hit[0].code,), hit[1]
        return pair
    if versions is not None and pair.marker is MarkerState.MATCH and job.version_cleanup.enabled:
        on_phase(Phase.CLEANING)
        pair.cleanup = versions(job, destination, pair.storage)
    leaving = record is not None or content is Content.LEAVE
    scope = None
    if leaving:
        scope = listing_scope(source.listing, *((record.own, record.left) if record is not None else ()))
    on_phase(Phase.LISTING)
    try:
        pair.listing = pair.storage.list_tree(drive_root=destination.endpoint.is_drive_root,
                                              entry_filter=entry_filter, progress=listed, scope=scope)
    except ListingError as exc:
        pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, ("destination-unlistable",), str(exc)
        return pair
    except RunCancelled as stop:
        pair.outcome, pair.reasons, pair.detail = Outcome.CANCELLED, ("cancelled",), stop.detail
        return pair
    if pair.listing.ambiguous:
        pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, (NAMES_AMBIGUOUS,), _ambiguous(pair.listing)
        return pair
    on_phase(Phase.PLANNING)
    window = max(time_window_ns(source.resolution.volume.file_system), pair.storage.time_window_ns)
    trust = not pair.first_run
    if leaving or pair.rebuild or (pair.first_run and protected and (granted is not None or first_backup)):
        pair.sharing = share(pair.listing, source.listing, *((record.own, record.left) if record is not None else ()))
    plan = _plan(source.listing, pair.listing, pair.sharing, window, trust, hidden)
    left_view = None
    if pair.rebuild:
        pair.content, pair.withheld = Content.LEAVE, REBUILD_WITHHELD
    elif choose is not None and pair.first_run and granted is None and not pair.listing.holds_nothing:
        left_view = share(pair.listing, source.listing)
        if not left_view.leaves_anything:
            left_view = None
    try:
        lost = frozenset(assess_signatures(plan, source.root, None, _read_head, _stored_head(pair.storage),
                                           examined))
    except RunCancelled as stop:
        pair.plan = plan
        pair.outcome, pair.reasons, pair.detail = Outcome.CANCELLED, ("cancelled",), stop.detail
        return pair
    except StorageError as exc:              # only a remote raises it: it stopped answering
        return _fail_unworked(pair, exc)
    pair.plan = plan.with_signature_losses(lost)
    holds_content = (not pair.listing.holds_nothing) if scope is None else (True if pair.first_run else None)
    try:
        measured = _measured(pair) if room is None else None
        with room(pair) if room is not None else nullcontext((measured, 0)) as (volume, reserved):
            pair.decision = judge(pair.plan, holds=job.holds, first_run=pair.first_run, destination=volume,
                                  confirmed=granted, reserved=reserved, first_backup=first_backup,
                                  holds_content=holds_content)
    except StorageError as exc:              # only a remote raises it: its space could not be asked
        return _fail_unworked(pair, exc)
    if left_view is not None:
        left_plan = _plan(source.listing, pair.listing, left_view, window, trust, hidden)
        left_plan = left_plan.with_signature_losses(lost & {c.destination.key for c in left_plan.changed})
        left = Alternative(Content.LEAVE, left_plan, judge(left_plan, holds=job.holds, first_run=True,
                                                           destination=measured if measured is not None
                                                           else _measured(pair), holds_content=True), left_view)
        moved = Alternative(Content.MOVE, pair.plan, pair.decision)
        pair.alternatives, pair.withheld = (left, moved), next((_withheld(f) for f in protected), "")
        chosen = moved if choose is Content.MOVE and not pair.withheld else left
        pair.content, pair.plan, pair.decision, pair.sharing = chosen.content, chosen.plan, chosen.decision, \
            chosen.sharing
    if pair.decision.verdict is Verdict.STOP:
        pair.outcome, pair.reasons, pair.detail = Outcome.STOPPED, pair.decision.reasons, pair.decision.detail
    elif pair.decision.verdict is Verdict.AWAIT_CONFIRMATION:
        pair.outcome, pair.reasons = Outcome.HELD, pair.decision.reasons
        pair.detail = pair.decision.describe()
    if pair.rebuild and pair.outcome is not None:
        pair.reasons += (SHARED_RECORD_LOST,)
        pair.detail = f"its record of what to leave where it is was lost ({pair.rebuild}); {pair.detail}"
    return pair


def _plan(source: TreeListing, destination: TreeListing, sharing: Optional[Sharing], window: int,
          trust: bool, hidden: FrozenSet[str]) -> PairPlan:
    """The pair's plan: of the whole destination, or of a shared one's part (``sharing``).
    ``hidden``: the keys of the entries the job's exclusions hide protected folders behind
    (``_protection``), which stay where they are like what a shared destination leaves."""
    if sharing is None:
        return build_plan(source, destination, time_window_ns=window, trust_temporaries=trust, leave=hidden)
    return build_plan(source, sharing.listing, time_window_ns=window, trust_temporaries=trust,
                      leave=sharing.left_keys | hidden)


def _context(job: Job, contents: StoreContents, volumes, sync_roots, store_path: Path, *,
             remote_setup: Optional[RemoteSetup] = None) -> ValidationContext:
    others = tuple(j for j in contents.jobs if j.job_id != job.job_id)
    return ValidationContext.on_this_computer(volumes, sync_roots, others, store_path, remote_setup=remote_setup)


def protected_folders(job: Job, index: int, context: ValidationContext) -> Tuple[Contained, ...]:
    """What no run of destination ``index`` (1-based) may move or write into: the
    synchronised folders and other jobs' sources strictly inside it
    (``validation.contained_folders``), and the folders Windows and this tool need
    (``context.system_folders``) inside it or equal to it."""
    destination = job.destinations[index - 1].endpoint
    found = list(contained_folders(job, index, context))
    for folder, inner in system_folder_endpoints(context.system_folders, context.volumes):
        relative = relative_inside(destination, inner)
        if relative is not None:
            found.append(Contained(SYSTEM_FOLDER_CODE, relative, folder.words))
    return tuple(found)


def _set_aside_lost_record(batch: VersionsBatch) -> None:
    """Move a lost shared record still on the destination into the run's versions batch,
    so the record made again replaces nothing: what it held stays there for the job's
    version cleanup, like everything else a run moves aside."""
    relative = f"{MANAGEMENT_DIR}\\{SHARED_FILE}"
    if batch.storage.exists(relative):
        batch.move_in(relative)


def _withheld(folder: Contained) -> str:
    """Why moving what a destination holds into versions is not offered, as a clause."""
    return f"{folder.words}, is inside this folder" if folder.relative else f"this folder is {folder.words}"


LEAVE_BY_EXCLUDING = "excluding it from the job leaves it where it is"
"""How the user keeps a run from stopping at a folder it protects, as the stop says it."""


def _protection(protected: Sequence[Contained], source: TreeListing, own: Sequence[str], whole: bool,
                entry_filter: EntryFilter) -> Tuple[Optional[Tuple[Contained, str]], FrozenSet[str]]:
    """The protected folder a run of this pair would move or write into, with why, or
    None; and the keys of the entries ``entry_filter`` hides protected folders behind,
    which the plan keeps where they are. ``whole``: the destination is the job's whole, or
    its first run was confirmed to make it so — its runs move into versions whatever the
    source does not hold."""
    names = {path_key(name): name for name in own}
    names.update(top_names(source))
    hidden: Set[str] = set()
    for folder in protected:
        behind = entry_filter.hiding(folder.relative, True)
        if behind is not None:
            key = path_key(behind)
            filed = next((k for k in (key, *parent_keys(key)) if k in source.files), None)
            if filed is not None:
                return (folder, f"{folder.words}, lies inside this destination, and the source holds a file at "
                                f"{source.files[filed].path}: copying it would move the folder into versions"), \
                    frozenset()
            hidden.add(key)
        elif whole:
            if not folder.relative:
                return (folder, f"{folder.words}, is this destination, and its run would move it into versions"), \
                    frozenset()
            return (folder, f"{folder.words}, lies inside this destination, and its run would move it into versions; "
                            f"{LEAVE_BY_EXCLUDING}"), frozenset()
        elif folder.top is not None and folder.top in names:
            return (folder, f"{folder.words}, lies inside this destination beneath {names[folder.top]}, which this "
                            f"job backs up there; {LEAVE_BY_EXCLUDING}"), frozenset()
    return None, frozenset(hidden)


# --- preview -------------------------------------------------------------------------

def preview_job(store: ConfigStore, job_id: str, *, volume_source: VolumeSource, sync_roots: Sequence[SyncRoot],
                destinations: Optional[Sequence[int]] = None, markers: Optional[Sequence[str]] = None,
                progress: Optional[Callable[[PreviewProgress], None]] = None,
                content: Content = Content.LEAVE) -> JobPreview:
    """What a run of the job would do now. Changes nothing but the job's ticket.

    ``destinations`` names the destinations to preview by number, ``markers`` by marker,
    either found in the job as this preview reads it; neither, every one.
    ``content``: what each first run onto what the source does not hold does with it,
    where both are offered; its ticket confirms that, and ``ticket_for`` the other.
    ``progress`` is told where the preview is; an exception it raises ends the preview
    before any ticket is written and reaches the caller unchanged."""
    report_progress = progress or (lambda p: None)
    contents = _load(store)
    job = _job(contents, job_id)
    indexes = _selected(job, destinations, markers)
    volumes = volume_source.scan().volumes
    remote = [d.remote for d in job.destinations if d.remote is not None]   # validation judges every destination
    remotes = reach(contents.preferences, store.path, remote) if remote else None
    try:
        return _preview(store, job, indexes, volumes, sync_roots, contents, remotes, report_progress, content)
    finally:
        if remotes is not None:
            remotes.close()


def _preview(store: ConfigStore, job: Job, indexes: Sequence[int], volumes: Sequence[VolumeInfo],
             sync_roots: Sequence[SyncRoot], contents: StoreContents, remotes: Optional[Reached],
             report_progress: Callable[[PreviewProgress], None], content: Content) -> JobPreview:
    """``preview_job`` once its remote destinations, if any, are reached (``remotes``)."""
    layout = runtime_layout(store.path)
    context = _context(job, contents, volumes, sync_roots, store.path,
                       remote_setup=remotes.setup if remotes is not None else None)
    report = validate_job(job, context, None)
    report_progress(PreviewProgress(Phase.LISTING))
    source = _assess_source(job, volumes, report, lambda n: report_progress(PreviewProgress(Phase.LISTING, 0, n)))
    history = read_records(layout, job.job_id)
    previews = []
    for index in indexes:
        pair = _assess_pair(job, index, source, volumes, report, None,
                            lambda p, i=index: report_progress(PreviewProgress(p, i)),
                            lambda n, i=index: report_progress(PreviewProgress(Phase.LISTING, i, n)),
                            versions=lambda j, _d, storage: plan_cleanup(storage, j.version_cleanup.keep_days),
                            examined=lambda n, _total, i=index: report_progress(PreviewProgress(Phase.PLANNING, i, n)),
                            protected=protected_folders(job, index, context), choose=content, remotes=remotes)
        guess = None
        judged = pair.plan is not None and pair.decision is not None    # a remote may fail between the two
        if judged:
            guess = estimate(pair.plan.bytes_to_copy if pair.decision.applies_removals or pair.first_run
                             else pair.plan.bytes_new, sum(pair.plan.listing_seconds),
                             throughputs(history, pair.destination.marker_id))
        back = None
        if judged and pair.marker is MarkerState.MATCH and not pair.first_run:
            back = put_back(pair.plan, pair.storage,
                            lambda i=index: report_progress(PreviewProgress(Phase.PLANNING, i)))
            if back is not None:
                back = replace(back, keeper=_keeper(job, index, report, context))
        previews.append(PairPreview(pair, guess, preview_notices(report, index), back))
    confirmations = _confirmations(previews, {})
    ticket = None
    if confirmations:
        ticket = new_ticket(job.job_id, confirmations)
        save_ticket(layout, ticket)
    return JobPreview(job, tuple(previews), ticket, source.detail if source.outcome is not None else "",
                      source=source.resolution, source_files=source.listing.file_count if source.listing else 0,
                      source_seconds=source.listing.seconds if source.listing else 0.0)


def _keeper(job: Job, index: int, report: ValidationReport, context: ValidationContext) -> str:
    """The cloud client keeping a copy of destination ``index``'s folder, where the job's
    report notices one (``destination-in-cloud-copy``); "" where it does not."""
    subject = f"destination {index}"
    if not any(f.code == DESTINATION_IN_CLOUD_COPY and f.subject == subject for f in report.findings):
        return ""
    destination = job.destinations[index - 1].endpoint
    return next((root.provider for root, folder in sync_root_endpoints(context.sync_roots, context.volumes)
                 if root.kind is SyncKind.CLOUD_COPY and folder.contains(destination)), "")


def _confirmations(previews: Sequence[PairPreview], choices: Mapping[int, Content]) -> Dict[str, PairConfirmation]:
    """What a ticket confirms of each previewed pair that can run: its removals and, for a
    first run onto what the source does not hold, the choice — ``choices`` (destination
    index -> choice) where it names one, else the preview's. RunError for moving where the
    preview did not offer it."""
    found = {}
    for item in previews:
        pair = item.assessment
        plan, decision, content = pair.plan, pair.decision, pair.content
        wanted = choices.get(pair.index)
        if wanted is Content.MOVE and pair.withheld and pair.content is not Content.MOVE:
            raise RunError(f"destination {pair.index}: moving what it holds into versions is not offered: "
                           f"{pair.withheld}")
        if wanted is not None and pair.alternatives and wanted is not pair.content:
            chosen = next(a for a in pair.alternatives if a.content is wanted)
            plan, decision, content = chosen.plan, chosen.decision, chosen.content
        if plan is not None and decision is not None and decision.verdict is not Verdict.STOP:
            found[pair.destination.marker_id] = PairConfirmation(pair.first_run, plan.removal_keys(), content)
    return found


def ticket_for(store: ConfigStore, preview: JobPreview, choices: Mapping[int, Content]) -> Optional[Ticket]:
    """A ticket confirming ``preview`` with each first run onto what the source does not
    hold confirmed as ``choices`` names it (destination index -> choice; the preview's own
    choice where none is named) — what the user chose after seeing both — saved in place of
    the job's ticket; None when no pair can run. Raises RunError for moving where the
    preview did not offer it."""
    confirmations = _confirmations(preview.pairs, choices)
    if not confirmations:
        return None
    ticket = new_ticket(preview.job.job_id, confirmations)
    save_ticket(runtime_layout(store.path), ticket)
    return ticket


# --- run -----------------------------------------------------------------------------

def _counts_planned(plan: Optional[PairPlan]) -> Dict[str, int]:
    counts = dict.fromkeys(PLANNED_KEYS, 0)
    if plan is None:
        return counts
    counts.update(new_files=len(plan.new), changed_files=len(plan.changed), files_to_copy=plan.files_to_copy,
                  bytes_to_copy=plan.bytes_to_copy, deleted_files=len(plan.deleted), renames=len(plan.renames),
                  unchanged=plan.unchanged, kept=len(plan.kept), temporaries=len(plan.temporaries),
                  destination_files=plan.destination_files)
    return counts


def _counts_done(result: Optional[ApplyResult]) -> Dict[str, int]:
    counts = dict.fromkeys(DONE_KEYS, 0)
    if result is not None:
        counts.update(files_copied=result.files_copied, bytes_copied=result.bytes_copied,
                      moved_to_versions=result.moved_to_versions, renamed=result.renamed,
                      temporaries_removed=result.temporaries_removed, files_identical=result.files_identical,
                      bytes_identical=result.bytes_identical)
    return counts


def _counts_left(plan: Optional[PairPlan], result: Optional[ApplyResult]) -> Dict[str, int]:
    """What the plan still holds. A changed file found identical is done: nothing is left
    to copy of it, and its destination copy no longer needs to move into versions."""
    counts = dict.fromkeys(LEFT_KEYS, 0)
    if plan is None:
        return counts
    done = result or ApplyResult()
    counts.update(files_to_copy=max(0, plan.files_to_copy - done.files_copied - done.files_identical),
                  bytes_to_copy=max(0, plan.bytes_to_copy - done.bytes_copied - done.bytes_identical),
                  removals=max(0, plan.removal_count - done.removals_done - done.files_identical),
                  renames=max(0, len(plan.renames) - done.renamed))
    return counts


def _writing(volume: VolumeIdentity, plan: PairPlan, decision: GateDecision) -> Optional[dict]:
    """What a pair's gate let it copy onto its destination's drive, published for the space
    checks of other runs; None when it copies nothing."""
    if decision.applies_removals:
        files, size = plan.files_to_copy, plan.bytes_to_copy
    elif decision.applies_additions:
        files, size = len(plan.new), plan.bytes_new
    else:
        return None
    return {"volume": volume.to_document(), "files": files, "bytes": size}


def reserved_on(layout: RuntimeLayout, volume: VolumeIdentity, besides: str) -> int:
    """The bytes, with the gate's margin, other live activities still have to write on
    ``volume``: what each published it may copy there (a run's gate, a repair's copies),
    less what it has done. A share whose drive cannot be read is counted wherever it may be."""
    total = 0
    for observed in observe_runs(layout):
        status = observed.status
        writing = status.get("writing")
        if observed.alive and not observed.readable:
            diagnostics.note(f"the status of run {observed.run_id} stayed unreadable; what it still has to write "
                             "is not counted")
        if not observed.alive or status.get("run_id") == besides or not isinstance(writing, dict):
            continue
        try:
            elsewhere = VolumeIdentity.from_document(writing.get("volume")) != volume
        except ContractError:
            elsewhere = False
        if elsewhere:
            continue
        done = status.get("done") or {}
        try:
            files = int(writing["files"]) - int(done.get("files_copied", 0)) - int(done.get("files_identical", 0))
            size = int(writing["bytes"]) - int(done.get("bytes_copied", 0)) - int(done.get("bytes_identical", 0))
        except (KeyError, TypeError, ValueError):
            continue
        total += space_needed(max(0, size), max(0, files))
    return total


class _Run:
    """One invocation of ``run_job``: its status, control and records."""

    def __init__(self, store: ConfigStore, job: Job, options: RunOptions, indexes: List[int], say: Say,
                 snapshots: SnapshotProvider, power: Optional[System] = None):
        self.store = store
        self.layout: RuntimeLayout = runtime_layout(store.path)
        self.job = job
        self.options = options
        self.indexes = indexes
        # The destinations asked for, by marker: the numbers name them in the job as it stood
        # when the run began, and the job may be reordered while the run waits its turn.
        self.chosen: Optional[Tuple[str, ...]] = (None if options.destinations is None and options.markers is None
                                                  else tuple(job.destinations[i - 1].marker_id for i in indexes))
        self.say = say
        self.run_id = uuid.uuid4().hex
        self.trigger = options.trigger
        self.status = LiveStatus(self.run_id, os.getpid(), job.job_id, job.name, self.trigger, utc_now(),
                                 pair_total=len(indexes), selected=list(indexes), markers=_markers(job))
        self.board = StatusBoard(self.layout, self.status)
        self.control = RunControl(self.status)
        self.hold = Hold(run_reason(job.name), lambda: self.status.admitted and not self.status.paused,
                         lambda: _load(store).preferences.activities, system=power)
        self.pump = StatusPump(self.board, self.control, on_tick=self.hold.tick)
        self.snapshot = SourceSnapshot(self.layout, self.run_id, snapshots)
        self.records: List[RunRecord] = []
        self.history: List[RunRecord] = []      # the job's records, this run's included: streaks are counted on them
        self.notes: List[str] = []              # told once, in the first record this run writes
        self.engine: Optional[Engine] = None
        self.volume_source: Optional[VolumeSource] = None
        self.remotes: Optional[Reached] = None  # the session the selected remote destinations are reached through

    # --- records -----------------------------------------------------------------

    def record(self, index: int, outcome: Outcome, *, reasons=(), detail="", started=None, plan=None, result=None,
               skipped=(), unbacked=(), errors=(), source_read: Optional[SourceRead] = None,
               destination_root: Optional[Path] = None, cleanup: Optional[CleanupReport] = None,
               space: Optional[Tuple[int, int]] = None, encrypted: Optional[bool] = None) -> RunRecord:
        """``skipped``: (path, why) of every file the run left out; ``unbacked``: the paths
        among them a later run could still back up; ``space``: (free, capacity) of the
        destination's drive when the pair ended; ``encrypted``: whether a remote the pair
        reached encrypts, which the window reads here since it never asks a remote."""
        destination = self.job.destinations[index - 1]
        self.status.writing = None
        listed, total = capped(skipped)
        streaks, _ = capped(unbacked_streaks(dict.fromkeys(unbacked), last_counted(self.history,
                                                                                   destination.marker_id)))
        notes, self.notes = self.notes, []
        record = RunRecord(self.run_id, self.job.job_id, self.job.name, destination.marker_id, index, self.trigger,
                           started or utc_now(), utc_now(), outcome, tuple(reasons), detail,
                           self.engine.version if self.engine else None, _counts_planned(plan), _counts_done(result),
                           _counts_left(plan, result), result.versions_batch if result else None, listed, total,
                           tuple((notes + list(errors))[:200]), plan.listing_seconds if plan else (0.0, 0.0),
                           result.transfer_seconds if result else 0.0, source_read, streaks,
                           cleanup.cleaned() if cleanup is not None else None, space, encrypted)
        write_record(self.layout, record, destination_root)
        self.records.append(record)
        self.history.append(record)
        self.status.finished.append([index, outcome.value])
        self.say(f"destination {index}: {outcome.value}" + (f" ({', '.join(reasons)})" if reasons else "")
                 + (f" - {detail}" if detail else ""))
        return record

    def record_unexpected(self, indexes: Sequence[int], exc: Exception, context: str) -> None:
        """A failure no one foresaw: its traceback to the diagnostic log, and a failed
        record for each of ``indexes`` not recorded yet, so the history shows it."""
        diagnostics.note_failure(f"run {self.run_id}: {context}")
        recorded = {r.destination_index for r in self.records}
        self.record_rest([i for i in indexes if i not in recorded], Outcome.FAILED, ("unexpected-error",),
                         f"{type(exc).__name__}: {exc}")

    def record_rest(self, indexes: Sequence[int], outcome: Outcome, reasons, detail: str) -> None:
        for index in indexes:
            self.record(index, outcome, reasons=reasons, detail=detail)

    def _listed(self, entries: int) -> None:
        """A listing's progress: published, and a safe point."""
        self.status.listed = entries
        if self.control.checkpoint() is not None:
            raise RunCancelled("cancelled while listing")

    def _examined(self, examined: int, total: int) -> None:
        """The signature reads' progress: published, and a safe point."""
        self.status.examined, self.status.to_examine = examined, total
        if self.control.checkpoint() is not None:
            raise RunCancelled("cancelled while planning")

    def _clean_versions(self, job: Job, destination: Destination, storage: DestinationStorage) -> CleanupReport:
        """The job's version cleanup on one verified destination; a pause holds it between two
        batches and a cancel leaves the rest for the next run."""
        report = clean_versions(storage, job.version_cleanup.keep_days, job_id=job.job_id,
                                marker_id=destination.marker_id, stop=lambda: self.control.checkpoint() is not None)
        if report.batches:
            self.say(f"deleted {counted(len(report.batches), 'versions batch', 'versions batches')} older than "
                     f"{counted(report.keep_days, 'day')} ({counted(report.bytes, 'byte')})")
        for problem in report.problems:
            self.say(f"version cleanup: {problem}")
        return report

    def _measure(self, pair: PairAssessment) -> Optional[VolumeInfo]:
        """The pair's destination drive as it is now; None when it is not present now or the
        volumes cannot be scanned (a measurement: the caller keeps the earlier one)."""
        try:
            volumes = self.volume_source.scan().volumes
        except Exception:
            return None
        now = resolve(pair.destination.endpoint, volumes)
        return now.volume if now.presence is Presence.PRESENT else None

    def _space(self, pair: PairAssessment) -> Optional[Tuple[int, int]]:
        """The free space and capacity of the pair's destination drive now, when it is
        present; a remote's as its service reports both, when it does."""
        if pair.resolution is None:
            return pair.storage.space() if pair.storage is not None else None
        if pair.resolution.presence is not Presence.PRESENT:
            return None
        volume = self._measure(pair) or pair.resolution.volume
        return volume.free_bytes, volume.capacity_bytes

    @contextmanager
    def _room(self, pair: PairAssessment) -> Iterator[Tuple[Union[VolumeInfo, Space], int]]:
        """The gate's space check with runs at the same time: the destination's drive
        measured now — what the cleanup freed and what other runs wrote are in it — and what
        other live runs still have to write there, counted as used. Held under the admission
        lock until this pair's own share is published, so no two runs judge one drive at the
        same instant; a share that cannot be published fails the pair. A remote's space is
        asked of its service now, and nothing is counted or published for it: every other
        activity that reaches a remote waits for this one (``records.admission``)."""
        if pair.resolution is None:
            yield _remote_space(pair.storage), 0
            return
        with holding(self.layout.admission_lock):
            identity = pair.resolution.volume.identity
            reserved = reserved_on(self.layout, identity, besides=self.run_id)
            yield self._measure(pair) or _freed(pair.resolution.volume, pair.cleanup), reserved
            self.status.writing = _writing(identity, pair.plan, pair.decision)
            self.board.publish(patience=PUBLISH_PATIENCE)

    # --- the run -----------------------------------------------------------------

    def execute(self, volume_source: VolumeSource, sync_roots: Sequence[SyncRoot]) -> None:
        self.volume_source = volume_source
        self.board.open()
        offer_tray(self.layout, self.store.path)
        self.pump.start()
        diagnostics.note(f"run {self.run_id}: job '{self.job.name}' ({self.job.job_id}), destinations "
                         f"{', '.join(map(str, self.indexes))}, {self.trigger}")
        diagnostics.mark(diagnostics.ACTIVITY, id=self.run_id, kind=KIND_RUN)
        try:
            self._execute(sync_roots)
        except (RunError, KeyboardInterrupt):
            raise
        except Exception as exc:  # a run that fails unexpectedly outside a pair still leaves a record of each
            self.record_unexpected(self.indexes, exc, "failed unexpectedly")
        finally:
            self._release_snapshot()
            if self.remotes is not None:
                self.remotes.close()
            self.pump.stop()
            self.hold.release()
            self.board.close()

    def _execute(self, sync_roots: Sequence[SyncRoot]) -> None:
        admitted = self._admit(self.volume_source)
        if admitted is None:
            self.record_rest(self.indexes, Outcome.CANCELLED, ("cancelled-while-waiting",),
                             "cancelled before it started")
            return
        contents, volumes = admitted
        self.status.phase, self.status.listed = Phase.LISTING, 0
        self.notes = remove_leftovers(self.layout, self.snapshot.provider, current_run=self.run_id)
        for note in self.notes:
            self.say(note)
        self.history = read_records(self.layout, self.job.job_id)
        ticket = None
        if self.options.ticket_id:
            try:
                ticket = load_ticket(self.layout, self.job.job_id, self.options.ticket_id)
            except TicketError as exc:
                raise RunError(str(exc)) from None
        self._pairs(contents, volumes, sync_roots, ticket)
        if ticket is not None:
            discard_ticket(self.layout, self.job.job_id, ticket.ticket_id)

    def _admit(self, volume_source: VolumeSource) -> Optional[Tuple[StoreContents, Tuple[VolumeInfo, ...]]]:
        """Wait for this run's turn (``records.admission``). The store and the volumes as they
        are at the attempt that admits it are what the run claims and works with — the
        drives it takes turns on as the store's preferences and their kinds say, and what it asks of
        the computer while it works. None when it is cancelled while it waits. A job file
        another program holds is read again at the next attempt."""
        while self.control.cancel is None:
            try:
                contents = _load(self.store)
            except StoreBusy:
                self.control.wait_change(ADMISSION_INTERVAL)
                continue
            job = contents.job(self.job.job_id)
            if job is None:
                raise RunError("the job was removed while this run waited")
            indexes = self._indexes_in(job)
            volumes = tuple(volume_source.scan().volumes)
            claim = activity_claim(job, indexes, volumes, contents.preferences.activities)
            if admit(self.board, claim).admitted:
                self.job, self.indexes, self.status.pair_total = job, indexes, len(indexes)
                self.status.selected, self.status.markers = list(indexes), _markers(job)
                self.hold.choose(contents.preferences.activities)
                return contents, volumes
            self.control.wait_change(ADMISSION_INTERVAL)
        return None

    def _indexes_in(self, job: Job) -> List[int]:
        """The numbers in ``job`` of the destinations this run serves: every one, or those
        asked for, found by marker wherever the job's order has put them. RunError when one
        asked for is no longer in the job."""
        if self.chosen is None:
            return _selected(job, None)
        numbers = {destination.marker_id: n for n, destination in enumerate(job.destinations, 1)}
        if any(marker not in numbers for marker in self.chosen):
            raise RunError("a destination this run was asked for was removed from the job while it waited")
        return sorted(numbers[marker] for marker in self.chosen)

    def _release_snapshot(self) -> None:
        if self.snapshot.snapshot is None:
            return
        self.status.phase = Phase.RELEASING
        problem = self.snapshot.release()
        if problem:
            self.say(problem)

    def _read_source(self, resolution: Resolution, volumes: Sequence[VolumeInfo]) -> Tuple[SourceRead, Path]:
        """Make the snapshot the source is read from, when it can be; a snapshot is only
        made when a selected destination is present to copy to — a drive attached, or a
        remote the engine reached."""
        self.status.phase = Phase.SNAPSHOT
        needed = any(self._copies_to(self.job.destinations[i - 1], volumes) for i in self.indexes)
        read = self.snapshot.take(self.job, resolution.volume, needed=needed)
        self.say(f"source {read.describe()}")
        self.status.phase, self.status.listed = Phase.LISTING, 0
        return read, self.snapshot.root(self.job.source.endpoint.path) or resolution.location

    def _copies_to(self, destination: Destination, volumes: Sequence[VolumeInfo]) -> bool:
        if destination.remote is not None:
            return self.remotes is not None and self.remotes.storage(destination.remote) is not None
        return resolve(destination.endpoint, volumes).presence is Presence.PRESENT

    def _pairs(self, contents: StoreContents, volumes: Sequence[VolumeInfo], sync_roots: Sequence[SyncRoot],
               ticket: Optional[Ticket]) -> None:
        status = self.status
        try:
            self.engine = open_engine(contents.preferences.engine_path)
            status.engine_version = self.engine.version
        except EngineError as exc:
            self.record_rest(self.indexes, Outcome.FAILED, ("engine-unusable",), str(exc))
            return
        remote = [self.job.destinations[i - 1].remote for i in self.indexes
                  if self.job.destinations[i - 1].remote is not None]
        if remote:
            self.remotes = reach(contents.preferences, self.store.path, remote, engine=self.engine)
        context = _context(self.job, contents, volumes, sync_roots, self.store.path,
                           remote_setup=self.remotes.setup if self.remotes is not None else None)
        report = validate_job(self.job, context, None)
        status.phase, status.listed = Phase.LISTING, 0
        self.say(f"listing the source of '{self.job.name}'")
        source = _assess_source(self.job, volumes, report, self._listed,
                                reader=lambda resolution: self._read_source(resolution, volumes))
        for position, index in enumerate(self.indexes):
            if self.control.checkpoint() is not None:
                self.record_rest(self.indexes[position:], Outcome.CANCELLED, ("cancelled",), "not started")
                return
            confirmation = ticket.pairs.get(self.job.destinations[index - 1].marker_id) if ticket else None
            try:
                self._pair(index, source, volumes, report, confirmation,
                           protected_folders(self.job, index, context))
            except KeyboardInterrupt:
                self.record(index, Outcome.INTERRUPTED, reasons=("interrupted-at-console",),
                            detail="the run was interrupted at the console; the next run converges")
                raise
            except Exception as exc:  # a run that fails unexpectedly still leaves a record
                self.record_unexpected([index], exc, f"destination {index} failed unexpectedly")
            if self.control.cancel is not None:
                self.record_rest(self.indexes[position + 1:], Outcome.CANCELLED, ("cancelled",), "not started")
                return

    def _pair(self, index: int, source: _Source, volumes, report: ValidationReport,
              confirmation: Optional[PairConfirmation], protected: Sequence[Contained]) -> None:
        status = self.status
        destination = self.job.destinations[index - 1]
        started = utc_now()
        status.pair_index, status.marker_id, status.pair_started = index, destination.marker_id, started
        status.planned, status.done = dict.fromkeys(PLANNED_KEYS, 0), dict.fromkeys(DONE_KEYS, 0)
        status.destination = f"destination {index}"
        status.paused, status.cancelling = False, None

        def phase(p: Phase) -> None:
            status.phase, status.listed, status.examined, status.to_examine = p, 0, 0, 0

        pair = _assess_pair(self.job, index, source, volumes, report, confirmation, phase, self._listed,
                            versions=self._clean_versions, examined=self._examined, room=self._room,
                            first_backup=self.options.first_backup, protected=protected, remotes=self.remotes)
        status.examined, status.to_examine = 0, 0
        if pair.storage is not None:
            status.destination = f"destination {index} at {pair.storage.describe()}"
        plan = pair.plan
        status.planned = _counts_planned(plan)
        source_skipped = plan.source_skipped if plan else ()
        left_out = plan.left_out if plan else ()
        source_skips = [(s.path, s.reason.value) for s in source_skipped] + [(s.path, s.detail) for s in left_out]
        source_unbacked = ([s.path for s in source_skipped if s.reason in UNBACKED_SOURCE_SKIPS]
                           + [s.path for s in left_out])
        if pair.outcome is not None:
            self.record(index, pair.outcome, reasons=pair.reasons, detail=pair.detail, started=started, plan=plan,
                        skipped=source_skips, unbacked=source_unbacked, source_read=source.read,
                        cleanup=pair.cleanup, space=self._space(pair), encrypted=_encrypts(pair))
            return
        decision = pair.decision
        self.say(f"destination {index}: {decision.describe()}")
        sharing = pair.sharing
        storage = pair.storage
        try:
            batch = VersionsBatch(storage, datetime.now(timezone.utc))
            if pair.rebuild:
                self.say(f"destination {index}: its record of what to leave where it is was lost ({pair.rebuild}); "
                         "it is made again")
                _set_aside_lost_record(batch)
            if sharing is not None and (pair.first_run or sharing.changed):
                write_shared(storage, SharedRecord(self.job.job_id, destination.marker_id, sharing.own, sharing.left))
            if pair.first_run:
                write_marker(storage, Marker(self.job.job_id, destination.marker_id, shared=sharing is not None))
            storage.write_file(f"{MANAGEMENT_DIR}\\{DEFINITION_FILE}",
                               self.store.export_text([self.job]).encode("utf-8"))
        except StorageError as exc:          # only a remote raises it: it stopped answering
            self.record(index, Outcome.FAILED, reasons=(REMOTE_FAILED,), detail=_remote_failed(exc.strerror),
                        started=started, plan=plan, skipped=source_skips, unbacked=source_unbacked,
                        source_read=source.read, cleanup=pair.cleanup, space=self._space(pair),
                        encrypted=_encrypts(pair))
            return
        result = apply_pair(plan, decision, source_root=source.root, destination_root=storage,
                            batch=batch, engine=self.engine, control=self.control, pump=self.pump,
                            list_path=engine_list_path(self.layout, self.run_id),
                            bandwidth_limit=self.options.bandwidth_limit)
        status.phase = Phase.FINISHING
        skipped = source_skips + [(s.path, s.detail or s.reason.value) for s in plan.blocked] + result.skipped
        unbacked = source_unbacked + [path for path, _why in result.skipped]
        if result.cancelled is not None:
            outcome, reasons = Outcome.CANCELLED, (f"cancelled-{result.cancelled.value}",)
            detail = ("stopped before the copy began" if not result.transfer_seconds
                      else "stopped after the files in flight" if result.cancelled is CancelMode.GRACEFUL
                      else "stopped at once")
        elif result.destination_failure:
            outcome, reasons, detail = Outcome.FAILED, (REMOTE_FAILED,), _remote_failed(result.destination_failure)
        elif result.engine_failure:
            outcome, reasons, detail = Outcome.FAILED, ("engine-failed",), result.engine_failure
        elif decision.verdict is Verdict.ADDITIONS_ONLY:
            outcome, reasons, detail = Outcome.HELD, decision.reasons, decision.describe()
        else:
            outcome, reasons, detail = Outcome.COMPLETED, (), ""
        self.record(index, outcome, reasons=reasons, detail=detail, started=started, plan=plan, result=result,
                    skipped=skipped, unbacked=unbacked, errors=result.errors, source_read=source.read,
                    destination_root=storage, cleanup=pair.cleanup, space=self._space(pair),
                    encrypted=_encrypts(pair))


def run_job(store: ConfigStore, job_id: str, options: RunOptions = RunOptions(), *, volume_source: VolumeSource,
            sync_roots: Sequence[SyncRoot], say: Optional[Say] = None,
            snapshots: Optional[SnapshotProvider] = None, power: Optional[System] = None) -> JobRunResult:
    """Run the job's selected pairs. Raises RunError when the run cannot start.

    ``snapshots``: where volume snapshots come from; Windows' own unless given. ``power``: what
    keeps the computer awake and names the run at shutdown while it works; Windows' own unless
    given."""
    contents = _load(store)
    job = _job(contents, job_id)
    if options.unattended and (options.ticket_id or options.first_backup):
        raise RunError("an unattended run confirms nothing; confirm a preview or a first backup in an attended run")
    if options.ticket_id and options.first_backup:
        raise RunError("a run confirms either a preview's ticket or first backups, not both")
    if options.automatic is not None and (options.automatic not in AUTOMATIC_TRIGGERS or not options.unattended):
        raise RunError(f"an automatic run is unattended and started by one of {', '.join(AUTOMATIC_TRIGGERS)}")
    indexes = _selected(job, options.destinations, options.markers)
    run = _Run(store, job, options, indexes, say or (lambda text: None), snapshots or default_snapshot_provider(),
               power)
    if options.unattended and not job.enabled:
        return JobRunResult(run.run_id, (), disabled=True)
    run.execute(volume_source, sync_roots)
    return JobRunResult(run.run_id, tuple(run.records))
