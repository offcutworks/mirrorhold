"""The notification-area icon while an activity works: brought up by the activity, owned by the
window's unit.

While a run, a full verification or a repair works, an icon in Windows' notification area
shows how far it has come and offers the window, pause, resume and cancel. The icon belongs to
the window's unit (``mirrorhold_gui.tray_view``) and lives in a short-lived process of its own:
one per store, showing every activity of that store at once, and ending a few seconds after
the last one has ended — unless a finished activity left a reminder (``reminders``), which keeps
it until the user opens the window. A fault in it costs the icon and never the work, as a
window's failure never stops a run. Nothing of it starts at sign-in.

An activity that leaves a reminder as it ends brings the icon up again (``offer``), after the
reminder is written, so the icon reaches a reminder however the two meet.

Every activity brings it up as it opens its status (``offer``), so an activity started in any
way — the window, the command line, a scheduled task, one that then waits its turn — has the
icon. ``offer`` starts the icon's process only while no process holds the store's
``RuntimeLayout.tray_lock``; two activities starting together may each start one, and the
second to begin finds the lock taken and ends at once. An icon process that finds no activity
and no reminder left lets the lock go and then looks once more: an activity whose status was
published before that look, or a reminder written before it, is seen by it, and one published
or written after it finds the lock free and starts a process of its own — so no activity and no
reminder is left without an icon.

The process is started as a run is (``run.launch``): the copy's own windowless interpreter, the
package as a module from the copy's folder (``invocation``), no console, a process group of its
own, outside the caller's job where the job allows it — so the activity that started it can end,
or be killed, without taking the icon with it.

A courtesy, never a condition: a process that cannot be started is said in the activity's
diagnostic log and the activity goes on. ``SWITCH_VARIABLE`` set to ``off`` makes a process, and
every activity it starts, bring up no icon; the test suite sets it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .invocation import PathLike, tray_arguments, windowless_interpreter
from .locations import PROJECT_ROOT, RuntimeLayout, as_reached
from .records import diagnostics
from .records.lock import is_held_elsewhere

SWITCH_VARIABLE = "MIRRORHOLD_TRAY"
"""``off`` makes this process, and every activity it starts, bring up no notification-area icon."""

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000 if sys.platform == "win32" else 0
_ACCESS_DENIED = 5

Start = Callable[[Sequence[str], Path], object]


def switched_off() -> bool:
    return os.environ.get(SWITCH_VARIABLE, "").strip().lower() == "off"


def tray_command(store_path: PathLike, *, interpreter: Optional[PathLike] = None) -> List[str]:
    """The command — program and arguments — that brings up the icon for the store at
    ``store_path``: this copy's windowless interpreter, the path made absolute here, since the
    process starts in the copy's folder."""
    return [str(windowless_interpreter(interpreter))] + tray_arguments("--store", as_reached(store_path))


def start_detached(command: Sequence[str], root: Path) -> subprocess.Popen:
    """Start ``command`` in ``root`` with no console, in a process group of its own and outside
    the caller's job where the job allows it, its output discarded; return at once."""
    flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    options = dict(cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    try:
        return subprocess.Popen(list(command), creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **options)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ACCESS_DENIED:
            raise
        return subprocess.Popen(list(command), creationflags=flags, **options)


def offer(layout: RuntimeLayout, store_path: PathLike, *, interpreter: Optional[PathLike] = None,
          root: Path = PROJECT_ROOT, start: Optional[Start] = None) -> bool:
    """Bring up the icon's process for the store at ``store_path``, whose runtime files are
    ``layout``, unless the switch is off or a process holds the store's icon lock; whether one
    was started. Called once the activity's status is published. Never raises."""
    if switched_off():
        return False
    try:
        if is_held_elsewhere(layout.tray_lock) is True:
            return False
        (start or start_detached)(tray_command(store_path, interpreter=interpreter), Path(root))
        return True
    except Exception as exc:  # the icon is a courtesy: the activity goes on without it
        diagnostics.note(f"the notification-area icon could not be brought up: {type(exc).__name__}: {exc}")
        return False
