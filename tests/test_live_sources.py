"""Live sources: reading a source from a volume snapshot, and the files a run could not back up.

Snapshots come from a provider in memory (``FakeProvider``): a snapshot of a volume is a
folder holding what the test says the volume held at that instant, and the provider
remembers what it made, what it removed and which snapshots someone else made. No test
here makes, lists or removes a real snapshot — ``support`` switches the real provider
off for the whole process; the real one was checked once by hand on the development
machine. The run itself is real: the pinned engine copies from the snapshot's folder on
temporary folders mapped as volumes.

Covered: the decision per run (switched off, nothing to copy to, a drive that cannot
have one, a drive holding someone else's snapshots, a failure, a snapshot made), the
run reading, judging and copying from the snapshot — onto a remote too, and no snapshot
while that remote cannot be reached — a held file backed up through it,
the snapshot kept through a pause and removed whatever the run's end, a dead run's
snapshot removed by the next run and nothing else, a live run's left alone; a skipped
file's runs in a row, the record's schema, the notice; the provider's mapping of
Windows' answers; a source root spelled as a snapshot's path is, listed and opened the
engine's way.
"""
import ctypes
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from support import GIB, TempDirTest, endpoint, live_activity, volume
from test_engine_contract import (MiB, current_user, device_path, drop_backup_privileges, growing_partial, icacls,
                                  needs_engine)
from test_run import Held, RunCase, tree, write

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.engine.rclone import open_source_file
from mirrorhold.jobs.model import Notifications, Preferences, RemoteSettings, new_job
from mirrorhold.jobs.store import StoreContents
from mirrorhold.locations import RuntimeLayout
from mirrorhold.notifications.message import notice_for
from mirrorhold.planning.listing import Side, list_tree
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, RECORD_SCHEMA, Outcome, ReadMode, RunRecord,
                                       SourceRead, last_counted, read_records, unbacked_streaks, write_record)
from mirrorhold.records.status import CancelMode, request
from mirrorhold.run.runner import RunOptions, run_job
from mirrorhold.snapshots import own
from mirrorhold.snapshots.provider import (Snapshot, SnapshotError, SnapshotFailure, SnapshotRoom, UnavailableProvider,
                                           default_snapshot_provider)
from mirrorhold.snapshots.reading import (NOT_NEEDED, SNAPSHOT_OFF, SPARE_FLOOR, UNSUPPORTED_DRIVE, WOULD_DISPLACE,
                                          SourceSnapshot, remove_leftovers, room_to_spare, spare_needed)
from mirrorhold.snapshots.windows import (WmiSnapshotProvider, com_code, failure_for_com, failure_for_return,
                                          referenced_device, valid_id)

WINDOWS = sys.platform == "win32"
FILE_ATTRIBUTE_OFFLINE = 0x1000


def shadow_id(n: int) -> str:
    return "{%08X-0000-4000-8000-%012X}" % (n, n)


class FakeProvider:
    """Snapshots in memory. ``prepare(live_root, shadow_root)`` fills a new snapshot; by
    default it copies the volume's folder as it is, times kept."""

    def __init__(self, folder: Path, roots):
        self.folder = Path(folder)
        self.roots = {device.casefold(): Path(root) for device, root in roots.items()}
        self.existing = {}           # identifier -> volume device
        self.made, self.deleted = [], []
        self.failure = None
        self.prepare = lambda live, shadow: shutil.copytree(live, shadow, copy_function=shutil.copy2)

    def foreign(self, volume: str, n: int) -> str:
        """A snapshot someone else made."""
        self.existing[shadow_id(n)] = volume
        return shadow_id(n)

    def shadow_ids(self, volume):
        return frozenset(i for i, v in self.existing.items() if v.casefold() == volume.casefold())

    def create(self, volume):
        if self.failure is not None:
            raise self.failure
        n = 100 + len(self.made)
        device = self.folder / f"shadow{n}"
        self.prepare(self.roots[volume.casefold()], device)
        self.existing[shadow_id(n)] = volume
        self.made.append(shadow_id(n))
        return Snapshot(shadow_id(n), str(device), volume)

    def delete(self, identifier):
        self.deleted.append(identifier)
        return self.existing.pop(identifier, None) is not None


class RoomyProvider(FakeProvider):
    """Snapshots in memory, and the volume's room for them as ``room_of`` says; reading it
    fails with ``room_failure`` when that is set."""

    def __init__(self, folder: Path, roots):
        super().__init__(folder, roots)
        self.room_of = None
        self.room_failure = None

    def room(self, volume):
        if self.room_failure is not None:
            raise self.room_failure
        return self.room_of


def run_record(outcome=Outcome.COMPLETED, *, unbacked=(), reasons=(), marker="m" * 32, skipped=()):
    return RunRecord("r" * 32, "j" * 32, "Job", marker, 1, "manual", "2026-10-06T10:00:00Z", "2026-10-06T10:05:00Z",
                     outcome, tuple(reasons), planned=dict.fromkeys(PLANNED_KEYS, 0), done=dict.fromkeys(DONE_KEYS, 0),
                     left=dict.fromkeys(LEFT_KEYS, 0), skipped=tuple(skipped), skipped_total=len(skipped),
                     not_backed_up=tuple(unbacked))


# --- the decision, without the engine -------------------------------------------------

class SourceSnapshotTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.live = self.tmp / "volS"
        (self.live / "Work").mkdir(parents=True)
        self.s = volume(1, mounts=(str(self.live) + "\\",), label="S")
        self.layout = RuntimeLayout(self.tmp / "Config")
        self.provider = FakeProvider(self.tmp / "shadows", {self.s.device: self.live})
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(volume(2, "F"), "Backup")])

    def take(self, job=None, vol=None, needed=True):
        reading = SourceSnapshot(self.layout, "a" * 32, self.provider)
        return reading, reading.take(job or self.job, vol or self.s, needed=needed)

    def test_a_snapshot_is_made_recorded_and_removed(self):
        reading, read = self.take()
        self.assertEqual(read.mode, ReadMode.SNAPSHOT)
        self.assertTrue(read.taken)
        self.assertEqual(reading.root("Work"), Path(str(self.tmp / "shadows" / "shadow100") + "\\Work"))
        self.assertEqual([o.shadow_id for o in own.recorded(self.layout)], self.provider.made)
        self.assertEqual(reading.release(), "")
        self.assertEqual((self.provider.deleted, own.recorded(self.layout)), (self.provider.made, []))
        self.assertIsNone(reading.root("Work"))

    def test_each_reason_to_read_as_it_is(self):
        off = replace(self.job, source=replace(self.job.source, read_from_snapshot=False))
        self.assertEqual(self.take(job=off)[1].reason, SNAPSHOT_OFF)
        self.assertEqual(self.take(needed=False)[1].reason, NOT_NEEDED)
        fat = replace(self.s, file_system="FAT32")
        self.assertEqual(self.take(vol=fat)[1].reason, UNSUPPORTED_DRIVE)
        self.assertEqual(self.provider.made, [])
        self.provider.failure = SnapshotError(SnapshotFailure.DENIED, "administrator rights are needed")
        _reading, denied = self.take()
        self.assertEqual((denied.mode, denied.reason), (ReadMode.LIVE, "snapshot-denied"))
        self.assertEqual(own.recorded(self.layout), [])
        self.provider.failure = None
        foreign = self.provider.foreign(self.s.device, 1)
        _reading, displace = self.take()
        self.assertEqual((displace.reason, self.provider.made), (WOULD_DISPLACE, []))
        self.assertIn(foreign, self.provider.existing)

    def test_a_provider_that_makes_nothing_reads_as_it_is(self):
        reading = SourceSnapshot(self.layout, "a" * 32, UnavailableProvider("not here"))
        read = reading.take(self.job, self.s)
        self.assertEqual((read.mode, read.reason, read.detail), (ReadMode.LIVE, "snapshot-unavailable", "not here"))

    def test_leftovers_are_removed_by_recorded_identifier_only(self):
        left = self.provider.foreign(self.s.device, 1)
        other = self.provider.foreign(self.s.device, 2)
        own.record_snapshot(self.layout, own.record_request(self.layout, "d" * 32, self.s.device, "S:"),
                            Snapshot(left, "x", self.s.device))
        own.record_request(self.layout, "e" * 32, self.s.device, "S:")
        notes = remove_leftovers(self.layout, self.provider)
        self.assertEqual(self.provider.deleted, [left])
        self.assertIn(other, self.provider.existing)
        self.assertEqual(own.recorded(self.layout), [])
        self.assertEqual(len(notes), 2)
        stuck = self.provider.foreign(self.s.device, 3)
        own.record_snapshot(self.layout, own.record_request(self.layout, "f" * 32, self.s.device, "S:"),
                            Snapshot(stuck, "x", self.s.device))
        notes = remove_leftovers(self.layout, UnavailableProvider("not here"))
        self.assertEqual([o.shadow_id for o in own.recorded(self.layout)], [stuck])
        self.assertIn("could not be removed", notes[0])

    def test_two_runs_making_snapshots_at_once_take_turns_and_neither_reads_the_other_as_foreign(self):
        made, go = threading.Event(), threading.Event()
        create = self.provider.create

        def slow_create(device):
            snapshot = create(device)
            if not made.is_set():
                made.set()
                go.wait(10)                               # made, and not yet recorded by its run
            return snapshot

        self.provider.create = slow_create
        readings = [SourceSnapshot(self.layout, run * 32, self.provider) for run in "ab"]
        reads = {}

        def take(n):
            reads[n] = readings[n].take(self.job, self.s)

        threads = [threading.Thread(target=take, args=(n,), daemon=True) for n in range(2)]
        threads[0].start()
        self.assertTrue(made.wait(10))
        threads[1].start()
        time.sleep(0.3)
        self.assertNotIn(1, reads)
        go.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual([reads[n].mode for n in range(2)], [ReadMode.SNAPSHOT, ReadMode.SNAPSHOT])
        self.assertEqual(len(self.provider.made), 2)
        for reading in readings:
            self.assertEqual(reading.release(), "")
        self.assertEqual(own.recorded(self.layout), [])

    def test_a_run_in_progress_keeps_its_snapshot_and_its_record(self):
        other = live_activity(self.layout, self.job.job_id, job_name="Work")
        self.addCleanup(other.close)
        theirs = self.provider.foreign(self.s.device, 1)
        own.record_snapshot(self.layout, own.record_request(self.layout, other.status.run_id, self.s.device, "S:"),
                            Snapshot(theirs, "x", self.s.device))
        being_made = live_activity(self.layout, self.job.job_id, job_name="Work")
        self.addCleanup(being_made.close)
        own.record_request(self.layout, being_made.status.run_id, self.s.device, "S:")
        self.assertEqual(remove_leftovers(self.layout, self.provider), [])
        self.assertEqual((self.provider.deleted, len(own.recorded(self.layout))), ([], 2))
        other.close()
        remove_leftovers(self.layout, self.provider)
        self.assertEqual(self.provider.deleted, [theirs])
        self.assertEqual([o.run_id for o in own.recorded(self.layout)], [being_made.status.run_id])


class RoomToSpareTest(TempDirTest):
    """Beside snapshots someone else made, a snapshot is made only while the volume's room
    for snapshots has room to spare, so Windows never deletes one of theirs to make space
    for it (the maintainer's decision of 2026-10-17)."""

    def setUp(self):
        super().setUp()
        self.live = self.tmp / "volS"
        (self.live / "Work").mkdir(parents=True)
        self.s = volume(1, mounts=(str(self.live) + "\\",), label="S")
        self.layout = RuntimeLayout(self.tmp / "Config")
        self.provider = RoomyProvider(self.tmp / "shadows", {self.s.device: self.live})
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(volume(2, "F"), "Backup")])
        self.theirs = self.provider.foreign(self.s.device, 1)

    def take(self):
        reading = SourceSnapshot(self.layout, "a" * 32, self.provider)
        return reading, reading.take(self.job, self.s)

    def test_with_room_to_spare_the_snapshot_is_made_beside_theirs(self):
        self.provider.room_of = SnapshotRoom(used=10 * GIB, limit=100 * GIB, free=500 * GIB, capacity=1000 * GIB)
        reading, read = self.take()
        self.assertEqual((read.mode, len(self.provider.made)), (ReadMode.SNAPSHOT, 1))
        self.assertEqual(reading.release(), "")
        self.assertEqual(self.provider.deleted, self.provider.made, "only its own is removed")
        self.assertIn(self.theirs, self.provider.existing)

    def test_with_too_little_to_spare_or_none_known_the_source_is_read_as_it_is(self):
        self.provider.room_of = SnapshotRoom(used=99 * GIB, limit=100 * GIB, free=500 * GIB, capacity=1000 * GIB)
        _reading, little = self.take()
        self.assertEqual((little.mode, little.reason), (ReadMode.LIVE, WOULD_DISPLACE))
        self.assertEqual(little.detail, "1 snapshot made by Windows or another program; 1.0 GiB of the drive's room "
                                        "for snapshots is free, 10.0 GiB is wanted")
        self.provider.room_of = None
        _reading, unknown = self.take()
        self.assertEqual(unknown.detail, "1 snapshot made by Windows or another program; how much of the drive's "
                                         "room for snapshots is free could not be read")
        self.provider.room_failure = SnapshotError(SnapshotFailure.FAILED, "the service failed")
        self.assertEqual(self.take()[1].reason, WOULD_DISPLACE)
        self.assertEqual(self.provider.made, [])

    def test_what_is_to_spare_and_what_is_wanted(self):
        bounded = SnapshotRoom(used=90 * GIB, limit=100 * GIB, free=4 * GIB, capacity=1000 * GIB)
        self.assertEqual((bounded.spare, bounded.size, spare_needed(bounded)), (4 * GIB, 100 * GIB, 10 * GIB),
                         "the room grows only while the volume holding it has space")
        unbounded = SnapshotRoom(used=0, limit=None, free=300 * GIB, capacity=1000 * GIB)
        self.assertEqual((unbounded.spare, spare_needed(unbounded)), (300 * GIB, 100 * GIB))
        small = SnapshotRoom(used=0, limit=10 * GIB, free=500 * GIB, capacity=1000 * GIB)
        self.assertEqual(spare_needed(small), SPARE_FLOOR)
        self.assertEqual(SnapshotRoom(used=120 * GIB, limit=100 * GIB, free=500 * GIB, capacity=1000 * GIB).spare, 0,
                         "a room already past its limit has nothing to spare")
        self.assertTrue(room_to_spare(small))
        self.assertFalse(room_to_spare(None))

    def test_a_wmi_reference_names_its_volume(self):
        reference = 'Win32_Volume.DeviceID="\\\\\\\\?\\\\Volume{3c5e9a10-4d27-4b6f-9e81-0a2f6c7d5b39}\\\\"'
        self.assertEqual(referenced_device(reference), "\\\\?\\Volume{3c5e9a10-4d27-4b6f-9e81-0a2f6c7d5b39}\\")
        self.assertEqual(referenced_device("Win32_Volume"), "")


# --- runs through the engine ------------------------------------------------------------

class SnapshotCase(RunCase):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider(self.tmp / "shadows", {self.s.device: self.vol_s})

    def run_now(self, **options):
        return run_job(self.store, self.job.job_id, RunOptions(**options), volume_source=self.volumes, sync_roots=(),
                       snapshots=self.provider)


class SnapshotRunTest(SnapshotCase):
    def test_the_run_reads_the_snapshot_and_removes_it_when_it_ends(self):
        self.seed({"a.txt": "a", "sub/b.txt": "b"})
        write(self.src, "live-only.txt", "written after the snapshot")

        def prepare(live, shadow):
            shutil.copytree(live, shadow, copy_function=shutil.copy2)
            os.remove(shadow / "Work" / "live-only.txt")
            write(shadow / "Work", "snapshot-only.txt", "in the snapshot")

        self.provider.prepare = prepare
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(tree(self.dst), ["a.txt", "snapshot-only.txt", "sub\\b.txt"])
        self.assertEqual((record.source_read.mode, record.source_read.drive), (ReadMode.SNAPSHOT, str(self.vol_s)))
        self.assertEqual(self.provider.deleted, self.provider.made)
        self.assertEqual(len(self.provider.made), 2)          # the confirmed first run made one too
        self.assertEqual(own.recorded(self.layout), [])

    def test_no_snapshot_without_a_destination_to_copy_to(self):
        write(self.src, "a.txt", "a")
        self.volumes = FixedVolumeSource((self.s,))
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.source_read.reason), (Outcome.ABSENT, NOT_NEEDED))
        self.assertEqual(self.provider.made, [])

    def test_without_the_right_to_make_one_the_run_reads_as_it_is_and_says_why(self):
        self.seed({"a.txt": "a"})
        write(self.src, "b.txt", "b")
        self.provider.failure = SnapshotError(SnapshotFailure.DENIED, "administrator rights are needed")
        record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((record.source_read.mode, record.source_read.reason), (ReadMode.LIVE, "snapshot-denied"))
        self.assertEqual(tree(self.dst), ["a.txt", "b.txt"])

    def test_a_drive_holding_snapshots_someone_else_made_is_read_as_it_is(self):
        self.seed({"a.txt": "a"})
        foreign = self.provider.foreign(self.s.device, 1)
        write(self.src, "b.txt", "b")
        made = list(self.provider.made)
        record = self.only(self.run_now())
        self.assertEqual((record.outcome, record.source_read.reason), (Outcome.COMPLETED, WOULD_DISPLACE))
        self.assertEqual(self.provider.made, made)
        self.assertIn(foreign, self.provider.existing)
        self.assertEqual(tree(self.dst), ["a.txt", "b.txt"])

    def test_a_snapshot_a_dead_run_left_is_removed_by_the_next_run_and_no_other(self):
        write(self.src, "a.txt", "a")
        left = self.provider.foreign(self.s.device, 1)
        own.record_snapshot(self.layout, own.record_request(self.layout, "d" * 32, self.s.device, "S:"),
                            Snapshot(left, "x", self.s.device))
        own.record_request(self.layout, "e" * 32, self.s.device, "S:")
        other = self.provider.foreign(self.s.device, 2)
        record = self.only(self.run_now())
        self.assertEqual(self.provider.deleted, [left])
        self.assertIn(other, self.provider.existing)
        self.assertEqual((own.recorded(self.layout), self.provider.made), ([], []))
        self.assertEqual(record.source_read.reason, WOULD_DISPLACE)
        self.assertTrue(any("left behind" in e for e in record.errors), record.errors)
        self.assertTrue(any("while it was making a snapshot" in e for e in record.errors), record.errors)


@needs_engine
class SnapshotRemoteTest(SnapshotCase):
    """A job whose one destination is a remote's folder: a local folder named as a remote
    in a throwaway configuration."""

    def setUp(self):
        super().setUp()
        self.configuration = self.tmp / "throwaway.conf"
        self.configuration.write_text("[loc]\ntype = local\n", encoding="utf-8")
        self.remote = self.tmp / "remote" / "Work"
        self.job = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("loc", self.remote.as_posix())])
        self.store.save(StoreContents((self.job,), Preferences(remotes=RemoteSettings(str(self.configuration)))))

    def test_a_run_onto_a_remote_reads_the_source_from_a_snapshot(self):
        write(self.src, "live-only.txt", "written after the snapshot")

        def prepare(live, shadow):
            shutil.copytree(live, shadow, copy_function=shutil.copy2)
            os.remove(shadow / "Work" / "live-only.txt")
            write(shadow / "Work", "snapshot-only.txt", "in the snapshot")

        self.provider.prepare = prepare
        record = self.only(self.run_now(first_backup=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((record.source_read.mode, record.source_read.drive), (ReadMode.SNAPSHOT, str(self.vol_s)))
        self.assertEqual(tree(self.remote), ["snapshot-only.txt"])
        self.assertEqual(self.provider.deleted, self.provider.made)

    def test_no_snapshot_while_the_remote_cannot_be_reached(self):
        self.configuration.write_text("[loc]\ntype = webdav\nurl = http://127.0.0.1:9/\nvendor = rclone\n",
                                      encoding="utf-8")
        write(self.src, "a.txt", "a")
        record = self.only(self.run_now(first_backup=True))
        self.assertEqual((record.outcome, record.source_read.reason), (Outcome.ABSENT, NOT_NEEDED))
        self.assertEqual(self.provider.made, [])


@unittest.skipUnless(WINDOWS, "exclusive opens and suspending a process")
class SnapshotLiveFileTest(SnapshotCase):
    @classmethod
    def setUpClass(cls):
        drop_backup_privileges()     # with them, a held file is read live (measured)

    def test_a_file_held_open_is_backed_up_from_the_snapshot(self):
        self.seed({"held.db": "old copy", "a.txt": "a", **{f"other{i}.txt": "o" for i in range(10)}})
        write(self.src, "held.db", "changed and longer")
        write(self.src, "newheld.db", "never backed up yet")
        frozen = self.tmp / "frozen"
        shutil.copytree(self.vol_s, frozen, copy_function=shutil.copy2)
        self.provider.prepare = lambda live, shadow: shutil.copytree(frozen, shadow, copy_function=shutil.copy2)
        with Held(self.src / "held.db", self.src / "newheld.db"):
            record = self.only(self.run_now())
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual((self.dst / "held.db").read_text(), "changed and longer")
        self.assertEqual((self.dst / "newheld.db").read_text(), "never backed up yet")
        self.assertEqual((record.skipped, record.not_backed_up), ((), ()))

    def test_a_paused_run_keeps_its_snapshot_and_a_cancelled_one_removes_it(self):
        self.seed({"a.txt": "a"})
        write(self.src, "video.bin", os.urandom(8 * MiB))
        thread, box = self.in_background(bandwidth_limit="2M")
        status = self.live(lambda st: st.get("engine_pid") and growing_partial(self.dst))
        request(self.layout, status["run_id"], pause=True)
        self.live(lambda st: st.get("paused"))
        made = self.provider.made[-1]
        self.assertIn(made, self.provider.existing)
        self.assertEqual([o.shadow_id for o in own.recorded(self.layout)], [made])
        request(self.layout, status["run_id"], cancel=CancelMode.GRACEFUL)
        record = self.only(self.finish(thread, box))
        self.assertEqual(record.outcome, Outcome.CANCELLED, record)
        self.assertNotIn(made, self.provider.existing)
        self.assertEqual(own.recorded(self.layout), [])


@unittest.skipUnless(WINDOWS, "access control lists and file attributes")
class UnbackedRunTest(RunCase):
    @classmethod
    def setUpClass(cls):
        drop_backup_privileges()
        cls.user = current_user()

    def test_a_file_that_cannot_be_read_is_counted_run_by_run_and_a_cloud_only_file_never(self):
        self.seed({"locked/a.txt": "a", "ok.txt": "ok", "cloud.txt": "c"})
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.assertTrue(k32.SetFileAttributesW(str(self.src / "cloud.txt"), FILE_ATTRIBUTE_OFFLINE))
        icacls(self.src / "locked", "/deny", f"{self.user}:(RD)")
        self.addCleanup(icacls, self.src / "locked", "/reset")
        first = self.only(self.run_now())
        self.assertEqual(first.not_backed_up, (("locked", 1),))
        self.assertIn(("cloud.txt", "cloud-only file, not read"), first.skipped)
        second = self.only(self.run_now())
        self.assertEqual(second.repeatedly_unbacked, (("locked", 2),))
        icacls(self.src / "locked", "/reset")
        third = self.only(self.run_now())
        self.assertEqual((third.outcome, third.not_backed_up), (Outcome.COMPLETED, ()))


# --- records and notices ----------------------------------------------------------------

class StreakTest(unittest.TestCase):
    def test_runs_in_a_row_follow_the_previous_counted_run(self):
        previous = run_record(unbacked=[("Mail\\Inbox.pst", 2), ("db\\ledger.db", 1)])
        self.assertEqual(unbacked_streaks(["mail\\inbox.pst", "new.txt"], previous),
                         (("mail\\inbox.pst", 3), ("new.txt", 1)))
        self.assertEqual(unbacked_streaks(["a"], None), (("a", 1),))

    def test_only_runs_that_reached_their_copies_count(self):
        counted = run_record(unbacked=[("a", 1)])
        later = [counted, run_record(Outcome.ABSENT), run_record(Outcome.STOPPED), run_record(Outcome.CANCELLED),
                 run_record(Outcome.HELD, reasons=("first-run",)), run_record(marker="n" * 32)]
        self.assertIs(last_counted(later, "m" * 32), counted)
        additions = run_record(Outcome.HELD, reasons=("above-threshold",))
        self.assertIs(last_counted(later + [additions], "m" * 32), additions)

    def test_the_files_the_user_is_told_about_most_runs_first(self):
        record = run_record(unbacked=[("b", 2), ("a", 1), ("c", 4)])
        self.assertEqual(record.repeatedly_unbacked, (("c", 4), ("b", 2)))

    def test_runs_started_in_the_same_second_read_back_in_the_order_they_happened(self):
        with tempfile.TemporaryDirectory() as folder:
            layout = RuntimeLayout(Path(folder))
            first = replace(run_record(Outcome.HELD, reasons=("first-run",)), run_id="f" * 32)
            second = replace(run_record(unbacked=[("a", 1)]), run_id="0" * 32)
            write_record(layout, first)
            time.sleep(0.02)
            write_record(layout, second)
            self.assertEqual(read_records(layout, "j" * 32), [first, second])
            self.assertIs(last_counted(read_records(layout, "j" * 32), "m" * 32).outcome, Outcome.COMPLETED)

    def test_the_current_schema_round_trips_and_schema_one_reads_with_neither(self):
        record = replace(run_record(unbacked=[("a", 2)]),
                         source_read=SourceRead(ReadMode.SNAPSHOT, "D:", taken="2026-10-06T10:00:01Z"))
        self.assertEqual(RunRecord.from_document(record.to_document()), record)
        old = record.to_document()
        del old["source_read"], old["not_backed_up"], old["versions_cleaned"], old["space"], old["encrypted"]
        del old["done"]["files_identical"], old["done"]["bytes_identical"]
        old["schema_version"] = 1
        decoded = RunRecord.from_document(old)
        self.assertEqual((decoded.source_read, decoded.not_backed_up), (None, ()))
        old["schema_version"] = RECORD_SCHEMA + 1
        with self.assertRaises(ValueError):
            RunRecord.from_document(old)

    def test_a_completed_run_with_files_not_backed_up_twice_is_announced_under_the_failure_choice(self):
        record = run_record(unbacked=[("Mail\\inbox.pst", 3), ("x.txt", 1)])
        notice = notice_for([record], Notifications())
        self.assertEqual(notice.title, "Job: some files are not being backed up")
        self.assertEqual(notice.lines, ("Destination 1: completed - 1 file not backed up for 2 runs in a row "
                                        "(for example inbox.pst)",))
        self.assertEqual(notice_for([record], Notifications(on_failure=False)).title, "Job: backed up")
        self.assertIsNone(notice_for([record], Notifications(on_completion=False, on_failure=False)))
        self.assertEqual(notice_for([run_record(unbacked=[("x.txt", 1)])], Notifications()).title, "Job: backed up")


# --- the provider ---------------------------------------------------------------------------

class ProviderTest(unittest.TestCase):
    def test_the_suite_switches_real_snapshots_off(self):
        provider = default_snapshot_provider()
        with self.assertRaises(SnapshotError) as caught:
            provider.shadow_ids("\\\\?\\Volume{x}\\")
        self.assertIs(caught.exception.failure, SnapshotFailure.UNAVAILABLE)

    def test_windows_answers_become_kinds_of_failure(self):
        self.assertIs(failure_for_return(1)[0], SnapshotFailure.DENIED)
        self.assertIs(failure_for_return(4)[0], SnapshotFailure.REFUSED)
        self.assertIs(failure_for_return(6)[0], SnapshotFailure.FAILED)
        self.assertIs(failure_for_return(99)[0], SnapshotFailure.FAILED)
        wmi_denied = Exception(-2147352567, "Exception occurred.", (0, "SWbemServicesEx", "Access denied", None, 0,
                                                                    -2147217405), None)
        self.assertEqual(com_code(wmi_denied), 0x80041003)
        self.assertIs(failure_for_com(wmi_denied).failure, SnapshotFailure.DENIED)
        self.assertIs(failure_for_com(ValueError("odd")).failure, SnapshotFailure.FAILED)

    def test_a_process_without_administrators_is_told_so_before_the_service_is_asked(self):
        provider = WmiSnapshotProvider(is_administrator=lambda: False)
        for call in (lambda: provider.shadow_ids("v"), lambda: provider.create("v"),
                     lambda: provider.delete(shadow_id(1))):
            with self.assertRaises(SnapshotError) as caught:
                call()
            self.assertIs(caught.exception.failure, SnapshotFailure.DENIED)

    def test_identifiers_and_paths(self):
        self.assertTrue(valid_id(shadow_id(7)))
        self.assertFalse(valid_id("{7}' OR 1=1"))
        snapshot = Snapshot(shadow_id(7), "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7", "v")
        self.assertEqual(str(snapshot.path("")), "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7")
        self.assertEqual(str(snapshot.path("Work\\Mail")),
                         "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\\Work\\Mail")


@unittest.skipUnless(WINDOWS, "the object manager's device namespace")
class DevicePathRootTest(TempDirTest):
    def test_a_device_path_root_lists_and_opens_like_any_folder(self):
        write(self.tmp / "src", "a.txt", "alpha")
        write(self.tmp / "src", "sub/b.bin", b"\x89PNG" + b"0" * 10)
        root = Path(device_path(self.tmp / "src"))
        listing = list_tree(root, Side.SOURCE, drive_root=False)
        self.assertEqual(sorted(e.path for e in listing.files.values()), ["a.txt", "sub\\b.bin"])
        with open_source_file(root / "sub" / "b.bin") as handle:
            self.assertEqual(handle.read(4), b"\x89PNG")

    def test_a_device_root_without_its_separator_is_listed_not_read_as_missing(self):
        """A drive-root source read from a snapshot is the device path itself, which
        ``pathlib`` spells without a separator; the listing must list it, not report it
        absent. Stopped after its first folder, through the progress callback."""
        device_root = Path(device_path(str(self.tmp)[:3]))      # the temporary folder's volume, at its root
        self.assertFalse(str(device_root).endswith("\\"))
        calls = []

        class Enough(Exception):
            pass

        def progress(seen):
            calls.append(seen)
            if len(calls) == 2:
                raise Enough

        with self.assertRaises(Enough):
            list_tree(device_root, Side.SOURCE, drive_root=True, progress=progress)
        self.assertGreater(calls[-1], 0)


if __name__ == "__main__":
    unittest.main()
