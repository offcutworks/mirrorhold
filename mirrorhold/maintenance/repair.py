"""Repair records: what each replacement of a destination's differing copies did, and when.

A full verification reports the copies that differ from the source and repairs nothing
(``verification``): it cannot tell which copy is damaged. A repair is the user's answer
when they trust the source's copies (``repairer``): each backup copy the verification
listed moves into the destination's versions, the source's copy is copied in its place,
and both are read back to check they now match.

One record per repair of one destination, in ``<layout>/repairs/<job id>/``, named by the
repair's start so that names sort by time. The repairer writes it before it changes
anything and again when it ends; a finished one — completed, stopped or failed — is also
copied into the destination's management folder, beside its run and verification
records, so a destination carries its own history. A record whose state is still
``running`` while no live repair carries its identifier belongs to a process that ended
without saying so — a shutdown, a crash — and reads as interrupted (``effective_state``).

A record names the verification pass it worked from. What a repair takes up is that
pass's listed copies — the first ``MAX_LISTED`` it found — less those an earlier repair
of the same pass replaced (``pending``). Each copy a repair takes up ends either
**replaced** — read back identical to the source — or **left** with a reason
(``LeftReason``); a copy a stop kept the repair from reaching is in neither list and stays
pending. A copy left because the copy failed, or because its new copy still differed or could
not be read back (``FAILURES``), is a failure; any other was left as it is, the backup copy
untouched. A failure stays pending too, so the next repair takes it up again.

No backup copy a repair moves into its versions batch lies there unnamed, since the job's
version cleanup deletes the batch after its days. Before the first move the record names
the batch and every copy whose backup copy is about to move (``replacing``), and is saved;
each copy leaves that list once it is settled — replaced, recorded as left, or its backup
copy moved back. A repair that ends by itself settles every one: a copy the engine did not
complete whose backup copy could not be moved back is recorded as in versions
(``LeftReason.IN_VERSIONS``, one of ``MOVED``). A process that dies first — a kill, a crash,
a power cut, a second Ctrl+C — leaves the rest named in ``replacing``: once the repair no
longer runs, those are ``unsettled``, their backup copies possibly in the batch, and count
as failures; they stay pending, so the next repair takes them up again.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Collection, List, Optional, Sequence, Tuple

from .. import exchange
from ..contract_errors import ContractError, require_keys
from ..locations import RuntimeLayout
from ..planning.listing import path_key
from ..records.record import json_text, write_destination_copy
from ..wording import agreeing, counted
from .verification import VerificationRecord

REPAIR_FORMAT = "mirrorhold-repair"
REPAIR_SCHEMA = 2
"""Schema 2 added ``replacing``; a schema 1 record reads with it empty."""
READABLE_SCHEMAS = (1, 2)
REPAIRS_DIR = "repairs"
RECORD_PATIENCE = 10.0
"""Seconds a repair record is tried again while another program holds its file."""


class RepairState(str, Enum):
    RUNNING = "running"            # in progress (or a process that ended without saying so)
    STOPPED = "stopped"            # the user stopped it
    INTERRUPTED = "interrupted"    # its process was interrupted
    FAILED = "failed"              # it could not go on: no room, no engine, an unexpected error
    COMPLETED = "completed"        # every copy it took up is replaced or left


class LeftReason(str, Enum):
    """Why a copy a repair took up was not replaced."""

    CHANGED = "changed"                        # its size or time changed since the verification
    MISSING_SOURCE = "missing-source"
    MISSING_DESTINATION = "missing-destination"
    EXCLUDED = "excluded"                      # the job excludes it now
    NOT_THE_JOBS = "not-the-jobs"              # on a shared destination, outside what its job owns
    PROTECTED = "protected"                    # it lies in a folder no run writes into (``run.runner``)
    LINK = "link"                              # it lies beneath a link or junction, or is one
    CLOUD_ONLY = "cloud-only"                  # the source copy is a cloud-only file
    LOST_SIGNATURE = "lost-signature"          # the source copy lost the file-format signature the backup has
    UNREADABLE = "unreadable"                  # a copy could not be read
    NOT_COPIED = "not-copied"                  # the copy failed; the backup copy was moved back
    IN_VERSIONS = "in-versions"                # the copy failed and the backup copy could not be moved back
    STILL_DIFFERS = "still-differs"            # replaced, and the new copy still differs when read back
    NOT_READ_BACK = "not-read-back"            # replaced, and a copy could not be read back


FAILURES = (LeftReason.NOT_COPIED, LeftReason.IN_VERSIONS, LeftReason.STILL_DIFFERS, LeftReason.NOT_READ_BACK)
"""Reasons that are failures of the repair rather than a copy it left as it is."""

MOVED = (LeftReason.IN_VERSIONS, LeftReason.STILL_DIFFERS, LeftReason.NOT_READ_BACK)
"""Reasons whose backup copy is in the repair's versions batch all the same."""

_LEFT_WORDS = {
    LeftReason.CHANGED: "changed since the verification: the next run copies it",
    LeftReason.MISSING_SOURCE: "no longer on the source",
    LeftReason.MISSING_DESTINATION: "no longer on the destination",
    LeftReason.EXCLUDED: "excluded by the job",
    LeftReason.NOT_THE_JOBS: "not this job's to replace on a shared destination",
    LeftReason.PROTECTED: "it lies inside a folder MirrorHold never writes into",
    LeftReason.LINK: "it lies beneath a link or junction",
    LeftReason.CLOUD_ONLY: "its source copy is a cloud-only file",
    LeftReason.LOST_SIGNATURE: "its source copy lost its file-format signature",
    LeftReason.UNREADABLE: "could not be read",
    LeftReason.NOT_COPIED: "could not be copied; its backup copy stays",
    LeftReason.IN_VERSIONS: "could not be copied, and its backup copy could not be moved back out of this repair's "
                            "versions",
    LeftReason.STILL_DIFFERS: "still differs after it was replaced: the destination drive may be failing",
    LeftReason.NOT_READ_BACK: "replaced, but could not be read back",
}

UNSETTLED_WORDS = "the repair ended before it was done with it: its backup copy may be in this repair's versions"
"""Why an unsettled copy was not replaced, in words."""


_REMOTE_LEFT_WORDS = {
    LeftReason.STILL_DIFFERS: "still differs after it was replaced: the service did not keep it as it was sent",
}
"""The words that differ where the destination is a remote's folder."""


def left_words(reason: LeftReason, detail: str = "", *, remote: bool = False) -> str:
    """Why a copy was not replaced, in words: 'could not be read: Access is denied'.
    ``remote``: the destination is a remote's folder."""
    words = (_REMOTE_LEFT_WORDS.get(reason) if remote else None) or _LEFT_WORDS[reason]
    return f"{words}: {detail}" if detail else words


@dataclass
class RepairRecord:
    """One repair of one destination. Mutable: the repairer updates it in place."""

    repair_id: str
    job_id: str
    job_name: str
    marker_id: str
    destination_index: int                 # 1-based, in the job's order
    verification_id: str                   # the pass whose listed copies it took up
    verification_started: str              # when that pass began
    started: str
    updated: str
    state: RepairState = RepairState.RUNNING
    ended: Optional[str] = None            # when it ended, whatever its end
    taken: int = 0                         # copies it took up
    differ_total: int = 0                  # copies the pass found to differ, listed or not
    replaced: List[Tuple[str, int]] = field(default_factory=list)          # (path, size), read back identical
    left: List[Tuple[str, LeftReason, str]] = field(default_factory=list)  # (path, reason, detail)
    bytes_copied: int = 0
    versions_batch: Optional[str] = None   # relative to the destination
    seconds: float = 0.0
    detail: str = ""                       # why it stopped or failed
    replacing: List[Tuple[str, int]] = field(default_factory=list)  # (path, size), named before its move, not settled

    @property
    def failures(self) -> int:
        return sum(1 for _path, reason, _detail in self.left if reason in FAILURES)

    @property
    def left_as_is(self) -> int:
        """Copies it left as they are, the backup copy untouched: those left but not failed."""
        return len(self.left) - self.failures

    @property
    def into_versions(self) -> int:
        """Backup copies it moved into versions and did not move back: those replaced and read
        back identical, those whose new copy still differs or could not be read back, and
        those it could not move back; and those named in ``replacing``, in versions while the
        repair works on them and possibly there still once it has ended."""
        return (len(self.replaced) + sum(1 for _path, reason, _detail in self.left if reason in MOVED)
                + len(self.replacing))

    def file_name(self) -> str:
        stamp = self.started.replace(":", "").replace("-", "")
        return f"{stamp}-{self.marker_id[:8]}-{self.repair_id[:8]}.json"

    def describe(self, live: Collection[str] = ()) -> str:
        """The command line's line for the repair. ``live``: the activities in progress."""
        text = (f"{self.started}  destination {self.destination_index}  repair {effective_state(self, live).value}: "
                f"{len(self.replaced)} of {counted(self.taken, 'copy', 'copies')} replaced")
        if self.failures:
            text += f", {self.failures} failed"
        if self.left_as_is:
            text += f", {self.left_as_is} left"
        stranded = len(unsettled(self, live))
        if stranded:
            where = self.versions_batch or "this repair's versions"
            text += (f", {stranded} not finished: {agreeing(stranded, 'its backup copy', 'their backup copies')} "
                     f"may be in {where}")
        if self.detail:
            text += f" - {self.detail}"
        return text

    def to_document(self) -> dict:
        return {"format": REPAIR_FORMAT, "schema_version": REPAIR_SCHEMA, "repair_id": self.repair_id,
                "job_id": self.job_id, "job_name": self.job_name, "marker_id": self.marker_id,
                "destination_index": self.destination_index, "verification_id": self.verification_id,
                "verification_started": self.verification_started, "started": self.started,
                "updated": self.updated, "state": self.state.value, "ended": self.ended, "taken": self.taken,
                "differ_total": self.differ_total, "replaced": [list(r) for r in self.replaced],
                "left": [[path, reason.value, detail] for path, reason, detail in self.left],
                "bytes_copied": self.bytes_copied, "versions_batch": self.versions_batch,
                "seconds": self.seconds, "detail": self.detail, "replacing": [list(r) for r in self.replacing]}

    @classmethod
    def from_document(cls, doc: Any) -> "RepairRecord":
        schema = doc.get("schema_version") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or doc.get("format") != REPAIR_FORMAT or schema not in READABLE_SCHEMAS:
            raise ContractError("format", f"not a repair record of schema {', '.join(map(str, READABLE_SCHEMAS))}")
        require_keys(doc, "", ("format", "schema_version", "repair_id", "job_id", "job_name", "marker_id",
                               "destination_index", "verification_id", "verification_started", "started",
                               "updated", "state", "ended", "taken", "differ_total", "replaced", "left",
                               "bytes_copied", "versions_batch", "seconds", "detail")
                     + (("replacing",) if schema >= 2 else ()))
        try:
            state = RepairState(doc["state"])
            left = [(str(p), LeftReason(r), str(d)) for p, r, d in doc["left"]]
        except ValueError as exc:
            raise ContractError("state", str(exc)) from None
        replacing = [(str(p), int(s)) for p, s in doc["replacing"]] if schema >= 2 else []
        return cls(str(doc["repair_id"]), str(doc["job_id"]), str(doc["job_name"]), str(doc["marker_id"]),
                   int(doc["destination_index"]), str(doc["verification_id"]), str(doc["verification_started"]),
                   str(doc["started"]), str(doc["updated"]), state, doc["ended"], int(doc["taken"]),
                   int(doc["differ_total"]), [(str(p), int(s)) for p, s in doc["replaced"]], left,
                   int(doc["bytes_copied"]), doc["versions_batch"], float(doc["seconds"]), str(doc["detail"]),
                   replacing)


def _write_json(path: Path, doc: dict) -> None:
    exchange.write_text(path, json_text(doc, indent=2), durable=True, patience=RECORD_PATIENCE)


def write_repair(layout: RuntimeLayout, record: RepairRecord, destination_root=None) -> Path:
    """Write the record into the store's records; with ``destination_root`` — a storage
    (``storage``) or a folder of this computer — also into that destination's management
    folder. A destination copy that cannot be written does not lose the record."""
    target = layout.repairs_dir / record.job_id / record.file_name()
    _write_json(target, record.to_document())
    if destination_root is not None:
        try:
            write_destination_copy(destination_root, REPAIRS_DIR, record.file_name(), record.to_document(),
                                   patience=RECORD_PATIENCE)
        except OSError:
            pass
    return target


def read_repairs(layout: RuntimeLayout, job_id: str, *, busy: Optional[List[Path]] = None) -> List[RepairRecord]:
    """The job's repair records, oldest first; an unreadable file is passed over — one the
    listing named and that is not there is looked for again for the moment a rename takes
    its name away — and one another program kept open past the exchange's patience is added
    to ``busy`` when given, so the caller can look again."""
    folder = layout.repairs_dir / job_id
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            text = exchange.read_text(path, missing=exchange.RENAME_PATIENCE)     # rewritten as the repair works
            record = RepairRecord.from_document(json.loads(text))
        except exchange.FileBusy:
            if busy is not None:
                busy.append(path)
            continue
        except (OSError, ValueError, ContractError, TypeError):
            continue
        found.append(record)
    return sorted(found, key=lambda r: (r.started, r.repair_id))


def effective_state(record: RepairRecord, live: Collection[str] = ()) -> RepairState:
    """The record's state, a running one whose process has ended read as interrupted.
    ``live``: the identifiers of the activities in progress."""
    if record.state is RepairState.RUNNING and record.repair_id not in live:
        return RepairState.INTERRUPTED
    return record.state


def unsettled(record: RepairRecord, live: Collection[str] = ()) -> List[Tuple[str, int]]:
    """The copies a repair named before moving their backup copies into its versions batch
    and never settled, once it no longer runs — its process died first: each backup copy may
    lie in the batch, and the copy is a failure of the repair. None while it runs, when they
    are its work in hand. ``live``: the activities in progress."""
    return [] if effective_state(record, live) is RepairState.RUNNING else list(record.replacing)


def of_pass(repairs: Sequence[RepairRecord], verification_id: str) -> List[RepairRecord]:
    """The repairs that took up the copies of one verification pass, oldest first."""
    return [r for r in repairs if r.verification_id == verification_id]


def replaced_keys(repairs: Sequence[RepairRecord], verification_id: str) -> frozenset:
    """The copies of one pass a repair replaced, by comparison key."""
    return frozenset(path_key(path) for r in of_pass(repairs, verification_id) for path, _size in r.replaced)


def pending(verification: VerificationRecord, repairs: Sequence[RepairRecord]) -> Tuple[Tuple[str, int], ...]:
    """The copies a pass listed as differing that no repair of it replaced: what a repair
    of the destination takes up, in the pass's order."""
    done = replaced_keys(repairs, verification.verification_id)
    return tuple((path, size) for path, size in verification.differ if path_key(path) not in done)


def all_replaced(verification: VerificationRecord, repairs: Sequence[RepairRecord]) -> bool:
    """Whether every copy the pass found to differ was replaced: it listed them all and a
    repair replaced each one."""
    return (bool(verification.differ_total) and verification.differ_total == len(verification.differ)
            and not pending(verification, repairs))
