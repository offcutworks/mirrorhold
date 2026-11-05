"""The notification-area icon while an activity works, or a finished one's reminder stands: what
it shows, what its menu offers and when its process ends. No toolkit.

The icon's process (``tray_view``) reads every activity's status as the Runs tab does
(``refresh.RunWatcher``) and takes each one's words from the Runs tab's own panel
(``runs_model.live_panels``), so the icon never says what the Runs tab would not, and offers a
request only where the Runs tab's row offers it — none for an activity whose status cannot be
read now. Its look (``tray_look``):

- **the picture** — the program's mark, its outlined block (the copy) filled from the bottom by
  the activities' progress: the mean of the fractions of those whose progress can be measured,
  empty while none can (waiting, starting, before a plan); its tile amber while a reminder
  stands, else grey while every activity at work is paused;
- **the tooltip** — ``MirrorHold`` (``MirrorHold - 3 at work``, ``MirrorHold - 1 at work, 2 to
  look at`` for several), then a line per activity in line order: what it is and how far it has
  come, then a line per reminder: the title of the notice that left it. Windows shows at most
  ``TIP_LIMIT`` UTF-16 code units, so a line too long is shortened and the lines that no longer
  fit are counted on a last line;
- **the menu** — *Open MirrorHold*, then for each activity a line naming it with its pause and
  cancel entries, worded as the row's buttons (``runs_model.pause_words``, ``cancel_words``),
  then each reminder's line and the entry that dismisses them (``dismiss_words``).

A reminder (``mirrorhold.reminders``) is what a finished activity whose notice asks something of
the user left; the window clears them as it opens and while it has the focus, and with them the
icon goes. ``Lifetime`` ends the process ``LINGER_SECONDS`` after the last activity has ended and
no reminder stands, judged by the liveness locks the watcher reads (an activity whose process was
killed has ended too) and the reminders read with each look; a look that failed leaves the icon
as it was, and ends the process once looks have failed for ``FAILING_SECONDS`` running. Ending,
the process first hands the store's icon lock back and looks once more (``hand_back``), so no
activity and no reminder is left without an icon. ``MissingWatch`` makes the icon again when
Windows has refused it twice running: Tk adds an icon again after the notification area restarts
only where its first add succeeded.

*Open MirrorHold* brings forward the main window open on the same store, or opens one. A main
window marks itself with ``WINDOW_PROPERTY`` holding ``window_key`` of its store's folder, which
is how the icon finds it among the windows on the desktop.
"""
from __future__ import annotations

import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from mirrorhold.records.status import ANY_KIND, UNREADABLE, ObservedRun, is_repair, is_verification

from .formatting import duration
from .runs_model import NO_PLACES, Places, RunPanel, destination_number, live_panels, of_total

TITLE = "MirrorHold"
TIP_LIMIT = 127
"""The UTF-16 code units Windows shows of a notification-area icon's tooltip."""

LINGER_SECONDS = 4.0
"""How long the icon stays after the last activity has ended and the last reminder was cleared."""

FAILING_SECONDS = 30.0
"""How long looks may fail running before the icon's process ends anyway."""

CHECK_SECONDS = 5.0
"""How often the icon's process asks Windows whether it holds the icon."""

OPEN_PATIENCE = 15.0
"""Seconds after *Open MirrorHold* started a window during which, its window not found yet, it
starts none again: a window takes a few seconds to open."""

WINDOW_PROPERTY = "MirrorHold.Store"
"""The property a main window carries, naming its store's folder by ``window_key``."""


@dataclass(frozen=True)
class TrayEntry:
    """One activity as the icon shows it: its line, and its Runs tab panel for what may be asked."""
    line: str
    panel: RunPanel


@dataclass(frozen=True)
class TrayLook:
    entries: Tuple[TrayEntry, ...] = ()
    tooltip: str = TITLE
    fill: float = 0.0          # 0 to 1: how much of the copy is filled
    paused: bool = False       # every activity at work is paused: the tile is grey
    reminders: Tuple[str, ...] = ()     # one line per standing reminder: the tile is amber

    @property
    def working(self) -> bool:
        """Whether any activity is alive, waiting ones included."""
        return bool(self.entries)

    @property
    def needed(self) -> bool:
        """Whether the icon is wanted: an activity alive or a reminder standing."""
        return self.working or bool(self.reminders)


def activity_title(status: Optional[dict], places: Places = NO_PLACES) -> str:
    """'Backing up Photos -> 1 of 2', 'Backing up Photos -> 3', 'Verifying Photos -> 1',
    'Replacing copies of Photos -> 1': what an activity is, from its status (one whose status
    cannot be read now: the last read of it), its destination numbered as ``places`` says its
    job stands now (``runs_model.destination_number``), with the job's count where it agrees
    (``runs_model.of_total``); 'Run, verification or repair' for one never read."""
    if not status:
        return ANY_KIND.capitalize()
    job = status.get("job_name", "")
    if is_verification(status):
        title = f"Verifying {job}"
    elif is_repair(status):
        title = f"Replacing copies of {job}"
    else:
        title = f"Backing up {job}"
    index = destination_number(status, places)
    if index:
        title += f" -> {index}{of_total(status, index)}"
    return title


def progress_words(panel: RunPanel, status: dict) -> str:
    """'42 %, about 12 min left', 'paused at 42 %', 'waiting for 'Work'', 'listing the source,
    1,234 entries': how far an activity has come, in the Runs tab's words."""
    if not panel.readable:
        return UNREADABLE
    if panel.waiting or panel.cancelling is not None:
        return _flat(panel.phase)
    percent = f"{int(100 * panel.fraction)} %" if panel.fraction is not None else ""
    if panel.paused:
        return f"paused at {percent}" if percent else "paused"
    if percent:
        eta = status.get("eta_seconds")
        return percent + (f", about {duration(float(eta))} left" if eta is not None else "")
    return _flat(panel.phase)


def _flat(words: str) -> str:
    """A row's phase on one line: the gap the row leaves before a count becomes a comma."""
    return re.sub(r"\s{2,}", ", ", words.strip())


def units(text: str) -> int:
    """The length Windows counts a tooltip in: UTF-16 code units, two for a character beyond
    the Basic Multilingual Plane (an emoji in a job's name)."""
    return len(text.encode("utf-16-le")) // 2


def _clipped(line: str, limit: int) -> str:
    if units(line) <= limit:
        return line
    kept = ""
    for character in line:
        if units(kept + character) > limit - 3:
            break
        kept += character
    return kept.rstrip() + "..."


def tooltip(lines: Sequence[str], reminders: Sequence[str] = ()) -> str:
    """The tooltip for ``lines``, one per activity, then ``reminders``, one per standing
    reminder, within ``TIP_LIMIT`` code units."""
    if not lines and not reminders:
        return TITLE
    counts = ([f"{len(lines)} at work"] if lines else []) + ([f"{len(reminders)} to look at"] if reminders else [])
    head = TITLE if len(lines) + len(reminders) == 1 else f"{TITLE} - {', '.join(counts)}"
    lines = [_clipped(line, TIP_LIMIT - units(head) - 1) for line in (*lines, *reminders)]
    for shown in range(len(lines), -1, -1):
        rest = len(lines) - shown
        text = "\n".join([head, *lines[:shown]] + ([f"and {rest} more"] if rest else []))
        if units(text) <= TIP_LIMIT:
            return text
    return _clipped(head, TIP_LIMIT)


def tray_look(runs: Sequence[ObservedRun], reminders: Sequence[str] = (), places: Places = NO_PLACES) -> TrayLook:
    """What the icon shows for the activities observed — the live ones, in line order — and the
    standing reminders, one line each; each destination numbered as ``places`` says its job
    stands now."""
    observed: Dict[str, ObservedRun] = {r.run_id: r for r in runs if r.alive}
    entries: List[TrayEntry] = []
    for panel in live_panels(runs, places):
        found = observed[panel.run_id]
        status = found.status if found.readable else found.last
        entries.append(TrayEntry(f"{activity_title(status, places)}: {progress_words(panel, found.status)}", panel))
    measured = [e.panel.fraction for e in entries
                if e.panel.readable and not e.panel.waiting and e.panel.fraction is not None]
    working = [e.panel for e in entries if e.panel.readable and not e.panel.waiting]
    return TrayLook(tuple(entries), tooltip([e.line for e in entries], reminders),
                    sum(measured) / len(measured) if measured else 0.0,
                    bool(working) and all(p.paused for p in working), tuple(reminders))


def dismiss_words(count: int) -> str:
    """The menu entry that clears the standing reminders without opening the window."""
    return "Dismiss reminder" if count == 1 else "Dismiss reminders"


class Lifetime:
    """When the icon's process ends: ``linger`` seconds after the last look that found the icon
    needed (``TrayLook.needed``), or once looks have failed for ``failing`` seconds running —
    whether an activity works can then not be told, and the process must not stay on that
    account."""

    def __init__(self, linger: float = LINGER_SECONDS, failing: float = FAILING_SECONDS):
        self.linger = linger
        self.failing = failing
        self.idle_since: Optional[float] = None
        self.failing_since: Optional[float] = None

    def on_failure(self, now: float) -> bool:
        """A look that failed; whether the process should end now."""
        if self.failing_since is None:
            self.failing_since = now
        return now - self.failing_since >= self.failing

    def on_look(self, needed: bool, now: float) -> bool:
        """One look; whether the process should end now."""
        self.failing_since = None
        if needed:
            self.idle_since = None
            return False
        if self.idle_since is None:
            self.idle_since = now
        return now - self.idle_since >= self.linger


def hand_back(lock, look_again: Callable[[], bool]) -> bool:
    """Let the icon lock go, then ask ``look_again`` whether the icon is still needed — an
    activity alive or a reminder standing: whether this process keeps the icon — needed at that
    look and the lock held again. An activity whose status was published before the look, or a
    reminder written before it, is seen by it; one published or written after it finds the lock
    free and brings up a process of its own. A look that fails finds none, so a store whose
    statuses cannot be read never keeps the process: the next activity to start brings it up."""
    lock.release()
    try:
        alive = look_again()
    except Exception:  # cannot tell: the process does not stay on that account
        alive = False
    return alive and lock.try_acquire()


class MissingWatch:
    """Windows' answers whether it holds the icon: refused twice running, the icon is made again,
    and again at each refusal after; None — the question cannot be asked — changes nothing."""

    def __init__(self) -> None:
        self.refusals = 0

    def on_check(self, present: Optional[bool]) -> bool:
        """One answer; whether to make the icon again."""
        if present is None:
            return False
        if present:
            self.refusals = 0
            return False
        self.refusals += 1
        return self.refusals >= 2


def icon_id(handle: str) -> Optional[int]:
    """The id Windows knows Tk's icon by, from the handle Tk gives it (``ico#2``); None for
    anything else."""
    prefix, _, number = handle.partition("#")
    return int(number) if prefix == "ico" and number.isdigit() else None


def window_key(layout_root: Path) -> int:
    """The number a main window open on the store whose runtime files are in ``layout_root``
    carries in ``WINDOW_PROPERTY``: never 0, which Windows answers for a window without it."""
    return (zlib.crc32(os.path.normcase(str(Path(layout_root))).encode("utf-8")) & 0x7FFFFFFF) or 1
