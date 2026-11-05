"""Creation-time validation: every refused, warned and noticed case, by its code.

Binds the designed creation rules (refused: source and destination equal or nested; two
jobs sharing a destination; one destination nested in another; a destination inside
a cloud sync root or on a cloud drive; a path inside a DrivePool member's pool
folder; a FAT32 destination for a source holding a 4 GiB file — warned: FAT32
otherwise, a source larger than free space — noticed: a destination inside another
job's source) and the three refusals derived from the rule that nothing inside a
source is changed and that pool members are written only through the pool."""
import unittest

from support import GIB, TempDirTest, endpoint, guid, volume

from mirrorhold.endpoints.cloud import SyncKind, SyncRoot
from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.system import SystemFolder
from mirrorhold.endpoints.volumes import Backing
from mirrorhold.engine.remote import RemoteFeatures
from mirrorhold.jobs.model import new_job
from mirrorhold.jobs.survey import SourceSurvey
from mirrorhold.jobs.validation import (DESTINATION_IN_CLOUD_COPY, DESTINATION_IS_SYSTEM_FOLDER,
                                        DESTINATION_NOT_A_FOLDER, DESTINATION_NOT_ENCRYPTED, PREVIEW_NOTICES,
                                        REMOTE_BELOW_FLOOR, REMOTE_CONFIGURATION_MISSING,
                                        REMOTE_CONFIGURATION_UNUSABLE, REMOTE_UNKNOWN, REMOTE_UNREACHABLE,
                                        SOURCE_NOT_A_FOLDER, SYNC_ROOTS_UNREADABLE, Severity, ValidationContext,
                                        contained_folders, preview_notices, relative_inside, validate_job)
from mirrorhold.remotes.facts import Reach, RemoteFacts, RemoteSetup, SetupState

D = volume(1, "D", free=900 * GIB)
E = volume(2, "E", free=300 * GIB)
F = volume(3, "F", fs="FAT32", free=900 * GIB)
G = volume(4, "G", fs="FAT32", backing=Backing.VIRTUAL, label="Google Drive")
MEMBER = volume(5, None, pool_member=True, label="Pool member 2")
VOLUMES = (D, E, F, G, MEMBER)
POOL = f"PoolPart.{guid(77)}"
SMALL = SourceSurvey(files=10, total_bytes=10 * GIB, largest_file_bytes=1 * GIB)


def make(src, *dests, name="job"):
    return new_job(name, src, list(dests))


def check(job, *, others=(), roots=(), survey=SMALL, volumes=VOLUMES):
    return validate_job(job, ValidationContext(tuple(volumes), tuple(roots), tuple(others)), survey)


class RelationTest(unittest.TestCase):
    def test_a_clean_job_has_no_findings(self):
        report = check(make(endpoint(D, "Work"), endpoint(E, "Backup\\Work")))
        self.assertEqual(report.findings, ())
        self.assertFalse(report.refused)

    def test_source_and_destination_equal_or_nested(self):
        for dest in (endpoint(D, "Work"), endpoint(D, "Work\\Backup"), endpoint(D, ""), endpoint(D, "work")):
            with self.subTest(dest=dest.path):
                report = check(make(endpoint(D, "Work"), dest))
                self.assertIn("source-destination-overlap", report.codes(Severity.REFUSED))

    def test_two_destinations_of_one_job_equal_or_nested(self):
        report = check(make(endpoint(D, "Work"), endpoint(E, "B"), endpoint(E, "B\\inner")))
        self.assertIn("destinations-overlap", report.codes(Severity.REFUSED))

    def test_two_jobs_sharing_a_destination(self):
        first = make(endpoint(D, "Work"), endpoint(E, "Backup"), name="first")
        report = check(make(endpoint(D, "Photos"), endpoint(E, "BACKUP")), others=[first])
        self.assertIn("destination-shared", report.codes(Severity.REFUSED))

    def test_a_destination_nested_in_another_jobs_destination(self):
        first = make(endpoint(D, "Work"), endpoint(E, "Backup"), name="first")
        for dest in (endpoint(E, "Backup\\Photos"), endpoint(E, "")):
            with self.subTest(dest=dest.path):
                report = check(make(endpoint(D, "Photos"), dest), others=[first])
                self.assertIn("destination-nested-with-other", report.codes(Severity.REFUSED))

    def test_a_destination_inside_another_jobs_source_is_allowed_with_a_notice(self):
        outer = make(endpoint(D, ""), endpoint(E, "D-drive"), name="outer")
        inner = make(endpoint(E, "Photos"), endpoint(D, "Mirrors\\Photos"), name="inner")
        report = check(inner, others=[outer])
        self.assertFalse(report.refused)
        self.assertIn("destination-inside-other-source", report.codes(Severity.NOTICE))
        seen_from_outer = check(outer, others=[inner])
        self.assertFalse(seen_from_outer.refused)
        self.assertIn("source-contains-other-destination", seen_from_outer.codes(Severity.NOTICE))

    def test_a_destination_containing_another_jobs_source_is_noticed_both_ways(self):
        first = make(endpoint(E, "Data"), endpoint(D, "Backup\\Data"), name="first")
        report = check(make(endpoint(D, "Work"), endpoint(E, "")), others=[first])
        self.assertFalse(report.refused)
        self.assertIn("destination-contains-other-source", report.codes(Severity.NOTICE))
        writer = make(endpoint(D, "Work"), endpoint(E, "Mirror"), name="writer")
        reader = make(endpoint(E, "Mirror\\Sub"), endpoint(D, "Second"), name="reader")
        seen_from_reader, seen_from_writer = check(reader, others=[writer]), check(writer, others=[reader])
        self.assertFalse(seen_from_reader.refused or seen_from_writer.refused)
        self.assertIn("source-inside-other-destination", seen_from_reader.codes(Severity.NOTICE))
        self.assertIn("destination-contains-other-source", seen_from_writer.codes(Severity.NOTICE))

    def test_a_job_is_not_compared_with_itself(self):
        job = make(endpoint(D, "Work"), endpoint(E, "Backup"))
        self.assertEqual(check(job, others=[job]).findings, ())


class VolumeRuleTest(unittest.TestCase):
    def test_a_path_inside_a_pool_folder(self):
        report = check(make(endpoint(MEMBER, POOL + "\\Photos"), endpoint(E, "x")))
        self.assertIn("endpoint-in-pool-folder", report.codes(Severity.REFUSED))
        report = check(make(endpoint(D, "x"), endpoint(MEMBER, POOL.lower())))
        self.assertIn("endpoint-in-pool-folder", report.codes(Severity.REFUSED))

    def test_any_destination_on_a_pool_member(self):
        report = check(make(endpoint(D, "x"), endpoint(MEMBER, "Elsewhere")))
        self.assertIn("destination-on-pool-member", report.codes(Severity.REFUSED))
        self.assertFalse(check(make(endpoint(MEMBER, "Elsewhere"), endpoint(E, "x"))).refused,
                         "reading a member's other folders is not writing to it")

    def test_a_destination_on_a_cloud_drive(self):
        report = check(make(endpoint(D, "x"), endpoint(G, "Backup")))
        self.assertIn("destination-on-virtual-drive", report.codes(Severity.REFUSED))

    def test_sync_roots(self):
        roots = [SyncRoot("OneDrive", "D:\\OneDrive - Org")]
        inside = check(make(endpoint(E, "x"), endpoint(D, "OneDrive - Org\\Backup")), roots=roots)
        self.assertIn("destination-in-sync-root", inside.codes(Severity.REFUSED))
        containing = check(make(endpoint(E, "x"), endpoint(D, "")), roots=roots)
        self.assertFalse(containing.refused, "a sync root strictly inside a destination is left where it is")
        self.assertIn("destination-contains-sync-root", containing.codes(Severity.NOTICE))
        source_in_root = check(make(endpoint(D, "OneDrive - Org\\Docs"), endpoint(E, "Docs")), roots=roots)
        self.assertFalse(source_in_root.refused, "a source inside a sync root is in scope")

    def test_a_folder_kept_on_every_computer_is_refused_and_one_copied_to_the_cloud_noticed(self):
        """The maintainer's decision of 2026-10-28: a folder of this computer a cloud client
        keeps a copy of in its cloud may hold a destination, which is noticed; one kept on
        every computer of the account may not."""
        mirrored = [SyncRoot("Google Drive", "D:\\My Drive", SyncKind.DEVICES)]
        copied = [SyncRoot("Google Drive", "D:\\Backups", SyncKind.CLOUD_COPY)]
        self.assertIn("destination-in-sync-root",
                      check(make(endpoint(E, "x"), endpoint(D, "My Drive\\Work")), roots=mirrored)
                      .codes(Severity.REFUSED))
        for dest in (endpoint(D, "Backups"), endpoint(D, "backups\\Work")):
            with self.subTest(dest=dest.path):
                report = check(make(endpoint(E, "x"), dest), roots=copied)
                self.assertFalse(report.refused)
                self.assertEqual(report.codes(Severity.NOTICE), (DESTINATION_IN_CLOUD_COPY,))
                self.assertIn("D:\\Backups, of which Google Drive keeps a copy in its cloud", report.findings[0].message)
                self.assertIn("may put back what a run moves into versions", report.findings[0].message)
        containing = check(make(endpoint(E, "x"), endpoint(D, "")), roots=copied)
        self.assertEqual(containing.codes(), ("destination-contains-sync-root",),
                         "a copied folder strictly inside a destination is protected like any synchronised folder")
        source_in_root = check(make(endpoint(D, "Backups\\Docs"), endpoint(E, "Docs")), roots=copied)
        self.assertEqual(source_in_root.findings, ())

    def test_a_folder_found_by_its_volume_is_named_where_it_lies_now(self):
        moved = [SyncRoot("Google Drive", "X:\\Backups", SyncKind.CLOUD_COPY, f"\\\\?\\Volume{{{guid(2)}}}\\Backups")]
        report = check(make(endpoint(D, "x"), endpoint(E, "Backups\\Work")), roots=moved)
        self.assertIn("this folder lies inside E:\\Backups, of which Google Drive keeps a copy", report.findings[0].message)
        outer = make(endpoint(D, "x"), endpoint(E, ""))
        (inside,) = contained_folders(outer, 1, ValidationContext(VOLUMES, tuple(moved)))
        self.assertEqual(inside.words, "E:\\Backups, which Google Drive synchronises")

    def test_a_list_of_folders_that_could_not_be_read_is_noticed_at_each_present_destination(self):
        roots = [SyncRoot("OneDrive", "D:\\OneDrive - Org"),
                 SyncRoot("Google Drive", "", problem="its list of folders could not be read (locked)")]
        absent = volume(9, "Y")
        report = check(make(endpoint(D, "x"), endpoint(E, "Backup"), endpoint(absent, "Backup")), roots=roots)
        self.assertFalse(report.refused)
        unreadable = [f for f in report.findings if f.code == SYNC_ROOTS_UNREADABLE]
        self.assertEqual([(f.severity, f.subject) for f in unreadable], [(Severity.NOTICE, "destination 1")])
        self.assertEqual(unreadable[0].message,
                         "whether Google Drive synchronises this folder cannot be told: its list of folders could not "
                         "be read (locked)")

    def test_a_preview_shows_its_destinations_cloud_notices_alone(self):
        roots = [SyncRoot("Google Drive", "E:\\Backups", SyncKind.CLOUD_COPY),
                 SyncRoot("Baidu", "", problem="unreadable")]
        report = check(make(endpoint(D, "x"), endpoint(F, "Card"), endpoint(E, "Backups\\Work")), roots=roots)
        self.assertIn("fat32-timestamps", report.codes(Severity.WARNING))
        self.assertEqual(preview_notices(report, 1), ("whether Baidu synchronises this folder cannot be told: "
                                                      "unreadable",))
        self.assertEqual(len(preview_notices(report, 2)), 2)
        self.assertIn("of which Google Drive keeps a copy in its cloud", preview_notices(report, 2)[0])
        self.assertEqual(preview_notices(report, 3), ())

    def test_fat32(self):
        big = SourceSurvey(files=3, total_bytes=12 * GIB, largest_file_bytes=4 * GIB)
        just_under = SourceSurvey(files=3, total_bytes=12 * GIB, largest_file_bytes=4 * GIB - 1)
        job = make(endpoint(D, "Video"), endpoint(F, "Video"))
        self.assertIn("fat32-large-file", check(job, survey=big).codes(Severity.REFUSED))
        under = check(job, survey=just_under)
        self.assertFalse(under.refused)
        self.assertIn("fat32-timestamps", under.codes(Severity.WARNING))
        unsurveyed = check(job, survey=None)
        self.assertFalse(unsurveyed.refused)
        self.assertIn("fat32-timestamps", unsurveyed.codes(Severity.WARNING))
        self.assertIn("size-checks-deferred", unsurveyed.codes(Severity.NOTICE))

    def test_source_larger_than_free_space(self):
        job = make(endpoint(D, "Video"), endpoint(E, "Video"))
        report = check(job, survey=SourceSurvey(files=1, total_bytes=301 * GIB, largest_file_bytes=GIB))
        self.assertEqual(report.codes(), ("source-exceeds-free-space",))
        self.assertFalse(report.refused)

    def test_the_warning_gives_the_source_and_the_free_space_in_bytes(self):
        job = make(endpoint(D, "Video"), endpoint(E, "Video"))
        report = check(job, survey=SourceSurvey(files=1, total_bytes=301 * GIB, largest_file_bytes=GIB))
        self.assertEqual([f.message for f in report.findings],
                         [f"the source holds {301 * GIB} bytes and this volume has {300 * GIB} bytes free; a first "
                          "run will not fit unless the destination already holds most of it"])

    def test_absent_and_ambiguous_volumes(self):
        job = make(endpoint(D, "x"), endpoint(F, "y"))
        report = check(job, volumes=(D,))
        self.assertEqual(report.codes(), ("volume-absent",))
        report = check(job, volumes=(F,), survey=None)
        self.assertIn("volume-absent", report.codes(Severity.NOTICE))
        clone = volume(1, "K")
        report = check(job, volumes=(D, clone, F))
        self.assertIn("volume-ambiguous", report.codes(Severity.REFUSED))

    def test_an_incomplete_survey_is_noticed(self):
        report = check(make(endpoint(D, "x"), endpoint(E, "y")),
                       survey=SourceSurvey(files=1, total_bytes=1, largest_file_bytes=1, unreadable_directories=2))
        self.assertEqual(report.codes(), ("survey-incomplete",))

    def test_an_incomplete_survey_counts_its_folders_in_the_number_they_need(self):
        job = make(endpoint(D, "x"), endpoint(E, "y"))
        for unlisted, words in ((1, "1 source folder could not be listed; the size figures leave it out"),
                                (2, "2 source folders could not be listed; the size figures leave them out")):
            with self.subTest(unlisted=unlisted):
                survey = SourceSurvey(files=1, total_bytes=1, largest_file_bytes=1, unreadable_directories=unlisted)
                (finding,) = check(job, survey=survey).findings
                self.assertEqual((finding.code, finding.message), ("survey-incomplete", words))


class ContainedFolderTest(unittest.TestCase):
    """A synchronised folder or another job's source strictly inside a destination is
    allowed: its runs leave it where it is and never write into it (the run enforces it,
    since how the destination's first run treated what it held is recorded on the
    destination). The same folder as the destination stays refused."""

    def test_another_jobs_source_strictly_inside_is_noticed_and_the_same_folder_refused(self):
        other = make(endpoint(E, "Data\\Work"), endpoint(D, "Backup\\Data"), name="other")
        outer = make(endpoint(D, "Work"), endpoint(E, ""))
        report = check(outer, others=[other])
        self.assertFalse(report.refused)
        self.assertIn("destination-contains-other-source", report.codes(Severity.NOTICE))
        self.assertIn("source-inside-other-destination", check(other, others=[outer]).codes(Severity.NOTICE))
        same = make(endpoint(D, "Work"), endpoint(E, "Data\\Work"))
        self.assertIn("destination-contains-other-source", check(same, others=[other]).codes(Severity.REFUSED))
        self.assertIn("source-inside-other-destination", check(other, others=[same]).codes(Severity.REFUSED))

    def test_a_synchronised_folder_strictly_inside_is_noticed(self):
        roots = [SyncRoot("OneDrive", "D:\\Users\\me\\OneDrive")]
        report = check(make(endpoint(E, "x"), endpoint(D, "")), roots=roots)
        self.assertFalse(report.refused)
        self.assertIn("destination-contains-sync-root", report.codes(Severity.NOTICE))
        self.assertIn("destination-in-sync-root",
                      check(make(endpoint(E, "x"), endpoint(D, "Users\\me\\OneDrive")), roots=roots)
                      .codes(Severity.REFUSED))

    def test_what_lies_inside_a_destination_by_its_path_there(self):
        other = make(endpoint(D, "Data\\Work"), endpoint(E, "Copy"), name="Work")
        outer = make(endpoint(E, "Mine"), endpoint(D, "Data"))
        context = ValidationContext(VOLUMES, (SyncRoot("OneDrive", "D:\\Data\\Cloud\\OneDrive"),), (other,))
        found = contained_folders(outer, 1, context)
        self.assertEqual([(c.code, c.relative, c.top) for c in found],
                         [("destination-contains-sync-root", "Cloud\\OneDrive", "cloud"),
                          ("destination-contains-other-source", "Work", "work")])
        self.assertEqual(found[0].words, "D:\\Data\\Cloud\\OneDrive, which OneDrive synchronises")
        self.assertEqual(found[1].words, "D:\\Data\\Work, the source of job 'Work'")
        self.assertEqual(relative_inside(endpoint(D, "Data"), endpoint(D, "data")), "")
        self.assertIsNone(relative_inside(endpoint(D, "Data"), endpoint(E, "Data\\x")))


class ThisComputerTest(TempDirTest):
    """The refusals that need what validation sees of this computer: a source that is a
    file and a destination that is itself a folder Windows or this tool needs (the
    maintainer's decisions of 2026-10-17), and a destination that is a file (the
    maintainer's decision of 2026-10-19)."""

    def setUp(self):
        super().setUp()
        self.vol = volume(1, mounts=(str(self.tmp) + "\\",), label="T")
        self.other = volume(2, "E", free=300 * GIB)
        self.store = self.tmp / "Config" / "mirrorhold.json"

    def context(self):
        return ValidationContext.on_this_computer((self.vol, self.other), (), (), self.store)

    def test_a_source_that_is_a_file_is_refused(self):
        (self.tmp / "notes.txt").write_text("a file", encoding="utf-8")
        (self.tmp / "Work").mkdir()
        job = make(endpoint(self.vol, "notes.txt"), endpoint(self.other, "Backup"))
        report = validate_job(job, self.context(), None)
        self.assertEqual([(f.code, f.subject, f.message) for f in report.findings if f.severity is Severity.REFUSED],
                         [(SOURCE_NOT_A_FOLDER, "source",
                           "this is a file, not a folder; a job backs up a folder or a whole drive")])
        folder = make(endpoint(self.vol, "Work"), endpoint(self.other, "Backup"))
        self.assertFalse(validate_job(folder, self.context(), None).refused)
        missing = make(endpoint(self.vol, "Gone"), endpoint(self.other, "Backup"))
        self.assertFalse(validate_job(missing, self.context(), None).refused, "a run says a missing source is missing")
        self.assertFalse(validate_job(job, ValidationContext((self.vol, self.other)), None).refused,
                         "a context built from its fields alone finds no file")

    def test_a_destination_that_is_a_file_is_refused(self):
        (self.tmp / "Work").mkdir()
        (self.tmp / "Copy").mkdir()
        (self.tmp / "Backup").write_text("a file", encoding="utf-8")
        job = make(endpoint(self.vol, "Work"), endpoint(self.vol, "Copy"), endpoint(self.vol, "Backup"))
        report = validate_job(job, self.context(), None)
        self.assertEqual([(f.code, f.subject, f.message) for f in report.findings if f.severity is Severity.REFUSED],
                         [("destination-not-a-folder", "destination 2",
                           "this is a file, not a folder; a backup is written into a folder or onto a whole drive")])
        self.assertEqual(next(f for f in report.findings if f.code == "destination-not-a-folder").describe(),
                         "REFUSED [destination-not-a-folder] destination 2: this is a file, not a folder; a backup is "
                         "written into a folder or onto a whole drive")
        missing = make(endpoint(self.vol, "Work"), endpoint(self.vol, "Gone"))
        self.assertFalse(validate_job(missing, self.context(), None).refused, "a run makes a missing destination")
        self.assertFalse(validate_job(job, ValidationContext((self.vol, self.other)), None).refused,
                         "a context built from its fields alone finds no file")

    def test_a_destination_that_is_itself_a_folder_windows_or_this_tool_needs_is_refused(self):
        profile = self.tmp / "Users" / "me"
        context = ValidationContext((self.vol, self.other), system_folders=(
            SystemFolder(str(profile), f"{profile}, a user profile"),))
        job = make(endpoint(self.other, "Work"), endpoint(self.vol, "users\\ME"))
        report = validate_job(job, context, None)
        self.assertEqual([(f.code, f.subject, f.message) for f in report.findings if f.severity is Severity.REFUSED],
                         [(DESTINATION_IS_SYSTEM_FOLDER, "destination 1",
                           f"this folder is {profile}, a user profile: backing up here would write into it. "
                           "Choose a folder of its own instead")])
        inside = make(endpoint(self.other, "Work"), endpoint(self.vol, "Users\\me\\Backup"))
        self.assertFalse(validate_job(inside, context, None).refused, "a folder of its own inside one is allowed")
        tool = make(endpoint(self.other, "Work"), endpoint(self.vol, "Config"))
        self.assertIn(DESTINATION_IS_SYSTEM_FOLDER, validate_job(tool, self.context(), None).codes(Severity.REFUSED),
                      "the folder of the job file in use is one this tool needs")


def features(*, moves=True, copies=False, precision_ns=10 ** 9, encrypted=False):
    return RemoteFeatures(moves=moves, copies=copies, moves_folders=moves, precision_ns=precision_ns, hashes=(),
                          reports_space=True, keeps_empty_folders=True, case_insensitive=False, duplicate_names=False,
                          partial_uploads=False, deletes_folders=True, encrypted=encrypted)


CLOUD = RemoteEndpoint("cloud", "Backups/Work")


def ready(facts=None, remotes=("cloud",)):
    return RemoteSetup(SetupState.READY, remotes=remotes,
                       facts={CLOUD: facts or RemoteFacts(Reach.REACHED, features=features(),
                                                          space=(50 * GIB, 90 * GIB))})


class RemoteDestinationTest(unittest.TestCase):
    """A remote destination judged from what the engine said of it, never by starting it."""

    def report(self, setup, *, survey=SMALL, others=()):
        job = make(endpoint(D, "Work"), CLOUD)
        return validate_job(job, ValidationContext(VOLUMES, (), tuple(others), remote_setup=setup), survey)

    def test_a_remote_that_meets_the_floor_and_does_not_encrypt_is_noticed_in_every_preview(self):
        report = self.report(ready())
        self.assertFalse(report.refused)
        self.assertEqual(report.codes(), (DESTINATION_NOT_ENCRYPTED,))
        self.assertIn(DESTINATION_NOT_ENCRYPTED, PREVIEW_NOTICES)
        self.assertEqual(preview_notices(report, 1), (
            "this remote does not encrypt: the service, and anyone signed in to its account, can read every file and "
            "its name, and the job's definition kept with the backup, which names the source folder's path on this "
            "computer",))
        encrypted = self.report(ready(RemoteFacts(Reach.REACHED, features=features(encrypted=True))))
        self.assertEqual(encrypted.findings, ())

    def test_the_floor_needs_times_of_two_seconds_or_finer_and_a_move_or_copy_within(self):
        cases = {
            "it keeps no modification times": features(precision_ns=100 * 365 * 24 * 3600 * 10 ** 9),
            "it keeps modification times only to 3 seconds": features(precision_ns=3 * 10 ** 9),
            "it can neither move nor copy a file within itself": features(moves=False, copies=False),
        }
        for words, found in cases.items():
            with self.subTest(words=words):
                report = self.report(ready(RemoteFacts(Reach.REACHED, features=found)))
                (refusal,) = [f for f in report.findings if f.severity is Severity.REFUSED]
                self.assertEqual(refusal.code, REMOTE_BELOW_FLOOR)
                self.assertIn(f"remote 'cloud' cannot hold a backup: {words}", refusal.message)
        for fine in (features(precision_ns=2 * 10 ** 9), features(moves=False, copies=True)):
            self.assertFalse(self.report(ready(RemoteFacts(Reach.REACHED, features=fine))).refused)

    def test_the_configuration_missing_unusable_or_without_the_remote_refuses(self):
        self.assertEqual(self.report(RemoteSetup(SetupState.MISSING, "none named")).codes(Severity.REFUSED),
                         (REMOTE_CONFIGURATION_MISSING,))
        locked = self.report(RemoteSetup(SetupState.UNUSABLE, "cannot be opened: locked"))
        self.assertEqual([(f.code, f.message) for f in locked.findings],
                         [(REMOTE_CONFIGURATION_UNUSABLE, "cannot be opened: locked")])
        self.assertEqual(self.report(ready(remotes=("other",))).codes(Severity.REFUSED), (REMOTE_UNKNOWN,))
        refused = self.report(ready(RemoteFacts(Reach.REFUSED, "401 Unauthorized")))
        self.assertEqual([(f.code, f.message) for f in refused.findings],
                         [(REMOTE_CONFIGURATION_UNUSABLE, "the service of remote 'cloud' refused: 401 Unauthorized")])

    def test_a_remote_not_reached_or_not_asked_waits_with_a_notice(self):
        unreached = self.report(ready(RemoteFacts(Reach.UNREACHABLE, "dial tcp: connection refused")))
        self.assertEqual([(f.severity, f.code) for f in unreached.findings], [(Severity.NOTICE, REMOTE_UNREACHABLE)])
        self.assertIn("cannot be reached: dial tcp: connection refused", unreached.findings[0].message)
        not_asked = self.report(None)
        self.assertEqual(not_asked.codes(), (REMOTE_UNREACHABLE,))
        self.assertFalse(not_asked.refused)

    def test_a_file_where_the_folder_is_named_is_refused_and_free_space_is_judged_where_reported(self):
        report = self.report(ready(RemoteFacts(Reach.REACHED, is_file=True)))
        self.assertEqual(report.codes(), (DESTINATION_NOT_A_FOLDER,))
        large = SourceSurvey(files=1, total_bytes=60 * GIB, largest_file_bytes=6 * GIB)
        self.assertIn("source-exceeds-free-space", self.report(ready(), survey=large).codes(Severity.WARNING))
        unknown = ready(RemoteFacts(Reach.REACHED, features=features()))
        self.assertNotIn("source-exceeds-free-space", self.report(unknown, survey=large).codes())

    def test_remote_destinations_overlap_by_remote_and_folder_never_with_a_drives(self):
        other = make(endpoint(E, "Elsewhere"), RemoteEndpoint("Cloud", "backups"), name="other")
        self.assertIn("destination-nested-with-other", self.report(ready(), others=[other]).codes(Severity.REFUSED))
        same = make(endpoint(E, "Elsewhere"), RemoteEndpoint("cloud", "Backups/Work/"), name="same")
        self.assertIn("destination-shared", self.report(ready(), others=[same]).codes(Severity.REFUSED))
        apart = make(endpoint(E, "Elsewhere"), RemoteEndpoint("cloud2", "Backups/Work"), endpoint(E, "Backups"),
                     name="apart")
        self.assertFalse(self.report(ready(), others=[apart]).refused)
        twice = make(endpoint(D, "Work"), CLOUD, RemoteEndpoint("cloud", "Backups"))
        report = validate_job(twice, ValidationContext(VOLUMES, remote_setup=ready()), SMALL)
        self.assertIn("destinations-overlap", report.codes(Severity.REFUSED))


if __name__ == "__main__":
    unittest.main()
