"""The Import jobs and Export jobs windows. Tk."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Sequence

from mirrorhold.jobs.store import JobList, StoreError
from mirrorhold.endpoints.volumes import VolumeInfo
from mirrorhold.wording import counted

from .common_view import PAD, em, modal, size_to_content
from .session import GuiSession, StoreConflict
from .transfer_model import ImportState, apply_import, import_lines

_FILE_TYPES = [("MirrorHold jobs", "*.json"), ("All files", "*.*")]


def import_jobs(parent, session: GuiSession, volumes: Sequence[VolumeInfo], on_done: Callable[[], None]) -> None:
    chosen = filedialog.askopenfilename(parent=parent, title="Import jobs", filetypes=_FILE_TYPES)
    if not chosen:
        return
    path = os.path.normpath(chosen)
    try:
        listed = session.store.read_export(path)
    except StoreError as exc:
        messagebox.showerror("Import jobs", str(exc), parent=parent)
        return
    ImportWindow(parent, session, path, listed, volumes, on_done)


class ImportWindow:
    def __init__(self, parent, session: GuiSession, path: str, listed: JobList,
                 volumes: Sequence[VolumeInfo], on_done: Callable[[], None]):
        self.session, self.volumes, self.on_done = session, tuple(volumes), on_done
        self.imported, self.groups = listed.jobs, listed.groups
        w = self.window = tk.Toplevel(parent)
        w.title("Import jobs")
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        ttk.Label(frame, text=f"From: {path}").grid(row=0, column=0, sticky="w")
        lines = import_lines(session.contents, self.imported)
        listed = ", ".join(f"{line.job.name} ({line.state.value})" for line in lines) or "no jobs"
        ttk.Label(frame, text=f"{counted(len(lines), 'job')}: {listed}",
                  wraplength=em(w, 24), justify="left").grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.replace = tk.BooleanVar(value=False)
        different = any(line.state is ImportState.DIFFERENT for line in lines)
        check = ttk.Checkbutton(frame, variable=self.replace,
                                text="Replace jobs that are already here with the imported ones")
        check.grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        check.state(["!disabled"] if different else ["disabled"])
        ttk.Label(frame, text="(otherwise they are left as they are)").grid(row=3, column=0, sticky="w",
                                                                            padx=(em(w, 1.2), 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="e", pady=(em(w, 0.6), 0))
        go = ttk.Button(buttons, text="Import", default="active", command=self._import)
        go.grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Return>", lambda e: self._import())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        modal(w, parent)
        go.focus_set()

    def _import(self):
        result = apply_import(self.session.contents, self.imported, replace_existing=self.replace.get(),
                              volumes=self.volumes, sync_roots=self.session.sync_roots(), groups=self.groups,
                              store_path=self.session.store.path)
        if not result.ok:
            reasons = "\n".join(f"{name}: {why}" for name, why in result.refused[:8])
            messagebox.showwarning("Nothing imported", f"These jobs are refused, so nothing was imported:\n\n{reasons}",
                                   parent=self.window)
            return
        outcome = result.outcome
        if outcome.added or outcome.replaced:
            try:
                self.session.save(result.contents)
            except StoreConflict as exc:
                messagebox.showwarning("Not imported", f"{exc}. Nothing was imported; the list is reloaded.",
                                       parent=self.window)
                self.window.destroy()
                self.on_done()
                return
        messagebox.showinfo("Import jobs", f"{len(outcome.added)} added, {len(outcome.replaced)} replaced, "
                                           f"{len(outcome.unchanged)} already here, {len(outcome.conflicts)} left "
                                           "as they are.", parent=self.window)
        self.window.destroy()
        self.on_done()


class ExportWindow:
    def __init__(self, parent, session: GuiSession):
        self.session = session
        w = self.window = tk.Toplevel(parent)
        w.title("Export jobs")
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        self.chosen = []
        jobs = session.contents.jobs
        for i, job in enumerate(jobs):
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(frame, text=job.name, variable=var).grid(row=i // 3, column=i % 3, sticky="w",
                                                                    padx=(0, PAD))
            self.chosen.append((job, var))
        if not jobs:
            ttk.Label(frame, text="There is no job to export.").grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(jobs) // 3 + 1, column=0, columnspan=3, sticky="e", pady=(em(w, 0.6), 0))
        go = ttk.Button(buttons, text="Save as...", default="active", command=self._export)
        go.grid(row=0, column=0)
        go.state(["!disabled"] if jobs else ["disabled"])
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        modal(w, parent)

    def _export(self):
        jobs = [job for job, var in self.chosen if var.get()]
        if not jobs:
            return
        chosen = filedialog.asksaveasfilename(parent=self.window, title="Export jobs", defaultextension=".json",
                                              initialfile="mirrorhold-jobs.json", filetypes=_FILE_TYPES)
        if not chosen:
            return
        try:
            self.session.store.export_jobs(jobs, os.path.normpath(chosen), groups=self.session.contents.groups)
        except OSError as exc:
            messagebox.showerror("Export jobs", str(exc), parent=self.window)
            return
        self.window.destroy()
