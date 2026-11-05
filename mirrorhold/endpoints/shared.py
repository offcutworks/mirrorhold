"""What a shared destination's job owns there, and what it leaves where it is.

A destination is **shared** when its first run left what it already held where it is —
the user's choice, or the only one offered (``run.runner``). Its job then owns only the
top-level entries it put there:

- ``own``: their names, as spelled;
- ``left``: beneath them, what was there before the job and the source does not hold —
  the top-most such entries, as paths relative to the destination, spelled as it
  spells them.

Everything at the destination's top level outside ``own`` is not the job's, whenever it
appeared. ``planning.sharing`` derives what a run covers and how the record changes; the
run writes the record before it applies anything, and a full verification only reads it.

The record lives with the destination, beside the marker in the management folder,
because it describes that destination's content and must outlive a lost or re-imported
job file. The marker of a shared destination says so (``marker.Marker.shared``, a
schema a copy of the tool that does not know this record stops at), and a shared
destination whose record cannot be used is never written to as it stands. A record that
is **lost** (``SharedLost``) — missing, not a record this copy can read, or naming another
job or destination — says nothing any more about what is the job's; its run makes it
again as a first run that leaves what is there, once the user has reviewed that
(``run.runner``). One that cannot be read now — another program holds it, or a remote
answering will not serve it — is intact: its run stops untouched and the next one reads it
again. A remote that stops answering as the record is read is never told as the record:
the run fails the pair, and a verification or a repair ends, as a remote that stopped
answering.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

from ..contract_errors import ContractError, require_keys
from ..storage.destination import MANAGEMENT_DIR, raise_if_unanswered
from ..storage.drive import destination_storage
from .marker import management_dir

SHARED_FILE = "shared.json"
SHARED_RELATIVE = f"{MANAGEMENT_DIR}\\{SHARED_FILE}"
SHARED_FORMAT = "mirrorhold-shared"
SHARED_SCHEMA = 1


class SharedError(Exception):
    """A shared destination's record that cannot be used: lost (``SharedLost``), or not
    readable now."""


class SharedLost(SharedError):
    """A shared destination's record that is missing, is not a record this copy can read,
    or belongs to another job or destination: what it said can no longer be known."""


def _names(value: Any, where: str, *, paths: bool) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(where, "expected a list")
    seen = set()
    for i, item in enumerate(value):
        parts = item.split("\\") if isinstance(item, str) else None
        if not parts or not all(parts) or (len(parts) > 1 and not paths):
            raise ContractError(f"{where}[{i}]", f"not a {'relative path' if paths else 'name'}: {item!r}")
        if item.casefold() in seen:
            raise ContractError(f"{where}[{i}]", f"listed twice: {item!r}")
        seen.add(item.casefold())
    return tuple(value)


@dataclass(frozen=True)
class SharedRecord:
    job_id: str
    marker_id: str
    own: Tuple[str, ...] = ()
    left: Tuple[str, ...] = ()

    def to_document(self) -> dict:
        return {"format": SHARED_FORMAT, "schema_version": SHARED_SCHEMA, "job_id": self.job_id,
                "marker_id": self.marker_id, "own": list(self.own), "left": list(self.left)}

    @classmethod
    def from_document(cls, doc: Any) -> "SharedRecord":
        require_keys(doc, "", ("format", "schema_version", "job_id", "marker_id", "own", "left"))
        if doc["format"] != SHARED_FORMAT:
            raise ContractError("format", f"not a shared destination's record: {doc['format']!r}")
        if type(doc["schema_version"]) is not int or doc["schema_version"] != SHARED_SCHEMA:
            raise ContractError("schema_version", f"not a record of schema {SHARED_SCHEMA}")
        if not isinstance(doc["job_id"], str) or not isinstance(doc["marker_id"], str):
            raise ContractError("", "job_id and marker_id must be strings")
        own = _names(doc["own"], "own", paths=False)
        left = _names(doc["left"], "left", paths=True)
        owned = {name.casefold() for name in own}
        for i, path in enumerate(left):
            if path.split("\\", 1)[0].casefold() not in owned:
                raise ContractError(f"left[{i}]", f"not beneath a name the job owns: {path!r}")
        return cls(doc["job_id"], doc["marker_id"], own, left)


def shared_path(destination: Path) -> Path:
    """A drive destination's record, as a path of this computer."""
    return management_dir(destination) / SHARED_FILE


def read_shared(destination, job_id: str, marker_id: str) -> SharedRecord:
    """The record of a shared destination of this job — ``destination`` a storage
    (``storage``) or a folder of this computer. SharedLost when it is missing, is not such
    a record, or belongs to another job or destination; SharedError when it cannot be read
    now; a remote's ``StorageError`` when the remote stopped answering as it was read
    (``storage.destination.raise_if_unanswered``), what the record says then not known."""
    storage = destination_storage(destination)
    path = storage.describe(SHARED_RELATIVE)
    try:
        data = storage.read_file(SHARED_RELATIVE)
    except FileNotFoundError:
        raise SharedLost(f"{path} is missing") from None
    except OSError as exc:
        raise_if_unanswered(storage, SHARED_RELATIVE, exc)
        raise SharedError(f"{path}: {exc.strerror or exc}") from None
    try:
        record = SharedRecord.from_document(json.loads(data.decode("utf-8")))
    except (ValueError, ContractError) as exc:
        raise SharedLost(f"{path}: {exc}") from None
    if record.job_id != job_id or record.marker_id != marker_id:
        raise SharedLost(f"{path} belongs to another job or destination")
    return record


def write_shared(destination, record: SharedRecord):
    """Write the record whole, the management folder made as the destination keeps it
    (hidden on a drive) if needed. Names are written escaped, so a name NTFS holds and UTF-8
    cannot (an unpaired surrogate) reads back unchanged. Returns where it was written, as
    the engine is handed the place."""
    storage = destination_storage(destination)
    storage.make_management_folder()
    storage.write_file(SHARED_RELATIVE, json.dumps(record.to_document(), indent=2).encode("utf-8"))
    return storage.locate(SHARED_RELATIVE)
