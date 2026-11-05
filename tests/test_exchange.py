"""The files the core's processes share, under contention: the exchange through which each
is written and read, the liveness lock looked at while its owner removes it, a run's
status and control files, a run found by its lock while its status is not there, the job
file — each against a second, real process where the claim is about two processes, never
a mock of the file system. The suite's own sweep of the folders earlier runs left meets
the same refusals, and is tested beside the lock.

A file "another program holds" is held here without sharing (``held``), as a program
that does not share deletion holds it: an antivirus scanning it, a backup reading it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest

import support
from support import (PROJECT, TEMPORARY_PREFIX, TempDirTest, endpoint, held, job_file_saver, let_go_after,
                     live_activity, other_process, result_of, volume)

from mirrorhold import exchange
from mirrorhold.exchange import FileBusy
from mirrorhold.jobs.model import new_job
from mirrorhold.jobs.store import ConfigStore, LoadStatus, StoreBusy, StoreConflict, StoreContents
from mirrorhold.locations import RuntimeLayout
from mirrorhold.records.admission import Activity, Claim, WaitedFor, admit, blockers, live_activities
from mirrorhold.records.lock import FileLock, is_held_elsewhere
from mirrorhold.records.record import Outcome, read_records
from mirrorhold.records.status import (MISSING_PATIENCE, CancelMode, LastRead, LiveStatus, NoSuchRun, ObservedRun,
                                       StatusBoard, Wanted, find_run, observe_runs, reap_interrupted, request)
from mirrorhold.run.control import RunControl, StatusPump

WINDOWS = sys.platform == "win32"
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SECONDS = 3.0


def status(run_id: str = "a" * 32) -> LiveStatus:
    return LiveStatus(run_id, os.getpid(), "j" * 32, "Job", "manual", "2026-10-14T10:00:00Z", pair_index=1,
                      marker_id="m" * 32, pair_started="2026-10-14T10:00:01Z")


def read_again(path, patience: float) -> bool:
    """Whether ``path`` is read within ``patience`` seconds of a read that found it missing."""
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        try:
            exchange.read_bytes(path)
            return True
        except FileNotFoundError:
            time.sleep(0.001)
    return False


@unittest.skipUnless(WINDOWS, "Windows' refusals")
class ExchangeTest(TempDirTest):
    def test_a_reader_that_shares_deletion_never_stands_in_a_writers_way(self):
        path = self.tmp / "file.json"
        exchange.write_text(path, "old")
        with exchange.open_shared(path) as reading:
            exchange.write_text(path, "new", patience=0.0)
            self.assertEqual(reading.read(), b"old")
        self.assertEqual(exchange.read_text(path), "new")

    def test_a_file_another_program_holds_is_written_once_it_lets_go(self):
        path = self.tmp / "file.json"
        exchange.write_text(path, "old")
        let_go_after(0.4, held(path))
        exchange.write_text(path, "new", patience=5.0)
        self.assertEqual(exchange.read_text(path), "new")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["file.json"])

    def test_a_file_held_past_the_patience_is_busy_and_left_as_it_was(self):
        path = self.tmp / "file.json"
        exchange.write_text(path, "old")
        with held(path):
            with self.assertRaises(FileBusy):
                exchange.write_text(path, "new", patience=0.3)
            with self.assertRaises(FileBusy):
                exchange.read_bytes(path, patience=0.3)
            self.assertFalse(exchange.remove(path, patience=0.3))
        self.assertEqual(exchange.read_text(path), "old")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["file.json"])

    def test_a_missing_file_is_not_a_refusal(self):
        started = time.monotonic()
        with self.assertRaises(FileNotFoundError):
            exchange.read_bytes(self.tmp / "none.json", patience=5.0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(exchange.remove(self.tmp / "none.json"))

    def test_a_file_renamed_in_while_it_is_looked_for_again_is_read(self):
        path = self.tmp / "file.json"
        writer = threading.Timer(0.05, lambda: exchange.write_text(path, "new"))
        writer.start()
        self.addCleanup(writer.join)
        self.assertEqual(exchange.read_text(path, missing=2.0), "new")

    def test_a_file_truly_not_there_is_told_so_once_the_missing_patience_is_spent(self):
        started = time.monotonic()
        with self.assertRaises(FileNotFoundError):
            exchange.read_bytes(self.tmp / "none.json", missing=0.1)
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_two_processes_replacing_and_reading_one_file_never_fail_each_other(self):
        # The writer is never refused. Under load a read is refused for a moment, and finds
        # no file for the moment a rename takes the old file's name away (the exchange's
        # orientation): the read's patience bears the first, and the second ends.
        path = self.tmp / "file.json"
        exchange.write_text(path, json.dumps({"n": 0}))
        writer = other_process(self, f"""
            import json, time
            from mirrorhold import exchange
            from mirrorhold.exchange import FileBusy
            path, failures, n = {str(path)!r}, 0, 0
            stop = time.monotonic() + {SECONDS}
            while time.monotonic() < stop:
                n += 1
                try:
                    exchange.write_text(path, json.dumps({{"n": n, "pad": "x" * 4000}}), patience=0.0)
                except FileBusy:
                    failures += 1
            print(json.dumps({{"writes": n, "failures": failures}}))
        """)
        reads, failures = 0, []
        stop = time.monotonic() + SECONDS
        while time.monotonic() < stop:
            reads += 1
            try:
                json.loads(exchange.read_text(path))
            except FileBusy as exc:
                failures.append(str(exc))
            except FileNotFoundError:
                if not read_again(path, exchange.READ_PATIENCE):
                    failures.append("the file stayed missing")
        written = result_of(self, writer)
        self.assertGreater((reads, written["writes"]), (100, 100))
        self.assertEqual((failures, written["failures"]), ([], 0))


@unittest.skipUnless(WINDOWS, "Windows' refusals")
class LockLookTest(TempDirTest):
    def test_looking_at_a_lock_its_owner_removes_never_raises_and_never_makes_it_again(self):
        path = self.tmp / "run.lock"
        stop, answers, errors = threading.Event(), set(), []

        def owner():
            while not stop.is_set():
                lock = FileLock(path)
                if lock.try_acquire():
                    time.sleep(0.001)
                    lock.release(remove=True)

        def look():
            while not stop.is_set():
                try:
                    answers.add(is_held_elsewhere(path))
                except OSError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=owner, daemon=True), threading.Thread(target=look, daemon=True)]
        for thread in threads:
            thread.start()
        time.sleep(SECONDS)
        stop.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertIn(True, answers)
        self.assertFalse(path.exists(), "a look made the lock file again after its owner removed it")
        self.assertIsNone(is_held_elsewhere(path))
        self.assertFalse(path.exists())


@unittest.skipUnless(WINDOWS, "Windows' refusals")
class SuiteSweepTest(TempDirTest):
    def test_a_folder_whose_lock_file_is_refused_is_left_and_the_others_are_still_swept(self):
        refused, left = TEMPORARY_PREFIX + "refused", TEMPORARY_PREFIX + "left"
        for name in (refused, left):
            (self.tmp / name).mkdir()
            (self.tmp / name / "a.txt").write_text("a", encoding="utf-8")
            (self.tmp / (name + ".lock")).write_bytes(b"")
        with held(self.tmp / (refused + ".lock")):
            support._sweep(self.tmp)
        self.assertTrue((self.tmp / refused / "a.txt").is_file())
        self.assertEqual([p.exists() for p in (self.tmp / left, self.tmp / (left + ".lock"))], [False, False])


@unittest.skipUnless(WINDOWS, "Windows' refusals")
class StatusChannelTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.layout = RuntimeLayout(self.tmp)

    def board(self, run_id="a" * 32) -> StatusBoard:
        board = StatusBoard(self.layout, status(run_id))
        board.open()
        self.addCleanup(board.close)
        return board

    def test_a_status_published_by_another_process_is_seen_alive_at_every_look(self):
        done = self.tmp / "done"          # the publisher closes once every look has returned
        publisher = other_process(self, f"""
            import ctypes, json, os
            from ctypes import wintypes
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            board = StatusBoard(RuntimeLayout(Path({str(self.tmp)!r})),
                                LiveStatus("b" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-14T10:00:00Z"))
            board.open()
            print("open", flush=True)
            k32 = ctypes.WinDLL("kernel32")
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            test = k32.OpenProcess(0x00100000, False, {os.getpid()})          # SYNCHRONIZE
            failures, n = 0, 0
            done = Path({str(done)!r})
            while not done.exists() and k32.WaitForSingleObject(test, 0) != 0:   # 0: the test's process ended
                n += 1
                board.status.listed = n
                try:
                    board.publish()
                except OSError:
                    failures += 1
            board.close()
            print(json.dumps({{"publications": n, "failures": failures}}))
        """)
        self.assertEqual(publisher.stdout.readline().strip(), "open")
        looks, seen = 0, 0
        stop = time.monotonic() + SECONDS - 0.5
        while time.monotonic() < stop:
            looks += 1
            runs = observe_runs(self.layout)
            seen += [(r.run_id, r.alive, r.readable) for r in runs] == [("b" * 32, True, True)]
        done.write_bytes(b"")
        published = result_of(self, publisher)
        self.assertGreater(looks, 20)
        self.assertEqual((seen, published["failures"]), (looks, 0))

    def test_a_status_another_program_holds_is_alive_and_unreadable_and_conflicts_with_everything(self):
        board = self.board()
        with held(self.layout.status_dir / f"{board.status.run_id}.json"):
            runs = observe_runs(self.layout)
            self.assertEqual([(r.run_id, r.alive, r.readable) for r in runs], [("a" * 32, True, False)])
            me = Activity.from_status(LiveStatus("c" * 32, 1, "k" * 32, "Other", "manual", "2026-10-14T10:00:00Z",
                                                 claim={"reads": [], "writes": []}).to_document())
            self.assertEqual([a.run_id for a in blockers(me, live_activities(self.layout))], ["a" * 32])
            with self.assertRaises(NoSuchRun):
                find_run(self.layout, None)
        self.assertEqual([(r.alive, r.readable) for r in observe_runs(self.layout)], [(True, True)])

    def test_an_observer_names_a_run_whose_status_it_cannot_read_by_the_last_status_it_read(self):
        board = self.board()
        last_read = LastRead()
        self.assertEqual([r.readable for r in observe_runs(self.layout, last_read)], [True])
        with held(self.layout.status_dir / f"{board.status.run_id}.json"):
            remembered = observe_runs(self.layout, last_read)
            once = observe_runs(self.layout)
        self.assertEqual([(r.readable, (r.last or {}).get("job_name")) for r in remembered], [(False, "Job")])
        self.assertEqual([(r.readable, r.last) for r in once], [(False, None)], "a look kept by no observer")

    def test_a_waiting_activity_names_one_whose_status_it_cannot_read_by_the_last_status_it_read(self):
        other = live_activity(self.layout, "j" * 32, job_name="Photos")
        self.addCleanup(other.close)

        def waiting(run_id: str, job_id: str) -> StatusBoard:
            board = StatusBoard(self.layout, LiveStatus(run_id, os.getpid(), job_id, "Mine", "manual",
                                                        "2026-10-21T10:00:00Z"))
            board.open()
            self.addCleanup(board.close)
            return board

        def waits_for(board: StatusBoard) -> tuple:
            other_one = WaitedFor.from_status(board.status.to_document())
            return other_one.run_id, other_one.kind, other_one.job_name, other_one.readable

        seen = waiting("c" * 32, "j" * 32)
        self.assertFalse(admit(seen, Claim()).admitted, "two runs of one job conflict")
        self.assertEqual(waits_for(seen), (other.status.run_id, "run", "Photos", True))
        unseen = waiting("d" * 32, "k" * 32)
        with held(self.layout.status_dir / f"{other.status.run_id}.json"):
            self.assertFalse(admit(seen, Claim()).admitted)
            self.assertFalse(admit(unseen, Claim()).admitted, "one whose claim cannot be read conflicts with all")
        self.assertEqual(waits_for(seen), (other.status.run_id, "run", "Photos", False))
        self.assertEqual(waits_for(unseen), (other.status.run_id, "", "", False))

    def test_a_run_whose_status_cannot_be_read_is_reached_by_its_identifier(self):
        board = self.board()
        with held(self.layout.status_dir / f"{board.status.run_id}.json"):
            self.assertEqual(find_run(self.layout, "aaaa").run_id, board.status.run_id)
            with self.assertRaises(NoSuchRun) as raised:
                find_run(self.layout, None)
        self.assertIn("the status of aaaaaaaaaaaa cannot be read now: name it to reach it", str(raised.exception))

    def test_a_control_file_another_program_holds_leaves_what_was_wanted(self):
        board = self.board()
        control = RunControl(board.status)
        pump = StatusPump(board, control)
        request(self.layout, board.status.run_id, cancel=CancelMode.GRACEFUL)
        pump.tick()
        self.assertEqual(control.wanted, Wanted(False, CancelMode.GRACEFUL))
        with held(self.layout.status_dir / f"{board.status.run_id}.control"):
            self.assertIsNone(board.wanted())
            pump.tick()
            self.assertEqual(control.wanted, Wanted(False, CancelMode.GRACEFUL))

    def test_a_control_file_not_there_for_a_moment_leaves_what_was_wanted(self):
        board = self.board()
        control = RunControl(board.status)
        pump = StatusPump(board, control)
        pump.tick()
        self.assertEqual(control.wanted, Wanted(), "before the first request")
        request(self.layout, board.status.run_id, pause=True, cancel=CancelMode.GRACEFUL)
        pump.tick()
        self.assertEqual(control.wanted, Wanted(True, CancelMode.GRACEFUL))
        path = self.layout.status_dir / f"{board.status.run_id}.control"
        os.replace(path, path.with_name(path.name + ".taken"))   # as a request's rename takes the old file's name
        pump.tick()
        self.assertEqual(control.wanted, Wanted(True, CancelMode.GRACEFUL), "a pause and a cancel never withdrawn")

    def test_a_request_the_control_file_does_not_take_is_reported_and_one_it_takes_lands(self):
        board = self.board()
        request(self.layout, board.status.run_id, pause=True)
        with held(self.layout.status_dir / f"{board.status.run_id}.control"):
            with self.assertRaises(FileBusy):
                request(self.layout, board.status.run_id, cancel=CancelMode.IMMEDIATE, patience=0.3)
        self.assertEqual(board.wanted(), Wanted(True, None))
        let_go_after(0.4, held(self.layout.status_dir / f"{board.status.run_id}.control"))
        self.assertEqual(request(self.layout, board.status.run_id, cancel=CancelMode.IMMEDIATE, patience=5.0),
                         Wanted(True, CancelMode.IMMEDIATE))
        self.assertEqual(board.wanted(), Wanted(True, CancelMode.IMMEDIATE))
        self.assertEqual(sorted(p.name for p in self.layout.status_dir.iterdir() if "writing" in p.name), [])

    def test_requests_to_one_run_take_turns_so_neither_drops_the_others_change(self):
        board = StatusBoard(self.layout, status())
        board.open()
        run_id = board.status.run_id
        turn = FileLock(self.layout.status_dir / f"{run_id}.request")
        self.assertTrue(turn.try_acquire(), "another request at work")
        with self.assertRaises(FileBusy):
            request(self.layout, run_id, cancel=CancelMode.IMMEDIATE, patience=0.3)
        landed = []
        pausing = threading.Thread(target=lambda: landed.append(request(self.layout, run_id, pause=True)))
        pausing.start()
        time.sleep(0.3)
        self.assertEqual((landed, board.wanted()), ([], None), "the pause waits its turn, reading nothing yet")
        control = self.layout.status_dir / f"{run_id}.control"
        exchange.write_text(control, json.dumps(Wanted(False, CancelMode.GRACEFUL).to_document()))
        self.assertEqual([r.run_id for r in observe_runs(self.layout)], [run_id], "a request's turn is no run")
        turn.release()
        pausing.join(10)
        self.assertEqual(landed, [Wanted(True, CancelMode.GRACEFUL)], "the cancel made meanwhile is kept")
        self.assertEqual(board.wanted(), Wanted(True, CancelMode.GRACEFUL))
        self.assertFalse((self.layout.status_dir / f"{run_id}.request").exists(), "a turn's file goes as it is let go")
        board.close()
        self.assertEqual(list(self.layout.status_dir.iterdir()), [])

    def test_a_request_that_has_its_turn_once_its_run_has_ended_writes_nothing(self):
        board = StatusBoard(self.layout, status())
        board.open()
        run_id = board.status.run_id
        board.close()
        with self.assertRaises(NoSuchRun):
            request(self.layout, run_id, pause=True)
        self.assertEqual(list(self.layout.status_dir.iterdir()), [], "no control file of a run that ended")

    def test_an_ending_run_waits_for_a_request_at_work_and_removes_what_it_wrote(self):
        board = StatusBoard(self.layout, status())
        board.open()
        run_id = board.status.run_id
        turn = FileLock(self.layout.status_dir / f"{run_id}.request")
        self.assertTrue(turn.try_acquire(), "a request at work")
        closing = threading.Thread(target=board.close)
        closing.start()
        time.sleep(0.3)
        self.assertTrue(closing.is_alive(), "the run waits for the request's turn")
        self.assertEqual([(r.run_id, r.alive, r.readable) for r in observe_runs(self.layout)], [(run_id, True, True)],
                         "still alive, its status there: never a live run whose status cannot be read")
        exchange.write_text(self.layout.status_dir / f"{run_id}.control",
                            json.dumps(Wanted(True, CancelMode.GRACEFUL).to_document()))   # what the request writes
        turn.release()
        closing.join(10)
        self.assertFalse(closing.is_alive())
        self.assertEqual(list(self.layout.status_dir.iterdir()), [])

    def test_an_ending_run_waits_for_a_reader_to_let_go_of_its_status(self):
        board = StatusBoard(self.layout, status())
        board.open()
        let_go_after(0.5, held(self.layout.status_dir / f"{board.status.run_id}.json"))
        board.close()
        self.assertEqual(list(self.layout.status_dir.iterdir()), [])
        self.assertEqual(reap_interrupted(self.layout), [])

    def test_a_dead_run_whose_status_is_held_is_recorded_once_it_is_let_go(self):
        died = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            StatusBoard(RuntimeLayout(Path({str(self.tmp)!r})),
                        LiveStatus("d" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-14T10:00:00Z",
                                   pair_index=1, marker_id="m" * 32, pair_started="2026-10-14T10:00:01Z")).open()
            os._exit(0)
        """)], capture_output=True, text=True, creationflags=FLAGS, timeout=60)
        self.assertEqual(died.returncode, 0, died.stderr)
        with held(self.layout.status_dir / f"{'d' * 32}.json"):
            self.assertEqual(reap_interrupted(self.layout), [])
        self.assertEqual([r.outcome for r in reap_interrupted(self.layout)], [Outcome.INTERRUPTED])
        self.assertEqual(reap_interrupted(self.layout), [])
        self.assertEqual(len(read_records(self.layout, "j" * 32)), 1)


class StatusFoundByItsLockTest(TempDirTest):
    """A run is found by its liveness lock as well as by its status: a look that meets its
    status as it is renamed over finds no file, and must not take the run as ended."""

    def setUp(self):
        super().setUp()
        self.layout = RuntimeLayout(self.tmp)
        self.layout.status_dir.mkdir(parents=True)

    def lock(self, run_id="a" * 32) -> FileLock:
        lock = FileLock(self.layout.status_dir / f"{run_id}.lock")
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        return lock

    def test_a_held_lock_whose_status_stays_missing_is_a_live_run_whose_status_cannot_be_read(self):
        self.lock()
        started = time.monotonic()
        runs = observe_runs(self.layout)
        self.assertEqual([(r.run_id, r.alive, r.readable) for r in runs], [("a" * 32, True, False)])
        self.assertGreaterEqual(time.monotonic() - started, MISSING_PATIENCE)
        me = Activity.from_status(LiveStatus("c" * 32, 1, "k" * 32, "Other", "manual", "2026-10-14T10:00:00Z",
                                             claim={"reads": [], "writes": []}).to_document())
        self.assertEqual([a.run_id for a in blockers(me, live_activities(self.layout))], ["a" * 32])
        self.assertEqual(reap_interrupted(self.layout), [])

    def test_a_status_that_appears_while_it_is_looked_for_is_read(self):
        self.lock()
        path = self.layout.status_dir / f"{'a' * 32}.json"
        timer = threading.Timer(MISSING_PATIENCE / 4, lambda: exchange.write_text(path, json.dumps(
            status().to_document())))
        timer.start()
        self.addCleanup(timer.cancel)
        runs = observe_runs(self.layout)
        self.assertEqual([(r.run_id, r.alive, r.readable) for r in runs], [("a" * 32, True, True)])

    def test_a_lock_nobody_holds_with_no_status_is_no_run(self):
        (self.layout.status_dir / f"{'a' * 32}.lock").write_bytes(b"")
        self.assertEqual(observe_runs(self.layout), [])


class LastReadTest(unittest.TestCase):
    """What an observer remembers of the activities it finds: the last status it read of
    each, taken only to name one whose status it cannot read now."""

    def test_a_status_read_names_the_activity_until_it_is_no_longer_found(self):
        last_read = LastRead()
        read = ObservedRun({"run_id": "a" * 32, "job_name": "Photos", "kind": "verification"}, True)
        unread = ObservedRun({"run_id": "a" * 32}, True, False)
        self.assertEqual(last_read.look([read]), [read])
        self.assertEqual(last_read.look([unread])[0].last, read.status)
        self.assertEqual(last_read.look([unread])[0].last, read.status, "still remembered while it is found")
        self.assertEqual(last_read.look([]), [])
        self.assertIsNone(last_read.look([unread])[0].last, "one no longer found is forgotten")


@unittest.skipUnless(WINDOWS, "Windows' refusals")
class JobFileTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        self.vol = volume(1, "C")

    def job(self, name: str):
        return new_job(name, endpoint(self.vol, name), [endpoint(self.vol, name + "-copy")])

    def test_a_save_over_a_file_changed_since_it_was_read_is_refused_and_nothing_written(self):
        mine = self.store.load()
        self.assertIsNone(mine.fingerprint)
        theirs = self.store.save(StoreContents((self.job("Theirs"),)))
        before = self.store.path.read_bytes()
        with self.assertRaises(StoreConflict):
            self.store.save(StoreContents((self.job("Mine"),)), expected=mine.fingerprint)
        self.assertEqual(self.store.path.read_bytes(), before)
        again = self.store.load()
        self.assertEqual((again.fingerprint, [j.name for j in again.contents.jobs]), (theirs, ["Theirs"]))
        after = self.store.save(again.contents.with_job(self.job("Mine")), expected=again.fingerprint)
        self.assertEqual(self.store.load().fingerprint, after)

    def test_a_job_file_another_program_holds_is_busy_never_corrupt(self):
        self.store.save(StoreContents((self.job("A"),)))
        with held(self.store.path):
            with self.assertRaises(StoreBusy) as caught:
                self.store.load()
        self.assertEqual(str(caught.exception), "the job file could not be read: another program kept it open; "
                                                "try again")
        self.assertEqual(sorted(p.name for p in self.store.path.parent.iterdir() if "corrupt" in p.name), [])
        self.assertIs(self.store.load().status, LoadStatus.LOADED)

    def test_a_previous_copy_another_program_holds_is_left_and_the_save_goes_through(self):
        store = ConfigStore(self.store.path, keep_previous=1)
        jobs = (self.job("A"),)
        store.save(StoreContents(jobs))
        jobs += (self.job("B"),)
        store.save(StoreContents(jobs))
        oldest = store.previous_copies()[-1]
        jobs += (self.job("C"),)
        with held(oldest):
            store.save(StoreContents(jobs))
            self.assertEqual(store.previous_copies()[1:], (oldest,), "the copy another program holds is left")
        self.assertEqual([j.name for j in store.load().contents.jobs], ["A", "B", "C"])
        newest = json.loads(store.previous_copies()[0].read_text(encoding="utf-8"))
        self.assertEqual([j["name"] for j in newest["jobs"]], ["A", "B"], "the file the save replaced")
        store.save(StoreContents(jobs + (self.job("D"),)))
        self.assertEqual(len(store.previous_copies()), 1, "a later save removes what was left")

    def test_a_job_file_saved_by_another_process_is_loaded_at_every_try(self):
        self.store.save(StoreContents((self.job("A"),)))
        saver = job_file_saver(self, self.store.path, SECONDS)
        loads, failures = 0, []
        stop = time.monotonic() + SECONDS
        while time.monotonic() < stop:
            loads += 1
            try:
                loaded = self.store.load()
            except StoreBusy as exc:
                failures.append(str(exc))
                continue
            if loaded.status is not LoadStatus.LOADED:                 # fresh: no file; corrupt: moved aside
                failures.append(loaded.status.value + (f": {loaded.detail}" if loaded.detail else ""))
        saved = result_of(self, saver)
        self.assertGreater((loads, saved["saves"]), (20, 20))
        self.assertEqual((len(failures), saved["failures"]), (0, 0), "\n".join(failures))   # each miss, unshortened


if __name__ == "__main__":
    unittest.main()
