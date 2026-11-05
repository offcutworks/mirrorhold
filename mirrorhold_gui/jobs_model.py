"""The main window's job list: a row per group, per job and per destination, in words. No toolkit.

A row is computed from what is known at one moment — the jobs, the present volumes,
each present destination's marker, the run records and the runs in progress — so the
list, its status words and the actions it enables never disagree with each other.

The list's top level (``arrange``) is each group, in the store's order, holding its
jobs in the group's order, and then the jobs in no group. A group's status is its most
pressing job's — a job at work, then one needing review, a problem, one needing
attention, any other word but "Up to date" — named when it is the only one at that
level ("Phone: Running"), counted when several share its words ("Running (2 jobs)"),
else named with how many more ("Phone: Running (+1)"); "Up to date" when every job is.
Its last success is its oldest job's, "never" while any has none: the stalest backup is
the one the user needs to see.

Destination status, most specific first: a run in progress that serves it (a run names
the destinations it serves; one that names none, an older copy's, serves every one),
and while that run waits its turn, its place in line, whom it waits for and the drive
they share when that alone is why ("2nd in line, waiting for 'Work' (drive I:)") — the
place first, as a narrow Status column cuts the words from their end — or "Starting"
before it has asked for its turn and as it is admitted; a verification of it in
progress, with the share it has compared ("Verifying 42 %", "Verifying new copies
42 %"), or waiting its turn alike; a
repair of it in progress ("Replacing copies"), or waiting its turn; its
drive two drives match (refused); its drive not attached; a marker naming another job,
or unreadable; no marker yet — its first run waits for the user: a folder that holds
nothing (or does not exist yet) for the go-ahead of a first backup, which the window
asks in one question (``first_backup_question``), a folder that holds something for a
review of it in the preview, and one whose contents are not known yet for a
confirmation, as the preview gives it — else its last record's outcome, a shared
destination whose record was lost waiting for a review that makes it again ("Record
lost: review") — a completed run
that left files unbacked-up for two runs in a row or more says how many, in the
attention tone — and, unless that outcome is a problem or needs review, copies its latest
finished verification found to differ, in the problem tone, until a repair has replaced
every one of them (``maintenance.repair.all_replaced``); else, where its words would
be in the normal tone, its pass waiting to be continued — its unfinished full pass, else
its unfinished pass of new copies ("Verification failed at 42 %", "Verification of new
copies stopped at 42 %": stopped or interrupted in the attention tone, failed in the
problem tone), which *Continue verification* takes up. A destination whose latest finished
pass found copies that differ offers *Replace backup copies...* while a repair of them can
start (``can_repair``). A job's status is its most urgent destination's;
"Files not backed up" when that is all there is to say. A job whose latest start of a
run, verification or repair left no record — refused, failed before it recorded anything, or its
process gone without a word, as its log says (``records.diagnostics``) — reads so in the
problem tone ("Run could not start - see History") until a record is written after it,
unless something in progress or a review comes first.

A live activity whose status cannot be read now is placed by the last status the window
read of it, which says what it is — its kind, its job, the destinations it serves and
those a run had finished — and never how far it has come: its destinations, and its job
for a run or a repair, read "Run in progress - its status cannot be read now" (or
"Verification ..." or "Repair ..."), a run's on every destination it serves and has not
finished; a verification's job reads "Verifying", as for any verification. One the window
never read names no job, and the list shows nothing of it (the Runs tab and the status bar
do).

Two columns beside the status: the free space of a present destination's drive, "low"
below the warning's share; and its last full verification, "due" once the job's
reminder interval has passed — a verification of new copies never counts there, as it
leaves unread what an earlier pass read. Either one turns an otherwise normal row to the
attention tone; the words carry it, the colour only reinforces them.

A remote destination is never asked at a look: all the list says of one comes from the
records. Its first run is confirmed once a run was applied there, which stands in for its
marker; its free space is the last a run recorded; a last run that could not reach it reads
"Waits for its remote"; a run that found it does not encrypt adds so to its folder. What
the records cannot say — a first run's folder holding anything — is confirmed through the
preview, and the activities the list offers check for themselves as they start.

A row whose words say it waits for a drive or a remote — a destination whose drive is not
attached, one whose remote could not be reached, a job whose source drive is not, or one
whose every destination waits — stays listed in the absent tone, drawn grey. A row asking
something of the user keeps its own tone while a drive is away (a run or verification in
progress, a review, a problem — two drives matching among them), its Drive column still
saying "not attached".
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from mirrorhold.endpoints.endpoint import Endpoint, RemoteEndpoint
from mirrorhold.endpoints.marker import MarkerState
from mirrorhold.endpoints.resolution import Presence, Resolution, resolve
from mirrorhold.endpoints.volumes import VolumeInfo, free_percent, low_on_space
from mirrorhold.engine.rclone import RemoteRoot
from mirrorhold.jobs.model import Group, Job
from mirrorhold.jobs.store import StoreContents
from mirrorhold.jobs.validation import (DESTINATION_IS_SYSTEM_FOLDER, DESTINATION_NOT_A_FOLDER,
                                        REMOTE_BELOW_FLOOR, REMOTE_CONFIGURATION_MISSING,
                                        REMOTE_CONFIGURATION_UNUSABLE, REMOTE_UNKNOWN, REMOTE_UNREACHABLE,
                                        SOURCE_NOT_A_FOLDER)
from mirrorhold.maintenance.cleanup import VERSIONS
from mirrorhold.maintenance.repair import RepairRecord, all_replaced, pending
from mirrorhold.maintenance.verification import (VerificationMode, VerificationRecord, VerificationState,
                                                 effective_state, last_full_verification, latest_finished,
                                                 unfinished as unfinished_pass, verification_due)
from mirrorhold.planning.listing import Occupancy
from mirrorhold.records.admission import WaitedFor, line_words, starting
from mirrorhold.records.diagnostics import ActivityLog
from mirrorhold.records.record import (FIRST_RUN_REASON, HOLDS_CONTENT_REASON, SHARED_RECORD_LOST, Outcome, RunRecord,
                                       destination_state)
from mirrorhold.records.status import UNREADABLE, ObservedRun, Phase, is_repair, is_run, is_verification, place_of
from mirrorhold.run.gate import Hold
from mirrorhold.run.runner import NAMES_AMBIGUOUS, REMOTE_FAILED, SHARED_RECORD_UNREADABLE, SYSTEM_FOLDER_CODE
from mirrorhold.wording import agreeing, counted

from .formatting import files, local_time, parse_utc, size
from .runs_model import whom as whom_waited_for

UP_TO_DATE = "Up to date"
STARTING = "Starting"
REPAIRING = "Replacing copies"
REPAIR_WAITING = "Waiting to replace copies"
UNREAD_RUN = f"Run in progress - {UNREADABLE}"
UNREAD_VERIFICATION = f"Verification in progress - {UNREADABLE}"
UNREAD_REPAIR = f"Repair in progress - {UNREADABLE}"

_UNREAD = "unread"
"""The key marking what the list knows of an activity whose status cannot be read now
(``_known``), in place of its status."""

_IDENTITY = ("run_id", "job_id", "job_name", "kind", "marker_id", "selected", "markers", "finished")
"""What the list takes from the last status read of such an activity: what it is — its
kind, job and destinations, and what a run's numbers name — and the destinations a run had
finished, which only grow."""

_REASON_WORDS = {
    "insufficient-space": "not enough free space",
    "fat32-large-file": "a file too large for FAT32",
    "volume-ambiguous": "two drives match",
    "marker-mismatch": "the folder belongs to another job",
    "marker-unreadable": "its marker cannot be read",
    "source-unlistable": "the source cannot be listed",
    "source-missing": "the source folder is missing",
    "destination-unlistable": "the destination cannot be listed",
    "engine-unusable": "the sync engine cannot be used",
    "engine-failed": "the sync engine failed",
    "unexpected-error": "an unexpected error",
    "process-ended": "its process ended",
    "source-absent": "the source's drive is not attached",
    "destination-absent": "the drive is not attached",
    Hold.SHARE_OF_DESTINATION.value: "too large a share of the destination's files would move into versions",
    "half-of-destination": "half or more of the destination's files would move into versions",   # older records
    HOLDS_CONTENT_REASON: "the folder already holds files or folders",
    "destination-contains-sync-root": "a synchronised folder would be written into",
    "destination-contains-other-source": "another job's source would be written into",
    SYSTEM_FOLDER_CODE: "a folder Windows or MirrorHold needs would be written into",
    SHARED_RECORD_UNREADABLE: "its record of what to leave cannot be read",
    SHARED_RECORD_LOST: "its record of what to leave was lost",
    SOURCE_NOT_A_FOLDER: "the source is a file, not a folder",
    DESTINATION_NOT_A_FOLDER: "the destination is a file, not a folder",
    DESTINATION_IS_SYSTEM_FOLDER: "the folder itself is one Windows or MirrorHold needs",
    REMOTE_CONFIGURATION_MISSING: "no rclone configuration is set",
    REMOTE_CONFIGURATION_UNUSABLE: "the rclone configuration cannot be used",
    REMOTE_UNKNOWN: "the rclone configuration holds no such remote",
    REMOTE_BELOW_FLOOR: "this remote cannot hold a backup",
    REMOTE_UNREACHABLE: "the remote could not be reached",
    REMOTE_FAILED: "the remote stopped answering",
    NAMES_AMBIGUOUS: "the remote holds names this tool cannot tell apart",
}


def reason_words(code: str) -> str:
    """A reason code (a hold, a stop, a failure, a refusal) in plain words."""
    return _REASON_WORDS.get(code, code.replace("-", " "))


_LIVE_READING_WORDS = {
    "snapshot-off": "snapshot reading is switched off for this job",
    "snapshot-not-needed": "no destination was attached",
    "snapshot-unsupported-drive": "this drive does not support snapshots",
    "snapshot-would-displace": "the drive keeps Windows restore points or earlier versions, which a snapshot could "
                               "make Windows delete",
    "snapshot-unavailable": "snapshots are not available to this run",
    "snapshot-denied": "a snapshot could not be made (administrator rights are needed)",
    "snapshot-refused": "the drive does not allow a snapshot",
    "snapshot-failed": "a snapshot could not be made",
}
"""Why a source is read as it is, in words; ``snapshots.reading`` defines the codes."""


def live_reading_words(code: str) -> str:
    """Why a run reads (or read) its source as it is, in plain words."""
    return _LIVE_READING_WORDS.get(code, code.replace("-", " "))


class Tone(str, Enum):
    NORMAL = "normal"
    ACTIVE = "active"          # a run in progress or waiting
    ATTENTION = "attention"    # needs the user's review
    PROBLEM = "problem"        # stopped, failed, refused
    ABSENT = "absent"          # waits for a drive that is not attached, or a remote not reached


@dataclass(frozen=True)
class RemotePlace:
    """A folder of a remote: no place on this computer can be opened, so the window shows
    its address, as rclone names it."""

    address: str


Place = Union[Path, RemotePlace]
"""Where a folder of a destination is, as the window opens it."""

Root = Union[Path, RemoteEndpoint]
"""A destination's folder: on this computer, or on a remote."""


def place_under(root: Root, relative: str = "") -> Place:
    """The folder at ``relative`` (a carried path, backslash-separated) below ``root``."""
    if isinstance(root, RemoteEndpoint):
        return RemotePlace(RemoteRoot(root.remote, root.folder).joined(relative).fs)
    return root / relative if relative else root


@dataclass(frozen=True)
class DestinationRow:
    job_id: str
    index: int                       # 1-based, in the job's order
    marker_id: str
    folder: str
    drive: str
    last_success: str
    status: str
    tone: Tone
    location: Optional[Path] = None  # where it is now, when its drive is present
    marker: Optional[MarkerState] = None
    needs_review: bool = False
    last_success_raw: Optional[str] = None
    name: str = ""                   # short: the drive's label, else the folder's own name
    remote: Optional[RemoteEndpoint] = None   # the remote's folder, for a remote destination
    away: bool = False               # a remote the last run could not reach
    repeated: int = 0                # files its last run left unbacked-up for two runs in a row or more
    last_attempt: Optional[RunRecord] = None   # its latest run that read the source: the files-not-backed-up report
    free: str = ""                   # its drive's free space in words, when present
    free_low: bool = False
    verified: str = ""               # its last full verification in words, "- due" once the reminder is
    verification_due: bool = False
    last_verification: Optional[VerificationRecord] = None   # its latest finished verification, of either kind
    verifying: bool = False          # a verification of it is in progress
    can_verify: bool = False         # its drive or remote is there, the source's drive too, its marker matches
    occupancy: Optional[Occupancy] = None   # what its folder held when last looked at, while it has no marker
    unfinished: Optional[VerificationRecord] = None   # its pass to continue, full before new copies, when not at work
    found_differ: Optional[VerificationRecord] = None   # its latest finished pass, when that found copies that differ
    repairing: bool = False          # a repair of it is in progress
    can_repair: bool = False         # a repair of the copies its latest finished pass found to differ can start now

    @property
    def iid(self) -> str:
        return f"dest:{self.job_id}:{self.index}"

    @property
    def first_backup(self) -> bool:
        """Its first run waits for nothing but the user's go-ahead: no marker yet, and its
        folder held nothing when last looked at. The run looks again."""
        return (self.location is not None and self.marker is MarkerState.MISSING and self.occupancy is not None
                and self.occupancy.holds_nothing)

    @property
    def differ(self) -> int:
        """Copies its latest finished verification found to differ."""
        return self.last_verification.differ_total if self.last_verification is not None else 0

    @property
    def has_unbacked(self) -> bool:
        return self.last_attempt is not None and bool(self.last_attempt.not_backed_up)

    @property
    def can_open(self) -> bool:
        return self.root is not None and self.marker is MarkerState.MATCH

    @property
    def root(self) -> Optional[Root]:
        """The destination's folder: its remote's, or on this computer when its drive is present."""
        return self.remote if self.remote is not None else self.location

    @property
    def attached(self) -> bool:
        """Whether its drive is present, or its remote was not found gone by its last run."""
        return self.root is not None and not self.away

    @property
    def versions_folder(self) -> Optional[Place]:
        """The destination's versions folder, which the window opens."""
        return place_under(self.root, VERSIONS) if self.can_open else None


@dataclass(frozen=True)
class JobRow:
    job_id: str
    name: str
    source: str
    drive: str
    last_success: str
    status: str
    tone: Tone
    destinations: Tuple[DestinationRow, ...]
    running: bool = False
    last_success_raw: Optional[str] = None   # its newest destination's last success (UTC)
    start_failure: Optional[ActivityLog] = None   # its latest start, when it left no record and nothing came since

    @property
    def iid(self) -> str:
        return f"job:{self.job_id}"

    @property
    def needs_review(self) -> bool:
        return any(d.needs_review for d in self.destinations)

    @property
    def first_backups(self) -> Tuple[DestinationRow, ...]:
        """Its destinations whose first run waits only for the user's go-ahead."""
        return tuple(d for d in self.destinations if d.first_backup)

    @property
    def needs_other_review(self) -> bool:
        """Something besides a first backup's go-ahead waits for the user."""
        return any(d.needs_review and not d.first_backup for d in self.destinations)

    def serving(self, destinations: Optional[Collection[str]]) -> "JobRow":
        """The job as a run of ``destinations`` (markers) sees it: its rows of those alone,
        so what it says of first backups and reviews is that run's; itself for None, a run of
        every destination."""
        if destinations is None:
            return self
        wanted = set(destinations)
        return replace(self, destinations=tuple(d for d in self.destinations if d.marker_id in wanted))


@dataclass(frozen=True)
class GroupRow:
    group_id: str
    name: str
    jobs: Tuple[JobRow, ...]          # in the group's order
    last_success: str
    status: str
    tone: Tone

    @property
    def iid(self) -> str:
        return f"group:{self.group_id}"

    @property
    def label(self) -> str:
        return f"{self.name}      ({counted(len(self.jobs), 'job')})"


WAITS_REMOTE = "Waits for its remote"
NOT_ENCRYPTED = "remote, not encrypted"
"""What a remote's folder says in the list once a run found it does not encrypt."""


def describe_remote(remote: RemoteEndpoint) -> str:
    return f"{remote.describe()} (remote)"


def describe_folder(resolution: Resolution) -> str:
    """Where an endpoint is: its folder now, with the volume's label; or its path on a
    drive that is not attached; or a remote's folder as rclone names it."""
    if isinstance(resolution.endpoint, RemoteEndpoint):
        return describe_remote(resolution.endpoint)
    if resolution.presence is Presence.PRESENT:
        label = resolution.volume.label
        return f"{resolution.location}" + (f' "{label}"' if label else "")
    return _stored_path(resolution.endpoint)


def _stored_path(endpoint: Endpoint) -> str:
    return "\\" + endpoint.path if endpoint.path else "(the root of its drive)"


def short_name(resolution: Resolution) -> str:
    """A destination in a few words: its drive's label when the drive is present and
    labelled, else its folder's own name — a remote's top by the remote's name."""
    endpoint = resolution.endpoint
    if isinstance(endpoint, RemoteEndpoint):
        return endpoint.folder.rstrip("/").rsplit("/", 1)[-1] or endpoint.remote
    if resolution.presence is Presence.PRESENT and resolution.volume.label:
        return resolution.volume.label
    path = endpoint.path
    return path.rsplit("\\", 1)[-1] if path else "drive root"


def _drive_words(presence: Presence) -> str:
    return {Presence.PRESENT: "present", Presence.ABSENT: "not attached",
            Presence.AMBIGUOUS: "two drives match"}[presence]


def _known(observed: ObservedRun) -> Optional[dict]:
    """What the list knows of a live activity: its status; for one whose status cannot be
    read now, what the last status the window read of it says it is (``_IDENTITY``), marked
    ``_UNREAD``; None for one the window never read, which names no job."""
    if observed.readable:
        return observed.status
    if observed.last is None:
        return None
    known = {key: observed.last[key] for key in _IDENTITY if key in observed.last}
    known[_UNREAD] = True
    return known


def _live(runs: Iterable[ObservedRun], job_id: str) -> List[dict]:
    """What the list knows of the job's live activities, every kind."""
    known = (_known(r) for r in runs if r.alive)
    return [status for status in known if status is not None and status.get("job_id") == job_id]


def _live_runs(runs: Iterable[ObservedRun], job_id: str) -> List[dict]:
    """The job's runs in progress: those at work, then those whose status cannot be read now,
    their step not known, then those waiting. Two runs of one job never work at once
    (``records.admission.conflict``), so while one that can be read works, one that cannot
    waits, and never stands before it."""
    mine = [s for s in _live(runs, job_id) if is_run(s)]
    unread = [s for s in mine if s.get(_UNREAD)]
    read = [s for s in mine if not s.get(_UNREAD)]
    return [s for s in read if s.get("phase") != Phase.WAITING.value] + unread + \
        [s for s in read if s.get("phase") == Phase.WAITING.value]


def run_number(status: dict, marker_id: str, index: int) -> Optional[int]:
    """The number a run gives the destination now at ``index`` of its job, whose marker is
    ``marker_id``. A run numbers its destinations as the job stood when it began, and names
    the marker behind each number, since an edit of the job while it works may move,
    remove or add one: the number is found by the marker, and is None for a destination
    the run does not number — one added since it began. A run naming no markers, an older
    copy's, numbers as the job stands now."""
    markers = status.get("markers")
    if not isinstance(markers, list) or not markers:
        return index
    return markers.index(marker_id) + 1 if marker_id in markers else None


def _serving(lives: Sequence[dict], marker_id: str, index: int) -> Optional[dict]:
    """The run in progress that serves the destination now at ``index``, whose marker is
    ``marker_id``: one naming its destinations names it; one naming none — an older copy's —
    serves every one it numbers."""
    for status in lives:
        number = run_number(status, marker_id, index)
        if number is not None and (not status.get("selected") or number in status.get("selected")):
            return status
    return None


def percent(done: int, total: int) -> int:
    """How much of ``total`` is done, in whole percent rounded down."""
    return min(100, (100 * done) // total) if total > 0 else 0


def _live_share(status: dict) -> Optional[int]:
    """The share a verification at work has compared, by bytes; None while it lists."""
    verify = status.get("verify") or {}
    if status.get("phase") != Phase.VERIFYING.value or not verify.get("files"):
        return None
    if verify.get("bytes"):
        return percent(int(verify.get("bytes_done") or 0), int(verify["bytes"]))
    return percent(int(verify.get("files_done") or 0), int(verify["files"]))


def waiting_words(status: dict, verification: bool = False) -> str:
    """A row's words while its run or verification waits its turn, its place in line first
    so that a narrow Status column cuts the rest: "2nd in line, waiting for 'Work' (drive
    I:)", "Verification next in line, waiting for 'Work'" — the drive named when sharing it
    alone is why, and one whose status the waiting one cannot read now named as the Runs
    tab names it (``runs_model.whom``); "Starting" for an activity of this copy starting
    (``admission.starting``); "Waiting to run" or "Waiting to verify" for a status naming
    neither otherwise — one of this copy that has asked for its turn, or an older copy's."""
    other = WaitedFor.from_status(status)
    place = place_of(status)
    if other is None and not place:
        if starting(status):
            return STARTING
        return "Waiting to verify" if verification else "Waiting to run"
    whom = ""
    if other is not None:
        whom = "waiting for " + whom_waited_for(other)
        whom += f" ({other.drive})" if other.drive else ""
    if not place:
        return f"Verification {whom}" if verification else whom[:1].upper() + whom[1:]
    lead = f"Verification {line_words(place)}" if verification else line_words(place).capitalize()
    return lead + (f", {whom}" if whom else "")


def _verifying_words(status: dict) -> str:
    if status.get(_UNREAD):
        return UNREAD_VERIFICATION
    if status.get("phase") == Phase.WAITING.value:
        return waiting_words(status, verification=True)
    share = _live_share(status)
    lead = "Verifying new copies" if (status.get("verify") or {}).get("mode") == VerificationMode.NEW_COPIES.value \
        else "Verifying"
    return lead + (f" {share} %" if share is not None else "") + (" (paused)" if status.get("paused") else "")


def record_share(record: VerificationRecord) -> int:
    """The share of its pass a verification record has compared, by bytes."""
    if record.bytes_total:
        return percent(record.compared_bytes, record.bytes_total)
    return percent(record.compared_files, record.files_total)


_UNFINISHED = {VerificationState.STOPPED: ("stopped", Tone.ATTENTION),
               VerificationState.INTERRUPTED: ("interrupted", Tone.ATTENTION),
               VerificationState.FAILED: ("failed", Tone.PROBLEM)}


def unfinished_words(record: VerificationRecord, live: Collection[str] = ()) -> Tuple[str, Tone]:
    """'Verification stopped at 42 %' ('Verification of new copies stopped at 42 %') and its
    tone, for a pass that did not finish."""
    word, tone = _UNFINISHED[effective_state(record, live)]
    what = "Verification" if record.full else "Verification of new copies"
    return f"{what} {word} at {record_share(record)} %", tone


def _live_verifications(runs: Iterable[ObservedRun], job_id: str) -> Dict[str, dict]:
    """The job's verifications in progress, by the marker of the destination each verifies."""
    return {s.get("marker_id"): s for s in _live(runs, job_id) if is_verification(s)}


def _live_repairs(runs: Iterable[ObservedRun], job_id: str) -> Dict[str, dict]:
    """The job's repairs in progress, by the marker of the destination each replaces copies on."""
    return {s.get("marker_id"): s for s in _live(runs, job_id) if is_repair(s)}


def free_words(volume: VolumeInfo) -> Tuple[str, bool]:
    """'1.2 TiB (31 %)', or '212 GiB (6 %) low' below the warning's share."""
    return _free_words(volume.free_bytes, volume.capacity_bytes)


def _free_words(free: int, capacity: int) -> Tuple[str, bool]:
    low = low_on_space(free, capacity)
    text = f"{size(free)} ({free_percent(free, capacity):.0f} %)"
    return (text + " low" if low else text), low


def verified_words(last: Optional[str], due: bool) -> str:
    """'2026-09-01', 'never', with ' - due' once the reminder interval has passed."""
    moment = parse_utc(last)
    text = moment.astimezone().strftime("%Y-%m-%d") if moment is not None else "never"
    return text + (" - due" if due else "")


def _run_words(status: dict) -> str:
    if status.get(_UNREAD):
        return UNREAD_RUN
    if status.get("phase") == Phase.WAITING.value:
        return waiting_words(status)
    if status.get("cancelling"):
        return "Cancelling"
    if status.get("paused"):
        return "Paused"
    return "Running"


def _record_status(last: Optional[RunRecord]) -> Tuple[str, Tone, bool]:
    """Status words, tone and whether it needs review, from a destination's last record
    once its marker matches."""
    if last is None or last.outcome is Outcome.ABSENT:
        return "Ready to run", Tone.NORMAL, False
    outcome = last.outcome
    if outcome is Outcome.COMPLETED and last.repeatedly_unbacked:
        return f"Completed - {files(len(last.repeatedly_unbacked))} not backed up", Tone.ATTENTION, False
    if outcome is Outcome.COMPLETED:
        return "Completed", Tone.NORMAL, False
    if outcome is Outcome.HELD and SHARED_RECORD_LOST in last.reasons:
        return "Record lost: review", Tone.ATTENTION, True
    if outcome is Outcome.HELD:
        return "Removals held: review", Tone.ATTENTION, True
    if outcome is Outcome.CANCELLED:
        return "Cancelled - next run continues", Tone.NORMAL, False
    if outcome is Outcome.INTERRUPTED:
        return "Interrupted - next run continues", Tone.NORMAL, False
    if outcome is Outcome.FAILED:
        return "Failed - see history", Tone.PROBLEM, False
    reason = reason_words(last.reasons[0]) if last.reasons else "see history"
    return f"Stopped: {reason}", Tone.PROBLEM, False


def _destination_row(job: Job, index: int, volumes: Sequence[VolumeInfo],
                     markers: Mapping[Tuple[str, str], MarkerState], records: Sequence[RunRecord],
                     live: Optional[dict], verifications: Sequence[VerificationRecord],
                     verifying: Optional[dict], source_present: bool, now: Optional[datetime],
                     occupancy: Optional[Occupancy] = None, live_ids: Collection[str] = (),
                     repairs: Sequence[RepairRecord] = (), repairing: Optional[dict] = None) -> DestinationRow:
    destination = job.destinations[index - 1]
    remote = destination.remote
    resolution = resolve(destination.endpoint, volumes)
    present = resolution.presence is Presence.PRESENT
    mine = [r for r in records if r.marker_id == destination.marker_id]
    last = mine[-1] if mine else None
    if remote is not None:
        confirmed = destination_state(records, destination.marker_id).first_run_confirmed
        marker = MarkerState.MATCH if confirmed else MarkerState.MISSING
    else:
        marker = markers.get((job.job_id, destination.marker_id)) if present else None
    occupancy = occupancy if marker is MarkerState.MISSING else None
    last_verified = last_full_verification(verifications, destination.marker_id)
    state = destination_state(records, destination.marker_id, marker, last_verified)
    attempt = next((r for r in reversed(mine) if r.source_read is not None), None)
    due = verification_due(job.verification_reminder_days, destination.marker_id, records, verifications,
                           now) is not None
    if remote is not None:
        space = next((r.space for r in reversed(mine) if r.space is not None), None)
        free, low = _free_words(*space) if space is not None else ("", False)
        folder = _remote_row_folder(remote, mine)
    else:
        free, low = free_words(resolution.volume) if present else ("", False)
        folder = describe_folder(resolution)
    away = remote is not None and last is not None and REMOTE_UNREACHABLE in last.reasons
    reachable = present or remote is not None
    checked = latest_finished(verifications, destination.marker_id)
    found = checked if checked is not None and checked.differ else None
    idle = verifying is None and repairing is None and live is None
    common = dict(job_id=job.job_id, index=index, marker_id=destination.marker_id,
                  folder=folder, drive="remote" if remote is not None else _drive_words(resolution.presence),
                  last_success=local_time(state.last_success), location=resolution.location, marker=marker,
                  last_success_raw=state.last_success, name=short_name(resolution), remote=remote, away=away,
                  last_attempt=attempt,
                  free=free, free_low=low, verified=verified_words(state.last_full_verification, due),
                  verification_due=due, last_verification=checked,
                  verifying=verifying is not None,
                  can_verify=(reachable and source_present and marker is MarkerState.MATCH and verifying is None),
                  occupancy=occupancy,
                  unfinished=unfinished_pass(verifications, destination.marker_id, live_ids) if verifying is None
                  else None,
                  found_differ=found, repairing=repairing is not None,
                  can_repair=(reachable and source_present and marker is MarkerState.MATCH and idle
                              and found is not None and bool(pending(found, repairs))))
    number = run_number(live, destination.marker_id, index) if live is not None else None
    finished = {int(i) for i, _ in (live or {}).get("finished") or []}
    if live is not None and number is not None and number not in finished:
        # A run whose status cannot be read now may be on any destination it has not finished.
        if live.get(_UNREAD) or live.get("phase") == Phase.WAITING.value or live.get("pair_index") == number:
            return DestinationRow(status=_run_words(live), tone=Tone.ACTIVE, **common)
        return DestinationRow(status="Waiting to run", tone=Tone.ACTIVE, **common)
    if verifying is not None:
        return DestinationRow(status=_verifying_words(verifying), tone=Tone.ACTIVE, **common)
    if repairing is not None:
        if repairing.get(_UNREAD):
            status = UNREAD_REPAIR
        else:
            status = REPAIR_WAITING if repairing.get("phase") == Phase.WAITING.value else REPAIRING
        return DestinationRow(status=status, tone=Tone.ACTIVE, **common)
    if away:
        return DestinationRow(status=WAITS_REMOTE, tone=Tone.ABSENT, **common)
    if resolution.presence is Presence.AMBIGUOUS:
        return DestinationRow(status="Stopped: two drives match", tone=Tone.PROBLEM, **common)
    if not reachable:
        return DestinationRow(status="Waits for its drive", tone=Tone.ABSENT, **common)
    if marker is MarkerState.MISMATCH:
        return DestinationRow(status="Stopped: the folder belongs to another job", tone=Tone.PROBLEM, **common)
    if marker is MarkerState.UNREADABLE:
        return DestinationRow(status="Stopped: its marker cannot be read", tone=Tone.PROBLEM, **common)
    stopped_first = remote is not None and last is not None and last.outcome in (Outcome.STOPPED, Outcome.FAILED)
    if marker is MarkerState.MISSING and not stopped_first:
        if occupancy is None or occupancy is Occupancy.UNKNOWN:
            status = "First run: confirm"
        elif occupancy.holds_nothing:
            status = "First backup: confirm"
        else:
            status = "First run: review"
        return DestinationRow(status=status, tone=Tone.ATTENTION, needs_review=True, **common)
    status, tone, review = _record_status(last)
    repeated = len(last.repeatedly_unbacked) if last is not None and last.outcome is Outcome.COMPLETED else 0
    checked = common["last_verification"]
    differ = checked.differ_total if checked is not None and not all_replaced(checked, repairs) else 0
    if differ and tone is not Tone.PROBLEM and not review:
        status, tone = "Copies differ: see verification", Tone.PROBLEM
    unfinished = common["unfinished"]
    if unfinished is not None and tone is Tone.NORMAL and not review:
        status, tone = unfinished_words(unfinished, live_ids)
    if tone is Tone.NORMAL and (low or due):
        tone = Tone.ATTENTION
    return DestinationRow(status=status, tone=tone, needs_review=review, repeated=repeated, **common)


def _remote_row_folder(remote: RemoteEndpoint, records: Sequence[RunRecord]) -> str:
    """A remote's folder in the list, saying so once the latest run that reached it found it
    does not encrypt."""
    encrypted = next((r.encrypted for r in reversed(records) if r.encrypted is not None), None)
    return f"{remote.describe()} ({NOT_ENCRYPTED})" if encrypted is False else describe_remote(remote)


def start_failure_words(log: ActivityLog) -> str:
    """A job's status while its latest start left no record: why it shows, and where to look."""
    if log.vanished:
        return "Ended without a record - see History"
    what = {"verify": "Verification", "repair": "Repair"}.get(log.command, "Run")
    return f"{what} could not start - see History"


def log_job(log: ActivityLog, jobs: Sequence[Job]) -> Optional[str]:
    """The job a log names: the one it resolved, else the one it was given, as the command
    line finds a job — by identifier, exact name, or a unique identifier prefix."""
    key = log.job
    if not key:
        return None
    if any(j.job_id == key for j in jobs):
        return key
    by_name = [j.job_id for j in jobs if j.name.casefold() == key.casefold()]
    if len(by_name) == 1:
        return by_name[0]
    by_id = [j.job_id for j in jobs if j.job_id.startswith(key.lower())] if len(key) >= 4 else []
    return by_id[0] if len(by_id) == 1 else None


def left_no_record(log: ActivityLog, records: Sequence[RunRecord], verifications: Sequence[VerificationRecord],
                   repairs: Sequence[RepairRecord] = ()) -> bool:
    """Whether none of the runs, verifications and repairs the log's process began left a record."""
    ids = {activity for activity, _kind in log.activities}
    return (not any(r.run_id in ids for r in records) and not any(v.verification_id in ids for v in verifications)
            and not any(r.repair_id in ids for r in repairs))


def start_failure(logs: Sequence[ActivityLog], records: Sequence[RunRecord],
                  verifications: Sequence[VerificationRecord],
                  repairs: Sequence[RepairRecord] = ()) -> Optional[ActivityLog]:
    """The job's latest start, from ``logs`` (the job's own), when it ended without running
    — refused, failed before it recorded anything, or its process gone without a word — and
    no record was written since; else None."""
    done = [log for log in logs if log.ended or log.vanished]
    if not done:
        return None
    last = max(done, key=lambda log: (log.started, log.path.name))
    if not (last.failed or last.vanished) or not left_no_record(last, records, verifications, repairs):
        return None
    since = last.written or last.started
    if any((r.ended or r.started) >= since for r in records) or any(v.updated >= since for v in verifications) \
            or any(r.updated >= since for r in repairs):
        return None
    return last


def _job_status(job_rows: Sequence[DestinationRow], source: Resolution, live: Optional[dict],
                failure: Optional[ActivityLog] = None) -> Tuple[str, Tone]:
    if live is not None:
        return _run_words(live), Tone.ACTIVE
    if any(d.verifying for d in job_rows):
        return "Verifying", Tone.ACTIVE
    repairing = next((d for d in job_rows if d.repairing), None)
    if repairing is not None:
        return repairing.status, Tone.ACTIVE
    if any(d.needs_review for d in job_rows):
        return "Needs your review", Tone.ATTENTION
    if failure is not None:
        return start_failure_words(failure), Tone.PROBLEM
    if source.presence is Presence.AMBIGUOUS:
        return "Stopped: two drives match the source", Tone.PROBLEM
    problem = next((d for d in job_rows if d.tone is Tone.PROBLEM), None)
    if problem is not None:
        return problem.status, Tone.PROBLEM
    if source.presence is Presence.ABSENT:
        return "Waits for its source drive", Tone.ABSENT
    present = [d for d in job_rows if d.attached]
    if not present:
        return (WAITS_REMOTE if job_rows and all(d.remote is not None for d in job_rows)
                else "Waits for its drive"), Tone.ABSENT
    if any(d.repeated for d in present):
        return "Files not backed up", Tone.ATTENTION
    if all(d.status == "Completed" for d in present):
        return UP_TO_DATE, Tone.NORMAL
    other = next(d for d in present if d.status != "Completed")
    return other.status, other.tone


def build_rows(contents: StoreContents, volumes: Sequence[VolumeInfo],
               markers: Mapping[Tuple[str, str], MarkerState], records: Mapping[str, Sequence[RunRecord]],
               runs: Sequence[ObservedRun], verifications: Optional[Mapping[str, Sequence[VerificationRecord]]] = None,
               now: Optional[datetime] = None,
               occupancy: Optional[Mapping[Tuple[str, str], Occupancy]] = None,
               logs: Sequence[ActivityLog] = (),
               repairs: Optional[Mapping[str, Sequence[RepairRecord]]] = None) -> Tuple[JobRow, ...]:
    """``verifications``: each job's verification records; ``now``: the reminder's clock
    (the computer's when None); ``occupancy``: what the folder of each present destination
    without a marker held, by (job, marker identity), as the refresher last looked;
    ``logs``: the activities' logs, read back (``records.diagnostics.LogWatch``);
    ``repairs``: each job's repair records."""
    live_ids = frozenset(r.run_id for r in runs if r.alive)
    by_job: Dict[str, List[ActivityLog]] = {}
    for log in logs:
        job_id = log_job(log, contents.jobs)
        if job_id is not None:
            by_job.setdefault(job_id, []).append(log)
    rows = []
    for job in contents.jobs:
        history = tuple(records.get(job.job_id, ()))
        checks = tuple((verifications or {}).get(job.job_id, ()))
        fixes = tuple((repairs or {}).get(job.job_id, ()))
        lives = _live_runs(runs, job.job_id)
        live = lives[0] if lives else None
        verifying = _live_verifications(runs, job.job_id)
        repairing = _live_repairs(runs, job.job_id)
        source = resolve(job.source.endpoint, volumes)
        source_present = source.presence is Presence.PRESENT
        destinations = tuple(_destination_row(job, i, volumes, markers, history,
                                               _serving(lives, job.destinations[i - 1].marker_id, i), checks,
                                               verifying.get(job.destinations[i - 1].marker_id), source_present, now,
                                               (occupancy or {}).get((job.job_id, job.destinations[i - 1].marker_id)),
                                               live_ids, fixes, repairing.get(job.destinations[i - 1].marker_id))
                             for i in range(1, len(job.destinations) + 1))
        failure = start_failure(by_job.get(job.job_id, ()), history, checks, fixes)
        status, tone = _job_status(destinations, source, live, failure)
        successes = [d.last_success_raw for d in destinations if d.last_success_raw]
        last = max(successes) if successes else None
        rows.append(JobRow(job.job_id, job.name, describe_folder(source), _drive_words(source.presence),
                           local_time(last), status, tone, destinations, running=live is not None,
                           last_success_raw=last, start_failure=failure))
    return tuple(rows)


def _pressing(row: JobRow) -> int:
    """How much a job's status asks of the user, most first."""
    if row.tone is Tone.ACTIVE:
        return 5
    if row.needs_review:
        return 4
    if row.tone is Tone.PROBLEM:
        return 3
    if row.tone is Tone.ATTENTION:
        return 2
    return 0 if row.status == UP_TO_DATE else 1


def _group_row(group: Group, jobs: Tuple[JobRow, ...]) -> GroupRow:
    if not jobs:
        return GroupRow(group.group_id, group.name, jobs, "", "No jobs", Tone.NORMAL)
    successes = [j.last_success_raw for j in jobs]
    last = local_time(None if None in successes else min(successes))
    level = max(_pressing(j) for j in jobs)
    if level == 0:
        return GroupRow(group.group_id, group.name, jobs, last, UP_TO_DATE, Tone.NORMAL)
    at_level = [j for j in jobs if _pressing(j) == level]
    first = at_level[0]
    if len(at_level) == 1:
        status = f"{first.name}: {first.status}"
    elif all(j.status == first.status for j in at_level):
        status = f"{first.status} ({counted(len(at_level), 'job')})"
    else:
        status = f"{first.name}: {first.status} (+{len(at_level) - 1})"
    return GroupRow(group.group_id, group.name, jobs, last, status, first.tone)


def arrange(contents: StoreContents, rows: Sequence[JobRow]) -> Tuple[object, ...]:
    """The list's top level, in the store's one order (``StoreContents.in_order``): each
    group holding its jobs, then each job in no group."""
    by_id = {row.job_id: row for row in rows}
    grouped = {job_id for group in contents.groups for job_id in group.job_ids}
    entries: list = [_group_row(group, tuple(by_id[j] for j in group.job_ids if j in by_id))
                     for group in contents.groups]
    entries.extend(by_id[job.job_id] for job in contents.in_order()
                   if job.job_id not in grouped and job.job_id in by_id)
    return tuple(entries)


@dataclass(frozen=True)
class Question:
    title: str
    text: str
    button: str                      # the button that answers yes


def _destinations(indexes: Sequence[int]) -> str:
    """'Destination 3', 'Destinations 3 and 4', 'Destinations 3, 4 and 5'."""
    if len(indexes) == 1:
        return f"Destination {indexes[0]}"
    return "Destinations " + ", ".join(str(i) for i in indexes[:-1]) + f" and {indexes[-1]}"


def first_backup_question(job: JobRow) -> Optional[Question]:
    """The one question asked before a job's first backups start — by *Run now*, or by
    *Review and confirm...* on such a destination: where no backup exists yet, what is
    copied there, and what becomes of the run's other attached destinations. ``job`` is the
    job as the run sees it (``JobRow.serving``), so a run of some destinations names only
    those. None when no destination's first run waits only for the user's go-ahead. Yes
    starts the run with that go-ahead; no starts nothing."""
    firsts = job.first_backups
    if not firsts:
        return None
    lines = ["No backup exists yet in:"]
    for d in firsts:
        lines.append(f"    {d.folder}" + ("   (the backup creates this folder)" if d.occupancy is Occupancy.MISSING
                                          else ""))
    lines.append(f"MirrorHold copies everything in {job.source} there. "
                 + ("The folder holds nothing yet, so nothing in it is moved or replaced." if len(firsts) == 1
                    else "The folders hold nothing yet, so nothing in them is moved or replaced."))
    usual = []
    for d in job.destinations:
        if d.first_backup or not d.attached:
            continue
        if d.marker is MarkerState.MISSING:
            holds = ", already holds files:" if d.occupancy is Occupancy.OCCUPIED else ":"
            lines.append(f"Destination {d.index}, {d.folder}{holds} its first run waits for your review "
                         "(Review and confirm...).")
        elif d.marker is MarkerState.MATCH:
            usual.append(d.index)
    if usual:
        lines.append(f"{_destinations(usual)} {agreeing(len(usual), 'runs', 'run')} as usual.")
    return Question(f"Start the first backup: {job.name}", "\n".join(lines), "Start first backup")


def job_has_run(records: Sequence[RunRecord]) -> bool:
    """Whether a run of the job may have written to a destination: its source is then
    fixed, because a new source would make the next run replace what is there."""
    for r in records:
        if r.outcome in (Outcome.COMPLETED, Outcome.CANCELLED, Outcome.INTERRUPTED):
            return True
        if r.outcome is Outcome.HELD and FIRST_RUN_REASON not in r.reasons:
            return True
        if r.outcome is Outcome.FAILED and any(r.done.values()):
            return True
    return False


def row_by_iid(entries: Sequence[object]) -> Dict[str, object]:
    """Every row under ``entries`` — groups, jobs, destinations — by its identifier in the list."""
    found: Dict[str, object] = {}
    for entry in entries:
        found[entry.iid] = entry
        if isinstance(entry, GroupRow):
            found.update(row_by_iid(entry.jobs))
        elif isinstance(entry, JobRow):
            found.update(row_by_iid(entry.destinations))
    return found
