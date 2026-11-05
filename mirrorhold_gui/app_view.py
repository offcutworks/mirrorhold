"""The GUI's composition root: the display, the process's identity, the icon, the job file at
start, the main window, marked with its store so the notification-area icon can bring it
forward (``tray_view.mark_main_window``), clearing the store's reminders as it opens
(``mirrorhold.reminders``). Tk.

    pythonw -m mirrorhold_gui [--store FILE]

run from the project's folder, or ``main.py`` there from any folder. ``--store`` names
another configuration store; the default is the copy's own
(``mirrorhold.locations.default_store_path``: a portable release's lives in
``%LOCALAPPDATA%``).
"""
from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from typing import Optional, Sequence

from mirrorhold import reminders
from mirrorhold.jobs.store import StoreBusy

from .common_view import apply_icon, apply_style, claim_identity, prepare_display
from .main_view import MainWindow
from .prefs_view import offer_set_up_here
from .session import GuiSession
from .startup_model import startup_problem
from .startup_view import StartupChoice, ask_when_held, resolve_startup
from .tray_view import mark_main_window


def open_main_window(root: tk.Tk, session: GuiSession, *, refresh_interval: float = 4.0) -> Optional[MainWindow]:
    """Load the job file, settle what its state asks, and open the main window on
    ``root``; None when the user chose to quit. A job file another program keeps open is
    asked about until it can be read or the user quits. A portable release opened while
    another copy is set up then asks whether to use this copy instead."""
    apply_style(root)
    apply_icon(root)
    root.withdraw()
    while True:
        try:
            result = session.load()
            break
        except StoreBusy:
            if ask_when_held(root) is StartupChoice.QUIT:
                return None
    problem = startup_problem(result)
    if problem is not None and resolve_startup(root, session, problem) is StartupChoice.QUIT:
        return None
    window = MainWindow(root, session, refresh_interval=refresh_interval)
    window.show()
    reminders.clear(session.layout)
    mark_main_window(root, session.layout.root)
    if session.writable:
        offer_set_up_here(root, session)
    return window


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mirrorhold_gui", description="MirrorHold")
    parser.add_argument("--store", type=Path, default=None, help="the configuration store to use")
    args = parser.parse_args(argv)
    prepare_display()
    claim_identity()
    root = tk.Tk()
    if open_main_window(root, GuiSession(args.store)) is None:
        root.destroy()
        return 0
    root.mainloop()
    return 0
