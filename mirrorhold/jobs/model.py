"""The job contract: what a user can set per job, and the application's preferences.

This is the seam between the GUI and the core. Both build and read jobs only
through these types, and the core re-validates a job against them whenever it
loads one, never trusting that the caller did.

A job has one source, one or more destinations, triggers, hold thresholds, a version
cleanup setting and a verification reminder. Thresholds and cleanup belong to the
job, not to a destination. Each destination also carries STATE (whether its first
run was confirmed, its last success, its last full verification), which is not
configuration and is not stored with it. A destination is a folder of this computer's
drives or of a remote of the user's rclone configuration (``endpoints.endpoint``); the
source is always a folder of this computer.

Which rclone configuration the remotes are reached through, and how it is unlocked, is
one setting for every job (``RemoteSettings``, in the preferences): only the file's path
and the unlock chosen are kept, never anything the configuration holds.

A group is a name the user keeps jobs together under, its members in the order they
were put in it. It arranges jobs and changes nothing about how any of them runs; the
rules that span groups and jobs — a job in at most one group, every member a stored
job, no two groups of one name — are the store's (``jobs.store.StoreContents``).

Documents are decoded strictly: every key is required and an unknown key is an
error, so a typo in a hand-edited or imported file is reported at its field rather
than read as a default. A newer schema arrives through the store's migrations.
"""
from __future__ import annotations

import ntpath
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..contract_errors import ContractError, require_keys
from ..endpoints.endpoint import AnyEndpoint, Endpoint, RemoteEndpoint, endpoint_from_document
from ..endpoints.volumes import DriveKind, lasting_drive
from .schedule import ScheduleError, parse_schedule

_ID = re.compile(r"^[0-9a-f]{32}$")

DEFAULT_COUNT_FLOOR = 1000
DEFAULT_PERCENT = 5
DEFAULT_SHARE = 50
DEFAULT_REMINDER_DAYS = 90
MAX_NAME_LENGTH = 200


def new_id() -> str:
    return uuid.uuid4().hex


def _check_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ContractError(where, f"not an identifier (32 lowercase hexadecimal digits): {value!r}")
    return value


def _check_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(where, f"expected true or false, got {value!r}")
    return value


def _check_int(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(where, f"expected a whole number of at least {minimum}, got {value!r}")
    return value


def _decode(where: str, build):
    try:
        return build()
    except ContractError as exc:
        raise exc.within(where) from None


@dataclass(frozen=True)
class DefaultExclusions:
    """The built-in exclusion categories a user may switch off for a source.

    The operating system's drive-root artefacts (recycle bin, system volume
    information, page, swap, hibernation and dump files) are always excluded from a
    drive-root source and are deliberately not represented here."""

    sync_client_artefacts: bool = True
    transient_editor_files: bool = True

    def __post_init__(self):
        _check_bool(self.sync_client_artefacts, "sync_client_artefacts")
        _check_bool(self.transient_editor_files, "transient_editor_files")

    def to_document(self) -> dict:
        return {"sync_client_artefacts": self.sync_client_artefacts,
                "transient_editor_files": self.transient_editor_files}

    @classmethod
    def from_document(cls, doc: Any) -> "DefaultExclusions":
        require_keys(doc, "", ("sync_client_artefacts", "transient_editor_files"))
        return cls(doc["sync_client_artefacts"], doc["transient_editor_files"])


@dataclass(frozen=True)
class Source:
    endpoint: Endpoint
    read_from_snapshot: bool = True
    default_exclusions: DefaultExclusions = field(default_factory=DefaultExclusions)
    exclusions: Tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.endpoint, Endpoint):
            raise ContractError("endpoint", "a source is a folder of this computer")
        _check_bool(self.read_from_snapshot, "read_from_snapshot")
        patterns = tuple(self.exclusions) if isinstance(self.exclusions, (list, tuple)) else None
        if patterns is None:
            raise ContractError("exclusions", "expected a list of patterns")
        for i, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern.strip() or "\n" in pattern or "\r" in pattern:
                raise ContractError(f"exclusions[{i}]", f"not a pattern: {pattern!r}")
        object.__setattr__(self, "exclusions", patterns)

    def to_document(self) -> dict:
        return {"endpoint": self.endpoint.to_document(),
                "read_from_snapshot": self.read_from_snapshot,
                "default_exclusions": self.default_exclusions.to_document(),
                "exclusions": list(self.exclusions)}

    @classmethod
    def from_document(cls, doc: Any) -> "Source":
        require_keys(doc, "", ("endpoint", "read_from_snapshot", "default_exclusions", "exclusions"))
        return cls(_decode("endpoint", lambda: Endpoint.from_document(doc["endpoint"])),
                   doc["read_from_snapshot"],
                   _decode("default_exclusions", lambda: DefaultExclusions.from_document(doc["default_exclusions"])),
                   doc["exclusions"])


@dataclass(frozen=True)
class Destination:
    endpoint: AnyEndpoint
    marker_id: str = field(default_factory=new_id)

    def __post_init__(self):
        if not isinstance(self.endpoint, (Endpoint, RemoteEndpoint)):
            raise ContractError("endpoint", f"not a destination's folder: {self.endpoint!r}")
        _check_id(self.marker_id, "marker_id")

    @property
    def remote(self) -> Optional[RemoteEndpoint]:
        """The remote's folder this destination is, or None for a folder of this computer."""
        return self.endpoint if isinstance(self.endpoint, RemoteEndpoint) else None

    def to_document(self) -> dict:
        return {"endpoint": self.endpoint.to_document(), "marker_id": self.marker_id}

    @classmethod
    def from_document(cls, doc: Any) -> "Destination":
        require_keys(doc, "", ("endpoint", "marker_id"))
        return cls(_decode("endpoint", lambda: endpoint_from_document(doc["endpoint"])), doc["marker_id"])


@dataclass(frozen=True)
class Triggers:
    """When a job runs by itself; a run started by the user is always possible.

    Neither is set by default: a new job runs only when the user starts it.
    ``on_arrival``: when any of the job's drives is attached, or the computer starts
    with it attached. ``schedule``: a schedule in ``jobs.schedule``'s grammar, kept in
    its canonical text, or None."""

    on_arrival: bool = False
    schedule: Optional[str] = None

    def __post_init__(self):
        _check_bool(self.on_arrival, "on_arrival")
        if self.schedule is not None:
            try:
                canonical = parse_schedule(self.schedule).text()
            except ScheduleError as exc:
                raise ContractError("schedule", str(exc)) from None
            object.__setattr__(self, "schedule", canonical)

    @property
    def automatic(self) -> bool:
        """Whether anything but the user starts this job's runs."""
        return self.on_arrival or self.schedule is not None

    def to_document(self) -> dict:
        return {"on_arrival": self.on_arrival, "schedule": self.schedule}

    @classmethod
    def from_document(cls, doc: Any) -> "Triggers":
        require_keys(doc, "", ("on_arrival", "schedule"))
        return cls(doc["on_arrival"], doc["schedule"])


def _check_percentage(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
        raise ContractError(where, f"expected a percentage above 0 and at most 100, got {value!r}")
    return value


@dataclass(frozen=True)
class HoldThresholds:
    """One run's removals wait for the user when they exceed the larger of
    ``count_floor`` files and ``percent`` per cent of the destination's files, or when
    they reach ``share`` per cent of the destination's files — what an emptied or wrong
    source looks like. ``share`` None switches that hold off: a source of one or a few
    files reaches half with a single edit, and whether that is a reason to wait is the
    user's call, never the tool's."""

    count_floor: int = DEFAULT_COUNT_FLOOR
    percent: float = DEFAULT_PERCENT
    share: Optional[float] = DEFAULT_SHARE

    def __post_init__(self):
        _check_int(self.count_floor, "count_floor", 0)
        _check_percentage(self.percent, "percent")
        if self.share is not None:
            _check_percentage(self.share, "share")

    def to_document(self) -> dict:
        return {"count_floor": self.count_floor, "percent": self.percent, "share": self.share}

    @classmethod
    def from_document(cls, doc: Any) -> "HoldThresholds":
        require_keys(doc, "", ("count_floor", "percent", "share"))
        return cls(doc["count_floor"], doc["percent"], doc["share"])


@dataclass(frozen=True)
class VersionCleanup:
    """``keep_days`` None: cleanup is off (the default); otherwise preserved versions
    older than that many days are removed."""

    keep_days: Optional[int] = None

    def __post_init__(self):
        if self.keep_days is not None:
            _check_int(self.keep_days, "keep_days", 1)

    @property
    def enabled(self) -> bool:
        return self.keep_days is not None

    def to_document(self) -> dict:
        return {"keep_days": self.keep_days}

    @classmethod
    def from_document(cls, doc: Any) -> "VersionCleanup":
        require_keys(doc, "", ("keep_days",))
        return cls(doc["keep_days"])


@dataclass(frozen=True)
class Job:
    job_id: str
    name: str
    source: Source
    destinations: Tuple[Destination, ...]
    enabled: bool = True
    triggers: Triggers = field(default_factory=Triggers)
    holds: HoldThresholds = field(default_factory=HoldThresholds)
    version_cleanup: VersionCleanup = field(default_factory=VersionCleanup)
    verification_reminder_days: int = DEFAULT_REMINDER_DAYS

    def __post_init__(self):
        _check_id(self.job_id, "id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ContractError("name", "a job needs a name")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ContractError("name", f"longer than {MAX_NAME_LENGTH} characters")
        _check_bool(self.enabled, "enabled")
        destinations = tuple(self.destinations) if isinstance(self.destinations, (list, tuple)) else None
        if not destinations:
            raise ContractError("destinations", "a job needs at least one destination")
        markers = [d.marker_id for d in destinations]
        if len(set(markers)) != len(markers):
            raise ContractError("destinations", "two destinations share a marker identity")
        object.__setattr__(self, "destinations", destinations)
        _check_int(self.verification_reminder_days, "verification_reminder_days", 1)

    def number_of(self, marker_id: str) -> Optional[int]:
        """The number (1-based, in the job's order) of the destination whose marker is
        ``marker_id``, or None when the job has none."""
        for number, destination in enumerate(self.destinations, 1):
            if destination.marker_id == marker_id:
                return number
        return None

    def destination_number(self, index: Optional[int] = None, marker: Optional[str] = None) -> int:
        """The number of the destination named by ``index`` (1-based) or by ``marker`` — exactly
        one of them; ValueError, saying why, when it names none of the job's destinations."""
        if (index is None) == (marker is None):
            raise ValueError("a destination is named by its number or by its marker")
        if marker is not None:
            number = self.number_of(marker)
            if number is None:
                raise ValueError(f"the job has no destination with marker {marker}")
            return number
        if not 1 <= index <= len(self.destinations):
            raise ValueError(f"the job has no destination {index}")
        return index

    def to_document(self) -> dict:
        return {"id": self.job_id,
                "name": self.name,
                "enabled": self.enabled,
                "source": self.source.to_document(),
                "destinations": [d.to_document() for d in self.destinations],
                "triggers": self.triggers.to_document(),
                "holds": self.holds.to_document(),
                "version_cleanup": self.version_cleanup.to_document(),
                "verification_reminder_days": self.verification_reminder_days}

    @classmethod
    def from_document(cls, doc: Any) -> "Job":
        require_keys(doc, "", ("id", "name", "enabled", "source", "destinations", "triggers", "holds",
                               "version_cleanup", "verification_reminder_days"))
        if not isinstance(doc["destinations"], list):
            raise ContractError("destinations", "expected a list")
        destinations = tuple(_decode(f"destinations[{i}]", lambda d=d: Destination.from_document(d))
                             for i, d in enumerate(doc["destinations"]))
        return cls(job_id=doc["id"],
                   name=doc["name"],
                   source=_decode("source", lambda: Source.from_document(doc["source"])),
                   destinations=destinations,
                   enabled=doc["enabled"],
                   triggers=_decode("triggers", lambda: Triggers.from_document(doc["triggers"])),
                   holds=_decode("holds", lambda: HoldThresholds.from_document(doc["holds"])),
                   version_cleanup=_decode("version_cleanup",
                                           lambda: VersionCleanup.from_document(doc["version_cleanup"])),
                   verification_reminder_days=doc["verification_reminder_days"])


def new_job(name: str, source: Endpoint, destinations: Sequence[AnyEndpoint], **settings) -> Job:
    """A job with fresh identifiers and every setting at its default unless given."""
    source_settings = {k: settings.pop(k) for k in ("read_from_snapshot", "default_exclusions", "exclusions")
                       if k in settings}
    return Job(job_id=new_id(), name=name.strip() if isinstance(name, str) else name,
               source=Source(source, **source_settings),
               destinations=tuple(Destination(e) for e in destinations), **settings)


def group_name_key(name: str) -> str:
    """What two group names are compared by: no two groups share one."""
    return name.strip().casefold()


@dataclass(frozen=True)
class Group:
    """A name jobs are kept together under, and the identifiers of its jobs in the order
    the user arranged them — the order they were put in it until the user moves one. Only
    the store can tell whether a member is a stored job."""

    group_id: str
    name: str
    job_ids: Tuple[str, ...] = ()

    def __post_init__(self):
        _check_id(self.group_id, "id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ContractError("name", "a group needs a name")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ContractError("name", f"longer than {MAX_NAME_LENGTH} characters")
        members = tuple(self.job_ids) if isinstance(self.job_ids, (list, tuple)) else None
        if members is None:
            raise ContractError("jobs", "expected a list of job identifiers")
        for i, job_id in enumerate(members):
            _check_id(job_id, f"jobs[{i}]")
        if len(set(members)) != len(members):
            raise ContractError("jobs", "a job is listed twice")
        object.__setattr__(self, "job_ids", members)

    def to_document(self) -> dict:
        return {"id": self.group_id, "name": self.name, "jobs": list(self.job_ids)}

    @classmethod
    def from_document(cls, doc: Any) -> "Group":
        require_keys(doc, "", ("id", "name", "jobs"))
        return cls(doc["id"], doc["name"], doc["jobs"])


def new_group(name: str, job_ids: Sequence[str] = ()) -> Group:
    """A group with a fresh identifier."""
    return Group(new_id(), name.strip() if isinstance(name, str) else name, tuple(job_ids))


class DriveTurns(str, Enum):
    """Whether activities that use one drive, reading or writing it, take turns on it — one
    at a time, the later waiting — or work at the same time, sharing its rate
    (``records.admission``). Decided per drive, by what the drive is
    (``endpoints.volumes.DriveKind``), the maintainer's decision of 2026-10-21."""

    AUTOMATIC = "automatic"                 # turns on a disk with a seek penalty or of a kind not known
    ONE_AT_A_TIME = "one_at_a_time"         # turns on every drive
    AT_THE_SAME_TIME = "at_the_same_time"   # turns on none


MARKED_KINDS = (DriveKind.SPINNING, DriveKind.SOLID, DriveKind.CLOUD)
"""What the user may say a drive is: a hard disk, a solid-state drive or a cloud drive."""


@dataclass(frozen=True)
class DriveMark:
    """What the user says one drive is, in place of what its disk reports (the maintainer's
    decision of 2026-10-21). ``drive`` names it as ``VolumeInfo.drive`` does — the disk by the
    GUID or signature it carries, or a volume no single disk carries by its own anchor — so
    the mark follows the drive whatever its letter or port, and only a name that lasts may be
    marked (``endpoints.volumes.lasting_drive``). ``name`` is how a person called it when it
    was last seen ("drive I:"), shown while it is not attached."""

    drive: str
    name: str
    kind: DriveKind

    def __post_init__(self):
        if not isinstance(self.drive, str) or not lasting_drive(self.drive):
            raise ContractError("drive", f"expected a drive whose name lasts, got {self.drive!r}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ContractError("name", f"expected the drive's name, got {self.name!r}")
        if not isinstance(self.kind, DriveKind) or self.kind not in MARKED_KINDS:
            raise ContractError("kind", f"expected one of {', '.join(k.value for k in MARKED_KINDS)}, "
                                        f"got {self.kind!r}")

    def to_document(self) -> dict:
        return {"drive": self.drive, "name": self.name, "kind": self.kind.value}

    @classmethod
    def from_document(cls, doc: Any) -> "DriveMark":
        require_keys(doc, "", ("drive", "name", "kind"))
        kind = next((k for k in MARKED_KINDS if k.value == doc["kind"]), doc["kind"])
        return cls(doc["drive"], doc["name"], kind)


@dataclass(frozen=True)
class Activities:
    """What the user chose for runs and full verifications — activities — as they work.

    ``drive_turns``: when activities that use one drive take turns on it; automatic by
    default — on a drive with a seek penalty, or one whose kind Windows does not report,
    they take turns, and on one with none (a solid-state drive) or a cloud drive they work at
    the same time.
    ``drive_kinds``: what the user says drives are, each taking the place of what its disk
    reports wherever automatic decides by the drive's kind; at most one mark a drive.
    ``keep_awake``: the computer is kept from falling asleep by itself while an activity
    works; ``name_at_shutdown``: Windows' shutdown screen names the activity at work and
    offers to cancel the shutdown (both ``power``). Both on at first (the maintainer's
    decision, 2026-10-13)."""

    drive_turns: DriveTurns = DriveTurns.AUTOMATIC
    keep_awake: bool = True
    name_at_shutdown: bool = True
    drive_kinds: Tuple[DriveMark, ...] = ()

    def __post_init__(self):
        if not isinstance(self.drive_turns, DriveTurns):
            raise ContractError("drive_turns", f"expected one of {', '.join(t.value for t in DriveTurns)}, "
                                               f"got {self.drive_turns!r}")
        for name in ("keep_awake", "name_at_shutdown"):
            _check_bool(getattr(self, name), name)
        if not isinstance(self.drive_kinds, (list, tuple)):
            raise ContractError("drive_kinds", "expected a list of drives")
        marks = tuple(self.drive_kinds)
        seen = set()
        for i, mark in enumerate(marks):
            if not isinstance(mark, DriveMark):
                raise ContractError(f"drive_kinds[{i}]", f"expected a drive's mark, got {mark!r}")
            if mark.drive in seen:
                raise ContractError(f"drive_kinds[{i}]", "a drive is marked twice")
            seen.add(mark.drive)
        object.__setattr__(self, "drive_kinds", marks)

    def to_document(self) -> dict:
        return {"drive_turns": self.drive_turns.value, "keep_awake": self.keep_awake,
                "name_at_shutdown": self.name_at_shutdown,
                "drive_kinds": [mark.to_document() for mark in self.drive_kinds]}

    @classmethod
    def from_document(cls, doc: Any) -> "Activities":
        require_keys(doc, "", ("drive_turns", "keep_awake", "name_at_shutdown", "drive_kinds"))
        turns = next((t for t in DriveTurns if t.value == doc["drive_turns"]), doc["drive_turns"])
        marks = doc["drive_kinds"]
        if not isinstance(marks, list):
            raise ContractError("drive_kinds", "expected a list of drives")
        return cls(turns, doc["keep_awake"], doc["name_at_shutdown"],
                   tuple(_decode(f"drive_kinds[{i}]", lambda m=m: DriveMark.from_document(m))
                         for i, m in enumerate(marks)))


@dataclass(frozen=True)
class Notifications:
    on_completion: bool = True
    on_hold: bool = True
    on_failure: bool = True

    def __post_init__(self):
        for name in ("on_completion", "on_hold", "on_failure"):
            _check_bool(getattr(self, name), name)

    def to_document(self) -> dict:
        return {"on_completion": self.on_completion, "on_hold": self.on_hold, "on_failure": self.on_failure}

    @classmethod
    def from_document(cls, doc: Any) -> "Notifications":
        require_keys(doc, "", ("on_completion", "on_hold", "on_failure"))
        return cls(doc["on_completion"], doc["on_hold"], doc["on_failure"])


class Unlock(str, Enum):
    """How an encrypted rclone configuration is opened for a start of the engine."""

    NONE = "none"                 # not encrypted, or refused while it is
    REMEMBERED = "remembered"     # the password, sealed for the user's Windows account (``remotes.seal``)
    COMMAND = "command"           # a command of the user's own prints it


@dataclass(frozen=True)
class RemoteSettings:
    """The rclone configuration every remote destination is reached through, and its unlock.

    ``configuration``: the absolute path of the file the user made with rclone, or None —
    no remote can then be reached. ``unlock_command``: the program and its arguments when
    ``unlock`` is a command, empty otherwise. Nothing the configuration holds is kept here;
    the remembered password, where the user chose it, is a sealed file beside the store."""

    configuration: Optional[str] = None
    unlock: Unlock = Unlock.NONE
    unlock_command: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.configuration is not None and (
                not isinstance(self.configuration, str) or not self.configuration.strip()
                or not ntpath.isabs(self.configuration) or "\n" in self.configuration):
            raise ContractError("configuration", f"expected an absolute path or null, got {self.configuration!r}")
        if not isinstance(self.unlock, Unlock):
            raise ContractError("unlock", f"expected one of {', '.join(u.value for u in Unlock)}, "
                                          f"got {self.unlock!r}")
        if not isinstance(self.unlock_command, (list, tuple)):
            raise ContractError("unlock_command", "expected a list of words")
        words = tuple(self.unlock_command)
        for i, word in enumerate(words):
            if not isinstance(word, str) or "\x00" in word or "\n" in word or "\r" in word:
                raise ContractError(f"unlock_command[{i}]", f"not a word of a command: {word!r}")
        if (self.unlock is Unlock.COMMAND) != bool(words) or (words and not words[0].strip()):
            raise ContractError("unlock_command", "a command is given exactly when the unlock is a command, "
                                                  "and names its program first")
        object.__setattr__(self, "unlock_command", words)

    def to_document(self) -> dict:
        return {"configuration": self.configuration, "unlock": self.unlock.value,
                "unlock_command": list(self.unlock_command)}

    @classmethod
    def from_document(cls, doc: Any) -> "RemoteSettings":
        require_keys(doc, "", ("configuration", "unlock", "unlock_command"))
        unlock = next((u for u in Unlock if u.value == doc["unlock"]), doc["unlock"])
        return cls(doc["configuration"], unlock, doc["unlock_command"])


def _check_plain(value: Any, where: str) -> Any:
    """A value JSON round-trips unchanged: null, boolean, number, string, list, object."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_check_plain(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ContractError(where, f"a key must be text, got {k!r}")
            out[k] = _check_plain(v, f"{where}.{k}")
        return out
    raise ContractError(where, f"not a plain value: {value!r}")


@dataclass(frozen=True)
class Preferences:
    """Application settings that belong to no job.

    ``engine_path`` None means the pinned engine copy. ``gui_state`` is the GUI's own
    remembered state (window geometry, last view); the core stores it and never
    reads it. ``activities``: the choices every run and verification reads as it works,
    whoever started it. ``remotes``: the rclone configuration remote destinations are
    reached through, and its unlock."""

    engine_path: Optional[str] = None
    notifications: Notifications = field(default_factory=Notifications)
    gui_state: Mapping[str, Any] = field(default_factory=dict)
    activities: Activities = field(default_factory=Activities)
    remotes: RemoteSettings = field(default_factory=RemoteSettings)

    def __post_init__(self):
        if self.engine_path is not None and (not isinstance(self.engine_path, str) or not self.engine_path.strip()):
            raise ContractError("engine_path", f"expected a path or null, got {self.engine_path!r}")
        if not isinstance(self.gui_state, Mapping):
            raise ContractError("gui_state", "expected an object")
        object.__setattr__(self, "gui_state", _check_plain(self.gui_state, "gui_state"))
        if not isinstance(self.remotes, RemoteSettings):
            raise ContractError("remotes", f"expected the remotes' settings, got {self.remotes!r}")

    def to_document(self) -> dict:
        return {"engine_path": self.engine_path,
                "notifications": self.notifications.to_document(),
                "gui_state": dict(self.gui_state),
                "activities": self.activities.to_document(),
                "remotes": self.remotes.to_document()}

    @classmethod
    def from_document(cls, doc: Any) -> "Preferences":
        require_keys(doc, "", ("engine_path", "notifications", "gui_state", "activities", "remotes"))
        return cls(doc["engine_path"],
                   _decode("notifications", lambda: Notifications.from_document(doc["notifications"])),
                   doc["gui_state"],
                   _decode("activities", lambda: Activities.from_document(doc["activities"])),
                   _decode("remotes", lambda: RemoteSettings.from_document(doc["remotes"])))


@dataclass(frozen=True)
class DestinationState:
    """What has happened to one destination of a job — state, not configuration.

    Times are ISO 8601 strings in UTC, or None when the event has not happened."""

    first_run_confirmed: bool = False
    last_success: Optional[str] = None
    last_full_verification: Optional[str] = None

    def to_document(self) -> Dict[str, Any]:
        return {"first_run_confirmed": self.first_run_confirmed,
                "last_success": self.last_success,
                "last_full_verification": self.last_full_verification}

    @classmethod
    def from_document(cls, doc: Any) -> "DestinationState":
        require_keys(doc, "", ("first_run_confirmed", "last_success", "last_full_verification"))
        _check_bool(doc["first_run_confirmed"], "first_run_confirmed")
        for key in ("last_success", "last_full_verification"):
            if doc[key] is not None and not isinstance(doc[key], str):
                raise ContractError(key, f"expected a time or null, got {doc[key]!r}")
        return cls(doc["first_run_confirmed"], doc["last_success"], doc["last_full_verification"])
