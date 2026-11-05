"""The seam between a run and the operating system's volume snapshots.

A ``SnapshotProvider`` lists the snapshots a volume holds (whoever made them), makes one,
removes one by its identifier, and says how much of the volume's room for snapshots is in
use (``SnapshotRoom``). ``WmiSnapshotProvider`` (``windows``) is the real one;
tests pass their own. ``default_snapshot_provider`` is what a run uses unless told
otherwise: the Windows one, or one that makes nothing when this process may not make
snapshots — on another platform, or when ``MIRRORHOLD_SNAPSHOTS=off`` (the test
suites set it, so no test and no run a test starts touches a real volume's snapshots).

Every failure is a ``SnapshotError`` carrying one of four kinds, which the run records
as the reason it read the source as it is.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import FrozenSet, Optional, Protocol

SWITCH_VARIABLE = "MIRRORHOLD_SNAPSHOTS"
"""``off`` makes this process, and every run it starts, read sources as they are."""


class SnapshotFailure(str, Enum):
    UNAVAILABLE = "snapshot-unavailable"   # no snapshot service for this process
    DENIED = "snapshot-denied"             # the process lacks the rights to make one
    REFUSED = "snapshot-refused"           # the volume or its snapshot provider does not allow one
    FAILED = "snapshot-failed"             # anything else


class SnapshotError(Exception):
    def __init__(self, failure: SnapshotFailure, detail: str):
        self.failure = failure
        self.detail = detail
        super().__init__(f"{failure.value}: {detail}")


@dataclass(frozen=True)
class Snapshot:
    """One volume snapshot. ``device``: its device path, ``\\\\?\\GLOBALROOT\\Device\\...``,
    without a trailing separator; ``volume``: the device path of the volume it copies."""

    shadow_id: str
    device: str
    volume: str

    def path(self, relative: str) -> Path:
        """Where a folder of the volume (relative to its root; "" for the root) is in the
        snapshot. The root is the device path itself: ``pathlib`` keeps no trailing
        separator, so a test that it is a folder adds one (``os.path.join(root, "")``)."""
        return Path(self.device + "\\" + relative) if relative else Path(self.device)


@dataclass(frozen=True)
class SnapshotRoom:
    """The room Windows keeps for a volume's snapshots: the bytes its snapshots use there,
    its limit (None: as much as the volume holding the room has free), and the free space
    and capacity of that volume. A room that would grow past what it can hold makes Windows
    delete the volume's oldest snapshots to make space (its documented behaviour)."""

    used: int
    limit: Optional[int]
    free: int
    capacity: int

    @property
    def spare(self) -> int:
        """The bytes the room can still grow by before Windows deletes a snapshot."""
        if self.limit is None:
            return max(0, self.free)
        return max(0, min(self.limit - self.used, self.free))

    @property
    def size(self) -> int:
        """The room's limit, or the capacity of the volume holding it when it has none."""
        return self.limit if self.limit is not None else self.capacity


class SnapshotProvider(Protocol):
    def shadow_ids(self, volume: str) -> FrozenSet[str]:
        """The identifiers of every snapshot ``volume`` holds, whoever made it."""

    def create(self, volume: str) -> Snapshot:
        """Make a snapshot of ``volume`` that lasts until ``delete``."""

    def delete(self, shadow_id: str) -> bool:
        """Remove that snapshot; False when it no longer exists."""

    def room(self, volume: str) -> Optional[SnapshotRoom]:
        """``volume``'s room for snapshots; None when it has none yet or it cannot be read."""


class UnavailableProvider:
    """A provider for a process that makes no snapshot: every call fails with the reason."""

    def __init__(self, detail: str):
        self.detail = detail

    def shadow_ids(self, volume: str) -> FrozenSet[str]:
        raise SnapshotError(SnapshotFailure.UNAVAILABLE, self.detail)

    def create(self, volume: str) -> Snapshot:
        raise SnapshotError(SnapshotFailure.UNAVAILABLE, self.detail)

    def delete(self, shadow_id: str) -> bool:
        raise SnapshotError(SnapshotFailure.UNAVAILABLE, self.detail)

    def room(self, volume: str) -> Optional[SnapshotRoom]:
        raise SnapshotError(SnapshotFailure.UNAVAILABLE, self.detail)


def default_snapshot_provider() -> SnapshotProvider:
    if os.environ.get(SWITCH_VARIABLE, "").strip().lower() == "off":
        return UnavailableProvider(f"snapshots are switched off for this process ({SWITCH_VARIABLE}=off)")
    if sys.platform != "win32":
        return UnavailableProvider("volume snapshots need Windows")
    from .windows import WmiSnapshotProvider
    return WmiSnapshotProvider()
