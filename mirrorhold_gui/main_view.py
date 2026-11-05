"""The main window: the job list, the runs in progress and the selection's history. Tk.

Three clocks drive it, all on the window's thread. Two take what is read on threads of
their own (``refresh``) from a queue every 200 ms: the refresher's observations (the
drives, markers, records and the activities' logs) and the runs' status files, read
every 500 ms — a status another program holds is waited for there, never here. The
third compares the job file with what was read every 2 s, so a change made elsewhere
shows a bar offering to reload instead of being overwritten. Rows are updated in place,
so a selection survives. A clock whose look fails keeps going: the window keeps what it
saw last, and the status bar says since when it has not looked and why, until a look
succeeds again. As an activity ends, the job list shows it as last seen at work until
the refresher has looked at what it left (``refresh.Settling``), so a first backup just
made is not offered again. With each look at the runs the window, while it has the focus,
clears the store's reminders (``mirrorhold.reminders``): the user has it in front of them,
which is all a reminder asks, and the notification-area icon that showed them goes.

History shows the selection's records: one job's, a group's jobs', the jobs several
selected rows reach, or every job's when nothing is selected (``history_model``). The
File menu saves what History shows, and every diagnostic log, to files the user chooses.

The list holds groups above their jobs and takes several rows at once (Ctrl and Shift);
what the selection allows is ``groups_model.Selection``'s. Jobs are moved between
groups from the right-click menu, and a group, or a job within its level, up or down
from it or with Alt+Up and Alt+Down. A row whose drive is not attached stays listed, grey
(``jobs_model``'s absent tone). The list and the Runs and History tabs share the window
across a divider with a visible grip (``common_view.DIVIDER_STYLE``) the user drags.

As the window closes, which groups the user closed and the window's place — its size,
position, whether it is maximised, and the divider — are kept in the job file's
remembered state (``session.save_view_state``); ``show`` opens the window there, fitted
to the monitors attached now (``window_model``), or at its content's size when nothing
usable is remembered. The size a maximised or minimised window returns to is followed as
it changes, since Tk reports only the size the window has now.

The window never applies a run: *Run now* and a confirmed preview start a core process
of their own (``session.start_run``) — of a job selected whole, or of the destinations
selected alone (``groups_model.Selection.chosen``), which *Preview...* and *Review and
confirm...* on a destination preview and run alone too — as *Verify...* starts a full
verification of each destination the selection reaches (``session.start_verification``;
one through the Verify dialog, several after one question, ``groups_model.verify_several``)
and *Replace backup copies...* — in the right-click menu of a destination whose latest
finished verification found copies that differ, and on that verification's details — a
repair of them after one question (``session.start_repair``, ``repair_model``), and
closing the window leaves every one going. A job's first backups into folders that held
nothing when last looked at are started only after one question
(``jobs_model.first_backup_question``), whose yes the run carries as its go-ahead. Every
start that ended without running since the runs' last look is said in one window
(``runs_model.not_started_words``).
"""
from __future__ import annotations

import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Sequence, Tuple

from mirrorhold import reminders
from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.marker import MarkerState
from mirrorhold.installation.release import read_release
from mirrorhold.locations import PROJECT_ROOT
from mirrorhold.jobs.model import new_group
from mirrorhold.jobs.store import LoadStatus, StoreBusy, StoreContents, StoreError
from mirrorhold.maintenance import verified
from mirrorhold.maintenance.verification import VerificationRecord, unfinished
from mirrorhold.records.diagnostics import ActivityLog, write_joined
from mirrorhold.records.status import ObservedRun, is_repair, is_verification
from mirrorhold.wording import counted

from .about_model import about_content, licence_path, notices_path
from .about_view import AboutWindow
from .common_view import (DIVIDER_STYLE, PAD, choose, em, frame_size, set_enabled, size_to_content, tag_tones,
                          work_areas)
from .editor_view import EditorWindow
from .groups_model import (NEW_GROUP_NOTE, Selection, collapsed_groups, delete_group_question, remove_jobs_question,
                           reordered, run_several, verify_several, with_collapsed)
from .groups_view import ask_group_name
from .history_model import JobHistory, empty_words, history_file_name, log_file_name, save_history
from .history_view import HistoryPanel
from .jobs_model import (DestinationRow, GroupRow, JobRow, arrange, build_rows, first_backup_question, job_has_run,
                         log_job, place_under, row_by_iid)
from .prefs_model import automation_problem
from .prefs_view import PreferencesWindow
from .preview_view import PreviewWindow
from .refresh import Observation, Refresher, RunsLook, RunWatcher, Settling, ended_runs
from .remote_view import open_place
from .repair_model import replace_question
from .repair_view import ReplaceWindow
from .runs_model import (closing_question, destination_places, kind_counts, live_panels, not_started_words,
                         unreadable_words, waits)
from .runs_view import RunsPanel
from .session import GuiSession, NotStarted, StoreConflict
from .skipped_model import unbacked_report
from .skipped_view import UnbackedWindow
from .startup_model import status_note
from .transfer_view import ExportWindow, import_jobs
from .verify_model import verify_dialog, verify_request
from .verify_view import VerifyWindow
from .window_model import Place, fit, geometry_text, parse_geometry, remembered_place, share, with_place

_DRAIN_MS, _RUNS_MS, _STORE_MS = 200, 500, 2000
_MIN_WIDTH_EM, _MIN_HEIGHT_EM = 30, 18


class MainWindow:
    def __init__(self, root: tk.Tk, session: GuiSession, *, refresh_interval: float = 4.0):
        self.root = root
        self.session = session
        self.observation = Observation()
        self.runs: List[ObservedRun] = []
        self.rows: Tuple[JobRow, ...] = ()
        self.entries: Tuple[object, ...] = ()
        self._by_iid: Dict[str, object] = {}
        self._collapsed = collapsed_groups(session.contents.preferences.gui_state)
        self._queue: "queue.Queue[Observation]" = queue.Queue()
        self._closing = False
        self._timers: Dict[str, str] = {}
        self._runs_queue: "queue.Queue[RunsLook]" = queue.Queue()
        self.settling = Settling()
        self._normal: Optional[Tuple[int, int, int, int]] = None   # (width, height, x, y) it returns to
        self._maximised = False
        self._job_logs: Dict[str, List[ActivityLog]] = {}
        self._runs_seen = self._store_seen = 0.0     # when each clock last looked successfully
        self._runs_failed = self._store_failed = ""  # why its latest look failed, while it does

        root.title("MirrorHold")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Configure>", self._configured, add="+")
        self._build_menu()
        outer = ttk.Frame(root, padding=(em(root, 0.5), em(root, 0.4)))
        outer.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        self._build_toolbar(outer).grid(row=0, column=0, sticky="ew")
        self.banner = ttk.Frame(outer, padding=(0, PAD // 2))
        self.banner_text = ttk.Label(self.banner, text="", style="AttentionHeadline.TLabel")
        self.banner_text.grid(row=0, column=0, sticky="w")
        self.banner_button = ttk.Button(self.banner, text="Reload", command=self.reload)
        self.banner_button.grid(row=0, column=1, padx=(PAD, 0))
        self.banner.grid(row=1, column=0, sticky="ew")
        self.banner.grid_remove()

        self.panes = panes = ttk.Panedwindow(outer, orient="vertical", style=DIVIDER_STYLE)
        panes.grid(row=3, column=0, sticky="nsew", pady=(PAD // 2, 0))
        top = ttk.Frame(panes)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        self._build_tree(top)
        self._build_destination_actions(top).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(PAD // 2, 0))
        panes.add(top, weight=3)
        self.notebook = ttk.Notebook(panes)
        runs_tab = ttk.Frame(self.notebook, padding=PAD)
        runs_tab.columnconfigure(0, weight=1)
        runs_tab.rowconfigure(0, weight=1)
        self.runs_panel = RunsPanel(runs_tab, session, self._status_of)
        self.runs_panel.grid(row=0, column=0, sticky="nsew")
        history_tab = ttk.Frame(self.notebook, padding=PAD)
        history_tab.columnconfigure(0, weight=1)
        history_tab.rowconfigure(0, weight=1)
        self.history = HistoryPanel(history_tab, session.opener, self._repair_of)
        self.history.grid(row=0, column=0, sticky="nsew")
        self.notebook.add(runs_tab, text="Runs")
        self.notebook.add(history_tab, text="History")
        panes.add(self.notebook, weight=2)
        self.status = ttk.Label(outer, text="")
        self.status.grid(row=4, column=0, sticky="ew", pady=(PAD // 2, 0))

        self.refresher = Refresher(session.layout, session.volume_source(), self._queue.put,
                                   interval=refresh_interval)
        self.refresher.set_contents(session.contents)
        self.refresher.start()
        self.watcher = RunWatcher(session.layout, self._runs_queue.put, interval=_RUNS_MS / 1000)
        self.watcher.start()
        self._actions()
        self._drain()
        self._drain_runs()
        self._watch_store()

    # --- building -----------------------------------------------------------------------

    def _build_menu(self):
        bar = tk.Menu(self.root)
        self.file_menu = tk.Menu(bar, tearoff=False)
        self.file_menu.add_command(label="Import jobs...", command=self.import_jobs)
        self.file_menu.add_command(label="Export jobs...", command=self.export_jobs)
        self.file_menu.add_command(label="Save history...", command=self.save_history)
        self.file_menu.add_command(label="Save log...", command=self.save_log)
        self.file_menu.add_command(label="Preferences...", command=self.preferences)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.close)
        bar.add_cascade(label="File", menu=self.file_menu)
        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="About", command=self.about)
        bar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=bar)

    def _build_toolbar(self, outer) -> ttk.Frame:
        bar = ttk.Frame(outer)
        bar.columnconfigure(4, weight=1)
        self.buttons = {}
        for i, (key, label, command) in enumerate((("new", "New job...", self.new_job),
                                                   ("group", "New group...", self.new_group),
                                                   ("edit", "Edit...", self.edit_job),
                                                   ("remove", "Remove...", self.remove_job))):
            self.buttons[key] = ttk.Button(bar, text=label, command=command)
            self.buttons[key].grid(row=0, column=i, padx=(PAD if i else 0, 0))
        self.buttons["preview"] = ttk.Button(bar, text="Preview...", command=self.preview)
        self.buttons["preview"].grid(row=0, column=5)
        self.buttons["run"] = ttk.Button(bar, text="Run now", default="active", command=self.run_now)
        self.buttons["run"].grid(row=0, column=6, padx=(PAD, 0))
        return bar

    def _build_tree(self, top):
        self.tree = ttk.Treeview(top, columns=("drive", "success", "free", "verified", "status"), height=8,
                                 selectmode="extended")
        self.tree.heading("#0", text="Job / destination")
        self.tree.heading("drive", text="Drive")
        self.tree.heading("success", text="Last backup")
        self.tree.heading("free", text="Free")
        self.tree.heading("verified", text="Verified")
        self.tree.heading("status", text="Status")
        font = tkfont.nametofont("TkDefaultFont")
        pad = em(top, 1.0)
        self.tree.column("#0", width=em(top, 17), stretch=True)
        self.tree.column("drive", width=font.measure("two drives match") + pad, stretch=False)
        self.tree.column("success", width=font.measure("2026-10-06 00:00") + pad, stretch=False)
        self.tree.column("free", width=font.measure("1023.9 GiB (100 %) low") + pad, stretch=False)
        self.tree.column("verified", width=font.measure("2026-10-06 - due") + pad, stretch=False)
        self.tree.column("status", width=font.measure("Completed - 12 files not backed up") + pad, stretch=True)
        tag_tones(self.tree)
        bar = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selection_changed())
        self.tree.bind("<Double-1>", lambda e: self._double_click())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<<TreeviewOpen>>", lambda e: self._toggled(opened=True))
        self.tree.bind("<<TreeviewClose>>", lambda e: self._toggled(opened=False))
        self.tree.bind("<Alt-Up>", lambda e: self._move_key(-1))
        self.tree.bind("<Alt-Down>", lambda e: self._move_key(1))

    def _build_destination_actions(self, top) -> ttk.Frame:
        row = ttk.Frame(top)
        ttk.Label(row, text="Destination:").grid(row=0, column=0, padx=(0, PAD))
        for i, (key, label, command) in enumerate((("open", "Open folder", self.open_folder),
                                                   ("versions", "Open versions", self.open_versions),
                                                   ("review", "Review and confirm...", self.review),
                                                   ("unbacked", "Files not backed up...", self.not_backed_up),
                                                   ("verify", "Verify...", self.verify))):
            self.buttons[key] = ttk.Button(row, text=label, command=command)
            self.buttons[key].grid(row=0, column=1 + i, padx=(PAD if i else 0, 0))
        return row

    # --- what is selected ------------------------------------------------------------------

    def selection(self) -> Selection:
        items = [self._by_iid[iid] for iid in self.tree.selection() if iid in self._by_iid]
        return Selection.of(items, self._by_iid)

    def selected(self) -> Tuple[Optional[JobRow], Optional[DestinationRow]]:
        """The one job selected — itself or through one of its destinations — and that
        destination; (None, None) for any other selection."""
        selection = self.selection()
        return selection.single_job, selection.single_destination

    def latest_volumes(self):
        return self.observation.volumes

    def _status_of(self, run_id: str) -> Optional[dict]:
        return next((r.status for r in self.runs if r.run_id == run_id), None)

    # --- clocks --------------------------------------------------------------------------

    def _later(self, name: str, ms: int, callback) -> None:
        """Schedule ``callback``; the window cancels every pending one when it closes, and
        schedules nothing once it is closing."""
        if not self._closing:
            self._timers[name] = self.root.after(ms, callback)

    def _drain(self):
        if self._closing:
            return
        try:
            observation = _latest(self._queue)
            if observation is not None:
                self.observation = observation
                self.settling.settle(observation.answered)
                self._rebuild()
        finally:
            self._later("drain", _DRAIN_MS, self._drain)

    def _drain_runs(self):
        if self._closing:
            return
        try:
            look = _latest(self._runs_queue)
            if look is not None:
                self._take_runs(look)
                self._attend()
        finally:
            self._later("runs", _DRAIN_MS, self._drain_runs)

    def _attend(self) -> None:
        """Clear the store's reminders while the window has the focus."""
        try:
            focused = self.root.focus_get() is not None
        except (KeyError, tk.TclError):     # the focus on a widget tkinter does not know, a menu's
            focused = False
        if focused:
            reminders.clear(self.session.layout)

    def _take_runs(self, look: RunsLook):
        """The runs as the watcher last read them. An activity that ended is held in the job
        list until the refresher, asked at once, has looked at what it left."""
        if look.failed:   # the runs stay as last seen; the status bar says since when
            self._runs_failed = look.failed
            self._status_line()
            return
        ended = ended_runs(self.runs, look.runs)
        self.runs, self._runs_seen, self._runs_failed = list(look.runs), look.taken, ""
        if ended:
            self.settling.hold(ended, self.refresher.refresh_now(markers=True))
        self.runs_panel.update(live_panels(self.runs, destination_places(self.session.contents.jobs)))
        self._rebuild()
        problems = self.session.launch_problems()
        if problems:              # one window for every start that failed since the last look
            title, text = not_started_words(problems)
            messagebox.showwarning(title, text, parent=self.root)

    def _watch_store(self):
        if self._closing:
            return
        try:
            try:
                changed = self.session.writable and self.session.changed_outside()
            except Exception as exc:  # the job file is looked at again; the status bar says since when
                self._store_failed = f"{type(exc).__name__}: {exc}"
                self._status_line()
                return
            if self._store_failed:
                self._store_failed = ""
                self._status_line()
            self._store_seen = time.time()
            if changed:
                self.banner_text.configure(text="The jobs were changed outside this window. Nothing is overwritten.")
                self.banner_button.grid()
                self.banner.grid()
        finally:
            self._later("store", _STORE_MS, self._watch_store)

    # --- showing -------------------------------------------------------------------------

    def _rebuild(self):
        obs = self.observation
        jobs = self.session.contents.jobs
        self._job_logs = {}
        for log in obs.logs:
            job_id = log_job(log, jobs)
            if job_id is not None:
                self._job_logs.setdefault(job_id, []).append(log)
        self.rows = build_rows(self.session.contents, obs.volumes, obs.markers, obs.records,
                               self.settling.listed(self.runs), obs.verifications, occupancy=obs.occupancy,
                               logs=obs.logs, repairs=obs.repairs)
        self.entries = arrange(self.session.contents, self.rows)
        self._by_iid = row_by_iid(self.entries)
        wanted: List[str] = []
        for position, entry in enumerate(self.entries):
            if isinstance(entry, GroupRow):
                wanted.append(entry.iid)
                self._put("", position, entry.iid, entry.label, ("", entry.last_success, "", "", entry.status),
                          entry.tone.value, open_=entry.group_id not in self._collapsed)
                for j_position, row in enumerate(entry.jobs):
                    self._put_job(entry.iid, j_position, row, wanted)
            else:
                self._put_job("", position, entry, wanted)
        for iid in self._items("") - set(wanted):
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self._selection_changed()
        self._status_line()

    def _put_job(self, parent, position, row: JobRow, wanted: List[str]):
        wanted.append(row.iid)
        self._put(parent, position, row.iid, f"{row.name}      {row.source}",
                  (row.drive, row.last_success, "", "", row.status), row.tone.value, open_=True)
        for d_position, d in enumerate(row.destinations):
            wanted.append(d.iid)
            self._put(row.iid, d_position, d.iid, f"{d.index}  {d.folder}",
                      (d.drive, d.last_success, d.free, d.verified, d.status), d.tone.value)

    def _items(self, parent) -> set:
        """Every item under ``parent``, at any depth."""
        found = set()
        for iid in self.tree.get_children(parent):
            found.add(iid)
            found |= self._items(iid)
        return found

    def _put(self, parent, position, iid, text, values, tag, open_=False):
        if self.tree.exists(iid):
            self.tree.item(iid, text=text, values=values, tags=(tag,))
            if self.tree.parent(iid) != parent or self.tree.index(iid) != position:
                self.tree.move(iid, parent, position)
        else:
            self.tree.insert(parent, position, iid=iid, text=text, values=values, tags=(tag,), open=open_)

    def _status_line(self):
        live = [r for r in self.runs if r.alive and r.readable and not is_verification(r.status)
                and not is_repair(r.status)]
        checks = [r for r in self.runs if r.alive and r.readable and is_verification(r.status)]
        fixes = sum(1 for r in self.runs if r.alive and r.readable and is_repair(r.status))
        unreadable = unreadable_words(self.runs)
        waiting = sum(1 for r in live if waits(r.status))      # one starting counts as in progress
        working = len(live) - waiting
        parts = [counted(len(self.rows), "job")]
        runs = f"{counted(working, 'run')} in progress"
        if waiting:
            runs += f", {waiting} waiting"
        if checks:
            runs += f", {counted(len(checks), 'verification')}"
            checks_waiting = sum(1 for r in checks if waits(r.status))
            if checks_waiting:
                runs += f" ({checks_waiting} waiting)"
        if fixes:
            runs += f", {counted(fixes, 'repair')}"
        parts.append(runs)
        if unreadable:
            parts.append(unreadable)
        if self.observation.failed:
            parts.append(_not_checked("drives and records", self.observation.taken, self.observation.failed))
        elif self.observation.taken:
            parts.append("drives checked " + datetime.fromtimestamp(self.observation.taken).strftime("%H:%M:%S"))
        if self.observation.error:
            parts.append(f"drives could not be checked: {self.observation.error}")
        if self._runs_failed:
            parts.append(_not_checked("runs", self._runs_seen, self._runs_failed))
        if self._store_failed:
            parts.append(_not_checked("the job file", self._store_seen, self._store_failed))
        if not self.session.writable:
            parts.append("read-only: the job file belongs to a newer version")
        elif self.session.loaded is not None and status_note(self.session.loaded):
            parts.append(status_note(self.session.loaded))
        self.status.configure(text=" - ".join(parts))

    def _selection_changed(self):
        self._actions()
        self._show_history()

    def _history_jobs(self) -> Tuple[List[JobRow], str]:
        """The jobs History shows, and what it says while it shows no record: one job, a
        group's jobs, the jobs several selected rows reach, or every job when nothing is
        selected."""
        selection = self.selection()
        if not selection.items:
            return list(self.rows), empty_words([], every=True)
        group = selection.single_group
        if group is not None:
            return list(group.jobs), empty_words([], group=group.name)
        jobs = list(selection.reached)
        return jobs, empty_words([job.name for job in jobs])

    def _show_history(self):
        jobs, empty = self._history_jobs()
        obs = self.observation
        histories, roots = [], {}
        for job in jobs:
            histories.append(JobHistory(job.job_id, job.name, tuple(obs.records.get(job.job_id, ())),
                                        tuple(obs.verifications.get(job.job_id, ())),
                                        {d.index: d.name for d in job.destinations},
                                        tuple(self._job_logs.get(job.job_id, ())),
                                        tuple(obs.repairs.get(job.job_id, ())),
                                        tuple(d.marker_id for d in job.destinations if d.remote is not None)))
            for d in job.destinations:
                roots[(job.job_id, d.index)] = d.root if d.can_open else None
        self.history.show(histories, empty, lambda job_id, index: roots.get((job_id, index)), self._live_ids(),
                          obs.logs)
        _set_entry(self.file_menu, "Save history...", bool(self.history.rows))

    def _live_ids(self):
        return frozenset(r.run_id for r in self.runs if r.alive)

    def _actions(self):
        selection = self.selection()
        job, destination = selection.single_job, selection.single_destination
        group = selection.single_group
        writable = self.session.writable
        set_enabled(self.buttons["new"], writable)
        set_enabled(self.buttons["group"], writable)
        set_enabled(self.buttons["edit"], writable and (job is not None or group is not None))
        set_enabled(self.buttons["remove"], writable and (job is not None or group is not None
                                                          or bool(selection.removable)))
        set_enabled(self.buttons["preview"], writable and job is not None)
        set_enabled(self.buttons["run"], writable and bool(selection.startable))
        set_enabled(self.buttons["open"], destination is not None and destination.can_open)
        set_enabled(self.buttons["versions"], destination is not None and destination.can_open)
        set_enabled(self.buttons["review"], writable and destination is not None and destination.needs_review
                    and not (job is not None and job.running))
        set_enabled(self.buttons["unbacked"], destination is not None and destination.has_unbacked)
        set_enabled(self.buttons["verify"], writable and bool(selection.verifiable))
        for label in ("Import jobs...", "Preferences..."):
            _set_entry(self.file_menu, label, writable)

    def _double_click(self):
        job, destination = self.selected()
        if destination is not None and destination.needs_review:
            self.review()
        elif job is not None and destination is None and self.session.writable:
            self.edit_job()

    # --- actions -------------------------------------------------------------------------

    def _after_change(self, _job=None):
        problem = automation_problem(self.session.automation)
        self.session.automation = None
        if problem:
            messagebox.showwarning("Automatic runs", problem, parent=self.root)
        if self.session.changed_outside():
            self.reload()
            return
        self.refresher.set_contents(self.session.contents)
        self._rebuild()

    def _save(self, contents: StoreContents, failure: str) -> bool:
        """Save the jobs as changed here; a save refused or failed is reported, and the
        list reloaded."""
        try:
            self.session.save(contents)
        except (StoreConflict, StoreError, OSError) as exc:
            messagebox.showwarning(failure, f"{exc}. The list is reloaded.", parent=self.root)
            self.reload()
            return False
        self._after_change()
        return True

    def reload(self):
        try:
            result = self.session.load()
        except StoreBusy:
            messagebox.showwarning("Not reloaded", "The job file could not be read: another program kept it open; "
                                                   "try again.", parent=self.root)
            return
        self.banner.grid_remove()
        if result.status is LoadStatus.CORRUPT:
            messagebox.showwarning("MirrorHold", result.detail, parent=self.root)
        self.refresher.set_contents(self.session.contents)
        self._rebuild()

    def new_job(self):
        EditorWindow(self.root, self.session, self.observation.volumes, on_saved=self._after_change)

    def edit_job(self):
        selection = self.selection()
        if selection.single_group is not None:
            self.rename_group(selection.single_group)
            return
        job_row = selection.single_job
        if job_row is None:
            return
        job = self.session.contents.job(job_row.job_id)
        records = self.observation.records.get(job.job_id, ())
        has_run = job_has_run(records) or any(d.marker is MarkerState.MATCH for d in job_row.destinations)
        EditorWindow(self.root, self.session, self.observation.volumes, job=job, source_editable=not has_run,
                     on_saved=self._after_change)

    def remove_job(self):
        selection = self.selection()
        if selection.single_group is not None:
            self.delete_group(selection.single_group)
            return
        if selection.removable:
            self.remove_jobs(selection.removable)
            return
        job_row = selection.single_job
        if job_row is None:
            return
        answer = choose(self.root, "Remove job",
                        f"Remove the job \"{job_row.name}\"?\nNothing is deleted on any drive: its destinations keep "
                        "their files, versions and run records.",
                        [("remove", "Remove"), ("cancel", "Cancel")], default="cancel", cancel="cancel")
        if answer != "remove":
            return
        self._save(self.session.contents.without_job(job_row.job_id), "Not removed")

    def remove_jobs(self, rows: Sequence[JobRow]):
        title, text = remove_jobs_question(rows)
        if choose(self.root, title, text, [("remove", "Remove"), ("cancel", "Cancel")], default="cancel",
                  cancel="cancel") != "remove":
            return
        contents = self.session.contents
        for row in rows:
            contents = contents.without_job(row.job_id)
        self._save(contents, "Not removed")

    # --- groups ----------------------------------------------------------------------------

    def new_group(self):
        name = ask_group_name(self.root, self.session.contents, title="New group", button="Create",
                              note=NEW_GROUP_NOTE)
        if name is not None:
            self._save(self.session.contents.with_group(new_group(name)), "Group not created")

    def rename_group(self, row: GroupRow):
        group = self.session.contents.group(row.group_id)
        if group is None:
            return
        name = ask_group_name(self.root, self.session.contents, title="Rename group", button="Rename",
                              current=group.name, besides=group.group_id)
        if name is not None and name != group.name:
            self._save(self.session.contents.with_group(replace(group, name=name)), "Group not renamed")

    def delete_group(self, row: GroupRow):
        title, text = delete_group_question(row)
        if choose(self.root, title, text, [("delete", "Delete group"), ("cancel", "Cancel")], default="cancel",
                  cancel="cancel") != "delete":
            return
        if self._save(self.session.contents.without_group(row.group_id), "Group not deleted"):
            self._collapsed.discard(row.group_id)

    def move_to(self, job_ids: Sequence[str], group_id: Optional[str]):
        """Move the jobs into the group, or out of every group with None."""
        try:
            contents = self.session.contents.regrouped(job_ids, group_id)
        except ContractError as exc:
            messagebox.showwarning("Not moved", f"{exc}. The list is reloaded.", parent=self.root)
            self.reload()
            return
        self._save(contents, "Not moved")

    def move_to_new_group(self, job_ids: Sequence[str]):
        name = ask_group_name(self.root, self.session.contents, title="New group", button="Create")
        if name is not None:
            group = new_group(name)
            self._save(self.session.contents.with_group(group).regrouped(job_ids, group.group_id), "Not moved")

    def move(self, step: int) -> None:
        """Move the selected group, or job, ``step`` places within its level and keep it
        selected; a save refused or failed is reported, and the list reloaded."""
        if not self.session.writable:
            return
        moved = reordered(self.session.contents, self.selection(), step)
        if moved is None:
            return
        try:
            self.session.save_order(moved)
        except (StoreConflict, StoreError, OSError) as exc:
            messagebox.showwarning("Not moved", f"{exc}. The list is reloaded.", parent=self.root)
            self.reload()
            return
        self.refresher.set_contents(self.session.contents)
        self._rebuild()
        for iid in self.tree.selection():
            self.tree.see(iid)

    def _move_key(self, step: int) -> str:
        self.move(step)
        return "break"                   # the list's own Up and Down would move the selection too

    def _context_menu(self, event):
        """Right-click: the row under the pointer joins the selection unless it is in it."""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self._selection_changed()
        menu = self.context_menu()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def context_menu(self) -> tk.Menu:
        """The selection's right-click menu: a group's own actions and its moves up and
        down; otherwise a destination's verification — or, for a job or several items, the
        verification of every destination they reach — the job's moves up and down, and
        moving the selected jobs between groups. An action that cannot be taken is shown
        disabled."""
        selection = self.selection()
        writable = self.session.writable
        menu = tk.Menu(self.tree, tearoff=False)

        def moves():
            for label, step, keys in (("Move up", -1, "Alt+Up"), ("Move down", 1, "Alt+Down")):
                can = writable and reordered(self.session.contents, selection, step) is not None
                menu.add_command(label=label, accelerator=keys, command=lambda s=step: self.move(s),
                                 state=_menu_state(can))

        if selection.single_group is not None:
            menu.add_command(label="Run now", command=self.run_now,
                             state=_menu_state(writable and bool(selection.startable)))
            menu.add_command(label="Verify...", command=self.verify,
                             state=_menu_state(writable and bool(selection.verifiable)))
            menu.add_command(label="Rename...", command=self.edit_job, state=_menu_state(writable))
            menu.add_command(label="Delete group...", command=self.remove_job, state=_menu_state(writable))
            menu.add_separator()
            moves()
        else:
            destination = selection.single_destination
            if destination is not None:
                if destination.unfinished is not None:
                    menu.add_command(label="Continue verification", command=self.continue_verification,
                                     state=_menu_state(writable and destination.can_verify))
                menu.add_command(label="Verify...", command=self.verify,
                                 state=_menu_state(writable and destination.can_verify))
                if destination.found_differ is not None:
                    menu.add_command(label="Replace backup copies...", command=self.replace_copies,
                                     state=_menu_state(writable and destination.can_repair))
                menu.add_separator()
            elif selection.destinations:
                menu.add_command(label="Verify...", command=self.verify,
                                 state=_menu_state(writable and bool(selection.verifiable)))
                menu.add_separator()
            moves()
            menu.add_separator()
            contents = self.session.contents
            jobs = selection.movable
            current = {contents.group_of(job_id) for job_id in jobs}
            move = tk.Menu(menu, tearoff=False)
            for group in contents.groups:
                move.add_command(label=group.name, command=lambda g=group.group_id: self.move_to(jobs, g),
                                 state=_menu_state(current != {group}))
            if contents.groups:
                move.add_separator()
            move.add_command(label="New group...", command=lambda: self.move_to_new_group(jobs))
            move.add_command(label="No group", command=lambda: self.move_to(jobs, None),
                             state=_menu_state(current != {None}))
            menu.add_cascade(label="Move to group", menu=move, state=_menu_state(writable and bool(jobs)))
        return menu

    def _toggled(self, *, opened: bool):
        """A group opened or closed in the list: remembered for the next window."""
        entry = self._by_iid.get(self.tree.focus())
        if isinstance(entry, GroupRow):
            if opened:
                self._collapsed.discard(entry.group_id)
            else:
                self._collapsed.add(entry.group_id)

    def _remember_view(self):
        """Keep which groups are closed, and the window's place, in the job file's
        remembered state. A window that cannot save it — read-only, or the file changed
        elsewhere — keeps nothing: it is a convenience, and closing must not stop on it."""
        contents = self.session.contents
        state = dict(contents.preferences.gui_state)
        wanted = with_collapsed(state, self._collapsed & {g.group_id for g in contents.groups})
        place = self.place()
        if place is not None:
            wanted = with_place(wanted, place)
        if wanted == state or not self.session.writable:
            return
        try:
            self.session.save_view_state(wanted)
        except (StoreConflict, StoreError, OSError):
            pass

    # --- the window's place -----------------------------------------------------------------

    def show(self) -> None:
        """Open the window where it was when it last closed, fitted to the monitors
        attached now; at its content's size when nothing usable is remembered."""
        root = self.root
        place = remembered_place(self.session.contents.preferences.gui_state)
        if place is None:
            initial_size(root)
            root.deiconify()
            return
        areas = work_areas(root)
        fitted = fit(place, areas)
        root.geometry(geometry_text(fitted))
        root.minsize(min(fitted.width, em(root, _MIN_WIDTH_EM)), min(fitted.height, em(root, _MIN_HEIGHT_EM)))
        root.deiconify()
        root.update_idletasks()
        framed = fit(place, areas, frame_size(root))
        if framed != fitted:
            root.geometry(geometry_text(framed))
        self._normal, self._maximised = (framed.width, framed.height, framed.x, framed.y), place.maximised
        if place.maximised:
            root.state("zoomed")
        if place.divider is not None:
            self._divider_once(place.divider)

    def _divider_once(self, list_share: float) -> None:
        """Set the divider once the list and the tabs are first laid out: before then the
        paned window has no height to share, and a divider set earlier would be lost."""
        def laid_out(event):
            if event.height > 1:
                self.panes.unbind("<Configure>", binding)
                self.root.after_idle(lambda: self.set_divider(list_share))
        binding = self.panes.bind("<Configure>", laid_out, add="+")

    def set_divider(self, list_share: float) -> None:
        """Give the job list ``list_share`` of the height it shares with the tabs."""
        height = self.panes.winfo_height()
        if height > 1:
            self.panes.sashpos(0, round(list_share * height))

    def divider(self) -> Optional[float]:
        """The job list's share of the height it shares with the tabs; None before the
        window has been laid out."""
        height = self.panes.winfo_height()
        return share(self.panes.sashpos(0) / height) if height > 1 else None

    def place(self) -> Optional[Place]:
        """Where the window is: the size and position it has, or returns to when it is
        maximised or minimised, and the divider; None before it has been shown."""
        if self._normal is None:
            return None
        width, height, x, y = self._normal
        return Place(x, y, width, height, self._maximised, self.divider())

    def _configured(self, event) -> None:
        """Follow the size the window returns to: Tk reports only the one it has now."""
        if event.widget is not self.root:
            return
        state = self.root.state()
        if state == "normal":
            geometry = parse_geometry(self.root.geometry())
            if geometry is not None:
                self._normal = geometry
            self._maximised = False
        elif state == "zoomed":
            self._maximised = True

    def preview(self, job_id: Optional[str] = None, destinations: Optional[Sequence[str]] = None):
        """Preview ``job_id`` for ``destinations`` (markers; every one for None); with no job
        named, the selected job, for the selected destination alone when one is selected."""
        if job_id is None:
            job_row, destination = self.selected()
            if job_row is None:
                return
            job_id = job_row.job_id
            destinations = (destination.marker_id,) if destination is not None else None
        PreviewWindow(self.root, self.session, job_id, destinations=destinations, volumes=self.latest_volumes,
                      on_started=self._run_started)

    def review(self):
        """Review the selected destination's run: the one question before its first backup,
        or its preview; what either starts runs that destination alone."""
        job_row, destination = self.selected()
        if job_row is None:
            return
        destinations = (destination.marker_id,) if destination is not None else None
        if destination is not None and destination.first_backup:
            self._start_first_backups(job_row, destinations)
        else:
            self.preview(job_row.job_id, destinations)

    def run_now(self):
        """Start what the selection names (``Selection.chosen``): one job — whole, or for
        the destinations selected — asking first when the run's first backups wait for the
        go-ahead; a group or several jobs ask once, then start each job not running
        already."""
        selection = self.selection()
        chosen = selection.chosen
        if selection.single_group is None and len(selection.reached) == 1:
            job_row = selection.reached[0]
            if job_row.running:
                return
            destinations = chosen.get(job_row.job_id)
            if job_row.serving(destinations).first_backups:
                self._start_first_backups(job_row, destinations)
            elif self._start(job_row, destinations=destinations):
                self._run_started()
            return
        if not selection.startable:
            return
        group = selection.single_group
        plan = run_several(selection.reached, group.name if group is not None else None,
                           turns=self.session.contents.preferences.activities.drive_turns, chosen=chosen)
        if choose(self.root, plan.title, plan.text, [("run", plan.button), ("cancel", "Cancel")], default="run",
                  cancel="cancel") != "run":
            return
        rows = {job.job_id: job for job in selection.reached}
        started = [self._start(rows[job_id], destinations=plan.destinations.get(job_id),
                               first_backup=job_id in plan.first_backup) for job_id in plan.start]
        if any(started):
            self._run_started()

    def _start_first_backups(self, job_row: JobRow, destinations: Optional[Sequence[str]] = None):
        """Ask the one question before the first backups of a run of ``destinations``
        (markers; every one for None); yes starts that run with the go-ahead, no starts
        nothing."""
        question = first_backup_question(job_row.serving(destinations))
        if question is None or job_row.running:
            return
        if choose(self.root, question.title, question.text, [("start", question.button), ("cancel", "Cancel")],
                  default="start", cancel="cancel") != "start":
            return
        if self._start(job_row, destinations=destinations, first_backup=True):
            self._run_started()

    def _start(self, job_row: JobRow, **options) -> bool:
        """Start a run of the job (``session.start_run``); one the session would not start
        is said, and False."""
        try:
            self.session.start_run(job_row.job_id, **options)
        except NotStarted as exc:
            messagebox.showwarning("Run not started", f'The run of "{job_row.name}" was not started: {exc}.',
                                   parent=self.root)
            return False
        return True

    def _run_started(self):
        self.notebook.select(0)
        self._later("started", 300, self.watcher.look_now)

    def open_folder(self):
        _, destination = self.selected()
        if destination is not None and destination.can_open:
            self._open(place_under(destination.root), "Open folder")

    def not_backed_up(self):
        job, destination = self.selected()
        if destination is not None and destination.has_unbacked:
            UnbackedWindow(self.root, unbacked_report(destination.last_attempt, job.name if job else ""))

    def _list_kept(self, destination: DestinationRow) -> bool:
        """Whether a list of verified copies is kept for the destination to build on."""
        records = self.observation.verifications.get(destination.job_id, ()) if self.observation else ()
        return verified.kept(self.session.layout, records, destination.marker_id) is not None

    def verify(self):
        """Verify what the selection reaches: one destination through the Verify dialog, its
        choice of new copies, a full verification or continuing included; several after one
        question answered with new copies or a full verification, each continuing its
        destination's unfinished pass of that kind."""
        selection = self.selection()
        if not self.session.writable or not selection.verifiable:
            return
        if len(selection.destinations) == 1:
            self._verify_one(selection.destinations[0])
            return
        named = selection.single_group or selection.single_job
        kept = {(d.job_id, d.index) for d in selection.destinations if d.can_verify and self._list_kept(d)}
        plan = verify_several(selection.destinations, {job.job_id: job for job in self.rows},
                              named.name if named is not None else None,
                              turns=self.session.contents.preferences.activities.drive_turns, kept=kept)
        if not plan.start:
            return
        answer = choose(self.root, plan.title, plan.text,
                        [("new", plan.new_copies_button), ("full", plan.full_button), ("cancel", "Cancel")],
                        default="full" if plan.full_default else "new", cancel="cancel")
        if answer not in ("new", "full"):
            return
        markers = {(d.job_id, d.index): d.marker_id for d in selection.destinations}
        for job_id, index in plan.start:
            self.session.start_verification(job_id, markers[(job_id, index)], new_copies=answer == "new")
        self._run_started()

    def _verify_one(self, destination: DestinationRow):
        job = self.session.contents.job(destination.job_id)
        if job is None or not destination.can_verify:
            return
        previous = unfinished(self.observation.verifications.get(job.job_id, ()), destination.marker_id,
                              self._live_ids())
        marker = destination.marker_id
        kept = self._list_kept(destination)
        dialog = verify_dialog(job.name, destination, job.verification_reminder_days, previous, list_kept=kept)

        def start(choice: str):
            new_copies, restart = verify_request(choice, previous, list_kept=kept)
            self._start_verification(job.job_id, marker, restart, new_copies)

        VerifyWindow(self.root, dialog, start)

    def continue_verification(self):
        """Take up the selected destination's unfinished pass at once, without asking."""
        job_row, destination = self.selected()
        if job_row is None or destination is None or destination.unfinished is None or not destination.can_verify \
                or not self.session.writable:
            return
        self._start_verification(job_row.job_id, destination.marker_id, False, not destination.unfinished.full)

    def _start_verification(self, job_id: str, marker: str, restart: bool, new_copies: bool = False):
        self.session.start_verification(job_id, marker, restart=restart, new_copies=new_copies)
        self._run_started()

    def replace_copies(self):
        """Ask before replacing the backup copies the selected destination's latest
        finished verification found to differ, and start the repair on yes."""
        job_row, destination = self.selected()
        if job_row is not None and destination is not None:
            self._ask_replace(job_row, destination)

    def _repair_of(self, verification: VerificationRecord):
        """What a verification's details window calls to replace the copies it found to
        differ — handed what to call once the repair has started — or None while a repair
        of them cannot start."""
        rows = [(job, d) for job in self.rows for d in job.destinations if job.job_id == verification.job_id
                and d.marker_id == verification.marker_id]
        if not rows or not self.session.writable:
            return None
        job_row, destination = rows[0]
        found = destination.found_differ
        if not destination.can_repair or found is None or found.verification_id != verification.verification_id:
            return None
        return lambda started: self._ask_replace(job_row, destination, started)

    def _ask_replace(self, job_row: JobRow, destination: DestinationRow, started=lambda: None):
        if not destination.can_repair or destination.found_differ is None or not self.session.writable:
            return
        job = self.session.contents.job(job_row.job_id)
        if job is None:
            return
        question = replace_question(job.name, destination.index, destination.name, destination.found_differ,
                                    self.observation.repairs.get(job.job_id, ()), job.version_cleanup.keep_days,
                                    remote=destination.remote is not None)
        marker = destination.marker_id

        def replace():
            self.session.start_repair(job.job_id, marker)
            started()
            self._run_started()

        ReplaceWindow(self.root, question, replace)

    def open_versions(self):
        _, destination = self.selected()
        if destination is None or not destination.can_open:
            return
        folder = destination.versions_folder
        if isinstance(folder, Path) and not folder.is_dir():
            messagebox.showinfo("Open versions", "Nothing has been moved into versions on this destination yet.",
                                parent=self.root)
            return
        self._open(folder, "Open versions")

    def _open(self, place, title):
        open_place(self.root, self.session.opener, place, title)

    def import_jobs(self):
        if self.session.writable:
            import_jobs(self.root, self.session, self.observation.volumes, self._after_change)

    def export_jobs(self):
        ExportWindow(self.root, self.session)

    def save_history(self):
        """Save what History shows to a CSV file the user chooses."""
        rows = self.history.rows
        if not rows:
            return
        chosen = filedialog.asksaveasfilename(parent=self.root, title="Save history", defaultextension=".csv",
                                              initialfile=history_file_name(date.today()),
                                              filetypes=(("CSV file", "*.csv"), ("All files", "*.*")))
        if not chosen:
            return
        try:
            save_history(Path(chosen), rows)
        except OSError as exc:
            messagebox.showwarning("Not saved", str(exc), parent=self.root)

    def save_log(self):
        """Save every diagnostic log kept beside the job file, as one text, to a file the
        user chooses."""
        chosen = filedialog.asksaveasfilename(parent=self.root, title="Save log", defaultextension=".txt",
                                              initialfile=log_file_name(date.today()),
                                              filetypes=(("Text file", "*.txt"), ("All files", "*.*")))
        if not chosen:
            return
        try:
            write_joined(self.session.layout, Path(chosen))
        except OSError as exc:
            messagebox.showwarning("Not saved", str(exc), parent=self.root)

    def preferences(self):
        if self.session.writable:
            PreferencesWindow(self.root, self.session, on_saved=self._after_change)

    def about(self):
        AboutWindow(self.root, about_content(read_release(PROJECT_ROOT), licence_path(), notices_path()))

    def close(self):
        counts = kind_counts(self.runs)
        if any(counts):
            text = closing_question(*counts)
            if choose(self.root, "MirrorHold", text, [("close", "Close window"), ("keep", "Keep open")],
                      default="close", cancel="keep") != "close":
                return
        self._remember_view()
        self._closing = True
        for timer in self._timers.values():
            self.root.after_cancel(timer)
        self.refresher.stop()
        self.watcher.stop()
        self.root.destroy()


def _latest(waiting: queue.Queue):
    """The newest item ``waiting`` holds, the older ones dropped; None when it holds none."""
    latest = None
    try:
        while True:
            latest = waiting.get_nowait()
    except queue.Empty:
        return latest


def initial_size(root: tk.Tk) -> None:
    size_to_content(root, min_width=em(root, _MIN_WIDTH_EM), min_height=em(root, _MIN_HEIGHT_EM))


def _menu_state(enabled: bool) -> str:
    return "normal" if enabled else "disabled"


def _set_entry(menu: tk.Menu, label: str, enabled: bool) -> None:
    """Enable or disable a menu entry, leaving it alone when it already is so: Tk on Windows
    rebuilds the whole native menu at every entry configured, even to the state it has,
    which a menu held open shows as a flicker at every look the window takes."""
    state = _menu_state(enabled)
    if str(menu.entrycget(label, "state")) != state:
        menu.entryconfigure(label, state=state)


def _not_checked(what: str, since: float, why: str) -> str:
    """'runs not checked since 10:12:01: <why>', or, before any look succeeded, 'runs could
    not be checked: <why>'."""
    if since:
        return f"{what} not checked since {datetime.fromtimestamp(since).strftime('%H:%M:%S')}: {why}"
    return f"{what} could not be checked: {why}"
