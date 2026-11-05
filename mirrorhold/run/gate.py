"""The safety gate: whether a pair's plan may be applied, and how much of it.

Judged before anything is applied, on the plan's exact quantities (the engine's own
deletion limit counts no overwrites and stops part-way, so it is no gate):

1. **FAT32** — no file of 4 GiB or more is copied to a FAT destination: the copy
   would fail on every run. Stops the pair.
2. **Space** — the bytes the run will copy fit the destination's free space with a
   margin, less what other runs in progress still have to write on that drive.
   Removals free nothing: they move into versions on the same volume. Stops the pair.
   A destination that reports no free space — a remote whose service reports none — is
   not judged on it: a copy that does not fit there fails file by file, recorded.
3. **First run** — a pair's first run (its destination has no marker yet) waits whole
   for the user's confirmation, and nothing confirms it but the user: either a preview's
   ticket, or — onto a destination that holds nothing (``PairPlan.destination_empty``)
   — the user's go-ahead for a first backup (``first_backup``), which needs no preview
   because nothing there can be moved or replaced. That go-ahead covers the destination
   as it was: one that holds something when the run lists it waits as if unconfirmed. A
   destination holding something is adopted only through a ticket, and its waiting
   first run says so in its reasons (``HOLDS_CONTENT_REASON``). A ticket confirms
   exactly the removals it names: a first run whose plan holds one it does not name —
   the destination gained something since the preview — waits whole again, nothing
   written, rather than claim a destination the user has not seen.
4. **Removals** — the files the run would move into versions (deleted at the source or
   overwritten) are held when they number more than the larger of the job's count
   floor and its percentage of the destination's files; or when they reach the job's
   share of the destination's files (half unless the user changed it, and a hold the
   user may switch off); or when any changed file lost its file signature, whatever the
   job's values. Removals the user confirmed in a preview's ticket are not judged again.

A shared destination's plan covers only its job's part (``planning.sharing``), so its
files are what the counts and shares above are taken of; whether a first run onto it
adopts what the destination holds is then given (``holds_content``), the plan alone
cannot tell.

While removals are held, the run copies its additions and nothing else. Identity and
marker checks (one present volume per endpoint, a matching marker) come before the
gate; the gate judges a resolved pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Tuple, Union

from ..endpoints.volumes import FAT_MAX_FILE_BYTES, VolumeInfo
from ..jobs.model import HoldThresholds
from ..planning.plan import PairPlan
from ..records.record import FIRST_RUN_REASON, HOLDS_CONTENT_REASON
from ..wording import counted

GiB = 1024 ** 3
SPACE_RESERVE_BYTES = 1 * GiB
SPACE_PER_FILE_BYTES = 64 * 1024
"""The margin a copy needs beyond its bytes: per file, up to one allocation cluster of
slack and its file record, taken together as 64 KiB; per run, a reserve for the
engine's temporary files and the records."""


@dataclass(frozen=True)
class Space:
    """A destination that is not a volume of this computer, as the gate judges its room: the
    free bytes it reports, None where it reports none, and no file system of this
    computer's, so no FAT limit. A volume of this computer is judged as it is
    (``VolumeInfo``)."""

    free_bytes: Optional[int]
    file_system: str = ""
    is_fat: bool = False


class Verdict(str, Enum):
    PROCEED = "proceed"
    ADDITIONS_ONLY = "additions only"          # removals held
    AWAIT_CONFIRMATION = "awaiting confirmation"  # a first run, held whole
    STOP = "stop"


class Hold(str, Enum):
    FIRST_RUN = FIRST_RUN_REASON
    ABOVE_THRESHOLD = "above-threshold"
    SHARE_OF_DESTINATION = "share-of-destination"
    LOST_SIGNATURES = "lost-signatures"
    NOT_CONFIRMED = "not-in-confirmed-plan"


class Stop(str, Enum):
    FAT32_LARGE_FILE = "fat32-large-file"
    INSUFFICIENT_SPACE = "insufficient-space"


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    holds: Tuple[Hold, ...] = ()
    stop: Optional[Stop] = None
    detail: str = ""
    first_run: bool = False
    adoption: bool = False          # a first run onto a destination that already holds something
    removals: int = 0               # judged (not confirmed) removals
    confirmed_removals: int = 0
    threshold: float = 0.0
    bytes_needed: int = 0
    free_bytes: Optional[int] = 0   # None: the destination reports none, and its space was not judged
    share: Optional[float] = None   # the job's share hold, in per cent; None while it is off

    @property
    def applies_removals(self) -> bool:
        return self.verdict is Verdict.PROCEED

    @property
    def applies_additions(self) -> bool:
        return self.verdict in (Verdict.PROCEED, Verdict.ADDITIONS_ONLY)

    @property
    def reasons(self) -> Tuple[str, ...]:
        """The codes a record keeps: the holds, a waiting adoption's note that the
        destination already holds something, and the stop."""
        content = (HOLDS_CONTENT_REASON,) if self.verdict is Verdict.AWAIT_CONFIRMATION and self.adoption else ()
        return tuple(h.value for h in self.holds) + content + ((self.stop.value,) if self.stop else ())

    def describe(self) -> str:
        if self.verdict is Verdict.PROCEED:
            return "proceeds"
        if self.verdict is Verdict.STOP:
            return f"stopped: {self.detail}"
        text = "; ".join(self._hold_text(h) for h in self.holds)
        if self.verdict is Verdict.AWAIT_CONFIRMATION:
            if self.adoption:
                text += "; the destination already holds files or folders"
            return f"waits for confirmation: {text}"
        return f"additions only, removals held: {text}"

    def _hold_text(self, hold: Hold) -> str:
        if hold is Hold.SHARE_OF_DESTINATION:
            return f"{share_words(self.share)} of the destination's files would move into versions"
        return _HOLD_TEXT[hold]


_HOLD_TEXT = {
    Hold.FIRST_RUN: "the first run of this pair waits for confirmation",
    Hold.ABOVE_THRESHOLD: "more files would move into versions than the job's threshold allows",
    Hold.LOST_SIGNATURES: "changed files lost their file-format signature (as encryption leaves them)",
    Hold.NOT_CONFIRMED: "removals beyond the confirmed plan",
}


def share_words(share: Optional[float]) -> str:
    """'half or more', '30 % or more': the share hold's bar in words."""
    return "half or more" if share == 50 else f"{share:g} % or more"


def space_needed(bytes_to_copy: int, files_to_copy: int) -> int:
    if not files_to_copy:
        return 0
    return bytes_to_copy + files_to_copy * SPACE_PER_FILE_BYTES + SPACE_RESERVE_BYTES


def judge(plan: PairPlan, *, holds: HoldThresholds, first_run: bool, destination: Union[VolumeInfo, Space],
          confirmed: Optional[FrozenSet[str]] = None, reserved: int = 0, first_backup: bool = False,
          holds_content: Optional[bool] = None) -> GateDecision:
    """``destination``: the destination's volume, or a remote's ``Space``;
    ``confirmed``: the removal keys a ticket confirmed for this pair, or None without a ticket;
    ``reserved``: the bytes, margin included, other runs in progress still have to write on
    the destination's drive, counted as used; ``first_backup``: the user's go-ahead for a
    first run onto a destination that holds nothing, which confirms it only while it does;
    ``holds_content``: whether the destination holds anything, when the plan covers only
    the job's part of it."""
    adoption = first_run and (not plan.destination_empty if holds_content is None else holds_content)
    if first_run and confirmed is None and first_backup and not adoption:
        confirmed = frozenset()
    common = dict(first_run=first_run, adoption=adoption, free_bytes=destination.free_bytes, share=holds.share)

    if destination.is_fat and plan.largest_copy > FAT_MAX_FILE_BYTES:
        return GateDecision(Verdict.STOP, stop=Stop.FAT32_LARGE_FILE,
                            detail=f"a file of {counted(plan.largest_copy, 'byte')} cannot be stored on "
                                   f"{destination.file_system}", **common)

    removal_keys = plan.removal_keys()
    granted = removal_keys & confirmed if confirmed is not None else frozenset()
    judged = removal_keys - granted
    reasons = []
    threshold = max(float(holds.count_floor), holds.percent / 100.0 * plan.destination_files)
    if first_run and confirmed is None:
        reasons.append(Hold.FIRST_RUN)
    elif first_run:
        if judged:
            reasons += [Hold.FIRST_RUN, Hold.NOT_CONFIRMED]
    else:
        count = len(judged)
        if count > threshold:
            reasons.append(Hold.ABOVE_THRESHOLD)
        if count and holds.share is not None and count * 100 >= holds.share * plan.destination_files:
            reasons.append(Hold.SHARE_OF_DESTINATION)
        if plan.signature_losses & judged:
            reasons.append(Hold.LOST_SIGNATURES)

    if Hold.FIRST_RUN in reasons:
        verdict = Verdict.AWAIT_CONFIRMATION
        needed = space_needed(plan.bytes_to_copy, plan.files_to_copy)
    elif reasons:
        verdict = Verdict.ADDITIONS_ONLY
        needed = space_needed(plan.bytes_new, len(plan.new))
    else:
        verdict = Verdict.PROCEED
        needed = space_needed(plan.bytes_to_copy, plan.files_to_copy)
    if destination.free_bytes is not None and needed > destination.free_bytes - reserved:
        others = (f", {counted(reserved, 'byte')} of them still to be written by other runs in progress"
                  if reserved else "")
        return GateDecision(Verdict.STOP, stop=Stop.INSUFFICIENT_SPACE,
                            detail=f"the run needs {counted(needed, 'byte')} with its margin and the destination has "
                                   f"{counted(destination.free_bytes, 'byte')} free{others}", removals=len(judged),
                            confirmed_removals=len(granted), threshold=threshold, bytes_needed=needed, **common)
    return GateDecision(verdict, holds=tuple(reasons), removals=len(judged), confirmed_removals=len(granted),
                        threshold=threshold, bytes_needed=needed, **common)
