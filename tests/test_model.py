"""The job contract: vocabulary, defaults, strict documents, located errors.

Binds the job contract — one source, one or more destinations, an enabled
switch, triggers, hold thresholds (count floor 1000, 5 per cent), version cleanup
(off, or keep N days), a verification reminder (90 days), snapshot reading and
exclusions on the source; thresholds and cleanup per job — and a group of jobs: a name
and its members' identifiers, in order; and the choices for activities among the
preferences."""
import copy
import json
import unittest

from support import identity

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.endpoint import Endpoint, RemoteEndpoint
from mirrorhold.jobs.model import (Activities, DefaultExclusions, Destination, DestinationState, DriveTurns, Group,
                                   HoldThresholds, Job, Notifications, Preferences, RemoteSettings, Source, Triggers,
                                   Unlock, VersionCleanup, group_name_key, new_group, new_job)


def sample_job(**settings) -> Job:
    return new_job("Photos", Endpoint(identity(1), "Photos"),
                   [Endpoint(identity(2), "Backup\\Photos"), Endpoint(identity(3), "")], **settings)


class DefaultsTest(unittest.TestCase):
    def test_a_new_job_carries_the_contract_defaults(self):
        job = sample_job()
        self.assertTrue(job.enabled)
        self.assertEqual((job.holds.count_floor, job.holds.percent), (1000, 5))
        self.assertFalse(job.version_cleanup.enabled)
        self.assertEqual(job.verification_reminder_days, 90)
        self.assertEqual(job.triggers, Triggers(on_arrival=False, schedule=None))
        self.assertTrue(job.source.read_from_snapshot)
        self.assertEqual(job.source.default_exclusions, DefaultExclusions(True, True))
        self.assertEqual(job.source.exclusions, ())
        self.assertEqual(len(job.destinations), 2)
        self.assertEqual(len({job.job_id, *(d.marker_id for d in job.destinations)}), 3)

    def test_settings_are_passed_through(self):
        job = sample_job(enabled=False, holds=HoldThresholds(50, 2.5), version_cleanup=VersionCleanup(30),
                         verification_reminder_days=30, triggers=Triggers(True, "daily 03:00"),
                         read_from_snapshot=False, exclusions=("*.tmp", "Cache\\**"),
                         default_exclusions=DefaultExclusions(False, True))
        self.assertFalse(job.enabled)
        self.assertEqual(job.holds.percent, 2.5)
        self.assertEqual(job.version_cleanup.keep_days, 30)
        self.assertEqual(job.triggers.schedule, "daily 03:00")
        self.assertFalse(job.source.read_from_snapshot)
        self.assertEqual(job.source.exclusions, ("*.tmp", "Cache\\**"))
        self.assertFalse(job.source.default_exclusions.sync_client_artefacts)

    def test_preferences_defaults(self):
        prefs = Preferences()
        self.assertIsNone(prefs.engine_path)
        self.assertEqual(prefs.notifications, Notifications(True, True, True))
        self.assertEqual(dict(prefs.gui_state), {})


class DestinationNumberTest(unittest.TestCase):
    """A destination named by its number or by its marker: the number it has in the job."""

    def test_a_destination_is_found_by_its_number_or_its_marker(self):
        job = sample_job()
        first, second = (d.marker_id for d in job.destinations)
        self.assertEqual((job.number_of(second), job.number_of("f" * 32)), (2, None))
        self.assertEqual((job.destination_number(2), job.destination_number(marker=first)), (2, 1))

    def test_a_destination_the_job_does_not_have_or_one_named_both_ways_or_neither_is_refused(self):
        job = sample_job()
        for index, marker, why in ((3, None, "the job has no destination 3"),
                                   (0, None, "the job has no destination 0"),
                                   (None, "f" * 32, "the job has no destination with marker " + "f" * 32),
                                   (1, job.destinations[0].marker_id, "by its number or by its marker"),
                                   (None, None, "by its number or by its marker")):
            with self.assertRaisesRegex(ValueError, why):
                job.destination_number(index, marker)


class ShareHoldTest(unittest.TestCase):
    """The share hold, a value of each job: half of the destination's files by default, a
    percentage above 0 and at most 100, or switched off (None)."""

    def test_half_by_default_adjustable_and_switched_off(self):
        self.assertEqual(sample_job().holds.share, 50)
        self.assertEqual((HoldThresholds(share=30).share, HoldThresholds(share=12.5).share), (30, 12.5))
        self.assertIsNone(HoldThresholds(share=None).share)

    def test_it_round_trips_through_the_job_document(self):
        for share in (50, 30, 12.5, 100, None):
            job = sample_job(holds=HoldThresholds(share=share))
            doc = json.loads(json.dumps(job.to_document()))
            self.assertEqual(doc["holds"]["share"], share)
            self.assertEqual(Job.from_document(doc), job)

    def test_values_outside_the_contract_are_errors_at_their_field(self):
        for bad in (0, -5, 100.5, True, "50"):
            doc = sample_job().to_document()
            doc["holds"]["share"] = bad
            with self.assertRaises(ContractError) as ctx:
                Job.from_document(doc)
            self.assertEqual(ctx.exception.where, "holds.share", bad)
        missing = sample_job().to_document()
        del missing["holds"]["share"]
        with self.assertRaises(ContractError) as ctx:
            Job.from_document(missing)
        self.assertEqual(ctx.exception.where, "holds")
        self.assertIn("missing share", str(ctx.exception))


class DocumentTest(unittest.TestCase):
    def test_round_trip_through_json(self):
        job = sample_job(holds=HoldThresholds(10, 50), exclusions=("*.bak",))
        text = json.dumps(job.to_document())
        self.assertEqual(Job.from_document(json.loads(text)), job)
        prefs = Preferences("D:\\tools\\rclone.exe", Notifications(False, True, True),
                            {"geometry": "800x600+10+10", "last_view": ["jobs", 2]})
        self.assertEqual(Preferences.from_document(json.loads(json.dumps(prefs.to_document()))), prefs)
        state = DestinationState(True, "2026-10-05T10:00:00Z", None)
        self.assertEqual(DestinationState.from_document(state.to_document()), state)

    def test_missing_and_unknown_keys_are_errors(self):
        doc = sample_job().to_document()
        missing = copy.deepcopy(doc)
        del missing["holds"]
        with self.assertRaises(ContractError) as ctx:
            Job.from_document(missing)
        self.assertIn("missing holds", str(ctx.exception))
        unknown = copy.deepcopy(doc)
        unknown["source"]["exclusion"] = []
        with self.assertRaises(ContractError) as ctx:
            Job.from_document(unknown)
        self.assertEqual(ctx.exception.where, "source")
        self.assertIn("unknown exclusion", str(ctx.exception))

    def test_errors_are_located_at_their_field(self):
        cases = [
            (("holds", "percent"), 0, "holds.percent"),
            (("holds", "count_floor"), -1, "holds.count_floor"),
            (("holds", "count_floor"), True, "holds.count_floor"),
            (("version_cleanup", "keep_days"), 0, "version_cleanup.keep_days"),
            (("verification_reminder_days",), 0, "verification_reminder_days"),
            (("enabled",), "yes", "enabled"),
            (("triggers", "schedule"), "  ", "triggers.schedule"),
            (("source", "endpoint", "path"), "a\\..\\b", "source.endpoint.path"),
            (("source", "endpoint", "volume", "anchor"), "C:", "source.endpoint.volume.anchor"),
            (("source", "exclusions"), ["ok", ""], "source.exclusions[1]"),
            (("destinations",), [], "destinations"),
            (("id",), "not-an-id", "id"),
            (("name",), "   ", "name"),
        ]
        for keys, value, where in cases:
            with self.subTest(where=where):
                doc = sample_job().to_document()
                target = doc
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(ContractError) as ctx:
                    Job.from_document(doc)
                self.assertEqual(ctx.exception.where, where)

    def test_second_destination_error_is_indexed(self):
        doc = sample_job().to_document()
        doc["destinations"][1]["marker_id"] = "x"
        with self.assertRaises(ContractError) as ctx:
            Job.from_document(doc)
        self.assertEqual(ctx.exception.where, "destinations[1].marker_id")

    def test_destinations_may_not_share_a_marker(self):
        d = Destination(Endpoint(identity(2), "a"))
        with self.assertRaises(ContractError):
            Job(job_id="a" * 32, name="x", source=Source(Endpoint(identity(1))), destinations=(d, d))

    def test_gui_state_must_be_plain(self):
        with self.assertRaises(ContractError) as ctx:
            Preferences(gui_state={"bad": object()})
        self.assertEqual(ctx.exception.where, "gui_state.bad")


class GroupTest(unittest.TestCase):
    """A group: a name and the identifiers of its jobs, in the order the user gives them."""

    def test_a_new_group_round_trips_and_keeps_its_order(self):
        group = new_group("  Pictures ", ("b" * 32, "a" * 32))
        self.assertEqual((group.name, group.job_ids), ("Pictures", ("b" * 32, "a" * 32)))
        self.assertEqual(Group.from_document(json.loads(json.dumps(group.to_document()))), group)
        self.assertEqual(set(group.to_document()), {"id", "name", "jobs"})
        self.assertEqual(group_name_key(" PICTURES "), group_name_key("pictures"))

    def test_errors_are_located_at_their_field(self):
        good = new_group("Pictures", ("a" * 32,)).to_document()
        cases = [("id", "x", "id"), ("name", "  ", "name"), ("name", "n" * 201, "name"),
                 ("jobs", ["a" * 32, "a" * 32], "jobs"), ("jobs", ["nope"], "jobs[0]"), ("jobs", "a" * 32, "jobs")]
        for key, value, where in cases:
            with self.subTest(where=where, value=value):
                doc = dict(good, **{key: value})
                with self.assertRaises(ContractError) as ctx:
                    Group.from_document(doc)
                self.assertEqual(ctx.exception.where, where)
        with self.assertRaises(ContractError) as ctx:
            Group.from_document(dict(good, order=1))
        self.assertIn("unknown order", str(ctx.exception))


class ActivitiesTest(unittest.TestCase):
    """The choices for runs and verifications as they work: turns on a drive decided by the
    drive's kind, the computer kept awake, the activity named at shutdown — each so at first."""

    def test_on_at_first_and_kept_through_the_document(self):
        self.assertEqual(Preferences().activities, Activities(DriveTurns.AUTOMATIC, True, True))
        prefs = Preferences(activities=Activities(drive_turns=DriveTurns.AT_THE_SAME_TIME, keep_awake=True,
                                                  name_at_shutdown=False))
        doc = json.loads(json.dumps(prefs.to_document()))
        self.assertEqual(doc["activities"],
                         {"drive_turns": "at_the_same_time", "keep_awake": True, "name_at_shutdown": False,
                          "drive_kinds": []})
        self.assertEqual(Preferences.from_document(doc), prefs)

    def test_a_choice_that_is_not_true_or_false_or_missing_is_an_error_at_its_field(self):
        for field, wrong in (("drive_turns", "yes"), ("drive_turns", True), ("keep_awake", "yes")):
            doc = Preferences().to_document()
            doc["activities"][field] = wrong
            with self.assertRaises(ContractError) as ctx:
                Preferences.from_document(doc)
            self.assertEqual(ctx.exception.where, f"activities.{field}")
        missing = Preferences().to_document()
        del missing["activities"]
        with self.assertRaises(ContractError) as ctx:
            Preferences.from_document(missing)
        self.assertIn("missing activities", str(ctx.exception))


class RemoteContractTest(unittest.TestCase):
    """A destination of either kind, and the configuration remotes are reached through."""

    def test_a_remote_destination_round_trips_and_a_source_is_never_one(self):
        job = new_job("R", Endpoint(identity(1), "W"), [RemoteEndpoint("cloud", "/Backups/W/")])
        self.assertEqual(job.destinations[0].remote, RemoteEndpoint("cloud", "/Backups/W"))
        self.assertIsNone(sample_job().destinations[0].remote)
        self.assertEqual(Job.from_document(json.loads(json.dumps(job.to_document()))), job)
        with self.assertRaisesRegex(ContractError, "a source is a folder of this computer"):
            Source(RemoteEndpoint("cloud", "W"))
        doc = job.to_document()
        doc["source"]["endpoint"] = {"remote": "cloud", "folder": "W"}
        with self.assertRaises(ContractError):
            Job.from_document(doc)

    def test_the_configuration_is_an_absolute_path_and_a_command_goes_with_the_command_unlock(self):
        self.assertEqual(RemoteSettings(), RemoteSettings(None, Unlock.NONE, ()))
        RemoteSettings("X:\\settings\\remotes.conf", Unlock.REMEMBERED)
        for bad in (dict(configuration="rclone.conf"), dict(unlock=Unlock.COMMAND),
                    dict(unlock_command=("pass",)), dict(unlock=Unlock.COMMAND, unlock_command=(" ",)),
                    dict(unlock="command", unlock_command=("pass",)),
                    dict(unlock=Unlock.COMMAND, unlock_command=("pass", "two\nlines"))):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    RemoteSettings(**bad)
        settings = RemoteSettings("D:\\r.conf", Unlock.COMMAND, ["pass", "show", "rclone"])
        self.assertEqual(settings.unlock_command, ("pass", "show", "rclone"))
        preferences = Preferences(remotes=settings)
        self.assertEqual(Preferences.from_document(json.loads(json.dumps(preferences.to_document()))), preferences)
        doc = preferences.to_document()
        doc["remotes"]["password"] = "x"
        with self.assertRaisesRegex(ContractError, "unknown password"):
            Preferences.from_document(doc)


if __name__ == "__main__":
    unittest.main()
