"""The windows of a remote's folders: choosing one through the engine, and where one is. Tk.

``RemoteFolderWindow`` browses a remote's folders through a session the window that opened
it keeps (``remote_model.RemoteAccess``), each look on a thread of its own, the folder
being chosen whether or not it exists yet. A remote's folder has no place on this computer
to open: ``open_place`` shows a folder of this computer in Explorer and a remote's address
in ``LocationWindow``, to copy to rclone or to the service's own site.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.endpoint import RemoteEndpoint

from .common_view import PAD, em, modal, set_enabled, size_to_content, tone_style
from .formatting import sentence
from .jobs_model import Place, RemotePlace, Tone
from .remote_model import (LOOKING_FOLDER, NO_FOLDERS, NOT_THERE_YET, Background, RemoteAccess, at_top, child_folder,
                           parent_folder)
from .session import Opener

ON_THE_SERVICE = "This folder is on the service, not on this computer."
_POLL_MS = 100


def open_place(parent: tk.Misc, opener: Opener, place: Place, title: str) -> None:
    """Open a folder of a destination, or show where it is when it is a remote's."""
    if isinstance(place, RemotePlace):
        LocationWindow(parent, title, place)
        return
    try:
        opener(place)
    except OSError as exc:
        messagebox.showwarning(title, str(exc), parent=parent)


def can_open(place: Optional[Place]) -> bool:
    """Whether ``place`` can be opened now: a remote's always shows its address."""
    return isinstance(place, RemotePlace) or (place is not None and place.is_dir())


class LocationWindow:
    """Where a folder of a remote is, to copy."""

    def __init__(self, parent: tk.Misc, title: str, place: RemotePlace):
        self.place = place
        w = self.window = tk.Toplevel(parent)
        w.title(title)
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=ON_THE_SERVICE).grid(row=0, column=0, columnspan=2, sticky="w")
        self.address = tk.StringVar(value=place.address)       # kept: Tk empties a variable Python let go of
        ttk.Entry(frame, textvariable=self.address, state="readonly",
                  width=min(70, max(36, len(place.address)))).grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        ttk.Button(frame, text="Copy", command=self._copy).grid(row=1, column=1, padx=(PAD, 0), pady=(PAD, 0))
        close = ttk.Button(frame, text="Close", default="active", command=w.destroy)
        close.grid(row=2, column=0, columnspan=2, sticky="e", pady=(em(w, 0.6), 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def _copy(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.place.address)


class RemoteFolderWindow:
    """*Browse...* of a remote: the folders inside one, entered by a double click or Enter,
    and *Up*; ``chosen`` is the folder shown when *Choose this folder* was pressed, else None."""

    def __init__(self, parent: tk.Misc, access: RemoteAccess, remote: str, folder: str):
        self.access = access
        self.remote = remote
        self.folder = folder
        self.chosen: Optional[str] = None
        self._look: Optional[Background] = None
        w = self.window = tk.Toplevel(parent)
        w.title(f"Choose a folder of {remote}:")
        frame = ttk.Frame(w, padding=em(w, 0.7))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.where = ttk.Label(frame, text="")
        self.where.grid(row=0, column=0, sticky="w")
        self.up = ttk.Button(frame, text="Up", command=self._up)
        self.up.grid(row=0, column=1, columnspan=2, sticky="e")
        self.tree = ttk.Treeview(frame, show="tree", selectmode="browse", height=12)
        self.tree.column("#0", width=em(w, 18), stretch=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(PAD, 0))
        bar.grid(row=1, column=2, sticky="ns", pady=(PAD, 0))
        self.tree.bind("<Double-1>", lambda e: self._enter())
        self.tree.bind("<Return>", lambda e: self._enter())
        self.line = ttk.Label(frame, text="", wraplength=em(w, 20), justify="left")
        self.line.grid(row=2, column=0, columnspan=3, sticky="w", pady=(PAD // 2, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e", pady=(em(w, 0.6), 0))
        ttk.Button(buttons, text="Choose this folder", default="active", command=self._choose).grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=w.destroy).grid(row=0, column=1, padx=(PAD, 0))
        w.bind("<Escape>", lambda e: w.destroy())
        self._show(folder)
        size_to_content(w, parent=parent)
        modal(w, parent)
        self.tree.focus_set()
        parent.wait_window(w)

    def _show(self, folder: str) -> None:
        """Look at ``folder``; a look still going is left to end by itself."""
        self.folder = folder
        self.where.configure(text=f"{self.remote}:{folder}")
        set_enabled(self.up, not at_top(folder))
        self.tree.delete(*self.tree.get_children())
        try:
            endpoint = RemoteEndpoint(self.remote, folder)
        except ContractError as exc:         # typed by hand, or a name no folder of a remote can have
            self._look = None
            self.line.configure(text=sentence(f"{exc}."), style=tone_style(Tone.PROBLEM))
            return
        self.line.configure(text=LOOKING_FOLDER, style=tone_style(Tone.NORMAL))
        look = self._look = Background(lambda: self.access.folders(endpoint))
        self._wait(look)

    def _wait(self, look: Background) -> None:
        if not self.window.winfo_exists() or look is not self._look:
            return
        if not look.done:
            self.window.after(_POLL_MS, lambda: self._wait(look))
            return
        if look.failure:
            self.line.configure(text=look.failure, style=tone_style(Tone.PROBLEM))
            return
        names = look.value
        for name in names or ():
            self.tree.insert("", "end", text=name)
        self.line.configure(text=NOT_THERE_YET if names is None else "" if names else NO_FOLDERS,
                            style=tone_style(Tone.NORMAL))

    def _up(self) -> None:
        self._show(parent_folder(self.folder))

    def _enter(self) -> None:
        selection = self.tree.selection()
        if selection:
            self._show(child_folder(self.folder, self.tree.item(selection[0], "text")))

    def _choose(self) -> None:
        self.chosen = self.folder
        self.window.destroy()
