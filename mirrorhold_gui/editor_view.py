"""The job editor window (New job / Edit job). Tk.

Its state lives in ``editor_model.JobDraft``; every change re-runs the core's validation
a moment after the last keystroke and shows the findings under *Checks*. *Save* is
enabled only while nothing is refused. The source's folder is typed into its field or
chosen with *Browse...*; a typed one is checked when the field is left or Enter is
pressed, and before a save, and a wrong path is said beneath the field, which keeps what
was typed. A destination is added through a small window of its own
(``DestinationWindow``), whose field takes a folder not made yet, or a remote the engine
lists and a folder in it, typed or browsed. *Source* offers reading from a snapshot of the
drive, disabled for a drive that cannot be snapshotted, which a line under it then says.
The engine is asked about a job's remote destinations as they are added or the job opens,
on a thread: *Save* waits for its answer, and the checks then say what it found.
*When to run* says first that the job runs when the user starts it, and offers the
automatic runs as additions, each off for a new job; the switch pausing them is enabled
only while one is chosen, and a line says so only when automatic runs would not take
effect. A schedule takes its days — daily, chosen weekdays or a day of the month — and its
times — one or more listed, or every few hours, between two times or not — and a control
the choices made do not use is disabled, its value kept, rather than hidden. *Holding removals* takes the count floor, the percentage and the share hold,
whose entry is disabled, its value kept, while the hold is switched off.
*Versions and verification* keeps every version by default; a save that switches the
deletion of old versions on, or lowers its days, asks first. What explains a choice is
the hint of the control it explains (``common_view.Hint``), not a line of the window.

The sections scroll inside the window and *Save* and *Cancel* stay below them, so the
buttons are reachable however tall the sections grow on a display of high scale. The
window opens at most two-thirds of the screen's height, and a check appearing as the
user types grows it no further. Once the user resizes the window it keeps the size they
gave it, and a check appearing scrolls instead.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional, Sequence

from mirrorhold.endpoints.resolution import EndpointError
from mirrorhold.endpoints.volumes import VolumeInfo
from mirrorhold.jobs.model import Job
from mirrorhold.jobs.schedule import MONTHLY, WEEKLY
from mirrorhold.remotes.facts import RemoteSetup, SetupState

from .common_view import (PAD, Hint, ScrollFrame, choose, em, grow_to_content, modal, set_enabled, size_to_content,
                          tone_style)
from .editor_model import (CLEANUP_NOTE, EVERY_CHOICES, MONTH_DAY_CHOICES, NEW_FOLDER_NOTE, NOTE, REFUSED,
                           SCHEDULE_KINDS, SNAPSHOT_NOTE, SOURCE_PROMPT, WARNING, WEEKDAYS, CheckResult, JobDraft,
                           check, cleanup_question, endpoint_line, path_problem, snapshot_choice, typed_path)
from .jobs_model import Tone
from .prefs_model import automation_words
from .remote_model import ASKING_REMOTES, NAME_CONFIGURATION, Background, RemoteAccess, ask_setup, window_message
from .remote_view import RemoteFolderWindow
from .session import GuiSession, StoreConflict

_LEVEL_TONE = {REFUSED: Tone.PROBLEM, WARNING: Tone.ATTENTION, NOTE: Tone.NORMAL}
_DELAY_MS = 300
_POLL_MS = 150
_ON_THIS_COMPUTER, _ON_A_REMOTE = "computer", "remote"
_SCREEN_SHARE = 2 / 3      # the most of the screen's height the window opens at


class EditorWindow:
    def __init__(self, parent: tk.Misc, session: GuiSession, volumes: Sequence[VolumeInfo], *,
                 job: Optional[Job] = None, source_editable: bool = True,
                 on_saved: Callable[[Optional[Job]], None] = lambda job: None):
        """``on_saved`` receives the saved job, or None when the save was refused because
        the job file changed elsewhere (the caller then reloads)."""
        self.parent = parent
        self.session = session
        self.volumes = tuple(volumes)
        self.sync_roots = session.sync_roots()       # read once per editor, not on every check
        self.on_saved = on_saved
        self.draft = JobDraft.from_job(job, source_editable=source_editable) if job else JobDraft()
        self.result: Optional[CheckResult] = None
        self.remote_setup: Optional[RemoteSetup] = None
        self._remotes_asked: set = set()       # the remote folders the engine was asked about
        self._asking: Optional[Background] = None
        self._pending = None
        self._wanted: Optional[tuple] = None   # a size this window asked for and has not reached yet
        self._size: Optional[tuple] = None     # the window's size once it reached the one asked for
        self._resized = False                  # whether the user has resized the window

        w = self.window = tk.Toplevel(parent)
        w.title("Edit job" if job else "New job")
        w.bind("<Configure>", self._configured, add="+")
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.scroll = ScrollFrame(frame)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        outer = self.scroll.body
        outer.columnconfigure(0, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Name").grid(row=0, column=0, sticky="w", padx=(0, PAD))
        self.name = tk.StringVar(value=self.draft.name)
        name_entry = ttk.Entry(top, textvariable=self.name, width=40)
        name_entry.grid(row=0, column=1, sticky="ew")
        self.name.trace_add("write", lambda *a: self._changed())

        self._build_source(outer, row=1)
        self._build_destinations(outer, row=2)
        self._build_exclusions(outer, row=3)
        self._build_holds(outer, row=4)
        self._build_when(outer, row=5)
        self._build_versions(outer, row=6)
        self.checks = ttk.LabelFrame(outer, text="Checks", padding=PAD)
        self.checks.grid(row=7, column=0, sticky="ew", pady=(PAD, 0))
        self.checks.columnconfigure(1, weight=1)

        self.buttons = buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="e", pady=(em(w, 0.6), 0))
        self.save_button = ttk.Button(buttons, text="Save", default="active", command=self._save)
        self.save_button.grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Escape>", lambda e: w.destroy())

        self._refresh_source()
        self._refresh_destinations()
        self._validate()
        self._fit_sections()
        self._asked(size_to_content(w, min_width=em(w, 26), parent=parent))
        modal(w, parent)
        name_entry.focus_set()

    def _asked(self, size: Optional[tuple]) -> None:
        """Note a size this window asked for: until the window has it, the sizes it passes
        through on the way — Tk's own as it maps the window, among them — are not the
        user's."""
        if size is not None:
            self._wanted = size

    def _configured(self, event) -> None:
        """A size the window did not ask for is the user's, and is kept from then on."""
        if event.widget is not self.window:
            return
        size = (event.width, event.height)
        if self._wanted is not None:
            if size == self._wanted:
                self._wanted, self._size = None, size
            return
        if self._size is not None and size != self._size:
            self._resized = True
            self._size = size

    def _fit_sections(self) -> None:
        """Show the sections whole when two-thirds of the screen's height has room for
        them and the buttons, and scroll them otherwise."""
        w = self.window
        body = self.scroll.body
        sections = self.scroll.requested_height()      # first: it brings every requested size up to date
        room = int(w.winfo_screenheight() * _SCREEN_SHARE) - self.buttons.winfo_reqheight() - em(w, 2)
        self.scroll.canvas.configure(height=min(sections, max(room, em(w, 10))), width=body.winfo_reqwidth())

    # --- sections ----------------------------------------------------------------------

    def _build_source(self, outer, row):
        box = ttk.LabelFrame(outer, text="Source", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Folder").grid(row=0, column=0, sticky="w", padx=(0, PAD))
        self.source_text = tk.StringVar()
        self._source_shown = ""                 # the field's text when last shown or checked
        self._source_wrong = False              # whether that check found a wrong path
        entry = ttk.Entry(box, textvariable=self.source_text,
                          state="normal" if self.draft.source_editable else "readonly")
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<FocusOut>", lambda e: self.window.after_idle(self._left_source), add="+")
        entry.bind("<Return>", lambda e: self._take_source(again=True), add="+")
        self.browse = ttk.Button(box, text="Browse...", command=self._browse_source)
        self.browse.grid(row=0, column=2, padx=(PAD, 0))
        self.source_detail = ttk.Label(box, text="")
        self.source_detail.grid(row=1, column=1, columnspan=2, sticky="w")
        set_enabled(self.browse, self.draft.source_editable)
        if not self.draft.source_editable:
            fixed = ttk.Label(box, text="A job that has run keeps its source.")
            fixed.grid(row=2, column=1, columnspan=2, sticky="w")
            Hint(fixed, "To back up another folder, make a new job.")
        self.snapshot = tk.BooleanVar(value=self.draft.read_from_snapshot)
        self.snapshot_button = ttk.Checkbutton(box, variable=self.snapshot, command=self._changed,
                                               text="Read from a snapshot of the drive when possible")
        self.snapshot_button.grid(row=3, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        Hint(self.snapshot_button, SNAPSHOT_NOTE)
        self.snapshot_note = ttk.Label(box, text="", wraplength=em(outer, 26), justify="left")
        self.snapshot_note.grid(row=4, column=0, columnspan=3, sticky="w", padx=(em(outer, 1.2), 0))

    def _build_destinations(self, outer, row):
        box = ttk.LabelFrame(outer, text="Destinations", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(0, weight=1)
        self.dest_tree = ttk.Treeview(box, columns=("folder", "drive", "free"), show="headings", height=3,
                                      selectmode="browse")
        for column, title, width in (("folder", "Folder", 14), ("drive", "Drive", 9), ("free", "Free", 4.5)):
            self.dest_tree.heading(column, text=title)
            self.dest_tree.column(column, width=em(box, width), stretch=column == "folder",
                                  anchor="e" if column == "free" else "w")
        self.dest_tree.grid(row=0, column=0, sticky="ew")
        row_buttons = ttk.Frame(box)
        row_buttons.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.dest_buttons = {}
        for i, (key, label, command) in enumerate((("add", "Add...", self._add_destination),
                                                   ("remove", "Remove", self._remove_destination),
                                                   ("up", "Move up", lambda: self._move(-1)),
                                                   ("down", "Move down", lambda: self._move(1)))):
            button = ttk.Button(row_buttons, text=label, command=command)
            button.grid(row=0, column=i, padx=(PAD if i else 0, 0))
            self.dest_buttons[key] = button
        self.dest_tree.bind("<<TreeviewSelect>>", lambda e: self._destination_buttons())
        Hint(self.dest_buttons["remove"], "Removing a destination deletes nothing there.")

    def _build_exclusions(self, outer, row):
        box = ttk.LabelFrame(outer, text="Exclusions", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(0, weight=1)
        self.sync_files = tk.BooleanVar(value=self.draft.sync_client_artefacts)
        self.editor_files = tk.BooleanVar(value=self.draft.transient_editor_files)
        sync_files = ttk.Checkbutton(box, variable=self.sync_files, command=self._changed,
                                     text="Sync clients' working files")
        sync_files.grid(row=0, column=0, sticky="w")
        # The hint names these four clients alone; every client's files that jobs.exclusions
        # lists are excluded all the same (the maintainer's decision, 2026-11-04).
        Hint(sync_files, "Dropbox, Google Drive, Syncthing, Nextcloud and other sync clients keep these beside "
                         "your files.")
        editor_files = ttk.Checkbutton(box, variable=self.editor_files, command=self._changed,
                                       text="Temporary editor files")
        editor_files.grid(row=1, column=0, sticky="w")
        Hint(editor_files, "Office owner files (~$*), Word temporaries and LibreOffice locks.")
        patterns = ttk.Label(box, text="Your patterns, one per line:")
        patterns.grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        Hint(patterns, "Read as .gitignore reads them. Always excluded at a drive root: the recycle bin, System "
                       "Volume Information, page, swap, hibernation and dump files. Links are never followed.")
        self.patterns = tk.Text(box, height=3, width=40, wrap="none", undo=True)
        self.patterns.insert("1.0", self.draft.patterns_text)
        self.patterns.grid(row=3, column=0, sticky="w")
        self.patterns.bind("<<Modified>>", self._patterns_changed)

    def _build_holds(self, outer, row):
        box = ttk.LabelFrame(outer, text="Holding removals", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        self.floor = tk.StringVar(value=self.draft.count_floor_text)
        self.percent = tk.StringVar(value=self.draft.percent_text)
        values = ttk.Frame(box)            # each line a frame of its own: the longest sets no other's columns
        values.grid(row=0, column=0, sticky="w")
        held = ttk.Label(values, text="Hold removals for review above")
        held.grid(row=0, column=0, sticky="w", padx=(0, PAD))
        Hint(held, "A run holds its removals for your review when they number more than both. Always held, "
                   "whatever these values: changed files that no longer look like their file type (what encryption "
                   "leaves).")
        ttk.Entry(values, textvariable=self.floor, width=7, justify="right").grid(row=0, column=1, sticky="w")
        ttk.Label(values, text="files and").grid(row=0, column=2, sticky="w", padx=PAD)
        ttk.Entry(values, textvariable=self.percent, width=5, justify="right").grid(row=0, column=3, sticky="w")
        ttk.Label(values, text="% of the destination's files").grid(row=0, column=4, sticky="w", padx=(PAD, 0))
        line = ttk.Frame(box)
        line.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.share_on = tk.BooleanVar(value=self.draft.share_on)
        share_on = ttk.Checkbutton(line, variable=self.share_on, command=self._share_changed, text="and when")
        share_on.grid(row=0, column=0, sticky="w")
        Hint(share_on, "What an emptied or wrong source looks like. Switch this off for a job of one or a few "
                       "files, where one edit is already half.")
        self.share = tk.StringVar(value=self.draft.share_text)
        self.share_entry = ttk.Entry(line, textvariable=self.share, width=5, justify="right")
        self.share_entry.grid(row=0, column=1, padx=PAD)
        ttk.Label(line, text="% or more of its files would move into versions").grid(row=0, column=2, sticky="w")
        for var in (self.floor, self.percent, self.share):
            var.trace_add("write", lambda *a: self._changed())
        self._share_state()

    def _share_changed(self):
        self._share_state()
        self._changed()

    def _share_state(self):
        set_enabled(self.share_entry, self.share_on.get())

    def _build_when(self, outer, row):
        d = self.draft
        box = ttk.LabelFrame(outer, text="When to run", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        ttk.Label(box, text="Runs when you start it. Also run automatically:").grid(row=0, column=0, columnspan=2,
                                                                                  sticky="w")
        indent = em(outer, 1.2)
        self.on_arrival = tk.BooleanVar(value=d.on_arrival)
        on_arrival = ttk.Checkbutton(box, variable=self.on_arrival, command=self._when_changed,
                                     text="When any of its drives is attached")
        on_arrival.grid(row=2, column=0, columnspan=2, sticky="w", padx=(indent, 0))
        Hint(on_arrival, "Also when the computer starts with that drive attached.")
        line = ttk.Frame(box)
        line.grid(row=3, column=0, columnspan=2, sticky="w", padx=(indent, 0))
        self.scheduled = tk.BooleanVar(value=d.scheduled)
        ttk.Checkbutton(line, variable=self.scheduled, command=self._when_changed, text="On a schedule:") \
            .grid(row=0, column=0, sticky="w")
        self.kind = tk.StringVar(value=SCHEDULE_KINDS[d.days_kind])
        self.kind_box = ttk.Combobox(line, textvariable=self.kind, values=list(SCHEDULE_KINDS.values()),
                                     state="readonly", width=8)
        self.kind_box.grid(row=0, column=1, padx=(PAD, 0))
        self.kind_box.bind("<<ComboboxSelected>>", lambda e: self._when_changed())
        days = ttk.Frame(box)
        days.grid(row=4, column=0, columnspan=2, sticky="w", padx=(indent * 2, 0))
        ttk.Label(days, text="Weekly:").grid(row=0, column=0, padx=(0, PAD))
        self.days = []
        self.day_buttons = []
        for i, name in enumerate(WEEKDAYS):
            var = tk.BooleanVar(value=i in d.schedule_days)
            button = ttk.Checkbutton(days, text=name, variable=var, command=self._changed)
            button.grid(row=0, column=1 + i, padx=(0, PAD // 2))
            self.days.append(var)
            self.day_buttons.append(button)
        month = ttk.Frame(box)
        month.grid(row=5, column=0, columnspan=2, sticky="w", padx=(indent * 2, 0), pady=(PAD // 2, 0))
        ttk.Label(month, text="Monthly, on day").grid(row=0, column=0, padx=(0, PAD))
        self.month_day = tk.StringVar(value=d.month_day_text)
        self.month_day_box = ttk.Combobox(month, textvariable=self.month_day, values=MONTH_DAY_CHOICES,
                                          state="readonly", width=8)
        self.month_day_box.grid(row=0, column=1)
        self.month_day_box.bind("<<ComboboxSelected>>", lambda e: self._changed())
        Hint(self.month_day_box, "The 29th to the 31st are not offered, since some months lack them; "
                                 "the last day serves the end of every month.")
        self._build_times(box, indent)
        self.paused = tk.BooleanVar(value=d.paused)
        self.pause_button = ttk.Checkbutton(box, variable=self.paused, command=self._changed,
                                            text="Pause automatic runs")
        self.pause_button.grid(row=7, column=0, columnspan=2, sticky="w", padx=(indent, 0), pady=(PAD // 2, 0))
        Hint(self.pause_button, "The choices above are kept.")
        words = automation_words(self.session.installation())
        if words:
            ttk.Label(box, text=words, wraplength=em(outer, 26), justify="left").grid(row=8, column=0, columnspan=2,
                                                                                     sticky="w", pady=(PAD, 0))
        self._when_states()

    def _build_times(self, box, indent):
        """The times of the schedule: at the times listed, or every few hours."""
        d = self.draft
        times = ttk.Frame(box)
        times.grid(row=6, column=0, columnspan=2, sticky="w", padx=(indent * 2, 0), pady=(PAD // 2, 0))
        self.repeating = tk.BooleanVar(value=d.repeating)
        self.at_button = ttk.Radiobutton(times, text="At", variable=self.repeating, value=False,
                                         command=self._when_changed)
        self.at_button.grid(row=0, column=0, sticky="w")
        self.time_text = tk.StringVar(value=d.schedule_time_text)
        self.time_entry = ttk.Entry(times, textvariable=self.time_text, width=24)
        self.time_entry.grid(row=0, column=1, columnspan=5, sticky="w", padx=(PAD, 0))
        Hint(self.time_entry, "One time of day or several, separated by commas, such as 09:00, 13:00, 21:00.")
        self.every_button = ttk.Radiobutton(times, text="Every", variable=self.repeating, value=True,
                                            command=self._when_changed)
        self.every_button.grid(row=1, column=0, sticky="w", pady=(PAD // 2, 0))
        self.every = tk.StringVar(value=d.every_text)
        self.every_box = ttk.Combobox(times, textvariable=self.every, values=EVERY_CHOICES, state="readonly", width=3)
        self.every_box.grid(row=1, column=1, sticky="w", padx=(PAD, 0), pady=(PAD // 2, 0))
        self.every_box.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Label(times, text="hours").grid(row=1, column=2, sticky="w", padx=(PAD, 0), pady=(PAD // 2, 0))
        self.window_on = tk.BooleanVar(value=d.window_on)
        self.window_button = ttk.Checkbutton(times, text="between", variable=self.window_on,
                                             command=self._when_changed)
        self.window_button.grid(row=1, column=3, sticky="w", padx=(PAD, 0), pady=(PAD // 2, 0))
        Hint(self.window_button, "The first run comes at the first time and the last at the second, when the hours "
                                 "land on it; the hours may run past midnight, as 22:00 and 06:00 do.")
        self.window_start = tk.StringVar(value=d.window_start_text)
        self.window_end = tk.StringVar(value=d.window_end_text)
        self.window_start_entry = ttk.Entry(times, textvariable=self.window_start, width=6, justify="center")
        self.window_start_entry.grid(row=1, column=4, padx=(PAD // 2, 0), pady=(PAD // 2, 0))
        ttk.Label(times, text="and").grid(row=1, column=5, padx=PAD // 2, pady=(PAD // 2, 0))
        self.window_end_entry = ttk.Entry(times, textvariable=self.window_end, width=6, justify="center")
        self.window_end_entry.grid(row=1, column=6, pady=(PAD // 2, 0))
        for var in (self.time_text, self.window_start, self.window_end):
            var.trace_add("write", lambda *a: self._changed())

    def _build_versions(self, outer, row):
        d = self.draft
        box = ttk.LabelFrame(outer, text="Versions and verification", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        self.cleanup = tk.BooleanVar(value=d.cleanup)
        ttk.Radiobutton(box, text="Keep all versions", variable=self.cleanup, value=False,
                        command=self._versions_changed).grid(row=0, column=0, columnspan=3, sticky="w")
        line = ttk.Frame(box)
        line.grid(row=1, column=0, columnspan=3, sticky="w")
        cleanup = ttk.Radiobutton(line, text="Delete version batches older than", variable=self.cleanup, value=True,
                                  command=self._versions_changed)
        cleanup.grid(row=0, column=0, sticky="w")
        Hint(cleanup, CLEANUP_NOTE)
        self.keep_days = tk.StringVar(value=d.keep_days_text)
        self.keep_entry = ttk.Entry(line, textvariable=self.keep_days, width=5, justify="right")
        self.keep_entry.grid(row=0, column=1, padx=PAD)
        ttk.Label(line, text="days").grid(row=0, column=2, sticky="w")
        reminder = ttk.Frame(box)
        reminder.grid(row=3, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        ttk.Label(reminder, text="Remind me to verify fully every").grid(row=0, column=0, sticky="w")
        self.reminder_days = tk.StringVar(value=d.reminder_days_text)
        ttk.Entry(reminder, textvariable=self.reminder_days, width=5, justify="right").grid(row=0, column=1, padx=PAD)
        ttk.Label(reminder, text="days").grid(row=0, column=2, sticky="w")
        for var in (self.keep_days, self.reminder_days):
            var.trace_add("write", lambda *a: self._changed())
        self._versions_states()

    def _versions_changed(self):
        self._versions_states()
        self._changed()

    def _versions_states(self):
        set_enabled(self.keep_entry, self.cleanup.get())

    def _when_changed(self):
        self._when_states()
        self._changed()

    def _days_kind(self) -> str:
        return next(kind for kind, name in SCHEDULE_KINDS.items() if name == self.kind.get())

    def _when_states(self):
        scheduled = self.scheduled.get()
        kind = self._days_kind()
        repeating = self.repeating.get()
        for box, on in ((self.kind_box, scheduled), (self.month_day_box, scheduled and kind == MONTHLY),
                        (self.every_box, scheduled and repeating)):
            box.state(["!disabled", "readonly"] if on else ["disabled"])
        for button in self.day_buttons:
            set_enabled(button, scheduled and kind == WEEKLY)
        for control in (self.at_button, self.every_button):
            set_enabled(control, scheduled)
        set_enabled(self.time_entry, scheduled and not repeating)
        set_enabled(self.window_button, scheduled and repeating)
        for entry in (self.window_start_entry, self.window_end_entry):
            set_enabled(entry, scheduled and repeating and self.window_on.get())
        set_enabled(self.pause_button, self.on_arrival.get() or scheduled)

    # --- the draft -----------------------------------------------------------------------

    def _patterns_changed(self, _event=None):
        if self.patterns.edit_modified():
            self.patterns.edit_modified(False)
            self._changed()

    def _sync_draft(self) -> None:
        d = self.draft
        d.name = self.name.get()
        d.read_from_snapshot = self.snapshot.get()
        d.sync_client_artefacts = self.sync_files.get()
        d.transient_editor_files = self.editor_files.get()
        d.patterns_text = self.patterns.get("1.0", "end-1c")
        d.count_floor_text = self.floor.get()
        d.percent_text = self.percent.get()
        d.share_on = self.share_on.get()
        d.share_text = self.share.get()
        d.on_arrival = self.on_arrival.get()
        d.scheduled = self.scheduled.get()
        d.days_kind = self._days_kind()
        d.schedule_days = {i for i, var in enumerate(self.days) if var.get()}
        d.month_day_text = self.month_day.get()
        d.repeating = self.repeating.get()
        d.schedule_time_text = self.time_text.get()
        d.every_text = self.every.get()
        d.window_on = self.window_on.get()
        d.window_start_text = self.window_start.get()
        d.window_end_text = self.window_end.get()
        d.paused = self.paused.get()
        d.cleanup = self.cleanup.get()
        d.keep_days_text = self.keep_days.get()
        d.reminder_days_text = self.reminder_days.get()

    def _changed(self) -> None:
        if self._pending is not None:
            self.window.after_cancel(self._pending)
        self._pending = self.window.after(_DELAY_MS, self._validate)

    def _ask_remotes(self) -> None:
        """Ask the engine about the draft's remote folders when one is new to it, off this
        thread; ``check`` is handed its answer, the latest asking replacing any before it."""
        wanted = self.draft.remote_endpoints
        if not set(wanted) - self._remotes_asked:
            return
        self._remotes_asked |= set(wanted)
        preferences, path = self.session.contents.preferences, self.session.store.path
        asking = self._asking = Background(lambda: ask_setup(preferences, path, wanted))
        self._wait_remotes(asking)

    def _wait_remotes(self, asking: Background) -> None:
        if not self.window.winfo_exists() or asking is not self._asking:
            return
        if not asking.done:
            self.window.after(_POLL_MS, lambda: self._wait_remotes(asking))
            return
        self._asking = None
        self.remote_setup = asking.value or RemoteSetup(SetupState.UNUSABLE, asking.failure)
        self._refresh_destinations(select=self._selected_destination())
        self._validate()

    def _validate(self) -> None:
        self._pending = None
        self._sync_draft()
        self._ask_remotes()
        self.result = check(self.draft, self.volumes, self.sync_roots, self.session.contents.jobs,
                            self.session.store.path, remote_setup=self.remote_setup,
                            asking=self._asking is not None)
        for child in self.checks.winfo_children():
            child.destroy()
        if not self.result.checks:
            ttk.Label(self.checks, text="No problems found.").grid(row=0, column=0, columnspan=2, sticky="w")
        for i, item in enumerate(self.result.checks):
            tone = _LEVEL_TONE[item.level]
            ttk.Label(self.checks, text=item.level, style=tone_style(tone)).grid(row=i, column=0, sticky="nw",
                                                                                 padx=(0, PAD))
            ttk.Label(self.checks, text=item.text, wraplength=em(self.window, 28), justify="left",
                      style=tone_style(tone)).grid(row=i, column=1, sticky="w")
        set_enabled(self.save_button, self.result.can_save)
        if self.window.winfo_ismapped():
            self._fit_sections()
            if not self._resized:
                self._asked(grow_to_content(self.window))

    def _with_fresh_volumes(self, action) -> None:
        """Run ``action(volumes)``; on a path no known volume carries, scan once more —
        the drive may have just been attached."""
        try:
            action(self.volumes)
        except EndpointError:
            self.volumes = tuple(self.session.scan().volumes)
            action(self.volumes)

    def _browse_source(self):
        path = choose_folder(self.window, "Choose the source folder", self.source_text.get(), must_exist=True)
        if path:
            self.source_text.set(path)
            self._take_source()

    def _left_source(self) -> None:
        """The source's field left: checked, unless the focus went to its own *Browse...*,
        which fills it, or out of the application, as when a path is copied elsewhere."""
        if not self.window.winfo_exists():
            return
        try:
            focus = self.window.focus_get()
        except KeyError:                        # a focus Tk cannot name, such as a combobox's open list
            focus = self.window
        if focus is not None and focus is not self.browse:
            self._take_source()

    def _take_source(self, *, again: bool = False) -> None:
        """Check the source's field as typed: an empty field is no source, a wrong path
        no source with what is wrong said beneath it. Text already shown or checked is
        not read again — the field of a drive not attached shows words, not a path —
        but a wrong path is, ``again``, on Enter or a save: its drive may since have been
        attached."""
        if not self.window.winfo_exists() or not self.draft.source_editable:
            return
        text = self.source_text.get()
        if text == self._source_shown and not (again and self._source_wrong):
            return
        path, problem = typed_path(text), None
        if not path:
            self.draft.clear_source()
        else:
            try:
                self._with_fresh_volumes(lambda v: self.draft.set_source(path, v))
            except EndpointError as exc:
                self.draft.clear_source()
                problem = path_problem(exc)
        self._refresh_source(problem)
        self._refresh_destinations()
        self._validate()

    def _add_destination(self):
        def take(path: str) -> None:
            self._with_fresh_volumes(lambda v: self.draft.add_destination(path, v))

        added = DestinationWindow(self.window, take, self.draft.add_remote_destination, self.session).added
        if self.window.winfo_exists():
            self.window.grab_set()              # the editor is modal again once the smaller window closes
        if added:
            self._refresh_destinations()
            self._validate()

    def _selected_destination(self) -> Optional[int]:
        selection = self.dest_tree.selection()
        return int(selection[0]) if selection else None

    def _remove_destination(self):
        position = self._selected_destination()
        if position is not None:
            self._remotes_asked.discard(self.draft.destinations[position].endpoint)    # added again: asked again
            self.draft.remove_destination(position)
            self._refresh_destinations()
            self._validate()

    def _move(self, step: int):
        position = self._selected_destination()
        if position is not None:
            self.draft.move_destination(position, step)
            self._refresh_destinations(select=max(0, min(len(self.draft.destinations) - 1, position + step)))
            self._validate()

    def _refresh_source(self, problem: Optional[str] = None):
        """Show the draft's source in its field, or, given ``problem``, keep what was typed
        and say beneath it what is wrong with it."""
        enabled, note = snapshot_choice(self.draft, self.volumes)
        set_enabled(self.snapshot_button, enabled)
        if enabled:                     # what the choice does is its hint; only why it is disabled is a line
            self.snapshot_note.configure(text="")
            self.snapshot_note.grid_remove()
        else:
            self.snapshot_note.configure(text=note)
            self.snapshot_note.grid()
        if problem is not None:
            self.source_detail.configure(text=problem, style=tone_style(Tone.PROBLEM))
        elif self.draft.source is None:
            self.source_text.set("")
            self.source_detail.configure(text=SOURCE_PROMPT, style=tone_style(Tone.NORMAL))
        else:
            folder, drive, _free = endpoint_line(self.draft.source, self.volumes)
            self.source_text.set(folder)
            self.source_detail.configure(text=drive, style=tone_style(Tone.NORMAL))
        self._source_shown = self.source_text.get()
        self._source_wrong = problem is not None

    def _refresh_destinations(self, select: Optional[int] = None):
        self.dest_tree.delete(*self.dest_tree.get_children())
        for i, destination in enumerate(self.draft.destinations):
            folder, drive, free = endpoint_line(destination.endpoint, self.volumes, self.remote_setup)
            self.dest_tree.insert("", "end", iid=str(i), values=(f"{i + 1}  {folder}", drive, free))
        self.dest_tree.configure(height=max(3, min(8, len(self.draft.destinations))))
        if select is not None and self.draft.destinations:
            self.dest_tree.selection_set(str(select))
        self._destination_buttons()

    def _destination_buttons(self):
        position = self._selected_destination()
        set_enabled(self.dest_buttons["remove"], position is not None)
        set_enabled(self.dest_buttons["up"], position is not None and position > 0)
        set_enabled(self.dest_buttons["down"], position is not None and position < len(self.draft.destinations) - 1)

    # --- saving --------------------------------------------------------------------------

    def _save(self):
        if self._pending is not None:
            self.window.after_cancel(self._pending)
        self._take_source(again=True)           # a path typed and not yet checked: clicking Save may leave no field
        self._validate()
        if self.result is None or not self.result.can_save:
            return
        job = self.result.job
        question = cleanup_question(job, self.draft.base)
        if question and choose(self.window, "Delete old versions", question, [("save", "Save"), ("back", "Back")],
                               default="back", cancel="back") != "save":
            return
        try:
            self.session.save(self.session.contents.with_job(job))
        except StoreConflict as exc:
            messagebox.showwarning("Not saved", f"{exc}. Your change was not saved; the list is reloaded so you can "
                                                "make it again.", parent=self.window)
            self.window.destroy()
            self.on_saved(None)
            return
        self.window.destroy()
        self.on_saved(job)


def choose_folder(parent: tk.Misc, title: str, typed: str, *, must_exist: bool) -> Optional[str]:
    """Windows' folder chooser, opened at the typed folder where it exists; the folder
    chosen, normalised, or None."""
    start = typed_path(typed)
    chosen = filedialog.askdirectory(parent=parent, title=title, mustexist=must_exist,
                                     initialdir=start if start and os.path.isdir(start) else None)
    return os.path.normpath(chosen) if chosen else None


class DestinationWindow:
    """*Add...*'s window: a destination's folder typed or chosen on this computer, or a
    remote the engine lists and a folder in it; one not made yet is taken — the first backup
    makes it, which a line beneath the fields says. What is entered is checked when *Add* or
    Enter is pressed; a wrong one is said in that line's place, and the window stays open.
    The configuration is opened when a remote is first chosen, on a thread, and stays open
    for browsing until the window closes. ``take(path)`` adds a folder and
    ``take_remote(remote, folder)`` a remote's, raising EndpointError for a wrong one;
    ``added`` says whether one was."""

    def __init__(self, parent: tk.Misc, take: Callable[[str], None], take_remote: Callable[[str, str], None],
                 session: GuiSession):
        self.take = take
        self.take_remote = take_remote
        self.session = session
        self.added = False
        self.access: Optional[RemoteAccess] = None
        w = self.window = tk.Toplevel(parent)
        w.title("Add destination")
        w.bind("<Destroy>", self._closed, add="+")
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        self.kind = tk.StringVar(value=_ON_THIS_COMPUTER)
        ttk.Radiobutton(frame, text="Folder on this computer", variable=self.kind, value=_ON_THIS_COMPUTER,
                        command=self._kind_changed).grid(row=0, column=0, sticky="w", padx=(0, PAD))
        self.path = tk.StringVar()
        entry = self.path_entry = ttk.Entry(frame, textvariable=self.path, width=40)
        entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.browse_button = ttk.Button(frame, text="Browse...", command=self._browse)
        self.browse_button.grid(row=0, column=3, padx=(PAD, 0))
        ttk.Radiobutton(frame, text="Folder of a remote", variable=self.kind, value=_ON_A_REMOTE,
                        command=self._kind_changed).grid(row=1, column=0, sticky="w", padx=(0, PAD), pady=(PAD, 0))
        self.remote = tk.StringVar()
        self.remote_box = ttk.Combobox(frame, textvariable=self.remote, state="disabled", width=14)
        self.remote_box.grid(row=1, column=1, sticky="w", padx=(0, PAD), pady=(PAD, 0))
        self.remote_box.bind("<<ComboboxSelected>>", lambda e: self._typed())
        self.remote_folder = tk.StringVar()
        self.folder_entry = ttk.Entry(frame, textvariable=self.remote_folder)
        self.folder_entry.grid(row=1, column=2, sticky="ew", pady=(PAD, 0))
        self.remote_browse = ttk.Button(frame, text="Browse...", command=self._browse_remote)
        self.remote_browse.grid(row=1, column=3, padx=(PAD, 0), pady=(PAD, 0))
        self.line = ttk.Label(frame, text="", wraplength=em(w, 28), justify="left")
        self.line.grid(row=2, column=1, columnspan=3, sticky="w", pady=(PAD // 2, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=4, sticky="e", pady=(em(w, 0.6), 0))
        self.add_button = ttk.Button(buttons, text="Add", default="active", command=self._add)
        self.add_button.grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: self._add())
        w.bind("<Escape>", lambda e: w.destroy())
        self.path.trace_add("write", lambda *a: self._typed())
        self._kind_changed()
        size_to_content(w, parent=parent)
        modal(w, parent)
        entry.focus_set()
        parent.wait_window(w)

    def _closed(self, event) -> None:
        if event.widget is self.window and self.access is not None:
            Background(self.access.close)          # the engine takes seconds to end

    @property
    def _on_a_remote(self) -> bool:
        return self.kind.get() == _ON_A_REMOTE

    def _kind_changed(self) -> None:
        remote = self._on_a_remote
        set_enabled(self.path_entry, not remote)
        set_enabled(self.browse_button, not remote)
        set_enabled(self.folder_entry, remote)
        set_enabled(self.remote_browse, remote and bool(self.remote.get()))
        self.remote_box.state(["!disabled", "readonly"] if remote and self.remote_box["values"] else ["disabled"])
        self._typed()
        if remote:
            self._open_remotes()

    def _open_remotes(self) -> None:
        """Have the engine open the configuration and list its remotes, once."""
        if self.access is not None:
            return
        preferences = self.session.contents.preferences
        if preferences.remotes.configuration is None:
            self._say(NAME_CONFIGURATION, Tone.ATTENTION)
            return
        self._say(ASKING_REMOTES, Tone.NORMAL)
        self.access = RemoteAccess(preferences, self.session.store.path)
        self._wait_remotes(Background(self.access.open))

    def _wait_remotes(self, opening: Background) -> None:
        if not self.window.winfo_exists():
            return
        if not opening.done:
            self.window.after(_POLL_MS, lambda: self._wait_remotes(opening))
            return
        if opening.failure:
            self.access = None                  # choosing a remote again tries again
            self._say(window_message(opening.failure), Tone.PROBLEM)
            return
        names = opening.value or ()
        self.remote_box.configure(values=names)
        if names:
            self.remote.set(names[0])
        self._kind_changed()
        if not names:
            self._say("The configuration holds no remote.", Tone.ATTENTION)

    def _say(self, text: str, tone: Tone) -> None:
        self.line.configure(text=text, style=tone_style(tone))
        grow_to_content(self.window)

    def _typed(self) -> None:
        """A change of what is entered: the line back to what a folder not made yet becomes,
        and *Add* enabled while there is something to add."""
        self.line.configure(text=NEW_FOLDER_NOTE, style=tone_style(Tone.NORMAL))
        ready = bool(self.remote.get()) if self._on_a_remote else bool(typed_path(self.path.get()))
        set_enabled(self.add_button, ready)

    def _browse(self) -> None:
        path = choose_folder(self.window, "Choose a destination folder", self.path.get(), must_exist=False)
        if path:
            self.path.set(path)

    def _browse_remote(self) -> None:
        chosen = RemoteFolderWindow(self.window, self.access, self.remote.get(),
                                    self.remote_folder.get().strip()).chosen
        if self.window.winfo_exists():
            self.window.grab_set()              # this window is modal again once the smaller one closes
            if chosen is not None:
                self.remote_folder.set(chosen)

    def _add(self) -> None:
        path = typed_path(self.path.get())
        if not (self.remote.get() if self._on_a_remote else path):
            return
        try:
            if self._on_a_remote:
                self.take_remote(self.remote.get(), self.remote_folder.get().strip())
            else:
                self.take(path)
        except EndpointError as exc:
            self.line.configure(text=path_problem(exc), style=tone_style(Tone.PROBLEM))
            grow_to_content(self.window)
            return
        self.added = True
        self.window.destroy()
