"""The snapshots this tool made, one file per run in ``<layout>/snapshots/``.

A snapshot lasts until it is removed, so a run that ends without removing its own — a
crash, a shutdown, the process killed — leaves it on the volume. The next run removes
it, and only it, by the identifier recorded here; a snapshot nobody recorded is never
removed (a design rule: the tool removes no snapshot it did not make).

The file is written twice: before the snapshot is asked for, naming the volume, and
once it exists, adding its identifier. A file with no identifier belongs to a run that
ended while the snapshot was being made; whether one was made cannot be told from the
volume's other snapshots, so the next run reports it and removes nothing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..locations import RuntimeLayout
from ..records.lock import remove_quietly
from ..records.record import utc_now
from .provider import Snapshot

OWN_FORMAT = "mirrorhold-snapshot"


@dataclass(frozen=True)
class OwnSnapshot:
    run_id: str
    volume: str                      # the volume's device path
    drive: str                       # the volume as the user knows it, for a report
    requested: str                   # UTC
    shadow_id: Optional[str] = None  # None: the run ended before the snapshot existed, or while it was made
    device: Optional[str] = None

    def to_document(self) -> dict:
        return {"format": OWN_FORMAT, "run_id": self.run_id, "volume": self.volume, "drive": self.drive,
                "requested": self.requested, "shadow_id": self.shadow_id, "device": self.device}


def _path(layout: RuntimeLayout, run_id: str) -> Path:
    return layout.snapshots_dir / f"{run_id}.json"


def _write(layout: RuntimeLayout, own: OwnSnapshot) -> None:
    path = _path(layout, own.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".writing")
    with open(staging, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(own.to_document(), handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)


def record_request(layout: RuntimeLayout, run_id: str, volume: str, drive: str) -> OwnSnapshot:
    own = OwnSnapshot(run_id, volume, drive, utc_now())
    _write(layout, own)
    return own


def record_snapshot(layout: RuntimeLayout, requested: OwnSnapshot, snapshot: Snapshot) -> OwnSnapshot:
    own = OwnSnapshot(requested.run_id, requested.volume, requested.drive, requested.requested,
                      snapshot.shadow_id, snapshot.device)
    _write(layout, own)
    return own


def forget(layout: RuntimeLayout, run_id: str) -> None:
    remove_quietly(_path(layout, run_id))


def recorded(layout: RuntimeLayout) -> List[OwnSnapshot]:
    """Every recorded snapshot, oldest request first; an unreadable file is passed over."""
    folder = layout.snapshots_dir
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("format") != OWN_FORMAT:
                continue
            found.append(OwnSnapshot(str(doc["run_id"]), str(doc["volume"]), str(doc.get("drive") or ""),
                                     str(doc["requested"]), doc.get("shadow_id"), doc.get("device")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return sorted(found, key=lambda own: own.requested)
