"""The notification-area icon while an activity works or a finished one's reminder stands
(``mirrorhold.tray``, ``mirrorhold.reminders``, ``mirrorhold_gui.tray_model``,
``mirrorhold_gui.tray_view``), headless.

What the icon shows for the statuses observed and the reminders standing — its tooltip, its
menu's words, its fill, its grey and its amber — and when its process ends; the picture drawn
from the program's mark; its start by every kind of activity once its status is published, never
while another process holds the store's icon lock, never with the switch off, and never failing
the activity; the hand-back of the lock as the process ends; and the process ending at once when
another holds the lock. Reminders: kept one per job, read back, cleared — by the main window
only while it has the focus — shown when the statuses cannot be read, left by every notice
that asks something of the user — from the command line and from an automatic run, a run that
could not start included — written before the icon is brought up, and costing nothing when
they cannot be written. The suite switches the icon off (``support``): no test here shows one,
and the tests of its start pass a start of their own."""
import io
import os
import queue
import subprocess
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (puts the project on the import path)
from support import TempDirTest, endpoint, live_activity, volume
from test_automation import FakeRunner, Refusing, Shown, a_job, a_record
from test_maintenance import VerifyCase
from test_repair import RepairCase, damage
from test_run import RunCase, write

from mirrorhold import icon, reminders
from mirrorhold.automation.automatic import run_scheduled
from mirrorhold.cli import main as cli_main
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.invocation import TRAY_MODULE
from mirrorhold.jobs.model import Notifications, Triggers, new_job
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.notifications import show
from mirrorhold.notifications.message import Notice, failure_notice, notice_for
from mirrorhold.records.lock import FileLock
from mirrorhold.records.record import Outcome
from mirrorhold.records.status import ObservedRun, observe_runs
from mirrorhold.run.runner import RunError
from mirrorhold.tray import SWITCH_VARIABLE, offer, switched_off, tray_command
from mirrorhold_gui import tray_view
from mirrorhold_gui.main_view import MainWindow
from mirrorhold_gui.refresh import RunsLook
from mirrorhold_gui.runs_model import can_ask_cancel, can_ask_pause, cancel_words, pause_words, run_panel
from mirrorhold_gui.tray_model import (FAILING_SECONDS, LINGER_SECONDS, TIP_LIMIT, Lifetime, MissingWatch, TrayLook,
                                       activity_title, dismiss_words, hand_back, icon_id, progress_words, tooltip,
                                       tray_look, units, window_key)

REVIEW = "Recipes: a backup needs your review"
JOB_A, JOB_B = "a" * 32, "b" * 32


def status(run_id="a", *, job="Photos", kind="run", phase="copying", pair_index=1, pair_total=1, planned=0, done=0,
           eta=None, paused=False, place=0, **extra):
    doc = {"run_id": run_id * 32 if len(run_id) == 1 else run_id, "job_id": "j", "job_name": job, "kind": kind,
           "phase": phase, "pair_index": pair_index, "pair_total": pair_total, "finished": [],
           "planned": {"files_to_copy": 10, "bytes_to_copy": planned}, "done": {"files_copied": 1, "bytes_copied": done},
           "eta_seconds": eta, "paused": paused, "admitted": phase != "waiting", "place": place,
           "started": "2026-10-31T10:00:00Z", "queued": f"2026-10-31T10:00:0{len(run_id) % 10}.000000Z"}
    doc.update(extra)
    return doc


def alive(doc):
    return ObservedRun(doc, True)


class WordsTest(unittest.TestCase):
    def test_what_an_activity_is(self):
        self.assertEqual(activity_title(status()), "Backing up Photos -> 1")
        self.assertEqual(activity_title(status(pair_total=2)), "Backing up Photos -> 1 of 2")
        self.assertEqual(activity_title(status(kind="verification")), "Verifying Photos -> 1")
        self.assertEqual(activity_title(status(kind="repair")), "Replacing copies of Photos -> 1")
        self.assertEqual(activity_title(status(pair_index=0)), "Backing up Photos")
        self.assertEqual(activity_title(None), "Run, verification or repair")

    def test_the_destination_numbered_as_its_job_stands_now(self):
        """A destination moved up while its run works is named by its place in the job now."""
        fifth = status(pair_index=5, pair_total=5, marker_id="m" * 32)
        places = {"j": {"m" * 32: 4}}
        self.assertEqual(activity_title(fifth, places), "Backing up Photos -> 4", "renumbered: no run count")
        self.assertEqual(activity_title(fifth), "Backing up Photos -> 5 of 5", "the job not known: the run's number")
        self.assertEqual(activity_title(fifth, {"j": {}}), "Backing up Photos", "a destination the job no longer has")
        look = tray_look([alive(fifth)], places=places)
        self.assertTrue(look.entries[0].line.startswith("Backing up Photos -> 4: "), look.entries[0].line)

    def test_a_run_of_some_destinations_names_the_destination_alone(self):
        """A run of destinations 2 and 3 of a job of three counts two destinations, which a
        count beside the job's number 3 would read as '3 of 2'."""
        markers = ["a" * 32, "b" * 32, "c" * 32]
        some = status(pair_index=3, pair_total=2, selected=[2, 3], markers=markers)
        self.assertEqual(activity_title(some), "Backing up Photos -> 3")
        every = status(pair_index=3, pair_total=3, selected=[1, 2, 3], markers=markers)
        self.assertEqual(activity_title(every), "Backing up Photos -> 3 of 3")

    def test_how_far_it_has_come_in_the_runs_tabs_words(self):
        copying = status(planned=1000, done=420, eta=720)
        self.assertEqual(progress_words(run_panel(alive(copying)), copying), "42 %, about 12 min left")
        no_eta = status(planned=1000, done=420)
        self.assertEqual(progress_words(run_panel(alive(no_eta)), no_eta), "42 %")
        paused = status(planned=1000, done=420, paused=True)
        self.assertEqual(progress_words(run_panel(alive(paused)), paused), "paused at 42 %")
        listing = status(phase="listing", pair_index=0, listed=1234)
        self.assertEqual(progress_words(run_panel(alive(listing)), listing), "listing the source, 1,234 entries")
        waiting = status(phase="waiting", waiting_for={"run_id": "x" * 32, "job_name": "Work", "kind": "run"})
        self.assertEqual(progress_words(run_panel(alive(waiting)), waiting), "waiting for 'Work'")
        cancelling = status(planned=1000, done=420, cancelling="graceful")
        self.assertEqual(progress_words(run_panel(alive(cancelling)), cancelling),
                         "cancelling: finishing the files in flight")

    def test_an_activity_whose_status_cannot_be_read_is_named_by_its_last_status_and_offers_nothing(self):
        unread = ObservedRun({"format": "mirrorhold-run-status", "run_id": "u" * 32}, True, False, status("u"))
        look = tray_look([unread])
        self.assertEqual(look.entries[0].line, "Backing up Photos -> 1: its status cannot be read now")
        panel = look.entries[0].panel
        self.assertFalse(can_ask_pause(panel) or can_ask_cancel(panel))
        never = ObservedRun({"format": "mirrorhold-run-status", "run_id": "n" * 32}, True, False)
        self.assertEqual(tray_look([never]).entries[0].line,
                         "Run, verification or repair: its status cannot be read now")

    def test_the_menus_entries_are_worded_as_the_rows_buttons(self):
        run, verification, repair = (run_panel(alive(status(kind=k, planned=10, done=1)))
                                     for k in ("run", "verification", "repair"))
        self.assertEqual([cancel_words(p) for p in (run, verification, repair)], ["Cancel...", "Stop...", "Stop..."])
        self.assertEqual(pause_words(run), "Pause")
        self.assertEqual(pause_words(run_panel(alive(status(paused=True)))), "Resume")
        self.assertEqual(cancel_words(run_panel(alive(status(cancelling="graceful")))), "Stop now...")


class LookTest(unittest.TestCase):
    def test_one_activity(self):
        look = tray_look([alive(status(planned=1000, done=420, eta=720))])
        self.assertTrue(look.working)
        self.assertEqual(look.tooltip, "MirrorHold\nBacking up Photos -> 1: 42 %, about 12 min left")
        self.assertAlmostEqual(look.fill, 0.42)
        self.assertFalse(look.paused)

    def test_several_in_line_order_with_one_fill_of_those_measured(self):
        waiting = status("w", job="Music", phase="waiting", place=1, waiting_for={"run_id": "x" * 32, "job_name": "Work", "kind": "run"})
        first = status("bb", planned=100, done=20)
        second = status("ccc", job="Work", kind="verification", phase="verifying",
                        verify={"bytes": 100, "bytes_done": 60, "files": 2, "files_done": 1})
        look = tray_look([alive(waiting), alive(first), alive(second), ObservedRun(status("d"), False)])
        self.assertEqual(look.tooltip.splitlines()[0], "MirrorHold - 3 at work")
        self.assertEqual([e.line.split(":")[0] for e in look.entries],
                         ["Backing up Photos -> 1", "Verifying Work -> 1", "Backing up Music -> 1"])
        self.assertEqual(look.entries[2].line, "Backing up Music -> 1: waiting for 'Work'")
        self.assertAlmostEqual(look.fill, 0.4, msg="the mean of 20 % and 60 %; the waiting one is not measured")

    def test_the_tile_is_grey_only_while_every_activity_at_work_is_paused(self):
        paused = alive(status("a", planned=10, done=5, paused=True))
        working = alive(status("bb", planned=10, done=5))
        waiting = alive(status("ccc", phase="waiting"))
        self.assertTrue(tray_look([paused, waiting]).paused)
        self.assertFalse(tray_look([paused, working]).paused)
        self.assertFalse(tray_look([waiting]).paused)
        self.assertEqual(tray_look([waiting]).fill, 0.0, "a waiting activity shows the copy empty")

    def test_nothing_alive_is_nothing_at_work(self):
        look = tray_look([ObservedRun(status(), False)])
        self.assertFalse(look.working)
        self.assertEqual((look.tooltip, look.fill), ("MirrorHold", 0.0))

    def test_the_tooltip_stays_within_what_windows_shows(self):
        long = "Backing up " + "Photographs of the whole family " * 6 + "-> 1: 42 %"
        one = tooltip([long])
        self.assertLessEqual(len(one), TIP_LIMIT)
        self.assertTrue(one.startswith("MirrorHold\nBacking up Photographs") and one.endswith("..."))
        many = tooltip([f"Backing up Job {i} -> 1: {i} %, about 12 min left" for i in range(9)])
        self.assertLessEqual(len(many), TIP_LIMIT)
        lines = many.splitlines()
        self.assertEqual(lines[0], "MirrorHold - 9 at work")
        self.assertEqual(lines[-1], f"and {9 - (len(lines) - 2)} more")
        self.assertEqual(tooltip([]), "MirrorHold")

    def test_a_standing_reminder_keeps_the_icon_and_is_shown_after_the_activities(self):
        look = tray_look([], [REVIEW])
        self.assertTrue(look.needed)
        self.assertFalse(look.working)
        self.assertEqual(look.tooltip, f"MirrorHold\n{REVIEW}")
        both = tray_look([alive(status(planned=100, done=50))], [REVIEW, "Work: a backup did not finish"])
        self.assertEqual(both.tooltip.splitlines(),
                         ["MirrorHold - 1 at work, 2 to look at", "Backing up Photos -> 1: 50 %", REVIEW,
                          "and 1 more"], "what Windows cannot show is counted, as for activities")
        self.assertEqual(both.reminders, (REVIEW, "Work: a backup did not finish"))
        self.assertEqual(tray_look([], [REVIEW, REVIEW]).tooltip.splitlines()[0], "MirrorHold - 2 to look at")
        self.assertFalse(tray_look([]).needed)
        self.assertEqual((dismiss_words(1), dismiss_words(2)), ("Dismiss reminder", "Dismiss reminders"))

    def test_a_reminder_turns_the_tile_amber_over_the_grey_of_a_pause(self):
        paused = alive(status(planned=10, done=5, paused=True))
        self.assertEqual(tray_view.tile(tray_look([paused])), icon.GREY)
        self.assertEqual(tray_view.tile(tray_look([paused], [REVIEW])), icon.AMBER)
        self.assertEqual(tray_view.tile(tray_look([alive(status())])), icon.TEAL)

    def test_the_tooltip_is_measured_as_windows_measures_it(self):
        emoji = "\U0001F4F7"                              # two UTF-16 code units, one character
        self.assertEqual(units(emoji), 2)
        text = tooltip([f"Backing up {emoji * 70} -> 1: 42 %"])
        self.assertLessEqual(units(text), TIP_LIMIT)
        self.assertTrue(text.endswith(emoji + "..."), "shortened between two characters, never inside one")
        self.assertGreater(units(text), TIP_LIMIT - 4)


class LifetimeTest(unittest.TestCase):
    def test_it_ends_a_few_seconds_after_the_last_activity(self):
        life = Lifetime()
        self.assertFalse(life.on_look(True, 100.0))
        self.assertFalse(life.on_look(False, 101.0))
        self.assertFalse(life.on_look(False, 101.0 + LINGER_SECONDS - 0.1))
        self.assertFalse(life.on_look(True, 106.0), "an activity starting while it lingers keeps it")
        self.assertFalse(life.on_look(False, 107.0))
        self.assertTrue(life.on_look(False, 107.0 + LINGER_SECONDS))

    def test_looks_failing_for_long_enough_end_it_and_a_good_look_starts_the_count_again(self):
        life = Lifetime()
        self.assertFalse(life.on_failure(100.0))
        self.assertFalse(life.on_failure(100.0 + FAILING_SECONDS - 1))
        self.assertFalse(life.on_look(True, 100.0 + FAILING_SECONDS - 0.5))
        self.assertFalse(life.on_failure(100.0 + FAILING_SECONDS))
        self.assertTrue(life.on_failure(100.0 + 2 * FAILING_SECONDS))

    def test_the_icon_is_made_again_after_two_refusals_running(self):
        watch = MissingWatch()
        self.assertEqual([watch.on_check(a) for a in (False, None, True, False, False, False)],
                         [False, False, False, False, True, True])

    def test_the_handle_windows_knows_the_icon_by(self):
        self.assertEqual((icon_id("ico#2"), icon_id("ico#"), icon_id("img#2")), (2, None, None))

    def test_the_mark_a_main_window_carries_names_its_store(self):
        here, there = Path("C:/a/Config"), Path("C:/b/Config")
        self.assertEqual(window_key(here), window_key(Path("c:/A/config")))
        self.assertNotEqual(window_key(here), window_key(there))
        self.assertGreater(window_key(here), 0)


class PictureTest(unittest.TestCase):
    def white(self, pixels):
        return sum(1 for p in pixels if p[:3] == icon.WHITE and p[3] == 255)

    def test_the_copy_fills_from_the_bottom_and_the_tile_greys(self):
        for size in (16, 24, 32):
            plain = icon.render(size)
            self.assertEqual(icon.render(size, fill=0.0), plain, "no fill is the program's icon")
            counts = [self.white(icon.render(size, fill=f)) for f in (0.0, 0.25, 0.5, 1.0)]
            self.assertEqual(counts, sorted(counts))
            self.assertGreater(counts[-1], counts[0])
            half = icon.render(size, fill=0.5)
            changed = [i // size for i, (a, b) in enumerate(zip(plain, half)) if a != b]
            self.assertGreater(min(changed), size // 3, "only the lower part of the copy is filled")
            grey = icon.render(size, tile=icon.GREY)
            self.assertIn(icon.GREY + (255,), grey)
            self.assertNotIn(icon.TEAL + (255,), grey)
            amber = icon.render(size, tile=icon.AMBER)
            self.assertIn(icon.AMBER + (255,), amber)
            self.assertEqual(self.white(amber), self.white(plain), "the blocks stay as they are on an amber tile")


class OfferTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.layout = runtime_layout(self.store_path)
        self.started = []
        patcher = mock.patch.dict(os.environ, {SWITCH_VARIABLE: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, command, root):
        self.started.append((list(command), root))

    def test_it_starts_the_icons_process_for_the_store(self):
        self.assertTrue(offer(self.layout, self.store_path, interpreter=sys.executable, start=self.start))
        (command, root), = self.started
        self.assertEqual(command[1:], ["-m", TRAY_MODULE, "--store", str(self.store_path)])
        self.assertEqual(Path(command[0]).name.lower(), "pythonw.exe")
        self.assertEqual(root, support.PROJECT)

    def test_the_store_is_named_by_an_absolute_path(self):
        command = tray_command(Path("Config") / "mirrorhold.json", interpreter=sys.executable)
        self.assertTrue(Path(command[-1]).is_absolute())

    def test_none_is_started_while_another_process_holds_the_icon_lock(self):
        lock = FileLock(self.layout.tray_lock)
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        self.assertFalse(offer(self.layout, self.store_path, start=self.start))
        lock.release()
        self.assertTrue(offer(self.layout, self.store_path, start=self.start))
        self.assertEqual(len(self.started), 1)

    def test_with_the_switch_off_none_is_started(self):
        with mock.patch.dict(os.environ, {SWITCH_VARIABLE: "off"}):
            self.assertTrue(switched_off())
            self.assertFalse(offer(self.layout, self.store_path, start=self.start))
        self.assertEqual(self.started, [])

    def test_a_start_that_fails_costs_the_icon_alone(self):
        def refused(command, root):
            raise OSError("refused")
        self.assertFalse(offer(self.layout, self.store_path, start=refused))


class HandBackTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        self.lock = FileLock(self.layout.tray_lock)
        self.assertTrue(self.lock.try_acquire())
        self.addCleanup(self.lock.release)

    def alive(self):
        return any(r.alive for r in observe_runs(self.layout))

    def test_with_no_activity_left_the_lock_is_let_go(self):
        self.assertFalse(hand_back(self.lock, self.alive))
        self.assertFalse(self.lock.held)

    def test_an_activity_published_before_the_look_keeps_the_icon(self):
        board = live_activity(self.layout, "j")
        self.addCleanup(board.close)
        self.assertTrue(hand_back(self.lock, self.alive))
        self.assertTrue(self.lock.held)

    def test_a_process_that_took_the_lock_meanwhile_has_the_icon(self):
        board = live_activity(self.layout, "j")
        self.addCleanup(board.close)
        other = FileLock(self.layout.tray_lock)
        self.addCleanup(other.release)

        def look_again():
            self.assertTrue(other.try_acquire(), "the lock was let go before the look")
            return self.alive()

        self.assertFalse(hand_back(self.lock, look_again))
        self.assertFalse(self.lock.held)

    def test_a_reminder_written_before_the_look_keeps_the_icon(self):
        reminders.leave(self.layout, JOB_A, Notice(REVIEW, (), asks=True))
        icon_process = SimpleNamespace(layout=self.layout)
        with mock.patch.object(tray_view, "observe_runs", side_effect=OSError("cannot read")):
            self.assertTrue(hand_back(self.lock, lambda: tray_view.TrayIcon._still_needed(icon_process)),
                            "statuses that cannot be read do not take the icon from a reminder")
        self.assertTrue(self.lock.held)

    def test_a_look_that_fails_finds_none_and_the_lock_is_let_go(self):
        board = live_activity(self.layout, "j")
        self.addCleanup(board.close)

        def failing():
            raise OSError("cannot read")
        self.assertFalse(hand_back(self.lock, failing))
        self.assertFalse(self.lock.held)


class FailedLookTest(TempDirTest):
    """A look whose statuses cannot be read shows the standing reminders alone, and counts as a
    failed look only where none stands: a process a reminder keeps always shows it."""

    def setUp(self):
        super().setUp()
        self.layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        self.ended, self.shown = [], []
        self.icon = SimpleNamespace(_looks=queue.Queue(), lifetime=Lifetime(), layout=self.layout, look=TrayLook(),
                                    statuses={"a" * 32: {}}, unavailable=False)
        self.icon._end = lambda: self.ended.append(True) or True
        self.icon._show = lambda: self.shown.append(self.icon.look)

    def failed_look_at(self, now):
        self.icon._looks.put(RunsLook(failed="cannot read"))
        with mock.patch.object(tray_view.time, "monotonic", return_value=now):
            return tray_view.TrayIcon._take_look(self.icon)

    def test_a_standing_reminder_is_shown_however_long_the_statuses_cannot_be_read(self):
        reminders.leave(self.layout, JOB_A, Notice(REVIEW, (), asks=True))
        for step in range(3):
            self.assertFalse(self.failed_look_at(100.0 + step * FAILING_SECONDS))
        self.assertEqual(self.ended, [])
        self.assertEqual([look.reminders for look in self.shown], [(REVIEW,)] * 3)
        self.assertEqual(self.icon.statuses, {}, "no request is asked of a status the look could not read")

    def test_with_no_reminder_failing_looks_end_the_process(self):
        self.assertFalse(self.failed_look_at(100.0))
        self.assertTrue(self.failed_look_at(100.0 + FAILING_SECONDS))
        self.assertEqual((self.ended, self.shown), ([True], []))


class WindowClearsRemindersTest(TempDirTest):
    """The main window clears the store's reminders with each look at the runs, only while it
    has the focus."""

    def test_only_while_the_window_has_the_focus(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        reminders.leave(layout, JOB_A, Notice(REVIEW, (), asks=True))

        def raising(error):
            def focus_get():
                raise error
            return focus_get
        for focus_get in (lambda: None, raising(KeyError("#menu")), raising(tk.TclError("bad window"))):
            window = SimpleNamespace(root=SimpleNamespace(focus_get=focus_get), session=SimpleNamespace(layout=layout))
            MainWindow._attend(window)
            self.assertEqual(len(reminders.standing(layout)), 1, "not cleared while the window is in the background")
        window = SimpleNamespace(root=SimpleNamespace(focus_get=lambda: object()), session=SimpleNamespace(layout=layout))
        MainWindow._attend(window)
        self.assertEqual(reminders.standing(layout), [])


class ProcessTest(TempDirTest):
    def test_the_process_ends_at_once_while_another_holds_the_icon_lock(self):
        store_path = self.tmp / "Config" / "mirrorhold.json"
        lock = FileLock(runtime_layout(store_path).tray_lock)
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        self.assertEqual(tray_view.main(["--store", str(store_path)]), 0)

    def test_the_icons_module_is_started_as_its_own_main(self):
        done = subprocess.run([sys.executable, "-m", TRAY_MODULE, "--help"], cwd=support.PROJECT,
                              capture_output=True, text=True, timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--store", done.stdout)


# --- every kind of activity brings the icon up once its status is published -----------------

class Recorder:
    def __init__(self, test, layout):
        self.test, self.layout, self.calls = test, layout, []

    def __call__(self, layout, store_path):
        published = [r for r in observe_runs(layout) if r.alive]
        self.calls.append((layout, Path(store_path), len(published)))
        return True


class RunStartTest(RunCase):
    def test_a_run_brings_the_icon_up_once_its_status_is_published(self):
        write(self.src, "a.txt", "alpha")
        recorder = Recorder(self, self.layout)
        with mock.patch("mirrorhold.run.runner.offer_tray", recorder):
            record = self.only(self.run_now(first_backup=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(recorder.calls, [(self.layout, self.store.path, 1)])

    def test_a_run_whose_icon_cannot_be_started_runs_as_without_it(self):
        write(self.src, "a.txt", "alpha")
        with mock.patch.dict(os.environ, {SWITCH_VARIABLE: ""}), \
                mock.patch("mirrorhold.tray.start_detached", side_effect=OSError("refused")):
            record = self.only(self.run_now(first_backup=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)


class VerificationStartTest(VerifyCase):
    def test_a_verification_brings_the_icon_up_once_its_status_is_published(self):
        self.mirror({"a.txt": "alpha"})
        recorder = Recorder(self, self.layout)
        with mock.patch("mirrorhold.maintenance.verifier.offer_tray", recorder):
            self.verify()
        self.assertEqual(recorder.calls, [(self.layout, self.store.path, 1)])


class RepairStartTest(RepairCase):
    def test_a_repair_brings_the_icon_up_once_its_status_is_published(self):
        self.mirror({"a.txt": "alpha", "photo.raw": b"\x00" * 4096})
        damage(self.dst / "photo.raw", 2048)
        self.assertEqual(self.verify().differ_total, 1)
        recorder = Recorder(self, self.layout)
        with mock.patch("mirrorhold.maintenance.repairer.offer_tray", recorder):
            self.repair()
        self.assertEqual(recorder.calls, [(self.layout, self.store.path, 1)])


# --- reminders: what a finished activity left for the user to look at -------------------------

class ReminderStoreTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")

    def test_one_per_job_the_latest_replacing_the_one_before(self):
        self.assertEqual(reminders.standing(self.layout), [])
        reminders.leave(self.layout, JOB_A, Notice("Photos: a backup did not finish", ("Destination 1: failed",), True))
        reminders.leave(self.layout, JOB_B, Notice(REVIEW, ("Destination 1: removals held",), True))
        reminders.leave(self.layout, JOB_A, Notice("Photos: a backup needs your review", (), True))
        found = reminders.standing(self.layout)
        self.assertEqual([(r.job_id, r.title) for r in found],
                         [(JOB_B, REVIEW), (JOB_A, "Photos: a backup needs your review")])
        self.assertEqual(found[0].lines, ("Destination 1: removals held",))

    def test_a_reminder_that_cannot_be_read_is_shown_in_general_words_and_a_staging_file_is_not_one(self):
        folder = self.layout.reminders_dir
        folder.mkdir(parents=True)
        (folder / f"{JOB_A}.json").write_text("{not json", encoding="utf-8")
        (folder / f"{JOB_B}.json.0123456789ab.writing").write_text("{}", encoding="utf-8")
        self.assertEqual([(r.job_id, r.title) for r in reminders.standing(self.layout)],
                         [(JOB_A, reminders.UNREADABLE_TITLE)])

    def test_a_reminder_another_program_keeps_refused_is_shown_without_waiting(self):
        reminders.leave(self.layout, JOB_A, Notice(REVIEW, (), True))
        with mock.patch("mirrorhold.exchange.open_shared", side_effect=PermissionError(13, "in use")) as refused:
            found = reminders.standing(self.layout, patience=0)
        self.assertEqual([r.title for r in found], [reminders.UNREADABLE_TITLE])
        self.assertEqual(refused.call_count, 1, "refused once, never waited for")

    def test_clearing_removes_every_one_and_a_dead_writers_staging_file(self):
        self.assertFalse(reminders.clear(self.layout), "nothing to clear, and no folder")
        for job in (JOB_A, JOB_B):
            reminders.leave(self.layout, job, Notice(REVIEW, (), True))
        left = self.layout.reminders_dir / f"{JOB_A}.json.0123456789ab.writing"
        left.write_text("{}", encoding="utf-8")
        self.assertTrue(reminders.clear(self.layout))
        self.assertEqual(reminders.standing(self.layout), [])
        self.assertTrue(left.exists(), "a staging file may be a write in progress")
        self.assertFalse(reminders.clear(self.layout, now=left.stat().st_mtime + reminders.STALE_STAGING_SECONDS))
        self.assertFalse(left.exists())

    def test_anything_but_a_jobs_id_is_refused(self):
        for job_id in ("", "..", "a\\b", "../b", "C:x", "CON", JOB_A + ".", JOB_A.upper(), JOB_A[:-1]):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                reminders.leave(self.layout, job_id, Notice(REVIEW, (), True))
        self.assertFalse(self.layout.reminders_dir.exists())

    def test_a_reminder_is_the_job_its_file_is_named_for_whatever_it_holds(self):
        reminders.leave(self.layout, JOB_A, Notice(REVIEW, (), True))
        (self.layout.reminders_dir / f"{JOB_A}.json").replace(self.layout.reminders_dir / f"{JOB_B}.json")
        self.assertEqual([(r.job_id, r.title) for r in reminders.standing(self.layout)], [(JOB_B, REVIEW)])


class ReminderLeftByNoticeTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.layout = runtime_layout(self.store_path)
        self.brought_up = []

    def bring_up(self, layout, store_path):
        self.brought_up.append(([r.title for r in reminders.standing(layout)], Path(store_path)))

    def remind(self, job_id=JOB_A):
        return reminders.reminding(self.store_path, job_id, bring_up=self.bring_up)

    def test_a_notice_that_asks_is_left_before_the_icon_is_brought_up(self):
        self.remind()(Notice(REVIEW, ("Destination 1: removals held",), asks=True))
        self.assertEqual(self.brought_up, [([REVIEW], self.store_path)])

    def test_a_notice_that_only_tells_leaves_nothing(self):
        self.remind()(Notice("Recipes: backed up", ("Destination 1: completed",)))
        self.assertEqual((reminders.standing(self.layout), self.brought_up), ([], []))

    def test_a_reminder_that_cannot_be_left_costs_the_reminder_alone(self):
        with mock.patch("mirrorhold.reminders.leave", side_effect=OSError("refused")):
            self.remind()(Notice(REVIEW, (), asks=True))
        self.remind("..")(Notice(REVIEW, (), asks=True))
        self.assertEqual(self.brought_up, [])

    def test_a_notice_windows_refused_still_leaves_its_reminder(self):
        problem = show(Notice(REVIEW, (), asks=True), Refusing(), remind=self.remind())
        self.assertTrue(problem.startswith("the notification could not be shown"), problem)
        self.assertEqual([r.title for r in reminders.standing(self.layout)], [REVIEW])

    def test_which_notices_ask(self):
        job = a_job()
        asks = {outcome: notice_for([a_record(job, 1, outcome)], Notifications()).asks
                for outcome in (Outcome.COMPLETED, Outcome.HELD, Outcome.FAILED, Outcome.INTERRUPTED)}
        self.assertEqual(asks, {Outcome.COMPLETED: False, Outcome.HELD: True, Outcome.FAILED: True,
                                Outcome.INTERRUPTED: True})
        self.assertTrue(failure_notice("Photos", "why", Notifications()).asks)
        mixed = notice_for([a_record(job, 1, Outcome.COMPLETED), a_record(job, 2, Outcome.HELD)], Notifications())
        self.assertTrue(mixed.asks, "a notice asks when any destination it announces asks")
        quiet = notice_for([a_record(job, 1, Outcome.COMPLETED), a_record(job, 2, Outcome.HELD)],
                           Notifications(on_hold=False))
        self.assertFalse(quiet.asks, "a held destination the user chose not to hear of asks nothing")


class AutomaticReminderTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "state" / "store.json")
        self.layout = runtime_layout(self.store.path)
        self.job = a_job(triggers=Triggers(False, "daily 21:00"))
        self.store.save(StoreContents((self.job,)))
        self.offered = []
        patcher = mock.patch("mirrorhold.reminders.offer", lambda layout, path: self.offered.append(Path(path)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def scheduled(self, runner, notifier):
        return run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                             notifier=notifier, runner=runner)

    def titles(self):
        return [(r.job_id, r.title) for r in reminders.standing(self.layout)]

    def test_a_scheduled_run_held_for_review_leaves_its_reminder_and_brings_the_icon_up(self):
        self.scheduled(FakeRunner(outcome=Outcome.HELD), Shown())
        self.assertEqual(self.titles(), [(self.job.job_id, "Photos: a backup needs your review")])
        self.assertEqual(self.offered, [self.store.path])

    def test_a_completed_run_leaves_none_and_one_that_could_not_start_leaves_one(self):
        self.scheduled(FakeRunner(), Shown())
        self.assertEqual((self.titles(), self.offered), ([], []))
        self.scheduled(FakeRunner(error=RunError("the engine is missing")), Shown())
        self.assertEqual(self.titles(), [(self.job.job_id, "Photos: an automatic backup could not start")])

    def test_a_process_that_raises_no_notice_leaves_no_reminder(self):
        self.scheduled(FakeRunner(outcome=Outcome.HELD), None)
        self.assertEqual(self.titles(), [])


class CommandLineReminderTest(TempDirTest):
    """A run from the command line — every run the window starts is one — that stops at a
    destination that is a file leaves its job's reminder."""

    def setUp(self):
        super().setUp()
        vol_d, vol_e = self.tmp / "volD", self.tmp / "volE"
        (vol_d / "Work").mkdir(parents=True)
        vol_e.mkdir()
        self.d = volume(1, mounts=(str(vol_d) + "\\",), label="D")
        self.e = volume(2, mounts=(str(vol_e) + "\\",), label="E")
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.job = new_job("Work", endpoint(self.d, "Work"), [endpoint(self.e, "Backup")])
        ConfigStore(self.store_path).save(StoreContents((self.job,)))
        (vol_e / "Backup").write_text("a file where the backup's folder would be", encoding="utf-8")

    def test_a_run_that_did_not_finish_leaves_its_reminder(self):
        with mock.patch("mirrorhold.reminders.offer") as offered:
            cli_main(["--store", str(self.store_path), "run", "Work", "--unattended"],
                     volume_source=FixedVolumeSource((self.d, self.e)), sync_roots=(), out=io.StringIO(),
                     err=io.StringIO(), notifier=Shown())
        self.assertEqual([(r.job_id, r.title) for r in reminders.standing(runtime_layout(self.store_path))],
                         [(self.job.job_id, "Work: a backup did not finish")])
        offered.assert_called_once()


if __name__ == "__main__":
    unittest.main()
