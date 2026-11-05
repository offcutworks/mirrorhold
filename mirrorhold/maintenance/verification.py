"""Verification records: what each verification of a destination found, and when.

One record per pass over one destination, in ``<layout>/verifications/<job id>/``,
named by the pass's start so that names sort by time. The verifier rewrites it as the
pass goes (``state`` running, its ``cursor`` the last file handled in key order), so a
pass that stops, fails or dies keeps its progress and the next verification of that
destination continues from the cursor. A finished pass is also copied into the
destination's management folder, beside its run records, so a destination carries its
own history.

A pass is of one of two kinds (``mode``). A **full** pass reads both copies of every file
that is the same size and time on both sides. A pass of **new copies** reads only those no
earlier pass found the same as they are now — kept in a list of verified copies
(``maintenance.verified``), the one the pass names as its ``baseline`` — and counts the
others as unchanged, not read: a routine check after backups, which cannot find a copy
damaged on its drive since it was last read. Only a full pass dates the destination's
verification; a pass of each kind is continued by the next verification of that kind.

A record whose state is still ``running`` while no live verification carries its
identifier belongs to a process that ended without saying so — a shutdown, a crash —
and reads as interrupted (``effective_state``).

What a pass sorts each file into, in the source's and the destination's key order:

- **compared** — on both sides with the same size and modification time: both copies
  read in full and their digests compared; ``differ`` lists those whose contents do
  not match, which no run would ever copy again;
- **changed** — on both sides with another size or time, or changed while it was read:
  the next run copies it, so it is counted and not compared;
- **missing** — at the source only: not backed up yet; **extra** — at the destination
  only: the next run moves it into versions;
- **unreadable** — either side could not be read or listed; not compared.

The destination's state (``last_full_verification``) and the reminder
(``verification_due``) are derived from these records, never stored apart. A finished
full pass dates the destination's verification by when it began, not when it ended: a
pass continued over several sittings read its first files then, and the reminder promises
that no file has gone unchecked longer than its interval. A run that backs up a destination
whose verification is due says so in its notice (``due_after_run``), at every such run
until the destination is verified in full.

A pass's expected time counts its files as well as its bytes (``FILE_SECONDS``): a
destination of many small files is read at a fraction of a large file's rate. The rate a
record measures (``throughput``) is therefore its bytes over its comparing time less the
program's own cost of each file it compared, so that a pass of small files and one of
large files measure the same drive alike.

How a pass compared its files is its ``measure``. On a drive both copies are read and their
digests compared (``Measure.READ``). On a remote a copy is compared without reading it
wherever the service allows: by the fingerprint the service keeps for it against the same
kind computed from the source (``Measure.FINGERPRINT``, the kind in ``fingerprint``), or, on
an encrypted remote, by the engine's check of the encrypted form against the fingerprint
the service beneath keeps (``Measure.ENCRYPTED``); a remote that allows neither is read as a
drive is, the destination's copy by download. Such a comparison confirms that the service
keeps what it recorded of each file as the source holds it; it cannot see damage the service
did not record. A file the service keeps no fingerprint for is read on both sides, and
counted (``read_files``, ``read_bytes``). The rates of passes compared by different measures
differ — a fingerprint pass reads the source alone — so the estimate takes only those of
its own measure (``verification_throughputs``).

Schema 2 added the kind, the baseline and the files left unchanged; schema 3 the measure,
the fingerprint kind and the files read in full in a pass that compared by fingerprint. A
schema 1 record reads as a full pass, and one before schema 3 as read in full.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Collection, List, Optional, Sequence, Tuple

from ..contract_errors import ContractError, require_keys
from ..locations import RuntimeLayout
from ..planning.estimate import MIN_SAMPLE_BYTES
from .. import exchange
from ..records.record import (MAX_LISTED, Outcome, RunRecord, first_success, json_text, read_records,
                              write_destination_copy)
from ..wording import agreeing, counted

VERIFICATION_FORMAT = "mirrorhold-verification"
VERIFICATION_SCHEMA = 3
READABLE_SCHEMAS = (1, 2, 3)
VERIFICATIONS_DIR = "verifications"

FILE_SECONDS = (0.0025, 0.015)
"""What comparing one file costs beside reading its bytes, shortest and longest, in
seconds: 2.5 ms is the program's own cost, measured on the development machine with the
files cached (2.0 to 2.9 ms); 15 ms is assumed, not measured: a USB 3 hard disk's seek (the
maintainer's decision, 2026-10-19)."""


class VerificationMode(str, Enum):
    FULL = "full"                  # both copies of every file read
    NEW_COPIES = "new-copies"      # only the copies no earlier pass found the same as they are now


class Measure(str, Enum):
    """How a pass compared each copy with its source."""

    READ = "read"                  # both copies read in full, their SHA-256 digests compared
    FINGERPRINT = "fingerprint"    # the fingerprint the service keeps, against the source's of that kind
    ENCRYPTED = "encrypted"        # the engine's check of the encrypted form


class VerificationState(str, Enum):
    RUNNING = "running"            # a sitting in progress (or a process that ended without saying so)
    STOPPED = "stopped"            # the user stopped it
    INTERRUPTED = "interrupted"    # its process was interrupted
    FAILED = "failed"              # an unexpected error
    COMPLETED = "completed"        # every file handled


RESUMABLE = (VerificationState.RUNNING, VerificationState.STOPPED, VerificationState.INTERRUPTED,
             VerificationState.FAILED)
"""A pass in one of these states is continued by the next verification of its destination."""


def _utc(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass
class VerificationRecord:
    """One pass over one destination. Mutable: the verifier updates it in place."""

    verification_id: str
    job_id: str
    job_name: str
    marker_id: str
    destination_index: int                 # 1-based, in the job's order
    started: str
    updated: str
    state: VerificationState = VerificationState.RUNNING
    ended: Optional[str] = None            # when the pass completed
    sittings: int = 1                      # processes that worked on it
    cursor: Optional[str] = None           # the key of the last entry handled, in key order
    files_total: int = 0                   # files to compare in the whole pass, as of its latest listing
    bytes_total: int = 0
    compared_files: int = 0
    compared_bytes: int = 0
    differ: List[Tuple[str, int]] = field(default_factory=list)       # (path, size), the first MAX_LISTED
    differ_total: int = 0
    unreadable: List[Tuple[str, str]] = field(default_factory=list)   # (path, why), the first MAX_LISTED
    unreadable_total: int = 0
    changed_total: int = 0
    missing_total: int = 0
    extra_total: int = 0
    seconds: float = 0.0                   # time spent comparing, every sitting together
    listing_seconds: Tuple[float, float] = (0.0, 0.0)   # the latest listing of each side
    detail: str = ""
    mode: VerificationMode = VerificationMode.FULL
    baseline: Optional[str] = None         # new copies: the pass whose list of verified copies it builds on
    unchanged_files: int = 0               # new copies: found the same by an earlier pass, unchanged since, not read
    unchanged_bytes: int = 0
    measure: Measure = Measure.READ
    fingerprint: Optional[str] = None      # the kind compared, when the measure is not READ and the engine named it
    read_files: int = 0                    # a pass not READ: files compared by reading both copies, as no
    read_bytes: int = 0                    # fingerprint was kept for them

    @property
    def finished(self) -> bool:
        return self.state is VerificationState.COMPLETED

    @property
    def full(self) -> bool:
        return self.mode is VerificationMode.FULL

    @property
    def not_compared(self) -> int:
        return self.changed_total + self.missing_total + self.unreadable_total

    @property
    def throughput(self) -> Optional[float]:
        """Bytes compared per second of reading — the comparing time less the program's own
        cost of each file compared — when enough were compared to measure."""
        reading = self.seconds - self.compared_files * FILE_SECONDS[0]
        if self.compared_bytes < MIN_SAMPLE_BYTES or reading <= 0:
            return None
        return self.compared_bytes / reading

    def add_differ(self, path: str, size: int) -> None:
        self.differ_total += 1
        if len(self.differ) < MAX_LISTED:
            self.differ.append((path, size))

    def add_unreadable(self, path: str, why: str) -> None:
        self.unreadable_total += 1
        if len(self.unreadable) < MAX_LISTED:
            self.unreadable.append((path, why))

    def file_name(self) -> str:
        stamp = self.started.replace(":", "").replace("-", "")
        return f"{stamp}-{self.marker_id[:8]}-{self.verification_id[:8]}.json"

    def describe(self, live: Collection[str] = ()) -> str:
        """The command line's line for the pass. ``live``: the activities in progress."""
        what = "verification" if self.full else "verification of new copies"
        text = (f"{self.started}  destination {self.destination_index}  {what} {effective_state(self, live).value}: "
                f"{self.compared_files} of {counted(self.files_total, 'file')} compared ({self.compared_bytes} of "
                f"{counted(self.bytes_total, 'byte')}), {self.differ_total} "
                f"{agreeing(self.differ_total, 'differs', 'differ')}")
        if self.unchanged_files:
            text += f"; {self.unchanged_files} verified earlier and unchanged, not read"
        if self.measure is Measure.FINGERPRINT:
            text += f"; compared by {self.fingerprint} fingerprint"
        elif self.measure is Measure.ENCRYPTED:
            text += f"; compared by the encrypted form ({self.fingerprint or 'its fingerprint'})"
        if self.read_files:
            text += (f"; {counted(self.read_files, 'file')} read in full ({counted(self.read_bytes, 'byte')}): "
                     f"the service kept no fingerprint for {agreeing(self.read_files, 'it', 'them')}")
        if self.not_compared:
            text += (f"; not compared: {self.changed_total} changed, {self.missing_total} not backed up yet, "
                     f"{self.unreadable_total} unreadable")
        if self.sittings > 1:
            text += f"; {counted(self.sittings, 'sitting')}"
        if self.detail:
            text += f" - {self.detail}"
        return text

    def to_document(self) -> dict:
        return {"format": VERIFICATION_FORMAT, "schema_version": VERIFICATION_SCHEMA,
                "verification_id": self.verification_id, "job_id": self.job_id, "job_name": self.job_name,
                "marker_id": self.marker_id, "destination_index": self.destination_index, "started": self.started,
                "updated": self.updated, "state": self.state.value, "ended": self.ended, "sittings": self.sittings,
                "cursor": self.cursor, "files_total": self.files_total, "bytes_total": self.bytes_total,
                "compared_files": self.compared_files, "compared_bytes": self.compared_bytes,
                "differ": [list(d) for d in self.differ], "differ_total": self.differ_total,
                "unreadable": [list(u) for u in self.unreadable], "unreadable_total": self.unreadable_total,
                "changed_total": self.changed_total, "missing_total": self.missing_total,
                "extra_total": self.extra_total, "seconds": self.seconds,
                "listing_seconds": list(self.listing_seconds), "detail": self.detail, "mode": self.mode.value,
                "baseline": self.baseline, "unchanged_files": self.unchanged_files,
                "unchanged_bytes": self.unchanged_bytes, "measure": self.measure.value,
                "fingerprint": self.fingerprint, "read_files": self.read_files, "read_bytes": self.read_bytes}

    @classmethod
    def from_document(cls, doc: Any) -> "VerificationRecord":
        if not isinstance(doc, dict) or doc.get("format") != VERIFICATION_FORMAT \
                or doc.get("schema_version") not in READABLE_SCHEMAS:
            raise ContractError("format", f"not a verification record of schema {READABLE_SCHEMAS}")
        keys = ("format", "schema_version", "verification_id", "job_id", "job_name", "marker_id", "destination_index",
                "started", "updated", "state", "ended", "sittings", "cursor", "files_total", "bytes_total",
                "compared_files", "compared_bytes", "differ", "differ_total", "unreadable", "unreadable_total",
                "changed_total", "missing_total", "extra_total", "seconds", "listing_seconds", "detail")
        if doc["schema_version"] >= 2:
            keys += ("mode", "baseline", "unchanged_files", "unchanged_bytes")
        if doc["schema_version"] >= 3:
            keys += ("measure", "fingerprint", "read_files", "read_bytes")
        require_keys(doc, "", keys)
        try:
            state = VerificationState(doc["state"])
        except ValueError:
            raise ContractError("state", f"unknown state {doc['state']!r}") from None
        try:
            mode = VerificationMode(doc.get("mode", VerificationMode.FULL.value))
        except ValueError:
            raise ContractError("mode", f"unknown kind {doc['mode']!r}") from None
        try:
            measure = Measure(doc.get("measure", Measure.READ.value))
        except ValueError:
            raise ContractError("measure", f"unknown measure {doc['measure']!r}") from None
        fingerprint = doc.get("fingerprint")
        baseline = doc.get("baseline")
        source, destination = doc["listing_seconds"]
        return cls(str(doc["verification_id"]), str(doc["job_id"]), str(doc["job_name"]), str(doc["marker_id"]),
                   int(doc["destination_index"]), str(doc["started"]), str(doc["updated"]), state, doc["ended"],
                   int(doc["sittings"]), doc["cursor"], int(doc["files_total"]), int(doc["bytes_total"]),
                   int(doc["compared_files"]), int(doc["compared_bytes"]),
                   [(str(p), int(s)) for p, s in doc["differ"]], int(doc["differ_total"]),
                   [(str(p), str(w)) for p, w in doc["unreadable"]], int(doc["unreadable_total"]),
                   int(doc["changed_total"]), int(doc["missing_total"]), int(doc["extra_total"]),
                   float(doc["seconds"]), (float(source), float(destination)), str(doc["detail"]), mode,
                   str(baseline) if baseline is not None else None, int(doc.get("unchanged_files", 0)),
                   int(doc.get("unchanged_bytes", 0)), measure,
                   str(fingerprint) if fingerprint is not None else None, int(doc.get("read_files", 0)),
                   int(doc.get("read_bytes", 0)))


def _write_json(path: Path, doc: dict, patience: float) -> None:
    exchange.write_text(path, json_text(doc, indent=2), durable=True, patience=patience)


def write_verification(layout: RuntimeLayout, record: VerificationRecord,
                       destination_root=None, *,
                       patience: float = exchange.WRITE_PATIENCE) -> Path:
    """Write the record into the store's records; with ``destination_root`` — a storage
    (``storage``) or a folder of this computer — also into that destination's management
    folder. A destination copy that cannot be written does not lose the record. The window
    reads the record while the verifier rewrites it; another program holding it is borne
    for ``patience`` seconds, and FileBusy then reaches the caller with the record on disk
    as it was."""
    target = layout.verifications_dir / record.job_id / record.file_name()
    _write_json(target, record.to_document(), patience)
    if destination_root is not None:
        try:
            write_destination_copy(destination_root, VERIFICATIONS_DIR, record.file_name(), record.to_document(),
                                   patience=patience)
        except OSError:
            pass
    return target


def read_verifications(layout: RuntimeLayout, job_id: str, *,
                       busy: Optional[List[Path]] = None) -> List[VerificationRecord]:
    """The job's verification records, oldest first; an unreadable file is passed over —
    one the listing named and that is not there is looked for again for the moment a rename
    takes its name away — and one another program kept open past the exchange's patience
    is added to ``busy`` when given, so the caller can look again."""
    folder = layout.verifications_dir / job_id
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            text = exchange.read_text(path, missing=exchange.RENAME_PATIENCE)     # rewritten as its pass goes on
            record = VerificationRecord.from_document(json.loads(text))
        except exchange.FileBusy:
            if busy is not None:
                busy.append(path)
            continue
        except (OSError, ValueError, ContractError, TypeError):
            continue
        found.append(record)
    return sorted(found, key=lambda r: (r.started, r.verification_id))


def effective_state(record: VerificationRecord, live: Collection[str] = ()) -> VerificationState:
    """The record's state, a running one whose process has ended read as interrupted.
    ``live``: the identifiers of the runs and verifications in progress."""
    if record.state is VerificationState.RUNNING and record.verification_id not in live:
        return VerificationState.INTERRUPTED
    return record.state


def latest_finished(records: Sequence[VerificationRecord], marker_id: str,
                    mode: Optional[VerificationMode] = None) -> Optional[VerificationRecord]:
    """The destination's latest finished pass, of ``mode`` when given, else of either kind."""
    return next((r for r in reversed(records)
                 if r.marker_id == marker_id and r.finished and (mode is None or r.mode is mode)), None)


def resumable(records: Sequence[VerificationRecord], marker_id: str, live: Collection[str] = (),
              mode: VerificationMode = VerificationMode.FULL) -> Optional[VerificationRecord]:
    """The destination's pass a new verification of ``mode`` continues: its latest of that
    kind, when that one did not finish and is not in progress. A pass of new copies that a
    full pass finished after it began is not continued: that one compared everything it
    would."""
    record = next((r for r in reversed(records) if r.marker_id == marker_id and r.mode is mode), None)
    if record is None or record.state not in RESUMABLE or record.verification_id in live:
        return None
    if mode is VerificationMode.NEW_COPIES:
        full = latest_finished(records, marker_id, VerificationMode.FULL)
        if full is not None and (full.ended or full.updated) >= record.started:
            return None
    return record


def unfinished(records: Sequence[VerificationRecord], marker_id: str,
               live: Collection[str] = ()) -> Optional[VerificationRecord]:
    """The destination's pass waiting to be continued that the window names: its unfinished
    full pass, else its unfinished pass of new copies."""
    return (resumable(records, marker_id, live, VerificationMode.FULL)
            or resumable(records, marker_id, live, VerificationMode.NEW_COPIES))


def last_full_verification(records: Sequence[VerificationRecord], marker_id: str) -> Optional[str]:
    """When the destination's latest finished full pass began (UTC), or None: every file it
    compared was read no earlier. A pass of new copies never counts: it leaves unread the
    copies an earlier pass read."""
    finished = latest_finished(records, marker_id, VerificationMode.FULL)
    return finished.started if finished is not None else None


def verification_due(reminder_days: int, marker_id: str, runs: Sequence[RunRecord],
                     verifications: Sequence[VerificationRecord], now: Optional[datetime] = None) -> Optional[datetime]:
    """When the reminder fell due, once it has: ``reminder_days`` after the destination's
    last full verification, or after its first completed run when it was never verified in full.
    None while it is not due, and for a destination no run has completed."""
    since = _utc(last_full_verification(verifications, marker_id)) or _utc(first_success(runs, marker_id))
    if since is None:
        return None
    due = since + timedelta(days=reminder_days)
    return due if (now or datetime.now(timezone.utc)) >= due else None


DUE_AFTER = (Outcome.COMPLETED, Outcome.HELD)
"""A run's outcomes on a destination that make its due verification worth telling at the run's
end: the run backed it up, wholly or its additions."""


def due_after_run(layout: RuntimeLayout, reminder_days: int, records: Sequence[RunRecord],
                  now: Optional[datetime] = None) -> Tuple[Tuple[int, Optional[str]], ...]:
    """The destinations a run's ``records`` backed up whose full verification is due, in the
    run's order: (destination index, when its last finished pass began, or None for one never
    verified). What the run's notice reminds the user of. The job's records are read again,
    this run's included; a record another program holds is passed over."""
    backed_up = [r for r in records if r.outcome in DUE_AFTER]
    if not backed_up:
        return ()
    runs = read_records(layout, backed_up[0].job_id)
    verifications = read_verifications(layout, backed_up[0].job_id)
    return tuple((r.destination_index, last_full_verification(verifications, r.marker_id)) for r in backed_up
                 if verification_due(reminder_days, r.marker_id, runs, verifications, now) is not None)


def verification_throughputs(records: Sequence[VerificationRecord], marker_id: str,
                             measure: Measure = Measure.READ) -> List[float]:
    """This destination's measured comparing throughputs of passes compared by ``measure``,
    oldest first."""
    return [r.throughput for r in records
            if r.marker_id == marker_id and r.measure is measure and r.throughput is not None]
