"""The management folder and the marker that proves a destination belongs to a job.

Each destination holds one hidden management folder at its root; it is the tool's,
it is excluded from mirroring on both sides, and the marker inside it names the job
and the destination's marker identity. A destination resolves only when its volume
is present AND its marker matches, so a drive that merely carries the right volume
identity — a clone — or a folder someone pointed at by mistake is never written to.

The marker is written when a destination's first run is confirmed; until then a
destination reports MISSING.

The marker names its schema. One without a schema version predates the field and reads
as schema 1; a schema this copy does not know reads as UNREADABLE, so a copy of the tool
older than the marker stops at that destination rather than guess what it says.

Schema 2 adds ``content``: ``shared`` for a destination whose first run left what it
already held where it is, so that its job owns only part of it (``endpoints.shared``
records which). A marker is written in the lowest schema that says what it must: a
destination that is its job's whole keeps schema 1, read alike by every copy of the
tool, and a shared one takes schema 2, which a copy that does not know the record stops
at — it would otherwise move what it must leave into versions.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Tuple

from ..contract_errors import ContractError, require_keys
from ..storage.destination import MANAGEMENT_DIR, raise_if_unanswered
from ..storage.drive import destination_storage

MARKER_FILE = "marker.json"
MARKER_RELATIVE = f"{MANAGEMENT_DIR}\\{MARKER_FILE}"
MARKER_FORMAT = "mirrorhold-marker"
MARKER_SCHEMA = 1
SHARED_MARKER_SCHEMA = 2
READABLE_SCHEMAS = (1, 2)
CONTENT_WHOLE, CONTENT_SHARED = "whole", "shared"


class MarkerError(Exception):
    """A marker file exists but cannot be read as one."""


class MarkerState(str, Enum):
    MATCH = "match"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Marker:
    job_id: str
    marker_id: str
    shared: bool = False             # the job owns part of the destination (``endpoints.shared``)

    def to_document(self) -> dict:
        if self.shared:
            return {"format": MARKER_FORMAT, "schema_version": SHARED_MARKER_SCHEMA, "job_id": self.job_id,
                    "marker_id": self.marker_id, "content": CONTENT_SHARED}
        return {"format": MARKER_FORMAT, "schema_version": MARKER_SCHEMA, "job_id": self.job_id,
                "marker_id": self.marker_id}

    @classmethod
    def from_document(cls, doc) -> "Marker":
        if not isinstance(doc, Mapping):
            raise ContractError("", "expected an object")
        versioned = "schema_version" in doc
        schema = doc["schema_version"] if versioned else 1
        if type(schema) is not int or schema not in READABLE_SCHEMAS:
            raise ContractError("schema_version", f"not a marker of schema {', '.join(map(str, READABLE_SCHEMAS))}")
        require_keys(doc, "", ("format",) + (("schema_version",) if versioned else ()) + ("job_id", "marker_id")
                     + (("content",) if schema >= SHARED_MARKER_SCHEMA else ()))
        if doc["format"] != MARKER_FORMAT:
            raise ContractError("format", f"not a marker: {doc['format']!r}")
        if not isinstance(doc["job_id"], str) or not isinstance(doc["marker_id"], str):
            raise ContractError("", "job_id and marker_id must be strings")
        shared = False
        if schema >= SHARED_MARKER_SCHEMA:
            if doc["content"] not in (CONTENT_WHOLE, CONTENT_SHARED):
                raise ContractError("content",
                                    f"expected {CONTENT_WHOLE!r} or {CONTENT_SHARED!r}, got {doc['content']!r}")
            shared = doc["content"] == CONTENT_SHARED
        return cls(doc["job_id"], doc["marker_id"], shared)


def management_dir(destination: Path) -> Path:
    """A drive destination's management folder, as a path of this computer."""
    return Path(destination) / MANAGEMENT_DIR


def marker_path(destination: Path) -> Path:
    """A drive destination's marker, as a path of this computer."""
    return management_dir(destination) / MARKER_FILE


def read_marker(destination) -> Optional[Marker]:
    """The marker of ``destination`` — a storage (``storage``), or a folder of this
    computer — or None when it has none; ``MarkerError`` when it cannot be read as one.

    A remote that stops answering is never told as its marker: its ``StorageError`` is raised
    (``storage.destination.raise_if_unanswered``), and only a marker the service will not
    serve while it answers is one that cannot be read."""
    storage = destination_storage(destination)
    where = storage.describe(MARKER_RELATIVE)
    try:
        data = storage.read_file(MARKER_RELATIVE)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise_if_unanswered(storage, MARKER_RELATIVE, exc)
        raise MarkerError(f"{where}: {exc.strerror or exc}") from None
    try:
        return Marker.from_document(json.loads(data.decode("utf-8")))
    except (ValueError, ContractError) as exc:     # bytes that are not UTF-8 included: a ValueError
        raise MarkerError(f"{where}: {exc}") from None


def check_marker(destination, job_id: str, marker_id: str) -> MarkerState:
    return marker_check(destination, job_id, marker_id)[0]


def marker_check(destination, job_id: str, marker_id: str) -> Tuple[MarkerState, Optional[Marker]]:
    """The state of a job's destination's marker, and the marker itself when it matches.
    ``StorageError`` when a remote stopped answering as its marker was read (``read_marker``):
    the marker's state is then not known."""
    try:
        marker = read_marker(destination)
    except MarkerError:
        return MarkerState.UNREADABLE, None
    if marker is None:
        return MarkerState.MISSING, None
    if marker.job_id == job_id and marker.marker_id == marker_id:
        return MarkerState.MATCH, marker
    return MarkerState.MISMATCH, None


def write_marker(destination, marker: Marker):
    """Make the management folder as the destination keeps it (hidden on a drive) if
    needed, and write the marker whole. Returns where it was written, as the engine is
    handed the place (``storage.DestinationStorage.locate``)."""
    storage = destination_storage(destination)
    storage.make_management_folder()
    text = json.dumps(marker.to_document(), indent=2).replace("\n", os.linesep)   # the system's line endings
    storage.write_file(MARKER_RELATIVE, text.encode("utf-8"))
    return storage.locate(MARKER_RELATIVE)
