"""The Verify... dialog and a verification's details window. Tk.

*Verify...* on one destination asks before a verification starts, with the choice of its
new copies only, a full verification, or continuing its unfinished pass — saying why a pass
that failed did not finish; several destinations are asked about in one question instead
(``groups_model.verify_several``). A verification runs as a core process of its own and
shows in the Runs tab. The details window
lists the copies found to differ, says what the user can do about them — *Replace backup
copies...*, which asks before a repair starts (``repair_view``) — and what a repair
replaced since, and opens the log of the pass's latest sitting; nothing here changes a file.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from mirrorhold.wording import agreeing

from .common_view import PAD, ListWindow, em, modal, set_enabled, size_to_content
from .formatting import count, size
from .remote_view import can_open, open_place
from .session import Opener
from .verify_model import CHOOSE_FULL, CHOOSE_NEW, CONTINUE, VerificationDetails, VerifyDialog

SHOWN_DIFFER = 8


def _first_listed(n: int) -> str:
    """'; the first 1,000 are listed': what a list headed by a larger total holds."""
    return f"; the first {count(n)} {agreeing(n, 'is', 'are')} listed"


class VerifyWindow:
    def __init__(self, parent: tk.Misc, dialog: VerifyDialog, on_verify: Callable[[str], None]):
        """``on_verify`` receives the choice made (``verify_model.verify_request`` reads it)."""
        self.on_verify = on_verify
        w = self.window = tk.Toplevel(parent)
        w.title(dialog.title)
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.8))
        frame.grid(sticky="nsew")
        wrap = em(w, 24)
        indent = em(w, 1.4)
        ttk.Label(frame, text=dialog.intro, wraplength=wrap, justify="left").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=dialog.last, wraplength=wrap, justify="left").grid(row=1, column=0, sticky="w",
                                                                                 pady=(PAD, 0))
        row = 2
        if dialog.new_last:
            ttk.Label(frame, text=dialog.new_last, wraplength=wrap, justify="left").grid(row=row, column=0,
                                                                                         sticky="w")
            row += 1
        self.choice = tk.StringVar(value=dialog.choice)

        def option(text: str, value: str, notes=(), first: bool = False):
            nonlocal row
            ttk.Radiobutton(frame, text=text, variable=self.choice, value=value).grid(
                row=row, column=0, sticky="w", pady=(PAD if first else 0, 0))
            row += 1
            for note in notes:
                if note:
                    ttk.Label(frame, text=note, wraplength=wrap - indent, justify="left").grid(
                        row=row, column=0, sticky="w", padx=(indent, 0), pady=(0, PAD // 2))
                    row += 1

        option(dialog.new_copies, CHOOSE_NEW, (dialog.new_copies_note,), first=True)
        option(dialog.full, CHOOSE_FULL)
        if dialog.resume:
            option(dialog.resume, CONTINUE, (dialog.why, dialog.resume_note))
        ttk.Label(frame, text=dialog.note, wraplength=wrap, justify="left").grid(row=row, column=0, sticky="w",
                                                                                pady=(PAD, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 1, column=0, sticky="e", pady=(em(w, 0.8), 0))
        go = ttk.Button(buttons, text="Verify", default="active", command=self._verify)
        go.grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: self._verify())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        modal(w, parent)
        go.focus_set()

    def _verify(self):
        choice = self.choice.get()
        self.window.destroy()
        self.on_verify(choice)


class VerificationWindow:
    def __init__(self, parent: tk.Misc, details: VerificationDetails, opener: Opener, log: Optional[Path] = None,
                 on_repair: Optional[Callable[[Callable[[], None]], None]] = None):
        """``log``: the log of the latest process that worked on the pass, when it is kept;
        ``on_repair``: asks to replace the backup copies the pass found to differ, None while
        a repair of them cannot start; it is handed what to call once a repair has started."""
        self.details = details
        self.opener = opener
        self.log = log
        self.on_repair = on_repair
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
        if details.differ_line:
            style = "ProblemHeadline.TLabel" if details.differ_total else "TLabel"
            ttk.Label(frame, text=details.differ_line, wraplength=wrap, justify="left", style=style).grid(
                row=row, column=0, columnspan=2, sticky="w")
            row += 1
        if details.differ:
            tree = ttk.Treeview(frame, columns=("size",), height=min(SHOWN_DIFFER, len(details.differ)))
            font = tkfont.nametofont("TkDefaultFont")
            tree.heading("#0", text="File")
            tree.heading("size", text="Size")
            tree.column("#0", width=em(w, 18), stretch=True)
            tree.column("size", width=font.measure("1,023.9 MiB") + em(w, 1.0), stretch=False, anchor="e")
            for item in details.differ[:SHOWN_DIFFER]:
                tree.insert("", "end", text=item.path, values=(size(item.size),))
            tree.grid(row=row, column=0, sticky="nsew", pady=(PAD // 2, 0))
            frame.rowconfigure(row, weight=1)
            show = ttk.Button(frame, text="Show all...", command=self._show_differ)
            show.grid(row=row, column=1, sticky="ne", padx=(PAD, 0), pady=(PAD // 2, 0))
            set_enabled(show, details.differ_total > SHOWN_DIFFER)
            row += 1
        if details.repaired:
            ttk.Label(frame, text=details.repaired, wraplength=wrap, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(PAD // 2, 0))
            row += 1
        if details.advice:
            ttk.Label(frame, text=details.advice, wraplength=wrap, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(PAD, 0))
            row += 1
        if details.not_compared:
            ttk.Label(frame, text=details.not_compared, wraplength=wrap, justify="left").grid(
                row=row, column=0, sticky="w", pady=(PAD, 0))
            unreadable = ttk.Button(frame, text="Show all...", command=self._show_unreadable)
            unreadable.grid(row=row, column=1, sticky="ne", padx=(PAD, 0), pady=(PAD, 0))
            set_enabled(unreadable, bool(details.unreadable))
            row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(em(w, 0.6), 0))
        column = 0
        self.replace = None
        if details.differ_total:
            self.replace = ttk.Button(buttons, text="Replace backup copies...", command=self._repair)
            self.replace.grid(row=0, column=column, padx=(0, PAD))
            set_enabled(self.replace, on_repair is not None)
            column += 1
        open_folder = ttk.Button(buttons, text="Open folder", command=self._open)
        open_folder.grid(row=0, column=column)
        set_enabled(open_folder, can_open(details.destination_path))
        open_log = ttk.Button(buttons, text="Open log", command=self._open_log)
        open_log.grid(row=0, column=column + 1, padx=(PAD, 0))
        set_enabled(open_log, log is not None and log.is_file())
        close = ttk.Button(buttons, text="Close", default="active", command=w.destroy)
        close.grid(row=0, column=column + 2, padx=(PAD, 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def _repair(self):
        if self.on_repair is not None:
            self.on_repair(self._repair_started)

    def _repair_started(self):
        if self.replace is not None and self.replace.winfo_exists():
            set_enabled(self.replace, False)

    def _show_differ(self):
        d = self.details
        heading = f"{count(d.differ_total)} {agreeing(d.differ_total, 'differs', 'differ')}" + (
            _first_listed(len(d.differ)) if len(d.differ) < d.differ_total else "")
        ListWindow(self.window, f"Differ - {d.title}", heading, d.differ)

    def _show_unreadable(self):
        d = self.details
        heading = f"{count(d.unreadable_total)} could not be read" + (
            _first_listed(len(d.unreadable)) if len(d.unreadable) < d.unreadable_total else "")
        ListWindow(self.window, f"Not compared - {d.title}", heading, d.unreadable)

    def _open(self):
        open_place(self.window, self.opener, self.details.destination_path, "Open folder")

    def _open_log(self):
        try:
            self.opener(self.log)
        except OSError as exc:
            messagebox.showwarning("Open log", str(exc), parent=self.window)
