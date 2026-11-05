"""Repair: replacing the copies a full verification found to differ.

The promised outcomes, on temporary folders with the real engine: the copies a
verification found to differ are replaced by the source's, each backup copy kept in the
destination's versions and the new copy read back identical; a copy that changed since,
is gone, is excluded, lies beneath a junction, is not its job's on a shared destination,
lies in a folder no run writes into or whose source copy lost the file-format signature
its backup copy has, is left as it is;
a copy the engine cannot complete has its backup copy moved back; a copy step that raises,
an interruption and a stop leave no backup copy in versions unnamed, one that cannot be
moved back recorded as in versions; a process that dies while the backup copies move or
while the engine copies leaves a record naming the batch and each copy whose backup copy
may lie in it, as does a second interruption during the clean-up for the copies it had not
settled; a repair takes up the destination's latest finished
pass, never one unfinished; a second repair takes up what the first did not replace, and
none starts once every listed copy is replaced; a repair without room fails having changed
nothing; a stop before its turn records nothing.
Around them: the record's form, an earlier schema's included, what is pending and what a
repair that no longer runs left unsettled, a repair's place among other
activities in admission, a dead repair leaving no run record, the notice, and the live
paths — ``repair`` on the command line in process, and a repair started as a process of
its own on this machine's real volume source.

Volumes are temporary folders mapped as volumes (``support.volume``); nothing touches a
real volume but through a temporary folder on the system drive."""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

from support import PROJECT, TempDirTest, endpoint, live_activity, volume
from test_live_sources import FakeProvider
from test_run import write

from mirrorhold import exchange
from mirrorhold.cli import EXIT_HELD, EXIT_OK, EXIT_REFUSED, main
from mirrorhold.endpoints.cloud import SyncRoot
from mirrorhold.endpoints.marker import MANAGEMENT_DIR, Marker, write_marker
from mirrorhold.endpoints.shared import SharedRecord, write_shared
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.jobs.model import Destination, Notifications, new_job
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.maintenance.repair import (REPAIR_SCHEMA, REPAIRS_DIR, LeftReason, RepairRecord, RepairState,
                                           all_replaced, effective_state, pending, read_repairs, unsettled,
                                           write_repair)
from mirrorhold.maintenance.repairer import RepairError, repair_destination
from mirrorhold.maintenance.verification import (VerificationRecord, VerificationState, read_verifications,
                                                 write_verification)
from mirrorhold.maintenance.verifier import verify_destination
from mirrorhold.notifications.message import repair_notice
from mirrorhold.records.admission import Activity, Claim, gives_way, goes_first
from mirrorhold.records.record import read_records, utc_now
from mirrorhold.records.status import (KIND_REPAIR, KIND_RUN, KIND_VERIFICATION, CancelMode, LiveStatus, StatusBoard,
                                       observe_runs, reap_interrupted, request)
from mirrorhold.run.apply import ApplyResult, VersionsBatch, apply_pair
from mirrorhold.run.launch import start_repair
from mirrorhold.snapshots.provider import UnavailableProvider

WINDOWS = sys.platform == "win32"
NO_SNAPSHOTS = UnavailableProvider("snapshots are not made in this test")
LOCKED_AT = 1024 * 1024
"""Where a test locks a range of a source file: beyond what reading a file's first bytes
buffers (128 KiB from CPython 3.14), so only the engine's whole read meets the lock."""


def damage(path: Path, at: int = 0) -> None:
    """Flip one bit of ``path``, keeping its size and its times, as a bad sector would."""
    times = os.stat(path)
    data = bytearray(path.read_bytes())
    data[at] ^= 0x01
    path.write_bytes(bytes(data))
    os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))


def verification(differ, *, total=None, verification_id="v" * 32, marker="m" * 32,
                 finished=False) -> VerificationRecord:
    """A pass that found ``differ``: running, or with ``finished`` completed, as a repair
    takes up only a finished pass."""
    record = VerificationRecord(verification_id, "j" * 32, "Work", marker, 1, "2026-10-17T10:00:00Z",
                                "2026-10-17T10:05:00Z")
    if finished:
        record.state, record.ended = VerificationState.COMPLETED, record.updated
    record.differ = list(differ)
    record.differ_total = len(differ) if total is None else total
    return record


def repair(replaced=(), left=(), *, verification_id="v" * 32, repair_id="r" * 32, state=RepairState.COMPLETED):
    return RepairRecord(repair_id, "j" * 32, "Work", "m" * 32, 1, verification_id, "2026-10-17T10:00:00Z",
                        "2026-10-17T11:00:00Z", "2026-10-17T11:01:00Z", state, taken=len(replaced) + len(left),
                        replaced=list(replaced), left=list(left))


# --- the record, without the engine -------------------------------------------------------

class RepairRecordTest(TempDirTest):
    def test_a_record_round_trips_and_reads_back_beside_the_store_and_on_the_destination(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        record = repair([("a.txt", 5)], [("b.txt", LeftReason.CHANGED, ""), ("c.txt", LeftReason.STILL_DIFFERS, "")])
        record.bytes_copied, record.versions_batch = 10, ".mirrorhold\\versions\\2026-10-17T11-00-00Z"
        destination = self.tmp / "dst"
        write_repair(layout, record, destination)
        self.assertEqual(read_repairs(layout, record.job_id), [record])
        self.assertTrue((destination / MANAGEMENT_DIR / REPAIRS_DIR / record.file_name()).is_file())
        self.assertEqual((record.failures, record.into_versions), (1, 2))
        doc = record.to_document()
        self.assertEqual((doc["format"], doc["schema_version"]), ("mirrorhold-repair", 2))
        self.assertEqual(doc["left"][0], ["b.txt", "changed", ""])

    def test_a_record_met_as_it_is_renamed_over_is_still_read(self):
        """A repair rewrites its record as it works: a look that meets the moment its name is
        away looks again rather than passing the repair over."""
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        record = repair([("a.txt", 5)])
        write_repair(layout, record, self.tmp / "dst")
        path = layout.repairs_dir / record.job_id / record.file_name()
        real, missed = exchange.open_shared, []

        def opening(opened):
            if Path(opened) == path and not missed:
                missed.append(opened)
                raise FileNotFoundError(2, "renamed over", str(opened))
            return real(opened)

        with mock.patch.object(exchange, "open_shared", side_effect=opening):
            self.assertEqual(read_repairs(layout, record.job_id), [record])
        self.assertEqual(len(missed), 1)

    def test_what_is_pending_is_what_no_repair_of_the_pass_replaced(self):
        found = verification([("a.txt", 1), ("B.txt", 2), ("c.txt", 3)])
        earlier = repair([("b.txt", 2)], [("c.txt", LeftReason.CHANGED, "")])
        other_pass = repair([("a.txt", 1)], verification_id="w" * 32, repair_id="s" * 32)
        self.assertEqual(pending(found, [earlier, other_pass]), (("a.txt", 1), ("c.txt", 3)))
        self.assertFalse(all_replaced(found, [earlier]))
        self.assertTrue(all_replaced(found, [earlier, repair([("A.txt", 1), ("c.txt", 3)], repair_id="t" * 32)]))
        self.assertFalse(all_replaced(verification([("a.txt", 1)], total=5), [repair([("a.txt", 1)])]),
                         "copies found beyond the list still differ")

    def test_a_running_record_whose_process_ended_reads_as_interrupted(self):
        record = repair(state=RepairState.RUNNING)
        self.assertEqual(effective_state(record, ()), RepairState.INTERRUPTED)
        self.assertEqual(effective_state(record, {record.repair_id}), RepairState.RUNNING)

    def test_a_schema_1_record_reads_with_no_copy_being_replaced(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        doc = repair([("a.txt", 5)], [("b.txt", LeftReason.CHANGED, "")]).to_document()
        doc["schema_version"] = 1
        del doc["replacing"]
        folder = layout.repairs_dir / doc["job_id"]
        folder.mkdir(parents=True)
        (folder / "earlier.json").write_text(json.dumps(doc), encoding="utf-8")
        (record,) = read_repairs(layout, doc["job_id"])
        self.assertEqual((record.replaced, record.replacing, record.into_versions), ([("a.txt", 5)], [], 1))
        current = repair([("a.txt", 5)], [("b.txt", LeftReason.CHANGED, "")])
        (folder / "earlier.json").write_text(json.dumps(current.to_document()), encoding="utf-8")
        self.assertEqual(read_repairs(layout, doc["job_id"]), [current])
        later = dict(current.to_document(), schema_version=REPAIR_SCHEMA + 1)
        (folder / "earlier.json").write_text(json.dumps(later), encoding="utf-8")
        self.assertEqual(read_repairs(layout, doc["job_id"]), [], "a later schema is passed over")

    def test_the_copies_a_repair_that_no_longer_runs_was_replacing_are_unsettled(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        record = repair([("a.txt", 5)], state=RepairState.RUNNING)
        record.taken, record.versions_batch = 3, ".mirrorhold\\versions\\2026-10-17T11-00-00Z"
        record.replacing = [("b.txt", 4), ("c.txt", 5)]
        write_repair(layout, record)
        self.assertEqual(read_repairs(layout, record.job_id), [record])
        self.assertEqual(unsettled(record), [("b.txt", 4), ("c.txt", 5)])
        self.assertEqual(unsettled(record, {record.repair_id}), [], "while it runs they are its work in hand")
        self.assertEqual((record.failures, record.into_versions), (0, 3))
        self.assertIn("repair interrupted: 1 of 3 copies replaced, 2 not finished: their backup copies may be in "
                      ".mirrorhold\\versions\\2026-10-17T11-00-00Z", record.describe())
        record.replacing = record.replacing[:1]
        self.assertIn(", 1 not finished: its backup copy may be in ", record.describe())
        working = record.describe({record.repair_id})
        self.assertIn("repair running: 1 of 3 copies replaced", working)
        self.assertNotIn("not finished", working)
        found = verification([("a.txt", 5), ("b.txt", 4), ("c.txt", 5)])
        self.assertEqual(pending(found, [record]), (("b.txt", 4), ("c.txt", 5)), "the next repair takes them up")

    def test_the_notice_follows_the_users_choices(self):
        done = repair([("a.txt", 1), ("b.txt", 1), ("c.txt", 1)])
        self.assertEqual(repair_notice(done, Notifications()).title, "Work, destination 1: 3 copies replaced")
        self.assertIsNone(repair_notice(done, Notifications(on_completion=False)))
        some = repair([("a.txt", 1), ("b.txt", 1)], [("c.txt", LeftReason.CHANGED, "")])
        self.assertEqual(repair_notice(some, Notifications()).title,
                         "Work, destination 1: 2 copies replaced, 1 left - see History")
        self.assertIsNone(repair_notice(some, Notifications(on_failure=False)))
        self.assertIsNone(repair_notice(repair([("a.txt", 1)], state=RepairState.STOPPED), Notifications()))
        one = repair([("a.txt", 1)])
        self.assertEqual(repair_notice(one, Notifications()).title, "Work, destination 1: 1 copy replaced")


class RepairAdmissionTest(TempDirTest):
    def activity(self, kind, queued, *, admitted=False, job="j"):
        return Activity(kind + queued, kind, job * 32, "Job", "m" * 32, queued, admitted, False, Claim())

    def test_a_waiting_repair_goes_before_a_waiting_verification_and_after_an_earlier_run(self):
        repair_, check, run = (self.activity(KIND_REPAIR, "2"), self.activity(KIND_VERIFICATION, "1"),
                               self.activity(KIND_RUN, "1"))
        self.assertTrue(goes_first(repair_, check))
        self.assertFalse(goes_first(check, repair_))
        self.assertTrue(goes_first(run, repair_))
        self.assertFalse(goes_first(repair_, run))

    def test_a_working_verification_gives_way_to_a_waiting_repair_of_what_it_reads(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        s, d = volume(1), volume(2)
        source, target = endpoint(s, "Work"), endpoint(d, "Backup")
        check = LiveStatus("v" * 32, os.getpid(), "j" * 32, "Work", "manual", utc_now(), kind=KIND_VERIFICATION,
                           admitted=True, claim=Claim((source, target), ()).to_document())
        self.assertFalse(gives_way(layout, check))
        board = live_activity(layout, "k" * 32, reads=(source,), writes=(target,), kind=KIND_REPAIR, admitted=False)
        try:
            self.assertTrue(gives_way(layout, check))
        finally:
            board.close()

    def test_a_dead_repair_leaves_no_run_record(self):
        layout = runtime_layout(self.tmp / "Config" / "mirrorhold.json")
        status = LiveStatus("d" * 32, os.getpid(), "j" * 32, "Work", "manual", utc_now(), kind=KIND_REPAIR,
                            pair_index=1, marker_id="m" * 32, pair_started=utc_now())
        layout.status_dir.mkdir(parents=True)
        (layout.status_dir / f"{status.run_id}.json").write_text(json.dumps(status.to_document()), encoding="utf-8")
        reap_interrupted(layout)
        self.assertEqual((read_records(layout, "j" * 32), observe_runs(layout)), ([], []))


# --- a repair, with the engine -----------------------------------------------------------------

class RepairCase(TempDirTest):
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

    def verify(self):
        return verify_destination(self.store, self.job.job_id, 1, volume_source=self.volumes)

    def repair(self, **kwargs):
        kwargs.setdefault("snapshots", NO_SNAPSHOTS)
        said = []
        record = repair_destination(self.store, self.job.job_id, 1, volume_source=self.volumes, say=said.append,
                                    **kwargs)
        self.said = said
        return record

    def versions(self, record):
        root = self.dst / record.versions_batch
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def left(self, record):
        return {path: reason for path, reason, _detail in record.left}


class RepairTest(RepairCase):
    def test_the_copies_that_differ_are_replaced_and_their_backup_copies_kept_in_versions(self):
        self.mirror({"a.txt": "alpha", "photo.raw": b"\x00" * 4096, "sub\\b.bin": os.urandom(70_000)})
        damage(self.dst / "photo.raw", 2048)
        damage(self.dst / "sub" / "b.bin", 100)
        damaged = (self.dst / "photo.raw").read_bytes()
        found = self.verify()
        self.assertEqual(found.differ_total, 2)
        record = self.repair()
        self.assertEqual((record.state, record.taken, record.left), (RepairState.COMPLETED, 2, []))
        self.assertEqual(sorted(record.replaced), [("photo.raw", 4096), ("sub\\b.bin", 70_000)])
        self.assertEqual(record.verification_id, found.verification_id)
        for rel in ("photo.raw", "sub\\b.bin"):
            self.assertEqual((self.dst / rel).read_bytes(), (self.src / rel).read_bytes())
        self.assertEqual(self.versions(record), ["photo.raw", "sub\\b.bin"])
        self.assertEqual((self.dst / record.versions_batch / "photo.raw").read_bytes(), damaged)
        self.assertEqual(read_repairs(self.layout, self.job.job_id), [record])
        self.assertTrue((self.dst / MANAGEMENT_DIR / REPAIRS_DIR / record.file_name()).is_file())
        self.assertEqual(read_verifications(self.layout, self.job.job_id), [found], "the verification keeps its finds")
        self.assertIn("replacing the backup copies of 2 files that differ", self.said[0])
        self.assertIn("replaced: photo.raw", self.said)
        self.assertEqual(observe_runs(self.layout), [])
        self.assertEqual(self.verify().differ_total, 0)

    def test_one_copy_that_differs_is_announced_in_the_singular(self):
        self.mirror({"a.txt": "alpha", "photo.raw": b"\x00" * 4096})
        damage(self.dst / "photo.raw", 2048)
        self.assertEqual(self.verify().differ_total, 1)
        record = self.repair()
        self.assertEqual((record.state, record.taken), (RepairState.COMPLETED, 1))
        self.assertIn("replacing the backup copies of 1 file that differs (verification of", self.said[0])

    def test_copies_it_must_not_replace_are_left_as_they_are(self):
        self.mirror({"changed.txt": "one", "gone.txt": "two", "photo.jpg": b"\xff\xd8\xff\xe0" + b"j" * 60,
                     "skip-me.txt": "three", "fine.txt": "four"})
        for name in ("changed.txt", "gone.txt", "skip-me.txt", "fine.txt"):
            damage(self.dst / name, 1)
        damage(self.dst / "photo.jpg", 10)
        self.assertEqual(self.verify().differ_total, 5)
        write(self.src, "changed.txt", "a longer new version")
        (self.src / "gone.txt").unlink()
        times = os.stat(self.src / "photo.jpg")
        (self.src / "photo.jpg").write_bytes(b"\x93\x11" + b"e" * 62)
        os.utime(self.src / "photo.jpg", ns=(times.st_atime_ns, times.st_mtime_ns))
        self.job = replace(self.job, source=replace(self.job.source, exclusions=("skip-*",)))
        self.store.save(StoreContents((self.job,)))
        backups = {name: (self.dst / name).read_bytes() for name in ("changed.txt", "photo.jpg", "skip-me.txt")}
        record = self.repair()
        self.assertEqual(record.replaced, [("fine.txt", 4)])
        self.assertEqual(self.left(record), {"changed.txt": LeftReason.CHANGED, "gone.txt": LeftReason.MISSING_SOURCE,
                                             "photo.jpg": LeftReason.LOST_SIGNATURE,
                                             "skip-me.txt": LeftReason.EXCLUDED})
        for name, data in backups.items():
            self.assertEqual((self.dst / name).read_bytes(), data, name)
        self.assertEqual(self.versions(record), ["fine.txt"])
        self.assertIn("left: photo.jpg - its source copy lost its file-format signature", self.said)

    def test_a_second_repair_takes_up_only_what_none_replaced_and_none_starts_once_all_are(self):
        self.mirror({"a.txt": "alpha", "b.txt": "beta"})
        damage(self.dst / "a.txt")
        damage(self.dst / "b.txt")
        self.verify()
        write(self.src, "b.txt", "beta, changed")
        first = self.repair()
        self.assertEqual((first.replaced, self.left(first)), ([("a.txt", 5)], {"b.txt": LeftReason.CHANGED}))
        write(self.src, "b.txt", "beta")
        shutil.copystat(self.dst / "b.txt", self.src / "b.txt")
        second = self.repair()
        self.assertEqual((second.taken, second.replaced), (1, [("b.txt", 4)]))
        with self.assertRaises(RepairError) as refused:
            self.repair()
        self.assertEqual(str(refused.exception), "every copy that verification listed was replaced already")

    def test_with_nothing_to_take_up_or_a_verification_at_work_none_starts(self):
        self.mirror({"a.txt": "alpha"})
        with self.assertRaises(RepairError) as none:
            self.repair()
        self.assertEqual(str(none.exception),
                         "the destination's latest finished verification found no copy that differs")
        self.verify()
        with self.assertRaises(RepairError) as clean:
            self.repair()
        self.assertEqual(str(clean.exception),
                         "the destination's latest finished verification found no copy that differs")
        time.sleep(1.1)
        found = verification([("a.txt", 5)], verification_id="q" * 32, marker=self.marker)
        found.job_id, found.started = self.job.job_id, utc_now()
        write_verification(self.layout, found)
        status = LiveStatus(found.verification_id, os.getpid(), self.job.job_id, "Work", "manual", utc_now(),
                            kind=KIND_VERIFICATION, marker_id=self.marker)
        live = StatusBoard(self.layout, status)
        live.open()
        try:
            with self.assertRaises(RepairError) as busy:
                self.repair()
            self.assertEqual(str(busy.exception), "a verification of the destination is in progress")
        finally:
            live.close()

    def test_on_a_shared_destination_what_its_job_does_not_own_is_left(self):
        self.mirror({"Mine\\a.txt": "alpha", "Mine\\Kept\\b.txt": "beta"})
        write(self.dst, "Theirs\\c.txt", "gamma")
        write_marker(self.dst, Marker(self.job.job_id, self.marker, shared=True))
        write_shared(self.dst, SharedRecord(self.job.job_id, self.marker, ("Mine",), ("Mine\\Kept",)))
        found = verification([("Mine\\a.txt", 5), ("Mine\\Kept\\b.txt", 4), ("Theirs\\c.txt", 5)],
                             marker=self.marker, finished=True)
        found.job_id = self.job.job_id
        write_verification(self.layout, found)
        damage(self.dst / "Mine" / "a.txt")
        record = self.repair()
        self.assertEqual(record.replaced, [("Mine\\a.txt", 5)])
        self.assertEqual(self.left(record), {"Mine\\Kept\\b.txt": LeftReason.NOT_THE_JOBS,
                                             "Theirs\\c.txt": LeftReason.NOT_THE_JOBS})

    def test_a_copy_inside_a_folder_no_run_writes_into_is_left(self):
        self.mirror({"a.txt": "alpha", "Inner\\data.txt": "another job's source", "Cloud\\c.txt": "synchronised"})
        for rel in ("a.txt", "Inner\\data.txt", "Cloud\\c.txt"):
            damage(self.dst / rel)
        self.assertEqual(self.verify().differ_total, 3)
        inner = new_job("Inner", endpoint(self.d, "Backup\\Inner"), [endpoint(self.s, "InnerCopy")])
        self.store.save(StoreContents((self.job, inner)))
        backups = {rel: (self.dst / rel).read_bytes() for rel in ("Inner\\data.txt", "Cloud\\c.txt")}
        cloud = self.dst / "Cloud"
        record = self.repair(sync_roots=(SyncRoot("OneDrive", str(cloud)),))
        self.assertEqual(record.replaced, [("a.txt", 5)])
        self.assertEqual(self.left(record), {"Inner\\data.txt": LeftReason.PROTECTED,
                                             "Cloud\\c.txt": LeftReason.PROTECTED})
        for rel, data in backups.items():
            self.assertEqual((self.dst / rel).read_bytes(), data, rel)
        self.assertIn(f"left: Cloud\\c.txt - it lies inside a folder MirrorHold never writes into: {cloud}, which "
                      "OneDrive synchronises", self.said)

    @unittest.skipUnless(WINDOWS, "junctions are Windows'")
    def test_a_copy_beneath_a_junction_is_left(self):
        import _winapi
        self.mirror({"a.txt": "alpha"})
        elsewhere = self.tmp / "elsewhere"
        write(elsewhere, "b.txt", "beta")
        _winapi.CreateJunction(str(elsewhere), str(self.dst / "Linked"))
        write(self.src, "Linked\\b.txt", "beta")
        shutil.copystat(elsewhere / "b.txt", self.src / "Linked" / "b.txt")
        found = verification([("Linked\\b.txt", 4)], marker=self.marker, finished=True)
        found.job_id = self.job.job_id
        write_verification(self.layout, found)
        record = self.repair()
        self.assertEqual(self.left(record), {"Linked\\b.txt": LeftReason.LINK})
        self.assertEqual((elsewhere / "b.txt").read_text(encoding="utf-8"), "beta")

    def test_without_room_it_fails_having_changed_nothing(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        self.verify()
        before = (self.dst / "a.txt").read_bytes()
        self.d = replace(self.d, free_bytes=1000)
        self.volumes = FixedVolumeSource((self.s, self.d))
        record = self.repair()
        self.assertEqual((record.state, record.replaced, record.versions_batch), (RepairState.FAILED, [], None))
        self.assertIn("the repair needs", record.detail)
        self.assertEqual((self.dst / "a.txt").read_bytes(), before)
        self.assertEqual(effective_state(read_repairs(self.layout, self.job.job_id)[0]), RepairState.FAILED)

    @unittest.skipUnless(WINDOWS, "a byte-range lock is Windows'")
    def test_a_copy_the_engine_cannot_complete_keeps_its_backup_copy(self):
        import msvcrt
        self.mirror({"locked.bin": os.urandom(LOCKED_AT + 64 * 1024), "a.txt": "alpha"})
        damage(self.dst / "locked.bin", 5)
        damage(self.dst / "a.txt")
        self.verify()
        backup = (self.dst / "locked.bin").read_bytes()
        with open(self.src / "locked.bin", "rb+") as handle:
            handle.seek(LOCKED_AT)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 4096)
            try:
                record = self.repair()
            finally:
                handle.seek(LOCKED_AT)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 4096)
        self.assertEqual(record.replaced, [("a.txt", 5)])
        self.assertEqual(self.left(record), {"locked.bin": LeftReason.NOT_COPIED})
        self.assertEqual((record.failures, (self.dst / "locked.bin").read_bytes()), (1, backup))
        self.assertEqual(self.versions(record), ["a.txt"])

    def later_pass(self, state, differ=(), verification_id="q" * 32):
        """A pass of the destination begun after every other, in ``state``: its process ended."""
        later = verification(differ, verification_id=verification_id, marker=self.marker)
        later.job_id, later.started, later.state = self.job.job_id, "2099-01-01T00:00:00Z", state
        write_verification(self.layout, later)
        return later

    def test_a_later_pass_that_did_not_finish_leaves_the_latest_finished_one_to_take_up(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        found = self.verify()
        self.later_pass(VerificationState.STOPPED)
        record = self.repair()
        self.assertEqual((record.verification_id, record.replaced), (found.verification_id, [("a.txt", 5)]))

    def test_what_an_unfinished_pass_found_so_far_is_never_taken_up(self):
        self.mirror({"a.txt": "alpha", "b.txt": "beta"})
        damage(self.dst / "a.txt")
        found = self.verify()
        damage(self.dst / "b.txt")
        damaged = (self.dst / "b.txt").read_bytes()
        self.later_pass(VerificationState.RUNNING, [("b.txt", 4)])
        record = self.repair()
        self.assertEqual((record.verification_id, record.replaced), (found.verification_id, [("a.txt", 5)]))
        self.assertEqual((self.dst / "b.txt").read_bytes(), damaged, "only the finished pass's copies are replaced")

    def three_differing(self):
        """Three copies that differ, verified; their backup copies' bytes."""
        self.mirror({"a.txt": "alpha", "b.txt": "beta", "c.txt": "gamma"})
        for name in ("a.txt", "b.txt", "c.txt"):
            damage(self.dst / name)
        self.assertEqual(self.verify().differ_total, 3)
        return {name: (self.dst / name).read_bytes() for name in ("a.txt", "b.txt", "c.txt")}

    def stuck(self, *names):
        """``VersionsBatch.move_back`` refusing the copies ``names``, as a file another program
        holds open would."""
        real = VersionsBatch.move_back

        def move_back(batch, relative):
            if relative in names:
                raise PermissionError(13, "Access is denied")
            return real(batch, relative)
        return mock.patch.object(VersionsBatch, "move_back", move_back)

    def test_a_copy_step_that_raises_leaves_no_backup_copy_in_versions_unnamed(self):
        backups = self.three_differing()

        def copies_one_then_raises(plan, decision, *, source_root, destination_root, **_):
            shutil.copy2(source_root / "a.txt", destination_root / "a.txt")
            raise RuntimeError("the copy step broke")
        with mock.patch("mirrorhold.maintenance.repairer.apply_pair", copies_one_then_raises), self.stuck("c.txt"):
            record = self.repair()
        self.assertEqual((record.state, record.detail), (RepairState.FAILED, "RuntimeError: the copy step broke"))
        self.assertEqual(record.left,
                         [("c.txt", LeftReason.IN_VERSIONS, "Access is denied"),
                          ("a.txt", LeftReason.NOT_READ_BACK, "the repair failed before it was read back")])
        self.assertEqual((self.dst / "b.txt").read_bytes(), backups["b.txt"], "moved back, untouched")
        self.assertEqual(self.versions(record), ["a.txt", "c.txt"])
        self.assertEqual((self.dst / record.versions_batch / "c.txt").read_bytes(), backups["c.txt"])
        self.assertFalse((self.dst / "c.txt").exists())
        self.assertEqual((record.failures, record.into_versions), (2, 2))
        self.assertEqual(read_repairs(self.layout, self.job.job_id), [record])
        found = read_verifications(self.layout, self.job.job_id)[-1]
        self.assertEqual(pending(found, [record]), (("a.txt", 5), ("b.txt", 4), ("c.txt", 5)),
                         "none counts as replaced")

    def test_an_interrupted_copy_step_moves_the_backup_copies_back(self):
        backups = self.three_differing()

        def interrupted(plan, decision, **_):
            raise KeyboardInterrupt
        with mock.patch("mirrorhold.maintenance.repairer.apply_pair", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.repair()
        (record,) = read_repairs(self.layout, self.job.job_id)
        self.assertEqual((record.state, record.left), (RepairState.INTERRUPTED, []))
        for name, data in backups.items():
            self.assertEqual((self.dst / name).read_bytes(), data, name)
        self.assertEqual(self.versions(record), [])

    def test_an_interruption_just_after_a_backup_copy_moved_still_moves_it_back(self):
        backups = self.three_differing()
        real = VersionsBatch.move_in

        def interrupted_after_b(batch, relative):
            real(batch, relative)
            if relative == "b.txt":
                raise KeyboardInterrupt
        with mock.patch.object(VersionsBatch, "move_in", interrupted_after_b):
            with self.assertRaises(KeyboardInterrupt):
                self.repair()
        (record,) = read_repairs(self.layout, self.job.job_id)
        self.assertEqual((record.state, record.left), (RepairState.INTERRUPTED, []))
        for name, data in backups.items():
            self.assertEqual((self.dst / name).read_bytes(), data, name)
        self.assertEqual(self.versions(record), [])

    def test_a_backup_copy_that_cannot_be_moved_back_is_recorded_as_in_versions_when_stopped(self):
        backups = self.three_differing()

        def stopped(plan, decision, **_):
            return ApplyResult(cancelled=CancelMode.GRACEFUL)
        with mock.patch("mirrorhold.maintenance.repairer.apply_pair", stopped), self.stuck("b.txt"):
            record = self.repair()
        self.assertEqual((record.state, record.replaced), (RepairState.STOPPED, []))
        self.assertEqual(record.left, [("b.txt", LeftReason.IN_VERSIONS, "Access is denied")])
        self.assertEqual(self.versions(record), ["b.txt"])
        self.assertEqual((self.dst / record.versions_batch / "b.txt").read_bytes(), backups["b.txt"])
        for name in ("a.txt", "c.txt"):
            self.assertEqual((self.dst / name).read_bytes(), backups[name], name)

    def on_disk(self):
        """The repair's record as its file holds it now: what the process dying at this moment
        would leave."""
        (record,) = read_repairs(self.layout, self.job.job_id)
        return record

    def test_a_death_during_the_moves_finds_every_copy_and_its_batch_named(self):
        self.three_differing()
        real = VersionsBatch.move_in
        seen = {}

        def move_in(batch, relative):
            seen.setdefault("before", {})[relative] = self.on_disk()
            real(batch, relative)
            if relative == "b.txt":
                seen["dead"], seen["batch"] = self.on_disk(), batch
                seen["held"] = sorted(p.name for p in batch.path.iterdir())
        with mock.patch.object(VersionsBatch, "move_in", move_in):
            record = self.repair()
        for name, before in seen["before"].items():
            self.assertIn(name, [path for path, _size in before.replacing], f"{name} named before it moved")
            self.assertEqual(before.versions_batch, str(seen["batch"].relative))
        dead = seen["dead"]
        self.assertEqual((dead.state, effective_state(dead)), (RepairState.RUNNING, RepairState.INTERRUPTED))
        self.assertEqual(sorted(unsettled(dead)), [("a.txt", 5), ("b.txt", 4), ("c.txt", 5)])
        self.assertEqual(seen["held"], ["a.txt", "b.txt"])
        self.assertEqual((dead.replaced, dead.left), ([], []))
        self.assertEqual((record.state, record.replacing, len(record.replaced)), (RepairState.COMPLETED, [], 3))

    def test_a_death_during_the_copying_finds_the_copies_it_moved_named(self):
        backups = self.three_differing()
        seen = {}

        def copying(plan, decision, *, source_root, destination_root, batch, **rest):
            shutil.copy2(source_root / "a.txt", destination_root / "a.txt")
            seen["dead"] = self.on_disk()
            seen["held"] = {p.name: p.read_bytes() for p in batch.path.iterdir()}
            (destination_root / "a.txt").unlink()
            return apply_pair(plan, decision, source_root=source_root, destination_root=destination_root, batch=batch,
                              **rest)
        with mock.patch("mirrorhold.maintenance.repairer.apply_pair", copying):
            record = self.repair()
        dead = seen["dead"]
        self.assertEqual(effective_state(dead), RepairState.INTERRUPTED)
        self.assertEqual(dead.versions_batch, record.versions_batch)
        self.assertEqual(sorted(unsettled(dead)), [("a.txt", 5), ("b.txt", 4), ("c.txt", 5)])
        self.assertEqual(seen["held"], backups, "each named copy's backup copy lies in the named batch")
        self.assertIn(f"3 not finished: their backup copies may be in {dead.versions_batch}", dead.describe())
        found = read_verifications(self.layout, self.job.job_id)[-1]
        self.assertEqual(len(pending(found, [dead])), 3, "none counts as replaced")
        self.assertEqual((record.state, record.replacing, len(record.replaced)), (RepairState.COMPLETED, [], 3))

    def test_a_second_interruption_during_the_clean_up_leaves_the_copies_not_settled_named(self):
        backups = self.three_differing()
        real = VersionsBatch.move_back
        calls = []

        def interrupted(plan, decision, **_):
            raise KeyboardInterrupt

        def move_back(batch, relative):
            calls.append(relative)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(batch, relative)
        with mock.patch("mirrorhold.maintenance.repairer.apply_pair", interrupted), \
                mock.patch.object(VersionsBatch, "move_back", move_back):
            with self.assertRaises(KeyboardInterrupt):
                self.repair()
        record = self.on_disk()
        self.assertEqual((record.state, record.left, record.replaced), (RepairState.INTERRUPTED, [], []))
        back = calls[0]
        self.assertEqual((self.dst / back).read_bytes(), backups[back], "moved back, no longer named")
        stranded = sorted(path for path, _size in unsettled(record))
        self.assertEqual(stranded, sorted(set(backups) - {back}))
        self.assertEqual(self.versions(record), stranded)
        for name in stranded:
            self.assertEqual((self.dst / record.versions_batch / name).read_bytes(), backups[name], name)

    @unittest.skipUnless(WINDOWS, "a byte-range lock is Windows'")
    def test_a_copy_the_engine_cannot_complete_whose_backup_copy_cannot_be_moved_back_is_in_versions(self):
        import msvcrt
        self.mirror({"locked.bin": os.urandom(LOCKED_AT + 64 * 1024), "a.txt": "alpha"})
        damage(self.dst / "locked.bin", 5)
        damage(self.dst / "a.txt")
        self.verify()
        backup = (self.dst / "locked.bin").read_bytes()
        with open(self.src / "locked.bin", "rb+") as handle, self.stuck("locked.bin"):
            handle.seek(LOCKED_AT)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 4096)
            try:
                record = self.repair()
            finally:
                handle.seek(LOCKED_AT)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 4096)
        self.assertEqual(record.replaced, [("a.txt", 5)])
        self.assertEqual(self.left(record), {"locked.bin": LeftReason.IN_VERSIONS})
        self.assertEqual((self.dst / record.versions_batch / "locked.bin").read_bytes(), backup)
        self.assertEqual(self.versions(record), ["a.txt", "locked.bin"])
        self.assertIn("left: locked.bin - could not be copied, and its backup copy could not be moved back out of "
                      "this repair's versions: Access is denied", self.said)

    def test_the_source_is_read_from_a_snapshot_when_a_run_would_be(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        self.verify()
        provider = FakeProvider(self.tmp / "shadows", {self.s.device: self.vol_s})
        record = self.repair(snapshots=provider)
        self.assertEqual(record.replaced, [("a.txt", 5)])
        self.assertEqual((len(provider.made), provider.deleted), (1, provider.made))
        self.assertIn("source read from a snapshot", self.said[1])

    def test_a_stop_before_its_turn_records_nothing(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        self.verify()
        board = live_activity(self.layout, "o" * 32, writes=(self.job.destinations[0].endpoint,))
        self.addCleanup(board.close)
        box = {}
        thread = threading.Thread(target=lambda: box.update(record=self.repair()), daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        mine = []
        while not mine and time.monotonic() < deadline:
            mine = [r for r in observe_runs(self.layout) if r.alive and r.status.get("kind") == KIND_REPAIR]
            time.sleep(0.05)
        self.assertTrue(mine, "the repair waits for its turn")
        self.assertEqual(mine[0].status.get("waiting_for", {}).get("run_id"), board.status.run_id)
        request(self.layout, mine[0].run_id, cancel=CancelMode.GRACEFUL)
        thread.join(20)
        self.assertIsNone(box.get("record", "not returned"))
        self.assertEqual(read_repairs(self.layout, self.job.job_id), [])


class RepairCommandTest(RepairCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store.path), *args], volume_source=self.volumes, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_repair_and_history_on_the_command_line(self):
        self.mirror({"a.txt": "alpha", "b.txt": "beta"})
        damage(self.dst / "a.txt")
        damage(self.dst / "b.txt")
        self.verify()
        write(self.src, "b.txt", "beta, changed")
        code, out, err = self.cli("repair", "Work")
        self.assertEqual(code, EXIT_HELD, out + err)
        self.assertIn("replaced: a.txt", out)
        self.assertIn("left: b.txt - changed since the verification: the next run copies it", out)
        self.assertIn("repair completed: 1 of 2 copies replaced, 1 left", out)
        code, out, _err = self.cli("history", "Work")
        self.assertIn("repair completed", out)
        code, out, err = self.cli("repair", "Work")
        self.assertEqual(code, EXIT_HELD)
        code, _out, err = self.cli("repair", "Work", "--dest", "2")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("cannot repair: the job has no destination 2", err)

    def test_a_repair_with_every_copy_replaced_ends_with_success(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        self.verify()
        code, out, err = self.cli("repair", "Work")
        self.assertEqual(code, EXIT_OK, out + err)
        code, _out, err = self.cli("repair", "Work")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("cannot repair: every copy that verification listed was replaced already", err)

    def test_a_repair_takes_its_destination_by_marker_wherever_the_job_puts_it(self):
        self.mirror({"a.txt": "alpha"})
        damage(self.dst / "a.txt")
        self.verify()
        self.job = replace(self.job, destinations=(Destination(endpoint(self.d, "Other")),) + self.job.destinations)
        self.store.save(StoreContents((self.job,)))
        code, _out, err = self.cli("repair", "Work", "--marker", "f" * 32)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(f"the job has no destination with marker '{'f' * 32}'", err)
        code, out, err = self.cli("repair", "Work", "--marker", self.marker[:8])
        self.assertEqual(code, EXIT_OK, out + err)
        record = read_repairs(self.layout, self.job.job_id)[-1]
        self.assertEqual((record.marker_id, record.destination_index, record.replaced),
                         (self.marker, 2, [("a.txt", 5)]))
        self.assertEqual((self.dst / "a.txt").read_text(encoding="utf-8"), "alpha")

    def test_a_destination_is_named_by_its_number_or_its_marker_and_one_the_job_holds(self):
        with self.assertRaisesRegex(RepairError, "no destination with marker " + "f" * 32):
            repair_destination(self.store, self.job.job_id, marker="f" * 32, volume_source=self.volumes)
        with self.assertRaisesRegex(RepairError, "by its number or by its marker"):
            repair_destination(self.store, self.job.job_id, 1, marker=self.marker, volume_source=self.volumes)
        self.assertEqual(read_repairs(self.layout, self.job.job_id), [])


class LaunchedRepairTest(TempDirTest):
    """Live path: a repair started as a process of its own, the way the GUI starts one, on
    this machine's real volume source (read-only), with a store in a temporary folder."""

    def test_a_launched_repair_runs_by_itself_and_records_what_it_did(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        source, target = self.tmp / "src", self.tmp / "dst"
        write(source, "a.txt", "alpha")
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(store), "add", "Live",
                                 "--source", str(source), "--dest", str(target), "--no-survey"], cwd=PROJECT,
                                capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        contents = ConfigStore(store).load().contents
        job = contents.jobs[0]
        job = replace(job, source=replace(job.source, read_from_snapshot=False))
        ConfigStore(store).save(StoreContents((job,)))
        target.mkdir()
        shutil.copy2(source / "a.txt", target / "a.txt")
        damage(target / "a.txt")
        write_marker(target, Marker(job.job_id, job.destinations[0].marker_id))
        found = VerificationRecord("v" * 32, job.job_id, job.name, job.destinations[0].marker_id, 1, utc_now(),
                                   utc_now(), state=VerificationState.COMPLETED, ended=utc_now(),
                                   differ=[("a.txt", 5)], differ_total=1)
        write_verification(runtime_layout(store), found)
        launch = start_repair(store, job.job_id, job.destinations[0].marker_id)
        try:
            launch.process.wait(120)
            self.assertEqual((launch.ended(), launch.problem()), (EXIT_OK, None))
        finally:
            launch.discard()
        record = read_repairs(runtime_layout(store), job.job_id)[-1]
        self.assertEqual((record.state, record.replaced), (RepairState.COMPLETED, [("a.txt", 5)]))
        self.assertEqual((target / "a.txt").read_text(encoding="utf-8"), "alpha")
        logs = sorted(runtime_layout(store).logs_dir.glob("*-repair-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn(f'"kind": "{KIND_REPAIR}"', logs[0].read_text(encoding="utf-8"))

    def test_a_launched_repair_finds_its_destination_by_marker(self):
        store = self.tmp / "Config" / "mirrorhold.json"
        source, target = self.tmp / "src", self.tmp / "dst"
        write(source, "a.txt", "alpha")
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(store), "add", "Live",
                                 "--source", str(source), "--dest", str(target), "--no-survey"], cwd=PROJECT,
                                capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = ConfigStore(store).load().contents.jobs[0]
        job = replace(job, source=replace(job.source, read_from_snapshot=False))
        marker = job.destinations[0].marker_id
        ConfigStore(store).save(StoreContents((job,)))
        target.mkdir()
        shutil.copy2(source / "a.txt", target / "a.txt")
        damage(target / "a.txt")
        write_marker(target, Marker(job.job_id, marker))
        found = VerificationRecord("v" * 32, job.job_id, job.name, marker, 1, utc_now(), utc_now(),
                                   state=VerificationState.COMPLETED, ended=utc_now(), differ=[("a.txt", 5)],
                                   differ_total=1)
        write_verification(runtime_layout(store), found)
        launch = start_repair(store, job.job_id, marker)
        try:
            launch.process.wait(120)
            self.assertEqual((launch.ended(), launch.problem()), (EXIT_OK, None))
        finally:
            launch.discard()
        record = read_repairs(runtime_layout(store), job.job_id)[-1]
        self.assertEqual((record.state, record.marker_id, record.replaced),
                         (RepairState.COMPLETED, marker, [("a.txt", 5)]))
        logs = sorted(runtime_layout(store).logs_dir.glob("*-repair-*.log"))
        self.assertIn(f"repair {job.job_id} --marker {marker}", logs[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
