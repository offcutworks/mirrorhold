"""The *About* window, reached from the Help menu, and the text windows it opens: the
licence and, in a release, the third-party notices. Tk.

The official address opens in the user's browser. The licence and the notices are shown
in a window of the program's own rather than handed to Windows: the licence file has no
extension, so Windows would ask which program should open it. A copy that ships no
notices shows no button for them.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk

from .about_model import (ENGINE, FREE, LICENCE_TITLE, NOTICES_TITLE, OFFICIAL, OFFICIAL_COPIES, TAGLINE,
                          WINDOW_TITLE, AboutContent)
from .common_view import LINK_STYLE, PAD, em, set_enabled, size_to_content


class AboutWindow:
    def __init__(self, parent: tk.Misc, content: AboutContent):
        self.content = content
        w = self.window = tk.Toplevel(parent)
        w.title(WINDOW_TITLE)
        w.resizable(False, False)
        frame = ttk.Frame(w, padding=em(w, 0.8))
        frame.grid(sticky="nsew")
        wrap = em(w, 26)
        gap = em(w, 0.8)
        ttk.Label(frame, text=content.title, style="Headline.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=TAGLINE, wraplength=wrap, justify="left").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text=content.copyright, wraplength=wrap, justify="left").grid(
            row=2, column=0, sticky="w", pady=(gap, 0))
        ttk.Label(frame, text=FREE, wraplength=wrap, justify="left").grid(row=3, column=0, sticky="w")
        ttk.Label(frame, text=OFFICIAL).grid(row=4, column=0, sticky="w")
        link = ttk.Label(frame, text=OFFICIAL_COPIES, style=LINK_STYLE, cursor="hand2")
        link.grid(row=5, column=0, sticky="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(OFFICIAL_COPIES))
        ttk.Label(frame, text=ENGINE).grid(row=6, column=0, sticky="w", pady=(gap, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, sticky="e", pady=(gap, 0))
        licence = ttk.Button(buttons, text="Licence", command=self.open_licence)
        licence.grid(row=0, column=0)
        set_enabled(licence, content.licence is not None)
        column = 1
        if content.notices is not None:
            notices = ttk.Button(buttons, text="Third-party notices", command=self.open_notices)
            notices.grid(row=0, column=column, padx=(PAD, 0))
            column += 1
        close = ttk.Button(buttons, text="Close", default="active", command=w.destroy)
        close.grid(row=0, column=column, padx=(PAD, 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()

    def open_licence(self):
        if self.content.licence is not None:
            TextWindow(self.window, LICENCE_TITLE, self.content.licence)

    def open_notices(self):
        if self.content.notices is not None:
            TextWindow(self.window, NOTICES_TITLE, self.content.notices)


class TextWindow:
    """A read-only text — the licence, the third-party notices — in a scrolling window."""

    def __init__(self, parent: tk.Misc, title: str, text: str):
        w = self.window = tk.Toplevel(parent)
        w.title(title)
        frame = ttk.Frame(w, padding=em(w, 0.6))
        frame.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        lines = text.splitlines()
        body = tk.Text(frame, wrap="word", font="TkDefaultFont", width=max(60, max(map(len, lines), default=0)),
                       height=min(24, max(8, len(lines))), borderwidth=0, padx=PAD, pady=PAD)
        body.insert("1.0", text)
        body.configure(state="disabled")
        bar = ttk.Scrollbar(frame, orient="vertical", command=body.yview)
        body.configure(yscrollcommand=bar.set)
        body.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        close = ttk.Button(frame, text="Close", default="active", command=w.destroy)
        close.grid(row=1, column=0, columnspan=2, sticky="e", pady=(PAD, 0))
        w.bind("<Return>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())
        size_to_content(w, parent=parent)
        w.transient(parent.winfo_toplevel())
        close.focus_set()
