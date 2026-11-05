"""The Preferences window: the sync engine, remote destinations, the notifications, runs on one
drive, the computer while work goes on, the installation. Tk.

*Save* keeps the engine, the rclone configuration with its unlock, the notification choices,
runs on one drive — with what each drive is, once the drives have been read on a thread of
the window's own — and the computer's two choices: kept awake, and the work named on
Windows' shutdown screen. A password typed for remembering is handed over as *Save* is
pressed, and the entry emptied at once; the engine opens the configuration with it on a
thread, and one that does not open it keeps the window open with why, saving nothing. A
password that was remembered then says what remembering means, once. The sections scroll
inside the window where the screen is too short for them, *Save* and *Cancel* staying
below them. The installation's
buttons act at once — installing writes no preference — and uninstalling asks first. Their
section, labels and words come from ``prefs_model.installation_view``: a portable release
offers to set itself up rather than to install, and what removing keeps is the hint of its
button. ``offer_set_up_here`` is the question a portable release asks as it opens while
another copy is set up.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from mirrorhold.installation.installer import InstallError
from mirrorhold.jobs.model import DriveTurns, RemoteSettings, Unlock
from mirrorhold.remotes import seal

from .common_view import (PAD, Hint, ScrollFrame, choose, em, grow_to_content, modal, set_enabled, size_to_content,
                          tone_style)
from .jobs_model import Tone
from .prefs_model import (COMMAND_NOTE, COMMAND_PASSWORD, CONFIGURATION_LABEL, DRIVE_CHOICES, DRIVE_NOTE,
                          DRIVE_QUESTION, DRIVE_TITLE, KEEP_AWAKE, KEEP_AWAKE_NOTE, KINDS_TITLE, LOOKING,
                          NAME_AT_SHUTDOWN, NAME_AT_SHUTDOWN_NOTE, NO_LASTING_NAME, NO_PASSWORD, PASSWORD_KEPT,
                          PINNED_DESCRIPTION, REMEMBER_NOTE, REMEMBER_PASSWORD, REMEMBERED_TITLE, REMOTES_TITLE,
                          SAVE_CHECKS, WORK_TITLE, EngineCheck, RemotesDraft, check_configuration, check_engine,
                          drive_rows, drives_unreadable, install_done, installation_view, kind_box_width,
                          read_remotes, remember, remotes_draft, set_up_here_question, with_drive_kinds,
                          with_drive_turns, with_engine, with_keep_awake, with_name_at_shutdown, with_notifications,
                          with_remotes)
from .remote_model import Background
from .session import GuiSession, StoreConflict

_POLL_MS = 150
_SCREEN_SHARE = 0.85       # the most of the screen's height the window opens at


class PreferencesWindow:
    def __init__(self, parent, session: GuiSession, *, on_saved: Callable[[], None] = lambda: None):
        self.session = session
        self.on_saved = on_saved
        preferences = session.contents.preferences
        w = self.window = tk.Toplevel(parent)
        w.title("Preferences")
        w.resizable(False, False)
        outer = ttk.Frame(w, padding=em(w, 0.7))
        outer.grid(sticky="nsew")
        self.scroll = ScrollFrame(outer)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.buttons = buttons = ttk.Frame(outer)     # first: the drives may be read before the sections are built
        frame = self.scroll.body
        frame.columnconfigure(0, weight=1)
        box = ttk.LabelFrame(frame, text="Sync engine", padding=PAD)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self.choice = tk.StringVar(value="pinned" if preferences.engine_path is None else "other")
        self.path = tk.StringVar(value=preferences.engine_path or "")
        ttk.Radiobutton(box, text=PINNED_DESCRIPTION, variable=self.choice, value="pinned",
                        command=self._recheck).grid(row=0, column=0, columnspan=3, sticky="w")
        other = ttk.Radiobutton(box, text="Another copy of the same version:", variable=self.choice, value="other",
                                command=self._recheck)
        other.grid(row=1, column=0, sticky="w")
        Hint(other, "Other engine versions are refused: MirrorHold is tested on this one.")
        self.entry = ttk.Entry(box, textvariable=self.path, width=36)
        self.entry.grid(row=1, column=1, sticky="ew", padx=(PAD, 0))
        self.browse = ttk.Button(box, text="Browse...", command=self._browse)
        self.browse.grid(row=1, column=2, padx=(PAD, 0))
        self.result = ttk.Label(box, text="", wraplength=em(w, 24), justify="left")
        self.result.grid(row=2, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        self._build_remotes(frame, preferences, row=1)
        self._build_notifications(frame, preferences, row=2)
        self._build_drives(frame, preferences, row=3)
        self._build_work(frame, preferences, row=4)
        self._build_installation(frame, row=5)
        buttons.grid(row=1, column=0, sticky="e", pady=(em(w, 0.6), 0))
        self.save = ttk.Button(buttons, text="Save", default="active", command=self._save)
        self.save.grid(row=0, column=0)
        self.cancel = ttk.Button(buttons, text="Cancel", command=self._close)
        self.cancel.grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Escape>", lambda e: self._close())
        w.protocol("WM_DELETE_WINDOW", self._close)
        self._sealing = False
        self.path.trace_add("write", lambda *a: self._recheck())
        self._check = None
        self._generation = 0
        self._recheck()
        self._fit_sections()
        size_to_content(w, parent=parent)
        modal(w, parent)

    def _close(self):
        """Close the window, unless a password is being sealed: the window must see how that
        ended, or a password could stay sealed that the job file does not name."""
        if not self._sealing:
            self.window.destroy()

    def _fit_sections(self):
        """Show the sections whole where the screen has room for them and the buttons, and
        scroll them otherwise, the scroll bar shown only then."""
        w = self.window
        sections = self.scroll.requested_height()
        room = int(w.winfo_screenheight() * _SCREEN_SHARE) - self.buttons.winfo_reqheight() - em(w, 2)
        self.scroll.canvas.configure(height=min(sections, max(room, em(w, 10))),
                                     width=self.scroll.body.winfo_reqwidth())
        if sections > room:
            self.scroll.bar.grid()
        else:
            self.scroll.bar.grid_remove()

    def _build_remotes(self, frame, preferences, row):
        draft = remotes_draft(preferences.remotes)
        box = ttk.LabelFrame(frame, text=REMOTES_TITLE, padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=CONFIGURATION_LABEL).grid(row=0, column=0, sticky="w", padx=(0, PAD))
        self.configuration = tk.StringVar(value=draft.configuration)
        ttk.Entry(box, textvariable=self.configuration, width=36).grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="Browse...", command=self._browse_configuration).grid(row=0, column=2, padx=(PAD, 0))
        self.unlock = tk.StringVar(value=draft.unlock.value)
        ttk.Radiobutton(box, text=NO_PASSWORD, variable=self.unlock, value=Unlock.NONE.value,
                        command=self._unlock_states).grid(row=1, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        remembered = ttk.Radiobutton(box, text=REMEMBER_PASSWORD, variable=self.unlock, value=Unlock.REMEMBERED.value,
                                     command=self._unlock_states)
        remembered.grid(row=2, column=0, sticky="w", padx=(0, PAD))
        Hint(remembered, REMEMBER_NOTE)
        self.password = tk.StringVar()
        self.password_entry = ttk.Entry(box, textvariable=self.password, show="*")
        self.password_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        commanded = ttk.Radiobutton(box, text=COMMAND_PASSWORD, variable=self.unlock, value=Unlock.COMMAND.value,
                                    command=self._unlock_states)
        commanded.grid(row=3, column=0, sticky="w", padx=(0, PAD))
        Hint(commanded, COMMAND_NOTE)
        self.command = tk.StringVar(value=draft.command)
        self.command_entry = ttk.Entry(box, textvariable=self.command)
        self.command_entry.grid(row=3, column=1, columnspan=2, sticky="ew")
        self.check_button = ttk.Button(box, text="Check", command=self._check_remotes)
        self.check_button.grid(row=4, column=0, sticky="w", pady=(PAD, 0))
        self.remotes_line = ttk.Label(box, text="", wraplength=em(frame, 20), justify="left")
        self.remotes_line.grid(row=4, column=1, columnspan=2, sticky="w", pady=(PAD, 0))
        self._checking = None
        if self.session.password_kept() and draft.unlock is Unlock.REMEMBERED:
            self.remotes_line.configure(text=PASSWORD_KEPT)
        self.configuration.trace_add("write", lambda *a: self._configuration_changed())
        self._unlock_states()
        self._configuration_changed()

    def _configuration_changed(self):
        set_enabled(self.check_button, bool(self.configuration.get().strip()))

    def _unlock_states(self):
        set_enabled(self.password_entry, self.unlock.get() == Unlock.REMEMBERED.value)
        set_enabled(self.command_entry, self.unlock.get() == Unlock.COMMAND.value)

    def _browse_configuration(self):
        chosen = filedialog.askopenfilename(parent=self.window, title="Choose the rclone configuration",
                                            filetypes=[("rclone configuration", "*.conf"), ("All files", "*.*")])
        if chosen:
            self.configuration.set(os.path.normpath(chosen))

    def _remotes_plan(self):
        draft = RemotesDraft(self.configuration.get(), Unlock(self.unlock.get()), self.command.get(),
                             self.password.get())
        return read_remotes(draft, self.session.contents.preferences.remotes)

    def _say_remotes(self, text: str, tone: Tone = Tone.NORMAL):
        self.remotes_line.configure(text=text, style=tone_style(tone))
        if self.window.winfo_ismapped():
            self._fit_sections()
            grow_to_content(self.window)

    def _preferences(self):
        """The preferences as the engine's choice in the window starts one."""
        return with_engine(self.session.contents.preferences, self._chosen())

    def _check_remotes(self):
        plan = self._remotes_plan()
        if plan.problem or plan.remember:
            self._say_remotes(plan.problem or SAVE_CHECKS, Tone.PROBLEM if plan.problem else Tone.NORMAL)
            return
        set_enabled(self.check_button, False)
        self._say_remotes("Asking the engine...")
        preferences, path = self._preferences(), self.session.store.path
        self._checking = Background(lambda: check_configuration(preferences, path, plan.settings))
        self._wait_check()

    def _wait_check(self):
        if not self.window.winfo_exists():
            return
        if not self._checking.done:
            self.window.after(_POLL_MS, self._wait_check)
            return
        usable, text = self._checking.value or (False, self._checking.failure)
        self._say_remotes(text, Tone.NORMAL if usable else Tone.PROBLEM)
        self._configuration_changed()

    def _build_notifications(self, frame, preferences, row):
        box = ttk.LabelFrame(frame, text="Notifications", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        ttk.Label(box, text="Show a Windows notification:").grid(row=0, column=0, columnspan=3, sticky="w")
        chosen = preferences.notifications
        self.notify = {}
        for i, (key, label, value) in enumerate((("on_completion", "When a run completes", chosen.on_completion),
                                                 ("on_hold", "When removals are held", chosen.on_hold),
                                                 ("on_failure", "When a run fails", chosen.on_failure))):
            self.notify[key] = tk.BooleanVar(value=value)
            ttk.Checkbutton(box, text=label, variable=self.notify[key]).grid(row=1, column=i, sticky="w",
                                                                             padx=(0, em(frame, 0.8)))

    def _build_drives(self, frame, preferences, row):
        box = ttk.LabelFrame(frame, text=DRIVE_TITLE, padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        question = ttk.Label(box, text=DRIVE_QUESTION)
        question.grid(row=0, column=0, sticky="w")
        Hint(question, DRIVE_NOTE)
        self.drive_turns = tk.StringVar(value=preferences.activities.drive_turns.value)
        for i, (turns, text) in enumerate(DRIVE_CHOICES):
            ttk.Radiobutton(box, text=text, variable=self.drive_turns, value=turns.value).grid(
                row=i + 1, column=0, sticky="w")
        ttk.Label(box, text=KINDS_TITLE).grid(row=len(DRIVE_CHOICES) + 1, column=0, sticky="w", pady=(PAD, 0))
        self.kinds = ttk.Frame(box)
        self.kinds.grid(row=len(DRIVE_CHOICES) + 2, column=0, sticky="w", padx=(em(frame, 1), 0))
        self.kinds_text = ttk.Label(self.kinds, text=LOOKING, wraplength=em(frame, 24), justify="left")
        self.kinds_text.grid(row=0, column=0, columnspan=2, sticky="w")
        self.drive_rows = None              # set once the drives are read; until then no mark changes
        self.kind_boxes = {}
        self.drive_turns.trace_add("write", lambda *a: self._kinds_enabled())
        holder = {}

        def read():
            try:
                holder["rows"] = drive_rows(self.session.scan().volumes, preferences.activities.drive_kinds)
            except Exception as exc:  # shown in the window: a scan that fails must not leave it looking
                holder["error"] = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        threading.Thread(target=read, daemon=True).start()
        self._wait_drives(holder)

    def _wait_drives(self, holder):
        if not self.window.winfo_exists():
            return
        if not holder:
            self.window.after(_POLL_MS, lambda: self._wait_drives(holder))
            return
        if "error" in holder:
            self.kinds_text.configure(text=drives_unreadable(holder["error"]), style=tone_style(Tone.PROBLEM))
            return
        self.kinds_text.grid_remove()
        self.drive_rows = holder["rows"]
        width = kind_box_width(self.drive_rows)
        for i, row in enumerate(self.drive_rows, start=1):
            label = ttk.Label(self.kinds, text=row.label)
            label.grid(row=i, column=0, sticky="w", padx=(0, PAD))
            chooser = ttk.Combobox(self.kinds, values=[text for _kind, text in row.choices], state="readonly",
                                   width=width)
            chooser.set(row.chosen)
            chooser.grid(row=i, column=1, sticky="w", pady=(0, 2))
            if not row.markable:
                Hint(label, NO_LASTING_NAME)
                Hint(chooser, NO_LASTING_NAME)
            self.kind_boxes[row.drive] = chooser
        self._kinds_enabled()
        self._fit_sections()
        grow_to_content(self.window)

    def _kinds_enabled(self):
        automatic = self.drive_turns.get() == DriveTurns.AUTOMATIC.value
        for row in self.drive_rows or ():
            self.kind_boxes[row.drive].state(["!disabled", "readonly"] if automatic and row.markable
                                             else ["disabled"])

    def _chosen_kinds(self):
        """Each drive's choice in the window: its kind, or None for as its disk reports."""
        return {row.drive: next(kind for kind, text in row.choices if text == self.kind_boxes[row.drive].get())
                for row in self.drive_rows}

    def _build_work(self, frame, preferences, row):
        box = ttk.LabelFrame(frame, text=WORK_TITLE, padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        self.keep_awake = tk.BooleanVar(value=preferences.activities.keep_awake)
        awake = ttk.Checkbutton(box, text=KEEP_AWAKE, variable=self.keep_awake)
        awake.grid(row=0, column=0, sticky="w")
        Hint(awake, KEEP_AWAKE_NOTE)
        self.name_at_shutdown = tk.BooleanVar(value=preferences.activities.name_at_shutdown)
        named = ttk.Checkbutton(box, text=NAME_AT_SHUTDOWN, variable=self.name_at_shutdown)
        named.grid(row=1, column=0, sticky="w")
        Hint(named, NAME_AT_SHUTDOWN_NOTE)

    def _build_installation(self, frame, row):
        self.install_box = box = ttk.LabelFrame(frame, text="Installation", padding=PAD)
        box.grid(row=row, column=0, sticky="ew", pady=(PAD, 0))
        self.install_text = ttk.Label(box, text="", wraplength=em(frame, 24), justify="left")
        self.install_text.grid(row=0, column=0, sticky="w")
        actions = ttk.Frame(box)
        actions.grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.install_button = ttk.Button(actions, text="Install", command=self._install)
        self.install_button.grid(row=0, column=0)
        self.uninstall_button = ttk.Button(actions, text="Uninstall", command=self._uninstall)
        self.uninstall_button.grid(row=0, column=1, padx=(PAD, 0))
        self.uninstall_hint = Hint(self.uninstall_button, "")
        self._show_installation()

    def _show_installation(self):
        view = self.view = installation_view(self.session.installation())
        self.install_box.configure(text=view.title)
        self.install_text.configure(text=view.text)
        self.uninstall_hint.text = view.note
        self.install_button.configure(text=view.install_label)
        self.uninstall_button.configure(text=view.uninstall_label)
        set_enabled(self.install_button, view.can_install)
        set_enabled(self.uninstall_button, view.can_uninstall)

    def _install(self):
        self.install_text.configure(text=self.view.busy)
        self.window.update_idletasks()
        set_up(self.window, self.session, self.view.done_title)
        self._show_installation()

    def _uninstall(self):
        label = self.view.uninstall_label
        answer = choose(self.window, label, self.view.remove_question, [("remove", label), ("cancel", "Cancel")],
                        default="cancel", cancel="cancel")
        if answer != "remove":
            return
        try:
            result = self.session.uninstall()
        except InstallError as exc:
            messagebox.showwarning(label, f"Nothing was changed: {exc}.", parent=self.window)
        else:
            if result.problems:
                messagebox.showwarning(label, "\n".join(result.problems), parent=self.window)
        self._show_installation()

    def _chosen(self):
        return None if self.choice.get() == "pinned" else (self.path.get().strip() or None)

    def _recheck(self):
        other = self.choice.get() == "other"
        self.entry.state(["!disabled"] if other else ["disabled"])
        set_enabled(self.browse, other)
        set_enabled(self.save, False)
        if other and not self.path.get().strip():
            self.result.configure(text="Choose the engine's rclone.exe.", style=tone_style(Tone.ATTENTION))
            return
        self._generation += 1
        generation, path = self._generation, self._chosen()
        self.result.configure(text="Checking the engine...", style=tone_style(Tone.NORMAL))
        holder = {}
        threading.Thread(target=lambda: holder.setdefault("check", check_engine(path)), daemon=True).start()
        self._wait(generation, holder)

    def _wait(self, generation, holder):
        if not self.window.winfo_exists() or generation != self._generation:
            return
        check: EngineCheck = holder.get("check")
        if check is None:
            self.window.after(_POLL_MS, lambda: self._wait(generation, holder))
            return
        self._check = check
        self.result.configure(text=f"In use: {check.text}" if check.usable else check.text,
                              style=tone_style(Tone.NORMAL if check.usable else Tone.PROBLEM))
        set_enabled(self.save, check.usable and not self._sealing)

    def _browse(self):
        chosen = filedialog.askopenfilename(parent=self.window, title="Choose rclone.exe",
                                            filetypes=[("rclone", "rclone.exe"), ("Programs", "*.exe")])
        if chosen:
            self.path.set(os.path.normpath(chosen))

    def _save(self):
        if self._check is None or not self._check.usable or self._sealing:
            return
        plan = self._remotes_plan()
        if plan.problem:
            self._say_remotes(plan.problem, Tone.PROBLEM)
            return
        if not plan.remember:
            self._keep(plan.settings)
            return
        preferences, path, password = self._preferences(), self.session.store.path, self.password.get()
        previous = seal.kept(self.session.layout.remote_password)
        self.password.set("")
        self._sealing = True
        set_enabled(self.save, False)
        set_enabled(self.cancel, False)
        self._say_remotes("Checking the password...")
        sealing = Background(lambda: remember(preferences, path, plan.settings, password))
        self._wait_password(plan.settings, sealing, previous)

    def _wait_password(self, settings: RemoteSettings, sealing: Background, previous):
        if not sealing.done:
            self.window.after(_POLL_MS, lambda: self._wait_password(settings, sealing, previous))
            return
        problem = sealing.failure or sealing.value
        self._sealing = False
        if problem:
            self._say_remotes(problem, Tone.PROBLEM)
            set_enabled(self.save, self._check is not None and self._check.usable)
            set_enabled(self.cancel, True)
            return
        self._keep(settings, sealed=True, previous=previous)

    def _keep(self, settings: RemoteSettings, *, sealed: bool = False, previous=None):
        """Save the window's choices; a password no longer used goes once the job file no
        longer names it, and with a save that was refused, one just sealed gives way to what
        was remembered before (``previous``), which the job file still names."""
        contents = self.session.contents
        before = contents.preferences.remotes
        preferences = with_notifications(with_remotes(with_engine(contents.preferences, self._chosen()), settings),
                                         **{key: var.get() for key, var in self.notify.items()})
        preferences = with_keep_awake(with_drive_turns(preferences, DriveTurns(self.drive_turns.get())),
                                      self.keep_awake.get())
        preferences = with_name_at_shutdown(preferences, self.name_at_shutdown.get())
        if self.drive_rows is not None:
            preferences = with_drive_kinds(preferences, self.drive_rows, self._chosen_kinds())
        updated = replace(contents, preferences=preferences)
        remembered_file = self.session.layout.remote_password
        try:
            self.session.save(updated)
        except StoreConflict as exc:
            if sealed:
                seal.put_back(remembered_file, previous)
            messagebox.showwarning("Not saved", f"{exc}. The preference was not saved; the list is reloaded.",
                                   parent=self.window)
        else:
            if before.unlock is Unlock.REMEMBERED and settings.unlock is not Unlock.REMEMBERED \
                    and not seal.forget(remembered_file):
                messagebox.showwarning("Password not removed", f"{remembered_file} could not be removed: another "
                                       "program kept it open.", parent=self.window)
            if sealed:
                messagebox.showinfo(REMEMBERED_TITLE, REMEMBER_NOTE, parent=self.window)
        self.window.destroy()
        self.on_saved()


def set_up(parent, session: GuiSession, title: str) -> bool:
    """Install, or set this portable release up, and say how it went; whether it was done."""
    try:
        result = session.install()
    except InstallError as exc:
        messagebox.showwarning(title, f"Nothing was changed: {exc}.", parent=parent)
        return False
    (messagebox.showwarning if result.problems else messagebox.showinfo)(title, install_done(result), parent=parent)
    return True


def offer_set_up_here(parent, session: GuiSession) -> None:
    """Ask, as a portable release opens while another copy is set up, whether to use this
    copy instead, and set it up when the user says so."""
    question = set_up_here_question(session.installation())
    if question is None:
        return
    if choose(parent, "MirrorHold", question, [("use", "Use this copy"), ("later", "Not now")], default="use",
              cancel="later") == "use":
        set_up(parent, session, "MirrorHold")
