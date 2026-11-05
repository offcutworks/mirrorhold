"""The computer while an activity works (``mirrorhold.power``): kept from falling asleep by itself,
and the activity named on Windows' shutdown screen, as the user chooses, only while the activity
works.

The hold follows the activity's state and the user's choices through a system replaced by a
recorder, its clock replaced too; one test asks Windows itself and reads the system's execution
state back. Runs and verifications hold through the same seam: a run and a verification on
temporary folders with the recorder watching, the choice on and off, and a verification paused
and resumed. The suite switches the shutdown screen off (``support``); its tests switch it on
for themselves, and its real window is opened and closed without ever holding the shutdown."""
import ctypes
import os
import sys
import unittest
from unittest import mock

import support  # noqa: F401  (puts the project on the import path)
from test_maintenance import VerifyCase
from test_run import RunCase, write

from mirrorhold.jobs.model import Activities, Preferences
from mirrorhold.jobs.store import StoreBusy, StoreContents
from mirrorhold.maintenance.verification import VerificationState
from mirrorhold.power import (AWAKE, CHOICES_INTERVAL, SCREEN, SCREEN_CLASS, SCREEN_SWITCH_VARIABLE, SCREEN_TITLE, Hold,
                              WindowsSystem, run_reason, screen_switched_off, verification_reason)
from mirrorhold.records.record import Outcome
from mirrorhold.records.status import Phase, request
from mirrorhold.run.runner import RunOptions, run_job

WINDOWS = sys.platform == "win32"


class Recorder:
    """A system that records what is asked of it; ``refuse`` makes every request fail, each
    recorded as refused."""

    def __init__(self, refuse=False):
        self.events, self.refuse = [], refuse

    def awake(self, reason):
        if self.refuse:
            self.events.append(("refused", reason))
            raise OSError("refused")
        self.events.append(("create", reason))
        return _Recorded(self.events)


class _Recorded:
    def __init__(self, events):
        self.events = events

    def set(self, on):
        self.events.append(("set", on))

    def close(self):
        self.events.append(("close",))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HoldTest(unittest.TestCase):
    def setUp(self):
        self.recorder, self.clock = Recorder(), Clock()
        self.state = {"working": False, "choices": Activities()}

    def hold(self, recorder=None):
        def choices():
            found = self.state["choices"]
            if isinstance(found, Exception):
                raise found
            return found

        return Hold('Backing up "Work"', lambda: self.state["working"], choices, system=recorder or self.recorder,
                    clock=self.clock)

    def test_the_computer_is_kept_awake_only_while_the_activity_works(self):
        hold = self.hold()
        hold.tick()
        hold.choose(Activities())
        self.assertEqual(self.recorder.events, [], "nothing while it waits for its turn")
        self.state["working"] = True
        hold.tick()
        hold.tick()
        self.assertEqual(self.recorder.events, [("create", 'Backing up "Work"'), ("set", True)])
        self.assertEqual(hold.held, {"keep_awake": True})
        self.state["working"] = False                                  # paused, or waiting again
        hold.tick()
        self.state["working"] = True
        hold.tick()
        hold.release()
        self.assertEqual(self.recorder.events[2:], [("set", False), ("set", True), ("close",)])
        self.assertEqual(hold.held, {"keep_awake": False})
        hold.tick()
        self.assertEqual(self.recorder.events[-1], ("close",), "nothing after its end")

    def test_with_the_choice_off_nothing_is_asked(self):
        self.state["working"] = True
        hold = self.hold()
        hold.choose(Activities(keep_awake=False))
        hold.tick()
        hold.release()
        self.assertEqual(self.recorder.events, [])

    def test_a_change_of_the_choice_reaches_an_activity_already_working(self):
        self.state["working"] = True
        hold = self.hold()
        hold.choose(Activities())
        self.state["choices"] = Activities(keep_awake=False)
        self.clock.now += CHOICES_INTERVAL - 1
        hold.tick()
        self.assertEqual(hold.held, {"keep_awake": True}, "read again only once due")
        self.clock.now += 1
        hold.tick()
        self.assertEqual(hold.held, {"keep_awake": False})
        self.state["choices"] = StoreBusy("held")
        self.clock.now += CHOICES_INTERVAL
        hold.tick()
        self.assertEqual(hold.held, {"keep_awake": False}, "a job file that cannot be read leaves the last choice")
        self.state["choices"] = Activities()
        self.clock.now += CHOICES_INTERVAL
        hold.tick()
        self.assertEqual(hold.held, {"keep_awake": True})
        self.assertEqual(self.recorder.events, [("create", 'Backing up "Work"'), ("set", True), ("set", False),
                                                ("set", True)])

    def test_a_refusal_leaves_the_activity_working_and_is_not_asked_again(self):
        refusing = Recorder(refuse=True)
        self.state["working"] = True
        hold = self.hold(refusing)
        hold.choose(Activities())
        hold.tick()
        self.state["working"] = False                                  # paused, or waiting again
        hold.tick()
        self.state["working"] = True
        hold.tick()
        self.clock.now += CHOICES_INTERVAL                             # the choices read again
        hold.tick()
        self.assertEqual(hold.held, {"keep_awake": False})
        hold.release()
        self.assertEqual(hold.held, {"keep_awake": False})
        self.assertEqual(refusing.events, [("refused", 'Backing up "Work"')], "Windows is asked once")

    def test_what_the_request_says(self):
        self.assertEqual((run_reason("Photos"), verification_reason("Photos")),
                         ('Backing up "Photos"', 'Verifying the backup of "Photos"'))


def execution_state() -> int:
    state = ctypes.c_ulong()
    ctypes.WinDLL("powrprof").CallNtPowerInformation(16, None, 0, ctypes.byref(state), ctypes.sizeof(state))
    return state.value


@unittest.skipUnless(WINDOWS, "Windows' power requests")
class WindowsHoldTest(unittest.TestCase):
    def test_windows_keeps_the_system_required_while_held(self):
        before = execution_state() & 1
        hold = Hold('Backing up "a test of MirrorHold"', lambda: True, lambda: Activities(), system=WindowsSystem())
        try:
            hold.choose(Activities())
            self.assertEqual(hold.held, {"keep_awake": True})
            self.assertTrue(execution_state() & 1, "ES_SYSTEM_REQUIRED while the request is held")
        finally:
            hold.release()
        if not before:
            self.assertFalse(execution_state() & 1, "nothing else held it before, and nothing holds it after")


class RunHoldTest(RunCase):
    def run_with(self, recorder, **options):
        return run_job(self.store, self.job.job_id, RunOptions(**options), volume_source=self.volumes, sync_roots=(),
                       power=recorder)

    def test_a_run_keeps_the_computer_awake_while_it_works_and_gives_it_back(self):
        write(self.src, "a.txt", "alpha")
        recorder = Recorder()
        record = self.run_with(recorder, first_backup=True).records[0]
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(recorder.events[:2], [("create", 'Backing up "Work"'), ("set", True)])
        self.assertEqual(recorder.events[-1], ("close",))
        self.assertEqual(recorder.events.count(("create", 'Backing up "Work"')), 1)

    def test_with_the_choice_off_a_run_asks_nothing(self):
        self.store.save(StoreContents((self.job,), Preferences(activities=Activities(keep_awake=False))))
        write(self.src, "a.txt", "alpha")
        recorder = Recorder()
        self.assertEqual(self.run_with(recorder, first_backup=True).records[0].outcome, Outcome.COMPLETED)
        self.assertEqual(recorder.events, [])


class VerificationHoldTest(VerifyCase):
    def test_a_verification_holds_while_it_works_and_lets_go_while_paused(self):
        recorder = Recorder()
        thread, box = self.in_background(power=recorder, **self.slowly(6, 1024 * 1024))
        status = self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value and s)
        self.assertEqual(recorder.events[:2], [("create", 'Verifying the backup of "Work"'), ("set", True)])
        request(self.layout, status["run_id"], pause=True)
        self.wait(lambda: recorder.events[-1] == ("set", False))
        request(self.layout, status["run_id"], pause=False)
        self.wait(lambda: recorder.events[-1] == ("set", True))
        self.slow.clear()
        thread.join(60)
        self.assertNotIn("error", box)
        self.assertEqual(box["record"].state, VerificationState.COMPLETED, box["record"].detail)
        self.assertEqual(recorder.events[-1], ("close",))
        self.assertEqual([e for e in recorder.events if e[0] == "create"],
                         [("create", 'Verifying the backup of "Work"')])


# --- the shutdown screen ----------------------------------------------------------------------

class BothRecorder:
    """A system recording what is asked of each switch, by choice name; ``refuse`` names the
    switches whose making fails, each refusal recorded."""

    def __init__(self, refuse=()):
        self.events, self.refuse = [], set(refuse)

    def _make(self, name, reason):
        if name in self.refuse:
            self.events.append((name, "refused", reason))
            raise OSError("refused")
        self.events.append((name, "create", reason))
        return _NamedSwitch(self.events, name)

    def awake(self, reason):
        return self._make(AWAKE, reason)

    def at_shutdown(self, reason):
        return self._make(SCREEN, reason)

    def of(self, name):
        return [event[1:] for event in self.events if event[0] == name]


class _NamedSwitch:
    def __init__(self, events, name):
        self.events, self.name = events, name

    def set(self, on):
        self.events.append((self.name, "set", on))

    def close(self):
        self.events.append((self.name, "close"))


def screen_on(test):
    """The shutdown screen switched on for ``test`` alone; the suite switches it off."""
    patcher = mock.patch.dict(os.environ, {SCREEN_SWITCH_VARIABLE: "on"})
    patcher.start()
    test.addCleanup(patcher.stop)


class ShutdownScreenHoldTest(unittest.TestCase):
    """The activity named on the shutdown screen, as the user chooses, only while it works —
    beside the computer kept awake and apart from it."""

    def setUp(self):
        screen_on(self)
        self.recorder, self.clock = BothRecorder(), Clock()
        self.state = {"working": False, "choices": Activities()}

    def hold(self, recorder=None):
        def choices():
            found = self.state["choices"]
            if isinstance(found, Exception):
                raise found
            return found

        return Hold('Backing up "Work"', lambda: self.state["working"], choices, system=recorder or self.recorder,
                    clock=self.clock)

    def test_the_activity_is_named_at_shutdown_only_while_it_works(self):
        hold = self.hold()
        hold.choose(Activities())
        hold.tick()
        self.assertEqual(self.recorder.events, [], "nothing while it waits for its turn")
        self.assertEqual(hold.held, {AWAKE: False, SCREEN: False})
        self.state["working"] = True
        hold.tick()
        self.assertEqual(self.recorder.of(SCREEN), [("create", 'Backing up "Work"'), ("set", True)])
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: True})
        self.state["working"] = False                                  # paused, or waiting again
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: False, SCREEN: False})
        self.state["working"] = True
        hold.tick()
        hold.release()
        self.assertEqual(self.recorder.of(SCREEN)[2:], [("set", False), ("set", True), ("close",)])
        self.assertEqual(hold.held, {AWAKE: False, SCREEN: False})
        hold.tick()
        self.assertEqual(self.recorder.of(SCREEN)[-1], ("close",), "nothing after its end")

    def test_each_choice_holds_on_its_own(self):
        self.state["working"] = True
        hold = self.hold()
        hold.choose(Activities(name_at_shutdown=False))
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: False})
        self.assertEqual(self.recorder.of(SCREEN), [])
        hold.release()
        recorder = BothRecorder()
        other = self.hold(recorder)
        other.choose(Activities(keep_awake=False))
        other.tick()
        self.assertEqual(other.held, {AWAKE: False, SCREEN: True})
        other.release()
        self.assertEqual(recorder.of(AWAKE), [])
        self.assertEqual(recorder.of(SCREEN), [("create", 'Backing up "Work"'), ("set", True), ("close",)])

    def test_a_change_of_the_choice_reaches_an_activity_already_working(self):
        self.state["working"] = True
        hold = self.hold()
        hold.choose(Activities())
        self.state["choices"] = Activities(name_at_shutdown=False)
        self.clock.now += CHOICES_INTERVAL - 1
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: True}, "read again only once due")
        self.clock.now += 1
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: False})
        self.state["choices"] = StoreBusy("held")
        self.clock.now += CHOICES_INTERVAL
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: False}, "a job file that cannot be read leaves the last")
        self.state["choices"] = Activities()
        self.clock.now += CHOICES_INTERVAL
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: True})
        self.assertEqual(self.recorder.of(SCREEN), [("create", 'Backing up "Work"'), ("set", True), ("set", False),
                                                    ("set", True)])

    def test_a_refused_screen_leaves_the_computer_kept_awake_and_is_not_asked_again(self):
        refusing = BothRecorder(refuse=(SCREEN,))
        self.state["working"] = True
        hold = self.hold(refusing)
        hold.choose(Activities())
        hold.tick()
        self.state["working"] = False
        hold.tick()
        self.state["working"] = True
        hold.tick()
        self.clock.now += CHOICES_INTERVAL
        hold.tick()
        self.assertEqual(hold.held, {AWAKE: True, SCREEN: False})
        hold.release()
        self.assertEqual(refusing.of(SCREEN), [("refused", 'Backing up "Work"')], "Windows is asked once")
        self.assertEqual(refusing.of(AWAKE)[-1], ("close",))

    def test_a_process_with_the_screen_switched_off_takes_no_part_in_it(self):
        with mock.patch.dict(os.environ, {SCREEN_SWITCH_VARIABLE: "off"}):
            self.assertTrue(screen_switched_off())
            hold = self.hold()
        self.state["working"] = True
        hold.choose(Activities())
        hold.tick()
        hold.release()
        self.assertEqual(hold.held, {AWAKE: False})
        self.assertEqual(self.recorder.of(SCREEN), [])
        self.assertEqual(self.recorder.of(AWAKE)[:2], [("create", 'Backing up "Work"'), ("set", True)])

    def test_the_screen_is_on_at_first(self):
        self.assertTrue(Activities().name_at_shutdown)


class ScreenRunHoldTest(RunCase):
    def setUp(self):
        super().setUp()
        screen_on(self)

    def run_with(self, recorder, **options):
        return run_job(self.store, self.job.job_id, RunOptions(**options), volume_source=self.volumes, sync_roots=(),
                       power=recorder)

    def test_a_run_is_named_at_shutdown_while_it_works_and_lets_go_at_its_end(self):
        write(self.src, "a.txt", "alpha")
        recorder = BothRecorder()
        record = self.run_with(recorder, first_backup=True).records[0]
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        named = recorder.of(SCREEN)
        self.assertEqual(named[:2], [("create", 'Backing up "Work"'), ("set", True)])
        self.assertEqual(named[-1], ("close",))
        self.assertEqual(named.count(("create", 'Backing up "Work"')), 1)

    def test_with_the_choice_off_a_run_is_not_named(self):
        self.store.save(StoreContents((self.job,), Preferences(activities=Activities(name_at_shutdown=False))))
        write(self.src, "a.txt", "alpha")
        recorder = BothRecorder()
        self.assertEqual(self.run_with(recorder, first_backup=True).records[0].outcome, Outcome.COMPLETED)
        self.assertEqual(recorder.of(SCREEN), [])
        self.assertEqual(recorder.of(AWAKE)[0], ("create", 'Backing up "Work"'))


def screen_windows():
    """This process's windows of the shutdown screen's class and title: (handle, visible)."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    def each(hwnd, _):
        pid, name, title = wintypes.DWORD(), ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        user32.GetClassNameW(hwnd, name, 256)
        user32.GetWindowTextW(hwnd, title, 256)
        if pid.value == os.getpid() and (name.value, title.value) == (SCREEN_CLASS, SCREEN_TITLE):
            found.append((hwnd, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(enum(each), 0)
    return found


WM_CLOSE, WM_QUERYENDSESSION = 0x0010, 0x0011


def send(hwnd, message):
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    return user32.SendMessageW(hwnd, message, 0, 0)


@unittest.skipUnless(WINDOWS, "Windows' shutdown screen")
class WindowsScreenTest(unittest.TestCase):
    """The screen's real window, never holding: a test never holds up the machine's shutdown."""

    def test_a_window_closed_from_outside_leaves_the_switch_refusing(self):
        before = screen_windows()
        screen = WindowsSystem().at_shutdown('Backing up "a test of MirrorHold"')
        try:
            (hwnd, _visible), = [w for w in screen_windows() if w not in before]
            send(hwnd, WM_CLOSE)
            self.assertEqual(screen_windows(), before)
            with self.assertRaises(OSError):
                screen.set(True)
        finally:
            screen.close()
        self.assertEqual(screen_windows(), before)

    def test_the_window_is_hidden_says_yes_while_not_holding_and_goes_with_the_switch(self):
        before = screen_windows()
        first = WindowsSystem().at_shutdown('Backing up "a test of MirrorHold"')
        second = WindowsSystem().at_shutdown('Verifying the backup of "a test of MirrorHold"')
        try:
            opened = [w for w in screen_windows() if w not in before]
            self.assertEqual(len(opened), 2, "one window a switch")
            self.assertEqual([visible for _hwnd, visible in opened], [False, False])
            for hwnd, _visible in opened:
                self.assertEqual(send(hwnd, WM_QUERYENDSESSION), 1, "the session may end")
        finally:
            first.close()
            second.close()
        self.assertEqual(screen_windows(), before)
        first.close()                                                  # closing again does nothing


if __name__ == "__main__":
    unittest.main()
