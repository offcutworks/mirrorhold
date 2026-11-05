"""Run records: what each pair's run did, written when it ends.

One record per pair per run, in ``<layout>/runs/<job id>/``, named by the run's start
time so that names sort by time; the same record is copied into the destination's
management folder when the run had verified (or, at a confirmed first run, written)
that destination's marker, so a destination carries its own history. A record is
written whatever the outcome, an unexpected failure included; a run whose process died
is recorded as interrupted by the next run (``status.reap_interrupted``).

Records feed the history, the estimate's throughput, and the per-destination state
(first run confirmed, last success). Counts are kept in three fixed sets — planned,
done, and for a run that did not complete, left — so a reader never guesses a key.

A record also says how the source was read (from a volume snapshot, or as it is and
why), and which skipped files a later run could still back up — every skipped file
but a cloud-only one or a link, which are left out by design — each with the number
of runs in a row it has gone unbacked-up. A file skipped again by the next run that
reaches its copies has gone ``ESCALATE_AFTER`` runs: the user is told.

It also says what the job's version cleanup deleted from the destination, when the job
has it on (``VersionsCleaned``) — and whether the service of a remote destination may keep
what was deleted until it removes it — and how much free space the destination's drive
had when the pair ended, a remote's as its service reports it, so the history and the
run's notice can say when it is low.

Among what was done are the changed files the engine found identical to their copy and
only brought up to date (their time set): ``files_identical`` and ``bytes_identical``.
They count as done against the plan, so they are not ``left``.

Every file the records package writes holds names as NTFS spells them, an unpaired
surrogate included, which UTF-8 cannot carry: ``json_text`` escapes it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contract_errors import ContractError, require_keys
from ..endpoints.marker import MANAGEMENT_DIR, MarkerState
from ..endpoints.volumes import free_percent, low_on_space
from ..jobs.model import DestinationState
from ..locations import RuntimeLayout
from ..planning.estimate import MIN_SAMPLE_BYTES
from ..storage.drive import destination_storage
from ..wording import counted
from .. import exchange

RECORD_FORMAT = "mirrorhold-run-record"
RECORD_SCHEMA = 6
"""Schema 2 added ``source_read`` and ``not_backed_up``, schema 3 ``versions_cleaned``
and ``space``, schema 4 ``files_identical`` and ``bytes_identical`` among the done
counts, schema 5 ``trash`` in ``versions_cleaned``, schema 6 ``encrypted``; an older record
reads with the later fields absent (the source's reading unknown, no streak, no cleanup,
the free space unknown, nothing found identical, no trash said, whether a remote encrypts
not known)."""
READABLE_SCHEMAS = (1, 2, 3, 4, 5, 6)
RUNS_DIR = "runs"
MAX_LISTED = 1000
"""Skipped files and errors are listed up to this many; their totals are always kept."""
MAX_PROBLEMS = 50

PLANNED_KEYS = ("new_files", "changed_files", "files_to_copy", "bytes_to_copy", "deleted_files", "renames",
                "unchanged", "kept", "temporaries", "destination_files")
DONE_KEYS = ("files_copied", "bytes_copied", "moved_to_versions", "renamed", "temporaries_removed",
             "files_identical", "bytes_identical")
_DONE_KEYS_BEFORE_4 = DONE_KEYS[:5]
LEFT_KEYS = ("files_to_copy", "bytes_to_copy", "removals", "renames")


class Outcome(str, Enum):
    COMPLETED = "completed"       # the plan applied; files listed as skipped excepted
    HELD = "held"                 # removals (or a first run whole) wait for the user; additions copied
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"   # the process ended mid-run: shutdown, unplugging, a crash
    FAILED = "failed"             # an unexpected error, or the engine ending unasked
    STOPPED = "stopped"           # the safety gate refused the pair (identity, marker, space, FAT32)
    ABSENT = "absent"             # an endpoint's volume is not present; the pair waits


APPLIED = (Outcome.COMPLETED, Outcome.HELD, Outcome.CANCELLED, Outcome.INTERRUPTED, Outcome.FAILED)

FIRST_RUN_REASON = "first-run"
"""The reason a pair's first run, held whole for confirmation, is recorded with."""

HOLDS_CONTENT_REASON = "destination-holds-content"
"""Recorded beside ``FIRST_RUN_REASON`` when the destination already held something: its
first run needs the user's review of what is there, where one onto a folder holding
nothing needs only their go-ahead."""

SHARED_RECORD_LOST = "shared-record-lost"
"""Recorded last, beside the reasons a pair stopped or waits, when the pair is a shared
destination whose record was lost: its run is a first run again, which leaves what is
there and makes the record anew once the user has reviewed it (``run.runner``)."""

ESCALATE_AFTER = 2
"""Runs in a row a file must go unbacked-up before the user is told: skipped again by
the next run, which is what "consecutive runs" means at its least."""


class ReadMode(str, Enum):
    SNAPSHOT = "snapshot"         # from a volume snapshot made for the run
    LIVE = "live"                 # as it is on the volume


@dataclass(frozen=True)
class SourceRead:
    """How a run read its source. ``drive``: the source's drive as the user knows it (its
    letter, else its label). ``reason``: why it was read as it is (a code), "" for a
    snapshot; ``taken``: when the snapshot was made (UTC)."""

    mode: ReadMode
    drive: str = ""
    reason: str = ""
    detail: str = ""
    taken: Optional[str] = None

    def describe(self) -> str:
        if self.mode is ReadMode.SNAPSHOT:
            return f"read from a snapshot of {self.drive or 'its drive'} taken {self.taken}"
        return f"read as it is ({self.reason}" + (f": {self.detail})" if self.detail else ")")

    def to_document(self) -> dict:
        return {"mode": self.mode.value, "drive": self.drive, "reason": self.reason, "detail": self.detail,
                "taken": self.taken}

    @classmethod
    def from_document(cls, doc: Any) -> "SourceRead":
        require_keys(doc, "source_read", ("mode", "drive", "reason", "detail", "taken"))
        try:
            mode = ReadMode(doc["mode"])
        except ValueError:
            raise ContractError("source_read.mode", f"unknown mode {doc['mode']!r}") from None
        return cls(mode, str(doc["drive"]), str(doc["reason"]), str(doc["detail"]), doc["taken"])


@dataclass(frozen=True)
class VersionsCleaned:
    """What a run's version cleanup deleted from one destination: whole versions batches
    older than ``keep_days`` days, the only permanent deletion the tool performs.
    ``batches``: the deleted batches' names, the first MAX_LISTED; ``problems``: what it
    could not delete or would not touch; ``trash``: the destination's service may keep
    what was deleted, counted against its space, until it removes it itself."""

    keep_days: int
    batches: Tuple[str, ...] = ()
    batch_total: int = 0
    files: int = 0
    bytes: int = 0
    problems: Tuple[str, ...] = ()
    trash: bool = False

    def describe(self) -> str:
        if not self.batch_total:
            text = f"no versions batch older than {counted(self.keep_days, 'day')}"
        else:
            text = (f"{counted(self.batch_total, 'versions batch', 'versions batches')} older than "
                    f"{counted(self.keep_days, 'day')} deleted ({counted(self.files, 'file')}, "
                    f"{counted(self.bytes, 'byte')})")
            if self.trash:
                text += "; the service may keep them until it removes them"
        if self.problems:
            text += f"; {counted(len(self.problems), 'problem')}"
        return text

    def to_document(self) -> dict:
        return {"keep_days": self.keep_days, "batches": list(self.batches), "batch_total": self.batch_total,
                "files": self.files, "bytes": self.bytes, "problems": list(self.problems), "trash": self.trash}

    @classmethod
    def from_document(cls, doc: Any, schema: int = RECORD_SCHEMA) -> "VersionsCleaned":
        """``schema``: the record's; ``trash`` is a key of schema 5 on, and a record older
        says nothing of it."""
        keys = ("keep_days", "batches", "batch_total", "files", "bytes", "problems")
        require_keys(doc, "versions_cleaned", keys + (("trash",) if schema >= 5 else ()))
        if schema >= 5 and not isinstance(doc["trash"], bool):
            raise ContractError("versions_cleaned.trash", f"expected true or false, got {doc['trash']!r}")
        return cls(int(doc["keep_days"]), tuple(str(b) for b in doc["batches"]), int(doc["batch_total"]),
                   int(doc["files"]), int(doc["bytes"]), tuple(str(p) for p in doc["problems"]),
                   doc["trash"] if schema >= 5 else False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counts(doc: Any, keys: Sequence[str], where: str) -> Dict[str, int]:
    require_keys(doc, where, keys)
    for k in keys:
        if isinstance(doc[k], bool) or not isinstance(doc[k], int):
            raise ContractError(f"{where}.{k}", f"expected a whole number, got {doc[k]!r}")
    return {k: doc[k] for k in keys}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    job_id: str
    job_name: str
    marker_id: str
    destination_index: int          # 1-based, in the job's order
    trigger: str                    # "manual", "unattended", "arrival" or "schedule"
    started: str
    ended: str
    outcome: Outcome
    reasons: Tuple[str, ...] = ()   # hold reasons, stop and failure codes
    detail: str = ""
    engine_version: Optional[str] = None
    planned: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(PLANNED_KEYS, 0))
    done: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(DONE_KEYS, 0))
    left: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(LEFT_KEYS, 0))
    versions_batch: Optional[str] = None     # relative to the destination folder
    skipped: Tuple[Tuple[str, str], ...] = ()  # (path, reason), the first MAX_LISTED
    skipped_total: int = 0
    errors: Tuple[str, ...] = ()
    listing_seconds: Tuple[float, float] = (0.0, 0.0)
    transfer_seconds: float = 0.0
    source_read: Optional[SourceRead] = None          # None: the source was not read
    not_backed_up: Tuple[Tuple[str, int], ...] = ()   # (path, runs in a row), the first MAX_LISTED
    versions_cleaned: Optional[VersionsCleaned] = None  # None: the job's cleanup is off, or the pair ended first
    space: Optional[Tuple[int, int]] = None           # (free, capacity) bytes of the destination's drive at the end
    encrypted: Optional[bool] = None                  # whether a remote it reached encrypts; None: a drive, not reached

    @property
    def low_on_space(self) -> bool:
        """Whether the destination's drive had less free space than the warning's share
        when the pair ended."""
        return self.space is not None and low_on_space(*self.space)

    @property
    def free_percent(self) -> Optional[float]:
        return free_percent(*self.space) if self.space is not None else None

    @property
    def repeatedly_unbacked(self) -> Tuple[Tuple[str, int], ...]:
        """The files this run left unbacked-up that the previous runs did too — what the
        user is told about — most runs first."""
        return tuple(sorted(((p, n) for p, n in self.not_backed_up if n >= ESCALATE_AFTER),
                            key=lambda item: (-item[1], item[0].casefold())))

    @property
    def throughput(self) -> Optional[float]:
        """Bytes per second of the copy phase — copied, or compared and found identical —
        when it processed enough to measure. Counting the identical bytes keeps a run that
        mostly compared from recording a rate near zero."""
        processed = self.done.get("bytes_copied", 0) + self.done.get("bytes_identical", 0)
        if processed < MIN_SAMPLE_BYTES or self.transfer_seconds <= 0:
            return None
        return processed / self.transfer_seconds

    def to_document(self) -> dict:
        return {"format": RECORD_FORMAT, "schema_version": RECORD_SCHEMA, "run_id": self.run_id,
                "job_id": self.job_id, "job_name": self.job_name, "marker_id": self.marker_id,
                "destination_index": self.destination_index, "trigger": self.trigger, "started": self.started,
                "ended": self.ended, "outcome": self.outcome.value, "reasons": list(self.reasons),
                "detail": self.detail, "engine_version": self.engine_version, "planned": dict(self.planned),
                "done": dict(self.done), "left": dict(self.left), "versions_batch": self.versions_batch,
                "skipped": [list(s) for s in self.skipped], "skipped_total": self.skipped_total,
                "errors": list(self.errors), "listing_seconds": list(self.listing_seconds),
                "transfer_seconds": self.transfer_seconds,
                "source_read": self.source_read.to_document() if self.source_read else None,
                "not_backed_up": [list(item) for item in self.not_backed_up],
                "versions_cleaned": self.versions_cleaned.to_document() if self.versions_cleaned else None,
                "space": list(self.space) if self.space is not None else None, "encrypted": self.encrypted}

    @classmethod
    def from_document(cls, doc: Any) -> "RunRecord":
        keys = ("format", "schema_version", "run_id", "job_id", "job_name", "marker_id", "destination_index",
                "trigger", "started", "ended", "outcome", "reasons", "detail", "engine_version", "planned", "done",
                "left", "versions_batch", "skipped", "skipped_total", "errors", "listing_seconds",
                "transfer_seconds")
        schema = doc.get("schema_version") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or doc.get("format") != RECORD_FORMAT or schema not in READABLE_SCHEMAS:
            raise ContractError("format", f"not a run record of schema {', '.join(map(str, READABLE_SCHEMAS))}")
        if schema >= 2:
            keys += ("source_read", "not_backed_up")
        if schema >= 3:
            keys += ("versions_cleaned", "space")
        if schema >= 6:
            keys += ("encrypted",)
        done_keys = DONE_KEYS if schema >= 4 else _DONE_KEYS_BEFORE_4
        require_keys(doc, "", keys)
        try:
            outcome = Outcome(doc["outcome"])
        except ValueError:
            raise ContractError("outcome", f"unknown outcome {doc['outcome']!r}") from None
        source_read, unbacked, cleaned, space, encrypted = None, (), None, None, None
        if schema >= 2:
            source_read = SourceRead.from_document(doc["source_read"]) if doc["source_read"] is not None else None
            unbacked = tuple((str(p), int(n)) for p, n in doc["not_backed_up"])
        if schema >= 3:
            if doc["versions_cleaned"] is not None:
                cleaned = VersionsCleaned.from_document(doc["versions_cleaned"], schema)
            if doc["space"] is not None:
                free, capacity = doc["space"]
                space = (int(free), int(capacity))
        if schema >= 6:
            encrypted = doc["encrypted"]
            if encrypted is not None and not isinstance(encrypted, bool):
                raise ContractError("encrypted", f"expected true, false or null, got {encrypted!r}")
        return cls(doc["run_id"], doc["job_id"], doc["job_name"], doc["marker_id"], doc["destination_index"],
                   doc["trigger"], doc["started"], doc["ended"], outcome, tuple(doc["reasons"]), doc["detail"],
                   doc["engine_version"], _counts(doc["planned"], PLANNED_KEYS, "planned"),
                   {**dict.fromkeys(DONE_KEYS, 0), **_counts(doc["done"], done_keys, "done")},
                   _counts(doc["left"], LEFT_KEYS, "left"),
                   doc["versions_batch"], tuple((str(p), str(r)) for p, r in doc["skipped"]), doc["skipped_total"],
                   tuple(doc["errors"]), tuple(doc["listing_seconds"]), float(doc["transfer_seconds"]),
                   source_read, unbacked, cleaned, space, encrypted)

    def file_name(self) -> str:
        stamp = self.started.replace(":", "").replace("-", "")
        return f"{stamp}-{self.marker_id[:8]}-{self.run_id[:8]}.json"

    def describe(self) -> str:
        reasons = f" ({', '.join(self.reasons)})" if self.reasons else ""
        done = self.done
        identical = (f", {done['files_identical']} already identical ({counted(done['bytes_identical'], 'byte')})"
                     if done.get("files_identical") else "")
        text = (f"{self.started}  destination {self.destination_index}  {self.outcome.value}{reasons}: "
                f"{done['files_copied']} copied ({counted(done['bytes_copied'], 'byte')}){identical}, "
                f"{done['moved_to_versions']} into versions, {done['renamed']} renamed")
        if self.skipped_total:
            text += f", {self.skipped_total} skipped"
        repeated = self.repeatedly_unbacked
        if repeated:
            text += f", {len(repeated)} not backed up for {counted(ESCALATE_AFTER, 'run')} or more in a row"
        if self.source_read is not None:
            text += f"; source {self.source_read.describe()}"
        if self.versions_cleaned is not None and (self.versions_cleaned.batch_total or self.versions_cleaned.problems):
            text += f"; {self.versions_cleaned.describe()}"
        if self.low_on_space:
            text += f"; only {self.free_percent:.0f} % free on its drive"
        if self.outcome not in (Outcome.COMPLETED, Outcome.ABSENT, Outcome.STOPPED) and any(self.left.values()):
            left = self.left
            text += (f"; left: {counted(left['files_to_copy'], 'file')} ({counted(left['bytes_to_copy'], 'byte')}) "
                     f"to copy, {counted(left['removals'], 'removal')}, {counted(left['renames'], 'rename')}")
        if self.detail:
            text += f" - {self.detail}"
        return text


def capped(items: Iterable, limit: int = MAX_LISTED) -> Tuple[tuple, int]:
    """The first ``limit`` items and the total."""
    listed, total = [], 0
    for item in items:
        total += 1
        if len(listed) < limit:
            listed.append(item)
    return tuple(listed), total


def counts_for_streaks(record: RunRecord) -> bool:
    """Whether a run reached its copies, so that a file it skipped went unbacked-up: a
    completed run, or one whose removals were held while its additions were copied."""
    return record.outcome is Outcome.COMPLETED or (record.outcome is Outcome.HELD
                                                   and FIRST_RUN_REASON not in record.reasons)


def last_counted(records: Sequence[RunRecord], marker_id: str) -> Optional[RunRecord]:
    """The pair's latest run that reached its copies; runs that did not (an absent drive,
    a stop, a cancel) neither extend a streak nor break it."""
    return next((r for r in reversed(records) if r.marker_id == marker_id and counts_for_streaks(r)), None)


def unbacked_streaks(paths: Iterable[str], previous: Optional[RunRecord]) -> Tuple[Tuple[str, int], ...]:
    """Each path with the runs in a row it has gone unbacked-up: one more than in the
    pair's previous counted run when that run left it too, else one."""
    before = {p.casefold(): n for p, n in previous.not_backed_up} if previous is not None else {}
    return tuple((p, before.get(p.casefold(), 0) + 1) for p in paths)


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def json_text(doc: Any, indent: Optional[int] = None) -> str:
    """``doc`` as JSON text UTF-8 can carry: characters as they are, but an unpaired
    surrogate — which an NTFS name may hold, and which a Python string holds only unpaired
    — escaped as ``\\udc80``, so the text reads back as the same name."""
    text = json.dumps(doc, indent=indent, ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


RECORD_PATIENCE = 10.0
"""Seconds a record is tried again while another program holds its file: a record is
written once, when its pair ends, and is not lost to a moment's refusal."""


def _write_json(path: Path, doc: dict) -> None:
    exchange.write_text(path, json_text(doc, indent=2), durable=True, patience=RECORD_PATIENCE)


def write_destination_copy(destination, folder: str, name: str, doc: dict, *, patience: float) -> None:
    """A record's copy in ``folder`` of a destination's management folder, as the store's
    copy is written (``json_text``), the destination a storage (``storage``) or a folder of
    this computer; OSError as the storage raises it."""
    destination_storage(destination).write_file(f"{MANAGEMENT_DIR}\\{folder}\\{name}",
                                                json_text(doc, indent=2).encode("utf-8"), patience=patience)


def write_record(layout: RuntimeLayout, record: RunRecord, destination_root=None) -> Path:
    """Write the record into the store's records; with ``destination_root`` — a storage
    (``storage``) or a folder of this computer — also into that destination's management
    folder. A destination copy that cannot be written does not lose the record."""
    target = layout.records_dir / record.job_id / record.file_name()
    _write_json(target, record.to_document())
    if destination_root is not None:
        try:
            write_destination_copy(destination_root, RUNS_DIR, record.file_name(), record.to_document(),
                                   patience=RECORD_PATIENCE)
        except OSError:
            pass
    return target


def read_records(layout: RuntimeLayout, job_id: str, *, busy: Optional[List[Path]] = None) -> List[RunRecord]:
    """The job's records, oldest first; an unreadable file is passed over, and one another
    program kept open past the exchange's patience is added to ``busy`` when given, so the
    caller can look again.

    Oldest by start time, which names the files to the second; records of runs that
    started in the same second are ordered by when they were written, which is the order
    the runs happened, a job's runs being one at a time. A file name alone would order them by
    run identifier, at random."""
    folder = layout.records_dir / job_id
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            record = RunRecord.from_document(json.loads(exchange.read_text(path)))
            written = path.stat().st_mtime_ns
        except exchange.FileBusy:
            if busy is not None:
                busy.append(path)
            continue
        except (OSError, ValueError, ContractError, TypeError):
            continue
        found.append((record.started, written, path.name, record))
    return [record for *_key, record in sorted(found, key=lambda item: item[:3])]


def throughputs(records: Sequence[RunRecord], marker_id: str) -> List[float]:
    """This pair's measured copy throughputs, oldest first."""
    return [r.throughput for r in records if r.marker_id == marker_id and r.throughput is not None]


def destination_state(records: Sequence[RunRecord], marker_id: str, marker: Optional[MarkerState] = None,
                      last_full_verification: Optional[str] = None) -> DestinationState:
    """What has happened to one destination, from its run records — and from its marker
    when the destination is present to read it. ``last_full_verification`` comes from the
    destination's verification records (``maintenance.verification``)."""
    mine = [r for r in records if r.marker_id == marker_id]
    if marker is not None:
        confirmed = marker is MarkerState.MATCH
    else:
        confirmed = any(r.outcome in APPLIED and FIRST_RUN_REASON not in r.reasons for r in mine)
    successes = [r.ended for r in mine if r.outcome is Outcome.COMPLETED]
    return DestinationState(first_run_confirmed=confirmed, last_success=max(successes) if successes else None,
                            last_full_verification=last_full_verification)


def first_success(records: Sequence[RunRecord], marker_id: str) -> Optional[str]:
    """When the destination's first completed run ended, or None."""
    successes = [r.ended for r in records if r.marker_id == marker_id and r.outcome is Outcome.COMPLETED]
    return min(successes) if successes else None
