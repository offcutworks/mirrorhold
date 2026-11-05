"""What the engine said of the configuration and of each remote folder a job names: the
facts validation judges a remote destination by, gathered by ``remotes.reach``.

Validation itself never starts the engine: it is handed these facts, or none — the
window's import, for one — and then says that a remote's checks wait until it can be
reached, as it does for a drive that is not attached.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

from ..endpoints.endpoint import RemoteEndpoint
from ..engine.remote import RemoteFeatures

FLOOR_PRECISION_NS = 2 * 10 ** 9
"""The coarsest modification times a remote may keep: FAT's two seconds, the coarsest a
destination of this computer keeps, which the plan already compares within. A coarser
service would hide a change of the same size made within its precision."""


class SetupState(str, Enum):
    READY = "ready"            # the configuration was opened
    MISSING = "missing"        # the preferences name no configuration
    UNUSABLE = "unusable"      # it cannot be opened or unlocked, or the engine cannot be used


class Reach(str, Enum):
    REACHED = "reached"
    UNREACHABLE = "unreachable"    # the service could not be reached at all: the remote waits
    REFUSED = "refused"            # the service answered with a failure: the remote fails, with why


@dataclass(frozen=True)
class RemoteFacts:
    """One remote folder as the engine found it. ``reason``: the engine's, when not reached.
    ``features``: the remote's, known when reached and no file stands where the folder is
    named. ``space``: (free, total) where the service reports both."""

    reach: Reach
    reason: str = ""
    features: Optional[RemoteFeatures] = None
    is_file: bool = False
    space: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class RemoteSetup:
    """The configuration as the engine opened it, and the facts of each remote folder looked
    at. ``remotes``: the names of the remotes it holds; ``reason``: why it is not ready."""

    state: SetupState
    reason: str = ""
    remotes: Tuple[str, ...] = ()
    facts: Mapping[RemoteEndpoint, RemoteFacts] = field(default_factory=dict)


def floor_problem(features: RemoteFeatures) -> Optional[str]:
    """Why a remote cannot be a destination, as a clause, or None when it can: it must keep
    modification times no coarser than ``FLOOR_PRECISION_NS`` — the plan judges a change by
    size and time — and move a file within itself, by a move or by a copy and a delete,
    since every removal or replacement is a move into versions."""
    problems = []
    if not features.keeps_times:
        problems.append("it keeps no modification times")
    elif features.precision_ns > FLOOR_PRECISION_NS:
        problems.append(f"it keeps modification times only to {features.precision_ns / 10 ** 9:g} seconds, "
                        "coarser than the two seconds a change is judged by")
    if not features.moves_within:
        problems.append("it can neither move nor copy a file within itself, so a removed or replaced file could not "
                        "move into versions there")
    return "; ".join(problems) or None
