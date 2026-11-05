"""The command line, in process against volumes mapped onto temporary folders, and once end
to end through ``python -m mirrorhold`` on this machine's real volume source.

Every test names its own store; none reads the live configuration."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from support import PROJECT, TempDirTest, endpoint, held, volume
from test_engine_contract import INSTALLED

from mirrorhold.cli import EXIT_ENGINE, EXIT_HELD, EXIT_OK, EXIT_REFUSED, EXIT_STORE, EXIT_USAGE, main
from mirrorhold.endpoints.cloud import SyncKind, SyncRoot
from mirrorhold.endpoints.marker import Marker, write_marker
from mirrorhold.endpoints.shared import shared_path
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.jobs.model import Triggers, new_group, new_job
from mirrorhold.jobs.store import SCHEMA_VERSION, ConfigStore, StoreContents
from mirrorhold.locations import RuntimeLayout, runtime_layout
from mirrorhold.notifications.toast import NotificationError
from mirrorhold.records import diagnostics
from mirrorhold.records.record import read_records
from mirrorhold.records.status import LiveStatus, Phase, StatusBoard, Wanted


class DiagnosticLogTest(TempDirTest):
    """The log a core process keeps of what it says: one per process, each line timed, a
    failure's traceback whole, bounded in size and in number."""

    def test_a_log_holds_what_was_said_and_a_failure_whole_and_nothing_once_closed(self):
        layout = RuntimeLayout(self.tmp)
        diagnostics.note("said before any log is open")
        with diagnostics.process_log(layout, "verify") as path:
            self.assertEqual(diagnostics.current_path(), path)
            diagnostics.note("first line\nsecond line")
            try:
                raise ValueError("planted")
            except ValueError:
                self.assertEqual(diagnostics.note_failure("a planted failure"), path)
        diagnostics.note("said after it closed")
        self.assertIsNone(diagnostics.current_path())
        self.assertRegex(path.name, r"^\d{8}T\d{6}Z-verify-\d+\.log$")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ first line$")
        text = "\n".join(lines)
        self.assertIn("Z second line", text)
        self.assertIn("Z a planted failure", text)
        self.assertIn("ValueError: planted", text)
        self.assertNotIn("before any log", text)
        self.assertNotIn("after it closed", text)

    def test_the_oldest_logs_go_beyond_their_bounds_and_a_log_stops_growing_at_its_size(self):
        layout = RuntimeLayout(self.tmp)
        layout.logs_dir.mkdir()
        for i in range(diagnostics.MAX_LOGS + 5):
            (layout.logs_dir / f"2020{i:04}T000000Z-run-1.log").write_text("x", encoding="utf-8")
        with unittest.mock.patch.object(diagnostics, "MAX_LOG_BYTES", 200):
            with diagnostics.process_log(layout, "run") as path:
                for i in range(20):
                    diagnostics.note(f"line {i}")
        names = [p.name for p in diagnostics.logs(layout)]
        self.assertEqual((len(names), names[-1]), (diagnostics.MAX_LOGS, path.name))
        self.assertEqual(names[0], "20200006T000000Z-run-1.log")
        text = path.read_text(encoding="utf-8")
        self.assertIn("line 0", text)
        self.assertNotIn("line 19", text)
        self.assertTrue(text.rstrip().endswith("the log reached its size limit; nothing more is written"))
        diagnostics.prune(layout, keep=3)
        self.assertEqual([p.name for p in diagnostics.logs(layout)],
                         ["20200203T000000Z-run-1.log", "20200204T000000Z-run-1.log", path.name])
        path.unlink()
        for name in ("20200203T000000Z-run-1.log", "20200204T000000Z-run-1.log"):
            (layout.logs_dir / name).write_text("x" * 10, encoding="utf-8")
        diagnostics.prune(layout, total=15)
        self.assertEqual([p.name for p in diagnostics.logs(layout)], ["20200204T000000Z-run-1.log"])

    def test_the_window_reads_a_logs_marks_back_as_they_are_written(self):
        layout = RuntimeLayout(self.tmp)
        watch = diagnostics.LogWatch(layout)
        with diagnostics.process_log(layout, "verify") as path:
            diagnostics.note("mirrorhold verify Photos --dest 2")
            diagnostics.mark(diagnostics.START, command="verify", job="Photos", trigger="manual", destinations=[2])
            diagnostics.mark(diagnostics.JOB, job="j" * 32)
            diagnostics.note("verifying destination 2 of 'Photos'")
            diagnostics.mark(diagnostics.ACTIVITY, id="v" * 32, kind="verification")
            (log,) = watch.look()
            self.assertEqual((log.path, log.command, log.trigger, log.job, log.destinations, log.activities),
                             (path, "verify", "manual", "j" * 32, (2,), (("v" * 32, "verification"),)))
            self.assertFalse(log.ended or log.failed or log.vanished)
            if sys.platform == "win32":
                self.assertIs(log.in_use, True)
            diagnostics.mark(diagnostics.END, status=1, failure="the destination's drive is not attached")
        (log,) = watch.look()
        self.assertEqual((log.status, log.failure, log.failed, log.in_use),
                         (1, "the destination's drive is not attached", True, None))
        self.assertRegex(log.started, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(log.written, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn('Z @end {"failure": "the destination\'s drive is not attached", "status": 1}',
                      path.read_text(encoding="utf-8"))

    def test_destinations_given_by_marker_are_numbered_by_the_job_mark(self):
        layout = RuntimeLayout(self.tmp)
        watch = diagnostics.LogWatch(layout)
        with diagnostics.process_log(layout, "run"):
            diagnostics.mark(diagnostics.START, command="run", job="Photos", trigger="manual", destinations=None)
            diagnostics.mark(diagnostics.JOB, job="j" * 32)
            (log,) = watch.look()
            self.assertEqual(log.destinations, ())
            diagnostics.mark(diagnostics.JOB, job="j" * 32, destinations=[1, 3])
            (log,) = watch.look()
            self.assertEqual((log.job, log.destinations), ("j" * 32, (1, 3)))

    def test_a_log_whose_process_ended_without_a_word_and_one_written_before_marks(self):
        layout = RuntimeLayout(self.tmp)
        layout.logs_dir.mkdir()
        stamp = "2026-10-14T09:00:00Z"
        start = json.dumps({"command": "run", "job": "j" * 32, "trigger": "manual", "destinations": None})
        (layout.logs_dir / "20261014T090000Z-run-41.log").write_text(
            f"{stamp} mirrorhold run {'j' * 32}\n{stamp} @start {start}\n{stamp} @activity "
            + json.dumps({"id": "r" * 32, "kind": "run"}) + "\n", encoding="utf-8")
        (layout.logs_dir / "20261014T080000Z-run-40.log").write_text(
            f"{stamp} mirrorhold run Work\n{stamp} destination 1: completed\n{stamp} @end {{\"status\": 0}}\n",
            encoding="utf-8")
        (log,) = diagnostics.LogWatch(layout).look()
        self.assertEqual((log.path.name, log.started, log.activities),
                         ("20261014T090000Z-run-41.log", stamp, (("r" * 32, "run"),)))
        if sys.platform == "win32":
            self.assertTrue(log.vanished)

    def test_every_log_saved_as_one_text_oldest_first(self):
        layout = RuntimeLayout(self.tmp)
        layout.logs_dir.mkdir()
        (layout.logs_dir / "20261014T090000Z-verify-2.log").write_text("second\n", encoding="utf-8")
        (layout.logs_dir / "20261014T080000Z-run-1.log").write_text("first", encoding="utf-8")
        target = self.tmp / "saved.txt"
        self.assertEqual(diagnostics.write_joined(layout, target), 2)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "===== 20261014T080000Z-run-1.log =====\nfirst\n\n"
                         "===== 20261014T090000Z-verify-2.log =====\nsecond\n\n")


class InProcessTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vol_d = self.tmp / "volD"
        self.vol_e = self.tmp / "volE"
        for root in (self.vol_d / "Work", self.vol_e):
            root.mkdir(parents=True)
        self.d = volume(1, mounts=(str(self.vol_d) + "\\",), label="D")
        self.e = volume(2, mounts=(str(self.vol_e) + "\\",), label="E")
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((self.d, self.e))

    def cli(self, *args, volumes=None, roots=()):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store_path), *args], volume_source=volumes or self.volumes,
                    sync_roots=roots, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def add(self, name, source, *dests):
        args = ["add", name, "--source", str(source), "--no-survey"]
        for d in dests:
            args += ["--dest", str(d)]
        return self.cli(*args)

    def test_status_counts_files_found_identical_and_names_the_files_compared(self):
        status = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Photos", "manual", "2026-10-09T10:00:00Z", pair_index=1,
                            destination="destination 1 at F:\\M", phase=Phase.COPYING)
        status.planned.update(files_to_copy=100, bytes_to_copy=1000)
        status.done.update(files_copied=2, bytes_copied=20, files_identical=48, bytes_identical=480)
        status.comparing = ["sub\\IMG_1.JPG"]
        board = StatusBoard(runtime_layout(self.store_path), status)
        board.open()
        self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("copied 2 of 100 files, 20 of 1000 bytes (48 already identical, 480 bytes);", out)
        self.assertIn("    comparing: sub\\IMG_1.JPG", out)
        status.phase, status.examined, status.to_examine = Phase.CHECKING, 7, 9
        board.publish()
        self.assertIn("checking changed files (7 of 9 changed files examined);", self.cli("status")[1])

    def test_status_names_what_a_waiting_run_or_verification_waits_for(self):
        layout = runtime_layout(self.store_path)
        held = {"run_id": "f" * 32, "kind": "run", "job_name": "Work", "paused": True, "waiting": False}
        run = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Photos", "manual", "2026-10-09T10:00:00Z",
                         waiting_for=held)
        check = LiveStatus("b" * 32, os.getpid(), "j" * 32, "Photos", "manual", "2026-10-09T10:00:01Z",
                           kind="verification", destination="destination 1", verify={"gave_way": True},
                           waiting_for=dict(held, paused=False, waiting=True))
        for status in (run, check):
            board = StatusBoard(layout, status)
            board.open()
            self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("    waiting for the run of 'Work' (ffffffffffff) to finish; it is paused\n", out)
        self.assertIn("    waiting for the run of 'Work' (ffffffffffff) to finish; it waits too; "
                      "this verification gave way to it\n", out)

    def test_status_names_a_verification_of_new_copies(self):
        layout = runtime_layout(self.store_path)
        for run_id, mode in (("a" * 32, "new-copies"), ("b" * 32, "full")):
            board = StatusBoard(layout, LiveStatus(run_id, os.getpid(), "j" * 32, "Photos", "manual",
                                                   "2026-10-19T10:00:00Z", kind="verification",
                                                   destination=f"destination {run_id[0]}", phase=Phase.VERIFYING,
                                                   verify={"mode": mode, "files": 4, "files_done": 1}))
            board.open()
            self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("  verification of new copies of destination a\n", out)
        self.assertIn("  verification of destination b\n", out)

    def test_status_names_the_drive_a_waiting_one_shares_and_its_place_in_line(self):
        layout = runtime_layout(self.store_path)
        held_by = {"run_id": "f" * 32, "kind": "run", "job_name": "Work", "paused": False, "waiting": False,
                   "drive": "drive I:"}
        run = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Photos", "manual", "2026-10-14T10:00:00Z",
                         waiting_for=held_by, place=2)
        board = StatusBoard(layout, run)
        board.open()
        self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("    waiting for the run of 'Work' (ffffffffffff) to finish; both use drive I:; 2nd in line\n",
                      out)

    @unittest.skipUnless(sys.platform == "win32", "holding a file unshared needs Windows")
    def test_status_lists_a_live_run_whose_status_cannot_be_read_and_a_request_reaches_it(self):
        layout = runtime_layout(self.store_path)
        unread = {"run_id": "f" * 32, "kind": "run", "job_name": "Work", "readable": False}
        statuses = [LiveStatus("f" * 32, os.getpid(), "f" * 32, "Work", "manual", "2026-10-21T10:00:00Z",
                               pair_index=1, phase=Phase.COPYING),
                    LiveStatus("a" * 32, os.getpid(), "a" * 32, "Photos", "manual", "2026-10-21T10:00:01Z",
                               waiting_for=unread, place=1),
                    LiveStatus("b" * 32, os.getpid(), "b" * 32, "Music", "manual", "2026-10-21T10:00:02Z",
                               waiting_for=dict(unread, kind="", job_name=""), place=2)]
        for status in statuses:
            board = StatusBoard(layout, status)
            board.open()
            self.addCleanup(board.close)
        with held(layout.status_dir / f"{'f' * 32}.json"):
            code, out, _ = self.cli("status")
            asked = self.cli("pause", "ffff")
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("no run in progress", out)
        self.assertIn("run ffffffffffff  - alive; its status cannot be read now, so every run, verification or "
                      "repair asking for its turn waits for it\n", out)
        self.assertIn("    waiting for the run of 'Work' (ffffffffffff) to finish; its status cannot be read now; "
                      "next in line\n", out)
        self.assertIn("    waiting for a run, verification or repair (ffffffffffff) to finish; its status cannot be "
                      "read now; 2nd in line\n", out)
        self.assertEqual(asked[:2], (EXIT_OK, "run ffffffffffff (its status cannot be read now): asked to pause\n"))
        self.assertEqual(Wanted.from_document(json.loads((layout.status_dir / f"{'f' * 32}.control").read_text(
            encoding="utf-8"))), Wanted(True, None))

    def test_status_calls_an_activity_never_read_a_run_verification_or_repair(self):
        layout = runtime_layout(self.store_path)
        never_read = {"run_id": "f" * 32, "kind": "", "job_name": "", "readable": False}
        board = StatusBoard(layout, LiveStatus("b" * 32, os.getpid(), "b" * 32, "Music", "manual",
                                               "2026-10-22T10:00:00Z", waiting_for=never_read, place=2))
        board.open()
        self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("    waiting for a run, verification or repair (ffffffffffff) to finish; its status cannot be "
                      "read now; 2nd in line\n", out)

    def test_status_says_a_run_is_starting_or_waits_for_its_turn(self):
        layout = runtime_layout(self.store_path)
        statuses = [LiveStatus("a" * 32, os.getpid(), "a" * 32, "Fresh", "manual", "2026-10-21T10:00:00Z"),
                    LiveStatus("b" * 32, os.getpid(), "b" * 32, "Asked", "manual", "2026-10-21T10:00:01Z",
                               claim={"reads": [], "writes": []})]
        for status in statuses:
            board = StatusBoard(layout, status)
            board.open()
            self.addCleanup(board.close)
        out = self.cli("status")[1]
        self.assertIn("job 'Fresh' (manual)  started 2026-10-21T10:00:00Z\n    starting\n", out)
        self.assertIn("job 'Asked' (manual)  started 2026-10-21T10:00:01Z\n    waiting for its turn\n", out)

    def test_status_lists_the_activities_in_line_order(self):
        layout = runtime_layout(self.store_path)
        started = "2026-10-19T10:00:00Z"                               # one second, as a group's runs start in
        held_by = {"run_id": "z" * 32, "kind": "run", "job_name": "Work", "paused": False, "waiting": False}
        statuses = [LiveStatus("a" * 32, os.getpid(), "a" * 32, "Second", "manual", started, waiting_for=held_by,
                               place=2),
                    LiveStatus("b" * 32, os.getpid(), "b" * 32, "Next", "manual", started, waiting_for=held_by,
                               place=1),
                    LiveStatus("z" * 32, os.getpid(), "z" * 32, "Work", "manual", started, phase=Phase.COPYING)]
        for status in statuses:
            board = StatusBoard(layout, status)
            board.open()
            self.addCleanup(board.close)
        code, out, _ = self.cli("status")
        self.assertEqual(code, EXIT_OK)
        heads = [line for line in out.splitlines() if line.startswith("run ")]
        self.assertEqual([head.split("'")[1] for head in heads], ["Work", "Next", "Second"])

    def test_add_list_validate_resolve_remove(self):
        code, out, _ = self.add("Work", self.vol_d / "Work", self.vol_e / "Work")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("valid", out)
        jobs = ConfigStore(self.store_path).load().contents.jobs
        self.assertEqual([j.name for j in jobs], ["Work"])
        self.assertEqual((jobs[0].source.endpoint.path, jobs[0].destinations[0].endpoint.path), ("Work", "Work"))

        code, out, _ = self.cli("jobs")
        self.assertIn("Work", out)
        self.assertEqual(self.cli("validate")[0], EXIT_OK)

        code, out, _ = self.cli("resolve", "work")
        self.assertIn("source: present", out)
        self.assertIn("marker missing", out)
        (self.vol_e / "Work").mkdir()
        write_marker(self.vol_e / "Work", Marker(jobs[0].job_id, jobs[0].destinations[0].marker_id))
        self.assertIn("marker match", self.cli("resolve", jobs[0].job_id[:6])[1])
        self.assertIn("destination 1: absent", self.cli("resolve", "Work", volumes=FixedVolumeSource((self.d,)))[1])

        code, out, _ = self.cli("remove", "Work")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(ConfigStore(self.store_path).load().contents.jobs, ())

    def test_resolve_prints_each_destinations_marker_identifier(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        (job,) = ConfigStore(self.store_path).load().contents.jobs
        lines = self.cli("resolve", "Work")[1].splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("destination 1: "))
        self.assertEqual(lines[at + 1], f"    marker identifier {job.destinations[0].marker_id}")

    def test_a_preview_takes_its_destinations_by_marker_and_one_named_both_ways_is_a_usage_error(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        (job,) = ConfigStore(self.store_path).load().contents.jobs
        marker = job.destinations[0].marker_id
        code, out, err = self.cli("preview", "Work", "--marker", marker[:4])
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("destination 1", out)
        code, _, err = self.cli("preview", "Work", "--marker", "f" * 32)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(f"the job has no destination with marker '{'f' * 32}'", err)
        self.assertEqual(self.cli("preview", "Work", "--dest", "1", "--marker", marker)[0], EXIT_USAGE)

    def test_a_save_over_a_job_file_changed_while_the_command_ran_is_refused(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        (self.vol_d / "Photos").mkdir()
        store = ConfigStore(self.store_path)

        class SavedMeanwhile(FixedVolumeSource):
            def scan(source):
                contents = store.load().contents
                store.save(replace(contents, preferences=replace(contents.preferences, gui_state={"saved": "here"})))
                return super().scan()

        code, _, err = self.cli("add", "Photos", "--source", str(self.vol_d / "Photos"), "--dest",
                                str(self.vol_e / "Photos"), "--no-survey", volumes=SavedMeanwhile((self.d, self.e)))
        self.assertEqual((code, err), (EXIT_REFUSED, "refused: the job file changed while this command ran (another "
                                                     "window or command saved it); nothing was saved - run the "
                                                     "command again\n"))
        contents = store.load().contents
        self.assertEqual(([j.name for j in contents.jobs], contents.preferences.gui_state),
                         (["Work"], {"saved": "here"}))
        self.assertEqual(self.add("Photos", self.vol_d / "Photos", self.vol_e / "Photos")[0], EXIT_OK)

    def look(self):
        return diagnostics.LogWatch(runtime_layout(self.store_path)).look()

    def test_a_logged_command_marks_what_it_was_given_resolved_and_why_it_could_not_start(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        job = ConfigStore(self.store_path).load().contents.jobs[0]
        code, _, err = self.cli("verify", "Work", "--dest", "9")
        self.assertEqual((code, err), (EXIT_REFUSED, "refused: cannot verify: the job has no destination 9\n"))
        (log,) = self.look()
        self.assertEqual((log.command, log.trigger, log.job, log.destinations, log.status, log.failure),
                         ("verify", "manual", job.job_id, (9,), EXIT_REFUSED, "the job has no destination 9"))
        self.assertTrue(log.failed)

    @unittest.skipUnless(sys.platform == "win32", "holding a file unshared needs Windows")
    def test_a_job_file_held_is_the_reason_a_runs_log_keeps(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        job = ConfigStore(self.store_path).load().contents.jobs[0]
        with held(self.store_path):
            code, _, _ = self.cli("run", job.job_id)
        self.assertEqual(code, EXIT_STORE)
        (log,) = self.look()
        self.assertEqual((log.command, log.job, log.status, log.failure),
                         ("run", job.job_id, EXIT_STORE, "the job file could not be read: another program kept it "
                                                         "open; try again"))

    def test_a_run_that_ran_marks_its_run_and_no_failure(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        job = ConfigStore(self.store_path).load().contents.jobs[0]
        code, _, _ = self.cli("run", "Work", volumes=FixedVolumeSource((self.d,)))
        (log,) = self.look()
        records = read_records(runtime_layout(self.store_path), job.job_id)
        self.assertEqual((log.status, log.failure, log.failed), (code, "", False))
        self.assertEqual(log.activities, ((records[0].run_id, "run"),))

    def test_an_automatic_start_that_could_not_start_keeps_why(self):
        self.store_path.parent.mkdir(parents=True)
        self.store_path.write_text("{", encoding="utf-8")
        code, out, _ = self.cli("automatic", "f" * 32, "--by", "schedule")
        self.assertEqual(code, EXIT_OK)
        (log,) = self.look()
        self.assertEqual((log.command, log.trigger, log.job), ("automatic", "schedule", "f" * 32))
        self.assertTrue(log.failed)
        self.assertIn(f"nothing ran: it could not start: {log.failure}", out)

    def test_a_command_that_fails_unexpectedly_says_so_in_one_line_and_logs_its_traceback(self):
        class Failing(FixedVolumeSource):
            def scan(source):
                raise RuntimeError("the volumes could not be enumerated")

        code, out, err = self.cli("volumes", volumes=Failing(()))
        self.assertEqual((code, out), (EXIT_REFUSED, ""))
        found = re.fullmatch(r"failed unexpectedly: RuntimeError: the volumes could not be enumerated; the details "
                             r"are in (.+\.log)\n", err)
        self.assertIsNotNone(found, err)
        log = Path(found.group(1))
        self.assertEqual(log.parent, runtime_layout(self.store_path).logs_dir)
        text = log.read_text(encoding="utf-8")
        self.assertIn("volumes failed unexpectedly", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: the volumes could not be enumerated", text)

    def test_a_request_a_runs_control_file_does_not_take_is_refused_in_words(self):
        board = StatusBoard(runtime_layout(self.store_path), LiveStatus("a" * 32, os.getpid(), "j" * 32, "Photos",
                                                                         "manual", "2026-10-14T10:00:00Z"))
        board.open()
        self.addCleanup(board.close)
        self.assertEqual(self.cli("pause")[0], EXIT_OK)
        with held(runtime_layout(self.store_path).status_dir / f"{'a' * 32}.control"):
            code, _, err = self.cli("cancel", "--now")
        self.assertEqual((code, err), (EXIT_REFUSED, "refused: the request could not be written: another program kept "
                                                     "the run's control file open; try again\n"))
        self.assertEqual(board.wanted(), Wanted(True, None))

    def test_a_job_file_another_program_holds_is_reported_in_words(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        with held(self.store_path):
            code, _, err = self.cli("jobs")
        self.assertEqual((code, err), (EXIT_STORE, "store: the job file could not be read: another program kept it "
                                                   "open; try again\n"))

    def test_a_refused_job_is_not_saved(self):
        code, out, _ = self.add("Bad", self.vol_d / "Work", self.vol_d / "Work" / "Backup")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("source-destination-overlap", out)
        self.assertFalse(self.store_path.exists())
        code, out, _ = self.cli("add", "Synced", "--source", str(self.vol_d / "Work"), "--dest",
                                str(self.vol_e / "Cloud" / "x"), "--no-survey",
                                roots=[SyncRoot("OneDrive", str(self.vol_e / "Cloud"))])
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("destination-in-sync-root", out)

    def test_a_path_on_no_volume_is_refused(self):
        code, _, err = self.add("Lost", self.vol_d / "Work", "Z:\\nowhere")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("not on any present local volume", err)

    def test_survey_runs_on_a_present_source(self):
        (self.vol_d / "Work" / "f.bin").write_bytes(b"x" * 123)
        code, out, _ = self.cli("add", "Work", "--source", str(self.vol_d / "Work"), "--dest", str(self.vol_e / "W"))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 file, 123 bytes", out)

    def test_export_and_import(self):
        self.add("Work", self.vol_d / "Work", self.vol_e / "Work")
        export = self.tmp / "jobs-export.json"
        self.assertEqual(self.cli("export", str(export))[0], EXIT_OK)
        self.assertEqual(json.loads(export.read_text(encoding="utf-8"))["format"], "mirrorhold-jobs")
        original = self.store_path
        self.store_path = self.tmp / "Other" / "mirrorhold.json"
        code, out, _ = self.cli("import", str(export))
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("1 added", out)
        self.assertEqual(ConfigStore(self.store_path).load().contents, ConfigStore(original).load().contents)
        self.assertIn("1 already present", self.cli("import", str(export))[1])

    def test_jobs_name_their_group_and_an_export_carries_it(self):
        self.add("Work", self.vol_d / "Work", self.vol_e / "Work")
        store = ConfigStore(self.store_path)
        contents = store.load().contents
        work = contents.jobs[0]
        store.save(replace(contents, groups=(new_group("Office", (work.job_id,)),)))
        code, out, _ = self.cli("jobs")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("    group        Office", out)
        export = self.tmp / "jobs-export.json"
        self.assertEqual(self.cli("export", str(export))[0], EXIT_OK)
        self.store_path = self.tmp / "Other" / "mirrorhold.json"
        code, out, _ = self.cli("import", str(export))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(ConfigStore(self.store_path).load().contents.group_of(work.job_id).name, "Office")

    def test_jobs_are_listed_in_the_order_the_window_shows_them(self):
        (self.vol_d / "Music").mkdir()
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        self.assertEqual(self.add("Music", self.vol_d / "Music", self.vol_e / "Music")[0], EXIT_OK)
        store = ConfigStore(self.store_path)
        contents = store.load().contents
        self.assertEqual([j.name for j in contents.jobs], ["Work", "Music"])
        music = contents.jobs[1]
        store.save(replace(contents, groups=(new_group("Office", (music.job_id,)),)))
        code, out, _ = self.cli("jobs")
        self.assertEqual(code, EXIT_OK)
        names = [line.split("  ", 1)[1] for line in out.splitlines() if line and not line.startswith(" ")]
        self.assertEqual(names, ["Music", "Work"], "a group's jobs come before the jobs in no group")

    def test_import_conflict_and_replace(self):
        self.add("Work", self.vol_d / "Work", self.vol_e / "Work")
        export = self.tmp / "jobs-export.json"
        self.cli("export", str(export))
        doc = json.loads(export.read_text(encoding="utf-8"))
        doc["jobs"][0]["name"] = "Work renamed"
        export.write_text(json.dumps(doc), encoding="utf-8")
        code, out, _ = self.cli("import", str(export))
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("conflict", out)
        self.assertEqual(self.cli("import", str(export), "--replace")[0], EXIT_OK)
        self.assertEqual(ConfigStore(self.store_path).load().contents.jobs[0].name, "Work renamed")

    def test_unknown_job(self):
        code, _, err = self.cli("resolve", "nothing")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("no job 'nothing'", err)

    def test_a_corrupt_store_is_reported_and_kept(self):
        self.add("Work", self.vol_d / "Work", self.vol_e / "Work")
        self.store_path.write_text("{ not json", encoding="utf-8")
        code, out, err = self.cli("jobs")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("moved to", err)
        self.assertIn("no jobs", out)
        self.assertTrue(any(p.name.startswith("mirrorhold.corrupt-") for p in self.store_path.parent.iterdir()))

    def test_a_store_of_the_schema_after_the_current_one_stops_every_command(self):
        self.store_path.parent.mkdir(parents=True)
        self.store_path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": SCHEMA_VERSION + 1}),
                                   encoding="utf-8")
        self.assertEqual(self.cli("jobs")[0], EXIT_STORE)
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_STORE)

    def test_engine_preference_pointing_nowhere(self):
        store = ConfigStore(self.store_path)
        contents = store.load().contents
        from dataclasses import replace
        from mirrorhold.jobs.model import Preferences
        store.save(replace(contents, preferences=Preferences(engine_path=str(self.tmp / "none.exe"))))
        code, _, err = self.cli("engine")
        self.assertEqual(code, EXIT_ENGINE)
        self.assertIn("not found", err)


class RefusedAtCreationCommandTest(TempDirTest):
    """``add`` refuses a file as a source, and a destination that is itself a folder this
    tool needs — here the folder of the job file in use (the maintainer's decisions of
    2026-10-17) — or a file (the maintainer's decision of 2026-10-19)."""

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        (self.vol_d / "Work").mkdir(parents=True)
        self.vol_e.mkdir()
        (self.vol_d / "notes.txt").write_text("a file", encoding="utf-8")
        self.store_path = self.vol_e / "Tool" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),
                                          volume(2, mounts=(str(self.vol_e) + "\\",), label="E")))

    def add(self, source, dest):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store_path), "add", "Job", "--source", str(source), "--dest", str(dest),
                     "--no-survey"], volume_source=self.volumes, sync_roots=(), out=out, err=err)
        return code, out.getvalue()

    def test_each_refusal_says_why_and_saves_nothing(self):
        code, out = self.add(self.vol_d / "notes.txt", self.vol_e / "Copy")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("REFUSED [source-not-a-folder] source: this is a file, not a folder; a job backs up a folder "
                      "or a whole drive", out)
        code, out = self.add(self.vol_d / "Work", self.vol_e / "Tool")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("REFUSED [destination-is-system-folder] destination 1: this folder is "
                      f"{self.store_path.resolve().parent}, which MirrorHold needs", out)
        self.assertEqual(ConfigStore(self.store_path).load().contents.jobs, ())
        self.assertEqual(self.add(self.vol_d / "Work", self.vol_e / "Copy")[0], EXIT_OK)

    def test_a_file_as_the_source_is_refused_alone_when_the_source_is_surveyed(self):
        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            code = main(["--store", str(self.store_path), *args], volume_source=self.volumes, sync_roots=(),
                        out=out, err=err)
            return code, out.getvalue().splitlines()

        refusal = ("  REFUSED [source-not-a-folder] source: this is a file, not a folder; a job backs up a folder or "
                   "a whole drive")
        code, lines = cli("add", "Job", "--source", str(self.vol_d / "notes.txt"), "--dest", str(self.vol_e / "Copy"))
        self.assertEqual((code, lines[1:]), (EXIT_REFUSED, [refusal]), "no survey of the file, and no notice of one")
        self.assertEqual(cli("add", "Job", "--source", str(self.vol_d / "Work"), "--dest", str(self.vol_e / "Copy"))[0],
                         EXIT_OK)
        (self.vol_d / "Work").rmdir()
        (self.vol_d / "Work").write_text("a file where the folder was", encoding="utf-8")
        code, lines = cli("validate", "Job")
        self.assertEqual((code, lines[1:]), (EXIT_REFUSED, [refusal]))

    def test_a_file_as_a_destination_is_refused_and_nothing_saved(self):
        (self.vol_e / "Backup").write_text("a file", encoding="utf-8")
        code, out = self.add(self.vol_d / "Work", self.vol_e / "Backup")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("  REFUSED [destination-not-a-folder] destination 1: this is a file, not a folder; a backup is "
                      "written into a folder or onto a whole drive\n", out)
        self.assertEqual(ConfigStore(self.store_path).load().contents.jobs, ())


class Refusing:
    """A notifier whose every notice Windows refuses, reported as ``ToastNotifier`` reports it."""

    def show(self, notice):
        raise NotificationError("the notification could not be shown: showing the toast: error 0x80070490")


REFUSED_NOTE = "note: the notification could not be shown: showing the toast: error 0x80070490"


class RefusedNoticeLogTest(TempDirTest):
    """Why Windows refused a notice is written to the diagnostic log of the process that
    raised it, where no one reads a console: an unattended run, and an automatic run whether
    it ran or could not start (the maintainer's decision, 2026-10-19). The run's records
    say nothing of it. Each run stops at a destination that is a file, so its notice is
    one of a backup that did not finish."""

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        (self.vol_d / "Work").mkdir(parents=True)
        self.vol_e.mkdir()
        self.d = volume(1, mounts=(str(self.vol_d) + "\\",), label="D")
        self.e = volume(2, mounts=(str(self.vol_e) + "\\",), label="E")
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.layout = runtime_layout(self.store_path)
        self.job = new_job("Work", endpoint(self.d, "Work"), [endpoint(self.e, "Backup")],
                           triggers=Triggers(False, "daily 21:00"))
        ConfigStore(self.store_path).save(StoreContents((self.job,)))
        (self.vol_e / "Backup").write_text("a file where the backup's folder would be", encoding="utf-8")

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store_path), *args], volume_source=FixedVolumeSource((self.d, self.e)),
                    sync_roots=(), out=out, err=err, notifier=Refusing())
        return code, out.getvalue(), err.getvalue()

    def log_lines(self):
        (log,) = diagnostics.logs(self.layout)
        return [line.split(" ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()]

    def assert_no_record_names_it(self):
        for record in read_records(self.layout, self.job.job_id):
            self.assertNotIn("notification", " ".join((record.detail,) + record.reasons))

    def test_an_unattended_run_writes_it_to_its_log_and_prints_nothing(self):
        code, out, err = self.command("run", "Work", "--unattended")
        self.assertEqual((out, err), ("", ""))
        self.assertIn(REFUSED_NOTE, self.log_lines())
        self.assert_no_record_names_it()

    def test_an_automatic_run_writes_it_to_its_log(self):
        self.command("automatic", self.job.job_id, "--by", "schedule")
        self.assertEqual(len(read_records(self.layout, self.job.job_id)), 1)
        self.assertIn(REFUSED_NOTE, self.log_lines())
        self.assert_no_record_names_it()

    def test_an_automatic_run_that_could_not_start_writes_it_to_its_log(self):
        self.store_path.write_text("{", encoding="utf-8")
        _code, out, _err = self.command("automatic", self.job.job_id, "--by", "schedule")
        self.assertIn("nothing ran: it could not start: ", out)
        self.assertIn(REFUSED_NOTE, self.log_lines())


@unittest.skipUnless(INSTALLED, "the pinned engine is not installed")
class FirstBackupCommandTest(TempDirTest):
    """``run --first-backup``: a first run onto a folder that holds nothing, started with
    the user's go-ahead and no preview; a run held without it says how to give it, and a
    folder holding something still waits for a preview. In process, through the engine."""

    cli, add = InProcessTest.cli, InProcessTest.add

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        (self.vol_d / "Work").mkdir(parents=True)
        self.vol_e.mkdir()
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),
                                          volume(2, mounts=(str(self.vol_e) + "\\",), label="E")))

    def test_a_first_backup_is_started_with_the_go_ahead_and_a_held_one_says_how(self):
        (self.vol_d / "Work" / "a.txt").write_text("a", encoding="utf-8")
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        code, out, _ = self.cli("run", "Work")
        self.assertEqual(code, EXIT_HELD, out)
        self.assertIn("the first backup onto destination 1 waits: mirrorhold run ", out)
        self.assertIn(" --first-backup", out)
        self.assertNotIn("then run with --confirm", out)
        self.assertEqual(self.cli("run", "Work", "--first-backup", "--unattended")[0], EXIT_REFUSED)
        self.assertEqual(self.cli("run", "Work", "--first-backup", "--confirm", "abc")[0], EXIT_REFUSED)
        self.assertFalse((self.vol_e / "Work").exists())
        code, out, _ = self.cli("run", "Work", "--first-backup")
        self.assertEqual(code, EXIT_OK, out)
        self.assertTrue((self.vol_e / "Work" / "a.txt").is_file())

    def test_a_folder_holding_something_waits_for_a_preview_despite_the_go_ahead(self):
        (self.vol_d / "Work" / "a.txt").write_text("a", encoding="utf-8")
        (self.vol_e / "Work" / "Empty").mkdir(parents=True)
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        code, out, _ = self.cli("run", "Work", "--first-backup")
        self.assertEqual(code, EXIT_HELD, out)
        self.assertIn("held work waits for confirmation: mirrorhold preview ", out)
        self.assertNotIn("waits: mirrorhold run", out)
        self.assertEqual(os.listdir(self.vol_e / "Work"), ["Empty"])
        code, out, _ = self.cli("preview", "Work")
        self.assertIn("first run, adopting a destination that holds no file but already holds folders or links", out)


class LeaveCommandTest(TempDirTest):
    """A first run into a folder that already holds what the source does not: the preview
    leaves that where it is unless asked to move it (``preview --move``), and says which.
    In process, through the engine."""

    cli, add = InProcessTest.cli, InProcessTest.add

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        (self.vol_d / "Work" / "Photos").mkdir(parents=True)
        self.vol_e.mkdir()
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),
                                          volume(2, mounts=(str(self.vol_e) + "\\",), label="E")))

    def put(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_preview_leaves_what_the_folder_holds_unless_asked_to_move_it(self):
        self.put(self.vol_d / "Work" / "Photos" / "a.jpg", "a")
        self.put(self.vol_e / "Work" / "Windows" / "kernel.dll", "the operating system")
        self.put(self.vol_e / "Work" / "Photos" / "old.jpg", "an old photo")
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        code, out, _ = self.cli("preview", "Work")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("first run, adopting a destination that already holds 2 files", out)
        self.assertIn("what is already there and not in the source: left where it is (preview with --move to move "
                      "it into versions)", out)
        self.assertIn("left as they are: 2 files - Windows, Photos\\old.jpg", out)
        self.assertIn("in the source and already here: Photos - its 1 file the source does not have is left as it is",
                      out)
        self.assertIn("into versions: 0 not in the source, 0 overwritten", out)
        code, out, _ = self.cli("preview", "Work", "--move")
        self.assertIn("what is already there and not in the source: moved into versions", out)
        self.assertIn("into versions: 2 not in the source, 0 overwritten", out)
        ticket = re.search(r"^ticket ([0-9a-f]+)$", out, re.M).group(1)
        self.assertEqual(self.cli("run", "Work", "--confirm", ticket)[0], EXIT_OK)
        self.assertFalse((self.vol_e / "Work" / "Windows").exists())

    def test_moving_is_not_offered_where_another_jobs_source_lies(self):
        self.put(self.vol_d / "Work" / "a.txt", "a")
        self.put(self.vol_e / "Work" / "Inner" / "data.txt", "another job's source")
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        code, out, _ = self.add("Inner", self.vol_e / "Work" / "Inner", self.vol_d / "InnerCopy")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("NOTICE [source-inside-other-destination]", out)
        code, out, _ = self.cli("preview", "Work", "--move")
        self.assertIn("what is already there and not in the source: left where it is - moving it is not offered "
                      "here: ", out)
        self.assertIn("the source of job 'Inner', is inside this folder", out)

    def test_the_preview_of_a_lost_record_says_what_it_leaves(self):
        self.put(self.vol_d / "Work" / "Photos" / "a.jpg", "a")
        self.put(self.vol_e / "Work" / "Windows" / "kernel.dll", "the operating system")
        self.put(self.vol_e / "Work" / "Photos" / "old.jpg", "an old photo")
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        ticket = re.search(r"^ticket ([0-9a-f]+)$", self.cli("preview", "Work")[1], re.M).group(1)
        self.assertEqual(self.cli("run", "Work", "--confirm", ticket)[0], EXIT_OK)
        shared_path(self.vol_e / "Work").unlink()
        code, out, _ = self.cli("preview", "Work")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("its record of what to leave where it is was lost (", out)
        self.assertIn("left as they are: 2 files - Windows, Photos\\old.jpg", out)
        self.assertIn("in the source and already here: Photos - its 1 file the source does not have is left as it is",
                      out)
        self.assertIn("files this job backed up here whose source copies are gone, if any, are among them: they stay "
                      "as they are from now on", out)
        self.assertNotIn("what is already there and not in the source:", out, "no choice is offered")


class CloudCopyCommandTest(TempDirTest):
    """A destination inside a folder a cloud client keeps a copy of in its cloud: the job is
    saved with the notice, and every preview repeats it beside the destination, as it does
    a client whose list of folders could not be read. In process, through no engine."""

    cli, add = InProcessTest.cli, InProcessTest.add

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        (self.vol_d / "Work").mkdir(parents=True)
        (self.vol_d / "Work" / "a.txt").write_text("a", encoding="utf-8")
        self.vol_e.mkdir()
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),
                                          volume(2, mounts=(str(self.vol_e) + "\\",), label="E")))
        self.copied = [SyncRoot("Google Drive", str(self.vol_e / "Backups"), SyncKind.CLOUD_COPY)]

    def test_the_notice_is_given_when_the_job_is_saved_and_in_every_preview(self):
        code, out, _ = self.cli("add", "Work", "--source", str(self.vol_d / "Work"), "--dest",
                                str(self.vol_e / "Backups" / "Work"), "--no-survey", roots=self.copied)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("NOTICE [destination-in-cloud-copy] destination 1: ", out)
        code, out, _ = self.cli("preview", "Work", roots=self.copied)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn(f"    notice: this folder lies inside {self.vol_e / 'Backups'}, of which Google Drive keeps a "
                      "copy in its cloud; Google Drive may put back what a run moves into versions", out)
        code, out, _ = self.cli("preview", "Work")
        self.assertNotIn("notice:", out)

    def test_a_list_of_folders_that_could_not_be_read_is_said_in_the_preview(self):
        self.assertEqual(self.add("Work", self.vol_d / "Work", self.vol_e / "Work")[0], EXIT_OK)
        code, out, _ = self.cli("preview", "Work", roots=[SyncRoot("Google Drive", "", problem="locked")])
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("    notice: whether Google Drive synchronises this folder cannot be told: locked", out)


class PutBackCommandTest(TempDirTest):
    """Removals that reappeared after being moved into versions: the preview names them apart
    from the others, with when they were moved, and the cloud client the destination's notice
    names. In process, through no engine: the mirror and its versions batch are made by hand."""

    cli, add = InProcessTest.cli, InProcessTest.add
    BATCH = "2026-10-27T22-14-31Z"

    def setUp(self):
        super().setUp()
        self.vol_d, self.vol_e = self.tmp / "volD", self.tmp / "volE"
        self.src, self.dst = self.vol_d / "Work", self.vol_e / "Backups" / "Work"
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),
                                          volume(2, mounts=(str(self.vol_e) + "\\",), label="E")))
        self.copied = [SyncRoot("Google Drive", str(self.vol_e / "Backups"), SyncKind.CLOUD_COPY)]
        for i in range(20):
            self.both(f"keep{i:02}.txt", "k" * (i + 1))
        batch = self.dst / ".mirrorhold" / "versions" / self.BATCH
        for rel, data in (("tree\\a.txt", "a" * 31), ("tree\\sub\\b.txt", "b" * 32), ("gone.txt", "g" * 33)):
            moved = self.file(batch / rel, data)
            shutil.copy2(moved, self.file(self.dst / rel, ""))
        self.file(self.dst / "stray.txt", "s" * 34)
        self.assertEqual(self.add("Work", self.src, self.dst)[0], EXIT_OK)
        job = ConfigStore(self.store_path).load().contents.jobs[0]
        write_marker(self.dst, Marker(job.job_id, job.destinations[0].marker_id))

    @staticmethod
    def file(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return path

    def both(self, rel, data):
        shutil.copy2(self.file(self.src / rel, data), self.file(self.dst / rel, ""))

    def test_the_removals_that_reappeared_are_named_apart_with_when_they_were_moved(self):
        code, out, _ = self.cli("preview", "Work")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("    into versions: 4 deleted at the source (3 of them reappeared), 0 overwritten\n", out)
        self.assertIn("    deleted at the source: stray.txt\n", out)
        self.assertIn("    reappeared after being moved into versions at 2026-10-27T22:14:31Z: 3 - "
                      "tree\\ (whole folder, 2), gone.txt\n", out)
        self.assertNotIn("may have put them back", out)

    def test_the_cloud_client_keeping_a_copy_of_the_folder_is_named(self):
        code, out, _ = self.cli("preview", "Work", roots=self.copied)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("notice: this folder lies inside", out)
        self.assertIn("    Google Drive keeps a copy of this folder and may have put them back\n", out)


@unittest.skipUnless(sys.platform == "win32", "the production volume source is Windows-only")
class EndToEndTest(TempDirTest):
    """Live path: the production entry point, the real volume source and the records of the
    folders cloud clients synchronise (all read-only), a store in a temporary folder."""

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "mirrorhold", "--store", str(self.store), *args],
                                cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
        return result.returncode, result.stdout + result.stderr

    def test_define_save_reload_export_import(self):
        self.store = self.tmp / "Config" / "mirrorhold.json"
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.txt").write_text("a", encoding="utf-8")
        code, out = self.run_cli("add", "Live", "--source", str(self.tmp / "src"), "--dest", str(self.tmp / "dst"))
        self.assertEqual(code, 0, out)
        self.assertIn("1 file, 1 byte", out)
        code, out = self.run_cli("resolve", "Live")
        self.assertEqual(code, 0, out)
        self.assertIn("source: present", out)
        self.assertIn("marker missing", out)
        export = self.tmp / "jobs-export.json"
        self.assertEqual(self.run_cli("export", str(export))[0], 0)
        self.store = self.tmp / "Second" / "mirrorhold.json"
        code, out = self.run_cli("import", str(export))
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("validate", "Live", "--no-survey")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
