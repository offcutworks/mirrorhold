"""Folders a cloud client keeps synchronised on this computer, and what it does with them.

Two records are read, neither holding a credential nor any synchronised file:

- **Windows' Cloud Files registrations.** Every client built on the Cloud Files API
  (OneDrive, Baidu's sync disk and others) registers its sync roots under
  ``SyncRootManager``; the key name starts with the provider, and its ``UserSyncRoots``
  subkey maps each user's SID to the root's path. No list of clients is kept: a client
  that registers is seen.
- **Google Drive for desktop's own list.** Drive registers none of its folders there. Its
  folder list is the ``roots`` table of ``root_preference_sqlite.db`` in its data folder
  under ``%LOCALAPPDATA%``: each row names the folder's volume by its volume GUID, its
  path on that volume, whether it is My Drive's folder in mirror mode, and the path Drive last
  saw it at. Only those columns are read, through a read-only connection; the database
  is Drive's and undocumented, so a list that cannot be read is said
  (``SyncRoot.problem``), never taken for an empty one.

A root's ``kind`` says what the cloud does with the folder. A folder kept on every
computer the account reaches (``SyncKind.DEVICES``: Cloud Files roots, My Drive in mirror mode)
cannot be a destination: each run's writes would spread to every one of them. A folder
from this computer that the client keeps a copy of in its cloud only
(``SyncKind.CLOUD_COPY``: Drive's other folders) may be one — that copy can be how a user
keeps a backup in the cloud — and the client may still write back into it, so it is
noticed (``jobs.validation``). The records are read as they are, stale entries included:
a stale entry costs a refusal or a notice, a missed one costs the user's cloud. A client
that neither registers its folders nor is read here is not seen.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..contract_errors import ContractError
from .endpoint import Endpoint
from .volumes import VolumeInfo, find_by_mount

SYNC_ROOT_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager"
GOOGLE_DRIVE = "Google Drive"
DRIVE_ROOTS_FILE = "root_preference_sqlite.db"
DRIVE_ROOTS_QUERY = "SELECT media_id, root_path, is_my_drive, last_seen_absolute_path FROM roots"
DRIVE_READ_TIMEOUT = 2.0
"""Seconds a read of Drive's list waits for a write of Drive's own to end."""
_VOLUME_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class SyncKind(str, Enum):
    DEVICES = "devices"          # kept on every computer the account reaches
    CLOUD_COPY = "cloud-copy"    # this computer's folder, of which the cloud keeps a copy


@dataclass(frozen=True)
class SyncRoot:
    """One synchronised folder: ``path`` as the user knows it, and ``locator``, the path it
    is found on the present volumes by ("" for ``path``) — a volume device path where the
    client names the volume itself, so a changed drive letter still finds it. An entry
    with ``problem`` set names no folder: the provider's list could not be read, and why."""

    provider: str
    path: str
    kind: SyncKind = SyncKind.DEVICES
    locator: str = ""
    problem: str = ""


def sync_roots_on_this_computer() -> Tuple[SyncRoot, ...]:
    """Every synchronised folder this computer's records name for this user."""
    roots = list(registered_sync_roots())
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    roots.extend(drive_sync_roots(base / "Google" / "DriveFS"))
    return tuple(roots)


def registered_sync_roots() -> Tuple[SyncRoot, ...]:
    if sys.platform != "win32":
        return ()
    import winreg
    roots: List[SyncRoot] = []
    try:
        manager = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SYNC_ROOT_KEY)
    except OSError:
        return ()
    with manager:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(manager, index)
            except OSError:
                break
            index += 1
            try:
                users = winreg.OpenKey(manager, name + r"\UserSyncRoots")
            except OSError:
                continue
            with users:
                value_index = 0
                while True:
                    try:
                        _sid, path, kind = winreg.EnumValue(users, value_index)
                    except OSError:
                        break
                    value_index += 1
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and path:
                        roots.append(SyncRoot(name.split("!", 1)[0], path))
    return tuple(roots)


def drive_sync_roots(data_folder: Path) -> Tuple[SyncRoot, ...]:
    """The folders Google Drive for desktop synchronises, from its data folder: none when
    that folder does not exist (Drive is not used here), one entry with ``problem`` when
    its list cannot be read."""
    if not data_folder.is_dir():
        return ()
    database = data_folder / DRIVE_ROOTS_FILE
    if not database.is_file():
        return (SyncRoot(GOOGLE_DRIVE, "", problem=f"its list of folders, {DRIVE_ROOTS_FILE}, is not in "
                                                   f"{data_folder}"),)
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=DRIVE_READ_TIMEOUT)
        try:
            rows = connection.execute(DRIVE_ROOTS_QUERY).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return (SyncRoot(GOOGLE_DRIVE, "", problem=f"its list of folders could not be read ({exc})"),)
    return tuple(root for root in (_drive_root(*row) for row in rows) if root is not None)


def _drive_root(media, relative, my_drive, last_seen) -> Optional[SyncRoot]:
    """One row of Drive's list as a root; None for a row naming no folder."""
    if not isinstance(relative, str):
        return None
    last_seen = last_seen if isinstance(last_seen, str) else ""
    kind = SyncKind.DEVICES if my_drive else SyncKind.CLOUD_COPY
    if isinstance(media, str) and _VOLUME_GUID.match(media):
        inner = relative.replace("/", "\\").strip("\\")
        locator = f"\\\\?\\Volume{{{media.lower()}}}\\{inner}"
        return SyncRoot(GOOGLE_DRIVE, last_seen or locator, kind, locator)
    if not last_seen:
        return None
    return SyncRoot(GOOGLE_DRIVE, last_seen, kind)


def sync_root_endpoints(roots: Sequence[SyncRoot],
                        volumes: Sequence[VolumeInfo]) -> Tuple[Tuple[SyncRoot, Endpoint], ...]:
    """Each sync root on a present volume, as an endpoint; roots elsewhere, and entries
    naming no folder, are skipped."""
    found = []
    for root in roots:
        if root.problem:
            continue
        located = find_by_mount(volumes, root.locator or root.path)
        if located is None:
            continue
        volume, remainder = located
        try:
            found.append((root, Endpoint(volume.identity, remainder)))
        except ContractError:
            continue
    return tuple(found)
