"""The computer while an activity works: kept from falling asleep by itself, and the activity
named on Windows' shutdown screen.

Both are the user's choices (``jobs.model.Activities.keep_awake`` and ``name_at_shutdown``, on
by default) and hold only while the activity *works*: from the moment it is admitted
(``records.admission``) to its end, but not while it waits for its turn, nor while the user has
paused it (the maintainer's decision, 2026-10-15). The process re-reads the choices from the
job file every ``CHOICES_INTERVAL`` seconds while it works, so a change saved in the window
reaches an activity already working within that time.

Keeping the computer awake is a power request (``PowerCreateRequest``, ``PowerSetRequest`` with
``PowerRequestSystemRequired``) carrying the activity's reason, held by the activity's own
process and released by its end — a process that dies takes its request with it. It holds off
the computer's own idle sleep and nothing else: sleep chosen from the Start menu, the display's
timer and every power setting are left as they are. Windows lists the request under SYSTEM in
``powercfg /requests``, with its reason, and sets ``ES_SYSTEM_REQUIRED`` in the system's
execution state while it is held (measured on the development machine, 2026-10-15, from a
windowless process as the core's are).

Naming the activity at shutdown is a hidden top-level window titled ``SCREEN_TITLE``, on a
thread of the activity's process that does nothing else, with the activity's reason registered
on it (``ShutdownBlockReasonCreate``) while it holds. Asked whether the session may end
(``WM_QUERYENDSESSION``), the window answers no while it holds and yes otherwise, so Windows'
shutdown screen lists it with its reason and offers to cancel. The window is real because a
process without one takes no part in that screen: a message-only window does not receive the
question, and a console's handler is not called for it once the window library is loaded. At
the activity's end the reason and the window go, and nothing of the process is left for the
screen to wait for; a shutdown the user lets go ends the process as a crash would, and the
activity reads as interrupted and continues at its next run. Measured on the development
machine: a windowless process registers the reason and reads it back. Not measured on a desktop
of one's own: whether the screen lists such a window, and what the screen does when the work
ends while it shows (the maintainer's decision of 2026-10-21 to build it without that
measurement). In Windows Sandbox, a remote session, Windows ended the session about ten seconds
after the window answered no, and showed no screen.

``SCREEN_SWITCH_VARIABLE`` set to ``off`` makes a process, and every activity it starts, take no
part in the shutdown screen; the test suite sets it, so no test holds up the machine's shutdown.

A request Windows refuses is said once in the diagnostic log and leaves the activity working:
keeping the computer awake and naming the activity are courtesies to the activity, never
conditions of it. The Windows calls sit behind ``System``, which tests replace.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Callable, Dict, Optional, Protocol

from .jobs.model import Activities
from .records import diagnostics

CHOICES_INTERVAL = 5.0
"""Seconds between two readings of the user's choices while an activity works."""

AWAKE = "keep_awake"
SCREEN = "name_at_shutdown"

SCREEN_SWITCH_VARIABLE = "MIRRORHOLD_SHUTDOWN_SCREEN"
"""``off`` makes this process, and every activity it starts, take no part in Windows' shutdown
screen."""


def screen_switched_off() -> bool:
    return os.environ.get(SCREEN_SWITCH_VARIABLE, "").strip().lower() == "off"


def run_reason(job_name: str) -> str:
    """What a run's requests say: to whoever lists the computer's power requests, and on the
    shutdown screen beside ``SCREEN_TITLE``."""
    return f'Backing up "{job_name}"'


def verification_reason(job_name: str) -> str:
    """What a full verification's request says."""
    return f'Verifying the backup of "{job_name}"'


def repair_reason(job_name: str) -> str:
    """What a repair's request says (``maintenance.repairer``)."""
    return f'Replacing backup copies of "{job_name}"'


class Switch(Protocol):
    """One thing an activity asks of Windows, set on and off while it works."""

    def set(self, on: bool) -> None: ...

    def close(self) -> None: ...


class System(Protocol):
    def awake(self, reason: str) -> Switch: ...

    def at_shutdown(self, reason: str) -> Switch: ...


_ASKED = {AWAKE: (lambda system, reason: system.awake(reason), "keep the computer awake"),
          SCREEN: (lambda system, reason: system.at_shutdown(reason), "name the activity on the shutdown screen")}
"""Each choice's switch, as the system makes it, and what a refusal of it says."""


class Hold:
    """What one activity asks of the computer, following its state and the user's choices.

    ``working`` answers whether the activity works now; ``choices`` reads the user's choices
    from the job file (it may raise: the last choices then stand). ``choose`` gives the
    choices read as the activity is admitted; ``tick``, called twice a second by the status
    pump, follows the activity's state and re-reads the choices when they are due; ``release``
    gives everything back at the activity's end. A process with the shutdown screen switched
    off (``SCREEN_SWITCH_VARIABLE``) acts on the computer's sleep alone. Thread-safe: the pump
    ticks on its own thread."""

    def __init__(self, reason: str, working: Callable[[], bool], choices: Callable[[], Activities], *,
                 system: Optional[System] = None, clock: Callable[[], float] = time.monotonic):
        self._reason = reason
        self._working = working
        self._read = choices
        self._system = system if system is not None else default_system()
        self._clock = clock
        self._lock = threading.Lock()
        self._choices: Optional[Activities] = None
        self._read_at = 0.0
        self._switches: Dict[str, Switch] = {}
        self._on: Dict[str, bool] = dict.fromkeys((AWAKE,) if screen_switched_off() else (AWAKE, SCREEN), False)
        self._broken: set = set()
        self._unreadable = False

    def choose(self, choices: Activities) -> None:
        with self._lock:
            self._choices, self._read_at = choices, self._clock()
            self._apply()

    def tick(self) -> None:
        with self._lock:
            if self._choices is not None and self._clock() - self._read_at >= CHOICES_INTERVAL:
                self._refresh()
            self._apply()

    def release(self) -> None:
        with self._lock:
            self._choices = None
            for name, switch in self._switches.items():
                try:
                    switch.close()
                except OSError as exc:
                    diagnostics.note(f"{self._reason}: {name} could not be given back: {exc}")
            self._switches.clear()
            self._on = dict.fromkeys(self._on, False)

    @property
    def held(self) -> Dict[str, bool]:
        """What is held now, by choice name: each choice this process acts on."""
        with self._lock:
            return dict(self._on)

    def _refresh(self) -> None:
        self._read_at = self._clock()
        try:
            self._choices = self._read()
            self._unreadable = False
        except Exception as exc:  # the job file held or unreadable now: the last choices stand
            if not self._unreadable:
                diagnostics.note(f"{self._reason}: the choices for the computer could not be read again; the last "
                                 f"ones stand: {type(exc).__name__}: {exc}")
            self._unreadable = True

    def _apply(self) -> None:
        working = self._choices is not None and self._working()
        wanted = {AWAKE: working and self._choices.keep_awake, SCREEN: working and self._choices.name_at_shutdown}
        for name, on in wanted.items():
            if name not in self._on or on == self._on[name] or name in self._broken:
                continue
            make, words = _ASKED[name]
            try:
                switch = self._switches.get(name)
                if switch is None:
                    switch = self._switches[name] = make(self._system, self._reason)
                switch.set(on)
                self._on[name] = on
            except OSError as exc:
                self._broken.add(name)
                diagnostics.note(f"{self._reason}: Windows refused to {words}; the activity goes on without it: "
                                 f"{exc}")


# --- Windows -------------------------------------------------------------------------------

_POWER_REQUEST_SYSTEM_REQUIRED = 1
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1


class _ReasonContext(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("Flags", ctypes.c_ulong), ("SimpleReasonString", ctypes.c_wchar_p)]


class _PowerRequest:
    """A power request of this process, with its reason: system required while set."""

    def __init__(self, reason: str):
        from ctypes import wintypes
        self._kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
        kernel32.PowerCreateRequest.restype = wintypes.HANDLE
        kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._context = _ReasonContext(0, _POWER_REQUEST_CONTEXT_SIMPLE_STRING, reason)
        handle = kernel32.PowerCreateRequest(ctypes.byref(self._context))
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        self._set = False

    def set(self, on: bool) -> None:
        call = self._kernel32.PowerSetRequest if on else self._kernel32.PowerClearRequest
        if not call(self._handle, _POWER_REQUEST_SYSTEM_REQUIRED):
            raise ctypes.WinError(ctypes.get_last_error())
        self._set = on

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            if self._set:
                self.set(False)
        finally:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


SCREEN_TITLE = "MirrorHold"
"""The hidden window's title: the name the shutdown screen gives the activity's reason."""
SCREEN_CLASS = "MirrorHoldShutdownScreen"

_WM_DESTROY, _WM_CLOSE, _WM_QUERYENDSESSION = 0x0002, 0x0010, 0x0011
_WM_HOLD = 0x8001                     # WM_APP + 1: hold the shutdown (wParam 1) or let it go (0)
_ENDSESSION_CLOSEAPP = 0x00000001    # lParam: an installer's Restart Manager closing the program
_WS_OVERLAPPEDWINDOW = 0x00CF0000
_OPEN_PATIENCE = 10.0                # seconds the window's thread has to open it, and to close it

_windows: Dict[int, "_ShutdownScreen"] = {}    # open screens by window handle, for the one procedure
_class_lock = threading.Lock()
_window_class: Optional[tuple] = None          # (user32, kernel32, procedure) once registered


def _registered_class():
    """user32 and kernel32 typed for the screen's window, its class registered once per process
    with one window procedure that hands each message to the screen owning the window (a class
    keeps the procedure it was registered with, so a procedure per screen could not be). A class
    of that name already registered — by another copy of this module, whose procedure would
    never reach these screens — is a refusal, not a class to share."""
    global _window_class
    with _class_lock:
        if _window_class is None:
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            lresult = ctypes.c_ssize_t
            wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            class WndClassExW(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", wndproc),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                            ("hIconSm", wintypes.HICON)]

            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = lresult
            user32.RegisterClassExW.argtypes = [ctypes.POINTER(WndClassExW)]
            user32.RegisterClassExW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                               wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
            user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                           wintypes.UINT]
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = lresult
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def procedure(hwnd, message, wparam, lparam):
                screen = _windows.get(hwnd)
                if screen is not None:
                    try:
                        answer = screen._answer(hwnd, message, wparam, lparam)
                    except Exception as exc:  # a window procedure must not raise into Windows
                        diagnostics.note(f"{screen._reason}: the shutdown screen's window failed: "
                                         f"{type(exc).__name__}: {exc}")
                        answer = None
                    if answer is not None:
                        return answer
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            procedure = wndproc(procedure)
            window_class = WndClassExW(ctypes.sizeof(WndClassExW), 0, procedure, 0, 0,
                                       kernel32.GetModuleHandleW(None), None, None, None, None, SCREEN_CLASS, None)
            if not user32.RegisterClassExW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            _window_class = (user32, kernel32, procedure)
        return _window_class[0], _window_class[1]


class _ShutdownScreen:
    """The activity named on Windows' shutdown screen while set: a hidden top-level window of
    this process, on a thread of its own that pumps its messages, with the reason registered
    on it and the session's end refused while it holds.

    ``set`` and ``close`` come from other threads and reach the window as messages sent to it:
    Windows takes a window's reason only from the thread that made the window. A window closed
    from outside drops its reason and leaves the screen closed: ``set`` then refuses.

    Only a session's end is refused: an installer's Restart Manager asking the program to close
    (``ENDSESSION_CLOSEAPP``) is answered yes, as a process without the window would be."""

    def __init__(self, reason: str):
        self._user32, kernel32 = _registered_class()
        self._reason = reason
        self._hwnd: Optional[int] = None
        self._held = False
        self._error = 0
        self._opening = threading.Lock()
        self._abandoned = False
        opened, failure = threading.Event(), []

        def pump():
            from ctypes import wintypes
            hwnd = self._user32.CreateWindowExW(0, SCREEN_CLASS, SCREEN_TITLE, _WS_OVERLAPPEDWINDOW, 0, 0, 0, 0,
                                                None, None, kernel32.GetModuleHandleW(None), None)
            if not hwnd:
                failure.append(ctypes.get_last_error())
                opened.set()
                return
            _windows[hwnd] = self
            with self._opening:
                abandoned = self._abandoned
                if not abandoned:
                    self._hwnd = hwnd
            if abandoned:                        # opened after the opener stopped waiting
                self._user32.DestroyWindow(hwnd)
            opened.set()
            message = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))

        self._thread = threading.Thread(target=pump, name="MirrorHold shutdown screen", daemon=True)
        self._thread.start()
        opened.wait(_OPEN_PATIENCE)
        with self._opening:
            if self._hwnd is None:
                self._abandoned = True
        if self._hwnd is None:
            if failure:
                raise ctypes.WinError(failure[0])
            raise OSError("the shutdown screen's window did not open")

    def _answer(self, hwnd: int, message: int, wparam: int, lparam: int) -> Optional[int]:
        """The window's own answer to ``message``, on its thread; None leaves it to Windows."""
        if message == _WM_QUERYENDSESSION:
            return 0 if self._held and not lparam & _ENDSESSION_CLOSEAPP else 1
        if message == _WM_HOLD:
            if bool(wparam) == self._held:
                return 1
            done = (self._user32.ShutdownBlockReasonCreate(hwnd, self._reason) if wparam
                    else self._user32.ShutdownBlockReasonDestroy(hwnd))
            if not done:
                self._error = ctypes.get_last_error()
                return 0
            self._held = bool(wparam)
            return 1
        if message == _WM_CLOSE:
            if self._held:
                self._user32.ShutdownBlockReasonDestroy(hwnd)
                self._held = False
            self._user32.DestroyWindow(hwnd)
            return 0
        if message == _WM_DESTROY:
            _windows.pop(hwnd, None)
            if self._hwnd == hwnd:
                self._hwnd, self._held = None, False
            self._user32.PostQuitMessage(0)
            return 0
        return None

    def set(self, on: bool) -> None:
        hwnd = self._hwnd
        if hwnd is None:
            raise OSError("the shutdown screen's window is closed")
        self._error = 0
        if not self._user32.SendMessageW(hwnd, _WM_HOLD, 1 if on else 0, 0):
            if self._error:
                raise ctypes.WinError(self._error)
            raise OSError("the shutdown screen's window did not answer")

    def close(self) -> None:
        hwnd = self._hwnd
        if hwnd is not None:
            self._user32.SendMessageW(hwnd, _WM_CLOSE, 0, 0)
        self._hwnd = None
        self._thread.join(_OPEN_PATIENCE)


class WindowsSystem:
    def awake(self, reason: str) -> Switch:
        return _PowerRequest(reason)

    def at_shutdown(self, reason: str) -> Switch:
        return _ShutdownScreen(reason)


class _Nothing:
    def set(self, on: bool) -> None:
        pass

    def close(self) -> None:
        pass


class NoSystem:
    """Away from Windows nothing is asked of the computer."""

    def awake(self, reason: str) -> Switch:
        return _Nothing()

    def at_shutdown(self, reason: str) -> Switch:
        return _Nothing()


def default_system() -> System:
    return WindowsSystem() if sys.platform == "win32" else NoSystem()
