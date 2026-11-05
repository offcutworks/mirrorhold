"""The History tab, a run's details window and a start's details window. Tk.

History shows the selection's runs, full verifications, repairs and starts that left no
record together, newest first (``history_model.selection_entries``), with a *Job* column
while it shows more than one job. A verification's row opens its own details window
(``verify_view``), from which a repair of the copies it found to differ can be asked for;
a repair's row opens its details (``repair_view``); a start's row says why it could not
start. Each details window opens the log of the process that wrote it, while that log is
kept."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Collection, Dict, List, Optional, Sequence, Tuple

from mirrorhold.maintenance.repair import RepairRecord
from mirrorhold.maintenance.verification import VerificationRecord
from mirrorhold.records.diagnostics import ActivityLog

from .common_view import PAD, ListWindow, em, set_enabled, size_to_content, tone_style
from .history_model import (Entry, HistoryRow, JobHistory, RecordDetails, StartDetails, log_of, record_details,
                            selection_entries, start_details)
from .jobs_model import Root, Tone
from .preview_model import ListedFile
from .remote_view import can_open, open_place
from .repair_model import repair_details
from .repair_view import RepairWindow
from .session import Opener
from .verify_model import verification_details
from .verify_view import VerificationWindow

_COLUMNS = (("job", "Job", "Photos and music"), ("started", "Started", "2026-10-06 00:00"),
            ("destination", "Destination", "1 Backup drive"), ("how", "How started", "automatic"),
            ("result", "Result", "Waiting for first-run confirmation"),
            ("copied", "Copied", "22,230 compared / 338.0 GiB"), ("into", "Into versions", "1,840"),
            ("renamed", "Renamed", "312"), ("skipped", "Skipped", "12"), ("took", "Took", "1.2 h"))
"""(column, heading, a typical value): each column is as wide as the wider of the two."""

Roots = Callable[[str, int], Optional[Root]]
"""A destination's folder now, by job and destination index; None when it is not there."""

Repair = Callable[[VerificationRecord], Optional[Callable[[Callable[[], None]], None]]]
"""What asks to replace the copies a verification found to differ, handed what to call once
a repair has started; None while a repair of them cannot start."""


class HistoryPanel:
    def __init__(self, parent, opener: Opener, repair: Repair = lambda verification: None):
        self.opener = opener
        self.repair = repair
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.frame, columns=[c for c, _, _ in _COLUMNS], show="headings", height=6,
                                 selectmode="browse")
        font = tkfont.nametofont("TkDefaultFont")
        for column, title, sample in _COLUMNS:
            self.tree.heading(column, text=title)
            numeric = column in ("into", "renamed", "skipped", "took")
            self.tree.column(column, width=max(font.measure(title), font.measure(sample)) + em(parent, 1.0),
                             stretch=column in ("job", "destination", "result"), anchor="e" if numeric else "w")
        bar = ttk.Scrollbar(self.frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.details = ttk.Button(self.frame, text="Details...", command=self._open)
        self.details.grid(row=1, column=0, sticky="e", pady=(PAD, 0))
        self.empty = ttk.Label(self.frame, text="")
        self.empty.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.tree.bind("<Double-1>", lambda e: self._open())
        self.tree.bind("<Return>", lambda e: self._open())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._buttons())
        self._entries: Dict[str, Tuple[Entry, JobHistory]] = {}
        self._roots: Roots = lambda job_id, index: None
        self._live: Collection[str] = ()
        self._logs: Sequence[ActivityLog] = ()
        self._shown = None
        self._job_column = True
        self._result_width = int(self.tree.column("result", "width"))
        self.rows: Tuple[HistoryRow, ...] = ()
        self._buttons()

    def _show_job_column(self, shown: bool) -> None:
        """Show or hide the *Job* column, its width given to *Result* while it is hidden: a
        column hidden leaves its width empty until the window is next resized."""
        if shown == self._job_column:
            return
        job, result = int(self.tree.column("job", "width")), int(self.tree.column("result", "width"))
        self.tree.column("result", width=max(result - job, self._result_width) if shown else result + job)
        self.tree.configure(displaycolumns=[c for c, _, _ in _COLUMNS if c != "job" or shown])
        self._job_column = shown

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    def show(self, histories: Sequence[JobHistory], empty: str, roots: Roots, live: Collection[str] = (),
             logs: Sequence[ActivityLog] = ()) -> None:
        """The records of the jobs ``histories`` hold; ``empty``: what to say when there is
        none; ``roots`` gives a destination's folder now, or None; ``live``: the identifiers
        of the runs and verifications in progress; ``logs``: every log read back."""
        self._roots, self._live, self._logs = roots, frozenset(live), tuple(logs)
        key = (empty, tuple((h.job_id, h.name, tuple((r.run_id, r.destination_index, r.ended) for r in h.records),
                             tuple((v.verification_id, v.updated, v.state, v.verification_id in self._live)
                                   for v in h.verifications),
                             tuple((r.repair_id, r.updated, r.state, r.repair_id in self._live) for r in h.repairs),
                             tuple(sorted((h.names or {}).items())),
                             tuple((g.path.name, g.status, g.in_use) for g in h.logs)) for h in histories))
        if key == self._shown:
            return
        self._shown = key
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self._show_job_column(len(histories) > 1)
        self._entries = {}
        by_job = {h.job_id: h for h in histories}
        rows: List[HistoryRow] = []
        for row, entry in selection_entries(histories, self._live):
            job_id = entry.job_id if not isinstance(entry, ActivityLog) else \
                next(h.job_id for h in histories if entry in h.logs)
            self._entries[row.iid] = (entry, by_job[job_id])
            rows.append(row)
            self.tree.insert("", "end", iid=row.iid, values=(row.job, row.started, row.destination, row.how,
                                                             row.result, row.copied, row.into_versions, row.renamed,
                                                             row.skipped, row.took))
        self.rows = tuple(rows)
        if selected and selected[0] in self._entries:
            self.tree.selection_set(selected[0])
        self.empty.configure(text="" if self._entries else empty)
        self._buttons()

    def _buttons(self):
        set_enabled(self.details, bool(self.tree.selection()))

    def _open(self):
        selection = self.tree.selection()
        if not selection:
            return
        entry, history = self._entries[selection[0]]
        log = log_of(entry, self._logs)
        if isinstance(entry, ActivityLog):
            StartWindow(self.frame, start_details(entry, history.name), self.opener)
            return
        root = self._roots(history.job_id, entry.destination_index)
        name = (history.names or {}).get(entry.destination_index, "")
        if isinstance(entry, VerificationRecord):
            VerificationWindow(self.frame, verification_details(entry, root, name, self._live, history.repairs),
                               self.opener, log, self.repair(entry))
            return
        if isinstance(entry, RepairRecord):
            RepairWindow(self.frame, repair_details(entry, root, name, self._live), self.opener, log)
            return
        RecordWindow(self.frame, record_details(entry, root, name), self.opener, log)


def open_log(window: tk.Misc, opener: Opener, log: Optional[Path]) -> None:
    """Open ``log`` in the program Windows opens a log with."""
    if log is None:
        return
    try:
        opener(log)
    except OSError as exc:
        messagebox.showwarning("Open log", str(exc), parent=window)


class StartWindow:
    """A start that left no record: why, and its log."""

    def __init__(self, parent, details: StartDetails, opener: Opener):
        w = self.window = tk.Toplevel(parent)
        w.title(details.title)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=details.text, wraplength=em(w, 26), justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w")
        log = ttk.Button(frame, text="Open log", command=lambda: open_log(w, opener, details.log))
        log.grid(row=1, column=0, sticky="w", pady=(em(w, 0.6), 0))
        set_enabled(log, details.log.is_file())
        close = ttk.Button(frame, text="Close", default="active", command=w.destroy)
        close.grid(row=1, column=1, sticky="e", pady=(em(w, 0.6), 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()


class RecordWindow:
    def __init__(self, parent, details: RecordDetails, opener: Opener, log: Optional[Path] = None):
        self.details = details
        self.opener = opener
        w = self.window = tk.Toplevel(parent)
        w.title(details.title)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        ttk.Label(frame, text=details.summary, wraplength=em(w, 26), justify="left",
                  style="Headline.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, PAD))
        for column, title in enumerate(("", "planned", "done", "left")):
            ttk.Label(frame, text=title).grid(row=1, column=column, sticky="e" if column else "w", padx=(0, PAD))
        for r, (what, planned, done, left) in enumerate(details.table, start=2):
            for column, text in enumerate((what, planned, done, left)):
                ttk.Label(frame, text=text).grid(row=r, column=column, sticky="e" if column else "w",
                                                 padx=(em(w, 0.6) if not column else 0, PAD))
        row = 2 + len(details.table)
        for text, tone in ((details.source, Tone.NORMAL), (details.cleanup, Tone.NORMAL),
                           (details.space, Tone.ATTENTION if details.space_low else Tone.NORMAL)):
            if text:
                ttk.Label(frame, text=text, wraplength=em(w, 26), justify="left", style=tone_style(tone)).grid(
                    row=row, column=0, columnspan=4, sticky="w", pady=(PAD if row == 2 + len(details.table) else 0, 0))
                row += 1
        skipped = ttk.Frame(frame)
        skipped.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(PAD, 0))
        if details.skipped_total:
            first = details.skipped[0]
            ttk.Label(skipped, text=f"Skipped ({details.skipped_total:,}):  {first[0]} - {first[1]}",
                      wraplength=em(w, 22), justify="left").grid(row=0, column=0, sticky="w")
            ttk.Button(skipped, text="Show all...", command=self._show_skipped).grid(row=0, column=1, padx=(PAD, 0))
        else:
            ttk.Label(skipped, text="Skipped: none").grid(row=0, column=0, sticky="w")
        errors = "; ".join(details.errors[:3]) + (" ..." if len(details.errors) > 3 else "") if details.errors \
            else "none"
        ttk.Label(frame, text=f"Errors: {errors}", wraplength=em(w, 26), justify="left").grid(
            row=row + 1, column=0, columnspan=4, sticky="w")
        batch = ttk.Frame(frame)
        batch.grid(row=row + 2, column=0, columnspan=4, sticky="ew")
        ttk.Label(batch, text=f"Versions batch: {details.versions_batch or 'none'}").grid(row=0, column=0, sticky="w")
        open_batch = ttk.Button(batch, text="Open batch", command=self._open_batch)
        open_batch.grid(row=0, column=1, padx=(PAD, 0))
        set_enabled(open_batch, can_open(details.versions_path))
        ttk.Label(frame, text=f"Engine: {details.engine}").grid(row=row + 3, column=0, columnspan=4, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 4, column=0, columnspan=4, sticky="e", pady=(PAD, 0))
        open_it = ttk.Button(buttons, text="Open log", command=lambda: open_log(w, opener, log))
        open_it.grid(row=0, column=0)
        set_enabled(open_it, log is not None and log.is_file())
        close = ttk.Button(buttons, text="Close", default="active", command=w.destroy)
        close.grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def _show_skipped(self):
        items = [ListedFile(path, why) for path, why in self.details.skipped]
        heading = f"{self.details.skipped_total:,} skipped" + (
            f"; the first {len(items):,} are listed" if len(items) < self.details.skipped_total else "")
        ListWindow(self.window, f"Skipped - {self.details.title}", heading, items)

    def _open_batch(self):
        open_place(self.window, self.opener, self.details.versions_path, "Open batch")
