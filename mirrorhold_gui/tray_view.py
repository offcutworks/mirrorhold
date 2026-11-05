"""The notification-area icon while an activity works, or a finished one's reminder stands, in a
process of its own. Tk.

    pythonw -m mirrorhold_gui.tray_view --store FILE

started by an activity (``mirrorhold.tray.offer``), never by the user. The process holds the
store's icon lock (``RuntimeLayout.tray_lock``) or ends at once; reads every status on the run
watcher's thread (``refresh.RunWatcher``) and the store's reminders (``mirrorhold.reminders``)
with each of its looks, and the job file beside the statuses, without ever acting on it
(``ConfigStore.peek``), so each activity's destination is numbered as its job stands now
(``runs_model.destination_number``); shows ``tray_model``'s look through Tk 9's own
``tk systray``; and ends
as ``tray_model.Lifetime`` says, letting the lock go and looking once more first, so an activity
that began or a reminder left meanwhile keeps it (``mirrorhold.tray``). The icon is made at the
first look that finds it needed, and stays until the process ends. The menu's dismissal clears
the reminders as opening the window does.

A left click opens the main window on the store, or brings forward the one open on it, which
``mark_main_window`` marked; a right click opens the menu, built from the latest look as it
opens, so an open menu never changes under the pointer. Its entries ask what the Runs tab's
buttons ask, with their questions (``runs_view.RunRequests``). A question opens over a host
window that cannot be seen — fully transparent, without frame or taskbar button — at the
screen's centre, mapped only while a question is open: Tk maps a question transient to its
parent only where the parent is mapped, and this process has no other window.

Tk says nothing when Windows refuses its icon, nor adds it again after the notification area
restarts unless its first add succeeded, so the process asks Windows itself every
``CHECK_SECONDS`` — an icon modification that changes nothing, which Windows accepts only for an
icon it holds — and makes the icon again as ``tray_model.MissingWatch`` says.

Like the window, the process is not one of the core's: it keeps no log, and whatever it fails at
costs the icon alone. A store whose Tk has no ``tk systray`` shows no icon; the process then
holds the lock until the work ends, so that each new activity does not start another.
"""
from __future__ import annotations

import argparse
import base64
import functools
import os
import queue
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox
from typing import Dict, List, Optional, Sequence

from mirrorhold import reminders
from mirrorhold.icon import AMBER, GREY, TEAL, png, render
from mirrorhold.invocation import gui_arguments, windowless_interpreter
from mirrorhold.jobs.store import ConfigStore
from mirrorhold.locations import PROJECT_ROOT, RuntimeLayout, runtime_layout
from mirrorhold.records.lock import FileLock
from mirrorhold.records.status import observe_runs
from mirrorhold.tray import start_detached

from .common_view import apply_icon, apply_style, claim_identity, prepare_display
from .refresh import RunsLook, RunWatcher
from .runs_model import (NO_PLACES, Places, RunPanel, can_ask_cancel, can_ask_pause, cancel_words,
                         destination_places, pause_words)
from .runs_view import RunRequests
from .tray_model import (CHECK_SECONDS, OPEN_PATIENCE, TITLE, WINDOW_PROPERTY, Lifetime, MissingWatch, TrayLook,
                         dismiss_words, hand_back, icon_id, tray_look, window_key)

LOOK_MS = 200
HOST_MS = 500
TK_TRAY_CLASS = "Wtk_TaskbarHandler"
"""The window class Tk registers for the window owning its tray icon, one per interpreter."""
NIM_MODIFY = 1
SM_CXSMICON = 49
SW_RESTORE = 9


# --- Windows ---------------------------------------------------------------------------------

@functools.cache
def _enum_proc():
    import ctypes
    import ctypes.wintypes as wt
    return ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


@functools.cache
def _user32():
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32")
    user32.EnumWindows.argtypes = [_enum_proc(), wt.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetPropW.argtypes = [wt.HWND, wt.LPCWSTR]
    user32.GetPropW.restype = wt.HANDLE
    user32.SetPropW.argtypes = [wt.HWND, wt.LPCWSTR, wt.HANDLE]
    user32.SetPropW.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    return user32


def icon_size() -> int:
    """The size Windows draws notification-area icons at, in pixels (16 at 100 % scale)."""
    if sys.platform == "win32":
        try:
            size = _user32().GetSystemMetrics(SM_CXSMICON)
            if size > 0:
                return size
        except (AttributeError, OSError):
            pass
    return 16


def _top_windows(match) -> List[int]:
    """The top-level windows on the desktop for which ``match(hwnd)`` holds."""
    user32, found = _user32(), []

    def each(hwnd, _):
        if match(user32, hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(_enum_proc()(each), 0)
    return found


def tray_window() -> Optional[int]:
    """This process's window owning Tk's tray icon, or None."""
    import ctypes
    import ctypes.wintypes as wt
    pid = os.getpid()

    def mine(user32, hwnd) -> bool:
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return False
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        return name.value == TK_TRAY_CLASS

    found = _top_windows(mine)
    return found[0] if found else None


def holds_icon(hwnd: int, uid: int) -> bool:
    """Whether the notification area holds the icon ``uid`` of the window ``hwnd``: a
    modification that changes nothing, which Windows refuses for an icon it does not hold."""
    import ctypes
    import ctypes.wintypes as wt

    class NotifyIconData(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                    ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                    ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                    ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wt.HICON)]

    notify = ctypes.WinDLL("shell32").Shell_NotifyIconW
    notify.argtypes = [wt.DWORD, ctypes.POINTER(NotifyIconData)]
    notify.restype = wt.BOOL
    data = NotifyIconData(cbSize=ctypes.sizeof(NotifyIconData), hWnd=hwnd, uID=uid, uFlags=0)
    return bool(notify(NIM_MODIFY, ctypes.byref(data)))


def find_main_window(layout_root: Path) -> Optional[int]:
    """The main window open on the store whose runtime files are in ``layout_root``, or None."""
    if sys.platform != "win32":
        return None
    key = window_key(layout_root)
    found = _top_windows(lambda user32, hwnd: (user32.GetPropW(hwnd, WINDOW_PROPERTY) or 0) == key)
    return found[0] if found else None


def bring_forward(hwnd: int) -> None:
    """Restore the window if it is minimised and bring it to the front. Windows lets this
    process do it because the user just clicked its icon."""
    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def mark_main_window(root: tk.Tk, layout_root: Path) -> bool:
    """Mark the main window with its store (``tray_model.WINDOW_PROPERTY``), so the icon's
    *Open MirrorHold* finds it; whether Windows took the mark. Unmarked, the icon opens another
    window instead of bringing this one forward."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(root.wm_frame(), 16)
        return bool(_user32().SetPropW(hwnd, WINDOW_PROPERTY, window_key(layout_root)))
    except (AttributeError, OSError, ValueError, tk.TclError):
        return False


# --- the icon --------------------------------------------------------------------------------

def tile(look: TrayLook):
    """The tile's colour for ``look``: amber while a reminder stands, else grey while paused."""
    return AMBER if look.reminders else GREY if look.paused else TEAL


def picture(root: tk.Misc, size: int, look: TrayLook) -> tk.PhotoImage:
    """The icon for ``look``: the copy filled by its progress, on its tile (``tile``)."""
    pixels = render(size, fill=look.fill, tile=tile(look))
    return tk.PhotoImage(master=root, data=base64.b64encode(png(size, pixels)).decode("ascii"))


def reminder_lines(layout: RuntimeLayout) -> List[str]:
    """The standing reminders' lines, oldest first; none when they cannot be listed: the icon
    then shows what works, and the window still clears them. Read on the icon's own thread, so
    a file another program keeps refused is not waited for: it is shown in general words."""
    try:
        return [r.title for r in reminders.standing(layout, patience=0)]
    except OSError:
        return []


class TrayIcon:
    def __init__(self, root: tk.Tk, layout: RuntimeLayout, store_path: Path, lock: FileLock):
        self.root = root
        self.layout = layout
        self.store_path = store_path
        self.lock = lock
        self.size = icon_size()
        self.look = TrayLook()
        self.statuses: Dict[str, dict] = {}
        self.places: Places = NO_PLACES
        self.lifetime = Lifetime()
        self.missing = MissingWatch()
        self.made = False
        self.unavailable = False          # this Tk has no tray icon
        self.image: Optional[tk.PhotoImage] = None
        self._shown_picture: Optional[tuple] = None
        self._shown_text = ""
        self._opened_at: Optional[float] = None
        self._looks: "queue.Queue[RunsLook]" = queue.Queue()
        self.watcher = RunWatcher(layout, self._publish)
        bold = tkfont.nametofont("TkMenuFont").copy()
        bold.configure(weight="bold")
        self.bold = bold
        self.menu = tk.Menu(root, tearoff=False, postcommand=self._build_menu)
        self._submenus: List[tk.Menu] = []
        self.host = tk.Toplevel(root)
        self.host.withdraw()
        self.host.overrideredirect(True)
        self.host.attributes("-alpha", 0.0)
        self._warnings = 0                # native warnings open over the host, which Tk does not list
        self.requests = RunRequests(self._question_parent, layout, self.statuses_of, timer=root, warn=self.warn,
                                    places=lambda: self.places)
        self._click = root.register(self.open_window)
        self._popup = root.register(self.popup)

    def start(self) -> None:
        self.watcher.start()
        self.root.after(LOOK_MS, self._poll)
        self.root.after(int(CHECK_SECONDS * 1000), self._check)

    def statuses_of(self, run_id: str) -> Optional[dict]:
        return self.statuses.get(run_id)

    def _publish(self, look: RunsLook) -> None:
        """Hand a look to the icon with each job's destinations' numbers as the job file holds
        them now, read on the watcher's thread, so a file another program keeps open delays the
        next look, never the icon; where it cannot be read the icon keeps the numbers it last
        had — an activity's own until the file is first read. The numbers are replaced
        whole, never changed in place, so the icon's thread reads one set or the other."""
        contents = ConfigStore(self.store_path).peek()
        if contents is not None:
            self.places = destination_places(contents.jobs)
        self._looks.put(look)

    # --- looking -----------------------------------------------------------------------------

    def _poll(self) -> None:
        if not self._take_look():
            self.root.after(LOOK_MS, self._poll)

    def _take_look(self) -> bool:
        """Show the watcher's latest look; whether the process ended. A look that failed, or
        statuses this copy cannot word, show the standing reminders alone, and count as a failed
        look (``tray_model.Lifetime``) where none stands: what works cannot be told, but what was
        left to look at can, and a process kept by a reminder then shows it."""
        latest: Optional[RunsLook] = None
        while True:
            try:
                latest = self._looks.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return False
        now = time.monotonic()
        try:
            if latest.failed:
                raise OSError(latest.failed)
            look = tray_look(latest.runs, reminder_lines(self.layout), self.places)
            statuses = {r.run_id: r.status for r in latest.runs if r.readable}
        except Exception:  # whether an activity works cannot be told from this look
            lines = reminder_lines(self.layout)
            if not lines:
                return self.lifetime.on_failure(now) and self._end()
            look, statuses = tray_look([], lines), {}
        self.statuses = statuses
        self.look = look
        if self.lifetime.on_look(look.needed, now) and self._end():
            return True
        try:
            self._show()
        except Exception:  # an icon that cannot be drawn is not shown; the process still ends with the work
            self.unavailable = True
        return False

    def _end(self) -> bool:
        """Hand the lock back and look once more (``tray_model.hand_back``): end, unless the
        icon is kept. Whether the process ends."""
        if hand_back(self.lock, self._still_needed):
            self.lifetime = Lifetime()
            return False
        self.watcher.stop()
        self._take_down()
        self.root.destroy()
        return True

    def _still_needed(self) -> bool:
        """The last look before the process ends: a standing reminder first, so statuses that
        cannot be read never take the icon from a reminder."""
        return bool(reminder_lines(self.layout)) or any(r.alive for r in observe_runs(self.layout))

    # --- showing -----------------------------------------------------------------------------

    def _show(self) -> None:
        look = self.look
        if self.unavailable or not (look.needed or self.made):
            return
        key = (round(look.fill * 100), tile(look))
        try:
            if key != self._shown_picture:
                self.image = picture(self.root, self.size, look)    # kept alive while Windows shows it
            if not self.made:
                self._make()
            elif key != self._shown_picture or look.tooltip != self._shown_text:
                self.root.tk.call("tk", "systray", "configure", "-image", self.image, "-text", look.tooltip)
        except tk.TclError:
            self.unavailable = True
            return
        self._shown_picture, self._shown_text = key, look.tooltip

    def _make(self) -> None:
        self.root.tk.call("tk", "systray", "create", "-image", self.image, "-text", self.look.tooltip,
                          "-button1", self._click, "-button3", self._popup)
        self.made = True

    def _take_down(self) -> None:
        if self.made:
            try:
                self.root.tk.call("tk", "systray", "destroy")
            except tk.TclError:
                pass
            self.made = False

    def _check(self) -> None:
        if self.made and self.missing.on_check(self._present()):
            self._take_down()
            try:
                self._make()
            except tk.TclError:
                self.unavailable = True
        if self.root.winfo_exists():
            self.root.after(int(CHECK_SECONDS * 1000), self._check)

    def _present(self) -> Optional[bool]:
        """Whether Windows holds the icon now; None where that cannot be asked."""
        if sys.platform != "win32":
            return None
        try:
            if not self.root.tk.getboolean(self.root.tk.eval("tk systray exists")):
                return False
            uid = icon_id(str(self.root.tk.eval("set ::tk::systray::_ico")))
            hwnd = tray_window()
        except (tk.TclError, OSError):
            return None
        return holds_icon(hwnd, uid) if hwnd and uid is not None else None

    # --- the menu ----------------------------------------------------------------------------

    def popup(self) -> None:
        # The window owning the menu must be in front, or Windows leaves the menu open when the
        # pointer goes elsewhere; the root is that window even while it is withdrawn.
        self.root.focus_force()
        self.menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _build_menu(self) -> None:
        for submenu in self._submenus:
            submenu.destroy()
        self._submenus = []
        menu = self.menu
        menu.delete(0, "end")
        menu.add_command(label=f"Open {TITLE}", command=self.open_window, font=self.bold)
        entries = self.look.entries
        if entries:
            menu.add_separator()
        if len(entries) == 1:
            menu.add_command(label=entries[0].line, state="disabled")
            self._add_requests(menu, entries[0].panel)
        else:
            for entry in entries:
                submenu = tk.Menu(menu, tearoff=False)
                self._add_requests(submenu, entry.panel)
                menu.add_cascade(label=entry.line, menu=submenu)
                self._submenus.append(submenu)
        lines = self.look.reminders
        if lines:
            menu.add_separator()
            for line in lines:
                menu.add_command(label=line, state="disabled")
            menu.add_command(label=dismiss_words(len(lines)), command=self.dismiss)

    def _add_requests(self, menu: tk.Menu, panel: RunPanel) -> None:
        menu.add_command(label=pause_words(panel), state="normal" if can_ask_pause(panel) else "disabled",
                         command=lambda: self._ask(panel.run_id, self.requests.pause_or_resume))
        menu.add_command(label=cancel_words(panel), state="normal" if can_ask_cancel(panel) else "disabled",
                         command=lambda: self._ask(panel.run_id, self.requests.cancel_or_stop))

    def _ask(self, run_id: str, ask) -> None:
        """Ask of the activity as its panel stands now: the menu may have been open a while."""
        for entry in self.look.entries:
            if entry.panel.run_id == run_id:
                ask(entry.panel)
                return

    def _question_parent(self) -> tk.Misc:
        if not self.host.winfo_ismapped():
            width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.host.geometry(f"1x1+{width // 2}+{height // 3}")
            self.host.deiconify()
            self.host.update_idletasks()
            self.root.after(HOST_MS, self._park_host)
        return self.host

    def _park_host(self) -> None:
        """Unmap the host once no question and no warning is open over it."""
        if self._warnings or any(isinstance(child, tk.Toplevel) for child in self.host.winfo_children()):
            self.root.after(HOST_MS, self._park_host)
        else:
            self.host.withdraw()

    def warn(self, title: str, text: str) -> None:
        """A warning over the host, which stays mapped while it is open: Windows' own warning
        is no child Tk can list."""
        self._warnings += 1
        try:
            messagebox.showwarning(title, text, parent=self._question_parent())
        finally:
            self._warnings -= 1

    def dismiss(self) -> None:
        """Clear the store's reminders, as opening the window does, and look again at once."""
        reminders.clear(self.layout)
        self.watcher.look_now()

    # --- the window --------------------------------------------------------------------------

    def open_window(self) -> None:
        """Bring forward the main window open on this store, or open one."""
        hwnd = find_main_window(self.layout.root)
        if hwnd:
            bring_forward(hwnd)
            return
        now = time.monotonic()
        if self._opened_at is not None and now - self._opened_at < OPEN_PATIENCE:
            return
        self._opened_at = now
        try:
            start_detached([str(windowless_interpreter()), *gui_arguments("--store", self.store_path)],
                           PROJECT_ROOT)
        except OSError as exc:
            self.warn(f"{TITLE} not opened", f"{TITLE} could not be opened: {exc}.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mirrorhold_gui.tray_view", description="MirrorHold's notification-area icon")
    parser.add_argument("--store", type=Path, required=True, help="the configuration store whose activities it shows")
    args = parser.parse_args(argv)
    layout = runtime_layout(args.store)
    lock = FileLock(layout.tray_lock)
    if not lock.try_acquire():
        return 0
    prepare_display()
    claim_identity()
    root = tk.Tk()
    root.withdraw()
    apply_style(root)
    apply_icon(root)
    TrayIcon(root, layout, args.store, lock).start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
