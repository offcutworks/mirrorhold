"""What a drive is, said by the user: a drive marked a hard disk or a solid-state drive takes
the place of what its disk reports wherever the automatic choice decides turns by the drive's
kind — the maintainer's decision of 2026-10-21 — and, since 2026-11-01, a drive marked a cloud
drive. The expectations are that decision, the mark kept by the name the drive itself carries,
the job file's migration from schema 5, the one claim every run, verification and repair
builds, and the window's words."""
import json
import os
import shutil
import threading
import time
import unittest
from dataclasses import replace

from support import TempDirTest, endpoint, volume

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.marker import Marker, write_marker
from mirrorhold.endpoints.volumes import Backing, DriveKind, FixedVolumeSource, drive_name, lasting_drive
from mirrorhold.jobs.model import Activities, DriveMark, DriveTurns, Preferences, new_job
from mirrorhold.jobs.store import SCHEMA_VERSION, ConfigStore, LoadStatus, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.maintenance.verification import VerificationState
from mirrorhold.maintenance.verifier import verify_destination
from mirrorhold.records.admission import (Claim, ClaimedDrive, WaitedFor, activity_claim, run_claim,
                                          verification_claim)
from mirrorhold.records.record import utc_now
from mirrorhold.records.status import KIND_VERIFICATION, LiveStatus, Phase, StatusBoard, observe_runs
from mirrorhold_gui.groups_model import run_several, verify_several
from mirrorhold_gui.jobs_model import DestinationRow, JobRow, Tone
from mirrorhold_gui.prefs_model import (DRIVE_NOTE, KINDS_TITLE, NO_LASTING_NAME, DriveRow, drive_rows,
                                        drives_unreadable, kind_box_width, with_drive_kinds, with_drive_turns)

DS, DD = "disk:gpt:00000000-0000-4000-8000-0000000000d1", "disk:gpt:00000000-0000-4000-8000-0000000000d2"
DN = "disk:number:7"
S, D = replace(volume(1, "S"), disk=DS), replace(volume(2, "D"), disk=DD)


def of_kind(vol, kind):
    return replace(vol, disk_kind=kind)


# --- the mark ------------------------------------------------------------------------------

class DriveMarkTest(unittest.TestCase):
    def test_only_a_drive_whose_name_lasts_may_be_marked(self):
        self.assertTrue(lasting_drive(DS))
        self.assertTrue(lasting_drive("disk:mbr:0a1b2c3d"))
        self.assertTrue(lasting_drive(volume(9, "G").drive), "a volume no single disk carries, by its anchor")
        self.assertFalse(lasting_drive(DN))
        self.assertFalse(lasting_drive(""))
        with self.assertRaises(ContractError) as ctx:
            DriveMark(DN, "drive K:", DriveKind.SOLID)
        self.assertEqual(ctx.exception.where, "drive")

    def test_a_drive_is_marked_a_hard_disk_or_a_solid_state_drive_by_name(self):
        for kind in (DriveKind.SPINNING, DriveKind.SOLID):
            mark = DriveMark(DS, "drive S:", kind)
            self.assertEqual(DriveMark.from_document(mark.to_document()), mark)
        self.assertEqual(DriveMark(DS, "drive S:", DriveKind.SOLID).to_document(),
                         {"drive": DS, "name": "drive S:", "kind": "solid"})
        for bad in (DriveKind.UNKNOWN, "solid", None):
            with self.assertRaises(ContractError):
                DriveMark(DS, "drive S:", bad)
        with self.assertRaises(ContractError):
            DriveMark(DS, " ", DriveKind.SOLID)

    def test_a_drive_is_marked_a_cloud_drive_by_name(self):
        g = volume(9, "G", backing=Backing.VIRTUAL)
        for drive in (g.drive, DS):
            mark = DriveMark(drive, "drive G:", DriveKind.CLOUD)
            self.assertEqual(DriveMark.from_document(mark.to_document()), mark)
        self.assertEqual(DriveMark(g.drive, "drive G:", DriveKind.CLOUD).to_document(),
                         {"drive": g.drive, "name": "drive G:", "kind": "cloud"})

    def test_the_marks_are_kept_among_the_activity_choices_none_at_first_one_a_drive(self):
        self.assertEqual(Activities().drive_kinds, ())
        chosen = Activities(keep_awake=False, drive_kinds=[DriveMark(DS, "drive S:", DriveKind.SOLID),
                                                           DriveMark(DD, "drive D:", DriveKind.SPINNING)])
        self.assertIsInstance(chosen.drive_kinds, tuple)
        self.assertEqual(Activities.from_document(chosen.to_document()), chosen)
        with self.assertRaises(ContractError) as ctx:
            Activities(drive_kinds=(DriveMark(DS, "a", DriveKind.SOLID), DriveMark(DS, "b", DriveKind.SPINNING)))
        self.assertEqual(ctx.exception.where, "drive_kinds[1]")

    def test_a_mark_that_cannot_be_read_is_refused_at_its_field(self):
        doc = Preferences().to_document()
        for bad, where in (({"drive": DS, "name": "x", "kind": "hybrid"}, "activities.drive_kinds[0].kind"),
                           ({"drive": DN, "name": "x", "kind": "solid"}, "activities.drive_kinds[0].drive"),
                           ({"drive": DS, "name": "x"}, "activities.drive_kinds[0]")):
            doc["activities"]["drive_kinds"] = [bad]
            with self.assertRaises(ContractError) as ctx:
                Preferences.from_document(doc)
            self.assertEqual(ctx.exception.where, where, bad)
        doc["activities"]["drive_kinds"] = {"drive": DS}
        with self.assertRaises(ContractError):
            Preferences.from_document(doc)


class DriveMarkMigrationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "Backup")])

    def written(self, schema, activities):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        preferences = Preferences().to_document()
        preferences["activities"] = activities
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": schema,
                                         "jobs": [self.job.to_document()], "groups": [],
                                         "preferences": preferences}), encoding="utf-8")

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_a_schema_5_file_marks_no_drive_and_the_next_save_writes_the_current_schema(self):
        self.written(5, {"drive_turns": "at_the_same_time", "keep_awake": False, "name_at_shutdown": True})
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(result.contents.preferences.activities,
                         Activities(DriveTurns.AT_THE_SAME_TIME, False, True, ()))
        self.store.save(result.contents)
        self.assertEqual((self.saved()["schema_version"], self.saved()["preferences"]["activities"]["drive_kinds"]),
                         (SCHEMA_VERSION, []))

    def test_a_schema_4_file_passes_through_both_steps(self):
        self.written(4, {"one_per_drive": True, "keep_awake": True, "name_at_shutdown": False})
        result = self.store.load()
        self.assertEqual(result.contents.preferences.activities, Activities(DriveTurns.AUTOMATIC, True, False))

    def test_marks_are_saved_and_read_back(self):
        marks = (DriveMark(DD, "drive D:", DriveKind.SOLID),)
        self.store.save(StoreContents((self.job,), Preferences(activities=Activities(drive_kinds=marks))))
        self.assertEqual(self.saved()["preferences"]["activities"]["drive_kinds"],
                         [{"drive": DD, "name": "drive D:", "kind": "solid"}])
        self.assertEqual(self.store.load().contents.preferences.activities.drive_kinds, marks)

    def test_a_mark_that_cannot_be_read_is_a_corrupt_file(self):
        self.written(6, {"drive_turns": "automatic", "keep_awake": True, "name_at_shutdown": True,
                         "drive_kinds": [{"drive": DD, "name": "drive D:", "kind": "sometimes"}]})
        self.assertIs(self.store.load().status, LoadStatus.CORRUPT)


# --- the claim -----------------------------------------------------------------------------

class MarkedClaimTest(unittest.TestCase):
    def setUp(self):
        self.job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "Backup")])
        self.volumes = (of_kind(S, DriveKind.SOLID), of_kind(D, DriveKind.UNKNOWN))

    def test_a_mark_takes_the_place_of_what_the_disk_reports_either_way(self):
        marks = (DriveMark(DS, "an old name", DriveKind.SPINNING), DriveMark(DD, "drive D:", DriveKind.SOLID))
        claim = run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC, marks=marks)
        self.assertEqual(claim.drives, (ClaimedDrive(DS, "drive S:", DriveKind.SPINNING),
                                        ClaimedDrive(DD, "drive D:", DriveKind.SOLID)),
                         "the drive is named as it is called now")
        self.assertEqual([claim.takes_turns_on(d) for d in claim.drives], [True, False])
        check = verification_claim(self.job, 1, self.volumes, turns=DriveTurns.AUTOMATIC, marks=marks)
        self.assertEqual(check.drives, claim.drives)
        self.assertEqual(Claim.from_document(claim.to_document()), claim, "a claim publishes the kind it judged by")

    def test_a_drive_marked_a_cloud_drive_takes_no_turns_under_automatic(self):
        marks = (DriveMark(DD, "drive D:", DriveKind.CLOUD),)
        claim = run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC, marks=marks)
        self.assertEqual(claim.drives[1], ClaimedDrive(DD, "drive D:", DriveKind.CLOUD))
        self.assertFalse(claim.takes_turns_on(claim.drives[1]))
        g = of_kind(volume(9, "G", backing=Backing.VIRTUAL), DriveKind.CLOUD)
        job = new_job("Drive", endpoint(g, "Work"), [endpoint(D, "Backup")])
        hard = (DriveMark(g.drive, "drive G:", DriveKind.SPINNING),)
        claim = run_claim(job, [1], (g, of_kind(D, DriveKind.SOLID)), turns=DriveTurns.AUTOMATIC, marks=hard)
        self.assertEqual(claim.drives[0].kind, DriveKind.SPINNING, "a mark takes the place of a cloud drive too")
        self.assertTrue(claim.takes_turns_on(claim.drives[0]))

    def test_without_a_mark_for_it_a_drive_is_what_its_disk_reports(self):
        elsewhere = (DriveMark("disk:gpt:00000000-0000-4000-8000-0000000000e1", "drive E:", DriveKind.SOLID),)
        claim = run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC, marks=elsewhere)
        self.assertEqual([d.kind for d in claim.drives], [DriveKind.SOLID, DriveKind.UNKNOWN])
        self.assertEqual(claim, run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC))

    def test_always_and_never_ignore_the_marks_as_they_ignore_the_kind(self):
        marks = (DriveMark(DD, "drive D:", DriveKind.SOLID),)
        for turns, taken in ((DriveTurns.ONE_AT_A_TIME, True), (DriveTurns.AT_THE_SAME_TIME, False)):
            claim = run_claim(self.job, [1], self.volumes, turns=turns, marks=marks)
            self.assertEqual({claim.takes_turns_on(d) for d in claim.drives}, {taken}, turns)

    def test_every_activity_builds_its_claim_from_the_job_files_choice_and_marks_alike(self):
        activities = Activities(DriveTurns.AUTOMATIC, drive_kinds=(DriveMark(DD, "drive D:", DriveKind.SOLID),))
        self.assertEqual(activity_claim(self.job, [1], self.volumes, activities),
                         run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC,
                                   marks=activities.drive_kinds))
        self.assertEqual(activity_claim(self.job, [1], self.volumes, activities, verifying=True),
                         verification_claim(self.job, 1, self.volumes, turns=DriveTurns.AUTOMATIC,
                                            marks=activities.drive_kinds))
        self.assertIs(activity_claim(self.job, [1], self.volumes, activities).drives[1].kind, DriveKind.SOLID)


class MarkedVerificationTest(TempDirTest):
    """The live path: a verification reads the marks from the job file with the choice, and
    works beside a run on a drive that does not say what it is once it is marked solid, or
    waits for one on a solid drive marked a hard disk."""

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        self.src, self.dst = self.vol_s / "Work", self.vol_d / "Backup"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.s = replace(volume(1, mounts=(str(self.vol_s) + "\\",), label="S"), disk=DS, disk_kind=DriveKind.SOLID)
        self.d = replace(volume(2, mounts=(str(self.vol_d) + "\\",), label="D"), disk=DD)
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Backup")])
        self.layout = runtime_layout(self.store.path)
        write_marker(self.dst, Marker(self.job.job_id, self.job.destinations[0].marker_id))
        (self.src / "a.txt").write_text("a", encoding="utf-8")
        shutil.copy2(self.src / "a.txt", self.dst / "a.txt")

    def other_run(self, d):
        claim = Claim((endpoint(d, "OtherSource"),), (endpoint(d, "Other"),),
                      (ClaimedDrive(DD, drive_name((self.s, d), DD), DriveKind.SPINNING),), DriveTurns.AUTOMATIC)
        status = LiveStatus("o" * 32, os.getpid(), "k" * 32, "Other", "manual", utc_now(), admitted=True,
                            claim=claim.to_document(), phase=Phase.COPYING)
        board = StatusBoard(self.layout, status)
        board.open()
        self.addCleanup(board.close)
        return board

    def waiting_status(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for run in observe_runs(self.layout):
                if run.alive and run.status.get("kind") == KIND_VERIFICATION and run.status.get("waiting_for"):
                    return run.status
            time.sleep(0.05)
        self.fail("the verification never waited")

    def test_the_mark_in_the_job_file_decides_whether_it_waits(self):
        for reported, marked, waits in ((DriveKind.UNKNOWN, DriveKind.SOLID, False),
                                        (DriveKind.SOLID, DriveKind.SPINNING, True)):
            with self.subTest(reported=reported, marked=marked):
                d = of_kind(self.d, reported)
                marks = (DriveMark(DD, "drive 'D'", marked),)
                self.store.save(StoreContents((self.job,), Preferences(activities=Activities(drive_kinds=marks))))
                box = {}

                def work():
                    try:
                        box["record"] = verify_destination(self.store, self.job.job_id, 1,
                                                           volume_source=FixedVolumeSource((self.s, d)))
                    except BaseException as exc:  # reported by the test
                        box["error"] = exc
                thread = threading.Thread(target=work, daemon=True)
                run = self.other_run(d)
                thread.start()
                if waits:
                    status = self.waiting_status()
                    self.assertEqual(WaitedFor.from_status(status),
                                     WaitedFor(run.status.run_id, "run", "Other", drive="drive 'D'"))
                    run.close()
                thread.join(60)
                run.close()
                self.assertNotIn("error", box)
                self.assertEqual(box["record"].state, VerificationState.COMPLETED)


# --- the window ----------------------------------------------------------------------------

def job_row(name):
    destination = DestinationRow(name, 1, "m" * 32, "B", "present", "", "", Tone.NORMAL, can_verify=True)
    return JobRow(name, name.title(), "S:\\", "present", "", "", Tone.NORMAL, (destination,))


class DriveRowsTest(unittest.TestCase):
    def setUp(self):
        self.c = replace(volume(3, "C"), disk="disk:gpt:00000000-0000-4000-8000-0000000000c1",
                         disk_kind=DriveKind.SOLID)
        self.c2 = replace(volume(4, "D"), disk=self.c.disk, disk_kind=DriveKind.SOLID)
        self.usb = replace(volume(5, "I"), disk="disk:gpt:00000000-0000-4000-8000-0000000000a1")
        self.pool = replace(volume(6, "J"), disk="disk:gpt:00000000-0000-4000-8000-0000000000b1",
                            disk_kind=DriveKind.SPINNING)
        self.numbered = replace(volume(7, "K"), disk=DN)
        self.volumes = (self.usb, self.pool, self.c, self.c2, self.numbered)

    def test_every_drive_attached_by_name_then_each_marked_one_not_attached(self):
        away = DriveMark("disk:gpt:00000000-0000-4000-8000-0000000000f1", "drive F:", DriveKind.SOLID)
        rows = drive_rows(self.volumes, (DriveMark(self.usb.drive, "drive H:", DriveKind.SOLID), away))
        self.assertEqual([r.label for r in rows],
                         ["drive I:", "drive J:", "drive K:", "the drive of C: and D:", "drive F: (not attached)"])
        usb, pool, numbered, system, gone = rows
        self.assertEqual((usb.reported, usb.mark, usb.chosen), (DriveKind.UNKNOWN, DriveKind.SOLID, "Solid-state drive"))
        self.assertEqual([text for _kind, text in pool.choices],
                         ["As Windows reports: hard disk", "Hard disk", "Solid-state drive", "Cloud drive"])
        self.assertEqual(pool.chosen, "As Windows reports: hard disk")
        self.assertEqual(system.choices[0][1], "As Windows reports: solid-state drive")
        self.assertEqual(usb.choices[0][1], "As Windows reports: not said, taken for a hard disk")
        self.assertEqual((gone.attached, gone.choices[0][1], gone.chosen), (False, "As Windows reports",
                                                                             "Solid-state drive"))
        self.assertFalse(numbered.markable)
        self.assertTrue(all(r.markable for r in (usb, pool, system, gone)))

    def test_a_cloud_drive_is_reported_as_one_and_any_drive_may_be_set_to_one(self):
        g = of_kind(volume(9, "G", fs="FAT32", backing=Backing.VIRTUAL, label="Google Drive"), DriveKind.CLOUD)
        rows = drive_rows((g, self.pool), ())
        cloud, pool = rows
        self.assertEqual((cloud.label, cloud.reported, cloud.markable), ("drive G:", DriveKind.CLOUD, True))
        self.assertEqual([text for _kind, text in cloud.choices],
                         ["As Windows reports: no disk beneath it, taken for a cloud drive", "Hard disk",
                          "Solid-state drive", "Cloud drive"])
        self.assertEqual(cloud.chosen, "As Windows reports: no disk beneath it, taken for a cloud drive")
        self.assertEqual([kind for kind, _text in pool.choices],
                         [None, DriveKind.SPINNING, DriveKind.SOLID, DriveKind.CLOUD])
        saved = with_drive_kinds(Preferences(), rows, {pool.drive: DriveKind.CLOUD})
        self.assertEqual(saved.activities.drive_kinds, (DriveMark(self.pool.drive, "drive J:", DriveKind.CLOUD),))
        self.assertEqual(drive_rows((g, self.pool), saved.activities.drive_kinds)[1].chosen, "Cloud drive")

    def test_saving_marks_each_drive_as_chosen_names_it_as_now_and_keeps_the_rest(self):
        away = DriveMark("disk:gpt:00000000-0000-4000-8000-0000000000f1", "drive F:", DriveKind.SOLID)
        chosen = with_drive_turns(Preferences(activities=Activities(keep_awake=False, drive_kinds=(
            DriveMark(self.usb.drive, "drive H:", DriveKind.SOLID), away))), DriveTurns.AUTOMATIC)
        rows = drive_rows(self.volumes, chosen.activities.drive_kinds)
        saved = with_drive_kinds(chosen, rows, {self.pool.drive: DriveKind.SOLID, self.numbered.drive: DriveKind.SOLID,
                                                away.drive: None})
        self.assertEqual(saved.activities.drive_kinds,
                         (DriveMark(self.usb.drive, "drive I:", DriveKind.SOLID),
                          DriveMark(self.pool.drive, "drive J:", DriveKind.SOLID)))
        self.assertEqual(replace(saved.activities, drive_kinds=()), Activities(keep_awake=False))
        self.assertEqual(with_drive_kinds(saved, drive_rows(self.volumes, saved.activities.drive_kinds),
                                          {self.usb.drive: None, self.pool.drive: None}).activities.drive_kinds, ())

    def test_drives_that_share_a_name_are_told_apart_by_size_and_file_system(self):
        bare = [replace(volume(10 + i, capacity=gib * 1024 ** 3, fs=fs),
                        disk=f"disk:gpt:00000000-0000-4000-8000-0000000000{i}0")
                for i, (gib, fs) in enumerate(((1862, "NTFS"), (931, "exFAT")), start=1)]
        rows = drive_rows((self.usb, *bare), ())
        self.assertEqual([r.label for r in rows],
                         ["a drive with no letter (1.8 TiB, NTFS)", "a drive with no letter (931.0 GiB, exFAT)",
                          "drive I:"])
        self.assertEqual(rows[-1].name, "drive I:", "a name no other drive shares stays as it is")
        marked = with_drive_kinds(Preferences(), rows, {bare[0].drive: DriveKind.SOLID})
        self.assertEqual(marked.activities.drive_kinds[0].name, "a drive with no letter (1.8 TiB, NTFS)")

    def test_a_mark_saved_since_the_rows_were_read_is_kept(self):
        rows = drive_rows(self.volumes, ())
        since = DriveMark("disk:gpt:00000000-0000-4000-8000-0000000000f1", "drive F:", DriveKind.SOLID)
        now = Preferences(activities=Activities(drive_kinds=(since,)))
        saved = with_drive_kinds(now, rows, {self.usb.drive: DriveKind.SOLID})
        self.assertEqual(saved.activities.drive_kinds, (since, DriveMark(self.usb.drive, "drive I:", DriveKind.SOLID)))

    def test_every_box_takes_the_width_of_the_longest_choice_of_any_row(self):
        away = DriveMark("disk:gpt:00000000-0000-4000-8000-0000000000f1", "drive F:", DriveKind.SOLID)
        rows = drive_rows((self.pool, self.usb), (away,))
        self.assertEqual(kind_box_width(rows), len("As Windows reports: not said, taken for a hard disk"))
        g = of_kind(volume(9, "G", fs="FAT32", backing=Backing.VIRTUAL, label="Google Drive"), DriveKind.CLOUD)
        self.assertEqual(kind_box_width(drive_rows((self.pool, g), ())),
                         len("As Windows reports: no disk beneath it, taken for a cloud drive"))
        self.assertEqual(kind_box_width(drive_rows((), (away,))), len("As Windows reports"))
        self.assertEqual(kind_box_width(()), 0)

    def test_the_words_of_the_drives_and_what_sharing_one_means_under_automatic(self):
        self.assertEqual(KINDS_TITLE, "What each drive is, for Automatic:")
        self.assertEqual(DRIVE_NOTE, "A drive is one disk: two letters on one disk are one drive. Automatic takes a "
                                     "drive that does not say what it is, as many USB drives do not, as a hard disk, "
                                     "unless you say below what it is. Activities that reach a remote always take "
                                     "turns with each other.")
        self.assertEqual(NO_LASTING_NAME, "Windows gives this drive no lasting name, so what it is cannot be kept.")
        self.assertEqual(drives_unreadable("Access is denied"), "The drives could not be read: Access is denied.")
        self.assertEqual(DriveRow(DD, "drive D:", True).label, "drive D:")
        rows = [job_row("a" * 32), job_row("b" * 32)]
        destinations = [r.destinations[0] for r in rows]
        self.assertEqual(run_several(rows).text.splitlines()[-1],
                         "Jobs that use the same drive run one at a time unless it is a solid-state or cloud drive, as "
                         "Windows reports or as set in Preferences; the others run at the same time.")
        self.assertEqual(verify_several(destinations, {r.job_id: r for r in rows}).text.splitlines()[-2],
                         "Verifications that use the same drive run one at a time unless it is a solid-state or cloud "
                         "drive, as Windows reports or as set in Preferences; the others run at the same time.")


if __name__ == "__main__":
    unittest.main()
