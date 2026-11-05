"""The files-not-backed-up window of one destination. Tk.

The list cuts a long path or reason at its column; the selected file — the first, as the
window opens — is shown whole beneath it, wrapped to the window's width."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .common_view import PAD, em, size_to_content
from .skipped_model import UnbackedReport, whole_words


class UnbackedWindow:
    def __init__(self, parent: tk.Misc, report: UnbackedReport):
        self.report = report
        w = self.window = tk.Toplevel(parent)
        w.title(report.title)
        frame = ttk.Frame(w, padding=em(w, 0.6))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text="\n".join(report.summary), wraplength=em(w, 26), justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD))
        self.tree = ttk.Treeview(frame, columns=("why", "runs"), height=min(18, max(6, len(report.rows))),
                                 selectmode="browse")
        self.tree.heading("#0", text="File")
        self.tree.heading("why", text="Why")
        self.tree.heading("runs", text="Runs in a row")
        self.tree.column("#0", width=em(w, 16), stretch=True)
        self.tree.column("why", width=em(w, 14), stretch=True)
        self.tree.column("runs", width=em(w, 5), stretch=False, anchor="e")
        for position, row in enumerate(report.rows):
            self.tree.insert("", "end", iid=str(position), text=row.path, values=(row.why, row.runs))
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        bar.grid(row=1, column=1, sticky="ns")
        self.whole = ttk.Label(frame, text="", wraplength=em(w, 26), justify="left")
        self.whole.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        self.whole.bind("<Configure>", lambda e: self.whole.configure(wraplength=max(em(w, 8), e.width)), add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selected())
        ttk.Label(frame, text=report.note, wraplength=em(w, 26), justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(PAD, 0))
        close = ttk.Button(frame, text="Close", default="active", command=w.destroy)
        close.grid(row=4, column=0, columnspan=2, sticky="e", pady=(PAD, 0))
        if report.rows:
            self.tree.selection_set("0")
            self._selected()
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def _selected(self):
        selection = self.tree.selection()
        self.whole.configure(text=whole_words(self.report.rows[int(selection[0])]) if selection else "")
