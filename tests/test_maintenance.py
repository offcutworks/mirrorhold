"""Maintenance: version cleanup, full verification, the reminder, and the free-space warning.

The promised outcomes, on temporary folders: cleanup removes exactly the expired
batches of a job that has it on and nothing else, a junction planted inside a batch
included; a verification finds a planted corruption, continues after an interruption
and after a drive removed during a pass, and records its date — when the pass began.
Around them: cleanup's other fences, the preview measuring it and
the space check counting it, a verification's pause, stop and giving way to a waiting
run that writes what it reads, verifications beside each other and beside runs that do
not, a verification waiting for a run on one of its drives and giving way to one, the
reminder, the run record's schemas and the notices. The last classes follow the
live path: ``verify`` on the command line in process, and a verification started as a
process of its own on this machine's real volume source (read-only).

Volumes are temporary folders mapped as volumes (``support.volume``); nothing touches a
real volume but through a temporary folder on the system drive."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from support import (PROJECT, TempDirTest, endpoint, held, job_file_saver, let_go_after, live_activity, result_of,
                     volume)
from test_run import RunCase, write

from mirrorhold import exchange
from mirrorhold.cli import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from mirrorhold.endpoints.marker import MANAGEMENT_DIR, Marker, write_marker
from mirrorhold.endpoints.shared import SharedRecord, write_shared
from mirrorhold.endpoints.volumes import FixedVolumeSource, VolumeScan, drive_name, low_on_space
from mirrorhold.jobs.model import (Activities, Destination, DriveTurns, Notifications, Preferences, VersionCleanup,
                                   new_job)
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.maintenance.cleanup import batch_time, clean_versions, plan_cleanup
from mirrorhold.maintenance import verified
from mirrorhold.maintenance.verification import (VERIFICATION_SCHEMA, VERIFICATIONS_DIR, VerificationMode,
                                                 VerificationRecord, VerificationState, due_after_run,
                                                 effective_state, last_full_verification, read_verifications,
                                                 resumable, unfinished, verification_due, write_verification)
from mirrorhold.maintenance.verifier import GIVE_WAY_INTERVAL, SAVE_INTERVAL, VerifyError, verify_destination
from mirrorhold.notifications.message import notice_for, verification_notice
from mirrorhold.planning.estimate import DEFAULT_HIGH, DEFAULT_LOW
from mirrorhold.records import diagnostics
from mirrorhold.records.admission import Claim, ClaimedDrive, WaitedFor
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, RECORD_SCHEMA, Outcome, RunRecord,
                                       VersionsCleaned, read_records, utc_now, write_record)
from mirrorhold.records.status import (KIND_VERIFICATION, CancelMode, LiveStatus, Phase, StatusBoard, observe_runs,
                                       reap_interrupted, request)
from mirrorhold.run.gate import Verdict, space_needed
from mirrorhold.run.launch import start_verification, verify_arguments
from mirrorhold.storage.destination import StorageError
from mirrorhold.storage.drive import DriveStorage

WINDOWS = sys.platform == "win32"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
JOB, MARKER = "a" * 32, "b" * 32


def junction(link: Path, target: Path) -> None:
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H-%M-%SZ")


# --- version cleanup, without the engine ------------------------------------------------

class CleanupTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.dst = self.tmp / "volD" / "Backup"
        self.dst.mkdir(parents=True)
        write_marker(self.dst, Marker(JOB, MARKER))
        self.versions = self.dst / MANAGEMENT_DIR / "versions"

    def batch(self, name, files=None):
        folder = self.versions / name
        folder.mkdir(parents=True)
        for rel, data in (files or {"old.txt": "old"}).items():
            write(folder, rel, data)
        return folder

    def clean(self, days=30, **kwargs):
        return clean_versions(self.dst, days, job_id=JOB, marker_id=MARKER, now=NOW, **kwargs)

    def test_a_batch_is_dated_by_its_name(self):
        self.assertEqual(batch_time("2026-06-01T10-00-00Z"), datetime(2026, 6, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(batch_time("2026-06-01T10-00-00Z-2"), datetime(2026, 6, 1, 10, tzinfo=timezone.utc))
        self.assertIsNone(batch_time("notes"))
        self.assertIsNone(batch_time("2026-13-01T10-00-00Z"))

    def test_only_batches_older_than_the_days_are_deleted(self):
        old = stamp(NOW - timedelta(days=31))
        self.batch(old, {"a\\deep\\file.txt": "x" * 10, "b.txt": "y"})
        self.batch(old + "-2")
        self.batch(stamp(NOW - timedelta(days=29)))
        self.batch("notes", {"mine.txt": "kept"})
        report = self.clean()
        self.assertTrue(report.deleted)
        self.assertEqual([b.name for b in report.batches], [old, old + "-2"])
        self.assertEqual((report.files, report.bytes), (3, 14))
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), sorted([stamp(NOW - timedelta(days=29)),
                                                                                   "notes"]))
        self.assertTrue(any("notes is not a versions batch" in p for p in report.problems), report.problems)
        cleaned = report.cleaned()
        self.assertEqual((cleaned.batch_total, cleaned.files, cleaned.bytes, cleaned.keep_days), (2, 3, 14, 30))

    @unittest.skipUnless(WINDOWS, "junctions are Windows'")
    def test_a_junction_inside_a_batch_is_removed_and_what_it_points_at_is_untouched(self):
        outside = self.tmp / "outside"
        write(outside, "precious.txt", "keep me")
        write(outside, "sub\\more.txt", "and me")
        folder = self.batch(stamp(NOW - timedelta(days=40)))
        junction(folder / "link", outside)
        report = self.clean()
        self.assertEqual(len(report.batches), 1, report.problems)
        self.assertFalse(folder.exists())
        self.assertEqual((outside / "precious.txt").read_text(encoding="utf-8"), "keep me")
        self.assertTrue((outside / "sub" / "more.txt").is_file())

    @unittest.skipUnless(WINDOWS, "junctions are Windows'")
    def test_a_batch_or_a_versions_folder_that_is_a_link_is_left_alone(self):
        outside = self.tmp / "outside"
        write(outside, "precious.txt", "keep me")
        self.versions.mkdir(parents=True)
        junction(self.versions / stamp(NOW - timedelta(days=40)), outside)
        report = self.clean()
        self.assertEqual(report.batches, ())
        self.assertTrue((outside / "precious.txt").is_file())
        self.assertIn("not a versions batch", " ".join(report.problems))
        other = self.tmp / "volE" / "Backup"
        other.mkdir(parents=True)
        write_marker(other, Marker(JOB, MARKER))
        target = self.tmp / "elsewhere"
        self.batch_in(target / stamp(NOW - timedelta(days=40)))
        junction(other / MANAGEMENT_DIR / "versions", target)
        report = clean_versions(other, 30, job_id=JOB, marker_id=MARKER, now=NOW)
        self.assertEqual(report.batches, ())
        self.assertIn("is a link or junction", report.problems[0])
        self.assertTrue((target / stamp(NOW - timedelta(days=40)) / "old.txt").is_file())

    def batch_in(self, folder):
        write(folder, "old.txt", "old")

    def test_nothing_is_deleted_without_this_jobs_marker(self):
        folder = self.batch(stamp(NOW - timedelta(days=40)))
        report = clean_versions(self.dst, 30, job_id="c" * 32, marker_id=MARKER, now=NOW)
        self.assertEqual(report.batches, ())
        self.assertIn("marker does not match", report.problems[0])
        self.assertTrue(folder.is_dir())

    def test_a_remote_that_stops_answering_as_its_marker_is_read_deletes_nothing_and_is_told_as_the_remote(self):
        folder = self.batch(stamp(NOW - timedelta(days=40)))
        storage = DriveStorage(self.dst)
        unanswered = StorageError("the sync engine did not answer: invented")
        with mock.patch.object(storage, "read_file", side_effect=unanswered), \
                mock.patch.object(storage, "entry", side_effect=unanswered):
            report = clean_versions(storage, 30, job_id=JOB, marker_id=MARKER, now=NOW)
        self.assertEqual(report.batches, ())
        self.assertEqual(report.problems, ("the remote stopped answering: the sync engine did not answer: invented; "
                                           "nothing was deleted and the next run tries again",))
        self.assertTrue(folder.is_dir())

    def test_read_only_files_and_folders_are_deleted(self):
        folder = self.batch(stamp(NOW - timedelta(days=40)), {"ro\\file.txt": "r"})
        os.chmod(folder / "ro" / "file.txt", stat.S_IREAD)
        os.chmod(folder / "ro", stat.S_IREAD)
        report = self.clean()
        self.assertEqual((len(report.batches), report.problems), (1, ()))
        self.assertFalse(folder.exists())

    def test_the_preview_measures_without_deleting_and_a_stop_leaves_the_rest(self):
        first = self.batch(stamp(NOW - timedelta(days=50)), {"a.txt": "12345"})
        second = self.batch(stamp(NOW - timedelta(days=40)), {"b.txt": "123"})
        planned = plan_cleanup(self.dst, 30, NOW)
        self.assertFalse(planned.deleted)
        self.assertEqual(([b.name for b in planned.batches], planned.bytes), ([first.name, second.name], 8))
        self.assertTrue(first.is_dir() and second.is_dir())
        asked = []
        report = self.clean(stop=lambda: asked.append(1) or len(asked) > 1)
        self.assertEqual([b.name for b in report.batches], [first.name])
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())

    def test_no_versions_folder_is_nothing_to_do(self):
        self.assertEqual((self.clean().batches, self.clean().problems), ((), ()))

    @unittest.skipUnless(WINDOWS, "Windows names")
    def test_a_batch_holding_folders_whose_names_end_in_a_space_or_a_period_is_deleted_whole(self):
        long = "\\\\?\\"
        folder = self.batch(stamp(NOW - timedelta(days=40)))
        self.addCleanup(shutil.rmtree, long + str(self.tmp), ignore_errors=True)
        for name in ("Album ", "Dots."):
            os.makedirs(long + str(folder / name / "inner."))
            with open(long + str(folder / name / "inner." / "photo.jpg"), "wb") as handle:
                handle.write(b"x" * 10)
        planned = plan_cleanup(self.dst, 30, NOW)
        self.assertEqual((planned.files, planned.bytes), (3, 23))
        report = self.clean()
        self.assertEqual(([b.name for b in report.batches], report.problems), ([folder.name], ()))
        self.assertFalse(os.path.lexists(long + str(folder)))


# --- version cleanup and free space in a run, through the engine ------------------------

def used_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in Path(root).rglob("*") if path.is_file())


class FollowingVolumes:
    """A volume source whose destination drive's free space follows what its folder holds,
    as a real drive's does: ``base`` less the bytes under ``root``."""

    def __init__(self, source, destination, root: Path, base: int):
        self.source, self.destination, self.root, self.base = source, destination, root, base
        self.last = destination

    def scan(self) -> VolumeScan:
        """``last``: the destination drive as this scan measured it."""
        self.last = replace(self.destination, free_bytes=self.base - used_bytes(self.root))
        return VolumeScan((self.source, self.last), ())


class CleanupRunTest(RunCase):
    def with_cleanup(self, days):
        self.job = replace(self.job, version_cleanup=VersionCleanup(days))
        self.store.save(StoreContents((self.job,)))

    def plant(self, days_ago, data="old"):
        name = stamp(datetime.now(timezone.utc) - timedelta(days=days_ago))
        write(self.dst / MANAGEMENT_DIR / "versions" / name, "gone.txt", data)
        return name

    def test_a_run_deletes_the_expired_batches_and_records_them(self):
        self.seed({"a.txt": "a"})
        self.with_cleanup(30)
        old = self.plant(100)
        recent = self.plant(3)
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(sorted(p.name for p in (self.dst / MANAGEMENT_DIR / "versions").iterdir()), [recent])
        self.assertEqual(record.versions_cleaned.batches, (old,))
        self.assertEqual((record.versions_cleaned.batch_total, record.versions_cleaned.bytes), (1, 3))
        self.assertIn("1 versions batch older than 30 days deleted", record.describe())
        again = read_records(self.layout, self.job.job_id)[-1]
        self.assertEqual(again.versions_cleaned, record.versions_cleaned)

    def test_without_cleanup_nothing_is_deleted(self):
        self.seed({"a.txt": "a"})
        old = self.plant(100)
        record = self.only(self.run_now())
        self.assertIsNone(record.versions_cleaned)
        self.assertTrue((self.dst / MANAGEMENT_DIR / "versions" / old).is_dir())

    def test_the_preview_shows_the_cleanup_and_the_space_check_counts_it(self):
        self.seed({"a.txt": "a"})
        old = self.plant(100, "x" * 1000)
        write(self.src, "new.txt", "0123456789")
        needed = space_needed(10, 1)
        self.d = replace(self.d, free_bytes=needed - 500)
        self.volumes = FixedVolumeSource((self.s, self.d))
        without = self.preview().pairs[0].assessment
        self.assertEqual((without.decision.verdict, without.cleanup), (Verdict.STOP, None))
        self.with_cleanup(30)
        pair = self.preview().pairs[0].assessment
        self.assertEqual(pair.decision.verdict, Verdict.PROCEED)
        self.assertEqual(([b.name for b in pair.cleanup.batches], pair.cleanup.bytes), ([old], 1000))
        self.assertTrue((self.dst / MANAGEMENT_DIR / "versions" / old).is_dir())
        self.volumes = FollowingVolumes(self.s, self.d, self.vol_d, self.d.free_bytes + used_bytes(self.vol_d))
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertFalse((self.dst / MANAGEMENT_DIR / "versions" / old).exists())
        self.assertEqual(record.space, (self.volumes.last.free_bytes, self.volumes.last.capacity_bytes))
        self.assertTrue(record.low_on_space)
        self.assertIn("% free on its drive", record.describe())
        notice = notice_for([record], Notifications())
        self.assertIn("% free on its drive", notice.lines[0])

    def test_a_runs_space_check_measures_the_drive_once_its_cleanup_is_done(self):
        self.seed({"a.txt": "a"})
        old = self.plant(100, "x" * 100_000)
        write(self.src, "new.txt", "0123456789")
        needed = space_needed(10, 1)
        self.volumes = FollowingVolumes(self.s, self.d, self.vol_d, needed - 50_000 + used_bytes(self.vol_d))
        stopped = self.only(self.run_now())
        self.assertEqual((stopped.outcome, stopped.reasons), (Outcome.STOPPED, ("insufficient-space",)))
        self.with_cleanup(30)
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertFalse((self.dst / MANAGEMENT_DIR / "versions" / old).exists())
        self.assertEqual((self.dst / "new.txt").read_text(encoding="utf-8"), "0123456789")


# --- full verification, without the engine ----------------------------------------------

class VerifyCase(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        self.src, self.dst = self.vol_s / "Work", self.vol_d / "Backup"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="D")
        self.volumes = FixedVolumeSource((self.s, self.d))
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Backup")])
        self.store.save(StoreContents((self.job,)))
        self.layout = runtime_layout(self.store.path)
        self.marker = self.job.destinations[0].marker_id
        write_marker(self.dst, Marker(self.job.job_id, self.marker))

    def mirror(self, files):
        for rel, data in files.items():
            write(self.src, rel, data)
            target = self.dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.src / rel, target)

    def verify(self, **kwargs):
        return verify_destination(self.store, self.job.job_id, 1, volume_source=self.volumes, **kwargs)

    def slowly(self, files, size):
        """``files`` files of ``size`` bytes, and verifier options that read them slowly
        until ``self.slow`` is cleared: at least 0.02 s per 16 KiB read, more where the
        timer rounds a sleep up, so a test that needs the pass still running after some
        seconds sizes the files for its whole window at that least pace."""
        self.mirror({f"f{i:02}.bin": os.urandom(size) for i in range(files)})
        self.slow = threading.Event()
        self.slow.set()
        return dict(chunk_bytes=16 * 1024, pace=lambda: self.slow.is_set() and time.sleep(0.02))

    def in_background(self, **kwargs):
        box = {}

        def work():
            try:
                box["record"] = self.verify(**kwargs)
            except BaseException as exc:  # reported by the test
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, box

    def live_status(self):
        runs = [r for r in observe_runs(self.layout) if r.alive and r.status.get("kind") == KIND_VERIFICATION]
        return runs[0].status if runs else None

    def wait(self, condition, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        self.fail("the condition was not met in time")


class VerifyTest(VerifyCase):
    def test_a_clean_pass_compares_every_file_and_records_its_date(self):
        self.mirror({"a.txt": "alpha", "sub\\b.bin": os.urandom(300 * 1024), "c.txt": ""})
        record = self.verify()
        self.assertEqual(record.state, VerificationState.COMPLETED, record.detail)
        self.assertEqual((record.compared_files, record.files_total, record.differ_total), (3, 3, 0))
        self.assertEqual(record.compared_bytes, 5 + 300 * 1024)
        self.assertEqual(record.not_compared, 0)
        self.assertIsNotNone(record.ended)
        records = read_verifications(self.layout, self.job.job_id)
        self.assertEqual(last_full_verification(records, self.marker), record.started)
        self.assertTrue((self.dst / MANAGEMENT_DIR / VERIFICATIONS_DIR / record.file_name()).is_file())
        self.assertEqual(observe_runs(self.layout), [])

    def test_a_planted_corruption_is_found(self):
        self.mirror({"a.txt": "alpha", "photo.raw": b"\x00" * 4096, "z.txt": "zed"})
        damaged = self.dst / "photo.raw"
        times = os.stat(damaged)
        data = bytearray(damaged.read_bytes())
        data[2048] ^= 0x01
        damaged.write_bytes(bytes(data))
        os.utime(damaged, ns=(times.st_atime_ns, times.st_mtime_ns))
        record = self.verify()
        self.assertEqual((record.state, record.compared_files, record.differ_total),
                         (VerificationState.COMPLETED, 3, 1))
        self.assertEqual(record.differ, [("photo.raw", 4096)])
        notice = verification_notice(record, Notifications())
        self.assertEqual(notice.title, "Work: verification found 1 file that differs")
        self.assertIn("photo.raw", notice.lines[0])
        self.assertIsNone(verification_notice(record, Notifications(on_failure=False)))

    def test_entries_that_cannot_be_compared_are_counted_not_compared(self):
        self.mirror({"same.txt": "same", "changed.txt": "old"})
        write(self.src, "changed.txt", "a longer new version")
        write(self.src, "new.txt", "not backed up yet")
        write(self.dst, "gone.txt", "deleted at the source")
        record = self.verify()
        self.assertEqual((record.compared_files, record.changed_total, record.missing_total, record.extra_total),
                         (1, 1, 1, 1))
        self.assertEqual(record.differ_total, 0)

    def test_a_pass_continues_after_an_interruption(self):
        names = [f"f{i}.txt" for i in range(6)]
        self.mirror({name: name * 50 for name in names})
        handled = []

        def interrupt(path):
            handled.append(path)
            if len(handled) == 2:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.verify(after_item=interrupt)
        stopped = read_verifications(self.layout, self.job.job_id)[-1]
        self.assertEqual((stopped.state, stopped.cursor, stopped.compared_files),
                         (VerificationState.INTERRUPTED, "f1.txt", 2))
        self.assertIs(resumable([stopped], self.marker), stopped)
        second = []
        record = self.verify(after_item=second.append)
        self.assertEqual(record.verification_id, stopped.verification_id)
        self.assertEqual((record.state, record.sittings, record.compared_files), (VerificationState.COMPLETED, 2, 6))
        self.assertEqual(second, names[2:])
        self.assertEqual(len(read_verifications(self.layout, self.job.job_id)), 1)

    def test_a_pass_whose_process_died_is_continued_and_a_restart_begins_anew(self):
        self.mirror({f"f{i}.txt": "x" for i in range(4)})
        dead = VerificationRecord("d" * 32, self.job.job_id, "Work", self.marker, 1, "2026-10-01T10:00:00Z",
                                  "2026-10-01T10:05:00Z", cursor="f1.txt", compared_files=2, compared_bytes=2)
        write_verification(self.layout, dead)
        self.assertEqual(effective_state(dead), VerificationState.INTERRUPTED)
        self.assertEqual(effective_state(dead, {"d" * 32}), VerificationState.RUNNING)
        record = self.verify()
        self.assertEqual((record.verification_id, record.sittings, record.compared_files), ("d" * 32, 2, 4))
        fresh = self.verify(restart=True)
        self.assertNotEqual(fresh.verification_id, "d" * 32)
        self.assertEqual((fresh.sittings, fresh.compared_files), (1, 4))

    def test_pause_holds_it_and_a_stop_keeps_what_was_compared(self):
        thread, box = self.in_background(**self.slowly(16, 256 * 1024))
        status = self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value and s)
        request(self.layout, status["run_id"], pause=True)
        self.wait(lambda: (s := self.live_status()) and s.get("paused"))
        before = self.live_status()["verify"]["bytes_done"]
        time.sleep(0.6)
        self.assertEqual(self.live_status()["verify"]["bytes_done"], before)
        request(self.layout, status["run_id"], pause=False)
        self.wait(lambda: (s := self.live_status()) and s["verify"]["bytes_done"] > before)
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        thread.join(30)
        self.assertNotIn("error", box)
        stopped = box["record"]
        self.assertEqual(stopped.state, VerificationState.STOPPED)
        self.assertLess(stopped.compared_files, 16)
        self.slow.clear()
        record = self.verify()
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 16, 2))

    def test_it_gives_way_to_a_waiting_run_and_continues_after_it(self):
        thread, box = self.in_background(**self.slowly(10, 1024 * 1024))
        self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value)
        run = live_activity(self.layout, self.job.job_id, job_name="Work", admitted=False,
                            reads=[endpoint(self.s, "Work")], writes=[endpoint(self.d, "Backup")])
        try:
            status = self.wait(lambda: (s := self.live_status()) and s["verify"]["gave_way"] and s)
            self.assertEqual((status["phase"], status["admitted"]), (Phase.WAITING.value, False))
            run.status.admitted, run.status.phase = True, Phase.LISTING      # the run takes its turn and works
            run.publish()
            time.sleep(0.5)
            self.assertEqual(self.live_status()["phase"], Phase.WAITING.value)
        finally:
            run.close()
            self.slow.clear()
        thread.join(60)
        self.assertNotIn("error", box)
        record = box["record"]
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 10, 1),
                         record.detail)

    def test_it_works_beside_a_run_it_does_not_conflict_with_and_gives_way_to_one_that_writes_what_it_reads(self):
        thread, box = self.in_background(**self.slowly(10, 1024 * 1024))
        self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value)
        beside = live_activity(self.layout, "o" * 32, reads=[endpoint(self.s, "Other")],
                               writes=[endpoint(self.d, "Other")])
        try:
            time.sleep(GIVE_WAY_INTERVAL + 1.0)
            status = self.live_status()
            self.assertEqual((status["phase"], status["verify"]["gave_way"]), (Phase.VERIFYING.value, False))
            run = live_activity(self.layout, self.job.job_id, job_name="Work", admitted=False,
                                reads=[endpoint(self.s, "Work")], writes=[endpoint(self.d, "Backup")])
            try:
                status = self.wait(lambda: (s := self.live_status()) and s["verify"]["gave_way"] and s)
                self.assertEqual((status["phase"], status["admitted"]), (Phase.WAITING.value, False))
                run.status.admitted, run.status.phase = True, Phase.LISTING      # the run takes its turn
                run.publish()
                waiting = self.wait(lambda: (s := self.live_status()) and s.get("waiting_for")
                                    and not s["waiting_for"]["waiting"] and s)
                self.assertEqual((waiting["phase"], waiting["waiting_for"]["run_id"]),
                                 (Phase.WAITING.value, run.status.run_id))
            finally:
                run.close()
        finally:
            beside.close()
            self.slow.clear()
        thread.join(60)
        self.assertNotIn("error", box)
        record = box["record"]
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 10, 1))

    def test_verifications_of_two_destinations_work_at_the_same_time(self):
        second = self.vol_d / "Backup2"
        job = new_job("Pair", endpoint(self.s, "Work"), [endpoint(self.d, "Backup"), endpoint(self.d, "Backup2")])
        self.store.save(StoreContents((job,), Preferences(activities=Activities(DriveTurns.AT_THE_SAME_TIME))))
        for index, root in ((1, self.dst), (2, second)):
            write_marker(root, Marker(job.job_id, job.destinations[index - 1].marker_id))
        for i in range(8):
            write(self.src, f"f{i}.bin", os.urandom(256 * 1024))
            for root in (self.dst, second):
                shutil.copy2(self.src / f"f{i}.bin", root / f"f{i}.bin")
        slow = threading.Event()
        slow.set()
        boxes = [{}, {}]

        def verify(index, box):
            try:
                box["record"] = verify_destination(self.store, job.job_id, index, volume_source=self.volumes,
                                                   chunk_bytes=16 * 1024,
                                                   pace=lambda: slow.is_set() and time.sleep(0.02))
            except BaseException as exc:  # reported by the test
                box["error"] = exc

        threads = [threading.Thread(target=verify, args=(i + 1, boxes[i]), daemon=True) for i in range(2)]
        for thread in threads:
            thread.start()

        def together():
            phases = [r.status.get("phase") for r in observe_runs(self.layout)
                      if r.alive and r.status.get("kind") == KIND_VERIFICATION]
            return phases == [Phase.VERIFYING.value] * 2

        try:
            self.assertTrue(self.wait(together))
        finally:
            slow.clear()
        for thread, box in zip(threads, boxes):
            thread.join(60)
            self.assertNotIn("error", box)
            self.assertEqual((box["record"].state, box["record"].compared_files), (VerificationState.COMPLETED, 8))

    def on_its_drives(self, drive, *, admitted=True):
        """Another job's run as its process publishes it, on folders of its own on the pair's
        volumes, using ``drive`` and taking its drives one at a time."""
        status = LiveStatus("o" * 32, os.getpid(), "k" * 32, "Other", "manual", utc_now(), admitted=admitted,
                            claim=Claim((endpoint(self.s, "Other"),), (endpoint(self.d, "Other"),),
                                        (ClaimedDrive(drive.drive, drive_name((self.s, self.d), drive.drive)),),
                                        DriveTurns.ONE_AT_A_TIME).to_document(),
                            phase=Phase.COPYING if admitted else Phase.WAITING)
        board = StatusBoard(self.layout, status)
        board.open()
        self.addCleanup(board.close)
        return board

    def test_it_waits_for_a_run_on_one_of_its_drives(self):
        self.mirror({"a.txt": "a"})
        run = self.on_its_drives(self.d)
        thread, box = self.in_background()
        status = self.wait(lambda: (s := self.live_status()) and s.get("waiting_for") and s)
        self.assertEqual((status["phase"], WaitedFor.from_status(status)),
                         (Phase.WAITING.value, WaitedFor(run.status.run_id, "run", "Other", drive="drive 'D'")))
        run.close()
        thread.join(60)
        self.assertNotIn("error", box)
        self.assertEqual(box["record"].state, VerificationState.COMPLETED)

    def test_it_gives_way_to_a_waiting_run_on_one_of_its_drives(self):
        thread, box = self.in_background(**self.slowly(10, 1024 * 1024))
        self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value)
        run = self.on_its_drives(self.s, admitted=False)
        try:
            status = self.wait(lambda: (s := self.live_status()) and s["verify"]["gave_way"] and s)
            self.assertEqual((status["phase"], status["admitted"]), (Phase.WAITING.value, False))
            run.status.admitted, run.status.phase = True, Phase.LISTING      # the run takes its turn
            run.publish()
            waiting = self.wait(lambda: (s := self.live_status()) and s.get("waiting_for")
                                and not s["waiting_for"]["waiting"] and s)
            self.assertEqual(WaitedFor.from_status(waiting), WaitedFor(run.status.run_id, "run", "Other",
                                                                       drive="drive 'S'"))
        finally:
            run.close()
            self.slow.clear()
        thread.join(60)
        self.assertNotIn("error", box)
        record = box["record"]
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 10, 1))

    def test_it_cannot_start_without_its_pair(self):
        self.mirror({"a.txt": "a"})
        with self.assertRaisesRegex(VerifyError, "not attached"):
            verify_destination(self.store, self.job.job_id, 1, volume_source=FixedVolumeSource((self.s,)))
        write_marker(self.dst, Marker("c" * 32, self.marker))
        with self.assertRaisesRegex(VerifyError, "marker does not match"):
            self.verify()
        os.remove(self.dst / MANAGEMENT_DIR / "marker.json")
        with self.assertRaisesRegex(VerifyError, "first run"):
            self.verify()
        with self.assertRaisesRegex(VerifyError, "no destination 2"):
            verify_destination(self.store, self.job.job_id, 2, volume_source=self.volumes)
        self.assertEqual(read_verifications(self.layout, self.job.job_id), [])

    def test_a_destination_named_by_marker_is_verified_at_the_number_the_job_gives_it(self):
        self.mirror({"a.txt": "a"})
        self.job = replace(self.job, destinations=(Destination(endpoint(self.d, "Other")),) + self.job.destinations)
        self.store.save(StoreContents((self.job,)))
        record = verify_destination(self.store, self.job.job_id, marker=self.marker, volume_source=self.volumes)
        self.assertEqual((record.state, record.marker_id, record.destination_index),
                         (VerificationState.COMPLETED, self.marker, 2))

    def test_a_destination_is_named_by_its_number_or_its_marker_and_one_the_job_holds(self):
        with self.assertRaisesRegex(VerifyError, "no destination with marker " + "f" * 32):
            verify_destination(self.store, self.job.job_id, marker="f" * 32, volume_source=self.volumes)
        with self.assertRaisesRegex(VerifyError, "by its number or by its marker"):
            verify_destination(self.store, self.job.job_id, 1, marker=self.marker, volume_source=self.volumes)
        with self.assertRaisesRegex(VerifyError, "by its number or by its marker"):
            verify_destination(self.store, self.job.job_id, volume_source=self.volumes)
        self.assertEqual(read_verifications(self.layout, self.job.job_id), [])

    def test_a_second_verification_of_the_destination_is_refused(self):
        thread, box = self.in_background(**self.slowly(4, 256 * 1024))
        status = self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value and s)
        with self.assertRaisesRegex(VerifyError, "being verified already"):
            self.verify()
        request(self.layout, status["run_id"], pause=False, cancel=CancelMode.IMMEDIATE)
        thread.join(30)
        self.assertEqual(box["record"].state, VerificationState.STOPPED)

    def test_a_dead_verification_leaves_no_run_record(self):
        status_dir = self.layout.status_dir
        status_dir.mkdir(parents=True)
        doc = LiveStatus("f" * 32, 1, self.job.job_id, "Work", "manual", "2026-10-07T12:00:00Z", pair_index=1,
                         marker_id=self.marker, pair_started="2026-10-07T12:00:00Z", kind=KIND_VERIFICATION,
                         verify={}).to_document()
        (status_dir / ("f" * 32 + ".json")).write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(reap_interrupted(self.layout), [])
        self.assertEqual((read_records(self.layout, self.job.job_id), list(status_dir.iterdir())), ([], []))


# --- the reminder, the record and the notices --------------------------------------------

class SharedVerifyTest(VerifyCase):
    """A shared destination is verified as its runs see it: its job's part alone."""

    def share_it(self, own, left=()):
        write_marker(self.dst, Marker(self.job.job_id, self.marker, shared=True))
        write_shared(self.dst, SharedRecord(self.job.job_id, self.marker, own, left))

    def test_what_a_shared_destination_leaves_is_neither_extra_nor_compared(self):
        self.mirror({"Photos\\a.jpg": "alpha", "letter.txt": "letter"})
        write(self.dst, "Windows\\kernel.dll", "the operating system")
        write(self.dst, "Photos\\old.jpg", "there before the job")
        self.share_it(("letter.txt", "Photos"), ("Photos\\old.jpg",))
        record = self.verify()
        self.assertEqual((record.state, record.compared_files, record.extra_total, record.not_compared),
                         (VerificationState.COMPLETED, 2, 0, 0), record.detail)
        write(self.dst, "Photos\\hand.txt", "put inside the job's own folder")
        self.assertEqual(self.verify(restart=True).extra_total, 1)

    def test_a_shared_destination_whose_record_cannot_be_used_is_not_verified(self):
        self.mirror({"a.txt": "alpha"})
        write_marker(self.dst, Marker(self.job.job_id, self.marker, shared=True))
        with self.assertRaises(VerifyError):
            self.verify()


class SharedVerifyRecordTest(VerifyCase):
    """A shared destination is verified as its record says its job has backed it up: what
    the source holds now beyond that is not backed up yet, and what the destination holds
    there — perhaps the user's own — is never compared."""

    share_it = SharedVerifyTest.share_it

    def same_size_and_time(self, rel, source_data, destination_data):
        """A source file and a destination file at one path, of one size and time but other
        content: compared, they would differ."""
        write(self.src, rel, source_data)
        write(self.dst, rel, destination_data)
        when = 1_700_000_000_000_000_000
        for side in (self.src, self.dst):
            os.utime(side / rel, ns=(when, when))

    def test_a_name_the_source_gained_and_a_left_entry_it_reaches_are_not_compared(self):
        self.mirror({"Photos\\a.jpg": "alpha"})
        self.same_size_and_time("Music\\song.mp3", "the source's", "the user own")
        self.same_size_and_time("Photos\\old.jpg", "the source's", "there before")
        self.share_it(("Photos",), ("Photos\\old.jpg",))
        record = self.verify()
        self.assertEqual((record.state, record.compared_files, record.differ_total, record.missing_total,
                          record.extra_total), (VerificationState.COMPLETED, 1, 0, 2, 0), record.detail)
        self.assertEqual((self.dst / "Music" / "song.mp3").read_text(encoding="utf-8"), "the user own")


def run_record(outcome=Outcome.COMPLETED, *, ended="2026-06-01T10:00:00Z", marker=MARKER, space=None, cleaned=None):
    return RunRecord("r" * 32, JOB, "Job", marker, 1, "manual", ended, ended, outcome, (),
                     planned=dict.fromkeys(PLANNED_KEYS, 0), done=dict.fromkeys(DONE_KEYS, 0),
                     left=dict.fromkeys(LEFT_KEYS, 0), space=space, versions_cleaned=cleaned)


def verification(state=VerificationState.COMPLETED, *, ended="2026-09-01T10:00:00Z", marker=MARKER):
    return VerificationRecord("v" * 32, JOB, "Job", marker, 1, "2026-09-01T09:00:00Z", ended, state,
                              ended if state is VerificationState.COMPLETED else None)


class VerifySideGoneTest(VerifyCase):
    """A side that goes away during a pass ends it unfinished, never as if complete; a
    finished pass dates the verification by when it began."""

    def test_a_drive_removed_during_a_pass_leaves_it_unfinished_and_it_continues_later(self):
        self.mirror({f"f{i}.txt": f"f{i}" * 50 for i in range(4)})
        away = self.vol_d / "Backup-away"

        def remove_the_drive(path):
            if path == "f0.txt":
                self.dst.rename(away)

        stopped = self.verify(after_item=remove_the_drive)
        self.assertEqual((stopped.state, stopped.cursor, stopped.compared_files, stopped.unreadable_total),
                         (VerificationState.FAILED, "f0.txt", 1, 0))
        self.assertEqual(stopped.detail, f"the destination folder {self.dst} went away and did not come back within "
                                         "60 seconds: was its drive removed?")
        records = read_verifications(self.layout, self.job.job_id)
        self.assertIsNone(last_full_verification(records, self.marker))
        self.assertEqual(resumable(records, self.marker).verification_id, stopped.verification_id)
        away.rename(self.dst)
        record = self.verify()
        self.assertEqual((record.verification_id, record.state, record.sittings, record.compared_files,
                          record.unreadable_total),
                         (stopped.verification_id, VerificationState.COMPLETED, 2, 4, 0))

    def test_a_finished_pass_is_dated_by_when_it_began(self):
        self.mirror({f"f{i}.txt": "x" for i in range(3)})
        began = "2026-09-01T10:00:00Z"
        write_verification(self.layout, VerificationRecord("d" * 32, self.job.job_id, "Work", self.marker, 1, began,
                                                           "2026-09-01T10:05:00Z", cursor="f0.txt", compared_files=1,
                                                           compared_bytes=1))
        record = self.verify()
        self.assertEqual((record.verification_id, record.state), ("d" * 32, VerificationState.COMPLETED))
        self.assertEqual(last_full_verification(read_verifications(self.layout, self.job.job_id), self.marker), began)


class VerifyKeepsGoingTest(VerifyCase):
    """A verification ends only when it finishes or is stopped: a side that goes away for a
    moment is waited for, one that stays away ends the sitting in words, a read that does
    not answer does not keep a stop from ending it, and neither another program holding
    its record nor the job file being saved by another process ends it."""

    def test_a_side_that_comes_back_within_its_patience_lets_the_pass_finish(self):
        self.mirror({f"f{i}.txt": f"f{i}" * 50 for i in range(4)})
        away = self.vol_d / "Backup-away"

        def reset_the_drive(path):
            if path == "f0.txt":
                self.dst.rename(away)
                threading.Timer(1.0, away.rename, (self.dst,)).start()

        record = self.verify(after_item=reset_the_drive, side_patience=20)
        self.assertEqual((record.state, record.compared_files, record.unreadable_total, record.sittings),
                         (VerificationState.COMPLETED, 4, 0, 1), record.detail)
        self.assertFalse(away.exists())

    def test_a_side_that_stays_away_ends_the_sitting_in_words_and_the_pass_continues_later(self):
        self.mirror({f"f{i}.txt": f"f{i}" * 50 for i in range(4)})
        away = self.vol_d / "Backup-away"

        def remove_the_drive(path):
            if path == "f0.txt":
                self.dst.rename(away)

        stopped = self.verify(after_item=remove_the_drive, side_patience=2)
        self.assertEqual((stopped.state, stopped.cursor, stopped.compared_files, stopped.unreadable_total),
                         (VerificationState.FAILED, "f0.txt", 1, 0))
        self.assertEqual(stopped.detail, f"the destination folder {self.dst} went away and did not come back within "
                                         "2 seconds: was its drive removed?")
        away.rename(self.dst)
        record = self.verify()
        self.assertEqual((record.verification_id, record.state, record.sittings, record.compared_files),
                         (stopped.verification_id, VerificationState.COMPLETED, 2, 4))

    def test_a_stop_ends_a_sitting_whose_read_does_not_answer(self):
        self.mirror({"a.bin": os.urandom(256 * 1024), "b.bin": os.urandom(1024)})
        hang, release = threading.Event(), threading.Event()
        hang.set()
        self.addCleanup(release.set)
        thread, box = self.in_background(chunk_bytes=16 * 1024, stop_patience=2,
                                         pace=lambda: hang.is_set() and release.wait(120))
        status = self.wait(lambda: (s := self.live_status()) and s.get("in_flight") and s)
        request(self.layout, status["run_id"], cancel=CancelMode.IMMEDIATE)
        asked = time.monotonic()
        thread.join(60)
        self.assertLess(time.monotonic() - asked, 15)
        self.assertNotIn("error", box)
        stopped = box["record"]
        self.assertEqual((stopped.state, stopped.compared_files), (VerificationState.STOPPED, 0))
        self.assertEqual(stopped.detail, "stopped by you; a.bin, which was being read, did not answer within 2 seconds "
                                         "and is compared again next time")
        hang.clear()
        release.set()
        record = self.verify()
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 2, 2))

    @unittest.skipUnless(WINDOWS, "Windows' refusals")
    def test_its_record_held_by_another_program_does_not_end_the_pass(self):
        thread, box = self.in_background(**self.slowly(8, 1024 * 1024))
        self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.VERIFYING.value)
        records = self.layout.verifications_dir / self.job.job_id
        path = self.wait(lambda: next(records.glob("*.json"), None))
        with held(path):
            before = os.stat(path).st_mtime_ns
            time.sleep(SAVE_INTERVAL + 2)
            self.assertEqual(os.stat(path).st_mtime_ns, before, "no save landed while the record was held")
            self.assertTrue(thread.is_alive(), "the pass ended while its record was held")
        self.slow.clear()
        thread.join(60)
        self.assertNotIn("error", box)
        record = box["record"]
        self.assertEqual((record.state, record.compared_files, record.sittings), (VerificationState.COMPLETED, 8, 1),
                         record.detail)
        self.assertEqual(read_verifications(self.layout, self.job.job_id)[-1].state, VerificationState.COMPLETED)

    @unittest.skipUnless(WINDOWS, "Windows' refusals")
    def test_a_waiting_verification_survives_the_job_file_saved_by_another_process(self):
        self.mirror({"a.txt": "a"})
        run = live_activity(self.layout, self.job.job_id, job_name="Work", reads=[endpoint(self.s, "Work")],
                            writes=[endpoint(self.d, "Backup")])
        self.addCleanup(run.close)
        thread, box = self.in_background()
        self.wait(lambda: (s := self.live_status()) and s.get("phase") == Phase.WAITING.value)
        saved = result_of(self, job_file_saver(self, self.store.path, 3.0))
        self.assertTrue(thread.is_alive(), "the waiting verification ended while the job file was saved")
        run.close()
        thread.join(60)
        self.assertNotIn("error", box)
        self.assertEqual((box["record"].state, saved["failures"]), (VerificationState.COMPLETED, 0))
        self.assertGreater(saved["saves"], 20)

    def test_a_verification_that_cannot_check_the_drives_says_why(self):
        class Failing(FixedVolumeSource):
            def scan(self):
                raise RuntimeError("the volumes could not be enumerated")

        with self.assertRaises(VerifyError) as caught:
            verify_destination(self.store, self.job.job_id, 1, volume_source=Failing(()))
        self.assertEqual(str(caught.exception),
                         "the drives could not be checked: RuntimeError: the volumes could not be enumerated")
        self.assertEqual(observe_runs(self.layout), [])


NEW = VerificationMode.NEW_COPIES


def stop_after(count):
    """An ``after_item`` that interrupts the pass once ``count`` entries are handled."""
    seen = []

    def interrupt(path):
        seen.append(path)
        if len(seen) == count:
            raise KeyboardInterrupt
    return interrupt


class NewCopiesTest(VerifyCase):
    """A verification of new copies reads only the copies no earlier pass found the same as
    they are now, counts the rest as unchanged, and never dates the destination's
    verification; where no list of verified copies is kept it is a full pass; each kind
    continues its own unfinished pass. The verifier's clock advances a second at each
    look, so passes made in one test never share a start."""

    def setUp(self):
        super().setUp()
        moments = (f"2026-10-19T{h:02d}:{m:02d}:{s:02d}Z" for h in range(10, 24) for m in range(60) for s in range(60))
        clock = mock.patch("mirrorhold.maintenance.verifier.utc_now", lambda: next(moments))
        clock.start()
        self.addCleanup(clock.stop)

    def records(self):
        return read_verifications(self.layout, self.job.job_id)

    def touch(self, rel, moment):
        for root in (self.src, self.dst):
            os.utime(root / rel, (moment, moment))

    def test_only_what_no_earlier_pass_found_the_same_is_read(self):
        self.mirror({"a.txt": "alpha", "b.txt": "bravo", "c.txt": "charlie"})
        full = self.verify()
        self.assertEqual((full.mode, full.compared_files), (VerificationMode.FULL, 3))
        self.assertEqual(len(verified.read_list(self.layout, full)), 3)
        self.mirror({"b.txt": "bravo, edited", "d.txt": "delta, backed up since"})
        self.touch("c.txt", 1_600_000_000)            # the engine set its time: same contents, another time
        handled = []
        record = self.verify(mode=NEW, after_item=handled.append)
        self.assertEqual((record.state, record.mode, record.baseline),
                         (VerificationState.COMPLETED, NEW, full.verification_id))
        self.assertEqual((record.compared_files, record.files_total, record.unchanged_files, record.differ_total),
                         (3, 3, 1, 0))
        self.assertEqual((record.compared_bytes, record.unchanged_bytes),
                         (len("bravo, edited") + len("charlie") + len("delta, backed up since"), len("alpha")))
        self.assertEqual(handled, ["a.txt", "b.txt", "c.txt", "d.txt"])
        self.assertEqual(last_full_verification(self.records(), self.marker), full.started)
        again = self.verify(mode=NEW)
        self.assertEqual((again.compared_files, again.unchanged_files, again.baseline),
                         (0, 4, record.verification_id))
        self.assertEqual(len(verified.read_list(self.layout, again)), 4)
        self.assertFalse(verified.list_path(self.layout, full).exists(), "the destination's older lists are removed")
        self.assertFalse(verified.list_path(self.layout, record).exists())

    def test_a_copy_damaged_since_it_was_read_is_left_to_a_full_pass(self):
        self.mirror({"photo.raw": b"\x00" * 4096})
        self.verify()
        damaged = self.dst / "photo.raw"
        times = os.stat(damaged)
        damaged.write_bytes(b"\x01" + b"\x00" * 4095)
        os.utime(damaged, ns=(times.st_atime_ns, times.st_mtime_ns))
        quick = self.verify(mode=NEW)
        self.assertEqual((quick.compared_files, quick.unchanged_files, quick.differ_total), (0, 1, 0))
        full = self.verify()
        self.assertEqual((full.mode, full.differ_total), (VerificationMode.FULL, 1))
        later = self.verify(mode=NEW)
        self.assertEqual((later.compared_files, later.differ_total), (1, 1), "a copy found to differ is read again")

    def test_without_a_list_it_reads_every_file_as_a_full_pass(self):
        self.mirror({f"f{i}.txt": "x" for i in range(3)})
        said = []
        record = self.verify(mode=NEW, say=said.append)
        self.assertEqual((record.mode, record.compared_files, record.baseline), (VerificationMode.FULL, 3, None))
        self.assertIn("no list of verified copies is kept for destination 1: every file is read, as a full "
                      "verification", said)
        self.assertEqual(last_full_verification(self.records(), self.marker), record.started)

    def test_a_pass_made_before_lists_were_kept_is_not_built_on(self):
        self.mirror({"a.txt": "a"})
        write_verification(self.layout, VerificationRecord("o" * 32, self.job.job_id, "Work", self.marker, 1,
                                                           "2026-09-01T10:00:00Z", "2026-09-01T10:05:00Z",
                                                           VerificationState.COMPLETED, "2026-09-01T10:05:00Z",
                                                           files_total=1, bytes_total=1, compared_files=1,
                                                           compared_bytes=1))
        self.assertIsNone(verified.kept(self.layout, self.records(), self.marker))
        record = self.verify(mode=NEW)
        self.assertEqual((record.mode, record.compared_files), (VerificationMode.FULL, 1))

    def test_without_a_list_it_continues_the_unfinished_full_pass(self):
        self.mirror({f"f{i}.txt": f"f{i}" for i in range(4)})
        with self.assertRaises(KeyboardInterrupt):
            self.verify(after_item=stop_after(2))
        stopped = self.records()[-1]
        said = []
        record = self.verify(mode=NEW, say=said.append)
        self.assertEqual((record.verification_id, record.mode, record.sittings, record.compared_files),
                         (stopped.verification_id, VerificationMode.FULL, 2, 4))
        self.assertIn("no list of verified copies is kept for destination 1: its unfinished full verification is "
                      "continued", said)

    def test_each_kind_continues_its_own_unfinished_pass(self):
        self.mirror({f"f{i}.txt": f"f{i}" * 10 for i in range(6)})
        self.verify()
        with self.assertRaises(KeyboardInterrupt):
            self.verify(restart=True, after_item=stop_after(2))
        full = self.records()[-1]
        self.assertEqual((full.mode, full.state), (VerificationMode.FULL, VerificationState.INTERRUPTED))
        self.mirror({f"g{i}.txt": f"new {i}" for i in range(3)})
        with self.assertRaises(KeyboardInterrupt):
            self.verify(mode=NEW, after_item=stop_after(7))      # the six unchanged, then the first new copy
        quick = self.records()[-1]
        self.assertEqual((quick.mode, quick.compared_files, quick.unchanged_files), (NEW, 1, 6))
        records = self.records()
        self.assertEqual(unfinished(records, self.marker).verification_id, full.verification_id,
                         "the window names the unfinished full pass first")
        self.assertEqual(resumable(records, self.marker, mode=NEW).verification_id, quick.verification_id)
        again = self.verify(mode=NEW)
        self.assertEqual((again.verification_id, again.sittings, again.compared_files, again.unchanged_files),
                         (quick.verification_id, 2, 3, 6))
        self.assertEqual(resumable(self.records(), self.marker).verification_id, full.verification_id,
                         "a pass of new copies leaves the full pass to be continued")
        self.assertTrue(verified.partial_path(self.layout, full).exists(), "and its partial list")
        rest = self.verify()
        self.assertEqual((rest.verification_id, rest.sittings, rest.compared_files), (full.verification_id, 2, 9))

    def test_a_full_pass_finished_since_ends_an_unfinished_pass_of_new_copies(self):
        self.mirror({"a.txt": "a"})
        self.verify()
        self.mirror({f"n{i}.txt": "n" for i in range(3)})
        with self.assertRaises(KeyboardInterrupt):
            self.verify(mode=NEW, after_item=stop_after(2))
        quick = self.records()[-1]
        self.verify()
        self.assertIsNone(resumable(self.records(), self.marker, mode=NEW))
        self.assertFalse(verified.partial_path(self.layout, quick).exists())
        record = self.verify(mode=NEW)
        self.assertNotEqual(record.verification_id, quick.verification_id)
        self.assertEqual((record.compared_files, record.unchanged_files), (0, 4))

    def test_what_a_stopped_pass_found_the_same_is_kept_for_its_continuation(self):
        self.mirror({f"f{i}.txt": f"f{i}" for i in range(4)})
        with self.assertRaises(KeyboardInterrupt):
            self.verify(after_item=stop_after(2))
        stopped = self.records()[-1]
        partial = verified.partial_path(self.layout, stopped)
        self.assertEqual(partial.stat().st_size, len(verified.SIGNATURE) + 2 * 8)
        record = self.verify()
        self.assertEqual(len(verified.read_list(self.layout, record)), 4)
        self.assertFalse(partial.exists())


class VerifiedListTest(TempDirTest):
    """The list of verified copies on its own: fingerprints, a list written across sittings,
    a torn entry, a list read from its partial list, and which list a pass builds on."""

    def setUp(self):
        super().setUp()
        self.layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        self.record = self.finished("v" * 32, "2026-10-19T10:00:00Z")

    @staticmethod
    def finished(verification_id, moment):
        return VerificationRecord(verification_id, JOB, "Job", MARKER, 1, moment, moment, VerificationState.COMPLETED,
                                  moment)

    def keep(self, record, values):
        writer = verified.ListWriter(self.layout, record, continuing=False)
        for value in values:
            writer.add(value)
        self.assertTrue(writer.finish())

    def test_a_fingerprint_changes_with_the_key_the_size_and_either_time(self):
        base = verified.fingerprint("a.txt", 5, 10, 10)
        self.assertEqual(base, verified.fingerprint("a.txt", 5, 10, 10))
        for other in (("b.txt", 5, 10, 10), ("a.txt", 6, 10, 10), ("a.txt", 5, 11, 10), ("a.txt", 5, 10, 11)):
            self.assertNotEqual(base, verified.fingerprint(*other))
        self.assertLess(base, 2 ** 64)
        self.assertIsInstance(verified.fingerprint("\ud800 unpaired", 0, -1, 0), int)

    def test_a_list_written_across_sittings_is_sorted_each_once_and_a_torn_entry_dropped(self):
        writer = verified.ListWriter(self.layout, self.record, continuing=False)
        for value in (5, 3, 9):
            writer.add(value)
        self.assertTrue(writer.flush())
        with open(writer.path, "ab") as handle:
            handle.write(b"\x01\x02\x03")
        again = verified.ListWriter(self.layout, self.record, continuing=True)
        for value in (3, 1):
            again.add(value)
        self.assertTrue(again.finish())
        self.assertFalse(writer.path.exists())
        self.assertEqual(verified.list_path(self.layout, self.record).read_bytes(),
                         verified.SIGNATURE + b"".join(v.to_bytes(8, "little") for v in (1, 3, 5, 9)))
        copies = verified.read_list(self.layout, self.record)
        self.assertEqual(([v in copies for v in (1, 3, 5, 9, 2, 10)], len(copies)), ([True] * 4 + [False] * 2, 4))

    def test_an_append_that_failed_partway_is_written_again_in_its_place(self):
        writer = verified.ListWriter(self.layout, self.record, continuing=False)
        for value in (5, 3):
            writer.add(value)
        self.assertTrue(writer.flush())
        with open(writer.path, "ab") as handle:
            handle.write((9).to_bytes(8, "little")[:5])     # what a full disk let through of the next append
        for value in (9, 1):
            writer.add(value)
        self.assertTrue(writer.flush())
        self.assertEqual(writer.path.read_bytes(),
                         verified.SIGNATURE + b"".join(v.to_bytes(8, "little") for v in (5, 3, 9, 1)))
        writer.path.unlink()
        writer.add(7)
        self.assertFalse(writer.flush(), "a partial list gone since is begun again")
        self.assertTrue(writer.flush())
        self.assertEqual(writer.path.read_bytes(), verified.SIGNATURE + (7).to_bytes(8, "little"))

    @unittest.skipUnless(WINDOWS, "a file held without sharing is Windows'")
    def test_a_list_another_program_holds_is_waited_for(self):
        self.keep(self.record, (4, 2))
        path = verified.list_path(self.layout, self.record)
        with held(path):
            self.assertEqual(verified.kept(self.layout, [self.record], MARKER), self.record,
                             "a list held now is there all the same")
        let_go_after(1.0, held(path))
        copies = verified.read_list(self.layout, self.record)
        self.assertEqual((len(copies), 4 in copies), (2, True))

    def test_a_finished_pass_whose_list_was_not_sorted_is_read_from_its_partial_list(self):
        writer = verified.ListWriter(self.layout, self.record, continuing=False)
        for value in (7, 2, 7):
            writer.add(value)
        self.assertTrue(writer.flush())
        copies = verified.read_list(self.layout, self.record)
        self.assertEqual((len(copies), 2 in copies, 7 in copies), (2, True, True))
        writer.path.write_bytes(b"not a list")
        self.assertIsNone(verified.read_list(self.layout, self.record))

    def test_a_pass_builds_on_the_latest_finished_list_that_can_be_read(self):
        older, newer = self.finished("o" * 32, "2026-10-01T10:00:00Z"), self.finished("n" * 32, "2026-10-02T10:00:00Z")
        unlisted = self.finished("u" * 32, "2026-10-03T10:00:00Z")
        self.keep(older, (1,))
        self.keep(newer, (2,))
        records = [older, newer, unlisted]
        self.assertEqual(verified.kept(self.layout, records, MARKER).verification_id, newer.verification_id)
        found, copies = verified.baseline(self.layout, records, MARKER)
        self.assertEqual((found.verification_id, 2 in copies, 1 in copies), (newer.verification_id, True, False))
        self.assertEqual(verified.baseline(self.layout, records, MARKER, prefer=older.verification_id)[0],
                         older)
        self.assertIsNone(verified.kept(self.layout, records, "c" * 32))
        verified.prune(self.layout, records, newer)
        self.assertEqual((verified.list_path(self.layout, older).exists(),
                          verified.list_path(self.layout, newer).exists()), (False, True))


class VerificationKindsTest(TempDirTest):
    """The kind of pass a verification record keeps (schema 2 on), the reminder and the
    notices of a pass of new copies, and the arguments that start one."""

    def test_the_kind_round_trips_and_a_schema_1_record_reads_as_a_full_pass(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        moment = "2026-10-19T10:00:00Z"
        record = VerificationRecord("v" * 32, JOB, "Job", MARKER, 1, moment, moment, VerificationState.COMPLETED,
                                    moment, mode=NEW, baseline="b" * 32, unchanged_files=4, unchanged_bytes=40)
        doc = json.loads(write_verification(layout, record).read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["mode"], doc["baseline"]),
                         (VERIFICATION_SCHEMA, "new-copies", "b" * 32))
        again = read_verifications(layout, JOB)[0]
        self.assertEqual((again.mode, again.baseline, again.unchanged_files, again.unchanged_bytes),
                         (NEW, "b" * 32, 4, 40))
        unknown = dict(doc, mode="quick")
        with self.assertRaises(ValueError):
            VerificationRecord.from_document(unknown)
        for key in ("measure", "fingerprint", "read_files", "read_bytes"):       # schema 3's
            del doc[key]
        doc["schema_version"] = 2
        second = VerificationRecord.from_document(doc)
        self.assertEqual((second.mode, second.baseline, second.unchanged_files, second.unchanged_bytes),
                         (NEW, "b" * 32, 4, 40))
        for key in ("mode", "baseline", "unchanged_files", "unchanged_bytes"):
            del doc[key]
        with self.assertRaises(ValueError):                                       # schema 2 without its fields
            VerificationRecord.from_document(doc)
        doc["schema_version"] = 1
        older = VerificationRecord.from_document(doc)
        self.assertEqual((older.mode, older.baseline, older.unchanged_files), (VerificationMode.FULL, None, 0))
        doc["schema_version"] = VERIFICATION_SCHEMA + 1
        with self.assertRaises(ValueError):
            VerificationRecord.from_document(doc)

    def test_a_pass_of_new_copies_never_dates_the_verification(self):
        runs = [run_record(ended="2026-06-01T10:00:00Z")]
        quick = replace(verification(), mode=NEW)
        self.assertIsNone(last_full_verification([quick], MARKER))
        self.assertIsNotNone(verification_due(90, MARKER, runs, [quick], NOW))
        self.assertIsNone(verification_due(90, MARKER, runs, [verification(), replace(quick, verification_id="w" * 32)],
                                           NOW))

    def test_the_notices_of_a_pass_of_new_copies(self):
        clean = replace(verification(), mode=NEW, compared_files=2, compared_bytes=2048, unchanged_files=1234)
        notice = verification_notice(clean, Notifications())
        self.assertEqual(notice.title, "Job: verification of new copies found no differences")
        self.assertTrue(notice.lines[0].startswith("Destination 1: 2 files compared ("), notice.lines[0])
        self.assertTrue(notice.lines[0].endswith("; 1,234 verified earlier and unchanged, not read"), notice.lines[0])
        clean.add_differ("photo.raw", 10)
        clean.add_differ("video.mov", 20)
        self.assertEqual(verification_notice(clean, Notifications()).title,
                         "Job: verification of new copies found 2 files that differ")
        failed = replace(verification(VerificationState.FAILED), mode=NEW, detail="the source's drive is not attached")
        self.assertEqual(verification_notice(failed, Notifications()).title,
                         "Job: a verification of new copies did not finish")

    def test_the_arguments_of_a_verification_of_new_copies(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        marker = "c" * 32
        self.assertNotIn("--new-copies", verify_arguments(store, "j", marker))
        self.assertEqual(verify_arguments(store, "j", marker, new_copies=True, restart=True)[-2:],
                         ["--new-copies", "--restart"])

    def test_the_arguments_of_a_verification_name_its_destination_by_marker(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        self.assertEqual(verify_arguments(store, "j", "b" * 32, new_copies=True)[-5:],
                         ["verify", "j", "--marker", "b" * 32, "--new-copies"])
        self.assertNotIn("--dest", verify_arguments(store, "j", "b" * 32))


class DifferNoticeWordsTest(unittest.TestCase):
    """A verification's notice counts the copies that differ with the verb in the number the
    count needs: one file differs, two or more differ (the maintainer's decision, 2026-10-19)."""

    def test_one_file_differs_and_two_files_differ(self):
        for mode, what in ((VerificationMode.FULL, "verification"), (NEW, "verification of new copies")):
            with self.subTest(mode=mode.value):
                record = replace(verification(), mode=mode)
                record.add_differ("Pictures\\photo.raw", 4096)
                notice = verification_notice(record, Notifications())
                self.assertEqual((notice.title, notice.lines),
                                 (f"Job: {what} found 1 file that differs",
                                  ("Destination 1: 1 file differs from the source although size and date match "
                                   "(for example photo.raw)",)))
                record.add_differ("video.mov", 10)
                notice = verification_notice(record, Notifications())
                self.assertEqual((notice.title, notice.lines),
                                 (f"Job: {what} found 2 files that differ",
                                  ("Destination 1: 2 files differ from the source although size and date match "
                                   "(for example photo.raw)",)))


class DifferLineWordsTest(unittest.TestCase):
    """A verification's line in the command line's history counts the copies that differ
    with the verb in the number the count needs, as its notice does."""

    def test_one_differs_and_two_differ(self):
        record = verification()
        record.add_differ("Pictures\\photo.raw", 4096)
        self.assertIn(" bytes), 1 differs", record.describe())
        record.add_differ("video.mov", 10)
        self.assertIn(" bytes), 2 differ", record.describe())
        self.assertNotIn("2 differs", record.describe())


class VerificationEstimateTest(VerifyCase):
    """A verification's expected time counts its files beside its bytes, 2.5 ms to 15 ms a
    file, and the rate it learns from earlier passes leaves 2.5 ms a file out (the
    maintainer's decision, 2026-10-19)."""

    def test_the_estimate_a_pass_publishes_counts_each_file(self):
        self.mirror({f"f{i:03}.txt": "x" * 100 for i in range(40)})
        seen = []

        def look(_path):
            if not seen:
                seen.append(self.wait(lambda: ((self.live_status() or {}).get("verify") or {}).get("estimate")))

        self.verify(after_item=look)
        guess = seen[0]
        self.assertEqual((guess["files"], guess["bytes"], guess["samples"]), (40, 4000, 0))
        self.assertEqual((guess["low_file_seconds"], guess["high_file_seconds"]), (0.0025, 0.015))
        self.assertAlmostEqual(guess["shortest"], 4000 / DEFAULT_HIGH + 40 * 0.0025)
        self.assertAlmostEqual(guess["longest"], 4000 / DEFAULT_LOW + 40 * 0.015)

    def test_a_rate_from_an_earlier_pass_leaves_out_each_files_own_cost(self):
        mib = 1024 * 1024
        record = replace(verification(), compared_files=10_000, compared_bytes=100 * mib, seconds=35.0)
        self.assertAlmostEqual(record.throughput, 100 * mib / 10.0)
        self.assertIsNone(replace(record, seconds=25.0).throughput, "no time left for the bytes measures no rate")
        self.assertIsNone(replace(record, compared_bytes=mib).throughput, "too few bytes to measure")


class ReminderTest(unittest.TestCase):
    def test_due_after_the_last_verification_or_the_first_completed_run(self):
        runs = [run_record(ended="2026-06-01T10:00:00Z")]
        self.assertIsNone(verification_due(90, MARKER, [], [], NOW))
        self.assertIsNone(verification_due(90, MARKER, [run_record(Outcome.HELD)], [], NOW))
        self.assertEqual(verification_due(90, MARKER, runs, [], NOW), datetime(2026, 8, 30, 10, tzinfo=timezone.utc))
        self.assertIsNone(verification_due(90, MARKER, runs, [verification()], NOW))
        self.assertIsNotNone(verification_due(30, MARKER, runs, [verification()], NOW))
        stopped = verification(VerificationState.STOPPED)
        self.assertIsNotNone(verification_due(90, MARKER, runs, [stopped], NOW))
        self.assertIsNone(last_full_verification([stopped], MARKER))


class ReminderAfterRunTest(TempDirTest):
    """A run's notice is told which destinations it backed up are due a full verification."""

    def test_the_destinations_a_run_backed_up_that_are_due(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        other = "c" * 32
        write_record(layout, run_record(ended="2026-06-01T10:00:00Z"))
        write_record(layout, replace(run_record(ended="2026-06-01T10:00:00Z", marker=other), destination_index=2,
                                     run_id="s" * 32))
        write_verification(layout, verification(ended="2026-09-01T10:00:00Z", marker=other))
        this_run = [run_record(ended="2026-10-07T11:00:00Z"),
                    replace(run_record(ended="2026-10-07T11:00:00Z", marker=other), destination_index=2)]
        self.assertEqual(due_after_run(layout, 90, this_run, NOW), ((1, None),))
        self.assertEqual(due_after_run(layout, 30, this_run, NOW), ((1, None), (2, "2026-09-01T09:00:00Z")))
        self.assertEqual(due_after_run(layout, 90, [run_record(Outcome.FAILED)], NOW), (),
                         "a failed run reminds of nothing")
        self.assertEqual(due_after_run(layout, 90, [run_record(Outcome.HELD)], NOW), ((1, None),))
        self.assertEqual(due_after_run(layout, 90, [], NOW), ())


class RecordSchemaTest(TempDirTest):
    def test_the_current_schema_round_trips_and_older_records_still_read(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        cleaned = VersionsCleaned(30, ("2026-06-01T10-00-00Z",), 1, 2, 300, ("notes is not a versions batch",))
        record = run_record(space=(5 * 1024 ** 3, 100 * 1024 ** 3), cleaned=cleaned)
        path = write_record(layout, record)
        again = read_records(layout, JOB)[0]
        self.assertEqual((again.versions_cleaned, again.space), (cleaned, record.space))
        self.assertTrue(again.low_on_space)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["schema_version"] = 2
        del doc["versions_cleaned"], doc["space"], doc["encrypted"]
        del doc["done"]["files_identical"], doc["done"]["bytes_identical"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        older = read_records(layout, JOB)[0]
        self.assertEqual((older.versions_cleaned, older.space, older.low_on_space), (None, None, False))
        doc["schema_version"] = 1
        del doc["source_read"], doc["not_backed_up"]
        oldest = RunRecord.from_document(doc)
        self.assertEqual((oldest.source_read, oldest.not_backed_up, oldest.versions_cleaned), (None, (), None))
        doc["schema_version"] = RECORD_SCHEMA + 1
        with self.assertRaises(ValueError):
            RunRecord.from_document(doc)

    def test_low_space_is_below_ten_per_cent(self):
        self.assertTrue(low_on_space(9, 100))
        self.assertFalse(low_on_space(10, 100))
        self.assertFalse(low_on_space(0, 0))
        line = notice_for([run_record(space=(6, 100))], Notifications()).lines[0]
        self.assertTrue(line.endswith("- only 6 % free on its drive"), line)

    def test_verification_notices(self):
        clean = verification()
        clean.compared_files, clean.compared_bytes = 3, 2048
        self.assertEqual(verification_notice(clean, Notifications()).title, "Job: verification found no differences")
        self.assertIsNone(verification_notice(clean, Notifications(on_completion=False)))
        self.assertIsNone(verification_notice(verification(VerificationState.STOPPED), Notifications()))
        failed = verification(VerificationState.FAILED)
        failed.detail = "the destination's drive is not attached"
        self.assertIn("not attached", verification_notice(failed, Notifications()).lines[0])
        self.assertIsNone(verification_notice(None, Notifications()))


# --- the command line and a launched verification ----------------------------------------

class RecordRenamedOverTest(TempDirTest):
    """A verification rewrites its record as its pass goes on: a record the folder's listing
    named and that is not there for the moment it is renamed over is looked for again, never
    passed over."""

    def test_a_verification_record_met_as_it_is_renamed_over_is_still_read(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        path = Path(write_verification(layout, verification()))
        real, missed = exchange.open_shared, []

        def opening(opened):
            if Path(opened) == path and not missed:
                missed.append(opened)
                raise FileNotFoundError(2, "renamed over", str(opened))
            return real(opened)

        with mock.patch.object(exchange, "open_shared", side_effect=opening):
            self.assertEqual(len(read_verifications(layout, JOB)), 1)
        self.assertEqual(len(missed), 1)


class VerifyCommandTest(VerifyCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store.path), *args], volume_source=self.volumes, sync_roots=(), out=out,
                    err=err)
        return code, out.getvalue(), err.getvalue()

    def test_verify_and_history(self):
        self.mirror({"a.txt": "a", "b.txt": "b"})
        code, out, err = self.cli("verify", "Work")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("verification completed: 2 of 2 files compared", out)
        damaged = self.dst / "b.txt"
        times = os.stat(damaged)
        damaged.write_text("c", encoding="utf-8")
        os.utime(damaged, ns=(times.st_atime_ns, times.st_mtime_ns))
        code, out, _ = self.cli("verify", "Work", "--dest", "1", "--restart")
        self.assertEqual(code, EXIT_FAILED, out)
        self.assertIn("1 differ", out)
        code, out, _ = self.cli("history", "Work")
        self.assertEqual(out.count("verification completed"), 2, out)

    def test_verify_new_copies_and_history(self):
        self.mirror({"a.txt": "a", "b.txt": "b"})
        code, out, err = self.cli("verify", "Work", "--new-copies")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("no list of verified copies is kept for destination 1", out)
        self.assertIn("verification completed: 2 of 2 files compared", out)
        self.mirror({"c.txt": "c"})
        code, out, _ = self.cli("verify", "Work", "--new-copies")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("verifying the new copies of destination 1 of 'Work'", out)
        self.assertIn("verification of new copies completed: 1 of 1 file compared (1 of 1 byte), 0 differ; 2 "
                      "verified earlier and unchanged, not read", out)
        code, out, _ = self.cli("history", "Work")
        self.assertEqual((out.count("verification of new copies completed"), out.count("verification completed")),
                         (1, 1), out)

    def test_a_verification_that_cannot_start_is_refused(self):
        code, _, err = self.cli("verify", "Work", "--dest", "3")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("no destination 3", err)

    def test_verify_takes_its_destination_by_marker_or_a_unique_prefix_of_it(self):
        self.mirror({"a.txt": "a"})
        self.job = replace(self.job, destinations=(Destination(endpoint(self.d, "Other")),) + self.job.destinations)
        self.store.save(StoreContents((self.job,)))
        code, out, err = self.cli("verify", "Work", "--marker", self.marker[:6])
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertEqual(read_verifications(self.layout, self.job.job_id)[-1].marker_id, self.marker)
        code, _, err = self.cli("verify", "Work")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("name one with --dest or --marker", err)
        code, _, err = self.cli("verify", "Work", "--marker", self.marker[:3])
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(f"'{self.marker[:3]}' is too short: a marker is named by its identifier, or by its first four "
                      "characters or more", err)
        self.assertEqual(self.cli("verify", "Work", "--dest", "2", "--marker", self.marker)[0], EXIT_USAGE)


@unittest.skipUnless(WINDOWS, "the production volume source is Windows-only")
class LaunchedVerificationTest(TempDirTest):
    """Live path: a verification started as a process of its own, the way the GUI starts
    one, on this machine's real volume source (read-only), with a store in a temporary
    folder."""

    def test_a_launched_verification_runs_by_itself_and_records_its_pass(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        source, target = self.tmp / "src", self.tmp / "dst"
        write(source, "a.txt", "alpha")
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(store), "add", "Live",
                                 "--source", str(source), "--dest", str(target), "--no-survey"], cwd=PROJECT,
                                capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = ConfigStore(store).load().contents.jobs[0]
        target.mkdir()
        shutil.copy2(source / "a.txt", target / "a.txt")
        write_marker(target, Marker(job.job_id, job.destinations[0].marker_id))
        launch = start_verification(store, job.job_id, job.destinations[0].marker_id)
        try:
            launch.process.wait(120)
            self.assertEqual((launch.ended(), launch.problem()), (EXIT_OK, None))
        finally:
            launch.discard()
        record = read_verifications(runtime_layout(store), job.job_id)[-1]
        self.assertEqual((record.state, record.compared_files), (VerificationState.COMPLETED, 1))

    def test_a_launched_verification_that_cannot_start_says_why_in_one_line_and_in_its_log(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        source, target = self.tmp / "src", self.tmp / "dst"
        write(source, "a.txt", "alpha")
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(store), "add", "Live",
                                 "--source", str(source), "--dest", str(target), "--no-survey"], cwd=PROJECT,
                                capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = ConfigStore(store).load().contents.jobs[0]
        marker = job.destinations[0].marker_id
        launch = start_verification(store, job.job_id, marker)
        try:
            launch.process.wait(120)
            self.assertEqual(launch.ended(), EXIT_REFUSED)
            self.assertEqual(launch.problem(), "refused: cannot verify: the destination has no marker of this job "
                                               "yet: its first run has not been confirmed")
        finally:
            launch.discard()
        logs = sorted(runtime_layout(store).logs_dir.glob("*-verify-*.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn(f"verify {job.job_id} --marker {marker}", text)
        self.assertIn("refused: cannot verify: the destination has no marker of this job yet", text)

    def test_a_launched_verification_finds_its_destination_by_marker_and_its_log_numbers_it(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        source, target = self.tmp / "src", self.tmp / "dst"
        write(source, "a.txt", "alpha")
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(store), "add", "Live",
                                 "--source", str(source), "--dest", str(self.tmp / "other"), "--dest", str(target),
                                 "--no-survey"], cwd=PROJECT, capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = ConfigStore(store).load().contents.jobs[0]
        marker = job.destinations[1].marker_id
        target.mkdir()
        shutil.copy2(source / "a.txt", target / "a.txt")
        write_marker(target, Marker(job.job_id, marker))
        launch = start_verification(store, job.job_id, marker)
        try:
            launch.process.wait(120)
            self.assertEqual((launch.ended(), launch.problem()), (EXIT_OK, None))
        finally:
            launch.discard()
        record = read_verifications(runtime_layout(store), job.job_id)[-1]
        self.assertEqual((record.state, record.marker_id, record.destination_index),
                         (VerificationState.COMPLETED, marker, 2))
        logs = sorted(runtime_layout(store).logs_dir.glob("*-verify-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn(f"verify {job.job_id} --marker {marker}", logs[0].read_text(encoding="utf-8"))
        self.assertEqual([log.destinations for log in diagnostics.LogWatch(runtime_layout(store)).look()], [(2,)],
                         "the log numbers the destination it was given by marker, as the job then stood")


if __name__ == "__main__":
    unittest.main()
