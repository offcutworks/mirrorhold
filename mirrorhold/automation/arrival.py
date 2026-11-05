"""Which of a job's drives just arrived.

An arrival task starts the core on every volume arrival or removal and at logon, for
any drive, so the core decides what arrived for this job: it keeps, per job, which of
the job's drives (its source, each destination by marker identity) were present when
it last looked, and in which session of the computer (its boot time). A drive present
now that was not present then has arrived; at the first look of a new session every
present drive has arrived — the computer started with it attached. Removals are seen
by the same look, so a drive that left and came back arrives again.

What runs (``arrival_selection``): the source's arrival runs every destination present
and every remote one; a destination's arrival runs that destination. Nothing runs while
the source is absent. A remote destination never arrives — no device event marks a
network store, and it is never counted present here — so the source's arrival is what
runs it, besides a schedule or the user; whether it can be reached then is the run's to
find, and one that cannot is recorded as waiting.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, FrozenSet, Optional, Sequence, Tuple

from ..endpoints.resolution import Presence, resolve
from ..endpoints.volumes import VolumeInfo, VolumeSource
from ..jobs.model import Job
from ..locations import RuntimeLayout

STATE_FORMAT = "mirrorhold-arrival-state"
SAME_SESSION = timedelta(minutes=2)
"""Two boot times this close are one session: the clock is read, not stored by Windows."""

SETTLE_INTERVAL = 3.0
SETTLE_LIMIT = 60.0


def source_key(job: Job) -> str:
    """The source's presence key: a new source counts as a new drive."""
    doc = json.dumps(job.source.endpoint.to_document(), sort_keys=True)
    return "source:" + hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


def destination_key(marker_id: str) -> str:
    return "destination:" + marker_id


def present_keys(job: Job, volumes: Sequence[VolumeInfo]) -> FrozenSet[str]:
    """The job's drives present now: its source and each destination resolving to one volume."""
    keys = set()
    if resolve(job.source.endpoint, volumes).presence is Presence.PRESENT:
        keys.add(source_key(job))
    for d in job.destinations:
        if resolve(d.endpoint, volumes).presence is Presence.PRESENT:
            keys.add(destination_key(d.marker_id))
    return frozenset(keys)


def arrival_selection(job: Job, present: FrozenSet[str], arrived: FrozenSet[str]) -> Optional[Tuple[int, ...]]:
    """The destinations (1-based) an arrival runs, or None when nothing should run."""
    if source_key(job) not in present:
        return None
    if source_key(job) in arrived:
        chosen = [i for i, d in enumerate(job.destinations, 1)
                  if d.remote is not None or destination_key(d.marker_id) in present]
    else:
        chosen = [i for i, d in enumerate(job.destinations, 1) if destination_key(d.marker_id) in arrived]
    return tuple(chosen) or None


@dataclass(frozen=True)
class PresenceState:
    boot: datetime              # UTC
    present: FrozenSet[str]


def state_path(layout: RuntimeLayout, job_id: str) -> Path:
    return layout.automation_dir / f"{job_id}.json"


def read_state(layout: RuntimeLayout, job_id: str) -> Optional[PresenceState]:
    try:
        doc = json.loads(state_path(layout, job_id).read_text(encoding="utf-8"))
        if doc.get("format") != STATE_FORMAT:
            return None
        return PresenceState(datetime.fromisoformat(doc["boot"]), frozenset(doc["present"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_state(layout: RuntimeLayout, job_id: str, state: PresenceState) -> None:
    path = state_path(layout, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".writing")
    staging.write_text(json.dumps({"format": STATE_FORMAT, "boot": state.boot.isoformat(),
                                   "present": sorted(state.present)}, indent=2), encoding="utf-8")
    os.replace(staging, path)


def arrivals(previous: Optional[PresenceState], boot: datetime, present: FrozenSet[str]) -> FrozenSet[str]:
    """What arrived since ``previous``: everything present, in a new session."""
    if previous is None or abs(previous.boot - boot) > SAME_SESSION:
        return present
    return present - previous.present


class _TimeOfDay(ctypes.Structure):
    _fields_ = [("BootTime", ctypes.c_longlong), ("CurrentTime", ctypes.c_longlong),
                ("TimeZoneBias", ctypes.c_longlong), ("TimeZoneId", ctypes.c_ulong), ("Reserved", ctypes.c_ulong),
                ("BootTimeBias", ctypes.c_ulonglong), ("SleepTimeBias", ctypes.c_ulonglong)]


def system_boot_time() -> datetime:
    """When this session of the computer started, in UTC."""
    if sys.platform == "win32":
        info, size = _TimeOfDay(), ctypes.c_ulong()
        status = ctypes.windll.ntdll.NtQuerySystemInformation(3, ctypes.byref(info), ctypes.sizeof(info),
                                                              ctypes.byref(size))
        if status == 0 and info.BootTime > 0:
            return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=info.BootTime // 10)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return datetime.now(timezone.utc) - timedelta(milliseconds=kernel32.GetTickCount64())
    return datetime.now(timezone.utc) - timedelta(seconds=time.monotonic())


def _fingerprint(scan) -> tuple:
    return (tuple(sorted((v.identity.anchor, v.identity.serial, v.mount_points) for v in scan.volumes)),
            tuple(sorted(str(u) for u in scan.unavailable)))


def settled_volumes(source: VolumeSource, *, sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic, interval: float = SETTLE_INTERVAL,
                    limit: float = SETTLE_LIMIT) -> Tuple[VolumeInfo, ...]:
    """The present volumes once two looks ``interval`` apart agree (or ``limit`` passed):
    a drive just attached is listed only once its file system can be read."""
    deadline = clock() + limit
    scan = source.scan()
    while True:
        sleep(interval)
        again = source.scan()
        if _fingerprint(again) == _fingerprint(scan) or clock() >= deadline:
            return tuple(again.volumes)
        scan = again
