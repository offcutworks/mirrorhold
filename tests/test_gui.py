"""The GUI's logic, headless: every ``mirrorhold_gui`` module that imports no toolkit.

The rows and status words of the job list, the editor's draft and checks, a preview in
words and its run buttons, a run's status in words, the history, import, the job file
at start, the session's store and launches, the engine preference; and maintenance's
surfaces — free space and the verification reminder in the job list, copies found to
differ, a verification in the Runs tab and the history, the Verify... dialog and a
verification's details, the editor's versions settings, old versions in a preview and
in a run's details; groups of jobs in the list, what a selection of several rows allows,
and the words about starting a cancelled run or an unfinished verification again; the
tone of a row whose drive is not attached, and the main window's remembered place; the
application identity the GUI's process takes, claimed in a process of its own; and what
the window tells of every activity — how far a verification at work is, a pass that did
not finish, a run that serves some destinations, a start that left no record, the
history of the selection and the CSV file it saves, the preview's line on how the source
is read, the refresher that looks again and keeps what it saw when a look fails, the runs
read on a thread of their own, an activity that ended listed as last seen at work until
the refresher has looked at what it left, and the words of the closing question and of a
request that did not reach its run; and what a
waiting run or verification says of whom it waits for, the drive they share and its
place in line, with the choice of runs on one drive in Preferences; and a remote
destination as the window follows and sets it up, from the records alone
(``test_remote_window`` runs the same words against the engine).
Previews are real
(``preview_job`` on temporary folders mapped as volumes); records and status documents
are built by hand where the case cannot be produced without the engine. The last class
follows the live path with the engine: a job saved through the GUI session, run as a
core process, previewed, confirmed and read back as rows and history.

No test imports tkinter, and none reads the live configuration."""
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from support import GIB, PROJECT, TempDirTest, endpoint, held, live_activity, volume
from test_engine_contract import INSTALLED

from mirrorhold import exchange
from mirrorhold.endpoints.cloud import SyncKind, SyncRoot
from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.marker import Marker, MarkerState, write_marker
from mirrorhold.endpoints.resolution import EndpointError
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.engine.rclone import EngineError, PINNED_VERSION
from mirrorhold.engine.remote import RemoteFeatures
from mirrorhold.jobs.model import (Activities, Destination, DriveTurns, HoldThresholds, Preferences, RemoteSettings,
                                   Triggers, Unlock, VersionCleanup, new_group, new_job)
from mirrorhold.jobs.schedule import DAILY, MONTHLY, WEEKLY
from mirrorhold.jobs.store import SCHEMA_VERSION, ConfigStore, StoreContents
from mirrorhold.jobs.validation import REMOTE_CONFIGURATION_MISSING, REMOTE_UNREACHABLE
from mirrorhold.maintenance.repair import LeftReason, RepairRecord, RepairState, write_repair
from mirrorhold.maintenance.verification import Measure, VerificationMode, VerificationRecord, VerificationState
from mirrorhold.locations import runtime_layout
from mirrorhold.notifications.message import notice_for
from mirrorhold.planning.listing import Occupancy
from mirrorhold.records.diagnostics import ActivityLog
from mirrorhold.records.record import (DONE_KEYS, FIRST_RUN_REASON, LEFT_KEYS, PLANNED_KEYS, Outcome, ReadMode,
                                       RunRecord, SourceRead, VersionsCleaned, read_records, write_record)
from mirrorhold.records.admission import CONFIGURATION, Activity, WaitedFor, _shared_drive, run_claim
from mirrorhold.records.status import KIND_RUN, ObservedRun
from mirrorhold.remotes.facts import Reach, RemoteFacts, RemoteSetup, SetupState
from mirrorhold.remotes.reach import NO_CONFIGURATION, NO_REMEMBERED_PASSWORD, RemoteSetupError
from mirrorhold.run.confirmation import Content, load_ticket
from mirrorhold.run.runner import preview_job

from mirrorhold.automation.reconcile import AutomationOutcome, AutomationState, ReconcileResult
from mirrorhold.installation.installer import InstallationState, InstallResult
from mirrorhold.installation.manifest import InstalledCopy, InstallKind, write_installation
from mirrorhold.installation.release import Release
from mirrorhold.installation.shortcut import APP_ID
from mirrorhold.jobs.model import Notifications

from mirrorhold_gui import formatting
from mirrorhold_gui.about_model import (MISSING_LICENCE, about_content, about_title, copyright_line, licence_path,
                                       notices_path)
from mirrorhold_gui.shipped_model import ICON_FILE_NAME, NOTICES_FILE_NAME, icon_path, read_text
from mirrorhold_gui.editor_model import (ASKING_REMOTE, LAST_DAY_CHOICE, NO_SNAPSHOT_NOTE, NOTE, REFUSED,
                                          SNAPSHOT_NOTE, JobDraft, check, cleanup_question, endpoint_line, path_problem,
                                          snapshot_choice, typed_path)
from mirrorhold_gui.history_model import (JobHistory, empty_words, history_entries, history_file_name, history_rows,
                                          log_file_name, log_of, record_details, result_words, save_history,
                                          selection_entries, source_words, start_details, started_by)
from mirrorhold_gui.groups_model import (Selection, collapsed_groups, delete_group_question, group_name_problem,
                                         not_verified_why, remove_jobs_question, reordered, run_several,
                                         verify_several, with_collapsed)
from mirrorhold_gui.jobs_model import (RemotePlace, Tone, arrange, build_rows, first_backup_question, job_has_run,
                                       log_job, reason_words, row_by_iid, waiting_words)
from mirrorhold_gui.prefs_model import (AT_THE_SAME_TIME, AUTOMATIC, DRIVE_NOTE, DRIVE_TITLE, KEEP_AWAKE,
                                        KEEP_AWAKE_NOTE, NAME_AT_SHUTDOWN, NAME_AT_SHUTDOWN_NOTE, NO_COMMAND,
                                        ONE_AT_A_TIME, TYPE_PASSWORD, WORK_TITLE, RemotesDraft, automation_problem,
                                        automation_words, check_engine, command_text, command_words, install_done,
                                        installation_view, read_remotes, remotes_draft, set_up_here_question,
                                        with_drive_turns, with_engine, with_keep_awake, with_name_at_shutdown,
                                        with_notifications)
from mirrorhold_gui.preview_model import PairState, PreviewWorker, Quantity, WorkerState, summarise, ticket_to_run
from mirrorhold_gui.refresh import Refresher, RunWatcher, Settling, ended_runs, observe
from mirrorhold_gui.remote_model import (NAME_CONFIGURATION, Background, RemoteAccess, at_top, child_folder,
                                         parent_folder, window_message)
from mirrorhold_gui.runs_model import (REQUEST_TITLES, closing_question, destination_number, destination_places,
                                      kind_counts, live_panels, not_reached_words, not_started_words, run_panel,
                                      unreadable_panel, unreadable_words, waiting_note, waits)
from mirrorhold_gui.session import GuiSession, LaunchProblem, NotStarted, StoreConflict
from mirrorhold_gui.skipped_model import unbacked_report, whole_words
from mirrorhold_gui.startup_model import HELD_TEXT, HELD_TITLE, StartupKind, startup_problem
from mirrorhold_gui.transfer_model import ImportState, apply_import, import_lines
from mirrorhold_gui.verify_model import (result_words as verify_result_words, verification_details,
                                         verification_summary, verify_dialog)
from mirrorhold_gui import verify_model
from mirrorhold_gui.repair_model import repair_details, repaired_line, replace_question
from mirrorhold_gui.repair_model import result_words as repair_result_words
from mirrorhold_gui.jobs_model import start_failure_words
from mirrorhold_gui.window_model import (WINDOW_KEY, Area, Place, fit, geometry_text, parse_geometry,
                                         remembered_place, share, with_place)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
needs_engine = unittest.skipUnless(INSTALLED, "the pinned engine is not installed")
GUI = PROJECT / "mirrorhold_gui"


def write(root, rel, data=b"x"):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def record(job, index, outcome, *, reasons=(), started="2026-10-04T20:10:00Z", ended="2026-10-04T20:51:00Z",
           done=None, trigger="manual", batch=None, skipped=()):
    counts = dict.fromkeys(DONE_KEYS, 0)
    counts.update(done or {})
    return RunRecord("r" * 32, job.job_id, job.name, job.destinations[index - 1].marker_id, index, trigger, started,
                     ended, outcome, tuple(reasons), "", PINNED_VERSION, dict.fromkeys(PLANNED_KEYS, 0), counts,
                     dict.fromkeys(LEFT_KEYS, 0), batch, tuple(skipped), len(skipped))


def live(job, *, phase="copying", pair_index=1, **extra):
    status = {"run_id": "a" * 32, "job_id": job.job_id, "job_name": job.name, "phase": phase,
              "pair_index": pair_index, "finished": [], "planned": {}, "done": {}}
    status.update(extra)
    return ObservedRun(status, True)


class ToolkitFreeTest(unittest.TestCase):
    def test_no_logic_module_imports_a_toolkit(self):
        for path in GUI.glob("*.py"):
            if path.name.endswith("_view.py"):
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import tkinter", text, path.name)
            self.assertNotIn("from tkinter", text, path.name)


class FormattingTest(unittest.TestCase):
    def test_counts_sizes_and_times(self):
        self.assertEqual(formatting.count(2104331), "2,104,331")
        self.assertEqual(formatting.size(0), "0 bytes")
        self.assertEqual(formatting.size(1), "1 byte")
        self.assertEqual(formatting.size(1536), "1.5 KiB")
        self.assertEqual(formatting.size(5 * GIB), "5.0 GiB")
        self.assertEqual(formatting.files(1), "1 file")
        self.assertEqual(formatting.files(1052), "1,052 files")
        self.assertEqual(formatting.local_time(None), "never")
        self.assertRegex(formatting.local_time("2026-10-04T20:10:00Z"), r"^2026-10-0\d \d\d:\d\d$")
        self.assertEqual(formatting.elapsed("2026-10-04T20:10:00Z", "2026-10-04T20:51:00Z"), "41 min")


class JobRowsTest(unittest.TestCase):
    def setUp(self):
        self.s = volume(1, "S", label="Media")
        self.d = volume(2, "F", label="Backup 1")
        self.e = volume(3, "G", label="Backup 2")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror"), endpoint(self.e, "")])
        self.contents = StoreContents((self.job,))
        self.key1 = (self.job.job_id, self.job.destinations[0].marker_id)

    def rows(self, *, volumes=None, marker=MarkerState.MATCH, records=(), runs=()):
        volumes = (self.s, self.d) if volumes is None else volumes
        markers = {self.key1: marker} if marker is not None else {}
        return build_rows(self.contents, volumes, markers, {self.job.job_id: tuple(records)}, runs)[0]

    def test_a_new_destination_waits_for_its_first_run_confirmation(self):
        row = self.rows(marker=MarkerState.MISSING)
        first, second = row.destinations
        self.assertEqual((first.status, first.tone, first.needs_review), ("First run: confirm", Tone.ATTENTION, True))
        self.assertEqual((second.drive, second.status), ("not attached", "Waits for its drive"))
        self.assertEqual(second.folder, "(the root of its drive)")
        self.assertEqual((first.name, second.name), ("Backup 1", "drive root"))
        self.assertEqual((row.status, row.tone), ("Needs your review", Tone.ATTENTION))
        self.assertFalse(first.can_open)
        self.assertIn('"Backup 1"', first.folder)

    def test_a_completed_destination_is_up_to_date_and_can_be_opened(self):
        row = self.rows(records=[record(self.job, 1, Outcome.COMPLETED)])
        first = row.destinations[0]
        self.assertEqual(first.status, "Completed")
        self.assertEqual(row.status, "Up to date")
        self.assertTrue(first.can_open)
        self.assertEqual(first.versions_folder, Path("F:\\Mirror") / ".mirrorhold" / "versions")
        self.assertNotEqual(first.last_success, "never")
        self.assertEqual(row.last_success, first.last_success)

    def test_held_removals_need_review(self):
        row = self.rows(records=[record(self.job, 1, Outcome.HELD, reasons=("above-threshold",))])
        self.assertEqual(row.destinations[0].status, "Removals held: review")
        self.assertTrue(row.needs_review)

    def test_problems_are_named(self):
        failed = self.rows(records=[record(self.job, 1, Outcome.FAILED, reasons=("engine-failed",))])
        self.assertEqual((failed.status, failed.tone), ("Failed - see history", Tone.PROBLEM))
        stopped = self.rows(records=[record(self.job, 1, Outcome.STOPPED, reasons=("insufficient-space",))])
        self.assertEqual(stopped.destinations[0].status, "Stopped: not enough free space")
        foreign = self.rows(marker=MarkerState.MISMATCH)
        self.assertEqual(foreign.destinations[0].status, "Stopped: the folder belongs to another job")
        clone = replace(self.d, device="\\\\?\\Volume{clone}\\", mount_points=("K:\\",))
        twice = self.rows(volumes=(self.s, self.d, clone), marker=None)
        self.assertEqual((twice.destinations[0].drive, twice.destinations[0].status),
                         ("two drives match", "Stopped: two drives match"))

    def test_a_run_in_progress_shows_on_its_destinations(self):
        row = self.rows(runs=[live(self.job, phase="copying", pair_index=1)])
        self.assertEqual([d.status for d in row.destinations], ["Running", "Waiting to run"])
        self.assertEqual((row.status, row.running), ("Running", True))
        waiting = self.rows(runs=[live(self.job, phase="waiting", pair_index=0)])
        self.assertEqual(waiting.status, "Waiting to run")
        paused = self.rows(runs=[live(self.job, paused=True)])
        self.assertEqual(paused.destinations[0].status, "Paused")
        dead = self.rows(runs=[ObservedRun(live(self.job).status, False)],
                         records=[record(self.job, 1, Outcome.COMPLETED)])
        self.assertEqual(dead.status, "Up to date")

    def test_an_absent_source_waits(self):
        row = self.rows(volumes=(self.d,), records=[record(self.job, 1, Outcome.COMPLETED)])
        self.assertEqual((row.drive, row.status), ("not attached", "Waits for its source drive"))

    def test_whether_a_job_has_run(self):
        self.assertFalse(job_has_run([record(self.job, 1, Outcome.HELD, reasons=("first-run",))]))
        self.assertFalse(job_has_run([record(self.job, 1, Outcome.FAILED, reasons=("engine-unusable",))]))
        self.assertFalse(job_has_run([record(self.job, 1, Outcome.ABSENT)]))
        self.assertTrue(job_has_run([record(self.job, 1, Outcome.COMPLETED)]))
        self.assertTrue(job_has_run([record(self.job, 1, Outcome.FAILED, done={"files_copied": 3})]))


class EditorTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        (self.vol_s / "Work").mkdir(parents=True)
        self.vol_d.mkdir()
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="D", fs="FAT32")
        self.volumes = (self.s, self.d)

    def draft(self):
        draft = JobDraft(name="Work")
        draft.set_source(str(self.vol_s / "Work"), self.volumes)
        draft.add_destination(str(self.vol_d / "Backup"), self.volumes)
        return draft

    def test_an_empty_draft_names_what_is_missing(self):
        result = check(JobDraft(), self.volumes, (), ())
        self.assertIsNone(result.job)
        self.assertEqual({c.code for c in result.checks}, {"input-name", "input-source", "input-destinations"})
        self.assertFalse(result.can_save)

    def test_a_valid_draft_saves_with_the_core_findings_shown(self):
        draft = self.draft()
        draft.patterns_text = "*.tmp\n\n  /Cache/  \n"
        draft.count_floor_text, draft.percent_text = "200", "2.5"
        result = check(draft, self.volumes, (), ())
        self.assertTrue(result.can_save, result.checks)
        self.assertEqual(result.job.source.exclusions, ("*.tmp", "/Cache/"))
        self.assertEqual(result.job.holds, HoldThresholds(200, 2.5))
        codes = [c.code for c in result.checks]
        self.assertIn("fat32-timestamps", codes)
        self.assertNotIn("size-checks-deferred", codes)

    def test_a_refused_draft_cannot_be_saved(self):
        draft = self.draft()
        draft.add_destination(str(self.vol_s / "Work" / "inner"), self.volumes)
        result = check(draft, self.volumes, (), ())
        self.assertFalse(result.can_save)
        self.assertEqual(result.checks[0].level, REFUSED)
        self.assertEqual(result.checks[0].code, "source-destination-overlap")

    def test_hold_values_must_be_numbers_in_range(self):
        draft = self.draft()
        draft.count_floor_text = "many"
        self.assertIn("input-holds", [c.code for c in check(draft, self.volumes, (), ()).checks])
        draft.count_floor_text, draft.percent_text = "10", "0"
        self.assertIn("input-holds", [c.code for c in check(draft, self.volumes, (), ()).checks])

    def test_editing_keeps_marker_identities_and_the_settings_it_does_not_show(self):
        base = replace(check(self.draft(), self.volumes, (), ()).job, enabled=False,
                       triggers=Triggers(True, "daily 21:00"), version_cleanup=VersionCleanup(30))
        draft = JobDraft.from_job(base, source_editable=False)
        draft.name = "Work renamed"
        draft.add_destination(str(self.vol_d / "Second"), self.volumes)
        draft.move_destination(1, -1)
        job = check(draft, self.volumes, (), ()).job
        self.assertEqual(job.job_id, base.job_id)
        self.assertEqual(job.destinations[1].marker_id, base.destinations[0].marker_id)
        self.assertNotIn(job.destinations[0].marker_id, [d.marker_id for d in base.destinations])
        self.assertEqual((job.enabled, job.triggers, job.version_cleanup),
                         (False, Triggers(True, "daily 21:00"), VersionCleanup(30)))
        with self.assertRaises(EndpointError):
            draft.set_source(str(self.vol_s), self.volumes)

    def test_another_jobs_destination_is_refused(self):
        other = check(self.draft(), self.volumes, (), ()).job
        result = check(self.draft(), self.volumes, (), (other,))
        self.assertIn("destination-shared", [c.code for c in result.checks])

    def test_a_folder_on_no_present_volume_is_refused_at_once(self):
        with self.assertRaises(EndpointError):
            JobDraft().add_destination("Z:\\nowhere", self.volumes)
        folder, drive, free = endpoint_line(endpoint(volume(9, "Q"), "Old"), self.volumes)
        self.assertEqual((folder, drive, free), ("\\Old", "not attached", ""))

    def test_a_typed_folder_is_read_as_a_chosen_one(self):
        work = str(self.vol_s / "Work")
        self.assertEqual(typed_path(f'  "{work}\\"  '), work, "Explorer's Copy as path, pasted with spaces")
        self.assertEqual(typed_path(work.replace("\\", "/")), work)
        self.assertEqual(typed_path("   "), "")
        self.assertEqual(typed_path('""'), "")

    def test_a_typed_source_must_exist_and_a_wrong_one_is_said_in_the_command_lines_words(self):
        draft = self.draft()
        missing = str(self.vol_s / "Wrok")
        with self.assertRaises(EndpointError) as caught:
            draft.set_source(missing, self.volumes)
        self.assertEqual(path_problem(caught.exception), f"{missing}: this folder does not exist.")
        self.assertEqual(draft.source.path, "Work", "a wrong path leaves the draft to the editor")
        with self.assertRaises(EndpointError) as caught:
            draft.set_source("Z:\\Work", self.volumes)
        self.assertEqual(path_problem(caught.exception), "Z:\\Work: not on any present local volume.")
        with self.assertRaises(EndpointError) as caught:
            draft.set_source("\\\\nas\\share", self.volumes)
        self.assertEqual(path_problem(caught.exception),
                         "\\\\nas\\share: a network path is not an endpoint; choose a folder on a local drive.")
        draft.clear_source()
        self.assertEqual({c.code for c in check(draft, self.volumes, (), ()).checks}, {"input-source"})
        kept = JobDraft.from_job(check(self.draft(), self.volumes, (), ()).job, source_editable=False)
        kept.clear_source()
        self.assertIsNotNone(kept.source, "a job that has run keeps its source")

    def test_a_destination_not_made_yet_is_taken(self):
        draft = self.draft()
        later = self.vol_d / "Later" / "Backup"
        draft.add_destination(typed_path(f'"{later}"'), self.volumes)
        self.assertFalse(later.exists())
        result = check(draft, self.volumes, (), ())
        self.assertTrue(result.can_save, result.checks)
        self.assertEqual(result.job.destinations[1].endpoint.path, "Later\\Backup")

    def test_the_snapshot_choice_is_kept_and_offered_only_for_a_drive_that_can_have_one(self):
        draft = self.draft()
        self.assertTrue(check(draft, self.volumes, (), ()).job.source.read_from_snapshot)
        self.assertEqual(snapshot_choice(draft, self.volumes), (True, SNAPSHOT_NOTE))
        draft.read_from_snapshot = False
        job = check(draft, self.volumes, (), ()).job
        self.assertFalse(job.source.read_from_snapshot)
        edited = JobDraft.from_job(job, source_editable=False)
        self.assertFalse(edited.read_from_snapshot)
        edited.read_from_snapshot = True
        self.assertTrue(check(edited, self.volumes, (), ()).job.source.read_from_snapshot)
        on_fat = JobDraft(name="Card")
        on_fat.set_source(str(self.vol_d), self.volumes)
        self.assertEqual(snapshot_choice(on_fat, self.volumes), (False, NO_SNAPSHOT_NOTE))
        absent = JobDraft.from_job(job, source_editable=False)
        self.assertEqual(snapshot_choice(absent, ()), (True, SNAPSHOT_NOTE))
        self.assertEqual(snapshot_choice(JobDraft(), self.volumes), (True, SNAPSHOT_NOTE))


class PreviewCase(TempDirTest):
    holds = HoldThresholds(count_floor=3, percent=5)

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        self.src, self.dst = self.vol_s / "Work", self.vol_d / "Backup"
        self.src.mkdir(parents=True)
        self.vol_d.mkdir()
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="D")
        self.store = ConfigStore(self.tmp / "state" / "store.json")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Backup")], holds=self.holds)
        self.store.save(StoreContents((self.job,)))

    def preview(self, volumes=None, job=None):
        if job is not None:
            self.job = job
            self.store.save(StoreContents((job,)))
        source = FixedVolumeSource(volumes or (self.s, self.d))
        return summarise(preview_job(self.store, self.job.job_id, volume_source=source, sync_roots=()))

    def mirror(self, files):
        """A destination already mirrored and marked, as a confirmed first run leaves it."""
        for rel, data in files.items():
            src = write(self.src, rel, data)
            target = self.dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        write_marker(self.dst, Marker(self.job.job_id, self.job.destinations[0].marker_id))


class PreviewSummaryTest(PreviewCase):
    def test_a_first_run_onto_an_empty_folder(self):
        write(self.src, "a.txt", "a")
        summary = self.preview()
        pair = summary.pairs[0]
        self.assertEqual(pair.state, PairState.FIRST_RUN)
        self.assertTrue(pair.headline.startswith("FIRST RUN - nothing has been written"))
        self.assertIn("1 file", summary.source_line)
        actions = summary.actions
        self.assertEqual((actions.review, actions.apply_all, actions.keep_held, actions.run_now),
                         (True, True, False, False))
        self.assertTrue(actions.first_run_waits)
        self.assertIsNotNone(summary.ticket_id)
        self.assertIn("a default for a USB 3 hard disk", pair.time)

    def test_an_adoption_says_what_moves_into_versions(self):
        write(self.src, "a.txt", "a")
        write(self.dst, "old.txt", "old")
        write(self.dst, "a.txt", "different")
        pair = self.preview().pairs[0]
        self.assertEqual(pair.state, PairState.ADOPTION)
        self.assertTrue(pair.headline.startswith("FIRST RUN INTO A FOLDER THAT ALREADY HOLDS FILES OR FOLDERS - "),
                        pair.headline)
        self.assertTrue(pair.headline.endswith("Choose what happens to them:"), pair.headline)
        moved = pair.choices[1]
        self.assertEqual((moved.content, moved.offered), (Content.MOVE, True))
        into_versions = [q for q in moved.pair.quantities if q.label == "Into versions"]
        self.assertEqual([q.detail for q in into_versions], ["1 not in the source, 1 replaced by the source's version"])

    def test_held_removals_name_their_reasons_and_group_by_folder(self):
        self.mirror({f"keep\\k{i}.txt": f"k{i}" for i in range(4)} |
                    {f"gone\\g{i}.txt": f"g{i}" for i in range(4)} | {"top.txt": "t"})
        for i in range(4):
            (self.src / "gone" / f"g{i}.txt").unlink()
        (self.src / "top.txt").unlink()
        write(self.src, "new.txt", "n")
        summary = self.preview()
        pair = summary.pairs[0]
        self.assertEqual(pair.state, PairState.HELD)
        self.assertTrue(pair.headline.startswith("REMOVALS HELD - more files would move into versions than this job "
                                                 "allows in one run (the larger of 3 files and 5 % of 9 = 3)"))
        self.assertIn("half or more of this destination's files", pair.headline)
        self.assertEqual([(g.label, len(g.files)) for g in pair.groups], [("gone\\", 4), ("(top level)", 1)])
        self.assertEqual(pair.groups[0].samples, ("g0.txt", "g1.txt"))
        self.assertEqual(len(pair.lists["removals"]), 5)
        self.assertEqual((summary.actions.apply_all, summary.actions.keep_held), (True, True))
        self.assertFalse(summary.actions.first_run_waits)

    def test_a_folder_deleted_whole_is_one_group_named_by_itself(self):
        self.mirror({"tree\\a.txt": "a", "tree\\sub\\b.txt": "b", "tree\\sub\\deep\\c.txt": "c",
                     "tree\\sub\\deep\\d.txt": "d", "keep\\k0.txt": "k0", "keep\\k1.txt": "k1"})
        shutil.rmtree(self.src / "tree")
        (self.src / "keep" / "k0.txt").unlink()
        pair = self.preview().pairs[0]
        self.assertEqual([(g.label, len(g.files), g.whole) for g in pair.groups],
                         [("tree\\", 4, True), ("keep\\", 1, False)])
        tree, keep = pair.groups
        self.assertEqual(tree.detail, "whole folder deleted at the source")
        self.assertEqual([tree.name(f) for f in tree.files],
                         ["a.txt", "sub\\b.txt", "sub\\deep\\c.txt", "sub\\deep\\d.txt"])
        self.assertEqual(keep.detail, "for example k0.txt")
        self.assertEqual(len(pair.lists["removals"]), 5)

    def test_a_folder_an_adoption_would_move_whole_is_one_group(self):
        write(self.src, "a.txt", "a source file")
        write(self.dst, "old\\x.txt", "x")
        write(self.dst, "old\\deep\\y.txt", "yy")
        moved = next(c for c in self.preview().pairs[0].choices if c.content is Content.MOVE).pair
        self.assertEqual([(g.label, len(g.files), g.detail) for g in moved.groups],
                         [("old\\", 2, "whole folder not in the source")])

    BATCH = "2026-10-27T22-14-31Z"
    MOVED = datetime(2026, 10, 27, 22, 14, 31, tzinfo=timezone.utc).astimezone()

    def reappeared(self, *rels):
        """Files the destination holds again as a run moved them into versions: a copy of each,
        times kept, in the versions batch ``BATCH``."""
        for rel in rels:
            target = self.dst / ".mirrorhold" / "versions" / self.BATCH / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.dst / rel, target)

    def removed(self):
        """A mirror whose source lost a folder of two files and two files at the top; every
        file has a size of its own, so no removal is taken for a rename."""
        self.mirror({"tree\\a.txt": "a" * 31, "tree\\sub\\b.txt": "b" * 32, "gone.txt": "g" * 33,
                     "stray.txt": "s" * 34, "keep.txt": "k" * 35})
        shutil.rmtree(self.src / "tree")
        (self.src / "gone.txt").unlink()
        (self.src / "stray.txt").unlink()

    def test_removals_that_reappeared_are_grouped_apart_with_when_they_were_moved(self):
        self.removed()
        self.reappeared("tree\\a.txt", "tree\\sub\\b.txt", "gone.txt")
        pair = self.preview().pairs[0]
        self.assertEqual([(g.label, len(g.files), g.whole, g.detail) for g in pair.groups],
                         [("(top level)", 1, False, "for example stray.txt")])
        self.assertEqual([(g.label, len(g.files), g.whole, g.detail) for g in pair.back_groups],
                         [("tree\\", 2, True, "whole folder reappeared"),
                          ("(top level)", 1, False, "for example gone.txt")])
        self.assertEqual(pair.back_line, f"These reappeared after being moved into versions on "
                                         f"{self.MOVED:%Y-%m-%d} at {self.MOVED:%H:%M}.")
        into = next(q for q in pair.quantities if q.label == "Into versions")
        self.assertEqual((into.files, into.detail),
                         ("4 files", "4 deleted at the source (3 of them reappeared), 0 replaced by a newer version"))
        self.assertEqual(len(pair.lists["removals"]), 4)
        self.assertEqual(pair.hidden_back_groups, 0)

    def test_a_folder_moving_whole_whose_files_only_partly_reappeared_is_whole_on_neither_side(self):
        self.removed()
        self.reappeared("tree\\a.txt")
        pair = self.preview().pairs[0]
        self.assertEqual([(g.label, len(g.files), g.whole, g.detail) for g in pair.groups],
                         [("(top level)", 2, False, "for example gone.txt, stray.txt"),
                          ("tree\\", 1, False, "for example sub\\b.txt")])
        self.assertEqual([(g.label, len(g.files), g.whole, g.detail) for g in pair.back_groups],
                         [("tree\\", 1, False, "for example a.txt")])

    def test_the_cloud_client_keeping_a_copy_of_the_folder_is_named_beside_when(self):
        self.removed()
        self.reappeared("gone.txt")
        roots = (SyncRoot("Google Drive", str(self.vol_d), SyncKind.CLOUD_COPY),)
        source = FixedVolumeSource((self.s, self.d))
        pair = summarise(preview_job(self.store, self.job.job_id, volume_source=source, sync_roots=roots)).pairs[0]
        self.assertEqual(pair.back_line, f"These reappeared after being moved into versions on {self.MOVED:%Y-%m-%d} "
                                         f"at {self.MOVED:%H:%M}: Google Drive, which keeps a copy of this folder, "
                                         "may have put them back.")

    def test_nothing_reappeared_where_no_versions_batch_holds_the_removals(self):
        self.removed()
        pair = self.preview().pairs[0]
        self.assertEqual((pair.back_groups, pair.back_line), ((), ""))
        self.assertEqual(sum(len(g.files) for g in pair.groups), 4)

    def test_lost_signatures_are_listed(self):
        self.mirror({f"p{i}.png": PNG for i in range(5)})
        write(self.src, "p0.png", os.urandom(64))
        pair = self.preview().pairs[0]
        self.assertEqual(pair.state, PairState.HELD)
        self.assertIn("1 changed file no longer starts like its file type", pair.headline)
        self.assertIn("p0.png", pair.headline)
        self.assertEqual([f.path for f in pair.lists["lost"]], ["p0.png"])

    def test_a_routine_and_an_unchanged_destination(self):
        self.mirror({"a.txt": "a"})
        summary = self.preview()
        self.assertEqual(summary.pairs[0].state, PairState.READY)
        self.assertIn("matches the source", summary.pairs[0].headline)
        self.assertEqual((summary.actions.run_now, summary.actions.review), (True, False))
        write(self.src, "b.txt", "b")
        self.assertEqual(self.preview().pairs[0].headline, "Ready - nothing needs your review.")

    def test_absent_drives_and_a_full_destination_cannot_run(self):
        write(self.src, "a.txt", "a")
        summary = self.preview(volumes=(self.s,))
        self.assertEqual(summary.pairs[0].state, PairState.ABSENT)
        self.assertEqual(summary.actions.blocked, "Nothing can run now: no destination's drive is attached.")
        self.assertFalse(summary.actions.apply_all or summary.actions.run_now or summary.actions.keep_held)
        summary = self.preview(volumes=(self.d,))
        self.assertEqual(summary.actions.blocked, "Nothing can run now: the source's drive is not attached.")
        self.mirror({"a.txt": "a"})
        write(self.src, "big.bin", b"x" * 4096)
        full = replace(self.d, free_bytes=10)
        pair = self.preview(volumes=(self.s, full)).pairs[0]
        self.assertEqual(pair.state, PairState.CANNOT_RUN)
        self.assertRegex(pair.headline, r"^CANNOT RUN - needs .+ with its margin; 10 bytes free\.$")

    def test_a_destination_a_cloud_client_keeps_a_copy_of_carries_its_notice(self):
        self.mirror({"a.txt": "a"})
        roots = (SyncRoot("Google Drive", str(self.vol_d), SyncKind.CLOUD_COPY),)
        source = FixedVolumeSource((self.s, self.d))
        pair = summarise(preview_job(self.store, self.job.job_id, volume_source=source, sync_roots=roots)).pairs[0]
        self.assertEqual(pair.state, PairState.READY)
        (notice,) = pair.notices
        self.assertTrue(notice.startswith(f"This folder lies inside {self.vol_d}, of which Google Drive keeps a copy "
                                          "in its cloud; Google Drive may put back what a run moves into versions"))
        self.assertTrue(notice.endswith("."))
        self.assertEqual(self.preview().pairs[0].notices, ())


class PreviewChoiceTest(PreviewCase):
    """A first run into a folder that already holds what the source does not: its two
    answers, the preview's own leaving that where it is, the destination as it would run by
    each, and the ticket a run takes for the answer given (the maintainer's decision,
    2026-10-11)."""

    def holding(self):
        write(self.src, "Photos\\a.jpg", "a")
        write(self.dst, "Windows\\kernel.dll", "the operating system")
        write(self.dst, "Photos\\old.jpg", "an old photo")

    def test_the_answers_and_the_destination_by_each(self):
        self.holding()
        pair = self.preview().pairs[0]
        self.assertEqual(pair.state, PairState.ADOPTION)
        self.assertEqual(pair.headline, "FIRST RUN INTO A FOLDER THAT ALREADY HOLDS FILES OR FOLDERS - 2 files there "
                                        "are not in the source. Choose what happens to them:")
        self.assertEqual([(c.content, c.label, c.offered) for c in pair.choices],
                         [(Content.LEAVE, "Leave them where they are", True),
                          (Content.MOVE, "Move them into versions", True)])
        self.assertIs(pair.content, Content.LEAVE)
        self.assertEqual(pair.quantities[0], Quantity("Left as they are", "2 files", "", "Windows, Photos\\old.jpg",
                                                      "left"))
        self.assertEqual(pair.note, "Photos is in the source and already here: the source's files are backed up into "
                                    "it, the 1 file there that the source does not have stays as it is, and what you "
                                    "put into it yourself from now on moves into versions at the next run.")
        moved = pair.choices[1].pair
        self.assertEqual([q.label for q in moved.quantities][:2], ["To copy", "Into versions"])
        self.assertEqual(moved.quantities[1].detail, "2 not in the source, 0 replaced by the source's version")
        self.assertEqual({f.why for f in moved.lists["removals"]}, {"not in the source"})
        self.assertEqual(moved.note, "")

    def test_moving_is_shown_but_not_offered_where_another_jobs_source_lies(self):
        write(self.src, "a.txt", "a")
        write(self.dst, "Inner\\data.txt", "another job's source")
        inner = new_job("Inner", endpoint(self.d, "Backup\\Inner"), [endpoint(self.s, "InnerCopy")])
        self.store.save(StoreContents((self.job, inner)))
        move = self.preview().pairs[0].choices[1]
        self.assertEqual((move.label, move.offered), ("Move them into versions - not offered:", False))
        self.assertTrue(move.detail.endswith("the source of job 'Inner', is inside this folder."), move.detail)

    def test_the_run_takes_the_previews_ticket_or_one_for_the_answer_given(self):
        self.holding()
        volumes = FixedVolumeSource((self.s, self.d))
        session = GuiSession(self.store.path, volume_source=volumes, sync_roots=())
        session.load()
        result = preview_job(self.store, self.job.job_id, volume_source=volumes, sync_roots=())
        summary = summarise(result)
        self.assertEqual(ticket_to_run(session, result, summary, {1: Content.LEAVE}), summary.ticket_id)
        moved = ticket_to_run(session, result, summary, {1: Content.MOVE})
        self.assertNotEqual(moved, summary.ticket_id)
        ticket = load_ticket(runtime_layout(self.store.path), self.job.job_id, moved)
        self.assertIs(ticket.pairs[self.job.destinations[0].marker_id].content, Content.MOVE)

    def test_the_words_for_a_pair_stopped_to_protect_a_folder(self):
        self.assertEqual(reason_words("destination-contains-sync-root"), "a synchronised folder would be written into")
        self.assertEqual(reason_words("destination-contains-other-source"),
                         "another job's source would be written into")
        self.assertEqual(reason_words("destination-contains-system-folder"),
                         "a folder Windows or MirrorHold needs would be written into")
        self.assertEqual(reason_words("shared-record-unreadable"), "its record of what to leave cannot be read")


class PreviewWorkerTest(PreviewCase):
    def session(self):
        return GuiSession(self.store.path, volume_source=FixedVolumeSource((self.s, self.d)), sync_roots=())

    def test_a_preview_reports_its_progress_and_its_result(self):
        for i in range(30):
            write(self.src, f"d{i % 3}\\f{i}.txt", "x")
        session = self.session()
        session.load()
        worker = PreviewWorker(session, self.job.job_id).start()
        worker.join(30)
        self.assertEqual(worker.state, WorkerState.DONE, worker.error)
        self.assertEqual(worker.progress.destination, 1)
        self.assertEqual(worker.result.source_files, 30)

    def test_a_cancelled_preview_writes_no_ticket(self):
        write(self.src, "a.txt", "a")
        session = self.session()
        session.load()
        worker = PreviewWorker(session, self.job.job_id)
        worker.cancel()
        worker.start().join(30)
        self.assertEqual(worker.state, WorkerState.CANCELLED)
        self.assertFalse((session.layout.tickets_dir / f"{self.job.job_id}.json").exists())

    def test_a_failing_preview_says_why(self):
        session = self.session()
        session.load()
        worker = PreviewWorker(session, "f" * 32).start()
        worker.join(30)
        self.assertEqual(worker.state, WorkerState.FAILED)
        self.assertIn("no job", worker.error)

    def test_a_preview_of_some_destinations_previews_those_alone_wherever_the_job_has_put_them(self):
        write(self.src, "a.txt", "a")
        first, second = self.job.destinations[0], Destination(endpoint(self.d, "Second"))
        self.store.save(StoreContents((replace(self.job, destinations=(first, second)),)))
        session = self.session()
        session.load()

        def previewed(*markers):
            worker = PreviewWorker(session, self.job.job_id, destinations=markers).start()
            worker.join(30)
            self.assertEqual(worker.state, WorkerState.DONE, worker.error)
            return [(p.assessment.index, p.assessment.destination.marker_id) for p in worker.result.pairs]

        self.assertEqual(previewed(second.marker_id), [(2, second.marker_id)])
        self.store.save(StoreContents((replace(self.job, destinations=(second, first)),)))   # elsewhere, unseen here
        self.assertEqual(previewed(second.marker_id), [(1, second.marker_id)], "found by the job file as it is now")
        gone = PreviewWorker(session, self.job.job_id, destinations=("f" * 32,)).start()
        gone.join(30)
        self.assertEqual(gone.state, WorkerState.FAILED)
        self.assertIn("no longer in the job", gone.error)


class RunsModelTest(unittest.TestCase):
    def setUp(self):
        self.job = new_job("Photos", endpoint(volume(1, "S"), "P"), [endpoint(volume(2, "F"), "M")])

    def panel(self, **status):
        return run_panel(live(self.job, destination="destination 1 at F:\\M", **status))

    def test_waiting_listing_and_copying(self):
        waiting = self.panel(phase="waiting", pair_index=0)
        self.assertEqual((waiting.phase, waiting.can_cancel, waiting.can_pause), ("waiting for the run in progress",
                                                                                  True, False))
        self.assertEqual(waiting.title, "Photos")
        listing = self.panel(phase="listing", pair_index=0, listed=1234)
        self.assertEqual(listing.phase, "listing the source   1,234 entries")
        self.assertIsNone(listing.fraction)
        copying = self.panel(planned={"files_to_copy": 10, "bytes_to_copy": 4 * 1024 ** 2},
                             done={"files_copied": 5, "bytes_copied": 2 * 1024 ** 2}, rate=1024 ** 2, eta_seconds=120,
                             in_flight=[["sub\\photo-1.raw", 100, 61], ["photo-2.raw", 100, 8]])
        self.assertEqual(copying.title, "Photos -> 1  F:\\M")
        self.assertEqual(copying.fraction, 0.5)
        self.assertEqual(copying.counts, "5 of 10 files   2.0 MiB of 4.0 MiB   1.0 MiB/s   about 2 min left")
        self.assertEqual(copying.in_flight, "In flight: photo-1.raw (61 %), photo-2.raw (8 %)")
        self.assertTrue(copying.can_pause and copying.can_cancel)

    def test_paused_and_cancelling(self):
        paused = self.panel(paused=True)
        self.assertEqual((paused.phase, paused.can_resume, paused.can_pause), ("paused", True, False))
        graceful = self.panel(cancelling="graceful", in_flight=[["a", 2 * 1024 ** 2, 1024 ** 2], ["b", 10, 10]])
        self.assertEqual(graceful.phase, "cancelling: finishing the files in flight")
        self.assertEqual(graceful.counts, "Waiting for 2 files in flight, 1.0 MiB left; no new file starts")
        self.assertEqual((graceful.can_stop_now, graceful.can_cancel), (True, False))
        immediate = self.panel(cancelling="immediate")
        self.assertEqual((immediate.phase, immediate.can_stop_now, immediate.can_cancel),
                         ("stopping now", False, False))

    def test_only_live_runs_have_panels(self):
        self.assertEqual(live_panels([ObservedRun(live(self.job).status, False)]), [])

    def test_a_waiting_row_names_the_job_it_waits_for_and_whether_that_one_is_paused(self):
        def waiting_for(**other):
            return dict(dict(run_id="w" * 32, kind="run", job_name="Work", paused=False, waiting=False), **other)

        def words(**other):
            return self.panel(phase="waiting", pair_index=0, waiting_for=waiting_for(**other)).phase

        self.assertEqual(words(), "waiting for 'Work'")
        self.assertEqual(words(paused=True), "waiting for 'Work' (paused)")
        self.assertEqual(words(waiting=True), "waiting for 'Work' (waiting too)")
        self.assertEqual(words(kind="verification"), "waiting for a verification of 'Work'")
        check = live(self.job, phase="waiting", kind="verification", run_id="c" * 32,
                     verify={"gave_way": True, "files": 0}, waiting_for=waiting_for(paused=True))
        self.assertEqual(run_panel(check).phase, "gave way, waiting for 'Work' (paused)")
        check.status["verify"]["gave_way"] = False
        self.assertEqual(run_panel(check).phase, "waiting for 'Work' (paused)")

    def test_a_waiting_row_says_the_status_of_what_it_waits_for_cannot_be_read_now(self):
        def words(**other):
            waited = dict(dict(run_id="w" * 32, kind="run", job_name="Work", readable=False), **other)
            return self.panel(phase="waiting", pair_index=0, waiting_for=waited).phase

        self.assertEqual(words(), "waiting for 'Work' (its status cannot be read now)")
        self.assertEqual(words(kind="verification"),
                         "waiting for a verification of 'Work' (its status cannot be read now)")
        self.assertEqual(words(kind="", job_name=""),
                         "waiting for a run, verification or repair whose status cannot be read now")

    def test_a_live_activity_whose_status_cannot_be_read_has_a_row_titled_as_last_read(self):
        unread = {"format": "mirrorhold-run-status", "run_id": "b" * 32}
        last = live(self.job, destination="destination 1 at F:\\M").status
        panels = live_panels([ObservedRun(unread, True, False, last),
                              ObservedRun(dict(unread, run_id="c" * 32), True, False)])
        self.assertEqual([(p.title, p.phase, p.readable, p.fraction, p.counts) for p in panels],
                         [("Photos -> 1  F:\\M", "its status cannot be read now", False, None, ""),
                          ("Run, verification or repair cccccccccccc", "its status cannot be read now", False, None,
                           "")])
        self.assertFalse(any(p.can_pause or p.can_resume or p.can_cancel or p.can_stop_now for p in panels))


class UnreadActivityTest(unittest.TestCase):
    """A live activity whose status cannot be read now: called "a run, verification or
    repair" where the window never read it, counted by its kind in the closing question and
    the status bar, and shown in the job list where the window last read it."""

    UNREAD = {"format": "mirrorhold-run-status", "run_id": "b" * 32}

    def setUp(self):
        self.s, self.d, self.e = volume(1, "S"), volume(2, "F"), volume(3, "K")
        self.job = new_job("Photos", endpoint(self.s, "P"), [endpoint(self.d, "M"), endpoint(self.e, "M")])

    def unread(self, last=None, run_id="b" * 32):
        return ObservedRun(dict(self.UNREAD, run_id=run_id), True, False, last)

    def rows(self, runs):
        markers = {(self.job.job_id, d.marker_id): MarkerState.MATCH for d in self.job.destinations}
        done = [record(self.job, 1, Outcome.COMPLETED), record(self.job, 2, Outcome.COMPLETED)]
        return build_rows(StoreContents((self.job,)), (self.s, self.d, self.e), markers,
                          {self.job.job_id: tuple(done)}, runs)[0]

    def test_one_never_read_is_a_run_verification_or_repair_in_the_runs_tab(self):
        never_read = dict(run_id="w" * 32, kind="", job_name="", readable=False)
        waiting = run_panel(live(self.job, phase="waiting", pair_index=0, waiting_for=never_read))
        self.assertEqual(waiting.phase, "waiting for a run, verification or repair whose status cannot be read now")
        self.assertEqual([p.title for p in live_panels([self.unread(run_id="c" * 32)])],
                         ["Run, verification or repair cccccccccccc"])

    def test_the_closing_question_counts_each_by_the_kind_last_read(self):
        runs = [live(self.job), self.unread(live_repair(self.job).status), self.unread(run_id="c" * 32),
                ObservedRun(live(self.job, run_id="d" * 32).status, False)]
        self.assertEqual(kind_counts(runs), (1, 0, 1, 1), "the dead one is not counted")
        self.assertEqual(closing_question(*kind_counts(runs)),
                         "1 run, 1 repair and 1 run, verification or repair whose status cannot be read now are in "
                         "progress. They continue without this window; reopen MirrorHold to watch or control them.")
        self.assertEqual(closing_question(*kind_counts([self.unread(live_verification(self.job).status)])),
                         "1 verification is in progress. It continues without this window; reopen MirrorHold to "
                         "watch or control it.")
        self.assertEqual(closing_question(*kind_counts([self.unread()])),
                         "1 run, verification or repair whose status cannot be read now is in progress. It continues "
                         "without this window; reopen MirrorHold to watch or control it.")

    def test_the_status_bar_names_each_by_the_kind_last_read(self):
        runs = [live(self.job), self.unread(live_repair(self.job).status), self.unread(run_id="c" * 32),
                ObservedRun(dict(self.UNREAD, run_id="d" * 32), False, False, live_repair(self.job).status)]
        self.assertEqual(unreadable_words(runs),
                         "the status of 1 repair and 1 run, verification or repair cannot be read now",
                         "the dead one is not counted")
        self.assertEqual(unreadable_words([self.unread(live_verification(self.job).status),
                                           self.unread(live(self.job).status, run_id="c" * 32)]),
                         "the status of 1 run and 1 verification cannot be read now")
        self.assertEqual(unreadable_words([live(self.job)]), "")

    def test_the_job_list_shows_a_run_where_it_was_last_read(self):
        last = live(self.job, phase="copying", pair_index=2, finished=[[1, "completed"]]).status
        row = self.rows([self.unread(last)])
        words = "Run in progress - its status cannot be read now"
        self.assertEqual((row.status, row.tone, row.running), (words, Tone.ACTIVE, True))
        self.assertEqual([d.status for d in row.destinations], ["Completed", words], "the one it finished")
        first = self.rows([self.unread(live(self.job, phase="copying", pair_index=1).status)])
        self.assertEqual([d.status for d in first.destinations], [words, words],
                         "its step not known: every destination it has not finished")
        only = self.rows([self.unread(live(self.job, selected=[2]).status)])
        self.assertEqual([d.status for d in only.destinations], ["Completed", words], "only those it serves")

    def test_the_job_list_shows_a_verification_and_a_repair_where_they_were_last_read(self):
        checking = self.rows([self.unread(live_verification(self.job).status)])
        self.assertEqual((checking.status, checking.destinations[0].status, checking.destinations[0].tone),
                         ("Verifying", "Verification in progress - its status cannot be read now", Tone.ACTIVE))
        self.assertFalse(checking.destinations[0].can_verify)
        self.assertEqual(checking.destinations[1].status, "Completed")
        repairing = self.rows([self.unread(live_repair(self.job).status)])
        words = "Repair in progress - its status cannot be read now"
        self.assertEqual((repairing.status, repairing.destinations[0].status, repairing.destinations[0].repairing),
                         (words, words, True))

    def test_the_job_list_shows_nothing_of_one_never_read(self):
        row = self.rows([self.unread()])
        self.assertEqual((row.status, row.running), ("Up to date", False))

    def test_a_run_of_the_job_whose_status_cannot_be_read_never_hides_the_one_at_work(self):
        at_work = live(self.job, phase="copying", pair_index=1)
        waiting = self.unread(live(self.job, phase="waiting", pair_index=0, run_id="w" * 32).status, run_id="w" * 32)
        alone, both = self.rows([at_work]), self.rows([waiting, at_work])
        self.assertNotEqual(alone.status, "Run in progress - its status cannot be read now")
        self.assertEqual((both.status, [d.status for d in both.destinations]),
                         (alone.status, [d.status for d in alone.destinations]))
        queued = live(self.job, phase="waiting", pair_index=0, run_id="q" * 32)
        unread_first = self.rows([queued, self.unread(live(self.job, run_id="u" * 32).status, run_id="u" * 32)])
        self.assertEqual(unread_first.status, "Run in progress - its status cannot be read now",
                         "with none read at work, the one not read may be the one at work")


class HistoryModelTest(unittest.TestCase):
    def setUp(self):
        self.job = new_job("Photos", endpoint(volume(1, "S"), "P"), [endpoint(volume(2, "F"), "M")])

    def test_rows_newest_first_in_words(self):
        records = [record(self.job, 1, Outcome.ABSENT, started="2026-09-27T09:40:00Z"),
                   record(self.job, 1, Outcome.HELD, reasons=("first-run",), started="2026-09-28T09:40:00Z"),
                   record(self.job, 1, Outcome.COMPLETED, started="2026-10-04T20:10:00Z", trigger="unattended",
                          done={"files_copied": 2230, "bytes_copied": 3 * GIB, "moved_to_versions": 1840})]
        rows = history_rows(records, {1: "Backup 1"})
        self.assertEqual([r.result for r in rows], ["Completed", "Waiting for first-run confirmation",
                                                    "Drive not attached"])
        self.assertEqual((rows[0].how, rows[0].copied, rows[0].into_versions, rows[0].took),
                         ("automatic", "2,230 / 3.0 GiB", "1,840", "41 min"))
        self.assertEqual((rows[0].destination, rows[1].how), ("1 Backup 1", "by you"))
        self.assertEqual(rows[2].copied, "-")
        self.assertEqual(len({r.iid for r in rows}), 3)

    def test_details_name_what_was_left_and_where_the_batch_is(self):
        r = record(self.job, 1, Outcome.CANCELLED, reasons=("cancelled-graceful",),
                   batch=".mirrorhold\\versions\\2026-09-28T17-12-04Z", skipped=[("photo-0099.raw", "in use")])
        details = record_details(r, Path("F:\\M"), "Backup 1")
        self.assertEqual(details.summary, "Cancelled by you after the files in flight.")
        self.assertEqual(details.versions_path, Path("F:\\M") / ".mirrorhold\\versions\\2026-09-28T17-12-04Z")
        self.assertEqual(details.table[0][0], "Files to copy")
        self.assertEqual((details.skipped_total, details.engine), (1, PINNED_VERSION))
        self.assertIsNone(record_details(r, None).versions_path)
        self.assertEqual(result_words(record(self.job, 1, Outcome.HELD, reasons=("lost-signatures",))),
                         "Removals held")


class TransferModelTest(unittest.TestCase):
    def setUp(self):
        self.s, self.d = volume(1, "S"), volume(2, "D")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Backup")])

    def test_lines_and_import(self):
        contents = StoreContents((self.job,))
        renamed = replace(self.job, name="Work renamed")
        other = new_job("Other", endpoint(self.s, "Other"), [endpoint(self.d, "OtherBackup")])
        lines = import_lines(contents, (self.job, renamed, other))
        self.assertEqual([l.state for l in lines], [ImportState.SAME, ImportState.DIFFERENT, ImportState.NEW])
        kept = apply_import(contents, (renamed, other), replace_existing=False, volumes=(self.s, self.d), sync_roots=())
        self.assertTrue(kept.ok)
        self.assertEqual((kept.outcome.conflicts, kept.contents.job(self.job.job_id).name),
                         ((self.job.job_id,), "Work"))
        replaced = apply_import(contents, (renamed,), replace_existing=True, volumes=(self.s, self.d), sync_roots=())
        self.assertEqual(replaced.contents.job(self.job.job_id).name, "Work renamed")

    def test_a_refused_job_is_not_imported(self):
        clash = new_job("Clash", endpoint(self.s, "Elsewhere"), [endpoint(self.d, "Backup")])
        result = apply_import(StoreContents((self.job,)), (clash,), replace_existing=False, volumes=(self.s, self.d),
                              sync_roots=())
        self.assertFalse(result.ok)
        self.assertEqual(result.refused[0][0], "Clash")


class StartupAndSessionTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "state" / "store.json"
        self.job = new_job("Work", endpoint(volume(1, "S"), "Work"), [endpoint(volume(2, "D"), "Backup")])

    def test_a_corrupt_file_offers_its_earlier_copies(self):
        store = ConfigStore(self.path)
        store.save(StoreContents((self.job,)))
        store.save(StoreContents((self.job,)))
        self.path.write_text("{ not json", encoding="utf-8")
        session = GuiSession(self.path, volume_source=FixedVolumeSource(()), sync_roots=())
        problem = startup_problem(session.load())
        self.assertEqual(problem.kind, StartupKind.CORRUPT)
        self.assertIn("moved aside, unchanged, to store.corrupt-", problem.text)
        self.assertEqual(len(problem.copies), 1)
        self.assertRegex(problem.copies[0].saved, r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        session.restore(problem.copies[0].path)
        self.assertEqual(session.contents.jobs, (self.job,))

    def test_a_file_of_the_schema_after_the_current_one_opens_read_only(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": SCHEMA_VERSION + 1}),
                             encoding="utf-8")
        session = GuiSession(self.path, volume_source=FixedVolumeSource(()), sync_roots=())
        self.assertEqual(startup_problem(session.load()).kind, StartupKind.READ_ONLY)
        self.assertFalse(session.writable)
        with self.assertRaises(StoreConflict):
            session.save(StoreContents())

    def test_a_save_over_a_change_made_elsewhere_is_refused(self):
        ConfigStore(self.path).save(StoreContents((self.job,)))
        session = GuiSession(self.path, volume_source=FixedVolumeSource(()), sync_roots=())
        self.assertIsNone(startup_problem(session.load()))
        self.assertFalse(session.changed_outside())
        session.save(session.contents.without_job(self.job.job_id))
        self.assertFalse(session.changed_outside())
        ConfigStore(self.path).save(StoreContents((self.job,)))
        self.assertTrue(session.changed_outside())
        with self.assertRaises(StoreConflict):
            session.save(StoreContents())
        session.load()
        self.assertEqual(session.contents.jobs, (self.job,))

    @unittest.skipUnless(sys.platform == "win32", "the run process is started the Windows way")
    def test_a_run_that_cannot_start_says_why(self):
        ConfigStore(self.path).save(StoreContents((self.job,)))
        session = GuiSession(self.path, volume_source=FixedVolumeSource(()), sync_roots=())
        session.load()
        launch = session.start_run("f" * 32)
        launch.process.wait(60)
        problems = session.launch_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("refused", problems[0].message)
        self.assertFalse(launch.output.exists())
        self.assertEqual(session.launch_problems(), [])


class PreferencesModelTest(unittest.TestCase):
    def test_an_engine_is_checked_before_it_is_kept(self):
        def broken(path):
            raise EngineError(f"the sync engine was not found at {path}")
        self.assertEqual(check_engine("X:\\rclone.exe", opener=broken).usable, False)
        self.assertEqual(with_engine(Preferences(), "  D:\\Tools\\rclone.exe ").engine_path, "D:\\Tools\\rclone.exe")
        self.assertIsNone(with_engine(Preferences(engine_path="x"), " ").engine_path)

    @needs_engine
    def test_the_pinned_engine_checks(self):
        result = check_engine(None)
        self.assertTrue(result.usable, result.text)
        self.assertIn(PINNED_VERSION, result.text)


@needs_engine
@unittest.skipUnless(sys.platform == "win32", "the production volume source is Windows-only")
class GuiLivePathTest(TempDirTest):
    """Live path: a job saved through the GUI session on this machine's real volume source
    (read-only; temporary folders on the system drive), run as a core process, previewed,
    confirmed, and read back as job rows and history through the refresher's observe."""

    def wait(self, launch, timeout=120):
        launch.process.wait(timeout)
        return launch.ended()

    def test_save_run_preview_confirm_and_read_back(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src, "a.txt", "a")
        write(src, "sub\\b.txt", "bb")
        session = GuiSession(self.tmp / "state" / "store.json")
        session.load()
        volumes = session.scan().volumes
        draft = JobDraft(name="Live")
        draft.set_source(str(src), volumes)
        draft.add_destination(str(dst), volumes)
        result = check(draft, volumes, session.sync_roots(), session.contents.jobs)
        self.assertTrue(result.can_save, result.checks)
        session.save(session.contents.with_job(result.job))
        job = result.job

        self.assertEqual(self.wait(session.start_run(job.job_id)), 5)          # a first run waits
        self.assertEqual(session.launch_problems(), [])
        records = read_records(session.layout, job.job_id)
        self.assertEqual([r.outcome for r in records], [Outcome.HELD])
        observation = observe(session.contents, session.layout, session.volume_source())
        row = build_rows(session.contents, observation.volumes, observation.markers, observation.records, ())[0]
        self.assertEqual(row.destinations[0].status, "First run: confirm")

        worker = PreviewWorker(session, job.job_id).start()
        worker.join(60)
        self.assertEqual(worker.state, WorkerState.DONE, worker.error)
        summary = summarise(worker.result)
        self.assertEqual(summary.pairs[0].state, PairState.FIRST_RUN)
        self.assertEqual(self.wait(session.start_run(job.job_id, ticket_id=summary.ticket_id)), 0)
        self.assertTrue((dst / "sub" / "b.txt").is_file())

        observation = observe(session.contents, session.layout, session.volume_source(), observation,
                              reread_markers=True)
        row = build_rows(session.contents, observation.volumes, observation.markers, observation.records, ())[0]
        self.assertEqual((row.status, row.destinations[0].status), ("Up to date", "Completed"))
        self.assertTrue(row.destinations[0].can_open)
        history = history_rows(observation.records[job.job_id], {1: "dst"})
        self.assertEqual([h.result for h in history], ["Completed", "Waiting for first-run confirmation"])
        self.assertTrue(job_has_run(observation.records[job.job_id]))


class WhenToRunTest(TempDirTest):
    """The editor's *When to run*: a new job runs only when started; automatic runs are
    additions, and pausing keeps them."""

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        (self.vol_s / "Work").mkdir(parents=True)
        self.vol_d.mkdir()
        self.volumes = (volume(1, mounts=(str(self.vol_s) + "\\",)), volume(2, mounts=(str(self.vol_d) + "\\",)))

    def draft(self):
        draft = JobDraft(name="Work")
        draft.set_source(str(self.vol_s / "Work"), self.volumes)
        draft.add_destination(str(self.vol_d / "Backup"), self.volumes)
        return draft

    def test_a_new_job_runs_only_when_started(self):
        job = check(self.draft(), self.volumes, (), ()).job
        self.assertEqual((job.triggers, job.enabled), (Triggers(False, None), True))
        self.assertFalse(self.draft().automatic_chosen)

    def test_automatic_runs_are_chosen_and_paused(self):
        draft = self.draft()
        draft.on_arrival, draft.scheduled, draft.days_kind = True, True, WEEKLY
        draft.schedule_time_text, draft.schedule_days = "7:30", {3, 0}
        self.assertTrue(draft.automatic_chosen)
        job = check(draft, self.volumes, (), ()).job
        self.assertEqual((job.triggers, job.enabled), (Triggers(True, "weekly mon,thu 07:30"), True))
        draft.paused = True
        job = check(draft, self.volumes, (), ()).job
        self.assertEqual((job.triggers, job.enabled), (Triggers(True, "weekly mon,thu 07:30"), False))
        again = JobDraft.from_job(job, source_editable=True)
        self.assertEqual((again.on_arrival, again.scheduled, again.days_kind, again.schedule_time_text,
                          again.schedule_days, again.paused), (True, True, WEEKLY, "07:30", {0, 3}, True))
        draft.days_kind = DAILY
        self.assertEqual(check(draft, self.volumes, (), ()).job.triggers.schedule, "daily 07:30")

    def test_a_schedule_needs_a_time_and_a_day(self):
        draft = self.draft()
        draft.scheduled, draft.schedule_time_text = True, "9pm"
        self.assertIn("input-schedule-time", [c.code for c in check(draft, self.volumes, (), ()).checks])
        draft.schedule_time_text, draft.days_kind = "21:00", WEEKLY
        result = check(draft, self.volumes, (), ())
        self.assertIn("input-schedule-days", [c.code for c in result.checks])
        self.assertFalse(result.can_save)
        draft.scheduled = False
        self.assertTrue(check(draft, self.volumes, (), ()).can_save)


class ScheduleFormsEditorTest(TempDirTest):
    """*When to run*'s schedule forms: monthly days, several times a day and every few hours
    in a window, each saved in its canonical text and shown again as it was saved."""

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        (self.vol_s / "Work").mkdir(parents=True)
        self.vol_d.mkdir()
        self.volumes = (volume(1, mounts=(str(self.vol_s) + "\\",)), volume(2, mounts=(str(self.vol_d) + "\\",)))

    def scheduled(self, **choices):
        draft = JobDraft(name="Work")
        draft.set_source(str(self.vol_s / "Work"), self.volumes)
        draft.add_destination(str(self.vol_d / "Backup"), self.volumes)
        draft.scheduled = True
        for name, value in choices.items():
            setattr(draft, name, value)
        return draft

    def saved(self, draft):
        result = check(draft, self.volumes, (), ())
        self.assertTrue(result.can_save, [c.text for c in result.checks])
        return result.job

    def test_each_form_is_saved_and_shown_again(self):
        cases = (({"schedule_time_text": "21:00, 9:00"}, "daily 09:00,21:00"),
                 ({"days_kind": MONTHLY, "month_day_text": "15", "schedule_time_text": "22:00"}, "monthly 15 22:00"),
                 ({"days_kind": MONTHLY, "month_day_text": LAST_DAY_CHOICE}, "monthly last 21:00"),
                 ({"repeating": True, "every_text": "2", "window_on": True, "window_start_text": "8:00",
                   "window_end_text": "23:00"}, "daily every 2h 08:00-23:00"),
                 ({"days_kind": WEEKLY, "schedule_days": {0, 1, 2, 3, 4}, "repeating": True, "every_text": "1",
                   "window_on": True, "window_start_text": "09:00", "window_end_text": "18:00"},
                  "weekly mon,tue,wed,thu,fri every 1h 09:00-18:00"),
                 ({"repeating": True, "every_text": "3"}, "daily every 3h"))
        for choices, text in cases:
            job = self.saved(self.scheduled(**choices))
            self.assertEqual(job.triggers.schedule, text)
            again = JobDraft.from_job(job, source_editable=True)
            self.assertEqual(self.saved(again).triggers.schedule, text)

    def test_a_job_shows_its_schedule_in_the_controls(self):
        job = self.saved(self.scheduled())
        shown = JobDraft.from_job(replace(job, triggers=Triggers(False, "monthly last every 2h 22:00-06:00")),
                                  source_editable=True)
        self.assertEqual((shown.scheduled, shown.days_kind, shown.month_day_text, shown.repeating, shown.every_text,
                          shown.window_on, shown.window_start_text, shown.window_end_text),
                         (True, MONTHLY, LAST_DAY_CHOICE, True, "2", True, "22:00", "06:00"))
        listed = JobDraft.from_job(replace(job, triggers=Triggers(False, "weekly sat 08:00,20:00")),
                                   source_editable=True)
        self.assertEqual((listed.days_kind, listed.schedule_days, listed.repeating, listed.schedule_time_text),
                         (WEEKLY, {5}, False, "08:00, 20:00"))

    def test_what_a_schedule_refuses(self):
        def codes(draft):
            return [c.code for c in check(draft, self.volumes, (), ()).checks if c.level == REFUSED]
        self.assertIn("input-schedule-time", codes(self.scheduled(schedule_time_text="09:00, nine")))
        self.assertIn("input-schedule-time", codes(self.scheduled(schedule_time_text=", ")))
        many = ", ".join(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 20, 40))
        refused = check(self.scheduled(schedule_time_text=many), self.volumes, (), ()).checks
        self.assertEqual([(c.code, c.text) for c in refused if c.level == REFUSED],
                         [("input-schedule", "A schedule holds at most 48 times a day; for more, run every N hours.")])
        self.assertIn("input-schedule-every", codes(self.scheduled(repeating=True, every_text="13")))
        self.assertIn("input-schedule-window", codes(self.scheduled(repeating=True, window_on=True,
                                                                    window_start_text="08:00",
                                                                    window_end_text="08:00")))
        self.assertIn("input-schedule-month-day", codes(self.scheduled(days_kind=MONTHLY, month_day_text="30")))
        # A choice the form does not use is kept but not checked.
        self.assertEqual(self.saved(self.scheduled(repeating=True, schedule_time_text="nine", window_on=False,
                                                   window_start_text="x")).triggers.schedule, "daily every 2h")
        self.assertEqual(self.saved(self.scheduled(month_day_text="30")).triggers.schedule, "daily 21:00")


class PreferencesAutomationTest(TempDirTest):
    def copy(self, store):
        return InstalledCopy(self.tmp / "installed", "1.0.0", "2026-10-06T12:00:00+00:00", store,
                             Path("X:\\env\\pythonw.exe"), self.tmp)

    def test_the_installation_in_words(self):
        store = self.tmp / "store.json"
        view = installation_view(InstallationState(None, False, False))
        self.assertEqual((view.can_install, view.can_uninstall, view.install_label), (True, False, "Install"))
        self.assertIn("jobs run only when you start them", view.text)
        view = installation_view(InstallationState(self.copy(store), False, True))
        self.assertEqual((view.can_install, view.can_uninstall), (True, True))
        self.assertIn("Installed: version 1.0.0", view.text)
        view = installation_view(InstallationState(self.copy(self.tmp / "other.json"), False, False))
        self.assertEqual((view.can_install, view.can_uninstall), (False, True))
        view = installation_view(InstallationState(self.copy(store), True, True))
        self.assertEqual((view.can_install, view.can_uninstall), (False, False))
        self.assertIn("from the working copy", view.text)
        self.assertIn("need MirrorHold installed", automation_words(InstallationState(None, False, False)))
        self.assertEqual("", automation_words(InstallationState(self.copy(store), False, True)))

    def test_notification_choices_are_kept(self):
        preferences = with_notifications(Preferences(engine_path="x"), on_completion=False, on_hold=True,
                                         on_failure=True)
        self.assertEqual((preferences.notifications, preferences.engine_path),
                         (Notifications(False, True, True), "x"))

    def test_a_save_reconciles_the_tasks_and_a_failure_is_told(self):
        from test_automation import MemoryScheduler
        path = self.tmp / "state" / "store.json"
        root = self.tmp / "installed"
        root.mkdir()
        write_installation(self.copy(path))
        job = new_job("Work", endpoint(volume(1, "S"), "Work"), [endpoint(volume(2, "D"), "Backup")],
                      triggers=Triggers(True))
        scheduler = MemoryScheduler()
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(), scheduler=scheduler,
                             install_root=root)
        session.load()
        session.save(StoreContents((job,)))
        self.assertEqual(session.automation.state, AutomationState.APPLIED)
        self.assertEqual(list(scheduler.xml), [f"{job.job_id}-arrival"])
        self.assertIsNone(automation_problem(session.automation))
        self.assertTrue(session.installation().serves_store)
        failing = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(),
                             scheduler=MemoryScheduler(fail=True), install_root=root)
        failing.load()
        failing.save(StoreContents())
        self.assertIn("the scheduled tasks could not be updated", automation_problem(failing.automation))

    def test_history_names_what_started_a_run(self):
        job = new_job("Work", endpoint(volume(1, "S"), "Work"), [endpoint(volume(2, "D"), "Backup")])
        self.assertEqual([started_by(record(job, 1, Outcome.COMPLETED, trigger=t))
                          for t in ("manual", "unattended", "arrival", "schedule")],
                         ["by you", "automatic", "drive attached", "on schedule"])


class PortableWordsTest(TempDirTest):
    """A portable release's installation in words: the Preferences section, the editor's
    line, the question as it opens while another copy is set up, *About* and what a setup
    says once done."""

    def setUp(self):
        super().setUp()
        self.here = Release(self.tmp / "MirrorHold 2026.10.2", "2026.10.2")
        self.here.root.mkdir()
        (self.here.root / "build.json").write_text("{}", encoding="utf-8")
        self.store = self.tmp / "local" / "mirrorhold.json"

    def record(self, root, label="2026.10.2"):
        return InstalledCopy(root, "1.0.0", "2026-10-11T12:05:00+00:00", self.store, Path("X:\\pythonw.exe"), None,
                             InstallKind.PORTABLE, label)

    def state(self, copy=None, blocker=None):
        return InstallationState(copy, False, copy is not None, self.here, blocker, self.store.parent)

    def test_the_section_by_state(self):
        view = installation_view(self.state())
        self.assertEqual((view.title, view.install_label, view.uninstall_label, view.can_install, view.can_uninstall),
                         ("Automatic runs and Start menu", "Set up", "Remove", True, False))
        self.assertIn("runs from this folder without installing", view.text)
        self.assertIn(f"kept in {self.store.parent}", view.text)
        self.assertIn("Delete this folder afterwards", view.note)
        view = installation_view(self.state(self.record(self.here.root)))
        self.assertEqual((view.install_label, view.can_install, view.can_uninstall), ("Set up again", True, True))
        self.assertIn("use this copy of MirrorHold (release 2026.10.2)", view.text)
        self.assertIn("Keep this folder where it is", view.text)
        older = self.tmp / "MirrorHold 2026.9.1"
        older.mkdir()
        (older / "build.json").write_text("{}", encoding="utf-8")
        view = installation_view(self.state(self.record(older, "2026.9.1")))
        self.assertEqual((view.install_label, view.can_install, view.can_uninstall), ("Set up here", True, True))
        self.assertIn(f"use the copy of MirrorHold at {older} (release 2026.9.1)", view.text)
        gone = self.tmp / "gone"
        view = installation_view(self.state(self.record(gone, "2026.9.1")))
        self.assertIn(f"the copy of MirrorHold they use is gone ({gone})", view.text)
        source = InstalledCopy(self.tmp / "installed", "1.0.0", "t", self.tmp / "work.json", Path("X:\\pythonw.exe"),
                               self.tmp / "work")
        view = installation_view(self.state(blocker=source))
        self.assertEqual((view.can_install, view.can_uninstall), (False, False))
        self.assertIn(f"MirrorHold installed from source ({self.tmp / 'work'})", view.text)

    def test_the_section_names_the_program_by_its_own_name(self):
        self.assertIn("and to add MirrorHold to the Start menu.", installation_view(self.state()).text)

    def test_a_working_copy_while_a_release_is_set_up(self):
        blocker = self.record(self.here.root)
        state = InstallationState(None, False, False, None, blocker, self.tmp)
        view = installation_view(state)
        self.assertEqual((view.title, view.can_install, view.can_uninstall), ("Installation", False, False))
        self.assertIn(f"use the copy of MirrorHold at {self.here.root} (release 2026.10.2)", view.text)
        self.assertIn("serves another job file", automation_words(state))

    def test_the_editor_line_and_the_question_at_start(self):
        self.assertIn("need MirrorHold set up", automation_words(self.state()))
        self.assertEqual("", automation_words(self.state(self.record(self.here.root))))
        self.assertIn("is gone", automation_words(self.state(self.record(self.tmp / "gone"))))
        self.assertIsNone(set_up_here_question(self.state()))
        self.assertIsNone(set_up_here_question(self.state(self.record(self.here.root))))
        older = self.tmp / "older"
        older.mkdir()
        (older / "build.json").write_text("{}", encoding="utf-8")
        self.assertEqual(set_up_here_question(self.state(self.record(older, "2026.9.1"))),
                         f"Automatic runs and the Start menu entry use MirrorHold 2026.9.1 at {older}. Use this "
                         "copy (release 2026.10.2) instead?")
        self.assertTrue(set_up_here_question(self.state(self.record(self.tmp / "gone"))).startswith(
            f"Automatic runs cannot start: the copy of MirrorHold they use is gone ({self.tmp / 'gone'})."))
        self.assertIsNone(set_up_here_question(InstallationState(None, False, False)))

    def test_about_and_what_a_setup_says(self):
        self.assertEqual(about_title(self.here), "MirrorHold 2026.10.2")
        self.assertEqual(about_title(None), "MirrorHold, run from source")
        self.assertEqual(about_title(Release(self.here.root, None)), "MirrorHold, release unknown")
        outcome = AutomationOutcome(AutomationState.APPLIED, ReconcileResult(("a", "b")))
        done = install_done(InstallResult(self.record(self.here.root), None, outcome))
        self.assertEqual(done, "This copy is set up (release 2026.10.2). Automatic runs: 2 scheduled tasks "
                               "(2 registered, 0 removed).")
        source = InstalledCopy(self.tmp, "1.0.0", "t", self.store, Path("X:\\pythonw.exe"), self.tmp)
        self.assertTrue(install_done(InstallResult(source, None, outcome, ("entry",))).startswith(
            "Installed version 1.0.0."))


class AboutTest(TempDirTest):
    """*About* in words: the copyright line as the licence file shipped beside the packages
    writes it, and a copy without that file."""

    def test_the_licence_this_copy_ships_names_offcutworks(self):
        content = about_content(None, licence_path())
        self.assertEqual(content.title, "MirrorHold, run from source")
        self.assertRegex(content.copyright, r"^Copyright \(c\) \d{4} offcutworks$")
        self.assertTrue(content.licence.startswith("MIT License"))

    def test_the_copyright_line_is_shown_as_the_licence_writes_it(self):
        licence = self.tmp / "LICENSE"
        licence.write_text("MIT License\n\nCopyright (c) 2031 someone else\n\nPermission is hereby granted\n",
                           encoding="utf-8")
        content = about_content(Release(self.tmp, "2031.1.1"), licence)
        self.assertEqual((content.title, content.copyright),
                         ("MirrorHold 2031.1.1", "Copyright (c) 2031 someone else"))
        self.assertEqual(content.licence, licence.read_text(encoding="utf-8"))
        self.assertIsNone(copyright_line("MIT License\n\nPermission is hereby granted\n"))

    def test_a_copy_without_its_licence_says_so_and_offers_none(self):
        content = about_content(Release(self.tmp, None), self.tmp / "LICENSE")
        self.assertEqual((content.title, content.copyright, content.licence),
                         ("MirrorHold, release unknown", MISSING_LICENCE, None))

    def test_a_release_offers_the_third_party_notices_it_ships(self):
        notices = notices_path(self.tmp)
        notices.write_text("Third-party notices for MirrorHold 2031.1.1\n", encoding="utf-8")
        content = about_content(Release(self.tmp, "2031.1.1"), licence_path(), notices)
        self.assertEqual(content.notices, "Third-party notices for MirrorHold 2031.1.1\n")

    def test_a_copy_that_ships_no_notices_offers_none(self):
        self.assertIsNone(about_content(None, licence_path(), notices_path(self.tmp)).notices)
        self.assertIsNone(about_content(None, licence_path()).notices)


class ShippedTest(TempDirTest):
    """What a copy ships beside its packages for the window: found at the copy's root, and
    read as text or not at all."""

    def test_each_file_is_found_at_the_copys_root(self):
        self.assertEqual((notices_path(self.tmp), icon_path(self.tmp)),
                         (self.tmp / NOTICES_FILE_NAME, self.tmp / ICON_FILE_NAME))
        self.assertEqual(licence_path(self.tmp), self.tmp / "LICENSE")

    def test_a_missing_file_or_one_that_is_not_text_reads_as_none(self):
        self.assertIsNone(read_text(self.tmp / NOTICES_FILE_NAME))
        (self.tmp / NOTICES_FILE_NAME).write_bytes(b"\xff\xfe\xfa not text")
        self.assertIsNone(read_text(self.tmp / NOTICES_FILE_NAME))
        (self.tmp / NOTICES_FILE_NAME).write_text("Notices ©\n", encoding="utf-8")
        self.assertEqual(read_text(self.tmp / NOTICES_FILE_NAME), "Notices ©\n")


@unittest.skipUnless(sys.platform == "win32", "an application identity is Windows'")
class IdentityTest(unittest.TestCase):
    """The GUI's process takes the application identity its Start menu entry carries. It is
    claimed in a process of its own: the identity is the whole process's, and the module
    claiming it imports the toolkit."""

    def test_the_process_takes_the_identity_of_the_start_menu_entry(self):
        script = ("import ctypes\n"
                  "from mirrorhold_gui.common_view import claim_identity\n"
                  "claim_identity()\n"
                  "found = ctypes.c_wchar_p()\n"
                  "ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(found))\n"
                  "print(found.value)\n")
        done = subprocess.run([sys.executable, "-c", script], cwd=PROJECT, capture_output=True, text=True,
                              timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, APP_ID), done.stderr)


class NotBackedUpTest(unittest.TestCase):
    def setUp(self):
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.live_read = SourceRead(ReadMode.LIVE, "S:", "snapshot-would-displace", "11 snapshots made by Windows")

    def completed(self, unbacked, started="2026-10-04T20:10:00Z"):
        skipped = [(path, "could not be read now (in use)") for path, _n in unbacked]
        return replace(record(self.job, 1, Outcome.COMPLETED, skipped=skipped, started=started),
                       source_read=self.live_read, not_backed_up=tuple(unbacked))

    def rows(self, records):
        return build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: tuple(records)}, ())[0]

    def test_files_not_backed_up_twice_turn_the_status_to_attention(self):
        row = self.rows([self.completed([("Mail\\inbox.pst", 3), ("db\\ledger.db", 2), ("draft.docx", 1)])])
        first = row.destinations[0]
        self.assertEqual((first.status, first.tone, first.repeated), ("Completed - 2 files not backed up",
                                                                      Tone.ATTENTION, 2))
        self.assertEqual((row.status, row.tone), ("Files not backed up", Tone.ATTENTION))
        self.assertTrue(first.has_unbacked)
        once = self.rows([self.completed([("draft.docx", 1)])])
        self.assertEqual((once.destinations[0].status, once.status), ("Completed", "Up to date"))
        self.assertTrue(once.destinations[0].has_unbacked)
        self.assertFalse(self.rows([record(self.job, 1, Outcome.COMPLETED)]).destinations[0].has_unbacked)

    def test_the_report_lists_most_runs_first_and_says_how_many_went_unbacked_up_twice(self):
        report = unbacked_report(self.completed([("draft.docx", 1), ("Mail\\inbox.pst", 3), ("db\\ledger.db", 2)]),
                                 "Work")
        self.assertEqual(report.title, "Not backed up: Work, destination 1")
        self.assertEqual(report.summary[1], "2 of them have not been backed up for 2 runs or more in a row.")
        self.assertTrue(report.summary[0].startswith("3 files were not backed up by the last run ("))
        self.assertEqual([(r.path, r.why, r.runs) for r in report.rows],
                         [("Mail\\inbox.pst", "could not be read now (in use)", "3"),
                          ("db\\ledger.db", "could not be read now (in use)", "2"),
                          ("draft.docx", "could not be read now (in use)", "1")])
        single = unbacked_report(self.completed([("draft.docx", 1)]), "Work")
        self.assertEqual(single.summary, ("1 file was not backed up by the last run (" +
                                          formatting.local_time("2026-10-04T20:10:00Z") + ").",))

    def test_a_runs_details_say_how_the_source_was_read(self):
        details = record_details(self.completed([("Mail\\inbox.pst", 3), ("draft.docx", 1)]), None)
        self.assertEqual(details.source, "Source: read as it is - the drive keeps Windows restore points or earlier "
                                         "versions, which a snapshot could make Windows delete")
        self.assertEqual(details.skipped[0], ("Mail\\inbox.pst", "could not be read now (in use); 3 runs in a row"))
        self.assertEqual(details.skipped[1], ("draft.docx", "could not be read now (in use)"))
        taken = SourceRead(ReadMode.SNAPSHOT, "D:", taken="2026-10-04T20:10:04Z")
        self.assertRegex(source_words(taken), r"^Source: read from a snapshot of D: taken \d\d:\d\d:04$")
        denied = SourceRead(ReadMode.LIVE, "D:", "snapshot-denied", "access was denied")
        self.assertEqual(source_words(denied),
                         "Source: read as it is - a snapshot could not be made (administrator rights are needed)")
        self.assertEqual(source_words(None), "")

    def test_the_runs_tab_names_the_snapshots_phases(self):
        self.assertEqual(run_panel(live(self.job, phase="taking a snapshot", pair_index=0)).phase,
                         "taking a snapshot of the source")
        self.assertEqual(run_panel(live(self.job, phase="removing the snapshot")).phase, "removing the snapshot")


def verification(job, index=1, state=VerificationState.COMPLETED, *, differ=(), ended="2026-09-01T10:00:00Z",
                 verification_id="v" * 32, compared=3):
    record = VerificationRecord(verification_id, job.job_id, job.name, job.destinations[index - 1].marker_id, index,
                                "2026-09-01T09:00:00Z", ended, state,
                                ended if state is VerificationState.COMPLETED else None, files_total=compared + 2,
                                compared_files=compared, compared_bytes=3 * 1024, seconds=61.0)
    for path, size in differ:
        record.add_differ(path, size)
    return record


def live_verification(job, *, phase="verifying", paused=False, **verify):
    progress = {"files": 10, "bytes": 10 * GIB, "files_done": 4, "bytes_done": 4 * GIB, "differ": 0,
                "listing": None, "gave_way": False, "estimate": None}
    progress.update(verify)
    return live(job, phase=phase, paused=paused, kind="verification", verify=progress, run_id="c" * 32,
                marker_id=job.destinations[0].marker_id)


class MaintenanceRowsTest(unittest.TestCase):
    """Maintenance's columns and statuses in the job list: free space, the verification and
    its reminder, copies found to differ, a verification in progress."""

    NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.s = volume(1, "S", label="Media")
        self.d = volume(2, "F", label="Backup 1", free=400 * GIB, capacity=1000 * GIB)
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.done = record(self.job, 1, Outcome.COMPLETED, ended="2026-06-01T10:00:00Z")

    def rows(self, *, volumes=None, records=None, checks=(), runs=()):
        return build_rows(StoreContents((self.job,)), volumes or (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: tuple([self.done] if records is None else records)}, runs,
                          {self.job.job_id: tuple(checks)}, self.NOW)[0]

    def test_free_space_and_the_reminder_in_their_columns(self):
        first = self.rows(checks=[verification(self.job)]).destinations[0]
        self.assertEqual((first.free, first.free_low), ("400.0 GiB (40 %)", False))
        self.assertRegex(first.verified, r"^2026-09-0\d$")
        self.assertEqual((first.verification_due, first.status, first.tone), (False, "Completed", Tone.NORMAL))
        low = replace(self.d, free_bytes=50 * GIB)
        due = self.rows(volumes=(self.s, low)).destinations[0]
        self.assertEqual((due.free, due.free_low), ("50.0 GiB (5 %) low", True))
        self.assertEqual((due.verified, due.verification_due), ("never - due", True))
        self.assertEqual((due.status, due.tone), ("Completed", Tone.ATTENTION))
        fresh = self.rows(records=[record(self.job, 1, Outcome.COMPLETED, ended="2026-10-01T10:00:00Z")])
        self.assertEqual((fresh.destinations[0].verified, fresh.destinations[0].verification_due), ("never", False))
        absent = self.rows(volumes=(self.s,)).destinations[0]
        self.assertEqual((absent.free, absent.can_verify), ("", False))

    def test_copies_that_differ_are_a_problem_until_a_clean_verification(self):
        found = verification(self.job, differ=[("photo-0001.raw", 4096)])
        row = self.rows(checks=[found])
        self.assertEqual((row.destinations[0].status, row.destinations[0].tone),
                         ("Copies differ: see verification", Tone.PROBLEM))
        self.assertEqual(row.destinations[0].differ, 1)
        self.assertEqual(row.status, "Copies differ: see verification")
        clean = verification(self.job, verification_id="w" * 32, ended="2026-09-20T10:00:00Z")
        self.assertEqual(self.rows(checks=[found, clean]).destinations[0].status, "Completed")
        held = record(self.job, 1, Outcome.HELD, reasons=("above-threshold",))
        self.assertEqual(self.rows(records=[held], checks=[found]).destinations[0].status, "Removals held: review")

    def test_a_verification_in_progress(self):
        row = self.rows(runs=[live_verification(self.job)])
        first = row.destinations[0]
        self.assertEqual((first.status, first.tone, first.verifying, first.can_verify),
                         ("Verifying 40 %", Tone.ACTIVE, True, False))
        self.assertEqual((row.status, row.running), ("Verifying", False))
        waiting = self.rows(runs=[live_verification(self.job, phase="waiting")]).destinations[0]
        self.assertEqual(waiting.status, "Waiting to verify")
        self.assertTrue(self.rows().destinations[0].can_verify)
        self.assertFalse(self.rows(volumes=(self.d,)).destinations[0].can_verify)


class VerifyModelTest(unittest.TestCase):
    def setUp(self):
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.row = build_rows(StoreContents((self.job,)), (self.s, self.d), {key: MarkerState.MATCH},
                              {self.job.job_id: (record(self.job, 1, Outcome.COMPLETED),)}, ())[0].destinations[0]

    def test_the_dialog_offers_to_continue_a_stopped_pass(self):
        fresh = verify_dialog("Photos", self.row, 90, None)
        self.assertEqual(fresh.title, 'Verify: Photos, destination 1 "Backup 1"')
        self.assertEqual(fresh.resume, "")
        self.assertTrue(fresh.last.startswith("Last full verification: never"))
        self.assertTrue(fresh.last.endswith("Reminder every 90 days."))
        stopped = verification(self.job, state=VerificationState.STOPPED, compared=12)
        dialog = verify_dialog("Photos", self.row, 90, stopped)
        self.assertRegex(dialog.resume, r"^Continue the full verification stopped .+ \(12 of 14 files\)$")

    def test_results_and_details_in_words(self):
        found = verification(self.job, differ=[("2024\\photo-0001.raw", 4096)])
        self.assertEqual(verify_result_words(found), "Verified: 1 differs")
        self.assertEqual(verify_result_words(verification(self.job)), "Verified: no differences")
        running = verification(self.job, state=VerificationState.RUNNING)
        self.assertEqual(verify_result_words(running), "Verification interrupted")
        self.assertEqual(verify_result_words(running, {"v" * 32}), "Verifying")
        details = verification_details(found, None, "Backup 1")
        self.assertEqual(details.summary, "Finished: 3 files compared (3.0 KiB) in 1 min.")
        self.assertEqual(details.differ_line, "1 file differs from the source although its size and date are the "
                                              "same:")
        self.assertEqual([(f.path, f.size) for f in details.differ], [("2024\\photo-0001.raw", 4096)])
        self.assertTrue(details.advice.startswith("MirrorHold cannot tell which copy is damaged"))
        stopped = verification(self.job, state=VerificationState.STOPPED)
        stopped.changed_total, stopped.unreadable_total, stopped.extra_total = 12, 2, 1
        stopped.unreadable.append(("Mail\\inbox.pst", "the source copy could not be read: in use"))
        words = verification_details(stopped, None)
        self.assertEqual(words.summary, "Stopped by you: 3 of 5 files compared; Verify... continues from there.")
        self.assertEqual(words.not_compared, "Not compared: 12 changed since the last run (the next run copies them); "
                                             "2 could not be read. 1 file only on the destination: the next run moves "
                                             "it into versions.")
        self.assertEqual(words.advice, "")

    def test_the_history_lists_verifications_among_runs_newest_first(self):
        runs = (record(self.job, 1, Outcome.COMPLETED, started="2026-08-01T10:00:00Z"),
                record(self.job, 1, Outcome.COMPLETED, started="2026-10-01T10:00:00Z"))
        entries = history_entries(runs, (verification(self.job, differ=[("a", 1)]),), {1: "Backup 1"})
        self.assertEqual([row.result for row, _ in entries], ["Completed", "Verified: 1 differs", "Completed"])
        row = entries[1][0]
        self.assertEqual((row.iid, row.how, row.copied, row.into_versions, row.skipped, row.took),
                         ("v:" + "v" * 32, "by you", "3 compared / 3.0 KiB", "-", "0", "1 min"))

    def test_a_result_counts_the_copies_that_differ_with_the_verb_in_their_number(self):
        one, two = (verification(self.job, differ=found) for found in ([("a", 1)], [("a", 1), ("b", 2)]))
        self.assertEqual((verify_result_words(one), verify_result_words(two)),
                         ("Verified: 1 differs", "Verified: 2 differ"))
        new_copies = VerificationMode.NEW_COPIES
        self.assertEqual((verify_result_words(replace(one, mode=new_copies)),
                          verify_result_words(replace(two, mode=new_copies))),
                         ("New copies verified: 1 differs", "New copies verified: 2 differ"))

    def test_a_full_pass_over_one_file_says_one_file_in_its_details(self):
        one = verification(self.job, compared=1)
        self.assertEqual(verification_details(one, None, "Backup 1").summary,
                         "Finished: 1 file compared (3.0 KiB) in 1 min.")

    def test_the_runs_tab_shows_a_verification(self):
        panel = run_panel(live_verification(self.job, differ=2, estimate={
            "bytes": 10 * GIB, "shortest": 600.0, "longest": 3000.0, "samples": 0, "low_rate": 30 * 1024 ** 2,
            "high_rate": 150 * 1024 ** 2, "listing_seconds": 60.0}))
        self.assertEqual((panel.phase, panel.verification, panel.can_stop_now), ("verifying", True, False))
        self.assertEqual(panel.fraction, 0.4)
        self.assertTrue(panel.counts.startswith("4 of 10 files   4.0 GiB of 10.0 GiB"))
        self.assertEqual(panel.in_flight, "Differ so far: 2 files")
        self.assertEqual(panel.note, "Expected at start: 10 min to 50 min (default for a USB 3 hard disk, "
                                     "30.0 MiB/s to 150.0 MiB/s); listing took 1 min")
        gave_way = run_panel(live_verification(self.job, phase="waiting", gave_way=True))
        self.assertEqual((gave_way.phase, gave_way.waiting, gave_way.note), ("waiting: a backup runs first", True, ""))
        paused = run_panel(live_verification(self.job, paused=True))
        self.assertEqual((paused.phase, paused.can_resume, paused.can_pause), ("paused", True, False))
        self.assertEqual(run_panel(live(self.job, phase="removing old versions")).phase, "removing old versions")

    def test_the_runs_tab_names_what_each_file_costs_where_the_estimate_counted_files(self):
        mib = 1024 ** 2
        default = {"bytes": 5000 * 4096, "files": 5000, "shortest": 12.63, "longest": 75.65, "samples": 0,
                   "low_rate": 30 * mib, "high_rate": 150 * mib, "low_file_seconds": 0.0025, "high_file_seconds": 0.015,
                   "listing_seconds": 0.2}
        self.assertEqual(run_panel(live_verification(self.job, estimate=default)).note,
                         "Expected at start: 13 s to 1 min (default for a USB 3 hard disk, 30.0 MiB/s to 150.0 MiB/s, "
                         "2.5 ms to 15 ms a file); listing took 0 s")
        history = dict(default, samples=3, low_rate=80 * mib, high_rate=95 * mib)
        self.assertIn("(from this destination's last 3 verifications, 80.0 MiB/s to 95.0 MiB/s, 2.5 ms to 15 ms a "
                      "file)", run_panel(live_verification(self.job, estimate=history)).note)
        empty_files = dict(default, bytes=0, shortest=12.5, longest=75.0)
        self.assertEqual(run_panel(live_verification(self.job, estimate=empty_files)).note,
                         "Expected at start: 12 s to 1 min (default for a USB 3 hard disk, 30.0 MiB/s to 150.0 MiB/s, "
                         "2.5 ms to 15 ms a file); listing took 0 s")
        nothing = dict(default, bytes=0, files=0, shortest=0.0, longest=0.0)
        self.assertEqual(run_panel(live_verification(self.job, estimate=nothing)).note,
                         "Nothing to compare; listing took 0 s")


class VersionsEditorTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        (self.vol_s / "Work").mkdir(parents=True)
        self.vol_d.mkdir()
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="D")

    def draft(self, **settings):
        draft = JobDraft(name="Work", **settings)
        draft.set_source(str(self.vol_s / "Work"), (self.s, self.d))
        draft.add_destination(str(self.vol_d / "Backup"), (self.s, self.d))
        return draft

    def test_a_new_job_keeps_every_version_and_reminds_every_90_days(self):
        job, problems = self.draft().build()
        self.assertEqual((problems, job.version_cleanup.enabled, job.verification_reminder_days), ([], False, 90))
        self.assertIsNone(cleanup_question(job, None))

    def test_deleting_old_versions_is_set_and_asked_about(self):
        job, problems = self.draft(cleanup=True, keep_days_text="30", reminder_days_text="45").build()
        self.assertEqual((problems, job.version_cleanup.keep_days, job.verification_reminder_days), ([], 30, 45))
        self.assertEqual(cleanup_question(job, None), "From the next run on, version batches older than 30 days are "
                                                      "deleted permanently from this job's destinations.")
        edited = JobDraft.from_job(job, source_editable=False)
        self.assertEqual((edited.cleanup, edited.keep_days_text, edited.reminder_days_text), (True, "30", "45"))
        edited.keep_days_text = "60"
        longer, _ = edited.build()
        self.assertIsNone(cleanup_question(longer, job))
        edited.keep_days_text = "7"
        shorter, _ = edited.build()
        self.assertIsNotNone(cleanup_question(shorter, job))
        edited.cleanup = False
        off, _ = edited.build()
        self.assertEqual((off.version_cleanup.enabled, cleanup_question(off, job)), (False, None))
        self.assertEqual(off.job_id, job.job_id)

    def test_numbers_that_are_not_days_are_refused(self):
        _job, problems = self.draft(cleanup=True, keep_days_text="0").build()
        self.assertEqual([p.code for p in problems], ["input-keep-days"])
        _job, problems = self.draft(reminder_days_text="soon").build()
        self.assertEqual([p.code for p in problems], ["input-reminder-days"])
        job, problems = self.draft(cleanup=False, keep_days_text="x").build()
        self.assertEqual((problems, job.version_cleanup.enabled), ([], False))


class MaintenanceDetailsTest(PreviewCase):
    def test_the_preview_names_the_old_versions_a_run_deletes(self):
        self.mirror({"a.txt": "a"})
        write(self.dst / ".mirrorhold" / "versions" / "2020-01-01T00-00-00Z", "old.txt", "12345")
        plain = self.preview()
        self.assertNotIn("Old versions", [q.label for q in plain.pairs[0].quantities])
        summary = self.preview(job=replace(self.job, version_cleanup=VersionCleanup(30)))
        old = next(q for q in summary.pairs[0].quantities if q.label == "Old versions")
        self.assertEqual((old.files, old.size, old.detail),
                         ("1 batch", "5 bytes", "older than 30 days: deleted permanently by this run"))
        self.assertTrue((self.dst / ".mirrorhold" / "versions" / "2020-01-01T00-00-00Z").is_dir())

    def test_a_runs_details_name_the_cleanup_and_the_free_space(self):
        cleaned = VersionsCleaned(90, ("2026-06-01T10-00-00Z",), 3, 40, 12 * GIB)
        details = record_details(replace(record(self.job, 1, Outcome.COMPLETED), versions_cleaned=cleaned,
                                         space=(212 * GIB, 3000 * GIB)), None)
        self.assertEqual(details.cleanup, "Old versions deleted: 3 batches older than 90 days (12.0 GiB)")
        self.assertEqual((details.space, details.space_low),
                         ("Free after the run: 212.0 GiB of 2.9 TiB (7 %) - low", True))
        quiet = record_details(replace(record(self.job, 1, Outcome.COMPLETED), versions_cleaned=VersionsCleaned(90)),
                               None)
        self.assertEqual((quiet.cleanup, quiet.space), ("", ""))


class LargeLibraryWordsTest(PreviewCase):
    """A library whose destination already holds its files: what a run, its details, its
    notice and its preview say while files are compared and found identical; the steps
    before the copy counted; names the copy engine cannot keep, listed."""

    def panel(self, **status):
        return run_panel(live(self.job, destination="destination 1 at F:\\M", **status))

    def test_files_found_identical_count_as_done_and_the_files_compared_are_named(self):
        panel = self.panel(planned={"files_to_copy": 100, "bytes_to_copy": 100 * 1024 ** 2},
                           done={"files_copied": 2, "bytes_copied": 2 * 1024 ** 2, "files_identical": 48,
                                 "bytes_identical": 48 * 1024 ** 2},
                           rate=1024 ** 2, eta_seconds=50, comparing=["sub\\IMG_1.JPG", "IMG_2.JPG", "c", "d", "e"])
        self.assertEqual(panel.counts, "50 of 100 files   50.0 MiB of 100.0 MiB   1.0 MiB/s   about 50 s left")
        self.assertEqual(panel.fraction, 0.5)
        self.assertEqual(panel.in_flight, "Comparing with the backup copy: IMG_1.JPG, IMG_2.JPG, c and 2 more")
        self.assertEqual(panel.note, "48 files were already identical: only their date was updated")
        copying = self.panel(in_flight=[["a.raw", 100, 50]], comparing=["b.raw"])
        self.assertEqual((copying.in_flight, copying.note), ("In flight: a.raw (50 %)", ""))
        self.assertEqual(self.panel(paused=True, comparing=["b.raw"]).in_flight, "")

    def test_the_steps_before_the_copy_say_how_far_they_are(self):
        planning = self.panel(phase="planning", examined=1234, to_examine=20000)
        self.assertEqual(planning.phase, "planning   1,234 of 20,000 changed files read")
        self.assertEqual(self.panel(phase="planning").phase, "planning")
        checking = self.panel(phase="checking changed files", examined=7, to_examine=9)
        self.assertEqual(checking.phase, "checking changed files can be read   7 of 9")
        self.assertTrue(checking.can_pause and checking.can_cancel)

    def test_a_graceful_cancel_with_nothing_in_flight_is_stopping(self):
        self.assertEqual(self.panel(cancelling="graceful").counts, "No file is being copied; stopping")

    def test_a_runs_details_and_notice_count_the_files_found_identical(self):
        r = record(self.job, 1, Outcome.COMPLETED, done={"files_copied": 3, "bytes_copied": 3 * 1024 ** 2,
                                                         "files_identical": 20000, "bytes_identical": 40 * GIB})
        details = record_details(r, None)
        self.assertEqual(details.summary, "Completed. 20,000 files were already identical to their backup copy: only "
                                          "their date was updated.")
        self.assertEqual(details.table[0][2], "20,003")
        self.assertEqual(details.table[1][2], "40.0 GiB")
        self.assertEqual(record_details(record(self.job, 1, Outcome.COMPLETED), None).summary, "Completed.")
        line = notice_for([r], Notifications()).lines[0]
        self.assertEqual(line, "Destination 1: completed - 3 files copied (3.0 MiB), 20,000 already identical, "
                               "0 into versions")

    def test_an_adoption_says_same_size_files_are_compared_and_only_dated(self):
        for i in range(3):
            write(self.src, f"IMG_{i}.JPG", f"photo {i}")
            target = write(self.dst, f"IMG_{i}.JPG", f"photo {i}")
            stamp = os.stat(self.src / f"IMG_{i}.JPG").st_mtime_ns + 3600 * 10 ** 9
            os.utime(target, ns=(stamp, stamp))
        write(self.src, "grown.txt", "longer than before")
        write(self.dst, "grown.txt", "short")
        pair = self.preview().pairs[0]
        self.assertEqual(pair.state, PairState.ADOPTION)
        self.assertIn("4 files are replaced", pair.headline)
        self.assertIn("Of these, 3 have the same size as their copy: they are compared first, and those found "
                      "identical only have their date updated.", pair.headline)
        self.assertEqual(pair.quantities[0].detail, "0 new, 4 changed; 3 changed of the same size are compared "
                                                    "first, and only dated where identical")

    def test_names_the_copy_engine_cannot_keep_are_listed_with_why(self):
        self.mirror({"a.txt": "a"})
        write(self.src, "\u201bquote.jpg", "q")
        write(self.src, "bad\udc80name.jpg", "b")
        pair = self.preview().pairs[0]
        left_out = next(q for q in pair.quantities if q.label == "Left out at the source")
        self.assertEqual((left_out.files, left_out.listing), ("2 files", "skipped"))
        self.assertIn("names this tool cannot keep", left_out.detail)
        self.assertEqual(sorted(f.path for f in pair.lists["skipped"]), ["bad\udc80name.jpg", "\u201bquote.jpg"])
        self.assertTrue(all("copy engine" in f.why for f in pair.lists["skipped"]))
        self.assertEqual(pair.quantities[0].files, "0 files")
        self.assertEqual(pair.headline, "Ready - nothing needs your review.")    # not "matches the source"


class GroupsModelTest(unittest.TestCase):
    """Groups in the job list: the arrangement, a group's status and last success, what a
    selection allows, the questions asked, group names and the groups remembered closed."""

    def setUp(self):
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.camera = new_job("Camera", endpoint(self.s, "Camera"), [endpoint(self.d, "Camera")])
        self.phone = new_job("Phone", endpoint(self.s, "Phone"), [endpoint(self.d, "Phone")])
        self.work = new_job("Work", endpoint(self.s, "Work"), [endpoint(self.d, "Work")])
        self.pictures = new_group("Pictures", (self.phone.job_id, self.camera.job_id))
        self.contents = StoreContents((self.camera, self.phone, self.work), groups=(self.pictures, new_group("Empty")))
        self.markers = {(j.job_id, j.destinations[0].marker_id): MarkerState.MATCH for j in self.contents.jobs}

    def entries(self, records=(), runs=(), markers=None):
        by_job = {}
        for r in records:
            by_job.setdefault(r.job_id, []).append(r)
        rows = build_rows(self.contents, (self.s, self.d), markers or self.markers, by_job, runs)
        return arrange(self.contents, rows)

    def test_groups_come_first_holding_their_jobs_in_the_groups_order(self):
        entries = self.entries()
        self.assertEqual([type(e).__name__ for e in entries], ["GroupRow", "GroupRow", "JobRow"])
        pictures, empty, work = entries
        self.assertEqual(([j.name for j in pictures.jobs], work.name), (["Phone", "Camera"], "Work"))
        self.assertEqual((pictures.label, empty.label), ("Pictures      (2 jobs)", "Empty      (0 jobs)"))
        self.assertEqual((empty.status, empty.tone, empty.last_success), ("No jobs", Tone.NORMAL, ""))
        by_iid = row_by_iid(entries)
        self.assertIs(by_iid[pictures.iid], pictures)
        self.assertIs(by_iid[f"job:{self.camera.job_id}"], pictures.jobs[1])
        self.assertIs(by_iid[pictures.jobs[1].destinations[0].iid], pictures.jobs[1].destinations[0])

    def test_a_groups_status_is_its_most_pressing_jobs_and_its_last_success_its_oldest(self):
        done = [record(self.camera, 1, Outcome.COMPLETED),
                record(self.phone, 1, Outcome.COMPLETED, ended="2026-09-01T10:00:00Z")]
        pictures = self.entries(done)[0]
        self.assertEqual((pictures.status, pictures.tone, pictures.last_success),
                         ("Up to date", Tone.NORMAL, formatting.local_time("2026-09-01T10:00:00Z")))
        running = self.entries(done, runs=[live(self.phone)])[0]
        self.assertEqual((running.status, running.tone), ("Phone: Running", Tone.ACTIVE))
        held = [record(self.camera, 1, Outcome.HELD, reasons=("above-threshold",)), done[1]]
        self.assertEqual((self.entries(held)[0].status, self.entries(held)[0].tone),
                         ("Camera: Needs your review", Tone.ATTENTION))
        ready = self.entries()[0]
        self.assertEqual((ready.status, ready.last_success), ("Ready to run (2 jobs)", "never"))
        cancelled = [record(self.camera, 1, Outcome.CANCELLED), record(self.phone, 1, Outcome.INTERRUPTED)]
        self.assertEqual(self.entries(cancelled)[0].status, "Phone: Interrupted - next run continues (+1)")

    def test_what_a_selection_allows(self):
        entries = self.entries(runs=[live(self.phone)])
        pictures, empty, work = entries
        by_iid = row_by_iid(entries)
        camera = pictures.jobs[1]
        group = Selection.of([pictures], by_iid)
        self.assertEqual((group.single_group, group.single_job, group.removable, group.movable),
                         (pictures, None, (), ()))
        self.assertEqual([j.name for j in group.startable], ["Camera"], "a running job is not started again")
        destination = Selection.of([camera.destinations[0]], by_iid)
        self.assertEqual((destination.single_job, destination.single_destination), (camera, camera.destinations[0]))
        several = Selection.of([work, camera.destinations[0], camera], by_iid)
        self.assertIsNone(several.single_job)
        self.assertEqual([j.name for j in several.reached], ["Work", "Camera"])
        self.assertEqual((several.removable, several.movable), ((), (self.work.job_id, self.camera.job_id)))
        self.assertEqual(Selection.of([work, camera], by_iid).removable, (work, camera))
        self.assertEqual(Selection.of([empty], by_iid).startable, ())
        self.assertEqual(Selection.of([pictures, work], by_iid).movable, ())

    def test_the_questions_before_several_jobs_run_or_go(self):
        markers = dict(self.markers)
        markers[(self.phone.job_id, self.phone.destinations[0].marker_id)] = MarkerState.MISSING
        pictures, _empty, work = self.entries(runs=[live(self.work)], markers=markers)
        plan = run_several(pictures.jobs + (work,))
        self.assertEqual((plan.title, plan.button, plan.start),
                         ("Run 2 jobs?", "Run 2 jobs", (self.phone.job_id, self.camera.job_id)))
        self.assertEqual(plan.text.splitlines(),
                         ["Starts: Phone, Camera.", "Running already, not started again: Work.",
                          "Phone needs your review: its run holds what waits for your confirmation.",
                          "Jobs that use the same drive run one at a time unless it is a solid-state or cloud drive, "
                          "as Windows reports or as set in Preferences; the others run at the same time."])
        one = run_several(pictures.jobs[1:], "Pictures")
        self.assertEqual((one.title, one.text), ('Run 1 job of "Pictures"?', "Starts: Camera."))
        self.assertEqual(delete_group_question(pictures),
                         ("Delete group", 'Delete the group "Pictures"? Its 2 jobs stay, in no group. Nothing changes '
                                          "on any drive."))
        title, text = remove_jobs_question((work, pictures.jobs[1]))
        self.assertEqual(title, "Remove jobs")
        self.assertTrue(text.startswith("Remove 2 jobs: Work, Camera?\nNothing is deleted on any drive"))

    def test_the_question_before_several_jobs_says_what_sharing_a_drive_means_as_chosen(self):
        pictures = self.entries()[0]
        self.assertEqual(run_several(pictures.jobs).text.splitlines()[-1],
                         "Jobs that use the same drive run one at a time unless it is a solid-state or cloud drive, as "
                         "Windows reports or as set in Preferences; the others run at the same time.")
        self.assertEqual(run_several(pictures.jobs, turns=DriveTurns.ONE_AT_A_TIME).text.splitlines()[-1],
                         "Jobs that use the same drive run one at a time; the others run at the same time.")
        self.assertEqual(run_several(pictures.jobs, turns=DriveTurns.AT_THE_SAME_TIME).text.splitlines()[-1],
                         "They run at the same time; jobs that use the same drive share its speed.")

    def test_a_group_or_one_job_moves_within_its_level(self):
        entries = self.entries()
        pictures, _empty, work = entries
        by_iid = row_by_iid(entries)
        phone, camera = pictures.jobs
        down = reordered(self.contents, Selection.of([pictures], by_iid), 1)
        self.assertEqual([g.name for g in down.groups], ["Empty", "Pictures"])
        self.assertIsNone(reordered(self.contents, Selection.of([pictures], by_iid), -1), "the first group")
        through = reordered(self.contents, Selection.of([camera.destinations[0]], by_iid), -1)
        self.assertEqual(through.group(self.pictures.group_id).job_ids, (self.camera.job_id, self.phone.job_id),
                         "a destination moves its job")
        self.assertIsNone(reordered(self.contents, Selection.of([camera], by_iid), 1), "the last job of its group")
        self.assertIsNone(reordered(self.contents, Selection.of([work], by_iid), -1),
                          "a job in no group stays below every group")
        self.assertIsNone(reordered(self.contents, Selection.of([phone, camera], by_iid), 1), "several items")
        self.assertIsNone(reordered(self.contents, Selection.of([], by_iid), 1))

    def test_group_names_and_the_groups_remembered_closed(self):
        self.assertEqual(group_name_problem(self.contents, "  "), "A group needs a name.")
        self.assertEqual(group_name_problem(self.contents, " pictures "), 'A group is already called "Pictures".')
        self.assertEqual(group_name_problem(self.contents, "Pictures", besides=self.pictures.group_id), "")
        self.assertEqual(group_name_problem(self.contents, "x" * 201), "A name has at most 200 characters.")
        self.assertEqual(group_name_problem(self.contents, "Documents"), "")
        state = with_collapsed({"geometry": "1x1"}, {"b", "a"})
        self.assertEqual(state, {"geometry": "1x1", "collapsed_groups": ["a", "b"]})
        self.assertEqual(collapsed_groups(state), {"a", "b"})
        self.assertEqual(with_collapsed(state, ()), {"geometry": "1x1"})
        self.assertEqual(collapsed_groups({"collapsed_groups": "a"}), set())


class StartingAgainTest(TempDirTest):
    """What a cancelled run and an unfinished verification say about starting again, and
    the window's remembered state saved alone."""

    def setUp(self):
        super().setUp()
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)

    def row(self, outcome):
        return build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: (record(self.job, 1, outcome),)}, ())[0]

    def test_a_cancelled_run_says_the_next_run_continues(self):
        destination = self.row(Outcome.CANCELLED).destinations[0]
        self.assertEqual((destination.status, destination.tone), ("Cancelled - next run continues", Tone.NORMAL))

    def test_the_verify_dialog_says_when_the_pass_began_and_what_continuing_keeps(self):
        destination = self.row(Outcome.COMPLETED).destinations[0]
        stopped = verification(self.job, state=VerificationState.STOPPED, compared=12)
        self.assertEqual(verify_dialog("Photos", destination, 90, stopped).resume_note,
                         f"It began {formatting.local_time('2026-09-01T09:00:00Z')}; continued, it counts as a "
                         "verification from then.")
        found = verification(self.job, state=VerificationState.STOPPED, differ=[("a.raw", 1), ("b.raw", 2)])
        self.assertTrue(verify_dialog("Photos", destination, 90, found).resume_note.endswith(
            " The 2 files it found to differ stay listed; start again once you have dealt with them."))
        self.assertEqual(verify_dialog("Photos", destination, 90, None).resume_note, "")

    def test_the_windows_remembered_state_is_saved_alone(self):
        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(StoreContents((self.job,)))
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=())
        session.load()
        session.save_view_state({"collapsed_groups": ["g"]})
        self.assertIsNone(session.automation, "the jobs are unchanged: the scheduled tasks are left alone")
        self.assertFalse(session.changed_outside())
        self.assertEqual(dict(ConfigStore(path).load().contents.preferences.gui_state), {"collapsed_groups": ["g"]})
        self.assertEqual(ConfigStore(path).previous_copies(), ())
        ConfigStore(path).save(StoreContents((self.job,)))
        with self.assertRaises(StoreConflict):
            session.save_view_state({})


class FirstBackupWordsTest(TempDirTest):
    """A first backup into a folder that holds nothing waits only for the user's go-ahead:
    the job list says so, one question asks it — on its own or among several jobs — and
    the run is started carrying it. A folder holding something waits for a review, and one
    whose contents are not known yet for a confirmation, as before."""

    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d, self.vol_e = self.tmp / "volS", self.tmp / "volD", self.tmp / "volE"
        for folder in (self.vol_s / "Photos", self.vol_d, self.vol_e):
            folder.mkdir(parents=True)
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="Media")
        self.d = volume(2, mounts=(str(self.vol_d) + "\\",), label="Backup A")
        self.e = volume(3, mounts=(str(self.vol_e) + "\\",), label="Archive")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Photos"), endpoint(self.e, "Old")])
        self.contents = StoreContents((self.job,))
        self.keys = [(self.job.job_id, d.marker_id) for d in self.job.destinations]

    def row(self, markers, occupancy=None):
        return build_rows(self.contents, (self.s, self.d, self.e), dict(zip(self.keys, markers)), {}, (),
                          occupancy=dict(zip(self.keys, occupancy or ())))[0]

    def test_the_job_list_tells_a_first_backup_from_a_first_run_into_a_folder_holding_something(self):
        missing = MarkerState.MISSING
        row = self.row((missing, missing), (Occupancy.MISSING, Occupancy.OCCUPIED))
        first, second = row.destinations
        self.assertEqual((first.status, first.tone, first.needs_review, first.first_backup),
                         ("First backup: confirm", Tone.ATTENTION, True, True))
        self.assertEqual((second.status, second.tone, second.needs_review, second.first_backup),
                         ("First run: review", Tone.ATTENTION, True, False))
        self.assertEqual((row.first_backups, row.needs_other_review), ((first,), True))
        empty = self.row((missing, MarkerState.MATCH), (Occupancy.EMPTY,)).destinations[0]
        self.assertEqual((empty.status, empty.first_backup), ("First backup: confirm", True))
        for unknown in (None, Occupancy.UNKNOWN):
            told = self.row((missing, MarkerState.MATCH), (unknown,)).destinations[0]
            self.assertEqual((told.status, told.first_backup), ("First run: confirm", False))
        matched = self.row((MarkerState.MATCH, MarkerState.MATCH), (Occupancy.EMPTY,))
        self.assertEqual((matched.destinations[0].status, matched.first_backups), ("Ready to run", ()))

    def test_the_one_question_before_a_jobs_first_backups(self):
        row = self.row((MarkerState.MISSING, MarkerState.MATCH), (Occupancy.MISSING,))
        question = first_backup_question(row)
        self.assertEqual((question.title, question.button), ("Start the first backup: Photos", "Start first backup"))
        source, destination = row.source, row.destinations[0].folder
        self.assertEqual(question.text.splitlines(),
                         ["No backup exists yet in:",
                          f"    {destination}   (the backup creates this folder)",
                          f"MirrorHold copies everything in {source} there. The folder holds nothing yet, so "
                          "nothing in it is moved or replaced.",
                          "Destination 2 runs as usual."])
        mixed = self.row((MarkerState.MISSING, MarkerState.MISSING), (Occupancy.EMPTY, Occupancy.OCCUPIED))
        lines = first_backup_question(mixed).text.splitlines()
        self.assertEqual(lines[1], f"    {mixed.destinations[0].folder}")
        self.assertEqual(lines[-1], f"Destination 2, {mixed.destinations[1].folder}, already holds files: its first "
                                    "run waits for your review (Review and confirm...).")
        self.assertIsNone(first_backup_question(self.row((MarkerState.MISSING, MarkerState.MATCH),
                                                         (Occupancy.OCCUPIED,))))

    def test_a_run_of_one_destination_asks_about_that_destination_alone(self):
        row = self.row((MarkerState.MISSING, MarkerState.MISSING), (Occupancy.MISSING, Occupancy.OCCUPIED))
        self.assertTrue(first_backup_question(row).text.splitlines()[-1].startswith("Destination 2, "),
                        "a run of the whole job names the other destination's review")
        first, second = (d.marker_id for d in row.destinations)
        self.assertEqual(first_backup_question(row.serving((first,))).text.splitlines(),
                         ["No backup exists yet in:",
                          f"    {row.destinations[0].folder}   (the backup creates this folder)",
                          f"MirrorHold copies everything in {row.source} there. The folder holds nothing yet, so "
                          "nothing in it is moved or replaced."])
        self.assertIsNone(first_backup_question(row.serving((second,))),
                          "a folder holding files is reviewed in the preview, its first backup not asked")
        self.assertEqual([d.index for d in row.serving((second,)).destinations], [2])
        self.assertIs(row.serving(None), row)

    def test_several_jobs_confirm_their_first_backups_in_the_one_question(self):
        held = new_job("Docs", endpoint(self.s, "Docs"), [endpoint(self.e, "Docs")])
        self.contents = StoreContents((self.job, held))
        markers = {self.keys[0]: MarkerState.MISSING, self.keys[1]: MarkerState.MATCH,
                   (held.job_id, held.destinations[0].marker_id): MarkerState.MISSING}
        rows = build_rows(self.contents, (self.s, self.d, self.e), markers, {}, (),
                          occupancy={self.keys[0]: Occupancy.EMPTY,
                                     (held.job_id, held.destinations[0].marker_id): Occupancy.OCCUPIED})
        plan = run_several(rows)
        self.assertEqual((plan.start, plan.first_backup), ((self.job.job_id, held.job_id), (self.job.job_id,)))
        folder = rows[0].destinations[0].folder
        self.assertEqual(plan.text.splitlines()[1:3],
                         [f"First backups, into folders that hold nothing yet: Photos ({folder}). "
                          "Starting confirms them.",
                          "Docs needs your review: its run holds what waits for your confirmation."])

    def test_the_refresher_looks_into_a_destination_without_a_marker(self):
        layout = runtime_layout(self.tmp / "state" / "store.json")
        source = FixedVolumeSource((self.s, self.d, self.e))
        (self.vol_e / "Old").mkdir()
        write_marker(self.vol_e / "Old", Marker("x" * 32, "y" * 32))
        first = observe(self.contents, layout, source)
        self.assertEqual(first.occupancy, {self.keys[0]: Occupancy.MISSING})
        self.assertIs(first.markers[self.keys[1]], MarkerState.MISMATCH)
        write(self.vol_d / "Photos", "old.txt", "not ours")
        self.assertEqual(observe(self.contents, layout, source, first).occupancy[self.keys[0]], Occupancy.MISSING,
                         "read again with its marker, not at every look")
        again = observe(self.contents, layout, source, first, reread_markers=True)
        self.assertEqual(again.occupancy[self.keys[0]], Occupancy.OCCUPIED)
        self.assertEqual(self.row((MarkerState.MISSING, MarkerState.MISSING), (again.occupancy[self.keys[0]],))
                         .destinations[0].status, "First run: review")

    def test_the_session_starts_the_run_carrying_the_go_ahead(self):
        started = []

        def starter(store_path, job_id, **options):
            started.append((job_id, options))
            return None

        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(self.contents)
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(), starter=starter)
        session.load()
        session.start_run(self.job.job_id, first_backup=True)
        session.start_run(self.job.job_id, ticket_id="t1")
        self.assertEqual(started, [(self.job.job_id, {"ticket_id": None, "first_backup": True}),
                                   (self.job.job_id, {"ticket_id": "t1", "first_backup": False})])

    def test_the_session_starts_a_run_of_the_destinations_chosen_by_their_markers_across_a_reorder_elsewhere(self):
        started = []

        def starter(store_path, job_id, **options):
            started.append((job_id, options))
            return None

        path = self.tmp / "state" / "store.json"
        store = ConfigStore(path)
        store.save(self.contents)
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(), starter=starter)
        session.load()
        first, second = self.job.destinations
        session.start_run(self.job.job_id, destinations=[second.marker_id], first_backup=True)
        store.save(StoreContents((replace(self.job, destinations=(second, first)),)))   # elsewhere, unseen here
        session.start_run(self.job.job_id, destinations=[second.marker_id])
        chosen = {"ticket_id": None, "destinations": (second.marker_id,)}
        self.assertEqual(started, [(self.job.job_id, dict(chosen, first_backup=True)),
                                   (self.job.job_id, dict(chosen, first_backup=False))],
                         "the same destination after the reorder, never the one now at its former number")
        store.save(StoreContents((replace(self.job, destinations=(first,)),)))
        with self.assertRaises(NotStarted) as raised:
            session.start_run(self.job.job_id, destinations=[second.marker_id])
        self.assertIn("no longer in the job", str(raised.exception))
        self.assertEqual(len(started), 2, "nothing started for a destination the job no longer has")

    def test_the_session_hands_the_run_the_markers_of_the_destinations_chosen(self):
        started = []

        def starter(store_path, job_id, **options):
            started.append((job_id, options))
            return None

        path = self.tmp / "state" / "store.json"
        store = ConfigStore(path)
        store.save(self.contents)
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(), starter=starter)
        session.load()
        first, second = self.job.destinations
        store.save(StoreContents((replace(self.job, destinations=(second, first)),)))   # elsewhere, unseen here
        session.start_run(self.job.job_id, destinations=[second.marker_id], first_backup=True)
        self.assertEqual(started, [(self.job.job_id, {"ticket_id": None, "first_backup": True,
                                                      "destinations": (second.marker_id,)})],
                         "the destination itself, which the run finds wherever the job has put it")
        store.save(StoreContents((replace(self.job, destinations=(first,)),)))
        with self.assertRaises(NotStarted) as raised:
            session.start_run(self.job.job_id, destinations=[second.marker_id])
        self.assertIn("no longer in the job", str(raised.exception))
        self.assertEqual(len(started), 1, "nothing started for a destination the job no longer has")

    def test_the_session_starts_a_verification_and_a_repair_of_a_destination_named_by_its_marker(self):
        verified, repaired = [], []
        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(self.contents)
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(),
                             verifier=lambda store_path, job_id, destination, **options:
                             verified.append((job_id, destination, options)),
                             repairer=lambda store_path, job_id, destination: repaired.append((job_id, destination)))
        session.load()
        marker = self.job.destinations[1].marker_id
        session.start_verification(self.job.job_id, marker, new_copies=True)
        session.start_repair(self.job.job_id, marker)
        self.assertEqual(verified, [(self.job.job_id, marker, {"restart": False, "new_copies": True})])
        self.assertEqual(repaired, [(self.job.job_id, marker)])

    def test_the_session_tells_a_remembered_password_having_looked_again_for_one_being_replaced(self):
        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(self.contents)
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=())
        started = time.monotonic()
        self.assertFalse(session.password_kept())
        self.assertGreaterEqual(time.monotonic() - started, exchange.RENAME_PATIENCE,
                                "a seal not there is looked for again for the moment one is renamed over it")
        session.layout.remote_password.write_bytes(b"sealed")
        self.assertTrue(session.password_kept())


class ShareHoldEditorTest(PreviewCase):
    """The editor's share hold: half by default, adjustable, switched off with its value
    kept; and a preview's words for a job holding at another share than half."""

    def draft(self):
        draft = JobDraft(name="Work")
        volumes = (self.s, self.d)
        draft.set_source(str(self.src), volumes)
        draft.add_destination(str(self.dst), volumes)
        return draft

    def test_half_by_default_adjusted_and_switched_off(self):
        volumes = (self.s, self.d)
        draft = self.draft()
        self.assertEqual((draft.share_on, draft.share_text), (True, "50"))
        self.assertEqual(check(draft, volumes, (), ()).job.holds.share, 50)
        draft.share_text = "12.5"
        self.assertEqual(check(draft, volumes, (), ()).job.holds.share, 12.5)
        draft.share_on = False
        job = check(draft, volumes, (), ()).job
        self.assertIsNone(job.holds.share)
        edited = JobDraft.from_job(job, source_editable=False)
        self.assertEqual((edited.share_on, edited.share_text), (False, "50"))
        adjusted = JobDraft.from_job(replace(job, holds=HoldThresholds(share=30)), source_editable=False)
        self.assertEqual((adjusted.share_on, adjusted.share_text), (True, "30"))

    def test_a_share_outside_the_contract_cannot_be_saved_unless_switched_off(self):
        volumes = (self.s, self.d)
        draft = self.draft()
        for bad in ("0", "101", "half", ""):
            draft.share_text = bad
            result = check(draft, volumes, (), ())
            self.assertFalse(result.can_save, bad)
            share = next(c for c in result.checks if c.code == "input-share")
            self.assertEqual((share.level, share.text),
                             (REFUSED, "The share must be a percentage above 0 and at most 100."))
        draft.share_on = False
        self.assertTrue(check(draft, volumes, (), ()).can_save)

    def test_a_preview_names_the_jobs_own_share(self):
        self.mirror({f"f{i}.txt": f"f{i}" for i in range(10)})
        for i in range(3):
            (self.src / f"f{i}.txt").unlink()
        job = replace(self.job, holds=HoldThresholds(count_floor=1000, percent=100, share=30))
        pair = self.preview(job=job).pairs[0]
        self.assertEqual(pair.state, PairState.HELD)
        self.assertEqual(pair.headline, "REMOVALS HELD - 30 % or more of this destination's files would move into "
                                        "versions (this job holds at 30 %). This is what an empty or wrong source "
                                        "looks like.")


class AbsentDriveTest(unittest.TestCase):
    """A row whose drive is not attached stays listed, its words unchanged, in the absent
    tone the window draws grey; a row asking something of the user keeps its own tone; the
    tone goes when the drive returns."""

    def setUp(self):
        self.s = volume(1, "S", label="Media")
        self.d = volume(2, "F", label="Backup 1")
        self.e = volume(3, "G", label="Backup 2")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror"), endpoint(self.e, "")])
        self.contents = StoreContents((self.job,))
        self.markers = {(self.job.job_id, d.marker_id): MarkerState.MATCH for d in self.job.destinations}

    def row(self, volumes, records=(), runs=()):
        return build_rows(self.contents, volumes, self.markers, {self.job.job_id: tuple(records)}, runs,
                          now=datetime(2026, 10, 5, tzinfo=timezone.utc))[0]

    def completed(self):
        return [record(self.job, 1, Outcome.COMPLETED), record(self.job, 2, Outcome.COMPLETED)]

    def test_a_destination_whose_drive_is_away_is_absent_until_it_returns(self):
        row = self.row((self.s, self.d), self.completed())
        first, second = row.destinations
        self.assertEqual((second.drive, second.status, second.tone),
                         ("not attached", "Waits for its drive", Tone.ABSENT))
        self.assertEqual((first.status, first.tone), ("Completed", Tone.NORMAL))
        self.assertEqual((row.status, row.tone), ("Up to date", Tone.NORMAL))
        back = self.row((self.s, self.d, self.e), self.completed()).destinations[1]
        self.assertEqual((back.drive, back.status, back.tone), ("present", "Completed", Tone.NORMAL))

    def test_a_job_whose_source_drive_is_away_is_absent_unless_it_asks_something(self):
        away = self.row((self.d, self.e), self.completed())
        self.assertEqual((away.drive, away.status, away.tone),
                         ("not attached", "Waits for its source drive", Tone.ABSENT))
        self.assertEqual([d.tone for d in away.destinations], [Tone.NORMAL, Tone.NORMAL])
        held = self.row((self.d,), [record(self.job, 1, Outcome.HELD, reasons=("above-threshold",))])
        self.assertEqual((held.drive, held.status, held.tone), ("not attached", "Needs your review", Tone.ATTENTION))
        self.assertEqual([d.tone for d in held.destinations], [Tone.ATTENTION, Tone.ABSENT])
        running = self.row((self.d,), runs=[live(self.job)])
        self.assertEqual((running.status, running.tone), ("Running", Tone.ACTIVE))
        self.assertEqual([(d.status, d.tone) for d in running.destinations],
                         [("Running", Tone.ACTIVE), ("Waiting to run", Tone.ACTIVE)])

    def test_a_job_whose_every_destination_is_away_is_absent(self):
        row = self.row((self.s,), self.completed())
        self.assertEqual((row.drive, row.status, row.tone), ("present", "Waits for its drive", Tone.ABSENT))
        self.assertEqual([d.tone for d in row.destinations], [Tone.ABSENT, Tone.ABSENT])

    def test_two_drives_matching_stays_a_problem(self):
        clone = replace(self.d, device="\\\\?\\Volume{clone}\\", mount_points=("K:\\",))
        first = self.row((self.s, self.d, clone, self.e)).destinations[0]
        self.assertEqual((first.drive, first.status, first.tone),
                         ("two drives match", "Stopped: two drives match", Tone.PROBLEM))

    def test_a_group_takes_its_most_pressing_jobs_tone(self):
        w = volume(4, "W", label="Work")
        work = new_job("Work", endpoint(w, "Work"), [endpoint(self.d, "Work")])
        self.contents = StoreContents((self.job, work), groups=(new_group("All", (self.job.job_id, work.job_id)),))
        self.markers[(work.job_id, work.destinations[0].marker_id)] = MarkerState.MATCH

        def group(runs=()):
            rows = build_rows(self.contents, (self.d, self.e), self.markers, {}, runs)
            return arrange(self.contents, rows)[0]

        away = group()
        self.assertEqual((away.status, away.tone), ("Waits for its source drive (2 jobs)", Tone.ABSENT))
        running = group(runs=[live(self.job)])
        self.assertEqual((running.status, running.tone), ("Photos: Running", Tone.ACTIVE))


class RememberedWindowTest(TempDirTest):
    """The main window's remembered place: kept beside the groups closed in the list,
    saved alone through the session, read back, passed over where it cannot be used, and
    fitted onto the monitors attached."""

    def test_a_place_round_trips_beside_the_closed_groups(self):
        place = Place(120, 80, 1600, 1100, False, 0.62)
        state = with_place(with_collapsed({}, {"g"}), place)
        self.assertEqual(state, {"collapsed_groups": ["g"],
                                 WINDOW_KEY: {"x": 120, "y": 80, "width": 1600, "height": 1100, "maximised": False,
                                              "divider": 0.62}})
        self.assertEqual((remembered_place(state), collapsed_groups(state)), (place, {"g"}))
        bare = with_place({}, Place(-8, 0, 10, 20, True))
        self.assertNotIn("divider", bare[WINDOW_KEY])
        self.assertEqual(remembered_place(bare), Place(-8, 0, 10, 20, True, None))
        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(StoreContents())
        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=())
        session.load()
        session.save_view_state(state)
        self.assertEqual(remembered_place(ConfigStore(path).load().contents.preferences.gui_state), place)

    def test_what_cannot_be_used_is_passed_over(self):
        good = {"x": 0, "y": 0, "width": 800, "height": 600}
        self.assertIsNone(remembered_place({}))
        self.assertIsNone(remembered_place({WINDOW_KEY: "800x600+0+0"}))
        for bad in ({"width": 0}, {"height": -1}, {"x": True}, {"y": 1.5}, {"width": "800"}, {"x": 10 ** 6}):
            self.assertIsNone(remembered_place({WINDOW_KEY: dict(good, **bad)}), bad)
        missing = dict(good)
        del missing["y"]
        self.assertIsNone(remembered_place({WINDOW_KEY: missing}))
        for divider, kept in ((0, None), (1, None), ("0.5", None), (True, None), (0.999, 0.95), (0.01, 0.05),
                              (0.6254, 0.625)):
            self.assertEqual(remembered_place({WINDOW_KEY: dict(good, divider=divider)}).divider, kept, divider)
            self.assertEqual(share(divider), kept, divider)
        self.assertFalse(remembered_place({WINDOW_KEY: dict(good, maximised="yes")}).maximised)

    def test_a_place_is_fitted_onto_a_monitor(self):
        main, left = Area(0, 0, 3000, 1900), Area(-1700, 0, 0, 1000)
        areas, frame = (main, left), (16, 39)
        inside = Place(100, 100, 1600, 1100, True, 0.4)
        self.assertEqual(fit(inside, areas, frame), inside)
        self.assertEqual(fit(Place(100, 100, 5000, 3000), areas, frame), Place(0, 0, 2984, 1861))
        self.assertEqual(fit(Place(5000, 3000, 800, 600), areas, frame), Place(2184, 1261, 800, 600),
                         "a monitor no longer attached gives way to the main one")
        on_left = Place(-1600, 50, 1000, 700)
        self.assertEqual(fit(on_left, areas, frame), on_left)
        self.assertEqual(fit(Place(-1600, 50, 1000, 1200), areas, frame), Place(-1600, 0, 1000, 961))
        self.assertEqual(fit(Place(-300, 100, 1200, 800), areas, frame), Place(0, 100, 1200, 800),
                         "onto the monitor it overlaps most")
        self.assertEqual(fit(inside, (), frame), inside)

    def test_tks_geometry_in_words(self):
        self.assertEqual(parse_geometry("1600x1100+120+80"), (1600, 1100, 120, 80))
        self.assertEqual(parse_geometry("1700x950+-8+-8"), (1700, 950, -8, -8))
        for other in ("1600x1100", "1600x1100-5+3", "", "x+1+2"):
            self.assertIsNone(parse_geometry(other), other)
        self.assertEqual(geometry_text(Place(-8, 5, 10, 20)), "10x20+-8+5")
        self.assertEqual(parse_geometry(geometry_text(Place(-8, 5, 10, 20))), (10, 20, -8, 5))


def activity_log(job, *, command="run", started="2026-10-05T09:00:00Z", written="2026-10-05T09:00:05Z", status=1,
                 failure="the job file could not be read: another program kept it open; try again", in_use=None,
                 activities=(), trigger="manual", destinations=(), number=1):
    """What a log read back says of one process (``records.diagnostics.LogWatch``)."""
    name = started.replace("-", "").replace(":", "")
    return ActivityLog(Path(f"C:\\logs\\{name}-{command}-{number}.log"), started, command, trigger, job.job_id,
                       tuple(destinations), tuple(activities), status, failure, in_use, written)


class VerificationStatesTest(unittest.TestCase):
    """A verification in the job list: how far one at work is, and a pass that did not finish
    — stopped, interrupted or failed — with how far it got and the action that takes it up."""

    def setUp(self):
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)

    def rows(self, *, checks=(), runs=(), records=None):
        done = [record(self.job, 1, Outcome.COMPLETED)] if records is None else records
        return build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: tuple(done)}, runs, {self.job.job_id: tuple(checks)})[0]

    def unfinished(self, state, *, compared_bytes=60, bytes_total=100, detail="", verification_id="u" * 32):
        pass_ = verification(self.job, state=state, verification_id=verification_id)
        pass_.started = pass_.updated = "2026-10-05T09:00:00Z"
        pass_.compared_bytes, pass_.bytes_total, pass_.detail = compared_bytes, bytes_total, detail
        return pass_

    def test_a_verification_at_work_says_how_far_it_is(self):
        self.assertEqual(self.rows(runs=[live_verification(self.job)]).destinations[0].status, "Verifying 40 %")
        paused = self.rows(runs=[live_verification(self.job, paused=True)]).destinations[0]
        self.assertEqual((paused.status, paused.tone), ("Verifying 40 % (paused)", Tone.ACTIVE))
        listing = self.rows(runs=[live_verification(self.job, phase="listing")]).destinations[0]
        self.assertEqual(listing.status, "Verifying")
        self.assertEqual(self.rows(runs=[live_verification(self.job, phase="waiting")]).status, "Verifying")

    def test_a_pass_that_did_not_finish_says_so_with_how_far_it_got(self):
        for state, words, tone in ((VerificationState.STOPPED, "Verification stopped at 60 %", Tone.ATTENTION),
                                   (VerificationState.FAILED, "Verification failed at 60 %", Tone.PROBLEM),
                                   (VerificationState.INTERRUPTED, "Verification interrupted at 60 %", Tone.ATTENTION),
                                   (VerificationState.RUNNING, "Verification interrupted at 60 %", Tone.ATTENTION)):
            row = self.rows(checks=[verification(self.job), self.unfinished(state)])
            first = row.destinations[0]
            self.assertEqual((first.status, first.tone), (words, tone), state)
            self.assertEqual(first.unfinished.verification_id, "u" * 32)
            self.assertEqual((row.status, row.tone), (words, tone))
        by_files = self.unfinished(VerificationState.STOPPED, bytes_total=0)
        by_files.compared_files, by_files.files_total = 1, 3
        self.assertEqual(self.rows(checks=[by_files]).destinations[0].status, "Verification stopped at 33 %")

    def test_a_pass_at_work_or_finished_since_is_not_unfinished(self):
        at_work = self.unfinished(VerificationState.RUNNING, verification_id="c" * 32)
        first = self.rows(checks=[at_work], runs=[live_verification(self.job)]).destinations[0]
        self.assertEqual((first.status, first.unfinished), ("Verifying 40 %", None))
        finished = verification(self.job, verification_id="f" * 32)
        finished.started = "2026-10-06T09:00:00Z"
        first = self.rows(checks=[self.unfinished(VerificationState.FAILED), finished]).destinations[0]
        self.assertEqual((first.status, first.unfinished), ("Completed", None))

    def test_a_problem_a_review_or_copies_that_differ_come_first_and_the_pass_stays_continuable(self):
        failed = self.unfinished(VerificationState.FAILED)
        stopped_run = self.rows(checks=[failed], records=[record(self.job, 1, Outcome.STOPPED,
                                                                 reasons=("insufficient-space",))])
        self.assertEqual(stopped_run.destinations[0].status, "Stopped: not enough free space")
        self.assertIsNotNone(stopped_run.destinations[0].unfinished)
        held = self.rows(checks=[failed], records=[record(self.job, 1, Outcome.HELD, reasons=("lost-signatures",))])
        self.assertEqual(held.destinations[0].status, "Removals held: review")
        differ = verification(self.job, differ=[("a", 1)], verification_id="d" * 32)
        differ.started = "2026-09-01T08:00:00Z"
        first = self.rows(checks=[differ, failed]).destinations[0]
        self.assertEqual(first.status, "Copies differ: see verification")
        self.assertIsNotNone(first.unfinished)

    def test_the_dialog_says_why_a_failed_pass_did_not_finish(self):
        row = self.rows().destinations[0]
        why = "the destination folder F:\\Mirror went away and did not come back within 60 seconds"
        dialog = verify_dialog("Photos", row, 90, self.unfinished(VerificationState.FAILED, detail=why))
        self.assertEqual(dialog.why, f"Why it did not finish: {why}")
        self.assertEqual(verify_dialog("Photos", row, 90, self.unfinished(VerificationState.STOPPED)).why, "")


class SomeDestinationsTest(unittest.TestCase):
    """A run that serves some of a job's destinations leaves the others their own status."""

    def setUp(self):
        self.s, self.d, self.e = volume(1, "S"), volume(2, "F"), volume(3, "G")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "M"), endpoint(self.e, "M")])
        self.markers = {(self.job.job_id, d.marker_id): MarkerState.MATCH for d in self.job.destinations}
        self.done = [record(self.job, 1, Outcome.COMPLETED), record(self.job, 2, Outcome.COMPLETED)]

    def rows(self, runs):
        return build_rows(StoreContents((self.job,)), (self.s, self.d, self.e), self.markers,
                          {self.job.job_id: tuple(self.done)}, runs)[0]

    def test_the_destinations_a_run_does_not_serve_keep_their_own_status(self):
        row = self.rows([live(self.job, pair_index=2, selected=[2])])
        self.assertEqual([d.status for d in row.destinations], ["Completed", "Running"])
        self.assertEqual((row.status, row.running), ("Running", True))
        waiting = self.rows([live(self.job, phase="waiting", pair_index=0, selected=[2])])
        self.assertEqual([d.status for d in waiting.destinations], ["Completed", "Waiting to run"])

    def test_a_run_naming_no_destinations_serves_every_one(self):
        row = self.rows([live(self.job, pair_index=2)])
        self.assertEqual([d.status for d in row.destinations], ["Waiting to run", "Running"])


class EditedWhileRunningTest(unittest.TestCase):
    """A run numbers its destinations as the job stood when it began; the job edited while it
    works — a destination moved, removed or added — still shows the run on the destination it
    works on, found by marker."""

    def setUp(self):
        self.s, self.d, self.away, self.e = volume(1, "S"), volume(2, "F"), volume(3, "H"), volume(4, "G")
        self.began = new_job("Recipes", endpoint(self.s, "Recipes"),
                             [endpoint(self.d, "M"), endpoint(self.away, "M"), endpoint(self.e, "M")])
        first, absent, last = self.began.destinations
        self.moved = replace(self.began, destinations=(first, last, absent))
        self.markers = {(self.began.job_id, d.marker_id): MarkerState.MATCH for d in self.began.destinations}
        self.done = (record(self.began, 1, Outcome.COMPLETED),)
        self.order = [d.marker_id for d in self.began.destinations]

    def rows(self, job, runs):
        return build_rows(StoreContents((job,)), (self.s, self.d, self.e), self.markers,
                          {job.job_id: self.done}, runs)[0]

    def on_the_last(self, **extra):
        status = dict(pair_index=3, marker_id=self.order[2], selected=[1, 2, 3],
                      finished=[[1, "completed"], [2, "skipped"]], markers=self.order)
        status.update(extra)
        return live(self.began, **status)

    def test_a_destination_moved_up_shows_the_run_it_receives(self):
        row = self.rows(self.moved, [self.on_the_last()])
        self.assertEqual([d.status for d in row.destinations][:2], ["Completed", "Running"])
        self.assertNotIn(row.destinations[2].status, ("Running", "Waiting to run"),
                         "the destination the run finished keeps its own status at its new place")

    def test_the_job_as_it_began_shows_the_run_where_it_was(self):
        row = self.rows(self.began, [self.on_the_last()])
        self.assertEqual([d.status for d in row.destinations][0::2], ["Completed", "Running"])

    def test_a_destination_added_while_the_run_works_is_not_one_it_serves(self):
        added = replace(self.moved, destinations=self.moved.destinations
                        + (replace(self.began.destinations[0], marker_id="c" * 32),))
        self.markers[(added.job_id, "c" * 32)] = MarkerState.MATCH
        row = self.rows(added, [self.on_the_last(selected=[])])
        self.assertEqual(row.destinations[1].status, "Running")
        self.assertNotIn(row.destinations[3].status, ("Running", "Waiting to run"))

    def test_a_run_naming_no_markers_numbers_as_the_job_stands(self):
        row = self.rows(self.began, [self.on_the_last(markers=None)])
        self.assertEqual([d.status for d in row.destinations][0::2], ["Completed", "Running"],
                         "an older copy's run, on the job it began on")
        moved = self.rows(self.moved, [self.on_the_last(markers=None)])
        self.assertEqual(moved.destinations[1].status, self.rows(self.moved, []).destinations[1].status,
                         "its number 2 finished: its own status")
        self.assertEqual(moved.destinations[2].status, "Running",
                         "naming no markers, it is shown by its numbers, as every copy did before")

    def test_the_run_count_follows_only_the_runs_own_number(self):
        status = self.on_the_last(pair_total=3).status
        self.assertEqual(run_panel(ObservedRun(status, True), destination_places([self.began])).title,
                         "Recipes -> 3 of 3")
        self.assertEqual(run_panel(ObservedRun({**status, "pair_total": 2}, True),
                                   destination_places([self.moved])).title, "Recipes -> 2",
                         "renumbered: the run's count no longer counts what the number does")

    def test_a_status_that_cannot_be_read_keeps_what_its_numbers_name(self):
        unread = ObservedRun({"run_id": "a" * 32}, True, False, self.on_the_last(phase="copying").status)
        row, idle = self.rows(self.moved, [unread]), self.rows(self.moved, [])
        self.assertEqual(row.destinations[2].status, idle.destinations[2].status,
                         "finished before the status went unreadable: its own status")
        self.assertNotEqual(row.destinations[1].status, idle.destinations[1].status)

    def test_titles_number_the_destination_as_the_job_stands(self):
        status = self.on_the_last(pair_total=3).status
        places = destination_places([self.moved])
        self.assertEqual(destination_number(status, places), 2)
        self.assertEqual(destination_number(status), 3, "no job known: the run's own number")
        self.assertEqual(run_panel(ObservedRun(status, True), places).title, "Recipes -> 2")
        gone = destination_places([replace(self.moved, destinations=self.moved.destinations[:1])])
        self.assertEqual(destination_number(status, gone), None)
        self.assertEqual(run_panel(ObservedRun(status, True), gone).title, "Recipes")
        self.assertEqual(destination_number({**status, "pair_index": 0}, places), None)
        unread = ObservedRun({"run_id": "a" * 32}, True, False, status)
        self.assertEqual(unreadable_panel(unread, places).title, "Recipes -> 2")
        self.assertEqual([p.title for p in live_panels([ObservedRun(status, True)], places)], ["Recipes -> 2"])


class StartsWithoutRecordTest(unittest.TestCase):
    """A start that left no record of its own — refused, failed before it recorded
    anything, or its process gone without a word — read back from its log: on the job's
    row until a record is written after it, and in History, with why."""

    def setUp(self):
        self.s, self.d = volume(1, "S"), volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.done = record(self.job, 1, Outcome.COMPLETED, started="2026-10-04T20:10:00Z",
                           ended="2026-10-04T20:51:00Z")

    def row(self, logs, *, records=None, runs=()):
        return build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: tuple([self.done] if records is None else records)}, runs,
                          logs=logs)[0]

    def test_a_start_that_could_not_start_reads_on_the_jobs_row(self):
        row = self.row([activity_log(self.job)])
        self.assertEqual((row.status, row.tone), ("Run could not start - see History", Tone.PROBLEM))
        self.assertEqual(row.start_failure.failure, "the job file could not be read: another program kept it open; "
                                                    "try again")
        self.assertEqual(self.row([activity_log(self.job, command="verify")]).status,
                         "Verification could not start - see History")
        self.assertEqual(self.row([activity_log(self.job, command="automatic", trigger="arrival")]).status,
                         "Run could not start - see History")

    def test_a_process_gone_without_a_word_and_without_a_record(self):
        gone = activity_log(self.job, status=None, failure="", in_use=False, activities=(("x" * 32, "run"),))
        self.assertEqual(self.row([gone]).status, "Ended without a record - see History")
        still = replace(gone, in_use=True)
        self.assertEqual(self.row([still]).status, "Up to date")
        recorded = replace(gone, activities=(("r" * 32, "run"),))
        interrupted = record(self.job, 1, Outcome.INTERRUPTED, started="2026-10-05T09:00:01Z",
                             ended="2026-10-05T09:00:04Z")
        self.assertEqual(self.row([recorded], records=[interrupted]).status, "Interrupted - next run continues")

    def test_a_record_or_a_start_that_ran_since_clears_it(self):
        later = record(self.job, 1, Outcome.COMPLETED, started="2026-10-05T10:00:00Z", ended="2026-10-05T10:30:00Z")
        self.assertEqual(self.row([activity_log(self.job)], records=[self.done, later]).status, "Up to date")
        ran = activity_log(self.job, started="2026-10-05T11:00:00Z", status=0, failure="", number=2)
        self.assertEqual(self.row([activity_log(self.job), ran]).status, "Up to date")
        self.assertIsNone(self.row([activity_log(self.job), ran]).start_failure)

    def test_work_in_progress_and_a_review_come_first(self):
        self.assertEqual(self.row([activity_log(self.job)], runs=[live(self.job)]).status, "Running")
        first_run = self.row([activity_log(self.job)], records=[])
        self.assertEqual(first_run.status, "Run could not start - see History")
        no_marker = build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MISSING}, {}, (),
                               logs=[activity_log(self.job)])[0]
        self.assertEqual(no_marker.status, "Needs your review")

    def test_a_log_names_its_job_by_identifier_name_or_prefix(self):
        other = new_job("Music", endpoint(self.s, "Music"), [endpoint(self.d, "Music")])
        jobs = (self.job, other)
        for key in (self.job.job_id, "photos", self.job.job_id[:6]):
            self.assertEqual(log_job(replace(activity_log(self.job), job=key), jobs), self.job.job_id, key)
        for key in ("", "Pictures", self.job.job_id[:3]):
            self.assertIsNone(log_job(replace(activity_log(self.job), job=key), jobs), key)

    def test_history_lists_each_start_that_left_no_record_with_why(self):
        logs = [activity_log(self.job, command="automatic", trigger="arrival", started="2026-10-05T09:00:00Z"),
                activity_log(self.job, command="verify", destinations=(1,), started="2026-10-05T08:00:00Z",
                             failure="the destination's drive is not attached", number=2),
                activity_log(self.job, started="2026-10-05T07:00:00Z", status=None, failure="", in_use=False,
                             number=3),
                activity_log(self.job, started="2026-10-05T06:00:00Z", status=0, failure="", number=4)]
        entries = history_entries([self.done], (), {1: "Backup 1"}, job="Photos", logs=logs)
        rows = [row for row, _entry in entries]
        self.assertEqual([(r.result, r.how, r.destination, r.copied, r.took) for r in rows[:3]],
                         [("Could not start", "drive attached", "-", "-", "-"),
                          ("Could not start", "by you", "1 Backup 1", "-", "-"),
                          ("Ended without a record", "by you", "-", "-", "-")])
        self.assertEqual(len(rows), 4, "the start that ran is not a row of its own")
        self.assertEqual(rows[0].details, "The automatic run could not start: the job file could not be read: "
                                          "another program kept it open; try again")
        details = start_details(logs[1], "Photos")
        self.assertEqual((details.title, details.text),
                         (f"Could not start - Photos, {formatting.local_time('2026-10-05T08:00:00Z')}",
                          "The verification could not start: the destination's drive is not attached"))
        self.assertEqual(start_details(logs[2], "Photos").text,
                         "Its process ended without a record: a shutdown or a crash.")


class HistoryOfTheSelectionTest(TempDirTest):
    """History of one job, a group's jobs, several jobs or every job: each row names its job,
    and what History shows saves as a CSV file a person reads."""

    def setUp(self):
        super().setUp()
        s, d = volume(1, "S"), volume(2, "F")
        self.photos = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "Photos")])
        self.music = new_job("Musique d'ete", endpoint(s, "Music"), [endpoint(d, "Music")])

    def history(self, job, started, *, logs=()):
        run = replace(record(job, 1, Outcome.COMPLETED, started=started), run_id=job.job_id)
        return JobHistory(job.job_id, job.name, (run,), (), {1: "Backup"}, tuple(logs))

    def test_several_jobs_newest_first_each_row_naming_its_job(self):
        entries = selection_entries([self.history(self.photos, "2026-10-04T20:10:00Z"),
                                     self.history(self.music, "2026-10-05T20:10:00Z")])
        self.assertEqual([row.job for row, _entry in entries], ["Musique d'ete", "Photos"])
        self.assertEqual(entries[1][0].details, "Completed.")

    def test_what_history_says_while_it_shows_no_record(self):
        self.assertEqual(empty_words([], every=True), "No run is recorded yet.")
        self.assertEqual(empty_words([], group="Phone"), 'No run of the jobs in "Phone" is recorded yet.')
        self.assertEqual(empty_words(["Photos"]), 'No run of "Photos" is recorded yet.')
        self.assertEqual(empty_words(["Photos", "Music"]), "No run of the selected jobs is recorded yet.")

    def test_the_saved_history_is_the_words_shown_with_each_rows_details(self):
        logs = (activity_log(self.music, started="2026-10-06T08:00:00Z"),)
        rows = [row for row, _entry in selection_entries([self.history(self.photos, "2026-10-04T20:10:00Z"),
                                                          self.history(self.music, "2026-10-05T20:10:00Z",
                                                                       logs=logs)])]
        path = self.tmp / "saved.csv"
        save_history(path, rows)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"), "a byte-order mark for spreadsheet programs")
        with open(path, encoding="utf-8-sig", newline="") as saved:
            lines = list(csv.reader(saved))
        self.assertEqual(lines[0], ["Job", "Started", "Destination", "How started", "Result", "Copied",
                                    "Into versions", "Renamed", "Skipped", "Took", "Details"])
        self.assertEqual([line[0] for line in lines[1:]], ["Musique d'ete", "Musique d'ete", "Photos"])
        self.assertEqual(lines[1][4:6], ["Could not start", "-"])
        self.assertEqual(lines[3], [rows[2].job, rows[2].started, rows[2].destination, rows[2].how, rows[2].result,
                                    rows[2].copied, rows[2].into_versions, rows[2].renamed, rows[2].skipped,
                                    rows[2].took, "Completed."])
        self.assertEqual(history_file_name(date(2026, 10, 14)), "MirrorHold history 2026-10-14.csv")
        self.assertEqual(log_file_name(date(2026, 10, 14)), "MirrorHold log 2026-10-14.txt")

    def test_a_record_opens_the_log_of_the_process_that_wrote_it(self):
        run = record(self.photos, 1, Outcome.COMPLETED)
        pass_ = verification(self.photos, verification_id="v" * 32)
        first = activity_log(self.photos, started="2026-10-05T09:00:00Z", status=0, failure="",
                             activities=(("r" * 32, "run"), ("v" * 32, "verification")))
        second = activity_log(self.photos, command="verify", started="2026-10-06T09:00:00Z", status=0, failure="",
                              activities=(("v" * 32, "verification"),), number=2)
        self.assertEqual(log_of(run, [first, second]), first.path)
        self.assertEqual(log_of(pass_, [first, second]), second.path, "the latest sitting's")
        self.assertEqual(log_of(second, ()), second.path)
        self.assertIsNone(log_of(replace(run, run_id="z" * 32), [first, second]))


class WindowWordsTest(unittest.TestCase):
    """The words of the closing question, of a request that did not reach its run, of a
    file not backed up shown whole, and of the job file held at start."""

    def test_the_closing_question_names_runs_and_verifications(self):
        self.assertEqual(closing_question(2, 1), "2 runs and 1 verification are in progress. They continue without "
                                                 "this window; reopen MirrorHold to watch or control them.")
        self.assertEqual(closing_question(0, 1), "1 verification is in progress. It continues without this window; "
                                                 "reopen MirrorHold to watch or control it.")
        self.assertEqual(closing_question(1, 0), "1 run is in progress. It continues without this window; reopen "
                                                 "MirrorHold to watch or control it.")

    def test_a_request_that_did_not_reach_its_run(self):
        self.assertEqual(REQUEST_TITLES, {"pause": "Not paused", "resume": "Not resumed", "cancel": "Not cancelled",
                                          "stop": "Not stopped"})
        self.assertEqual(not_reached_words(False), "The request did not reach the run: another program kept its "
                                                   "control file open. Try again.")
        self.assertEqual(not_reached_words(True, "the folder is gone"),
                         "The request did not reach the verification: the folder is gone.")

    def test_a_file_not_backed_up_whole_and_the_job_file_held_at_start(self):
        report = unbacked_report(replace(record(new_job("W", endpoint(volume(1, "S"), "W"),
                                                        [endpoint(volume(2, "F"), "M")]), 1, Outcome.COMPLETED,
                                                skipped=[("a\\b.pst", "could not be read now (in use)")]),
                                         not_backed_up=(("a\\b.pst", 2),)), "W")
        self.assertEqual(whole_words(report.rows[0]), "a\\b.pst - could not be read now (in use)")
        self.assertEqual((HELD_TITLE, HELD_TEXT),
                         ("The job file is in use", "MirrorHold could not read its job file: another program kept it "
                                                    "open. Close that program, then try again."))


class PreviewReadingTest(PreviewCase):
    """The preview's line on how the run will read its source."""

    def test_how_the_run_will_read_the_source(self):
        write(self.src, "a.txt", "a")
        where = str(self.vol_s)
        self.assertEqual(self.preview().reading,
                         f"The run reads the source from a snapshot of {where} when it can make one as it starts.")
        on = self.job
        off = replace(on, source=replace(on.source, read_from_snapshot=False))
        self.assertEqual(self.preview(job=off).reading,
                         "The run reads the source as it is: snapshot reading is switched off for this job.")
        fat = replace(self.s, file_system="exFAT")
        self.assertEqual(self.preview(volumes=(fat, self.d), job=on).reading,
                         "The run reads the source as it is: this drive does not support snapshots.")
        self.assertEqual(self.preview(volumes=(self.s,)).reading, "", "no destination attached")
        self.assertEqual(self.preview(volumes=(self.d,)).reading, "", "the source's drive away")


class RefreshTest(TempDirTest):
    """The refresher reads a record another program held again at its next look, follows the
    logs, and a look that fails keeps what was seen last and says why."""

    def setUp(self):
        super().setUp()
        self.s, self.d = volume(1, "S"), volume(2, "F")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.contents = StoreContents((self.job,))
        self.layout = runtime_layout(self.tmp / "state" / "store.json")

    @unittest.skipUnless(sys.platform == "win32", "holding a file unshared needs Windows")
    def test_a_record_held_past_the_patience_is_read_at_the_next_look(self):
        written = replace(record(self.job, 1, Outcome.COMPLETED), run_id="a" * 32)
        path = write_record(self.layout, written)
        source = FixedVolumeSource((self.s, self.d))
        with held(path):
            first = observe(self.contents, self.layout, source)
        self.assertEqual(first.records[self.job.job_id], ())
        self.assertIsNone(first.record_stamps[self.job.job_id])
        second = observe(self.contents, self.layout, source, first)
        self.assertEqual([r.run_id for r in second.records[self.job.job_id]], ["a" * 32])

    def test_the_logs_are_followed_and_a_failed_look_keeps_what_was_seen(self):
        self.layout.logs_dir.mkdir(parents=True)
        log = self.layout.logs_dir / "20261005T090000Z-run-7.log"
        log.write_text("2026-10-05T09:00:00Z mirrorhold run Photos\n2026-10-05T09:00:00Z @start "
                       + json.dumps({"command": "run", "job": self.job.job_id, "trigger": "manual",
                                     "destinations": None}) + "\n2026-10-05T09:00:01Z @end "
                       + json.dumps({"status": 3, "failure": "held"}) + "\n", encoding="utf-8")
        seen = []
        refresher = Refresher(self.layout, FixedVolumeSource((self.s, self.d)), seen.append, interval=0.05)
        refresher.set_contents(self.contents)
        refresher.start()
        self.addCleanup(refresher.stop)
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([(g.job, g.status, g.failure) for g in seen[0].logs], [(self.job.job_id, 3, "held")])
        self.assertEqual(seen[0].failed, "")
        count = len(seen)
        refresher.volume_source = FixedVolumeSource((object(),))
        failed = None
        while failed is None and time.monotonic() < deadline:     # a look already under way may still succeed
            failed = next((o for o in seen[count:] if o.failed), None)
            time.sleep(0.05)
        self.assertIsNotNone(failed, "no look that failed was published")
        self.assertTrue(failed.failed.startswith("AttributeError"), failed.failed)
        self.assertEqual((failed.logs, failed.taken), (seen[0].logs, refresher.latest.taken))


def published(seen, predicate, timeout=10):
    """The first item ``seen`` gathers, on another thread, that ``predicate`` accepts; None
    when none came within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = next((item for item in list(seen) if predicate(item)), None)
        if found is not None:
            return found
        time.sleep(0.02)
    return None


class RunWatchTest(TempDirTest):
    """The runs' status is read on the watcher's thread — a status another program holds
    keeps that thread waiting, never the one that started the watcher or asked it to look —
    and a look that fails says why. An activity that ended is listed as last seen at work
    until a look of the refresher's begun after its end arrives, so a job whose first
    backup has just been made does not offer it again."""

    def setUp(self):
        super().setUp()
        self.layout = runtime_layout(self.tmp / "state" / "store.json")

    def watch(self, **kwargs):
        seen = []
        watcher = RunWatcher(self.layout, seen.append, interval=0.05, **kwargs)
        watcher.start()
        self.addCleanup(watcher.stop)
        return watcher, seen

    def test_each_look_is_published_and_a_failed_one_says_why(self):
        board = live_activity(self.layout, "j" * 32, job_name="Photos")
        self.addCleanup(board.close)
        _watcher, seen = self.watch()
        look = published(seen, lambda look: look.runs)
        self.assertIsNotNone(look, "no look naming the run was published")
        self.assertEqual(([r.run_id for r in look.runs], look.runs[0].alive, look.failed),
                         ([board.status.run_id], True, ""))

        def broken(_layout):
            raise OSError("the folder went away")

        _watcher, failed = self.watch(observer=broken)
        look = published(failed, lambda look: look.failed)
        self.assertIsNotNone(look, "no look that failed was published")
        self.assertEqual((look.failed, look.runs), ("OSError: the folder went away", ()))

    @unittest.skipUnless(sys.platform == "win32", "holding a file unshared needs Windows")
    def test_a_status_another_program_holds_keeps_the_watchers_thread_waiting_not_its_caller(self):
        board = live_activity(self.layout, "j" * 32, job_name="Photos")
        self.addCleanup(board.close)
        with held(self.layout.status_dir / f"{board.status.run_id}.json"):
            began = time.monotonic()
            watcher, seen = self.watch()
            watcher.look_now()
            asked = time.monotonic() - began
            look = published(seen, lambda look: look.runs)
            waited = time.monotonic() - began
        self.assertLess(asked, 0.5, "starting the watcher, or asking it to look, waited for the status")
        self.assertIsNotNone(look, "no look naming the run was published")
        self.assertGreater(waited, 1.0, "the status was not waited for")
        self.assertEqual([(r.run_id, r.alive, r.readable) for r in look.runs], [(board.status.run_id, True, False)])

    def test_a_look_begun_after_a_request_answers_it_once_one_succeeds(self):
        s, d = volume(1, "S"), volume(2, "F")
        job = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "Mirror")])
        source = FixedVolumeSource((s, d))
        seen = []
        refresher = Refresher(self.layout, source, seen.append, interval=60)
        refresher.set_contents(StoreContents((job,)))
        refresher.start()
        self.addCleanup(refresher.stop)
        first = published(seen, lambda observation: True)
        self.assertIsNotNone(first, "no observation was published")
        self.assertEqual(first.answered, 1, "setting the jobs is a request")
        request = refresher.refresh_now(markers=True)
        self.assertEqual(request, 2)
        self.assertIsNotNone(published(seen, lambda observation: observation.answered >= request),
                             "no look answered the request")
        refresher.volume_source = FixedVolumeSource((object(),))
        request = refresher.refresh_now(markers=True)
        failed = published(seen, lambda observation: observation.failed)
        self.assertIsNotNone(failed, "no look that failed was published")
        self.assertEqual(failed.answered, request - 1, "a look that read nothing answers no request")
        refresher.volume_source = source
        refresher.refresh_now()
        self.assertIsNotNone(published(seen, lambda observation: not observation.failed
                                       and observation.answered >= request),
                             "the next look that succeeds answers the request the failed one did not")

    def test_an_ended_activity_is_listed_as_last_seen_at_work_until_a_look_answers(self):
        root = self.tmp / "volD"
        root.mkdir()
        s, d = volume(1, "S"), volume(2, mounts=(str(root) + "\\",))
        job = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "Photos")])
        contents = StoreContents((job,))
        key = (job.job_id, job.destinations[0].marker_id)

        def row(runs):
            return build_rows(contents, (s, d), {key: MarkerState.MISSING}, {}, runs,
                              occupancy={key: Occupancy.MISSING})[0]

        at_work = live(job)
        gone = ObservedRun(at_work.status, False)
        self.assertEqual(ended_runs([at_work], [at_work]), ())
        self.assertEqual(ended_runs([at_work], [gone]), (at_work,))
        self.assertEqual(ended_runs([at_work], []), (at_work,))
        self.assertTrue(row([gone]).first_backups, "the destination as before the run offers its first backup")
        settling = Settling()
        settling.hold(ended_runs([at_work], [gone]), 3)
        self.assertEqual(settling.listed([gone]), (at_work,))
        held_row = row(settling.listed([gone]))
        self.assertEqual((held_row.running, held_row.destinations[0].status), (True, "Running"))
        self.assertEqual(Selection.of([held_row], {held_row.iid: held_row}).startable, ())
        settling.settle(2)
        self.assertEqual(settling.listed([]), (at_work,), "a look begun before the end does not settle it")
        settling.settle(3)
        self.assertEqual(settling.listed([gone]), (gone,))
        self.assertTrue(row(settling.listed([gone])).first_backups)


class OneDriveAtATimeWordsTest(unittest.TestCase):
    """What a waiting run or verification says of whom it waits for, the drive they share and
    its place in line — in the Runs tab and in the job list — and the choice in Preferences."""

    def setUp(self):
        self.s, self.d, self.e = volume(1, "S"), volume(2, "I"), volume(3, "K")
        self.job = new_job("Photos", endpoint(self.s, "P"), [endpoint(self.d, "M"), endpoint(self.e, "M")])

    @staticmethod
    def waiting_for(**other):
        return dict(dict(run_id="w" * 32, kind="run", job_name="Work", paused=False, waiting=False, drive=""), **other)

    def rows(self, runs):
        markers = {(self.job.job_id, d.marker_id): MarkerState.MATCH for d in self.job.destinations}
        done = [record(self.job, 1, Outcome.COMPLETED), record(self.job, 2, Outcome.COMPLETED)]
        return build_rows(StoreContents((self.job,)), (self.s, self.d, self.e), markers,
                          {self.job.job_id: tuple(done)}, runs)[0]

    def test_a_waiting_row_notes_the_drive_it_shares_and_its_place(self):
        def panel(**status):
            return run_panel(live(self.job, phase="waiting", pair_index=0, **status))

        shared = panel(waiting_for=self.waiting_for(drive="drive I:"), place=2)
        self.assertEqual((shared.phase, shared.note), ("waiting for 'Work'", "Both use drive I:. 2nd in line."))
        folders = panel(waiting_for=self.waiting_for(paused=True), place=1)
        self.assertEqual((folders.phase, folders.note), ("waiting for 'Work' (paused)", "Next in line."))
        self.assertEqual(panel(waiting_for=self.waiting_for()).note, "", "a status naming no place, an older copy's")
        check = live(self.job, phase="waiting", kind="verification", run_id="c" * 32, place=1,
                     verify={"gave_way": True, "files": 0},
                     waiting_for=self.waiting_for(drive="the drive of C: and D:"))
        self.assertEqual((run_panel(check).phase, run_panel(check).note),
                         ("gave way, waiting for 'Work'", "Both use the drive of C: and D:. Next in line."))

    def test_the_job_list_says_whom_a_waiting_run_waits_for_the_drive_and_its_place(self):
        waiting = self.rows([live(self.job, phase="waiting", pair_index=0, place=2,
                                  waiting_for=self.waiting_for(drive="drive I:"))])
        words = "2nd in line, waiting for 'Work' (drive I:)"
        self.assertEqual((waiting.status, [d.status for d in waiting.destinations]), (words, [words, words]))
        folders = self.rows([live(self.job, phase="waiting", pair_index=0, place=1,
                                  waiting_for=self.waiting_for(kind="verification"))])
        self.assertEqual(folders.status, "Next in line, waiting for a verification of 'Work'")
        unread = self.rows([live(self.job, phase="waiting", pair_index=0, place=1,
                                 waiting_for=self.waiting_for(readable=False))])
        self.assertEqual(unread.status, "Next in line, waiting for 'Work' (its status cannot be read now)")
        older = self.rows([live(self.job, phase="waiting", pair_index=0)])
        self.assertEqual(older.status, "Waiting to run")
        running = self.rows([live(self.job, phase="copying", pair_index=1)])
        self.assertEqual([d.status for d in running.destinations], ["Running", "Waiting to run"],
                         "a destination its own run has not reached")

    def test_the_job_list_says_whom_a_waiting_verification_waits_for(self):
        check = live(self.job, phase="waiting", kind="verification", run_id="c" * 32, place=1,
                     marker_id=self.job.destinations[0].marker_id, verify={"files": 0},
                     waiting_for=self.waiting_for(drive="drive I:"))
        row = self.rows([check])
        self.assertEqual(row.destinations[0].status, "Verification next in line, waiting for 'Work' (drive I:)")
        older = live(self.job, phase="waiting", kind="verification", run_id="c" * 32,
                     marker_id=self.job.destinations[0].marker_id, verify={"files": 0})
        self.assertEqual(self.rows([older]).destinations[0].status, "Waiting to verify")

    def test_the_choice_in_preferences_keeps_the_others(self):
        chosen = Preferences(activities=Activities(keep_awake=False, name_at_shutdown=False))
        changed = with_drive_turns(chosen, DriveTurns.ONE_AT_A_TIME)
        self.assertEqual(changed.activities, Activities(DriveTurns.ONE_AT_A_TIME, keep_awake=False,
                                                        name_at_shutdown=False))
        self.assertEqual(with_drive_turns(changed, DriveTurns.AUTOMATIC).activities, chosen.activities)
        self.assertEqual((DRIVE_TITLE, AUTOMATIC, ONE_AT_A_TIME, AT_THE_SAME_TIME),
                         ("Runs on one drive",
                          "Automatic: one at a time on a hard disk, at the same time on a solid-state or cloud drive",
                          "Always one at a time: each waits for its turn",
                          "Always at the same time: they share the drive's speed"))

    def test_keeping_the_computer_awake_is_a_choice_that_keeps_the_others(self):
        chosen = Preferences(activities=Activities(DriveTurns.AT_THE_SAME_TIME))
        changed = with_keep_awake(chosen, False)
        self.assertEqual(changed.activities, Activities(DriveTurns.AT_THE_SAME_TIME, keep_awake=False))
        self.assertEqual(with_keep_awake(changed, True).activities, chosen.activities)
        self.assertEqual((WORK_TITLE, KEEP_AWAKE, KEEP_AWAKE_NOTE),
                         ("While a run, verification or repair works", "Keep the computer from going to sleep",
                          "The computer's own sleep timer waits until the work ends; Sleep from the Start menu still "
                          "works."))

    def test_naming_the_work_at_shutdown_is_a_choice_on_at_first_that_keeps_the_others(self):
        self.assertTrue(Preferences().activities.name_at_shutdown)
        chosen = Preferences(activities=Activities(DriveTurns.AT_THE_SAME_TIME, keep_awake=False))
        changed = with_name_at_shutdown(chosen, False)
        self.assertEqual(changed.activities, Activities(DriveTurns.AT_THE_SAME_TIME, keep_awake=False,
                                                        name_at_shutdown=False))
        self.assertEqual(with_name_at_shutdown(changed, True).activities, chosen.activities)
        self.assertEqual((NAME_AT_SHUTDOWN, NAME_AT_SHUTDOWN_NOTE),
                         ("Show it on Windows' shutdown screen",
                          "A shutdown then waits for you to choose Shut down anyway or Cancel. If the work ends "
                          "first, MirrorHold stops holding it up; work a shutdown cuts continues next time."))


def repair_record(job, *, replaced=(), left=(), state=RepairState.COMPLETED, verification_id="v" * 32,
                  repair_id="p" * 32, started="2026-09-02T10:00:00Z", ended="2026-09-02T10:01:00Z"):
    return RepairRecord(repair_id, job.job_id, job.name, job.destinations[0].marker_id, 1, verification_id,
                        "2026-09-01T09:00:00Z", started, ended, state, ended, len(replaced) + len(left), 2,
                        list(replaced), list(left), sum(size for _path, size in replaced),
                        ".mirrorhold\\versions\\2026-09-02T10-00-00Z")


def live_repair(job, *, phase="copying", **extra):
    status = dict(kind="repair", run_id="e" * 32, marker_id=job.destinations[0].marker_id, selected=[1],
                  planned={"files_to_copy": 3, "bytes_to_copy": 3000}, done={"files_copied": 1, "bytes_copied": 1000})
    status.update(extra)
    return live(job, phase=phase, **status)


class RepairWordsTest(unittest.TestCase):
    """Replacing the copies a verification found to differ: what the job list offers and says
    while a repair works and once it has replaced them, the question before one, what a
    verification's details and History say of it, its row in the Runs tab."""

    NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.s = volume(1, "S", label="Media")
        self.d = volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.done = record(self.job, 1, Outcome.COMPLETED, ended="2026-06-01T10:00:00Z")
        self.found = verification(self.job, differ=[("2024\\a.raw", 4096), ("2024\\b.raw", 2048)])

    def rows(self, *, volumes=None, checks=None, runs=(), repairs=()):
        return build_rows(StoreContents((self.job,)), volumes or (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: (self.done,)}, runs,
                          {self.job.job_id: tuple([self.found] if checks is None else checks)}, self.NOW,
                          repairs={self.job.job_id: tuple(repairs)})[0]

    def test_a_destination_whose_latest_pass_found_copies_that_differ_offers_their_repair(self):
        first = self.rows().destinations[0]
        self.assertEqual((first.status, first.found_differ, first.can_repair),
                         ("Copies differ: see verification", self.found, True))
        self.assertFalse(self.rows(volumes=(self.d,)).destinations[0].can_repair, "the source's drive is away")
        self.assertFalse(self.rows(runs=[live(self.job)]).destinations[0].can_repair, "a run works on it")
        clean = self.rows(checks=[verification(self.job)]).destinations[0]
        self.assertEqual((clean.found_differ, clean.can_repair), (None, False))
        some = repair_record(self.job, replaced=[("2024\\a.raw", 4096)],
                             left=[("2024\\b.raw", LeftReason.CHANGED, "")])
        partly = self.rows(repairs=[some]).destinations[0]
        self.assertEqual((partly.status, partly.can_repair), ("Copies differ: see verification", True))
        every = repair_record(self.job, replaced=[("2024\\a.raw", 4096), ("2024\\b.raw", 2048)])
        repaired = self.rows(repairs=[every])
        self.assertEqual((repaired.destinations[0].status, repaired.destinations[0].can_repair), ("Completed", False))
        self.assertEqual(repaired.status, "Up to date")

    def test_a_repair_in_progress_reads_so_and_is_not_a_run(self):
        row = self.rows(runs=[live_repair(self.job)])
        first = row.destinations[0]
        self.assertEqual((first.status, first.tone, first.repairing, first.can_repair, first.can_verify),
                         ("Replacing copies", Tone.ACTIVE, True, False, True))
        self.assertEqual((row.status, row.running), ("Replacing copies", False))
        waiting = self.rows(runs=[live_repair(self.job, phase="waiting")])
        self.assertEqual((waiting.status, waiting.destinations[0].status),
                         ("Waiting to replace copies", "Waiting to replace copies"))

    def test_the_question_before_a_repair(self):
        question = replace_question("Photos", 1, "Backup 1", self.found, (), None)
        self.assertEqual(question.title, 'Replace backup copies: Photos, destination 1 "Backup 1"')
        self.assertEqual(question.text, "Replace the backup copies of 2 files that differ from the source? Each backup "
                                        "copy moves into this destination's versions, the source's copy is copied in "
                                        "its place, and both are read back to check they now match.")
        self.assertEqual((question.versions, question.unlisted), ("", ""))
        self.assertEqual(question.hint, "A file changed since the verification, gone from either side, or whose "
                                        "source copy lost the file-format signature its backup copy has, is left as "
                                        "it is.")
        many = verification(self.job, differ=[("a.raw", 1)])
        many.differ_total = 1234
        question = replace_question("Photos", 1, "", many, (), 30)
        self.assertTrue(question.text.startswith("Replace the backup copies of 1 file that differs from the source?"))
        self.assertEqual(question.versions, "Versions are kept 30 days: if a source copy turns out to be the damaged "
                                            "one, take the backup copy out of versions before then.")
        self.assertEqual(question.unlisted, "The verification listed 1 of the 1,234 files that differ; verify again "
                                            "from the beginning afterwards to find the rest.")
        some = repair_record(self.job, replaced=[("2024\\a.raw", 4096)])
        self.assertIn("of 1 file that differs", replace_question("Photos", 1, "", self.found, [some], None).text)

    def test_what_a_verification_says_a_repair_replaced(self):
        self.assertEqual(repaired_line(self.found, ()), "")
        some = repair_record(self.job, replaced=[("2024\\a.raw", 4096)],
                             left=[("2024\\b.raw", LeftReason.CHANGED, "")])
        when = formatting.local_time("2026-09-02T10:01:00Z")
        self.assertEqual(repaired_line(self.found, [some]),
                         f"Replaced {when}: 1 of these, each read back identical to the source; 1 left as it is "
                         "(see its details in History).")
        details = verification_details(self.found, None, "", (), [some])
        self.assertEqual(details.repaired, repaired_line(self.found, [some]))
        self.assertEqual(details.advice, "MirrorHold cannot tell which copy is damaged. If the source's copies are the "
                                         "good ones, replace the backup's: each backup copy moves into versions first.")

    def test_a_repair_in_history_and_its_details(self):
        some = repair_record(self.job, replaced=[("2024\\a.raw", 4096)],
                             left=[("2024\\b.raw", LeftReason.STILL_DIFFERS, "")])
        rows = history_entries((), (), {1: "Backup 1"}, repairs=[some])
        row, entry = rows[0]
        self.assertIs(entry, some)
        self.assertEqual((row.destination, row.how, row.result, row.copied, row.into_versions, row.renamed,
                          row.skipped, row.took),
                         ("1 Backup 1", "by you", "Copies replaced, 1 failed", "1 / 4.0 KiB", "2", "-", "1", "1 min"))
        started = formatting.local_time("2026-09-01T09:00:00Z")
        self.assertEqual(row.details, f"Replaced 1 of the 2 copies the verification of {started} found to differ; "
                                      "each read back identical to the source.")
        details = repair_details(some, Path("F:\\Mirror"), "Backup 1")
        self.assertEqual(details.title, f"Repair {formatting.local_time(some.started)} - Photos, destination 1 "
                                        '("Backup 1")')
        self.assertEqual([(f.path, f.why) for f in details.failed],
                         [("2024\\b.raw", "still differs after it was replaced: the destination drive may be "
                                          "failing")])
        self.assertEqual(details.left, ())
        self.assertEqual(details.versions_path, Path("F:\\Mirror\\.mirrorhold\\versions\\2026-09-02T10-00-00Z"))
        stopped = repair_record(self.job, replaced=[("2024\\a.raw", 4096)], state=RepairState.STOPPED)
        self.assertTrue(repair_details(stopped, None).summary.startswith("Stopped by you: replaced 1 of the 1 copy"))
        failed = repair_record(self.job, state=RepairState.FAILED)
        failed.detail = "the repair needs 1074 bytes with its margin and the destination has 10 free"
        self.assertEqual(repair_details(failed, None).summary,
                         "Did not finish: the repair needs 1074 bytes with its margin and the destination has 10 free. "
                         "No copy was replaced.")
        running = repair_record(self.job, state=RepairState.RUNNING)
        self.assertEqual(history_entries((), (), {}, repairs=[running])[0][0].result, "Interrupted")
        self.assertEqual(history_entries((), (), {}, {running.repair_id}, repairs=[running])[0][0].result,
                         "Replacing copies")

    def test_a_repair_in_the_runs_tab_and_the_closing_question(self):
        panel = run_panel(live_repair(self.job))
        self.assertEqual((panel.phase, panel.counts, panel.repair, panel.verification, panel.can_cancel,
                          panel.can_stop_now), ("replacing copies", "1 of 3", True, False, True, False))
        self.assertAlmostEqual(panel.fraction, 1 / 3)
        waiting = run_panel(live_repair(self.job, phase="waiting"))
        self.assertEqual((waiting.phase, waiting.counts), ("waiting for the run in progress", ""))
        self.assertEqual(closing_question(1, 1, 2), "1 run, 1 verification and 2 repairs are in progress. They "
                                                    "continue without this window; reopen MirrorHold to watch or "
                                                    "control them.")
        self.assertEqual(not_reached_words(False, repair=True), "The request did not reach the repair: another "
                                                               "program kept its control file open. Try again.")

    def test_a_repair_that_could_not_start_reads_so(self):
        log = activity_log(self.job, command="repair")
        self.assertEqual(start_failure_words(log), "Repair could not start - see History")

    def test_a_later_unfinished_pass_neither_hides_the_copies_that_differ_nor_their_repair(self):
        for state in (VerificationState.STOPPED, VerificationState.RUNNING, VerificationState.FAILED):
            later = verification(self.job, state=state, verification_id="w" * 32)
            first = self.rows(checks=[self.found, later]).destinations[0]
            self.assertEqual((first.status, first.found_differ, first.can_repair),
                             ("Copies differ: see verification", self.found, True), state)
        partial = verification(self.job, state=VerificationState.RUNNING, verification_id="w" * 32,
                               differ=[("2024\\c.raw", 1024)])
        first = self.rows(checks=[self.found, partial]).destinations[0]
        self.assertIs(first.found_differ, self.found, "what an unfinished pass found so far is never taken up")
        alone = self.rows(checks=[partial]).destinations[0]
        self.assertEqual((alone.found_differ, alone.can_repair), (None, False))

    def test_a_repairs_failures_read_as_failures_not_as_copies_replaced_or_left(self):
        failed = [("2024\\a.raw", LeftReason.NOT_COPIED, "Access is denied"),
                  ("2024\\b.raw", LeftReason.STILL_DIFFERS, "")]
        none = repair_record(self.job, left=failed)
        self.assertEqual(repair_result_words(none), "No copy replaced, 2 failed")
        mixed = repair_record(self.job, replaced=[("2024\\c.raw", 1)],
                              left=failed[:1] + [("2024\\d.raw", LeftReason.CHANGED, "")])
        self.assertEqual(repair_result_words(mixed), "Copies replaced, 1 failed, 1 left")
        kept = repair_record(self.job, left=[("2024\\d.raw", LeftReason.CHANGED, "")])
        self.assertEqual(repair_result_words(kept), "No copy replaced, 1 left")
        details = repair_details(mixed, None)
        self.assertEqual([(f.path, f.why) for f in details.failed],
                         [("2024\\a.raw", "could not be copied; its backup copy stays: Access is denied")])
        self.assertEqual([(f.path, f.why) for f in details.left],
                         [("2024\\d.raw", "changed since the verification: the next run copies it")])
        stuck = repair_record(self.job, left=[("2024\\a.raw", LeftReason.IN_VERSIONS, "Access is denied")])
        self.assertEqual([(f.path, f.why) for f in repair_details(stuck, None).failed],
                         [("2024\\a.raw", "could not be copied, and its backup copy could not be moved back out of "
                                          "this repair's versions: Access is denied")])

    def test_a_verification_tells_failed_left_and_unreached_copies_apart(self):
        found = verification(self.job, differ=[("2024\\a.raw", 1), ("2024\\b.raw", 1), ("2024\\c.raw", 1),
                                               ("2024\\d.raw", 1), ("2024\\e.raw", 1)])
        stopped = repair_record(self.job, replaced=[("2024\\a.raw", 1)],
                                left=[("2024\\b.raw", LeftReason.NOT_COPIED, ""),
                                      ("2024\\c.raw", LeftReason.CHANGED, "")], state=RepairState.STOPPED)
        when = formatting.local_time("2026-09-02T10:01:00Z")
        self.assertEqual(repaired_line(found, [stopped]),
                         f"Replaced {when}: 1 of these, each read back identical to the source; 1 failed and 1 left "
                         "as it is (see their details in History); 2 not reached by a repair yet.")
        failed = repair_record(self.job, left=[(path, LeftReason.STILL_DIFFERS, "") for path, _size in found.differ])
        self.assertEqual(repaired_line(found, [failed]),
                         f"Repair {when}: none of these replaced; 5 failed (see their details in History).")
        stuck = repair_record(self.job, left=[("2024\\a.raw", LeftReason.IN_VERSIONS, "")], repair_id="s" * 32)
        later = repair_record(self.job, left=[("2024\\a.raw", LeftReason.MISSING_DESTINATION, "")],
                              repair_id="t" * 32, started="2026-09-03T10:00:00Z", ended="2026-09-03T10:01:00Z")
        self.assertIn("; 1 failed (see its details in History); 4 not reached by a repair yet.",
                      repaired_line(found, [stuck, later]),
                      "a backup copy left in versions stays a failure whatever a later repair found")

    def test_a_repair_whose_process_died_names_the_copies_it_had_not_finished_as_failed(self):
        dead = repair_record(self.job, state=RepairState.RUNNING, ended=None)
        dead.replacing = [("2024\\a.raw", 4096), ("2024\\b.raw", 2048)]
        row = history_entries((), (), {1: "Backup 1"}, repairs=[dead])[0][0]
        self.assertEqual((row.result, row.into_versions, row.skipped), ("Interrupted", "2", "2"))
        working = history_entries((), (), {}, {dead.repair_id}, repairs=[dead])[0][0]
        self.assertEqual((working.result, working.into_versions, working.skipped), ("Replacing copies", "2", "0"))
        details = repair_details(dead, Path("F:\\Mirror"), "Backup 1")
        unfinished = ("the repair ended before it was done with it: its backup copy may be in this repair's "
                      "versions")
        self.assertEqual([(f.path, f.why) for f in details.failed],
                         [("2024\\a.raw", unfinished), ("2024\\b.raw", unfinished)])
        self.assertEqual(details.versions_path, Path("F:\\Mirror\\.mirrorhold\\versions\\2026-09-02T10-00-00Z"))
        self.assertTrue(details.summary.startswith("Interrupted (a shutdown, a crash or a closed console): "))
        self.assertEqual(repair_details(dead, None, "", {dead.repair_id}).failed, (), "its work in hand")
        when = formatting.local_time(dead.started)
        self.assertEqual(repaired_line(self.found, [dead]),
                         f"Repair {when}: none of these replaced; 2 failed (see their details in History).")
        self.assertEqual(verification_details(self.found, None, "", (), [dead]).repaired,
                         repaired_line(self.found, [dead]))
        self.assertEqual(repaired_line(self.found, [dead], {dead.repair_id}),
                         f"Repair {when}: none of these replaced; 2 not reached by a repair yet.")

    def test_a_repair_that_replaced_no_copy_is_never_told_as_one_that_replaced(self):
        """Where no copy was replaced, a verification's line and a repair's details say so, as
        History does of a completed repair, and never open with "Replaced" (the maintainer's
        decision, 2026-11-03)."""
        when = formatting.local_time("2026-09-02T10:01:00Z")
        started = formatting.local_time("2026-09-01T09:00:00Z")
        failures = [(path, LeftReason.STILL_DIFFERS, "") for path, _size in self.found.differ]
        failed = repair_record(self.job, left=failures)
        self.assertEqual(repair_result_words(failed), "No copy replaced, 2 failed")
        self.assertEqual(repaired_line(self.found, [failed]),
                         f"Repair {when}: none of these replaced; 2 failed (see their details in History).")
        self.assertEqual(repair_details(failed, None).summary,
                         f"None of the 2 copies the verification of {started} found to differ was replaced.")
        kept = repair_record(self.job, left=[("2024\\a.raw", LeftReason.CHANGED, "")])
        self.assertEqual(repaired_line(self.found, [kept]),
                         f"Repair {when}: none of these replaced; 1 left as it is (see its details in History); "
                         "1 not reached by a repair yet.")
        self.assertEqual(repair_details(kept, None).summary,
                         f"The copy the verification of {started} found to differ was not replaced.")
        stopped = repair_record(self.job, left=failures, state=RepairState.STOPPED)
        self.assertEqual(repair_details(stopped, None).summary,
                         f"Stopped by you: none of the 2 copies the verification of {started} found to differ was "
                         "replaced.")
        nothing = repair_record(self.job, state=RepairState.FAILED)
        nothing.detail = "the repair needs 1074 bytes with its margin and the destination has 10 free"
        self.assertEqual(repair_details(nothing, None).summary,
                         "Did not finish: the repair needs 1074 bytes with its margin and the destination has 10 "
                         "free. No copy was replaced.")
        dead = repair_record(self.job, state=RepairState.RUNNING, ended=None)
        dead.replacing = [("2024\\a.raw", 4096), ("2024\\b.raw", 2048)]
        working = formatting.local_time(dead.started)
        self.assertEqual(repaired_line(self.found, [dead]),
                         f"Repair {working}: none of these replaced; 2 failed (see their details in History).")
        self.assertEqual(repaired_line(self.found, [dead], {dead.repair_id}),
                         f"Repair {working}: none of these replaced; 2 not reached by a repair yet.")
        some = repair_record(self.job, replaced=[("2024\\a.raw", 4096)])
        self.assertTrue(repaired_line(self.found, [some]).startswith(f"Replaced {when}: 1 of these, "))

    def test_several_repairs_of_one_pass_are_counted_together_and_dated_by_the_last(self):
        """The line counts every repair of the pass, so a copy an earlier repair replaced is never
        dated by a later one's time: the replaced copies are dated by the last repair that
        replaced one, and where several did the line says how many and that the time is the
        last's (the maintainer's decision, 2026-11-03)."""
        first = repair_record(self.job, replaced=[("2024\\a.raw", 4096)])
        when = formatting.local_time("2026-09-02T10:01:00Z")
        later = repair_record(self.job, left=[("2024\\b.raw", LeftReason.STILL_DIFFERS, "")], repair_id="t" * 32,
                              started="2026-09-03T10:00:00Z", ended="2026-09-03T10:01:00Z")
        last = formatting.local_time("2026-09-03T10:01:00Z")
        self.assertEqual(repaired_line(self.found, [first, later]),
                         f"Replaced {when}: 1 of these, each read back identical to the source; 1 failed (see its "
                         "details in History).", "a later repair that replaced nothing does not date the copy")
        both = repair_record(self.job, replaced=[("2024\\b.raw", 2048)], repair_id="t" * 32,
                             started="2026-09-03T10:00:00Z", ended="2026-09-03T10:01:00Z")
        self.assertEqual(repaired_line(self.found, [first, both]),
                         f"Replaced by 2 repairs, the last {last}: 2 of these, each read back identical to the "
                         "source.")
        missed = repair_record(self.job, left=[("2024\\a.raw", LeftReason.STILL_DIFFERS, "")])
        self.assertEqual(repaired_line(self.found, [missed, later]),
                         f"2 repairs, the last {last}: none of these replaced; 2 failed (see their details in "
                         "History).")


class RepairSessionTest(TempDirTest):
    """The window's side of a repair: the session starts it as a core process, and the
    refresher reads the repairs' records."""

    def test_the_session_starts_a_repair_and_the_refresher_reads_its_record(self):
        s, d = volume(1, "S"), volume(2, "F")
        job = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "Mirror")])
        path = self.tmp / "state" / "store.json"
        ConfigStore(path).save(StoreContents((job,)))
        started = []

        def repairer(store_path, job_id, destination):
            started.append((job_id, destination))
            return None

        session = GuiSession(path, volume_source=FixedVolumeSource(()), sync_roots=(), repairer=repairer)
        session.load()
        marker = job.destinations[0].marker_id
        session.start_repair(job.job_id, marker)
        self.assertEqual(started, [(job.job_id, marker)])
        write_repair(runtime_layout(path), repair_record(job, replaced=[("a.raw", 10)]))
        seen = observe(session.contents, runtime_layout(path), FixedVolumeSource(()))
        self.assertEqual([r.replaced for r in seen.repairs[job.job_id]], [[("a.raw", 10)]])
        again = observe(session.contents, runtime_layout(path), FixedVolumeSource(()), seen)
        self.assertIs(again.repairs[job.job_id], seen.repairs[job.job_id], "read again only when its folder changes")


class LostRecordAndRefusalWordsTest(PreviewCase):
    """The words of a shared destination whose record of what to leave was lost, and of
    the refusals a job meets when it is made that look at this computer (the maintainer's
    decisions of 2026-10-17 and, for a destination that is a file, 2026-10-19)."""

    def lose_the_record(self):
        write(self.src, "Photos\\a.jpg", b"a")
        write(self.dst, "Photos\\a.jpg", b"a")
        write(self.dst, "Photos\\old.jpg", b"there before")
        write_marker(self.dst, Marker(self.job.job_id, self.job.destinations[0].marker_id, shared=True))

    def test_the_preview_of_a_lost_record_offers_only_to_leave(self):
        self.lose_the_record()
        pair = self.preview().pairs[0]
        self.assertEqual(pair.headline, "THE RECORD OF WHAT THIS FOLDER KEEPS AS IT IS WAS LOST - its first run left "
                                        "what the folder held where it is. 1 file there is not in the source.")
        self.assertEqual([(c.content, c.label, c.offered) for c in pair.choices],
                         [(Content.LEAVE, "Leave them where they are", True),
                          (Content.MOVE, "Move them into versions - not offered:", False)])
        self.assertTrue(pair.choices[0].detail.endswith(
            "Files this job backed up here whose source copies are gone, if any, are among them: they stay as they "
            "are from now on."), pair.choices[0].detail)
        self.assertEqual(pair.choices[1].detail, "this folder's first run left what it held where it is.")
        self.assertIs(pair.content, Content.LEAVE)
        self.assertTrue(pair.needs_review)

    def test_a_lost_records_preview_does_not_say_there_are_files_whose_source_copies_are_gone(self):
        """With the record lost nothing tells whether the job backed up there a file whose
        source copy is gone since, so leaving says "if any", as the command line does (the
        maintainer's decision, 2026-11-03)."""
        self.lose_the_record()
        detail = self.preview().pairs[0].choices[0].detail
        self.assertTrue(detail.endswith(
            "Files this job backed up here whose source copies are gone, if any, are among them: they stay as they "
            "are from now on."), detail)

    def test_a_lost_record_waits_for_a_review_on_its_row(self):
        waiting = record(self.job, 1, Outcome.HELD,
                         reasons=("first-run", "destination-holds-content", "shared-record-lost"))
        key = (self.job.job_id, self.job.destinations[0].marker_id)
        row = build_rows(StoreContents((self.job,)), (self.s, self.d), {key: MarkerState.MATCH},
                         {self.job.job_id: (waiting,)}, ())[0]
        first = row.destinations[0]
        self.assertEqual((first.status, first.tone, first.needs_review), ("Record lost: review", Tone.ATTENTION, True))

    def test_the_reasons_in_words(self):
        self.assertEqual(reason_words("shared-record-lost"), "its record of what to leave was lost")
        self.assertEqual(reason_words("source-not-a-folder"), "the source is a file, not a folder")
        self.assertEqual(reason_words("destination-is-system-folder"),
                         "the folder itself is one Windows or MirrorHold needs")

    def test_the_editor_and_an_import_say_why_each_is_refused(self):
        write(self.vol_s, "notes.txt", b"a file")
        store_path = self.vol_d / "Tool" / "mirrorhold.json"
        volumes = (self.s, self.d)
        draft = JobDraft(name="Notes")
        draft.set_source(str(self.vol_s / "notes.txt"), volumes)
        draft.add_destination(str(self.vol_d / "Tool"), volumes)
        refused = [c.text for c in check(draft, volumes, (), (), store_path).checks if c.level == REFUSED]
        tool = Path(store_path).resolve().parent
        self.assertEqual(refused, ["Source: this is a file, not a folder; a job backs up a folder or a whole drive.",
                                   f"Destination 1: this folder is {tool}, which MirrorHold needs: backing up here "
                                   "would write into it. Choose a folder of its own instead."])
        self.assertEqual([c for c in check(draft, volumes, (), ()).checks if c.level == REFUSED], [],
                         "without the job file in use, the checks look at no disk")
        job = new_job("Notes", endpoint(self.s, "notes.txt"), [endpoint(self.d, "Copy")])
        result = apply_import(StoreContents(()), (job,), replace_existing=False, volumes=volumes, sync_roots=(),
                              store_path=store_path)
        self.assertEqual(result.refused,
                         (("Notes", "source: this is a file, not a folder; a job backs up a folder or a whole drive"),))

    def test_a_destination_that_is_a_file_in_the_editor_the_preview_the_job_list_and_history(self):
        write(self.src, "a.txt", "a")
        write(self.vol_d, "Backup", b"a file where the backup's folder would be")
        volumes = (self.s, self.d)
        draft = JobDraft(name="Work")
        draft.set_source(str(self.src), volumes)
        draft.add_destination(str(self.dst), volumes)
        refused = [c.text for c in check(draft, volumes, (), (), self.store.path).checks if c.level == REFUSED]
        self.assertEqual(refused, ["Destination 1: this is a file, not a folder; a backup is written into a folder or "
                                   "onto a whole drive."])
        pair = self.preview().pairs[0]
        self.assertEqual((pair.state, pair.headline),
                         (PairState.CANNOT_RUN, "CANNOT RUN - the destination is a file, not a folder (the job is "
                                                "refused at this destination)."))
        stopped = record(self.job, 1, Outcome.STOPPED, reasons=("destination-not-a-folder",))
        key = (self.job.job_id, self.job.destinations[0].marker_id)
        row = build_rows(StoreContents((self.job,)), volumes, {key: MarkerState.MATCH},
                         {self.job.job_id: (stopped,)}, ())[0]
        self.assertEqual(row.destinations[0].status, "Stopped: the destination is a file, not a folder")
        self.assertEqual(record_details(stopped, None).summary, "Stopped: the destination is a file, not a folder.")


class SeveralAtOnceTest(unittest.TestCase):
    """What the window shows while the runs and verifications a group or several items
    started work together: the Runs tab in line order, an activity starting, each row's kind
    and which of a run's destinations it is on, no counts before a plan, every start that
    failed in one window, a waiting row's place in line first in the job list; and verifying
    every destination a job, a group or several items reach after one question."""

    QUEUED = "2026-10-19T10:00:00.{}Z"

    def setUp(self):
        self.s, self.d, self.e = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1"), volume(3, "K")
        self.alpha = new_job("Alpha", endpoint(self.s, "Alpha"), [endpoint(self.d, "Alpha"), endpoint(self.e, "Alpha")])
        self.beta = new_job("Beta", endpoint(self.s, "Beta"), [endpoint(self.d, "Beta")])
        self.gamma = new_job("Gamma", endpoint(self.s, "Gamma"), [endpoint(self.d, "Gamma")])
        group = new_group("Probe", (self.alpha.job_id, self.beta.job_id, self.gamma.job_id))
        self.contents = StoreContents((self.alpha, self.beta, self.gamma), groups=(group,))
        self.markers = {(j.job_id, d.marker_id): MarkerState.MATCH for j in self.contents.jobs for d in j.destinations}

    def entries(self, *, runs=(), markers=None, checks=None, volumes=None):
        done = {j.job_id: tuple(record(j, i, Outcome.COMPLETED) for i in range(1, len(j.destinations) + 1))
                for j in self.contents.jobs}
        rows = build_rows(self.contents, volumes or (self.s, self.d, self.e), markers or self.markers, done, runs,
                          checks or {})
        return arrange(self.contents, rows)

    def current(self, job, run_id, queued, **status):
        """A status as this copy publishes it: whether it holds its turn, and when it joined the line."""
        return live(job, **dict(dict(run_id=run_id, admitted=False, claim=None, queued=self.QUEUED.format(queued),
                                     started="2026-10-19T10:00:00Z"), **status))

    @staticmethod
    def waiting_for(**other):
        return dict(dict(run_id="w" * 32, kind="run", job_name="Work", paused=False, waiting=False, drive=""), **other)

    def test_the_runs_tab_lists_the_activities_in_line_order(self):
        working = self.current(self.gamma, "g" * 32, "300000", admitted=True, destination="destination 1 at F:\\Gamma")
        second = self.current(self.beta, "b" * 32, "100000", phase="waiting", pair_index=0, place=2,
                              waiting_for=self.waiting_for(job_name="Gamma"))
        first = self.current(self.alpha, "a" * 32, "200000", phase="waiting", pair_index=0, place=1,
                             waiting_for=self.waiting_for(job_name="Gamma"))
        starting = self.current(self.beta, "c" * 32, "050000", kind="verification", phase="waiting",
                                marker_id=self.beta.destinations[0].marker_id, verify={"files": 0})
        older = live(self.alpha, run_id="o" * 32, started="2026-10-19T09:59:59Z", destination="destination 1 at F:\\A")
        panels = live_panels([starting, second, working, first, older])
        self.assertEqual([p.title for p in panels], ["Alpha -> 1  F:\\A", "Gamma -> 1  F:\\Gamma", "Alpha", "Beta",
                                                     "Verification: Beta -> 1"])

    def test_an_older_copys_run_started_in_the_same_second_stands_before_a_later_one(self):
        later = self.current(self.gamma, "g" * 32, "900000", admitted=True, destination="destination 1 at F:\\Gamma")
        older = live(self.alpha, run_id="o" * 32, started="2026-10-19T10:00:00Z", destination="destination 1 at F:\\A")
        self.assertEqual([p.title for p in live_panels([later, older])], ["Alpha -> 1  F:\\A", "Gamma -> 1  F:\\Gamma"])

    def test_one_starting_is_not_counted_as_waiting(self):
        self.assertFalse(waits(self.current(self.beta, "b" * 32, "1", phase="waiting", pair_index=0).status))
        self.assertTrue(waits(self.current(self.beta, "b" * 32, "1", phase="waiting", pair_index=0, place=1,
                                           waiting_for=self.waiting_for()).status))
        self.assertTrue(waits(live(self.beta, phase="waiting", pair_index=0).status), "an older copy's waits")
        self.assertFalse(waits(live(self.beta).status))

    def test_lists_are_joined_with_and(self):
        self.assertEqual([formatting.joined(names) for names in ([], ["A"], ["A", "B"], ["A", "B", "C"])],
                         ["", "A", "A and B", "A, B and C"])

    def test_an_activity_starting_says_so_where_an_older_copys_waits_for_the_run_in_progress(self):
        fresh = self.current(self.beta, "b" * 32, "1", phase="waiting", pair_index=0)
        admitted = self.current(self.beta, "b" * 32, "1", phase="waiting", pair_index=0, admitted=True)
        check = self.current(self.beta, "c" * 32, "1", kind="verification", phase="waiting",
                             marker_id=self.beta.destinations[0].marker_id, verify={"files": 0})
        fix = self.current(self.beta, "e" * 32, "1", kind="repair", phase="waiting",
                           marker_id=self.beta.destinations[0].marker_id, selected=[1])
        panels = [run_panel(r) for r in (fresh, admitted, check, fix)]
        self.assertEqual([(p.phase, p.waiting, p.can_pause) for p in panels], [("starting", True, False)] * 4)
        self.assertEqual(run_panel(live(self.beta, phase="waiting", pair_index=0)).phase,
                         "waiting for the run in progress")
        beta = self.entries(runs=[fresh])[0].jobs[1]
        self.assertEqual((beta.status, beta.destinations[0].status), ("Starting", "Starting"))
        verifying = self.entries(runs=[check])[0].jobs[1]
        self.assertEqual((verifying.status, verifying.destinations[0].status), ("Verifying", "Starting"))
        older = self.entries(runs=[live(self.beta, phase="waiting", pair_index=0)])[0].jobs[1]
        self.assertEqual(older.status, "Waiting to run")

    def test_an_activity_that_has_asked_for_its_turn_and_names_nothing_waits_for_it(self):
        claim = {"reads": [], "writes": []}
        asked = self.current(self.beta, "b" * 32, "1", phase="waiting", pair_index=0, claim=claim)
        self.assertEqual((run_panel(asked).phase, waits(asked.status)), ("waiting for its turn", True))
        self.assertEqual(self.entries(runs=[asked])[0].jobs[1].status, "Waiting to run")
        gave_way = self.current(self.beta, "c" * 32, "1", kind="verification", phase="waiting", claim=claim,
                                marker_id=self.beta.destinations[0].marker_id, verify={"files": 0, "gave_way": True})
        self.assertEqual((run_panel(gave_way).phase, waits(gave_way.status)), ("waiting: a backup runs first", True))
        self.assertEqual(self.entries(runs=[gave_way])[0].jobs[1].destinations[0].status, "Waiting to verify")

    def test_a_rows_title_names_its_kind_and_a_runs_place_among_its_destinations(self):
        def title(job, **status):
            return run_panel(live(job, destination="destination 1 at F:\\M", **status)).title

        self.assertEqual(title(self.alpha, pair_total=2), "Alpha -> 1 of 2  F:\\M")
        self.assertEqual(title(self.beta, pair_total=1), "Beta -> 1  F:\\M")
        self.assertEqual(title(self.beta, kind="verification", pair_total=1, verify={"files": 0}),
                         "Verification: Beta -> 1  F:\\M")
        self.assertEqual(run_panel(live(self.beta, kind="repair", pair_total=1)).title, "Repair: Beta -> 1")

    def test_a_run_shows_no_counts_before_it_has_a_plan(self):
        self.assertEqual(run_panel(live(self.beta, phase="listing", pair_index=0)).counts, "")
        self.assertEqual(run_panel(live(self.beta, phase="planning", paused=True)).counts, "resumes where it stopped")
        copying = run_panel(live(self.beta, planned={"files_to_copy": 2, "bytes_to_copy": 2 * 1024 ** 2},
                                 done={"files_copied": 1, "bytes_copied": 1024 ** 2}))
        self.assertEqual(copying.counts, "1 of 2 files   1.0 MiB of 2.0 MiB")

    def test_the_starts_that_failed_are_said_in_one_window(self):
        self.assertEqual(not_started_words([LaunchProblem("Alpha", "the engine cannot be used")]),
                         ("Run not started", 'The run of "Alpha" could not start: the engine cannot be used'))
        self.assertEqual(not_started_words([LaunchProblem("Alpha", "x"), LaunchProblem("Beta", "y"),
                                            LaunchProblem("Gamma", "z")]),
                         ("3 runs not started", "Alpha: x\nBeta: y\nGamma: z"))
        self.assertEqual(not_started_words([LaunchProblem("Alpha", "x"), LaunchProblem("Beta", "y", "verification"),
                                            LaunchProblem("Gamma", "z", "verification")]),
                         ("1 run and 2 verifications not started",
                          "Alpha (run): x\nBeta (verification): y\nGamma (verification): z"))

    def test_a_waiting_rows_place_in_line_comes_first_in_the_job_list(self):
        def words(**status):
            beta = self.entries(runs=[live(self.beta, phase="waiting", pair_index=0, **status)])[0].jobs[1]
            return beta.status

        def check_words(**status):
            check = live(self.beta, phase="waiting", kind="verification", run_id="c" * 32,
                         marker_id=self.beta.destinations[0].marker_id, verify={"files": 0}, **status)
            return self.entries(runs=[check])[0].jobs[1].destinations[0].status

        self.assertEqual(words(place=2, waiting_for=self.waiting_for(drive="drive I:")),
                         "2nd in line, waiting for 'Work' (drive I:)")
        self.assertEqual(words(place=1, waiting_for=self.waiting_for(kind="verification")),
                         "Next in line, waiting for a verification of 'Work'")
        self.assertEqual(words(waiting_for=self.waiting_for()), "Waiting for 'Work'")
        self.assertEqual(check_words(place=1, waiting_for=self.waiting_for(drive="drive I:")),
                         "Verification next in line, waiting for 'Work' (drive I:)")
        self.assertEqual(check_words(waiting_for=self.waiting_for(kind="repair")),
                         "Verification waiting for a repair of 'Work'")

    def test_verify_reaches_every_destination_of_a_group_a_job_or_several_items(self):
        entries = self.entries()
        probe = entries[0]
        by_iid = row_by_iid(entries)
        alpha, beta, gamma = probe.jobs
        everything = alpha.destinations + beta.destinations + gamma.destinations
        self.assertEqual(Selection.of([probe], by_iid).destinations, everything)
        self.assertEqual(Selection.of([alpha], by_iid).destinations, alpha.destinations)
        several = Selection.of([gamma.destinations[0], alpha, alpha.destinations[1]], by_iid)
        self.assertEqual(several.destinations, alpha.destinations + gamma.destinations, "each once, in the list's order")
        self.assertEqual(Selection.of([beta.destinations[0]], by_iid).destinations, beta.destinations)
        self.assertEqual(Selection.of([probe], by_iid).verifiable, everything)

    def test_run_now_runs_a_destination_selected_alone_and_a_job_or_group_selected_whole(self):
        entries = self.entries()
        probe = entries[0]
        by_iid = row_by_iid(entries)
        alpha, beta, gamma = probe.jobs
        a1, a2 = (d.marker_id for d in self.alpha.destinations)
        self.assertEqual(Selection.of([alpha.destinations[1]], by_iid).chosen, {alpha.job_id: (a2,)})
        self.assertEqual(Selection.of([alpha], by_iid).chosen, {})
        self.assertEqual(Selection.of([probe], by_iid).chosen, {})
        several = Selection.of([beta.destinations[0], alpha.destinations[1], alpha.destinations[0]], by_iid)
        self.assertEqual(several.chosen, {alpha.job_id: (a1, a2), beta.job_id: (self.beta.destinations[0].marker_id,)},
                         "in the job's order")
        mixed = Selection.of([alpha.destinations[1], alpha, gamma.destinations[0]], by_iid)
        self.assertEqual(mixed.chosen, {gamma.job_id: (self.gamma.destinations[0].marker_id,)},
                         "a job whose own row is selected runs whole")

    def test_the_question_before_several_jobs_names_those_run_for_some_destinations(self):
        probe = self.entries()[0]
        alpha, beta, gamma = probe.jobs
        a1, a2 = (d.marker_id for d in self.alpha.destinations)
        g1 = self.gamma.destinations[0].marker_id
        plan = run_several(probe.jobs, chosen={alpha.job_id: (a2,), gamma.job_id: (g1,)})
        self.assertEqual(plan.text.splitlines()[0], "Starts: Alpha (destination 2), Beta, Gamma (destination 1).")
        self.assertEqual((plan.start, plan.destinations),
                         ((alpha.job_id, beta.job_id, gamma.job_id), {alpha.job_id: (a2,), gamma.job_id: (g1,)}))
        both = run_several(probe.jobs[:1], chosen={alpha.job_id: (a1, a2)})
        self.assertEqual(both.text.splitlines()[0], "Starts: Alpha (destinations 1 and 2).")
        self.assertEqual(run_several(probe.jobs).destinations, {}, "every job whole")

    def test_a_run_of_some_destinations_is_titled_by_the_destination_alone(self):
        markers = [d.marker_id for d in self.alpha.destinations]

        def title(**status):
            return run_panel(live(self.alpha, destination="destination 2 at F:\\M", pair_index=2, markers=markers,
                                  **status)).title

        self.assertEqual(title(pair_total=1, selected=[2]), "Alpha -> 2  F:\\M", "never '2 of 1'")
        self.assertEqual(title(pair_total=2, selected=[1, 2]), "Alpha -> 2 of 2  F:\\M", "every destination")

    def test_the_question_before_several_destinations_are_verified(self):
        markers = dict(self.markers)
        markers[(self.gamma.job_id, self.gamma.destinations[0].marker_id)] = MarkerState.MISSING
        stopped = verification(self.beta, state=VerificationState.STOPPED, verification_id="u" * 32)
        probe = self.entries(markers=markers, checks={self.beta.job_id: (stopped,)}, volumes=(self.s, self.d))[0]
        jobs = {job.job_id: job for job in probe.jobs}
        destinations = Selection.of([probe], row_by_iid([probe])).destinations
        plan = verify_several(destinations, jobs, "Probe")
        self.assertEqual((plan.title, plan.new_copies_button, plan.full_button, plan.full_default, plan.start),
                         ('Verify 2 destinations of "Probe"?', "New copies only", "Full verification", True,
                          ((self.alpha.job_id, 1), (self.beta.job_id, 1))))
        self.assertEqual(plan.text.splitlines(),
                         ["Verifies: Alpha 1, Beta 1.",
                          "New copies only reads what was backed up or changed since it was last verified; Alpha 1 "
                          "and Beta 1 have no list of verified copies yet and are read in full.",
                          "A full verification continues the unfinished one of Beta 1.",
                          "Not verified: Alpha 2 (its drive is not attached), Gamma 1 (its first run is not confirmed "
                          "yet).",
                          "Verifications that use the same drive run one at a time unless it is a solid-state or "
                          "cloud drive, as Windows reports or as set in Preferences; the others run at the same time.",
                          "A backup that is due runs first: the verifications step aside and continue."])
        together = verify_several(destinations, jobs, turns=DriveTurns.AT_THE_SAME_TIME)
        self.assertEqual(together.title, "Verify 2 destinations?")
        self.assertIn("They run at the same time; verifications that use the same drive share its speed.",
                      together.text.splitlines())
        alpha = probe.jobs[0]
        one = verify_several(alpha.destinations, jobs, "Alpha")
        self.assertEqual((one.title, one.new_copies_button, one.full_button, one.text.splitlines()),
                         ('Verify 1 destination of "Alpha"?', "New copies only", "Full verification",
                          ["Verifies: Alpha 1.",
                           "New copies only reads what was backed up or changed since it was last verified; Alpha 1 "
                           "has no list of verified copies yet and is read in full.",
                           "Not verified: Alpha 2 (its drive is not attached).",
                           "A backup that is due runs first: the verification steps aside and continues."]))
        at_work = self.entries(runs=[live_verification(self.gamma)])[0]
        gamma = at_work.jobs[2]
        busy = verify_several(at_work.jobs[0].destinations[:1] + gamma.destinations, jobs)
        self.assertEqual(busy.text.splitlines()[2], "Not verified: Gamma 1 (it is being verified).")


NEW = VerificationMode.NEW_COPIES


class NewCopiesWordsTest(unittest.TestCase):
    """A verification of new copies in the window: the Verify dialog's three choices, which
    it selects at first and what each starts; a pass of new copies in History, its details,
    the job list and the Runs tab; the Verified column counting full passes alone."""

    NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.s, self.d = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1")
        self.job = new_job("Photos", endpoint(self.s, "Photos"), [endpoint(self.d, "Mirror")])
        self.key = (self.job.job_id, self.job.destinations[0].marker_id)
        self.full = verification(self.job)                                     # began 2026-09-01: not due
        self.quick = replace(verification(self.job, verification_id="w" * 32, ended="2026-10-05T10:00:00Z"),
                             started="2026-10-05T09:00:00Z", mode=NEW, unchanged_files=98765)

    def rows(self, *, checks=(), runs=()):
        done = record(self.job, 1, Outcome.COMPLETED, ended="2026-06-01T10:00:00Z")
        return build_rows(StoreContents((self.job,)), (self.s, self.d), {self.key: MarkerState.MATCH},
                          {self.job.job_id: (done,)}, runs, {self.job.job_id: tuple(checks)}, self.NOW)[0]

    def test_the_dialog_offers_new_copies_a_full_verification_and_continuing(self):
        never = verify_dialog("Photos", self.rows().destinations[0], 90, None)
        self.assertEqual(never.intro, "Compares the backup's copies with the source's, reading both in full, to find "
                                      "copies damaged on a drive. It changes nothing on either drive.")
        self.assertEqual((never.new_copies, never.full),
                         ("New copies only: the files backed up or changed since they were last verified",
                          "Full verification: every file, to find copies damaged on the drive since they were "
                          "written"))
        self.assertEqual((never.choice, never.new_copies_note, never.new_last, never.resume),
                         ("full", "No list of verified copies is kept for this destination yet: this time every file "
                                  "is read, and it counts as a full verification.", "", ""))
        kept = verify_dialog("Photos", self.rows(checks=[self.full]).destinations[0], 90, None, list_kept=True)
        self.assertEqual((kept.choice, kept.new_copies_note, kept.new_last), ("new", "", ""))
        later = verify_dialog("Photos", self.rows(checks=[self.full, self.quick]).destinations[0], 90, None,
                              list_kept=True)
        self.assertRegex(later.new_last, r"^New copies last verified: 2026-10-0[456]\.$")
        self.assertRegex(later.last, r"^Last full verification: 2026-0[89]-\d\d\. Reminder every 90 days\.$")
        due = verify_dialog("Photos", self.rows(checks=[self.quick]).destinations[0], 90, None, list_kept=True)
        self.assertEqual(due.choice, "full", "a full verification is due: a pass of new copies does not count")
        stopped = verification(self.job, state=VerificationState.STOPPED, compared=12)
        resume = verify_dialog("Photos", self.rows(checks=[self.full]).destinations[0], 90, stopped, list_kept=True)
        self.assertEqual(resume.choice, "continue")
        self.assertRegex(resume.resume, r"^Continue the full verification stopped .+ \(12 of 14 files\)$")
        interrupted = replace(verification(self.job, state=VerificationState.RUNNING, compared=2), mode=NEW)
        self.assertRegex(verify_dialog("Photos", self.rows().destinations[0], 90, interrupted).resume,
                         r"^Continue the verification of new copies interrupted .+ \(2 of 4 files\)$")

    def test_what_each_choice_starts(self):
        full = verification(self.job, state=VerificationState.STOPPED)
        quick = replace(full, mode=NEW)
        request = verify_model.verify_request
        self.assertEqual([request(choice, full) for choice in ("continue", "new", "full")],
                         [(False, False), (True, False), (False, True)])
        self.assertEqual([request(choice, quick) for choice in ("continue", "new", "full")],
                         [(True, False), (True, True), (False, False)])
        self.assertEqual([request(choice, None) for choice in ("new", "full")], [(True, False), (False, False)])
        self.assertEqual([request("new", unfinished, list_kept=False) for unfinished in (full, quick, None)],
                         [(False, True), (False, False), (False, False)],
                         "without a list, new copies are a full verification from the beginning")

    def test_a_pass_of_new_copies_in_history_and_its_details(self):
        self.assertEqual(verify_result_words(self.quick), "New copies verified: no differences")
        found = replace(self.quick, differ=[], differ_total=0)
        found.add_differ("2024\\photo-0001.raw", 4096)
        self.assertEqual(verify_result_words(found), "New copies verified: 1 differs")
        stopped = replace(self.quick, state=VerificationState.STOPPED, ended=None)
        self.assertEqual(verify_result_words(stopped), "Verification of new copies stopped")
        running = replace(self.quick, state=VerificationState.RUNNING, ended=None)
        self.assertEqual(verify_result_words(running, {"w" * 32}), "Verifying new copies")
        details = verification_details(self.quick, None, "Backup 1")
        self.assertEqual(details.summary, "Finished: 3 new copies compared (3.0 KiB) in 1 min; 98,765 verified "
                                          "earlier and unchanged, not read.")
        self.assertTrue(details.title.startswith("Verification of new copies 2026-10-0"), details.title)
        self.assertEqual(verification_details(stopped, None).summary,
                         "Stopped by you: 3 of 5 new copies compared; Verify... continues from there.")
        entries = history_entries((), (self.full, self.quick), {1: "Backup 1"})
        self.assertEqual([row.result for row, _ in entries], ["New copies verified: no differences",
                                                              "Verified: no differences"])

    def test_the_job_list_and_the_runs_tab_tell_a_pass_of_new_copies(self):
        only_quick = self.rows(checks=[self.quick]).destinations[0]
        self.assertEqual((only_quick.verified, only_quick.verification_due), ("never - due", True))
        self.assertIs(only_quick.last_verification, self.quick)
        at_work = self.rows(runs=[live_verification(self.job, mode="new-copies")]).destinations[0]
        self.assertEqual(at_work.status, "Verifying new copies 40 %")
        stopped_quick = replace(self.quick, state=VerificationState.STOPPED, ended=None)
        quick_left = self.rows(checks=[self.full, stopped_quick]).destinations[0]
        self.assertEqual((quick_left.status, quick_left.unfinished.verification_id),
                         ("Verification of new copies stopped at 60 %", "w" * 32))
        stopped_full = verification(self.job, state=VerificationState.STOPPED, verification_id="f" * 32)
        both = self.rows(checks=[stopped_full, stopped_quick]).destinations[0]
        self.assertEqual((both.status, both.unfinished.verification_id), ("Verification stopped at 60 %", "f" * 32))
        self.assertEqual(run_panel(live_verification(self.job, mode="new-copies")).title,
                         "Verification (new copies): Photos -> 1")
        self.assertEqual(run_panel(live_verification(self.job)).title, "Verification: Photos -> 1")


class VerifySeveralKindsTest(unittest.TestCase):
    """The question before several destinations are verified, answered with their new copies
    or a full verification: which have no list to build on, which unfinished pass each answer
    continues, and which answer it offers by default."""

    def setUp(self):
        self.s, self.d, self.e = volume(1, "S", label="Media"), volume(2, "F", label="Backup 1"), volume(3, "K")
        self.alpha = new_job("Alpha", endpoint(self.s, "Alpha"), [endpoint(self.d, "Alpha"), endpoint(self.e, "Alpha")])
        self.beta = new_job("Beta", endpoint(self.s, "Beta"), [endpoint(self.d, "Beta")])
        self.gamma = new_job("Gamma", endpoint(self.s, "Gamma"), [endpoint(self.d, "Gamma")])
        group = new_group("Probe", (self.alpha.job_id, self.beta.job_id, self.gamma.job_id))
        self.contents = StoreContents((self.alpha, self.beta, self.gamma), groups=(group,))
        markers = {(j.job_id, d.marker_id): MarkerState.MATCH for j in self.contents.jobs for d in j.destinations}
        done = {j.job_id: tuple(record(j, i, Outcome.COMPLETED) for i in range(1, len(j.destinations) + 1))
                for j in self.contents.jobs}
        stopped_full = verification(self.beta, state=VerificationState.STOPPED, verification_id="u" * 32)
        stopped_quick = replace(verification(self.gamma, state=VerificationState.STOPPED, verification_id="q" * 32),
                                mode=NEW)
        rows = build_rows(self.contents, (self.s, self.d, self.e), markers, done, (),
                          {self.beta.job_id: (stopped_full,), self.gamma.job_id: (stopped_quick,)},
                          datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
        probe = arrange(self.contents, rows)[0]
        self.jobs = {job.job_id: job for job in probe.jobs}
        self.destinations = Selection.of([probe], row_by_iid([probe])).destinations
        self.every = {(d.job_id, d.index) for d in self.destinations}

    def test_the_question_says_what_each_answer_reads_and_continues(self):
        kept = self.every - {(self.gamma.job_id, 1)}
        plan = verify_several(self.destinations, self.jobs, "Probe", kept=kept)
        self.assertEqual(plan.text.splitlines(),
                         ["Verifies: Alpha 1, Alpha 2, Beta 1, Gamma 1.",
                          "New copies only reads what was backed up or changed since it was last verified; Gamma 1 "
                          "has no list of verified copies yet and is read in full.",
                          "A full verification continues the unfinished one of Beta 1.",
                          "New copies only continues the unfinished one of Gamma 1.",
                          "Verifications that use the same drive run one at a time unless it is a solid-state or "
                          "cloud drive, as Windows reports or as set in Preferences; the others run at the same time.",
                          "A backup that is due runs first: the verifications step aside and continue."])
        self.assertEqual((plan.title, plan.new_copies_button, plan.full_button, plan.full_default),
                         ('Verify 4 destinations of "Probe"?', "New copies only", "Full verification", True))
        everything = verify_several(self.destinations, self.jobs, "Probe", kept=self.every)
        self.assertEqual(everything.text.splitlines()[1],
                         "New copies only reads what was backed up or changed since it was last verified.")
        self.assertFalse(everything.full_default)
        nothing = verify_several(self.destinations, self.jobs, "Probe")
        self.assertTrue(nothing.text.splitlines()[1].endswith(
            "; Alpha 1, Alpha 2, Beta 1 and Gamma 1 have no list of verified copies yet and are read in full."))


class MarkersAfterRecordsTest(TempDirTest):
    """A job's markers are read again once its run records change: a run serving several
    destinations records each as it finishes it, so a first backup just made there stops
    asking for its go-ahead while the run goes on to the next destination."""

    def test_a_jobs_markers_are_read_again_once_its_records_change(self):
        root = self.tmp / "volD"
        (root / "One").mkdir(parents=True)
        s, d = volume(1, "S"), volume(2, mounts=(str(root) + "\\",))
        job = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "One"), endpoint(d, "Two")])
        contents = StoreContents((job,))
        layout = runtime_layout(self.tmp / "state" / "store.json")
        source = FixedVolumeSource((s, d))
        key = (job.job_id, job.destinations[0].marker_id)
        first = observe(contents, layout, source)
        self.assertEqual(first.markers[key], MarkerState.MISSING)
        write_marker(root / "One", Marker(job.job_id, job.destinations[0].marker_id))
        quiet = observe(contents, layout, source, first)
        self.assertEqual(quiet.markers[key], MarkerState.MISSING, "a marker is not read again at every look")
        write_record(layout, record(job, 1, Outcome.COMPLETED))
        recorded = observe(contents, layout, source, quiet)
        self.assertEqual(recorded.markers[key], MarkerState.MATCH)
        self.assertEqual([r.destination_index for r in recorded.records[job.job_id]], [1])

    @unittest.skipUnless(sys.platform == "win32", "holding a file unshared needs Windows")
    def test_a_record_another_program_holds_does_not_have_the_markers_read_at_every_look(self):
        root = self.tmp / "volD"
        (root / "One").mkdir(parents=True)
        s, d = volume(1, "S"), volume(2, mounts=(str(root) + "\\",))
        job = new_job("Photos", endpoint(s, "Photos"), [endpoint(d, "One")])
        contents = StoreContents((job,))
        layout = runtime_layout(self.tmp / "state" / "store.json")
        source = FixedVolumeSource((s, d))
        key = (job.job_id, job.destinations[0].marker_id)
        marker = write_marker(root / "One", Marker(job.job_id, job.destinations[0].marker_id))
        write_record(layout, record(job, 1, Outcome.COMPLETED))
        first = observe(contents, layout, source)
        self.assertEqual(first.markers[key], MarkerState.MATCH)
        path = write_record(layout, replace(record(job, 1, Outcome.COMPLETED), run_id="b" * 32))
        with held(path):
            passed_over = observe(contents, layout, source, first)      # the folder changed: markers read again
            self.assertIsNone(passed_over.record_stamps[job.job_id], "the held record is read again later")
            marker.unlink()
            still_held = observe(contents, layout, source, passed_over)
        self.assertEqual(still_held.markers[key], MarkerState.MATCH, "the marker was read again while nothing changed")


class RemoteDestinationRowTest(unittest.TestCase):
    """A remote destination's place in the list: its folder as rclone names it, marked as a
    remote's, and its short name — the folder's last part, or the remote's name for its top."""

    def test_a_remote_destination_is_listed_by_its_folder_and_named_by_its_last_part(self):
        from mirrorhold.endpoints.endpoint import RemoteEndpoint
        from mirrorhold.endpoints.resolution import resolve
        from mirrorhold.jobs.model import new_job
        from mirrorhold_gui.jobs_model import describe_folder, short_name
        s = volume(1, "S")
        job = new_job("Work", endpoint(s, "Work"), [RemoteEndpoint("cloud", "Backups/Work"),
                                                    RemoteEndpoint("cloud", "")])
        (row,) = build_rows(StoreContents((job,)), (s,), {}, {job.job_id: ()}, ())
        first, top = row.destinations
        self.assertEqual((first.folder, first.name, first.drive, first.location),
                         ("cloud:Backups/Work (remote)", "Work", "remote", None))
        self.assertEqual((top.folder, top.name), ("cloud: (remote)", "cloud"))
        self.assertEqual(describe_folder(resolve(RemoteEndpoint("cloud", "/srv"), (s,))), "cloud:/srv (remote)")
        self.assertEqual(short_name(resolve(RemoteEndpoint("cloud", "/srv/b/"), (s,))), "b")


class RemoteRowsTest(unittest.TestCase):
    """A remote destination in the list: nothing is asked of the remote, so its first run, its
    free space, whether it was reached and whether it encrypts all come from the run records."""

    def setUp(self):
        self.s = volume(1, "S")
        self.job = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("cloud", "Backups/Work")])

    def row(self, *records, volumes=None, verifications=(), repairs=()):
        (job,) = build_rows(StoreContents((self.job,)), (self.s,) if volumes is None else volumes, {},
                            {self.job.job_id: records}, (), {self.job.job_id: verifications},
                            repairs={self.job.job_id: repairs})
        return job, job.destinations[0]

    def run_of(self, outcome, **fields):
        return replace(record(self.job, 1, outcome, reasons=fields.pop("reasons", ())), **fields)

    def test_a_remote_never_run_waits_for_a_review_in_the_preview(self):
        job, row = self.row()
        self.assertEqual((row.drive, row.folder, row.status, row.tone, row.needs_review, row.first_backup),
                         ("remote", "cloud:Backups/Work (remote)", "First run: confirm", Tone.ATTENTION, True, False))
        self.assertEqual((row.location, row.can_verify, row.can_repair, row.can_open), (None, False, False, False))
        self.assertEqual(job.status, "Needs your review")
        held = self.run_of(Outcome.HELD, reasons=(FIRST_RUN_REASON,))
        self.assertEqual(self.row(held)[1].status, "First run: confirm")

    def test_a_remote_a_run_reached_reads_from_its_records(self):
        job, row = self.row(self.run_of(Outcome.COMPLETED, space=(100 * GIB, 400 * GIB), encrypted=False))
        self.assertEqual((row.status, row.free, row.free_low), ("Completed", "100.0 GiB (25 %)", False))
        self.assertEqual(row.folder, "cloud:Backups/Work (remote, not encrypted)")
        self.assertTrue(row.can_verify and row.can_open)
        self.assertEqual(row.versions_folder, RemotePlace("cloud:Backups/Work/.mirrorhold/versions"))
        self.assertEqual(job.status, "Up to date")
        low = self.run_of(Outcome.COMPLETED, space=(GIB, 400 * GIB), encrypted=True)
        self.assertEqual((self.row(low)[1].free_low, self.row(low)[1].folder),
                         (True, "cloud:Backups/Work (remote)"))

    def test_a_remote_the_last_run_could_not_reach_waits_with_what_is_known(self):
        first = self.run_of(Outcome.COMPLETED, space=(100 * GIB, 400 * GIB), encrypted=False)
        away = self.run_of(Outcome.ABSENT, reasons=(REMOTE_UNREACHABLE,), started="2026-10-05T20:10:00Z")
        job, row = self.row(first, away)
        self.assertEqual((row.status, row.tone, row.free), ("Waits for its remote", Tone.ABSENT, "100.0 GiB (25 %)"))
        self.assertEqual((job.status, job.tone), ("Waits for its remote", Tone.ABSENT))
        self.assertTrue(row.can_verify, "the verification itself finds out whether it can be reached")
        self.assertEqual(self.row(first, away, self.run_of(Outcome.COMPLETED))[1].status, "Completed")

    def test_a_first_contact_the_core_stopped_says_why_rather_than_asking_for_a_review(self):
        stopped = self.run_of(Outcome.STOPPED, reasons=("marker-mismatch",))
        row = self.row(stopped)[1]
        self.assertEqual((row.status, row.needs_review), ("Stopped: the folder belongs to another job", False))
        row = self.row(self.run_of(Outcome.STOPPED, reasons=(REMOTE_CONFIGURATION_MISSING,)))[1]
        self.assertEqual(row.status, "Stopped: no rclone configuration is set")

    def test_verifying_and_replacing_need_the_source_and_the_first_run_only(self):
        done = self.run_of(Outcome.COMPLETED)
        pass_ = verification(self.job, differ=(("a.txt", 3),))
        job, row = self.row(done, verifications=(pass_,))
        self.assertTrue(row.can_repair)
        self.assertEqual(row.status, "Copies differ: see verification")
        _, gone = self.row(done, volumes=())
        self.assertFalse(gone.can_verify)
        job, _row = self.row(done, volumes=())
        self.assertEqual(not_verified_why(gone, job), "the source's drive is not attached")
        job, first = self.row()
        self.assertEqual(not_verified_why(first, job), "its first run is not confirmed yet")

    def test_the_question_before_first_backups_counts_a_remote_with_the_others(self):
        vol_d = volume(2, "D")
        self.job = new_job("Work", endpoint(self.s, "Work"), [endpoint(vol_d, "B"), RemoteEndpoint("cloud", "Next")])
        markers = {(self.job.job_id, self.job.destinations[0].marker_id): MarkerState.MISSING}
        occupancy = {(self.job.job_id, self.job.destinations[0].marker_id): Occupancy.EMPTY}
        (job,) = build_rows(StoreContents((self.job,)), (self.s, vol_d), markers, {}, (), occupancy=occupancy)
        self.assertIn("Destination 2, cloud:Next (remote): its first run waits for your review",
                      first_backup_question(job).text)

    def test_two_activities_reaching_a_remote_name_the_configuration_they_share(self):
        one, other = (new_job(name, endpoint(self.s, name), [RemoteEndpoint("cloud", name)]) for name in ("A", "B"))
        activity = lambda job, order: Activity(job.job_id, KIND_RUN, job.job_id, job.name, "", str(order), order == 0,
                                               False, run_claim(job, [1], (self.s,)))
        self.assertEqual(_shared_drive(activity(one, 1), activity(other, 0)), CONFIGURATION)
        self.assertEqual(_shared_drive(activity(one, 1), activity(one, 0)), "", "one job's runs conflict regardless")
        status = {"waiting_for": WaitedFor("a" * 32, KIND_RUN, "B", drive=CONFIGURATION).to_document(), "place": 2}
        self.assertEqual(waiting_note(status), "Both use the rclone configuration. 2nd in line.")
        self.assertEqual(waiting_words(status), "2nd in line, waiting for 'B' (the rclone configuration)")


class RemotePreferencesTest(TempDirTest):
    """The section of the preferences: what it would save, and what stops it."""

    def setUp(self):
        super().setUp()
        self.file = write(self.tmp, "rclone.conf", "")

    def read(self, unlock, before=RemoteSettings(), **draft):
        return read_remotes(RemotesDraft(str(self.file), unlock, **draft), before)

    def test_no_file_named_keeps_nothing_and_a_file_not_there_is_refused(self):
        self.assertEqual(read_remotes(RemotesDraft(), RemoteSettings(str(self.file))).settings, RemoteSettings())
        plan = read_remotes(RemotesDraft(str(self.tmp / "no.conf")), RemoteSettings())
        self.assertIsNone(plan.settings)
        self.assertIn("no such file; make the configuration with rclone first.", plan.problem)
        quoted = read_remotes(RemotesDraft(f'  "{self.file}" '), RemoteSettings())
        self.assertEqual(quoted.settings, RemoteSettings(str(self.file)))

    def test_a_section_left_as_it_is_never_stops_the_windows_other_choices(self):
        gone = RemoteSettings(str(self.tmp / "on an absent drive.conf"), Unlock.REMEMBERED)
        plan = read_remotes(remotes_draft(gone), gone)
        self.assertEqual((plan.settings, plan.problem, plan.remember), (gone, "", False))

    def test_a_command_is_read_as_windows_quotes_it(self):
        plan = self.read(Unlock.COMMAND, command=r'"C:\Program Files\Keys\pass.exe" get rclone')
        self.assertEqual(plan.settings.unlock_command, (r"C:\Program Files\Keys\pass.exe", "get", "rclone"))
        self.assertEqual(remotes_draft(plan.settings).command, r'"C:\Program Files\Keys\pass.exe" get rclone')
        self.assertEqual(self.read(Unlock.COMMAND, command="  ").problem, NO_COMMAND)
        for words in (("powershell", "-Command", 'Get-Secret "x"'), ("tool.exe", "C:\\Program Files\\"),
                      ("tool.exe", "a\\b", "")):
            self.assertEqual(command_words(command_text(words)), words)

    def test_a_password_is_needed_unless_this_file_has_one_remembered(self):
        self.assertEqual(self.read(Unlock.REMEMBERED).problem, TYPE_PASSWORD)
        typed = self.read(Unlock.REMEMBERED, password="invented")
        self.assertTrue(typed.remember)
        self.assertEqual(typed.settings, RemoteSettings(str(self.file), Unlock.REMEMBERED))
        same = RemoteSettings(str(self.file), Unlock.REMEMBERED)
        kept = self.read(Unlock.REMEMBERED, before=same)
        self.assertEqual((kept.settings, kept.remember), (same, False))
        other = RemoteSettings(str(self.tmp / "other.conf"), Unlock.REMEMBERED)
        self.assertEqual(self.read(Unlock.REMEMBERED, before=other).problem, TYPE_PASSWORD)

    def test_a_refusal_naming_a_command_reads_in_the_windows_terms(self):
        self.assertEqual(window_message(NO_CONFIGURATION), NAME_CONFIGURATION)
        self.assertIn("File > Preferences", window_message(NO_REMEMBERED_PASSWORD))
        self.assertEqual(window_message("the engine failed."), "The engine failed.")

    def test_the_one_drive_hint_says_a_remote_takes_turns_whatever_is_chosen(self):
        self.assertIn("reach a remote always take turns", DRIVE_NOTE)


class RemoteEditorTest(unittest.TestCase):
    """A remote destination in the editor: the checks judge what they are handed, and the job
    cannot be saved while the engine is being asked."""

    def setUp(self):
        self.s = volume(1, "S")
        self.draft = JobDraft(name="Work", source=endpoint(self.s, "Work"))
        self.draft.add_remote_destination("cloud", "Backups/Work")
        self.endpoint = RemoteEndpoint("cloud", "Backups/Work")

    def facts(self, **features):
        known = dict(moves=True, copies=False, moves_folders=True, precision_ns=1000, hashes=(), reports_space=True,
                     keeps_empty_folders=True, case_insensitive=False, duplicate_names=False, partial_uploads=False,
                     deletes_folders=True, encrypted=False)
        known.update(features)
        return RemoteSetup(SetupState.READY, remotes=("cloud",), facts={
            self.endpoint: RemoteFacts(Reach.REACHED, features=RemoteFeatures(**known), space=(5 * GIB, 9 * GIB))})

    def check(self, **given):
        return check(self.draft, (self.s,), (), (), **given)

    def test_a_folder_no_remote_can_have_is_said_as_a_wrong_path_is(self):
        with self.assertRaises(EndpointError):
            self.draft.add_remote_destination("a", "x")
        self.assertEqual(self.draft.remote_endpoints, (self.endpoint,))

    def test_saving_waits_for_the_engine_and_then_says_what_it_found(self):
        asking = self.check(asking=True)
        self.assertFalse(asking.can_save)
        self.assertEqual([c.text for c in asking.checks], [ASKING_REMOTE])
        answered = self.check(remote_setup=self.facts())
        self.assertTrue(answered.can_save)
        self.assertIn("this remote does not encrypt", answered.checks[0].text)
        self.assertEqual(answered.checks[0].level, NOTE)
        self.assertEqual(self.check(remote_setup=self.facts(encrypted=True)).checks, ())

    def test_what_the_core_refuses_stops_the_save_in_the_windows_words(self):
        below = self.check(remote_setup=self.facts(moves=False))
        self.assertFalse(below.can_save)
        self.assertIn("cannot hold a backup", below.checks[0].text)
        missing = self.check(remote_setup=RemoteSetup(SetupState.MISSING, NO_CONFIGURATION))
        self.assertFalse(missing.can_save)
        self.assertEqual(missing.checks[0].text,
                         "Destination 1: name the rclone configuration in File > Preferences first.")

    def test_a_remote_stands_in_the_list_with_the_free_space_its_service_reported(self):
        self.assertEqual(endpoint_line(self.endpoint, ()), ("cloud:Backups/Work", "remote", ""))
        self.assertEqual(endpoint_line(self.endpoint, (), self.facts()), ("cloud:Backups/Work", "remote", "5.0 GiB"))

    def test_an_edited_job_keeps_its_remote_destination_and_its_marker(self):
        job, _problems = self.draft.build()
        again, problems = JobDraft.from_job(job, source_editable=False).build()
        self.assertEqual(problems, [])
        self.assertEqual(again.destinations, job.destinations)


class RemoteDetailsTest(unittest.TestCase):
    """What the history, a verification and a repair say of a remote."""

    def setUp(self):
        self.s = volume(1, "S")
        self.remote = RemoteEndpoint("cloud", "Backups/Work")
        self.job = new_job("Work", endpoint(self.s, "Work"), [self.remote])

    def test_a_run_that_could_not_reach_its_remote_says_why(self):
        away = replace(record(self.job, 1, Outcome.ABSENT, reasons=(REMOTE_UNREACHABLE,)),
                       detail="cloud:Backups/Work cannot be reached: dial tcp refused; the next run tries again")
        self.assertEqual(result_words(away), "Not reached")
        self.assertEqual(record_details(away, self.remote).summary,
                         "Its remote could not be reached; it waits. Dial tcp refused.")

    def test_a_remotes_batch_is_its_address_and_its_space_the_services(self):
        run = replace(record(self.job, 1, Outcome.COMPLETED, batch=".mirrorhold\\versions\\2026-10-04"),
                      space=(GIB, 4 * GIB), versions_cleaned=VersionsCleaned(30, ("b",), 1, 2, 1024, (), True))
        details = record_details(run, self.remote)
        self.assertEqual(details.versions_path, RemotePlace("cloud:Backups/Work/.mirrorhold/versions/2026-10-04"))
        self.assertEqual(details.space, "Free after the run, as the service reports it: 1.0 GiB of 4.0 GiB (25 %)")
        self.assertIn("; the service may keep them until it removes them", details.cleanup)
        self.assertEqual(record_details(run, Path("D:/x"), "").space, "Free after the run: 1.0 GiB of 4.0 GiB (25 %)")

    def test_a_verification_says_how_a_remotes_copies_were_compared(self):
        pass_ = replace(verification(self.job), measure=Measure.FINGERPRINT, fingerprint="md5")
        self.assertIn("; by their MD5 fingerprints, nothing downloaded.", verification_summary(pass_))
        pass_ = replace(pass_, measure=Measure.ENCRYPTED)
        self.assertIn("; by their encrypted form against the service's MD5 fingerprints.",
                      verification_summary(pass_))
        pass_ = replace(pass_, measure=Measure.FINGERPRINT, read_files=2, read_bytes=2048)
        self.assertIn("; 2 files read in full (2.0 KiB): the service kept no fingerprint for them.",
                      verification_summary(pass_))
        self.assertNotIn("fingerprint", verification_summary(verification(self.job)))
        details = verification_details(pass_, self.remote)
        self.assertEqual(details.destination_path, RemotePlace("cloud:Backups/Work"))

    def test_the_verify_dialog_says_what_a_remotes_comparison_reads(self):
        row = build_rows(StoreContents((self.job,)), (self.s,), {}, {}, ())[0].destinations[0]
        dialog = verify_dialog("Work", row, 90, None)
        self.assertEqual((dialog.intro, dialog.full), (verify_model.INTRO_REMOTE, verify_model.FULL_REMOTE))

    def test_a_repair_of_a_remote_says_how_its_new_copies_were_checked(self):
        pass_ = verification(self.job, differ=(("a.txt", 3),))
        question = replace_question("Work", 1, "Work", pass_, (), None, remote=True)
        self.assertIn("checked against the source the way this destination is verified", question.text)
        repair = RepairRecord("x" * 32, self.job.job_id, "Work", self.job.destinations[0].marker_id, 1,
                              pass_.verification_id, pass_.started, "2026-09-02T09:00:00Z", "2026-09-02T09:00:00Z",
                              RepairState.COMPLETED, taken=1, replaced=[("a.txt", 3)],
                              versions_batch=".mirrorhold\\versions\\b")
        self.assertIn("each found identical to the source", repaired_line(pass_, (repair,), remote=True))
        details = repair_details(replace(repair, left=[("b.txt", LeftReason.STILL_DIFFERS, "")]), self.remote)
        self.assertIn("each found identical to the source", details.summary)
        self.assertEqual(details.versions_path, RemotePlace("cloud:Backups/Work/.mirrorhold/versions/b"))
        self.assertIn("the service did not keep it", details.failed[0].why)
        entries = history_entries((), (), {}, repairs=(repair,), remotes=(repair.marker_id,))
        self.assertIn("each found identical", entries[0][0].details)


class RemoteFoldersTest(unittest.TestCase):
    def test_walking_a_remotes_folders(self):
        self.assertEqual((child_folder("", "a"), child_folder("a", "b"), child_folder("/", "a"),
                          child_folder("/a", "b")), ("a", "a/b", "/a", "/a/b"))
        self.assertEqual((parent_folder("a/b"), parent_folder("a"), parent_folder("/a"), parent_folder("/"),
                          parent_folder("")), ("a", "", "/", "/", ""))
        self.assertTrue(at_top("") and at_top("/") and not at_top("a"))

    def test_one_session_serves_a_window_and_ends_with_it(self):
        class Session:
            remotes, closed = ("cloud",), 0

            def close(self):
                self.closed += 1

        session = Session()
        calls = []
        access = RemoteAccess(Preferences(), Path("x"), lambda *a: calls.append(a) or session)
        self.assertEqual((access.open(), access.open(), len(calls)), (("cloud",), ("cloud",), 1))
        access.close()
        access.close()
        self.assertEqual(session.closed, 1)
        with self.assertRaises(RemoteSetupError):
            access.open()

    def test_work_off_the_window_says_what_it_gave_or_why_it_failed(self):
        def wait(work):
            job = Background(work)
            while not job.done:
                time.sleep(0.01)
            return job

        self.assertEqual(wait(lambda: 3).value, 3)
        self.assertEqual(wait(lambda: 1 / 0).failure, "division by zero")


if __name__ == "__main__":
    unittest.main()
