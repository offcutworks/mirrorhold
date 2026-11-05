"""Creation-time validation: which jobs may exist at all.

A finding is REFUSED (the job may not be saved), a WARNING (it may, and the user
should know why it may disappoint) or a NOTICE (information). Each carries a stable
code, so the GUI and the tests bind the code, never the wording.

Refused:
  source-not-a-folder              the source is a file: a job backs up a folder or a drive
  destination-not-a-folder         a destination is a file: a backup is written into a folder or onto a drive
  source-destination-overlap       the source and a destination are equal or nested
  destinations-overlap             two destinations of the job are equal or nested
  destination-shared               a destination is another job's destination
  destination-nested-with-other    a destination is nested with another job's destination
  destination-contains-other-source  a destination is another job's source: mirroring would write into it
  destination-is-system-folder     a destination is itself a folder Windows, installed programs, a user or
                                   this tool needs (``endpoints.system``): every run would write into it
  source-inside-other-destination  the same relation seen from the other job: this source is another job's destination
  endpoint-in-pool-folder          a path inside a DrivePool member's pool folder
  destination-on-pool-member       a destination on a DrivePool member disk, which is written only through the pool
  destination-on-virtual-drive     no disk partition behind the volume: a cloud or other virtual drive
  destination-in-sync-root         inside a folder a cloud client keeps on every computer of its account
                                   (the folder itself included)
  fat32-large-file                 a FAT32 destination and a source file of 4 GiB or more
  volume-ambiguous                 two present volumes carry the endpoint's identity
  remote-configuration-missing     a destination is a remote's folder and the preferences name no rclone
                                   configuration
  remote-configuration-unusable    the configuration cannot be opened or unlocked, the engine cannot be
                                   used, or the remote's service refused what it was asked
  remote-unknown                   the configuration holds no remote of the destination's name
  remote-below-floor               the remote keeps modification times coarser than two seconds, or none, or
                                   can neither move nor copy a file within itself
Warned:
  fat32-timestamps                 FAT32 keeps two-second, zone-less times
  source-exceeds-free-space        the source is larger than the destination's free space
Noticed:
  destination-in-cloud-copy        inside a folder of this computer a cloud client keeps a copy of in its
                                   cloud (the folder itself included): it may put back what a run moves
                                   into versions
  sync-roots-unreadable            a cloud client's list of the folders it synchronises could not be read,
                                   so whether it synchronises a destination cannot be told
  destination-contains-sync-root   a synchronised folder lies inside a destination
  destination-contains-other-source  another job's source lies inside a destination
  source-inside-other-destination  this source lies inside another job's destination
  destination-inside-other-source  allowed; the other job never copies this destination's management folder
  source-contains-other-destination  the same case seen from the outer job
  volume-absent                    checks that need the volume wait until it is present
  remote-unreachable               a remote's service could not be reached, or was not asked: checks that
                                   need it wait until it can be reached
  destination-not-encrypted        a remote that does not encrypt: the service, and anyone signed in to its
                                   account, can read every file, its name and the job's definition
  size-checks-deferred             no survey: the FAT32 and free-space checks run with the run
  survey-incomplete                some source folders could not be listed
  (neither, nor any check a survey feeds, for a source refused as a file: there is no folder to measure)

A synchronised folder or another job's source strictly inside a destination
(``contained_folders``) is allowed: the run protects it rather than creation refusing
it, because what protects it is how the destination's first run treated what it held,
which is recorded on the destination and not in the job. Such a destination's first run
is offered only to leave what it holds where it is, which makes it shared
(``endpoints.shared``), and a run stops at it, with the code above as its reason, when it
is the job's whole, when its first run was confirmed to move what it held into versions,
or when the folder lies beneath a top-level name the job owns or the source holds —
unless the job's exclusions hide the folder (``run.runner``).

What a cloud client does with its folder (``endpoints.cloud``) decides a destination
inside one: a folder kept on every computer of the account is refused, since every run's
writes would spread to all of them; a folder of this computer the client keeps a copy of
in its cloud only is allowed, since that copy can be how a user keeps a backup in the
cloud (the maintainer's decision of 2026-10-28), and noticed, since the client may write
back into it — Google Drive for desktop was measured putting back folders a run had just
moved into versions, which the next preview names as reappeared (``run.put_back``). That
notice, and the one saying a client's list could not be read, are the ones a preview shows
beside the destination (``PREVIEW_NOTICES``). A synchronised folder strictly inside a
destination is protected whatever its kind.

The folders Windows and this tool need are protected the same way when they lie strictly
inside a destination; one that is the destination itself cannot be, since leaving what
it holds still has every run put the source's folders into it — a user profile's
Documents among them — so such a destination is refused.

Every volume-level check needs the endpoint's volume present; the run re-checks what
creation could not. The context says what validation sees of the computer: the volumes,
the sync roots, the other jobs, the folders Windows and this tool need, and how to tell
whether a path is a file — the one check that looks at a disk, at the source's own entry
and at each destination's. ``ValidationContext.on_this_computer`` fills the last two from
this process's environment and disk; built from its fields alone, a context names no such
folder and finds no file, so the rules are tested without either.

A remote's folder is judged by what the engine said of it (``remotes.facts.RemoteSetup``,
the context's ``remote_setup``), gathered by the caller; a context without it — the
window's import — says the remote's checks wait, as for a drive not attached, and the
run re-checks. The relations between endpoints hold for remote folders alike, by remote
and folder (``RemoteEndpoint.contains``); a remote's folder never lies inside a drive's or
contains one. Where a remote does not encrypt, the notice saying what its service can read
(``destination-not-encrypted``) is shown beside it in every preview too.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from ..endpoints.cloud import SyncKind, SyncRoot, sync_root_endpoints
from ..endpoints.endpoint import Endpoint, RemoteEndpoint, first_component
from ..endpoints.resolution import Presence, resolve
from ..endpoints.system import SystemFolder, system_folder_endpoints, system_folders
from ..endpoints.volumes import FAT_MAX_FILE_BYTES, POOL_FOLDER, Backing, VolumeInfo
from ..remotes.facts import Reach, RemoteSetup, SetupState, floor_problem
from ..wording import agreeing, counted
from .model import Job
from .survey import SourceSurvey

SOURCE_NOT_A_FOLDER = "source-not-a-folder"
DESTINATION_NOT_A_FOLDER = "destination-not-a-folder"
DESTINATION_IS_SYSTEM_FOLDER = "destination-is-system-folder"
DESTINATION_IN_CLOUD_COPY = "destination-in-cloud-copy"
SYNC_ROOTS_UNREADABLE = "sync-roots-unreadable"
REMOTE_CONFIGURATION_MISSING = "remote-configuration-missing"
REMOTE_CONFIGURATION_UNUSABLE = "remote-configuration-unusable"
REMOTE_UNKNOWN = "remote-unknown"
REMOTE_BELOW_FLOOR = "remote-below-floor"
REMOTE_UNREACHABLE = "remote-unreachable"
DESTINATION_NOT_ENCRYPTED = "destination-not-encrypted"
PREVIEW_NOTICES = (DESTINATION_IN_CLOUD_COPY, SYNC_ROOTS_UNREADABLE, DESTINATION_NOT_ENCRYPTED)
"""The notices a preview shows beside their destination, where its removals are confirmed."""


class Severity(str, Enum):
    REFUSED = "refused"
    WARNING = "warning"
    NOTICE = "notice"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    subject: str
    message: str

    def describe(self) -> str:
        return f"{self.severity.value.upper()} [{self.code}] {self.subject}: {self.message}"


@dataclass(frozen=True)
class ValidationReport:
    job: Job
    findings: Tuple[Finding, ...]

    @property
    def refused(self) -> bool:
        return any(f.severity is Severity.REFUSED for f in self.findings)

    def codes(self, severity: Optional[Severity] = None) -> Tuple[str, ...]:
        return tuple(f.code for f in self.findings if severity is None or f.severity is severity)


def _no_file(path: str) -> bool:
    return False


@dataclass(frozen=True)
class ValidationContext:
    volumes: Tuple[VolumeInfo, ...] = ()
    sync_roots: Tuple[SyncRoot, ...] = ()
    other_jobs: Tuple[Job, ...] = ()
    system_folders: Tuple[SystemFolder, ...] = ()    # the folders Windows and this tool need
    is_file: Callable[[str], bool] = _no_file        # whether a path names a file
    remote_setup: Optional[RemoteSetup] = None       # what the engine said; None: not asked

    @classmethod
    def on_this_computer(cls, volumes: Sequence[VolumeInfo], sync_roots: Sequence[SyncRoot],
                         other_jobs: Sequence[Job], store_path: Path, *,
                         remote_setup: Optional[RemoteSetup] = None) -> "ValidationContext":
        """What validation sees of this computer for the job file at ``store_path``: the
        folders its environment names, and its disk asked whether a path is a file;
        ``remote_setup``, what the engine said of the job's remote folders, when asked."""
        return cls(tuple(volumes), tuple(sync_roots), tuple(other_jobs), system_folders(store_path),
                   os.path.isfile, remote_setup)


@dataclass(frozen=True)
class Contained:
    """A folder inside a destination that no run of its job may move or write into: the
    code it is reported under, its path relative to the destination, as the endpoint
    spells it ("" when it is the destination itself), and the folder in words — where it
    is, then what it is: "C:\\Users\\me\\OneDrive, which OneDrive synchronises"."""

    code: str
    relative: str
    words: str

    @property
    def top(self) -> Optional[str]:
        """The comparison key of the destination's top-level entry it lies beneath."""
        return self.relative.split("\\", 1)[0].casefold() if self.relative else None


def relative_inside(outer: Endpoint, inner: Endpoint) -> Optional[str]:
    """``inner``'s path relative to ``outer``, as ``inner`` spells it, when ``outer``
    contains it ("" when they are the same folder); else None."""
    if not outer.contains(inner):
        return None
    depth = len(outer.path.split("\\")) if outer.path else 0
    return "\\".join(inner.path.split("\\")[depth:]) if inner.path else ""


def _root_path(root: SyncRoot, endpoint: Endpoint, volumes: Sequence[VolumeInfo]) -> str:
    """A synchronised folder's path in words: where it lies now when the client names its
    volume (the path it last saw may carry a letter the volume no longer has), else the
    path the client gives."""
    if root.locator:
        where = resolve(endpoint, volumes)
        if where.presence is Presence.PRESENT:
            return str(where.location)
    return root.path


def contained_folders(job: Job, index: int, context: ValidationContext) -> Tuple[Contained, ...]:
    """The synchronised folders and other jobs' sources strictly inside destination
    ``index`` (1-based): what its runs leave where it is and never write into."""
    dest = job.destinations[index - 1].endpoint
    found: List[Contained] = []
    for root, root_endpoint in sync_root_endpoints(context.sync_roots, context.volumes):
        relative = relative_inside(dest, root_endpoint)
        if relative:
            found.append(Contained("destination-contains-sync-root", relative,
                                   f"{_root_path(root, root_endpoint, context.volumes)}, which {root.provider} "
                                   "synchronises"))
    for other in context.other_jobs:
        if other.job_id == job.job_id:
            continue
        relative = relative_inside(dest, other.source.endpoint)
        if relative:
            where = resolve(other.source.endpoint, context.volumes)
            path = where.location if where.presence is Presence.PRESENT else "\\" + other.source.endpoint.path
            found.append(Contained("destination-contains-other-source", relative,
                                   f"{path}, the source of job '{other.name}'"))
    return tuple(found)


def _in_pool_folder(endpoint: Endpoint) -> bool:
    head = first_component(endpoint.path)
    return head is not None and bool(POOL_FOLDER.match(head))


NOT_ENCRYPTED = ("this remote does not encrypt: the service, and anyone signed in to its account, can read every "
                 "file and its name, and the job's definition kept with the backup, which names the source folder's "
                 "path on this computer")
"""What a remote destination that does not encrypt leaves readable, said wherever it is judged."""


def _remote_findings(add, subject: str, dest: RemoteEndpoint, setup: Optional[RemoteSetup],
                     survey: Optional[SourceSurvey]) -> None:
    """A remote destination's findings from what the engine said of it."""
    if setup is None:
        add(Severity.NOTICE, REMOTE_UNREACHABLE, subject,
            f"{dest.describe()} was not asked; checks that need the remote run when it can be reached")
        return
    if setup.state is SetupState.MISSING:
        add(Severity.REFUSED, REMOTE_CONFIGURATION_MISSING, subject, setup.reason)
        return
    if setup.state is not SetupState.READY:
        add(Severity.REFUSED, REMOTE_CONFIGURATION_UNUSABLE, subject, setup.reason)
        return
    if dest.remote not in setup.remotes:
        add(Severity.REFUSED, REMOTE_UNKNOWN, subject,
            f"the rclone configuration holds no remote named '{dest.remote}'")
        return
    facts = setup.facts.get(dest)
    if facts is None or facts.reach is Reach.UNREACHABLE:
        why = f": {facts.reason}" if facts is not None and facts.reason else ""
        add(Severity.NOTICE, REMOTE_UNREACHABLE, subject,
            f"{dest.describe()} cannot be reached{why}; checks that need the remote run when it can be reached")
        return
    if facts.reach is Reach.REFUSED:
        add(Severity.REFUSED, REMOTE_CONFIGURATION_UNUSABLE, subject,
            f"the service of remote '{dest.remote}' refused: {facts.reason}")
        return
    if facts.is_file:
        add(Severity.REFUSED, DESTINATION_NOT_A_FOLDER, subject,
            "this is a file, not a folder; a backup is written into a folder or onto a whole drive")
        return
    problem = floor_problem(facts.features)
    if problem:
        add(Severity.REFUSED, REMOTE_BELOW_FLOOR, subject,
            f"remote '{dest.remote}' cannot hold a backup: {problem}")
    if not facts.features.encrypted:
        add(Severity.NOTICE, DESTINATION_NOT_ENCRYPTED, subject, NOT_ENCRYPTED)
    if survey is not None and facts.space is not None and survey.total_bytes > facts.space[0]:
        add(Severity.WARNING, "source-exceeds-free-space", subject,
            f"the source holds {counted(survey.total_bytes, 'byte')} and this remote reports "
            f"{counted(facts.space[0], 'byte')} free; "
            "a first run will not fit unless the destination already holds most of it")


def validate_job(job: Job, context: ValidationContext, survey: Optional[SourceSurvey] = None) -> ValidationReport:
    findings: List[Finding] = []

    def add(severity, code, subject, message):
        findings.append(Finding(severity, code, subject, message))

    source = job.source.endpoint
    destinations = [(f"destination {i + 1}", d.endpoint) for i, d in enumerate(job.destinations)]
    others = [j for j in context.other_jobs if j.job_id != job.job_id]

    # --- endpoint relations: need no volume ---------------------------------------
    for subject, dest in destinations:
        if source.overlaps(dest):
            add(Severity.REFUSED, "source-destination-overlap", subject,
                "the source and this destination are the same folder or one lies inside the other")
    for i, (subject_a, a) in enumerate(destinations):
        for subject_b, b in destinations[i + 1:]:
            if a.overlaps(b):
                add(Severity.REFUSED, "destinations-overlap", f"{subject_a} and {subject_b}",
                    "two destinations are the same folder or one lies inside the other")
    for other in others:
        other_source = other.source.endpoint
        for subject, dest in destinations:
            for other_dest in (d.endpoint for d in other.destinations):
                if dest.contains(other_dest) and other_dest.contains(dest):
                    add(Severity.REFUSED, "destination-shared", subject,
                        f"this folder is already a destination of job '{other.name}'")
                elif dest.overlaps(other_dest):
                    add(Severity.REFUSED, "destination-nested-with-other", subject,
                        f"this folder and a destination of job '{other.name}' lie one inside the other")
            if dest.contains(other_source) and other_source.contains(dest):
                add(Severity.REFUSED, "destination-contains-other-source", subject,
                    f"this folder is the source of job '{other.name}'; mirroring here would write into it")
            elif dest.contains(other_source):
                add(Severity.NOTICE, "destination-contains-other-source", subject,
                    f"the source of job '{other.name}' lies inside this destination; the job leaves it where it "
                    "is and never writes into it")
            elif other_source.contains(dest):
                add(Severity.NOTICE, "destination-inside-other-source", subject,
                    f"this destination lies inside the source of job '{other.name}'; that job will not copy "
                    "this destination's management folder")
        for other_dest in (d.endpoint for d in other.destinations):
            if other_dest.contains(source) and source.contains(other_dest):
                add(Severity.REFUSED, "source-inside-other-destination", "source",
                    f"this source is a destination of job '{other.name}', whose runs write into it; "
                    "back up that job's source directly instead")
            elif other_dest.contains(source):
                add(Severity.NOTICE, "source-inside-other-destination", "source",
                    f"this source lies inside a destination of job '{other.name}', which leaves it where it is "
                    "and never writes into it")
            elif source.contains(other_dest):
                add(Severity.NOTICE, "source-contains-other-destination", "source",
                    f"a destination of job '{other.name}' lies inside this source; this job will not copy "
                    "its management folder")
    for subject, endpoint in [("source", source)] + destinations:
        if isinstance(endpoint, Endpoint) and _in_pool_folder(endpoint):
            add(Severity.REFUSED, "endpoint-in-pool-folder", subject,
                "this path lies inside a DrivePool member's pool folder; use the pool's own drive instead")

    # --- volume checks: need the volume present ----------------------------------
    volumes = tuple(context.volumes)
    roots = sync_root_endpoints(context.sync_roots, volumes)
    unreadable = [root for root in context.sync_roots if root.problem]

    source_resolution = resolve(source, volumes)
    source_is_file = False
    if source_resolution.presence is Presence.ABSENT:
        add(Severity.NOTICE, "volume-absent", "source",
            "the source's volume is not present; checks that need it run when it is")
    elif source_resolution.presence is Presence.AMBIGUOUS:
        add(Severity.REFUSED, "volume-ambiguous", "source", source_resolution.describe())
    elif context.is_file(str(source_resolution.location)):
        source_is_file = True
        survey = None                       # a survey of a file measures no folder
        add(Severity.REFUSED, SOURCE_NOT_A_FOLDER, "source",
            "this is a file, not a folder; a job backs up a folder or a whole drive")

    size_needed = False
    for subject, dest in destinations:
        if isinstance(dest, RemoteEndpoint):
            _remote_findings(add, subject, dest, context.remote_setup, survey)
            continue
        resolution = resolve(dest, volumes)
        if resolution.presence is Presence.ABSENT:
            add(Severity.NOTICE, "volume-absent", subject,
                "this destination's volume is not present; checks that need it run when it is")
            continue
        if resolution.presence is Presence.AMBIGUOUS:
            add(Severity.REFUSED, "volume-ambiguous", subject, resolution.describe())
            continue
        if context.is_file(str(resolution.location)):
            add(Severity.REFUSED, DESTINATION_NOT_A_FOLDER, subject,
                "this is a file, not a folder; a backup is written into a folder or onto a whole drive")
        volume = resolution.volume
        if volume.pool_member:
            add(Severity.REFUSED, "destination-on-pool-member", subject,
                "this volume is a DrivePool member disk; the pool is written only through its own drive")
        if volume.backing is Backing.VIRTUAL:
            add(Severity.REFUSED, "destination-on-virtual-drive", subject,
                f"no disk partition carries '{volume.label or volume.root}': a cloud or other virtual drive "
                "cannot be a destination")
        for root, root_endpoint in roots:
            where = _root_path(root, root_endpoint, volumes)
            if root_endpoint.contains(dest):
                if root.kind is SyncKind.CLOUD_COPY:
                    add(Severity.NOTICE, DESTINATION_IN_CLOUD_COPY, subject,
                        f"this folder lies inside {where}, of which {root.provider} keeps a copy in its cloud; "
                        f"{root.provider} may put back what a run moves into versions, and the next preview then "
                        "names what reappeared, which its run moves into versions again")
                else:
                    add(Severity.REFUSED, "destination-in-sync-root", subject,
                        f"this folder lies inside {where}, which {root.provider} synchronises")
            elif dest.contains(root_endpoint):
                add(Severity.NOTICE, "destination-contains-sync-root", subject,
                    f"{where}, which {root.provider} synchronises, lies inside this destination; the job "
                    "leaves it where it is and never writes into it")
        for root in unreadable:
            add(Severity.NOTICE, SYNC_ROOTS_UNREADABLE, subject,
                f"whether {root.provider} synchronises this folder cannot be told: {root.problem}")
        if volume.is_fat:
            size_needed = True
            if survey is not None and survey.largest_file_bytes > FAT_MAX_FILE_BYTES:
                add(Severity.REFUSED, "fat32-large-file", subject,
                    f"the source holds a file of {counted(survey.largest_file_bytes, 'byte')}; {volume.file_system} "
                    "cannot store a file of 4 GiB or more, so the copy would fail on every run")
            else:
                add(Severity.WARNING, "fat32-timestamps", subject,
                    f"{volume.file_system} keeps times to two seconds and without a time zone; unchanged "
                    "files may be copied again after a daylight-saving change")
        if survey is not None:
            if survey.total_bytes > volume.free_bytes:
                add(Severity.WARNING, "source-exceeds-free-space", subject,
                    f"the source holds {counted(survey.total_bytes, 'byte')} and this volume has "
                    f"{counted(volume.free_bytes, 'byte')} free; a first run will not fit unless the destination "
                    "already holds most of it")
        else:
            size_needed = True

    systems = system_folder_endpoints(context.system_folders, volumes)
    for subject, dest in destinations:
        folder = next((f for f, inner in systems if dest.contains(inner) and inner.contains(dest)), None)
        if folder is not None:
            add(Severity.REFUSED, DESTINATION_IS_SYSTEM_FOLDER, subject,
                f"this folder is {folder.words}: backing up here would write into it. "
                "Choose a folder of its own instead")

    if survey is None and size_needed and source_resolution.presence is not Presence.AMBIGUOUS \
            and not source_is_file:
        add(Severity.NOTICE, "size-checks-deferred", "source",
            "the source was not surveyed; the FAT32 file-size and free-space checks run with the run")
    if survey is not None and not survey.complete:
        n = survey.unreadable_directories
        add(Severity.NOTICE, "survey-incomplete", "source",
            f"{counted(n, 'source folder')} could not be listed; the size figures leave "
            f"{agreeing(n, 'it', 'them')} out")
    return ValidationReport(job, tuple(findings))


def preview_notices(report: ValidationReport, index: int) -> Tuple[str, ...]:
    """What a preview says beside destination ``index`` (1-based): the messages of its
    ``PREVIEW_NOTICES``, in the report's order."""
    subject = f"destination {index}"
    return tuple(f.message for f in report.findings if f.code in PREVIEW_NOTICES and f.subject == subject)
