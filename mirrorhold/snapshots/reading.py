"""How a run reads its source: from a snapshot of the source's volume, or as it is.

``SourceSnapshot.take`` decides, once per run, before the source is listed:

1. the job's ``read_from_snapshot`` is off — as it is (``snapshot-off``);
2. no selected destination is present, so nothing will be copied — as it is
   (``snapshot-not-needed``);
3. the volume's file system cannot be snapshotted (FAT, exFAT, a cloud drive) — as it
   is (``snapshot-unsupported-drive``);
4. the volume already holds snapshots this tool did not make — Windows' restore points,
   a program's backups, previous versions — and its room for snapshots has too little to
   spare (``room_to_spare``), or cannot be read — as it is (``snapshot-would-displace``):
   when that room fills, Windows deletes the oldest snapshots to make space, and a
   snapshot this tool made would cost the user one of theirs (the maintainer's decisions
   of 2026-10-06 and 2026-10-17: never at that cost, so only with room to spare). What
   the snapshot's own share of the room grows by while the run reads is not known before
   it is made — the system's writes to the volume, and a destination on it — so the
   spare is a margin, not a guarantee;
5. making the snapshot fails — as it is, with the failure's kind
   (``snapshot-unavailable``, ``snapshot-denied``, ``snapshot-refused``,
   ``snapshot-failed``);
6. otherwise the run reads the source's folder inside the snapshot.

Steps 1 to 3 need nothing from Windows (``settled_without_asking``), so a preview says
from them alone whether the run reads as it is or asks for a snapshot.

A snapshot is never a precondition of a run: every refusal and failure reads the source
as it is and says why in the run's records. The snapshot lasts the whole run —
every destination, a pause included — and ``release`` removes it when the run ends,
whatever its outcome. ``remove_leftovers``, called by every run as it starts, removes a
snapshot a run that died left behind, by its recorded identifier only; a run still in
progress keeps its own.

Runs work at the same time, so steps 4 to 6 — looking at the volume's snapshots, asking
for one and recording it — and every removal hold the snapshot lock
(``RuntimeLayout.snapshot_lock``): Windows makes one snapshot set at a time, and a
snapshot another run is making, not yet recorded, would otherwise read as one made by
another program.
"""
from __future__ import annotations

import os
from typing import List, Optional

from ..endpoints.volumes import VolumeInfo
from ..jobs.model import Job
from ..locations import RuntimeLayout
from ..records.lock import holding
from ..records.record import ReadMode, SourceRead, utc_now
from ..records.status import is_live
from ..wording import counted
from . import own
from .provider import Snapshot, SnapshotError, SnapshotFailure, SnapshotProvider, SnapshotRoom

SNAPSHOT_OFF = "snapshot-off"
NOT_NEEDED = "snapshot-not-needed"
UNSUPPORTED_DRIVE = "snapshot-unsupported-drive"
WOULD_DISPLACE = "snapshot-would-displace"

GiB = 1024 ** 3
SPARE_FLOOR = 4 * GiB
"""The least a volume's room for snapshots must have to spare before a snapshot is made
beside snapshots someone else made."""
SPARE_SHARE = 0.10
"""The share of that room's size it must have to spare, where that is more than the floor."""


def spare_needed(room: SnapshotRoom) -> int:
    """What the room must have to spare for a snapshot beside others' snapshots."""
    return max(SPARE_FLOOR, int(SPARE_SHARE * room.size))


def room_to_spare(room: Optional[SnapshotRoom]) -> bool:
    """Whether a snapshot can be made beside others' snapshots without Windows deleting
    one to make space; never when the room cannot be read."""
    return room is not None and room.spare >= spare_needed(room)


def _displacing(others: int, room: Optional[SnapshotRoom]) -> str:
    """Why the run reads as it is beside others' snapshots, as its record says it."""
    made = f"{counted(others, 'snapshot')} made by Windows or another program"
    if room is None:
        return f"{made}; how much of the drive's room for snapshots is free could not be read"
    return (f"{made}; {room.spare / GiB:.1f} GiB of the drive's room for snapshots is free, "
            f"{spare_needed(room) / GiB:.1f} GiB is wanted")


def drive_name(volume: VolumeInfo) -> str:
    """A volume as the user knows it: its letter, else its label, else "its drive"."""
    if volume.mount_points:
        return volume.mount_points[0].rstrip("\\")
    return f'"{volume.label}"' if volume.label else "its drive"


def settled_without_asking(job: Job, volume: VolumeInfo, *, needed: bool = True) -> Optional[SourceRead]:
    """Steps 1 to 3: the source read as it is, with why, when the job's choice, a run with
    nothing to copy or the drive's kind settles it; None when a run asks Windows for a
    snapshot. A preview says what a run will do from this, asking Windows nothing."""
    drive = drive_name(volume)
    if not job.source.read_from_snapshot:
        return SourceRead(ReadMode.LIVE, drive, SNAPSHOT_OFF, "")
    if not needed:
        return SourceRead(ReadMode.LIVE, drive, NOT_NEEDED, "")
    if not volume.snapshot_eligible:
        return SourceRead(ReadMode.LIVE, drive, UNSUPPORTED_DRIVE, f"a {volume.file_system or 'virtual'} drive")
    return None


class SourceSnapshot:
    """The snapshot one run reads its source from, if it makes one."""

    def __init__(self, layout: RuntimeLayout, run_id: str, provider: SnapshotProvider):
        self.layout = layout
        self.run_id = run_id
        self.provider = provider
        self.snapshot: Optional[Snapshot] = None
        self.drive = ""

    def take(self, job: Job, volume: VolumeInfo, *, needed: bool = True) -> SourceRead:
        """Decide how the source is read and, when it can be, make the snapshot."""
        drive = self.drive = drive_name(volume)

        def live(reason: str, detail: str = "") -> SourceRead:
            return SourceRead(ReadMode.LIVE, drive, reason, detail)

        settled = settled_without_asking(job, volume, needed=needed)
        if settled is not None:
            return settled
        try:
            with holding(self.layout.snapshot_lock):
                mine = {o.shadow_id for o in own.recorded(self.layout) if o.shadow_id}
                others = self.provider.shadow_ids(volume.device) - mine
                if others:
                    room = self._room(volume.device)
                    if not room_to_spare(room):
                        return live(WOULD_DISPLACE, _displacing(len(others), room))
                requested = own.record_request(self.layout, self.run_id, volume.device, drive)
                try:
                    snapshot = self.provider.create(volume.device)
                except SnapshotError:
                    own.forget(self.layout, self.run_id)     # none was made; any other end keeps the request
                    raise
                try:
                    own.record_snapshot(self.layout, requested, snapshot)
                except OSError as exc:
                    self._remove(snapshot)
                    return live(SnapshotFailure.FAILED.value, f"the snapshot could not be recorded: {exc}")
                self.snapshot = snapshot
        except SnapshotError as exc:
            return live(exc.failure.value, exc.detail)
        except OSError as exc:
            return live(SnapshotFailure.FAILED.value, f"the snapshot could not be recorded: {exc}")
        if not os.path.isdir(os.path.join(snapshot.path(""), "")):
            self.release()
            return live(SnapshotFailure.FAILED.value, "the snapshot was made but cannot be read")
        return SourceRead(ReadMode.SNAPSHOT, drive, taken=utc_now())

    def _room(self, device: str) -> Optional[SnapshotRoom]:
        """The volume's room for snapshots; None when the provider cannot read it — a
        provider without ``room`` included — which reads the source as it is."""
        asking = getattr(self.provider, "room", None)
        if asking is None:
            return None
        try:
            return asking(device)
        except SnapshotError:
            return None

    def root(self, relative: str):
        """The source folder inside the snapshot; None when the run reads as it is."""
        return self.snapshot.path(relative) if self.snapshot is not None else None

    def _remove(self, snapshot: Snapshot) -> str:
        """Remove ``snapshot`` and its record; the caller holds the snapshot lock."""
        try:
            self.provider.delete(snapshot.shadow_id)
        except SnapshotError as exc:
            return f"the snapshot of {self.drive} could not be removed: {exc.detail}; the next run removes it"
        own.forget(self.layout, self.run_id)
        return ""

    def release(self) -> str:
        """Remove this run's snapshot. Returns why it could not be, or ""; its record then
        stays, so the next run removes it."""
        snapshot, self.snapshot = self.snapshot, None
        if snapshot is None:
            return ""
        with holding(self.layout.snapshot_lock):
            return self._remove(snapshot)


def remove_leftovers(layout: RuntimeLayout, provider: SnapshotProvider, *, current_run: str = "") -> List[str]:
    """Remove every snapshot a run recorded and left behind when it died, under the snapshot
    lock; a run still in progress — ``current_run`` among them — keeps its own, recorded or
    still being made. Returns what to tell the user."""
    with holding(layout.snapshot_lock):
        return _remove_leftovers(layout, provider, current_run)


def _remove_leftovers(layout: RuntimeLayout, provider: SnapshotProvider, current_run: str) -> List[str]:
    notes: List[str] = []
    for left in own.recorded(layout):
        if left.run_id == current_run or is_live(layout, left.run_id):
            continue
        where = left.drive or "a drive"
        if not left.shadow_id:
            notes.append(f"an earlier run ended while it was making a snapshot of {where}; a snapshot it may have "
                         "made cannot be told from the drive's others and was left")
            own.forget(layout, left.run_id)
            continue
        try:
            removed = provider.delete(left.shadow_id)
        except SnapshotError as exc:
            notes.append(f"a snapshot of {where} left by an earlier run could not be removed: {exc.detail}")
            continue
        own.forget(layout, left.run_id)
        if removed:
            notes.append(f"removed a snapshot of {where} that an earlier run left behind")
    return notes


__all__ = ["NOT_NEEDED", "SNAPSHOT_OFF", "SPARE_FLOOR", "SPARE_SHARE", "SourceSnapshot", "UNSUPPORTED_DRIVE",
           "WOULD_DISPLACE", "drive_name", "remove_leftovers", "room_to_spare", "settled_without_asking",
           "spare_needed"]
