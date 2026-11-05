"""The plan of one pair: what a run would do, computed from the two listings without doing it.

Categories (paths relative to the pair's roots):

- **new** — on the source only: copied. These are the run's additions.
- **changed** — on both sides, size or modification time differing: copied, the
  destination's copy moving into the run's versions batch. A removal.
- **deleted** — on the destination only: moved into the versions batch. A removal.
  ``deleted_dirs`` names the top-most destination-only folders, which move whole once
  every rename out of them is done; their files are counted in ``deleted`` too. A
  folder holding what a run leaves where it is never moves whole: what a shared
  destination leaves (``planning.sharing``) or a protected folder the job's exclusions
  hide (``run.runner``), both passed as ``leave``, or a file the plan keeps (**kept**,
  below). Its files that are not in the source move one by one, and it stays with what
  is left in it.
- **renames** — a destination-only file matched to a source-only file of the same size
  and modification time, the times compared within the tolerance below (the name breaks
  a tie), moved on the destination instead of copied again; and every file whose name
  differs in letter case alone, which the engine does not propagate.
  ``dir_case_renames`` are the folders whose names differ in case alone. A rename is
  not a removal.
- **temporaries** — the engine's unfinished copies on a destination this tool already
  owns: removed, never mirrored and never moved into versions as if they were data.
- **kept** — destination-only files beneath a source path that could not be read
  (a folder that could not be listed, an unreadable entry, a cloud-only file, a link):
  a listing failure is never read as a deletion; and destination-only files whose path
  a run cannot carry under its own name (``left_out``), which no step could move
  faithfully.
- **blocked** — source files whose destination path passes through a link on the
  destination, which a copy would follow out of the destination: not copied.
- **left out** — source files, and folders holding none of them that would be created,
  whose path a run cannot carry under its own name (``left_out``): the copy engine's
  limits (``engine.rclone.uncarried``), and a name ending in a space or a period, which
  the ordinary Windows paths this tool lists, opens, renames and creates through
  silently shorten. Left out of every step, so nothing is written under another name,
  and recorded with why. Their destination copies stay as they are.

Change detection reads no content: size and modification time, compared exactly unless
either side keeps coarser times (``time_window_ns``). A changed file of the same size on
both sides (``same_size_changed``) is compared by content in the engine, which only sets
its time when the contents agree.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from itertools import chain
from typing import AbstractSet, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from ..engine.rclone import is_engine_temporary, uncarried
from .listing import Entry, Side, SkipReason, Skipped, TreeListing, parent_keys, path_key


@dataclass(frozen=True)
class Change:
    source: Entry
    destination: Entry


@dataclass(frozen=True)
class Rename:
    old: str        # destination path now
    new: str        # destination path after the rename (the source's spelling)
    size: int
    case_only: bool = False


@dataclass(frozen=True)
class PairPlan:
    new: Tuple[Entry, ...] = ()
    changed: Tuple[Change, ...] = ()
    deleted: Tuple[Entry, ...] = ()
    deleted_dirs: Tuple[str, ...] = ()
    renames: Tuple[Rename, ...] = ()
    dir_case_renames: Tuple[Tuple[str, str], ...] = ()
    dirs_to_create: Tuple[str, ...] = ()
    temporaries: Tuple[str, ...] = ()
    kept: Tuple[Entry, ...] = ()
    blocked: Tuple[Skipped, ...] = ()
    unchanged: int = 0
    destination_files: int = 0
    source_skipped: Tuple[Skipped, ...] = ()
    destination_skipped: Tuple[Skipped, ...] = ()
    destination_present: bool = True
    signature_losses: FrozenSet[str] = frozenset()
    listing_seconds: Tuple[float, float] = (0.0, 0.0)   # source, destination
    left_out: Tuple[Skipped, ...] = ()                   # source paths a run cannot carry, each with why
    destination_dirs: int = 0                            # folders the destination's listing kept

    # --- quantities ----------------------------------------------------------------

    @property
    def destination_empty(self) -> bool:
        """The destination held nothing its listing keeps, or did not exist
        (``listing.TreeListing.holds_nothing``): a first run there has nothing to adopt."""
        return not self.destination_files and not self.destination_dirs and not self.destination_skipped

    @property
    def bytes_new(self) -> int:
        return sum(e.size for e in self.new)

    @property
    def bytes_changed(self) -> int:
        return sum(c.source.size for c in self.changed)

    @property
    def same_size_changed(self) -> int:
        """Changed files whose size is the same on both sides: the engine compares their
        contents first, and only sets the time of those it finds identical."""
        return sum(1 for c in self.changed if c.source.size == c.destination.size)

    @property
    def files_to_copy(self) -> int:
        return len(self.new) + len(self.changed)

    @property
    def bytes_to_copy(self) -> int:
        return self.bytes_new + self.bytes_changed

    @property
    def removal_count(self) -> int:
        return len(self.deleted) + len(self.changed)

    def removal_keys(self) -> FrozenSet[str]:
        """The files this plan would move into versions, by comparison key."""
        return frozenset([e.key for e in self.deleted] + [c.destination.key for c in self.changed])

    def moved_with_folders(self) -> Dict[str, str]:
        """The deleted files that move into versions inside a folder of ``deleted_dirs``:
        each file's comparison key to that folder as spelled. A file not here moves on its own."""
        folders = {path_key(d): d for d in self.deleted_dirs}
        moved: Dict[str, str] = {}
        for entry in self.deleted:
            folder = next((folders[p] for p in parent_keys(entry.key) if p in folders), None)
            if folder is not None:
                moved[entry.key] = folder
        return moved

    def whole_folders(self, keys: AbstractSet[str], moved: Optional[Dict[str, str]] = None) -> Dict[str, int]:
        """The folders of ``deleted_dirs`` all of whose deleted files are among ``keys``: each
        as spelled, with its file count. ``moved``: ``moved_with_folders()``, when the caller
        already holds it."""
        moved = self.moved_with_folders() if moved is None else moved
        beneath = Counter(moved.values())
        among = Counter(folder for key, folder in moved.items() if key in keys)
        return {folder: n for folder, n in among.items() if n == beneath[folder]}

    @property
    def largest_copy(self) -> int:
        return max([e.size for e in self.new] + [c.source.size for c in self.changed], default=0)

    @property
    def nothing_to_do(self) -> bool:
        return not (self.new or self.changed or self.deleted or self.deleted_dirs or self.renames
                    or self.dir_case_renames or self.temporaries or self.dirs_to_create)

    def with_signature_losses(self, keys: Iterable[str]) -> "PairPlan":
        return replace(self, signature_losses=frozenset(keys))


def left_out(relative: str) -> Optional[str]:
    """Why a run leaves a relative path out of every step, or None when it carries it under
    its own name: what the copy engine cannot carry, or a name ending in a space or a
    period, which Windows drops from an ordinary path (measured: the engine carries such a
    name, but this tool's own opening, renaming and folder creation would not; its listing
    reads such a folder as written, ``listing.enumerable``, so each file in it is named)."""
    why = uncarried(relative)
    if why is None and any(part.endswith((" ", ".")) for part in relative.split("\\")):
        why = "its name ends in a space or a period, which this tool cannot keep yet"
    return why


def _same(a: Entry, b: Entry, window_ns: int) -> bool:
    if a.size != b.size:
        return False
    return abs(a.mtime_ns - b.mtime_ns) <= window_ns


def _under(key: str, prefixes: Set[str]) -> bool:
    return key in prefixes or any(p in prefixes for p in parent_keys(key))


def _last(path: str) -> str:
    return path.rsplit("\\", 1)[-1]


def _time_groups(entries: List[Tuple[bool, Entry]], window_ns: int) -> Iterator[List[Tuple[bool, Entry]]]:
    """Files of one size, ordered by modification time and split wherever two
    consecutive times lie more than ``window_ns`` apart: each group is a run of files
    whose times chain within the window. With no window, a group is one exact time."""
    group: List[Tuple[bool, Entry]] = []
    for item in sorted(entries, key=lambda item: item[1].mtime_ns):
        if group and item[1].mtime_ns - group[-1][1].mtime_ns > window_ns:
            yield group
            group = []
        group.append(item)
    if group:
        yield group


def _match_renames(gone: Sequence[Entry], arrived: Sequence[Entry], window_ns: int) \
        -> Tuple[List[Tuple[Entry, Entry]], List[Entry], List[Entry]]:
    """Pair destination-only files with source-only files of the same size whose
    modification times agree within ``window_ns`` — the same comparison that finds a
    file unchanged, so a coarse destination clock never splits a pair. A group holding
    one of each pairs them; where a group holds several of either, a unique same-name
    pair whose times agree still matches.
    Returns (pairs of (old, new), unmatched old, unmatched new)."""
    by_size: Dict[int, List[Tuple[bool, Entry]]] = defaultdict(list)
    for e in gone:
        by_size[e.size].append((True, e))
    for e in arrived:
        by_size[e.size].append((False, e))
    pairs: List[Tuple[Entry, Entry]] = []
    matched_old: Set[str] = set()
    matched_new: Set[str] = set()
    for entries in by_size.values():
        for group in _time_groups(entries, window_ns):
            olds = [e for is_old, e in group if is_old]
            news = [e for is_old, e in group if not is_old]
            if not olds or not news:
                continue
            if len(olds) == 1 and len(news) == 1:
                candidates = [(olds[0], news[0])]
            else:
                by_name_old: Dict[str, List[Entry]] = defaultdict(list)
                by_name_new: Dict[str, List[Entry]] = defaultdict(list)
                for e in olds:
                    by_name_old[e.name.casefold()].append(e)
                for e in news:
                    by_name_new[e.name.casefold()].append(e)
                candidates = [(by_name_old[n][0], by_name_new[n][0]) for n in by_name_old.keys() & by_name_new.keys()
                              if len(by_name_old[n]) == 1 and len(by_name_new[n]) == 1
                              and _same(by_name_old[n][0], by_name_new[n][0], window_ns)]
            for old, new in candidates:
                pairs.append((old, new))
                matched_old.add(old.key)
                matched_new.add(new.key)
    return (pairs, [e for e in gone if e.key not in matched_old], [e for e in arrived if e.key not in matched_new])


def build_plan(source: TreeListing, destination: TreeListing, *, time_window_ns: int = 0,
               trust_temporaries: bool = False, leave: FrozenSet[str] = frozenset()) -> PairPlan:
    """The plan for one pair. ``trust_temporaries``: the destination's marker matched
    before this run, so leftovers of the engine's copies there are this tool's own;
    ``leave``: the keys of what the run leaves where it is and the destination's listing
    does not hold — what a shared destination leaves, a protected folder the exclusions
    hide."""
    if source.side is not Side.SOURCE or destination.side is not Side.DESTINATION:
        raise ValueError("build_plan takes a source listing and a destination listing")
    protected: Set[str] = set(source.skipped_keys())
    destination_links: Set[str] = set(destination.skipped_keys(SkipReason.LINK))
    blocked: List[Skipped] = []
    leaving: List[Skipped] = []
    new_candidates: List[Entry] = []
    changed: List[Change] = []
    renames: List[Rename] = []
    unchanged = 0

    for key, s in source.files.items():
        if _under(key, destination_links):
            blocked.append(Skipped(s.path, SkipReason.LINK, "the destination holds a link on this path"))
            continue
        why = left_out(s.path)
        if why is not None:
            leaving.append(Skipped(s.path, SkipReason.LEFT_OUT, why))
            continue
        d = destination.files.get(key)
        if d is None:
            new_candidates.append(s)
        elif _same(s, d, time_window_ns):
            unchanged += 1
            if _last(s.path) != _last(d.path):
                renames.append(Rename(d.path, s.path, s.size, case_only=True))
        else:
            changed.append(Change(s, d))

    removal_candidates: List[Entry] = []
    kept: List[Entry] = []
    temporaries: List[str] = []
    for key, d in destination.files.items():
        if key in source.files:
            continue
        if _under(key, protected) or left_out(d.path) is not None:
            kept.append(d)
        elif trust_temporaries and is_engine_temporary(d.name):
            temporaries.append(d.path)
        else:
            removal_candidates.append(d)

    pairs, deleted, new = _match_renames(removal_candidates, new_candidates, time_window_ns)
    renames.extend(Rename(old.path, arrived.path, arrived.size) for old, arrived in pairs)

    source_dirs = source.dirs
    around_left = {parent for key in chain(leave, (e.key for e in kept)) for parent in parent_keys(key)}
    dest_only_dirs = {k for k in destination.dirs if k not in source_dirs and not _under(k, protected)
                      and not _under(k, destination_links) and left_out(destination.dirs[k]) is None
                      and k not in around_left}
    deleted_dirs = sorted(destination.dirs[k] for k in dest_only_dirs
                          if not any(p in dest_only_dirs for p in parent_keys(k)))
    dir_case = sorted(((destination.dirs[k], source_dirs[k]) for k in destination.dirs.keys() & source_dirs.keys()
                       if _last(destination.dirs[k]) != _last(source_dirs[k])
                       and left_out(destination.dirs[k]) is None and left_out(source_dirs[k]) is None),
                      key=lambda pair: pair[1].count("\\"))
    to_create = [source_dirs[k] for k in source_dirs.keys() - destination.dirs.keys()
                 if not _under(k, destination_links)]
    dirs_to_create = sorted((p for p in to_create if left_out(p) is None), key=lambda p: (p.count("\\"), p))
    holding = {parent for s in leaving for parent in parent_keys(path_key(s.path))}
    leaving += sorted((Skipped(p, SkipReason.LEFT_OUT, left_out(p)) for p in to_create
                       if left_out(p) is not None and path_key(p) not in holding), key=lambda s: s.path)

    return PairPlan(new=tuple(new), changed=tuple(changed), deleted=tuple(deleted), deleted_dirs=tuple(deleted_dirs),
                    renames=tuple(renames), dir_case_renames=tuple(dir_case), dirs_to_create=tuple(dirs_to_create),
                    temporaries=tuple(sorted(temporaries)), kept=tuple(kept), blocked=tuple(blocked),
                    unchanged=unchanged, destination_files=destination.file_count,
                    source_skipped=tuple(source.skipped), destination_skipped=tuple(destination.skipped),
                    destination_present=destination.present,
                    listing_seconds=(source.seconds, destination.seconds), left_out=tuple(leaving),
                    destination_dirs=len(destination.dirs))


def time_window_ns(*file_systems: str) -> int:
    """The modification-time tolerance between two file systems: FAT keeps two-second
    times, exFAT ten-millisecond ones, NTFS and ReFS a hundred nanoseconds."""
    names = {f.upper() for f in file_systems}
    if names & {"FAT", "FAT32"}:
        return 2_000_000_000
    if "EXFAT" in names:
        return 10_000_000
    return 0
