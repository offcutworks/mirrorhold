"""What the window asks when the job file cannot be used at start. Tk."""
from __future__ import annotations

import tkinter as tk
from enum import Enum
from tkinter import messagebox, ttk

from mirrorhold.jobs.store import StoreError

from .common_view import PAD, em, size_to_content
from .session import GuiSession
from .startup_model import HELD_TEXT, HELD_TITLE, StartupKind, StartupProblem


class StartupChoice(str, Enum):
    CONTINUE = "continue"
    QUIT = "quit"


def ask_when_held(root: tk.Tk) -> StartupChoice:
    """Ask, before any window is open, whether to read the job file again or quit:
    another program keeps it open. CONTINUE tries again."""
    chosen = {"choice": StartupChoice.QUIT}
    w = tk.Toplevel(root)
    w.title(HELD_TITLE)
    w.resizable(False, False)
    frame = ttk.Frame(w, padding=em(w, 0.8))
    frame.grid(sticky="nsew")
    ttk.Label(frame, text=HELD_TEXT, wraplength=em(w, 22), justify="left").grid(row=0, column=0, sticky="w")
    buttons = ttk.Frame(frame)
    buttons.grid(row=1, column=0, sticky="e", pady=(em(w, 0.8), 0))

    def again():
        chosen["choice"] = StartupChoice.CONTINUE
        w.destroy()

    retry = ttk.Button(buttons, text="Try again", default="active", command=again)
    retry.grid(row=0, column=0)
    ttk.Button(buttons, text="Quit", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
    w.bind("<Return>", lambda e: again())
    w.bind("<Escape>", lambda e: w.destroy())
    w.protocol("WM_DELETE_WINDOW", w.destroy)
    size_to_content(w)
    w.grab_set()
    retry.focus_set()
    root.wait_window(w)
    return chosen["choice"]


def resolve_startup(root: tk.Tk, session: GuiSession, problem: StartupProblem) -> StartupChoice:
    """Show the problem; a corrupt file offers its earlier copies. Returns whether to go on."""
    if problem.kind is StartupKind.READ_ONLY:
        messagebox.showwarning(f"MirrorHold: {problem.title.lower()}", problem.text, parent=root)
        return StartupChoice.CONTINUE
    return _CorruptDialog(root, session, problem).choice


class _CorruptDialog:
    def __init__(self, root: tk.Tk, session: GuiSession, problem: StartupProblem):
        self.session = session
        self.choice = StartupChoice.QUIT
        w = self.window = tk.Toplevel(root)
        w.title(f"MirrorHold: {problem.title.lower()}")
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.8))
        frame.grid(sticky="nsew")
        ttk.Label(frame, text=problem.text, wraplength=em(w, 24), justify="left").grid(row=0, column=0, sticky="w")
        self.copy = tk.IntVar(value=0)
        self.copies = problem.copies
        for i, copy in enumerate(problem.copies):
            label = f"replaced {copy.saved}" if copy.saved else copy.path.name
            ttk.Radiobutton(frame, text=label, variable=self.copy, value=i).grid(row=1 + i, column=0, sticky="w",
                                                                              padx=(em(w, 1.2), 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=2 + len(problem.copies), column=0, sticky="e", pady=(em(w, 0.8), 0))
        restore = ttk.Button(buttons, text="Restore selected", default="active", command=self._restore)
        restore.grid(row=0, column=0)
        restore.state(["!disabled"] if problem.copies else ["disabled"])
        ttk.Button(buttons, text="Start with no jobs", command=self._fresh).grid(row=0, column=1, padx=(PAD, 0))
        ttk.Button(buttons, text="Quit", command=w.destroy).grid(row=0, column=2, padx=(PAD, 0))
        w.protocol("WM_DELETE_WINDOW", w.destroy)
        size_to_content(w)
        w.grab_set()
        root.wait_window(w)

    def _restore(self):
        copy = self.copies[self.copy.get()]
        try:
            self.session.restore(copy.path)
        except StoreError as exc:
            messagebox.showerror("Restore", str(exc), parent=self.window)
            return
        self.choice = StartupChoice.CONTINUE
        self.window.destroy()

    def _fresh(self):
        self.choice = StartupChoice.CONTINUE
        self.window.destroy()
