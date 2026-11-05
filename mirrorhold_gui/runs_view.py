"""The Runs tab and the cancel dialog. Tk.

Each run in progress has a row, updated in place from its status (``runs_model``); its
buttons ask the run, through ``records.status.request``, to pause, resume or cancel —
the run's own process does it. A row whose status cannot be read now shows no progress
and its buttons stay disabled. A graceful cancel is the default; stopping at once is the
second choice and is confirmed. A verification's row has *Stop...* instead, one question:
nothing is being written, so there is nothing in flight to finish. A repair's row has
*Stop...* too, one question: it stops after the files in flight, the copies already
replaced staying replaced.

A request is written off the window's thread: a control file another program keeps open
holds it for up to its patience, and the window answers meanwhile. One that did not
reach its run says so.

``RunRequests`` holds what a row's two buttons ask and the questions before them, so the
notification-area icon (``tray_view``) asks exactly what the Runs tab asks, in its words.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, Optional, Sequence

from mirrorhold.exchange import FileBusy
from mirrorhold.locations import RuntimeLayout
from mirrorhold.records.status import CancelMode, NoSuchRun, request
from mirrorhold.wording import agreeing

from .common_view import PAD, ScrollFrame, choose, em, modal, set_enabled, size_to_content
from .formatting import files
from .runs_model import (NO_PLACES, REQUEST_TITLES, Places, RunPanel, can_ask_cancel, can_ask_pause, cancel_words,
                         destination_number, destination_places, in_flight_left, not_reached_words, pause_words)
from .session import GuiSession

EMPTY_TEXT = "No run or verification in progress."
_ANSWER_MS = 100
STOP_VERIFYING_TEXT = "Stop verifying? The files already compared are kept; Verify... continues from here."
STOP_REPAIRING_TEXT = "Stop replacing copies? The copies already replaced stay replaced; the rest are left as they are."

StatusOf = Callable[[str], Optional[dict]]


class _RunRow:
    def __init__(self, parent, panel: RunPanel, actions: "RunsPanel"):
        self.run_id = panel.run_id
        self.actions = actions
        f = self.frame = ttk.Frame(parent, padding=(0, PAD))
        f.columnconfigure(0, weight=1)
        self.title = ttk.Label(f, style="Headline.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.phase = ttk.Label(f)
        self.phase.grid(row=0, column=1, columnspan=2, sticky="e")
        self.bar = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(PAD // 2, PAD // 2))
        self.counts = ttk.Label(f)
        self.counts.grid(row=2, column=0, columnspan=3, sticky="w")
        self.note = ttk.Label(f)
        self.note.grid(row=3, column=0, columnspan=3, sticky="w")
        self.flight = ttk.Label(f)
        self.flight.grid(row=4, column=0, sticky="w")
        self.pause = ttk.Button(f, text="Pause", command=self._pause_or_resume)
        self.pause.grid(row=4, column=1, sticky="e", padx=(PAD, 0))
        self.cancel = ttk.Button(f, text="Cancel...", command=self._cancel_or_stop)
        self.cancel.grid(row=4, column=2, sticky="e", padx=(PAD, 0))
        self.separator = ttk.Separator(parent, orient="horizontal")
        self.panel = panel
        self._indeterminate = False
        self.show(panel)

    def show(self, panel: RunPanel) -> None:
        self.panel = panel
        self.title.configure(text=panel.title)
        self.phase.configure(text=panel.phase)
        self.counts.configure(text=panel.counts)
        self.note.configure(text=panel.note)
        if panel.note:
            self.note.grid()
        else:
            self.note.grid_remove()
        self.flight.configure(text=panel.in_flight)
        quiet = panel.waiting or not panel.readable          # no progress to show
        for widget in (self.bar, self.counts):
            if quiet:
                widget.grid_remove()
            else:
                widget.grid()
        if panel.fraction is None and not quiet and not panel.paused:
            if not self._indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start(20)
                self._indeterminate = True
        else:
            if self._indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._indeterminate = False
            self.bar.configure(value=int(1000 * (panel.fraction or 0.0)))
        self.pause.configure(text=pause_words(panel))
        set_enabled(self.pause, can_ask_pause(panel))
        self.cancel.configure(text=cancel_words(panel))
        set_enabled(self.cancel, can_ask_cancel(panel))

    def _pause_or_resume(self):
        self.actions.requests.pause_or_resume(self.panel)

    def _cancel_or_stop(self):
        self.actions.requests.cancel_or_stop(self.panel)

    def destroy(self):
        self.bar.stop()
        self.frame.destroy()
        self.separator.destroy()


class RunsPanel:
    def __init__(self, parent, session: GuiSession, status_of: StatusOf):
        self.session = session
        self.status_of = status_of
        self.scroll = ScrollFrame(parent)
        self.requests = RunRequests(lambda: self.scroll, session.layout, status_of, timer=self.scroll,
                                    places=lambda: destination_places(session.contents.jobs))
        self.scroll.body.columnconfigure(0, weight=1)
        self.empty = ttk.Label(self.scroll.body, text=EMPTY_TEXT, wraplength=em(parent, 28), justify="left")
        self.scroll.canvas.bind("<Configure>", self._wrap, add="+")
        self.rows: Dict[str, _RunRow] = {}
        self.update([])

    def _wrap(self, event) -> None:
        """The empty text takes the tab's width as the window changes size."""
        self.empty.configure(wraplength=max(em(self.scroll, 8), event.width - PAD))

    def grid(self, **kwargs):
        self.scroll.grid(**kwargs)

    def update(self, panels: Sequence[RunPanel]) -> None:
        wanted = [p.run_id for p in panels]
        for run_id in [r for r in self.rows if r not in wanted]:
            self.rows.pop(run_id).destroy()
        for panel in panels:
            row = self.rows.get(panel.run_id)
            if row is None:
                self.rows[panel.run_id] = _RunRow(self.scroll.body, panel, self)
            else:
                row.show(panel)
        for position, run_id in enumerate(wanted):
            row = self.rows[run_id]
            row.frame.grid(row=2 * position, column=0, sticky="ew")
            row.separator.grid(row=2 * position + 1, column=0, sticky="ew")
        if panels:
            self.empty.grid_remove()
        else:
            self.empty.grid(row=0, column=0, sticky="w", pady=PAD)


class RunRequests:
    """What can be asked of a run, a verification or a repair in progress, and the questions
    before a cancel or a stop. ``parent`` gives the widget a question opens over, looked up only
    as one opens; ``timer`` is the widget whose clock waits for a request's answer; ``warn``
    says a request that did not reach its activity (by default a warning over ``parent``);
    ``status_of`` gives an activity's status as last read; ``places`` gives each job's
    destinations' numbers as the job stands now (``runs_model.destination_places``)."""

    def __init__(self, parent: Callable[[], tk.Misc], layout: RuntimeLayout, status_of: StatusOf, *,
                 timer: tk.Misc, warn: Optional[Callable[[str, str], None]] = None,
                 places: Callable[[], Places] = lambda: NO_PLACES):
        self.parent = parent
        self.layout = layout
        self.status_of = status_of
        self.places = places
        self.timer = timer
        self.warn = warn or (lambda title, text: messagebox.showwarning(title, text, parent=self.parent()))

    def pause_or_resume(self, panel: RunPanel) -> None:
        """A row's *Pause* or *Resume*."""
        self.ask(panel.run_id, "resume" if panel.paused else "pause", panel.verification, repair=panel.repair,
                 pause=not panel.paused)

    def cancel_or_stop(self, panel: RunPanel) -> None:
        """A row's *Cancel...*, *Stop now...* or *Stop...*, its question first."""
        if panel.verification:
            self.stop_verification(panel.run_id)
        elif panel.repair:
            self.stop_repair(panel.run_id)
        elif panel.can_stop_now:
            self.stop_now(panel.run_id)
        else:
            self.open_cancel(panel.run_id)

    def ask(self, run_id: str, what: str, verification: bool = False, *, repair: bool = False, **wanted) -> None:
        """Write the request off the window's thread; ``what`` ("pause", "resume", "cancel",
        "stop") names it if it does not reach the run (the verification, the repair)."""
        answer: "queue.Queue[str]" = queue.Queue()

        def send():
            try:
                request(self.layout, run_id, **wanted)
                answer.put("")
            except FileBusy:
                answer.put(not_reached_words(verification, repair=repair))
            except NoSuchRun:
                answer.put(not_reached_words(verification, "it had ended", repair=repair))
            except OSError as exc:
                answer.put(not_reached_words(verification, str(exc), repair=repair))

        threading.Thread(target=send, name="mirrorhold-request", daemon=True).start()
        self._answered(answer, what)

    def _answered(self, answer: "queue.Queue[str]", what: str) -> None:
        if not self.timer.winfo_exists():
            return
        try:
            problem = answer.get_nowait()
        except queue.Empty:
            self.timer.after(_ANSWER_MS, lambda: self._answered(answer, what))
            return
        if problem:
            self.warn(REQUEST_TITLES[what], problem)

    def stop_now(self, run_id: str) -> None:
        status = self.status_of(run_id) or {}
        if confirm_stop_now(self.parent(), status):
            self.ask(run_id, "stop", cancel=CancelMode.IMMEDIATE)

    def open_cancel(self, run_id: str) -> "CancelDialog":
        return CancelDialog(self.parent(), self, run_id)

    def stop_verification(self, run_id: str) -> None:
        if choose(self.parent(), "Stop verifying?", STOP_VERIFYING_TEXT,
                  [("stop", "Stop"), ("keep", "Keep verifying")], default="keep", cancel="keep") == "stop":
            self.ask(run_id, "stop", True, cancel=CancelMode.IMMEDIATE)

    def stop_repair(self, run_id: str) -> None:
        if choose(self.parent(), "Stop replacing copies?", STOP_REPAIRING_TEXT,
                  [("stop", "Stop"), ("keep", "Keep replacing")], default="keep", cancel="keep") == "stop":
            self.ask(run_id, "stop", repair=True, cancel=CancelMode.GRACEFUL)


def confirm_stop_now(parent, status: dict) -> bool:
    flying = len(status.get("in_flight") or [])
    if flying:
        text = (f"Stop now? {files(flying)} being copied {agreeing(flying, 'is', 'are')} discarded; the next run "
                f"copies {agreeing(flying, 'it', 'them')} again.")
    else:
        text = "Stop now? Nothing is being copied at the moment; the run stops at once."
    return choose(parent, "Stop now?", text, [("stop", "Stop now"), ("back", "Back")],
                  default="back", cancel="back") == "stop"


class CancelDialog:
    def __init__(self, parent, requests: RunRequests, run_id: str):
        self.requests = requests
        self.run_id = run_id
        status = requests.status_of(run_id) or {}
        title = status.get("job_name", "")
        number = destination_number(status, requests.places())
        if number:
            title += f" -> {number}"
        w = self.window = tk.Toplevel(parent)
        w.title(f"Cancel run: {title}")
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.8))
        frame.grid(sticky="nsew")
        self.mode = tk.StringVar(value=CancelMode.GRACEFUL.value)
        wrap = em(w, 22)
        ttk.Radiobutton(frame, text="Finish the files being copied, then stop        (recommended)",
                        variable=self.mode, value=CancelMode.GRACEFUL.value).grid(row=0, column=0, sticky="w")
        self.graceful_detail = ttk.Label(frame, text=f"{in_flight_left(status)}; no new file starts")
        self.graceful_detail.grid(row=1, column=0, sticky="w", padx=(em(w, 1.2), 0))
        ttk.Radiobutton(frame, text="Stop now", variable=self.mode,
                        value=CancelMode.IMMEDIATE.value).grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        ttk.Label(frame, wraplength=wrap, justify="left",
                  text="The files in flight are discarded and copied again by the next run; nothing incomplete "
                       "is left under its real name.").grid(row=3, column=0, sticky="w", padx=(em(w, 1.2), 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="e", pady=(em(w, 0.8), 0))
        go = ttk.Button(buttons, text="Cancel the run", default="active", command=self._confirm)
        go.grid(row=0, column=0)
        ttk.Button(buttons, text="Keep running", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: self._confirm())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        modal(w, parent)
        go.focus_set()

    def _confirm(self):
        status = self.requests.status_of(self.run_id) or {}
        if self.mode.get() == CancelMode.IMMEDIATE.value:
            if not confirm_stop_now(self.window, status):
                return
            self.requests.ask(self.run_id, "stop", cancel=CancelMode.IMMEDIATE)
        else:
            self.requests.ask(self.run_id, "cancel", cancel=CancelMode.GRACEFUL)
        self.window.destroy()
