"""The window's logic against the pinned engine: what it asks, and what it reads back.

Remotes are the engine's loopback servers and local and encrypted remotes in throwaway
configurations (``test_engine_contract.Loopback``), as in ``test_remote_runs``. No test
imports tkinter, none reads the user's configuration, and every store is the test's.

Covered: the editor's checks on what the engine says of a job's remote destinations; the
preferences' check of a configuration and the remembering of its password, a wrong one kept
nowhere; a remote's folders browsed through one session; a preview of a remote in words,
reached and not; and the job list and the details read back from a real run's records."""
import sys
import unittest
from dataclasses import replace

from support import TempDirTest, endpoint
from test_engine_contract import ENGINE, needs_engine
from test_remote_runs import RemoteRunCase, closed_port, write

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.engine.rclone import Configuration, run_engine
from mirrorhold.jobs.model import Preferences, RemoteSettings, Unlock, new_job
from mirrorhold.locations import runtime_layout
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, RECORD_SCHEMA, Outcome, RunRecord,
                                       VersionsCleaned, read_records)

from mirrorhold_gui.editor_model import JobDraft, check, endpoint_line
from mirrorhold_gui.history_model import record_details
from mirrorhold_gui.jobs_model import RemotePlace, build_rows
from mirrorhold_gui.prefs_model import check_configuration, remember, remotes_draft
from mirrorhold_gui.preview_model import PairState, summarise
from mirrorhold_gui.remote_model import RemoteAccess, ask_setup, child_folder

CONFIG_PASS = "invented-config-pass"


class EditorAndPreviewTest(RemoteRunCase):
    def test_the_editor_judges_what_the_engine_says(self):
        job, root = self.job_onto("local", "Asked")
        preferences = self.store.load().contents.preferences
        target = RemoteEndpoint(root.remote, root.folder)
        draft = JobDraft(name="Asked", source=endpoint(self.s, "Asked"))
        draft.add_remote_destination(root.remote, root.folder)
        setup = ask_setup(preferences, self.store.path, draft.remote_endpoints)
        result = check(draft, (self.s,), (), (), remote_setup=setup)
        self.assertTrue(result.can_save, result.checks)
        self.assertIn("this remote does not encrypt", result.checks[0].text)
        self.assertEqual(endpoint_line(target, (), setup)[:2], (target.describe(), "remote"))

    def test_an_encrypted_remote_has_no_notice_and_its_record_says_so(self):
        job, _root = self.job_onto("crypt", "Sealed")
        self.seed(job, {"a.txt": "a"})
        (record,) = read_records(self.layout, job.job_id)
        self.assertIs(record.encrypted, True)
        (row,) = build_rows(self.store.load().contents, (self.s,), {}, {job.job_id: (record,)}, ())
        self.assertNotIn("not encrypted", row.destinations[0].folder)

    def test_a_first_run_then_a_run_read_back_in_the_list_and_the_details(self):
        job, root = self.job_onto("s3", "Listed")
        write(self.vol_s / "Listed", "first.txt", "first")
        summary = summarise(self.preview(job))
        pair = summary.pairs[0]
        self.assertEqual(pair.title, f"Destination 1 - {root.fs} (remote)")
        self.assertEqual(pair.state, PairState.FIRST_RUN)
        self.assertTrue(any("does not encrypt" in n for n in pair.notices), pair.notices)
        self.assertTrue(summary.reading.startswith("The run reads the source"), summary.reading)
        self.assertIn(("Free space", "the service reports none, so it is not checked"),
                      [(q.label, q.detail) for q in pair.quantities])
        self.assertIn("a stated default", pair.time)
        self.seed(job, {"a.txt": "a", "b.txt": "bb"})
        write(self.vol_s / "Listed", "a.txt", "changed")
        self.only(self.run_now(job))
        records = tuple(read_records(self.layout, job.job_id))
        self.assertIs(records[-1].encrypted, False)
        (row,) = build_rows(self.store.load().contents, (self.s,), {}, {job.job_id: records}, ())
        destination = row.destinations[0]
        self.assertEqual(destination.status, "Completed")
        self.assertEqual(destination.folder, f"{root.fs} (remote, not encrypted)")
        self.assertTrue(destination.can_verify)
        details = record_details(records[-1], destination.root, destination.name)
        self.assertIsInstance(details.versions_path, RemotePlace)
        self.assertTrue(details.versions_path.address.startswith(f"{root.fs}/.mirrorhold/versions/"),
                        details.versions_path)

    def test_a_remote_not_reached_says_so_in_the_preview_and_the_list(self):
        conf = self.base / "down.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(f"[dav]\ntype = webdav\nurl = http://127.0.0.1:{closed_port()}/\nvendor = rclone\n",
                        encoding="utf-8")
        (self.vol_s / "Away").mkdir()
        job = new_job("Away", endpoint(self.s, "Away"), [RemoteEndpoint("dav", "Work")])
        self.save(job, settings=RemoteSettings(str(conf)))
        summary = summarise(self.preview(job))
        pair = summary.pairs[0]
        self.assertEqual(pair.state, PairState.ABSENT)
        self.assertTrue(pair.headline.startswith("Could not be reached - the next run tries again ("), pair.headline)
        self.assertEqual(summary.actions.blocked,
                         "Nothing can run now: no destination's drive is attached or its remote reached.")
        self.assertEqual(self.only(self.run_now(job)).outcome, Outcome.ABSENT)
        records = tuple(read_records(self.layout, job.job_id))
        (row,) = build_rows(self.store.load().contents, (self.s,), {}, {job.job_id: records}, ())
        self.assertEqual((row.status, row.destinations[0].status), ("Waits for its remote", "Waits for its remote"))

    def test_a_remotes_folders_are_browsed_through_one_session(self):
        job, root = self.job_onto("local", "Browsed")
        self.seed(job, {"sub\\a.txt": "a", "other\\b.txt": "bb", "c.txt": "ccc"})
        preferences = self.store.load().contents.preferences
        access = RemoteAccess(preferences, self.store.path)
        self.addCleanup(access.close)
        self.assertIn(root.remote, access.open())
        self.assertEqual(access.folders(RemoteEndpoint(root.remote, root.folder)), (".mirrorhold", "other", "sub"))
        self.assertEqual(access.folders(RemoteEndpoint(root.remote, child_folder(root.folder, "sub"))), ())
        self.assertIsNone(access.folders(RemoteEndpoint(root.remote, child_folder(root.folder, "none"))))


@needs_engine
class ConfigurationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "Config" / "mirrorhold.json"
        self.conf = self.tmp / "throwaway.conf"
        self.conf.write_text("[loc]\ntype = local\n\n[other]\ntype = local\n", encoding="utf-8")
        self.preferences = Preferences()

    def encrypt(self):
        script = self.tmp / "unlock.py"
        script.write_text(f"import sys\nsys.stdout.write({CONFIG_PASS!r} + '\\n')\n", encoding="utf-8")
        result = run_engine(ENGINE, ["config", "encryption", "set"],
                            configuration=Configuration(self.conf, (sys.executable, str(script))), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_check_names_the_remotes_and_never_needs_a_service(self):
        self.assertEqual(check_configuration(self.preferences, self.store, RemoteSettings(str(self.conf))),
                         (True, "Remotes in it: loc, other"))

    def test_a_locked_configuration_says_why_and_a_password_is_remembered_only_when_it_opens_it(self):
        self.encrypt()
        settings = RemoteSettings(str(self.conf), Unlock.REMEMBERED)
        usable, text = check_configuration(self.preferences, self.store, RemoteSettings(str(self.conf)))
        self.assertFalse(usable)
        self.assertIn("cannot be opened", text)
        path = runtime_layout(self.store).remote_password
        path.parent.mkdir(parents=True, exist_ok=True)
        wrong = remember(self.preferences, self.store, settings, "not the password")
        self.assertIn("could not be opened with this password, so it was not remembered", wrong)
        self.assertFalse(path.exists())
        self.assertEqual(remember(self.preferences, self.store, settings, CONFIG_PASS), "")
        self.assertTrue(path.exists())
        self.assertEqual(check_configuration(self.preferences, self.store, settings),
                         (True, "Remotes in it: loc, other"))
        self.assertEqual(remotes_draft(settings).unlock, Unlock.REMEMBERED)
        self.assertNotIn(CONFIG_PASS.encode(), path.read_bytes())

    def test_a_configuration_that_opens_without_one_has_no_password_to_remember(self):
        settings = RemoteSettings(str(self.conf), Unlock.REMEMBERED)
        self.assertIn("is not encrypted, so there is no password to remember",
                      remember(self.preferences, self.store, settings, "invented"))
        self.assertFalse(runtime_layout(self.store).remote_password.exists())


class RecordSchemaTest(unittest.TestCase):
    def test_a_record_says_whether_its_remote_encrypts_and_older_ones_say_nothing(self):
        cleaned = VersionsCleaned(30, ("2026-06-01T10-00-00Z",), 1, 2, 300, (), True)
        record = RunRecord("r" * 32, "j" * 32, "Job", "m" * 32, 1, "manual", "2026-10-06T10:00:00Z",
                           "2026-10-06T10:05:00Z", Outcome.COMPLETED, planned=dict.fromkeys(PLANNED_KEYS, 0),
                           done=dict.fromkeys(DONE_KEYS, 0), left=dict.fromkeys(LEFT_KEYS, 0),
                           versions_cleaned=cleaned, encrypted=False)
        doc = record.to_document()
        self.assertEqual((doc["schema_version"], doc["encrypted"]), (RECORD_SCHEMA, False))
        self.assertGreaterEqual(RECORD_SCHEMA, 6, "the schema that added it and every later one say it")
        self.assertEqual(RunRecord.from_document(doc), record)
        self.assertIsNone(replace(record, encrypted=None).to_document()["encrypted"])
        self.assertTrue(RunRecord.from_document(doc).versions_cleaned.trash)
        with self.assertRaises(ContractError):
            RunRecord.from_document({**doc, "encrypted": "yes"})
        older = {key: value for key, value in doc.items() if key != "encrypted"}
        with self.assertRaises(ContractError):
            RunRecord.from_document(older)                                  # the current schema says it
        read = RunRecord.from_document({**older, "schema_version": 5})
        self.assertEqual((read.encrypted, read.versions_cleaned.trash), (None, True))
        before = {**older, "schema_version": 4,
                  "versions_cleaned": {k: v for k, v in doc["versions_cleaned"].items() if k != "trash"}}
        self.assertFalse(RunRecord.from_document(before).versions_cleaned.trash)
        with self.assertRaises(ContractError):
            RunRecord.from_document({**doc, "schema_version": RECORD_SCHEMA + 1})


if __name__ == "__main__":
    unittest.main()
