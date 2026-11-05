"""What the window says about the job file when it opens. No toolkit.

The store's own load decides what happened (``LoadStatus``): a corrupt file has already
been moved aside, unchanged, and nothing replaced with defaults; a file from a newer
version is left untouched, and this window then opens read-only. This module only puts
that in words and lists the previous copies a user may restore. A file another program
keeps open past the store's patience (``StoreBusy``) is neither: the window asks to try
again or quit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from mirrorhold.jobs.store import LoadResult, LoadStatus

_STAMP = re.compile(r"\.(\d{8}T\d{6})(\d{6})?Z(?:-\d+)?$")


class StartupKind(str, Enum):
    CORRUPT = "corrupt"
    READ_ONLY = "read-only"


@dataclass(frozen=True)
class PreviousCopy:
    path: Path
    saved: str                  # when it was replaced, local time; "" when the name does not say


@dataclass(frozen=True)
class StartupProblem:
    kind: StartupKind
    title: str
    text: str
    copies: Tuple[PreviousCopy, ...] = ()


def _saved(path: Path) -> str:
    match = _STAMP.search(path.stem)
    if not match:
        return ""
    moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def startup_problem(result: LoadResult) -> Optional[StartupProblem]:
    if result.status is LoadStatus.CORRUPT:
        moved = result.moved_to.name if result.moved_to else "a file beside it"
        copies = tuple(PreviousCopy(p, _saved(p)) for p in result.previous_copies)
        return StartupProblem(StartupKind.CORRUPT, "The job file could not be read",
                              f"It was moved aside, unchanged, to {moved}, and nothing was replaced with defaults."
                              + (" Earlier copies:" if copies else " There is no earlier copy to restore."),
                              copies)
    if result.status is LoadStatus.UNSUPPORTED:
        return StartupProblem(StartupKind.READ_ONLY, "The job file belongs to a newer version",
                              "It was written by a newer version of MirrorHold and is left untouched. This window "
                              "opens read-only: runs in progress can be watched and controlled, but its jobs cannot "
                              "be read, edited or run here.")
    return None


HELD_TITLE = "The job file is in use"
HELD_TEXT = ("MirrorHold could not read its job file: another program kept it open. Close that program, then try "
             "again.")
"""Asked at start while another program keeps the job file past the store's patience."""


def status_note(result: LoadResult) -> str:
    """A line for the status bar: a migrated file, or nothing."""
    if result.status is LoadStatus.MIGRATED:
        return "The job file was read from an older format; the next save writes the current one."
    return ""
