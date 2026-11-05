"""A destination on a drive of this computer: a folder reached by its Windows path.

Each operation is the core's own way with a drive: a listing by directory
reads alone (``planning.listing``), the shared files written and read through the exchange
(``exchange``), a file opened the engine's way (``engine.rclone.open_source_file``), a move a
single rename on the destination's own volume. Folders are entered by their extended-length
spelling (``planning.listing.enumerable``), so one whose name ends in a space or a period is
looked into like any other. Nothing here follows a link: an entry is stat'ed as itself, and
a link or junction met inside a folder being deleted is removed as a link, what it points
at untouched.

A destination given to the core as a folder of this computer is this kind
(``destination_storage``), so a caller that holds only a path reaches it as every other
caller does.

``StoredEntry.is_link`` from ``entry`` and ``entries`` is any reparse point — what the version
cleanup and the recognition of what reappeared never enter; ``file_entry``'s is a name
surrogate alone, a link as a listing passes one over, which is what a repair leaves.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple, Union

from .. import exchange
from ..engine.rclone import open_source_file
from ..jobs.exclusions import EntryFilter, is_link as surrogate
from ..planning.listing import (ListingProgress, Occupancy, Scope, Side, TreeListing, enumerable, list_tree,
                                occupancy)
from ..planning.plan import time_window_ns
from .destination import MANAGEMENT_DIR, WRITE_PATIENCE, DestinationStorage, StoredEntry

_READ_ONLY = 0x1
_HIDDEN = 0x2
_DIRECTORY = 0x10
_REPARSE_POINT = 0x400


def _attributes(info: os.stat_result) -> int:
    return getattr(info, "st_file_attributes", 0)


def is_link(info: os.stat_result) -> bool:
    """Whether an entry is a link, a junction or any other reparse point: never entered."""
    return bool(_attributes(info) & _REPARSE_POINT) or stat.S_ISLNK(info.st_mode)


def _stored(name: str, info: os.stat_result) -> StoredEntry:
    return StoredEntry(name, stat.S_ISDIR(info.st_mode), is_link(info), info.st_size, info.st_mtime_ns,
                       _attributes(info))


class DriveStorage(DestinationStorage):
    """A destination folder of this computer. ``volume``: the volume it lies on, as the
    scan found it (``endpoints.volumes.VolumeInfo``), which gives its file system's time
    precision and its free space; without one, times compare exactly and no space is known."""

    def __init__(self, root, volume=None):
        self.root = Path(root)
        self.volume = volume

    def __repr__(self) -> str:
        return f"DriveStorage({str(self.root)!r})"

    def __truediv__(self, relative) -> Path:
        """``storage / name``: a path below the drive destination's folder, for a caller
        that holds the storage and builds a path of this computer from it. A remote's
        storage has none, so code that joins one fails at once rather than act on a path."""
        return self.root / relative

    def path(self, relative: str = "") -> Path:
        """``relative`` as a path of this computer."""
        return self.root / relative if relative else self.root

    def _enumerable(self, relative: str = "") -> str:
        return enumerable(str(self.path(relative)))

    # --- what it is ------------------------------------------------------------------

    def describe(self, relative: str = "") -> str:
        return str(self.path(relative))

    @property
    def local_folder(self) -> Optional[Path]:
        return self.root

    def locate(self, relative: str = "") -> Path:
        return self.path(relative)

    @property
    def time_window_ns(self) -> int:
        return time_window_ns(self.volume.file_system) if self.volume is not None else 0

    def space(self) -> Optional[Tuple[int, int]]:
        return (self.volume.free_bytes, self.volume.capacity_bytes) if self.volume is not None else None

    # --- reading ---------------------------------------------------------------------

    def entry(self, relative: str) -> Optional[StoredEntry]:
        where = self.path(relative)
        try:
            info = os.lstat(where)
        except FileNotFoundError:
            return None
        return _stored(where.name, info)

    def exists(self, relative: str) -> bool:
        """As ``os.path.lexists``: a link counts, and a path whose metadata Windows refuses
        to read counts as not there."""
        return os.path.lexists(self.path(relative))

    def is_folder(self, relative: str = "") -> bool:
        """Whether a folder is there — a device's root is one only with its separator, which
        ``pathlib`` drops, so the path is tested with one."""
        return os.path.isdir(os.path.join(self.path(relative), ""))

    def entries(self, relative: str) -> Tuple[StoredEntry, ...]:
        found: List[StoredEntry] = []
        with os.scandir(self._enumerable(relative)) as iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    found.append(StoredEntry(entry.name, False, problem=exc.strerror or str(exc)))
                    continue
                found.append(_stored(entry.name, info))
        return tuple(found)

    def file_entry(self, parts) -> Optional[StoredEntry]:
        """The entry at the path ``parts`` names, each folder on the way stat'ed as itself, so
        a link on the way is the entry returned; None when the path is not there. A link here
        is what a listing passes over as one — a symbolic link or a junction, a name
        surrogate (``jobs.exclusions.is_link``) — so a file under another reparse point (a
        compressed, deduplicated or placeholder file) is the file it lists as. A link is never
        followed."""
        where = self._enumerable()
        found = None
        for name in parts:
            where = os.path.join(where, name)
            try:
                info = os.lstat(where)
            except FileNotFoundError:
                return None
            found = replace(_stored(name, info),
                            is_link=surrogate(_attributes(info), getattr(info, "st_reparse_tag", 0)))
            if found.is_link:
                return found
        return found

    def list_tree(self, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None,
                  progress: Optional[ListingProgress] = None, scope: Optional[Scope] = None,
                  fingerprint: Optional[str] = None) -> TreeListing:
        """A drive keeps no fingerprint, so ``fingerprint`` adds none."""
        return list_tree(self.root, Side.DESTINATION, drive_root=drive_root, entry_filter=entry_filter,
                         progress=progress, scope=scope)

    def occupancy(self, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None) -> Occupancy:
        return occupancy(self.root, drive_root=drive_root, entry_filter=entry_filter)

    def read_file(self, relative: str) -> bytes:
        """A management file — the marker, a shared destination's record — which a run only
        ever renames over: one not there is looked for again for the moment a rename takes
        its name away (``exchange.RENAME_PATIENCE``)."""
        return exchange.read_bytes(self.path(relative), missing=exchange.RENAME_PATIENCE)

    def read_head(self, relative: str, size: int) -> bytes:
        with open_source_file(self.path(relative)) as handle:
            return handle.read(size)

    def open_file(self, relative: str) -> BinaryIO:
        return open_source_file(self.path(relative))

    # --- writing ---------------------------------------------------------------------

    def write_file(self, relative: str, data: bytes, *, patience: float = WRITE_PATIENCE) -> None:
        exchange.write_bytes(self.path(relative), data, durable=True, patience=patience)

    def make_management_folder(self) -> None:
        folder = self.path(MANAGEMENT_DIR)
        created = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        if created:
            _hide(folder)

    def make_folder(self, relative: str = "") -> None:
        self.path(relative).mkdir(parents=True, exist_ok=True)

    def move(self, old: str, new: str, *, parents: bool = True) -> None:
        target = self.path(new)
        if parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.path(old), target)

    def remove_file(self, relative: str) -> None:
        os.remove(self.path(relative))

    def remove_empty_tree(self, relative: str) -> bool:
        where = self.path(relative)
        return _holds_nothing(where) and _remove_empty_tree(where)

    def delete_tree(self, relative: str) -> Optional[str]:
        return _delete_tree(self._enumerable(relative))

    def measure(self, relative: str) -> Tuple[int, int]:
        return _measure(self._enumerable(relative))


def destination_storage(where: Union[DestinationStorage, str, os.PathLike]) -> DestinationStorage:
    """``where`` as the seam: a storage as it is, a folder of this computer as a drive's."""
    return where if isinstance(where, DestinationStorage) else DriveStorage(where)


def _hide(path: Path) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    k32.GetFileAttributesW.restype = wintypes.DWORD
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    attributes = k32.GetFileAttributesW(str(path))
    if attributes != 0xFFFFFFFF:
        k32.SetFileAttributesW(str(path), attributes | _HIDDEN)


def _holds_nothing(folder: Path) -> bool:
    """Whether a folder tree holds folders only: no file, no link, no other reparse point.
    Entered by its extended-length spelling, so a folder whose name ends in a space or a
    period is looked into rather than taken to hold something."""
    stack = [enumerable(str(folder))]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        return False
                    if _attributes(entry.stat(follow_symlinks=False)) & _REPARSE_POINT:
                        return False
                    stack.append(entry.path)
        except OSError:
            return False
    return True


def _remove_empty_tree(folder: Path) -> bool:
    """Remove a tree of empty folders, deepest first; False (and the rest left) as soon as
    one is not empty after all."""
    for directory, _subdirs, _files in os.walk(enumerable(str(folder)), topdown=False):
        try:
            os.rmdir(directory)
        except OSError:
            return False
    return True


def _measure(folder: str) -> Tuple[int, int]:
    """Files and bytes in a folder tree, links neither followed nor counted."""
    files = size = 0
    stack = [folder]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_link(info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                    else:
                        files += 1
                        size += info.st_size
        except OSError:
            continue
    return files, size


def _writable(path: str, attributes: int) -> None:
    if attributes & _READ_ONLY:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def _delete_tree(root: str) -> Optional[str]:
    """Delete a folder tree that is not a link, never entering a link inside it. Read-only
    files and folders — a copy keeps Windows' read-only attribute — are made writable
    first. Returns what could not be deleted, or None."""
    folders: List[Tuple[str, int]] = []
    stack = [root]
    try:
        while stack:
            folder = stack.pop()
            folders.append((folder, _attributes(os.lstat(folder))))
            with os.scandir(folder) as iterator:
                entries = list(iterator)
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                attributes = _attributes(info)
                if is_link(info):
                    if attributes & _DIRECTORY or stat.S_ISDIR(info.st_mode):
                        os.rmdir(entry.path)                  # the junction or folder link itself
                    else:
                        os.remove(entry.path)
                elif stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                else:
                    _writable(entry.path, attributes)
                    os.remove(entry.path)
        for folder, attributes in reversed(folders):
            _writable(folder, attributes)
            os.rmdir(folder)
    except OSError as exc:
        where = os.path.relpath(exc.filename, root) if exc.filename else ""
        return f"{where + ': ' if where and where != '.' else ''}{exc.strerror or exc}"
    return None
