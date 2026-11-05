"""Files that reappeared after being moved into versions, as a preview recognises them.

A file the destination holds and the source does not, whose path, size and modification
time equal a copy in the destination's newest versions batch, was moved into versions and
has since reappeared: a cloud client that keeps a copy of the folder writing it back, or a
person copying it back. The preview names such files apart from the other removals and says
when they were moved. A run treats them like any other removal: they move into versions
again and count toward the job's holds (the maintainer's decision of 2026-10-28).

Only the newest batch is read, and within it only the folders that hold a file the plan
would move into versions: one partial listing per destination per preview. Measured on the
development machine, a batch of some twenty thousand files listed whole in under a second.
The batch is found and entered as the version cleanup finds one (``maintenance.cleanup``):
a folder named as a run or a repair names its batch, its name giving the moment the move
began, never entered through a link. Times are compared exactly: a move into versions is a
rename on the destination's own volume, which keeps the time as it was, and Google Drive
was measured putting files back with their times to the 100 ns. What cannot be read
recognises nothing: a batch, or a folder of it, that cannot be listed leaves the files it
holds read as ordinary removals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, FrozenSet, List, Optional, Tuple

from ..maintenance.cleanup import VERSIONS, batch_time, versions_problem
from ..planning.listing import parent_keys, path_key
from ..planning.plan import PairPlan
from ..storage.drive import destination_storage


@dataclass(frozen=True)
class PutBack:
    """The removals of a plan that reappeared after being moved into the batch ``batch``."""

    batch: str                  # the batch's folder name
    moved: datetime             # when the move into it began, in UTC (from its name)
    keys: FrozenSet[str]        # comparison keys of the plan's deleted files found there
    keeper: str = ""            # the cloud client keeping a copy of the folder, as its notice names it


def _order(name: str) -> Tuple[datetime, int]:
    """A batch's place in time: its date, then the counter two moves begun in one second add."""
    return batch_time(name), int(name.rsplit("Z-", 1)[1]) if "Z-" in name else 1


def newest_batch(destination) -> Optional[Tuple[str, datetime]]:
    """The destination's newest versions batch, its name and when the move into it began;
    None when it has none, or its versions folder is not a real folder or cannot be read.
    ``destination``: a storage (``storage``), or a folder of this computer."""
    storage = destination_storage(destination)
    try:
        if versions_problem(storage) or not storage.is_folder(VERSIONS):
            return None
        listed = storage.entries(VERSIONS)
    except OSError:                  # a remote that cannot answer now, a folder gone since the look
        return None
    found: List[str] = [e.name for e in listed
                        if batch_time(e.name) is not None and not e.problem and e.is_dir and not e.is_link]
    if not found:
        return None
    name = max(found, key=_order)
    return name, batch_time(name)


def put_back(plan: PairPlan, destination, progress: Callable[[], None] = lambda: None) -> Optional[PutBack]:
    """The plan's deleted files that equal their copy in the newest versions batch, or None
    when there is none. ``progress`` is called after each folder of the batch read; an
    exception it raises ends the reading and reaches the caller."""
    if not plan.deleted:
        return None
    storage = destination_storage(destination)
    newest = newest_batch(storage)
    if newest is None:
        return None
    name, moved = newest
    wanted = {e.key: e for e in plan.deleted}
    folders = {parent for key in wanted for parent in parent_keys(key)}
    found = set()
    batch = f"{VERSIONS}\\{name}"
    stack = [""]
    while stack:
        relative = stack.pop()
        try:
            for entry in storage.entries(f"{batch}\\{relative}" if relative else batch):
                inner = f"{relative}\\{entry.name}" if relative else entry.name
                key = path_key(inner)
                if entry.problem or entry.is_link:
                    continue
                if entry.is_dir:
                    if key in folders:
                        stack.append(inner)
                elif key in wanted and entry.size == wanted[key].size and entry.mtime_ns == wanted[key].mtime_ns:
                    found.add(key)
        except OSError:
            pass
        progress()
    return PutBack(name, moved, frozenset(found)) if found else None
