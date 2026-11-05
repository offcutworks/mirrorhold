"""Turns on a drive: whether two activities that use one drive take turns on it is decided per
drive, by what the drive's disk reports of a seek penalty, under the user's three-way choice
— the maintainer's decision of 2026-10-21. A spinning disk, and a drive whose kind is not
known, take turns under the automatic choice; a drive with no seek penalty does not, nor does
a cloud drive, a volume whose device is a file system (2026-11-01). The expectations are that
decision, the job file's migration from schema 4 and a save writing the current schema — the
step from schema 5 to 6, which adds the drive marks, is bound by ``test_drive_marks`` — and the
claim a status publishes, as its older form is read too."""
import json
import os
import shutil
import sys
import threading
import time
import unittest
from dataclasses import replace

from support import TempDirTest, endpoint, volume

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.marker import Marker, write_marker
from mirrorhold.endpoints.volumes import (FILE_DEVICE_DISK_FILE_SYSTEM, Backing, DriveKind, FixedVolumeSource,
                                         decode_seek_penalty, device_kind, drive_name)
from mirrorhold.jobs.model import Activities, DriveTurns, Preferences, new_job
from mirrorhold.jobs.store import MIGRATIONS, SCHEMA_VERSION, ConfigStore, LoadStatus, StoreContents
from mirrorhold.locations import RuntimeLayout, runtime_layout
from mirrorhold.maintenance.verification import VerificationState
from mirrorhold.maintenance.verifier import verify_destination
from mirrorhold.records.admission import (Activity, Claim, ClaimedDrive, WaitedFor, admit, blockers, conflict,
                                          gives_way, run_claim, verification_claim)
from mirrorhold.records.record import utc_now
from mirrorhold.records.status import KIND_VERIFICATION, LiveStatus, Phase, StatusBoard, observe_runs
from mirrorhold_gui.groups_model import run_several, verify_several
from mirrorhold_gui.jobs_model import DestinationRow, JobRow, Tone
from mirrorhold_gui.prefs_model import (AT_THE_SAME_TIME, AUTOMATIC, DRIVE_CHOICES, ONE_AT_A_TIME, with_drive_turns,
                                        with_keep_awake)

DS, DD = "disk:gpt:00000000-0000-4000-8000-0000000000d1", "disk:gpt:00000000-0000-4000-8000-0000000000d2"
S, D = replace(volume(1, "S"), disk=DS), replace(volume(2, "D"), disk=DD)


def of_kind(vol, kind):
    return replace(vol, disk_kind=kind)


def seek_descriptor(incurs: int) -> bytes:
    """A DEVICE_SEEK_PENALTY_DESCRIPTOR as Windows returns it: Version, Size, IncursSeekPenalty."""
    return (12).to_bytes(4, "little") + (12).to_bytes(4, "little") + bytes((incurs, 0, 0, 0))


# --- what a drive is ---------------------------------------------------------------------

class DriveKindTest(unittest.TestCase):
    def test_a_disk_is_spinning_with_a_seek_penalty_solid_without_and_not_known_when_it_does_not_say(self):
        self.assertIs(decode_seek_penalty(seek_descriptor(1)), DriveKind.SPINNING)
        self.assertIs(decode_seek_penalty(seek_descriptor(0)), DriveKind.SOLID)
        for silent in (None, b"", seek_descriptor(1)[:8]):
            self.assertIs(decode_seek_penalty(silent), DriveKind.UNKNOWN, silent)

    def test_a_volume_takes_its_disks_kind_and_one_no_single_disk_carries_is_not_known(self):
        self.assertIs(S.drive_kind, DriveKind.UNKNOWN, "a disk that was not asked is not known")
        self.assertIs(of_kind(S, DriveKind.SOLID).drive_kind, DriveKind.SOLID)
        cloud = of_kind(volume(9, "G"), DriveKind.SOLID)
        self.assertIsNone(cloud.disk)
        self.assertIs(cloud.drive_kind, DriveKind.UNKNOWN)

    def test_a_volume_whose_device_is_a_file_system_is_a_cloud_drive_and_any_other_is_not_known(self):
        self.assertIs(device_kind(FILE_DEVICE_DISK_FILE_SYSTEM), DriveKind.CLOUD)
        self.assertEqual(FILE_DEVICE_DISK_FILE_SYSTEM, 8, "as Google Drive for desktop's drive answers")
        for other in (7, 0x14, 0x24, 0):
            self.assertIs(device_kind(other), DriveKind.UNKNOWN, other)
        cloud = of_kind(volume(9, "G", backing=Backing.VIRTUAL), DriveKind.CLOUD)
        self.assertIsNone(cloud.disk)
        self.assertIs(cloud.drive_kind, DriveKind.CLOUD)
        unasked = volume(9, "G", backing=Backing.VIRTUAL)
        self.assertIs(unasked.drive_kind, DriveKind.UNKNOWN, "no partition alone never makes a cloud drive")
        on_a_disk = replace(volume(9, "G", backing=Backing.VIRTUAL), disk=DS, disk_kind=DriveKind.SPINNING)
        self.assertIs(on_a_disk.drive_kind, DriveKind.SPINNING, "a disk Windows names keeps its own kind")

    @unittest.skipUnless(sys.platform == "win32", "Windows")
    def test_only_a_volume_windows_names_no_disk_for_may_be_a_cloud_drive(self):
        from mirrorhold.endpoints.volumes import WindowsVolumeSource
        for found in WindowsVolumeSource().scan().volumes:
            allowed = {DriveKind.CLOUD, DriveKind.UNKNOWN} if found.disk is None else set(DriveKind) - {DriveKind.CLOUD}
            self.assertIn(found.drive_kind, allowed, found.root)

    @unittest.skipUnless(sys.platform == "win32", "Windows")
    def test_every_volume_windows_reports_has_a_kind(self):
        from mirrorhold.endpoints.volumes import WindowsVolumeSource
        for found in WindowsVolumeSource().scan().volumes:
            self.assertIsInstance(found.drive_kind, DriveKind)
            if found.disk is None:
                self.assertIn(found.drive_kind, (DriveKind.CLOUD, DriveKind.UNKNOWN), found.root)


# --- the choice, and the job file ----------------------------------------------------------

class DriveTurnsChoiceTest(unittest.TestCase):
    def test_automatic_is_the_first_choice_and_each_is_kept_by_name(self):
        self.assertIs(Activities().drive_turns, DriveTurns.AUTOMATIC)
        for turns in DriveTurns:
            chosen = Activities(drive_turns=turns, keep_awake=False)
            self.assertEqual(Activities.from_document(chosen.to_document()), chosen)
        self.assertEqual(Activities().to_document(),
                         {"drive_turns": "automatic", "keep_awake": True, "name_at_shutdown": True, "drive_kinds": []})

    def test_a_choice_that_is_none_of_the_three_is_refused_at_its_field(self):
        doc = Preferences().to_document()
        for bad in ("sometimes", True, None):
            doc["activities"]["drive_turns"] = bad
            with self.assertRaises(ContractError) as ctx:
                Preferences.from_document(doc)
            self.assertEqual(ctx.exception.where, "activities.drive_turns", bad)
        doc["activities"] = {"one_per_drive": True, "keep_awake": True, "name_at_shutdown": True}
        with self.assertRaises(ContractError):
            Preferences.from_document(doc)
        with self.assertRaises(ContractError):
            Activities(drive_turns="automatic")


class DriveTurnsMigrationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "Backup")])

    def written(self, schema, activities):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        preferences = Preferences().to_document()
        if activities is None:
            del preferences["activities"]
        else:
            preferences["activities"] = activities
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": schema,
                                         "jobs": [self.job.to_document()], "groups": [],
                                         "preferences": preferences}), encoding="utf-8")

    def test_one_at_a_time_on_reads_as_automatic_and_off_as_always_at_the_same_time(self):
        for on, turns in ((True, DriveTurns.AUTOMATIC), (False, DriveTurns.AT_THE_SAME_TIME)):
            self.written(4, {"one_per_drive": on, "keep_awake": False, "name_at_shutdown": True})
            result = self.store.load()
            self.assertIs(result.status, LoadStatus.MIGRATED)
            self.assertEqual(result.contents.preferences.activities, Activities(turns, False, True))
            self.store.save(result.contents)
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual((doc["schema_version"], doc["preferences"]["activities"]),
                             (SCHEMA_VERSION, {"drive_turns": turns.value, "keep_awake": False,
                                               "name_at_shutdown": True, "drive_kinds": []}))

    def test_a_file_written_before_the_choices_reads_as_automatic(self):
        self.written(3, None)
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(result.contents.preferences.activities, Activities())

    def test_a_value_that_is_not_on_or_off_is_a_corrupt_file(self):
        self.written(4, {"one_per_drive": "yes", "keep_awake": True, "name_at_shutdown": True})
        self.assertIs(self.store.load().status, LoadStatus.CORRUPT)

    def test_the_step_writes_the_values_schema_5_names_as_literals(self):
        for on, written in ((True, "automatic"), (False, "at_the_same_time")):
            doc = {"preferences": {"activities": {"one_per_drive": on, "keep_awake": True, "name_at_shutdown": True}}}
            self.assertEqual(MIGRATIONS[4](doc)["preferences"]["activities"],
                             {"drive_turns": written, "keep_awake": True, "name_at_shutdown": True})


# --- the claim -----------------------------------------------------------------------------

class TurnsClaimTest(unittest.TestCase):
    def setUp(self):
        self.job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "Backup")])
        self.volumes = (of_kind(S, DriveKind.SOLID), of_kind(D, DriveKind.SPINNING))

    def test_a_claim_names_each_drives_kind_as_its_scan_read_it(self):
        claim = run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC)
        self.assertEqual(claim.drives, (ClaimedDrive(DS, "drive S:", DriveKind.SOLID),
                                        ClaimedDrive(DD, "drive D:", DriveKind.SPINNING)))
        self.assertIs(claim.turns, DriveTurns.AUTOMATIC)
        check = verification_claim(self.job, 1, self.volumes, turns=DriveTurns.ONE_AT_A_TIME)
        self.assertEqual((check.drives, check.turns), (claim.drives, DriveTurns.ONE_AT_A_TIME))
        self.assertEqual(Claim.from_document(claim.to_document()), claim)

    def test_automatic_takes_turns_on_every_drive_not_known_to_have_no_seek_penalty(self):
        solid, spinning, unknown = (ClaimedDrive("x", "x", kind) for kind in
                                    (DriveKind.SOLID, DriveKind.SPINNING, DriveKind.UNKNOWN))
        taken = {turns: [Claim(turns=turns).takes_turns_on(d) for d in (solid, spinning, unknown)]
                 for turns in DriveTurns}
        self.assertEqual(taken, {DriveTurns.AUTOMATIC: [False, True, True],
                                 DriveTurns.ONE_AT_A_TIME: [True, True, True],
                                 DriveTurns.AT_THE_SAME_TIME: [False, False, False]})

    def test_a_claim_publishes_for_an_older_copy_whether_it_takes_turns_on_any_drive(self):
        on_solid = run_claim(self.job, [1], (of_kind(S, DriveKind.SOLID), of_kind(D, DriveKind.SOLID)),
                             turns=DriveTurns.AUTOMATIC)
        self.assertFalse(on_solid.to_document()["one_per_drive"])
        self.assertTrue(run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC)
                        .to_document()["one_per_drive"])
        self.assertTrue(run_claim(self.job, [1], self.volumes, turns=DriveTurns.ONE_AT_A_TIME)
                        .to_document()["one_per_drive"])
        self.assertFalse(run_claim(self.job, [1], self.volumes, turns=DriveTurns.AT_THE_SAME_TIME)
                         .to_document()["one_per_drive"])

    def test_an_older_copys_claim_takes_turns_on_all_its_drives_or_none_their_kind_not_known(self):
        older = {"reads": [endpoint(S, "Work").to_document()], "writes": [],
                 "drives": [{"drive": DS, "name": "drive S:"}], "one_per_drive": True}
        self.assertEqual(Claim.from_document(older),
                         Claim((endpoint(S, "Work"),), (), (ClaimedDrive(DS, "drive S:", DriveKind.UNKNOWN),),
                               DriveTurns.ONE_AT_A_TIME))
        self.assertIs(Claim.from_document(dict(older, one_per_drive=False)).turns, DriveTurns.AT_THE_SAME_TIME)
        oldest = {"reads": [endpoint(S, "Work").to_document()], "writes": []}
        self.assertEqual(Claim.from_document(oldest), Claim((endpoint(S, "Work"),), ()))

    def test_automatic_takes_no_turns_on_a_cloud_drive_and_the_others_ignore_its_kind(self):
        cloud = ClaimedDrive("x", "x", DriveKind.CLOUD)
        self.assertEqual([Claim(turns=turns).takes_turns_on(cloud) for turns in DriveTurns],
                         [False, True, False])

    def test_a_cloud_drive_is_published_and_read_back_by_its_kind(self):
        g = of_kind(volume(9, "G", backing=Backing.VIRTUAL), DriveKind.CLOUD)
        job = new_job("Drive", endpoint(g, "Work"), [endpoint(D, "Backup")])
        claim = run_claim(job, [1], (g, of_kind(D, DriveKind.SPINNING)), turns=DriveTurns.AUTOMATIC)
        self.assertEqual(claim.drives[0], ClaimedDrive(g.drive, "drive G:", DriveKind.CLOUD))
        self.assertEqual(claim.to_document()["drives"][0]["kind"], "cloud")
        self.assertEqual(Claim.from_document(claim.to_document()), claim)

    def test_a_kind_this_copy_does_not_know_is_not_known_and_an_unreadable_claim_is_none(self):
        doc = run_claim(self.job, [1], self.volumes, turns=DriveTurns.AUTOMATIC).to_document()
        doc["drives"][0]["kind"] = "hybrid"
        self.assertIs(Claim.from_document(doc).drives[0].kind, DriveKind.UNKNOWN)
        bad_kind = {"turns": "automatic", "drives": [{"drive": DS, "name": "x", "kind": 1}]}
        for bad in ({"turns": "sometimes"}, {"turns": None}, bad_kind):
            self.assertIsNone(Claim.from_document(dict(doc, **bad)), bad)
        with self.assertRaises(TypeError):
            Claim(turns=True)


def busy(job, *, on, turns=DriveTurns.AUTOMATIC, admitted=True, queued="T1", kind="run"):
    """An activity of ``job`` on folders of its own, naming the drives ``on``: (drive, kind) pairs."""
    reads = (endpoint(S, job),) if kind == "run" else (endpoint(S, job), endpoint(D, job))
    writes = (endpoint(D, job),) if kind == "run" else ()
    drives = tuple(ClaimedDrive(drive, drive, k) for drive, k in on)
    return Activity(f"{job}-{queued}", kind, job, job.title(), f"m-{job}" if kind != "run" else "", queued,
                    admitted, False, Claim(reads, writes, drives, turns))


class TurnsConflictTest(unittest.TestCase):
    def test_under_automatic_two_on_one_drive_take_turns_unless_it_has_no_seek_penalty(self):
        for kind, expected in ((DriveKind.SPINNING, True), (DriveKind.UNKNOWN, True), (DriveKind.SOLID, False)):
            me, other = busy("me", on=((DD, kind),)), busy("other", on=((DD, kind),))
            self.assertEqual(conflict(me, other, by_drive=True), expected, kind)
            self.assertFalse(conflict(me, other), "the folders alone do not conflict")

    def test_under_automatic_two_on_one_cloud_drive_work_together(self):
        me, other = busy("me", on=((DD, DriveKind.CLOUD),)), busy("other", on=((DD, DriveKind.CLOUD),))
        self.assertFalse(conflict(me, other, by_drive=True))
        self.assertTrue(conflict(busy("me", on=((DD, DriveKind.CLOUD),), turns=DriveTurns.ONE_AT_A_TIME), other,
                                 by_drive=True))

    def test_sharing_only_a_solid_drive_they_work_together_and_a_spinning_one_still_counts(self):
        source_only = busy("me", on=((DS, DriveKind.SOLID), (DD, DriveKind.SPINNING)))
        elsewhere = busy("other", on=((DS, DriveKind.SOLID), ("disk:gpt:e", DriveKind.SPINNING)))
        self.assertFalse(conflict(source_only, elsewhere, by_drive=True))
        same_backup = busy("third", on=((DS, DriveKind.SOLID), (DD, DriveKind.SPINNING)))
        self.assertTrue(conflict(source_only, same_backup, by_drive=True))

    def test_always_and_never_ignore_the_kind_and_folders_conflict_whatever_the_choice(self):
        solid = ((DD, DriveKind.SOLID),)
        self.assertTrue(conflict(busy("me", on=solid, turns=DriveTurns.ONE_AT_A_TIME), busy("o", on=solid),
                                 by_drive=True))
        spinning = ((DD, DriveKind.SPINNING),)
        self.assertFalse(conflict(busy("me", on=spinning, turns=DriveTurns.AT_THE_SAME_TIME), busy("o", on=spinning),
                                  by_drive=True))
        again = busy("me", on=spinning, turns=DriveTurns.AT_THE_SAME_TIME, queued="T2")
        self.assertTrue(conflict(again, busy("me", on=spinning)), "two runs of one job")

    def test_the_waiting_activity_judges_by_its_own_reading_and_choice(self):
        holder = busy("holder", on=((DD, DriveKind.SPINNING),), queued="T0")
        reads_solid = busy("me", on=((DD, DriveKind.SOLID),), admitted=False)
        self.assertEqual(blockers(reads_solid, [holder]), [])
        reads_spinning = busy("me", on=((DD, DriveKind.SPINNING),), admitted=False)
        self.assertEqual(blockers(reads_spinning, [holder]), [holder])


class RemoteTurnsTest(unittest.TestCase):
    """A remote's folder in a claim: claimed whenever selected, on no drive, and every two
    activities that reach a remote taking turns whatever the choice for drives."""

    def remote_activity(self, job, folder, *, turns=DriveTurns.AT_THE_SAME_TIME, queued="T1"):
        claim = Claim((endpoint(S, job),), (RemoteEndpoint("cloud", folder),), (), turns)
        return Activity(f"{job}-{queued}", "run", job, job.title(), "", queued, True, False, claim)

    def test_a_remote_destination_is_claimed_whatever_the_volumes_say_and_lies_on_no_drive(self):
        job = new_job("Work", endpoint(S, "Work"), [RemoteEndpoint("cloud", "Work"), endpoint(D, "Backup")])
        claim = run_claim(job, [1, 2], (of_kind(S, DriveKind.SOLID),), turns=DriveTurns.AUTOMATIC)
        self.assertEqual((claim.writes, claim.drives), ((RemoteEndpoint("cloud", "Work"),),
                                                       (ClaimedDrive(DS, "drive S:", DriveKind.SOLID),)))
        self.assertTrue(claim.reaches_remote)
        self.assertEqual(Claim.from_document(json.loads(json.dumps(claim.to_document()))), claim)
        self.assertEqual(claim.to_document()["writes"], [{"remote": "cloud", "folder": "Work"}])
        self.assertTrue(verification_claim(job, 1).reaches_remote)
        self.assertFalse(verification_claim(job, 2).reaches_remote)

    def test_two_activities_reaching_remotes_take_turns_whatever_the_choice_and_folders(self):
        one = self.remote_activity("one", "A")
        two = self.remote_activity("two", "B", queued="T2")
        self.assertTrue(conflict(one, two))
        self.assertTrue(conflict(two, one, by_drive=True))
        drive_only = busy("drive", on=((DD, DriveKind.SOLID),), turns=DriveTurns.AT_THE_SAME_TIME)
        self.assertFalse(conflict(one, drive_only, by_drive=True))
        self.assertFalse(conflict(drive_only, one, by_drive=True))

    def test_remote_folders_conflict_by_remote_and_folder_and_never_with_a_drives(self):
        outer, inner = RemoteEndpoint("cloud", "Backups"), RemoteEndpoint("Cloud", "backups/Work")
        self.assertTrue(outer.overlaps(inner))
        self.assertFalse(outer.overlaps(RemoteEndpoint("cloud", "Backups2")))
        self.assertFalse(outer.overlaps(RemoteEndpoint("other", "Backups")))
        self.assertFalse(outer.overlaps(endpoint(D, "Backups")))
        self.assertFalse(endpoint(D, "").overlaps(outer))


class TurnsThroughStatusTest(TempDirTest):
    """Turns on a drive through the status channel: what a waiting activity publishes, and a
    verification giving way as the waiting run judges its own turn."""

    def setUp(self):
        super().setUp()
        self.layout = RuntimeLayout(self.tmp)

    def publish(self, letter, claim, *, admitted=True, kind="run", doc=None):
        status = LiveStatus(letter * 32, os.getpid(), letter * 32, letter.upper(), "manual", "2026-10-21T10:00:00Z",
                            kind=kind, marker_id=f"m{letter}" if kind != "run" else None, admitted=admitted,
                            claim=doc if doc is not None else claim.to_document() if claim is not None else None,
                            phase=Phase.COPYING if admitted else Phase.WAITING)
        board = StatusBoard(self.layout, status)
        board.open()
        self.addCleanup(board.close)
        return board

    @staticmethod
    def claim(folder, kind, turns=DriveTurns.AUTOMATIC):
        return Claim((endpoint(S, folder),), (endpoint(D, folder),), (ClaimedDrive(DD, "drive D:", kind),), turns)

    def test_on_a_spinning_drive_it_waits_naming_the_drive_and_on_a_solid_one_it_works(self):
        holder = self.publish("h", self.claim("H", DriveKind.SPINNING))
        me = self.publish("m", None, admitted=False)
        self.assertFalse(admit(me, self.claim("Me", DriveKind.SPINNING)).admitted)
        status = next(r.status for r in observe_runs(self.layout) if r.run_id == me.status.run_id)
        self.assertEqual(WaitedFor.from_status(status), WaitedFor(holder.status.run_id, "run", "H", drive="drive D:"))
        self.assertTrue(admit(me, self.claim("Me", DriveKind.SOLID)).admitted)

    def test_a_verification_gives_way_to_a_run_waiting_on_a_spinning_drive_only(self):
        check = self.publish("c", Claim((endpoint(S, "Work"), endpoint(D, "Backup")), (),
                                        (ClaimedDrive(DD, "drive D:", DriveKind.SPINNING),), DriveTurns.AUTOMATIC),
                             kind="verification")
        waiting = self.publish("w", self.claim("Other", DriveKind.SPINNING), admitted=False)
        self.assertTrue(gives_way(self.layout, check.status))
        waiting.status.claim = self.claim("Other", DriveKind.SOLID).to_document()
        waiting.publish()
        self.assertFalse(gives_way(self.layout, check.status), "the run reads its drive as solid")

    def test_a_verification_gives_way_to_an_older_copys_run_that_takes_its_drives_one_at_a_time(self):
        check = self.publish("c", Claim((endpoint(S, "Work"), endpoint(D, "Backup")), (),
                                        (ClaimedDrive(DD, "drive D:", DriveKind.SOLID),), DriveTurns.AUTOMATIC),
                             kind="verification")
        older = {"reads": [endpoint(S, "Other").to_document()], "writes": [endpoint(D, "Other").to_document()],
                 "drives": [{"drive": DD, "name": "drive D:"}], "one_per_drive": True}
        self.publish("w", None, admitted=False, doc=older)
        self.assertTrue(gives_way(self.layout, check.status))


class VerificationTurnsTest(TempDirTest):
    """The live path: a verification reads the choice from the job file and its drives' kinds
    from its own scan, and waits for a run on its destination's drive, or works beside it."""

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
        """Another job's run at work on folders of its own on the destination's drive alone."""
        claim = Claim((endpoint(d, "OtherSource"),), (endpoint(d, "Other"),),
                      (ClaimedDrive(DD, drive_name((self.s, d), DD), d.drive_kind),), DriveTurns.AUTOMATIC)
        status = LiveStatus("o" * 32, os.getpid(), "k" * 32, "Other", "manual", utc_now(), admitted=True,
                            claim=claim.to_document(), phase=Phase.COPYING)
        board = StatusBoard(self.layout, status)
        board.open()
        self.addCleanup(board.close)
        return board

    def verify(self, kind, turns):
        """Starts the verification with the destination's drive of ``kind`` and the job file
        holding ``turns``; its thread and what it returns or raises."""
        d = of_kind(self.d, kind)
        self.store.save(StoreContents((self.job,), Preferences(activities=Activities(drive_turns=turns))))
        box = {}

        def work():
            try:
                box["record"] = verify_destination(self.store, self.job.job_id, 1,
                                                   volume_source=FixedVolumeSource((self.s, d)))
            except BaseException as exc:  # reported by the test
                box["error"] = exc
        thread = threading.Thread(target=work, daemon=True)
        return d, thread, box

    def waiting_status(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for run in observe_runs(self.layout):
                if run.alive and run.status.get("kind") == KIND_VERIFICATION and run.status.get("waiting_for"):
                    return run.status
            time.sleep(0.05)
        self.fail("the verification never waited")

    def test_it_waits_for_a_run_on_a_spinning_drive_and_works_beside_one_on_a_solid_drive(self):
        for kind, turns, waits in ((DriveKind.SPINNING, DriveTurns.AUTOMATIC, True),
                                   (DriveKind.UNKNOWN, DriveTurns.AUTOMATIC, True),
                                   (DriveKind.SOLID, DriveTurns.ONE_AT_A_TIME, True),
                                   (DriveKind.SOLID, DriveTurns.AUTOMATIC, False),
                                   (DriveKind.SPINNING, DriveTurns.AT_THE_SAME_TIME, False)):
            with self.subTest(kind=kind, turns=turns):
                d, thread, box = self.verify(kind, turns)
                run = self.other_run(d)
                thread.start()
                if waits:
                    status = self.waiting_status()
                    self.assertEqual(WaitedFor.from_status(status),
                                     WaitedFor(run.status.run_id, "run", "Other", drive="drive 'D'"))
                    run.close()
                thread.join(60)
                run.close()
                self.assertFalse(thread.is_alive(), "the verification did not finish within a minute")
                self.assertNotIn("error", box)
                self.assertEqual(box["record"].state, VerificationState.COMPLETED)
            if thread.is_alive():
                break   # it would run on into the next case, which reuses its job file and the other run's identifier


# --- the window's words --------------------------------------------------------------------

def job_row(name, *, verifiable=True):
    destination = DestinationRow(name, 1, "m" * 32, "B", "present", "", "", Tone.NORMAL, can_verify=verifiable)
    return JobRow(name, name.title(), "S:\\", "present", "", "", Tone.NORMAL, (destination,))


class DriveTurnsWordsTest(unittest.TestCase):
    def test_the_three_choices_in_preferences_and_one_changed_keeps_the_others(self):
        self.assertEqual(DRIVE_CHOICES, ((DriveTurns.AUTOMATIC, AUTOMATIC), (DriveTurns.ONE_AT_A_TIME, ONE_AT_A_TIME),
                                         (DriveTurns.AT_THE_SAME_TIME, AT_THE_SAME_TIME)))
        self.assertEqual((AUTOMATIC, ONE_AT_A_TIME, AT_THE_SAME_TIME),
                         ("Automatic: one at a time on a hard disk, at the same time on a solid-state or cloud drive",
                          "Always one at a time: each waits for its turn",
                          "Always at the same time: they share the drive's speed"))
        chosen = with_keep_awake(Preferences(), False)
        changed = with_drive_turns(chosen, DriveTurns.AT_THE_SAME_TIME)
        self.assertEqual(changed.activities, Activities(DriveTurns.AT_THE_SAME_TIME, keep_awake=False))
        self.assertEqual(with_drive_turns(changed, DriveTurns.AUTOMATIC), chosen)

    def test_the_questions_before_several_say_what_sharing_a_drive_means_as_chosen(self):
        rows = [job_row("a" * 32), job_row("b" * 32)]
        destinations = [r.destinations[0] for r in rows]
        jobs = {r.job_id: r for r in rows}
        said = {turns: (run_several(rows, turns=turns).text.splitlines()[-1],
                        verify_several(destinations, jobs, turns=turns).text.splitlines()[-2])
                for turns in DriveTurns}
        self.assertEqual(said, {
            DriveTurns.AUTOMATIC: (
                "Jobs that use the same drive run one at a time unless it is a solid-state or cloud drive, as Windows "
                "reports or as set in Preferences; the others run at the same time.",
                "Verifications that use the same drive run one at a time unless it is a solid-state or cloud drive, as "
                "Windows reports or as set in Preferences; the others run at the same time."),
            DriveTurns.ONE_AT_A_TIME: (
                "Jobs that use the same drive run one at a time; the others run at the same time.",
                "Verifications that use the same drive run one at a time; the others run at the same time."),
            DriveTurns.AT_THE_SAME_TIME: (
                "They run at the same time; jobs that use the same drive share its speed.",
                "They run at the same time; verifications that use the same drive share its speed.")})
        self.assertEqual(run_several(rows).text, run_several(rows, turns=DriveTurns.AUTOMATIC).text)

    def test_automatic_names_the_cloud_drive_beside_the_solid_state_drive(self):
        self.assertEqual(AUTOMATIC, "Automatic: one at a time on a hard disk, at the same time on a solid-state or "
                                    "cloud drive")
        rows = [job_row("a" * 32), job_row("b" * 32)]
        jobs = {r.job_id: r for r in rows}
        self.assertEqual((run_several(rows).text.splitlines()[-1],
                          verify_several([r.destinations[0] for r in rows], jobs).text.splitlines()[-2]),
                         ("Jobs that use the same drive run one at a time unless it is a solid-state or cloud drive, "
                          "as Windows reports or as set in Preferences; the others run at the same time.",
                          "Verifications that use the same drive run one at a time unless it is a solid-state or "
                          "cloud drive, as Windows reports or as set in Preferences; the others run at the same time."))


if __name__ == "__main__":
    unittest.main()
