"""What a copy ships beside its packages for the window to read: its licence, the
third-party notices a release carries, and the program's icon. No toolkit.

A portable release carries all three at the root of its folder, written there by the
build (``tools/build_release.py``, which names them through this module, so the window
and the build cannot disagree about where they are). The source installation's copy
carries the licence and the icon, which ``install`` puts there. A working copy carries
the licence only, and its windows draw the icon themselves (``mirrorhold.icon``). Neither
of the last two has notices to show: the interpreter and the engine they run are the
user's own.

The names are the core's (``mirrorhold.locations``), since the installer names the icon
in the Start menu entry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mirrorhold.locations import ICON_FILE_NAME, LICENCE_FILE_NAME, NOTICES_FILE_NAME, PROJECT_ROOT  # noqa: F401


def licence_path(root: Path = PROJECT_ROOT) -> Path:
    """The licence file of the copy whose packages sit in ``root``."""
    return Path(root) / LICENCE_FILE_NAME


def notices_path(root: Path = PROJECT_ROOT) -> Path:
    """The third-party notices of the copy whose packages sit in ``root``."""
    return Path(root) / NOTICES_FILE_NAME


def icon_path(root: Path = PROJECT_ROOT) -> Path:
    """The program's icon in the copy whose packages sit in ``root``."""
    return Path(root) / ICON_FILE_NAME


def read_text(path: Path) -> Optional[str]:
    """A shipped file's text; None when it is missing or cannot be read as text."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
