"""Widgets and window idioms every MirrorHold window shares. Tk.

- ``prepare_display`` declares the process DPI-aware before Tk starts, so Windows does
  not stretch a low-resolution bitmap of the window; Tk then scales fonts and sizes to
  the display itself. ``claim_identity`` gives the process the application identity the
  Start menu entry carries, so the taskbar groups its windows with the entry.
  ``apply_style`` sizes list rows from the font, which a fixed row height would clip at
  a high scale. ``apply_icon`` gives every window the program's icon: the file a
  release or the source installation's copy ships, or, in a working copy, which ships
  none, the icon drawn at start by ``mirrorhold.icon`` — the drawing the file holds.
- ``size_to_content`` opens a window at what its content requests, clamped to 90 % of
  the screen, with a floor; nothing opens with its buttons off-screen.
  ``grow_to_content`` enlarges an open window whose content has grown since.
  ``work_areas`` and ``frame_size`` give what fitting a remembered place needs: each
  monitor's working area, and how much the window's frame adds to its inside.
- ``ScrollFrame`` owns its own wheel scrolling, wherever the pointer is over it. A
  dropdown takes no wheel: ``apply_style`` clears Tk's bindings for it, so the wheel over
  one scrolls the window as over any other control, and a choice changes only by click
  or keyboard.
- ``ToneLabel`` styles: state is written in words, and colour only reinforces it.
- ``LINK_STYLE``: a web address the user can click, underlined in the active tone.
- ``DIVIDER_STYLE``: a paned window whose divider shows a grip of dots. The system
  theme draws a divider as a few pixels of nothing, which the user cannot find; the
  style is a named one, so no other paned window changes.
- ``choose``: a small modal question with named buttons; ``ListWindow``: the
  "Show all" / "Details..." list of files with their reasons.
- ``Hint``: the explanation of a control, shown while the pointer rests on it. A window
  keeps on its face only the words a decision needs at that moment (the maintainer's
  decision, 2026-10-14); what explains a choice rather than states one is a hint on the
  control it explains. A hint is not reached from the keyboard, so nothing a window asks
  may depend on one.
"""
from __future__ import annotations

import base64
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Optional, Sequence, Tuple

from mirrorhold.icon import WINDOW_SIZES, png, render
from mirrorhold.installation.shortcut import APP_ID
from mirrorhold.locations import PROJECT_ROOT

from .formatting import size as format_size
from .jobs_model import Tone
from .shipped_model import icon_path
from .window_model import Area

PAD = 6
TONE_COLOURS = {Tone.ATTENTION: "#8A5300", Tone.PROBLEM: "#B3261E", Tone.ACTIVE: "#1F4E99",
                Tone.ABSENT: "#6D6D6D"}
"""Text colours reinforcing a state's words; each passes 4.5:1 on the system's white. The
absent tone's grey is the one Windows gives unavailable text (5.2:1)."""

DIVIDER_STYLE = "Divider.TPanedwindow"
LINK_STYLE = "Link.TLabel"
_GRIP_COLOUR = "#7A7A7A"   # 3.8:1 on the window's face, above the 3:1 a control needs


def prepare_display() -> None:
    """Declare system DPI awareness; must run before the first Tk window exists."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def claim_identity(app_id: str = APP_ID) -> None:
    """Give this process the application identity the Start menu entry carries; must run
    before the first window exists. The taskbar then groups the windows with the entry
    and shows its icon, and pinning a window pins the entry rather than the bare
    interpreter, however the window was started. Where Windows refuses, the taskbar
    groups the windows by the interpreter, as it would anyway: the identity decides
    nothing."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def apply_icon(root: tk.Tk, folder: Path = PROJECT_ROOT) -> bool:
    """Make the program's icon the default of every window ``root`` opens, itself
    included: the icon file the copy in ``folder`` ships, or, where it ships none or one
    Tk cannot read, the icon drawn here at the sizes a window uses — the same drawing the
    file holds. Whether it did; where neither works the windows keep Tk's own icon, since
    the icon names the program and decides nothing."""
    icon = icon_path(folder)
    if icon.is_file():
        try:
            root.iconbitmap(default=str(icon))
            return True
        except tk.TclError:
            pass
    try:
        images = tuple(tk.PhotoImage(master=root, data=base64.b64encode(png(size, render(size))).decode("ascii"))
                       for size in WINDOW_SIZES)
        root.iconphoto(True, *images)
    except tk.TclError:
        return False
    root._mirrorhold_icons = images          # kept alive with the root
    return True


def apply_style(root: tk.Misc) -> None:
    style = ttk.Style(root)
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    style.configure("Treeview", rowheight=int(line * 1.35))
    heading = tkfont.nametofont("TkHeadingFont").metrics("linespace")
    style.configure("Treeview.Heading", padding=(4, int(heading * 0.15)))
    for tone, colour in TONE_COLOURS.items():
        style.configure(f"{tone.value.capitalize()}.TLabel", foreground=colour)
    bold = tkfont.nametofont("TkDefaultFont").copy()
    bold.configure(weight="bold")
    root._mirrorhold_bold = bold          # kept alive with the root
    style.configure("Headline.TLabel", font=bold)
    for tone, colour in TONE_COLOURS.items():
        style.configure(f"{tone.value.capitalize()}Headline.TLabel", font=bold, foreground=colour)
    underlined = tkfont.nametofont("TkDefaultFont").copy()
    underlined.configure(underline=True)
    root._mirrorhold_underlined = underlined          # kept alive with the root
    style.configure(LINK_STYLE, font=underlined, foreground=TONE_COLOURS[Tone.ACTIVE])
    _divider_style(root, style, line)
    # Tk's class bindings let the wheel or a touchpad change a dropdown's choice, and they run
    # before _wheel_dispatch, which cannot stop them; once they are gone the wheel reaches it as
    # from any other control.
    for sequence in root.bind_class("TCombobox"):
        if "Wheel" in sequence or "Touchpad" in sequence:
            root.unbind_class("TCombobox", sequence)


def _divider_style(root: tk.Misc, style: ttk.Style, line: int) -> None:
    """``DIVIDER_STYLE``'s horizontal divider: a band about half a line tall holding a row
    of five dots, drawn as an image element over the theme's own divider. The image is
    sized from the font, so the grip scales with the display, and kept with the root."""
    if "Divider.hsash" in style.element_names():
        return
    band = max(6, int(line * 0.45))
    dot, gap, count = max(2, line // 10), max(3, line // 6), 5
    width = count * dot + (count - 1) * gap
    grip = tk.PhotoImage(master=root, width=width, height=band)
    top = (band - dot) // 2
    for i in range(count):
        left = i * (dot + gap)
        grip.put(_GRIP_COLOUR, to=(left, top, left + dot, top + dot))
    root._mirrorhold_grip = grip          # kept alive with the root
    style.element_create("Divider.hsash", "image", grip, sticky="")
    sash = f"{DIVIDER_STYLE}.Horizontal.Sash"
    style.layout(sash, [("Sash.hsash", {"sticky": "nswe", "children": [("Divider.hsash", {"sticky": ""})]})])
    style.configure(sash, sashthickness=band)


def tone_style(tone: Tone, *, headline: bool = False) -> str:
    if tone is Tone.NORMAL:
        return "Headline.TLabel" if headline else "TLabel"
    return f"{tone.value.capitalize()}{'Headline' if headline else ''}.TLabel"


def tag_tones(tree: ttk.Treeview) -> None:
    for tone, colour in TONE_COLOURS.items():
        tree.tag_configure(tone.value, foreground=colour)


def size_to_content(window: tk.Toplevel, *, min_width: int = 0, min_height: int = 0,
                    parent: Optional[tk.Misc] = None) -> Tuple[int, int]:
    """Open ``window`` at the size its content requests, within 90 % of the screen; the
    size set."""
    window.update_idletasks()
    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    width = min(max(min_width, window.winfo_reqwidth()), int(screen_w * 0.9))
    height = min(max(min_height, window.winfo_reqheight()), int(screen_h * 0.9))
    if parent is not None and parent.winfo_ismapped():
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
    else:
        x, y = (screen_w - width) // 2, (screen_h - height) // 3
    x = max(0, min(x, screen_w - width))
    y = max(0, min(y, screen_h - height))
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.minsize(min(width, max(min_width, 200)), min(height, max(min_height, 120)))
    return width, height


def grow_to_content(window: tk.Toplevel) -> Optional[Tuple[int, int]]:
    """Widen or heighten an open window whose content now asks for more, within 90 % of
    the screen, moving it up or left as far as it must to stay on the screen; it never
    shrinks under the user. The size set; None when the window already had room."""
    window.update_idletasks()
    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    width = min(max(window.winfo_width(), window.winfo_reqwidth()), int(screen_w * 0.9))
    height = min(max(window.winfo_height(), window.winfo_reqheight()), int(screen_h * 0.9))
    if (width, height) == (window.winfo_width(), window.winfo_height()):
        return None
    x = max(0, min(window.winfo_x(), screen_w - width))
    y = max(0, min(window.winfo_y(), screen_h - height - em(window, 2)))
    window.geometry(f"{width}x{height}+{x}+{y}")
    return width, height


def work_areas(widget: tk.Misc) -> Tuple[Area, ...]:
    """Each monitor's working area — its screen less the taskbar — the main monitor's
    first, in the physical pixels a DPI-aware process shares with Tk. Where Windows cannot
    be asked, the one screen Tk knows."""
    screen = (Area(0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()),)
    if sys.platform != "win32":
        return screen
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    found = []

    def visit(monitor, _dc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            found.append((not info.dwFlags & 1, Area(work.left, work.top, work.right, work.bottom)))
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                  wintypes.LPARAM)(visit)
    try:
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    except (AttributeError, OSError):
        return screen
    areas = tuple(area for _secondary, area in sorted(found, key=lambda f: f[0]) if area.width > 0 and area.height > 0)
    return areas or screen


def frame_size(window: tk.Misc) -> Tuple[int, int]:
    """How much wider and taller a mapped toplevel's frame is than its inside: the borders
    on both sides, and the title bar with the bottom border."""
    side = max(0, window.winfo_rootx() - window.winfo_x())
    top = max(0, window.winfo_rooty() - window.winfo_y())
    return 2 * side, top + side


def em(widget: tk.Misc, n: float) -> int:
    """``n`` times the default font's line height, in pixels: a size that scales."""
    return int(n * tkfont.nametofont("TkDefaultFont").metrics("linespace"))


def modal(window: tk.Toplevel, parent: tk.Misc) -> None:
    window.transient(parent.winfo_toplevel())
    window.grab_set()
    window.focus_set()


_SELF_SCROLLING = ("Treeview", "Text", "Listbox")


def _wheel_dispatch(event):
    """The one application-wide wheel handler: the ScrollFrame under the pointer scrolls,
    unless the pointer is over a widget that scrolls itself. An open dropdown's list is one
    tkinter does not know, so ``event.widget`` is then its path, a string."""
    try:
        widget = event.widget.winfo_containing(event.x_root, event.y_root)
    except (AttributeError, KeyError, tk.TclError):
        return None
    if widget is None or widget.winfo_class() in _SELF_SCROLLING:
        return None
    while widget is not None:
        if isinstance(widget, ScrollFrame):
            widget.scroll(event.delta)
            return "break"
        widget = widget.master
    return None


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame; put content in ``.body``. It scrolls by the wheel
    wherever the pointer is over it, its children included."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=ttk.Style(self).lookup("TFrame", "background") or None)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.body.bind("<Configure>", self._body_changed)
        self.canvas.bind("<Configure>", self._canvas_changed)
        root = self._root()
        if not getattr(root, "_mirrorhold_wheel", False):
            self.bind_all("<MouseWheel>", _wheel_dispatch)
            root._mirrorhold_wheel = True

    def _body_changed(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_changed(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def scroll(self, delta: int) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")

    def requested_height(self) -> int:
        self.body.update_idletasks()
        return self.body.winfo_reqheight()


def choose(parent: tk.Misc, title: str, text: str, buttons: Sequence[Tuple[str, str]], *, default: str,
           cancel: str) -> str:
    """Ask with named buttons ``(key, label)``; returns the key chosen, ``cancel`` when
    the window is closed."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.resizable(False, False)
    chosen = {"key": cancel}
    frame = ttk.Frame(window, padding=em(window, 0.8))
    frame.grid(sticky="nsew")
    ttk.Label(frame, text=text, wraplength=em(window, 22), justify="left").grid(row=0, column=0, sticky="w")
    row = ttk.Frame(frame)
    row.grid(row=1, column=0, sticky="e", pady=(em(window, 0.8), 0))

    def pick(key):
        chosen["key"] = key
        window.destroy()

    for i, (key, label) in enumerate(buttons):
        button = ttk.Button(row, text=label, command=lambda k=key: pick(k),
                            default="active" if key == default else "normal")
        button.grid(row=0, column=i, padx=(PAD if i else 0, 0))
        if key == default:
            button.focus_set()
    window.bind("<Return>", lambda e: pick(default))
    window.bind("<Escape>", lambda e: pick(cancel))
    window.protocol("WM_DELETE_WINDOW", lambda: pick(cancel))
    size_to_content(window, parent=parent)
    modal(window, parent)
    parent.wait_window(window)
    return chosen["key"]


class ListWindow:
    """Files with why and size; with ``grouped``, ``items`` are folder groups
    (``preview_model.FolderGroup``), each file named by its path within its group."""

    def __init__(self, parent: tk.Misc, title: str, heading: str, items, *, grouped: bool = False):
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        frame = ttk.Frame(self.window, padding=em(self.window, 0.6))
        frame.grid(sticky="nsew")
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        ttk.Label(frame, text=heading).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD))
        self.tree = ttk.Treeview(frame, columns=("why", "size"), height=min(18, max(6, len(items))))
        self.tree.heading("#0", text="Folder / file" if grouped else "File")
        self.tree.heading("why", text="Why")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=em(self.window, 18), stretch=True)
        self.tree.column("why", width=em(self.window, 11), stretch=False)
        self.tree.column("size", width=em(self.window, 4.5), stretch=False, anchor="e")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        bar.grid(row=1, column=1, sticky="ns")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self._fill(items, grouped)
        close = ttk.Button(frame, text="Close", default="active", command=self.window.destroy)
        close.grid(row=2, column=0, columnspan=2, sticky="e", pady=(PAD, 0))
        self.window.bind("<Return>", lambda e: self.window.destroy())
        self.window.bind("<Escape>", lambda e: self.window.destroy())
        size_to_content(self.window, parent=parent)
        self.window.transient(parent.winfo_toplevel())

    def _fill(self, items, grouped: bool) -> None:
        def row(parent_iid, name, item):
            self.tree.insert(parent_iid, "end", text=name,
                             values=(item.why, format_size(item.size) if item.size is not None else ""))

        if not grouped:
            for item in items:
                row("", item.path, item)
            return
        for position, group in enumerate(items):
            iid = self.tree.insert("", "end", text=f"{group.label}  ({len(group.files):,})", values=("", ""),
                                   open=position == 0)
            for item in group.files:
                row(iid, group.name(item), item)


def set_enabled(widget: ttk.Widget, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


HINT_DELAY_MS = 600
_HINT_BACKGROUND, _HINT_TEXT = "#FFFFE1", "#1A1A1A"   # the classic Windows hint background; 17:1


class Hint:
    """``text`` shown in a small borderless window below the pointer once it has rested on
    ``widget`` for ``HINT_DELAY_MS``; it closes when the pointer leaves or presses. A
    disabled control shows its hint too, so the hint can say why it is disabled. ``text``
    may be changed while the hint is closed; an empty one shows nothing."""

    def __init__(self, widget: tk.Misc, text: str):
        self.widget = widget
        self.text = text
        self.window: Optional[tk.Toplevel] = None
        self._pending = None
        widget.bind("<Enter>", self._entered, add="+")
        widget.bind("<Leave>", lambda e: self.close(), add="+")
        widget.bind("<ButtonPress>", lambda e: self.close(), add="+")
        widget.bind("<Destroy>", lambda e: self.close(), add="+")

    def _entered(self, event) -> None:
        self.close()
        self._pending = self.widget.after(HINT_DELAY_MS, lambda: self.show(event.x_root, event.y_root))

    def show(self, x: int, y: int) -> None:
        """Open the hint below the screen point (``x``, ``y``), kept on the screen."""
        self._pending = None
        if self.window is not None or not self.text:
            return
        w = self.window = tk.Toplevel(self.widget)
        w.wm_overrideredirect(True)
        tk.Label(w, text=self.text, justify="left", wraplength=em(w, 22), background=_HINT_BACKGROUND,
                 foreground=_HINT_TEXT, relief="solid", borderwidth=1, padx=em(w, 0.3), pady=em(w, 0.15)).pack()
        w.update_idletasks()
        below = y + em(w, 1.1)
        x = max(0, min(x, w.winfo_screenwidth() - w.winfo_reqwidth()))
        if below + w.winfo_reqheight() > w.winfo_screenheight():
            below = max(0, y - em(w, 0.4) - w.winfo_reqheight())
        w.geometry(f"+{x}+{below}")

    def close(self) -> None:
        if self._pending is not None:
            try:
                self.widget.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
