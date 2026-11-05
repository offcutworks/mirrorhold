"""How this copy starts its own core and its own GUI — the one place that decides.

Four callers start the tool again: ``run.launch`` (a run or a full verification, as a
core process of its own), ``automation.tasks`` (what a scheduled task runs),
``installation`` (what the Start menu entry opens) and ``tray`` (the notification-area
icon an activity brings up, which opens the window in its turn). Each asks this module
for the interpreter and the arguments, so how a copy starts itself differs here alone:
every kind of copy today is an interpreter starting a package as a module (``-m``) in
the folder the packages sit in, a portable release included, whose interpreter is the
one it carries.

The windowless interpreter is ``pythonw.exe`` beside a console one: what a scheduled
task, the Start menu entry and the notification-area icon start, so that none shows a
terminal.

A fifth caller is the engine: an rclone configuration whose password the user chose to
have remembered is unlocked by a password command that is this copy's ``unlock`` command
(``unlock_command``). The engine runs it in whatever folder the engine itself was started
in, so that command cannot rely on ``-m`` finding the packages in its working folder: it
names the folder the packages sit in itself, in isolated mode (``-I``), so no variable or
working folder of the moment can put other code ahead of this copy's.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Union

from .locations import PROJECT_ROOT, as_reached

CORE_MODULE = "mirrorhold"
GUI_MODULE = "mirrorhold_gui"
TRAY_MODULE = "mirrorhold_gui.tray_view"
"""The notification-area icon's process: started by this copy's activities, never by the user."""

PathLike = Union[str, Path]


def core_arguments(*args: PathLike) -> List[str]:
    """The interpreter arguments that start the core's command line with ``args``."""
    return ["-m", CORE_MODULE, *(str(a) for a in args)]


def gui_arguments(*args: PathLike) -> List[str]:
    """The interpreter arguments that open the GUI with ``args``."""
    return ["-m", GUI_MODULE, *(str(a) for a in args)]


def tray_arguments(*args: PathLike) -> List[str]:
    """The interpreter arguments that bring up the notification-area icon with ``args``."""
    return ["-m", TRAY_MODULE, *(str(a) for a in args)]


def current_interpreter() -> Path:
    """The interpreter running this code."""
    return Path(sys.executable)


def windowless_interpreter(interpreter: Optional[PathLike] = None) -> Path:
    """``pythonw.exe`` beside the given (default: the running) interpreter, when it exists."""
    base = Path(interpreter) if interpreter else current_interpreter()
    windowless = base.with_name("pythonw.exe")
    return windowless if windowless.is_file() else base


def unlock_command(store_path: PathLike, *, interpreter: Optional[PathLike] = None,
                   root: PathLike = PROJECT_ROOT, sealed: Optional[PathLike] = None) -> List[str]:
    """The command — program and arguments — the engine runs to unlock the configuration
    with the password remembered beside the store at ``store_path``: this copy's ``unlock``
    command, on its windowless interpreter, from the folder ``root`` its packages sit in.
    ``sealed``: the seal it reads instead, a password on trial before it is remembered."""
    arguments = ["--store", str(as_reached(store_path)), "unlock"]
    if sealed is not None:
        arguments += ["--seal", str(as_reached(sealed))]
    code = (f"import sys; sys.path.insert(0, {str(Path(root))!r}); from {CORE_MODULE}.cli import main; "
            f"sys.exit(main({arguments!r}))")
    return [str(windowless_interpreter(interpreter)), "-I", "-c", code]
