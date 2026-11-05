"""The run end to end, on temporary folders, through the pinned engine.

Each test is one of the run's designed outcomes, injected as a failure or an
event: a first run waiting for confirmation, removals landing in a dated versions
batch, an unlistable source folder, a long-held file, the engine killed mid-copy, the
four kinds of mass removal and a lost file signature held while additions are copied,
a tree reorganised by renames, pause and resume, graceful and immediate cancel, runs at
the same time and runs waiting their turn, runs on one drive one after the other — once
through this machine's real volume source. The last classes follow the live path: an
engine that must not outlive its core, and the command line through ``python -m
mirrorhold`` on this machine's real volume source (read-only) with a store in a
temporary folder.

Volumes are temporary folders mapped as volumes (``support.volume``); nothing touches a
real volume but through a temporary folder on the system drive."""
import ctypes
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from support import GIB, PROJECT, TempDirTest, endpoint, held, job_file_saver, live_activity, result_of, volume
from test_engine_contract import (CARRIED_NAMES, INSTALLED, MiB, current_user, drop_backup_privileges, growing_partial,
                                  icacls, partials, wait_for, write_verbatim)

from mirrorhold.endpoints.cloud import SyncKind, SyncRoot
from mirrorhold.endpoints.marker import Marker, MarkerState, check_marker, marker_check, write_marker
from mirrorhold.endpoints.resolution import endpoint_from_path
from mirrorhold.endpoints.shared import read_shared, shared_path
from mirrorhold.endpoints.volumes import FixedVolumeSource, default_volume_source, drive_name, find_by_mount
from mirrorhold.engine.rclone import Engine, TransferStats, _Windows, open_engine
from mirrorhold.jobs.model import Activities, Destination, DriveTurns, HoldThresholds, Preferences, new_job
from mirrorhold.jobs.store import ConfigStore, LoadStatus, StoreContents
from mirrorhold.locations import RuntimeLayout, runtime_layout
from mirrorhold.maintenance.cleanup import batch_time
from mirrorhold.planning.listing import Side, list_tree
from mirrorhold.planning.plan import build_plan
from mirrorhold.records import diagnostics
from mirrorhold.records.admission import WaitedFor
from mirrorhold.records.record import Outcome, read_records
from mirrorhold.records.status import CancelMode, LiveStatus, Phase, StatusBoard, Wanted, observe_runs, request
from mirrorhold.run import apply
from mirrorhold.run.apply import PASSED_OVER, VersionsBatch, apply_pair
from mirrorhold.run.control import RunControl, StatusPump
from mirrorhold.run.gate import judge
from mirrorhold.run.confirmation import Content
from mirrorhold.run.launch import EXIT_STARTED_NORMALLY, run_arguments
from mirrorhold.run.put_back import newest_batch, put_back
from mirrorhold.run.runner import RunError, RunOptions, preview_job, run_job, ticket_for

WINDOWS = sys.platform == "win32"
needs_engine = unittest.skipUnless(INSTALLED, "the pinned engine is not installed")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
BATCH = re.compile(r"^\.mirrorhold\\versions\\\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ(-\d+)?$")


def write(root, rel, data):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def tree(root):
    """Every file under ``root`` but the management folder, as spelled on disk."""
    root = Path(root)
    found = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".mirrorhold"]
        found += [os.path.relpath(os.path.join(directory, n), root) for n in names]
    return sorted(found)


def folders(root):
    root = Path(root)
    found = []
    for directory, dirs, _names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".mirrorhold"]
        found += [os.path.relpath(os.path.join(directory, d), root) for d in dirs]
    return sorted(found)


class Held:
    """A file held open by this process without sharing, as an application holding a
    database or a mailbox does."""

    def __init__(self, *paths):
        self.paths = paths
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32

    def __enter__(self):
        self.handles = [self.k32.CreateFileW(str(p), 0x80000000, 0, None, 3, 0x80, None) for p in self.paths]
        if any(h == wintypes.HANDLE(-1).value for h in self.handles):
            raise OSError("a file could not be held")
        return self

    def __exit__(self, *exc):
        for handle in self.handles:
            self.k32.CloseHandle(handle)


@needs_engine
class RunCase(TempDirTest):
    holds = HoldThresholds(count_floor=3, percent=5)

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        self.src, self.dst = self.vol_s / "Work", self.vol_d / "Backup"
        self.src.mkdir(parents=True)
        self.vol_d.mkdir()
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="D")
        self.volumes = FixedVolumeSource((self.s, self.d))
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Backup")], holds=self.holds)
        self.store.save(StoreContents((self.job,)))
        self.layout = runtime_layout(self.store.path)

    def run_now(self, **options):
        return run_job(self.store, self.job.job_id, RunOptions(**options), volume_source=self.volumes, sync_roots=())

    def only(self, result):
        self.assertEqual(len(result.records), 1)
        return result.records[0]

    def preview(self):
        return preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=())

    def confirm(self):
        ticket = self.preview().ticket
        self.assertIsNotNone(ticket)
        return self.only(self.run_now(ticket_id=ticket.ticket_id))

    def seed(self, files):
        for rel, data in files.items():
            write(self.src, rel, data)
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), tree(self.src))

    def versions(self):
        root = self.dst / ".mirrorhold" / "versions"
        if not root.is_dir():
            return []
        return sorted(os.path.relpath(os.path.join(d, n), root).split("\\", 1)[1]
                      for d, _dirs, names in os.walk(root) for n in names)

    def in_background(self, **options):
        box = {}

        def target():
            try:
                box["result"] = self.run_now(**options)
            except BaseException as exc:  # handed to the test thread by finish()
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box

    def finish(self, thread, box, timeout=180):
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "the run did not end")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def live(self, predicate, timeout=60):
        def find():
            for run in observe_runs(self.layout):
                if run.alive and predicate(run.status):
                    return run.status
            return None
        status = wait_for(find, timeout=timeout)
        self.assertIsNotNone(status, "the run never reached the awaited state")
        return status


# --- first run, mirror, versions -------------------------------------------------------

class FirstRunTest(RunCase):
    def test_a_first_run_waits_for_confirmation_and_changes_nothing(self):
        write(self.src, "a.txt", "a")
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.HELD, ("first-run",)))
        self.assertFalse(self.dst.exists())
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), ["a.txt"])
        self.assertIs(check_marker(self.dst, self.job.job_id, self.job.destinations[0].marker_id), MarkerState.MATCH)
        self.assertTrue((self.dst / ".mirrorhold" / "job.json").is_file())
        self.assertEqual(len(list((self.dst / ".mirrorhold" / "runs").iterdir())), 1)

    def test_a_non_empty_destination_is_adopted_only_through_confirmation(self):
        write(self.src, "same.txt", "same")
        write(self.dst, "same.txt", "same")
        os.utime(self.dst / "same.txt", ns=(os.stat(self.src / "same.txt").st_mtime_ns,) * 2)
        write(self.dst, "stray.txt", "not in the source")
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.HELD)
        self.assertEqual(tree(self.dst), ["same.txt", "stray.txt"])
        moving = preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                             content=Content.MOVE)
        pair = moving.pairs[0].assessment
        self.assertTrue(pair.decision.adoption)
        self.assertEqual([e.path for e in pair.plan.deleted], ["stray.txt"])
        record = self.only(self.run_now(ticket_id=moving.ticket.ticket_id))
        self.assertEqual((record.outcome, record.done["files_copied"]), (Outcome.COMPLETED, 0))
        self.assertEqual(tree(self.dst), ["same.txt"])
        self.assertEqual(self.versions(), ["stray.txt"])


class FirstBackupTest(RunCase):
    """A first backup onto a folder that holds nothing needs only the user's go-ahead, no
    preview; the go-ahead covers the folder as it was and lapses once it holds something."""

    def marker(self):
        return check_marker(self.dst, self.job.job_id, self.job.destinations[0].marker_id)

    def test_the_go_ahead_starts_a_first_backup_into_a_folder_not_made_yet(self):
        write(self.src, "a.txt", "a")
        write(self.src, "sub/b.txt", "b")
        record = self.only(self.run_now(first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.COMPLETED, ()))
        self.assertEqual(tree(self.dst), ["a.txt", "sub\\b.txt"])
        self.assertIs(self.marker(), MarkerState.MATCH)

    def test_the_go_ahead_starts_a_first_backup_into_an_empty_folder(self):
        write(self.src, "a.txt", "a")
        self.dst.mkdir()
        self.assertEqual(self.only(self.run_now(first_backup=True)).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), ["a.txt"])

    def test_the_go_ahead_lapses_on_a_folder_that_holds_something_and_nothing_is_written(self):
        write(self.src, "a.txt", "a")
        for holding in ("old.txt", "Empty\\"):
            with self.subTest(holding=holding):
                shutil.rmtree(self.dst, ignore_errors=True)
                if holding.endswith("\\"):
                    (self.dst / holding).mkdir(parents=True)
                else:
                    write(self.dst, holding, "not ours")
                before = tree(self.dst)
                record = self.only(self.run_now(first_backup=True))
                self.assertEqual((record.outcome, record.reasons),
                                 (Outcome.HELD, ("first-run", "destination-holds-content")))
                self.assertEqual(tree(self.dst), before)
                self.assertTrue((self.dst / holding).exists())
                self.assertIs(self.marker(), MarkerState.MISSING)

    def test_a_later_run_is_judged_as_any_run(self):
        write(self.src, "a.txt", "a")
        self.only(self.run_now(first_backup=True))
        write(self.src, "b.txt", "b")
        self.assertEqual(self.only(self.run_now(first_backup=True)).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), ["a.txt", "b.txt"])

    def test_nothing_unattended_or_ticketed_confirms_a_first_backup(self):
        write(self.src, "a.txt", "a")
        with self.assertRaises(RunError):
            self.run_now(first_backup=True, unattended=True)
        with self.assertRaises(RunError):
            self.run_now(first_backup=True, ticket_id="abc")
        record = self.only(self.run_now(unattended=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.HELD, ("first-run",)))
        self.assertFalse(self.dst.exists())

    @unittest.skipUnless(WINDOWS, "suspending a process")
    def test_a_first_backup_stopped_part_way_is_continued_by_the_next_run_without_asking(self):
        names = [f"part{i}.bin" for i in range(6)]
        for name in names:
            write(self.src, name, os.urandom(4 * MiB))
        thread, box = self.in_background(first_backup=True, bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        self.assertIs(self.marker(), MarkerState.MATCH, "the marker is written before the first copy")
        request(self.layout, status["run_id"], cancel=CancelMode.IMMEDIATE)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled-immediate",)))
        self.assertLess(len(tree(self.dst)), len(names))
        again = self.only(self.run_now())                    # no go-ahead, no preview: an ordinary run
        self.assertEqual((again.outcome, again.reasons), (Outcome.COMPLETED, ()))
        self.assertEqual(tree(self.dst), tree(self.src))
        self.assertEqual(self.versions(), [])


class LeaveTest(RunCase):
    """A first run into a folder that already holds what the source does not leaves that
    where it is by default, by that run and every later one; moving it into versions is the
    other choice (the maintainer's decisions of 2026-10-10)."""

    FOREIGN = {"Windows/System32/kernel.dll": "the operating system", "Users/someone/letter.docx": "someone's",
               "Photos/old.jpg": "an old photo"}
    OWN = {f"Photos/p{i}.jpg": f"photo {i}" for i in range(6)}

    def marker(self):
        return marker_check(self.dst, self.job.job_id, self.job.destinations[0].marker_id)

    def record_of_what_is_left(self):
        return read_shared(self.dst, self.job.job_id, self.job.destinations[0].marker_id)

    def first_leave(self):
        for rel, data in {**self.OWN, "Photos/a.jpg": "new a", "letter.txt": "letter"}.items():
            write(self.src, rel, data)
        for rel, data in {**self.FOREIGN, "Photos/a.jpg": "old a"}.items():
            write(self.dst, rel, data)
        os.utime(self.dst / "Photos" / "a.jpg", ns=(1_600_000_000_000_000_000,) * 2)
        pair = self.preview().pairs[0].assessment
        self.assertEqual([a.content for a in pair.alternatives], [Content.LEAVE, Content.MOVE])
        self.assertEqual((pair.content, pair.withheld, pair.sharing.left), (Content.LEAVE, "", ("Photos\\old.jpg",)))
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        return record

    def test_a_first_run_leaves_what_the_folder_holds_by_default(self):
        self.first_leave()
        self.assertEqual(tree(self.dst), sorted(["Photos\\a.jpg", "Photos\\old.jpg", "Users\\someone\\letter.docx",
                                                 "Windows\\System32\\kernel.dll", "letter.txt"]
                                                + [f"Photos\\p{i}.jpg" for i in range(6)]))
        self.assertEqual((self.dst / "Photos" / "a.jpg").read_text(encoding="utf-8"), "new a")
        self.assertEqual(self.versions(), ["Photos\\a.jpg"], "a source file where something was replaces it")
        state, marker = self.marker()
        self.assertEqual((state, marker.shared), (MarkerState.MATCH, True))
        shared = self.record_of_what_is_left()
        self.assertEqual((shared.own, shared.left), (("letter.txt", "Photos"), ("Photos\\old.jpg",)))

    def test_later_runs_leave_what_appears_beside_the_jobs_own_and_mirror_inside_it(self):
        self.first_leave()
        write(self.dst, "Program Files/App/new.exe", "installed since")
        write(self.dst, "Photos/hand.txt", "put there by hand")
        (self.src / "letter.txt").unlink()
        write(self.src, "Music/song.mp3", "song")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertIn("Program Files\\App\\new.exe", tree(self.dst))
        self.assertTrue((self.dst / "Windows" / "System32" / "kernel.dll").is_file())
        self.assertNotIn("Photos\\hand.txt", tree(self.dst))
        self.assertEqual(sorted(self.versions()), ["Photos\\a.jpg", "Photos\\hand.txt", "letter.txt"])
        write(self.src, "Photos/old.jpg", "the source's own old.jpg, longer")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((self.dst / "Photos" / "old.jpg").read_text(encoding="utf-8"),
                         "the source's own old.jpg, longer")
        self.assertIn("Photos\\old.jpg", self.versions(), "what was left moves into versions once the source "
                                                          "holds a file at its path")
        shared = self.record_of_what_is_left()
        self.assertEqual((shared.own, shared.left), (("Music", "Photos"), ()))
        self.assertEqual(sorted(t for t in tree(self.dst) if not t.startswith(("Photos", "Music"))),
                         ["Program Files\\App\\new.exe", "Users\\someone\\letter.docx",
                          "Windows\\System32\\kernel.dll"])

    def test_moving_what_the_folder_holds_is_the_whole_adoption(self):
        write(self.src, "same.txt", "same")
        write(self.dst, "stray.txt", "not in the source")
        preview = preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                              content=Content.MOVE)
        self.assertIs(preview.pairs[0].assessment.content, Content.MOVE)
        record = self.only(self.run_now(ticket_id=preview.ticket.ticket_id))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((tree(self.dst), self.versions()), (["same.txt"], ["stray.txt"]))
        state, marker = self.marker()
        self.assertEqual((state, marker.shared), (MarkerState.MATCH, False))
        self.assertFalse(shared_path(self.dst).exists())

    def test_the_choice_can_be_made_after_seeing_both(self):
        write(self.src, "same.txt", "same")
        write(self.dst, "stray.txt", "not in the source")
        preview = self.preview()
        ticket = ticket_for(self.store, preview, {1: Content.MOVE})
        self.assertNotEqual(ticket.ticket_id, preview.ticket.ticket_id)
        self.assertEqual(self.only(self.run_now(ticket_id=ticket.ticket_id)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.versions(), ["stray.txt"])

    def test_a_folder_holding_nothing_the_source_does_not_offers_no_choice(self):
        write(self.src, "same.txt", "same")
        write(self.dst, "same.txt", "different")
        pair = self.preview().pairs[0].assessment
        self.assertEqual((pair.alternatives, pair.content), ((), None))
        self.assertEqual(self.confirm().outcome, Outcome.COMPLETED)
        self.assertFalse(self.marker()[1].shared)

    def test_a_confirmed_first_run_the_folder_outgrew_waits_whole(self):
        write(self.src, "a.txt", "a")
        ticket = self.preview().ticket
        write(self.dst, "stray.txt", "appeared since the preview")
        record = self.only(self.run_now(ticket_id=ticket.ticket_id))
        self.assertEqual((record.outcome, record.reasons),
                         (Outcome.HELD, ("first-run", "not-in-confirmed-plan", "destination-holds-content")))
        self.assertEqual(tree(self.dst), ["stray.txt"])
        self.assertIs(self.marker()[0], MarkerState.MISSING)

    def test_what_appears_after_a_preview_that_leaves_is_left_too(self):
        write(self.src, "a.txt", "a")
        write(self.dst, "old.txt", "there before")
        ticket = self.preview().ticket
        write(self.dst, "later/theirs.txt", "appeared since the preview")
        self.assertEqual(self.only(self.run_now(ticket_id=ticket.ticket_id)).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), ["a.txt", "later\\theirs.txt", "old.txt"])
        self.assertEqual(self.versions(), [])

    def test_a_shared_destination_whose_record_was_removed_waits_untouched_for_a_review(self):
        self.first_leave()
        shared_path(self.dst).unlink()
        write(self.src, "new.txt", "new")
        before = tree(self.dst)
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons),
                         (Outcome.HELD, ("first-run", "destination-holds-content", "shared-record-lost")))
        self.assertEqual(tree(self.dst), before)


class LostRecordTest(RunCase):
    """A shared destination whose record of what to leave was lost: its run is a first run
    again, which leaves what is there, waits untouched for the user's review and,
    confirmed, makes the record anew (the maintainer's decisions of 2026-10-17)."""

    FOREIGN, OWN = LeaveTest.FOREIGN, LeaveTest.OWN
    first_leave = LeaveTest.first_leave
    marker = LeaveTest.marker
    record_of_what_is_left = LeaveTest.record_of_what_is_left

    def test_a_lost_record_waits_untouched_for_a_review(self):
        self.first_leave()
        shared_path(self.dst).unlink()
        write(self.src, "new.txt", "new")
        before = tree(self.dst)
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons),
                         (Outcome.HELD, ("first-run", "destination-holds-content", "shared-record-lost")))
        self.assertTrue(record.detail.startswith("its record of what to leave where it is was lost ("), record.detail)
        self.assertEqual(tree(self.dst), before)
        self.assertFalse(shared_path(self.dst).exists())

    def test_confirmed_it_leaves_what_is_there_and_makes_the_record_again(self):
        self.first_leave()
        shared_path(self.dst).write_text("not a record", encoding="utf-8")
        (self.src / "letter.txt").unlink()
        write(self.dst, "Mine/theirs.txt", "put beside the job's folders")
        write(self.src, "new.txt", "new")
        versions_before = self.versions()
        preview = self.preview()
        pair = preview.pairs[0].assessment
        self.assertIn("shared.json", pair.rebuild)
        self.assertEqual((pair.content, pair.withheld, pair.alternatives),
                         (Content.LEAVE, "this folder's first run left what it held where it is", ()))
        with self.assertRaises(RunError):
            ticket_for(self.store, preview, {1: Content.MOVE})
        record = self.only(self.run_now(ticket_id=preview.ticket.ticket_id))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertIn("letter.txt", tree(self.dst), "backed up by the job, its source copy gone: left from now on")
        self.assertIn("Mine\\theirs.txt", tree(self.dst))
        self.assertEqual((self.dst / "new.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual(sorted(set(self.versions()) - set(versions_before)), [".mirrorhold\\shared.json"],
                         "the lost record kept, nothing else moved")
        kept = [b / ".mirrorhold" / "shared.json" for b in (self.dst / ".mirrorhold" / "versions").iterdir()
                if (b / ".mirrorhold" / "shared.json").is_file()]
        self.assertEqual([p.read_text(encoding="utf-8") for p in kept], ["not a record"])
        state, marker = self.marker()
        self.assertEqual((state, marker.shared), (MarkerState.MATCH, True))
        shared = self.record_of_what_is_left()
        self.assertEqual((shared.own, shared.left), (("new.txt", "Photos"), ("Photos\\old.jpg",)))
        again = self.only(self.run_now())
        self.assertEqual((again.outcome, again.reasons), (Outcome.COMPLETED, ()))
        self.assertIn("letter.txt", tree(self.dst))

    @unittest.skipUnless(sys.platform == "win32", "holding a file without sharing")
    def test_a_record_another_program_holds_stops_the_run_untouched(self):
        self.first_leave()
        write(self.src, "new.txt", "new")
        before = tree(self.dst)
        with held(shared_path(self.dst)):
            record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("shared-record-unreadable",)))
        self.assertEqual(tree(self.dst), before)
        self.assertEqual(self.record_of_what_is_left().own, ("letter.txt", "Photos"), "the record is intact")


class RefusedAtTheRunTest(RunCase):
    """A job refused when it is made is refused at its run too, nothing touched: a source
    that is a file, a destination that is a file, a destination that is itself a folder
    this tool needs."""

    def test_a_source_that_is_a_file_stops_the_run(self):
        write(self.vol_s, "notes.txt", "a file")
        job = new_job("File", endpoint(self.s, "notes.txt"), [endpoint(self.d, "Backup")])
        self.store.save(StoreContents((job,)))
        result = run_job(self.store, job.job_id, RunOptions(), volume_source=self.volumes, sync_roots=())
        record = self.only(result)
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("source-not-a-folder",)))
        self.assertFalse(self.dst.exists())

    def test_a_destination_that_is_a_file_stops_the_run_and_its_first_backup(self):
        write(self.src, "a.txt", "a")
        write(self.vol_d, "Backup", "a file where the backup's folder would be")
        for options in (RunOptions(), RunOptions(first_backup=True)):
            record = self.only(run_job(self.store, self.job.job_id, options, volume_source=self.volumes,
                                       sync_roots=()))
            self.assertEqual((record.outcome, record.reasons, record.detail),
                             (Outcome.STOPPED, ("destination-not-a-folder",), "the job is refused at this destination"))
            self.assertEqual(self.dst.read_text(encoding="utf-8"), "a file where the backup's folder would be")

    def test_a_destination_that_is_the_folder_of_the_job_file_stops_the_run(self):
        store = ConfigStore(self.vol_d / "Tool" / "mirrorhold.json")
        job = new_job("Tool", endpoint(self.s, "Work"), [endpoint(self.d, "Tool")])
        store.save(StoreContents((job,)))
        write(self.src, "a.txt", "a")
        result = run_job(store, job.job_id, RunOptions(first_backup=True), volume_source=self.volumes, sync_roots=())
        record = self.only(result)
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-is-system-folder",)))
        self.assertFalse((self.vol_d / "Tool" / "a.txt").exists())


class ProtectedFolderTest(RunCase):
    """Folders inside a destination no run may move or write into: another job's source, a
    synchronised folder, a folder Windows or this tool needs (the maintainer's decisions of
    2026-10-10 and 2026-10-11)."""

    def add_inner_job(self):
        inner = new_job("Inner", endpoint(self.d, "Backup\\Inner"), [endpoint(self.s, "InnerCopy")])
        self.store.save(self.store.load().contents.with_job(inner))
        return inner

    def test_a_destination_holding_another_jobs_source_offers_only_to_leave_it(self):
        write(self.src, "a.txt", "a")
        write(self.dst, "Inner/data.txt", "another job's source")
        self.add_inner_job()
        preview = self.preview()
        pair = preview.pairs[0].assessment
        self.assertIs(pair.content, Content.LEAVE)
        self.assertIn("the source of job 'Inner'", pair.withheld)
        with self.assertRaises(RunError):
            ticket_for(self.store, preview, {1: Content.MOVE})
        self.assertEqual(self.only(self.run_now(ticket_id=preview.ticket.ticket_id)).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), ["Inner\\data.txt", "a.txt"])
        write(self.src, "Inner/mine.txt", "the source now holds a folder of that name")
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-contains-other-source",)))
        self.assertIn("beneath Inner", record.detail)
        self.assertEqual(tree(self.dst), ["Inner\\data.txt", "a.txt"])

    def test_a_whole_destination_holding_another_jobs_source_stops_untouched(self):
        write(self.src, "a.txt", "a")
        self.assertEqual(self.only(self.run_now(first_backup=True)).outcome, Outcome.COMPLETED)
        write(self.dst, "Inner/data.txt", "another job's source, made since")
        self.add_inner_job()
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-contains-other-source",)))
        self.assertEqual(tree(self.dst), ["Inner\\data.txt", "a.txt"])

    def test_a_first_backup_where_another_jobs_source_is_to_be_is_shared(self):
        write(self.src, "a.txt", "a")
        self.add_inner_job()
        self.assertEqual(self.only(self.run_now(first_backup=True)).outcome, Outcome.COMPLETED)
        marker = marker_check(self.dst, self.job.job_id, self.job.destinations[0].marker_id)[1]
        self.assertTrue(marker.shared)
        write(self.dst, "Inner/data.txt", "another job's source, made since")
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        self.assertEqual((tree(self.dst), self.versions()), (["Inner\\data.txt", "a.txt"], []))

    def test_a_folder_holding_the_job_file_is_offered_only_to_leave_it(self):
        store = ConfigStore(self.dst / "Tool" / "mirrorhold.json")
        store.save(StoreContents((self.job,)))
        write(self.src, "a.txt", "a")
        preview = preview_job(store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                              content=Content.MOVE)
        pair = preview.pairs[0].assessment
        self.assertIs(pair.content, Content.LEAVE, "moving is not offered, whatever was asked")
        self.assertIn("which MirrorHold needs", pair.withheld)
        result = run_job(store, self.job.job_id, RunOptions(ticket_id=preview.ticket.ticket_id),
                         volume_source=self.volumes, sync_roots=())
        self.assertEqual(self.only(result).outcome, Outcome.COMPLETED)
        self.assertTrue((self.dst / "Tool" / "mirrorhold.json").is_file())
        self.assertIn("a.txt", tree(self.dst))


class ExcludedProtectedFolderTest(RunCase):
    """A folder no run may move or write into that the job's exclusions hide stops no run
    for lying beneath the job's names, nor for lying in a destination that is the job's
    whole; no folder holding it moves into versions whole (the maintainer's decision of
    2026-10-21)."""

    def job_file_inside(self, relative, *patterns):
        """The job, excluding ``patterns``, in a job file in the folder ``relative`` inside
        its destination — a folder MirrorHold needs."""
        self.job = replace(self.job, source=replace(self.job.source, exclusions=patterns))
        store = ConfigStore(self.dst / relative / "mirrorhold.json")
        store.save(StoreContents((self.job,)))
        return store

    def run_in(self, store, **options):
        return self.only(run_job(store, self.job.job_id, RunOptions(**options), volume_source=self.volumes,
                                 sync_roots=()))

    def text(self, rel):
        return (self.dst / rel).read_text(encoding="utf-8")

    def test_an_excluded_job_file_folder_beneath_a_name_the_source_holds_is_left_as_it_is(self):
        write(self.src, "Tools\\code.py", "code")
        write(self.src, "Tools\\State\\a.txt", "the other computer's state")
        write(self.src, "Other\\x.txt", "x")
        write(self.dst, "Tools\\State\\mine.txt", "this computer's state")
        write(self.dst, "Old\\keep.txt", "the destination's own")
        store = self.job_file_inside("Tools\\State", "/Tools/State/")
        preview = preview_job(store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                              content=Content.MOVE)
        pair = preview.pairs[0].assessment
        self.assertEqual((pair.outcome, pair.content), (Outcome.HELD, Content.LEAVE),
                         "moving is not offered, whatever was asked")
        self.assertIn("which MirrorHold needs", pair.withheld)
        self.assertEqual(self.run_in(store, ticket_id=preview.ticket.ticket_id).outcome, Outcome.COMPLETED)
        self.assertEqual((self.text("Tools\\State\\mine.txt"), self.text("Tools\\code.py"), self.text("Old\\keep.txt")),
                         ("this computer's state", "code", "the destination's own"))
        self.assertFalse((self.dst / "Tools" / "State" / "a.txt").exists(), "what the job excludes is not copied")
        self.assertIs(store.load().status, LoadStatus.LOADED)
        self.assertEqual(self.versions(), [])

    def test_a_job_file_folder_not_excluded_whole_beneath_a_name_the_source_holds_stops_the_run(self):
        write(self.src, "Tools\\code.py", "code")
        write(self.dst, "Tools\\State\\mine.txt", "this computer's state")
        for patterns in ((), ("/Tools/State/old/",)):
            with self.subTest(patterns=patterns):
                record = self.run_in(self.job_file_inside("Tools\\State", *patterns), first_backup=True)
                self.assertEqual((record.outcome, record.reasons),
                                 (Outcome.STOPPED, ("destination-contains-system-folder",)))
                self.assertIn("beneath Tools, which this job backs up there; excluding it from the job leaves it "
                              "where it is", record.detail)
                self.assertFalse((self.dst / "Tools" / "code.py").exists())

    def test_a_folder_holding_an_excluded_job_file_folder_stays_when_the_source_drops_it(self):
        for rel in ("Tools\\code.py", "Other\\x.txt", "Other\\y.txt", "Other\\z.txt"):
            write(self.src, rel, rel)
        write(self.dst, "Tools\\State\\mine.txt", "this computer's state")
        store = self.job_file_inside("Tools\\State", "/Tools/State/")
        ticket = preview_job(store, self.job.job_id, volume_source=self.volumes, sync_roots=()).ticket
        self.assertEqual(self.run_in(store, ticket_id=ticket.ticket_id).outcome, Outcome.COMPLETED)
        shutil.rmtree(self.src / "Tools")
        record = self.run_in(store)
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(self.versions(), ["Tools\\code.py"], "its other files move one by one")
        self.assertEqual(self.text("Tools\\State\\mine.txt"), "this computer's state")
        self.assertIs(store.load().status, LoadStatus.LOADED)

    def test_a_source_file_where_a_folder_holding_an_excluded_job_file_folder_lies_stops_the_run(self):
        write(self.src, "Tools\\App", "a file where the destination holds a folder")
        write(self.dst, "Tools\\App\\State\\mine.txt", "this computer's state")
        record = self.run_in(self.job_file_inside("Tools\\App\\State", "/Tools/App/State/"), first_backup=True)
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-contains-system-folder",)))
        self.assertIn("the source holds a file at Tools\\App: copying it would move the folder into versions",
                      record.detail)
        self.assertTrue((self.dst / "Tools" / "App").is_dir())
        self.assertEqual(self.text("Tools\\App\\State\\mine.txt"), "this computer's state")

    def test_a_whole_destination_runs_on_around_another_jobs_source_its_job_excludes(self):
        for rel in ("a.txt", "b.txt", "c.txt", "Projects\\p.txt"):
            write(self.src, rel, rel)
        self.assertEqual(self.only(self.run_now(first_backup=True)).outcome, Outcome.COMPLETED)
        write(self.dst, "Projects\\Inner\\data.txt", "another job's source, made since")
        inner = new_job("Inner", endpoint(self.d, "Backup\\Projects\\Inner"), [endpoint(self.s, "InnerCopy")])
        self.store.save(StoreContents((self.job, inner)))
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-contains-other-source",)))
        self.assertIn("its run would move it into versions; excluding it from the job leaves it where it is",
                      record.detail)
        self.job = replace(self.job, source=replace(self.job.source, exclusions=("/Projects/Inner/",)))
        self.store.save(StoreContents((self.job, inner)))
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        shutil.rmtree(self.src / "Projects")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), ["Projects\\Inner\\data.txt", "a.txt", "b.txt", "c.txt"])
        self.assertEqual(self.versions(), ["Projects\\p.txt"])


class ShareHoldRunTest(RunCase):
    """A one-file job holds every edit while its share hold is on — one file of one is
    all of it — and updates by itself once its user switches the hold off."""

    def test_a_one_file_job_updates_by_itself_with_the_share_hold_off(self):
        self.seed({"vault.kdbx": "version one"})
        write(self.src, "vault.kdbx", "version two, edited")
        os.utime(self.src / "vault.kdbx", ns=(time.time_ns() + 5 * 10 ** 9,) * 2)
        record = self.only(self.run_now(unattended=True))
        self.assertEqual(record.outcome, Outcome.HELD)
        self.assertIn("share-of-destination", record.reasons)
        self.assertEqual((self.dst / "vault.kdbx").read_text(encoding="utf-8"), "version one")
        self.job = replace(self.job, holds=HoldThresholds(count_floor=3, percent=5, share=None))
        self.store.save(StoreContents((self.job,)))
        record = self.only(self.run_now(unattended=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED)
        self.assertEqual((self.dst / "vault.kdbx").read_text(encoding="utf-8"), "version two, edited")
        self.assertEqual(self.versions(), ["vault.kdbx"])


class PairOutcomeTest(RunCase):
    def test_an_absent_destination_waits_and_is_recorded(self):
        write(self.src, "a.txt", "a")
        self.volumes = FixedVolumeSource((self.s,))
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.ABSENT, ("destination-absent",)))

    def test_a_destination_marked_for_another_job_is_stopped_and_left_alone(self):
        write(self.src, "a.txt", "a")
        write(self.dst, "theirs.txt", "t")
        write_marker(self.dst, Marker("f" * 32, "e" * 32))
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("marker-mismatch",)))
        self.assertEqual(tree(self.dst), ["theirs.txt"])

    def test_an_unattended_run_of_a_disabled_job_does_nothing(self):
        write(self.src, "a.txt", "a")
        self.store.save(StoreContents((replace(self.job, enabled=False),)))
        result = self.run_now(unattended=True)
        self.assertEqual((result.disabled, result.records), (True, ()))
        self.assertEqual(read_records(self.layout, self.job.job_id), [])


class MirrorTest(RunCase):
    def test_a_confirmed_run_mirrors_and_its_removals_land_in_a_dated_versions_batch(self):
        self.seed({f"f{i}.txt": f"file {i}" for i in range(10)})
        os.remove(self.src / "f1.txt")
        write(self.src, "f2.txt", "changed, and longer")
        write(self.src, "sub/new.txt", "new")
        (self.src / "empty").mkdir()
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), tree(self.src))
        self.assertIn("empty", folders(self.dst))
        self.assertRegex(record.versions_batch, BATCH)
        self.assertEqual(self.versions(), ["f1.txt", "f2.txt"])
        batch = self.dst / record.versions_batch
        self.assertEqual((batch / "f2.txt").read_text(), "file 2")
        self.assertEqual((record.done["moved_to_versions"], record.done["files_copied"]), (2, 2))

    def test_an_unchanged_tree_changes_nothing(self):
        self.seed({"a.txt": "a", "b/c.txt": "c"})
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED)
        self.assertEqual(sum(record.done.values()), 0)
        self.assertIsNone(record.versions_batch)

    def test_the_history_and_the_estimate_follow_the_runs(self):
        write(self.src, "a.txt", "a")
        self.assertEqual(self.preview().pairs[0].estimate.basis, "default")
        self.confirm()
        self.assertEqual([r.outcome for r in read_records(self.layout, self.job.job_id)],
                         [Outcome.COMPLETED])
        self.assertTrue(self.preview().pairs[0].estimate.describe())


class PutBackTest(RunCase):
    """Files a run moved into versions that reappear on the destination, as a cloud client
    keeping a copy of the folder puts them back: the preview names them, and a run moves
    them into versions again, counting them toward its holds."""

    def removed(self):
        """A mirror of 20 files and three more, those three then deleted at the source and
        moved into versions by a run: a folder of two files, and one file at the top. Every
        file has a size of its own, so no removal is taken for a rename."""
        self.seed({**{f"keep{i:02}.txt": "k" * (i + 1) for i in range(20)},
                   "tree\\a.txt": "a" * 31, "tree\\sub\\b.txt": "b" * 32, "gone.txt": "g" * 33})
        shutil.rmtree(self.src / "tree")
        os.remove(self.src / "gone.txt")
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.done["moved_to_versions"]), (Outcome.COMPLETED, 3), record)
        return record

    def restore(self, record, *paths):
        """Copy files back out of the run's batch with their times, as a cloud client restores them."""
        for rel in paths:
            target = self.dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.dst / record.versions_batch / rel, target)

    def test_files_that_reappear_after_a_run_moved_them_into_versions_are_named_by_the_preview(self):
        record = self.removed()
        self.restore(record, "tree\\a.txt", "tree\\sub\\b.txt", "gone.txt")
        write(self.dst, "stray.txt", "s" * 34)
        item = self.preview().pairs[0]
        plan, back = item.assessment.plan, item.put_back
        self.assertEqual(sorted(e.path for e in plan.deleted), ["gone.txt", "stray.txt", "tree\\a.txt",
                                                                "tree\\sub\\b.txt"])
        self.assertEqual(back.keys, {"gone.txt", "tree\\a.txt", "tree\\sub\\b.txt"})
        self.assertEqual(back.batch, Path(record.versions_batch).name)
        self.assertEqual(back.moved, batch_time(back.batch))
        self.assertEqual(back.keeper, "")
        self.assertEqual(plan.whole_folders(back.keys), {"tree": 2})

    def test_a_copy_that_differs_from_the_one_moved_into_versions_is_not_taken_for_one_put_back(self):
        record = self.removed()
        self.restore(record, "tree\\a.txt", "gone.txt")
        later = (self.dst / "gone.txt").stat().st_mtime_ns + 10 ** 9
        os.utime(self.dst / "gone.txt", ns=(later, later))
        write(self.dst, "tree\\sub\\b.txt", "b" * 32)
        item = self.preview().pairs[0]
        self.assertEqual(item.put_back.keys, {"tree\\a.txt"})
        self.assertEqual(item.assessment.plan.whole_folders(item.put_back.keys), {})

    def test_files_that_reappeared_move_into_versions_again_and_count_toward_the_holds(self):
        record = self.removed()
        self.restore(record, "tree\\a.txt", "tree\\sub\\b.txt", "gone.txt")
        write(self.dst, "stray.txt", "s" * 34)
        held = self.only(self.run_now())
        self.assertEqual(held.outcome, Outcome.HELD, held)
        self.assertIn("above-threshold", held.reasons)
        os.remove(self.dst / "stray.txt")
        again = self.only(self.run_now())
        self.assertEqual((again.outcome, again.done["moved_to_versions"]), (Outcome.COMPLETED, 3), again)
        self.assertNotEqual(again.versions_batch, record.versions_batch)
        self.assertEqual(tree(self.dst), tree(self.src))

    def test_only_the_newest_batch_is_read(self):
        record = self.removed()
        os.remove(self.src / "keep00.txt")
        later = self.only(self.run_now())
        self.assertEqual((later.outcome, later.done["moved_to_versions"]), (Outcome.COMPLETED, 1), later)
        self.restore(record, "gone.txt")
        self.restore(later, "keep00.txt")
        item = self.preview().pairs[0]
        self.assertEqual(item.put_back.keys, {"keep00.txt"})
        self.assertEqual(item.put_back.batch, Path(later.versions_batch).name)

    def test_the_cloud_client_keeping_a_copy_of_the_folder_is_named(self):
        record = self.removed()
        self.restore(record, "gone.txt")
        roots = (SyncRoot("Google Drive", str(self.vol_d), SyncKind.CLOUD_COPY),)
        item = preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=roots).pairs[0]
        self.assertEqual((item.put_back.keys, item.put_back.keeper), ({"gone.txt"}, "Google Drive"))
        self.assertEqual(len(item.notices), 1)

    def test_nothing_is_named_where_nothing_moved_into_versions_came_back(self):
        self.removed()
        write(self.dst, "stray.txt", "s" * 34)
        self.assertIsNone(self.preview().pairs[0].put_back)


class NewestBatchTest(TempDirTest):
    """Which versions batch is the newest, and what of it is read: in process, no engine."""

    def setUp(self):
        super().setUp()
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"
        self.src.mkdir()
        self.versions = self.dst / ".mirrorhold" / "versions"

    def test_the_newest_batch_is_found_by_its_name_a_counter_included(self):
        self.assertIsNone(newest_batch(self.dst))
        for name in ("2026-10-27T22-14-31Z", "2026-10-27T22-14-31Z-2", "2026-10-27T22-14-31Z-10",
                     "2026-10-20T08-00-00Z", "not a batch", "2026-13-01T00-00-00Z"):
            (self.versions / name).mkdir(parents=True)
        write(self.versions, "2026-12-01T00-00-00Z", "a file named as a batch is not one")
        self.assertEqual(newest_batch(self.dst),
                         ("2026-10-27T22-14-31Z-10", datetime(2026, 10, 27, 22, 14, 31, tzinfo=timezone.utc)))

    def test_only_the_folders_holding_a_removal_are_read(self):
        batch = self.versions / "2026-10-27T22-14-31Z"
        moved = write(batch, "tree\\a.txt", "a" * 31)
        write(batch, "other\\deep\\x.txt", "x")
        shutil.copy2(moved, write(self.dst, "tree\\a.txt", ""))
        write(self.dst, "new.txt", "n" * 5)
        plan = build_plan(list_tree(self.src, Side.SOURCE, drive_root=False),
                          list_tree(self.dst, Side.DESTINATION, drive_root=False))
        seen = []
        back = put_back(plan, self.dst, lambda: seen.append(1))
        self.assertEqual(back.keys, {"tree\\a.txt"})
        self.assertEqual(len(seen), 2, "the batch's top level and tree, never other")


# --- reads that fail -------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "access control lists and exclusive opens")
class UnreadableSourceTest(RunCase):
    @classmethod
    def setUpClass(cls):
        drop_backup_privileges()     # with them, every read below would succeed (measured)
        cls.user = current_user()

    def test_a_source_folder_that_cannot_be_listed_deletes_nothing_beneath_it(self):
        self.seed({"locked/a.txt": "a", "locked/sub/b.txt": "b", "ok.txt": "ok", "gone.txt": "gone"})
        os.remove(self.src / "gone.txt")
        icacls(self.src / "locked", "/deny", f"{self.user}:(RD)")
        self.addCleanup(icacls, self.src / "locked", "/reset")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), ["locked\\a.txt", "locked\\sub\\b.txt", "ok.txt"])
        self.assertEqual(self.versions(), ["gone.txt"])
        self.assertIn(("locked", "folder could not be listed"), record.skipped)

    def test_a_long_held_file_is_skipped_and_reported_while_everything_else_converges(self):
        self.seed({"held.db": "old copy", "a.txt": "a", "b.txt": "b", **{f"other{i}.txt": "o" for i in range(10)}})
        write(self.src, "held.db", "changed and longer")
        write(self.src, "newheld.db", "never backed up yet")
        os.remove(self.src / "b.txt")
        write(self.src, "c.txt", "c")
        with Held(self.src / "held.db", self.src / "newheld.db"):
            record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((self.dst / "held.db").read_text(), "old copy")
        self.assertFalse((self.dst / "newheld.db").exists())
        self.assertEqual(tree(self.dst), sorted(["a.txt", "c.txt", "held.db"] + [f"other{i}.txt" for i in range(10)]))
        self.assertEqual(self.versions(), ["b.txt"])
        self.assertEqual(sorted(p for p, _ in record.skipped), ["held.db", "newheld.db"])
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), tree(self.src))


# --- interruption -------------------------------------------------------------------

class InterruptionTest(RunCase):
    def test_killing_the_engine_mid_copy_leaves_no_incomplete_file_and_the_next_run_converges(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        thread, box = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        os.kill(status["engine_pid"], signal.SIGTERM)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("engine-failed",)))
        self.assertFalse((self.dst / "video.bin").exists())
        self.assertEqual(len(partials(self.dst)), 1)
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((self.dst / "video.bin").read_bytes(), (self.src / "video.bin").read_bytes())
        self.assertEqual(partials(self.dst), [])
        self.assertEqual(self.versions(), [])


# --- holds ----------------------------------------------------------------------------

class HoldTest(RunCase):
    def setUp(self):
        super().setUp()
        self.seed({f"f{i:02}.txt": f"file {i}" for i in range(20)})

    def assert_held(self, record, *reasons):
        self.assertEqual(record.outcome, Outcome.HELD, record)
        for reason in reasons:
            self.assertIn(reason, record.reasons)
        self.assertEqual(self.versions(), [])

    def test_an_emptied_source_holds_every_removal(self):
        for i in range(20):
            os.remove(self.src / f"f{i:02}.txt")
        self.assert_held(self.only(self.run_now()), "share-of-destination", "above-threshold")
        self.assertEqual(len(tree(self.dst)), 20)

    def test_a_mass_deletion_above_the_bar_holds_removals_and_copies_additions(self):
        for i in range(5):
            os.remove(self.src / f"f{i:02}.txt")
        write(self.src, "new1.txt", "n1")
        write(self.src, "new2.txt", "n2")
        self.assert_held(self.only(self.run_now()), "above-threshold")
        self.assertEqual(len(tree(self.dst)), 22)
        self.assertTrue((self.dst / "new1.txt").exists())

    def test_a_rename_storm_the_tool_cannot_recognise_holds_removals_and_copies_the_new_names(self):
        for i in range(5):
            path = self.src / f"f{i:02}.txt"
            path.rename(self.src / f"renamed{i}.txt")
            os.utime(self.src / f"renamed{i}.txt", ns=(1_700_000_000_000_000_000 + i,) * 2)
        self.assert_held(self.only(self.run_now()), "above-threshold")
        self.assertTrue((self.dst / "f00.txt").exists())
        self.assertTrue((self.dst / "renamed0.txt").exists())

    def test_a_mass_overwrite_holds_removals_and_copies_additions(self):
        for i in range(5):
            write(self.src, f"f{i:02}.txt", f"overwritten {i}")
        write(self.src, "new.txt", "n")
        self.assert_held(self.only(self.run_now()), "above-threshold")
        self.assertEqual((self.dst / "f00.txt").read_text(), "file 0")
        self.assertTrue((self.dst / "new.txt").exists())

    def test_changed_files_that_lose_their_signature_hold_removals_whatever_their_number(self):
        write(self.src, "photo.png", PNG)
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        write(self.src, "photo.png", os.urandom(len(PNG)))
        self.assert_held(self.only(self.run_now()), "lost-signatures")
        self.assertEqual((self.dst / "photo.png").read_bytes(), PNG)

    def test_held_removals_are_applied_once_the_preview_is_confirmed(self):
        for i in range(5):
            os.remove(self.src / f"f{i:02}.txt")
        self.assert_held(self.only(self.run_now()), "above-threshold")
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), tree(self.src))
        self.assertEqual(self.versions(), [f"f{i:02}.txt" for i in range(5)])


class RenameTest(RunCase):
    def test_a_tree_reorganised_by_renames_moves_nothing_into_versions_and_copies_nothing_again(self):
        self.seed({"album/one.jpg": "1" * 1000, "album/two.jpg": "2" * 2000, "album/three.jpg": "3" * 3000,
                   "docs/paper.pdf": "%PDF-" + "p" * 500, "notes.txt": "n"})
        (self.src / "sorted").mkdir()
        os.rename(self.src / "album/one.jpg", self.src / "sorted/one.jpg")
        os.rename(self.src / "album/two.jpg", self.src / "album/two_renamed.jpg")
        os.rename(self.src / "album/three.jpg", self.src / "sorted/three_renamed.jpg")
        os.rename(self.src / "docs", self.src / "papers")
        os.rename(self.src / "notes.txt", self.src / "NOTES.TXT")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((record.done["files_copied"], record.done["moved_to_versions"], record.done["renamed"]),
                         (0, 0, 5))
        self.assertIsNone(record.versions_batch)
        self.assertEqual(tree(self.dst), tree(self.src))
        self.assertEqual(folders(self.dst), folders(self.src))


# --- run control ------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "suspending a process")
class ControlTest(RunCase):
    def test_a_pause_stops_data_moving_and_resuming_completes_the_run(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        thread, box = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        request(self.layout, status["run_id"], pause=True)
        self.live(lambda st: st.get("paused"))
        partial = self.dst / growing_partial(self.dst)
        time.sleep(0.5)
        before = partial.stat().st_size
        time.sleep(1.5)
        self.assertEqual(partial.stat().st_size, before)
        request(self.layout, status["run_id"], pause=False)
        record = self.only(self.finish(thread, box))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((self.dst / "video.bin").read_bytes(), (self.src / "video.bin").read_bytes())

    def test_a_graceful_cancel_finishes_the_files_in_flight_and_starts_no_other(self):
        self.seed({"a.txt": "a"})
        names = [f"part{i}.bin" for i in range(6)]
        for name in names:
            write(self.src, name, os.urandom(4 * MiB))
        thread, box = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled-graceful",)))
        self.assertEqual(partials(self.dst), [])
        copied = [n for n in names if (self.dst / n).exists()]
        self.assertTrue(1 <= len(copied) < len(names), copied)
        for name in copied:
            self.assertEqual((self.dst / name).read_bytes(), (self.src / name).read_bytes())
        self.assertEqual(record.left["files_to_copy"], len(names) - len(copied))
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        self.assertEqual(tree(self.dst), tree(self.src))

    def test_an_immediate_cancel_leaves_no_incomplete_file_and_the_next_run_converges(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        thread, box = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        request(self.layout, status["run_id"], cancel=CancelMode.IMMEDIATE)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled-immediate",)))
        self.assertFalse((self.dst / "video.bin").exists())
        self.assertEqual(partials(self.dst), [])
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        self.assertEqual((self.dst / "video.bin").read_bytes(), (self.src / "video.bin").read_bytes())


@unittest.skipUnless(WINDOWS, "suspending a process")
class StalledEngineTest(RunCase):
    """An engine that makes no progress for the bound is ended and the destination's run
    fails saying why; its turn is released and the next run completes. The engine is
    frozen from outside, so its remote control does not answer either."""

    def test_an_engine_frozen_past_the_bound_is_ended_and_the_next_run_completes(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        with mock.patch.object(apply, "STALL_LIMIT", 3.0), mock.patch.object(apply, "STALL_LOOK", 0.5):
            thread, box = self.in_background(bandwidth_limit="2M")
            status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
            _Windows.get().suspend(status["engine_pid"])
            record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("engine-failed",)))
        self.assertTrue(record.detail.startswith("the engine made no progress for "), record.detail)
        self.assertFalse((self.dst / "video.bin").exists())
        self.assertEqual(observe_runs(self.layout), [], "the run let its turn go")
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)
        self.assertEqual((self.dst / "video.bin").read_bytes(), (self.src / "video.bin").read_bytes())
        self.assertEqual(partials(self.dst), [])


class UnexpectedFailureTest(RunCase):
    def test_a_run_that_fails_outside_a_pair_records_each_destination_and_logs_why(self):
        write(self.src, "a.txt", "a")

        class Failing(FixedVolumeSource):
            def scan(self):
                raise RuntimeError("the volumes could not be enumerated")

        with diagnostics.process_log(self.layout, "run") as log:
            result = run_job(self.store, self.job.job_id, RunOptions(), volume_source=Failing(()), sync_roots=())
        record = self.only(result)
        self.assertEqual((record.outcome, record.reasons, record.detail),
                         (Outcome.FAILED, ("unexpected-error",), "RuntimeError: the volumes could not be enumerated"))
        self.assertEqual(read_records(self.layout, self.job.job_id), [record])
        text = log.read_text(encoding="utf-8")
        self.assertIn(f"run {result.run_id}: failed unexpectedly", text)
        self.assertIn("Traceback", text)
        self.assertEqual(observe_runs(self.layout), [])


class GlobalLockTest(RunCase):
    def test_a_run_records_an_earlier_run_that_died(self):
        write(self.src, "a.txt", "a")
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            status = LiveStatus("d" * 32, os.getpid(), {self.job.job_id!r}, "Work", "manual", "2026-10-06T10:00:00Z",
                                pair_index=1, marker_id={self.job.destinations[0].marker_id!r},
                                pair_started="2026-10-06T10:00:01Z")
            StatusBoard(RuntimeLayout(Path({str(self.layout.root)!r})), status).open()
            os._exit(0)
        """)
        died = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, creationflags=FLAGS,
                              timeout=60)
        self.assertEqual(died.returncode, 0, died.stderr)
        self.run_now()
        outcomes = [r.outcome for r in read_records(self.layout, self.job.job_id)]
        self.assertIn(Outcome.INTERRUPTED, outcomes)
        self.assertEqual(observe_runs(self.layout), [])


# --- large libraries: names, files found identical, control before the copy -----------------

def identical_tree(src, dst, names):
    """Each file on both sides with the same contents, the destination's an hour newer: an
    earlier copy that did not keep the times."""
    for name in names:
        data = b"\xff\xd8\xff" + name.encode("utf-8")
        write(src, name, data)
        target = write(dst, name, data)
        stamp = os.stat(Path(src) / name).st_mtime_ns + 3600 * 10 ** 9
        os.utime(target, ns=(stamp, stamp))


class DestinationsByMarkerTest(RunCase):
    """A run or a preview of destinations named by marker: each found in the job as the run
    or the preview reads it, wherever the job's order puts it."""

    def second_saved_first(self):
        """Work given a second destination, saved ahead of the first: that destination."""
        second = Destination(endpoint(self.d, "Second"))
        self.job = replace(self.job, destinations=(second,) + self.job.destinations)
        self.store.save(StoreContents((self.job,)))
        return second

    def test_a_run_serves_the_destination_its_marker_names_at_the_number_the_job_gives_it(self):
        write(self.src, "a.txt", "a")
        second = self.second_saved_first()
        record = self.only(self.run_now(markers=(second.marker_id,)))
        self.assertEqual((record.marker_id, record.destination_index), (second.marker_id, 1))

    def test_a_preview_shows_the_destinations_its_markers_name(self):
        write(self.src, "a.txt", "a")
        self.second_saved_first()
        backup = self.job.destinations[1].marker_id
        preview = preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                              markers=(backup,))
        self.assertEqual([p.assessment.destination.marker_id for p in preview.pairs], [backup])

    def test_a_marker_the_job_does_not_hold_is_refused_and_nothing_runs(self):
        with self.assertRaises(RunError) as raised:
            self.run_now(markers=("f" * 32,))
        self.assertIn("the job has no destination with marker " + "f" * 32, str(raised.exception))
        self.assertEqual(read_records(self.layout, self.job.job_id), [])

    def test_destinations_are_named_by_number_or_by_marker_not_both(self):
        with self.assertRaises(RunError) as raised:
            self.run_now(destinations=(1,), markers=(self.job.destinations[0].marker_id,))
        self.assertIn("by number or by marker, not both", str(raised.exception))


class ParallelRunsTest(RunCase):
    """The maintainer's design of 2026-10-09 for runs at the same time: runs whose folders do not
    conflict work at the same time, those on one drive only by the user's choice; the others
    wait their turn, oldest first, and say for whom; two runs never overcommit one drive's free space.

    Three jobs share the two volumes: Work (S\\Work to D\\Backup), Photos (S\\Photos to
    D\\PhotoBackup) and Outer, whose source is the whole of D — Work's destination lies
    inside it — copied to S\\OuterCopy."""

    def setUp(self):
        super().setUp()
        self.photos_src = self.vol_s / "Photos"
        self.photos_src.mkdir()
        self.photos = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "PhotoBackup")],
                              holds=self.holds)
        self.outer = new_job("Outer", endpoint(self.d), [endpoint(self.s, "OuterCopy")], holds=self.holds)
        self.store.save(StoreContents((self.job, self.photos, self.outer)))

    def start(self, job, **options):
        box = {}

        def target():
            try:
                box["result"] = run_job(self.store, job.job_id, RunOptions(**options), volume_source=self.volumes,
                                        sync_roots=())
            except BaseException as exc:  # handed to the test thread by finish()
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box

    def of(self, job, predicate, timeout=60):
        return self.live(lambda st: st.get("job_id") == job.job_id and predicate(st), timeout)

    def confirm_job(self, job):
        ticket = preview_job(self.store, job.job_id, volume_source=self.volumes, sync_roots=()).ticket
        record = self.only(run_job(self.store, job.job_id, RunOptions(ticket_id=ticket.ticket_id),
                                   volume_source=self.volumes, sync_roots=()))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)

    def waiting_for(self, job, other_id):
        """The status of ``job``'s run once it waits for ``other_id``."""
        return self.of(job, lambda st: st.get("phase") == "waiting"
                       and (st.get("waiting_for") or {}).get("run_id") == other_id)

    def at_the_same_time(self):
        """The user's choice that runs on one drive work at the same time."""
        self.store.save(replace(self.store.load().contents,
                                preferences=Preferences(activities=Activities(DriveTurns.AT_THE_SAME_TIME))))

    def test_two_runs_on_disjoint_folders_work_at_the_same_time(self):
        self.at_the_same_time()
        self.seed({"a.txt": "a"})
        write(self.photos_src, "p.txt", "p")
        self.confirm_job(self.photos)
        write(self.src, "video.bin", os.urandom(8 * MiB))
        write(self.photos_src, "raw.bin", os.urandom(8 * MiB))
        work, photos = self.start(self.job, bandwidth_limit="2M"), self.start(self.photos, bandwidth_limit="2M")

        def copying_together():
            copying = {r.status.get("job_id") for r in observe_runs(self.layout)
                       if r.alive and r.status.get("engine_pid") and r.status.get("admitted")}
            return {self.job.job_id, self.photos.job_id} <= copying

        self.assertTrue(wait_for(copying_together, timeout=60), "the two runs never copied at the same time")
        for thread, box in (work, photos):
            self.assertEqual(self.only(self.finish(thread, box)).outcome, Outcome.COMPLETED)
        self.assertEqual((self.vol_d / "PhotoBackup" / "raw.bin").read_bytes(),
                         (self.photos_src / "raw.bin").read_bytes())

    def test_a_second_run_of_the_same_job_waits_for_the_first_and_starts_when_it_ends(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        first = self.in_background(bandwidth_limit="2M")
        working = self.live(lambda st: st.get("engine_pid"))
        second = self.in_background()
        waiting = self.waiting_for(self.job, working["run_id"])
        self.assertEqual(WaitedFor.from_status(waiting), WaitedFor(working["run_id"], "run", "Work"))
        done = self.only(self.finish(*first))
        self.assertEqual(done.outcome, Outcome.COMPLETED)
        record = self.only(self.finish(*second))
        self.assertEqual(record.outcome, Outcome.COMPLETED)
        self.assertGreaterEqual(record.started, done.ended)

    def test_a_run_publishes_the_destinations_it_serves(self):
        write(self.src, "a.txt", "a")
        other = live_activity(self.layout, self.job.job_id, job_name="Work", reads=[endpoint(self.s, "Work")],
                              writes=[endpoint(self.d, "Backup")])
        self.addCleanup(other.close)
        thread, box = self.in_background(destinations=(1,))
        status = self.waiting_for(self.job, other.status.run_id)
        self.assertEqual(status["selected"], [1])
        self.assertEqual(status["markers"], [d.marker_id for d in self.job.destinations],
                         "what each number names, whatever the job's order becomes while it works")
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertEqual(record.reasons, ("cancelled-while-waiting",))

    def waiting_for_the_second_of_two(self):
        """Work given a second destination, and a run of it alone waiting its turn: the
        destination, and the run's thread and box."""
        write(self.src, "a.txt", "a")
        second = Destination(endpoint(self.d, "Second"))
        self.job = replace(self.job, destinations=self.job.destinations + (second,))
        self.store.save(StoreContents((self.job, self.photos, self.outer)))
        other = live_activity(self.layout, self.job.job_id, job_name="Work", reads=[endpoint(self.s, "Work")],
                              writes=[endpoint(self.d, "Backup")])
        self.addCleanup(other.close)
        thread, box = self.in_background(destinations=(2,))
        self.assertEqual(self.waiting_for(self.job, other.status.run_id)["selected"], [2])
        return second, other, thread, box

    def test_a_run_of_some_destinations_keeps_them_when_the_job_is_reordered_while_it_waits(self):
        second, other, thread, box = self.waiting_for_the_second_of_two()
        reordered = replace(self.job, destinations=tuple(reversed(self.job.destinations)))
        self.store.save(StoreContents((reordered, self.photos, self.outer)))
        other.close()
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.marker_id, record.destination_index), (second.marker_id, 1),
                         "the destination asked for, at the number the job now gives it")

    def test_a_run_of_a_destination_removed_while_it_waits_stops(self):
        _second, other, thread, box = self.waiting_for_the_second_of_two()
        self.store.save(StoreContents((replace(self.job, destinations=self.job.destinations[:1]), self.photos,
                                       self.outer)))
        other.close()
        with self.assertRaises(RunError) as raised:
            self.finish(thread, box)
        self.assertIn("removed from the job while it waited", str(raised.exception))
        self.assertEqual(read_records(self.layout, self.job.job_id), [], "nothing was run")

    def test_a_run_waiting_its_turn_can_be_cancelled_and_starts_once_the_other_ends(self):
        write(self.src, "a.txt", "a")
        other = live_activity(self.layout, self.job.job_id, job_name="Work", reads=[endpoint(self.s, "Work")],
                              writes=[endpoint(self.d, "Backup")])
        self.addCleanup(other.close)
        thread, box = self.in_background()
        status = self.waiting_for(self.job, other.status.run_id)
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled-while-waiting",)))
        thread, box = self.in_background()
        self.waiting_for(self.job, other.status.run_id)
        other.close()
        self.assertEqual(self.only(self.finish(thread, box)).outcome, Outcome.HELD)   # a first run, unconfirmed

    def test_a_run_whose_destination_lies_in_another_jobs_source_waits_and_so_does_that_job(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        work = self.in_background(bandwidth_limit="2M")
        working = self.live(lambda st: st.get("engine_pid"))
        outer = self.start(self.outer)
        self.waiting_for(self.outer, working["run_id"])
        self.assertEqual(self.only(self.finish(*work)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.only(self.finish(*outer)).outcome, Outcome.HELD)       # a first run, unconfirmed

        ticket = preview_job(self.store, self.outer.job_id, volume_source=self.volumes, sync_roots=()).ticket
        outer = self.start(self.outer, ticket_id=ticket.ticket_id, bandwidth_limit="2M")
        working = self.of(self.outer, lambda st: st.get("engine_pid"))
        write(self.src, "b.txt", "b")
        work = self.in_background()
        self.waiting_for(self.job, working["run_id"])
        self.assertEqual(self.only(self.finish(*outer)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.only(self.finish(*work)).outcome, Outcome.COMPLETED)
        self.assertEqual((self.vol_s / "OuterCopy" / "Backup" / "video.bin").read_bytes(),
                         (self.src / "video.bin").read_bytes())

    def test_a_paused_run_blocks_only_the_runs_it_conflicts_with(self):
        self.at_the_same_time()
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        first = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        request(self.layout, status["run_id"], pause=True)
        self.live(lambda st: st.get("run_id") == status["run_id"] and st.get("paused"))
        second = self.in_background()
        waiting = self.of(self.job, lambda st: (st.get("waiting_for") or {}).get("paused"))
        self.assertEqual(waiting["waiting_for"]["run_id"], status["run_id"])
        write(self.photos_src, "p.txt", "p")
        photos = self.only(run_job(self.store, self.photos.job_id, RunOptions(), volume_source=self.volumes,
                                   sync_roots=()))
        self.assertEqual(photos.outcome, Outcome.HELD)                                  # it ran: a first run, held
        self.assertTrue(self.of(self.job, lambda st: st.get("run_id") == waiting["run_id"]
                                and st.get("phase") == "waiting"))
        request(self.layout, status["run_id"], pause=False)
        self.assertEqual(self.only(self.finish(*first)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.only(self.finish(*second)).outcome, Outcome.COMPLETED)

    def test_conflicting_waiters_start_oldest_first(self):
        holder = live_activity(self.layout, "h" * 32, job_name="Holder", reads=[endpoint(self.d, "Elsewhere")],
                               writes=[endpoint(self.s, "Work")])
        self.addCleanup(holder.close)
        write(self.src, "a.txt", "a")
        work = self.in_background()
        waiting = self.waiting_for(self.job, holder.status.run_id)
        outer = self.start(self.outer)                  # it conflicts with Work's run, not with the holder
        later = self.waiting_for(self.outer, waiting["run_id"])
        self.assertEqual(WaitedFor.from_status(later), WaitedFor(waiting["run_id"], "run", "Work", waiting=True))
        holder.close()
        first = self.only(self.finish(*work))
        second = self.only(self.finish(*outer))
        self.assertEqual((first.outcome, second.outcome), (Outcome.HELD, Outcome.HELD))  # first runs, unconfirmed
        self.assertGreaterEqual(second.started, first.ended)

    def test_a_waiting_run_survives_the_job_file_saved_by_another_process(self):
        write(self.src, "a.txt", "a")
        holder = live_activity(self.layout, "h" * 32, job_name="Holder", reads=[endpoint(self.d, "Elsewhere")],
                               writes=[endpoint(self.s, "Work")])
        self.addCleanup(holder.close)
        thread, box = self.in_background()
        self.waiting_for(self.job, holder.status.run_id)
        saved = result_of(self, job_file_saver(self, self.store.path, 3.0))
        self.assertTrue(thread.is_alive(), "the waiting run ended while the job file was saved")
        holder.close()
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, saved["failures"]), (Outcome.HELD, 0))     # a first run, unconfirmed
        self.assertGreater(saved["saves"], 20)

    def test_two_runs_to_one_drive_cannot_overcommit_its_free_space(self):
        self.at_the_same_time()
        self.seed({"a.txt": "a"})
        write(self.photos_src, "p.txt", "p")
        self.confirm_job(self.photos)
        write(self.photos_src, "raw.bin", os.urandom(8 * MiB))
        write(self.src, "b.txt", "b")
        self.volumes = FixedVolumeSource((self.s, replace(self.d, free_bytes=GIB + 16 * MiB)))
        photos = self.start(self.photos, bandwidth_limit="2M")
        copying = self.of(self.photos, lambda st: st.get("engine_pid"))
        # Paused before its file is copied, Photos still has its share to write when Work's
        # space check reads it.
        request(self.layout, copying["run_id"], pause=True)
        try:
            held = self.of(self.photos, lambda st: st.get("paused") or (st.get("done") or {}).get("files_copied"))
            self.assertEqual(held["done"]["files_copied"], 0, "the pause landed after Photos had copied its file")
            record = self.only(self.run_now())
        finally:
            request(self.layout, copying["run_id"], pause=False)
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("insufficient-space",)))
        self.assertIn("still to be written by other runs in progress", record.detail)
        self.assertEqual(self.only(self.finish(*photos)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.only(self.run_now()).outcome, Outcome.COMPLETED)


class OneDriveAtATimeTest(RunCase):
    """The maintainer's decision of 2026-10-13, at its default: runs that use one drive — to
    read or to write — run one after the other, the later naming whom it waits for, the
    drive they share and its place in line; runs on other drives work beside them; two
    volumes on one disk are one drive.

    Work (S\\Work to D\\Backup), Photos and Third share both volumes; Apart copies E\\Apart to
    F\\Apart, two more volumes."""

    def setUp(self):
        super().setUp()
        self.vol_e, self.vol_f = self.tmp / "volE", self.tmp / "volF"
        for folder in (self.vol_s / "Photos", self.vol_s / "Third", self.vol_e / "Apart", self.vol_f):
            folder.mkdir(parents=True)
        self.e = volume(3, mounts=(str(self.vol_e) + "\\",), label="E")
        self.f = volume(4, mounts=(str(self.vol_f) + "\\",), label="F")
        self.volumes = FixedVolumeSource((self.s, self.d, self.e, self.f))
        self.photos = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "PhotoBackup")],
                              holds=self.holds)
        self.third = new_job("Third", endpoint(self.s, "Third"), [endpoint(self.d, "ThirdBackup")], holds=self.holds)
        self.apart = new_job("Apart", endpoint(self.e, "Apart"), [endpoint(self.f, "Apart")], holds=self.holds)
        self.store.save(StoreContents((self.job, self.photos, self.third, self.apart)))

    def start(self, job, **options):
        box = {}

        def target():
            try:
                box["result"] = run_job(self.store, job.job_id, RunOptions(**options), volume_source=self.volumes,
                                        sync_roots=())
            except BaseException as exc:  # handed to the test thread by finish()
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box

    def of(self, job, predicate, timeout=60):
        return self.live(lambda st: st.get("job_id") == job.job_id and predicate(st), timeout)

    def working(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        work = self.in_background(bandwidth_limit="2M")
        return work, self.live(lambda st: st.get("job_id") == self.job.job_id and st.get("engine_pid"))

    def test_runs_on_one_drive_run_one_after_the_other_and_each_says_whom_it_waits_for_and_its_place(self):
        write(self.vol_s / "Photos", "p.txt", "p")
        write(self.vol_s / "Third", "t.txt", "t")
        work, working = self.working()
        photos = self.start(self.photos)
        waiting = self.of(self.photos, lambda st: st.get("phase") == "waiting" and st.get("place") == 1)
        self.assertEqual(WaitedFor.from_status(waiting), WaitedFor(working["run_id"], "run", "Work", drive="drive 'S'"))
        third = self.start(self.third)
        later = self.of(self.third, lambda st: st.get("phase") == "waiting" and st.get("place") == 2)
        self.assertEqual(later["waiting_for"]["run_id"], working["run_id"])
        done = self.only(self.finish(*work))
        first, second = self.only(self.finish(*photos)), self.only(self.finish(*third))
        self.assertEqual((done.outcome, first.outcome, second.outcome),
                         (Outcome.COMPLETED, Outcome.HELD, Outcome.HELD))             # first runs, unconfirmed
        self.assertGreaterEqual(first.started, done.ended)
        self.assertGreaterEqual(second.started, first.ended)

    def test_a_run_on_other_drives_works_beside_them(self):
        write(self.vol_e / "Apart", "raw.bin", os.urandom(8 * MiB))
        work, working = self.working()

        def copying_together():
            copying = {r.status.get("job_id") for r in observe_runs(self.layout)
                       if r.alive and r.status.get("engine_pid") and r.status.get("admitted")}
            return {self.job.job_id, self.apart.job_id} <= copying

        # Paused, Work keeps its turn and its drives as a working run does, and cannot end
        # before Apart has been judged beside it.
        request(self.layout, working["run_id"], pause=True)
        try:
            held = self.live(lambda st: st.get("run_id") == working["run_id"]
                             and (st.get("paused") or (st.get("done") or {}).get("files_copied")))
            self.assertTrue(held.get("paused") and held.get("engine_pid"), "the pause landed after Work had copied its file")
            apart = self.start(self.apart, first_backup=True, bandwidth_limit="2M")
            self.assertTrue(wait_for(copying_together, timeout=60), "the two runs never copied at the same time")
        finally:
            request(self.layout, working["run_id"], pause=False)
        self.assertEqual(self.only(self.finish(*work)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.only(self.finish(*apart)).outcome, Outcome.COMPLETED)

    def test_two_volumes_on_one_disk_are_one_drive(self):
        disk = "disk:gpt:00000000-0000-4000-8000-0000000000d1"
        self.s, self.e = replace(self.s, disk=disk), replace(self.e, disk=disk)
        self.volumes = FixedVolumeSource((self.s, self.d, self.e, self.f))
        write(self.vol_e / "Apart", "a.txt", "a")
        work, working = self.working()
        apart = self.start(self.apart, first_backup=True)
        # A run's first status, published before it asks for its turn, waits for no one yet.
        waiting = self.of(self.apart, lambda st: st.get("phase") == "waiting" and st.get("waiting_for"))
        self.assertEqual(WaitedFor.from_status(waiting), WaitedFor(working["run_id"], "run", "Work", drive="drive 'S'"))
        done = self.only(self.finish(*work))
        after = self.only(self.finish(*apart))
        self.assertEqual((after.outcome, after.started >= done.ended), (Outcome.COMPLETED, True))


@needs_engine
@unittest.skipUnless(WINDOWS, "this machine's real volume source")
class OneRealDiskTest(TempDirTest):
    """Two jobs on temporary folders of the system drive, through this machine's real volume
    source (read-only): one disk, so with turns on every drive chosen the second waits for the
    first, naming the drive by its letter. Under the default choice the outcome would hang on
    the kind the system drive reports, which the volume tests bind apart."""

    def test_two_jobs_on_one_disk_run_one_after_the_other(self):
        source = default_volume_source()
        volumes = source.scan().volumes
        for name in ("A", "B"):
            write(self.tmp / name, f"{name}.bin", os.urandom(8 * MiB if name == "A" else 1024))
        jobs = [new_job(name, endpoint_from_path(self.tmp / name, volumes),
                        [endpoint_from_path(self.tmp / f"Backup{name}", volumes)]) for name in ("A", "B")]
        store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        store.save(StoreContents(tuple(jobs), Preferences(activities=Activities(DriveTurns.ONE_AT_A_TIME))))
        layout = runtime_layout(store.path)
        boxes = [{}, {}]

        def run(job, box, **options):
            try:
                box["result"] = run_job(store, job.job_id, RunOptions(first_backup=True, **options),
                                        volume_source=source, sync_roots=())
            except BaseException as exc:  # handed to the test thread below
                box["error"] = exc

        def live(predicate):
            return next((r.status for r in observe_runs(layout) if r.alive and predicate(r.status)), None)

        first = threading.Thread(target=run, args=(jobs[0], boxes[0]), kwargs={"bandwidth_limit": "2M"}, daemon=True)
        first.start()
        working = wait_for(lambda: live(lambda st: st.get("job_id") == jobs[0].job_id and st.get("engine_pid")),
                           timeout=60)
        self.assertIsNotNone(working, "the first run never copied")
        second = threading.Thread(target=run, args=(jobs[1], boxes[1]), daemon=True)
        second.start()
        waiting = wait_for(lambda: live(lambda st: st.get("job_id") == jobs[1].job_id
                                        and (st.get("waiting_for") or {}).get("run_id") == working["run_id"]),
                           timeout=60)
        self.assertIsNotNone(waiting, "the second run never waited for the first")
        drive = find_by_mount(volumes, str(self.tmp))[0].drive
        self.assertEqual(WaitedFor.from_status(waiting).drive, drive_name(volumes, drive))
        self.assertRegex(WaitedFor.from_status(waiting).drive, r"^(drive [A-Z]:|the drive of .+)$")
        for thread in (first, second):
            thread.join(180)
            self.assertFalse(thread.is_alive(), "a run did not end")
        for box in boxes:
            self.assertNotIn("error", box)
        done, after = (box["result"].records[0] for box in boxes)
        self.assertEqual((done.outcome, after.outcome), (Outcome.COMPLETED, Outcome.COMPLETED))
        self.assertGreaterEqual(after.started, done.ended)


@unittest.skipUnless(WINDOWS, "Windows names")
class StandInNamesTest(RunCase):
    def setUp(self):
        super().setUp()
        self.src = self.vol_s / "\u3082\u3068\uff1a\u3057\u3083\u3057\u3093"
        self.dst = self.vol_d / "\u3072\u304b\u3048\uff1a\u3057\u3083\u3057\u3093"
        self.src.mkdir()
        source = endpoint(self.s, "\u3082\u3068\uff1a\u3057\u3083\u3057\u3093")
        destination = endpoint(self.d, "\u3072\u304b\u3048\uff1a\u3057\u3083\u3057\u3093")
        self.job = new_job("Photos", source, [destination], holds=self.holds)
        self.store.save(StoreContents((self.job,)))

    def test_names_and_roots_with_stand_ins_are_backed_up_under_their_own_names(self):
        self.seed({name: name for name in CARRIED_NAMES})
        self.assertEqual(os.listdir(self.vol_d), ["\u3072\u304b\u3048\uff1a\u3057\u3083\u3057\u3093"])
        write(self.src, "\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8.jpg", "edited, and longer")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(self.versions(), ["\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8.jpg"])
        edited = (self.dst / "\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8.jpg").read_text(encoding="utf-8")
        self.assertEqual(edited, "edited, and longer")


@unittest.skipUnless(WINDOWS, "Windows names")
class NamesLeftOutTest(RunCase):
    LEFT_OUT = ("trailing space.jpg ", "trailing period.jpg.", "\u201bquote.jpg", "Album \\in.jpg", "bad\udc80name.jpg")

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.tmp), True)

    def test_names_a_run_cannot_keep_are_reported_and_never_written_under_another_name(self):
        write(self.src, "ok.jpg", "ok")
        for name in self.LEFT_OUT:
            write_verbatim(self.src, name, b"photo")
        os.mkdir("\\\\?\\" + str(self.src / "Empty "))
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(sorted(os.listdir("\\\\?\\" + str(self.dst))), [".mirrorhold", "ok.jpg"])
        expected = sorted(self.LEFT_OUT + ("Empty ",))
        self.assertEqual(sorted(p for p, _ in record.skipped), expected)
        self.assertTrue(all(why for _, why in record.skipped))
        self.assertEqual(sorted(record.not_backed_up), [(p, 1) for p in expected])
        again = self.only(self.run_now())
        self.assertEqual(again.outcome, Outcome.COMPLETED, again)
        self.assertEqual(sorted(again.repeatedly_unbacked), [(p, 2) for p in expected])


class IdenticalFilesTest(RunCase):
    def test_an_adoption_over_identical_files_records_them_and_leaves_nothing(self):
        names = [f"album{i % 3}\\IMG_{i:03d}.JPG" for i in range(30)]
        identical_tree(self.src, self.dst, names)
        write(self.src, "differs.jpg", b"\xff\xd8\xffnew")
        old = write(self.dst, "differs.jpg", b"\xff\xd8\xffold")
        os.utime(old, ns=(1_600_000_000_000_000_000,) * 2)
        write(self.src, "new.jpg", "new")
        plan = self.preview().pairs[0].assessment.plan
        self.assertEqual((len(plan.changed), plan.same_size_changed), (31, 31))
        record = self.confirm()
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((record.done["files_copied"], record.done["files_identical"]), (2, 30))
        self.assertEqual(record.done["bytes_identical"], sum(os.stat(self.src / n).st_size for n in names))
        self.assertEqual(record.left, dict.fromkeys(record.left, 0))
        self.assertEqual(record.skipped, ())
        self.assertEqual(self.versions(), ["differs.jpg"])
        for name in names:
            self.assertEqual(os.stat(self.dst / name).st_mtime_ns, os.stat(self.src / name).st_mtime_ns)
        again = self.only(self.run_now())
        self.assertEqual((again.outcome, sum(again.done.values())), (Outcome.COMPLETED, 0))

    def test_a_graceful_cancel_while_the_engine_compares_ends_promptly(self):
        # Enough files that the comparison outlasts the engine's own delay before it quits
        # (1.5 s) and the run's way to it, with seconds to spare should this side pause:
        # the engine compares about ten thousand a second (measured on the development machine).
        names = [f"d{i % 100}\\IMG_{i:05d}.JPG" for i in range(70000)]
        identical_tree(self.src, self.dst, names)
        ticket = self.preview().ticket
        thread, box = self.in_background(ticket_id=ticket.ticket_id)
        status = self.live(lambda st: st.get("comparing"), timeout=180)
        asked = time.monotonic()
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertLess(time.monotonic() - asked, 15)
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled-graceful",)))
        self.assertLess(record.done["files_identical"], len(names))
        self.assertEqual((record.done["files_copied"], partials(self.dst)), (0, []))


@unittest.skipUnless(WINDOWS, "suspending a process")
class ControlBeforeTheCopyTest(RunCase):
    def test_a_pause_asked_while_waiting_holds_the_run_at_its_listing_and_a_cancel_ends_it(self):
        write(self.src, "a.txt", "a")
        other = live_activity(self.layout, self.job.job_id, job_name="Work", reads=[endpoint(self.s, "Work")],
                              writes=[endpoint(self.d, "Backup")])
        self.addCleanup(other.close)
        thread, box = self.in_background()
        status = self.live(lambda st: st.get("phase") == "waiting")
        request(self.layout, status["run_id"], pause=True)
        other.close()
        self.live(lambda st: st.get("phase") == Phase.LISTING.value and st.get("paused"))
        time.sleep(1.0)
        self.live(lambda st: st.get("phase") == Phase.LISTING.value and st.get("paused"), timeout=1)
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertEqual((record.outcome, record.reasons), (Outcome.CANCELLED, ("cancelled",)))
        self.assertFalse(self.dst.exists())


@needs_engine
class ApplyStepTest(TempDirTest):
    """``run.apply.apply_pair`` on its own: the steps around the engine's copy."""

    def setUp(self):
        super().setUp()
        self.src, self.dst = self.tmp / "s", self.tmp / "d"
        for i in range(10):
            write(self.src, f"keep{i}.txt", "k")
            kept = write(self.dst, f"keep{i}.txt", "k")
            os.utime(kept, ns=(os.stat(self.src / f"keep{i}.txt").st_mtime_ns,) * 2)
        self.status = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-09T10:00:00Z", pair_index=1)
        self.board = StatusBoard(RuntimeLayout(self.tmp / "state"), self.status)
        self.board.open()
        self.addCleanup(self.board.close)
        self.control = RunControl(self.status)
        self.pump = StatusPump(self.board, self.control)

    def plan(self):
        plan = build_plan(list_tree(self.src, Side.SOURCE, drive_root=False),
                          list_tree(self.dst, Side.DESTINATION, drive_root=False), trust_temporaries=True)
        decision = judge(plan, holds=HoldThresholds(count_floor=1000, percent=100), first_run=False,
                         destination=volume(2, letter="E"))
        self.assertTrue(decision.applies_removals)
        return plan, decision

    def apply(self, plan, decision, engine):
        return apply_pair(plan, decision, source_root=self.src, destination_root=self.dst,
                          batch=VersionsBatch(self.dst, datetime.now(timezone.utc)), engine=engine,
                          control=self.control, pump=self.pump, list_path=self.tmp / "state" / "list.files")

    def test_a_listed_file_the_engine_passes_over_is_recorded_as_skipped(self):
        write(self.src, "a.txt", "a")
        gone = write(self.src, "gone.txt", "g")
        plan, decision = self.plan()
        self.assertEqual(sorted(e.path for e in plan.new), ["a.txt", "gone.txt"])
        os.remove(gone)                                   # after planning: the engine finds no such file
        result = self.apply(plan, decision, open_engine())
        self.assertEqual((result.files_copied, result.engine_failure, result.cancelled), (1, None, None))
        self.assertEqual(result.skipped, [("gone.txt", PASSED_OVER)])

    def test_a_pause_and_a_cancel_are_honoured_while_changed_files_are_checked(self):
        for i in range(3):
            write(self.src, f"c{i}.txt", f"new {i}")
            write(self.dst, f"c{i}.txt", "old")
        plan, decision = self.plan()
        self.control.update(Wanted(paused=True))
        box = {}
        never_started = Engine(self.tmp / "none.exe", "v0")
        thread = threading.Thread(target=lambda: box.update(result=self.apply(plan, decision, never_started)),
                                  daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: self.status.phase is Phase.CHECKING and self.status.paused))
        self.assertEqual((self.status.examined, self.status.to_examine), (0, 3))
        self.control.update(Wanted(paused=True, cancel=CancelMode.GRACEFUL))
        thread.join(30)
        result = box["result"]
        self.assertEqual((result.cancelled, result.files_copied, result.engine_failure), (CancelMode.GRACEFUL, 0, None))
        self.assertIsNone(self.status.engine_pid)
        self.assertEqual([(self.dst / f"c{i}.txt").read_text() for i in range(3)], ["old"] * 3)

    @unittest.skipUnless(WINDOWS, "Windows names")
    def test_a_tree_of_empty_folders_named_with_a_trailing_space_or_period_is_removed_not_moved(self):
        long = "\\\\?\\"
        self.addCleanup(shutil.rmtree, long + str(self.tmp), ignore_errors=True)
        for name in ("Old", "Older"):
            os.makedirs(long + str(self.dst / name / "Album " / "Dots."))
        plan, decision = self.plan()
        self.assertEqual(sorted(plan.deleted_dirs), ["Old", "Older"])
        result = self.apply(plan, decision, open_engine())
        self.assertEqual((result.moved_to_versions, result.versions_batch, result.errors), (0, None, []))
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), [f"keep{i}.txt" for i in range(10)])

    @unittest.skipUnless(WINDOWS, "Windows names")
    def test_a_folder_holding_a_name_a_run_cannot_keep_stays_with_that_file(self):
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.tmp), ignore_errors=True)
        write(self.dst, "Old\\a.txt", "a")
        write_verbatim(self.dst, "Old\\stray.jpg.", b"photo")
        plan, decision = self.plan()
        self.assertEqual(([e.path for e in plan.kept], plan.deleted_dirs), (["Old\\stray.jpg."], ()))
        result = self.apply(plan, decision, open_engine())
        self.assertEqual((result.moved_to_versions, result.errors), (1, []))
        self.assertEqual(os.listdir("\\\\?\\" + str(self.dst / "Old")), ["stray.jpg."])
        self.assertTrue((self.dst / result.versions_batch / "Old" / "a.txt").is_file())


class _Engine:
    """A stand-in for a running engine: its figures as the test sets them, a pause, and the
    stop the bound asks for."""

    def __init__(self):
        self.figures = TransferStats(0, 0, 0, 0, 0.0, None)
        self.suspended = False
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def stats(self):
        return None if self.suspended else self.figures

    def stop_now(self):
        self.stopped = True


class _Still:
    """A run control nobody asks anything of."""

    wanted = Wanted()

    def wait_change(self, timeout):
        pass


class StallBoundTest(unittest.TestCase):
    """The bound on an engine making no progress (``run.apply.STALL_LIMIT``): any change of
    its figures is progress, a pause and the computer asleep are not counted, figures that
    cannot be read are no progress, and under a cancel asked for the pair stays cancelled. A
    clock the test moves stands in for time."""

    def follow(self, engine, step, *, control=None, result=None, tick=60.0):
        """Follow ``engine`` while ``step(now)`` changes it at each turn of the loop, the clock
        moving ``tick(now)`` seconds a turn (a number: the same each turn); the minutes it went
        for and why it was ended (None: it ended by itself, or a cancel stands)."""
        now = [0.0]

        def clock():
            now[0] += tick(now[0]) if callable(tick) else tick
            step(now[0])
            if now[0] > 6 * 3600:
                engine.stopped = True
            return now[0]

        why = apply._follow(engine, control or _Still(),
                            LiveStatus("a" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-21T10:00:00Z"),
                            result or apply.ApplyResult(), clock=clock)
        return now[0] / 60, why

    def test_an_engine_with_no_progress_for_thirty_minutes_is_ended_saying_why(self):
        engine = _Engine()
        minutes, why = self.follow(engine, lambda now: None)
        self.assertTrue(engine.stopped)
        self.assertEqual(why, "the engine made no progress for 30 minutes (no byte copied, no file checked) and was "
                              "ended; the next run tries again")
        self.assertLessEqual(minutes, 32)

    def test_each_figure_that_moves_is_progress(self):
        for figure in ("bytes", "transfers", "checks", "errors", "identical", "io_bytes"):
            engine = _Engine()

            def step(now, engine=engine, figure=figure):
                if now <= 3 * 3600:
                    engine.figures = replace(engine.figures, **{figure: int(now // 1200)})

            minutes, why = self.follow(engine, step)
            self.assertGreaterEqual(minutes, 3 * 60 + 30, figure)
            self.assertIsNotNone(why, figure)

    def test_a_pause_is_not_counted_and_figures_that_cannot_be_read_are_no_progress(self):
        paused = _Engine()
        minutes, why = self.follow(paused, lambda now: setattr(paused, "suspended", now < 2 * 3600))
        self.assertGreaterEqual(minutes, 2 * 60 + 30)
        self.assertIsNotNone(why)
        unreadable = _Engine()
        unreadable.stats = lambda: None
        minutes, why = self.follow(unreadable, lambda now: None)
        self.assertLessEqual(minutes, 32)
        self.assertIsNotNone(why)

    def test_time_the_computer_slept_is_not_counted(self):
        engine = _Engine()
        minutes, why = self.follow(engine, lambda now: None, tick=lambda now: 3 * 3600 if now == 600 else 60)
        self.assertGreaterEqual(minutes, 3 * 60 + 30, "ten minutes before the sleep, the rest after it")
        self.assertLessEqual(minutes, 3 * 60 + 32)
        self.assertIsNotNone(why)

    def test_under_a_cancel_asked_for_the_engine_is_ended_and_the_pair_stays_cancelled(self):
        engine, result = _Engine(), apply.ApplyResult()

        class Cancelling(_Still):
            wanted = Wanted(cancel=CancelMode.GRACEFUL)

        engine.stop_gracefully = lambda: None
        engine.figures = replace(engine.figures, in_flight=(("big.bin", 10, 5),))
        minutes, why = self.follow(engine, lambda now: None, control=Cancelling(), result=result)
        self.assertTrue(engine.stopped)
        self.assertIsNone(why)
        self.assertLessEqual(minutes, 32)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(result.errors[0].startswith("the engine made no progress for 30 minutes"), result.errors)


# --- the live path ------------------------------------------------------------------------

@needs_engine
@unittest.skipUnless(WINDOWS, "job objects")
class EngineLifetimeTest(TempDirTest):
    def test_an_engine_never_outlives_the_core_process_that_started_it(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src, "video.bin", os.urandom(8 * MiB))
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.engine.rclone import Transfer, open_engine
            tmp = Path({str(self.tmp)!r})
            Transfer(open_engine(), tmp / "src", tmp / "dst", ["video.bin"], backup_dir=tmp / "dst" / "v",
                     list_path=tmp / "list.txt", bandwidth_limit="2M")
            time.sleep(120)
        """)
        core = subprocess.Popen([sys.executable, "-c", script], creationflags=FLAGS)
        try:
            self.assertIsNotNone(wait_for(lambda: growing_partial(dst), timeout=60))
        finally:
            core.kill()
            core.wait(timeout=30)
        time.sleep(6)            # the copy needs about four seconds at this rate; an orphan would finish it
        self.assertFalse((dst / "video.bin").exists())


@needs_engine
@unittest.skipUnless(WINDOWS, "the production volume source is Windows-only")
class CommandLineTest(TempDirTest):
    """The production entry point, the real volume source and the records of the folders
    cloud clients synchronise (all read only), a store in a temporary folder."""

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(self.store), *args],
                                cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=FLAGS, timeout=300)
        return result.returncode, result.stdout + result.stderr

    def test_add_run_preview_confirm_history_and_status(self):
        self.store = self.tmp / "Config" / "mirrorhold.json"
        write(self.tmp / "src", "a.txt", "a")
        write(self.tmp / "src", "sub/b.txt", "b")
        code, out = self.cli("add", "Live", "--source", str(self.tmp / "src"), "--dest", str(self.tmp / "dst"))
        self.assertEqual(code, 0, out)
        code, out = self.cli("run", "Live")
        self.assertEqual(code, 5, out)
        self.assertIn("first-run", out)
        self.assertFalse((self.tmp / "dst").exists())
        code, out = self.cli("preview", "Live")
        self.assertEqual(code, 0, out)
        self.assertIn("first run onto an empty destination", out)
        ticket = re.search(r"^ticket (\w+)$", out, re.M).group(1)
        code, out = self.cli("run", "Live", "--confirm", ticket)
        self.assertEqual(code, 0, out)
        self.assertEqual(tree(self.tmp / "dst"), ["a.txt", "sub\\b.txt"])
        code, out = self.cli("history", "Live")
        self.assertEqual((code, len(out.strip().splitlines())), (0, 2), out)
        self.assertEqual(self.cli("status"), (0, "no run in progress\n"))
        self.assertEqual(self.cli("pause")[0], 1)

    def test_an_unattended_run_confirms_nothing(self):
        self.store = self.tmp / "Config" / "mirrorhold.json"
        write(self.tmp / "src", "a.txt", "a")
        self.assertEqual(self.cli("add", "Live", "--source", str(self.tmp / "src"), "--dest",
                                  str(self.tmp / "dst"))[0], 0)
        code, out = self.cli("run", "Live", "--unattended", "--confirm", "abc")
        self.assertEqual(code, 1, out)
        code, out = self.cli("run", "Live", "--unattended")
        self.assertEqual((code, out), (5, ""))


class LaunchArgumentsTest(unittest.TestCase):
    def test_the_run_command_a_caller_starts(self):
        store = Path("X:\\state\\store.json")
        self.assertEqual(run_arguments(store, "j" * 32),
                         ["-m", "mirrorhold", "--store", str(store), "run", "j" * 32])
        self.assertEqual(run_arguments(store, "j", ticket_id="t1")[-2:], ["--confirm", "t1"])
        self.assertEqual(run_arguments(store, "j", unattended=True)[-1], "--unattended")
        self.assertEqual(EXIT_STARTED_NORMALLY, (0, 5, 6, 7))

    def test_a_first_backups_go_ahead_rides_on_the_run_command(self):
        store = Path("X:\\state\\store.json")
        self.assertEqual(run_arguments(store, "j", first_backup=True)[-1], "--first-backup")
        self.assertNotIn("--first-backup", run_arguments(store, "j"))

    def test_a_run_of_some_destinations_names_them_by_marker(self):
        store = Path("X:\\state\\store.json")
        first, second = "a" * 32, "b" * 32
        self.assertEqual(run_arguments(store, "j", destinations=(first, second), first_backup=True)[-5:],
                         ["--marker", first, "--marker", second, "--first-backup"])
        self.assertNotIn("--marker", run_arguments(store, "j"))
        self.assertNotIn("--dest", run_arguments(store, "j", destinations=(first,)))


@needs_engine
@unittest.skipUnless(WINDOWS, "the production volume source is Windows-only")
class LaunchTest(TempDirTest):
    """``run.launch``: a run started as a core process of its own outlives the process
    that started it — closing the GUI stops no run."""

    def test_a_launched_run_outlives_its_starter_and_leaves_its_record(self):
        store = ConfigStore(self.tmp / "state" / "store.json")
        write(self.tmp / "src", "a.txt", "a")
        volumes = default_volume_source().scan().volumes
        job = new_job("Live", endpoint_from_path(str(self.tmp / "src"), volumes),
                      [endpoint_from_path(str(self.tmp / "dst"), volumes)])
        store.save(StoreContents((job,)))
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(PROJECT)!r})
            from mirrorhold.run.launch import start_run
            start_run({str(store.path)!r}, {job.job_id!r})
        """)
        starter = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, creationflags=FLAGS,
                                 timeout=60)
        self.assertEqual(starter.returncode, 0, starter.stderr)
        layout = runtime_layout(store.path)
        records = wait_for(lambda: read_records(layout, job.job_id), timeout=120)
        self.assertEqual([(r.outcome, r.reasons) for r in records or ()], [(Outcome.HELD, ("first-run",))])
        logs = list(layout.launches_dir.glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertTrue(wait_for(lambda: not observe_runs(layout), timeout=30))
        self.assertEqual(logs[0].read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
