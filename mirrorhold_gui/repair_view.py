"""The question before a repair, and a repair's details window. Tk.

*Replace backup copies...* asks one question (``repair_model.replace_question``): what
replacing does, how long the job keeps versions while its cleanup is on, and what the
verification found beyond its list; its *Replace* button's hint says which copies are left
as they are. The repair itself runs as a core process of its own and shows in the Runs tab.
A repair's details window lists the copies it failed to replace, under a heading in the
problem style, then those it left as they are, on purpose, under a plain bold one, each with
why, and opens its versions batch and its log; nothing here changes a file.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .common_view import PAD, Hint, ListWindow, em, modal, set_enabled, size_to_content, tone_style
from .formatting import count
from .jobs_model import Tone
from .remote_view import can_open, open_place
from .repair_model import RepairDetails, ReplaceQuestion
from .session import Opener

SHOWN_LEFT = 3


class ReplaceWindow:
    def __init__(self, parent: tk.Misc, question: ReplaceQuestion, on_replace: Callable[[], None]):
        self.on_replace = on_replace
        w = self.window = tk.Toplevel(parent)
        w.title(question.title)
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.8))
        frame.grid(sticky="nsew")
        wrap = em(w, 24)
        ttk.Label(frame, text=question.text, wraplength=wrap, justify="left").grid(row=0, column=0, sticky="w")
        row = 1
        for line in (question.versions, question.unlisted):
            if line:
                ttk.Label(frame, text=line, wraplength=wrap, justify="left").grid(row=row, column=0, sticky="w",
                                                                                  pady=(PAD, 0))
                row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, sticky="e", pady=(em(w, 0.8), 0))
        go = ttk.Button(buttons, text="Replace", default="active", command=self._replace)
        go.grid(row=0, column=0)
        Hint(go, question.hint)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: self._replace())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        modal(w, parent)
        go.focus_set()

    def _replace(self):
        self.window.destroy()
        self.on_replace()


class RepairWindow:
    def __init__(self, parent: tk.Misc, details: RepairDetails, opener: Opener, log: Optional[Path] = None):
        """``log``: the log of the process that made the repair, when it is kept."""
        self.details = details
        self.opener = opener
        self.log = log
        w = self.window = tk.Toplevel(parent)
        w.title(details.title)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        wrap = em(w, 28)
        ttk.Label(frame, text=details.summary, wraplength=wrap, justify="left",
                  style="Headline.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD))
        row = 1
        row = self._group(frame, row, wrap, "Failed:", details.failed, self._show_failed,
                          tone_style(Tone.PROBLEM, headline=True))
        row = self._group(frame, row, wrap, "Left as they are:", details.left, self._show_left,
                          tone_style(Tone.NORMAL, headline=True))
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(em(w, 0.6), 0))
        versions = ttk.Button(buttons, text="Open versions", command=self._open_versions)
        versions.grid(row=0, column=0)
        set_enabled(versions, can_open(details.versions_path))
        open_log = ttk.Button(buttons, text="Open log", command=self._open_log)
        open_log.grid(row=0, column=1, padx=(PAD, 0))
        set_enabled(open_log, log is not None and log.is_file())
        close = ttk.Button(buttons, text="Close", default="active", command=w.destroy)
        close.grid(row=0, column=2, padx=(PAD, 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def _group(self, frame: ttk.Frame, row: int, wrap: int, heading: str, items, show_all, style: str) -> int:
        """A heading in ``style`` and the first copies of one group, with *Show all...* beside
        the last; nothing for an empty group. Returns the next free row."""
        if not items:
            return row
        w = self.window
        ttk.Label(frame, text=heading, style=style).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        for item in items[:SHOWN_LEFT]:
            ttk.Label(frame, text=f"{item.path} - {item.why}", wraplength=wrap, justify="left").grid(
                row=row, column=0, sticky="w", padx=(em(w, 0.6), 0))
            row += 1
        show = ttk.Button(frame, text="Show all...", command=show_all)
        show.grid(row=row - 1, column=1, sticky="ne", padx=(PAD, 0))
        set_enabled(show, len(items) > SHOWN_LEFT)
        return row

    def _show_failed(self):
        d = self.details
        ListWindow(self.window, f"Failed - {d.title}", f"{count(len(d.failed))} failed", d.failed)

    def _show_left(self):
        d = self.details
        ListWindow(self.window, f"Left as they are - {d.title}", f"{count(len(d.left))} left as they are", d.left)

    def _open_versions(self):
        open_place(self.window, self.opener, self.details.versions_path, "Open versions")

    def _open_log(self):
        try:
            self.opener(self.log)
        except OSError as exc:
            messagebox.showwarning("Open log", str(exc), parent=self.window)
