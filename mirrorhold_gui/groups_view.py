"""The New group and Rename group windows. Tk.

One name, checked as it is typed against the rule the store keeps
(``groups_model.group_name_problem``); the confirming button is enabled only while the
name can be used, and the reason it cannot is written beside the field.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from mirrorhold.jobs.store import StoreContents

from .common_view import PAD, em, grow_to_content, modal, set_enabled, size_to_content, tone_style
from .groups_model import group_name_problem
from .jobs_model import Tone


def ask_group_name(parent: tk.Misc, contents: StoreContents, *, title: str, button: str, current: str = "",
                   besides: Optional[str] = None, note: str = "") -> Optional[str]:
    """The name typed, stripped, once confirmed; None when the window is closed.
    ``besides``: the group being renamed."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.resizable(False, False)
    chosen = {"name": None}
    frame = ttk.Frame(window, padding=em(window, 0.8))
    frame.grid(sticky="nsew")
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text="Name:").grid(row=0, column=0, sticky="w", padx=(0, PAD))
    name = tk.StringVar(value=current)
    entry = ttk.Entry(frame, textvariable=name, width=36)
    entry.grid(row=0, column=1, sticky="ew")
    problem = ttk.Label(frame, text="", style=tone_style(Tone.ATTENTION), wraplength=em(window, 16), justify="left")
    problem.grid(row=1, column=1, sticky="w", pady=(PAD // 2, 0))
    row = 2
    if note:
        ttk.Label(frame, text=note, wraplength=em(window, 20), justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(PAD, 0))
        row += 1
    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(em(window, 0.8), 0))

    def check(*_) -> str:
        reason = group_name_problem(contents, name.get(), besides)
        shown = reason if name.get().strip() else ""        # an empty field is not yet a mistake
        if problem.cget("text") != shown:
            problem.configure(text=shown)
            if window.winfo_ismapped():
                grow_to_content(window)
        set_enabled(go, not reason)
        return reason

    def confirm(*_) -> None:
        if check():
            return
        chosen["name"] = name.get().strip()
        window.destroy()

    go = ttk.Button(buttons, text=button, default="active", command=confirm)
    go.grid(row=0, column=0)
    ttk.Button(buttons, text="Cancel", command=window.destroy).grid(row=0, column=1, padx=(PAD, 0))
    name.trace_add("write", check)
    check()
    window.bind("<Return>", confirm)
    window.bind("<Escape>", lambda e: window.destroy())
    size_to_content(window, parent=parent)
    modal(window, parent)
    entry.focus_set()
    entry.select_range(0, "end")
    parent.wait_window(window)
    return chosen["name"]
