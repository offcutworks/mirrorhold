"""The preview and confirmation window. Tk.

While the preview works it shows where it is, with *Cancel*; then how the run will read
the source and, per destination, what a run would do (``preview_model.summarise``) and
the run buttons that apply. A preview opened for some of the job's destinations shows
those alone, and its run buttons run those alone. A first run into a folder that already
holds what the source does not shows its two answers as a choice, the destination's
quantities following the answer chosen. A run button starts
the run as a core process of its own, with the ticket for the answers given
(``preview_model.ticket_to_run``), and closes the window; nothing here copies, moves or
deletes anything.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, Optional, Sequence

from mirrorhold.endpoints.resolution import resolve
from mirrorhold.endpoints.volumes import VolumeInfo
from mirrorhold.run.confirmation import Content
from mirrorhold.run.runner import RunError
from mirrorhold.wording import counted

from .common_view import (PAD, ListWindow, ScrollFrame, em, grow_to_content, modal, set_enabled, size_to_content,
                          tone_style)
from .formatting import count, files
from .jobs_model import Tone, describe_folder
from .preview_model import (SHOWN_FOLDERS, PairSummary, PreviewSummary, PreviewWorker, WorkerState, progress_words,
                            summarise, ticket_to_run)
from .session import GuiSession, NotStarted

_POLL_MS = 200
_LIST_TITLES = {"removals": "Into versions", "kept": "Left as they are", "skipped": "Left out at the source",
                "lost": "Lost their file type", "too_large": "Too large for FAT32",
                "left": "Left as they are, not in the source"}


class PreviewWindow:
    def __init__(self, parent: tk.Misc, session: GuiSession, job_id: str, *,
                 destinations: Optional[Sequence[str]] = None,
                 volumes: Callable[[], Sequence[VolumeInfo]] = lambda: (),
                 on_started: Callable[[], None] = lambda: None, worker: Optional[PreviewWorker] = None):
        """``destinations``: the markers of the destinations previewed and run, None for every one."""
        self.parent = parent
        self.session = session
        self.job = session.contents.job(job_id)
        self.destinations = tuple(destinations) if destinations is not None else None
        self.volumes = volumes
        self.on_started = on_started
        self.summary: Optional[PreviewSummary] = None
        self.choices: Dict[int, tk.StringVar] = {}    # destination index -> the answer chosen
        w = self.window = tk.Toplevel(parent)
        numbers = [str(n) for n, d in enumerate(self.job.destinations, 1) if d.marker_id in (self.destinations or ())]
        w.title(f"Preview: {self.job.name}" + (f" -> {', '.join(numbers)}" if numbers else ""))
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        w.protocol("WM_DELETE_WINDOW", self._close)
        w.bind("<Escape>", lambda e: self._close())
        self.worker = (worker or PreviewWorker(session, job_id, destinations=self.destinations)).start()
        self._build_working()
        size_to_content(w, min_width=em(w, 18), parent=parent)
        modal(w, parent)
        self._poll()

    # --- working -----------------------------------------------------------------------

    def _build_working(self):
        self.working = ttk.Frame(self.window, padding=em(self.window, 0.8))
        self.working.grid(sticky="nsew")
        self.working.columnconfigure(0, weight=1)
        self.where = ttk.Label(self.working, text="Listing the source", wraplength=em(self.window, 24),
                               justify="left")
        self.where.grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(self.working, mode="indeterminate", length=em(self.window, 16))
        self.bar.grid(row=1, column=0, sticky="ew", pady=PAD)
        self.bar.start(15)
        self.entries = ttk.Label(self.working, text="")
        self.entries.grid(row=2, column=0, sticky="w")
        ttk.Label(self.working, text="A cold listing of a large drive can take minutes.").grid(row=3, column=0,
                                                                                               sticky="w")
        ttk.Button(self.working, text="Cancel", command=self._close).grid(row=4, column=0, sticky="e",
                                                                          pady=(PAD, 0))

    def _poll(self):
        if not self.window.winfo_exists():
            return
        state = self.worker.state
        if state is WorkerState.WORKING:
            volumes = self.volumes()
            source = describe_folder(resolve(self.job.source.endpoint, volumes))
            destinations = [describe_folder(resolve(d.endpoint, volumes)) for d in self.job.destinations]
            progress = self.worker.progress
            self.where.configure(text=progress_words(progress, source, destinations))
            self.entries.configure(text=counted(progress.entries, "entry", "entries", separated=True)
                                   if progress.entries else "")
            grow_to_content(self.window)
            self.window.after(_POLL_MS, self._poll)
            return
        self.bar.stop()
        if state is WorkerState.FAILED:
            messagebox.showerror("Preview", f"The preview could not be made: {self.worker.error}", parent=self.window)
            self.window.destroy()
        elif state is WorkerState.DONE:
            self.summary = summarise(self.worker.result)
            self.working.destroy()
            self._build_result()
        else:
            self.window.destroy()

    def _close(self):
        if self.worker.state is WorkerState.WORKING:
            self.worker.cancel()
        self.window.destroy()

    # --- the result ----------------------------------------------------------------------

    def _build_result(self):
        w = self.window
        summary = self.summary
        outer = ttk.Frame(w, padding=em(w, 0.7))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text=summary.source_line).grid(row=0, column=0, sticky="w")
        if summary.reading:
            ttk.Label(head, text=summary.reading).grid(row=1, column=0, sticky="w")
        ttk.Label(head, text="The run lists again before it applies anything; a removal not shown here is held "
                             "again.").grid(row=2, column=0, sticky="w", pady=(0, PAD))
        scroll = ScrollFrame(outer)
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.body.columnconfigure(0, weight=1)
        for i, pair in enumerate(summary.pairs):
            self._pair_box(scroll.body, pair).grid(row=i, column=0, sticky="ew", pady=(0, PAD))
        self._buttons(outer).grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        body_height = scroll.requested_height()
        scroll.canvas.configure(height=min(body_height, int(w.winfo_screenheight() * 0.6)),
                                width=scroll.body.winfo_reqwidth())
        w.minsize(1, 1)
        size_to_content(w, min_width=em(w, 24), parent=self.parent)

    def _pair_box(self, parent, pair: PairSummary) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=pair.title, padding=PAD)
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text=pair.headline, wraplength=em(box, 30), justify="left",
                  style=tone_style(pair.tone, headline=True)).grid(row=0, column=0, sticky="w")
        body = ttk.Frame(box)
        body.grid(row=2, column=0, sticky="ew")
        if pair.choices:
            self._choice_frame(box, pair, body).grid(row=1, column=0, sticky="w", pady=(PAD, PAD))
        self._pair_body(body, pair)
        return box

    def _choice_frame(self, box, pair: PairSummary, body: ttk.Frame) -> ttk.Frame:
        """The answers to what a first run does with what the folder holds and the source
        does not; choosing one shows the destination as it would run by it."""
        choice = self.choices[pair.index] = tk.StringVar(box, value=pair.content.value)
        frame = ttk.Frame(box)
        row = 0
        for option in pair.choices:
            button = ttk.Radiobutton(frame, text=option.label, value=option.content.value, variable=choice,
                                     command=lambda: self._chosen(pair, body))
            button.grid(row=row, column=0, sticky="w")
            set_enabled(button, option.offered)
            row += 1
            if option.detail:
                ttk.Label(frame, text=option.detail, wraplength=em(box, 28), justify="left").grid(
                    row=row, column=0, sticky="w", padx=(em(box, 1.4), 0))
                row += 1
        return frame

    def _chosen(self, pair: PairSummary, body: ttk.Frame):
        chosen = self.choices[pair.index].get()
        option = next(o for o in pair.choices if o.content.value == chosen)
        for child in body.winfo_children():
            child.destroy()
        self._pair_body(body, option.pair)

    def _pair_body(self, box, pair: PairSummary):
        """What a run would do at the destination: its quantities, time and removals by folder,
        and the notices to read before confirming."""
        box.columnconfigure(3, weight=1)
        wrap = em(box, 30)
        row = 1
        for q in pair.quantities:
            ttk.Label(box, text=q.label).grid(row=row, column=0, sticky="w", padx=(em(box, 0.6), PAD))
            ttk.Label(box, text=q.files).grid(row=row, column=1, sticky="e", padx=(0, PAD))
            ttk.Label(box, text=q.size).grid(row=row, column=2, sticky="e", padx=(0, PAD))
            ttk.Label(box, text=q.detail).grid(row=row, column=3, sticky="w")
            if q.listing and q.listing != "removals":
                ttk.Button(box, text="Details...",
                           command=lambda key=q.listing: self._show_list(pair, key)).grid(row=row, column=4, sticky="e")
            row += 1
        if pair.time:
            ttk.Label(box, text="Time").grid(row=row, column=0, sticky="nw", padx=(em(box, 0.6), PAD))
            ttk.Label(box, text=pair.time, wraplength=wrap, justify="left").grid(row=row, column=1, columnspan=4,
                                                                                sticky="w")
            row += 1
        if pair.groups:
            row = self._folder_groups(box, row, pair, "Into versions", pair.groups, pair.hidden_groups)
        if pair.back_groups:
            row = self._folder_groups(box, row, pair, "Into versions again", pair.back_groups,
                                      pair.hidden_back_groups, pair.back_line)
        if pair.note:
            ttk.Label(box, text=pair.note, wraplength=wrap, justify="left").grid(row=row, column=0, columnspan=5,
                                                                                sticky="w", pady=(PAD, 0))
            row += 1
        for notice in pair.notices:
            ttk.Label(box, text=notice, wraplength=wrap, justify="left", style=tone_style(Tone.ATTENTION)).grid(
                row=row, column=0, columnspan=5, sticky="w", pady=(PAD, 0))
            row += 1

    def _folder_groups(self, box, row: int, pair: PairSummary, heading: str, groups, hidden: int,
                       line: str = "") -> int:
        """One list of removals by folder, under '<heading>, by folder' and ``line``: the
        largest folders, how many more, and Show all. Returns the next free row."""
        ttk.Label(box, text=f"{heading}, by folder").grid(row=row, column=0, columnspan=2, sticky="w",
                                                         padx=(em(box, 0.6), 0), pady=(PAD, 0))
        row += 1
        if line:
            ttk.Label(box, text=line, wraplength=em(box, 30), justify="left").grid(
                row=row, column=0, columnspan=5, sticky="w", padx=(em(box, 1.2), 0))
            row += 1
        for group in groups[:SHOWN_FOLDERS]:
            ttk.Label(box, text=group.label).grid(row=row, column=0, columnspan=2, sticky="w",
                                                  padx=(em(box, 1.2), PAD))
            ttk.Label(box, text=count(len(group.files))).grid(row=row, column=2, sticky="e", padx=(0, PAD))
            ttk.Label(box, text=group.detail).grid(row=row, column=3, sticky="w")
            row += 1
        more = f"({counted(hidden, 'more folder')})" if hidden else ""
        ttk.Label(box, text=more).grid(row=row, column=0, columnspan=3, sticky="w", padx=(em(box, 1.2), 0))
        ttk.Button(box, text="Show all...", command=lambda: self._show_groups(pair, heading, groups)).grid(
            row=row, column=4, sticky="e")
        return row + 1

    def _buttons(self, outer) -> ttk.Frame:
        actions = self.summary.actions
        frame = ttk.Frame(outer)
        frame.columnconfigure(0, weight=1)
        note = actions.blocked
        if actions.keep_held and actions.first_run_waits:
            note = "A first run waiting for confirmation does nothing when held removals are kept waiting."
        ttk.Label(frame, text=note, wraplength=em(outer, 20), justify="left").grid(row=0, column=0, sticky="w")
        column = 1
        if actions.review:
            apply_all = ttk.Button(frame, text="Run and apply everything shown", default="active",
                                   command=lambda: self._run(confirm=True))
            apply_all.grid(row=0, column=column, padx=(PAD, 0))
            set_enabled(apply_all, actions.apply_all)
            keep = ttk.Button(frame, text="Run, keep held removals waiting", command=lambda: self._run(confirm=False))
            keep.grid(row=0, column=column + 1, padx=(PAD, 0))
            set_enabled(keep, actions.keep_held)
            column += 2
            primary = apply_all if actions.apply_all else None
        else:
            run_now = ttk.Button(frame, text="Run now", default="active", command=lambda: self._run(confirm=False))
            run_now.grid(row=0, column=column, padx=(PAD, 0))
            set_enabled(run_now, actions.run_now)
            column += 1
            primary = run_now if actions.run_now else None
        ttk.Button(frame, text="Close", command=self.window.destroy).grid(row=0, column=column, padx=(PAD, 0))
        if primary is not None:
            primary.focus_set()
            self.window.bind("<Return>", lambda e: primary.invoke())
        return frame

    def _show_list(self, pair: PairSummary, key: str):
        items = pair.lists.get(key, ())
        ListWindow(self.window, f"{_LIST_TITLES[key]}: {self.job.name}, destination {pair.index}",
                   files(len(items)), items)

    def _show_groups(self, pair: PairSummary, heading: str, groups):
        ListWindow(self.window, f"{heading}: {self.job.name}, destination {pair.index}",
                   f"{files(sum(len(g.files) for g in groups))} in {counted(len(groups), 'folder', separated=True)}",
                   groups, grouped=True)

    def _run(self, *, confirm: bool):
        ticket = None
        if confirm:
            chosen = {index: Content(choice.get()) for index, choice in self.choices.items()}
            try:
                ticket = ticket_to_run(self.session, self.worker.result, self.summary, chosen)
            except (RunError, OSError) as exc:
                messagebox.showerror("Preview", f"The run could not be confirmed: {exc}", parent=self.window)
                return
        previewed = (tuple(p.assessment.destination.marker_id for p in self.worker.result.pairs)
                     if self.destinations is not None else None)    # those the preview showed, as it found them
        try:
            self.session.start_run(self.job.job_id, destinations=previewed, ticket_id=ticket)
        except NotStarted as exc:
            messagebox.showerror("Preview", f"The run could not start: {exc}.", parent=self.window)
            return
        self.window.destroy()
        self.on_started()
