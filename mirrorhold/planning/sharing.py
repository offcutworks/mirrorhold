"""What a run of a shared destination covers, and what it leaves where it is. No I/O.

A **shared** destination (``endpoints.shared``) holds, besides its job's own top-level
entries, content that is not the job's: whatever lies outside the names the job owns,
whenever it appeared, and, beneath those names, what was there before the job and the
source does not hold (``left``). ``share`` takes a listing of the destination — a whole
one, or one already limited by ``listing_scope`` — and the source's listing, and returns
the job's part of the destination, with the record as this run leaves it:

- **The job's names** are those it owns and the source's top-level names now. A source
  name the job does not own yet is claimed; what its destination entry already holds
  that the source does not hold is left — the top-most such entries, a folder whole.
- **A left entry the source reaches** — the source now holds something at its path or
  beneath it — is left no longer: what the source holds there is the job's, its copy on
  the destination replaced and the old one moving into versions, and what the source
  does not hold beneath it is left in its place.
- **What the source holds as a file where the destination holds a folder** takes that
  folder's place whole: the folder moves into versions with everything in it.
- **A name stops being the job's** once neither the source nor the destination holds
  it, and what was left beneath it is forgotten with it.

So nothing that is not the job's ever reaches the plan: it is never counted as deleted
at the source. Beneath a name the job owns, anything else — what the user puts there
later included — is mirrored as on any destination, and a folder holding a left entry
never moves whole (``plan.build_plan``'s ``leave``).

A full verification reads the record as it stands instead (``recorded_scope``), as a
repair does: what the job has backed up is beneath the names it owns, less what it
leaves. A name the source gained since the last run, and a left entry the source
reaches now, are not the job's until a run claims them — what the destination holds
there may be the user's own, and is never compared.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Sequence, Tuple

from .listing import Scope, TreeListing, parent_keys, path_key


@dataclass(frozen=True)
class Sharing:
    """The job's part of a shared destination, and its record as the run leaves it."""

    listing: TreeListing                    # the destination as far as it is the job's
    own: Tuple[str, ...]                    # the names the job owns afterwards, as spelled
    left: Tuple[str, ...]                   # what is left beneath them, top-most, as the destination spells it
    left_files: int = 0                     # files of what is left that the listing saw (all of it, from a whole one)
    left_items: Tuple[str, ...] = ()        # the top-most items left: outside the job's names, then beneath them
    meeting: Tuple[Tuple[str, int], ...] = ()   # names claimed this run over what was there, files left beneath
    changed: bool = False                   # whether the record differs from the one given

    @property
    def left_keys(self) -> FrozenSet[str]:
        return frozenset(path_key(p) for p in self.left)

    @property
    def leaves_anything(self) -> bool:
        return bool(self.left_items)


def top_names(source: TreeListing) -> Dict[str, str]:
    """A listing's top-level files and folders, by key, as spelled: for a source, the
    names a run puts at a shared destination's top level."""
    top: Dict[str, str] = {}
    for key, entry in source.files.items():
        if "\\" not in key:
            top[key] = entry.path
    for key, spelled in source.dirs.items():
        if "\\" not in key:
            top[key] = spelled
    return top


def _reached(key: str, source: TreeListing) -> bool:
    """Whether the source holds something at this path or beneath it (a folder the
    source holds anything beneath is itself in its listing)."""
    return key in source.files or key in source.dirs


def _taken(key: str, source: TreeListing) -> bool:
    """Whether a destination entry is the job's in a region it claims: the source holds
    something at its path or beneath it, or it lies inside a folder the source holds as
    a file, which it goes with."""
    return _reached(key, source) or any(p in source.files for p in parent_keys(key))


def listing_scope(source: TreeListing, own: Iterable[str] = (), left: Iterable[str] = ()) -> Scope:
    """The part of a shared destination its run lists: the names the job owns and the
    source's top-level names, without the left entries the source does not reach."""
    names = {path_key(name) for name in own} | set(top_names(source))
    skip = frozenset(k for k in (path_key(p) for p in left) if not _reached(k, source))
    return Scope(frozenset(names), skip)


def recorded_scope(own: Iterable[str], left: Iterable[str]) -> Scope:
    """The part of a shared destination its record says the job has backed up: the names
    it owns, without what it leaves, whatever the source holds now."""
    return Scope(frozenset(path_key(name) for name in own), frozenset(path_key(p) for p in left))


def share(destination: TreeListing, source: TreeListing, own: Sequence[str] = (),
          left: Sequence[str] = ()) -> Sharing:
    """The job's part of ``destination``, given the record's ``own`` and ``left`` — both
    empty at a first run that leaves what the destination holds."""
    owned = {path_key(name): name for name in own}
    source_top = top_names(source)
    names = set(owned) | set(source_top)
    recorded = {path_key(p): p for p in left}
    still_left = {k: p for k, p in recorded.items() if not _reached(k, source)}
    reached_left = {k for k in recorded if k not in still_left}
    claimed = {k for k in source_top if k not in owned}

    entries: Dict[str, str] = {k: e.path for k, e in destination.files.items()}
    entries.update(destination.dirs)
    entries.update((path_key(s.path), s.path) for s in destination.skipped)
    destination_top = {k for k in entries if "\\" not in k}

    outside: Dict[str, str] = {}
    new_left: Dict[str, str] = {}
    for key, spelled in entries.items():
        top = key.split("\\", 1)[0]
        if top not in names:
            if top == key:
                outside[key] = spelled
            continue
        lineage = [key, *parent_keys(key)]
        if any(k in still_left for k in lineage):
            continue
        claiming = top in claimed or any(k in reached_left for k in lineage[1:])
        if not claiming or _taken(key, source):
            continue
        parent = lineage[1] if len(lineage) > 1 else None
        if parent is None or _taken(parent, source):
            new_left[key] = spelled

    own_after = {k: source_top.get(k) or owned[k] for k in names if k in source_top or k in destination_top}
    left_after = {k: p for k, p in {**still_left, **new_left}.items() if k.split("\\", 1)[0] in own_after}
    gone = set(outside) | set(left_after)

    def kept(key: str) -> bool:
        return key not in gone and not any(p in gone for p in parent_keys(key))

    part = TreeListing(destination.root, destination.side, destination.present,
                       files={k: e for k, e in destination.files.items() if kept(k)},
                       dirs={k: p for k, p in destination.dirs.items() if kept(k)},
                       skipped=[s for s in destination.skipped if kept(path_key(s.path))],
                       excluded=destination.excluded, seconds=destination.seconds)
    left_files = len(destination.files) - len(part.files)
    met: Dict[str, int] = {k.split("\\", 1)[0]: 0 for k in new_left if k.split("\\", 1)[0] in claimed}
    for key in destination.files:
        if key.split("\\", 1)[0] in met and any(k in new_left for k in (key, *parent_keys(key))):
            met[key.split("\\", 1)[0]] += 1
    meeting: List[Tuple[str, int]] = [(source_top[name], met[name]) for name in sorted(met)]
    changed = set(own_after) != set(owned) or set(left_after) != set(recorded)
    left_spelled = tuple(left_after[k] for k in sorted(left_after))
    return Sharing(part, tuple(own_after[k] for k in sorted(own_after)), left_spelled, left_files,
                   tuple(outside[k] for k in sorted(outside)) + left_spelled, tuple(meeting), changed)
