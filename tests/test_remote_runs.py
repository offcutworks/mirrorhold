"""Runs and previews onto a remote destination, end to end on the pinned engine.

Remotes are the engine's own S3, WebDAV and SFTP servers on the loopback address, a local
folder named as a remote and an encrypted remote over another, named in a throwaway
configuration whose secrets are invented (``test_engine_contract.Loopback``); a remote
whose server does not run and an encrypted configuration of the test's own complete the
set. Nothing reaches the internet and the user's configuration is never named: every run
names a store in the test's folder, whose preferences name the throwaway file.

Covered: a first backup and the next run on every backend, nothing planned again (each
backend's times kept within its precision); renames, removals and replacements moving
into the versions batch on the remote; a held run and its confirmation; the command
line's words; an unreachable remote waiting, a locked configuration refused with its
reason and unlocked by the user's command; a name the remote cannot keep failing alone;
names the remote holds that the plan cannot tell apart; a remote that stops answering mid
run, or as its marker, its record of what to leave or its changed files' leading bytes are
read, told from a file it will not serve while it answers; a run and a preview of a remote
whose space cannot be asked; the engine's leftovers; the version cleanup and what a service may keep of it; what
reappeared after a move into versions; a shared destination; a scheduled run; an upload
outlasting the bound on an engine making no progress; and that no file the runs write
holds a secret of the configuration. Without the engine: which
destinations an arrival runs, the gate on a destination whose free space is unknown, what a
record says the service may keep, and the run's notice for a remote."""
import io
import json
import os
import re
import shutil
import socket
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from support import GIB, endpoint, remove_temporary_folder, temporary_folder, volume
from test_engine_contract import BACKENDS, ENGINE, Loopback, MiB, needs_engine

from mirrorhold.automation.arrival import arrival_selection, destination_key, source_key
from mirrorhold.automation.automatic import run_scheduled
from mirrorhold.cli import EXIT_OK, main
from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.engine.rclone import Configuration, RemoteRoot, open_engine, run_engine
from mirrorhold.engine.remote import RemoteEntry, RemoteSession
from mirrorhold.jobs.model import (HoldThresholds, Notifications, Preferences, RemoteSettings, Triggers, Unlock,
                                   VersionCleanup, new_job)
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.notifications.message import notice_for
from mirrorhold.planning.listing import Entry, Side, TreeListing
from mirrorhold.planning.plan import build_plan
from mirrorhold.contract_errors import ContractError
from mirrorhold.engine.remote import RemoteError, Space as ServiceSpace
from mirrorhold.maintenance.cleanup import CleanupReport, ExpiredBatch
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, RECORD_SCHEMA, Outcome, RunRecord,
                                       VersionsCleaned, read_records, write_record)
from mirrorhold.run import apply
from mirrorhold.run.gate import Space, Stop, Verdict, judge
from mirrorhold.endpoints.shared import SHARED_RELATIVE
from mirrorhold.run.runner import RunOptions, _remote_space, preview_job, run_job
from mirrorhold.storage.destination import StorageError
from mirrorhold.storage.remote import RemoteStorage

MANAGEMENT = ".mirrorhold"


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write(root: Path, relative: str, data: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return path


def unanswered_at(relative: str, original):
    """``original``, a storage's operation, failing as an engine that stopped answering for the
    file at ``relative`` alone."""
    def call(storage, at, *args, **options):
        if at == relative:
            raise StorageError("the sync engine did not answer: invented")
        return original(storage, at, *args, **options)
    return call


@needs_engine
class RemoteRunCase(unittest.TestCase):
    """One set of loopback servers for the class; each test makes its source volume, its
    store and its remote folders of its own."""

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.lock = temporary_folder()
        try:
            cls.loopback = Loopback(cls.tmp / "loopback")
            cls.session = RemoteSession(open_engine(), cls.loopback.configuration)
        except BaseException:
            remove_temporary_folder(cls.tmp, cls.lock)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.loopback.close()
        remove_temporary_folder(cls.tmp, cls.lock)

    def setUp(self):
        self.case = uuid.uuid4().hex[:8]
        self.base = self.tmp / "cases" / self.case
        self.vol_s = self.base / "volS"
        self.vol_s.mkdir(parents=True)
        self.s = volume(1, mounts=(str(self.vol_s) + "\\",), label="S")
        self.volumes = FixedVolumeSource((self.s,))
        self.store = ConfigStore(self.base / "Config" / "mirrorhold.json")
        self.layout = runtime_layout(self.store.path)
        self.settings = RemoteSettings(str(self.loopback.path))

    # --- set-up -----------------------------------------------------------------

    def job_onto(self, backend: str, folder: str = "Work", **settings):
        """A job backing up the source folder ``folder`` onto a folder of its own on
        ``backend``'s remote, saved alone in the store; the job and the remote folder."""
        (self.vol_s / folder).mkdir(exist_ok=True)
        root = self.loopback.root_of(backend, f"{self.case}-{folder}")
        settings.setdefault("holds", HoldThresholds(count_floor=3, percent=5))
        job = new_job(folder, endpoint(self.s, folder), [RemoteEndpoint(root.remote, root.folder)], **settings)
        self.save(job)
        return job, root

    def save(self, *jobs, settings=None):
        self.store.save(StoreContents(tuple(jobs), Preferences(remotes=settings or self.settings)))

    def run_now(self, job, **options):
        return run_job(self.store, job.job_id, RunOptions(**options), volume_source=self.volumes, sync_roots=())

    def only(self, result):
        self.assertEqual(len(result.records), 1, result.records)
        return result.records[0]

    def preview(self, job):
        return preview_job(self.store, job.job_id, volume_source=self.volumes, sync_roots=())

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--store", str(self.store.path), *args], volume_source=self.volumes, sync_roots=(), out=out,
                    err=err)
        return code, out.getvalue(), err.getvalue()

    # --- the remote as it is --------------------------------------------------------

    def files(self, root: RemoteRoot, below: str = ""):
        """The remote folder's files outside the management folder (or below ``below``):
        each relative path to its size."""
        found = self.session.list_tree(root, below) or ()
        prefix = f"{below}\\" if below else ""
        return {e.path[len(prefix):]: e.size for e in found if not e.is_dir
                and (below or not e.path.casefold().startswith(MANAGEMENT))}

    def managed(self, root: RemoteRoot, relative: str) -> bool:
        return self.session.stat(root, f"{MANAGEMENT}\\{relative}") is not None

    def seed(self, job, files):
        src = self.vol_s / job.name
        for relative, data in files.items():
            write(src, relative, data)
        record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        return record


class FirstAndNextRunTest(RemoteRunCase):
    def test_a_first_backup_and_the_next_run_on_every_backend(self):
        """MEASURED: on every backend a first backup copies the source, writes the marker,
        the job's definition and the record on the remote, and the next run finds every file
        unchanged — each service keeps the times it was given within its own precision."""
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                job, root = self.job_onto(backend, f"W{backend}")
                record = self.seed(job, {"a.txt": "a", "sub\\b.txt": "bb", "c d.txt": "ccc"})
                self.assertEqual(record.done["files_copied"], 3)
                self.assertEqual(self.files(root), {"a.txt": 1, "sub\\b.txt": 2, "c d.txt": 3})
                for relative in ("marker.json", "job.json", f"runs\\{record.file_name()}"):
                    self.assertTrue(self.managed(root, relative), relative)
                pair = self.preview(job).pairs[0].assessment
                self.assertTrue(pair.plan.nothing_to_do, pair.plan)
                self.assertEqual(pair.plan.unchanged, 3)
                again = self.only(self.run_now(job))
                self.assertEqual((again.outcome, again.done["files_copied"], again.done["files_identical"]),
                                 (Outcome.COMPLETED, 0, 0))

    def test_renames_removals_and_replacements_move_within_the_remote(self):
        """MEASURED: a file moved at the source is moved on the remote, not copied again; a
        file deleted at the source and one replaced move into the run's versions batch on the
        remote, S3's by a copy and a delete on the service."""
        for backend in ("s3", "webdav", "crypt"):
            with self.subTest(backend=backend):
                job, root = self.job_onto(backend, f"R{backend}", holds=HoldThresholds(count_floor=3, percent=5,
                                                                                       share=None))
                self.seed(job, {"a.txt": "a", "sub\\b.txt": "bb", "c d.txt": "ccc"})
                src = self.vol_s / job.name
                write(src, "a.txt", "aaaa")
                (src / "moved").mkdir()
                os.replace(src / "sub" / "b.txt", src / "moved" / "b.txt")
                (src / "c d.txt").unlink()
                record = self.only(self.run_now(job))
                self.assertEqual(record.outcome, Outcome.COMPLETED, record)
                self.assertEqual((record.done["renamed"], record.done["files_copied"],
                                  record.done["moved_to_versions"]), (1, 1, 2))
                self.assertEqual(self.files(root), {"a.txt": 4, "moved\\b.txt": 2})
                self.assertEqual(self.files(root, record.versions_batch.replace("/", "\\")),
                                 {"a.txt": 1, "c d.txt": 3})

    def test_a_drive_destination_beside_a_remote_one_is_backed_up_as_before(self):
        vol_d = self.base / "volD"
        vol_d.mkdir()
        d = volume(2, mounts=(str(vol_d) + "\\",), label="D")
        self.volumes = FixedVolumeSource((self.s, d))
        (self.vol_s / "Work").mkdir()
        write(self.vol_s / "Work", "a.txt", "a")
        root = self.loopback.root_of("local", f"{self.case}-both")
        job = new_job("Work", endpoint(self.s, "Work"), [endpoint(d, "Backup"), RemoteEndpoint(root.remote, root.folder)])
        self.save(job)
        records = self.run_now(job, first_backup=True).records
        self.assertEqual([r.outcome for r in records], [Outcome.COMPLETED, Outcome.COMPLETED], records)
        self.assertEqual(sorted(p.name for p in (vol_d / "Backup").iterdir()), [MANAGEMENT, "a.txt"])
        self.assertEqual(self.files(root), {"a.txt": 1})


class HeldRunTest(RemoteRunCase):
    def test_removals_above_the_threshold_are_held_and_a_confirmed_preview_applies_them(self):
        for backend in ("s3", "local"):
            with self.subTest(backend=backend):
                job, root = self.job_onto(backend, f"H{backend}", holds=HoldThresholds(count_floor=1, percent=5))
                self.seed(job, {"a.txt": "a", "b.txt": "bb", "c.txt": "ccc", "d.txt": "dddd"})
                src = self.vol_s / job.name
                for name in ("a.txt", "b.txt", "c.txt"):
                    (src / name).unlink()
                write(src, "new.txt", "new file")
                record = self.only(self.run_now(job))
                self.assertEqual(record.outcome, Outcome.HELD, record)
                self.assertEqual(self.files(root), {"a.txt": 1, "b.txt": 2, "c.txt": 3, "d.txt": 4, "new.txt": 8})
                ticket = self.preview(job).ticket
                confirmed = self.only(self.run_now(job, ticket_id=ticket.ticket_id))
                self.assertEqual(confirmed.outcome, Outcome.COMPLETED, confirmed)
                self.assertEqual(self.files(root), {"d.txt": 4, "new.txt": 8})
                self.assertEqual(self.files(root, confirmed.versions_batch.replace("/", "\\")),
                                 {"a.txt": 1, "b.txt": 2, "c.txt": 3})


class CommandLineTest(RemoteRunCase):
    def test_a_preview_names_the_remote_and_its_unknown_free_space_and_a_first_backup_runs(self):
        job, root = self.job_onto("s3")
        write(self.vol_s / "Work", "a.txt", "a")
        code, out, err = self.cli("preview", "Work")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn(f"destination 1 at {root.fs}: first run onto an empty destination", out)
        self.assertIn("    notice: this remote does not encrypt", out)
        self.assertIn("    free space: the service reports none, so it is not checked; a copy that does not fit fails "
                      "file by file", out)
        self.assertNotIn("does not back up yet", out)
        code, out, err = self.cli("run", "Work", "--first-backup")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertEqual(self.files(root), {"a.txt": 1})

    def test_a_confirmed_preview_runs_onto_a_remote(self):
        job, root = self.job_onto("local")
        write(self.vol_s / "Work", "a.txt", "a")
        code, out, _ = self.cli("preview", "Work")
        ticket = re.search(r"^ticket (\w+)$", out, re.M).group(1)
        code, out, err = self.cli("run", "Work", "--confirm", ticket)
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertEqual(self.files(root), {"a.txt": 1})


class ReachTest(RemoteRunCase):
    def test_a_remote_whose_server_does_not_answer_is_recorded_absent_and_waits(self):
        configuration = self.base / "gone.conf"
        configuration.write_text(f"[gone]\ntype = webdav\nurl = http://127.0.0.1:{closed_port()}/\nvendor = rclone\n",
                                 encoding="utf-8")
        (self.vol_s / "Work").mkdir()
        write(self.vol_s / "Work", "a.txt", "a")
        job = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("gone", "Work")])
        self.save(job, settings=RemoteSettings(str(configuration)))
        record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.ABSENT, ("remote-unreachable",)))
        self.assertTrue(record.detail.startswith("gone:Work cannot be reached: "), record.detail)
        self.assertTrue(record.detail.endswith("; the next run tries again"), record.detail)

    def test_a_locked_configuration_is_refused_with_why_and_the_users_command_unlocks_it(self):
        configuration = self.base / "locked.conf"
        folder = (self.base / "remote" / "Work").as_posix()
        configuration.write_text("[loc]\ntype = local\n", encoding="utf-8")
        script = self.base / "pass word" / "unlock.py"
        script.parent.mkdir()
        script.write_text("import sys\nsys.stdout.write('invented-locked-pass\\n')\n", encoding="utf-8")
        command = (sys.executable, str(script))
        result = run_engine(ENGINE, ["config", "encryption", "set"], configuration=Configuration(configuration, command),
                            timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.vol_s / "Work").mkdir()
        write(self.vol_s / "Work", "a.txt", "a")
        job = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("loc", folder)])
        self.save(job, settings=RemoteSettings(str(configuration)))
        record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("remote-configuration-unusable",)))
        self.assertIn(f"the configuration {configuration} cannot be opened", record.detail)
        self.save(job, settings=RemoteSettings(str(configuration), Unlock.COMMAND, command))
        record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), [MANAGEMENT, "a.txt"])

    def test_a_remote_below_the_floor_or_unknown_is_refused_with_why(self):
        job, _ = self.job_onto("local")
        write(self.vol_s / "Work", "a.txt", "a")
        unknown = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("elsewhere", "Work")])
        self.save(unknown)
        record = self.only(self.run_now(unknown, first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("remote-unknown",)))
        self.assertEqual(record.detail, "the rclone configuration holds no remote named 'elsewhere'")


class FilesTest(RemoteRunCase):
    def test_a_name_the_remote_cannot_keep_fails_alone(self):
        """MEASURED: an encrypted remote's name grows past what the folder beneath keeps for
        a long name, so its copy fails in the engine and the other files are copied."""
        job, root = self.job_onto("crypt")
        long_name = "n" * 200 + ".txt"
        record = self.seed(job, {"a.txt": "a", long_name: "long"})
        self.assertEqual(self.files(root), {"a.txt": 1})
        self.assertIn(long_name, [path for path, _why in record.skipped])

    def test_names_the_remote_holds_that_the_plan_cannot_tell_apart_stop_the_pair(self):
        """A service that tells names apart by case alone, or keeps two of one name in a
        folder, lists what the plan would read as one file: the pair stops, nothing moved."""
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})
        listed = RemoteSession.list_folder

        def twin(session, at, relative="", **options):
            found = listed(session, at, relative, **options)
            if at == root and relative == "" and found:
                found += (RemoteEntry("A.TXT", False, 5, found[0].mtime_ns),)
            return found

        with mock.patch.object(RemoteSession, "list_folder", autospec=True, side_effect=twin):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("destination-names-ambiguous",)))
        self.assertEqual(record.detail, "the remote holds names this tool cannot tell apart: a.txt and A.TXT; rename "
                                        "or remove one of them on the service")
        self.assertEqual(self.files(root), {"a.txt": 1})

    def test_a_remote_that_stops_answering_mid_run_fails_the_pair_with_what_was_done(self):
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})
        write(self.vol_s / "Work", "b.txt", "bb")
        written = RemoteStorage.write_file

        def failing(storage, relative, data, **options):
            if relative.endswith("job.json"):
                raise StorageError("the sync engine did not answer: invented failure")
            return written(storage, relative, data, **options)

        with mock.patch.object(RemoteStorage, "write_file", autospec=True, side_effect=failing):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(record.detail, "the remote stopped answering while the run worked: the sync engine did not "
                                        "answer: invented failure; what was done is recorded and the next run "
                                        "converges")
        self.assertEqual(self.only(self.run_now(job)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.files(root), {"a.txt": 1, "b.txt": 2})

    def test_a_free_space_the_service_reports_without_a_total_is_judged(self):
        job, root = self.job_onto("local")
        write(self.vol_s / "Work", "a.txt", "a")
        with mock.patch.object(RemoteSession, "space", autospec=True, return_value=ServiceSpace(1000, None)):
            record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, (Stop.INSUFFICIENT_SPACE.value,)))
        self.assertEqual(self.files(root), {})

    def test_a_remote_whose_space_cannot_be_asked_fails_the_pair_rather_than_pass_the_check(self):
        job, root = self.job_onto("local")
        write(self.vol_s / "Work", "a.txt", "a")
        with mock.patch.object(RemoteSession, "space", autospec=True,
                               side_effect=RemoteError("dial tcp 127.0.0.1:9: connectex: invented refusal")):
            record = self.only(self.run_now(job, first_backup=True))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(self.files(root), {})

    def test_a_preview_of_a_remote_whose_space_cannot_be_asked_says_the_remote_failed(self):
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})
        write(self.vol_s / "Work", "b.txt", "bb")
        with mock.patch.object(RemoteSession, "space", autospec=True,
                               side_effect=RemoteError("dial tcp 127.0.0.1:9: connectex: invented refusal")):
            preview = self.preview(job)
            code, out, _ = self.cli("preview", "Work")
        previewed = preview.pairs[0].assessment
        self.assertEqual((previewed.outcome, previewed.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertIsNone(preview.ticket)
        self.assertEqual(code, EXIT_OK)
        self.assertIn(f"failed (remote-failed) - {previewed.detail}", out)
        self.assertEqual(self.files(root), {"a.txt": 1})

    def test_a_remote_that_stops_answering_at_its_marker_fails_the_pair_rather_than_stop_it_as_unreadable(self):
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})

        def failing(storage, *args, **options):
            raise StorageError("the sync engine did not answer: invented")

        with mock.patch.object(RemoteStorage, "read_file", autospec=True, side_effect=failing), \
                mock.patch.object(RemoteStorage, "entry", autospec=True, side_effect=failing):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))

    def changed_picture(self):
        """A job seeded with a picture whose source copy then lost its signature, as
        encryption leaves it; the job, its remote folder and the picture's size there."""
        job, root = self.job_onto("local", holds=HoldThresholds(count_floor=3, percent=5, share=None))
        picture = self.vol_s / "Work" / "photo.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
        self.seed(job, {"a.txt": "a"})
        picture.write_bytes(b"bytes that match no format at all")
        return job, root, 14

    def test_a_remote_that_stops_answering_as_its_changed_files_are_read_fails_the_pair_before_applying(self):
        job, root, size = self.changed_picture()
        before = "the remote stopped answering before any of the plan was applied: "
        with mock.patch.multiple(RemoteStorage, read_head=unanswered_at("photo.png", RemoteStorage.read_head),
                                 entry=unanswered_at("photo.png", RemoteStorage.entry)):
            record = self.only(self.run_now(job))
            previewed = self.preview(job).pairs[0].assessment
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(record.detail, f"{before}the sync engine did not answer: invented; the next run tries again")
        self.assertEqual((previewed.reasons, previewed.detail), (record.reasons, record.detail))
        self.assertEqual(self.files(root), {"a.txt": 1, "photo.png": size})

    def test_a_changed_file_a_remote_will_not_serve_while_it_answers_is_not_judged(self):
        job, root, _size = self.changed_picture()
        self.assertIn("lost-signatures", self.preview(job).pairs[0].assessment.decision.reasons)
        with mock.patch.multiple(RemoteStorage, read_head=unanswered_at("photo.png", RemoteStorage.read_head)):
            previewed = self.preview(job).pairs[0].assessment
            record = self.only(self.run_now(job))
        self.assertIsNone(previewed.outcome)
        self.assertNotIn("lost-signatures", previewed.decision.reasons)
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(self.files(root), {"a.txt": 1, "photo.png": 33})

    def shared_destination(self):
        """A job whose first run left what its remote folder held there; the job and the
        remote folder."""
        job, root = self.job_onto("local")
        self.session.write(root, "theirs.txt", b"not the job's")
        write(self.vol_s / "Work", "a.txt", "a")
        ticket = self.preview(job).ticket
        self.assertEqual(self.only(self.run_now(job, ticket_id=ticket.ticket_id)).outcome, Outcome.COMPLETED)
        write(self.vol_s / "Work", "b.txt", "bb")
        return job, root

    def test_a_remote_that_stops_answering_as_its_shared_record_is_read_fails_the_pair(self):
        job, root = self.shared_destination()
        before = "the remote stopped answering before any of the plan was applied: "
        with mock.patch.multiple(RemoteStorage, read_file=unanswered_at(SHARED_RELATIVE, RemoteStorage.read_file),
                                 entry=unanswered_at(SHARED_RELATIVE, RemoteStorage.entry)):
            record = self.only(self.run_now(job))
            previewed = self.preview(job).pairs[0].assessment
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(record.detail, f"{before}the sync engine did not answer: invented; the next run tries again")
        self.assertEqual((previewed.reasons, previewed.detail), (record.reasons, record.detail))
        self.assertEqual(self.files(root), {"a.txt": 1, "theirs.txt": 13})

    def test_a_shared_record_a_remote_will_not_serve_while_it_answers_stops_the_pair_untouched(self):
        job, root = self.shared_destination()
        with mock.patch.multiple(RemoteStorage, read_file=unanswered_at(SHARED_RELATIVE, RemoteStorage.read_file)):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.STOPPED, ("shared-record-unreadable",)))
        self.assertTrue(record.detail.startswith("its record of what to leave where it is cannot be used: "),
                        record.detail)
        self.assertEqual(self.files(root), {"a.txt": 1, "theirs.txt": 13})

    def test_a_remote_that_stops_answering_before_the_plan_is_applied_says_so_and_nothing_of_work_done(self):
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})
        write(self.vol_s / "Work", "b.txt", "bb")

        def failing(storage, *args, **options):
            raise StorageError("the sync engine did not answer: invented")

        before = "the remote stopped answering before any of the plan was applied: "
        with mock.patch.object(RemoteStorage, "read_file", autospec=True, side_effect=failing), \
                mock.patch.object(RemoteStorage, "entry", autospec=True, side_effect=failing):
            record = self.only(self.run_now(job))
            previewed = self.preview(job).pairs[0].assessment
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(record.detail, f"{before}the sync engine did not answer: invented; the next run tries again")
        self.assertEqual((previewed.reasons, previewed.detail), (record.reasons, record.detail))
        self.assertEqual(self.files(root), {"a.txt": 1})

        with mock.patch.object(RemoteSession, "space", autospec=True,
                               side_effect=RemoteError("dial tcp 127.0.0.1:9: connectex: invented refusal")):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertTrue(record.detail.startswith(before), record.detail)
        self.assertIn("invented refusal", record.detail)
        self.assertNotIn("recorded", record.detail)
        self.assertEqual(self.files(root), {"a.txt": 1})

    def test_a_remote_that_stops_answering_as_the_plan_is_applied_says_what_was_done_is_recorded(self):
        job, root = self.job_onto("local")
        self.seed(job, {"a.txt": "a"})
        write(self.vol_s / "Work", "b.txt", "bb")
        made = RemoteStorage.make_folder

        def failing(storage, relative=""):
            if relative == "":
                raise StorageError("the sync engine did not answer: invented failure")
            return made(storage, relative)

        with mock.patch.object(RemoteStorage, "make_folder", autospec=True, side_effect=failing):
            record = self.only(self.run_now(job))
        self.assertEqual((record.outcome, record.reasons), (Outcome.FAILED, ("remote-failed",)))
        self.assertEqual(record.detail, "the remote stopped answering while the run worked: the sync engine did not "
                                        "answer: invented failure; what was done is recorded and the next run "
                                        "converges")
        self.assertEqual(self.only(self.run_now(job)).outcome, Outcome.COMPLETED)
        self.assertEqual(self.files(root), {"a.txt": 1, "b.txt": 2})

    def test_the_engines_leftovers_on_the_remote_are_removed(self):
        for backend in ("local", "sftp"):
            with self.subTest(backend=backend):
                job, root = self.job_onto(backend, f"T{backend}")
                self.seed(job, {"a.txt": "a"})
                self.session.write(root, "a.txt.0123abcd.partial", b"half")
                record = self.only(self.run_now(job))
                self.assertEqual((record.outcome, record.done["temporaries_removed"]), (Outcome.COMPLETED, 1))
                self.assertEqual(self.files(root), {"a.txt": 1})

    def test_what_reappeared_after_a_move_into_versions_is_named_by_the_preview(self):
        job, root = self.job_onto("local", holds=HoldThresholds(count_floor=3, percent=5, share=None))
        self.seed(job, {"a.txt": "a", "gone.txt": "gone"})
        (self.vol_s / "Work" / "gone.txt").unlink()
        record = self.only(self.run_now(job))
        batch = Path(root.folder) / record.versions_batch
        shutil.copy2(batch / "gone.txt", Path(root.folder) / "gone.txt")
        item = self.preview(job).pairs[0]
        self.assertIsNotNone(item.put_back)
        self.assertEqual(item.put_back.keys, frozenset({"gone.txt"}))

    def test_a_first_run_onto_a_remote_folder_holding_files_leaves_them_and_shares_it(self):
        job, root = self.job_onto("local")
        self.session.write(root, "theirs.txt", b"not the job's")
        write(self.vol_s / "Work", "a.txt", "a")
        ticket = self.preview(job).ticket
        record = self.only(self.run_now(job, ticket_id=ticket.ticket_id))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(self.files(root), {"a.txt": 1, "theirs.txt": 13})
        self.assertTrue(self.managed(root, "shared.json"))


class CleanupTest(RemoteRunCase):
    def test_expired_batches_are_deleted_on_the_remote_and_what_the_service_may_keep_is_said(self):
        """MEASURED: S3 reports that it empties a trash of its own (``CleanUp``); a local
        remote does not."""
        for backend, trash in (("s3", True), ("local", False)):
            with self.subTest(backend=backend):
                job, root = self.job_onto(backend, f"C{backend}", version_cleanup=VersionCleanup(keep_days=1))
                self.seed(job, {"a.txt": "a"})
                old = f"{MANAGEMENT}\\versions\\2020-01-01T00-00-00Z"
                self.session.write(root, f"{old}\\old.txt", b"old")
                write(self.vol_s / job.name, "b.txt", "bb")
                pair = self.preview(job).pairs[0].assessment
                self.assertEqual((len(pair.cleanup.batches), pair.cleanup.trash), (1, trash))
                record = self.only(self.run_now(job))
                self.assertEqual(record.outcome, Outcome.COMPLETED, record)
                self.assertEqual((record.versions_cleaned.batch_total, record.versions_cleaned.trash), (1, trash))
                stored = [r for r in read_records(self.layout, job.job_id) if r.run_id == record.run_id]
                self.assertEqual(stored, [record])
                self.assertIsNone(self.session.stat(root, old))
                code, out, _ = self.cli("preview", job.name)
                self.assertEqual(code, EXIT_OK)


class UnattendedTest(RemoteRunCase):
    def test_a_scheduled_run_backs_up_onto_the_remote(self):
        job, root = self.job_onto("webdav", triggers=Triggers(schedule="daily 03:00"))
        self.seed(job, {"a.txt": "a"})
        write(self.vol_s / "Work", "b.txt", "bb")
        result = run_scheduled(self.store, job.job_id, volume_source=self.volumes, sync_roots=())
        self.assertEqual([r.outcome for run in result.runs for r in run.records], [Outcome.COMPLETED])
        self.assertEqual(self.files(root), {"a.txt": 1, "b.txt": 2})


class ProgressBoundTest(RemoteRunCase):
    def test_an_upload_slower_than_the_bound_is_progress_and_the_run_completes(self):
        """MEASURED: while the engine uploads onto a WebDAV remote at the bandwidth limit, its
        copied bytes move at every reading of its figures, so the bound on an engine making no
        progress (``run.apply.STALL_LIMIT``, made a few seconds here) never strikes an upload
        that outlasts it twice over."""
        job, root = self.job_onto("webdav")
        (self.vol_s / job.name / "big.bin").write_bytes(os.urandom(8 * MiB))
        limit, progress, readings = 3.0, apply._progress, []

        def reading(stats):
            figures = progress(stats)
            readings.append((time.monotonic(), figures))
            return figures

        with mock.patch.object(apply, "STALL_LIMIT", limit), mock.patch.object(apply, "STALL_LOOK", 0.5), \
                mock.patch.object(apply, "_progress", side_effect=reading):
            record = self.only(self.run_now(job, first_backup=True, bandwidth_limit="1M"))
        self.assertEqual(record.outcome, Outcome.COMPLETED, record)
        self.assertEqual(self.files(root), {"big.bin": 8 * MiB})
        moves, seen = [], None
        for at, figures in readings:
            if figures is not None and figures != seen:
                if seen is not None:
                    self.assertGreater(figures[0], seen[0], "the copied bytes carry the progress")
                moves.append(at)
                seen = figures
        self.assertGreaterEqual(moves[-1] - moves[0], 2 * limit, "the upload outlasts the bound")
        self.assertLess(max(b - a for a, b in zip(moves, moves[1:])), limit, readings)


class SecretsTest(RemoteRunCase):
    def test_no_file_the_runs_write_holds_a_secret_of_the_configuration(self):
        text = self.loopback.path.read_text(encoding="utf-8")
        secrets = set(re.findall(r"^(?:pass|password|secret_access_key) = (\S+)$", text, re.M))
        secrets |= {"loopbacksecret", "loopback-dav-pass", "loopback-sftp-pass", "loopback-crypt-pass"}
        roots = []
        for backend in BACKENDS:
            job, root = self.job_onto(backend, f"S{backend}")
            self.seed(job, {"a.txt": "a"})
            write(self.vol_s / job.name, "a.txt", "changed")
            self.run_now(job)
            roots.append(root)
        written = [p for p in self.base.rglob("*") if p.is_file() and not p.is_relative_to(self.vol_s)]
        for root in roots:
            for entry in self.session.list_tree(root, MANAGEMENT) or ():
                if not entry.is_dir:
                    written.append((root, entry.path))
        self.assertTrue(written)
        for item in written:
            data = item.read_bytes() if isinstance(item, Path) else self.session.read(*item)
            for secret in secrets:
                self.assertNotIn(secret.encode("utf-8"), data, item)


class WithoutEngineTest(unittest.TestCase):
    def test_the_sources_arrival_runs_a_remote_destination_and_a_drives_arrival_does_not(self):
        s, d = volume(1, label="S"), volume(2, label="D")
        job = new_job("Work", endpoint(s, "Work"), [endpoint(d, "Backup"), RemoteEndpoint("cloud", "Backups")])
        drive = destination_key(job.destinations[0].marker_id)
        present = frozenset({source_key(job), drive})
        self.assertEqual(arrival_selection(job, present, frozenset({source_key(job)})), (1, 2))
        self.assertEqual(arrival_selection(job, present, frozenset({drive})), (1,))
        self.assertIsNone(arrival_selection(job, frozenset({drive}), frozenset({drive})))

    def test_the_gate_judges_a_free_space_the_destination_reports_and_none_it_does_not(self):
        listing = TreeListing(Path("s"), Side.SOURCE, files={"a": Entry("a", 5 * GIB, 0)})
        plan = build_plan(listing, TreeListing(Path("d"), Side.DESTINATION))
        unknown = judge(plan, holds=HoldThresholds(), first_run=False, destination=Space(None))
        self.assertEqual((unknown.verdict, unknown.free_bytes), (Verdict.PROCEED, None))
        small = judge(plan, holds=HoldThresholds(), first_run=False, destination=Space(GIB))
        self.assertEqual(small.verdict, Verdict.STOP)

    def test_a_record_says_what_the_service_may_keep_and_one_older_than_schema_5_says_nothing(self):
        cleaned = VersionsCleaned(30, ("2026-06-01T10-00-00Z",), 1, 2, 300, (), True)
        record = RunRecord("r" * 32, "j" * 32, "Job", "m" * 32, 1, "manual", "2026-10-06T10:00:00Z",
                           "2026-10-06T10:05:00Z", Outcome.COMPLETED, planned=dict.fromkeys(PLANNED_KEYS, 0),
                           done=dict.fromkeys(DONE_KEYS, 0), left=dict.fromkeys(LEFT_KEYS, 0),
                           versions_cleaned=cleaned)
        doc = record.to_document()
        self.assertEqual((doc["schema_version"], doc["versions_cleaned"]["trash"]), (RECORD_SCHEMA, True))
        self.assertEqual(RunRecord.from_document(doc), record)
        self.assertIn("; the service may keep them until it removes them", cleaned.describe())
        records = runtime_layout(Path(self.id().replace(".", "_")) / "mirrorhold.json")
        with mock.patch("mirrorhold.records.record._write_json") as written:
            write_record(records, record)
        self.assertEqual(RunRecord.from_document(json.loads(json.dumps(written.call_args[0][1]))), record)
        for wrong in ("yes", None):
            with self.assertRaises(ContractError):
                RunRecord.from_document({**doc, "versions_cleaned": {**doc["versions_cleaned"], "trash": wrong}})
        del doc["encrypted"]                                            # schema 6's
        doc["schema_version"] = 5
        self.assertTrue(RunRecord.from_document(doc).versions_cleaned.trash)
        with self.assertRaises(ContractError):
            RunRecord.from_document({**doc, "schema_version": 4})          # schema 4 knows no trash
        del doc["versions_cleaned"]["trash"]
        doc["schema_version"] = 4
        self.assertFalse(RunRecord.from_document(doc).versions_cleaned.trash)
        with self.assertRaises(ContractError):
            RunRecord.from_document({**doc, "schema_version": 5})          # schema 5 says it

    def test_a_preview_counts_what_the_cleanup_frees_only_where_the_service_frees_it(self):
        class Reporting:
            def __init__(self, free):
                self.free = free

            def free_bytes(self):
                if isinstance(self.free, Exception):
                    raise self.free
                return self.free

        batch = ExpiredBatch("2020-01-01T00-00-00Z", datetime(2020, 1, 1, tzinfo=timezone.utc), 1, 50)
        self.assertEqual(_remote_space(Reporting(100), CleanupReport(1, (batch,))).free_bytes, 150)
        self.assertEqual(_remote_space(Reporting(100), CleanupReport(1, (batch,), trash=True)).free_bytes, 100)
        self.assertIsNone(_remote_space(Reporting(None), CleanupReport(1, (batch,))).free_bytes)
        with self.assertRaises(StorageError):
            _remote_space(Reporting(StorageError("the sync engine did not answer")))

    def test_the_runs_notice_says_a_remotes_low_space_is_at_the_remote(self):
        record = RunRecord("r" * 32, "j" * 32, "Job", "m" * 32, 1, "manual", "2026-10-06T10:00:00Z",
                           "2026-10-06T10:05:00Z", Outcome.COMPLETED, planned=dict.fromkeys(PLANNED_KEYS, 0),
                           done=dict.fromkeys(DONE_KEYS, 0), left=dict.fromkeys(LEFT_KEYS, 0), space=(6, 100))
        chosen = Notifications(on_completion=True)
        self.assertTrue(notice_for([record], chosen).lines[0].endswith(" - only 6 % free on its drive"))
        self.assertTrue(notice_for([record], chosen, remote=frozenset({1})).lines[0]
                        .endswith(" - only 6 % free at its remote"))


if __name__ == "__main__":
    unittest.main()
