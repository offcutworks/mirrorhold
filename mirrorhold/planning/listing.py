"""One listing of one side of a pair: what a folder holds, from directory listings alone.

A run lists each side exactly once and hands the engine an explicit list, so this
listing is the only one a run pays for (a cold listing of a photo drive costs minutes).
It opens no file: every classification comes from the attributes and reparse tag a
directory listing already carries.

Left out of ``files`` and ``dirs``, with their consequences carried in the listing:

- the management folder (``.mirrorhold``) at any depth, on both sides: a destination
  of another job may lie inside a source, and its management folder is never copied;
- drive-root artefacts, when the side is a volume root;
- whatever the job's ``EntryFilter`` excludes (counted, not itemised);
- links and junctions — never followed; recorded as ``skipped`` so the plan neither
  copies through one nor removes what sits at its path;
- cloud-only placeholders on the source side — never read; recorded as ``skipped``, so
  an existing copy on the destination is kept;
- an entry whose metadata cannot be read, and a folder that cannot be listed — recorded,
  so nothing beneath them on the destination is read as deleted at the source;
- with a ``Scope`` (a shared destination's, ``planning.sharing``), whatever lies outside
  it: never read, not even stat'ed, so a destination at a system drive's root costs a run
  only its job's part of it.

A caller watching a long listing passes ``progress``, which is told after every folder
how many entries have been seen; an exception it raises ends the listing and reaches
the caller unchanged, which is how a caller cancels one.

A root may be a device path (``\\\\?\\GLOBALROOT\\Device\\...``, a volume snapshot's): it lists
like any folder, but the root of the device itself is a folder only when spelled with
its trailing separator, which ``pathlib`` drops — so the root is tested with one.

On Windows a folder is enumerated through its extended-length spelling (``\\\\?\\``,
``enumerable``), which Windows takes as written, and every folder beneath is reached
through its entry's own path in that spelling. Spelled the ordinary way, a folder whose
name ends in a space or a period loses that ending before it is listed and reads as
missing: CPython 3.13 and later normalise the folder as every other call does (measured
on 3.14.8), and 3.10 already dropped a trailing period, keeping a trailing space (measured
on 3.10.11). Listed as written,
such a folder's files are seen by name, and the plan leaves each out by name
(``plan.left_out``) rather than the folder reading as one that cannot be listed.

A destination **holds nothing** when its listing keeps nothing — no file, no folder, no
link and no entry it could not read — or when its folder does not exist yet. ``occupancy``
answers that from the top level alone: every top-level entry the listing would keep makes
the destination hold something, so the answer is the full listing's
(``TreeListing.holds_nothing``) for one directory read, which a window can afford before
it asks whether to start a first backup.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from ..jobs.exclusions import EntryFilter, is_cloud_placeholder, is_drive_root_artefact, is_link
from ..storage.destination import MANAGEMENT_DIR

ListingProgress = Callable[[int], None]
"""Told the number of entries seen so far, after each folder."""

_EXTENDED_PREFIXES = ("\\\\?\\", "\\\\.\\")


def enumerable(directory: str) -> str:
    """``directory`` spelled so that Windows enumerates the folder as named: its
    extended-length form, which no Windows call trims. A path already in a device or
    extended form, and every path off Windows, is returned as it is. The root of a listing
    is an absolute path, which is prefixed as it stands — resolving it again would trim it."""
    if os.name != "nt" or directory.startswith(_EXTENDED_PREFIXES):
        return directory
    full = directory if os.path.isabs(directory) else os.path.abspath(directory)
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


class Side(str, Enum):
    SOURCE = "source"
    DESTINATION = "destination"


class SkipReason(str, Enum):
    PLACEHOLDER = "cloud-only file, not read"
    LINK = "link or junction, not followed"
    UNREADABLE = "metadata could not be read"
    UNLISTABLE = "folder could not be listed"
    LEFT_OUT = "name this tool cannot keep"   # the plan's, not a listing's (``plan.left_out``)


class ListingError(Exception):
    """The side's root folder exists but cannot be listed at all."""


class Occupancy(str, Enum):
    """What a destination's folder holds, as its listing would see it."""

    MISSING = "missing"          # the folder does not exist yet
    EMPTY = "empty"              # it exists and the listing would keep nothing
    OCCUPIED = "occupied"        # the listing would keep something
    UNKNOWN = "unknown"          # it exists and is not a folder that can be listed

    @property
    def holds_nothing(self) -> bool:
        return self in (Occupancy.MISSING, Occupancy.EMPTY)


@dataclass(frozen=True)
class Scope:
    """The part of a destination a listing covers: the top-level entries whose comparison
    keys are in ``names``, and everything beneath them but the entries in ``skip`` and
    what lies beneath those."""

    names: FrozenSet[str]
    skip: FrozenSet[str] = frozenset()

    def covers(self, key: str, at_root: bool) -> bool:
        return (not at_root or key in self.names) and key not in self.skip


@dataclass(frozen=True, slots=True)
class Entry:
    """A file: its path relative to the side's root (backslash-separated, as the volume
    spells it), size and modification time."""

    path: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return path_key(self.path)

    @property
    def name(self) -> str:
        return self.path.rsplit("\\", 1)[-1]


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: SkipReason
    detail: str = ""


def path_key(path: str) -> str:
    """The comparison form of a relative path: names compare case-insensitively."""
    return path.casefold()


def parent_keys(key: str):
    """Every proper ancestor of a relative path key, nearest first."""
    while "\\" in key:
        key = key.rsplit("\\", 1)[0]
        yield key


@dataclass
class TreeListing:
    root: object                     # the folder listed: a Path, or a remote's folder (``engine.rclone.RemoteRoot``)
    side: Side
    present: bool = True
    files: Dict[str, Entry] = field(default_factory=dict)       # key -> entry
    dirs: Dict[str, str] = field(default_factory=dict)          # key -> relative path as spelled
    skipped: List[Skipped] = field(default_factory=list)
    excluded: int = 0
    seconds: float = 0.0
    ambiguous: List[Tuple[str, str]] = field(default_factory=list)   # two paths one key names: a remote's only
    fingerprints: Dict[str, str] = field(default_factory=dict)  # key -> the fingerprint a remote keeps, when asked

    def skipped_keys(self, *reasons: SkipReason) -> FrozenSet[str]:
        wanted = set(reasons) or set(SkipReason)
        return frozenset(path_key(s.path) for s in self.skipped if s.reason in wanted)

    @property
    def holds_nothing(self) -> bool:
        """No file, folder, link or unreadable entry kept; true of an absent listing."""
        return not self.files and not self.dirs and not self.skipped

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.files.values())

    @property
    def largest_file(self) -> int:
        return max((e.size for e in self.files.values()), default=0)


def list_tree(root: Path, side: Side, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None,
              progress: Optional[ListingProgress] = None, scope: Optional[Scope] = None) -> TreeListing:
    """List one side, within ``scope`` when one is given. A missing root is an empty,
    absent listing; a root that exists and cannot be listed raises ListingError."""
    entry_filter = entry_filter or EntryFilter()
    listing = TreeListing(Path(root), side)
    started = time.monotonic()
    if not os.path.isdir(os.path.join(root, "")):   # a device's root is a folder only with its separator
        if os.path.lexists(root):
            raise ListingError(f"{root} is not a folder")
        listing.present = False
        listing.seconds = time.monotonic() - started
        return listing
    stack: List[Tuple[str, str]] = [(enumerable(str(root)), "")]
    first = True
    seen = 0
    while stack:
        if progress is not None:
            progress(seen)
        directory, relative = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            if first:
                raise ListingError(f"{root} cannot be listed: {exc.strerror or exc}") from None
            listing.skipped.append(Skipped(relative, SkipReason.UNLISTABLE, exc.strerror or str(exc)))
            continue
        at_root, first = first, False
        seen += len(entries)
        for entry in entries:
            path = f"{relative}\\{entry.name}" if relative else entry.name
            if scope is not None and not scope.covers(path_key(path), at_root):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                listing.skipped.append(Skipped(path, SkipReason.UNREADABLE, exc.strerror or str(exc)))
                continue
            attributes = getattr(info, "st_file_attributes", 0)
            if is_link(attributes, getattr(info, "st_reparse_tag", 0)):
                listing.skipped.append(Skipped(path, SkipReason.LINK))
                continue
            if at_root and drive_root and is_drive_root_artefact(attributes):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir and entry.name.casefold() == MANAGEMENT_DIR:
                continue
            if entry_filter.excludes(path, entry.name, is_dir):
                listing.excluded += 1
                continue
            if is_dir:
                listing.dirs[path_key(path)] = path
                stack.append((entry.path, path))
                continue
            if side is Side.SOURCE and is_cloud_placeholder(attributes):
                listing.skipped.append(Skipped(path, SkipReason.PLACEHOLDER))
                continue
            listing.files[path_key(path)] = Entry(path, info.st_size, info.st_mtime_ns)
    if progress is not None:
        progress(seen)
    listing.seconds = time.monotonic() - started
    return listing


def occupancy(root: Path, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None) -> Occupancy:
    """What a destination holds, from its top level: each entry is classified as
    ``list_tree`` classifies it at the root, and the first one the listing would keep
    — a file, a folder, a link, an entry whose metadata cannot be read — makes it
    occupied. Keep the two in step: their agreement is the definition of a destination
    that holds nothing."""
    entry_filter = entry_filter or EntryFilter()
    if not os.path.isdir(os.path.join(root, "")):
        return Occupancy.UNKNOWN if os.path.lexists(root) else Occupancy.MISSING
    try:
        with os.scandir(enumerable(str(root))) as iterator:
            entries = list(iterator)
    except OSError:
        return Occupancy.UNKNOWN
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return Occupancy.OCCUPIED
        attributes = getattr(info, "st_file_attributes", 0)
        if is_link(attributes, getattr(info, "st_reparse_tag", 0)):
            return Occupancy.OCCUPIED
        if drive_root and is_drive_root_artefact(attributes):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            return Occupancy.OCCUPIED
        if is_dir and entry.name.casefold() == MANAGEMENT_DIR:
            continue
        if entry_filter.excludes(entry.name, entry.name, is_dir):
            continue
        return Occupancy.OCCUPIED
    return Occupancy.EMPTY
