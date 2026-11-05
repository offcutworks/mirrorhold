"""Verification and repair of a remote destination, end to end on the pinned engine.

Remotes are the engine's own S3 and WebDAV servers on the loopback address, a local folder
named as a remote, an encrypted remote over another local folder and one over the WebDAV
server, named in a throwaway configuration whose secrets are invented
(``test_engine_contract.Loopback``); a remote whose server does not run completes the set.
Nothing reaches the internet and the user's configuration is never named.

Covered: a verification by the fingerprints a service keeps (S3's MD5, a local remote's),
finding a difference planted at the source or on the remote without reading a copy there;
a verification of new copies building on it; a remote that keeps no fingerprint read on both
sides; an encrypted remote compared by the engine's check of the encrypted form, and one over
a service that keeps no fingerprint read instead; a repair on a remote, its new copies read
back by the same measure; the command line's words; a remote that cannot be reached, and one
that stops answering as its marker or a shared destination's record is read, told from a
record it will not serve while it answers. Without
the engine: the verification record's schema 3, the throughputs by measure, the estimate's
rates for a remote read by download, the choice of a fingerprint kind, and the reminder a
run's notice gives for a remote destination."""
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import endpoint, volume
from test_engine_contract import obscured
from test_remote_runs import RemoteRunCase, closed_port, unanswered_at, write

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.marker import MARKER_RELATIVE
from mirrorhold.endpoints.shared import SHARED_RELATIVE
from mirrorhold.engine.rclone import RemoteRoot
from mirrorhold.jobs.model import Notifications, RemoteSettings, new_job
from mirrorhold.locations import runtime_layout
from mirrorhold.maintenance import compare
from mirrorhold.maintenance.repair import LeftReason, RepairState, left_words
from mirrorhold.maintenance.repairer import RepairError, repair_destination
from mirrorhold.maintenance.verification import (VERIFICATION_SCHEMA, Measure, VerificationMode, VerificationRecord,
                                                 VerificationState, verification_throughputs)
from mirrorhold.maintenance.verifier import VerifyError, _Verifier, verify_destination
from mirrorhold.notifications import announce_run
from mirrorhold.records.record import DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, Outcome, RunRecord
from mirrorhold.snapshots.provider import UnavailableProvider
from mirrorhold.storage.remote import RemoteStorage

NO_SNAPSHOTS = UnavailableProvider("snapshots are not made in this test")


def plant(path: Path, data: str) -> None:
    """Give a file other content of the same length, its modification time kept: what no
    run copies again and only a verification finds."""
    info = path.stat()
    assert len(data.encode("utf-8")) == info.st_size, "a planted difference keeps the size"
    path.write_text(data, encoding="utf-8")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


class RemoteMaintenanceCase(RemoteRunCase):
    """``RemoteRunCase`` with an encrypted remote over the WebDAV server added to the
    throwaway configuration: a service beneath that keeps no fingerprint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(cls.loopback.path, "a", encoding="utf-8") as file:
            file.write(f"\n[encdav]\ntype = crypt\nremote = dav:cryptunder\n"
                       f"password = {obscured('loopback-encdav-pass')}\n")

    def job_onto_remote(self, remote: str, folder: str = "Work"):
        """A job onto ``remote:<case>-<folder>``, a remote this class adds."""
        (self.vol_s / folder).mkdir(exist_ok=True)
        root = RemoteRoot(remote, f"{self.case}-{folder}")
        job = new_job(folder, endpoint(self.s, folder), [RemoteEndpoint(root.remote, root.folder)])
        self.save(job)
        return job, root

    def seeded(self, backend: str, files=None):
        job, root = self.job_onto(backend) if backend in ("s3", "webdav", "sftp", "local", "crypt") \
            else self.job_onto_remote(backend)
        self.seed(job, files or {"a.txt": "alpha", "sub\\b.txt": "bravo two", "c.txt": "charlie"})
        return job, root

    def verify(self, job, **options):
        said = []
        record = verify_destination(self.store, job.job_id, 1, volume_source=self.volumes, say=said.append,
                                    **options)
        self.said = said
        return record

    def verify_unread(self, job, **options):
        """A verification during which no copy on the remote may be opened for reading."""
        with mock.patch.object(RemoteStorage, "open_file", side_effect=AssertionError("a copy was downloaded")) \
                as opened:
            record = self.verify(job, **options)
        self.assertEqual(opened.call_count, 0)
        return record

    def repair(self, job):
        said = []
        record = repair_destination(self.store, job.job_id, 1, volume_source=self.volumes, say=said.append,
                                    snapshots=NO_SNAPSHOTS)
        self.said = said
        return record


class FingerprintTest(RemoteMaintenanceCase):
    def test_s3_is_verified_by_md5_without_a_download_and_a_difference_at_the_source_is_found(self):
        job, root = self.seeded("s3")
        record = self.verify_unread(job)
        self.assertEqual((record.state, record.measure, record.fingerprint), (VerificationState.COMPLETED,
                                                                              Measure.FINGERPRINT, "md5"))
        self.assertEqual((record.compared_files, record.differ_total, record.read_files, record.unreadable_total),
                         (3, 0, 0, 0))
        self.assertIn(f"verifying destination 1 of 'Work' ({root.fs}) by the md5 fingerprints its service keeps; "
                      "nothing is downloaded", self.said)
        self.assertIn("; compared by md5 fingerprint", record.describe())
        plant(self.vol_s / "Work" / "a.txt", "ALPHA")
        record = self.verify_unread(job, restart=True)
        self.assertEqual((record.differ_total, record.differ), (1, [("a.txt", 5)]))
        self.assertIn("differs: a.txt", self.said)

    def test_a_difference_planted_on_a_local_remote_is_found_without_reading_the_copy_there(self):
        job, root = self.seeded("local")
        plant(Path(root.folder) / "sub" / "b.txt", "BRAVO TWO")
        record = self.verify_unread(job)
        self.assertEqual((record.measure, record.fingerprint, record.differ), (Measure.FINGERPRINT, "md5",
                                                                               [("sub\\b.txt", 9)]))

    def test_a_pass_of_new_copies_builds_on_a_full_pass_and_reads_only_what_is_new(self):
        job, _ = self.seeded("s3")
        self.assertEqual(self.verify_unread(job).compared_files, 3)
        write(self.vol_s / "Work", "d.txt", "delta")
        self.only(self.run_now(job))
        record = self.verify_unread(job, mode=VerificationMode.NEW_COPIES)
        self.assertEqual((record.mode, record.compared_files, record.unchanged_files, record.differ_total),
                         (VerificationMode.NEW_COPIES, 1, 3, 0))

    def test_a_repair_replaces_a_differing_copy_and_reads_it_back_by_its_fingerprint(self):
        job, root = self.seeded("s3")
        plant(self.vol_s / "Work" / "c.txt", "CHARLIE")
        self.assertEqual(self.verify_unread(job).differ_total, 1)
        with mock.patch.object(RemoteStorage, "open_file", side_effect=AssertionError("a copy was downloaded")):
            record = self.repair(job)
        self.assertEqual((record.state, record.replaced, record.left), (RepairState.COMPLETED, [("c.txt", 7)], []))
        self.assertIn("replaced: c.txt", self.said)
        self.assertEqual(self.files(root, record.versions_batch), {"c.txt": 7})
        self.assertEqual(self.verify_unread(job, restart=True).differ_total, 0)


class ReadTest(RemoteMaintenanceCase):
    def test_a_remote_keeping_no_fingerprint_is_read_on_both_sides(self):
        job, root = self.seeded("webdav")
        plant(self.vol_s / "Work" / "a.txt", "ALPHA")
        record = self.verify(job)
        self.assertEqual((record.measure, record.fingerprint, record.read_files), (Measure.READ, None, 0))
        self.assertEqual((record.compared_files, record.differ), (3, [("a.txt", 5)]))
        self.assertIn(f"verifying destination 1 of 'Work' ({root.fs}): its service keeps no fingerprint, so both "
                      "copies of every file are read, the destination's by download", self.said)
        self.assertNotIn("compared by", record.describe())
        record = self.repair(job)
        self.assertEqual(record.replaced, [("a.txt", 5)])

    def test_a_copy_listed_with_no_fingerprint_is_read_on_both_sides_and_counted(self):
        job, _ = self.seeded("s3")
        listed = RemoteStorage.list_tree

        def without_one(storage, **options):
            listing = listed(storage, **options)
            listing.fingerprints.pop("a.txt", None)
            return listing

        with mock.patch.object(RemoteStorage, "list_tree", without_one):
            record = self.verify(job)
        self.assertEqual((record.measure, record.compared_files, record.read_files, record.read_bytes),
                         (Measure.FINGERPRINT, 3, 1, 5))
        self.assertIn("; 1 file read in full (5 bytes): the service kept no fingerprint for it", record.describe())


class EncryptedTest(RemoteMaintenanceCase):
    def test_an_encrypted_remote_is_compared_by_its_encrypted_form_and_repaired(self):
        job, root = self.seeded("crypt")
        record = self.verify_unread(job)
        self.assertEqual((record.state, record.measure, record.fingerprint), (VerificationState.COMPLETED,
                                                                              Measure.ENCRYPTED, "md5"))
        self.assertEqual((record.compared_files, record.differ_total), (3, 0))
        self.assertIn(f"verifying destination 1 of 'Work' ({root.fs}) by its encrypted form against the md5 "
                      "fingerprints the service beneath keeps; only each file's first bytes are read there",
                      self.said)
        self.assertIn("; compared by the encrypted form (md5)", record.describe())
        plant(self.vol_s / "Work" / "sub" / "b.txt", "BRAVO TWO")
        record = self.verify_unread(job, restart=True)
        self.assertEqual(record.differ, [("sub\\b.txt", 9)])
        record = self.repair(job)
        self.assertEqual((record.state, record.replaced), (RepairState.COMPLETED, [("sub\\b.txt", 9)]))
        self.assertEqual(self.verify_unread(job, restart=True).differ_total, 0)

    def test_an_encrypted_remote_over_a_service_keeping_no_fingerprint_is_read(self):
        job, _ = self.seeded("encdav")
        record = self.verify(job)
        self.assertEqual((record.state, record.measure, record.compared_files, record.differ_total),
                         (VerificationState.COMPLETED, Measure.READ, 3, 0))

    def test_many_files_are_checked_in_batches_the_cursor_passing_each_batch_whole(self):
        files = {f"f{n:03}.txt": f"file {n:03}" for n in range(70)}
        job, _ = self.seeded("crypt", files)
        handled = []
        with mock.patch("mirrorhold.maintenance.verifier.CHECK_FILES", 32):
            record = verify_destination(self.store, job.job_id, 1, volume_source=self.volumes,
                                        after_item=handled.append)
        self.assertEqual((record.compared_files, record.differ_total, record.cursor), (70, 0, "f069.txt"))
        self.assertEqual(handled, sorted(files))
        handled.clear()
        with mock.patch("mirrorhold.maintenance.verifier.CHECK_ENTRIES", 8):
            record = verify_destination(self.store, job.job_id, 1, volume_source=self.volumes,
                                        mode=VerificationMode.NEW_COPIES, after_item=handled.append)
        self.assertEqual((record.unchanged_files, record.compared_files, record.cursor), (70, 0, "f069.txt"))
        self.assertEqual(handled, sorted(files))


class CommandLineTest(RemoteMaintenanceCase):
    def test_verify_and_repair_reach_a_remote_and_say_how_it_was_compared(self):
        job, root = self.seeded("s3")
        plant(self.vol_s / "Work" / "a.txt", "ALPHA")
        code, out, err = self.cli("verify", "Work")
        self.assertIn(f"verifying destination 1 of 'Work' ({root.fs}) by the md5 fingerprints its service keeps; "
                      "nothing is downloaded", out)
        self.assertIn("1 differs; compared by md5 fingerprint", out)
        code, out, err = self.cli("repair", "Work")
        self.assertIn("replaced: a.txt", out, out + err)
        code, out, err = self.cli("history", "Work")
        self.assertIn("compared by md5 fingerprint", out)

    def test_a_remote_that_cannot_be_reached_is_not_verified_and_says_why(self):
        configuration = self.base / "gone.conf"
        configuration.write_text(f"[gone]\ntype = webdav\nurl = http://127.0.0.1:{closed_port()}/\nvendor = rclone\n",
                                 encoding="utf-8")
        (self.vol_s / "Work").mkdir()
        job = new_job("Work", endpoint(self.s, "Work"), [RemoteEndpoint("gone", "Work")])
        self.save(job, settings=RemoteSettings(str(configuration)))
        with self.assertRaises(VerifyError) as raised:
            self.verify(job)
        self.assertTrue(str(raised.exception).startswith("gone:Work cannot be reached: "), raised.exception)
        self.assertTrue(str(raised.exception).endswith("; verify again later"), raised.exception)


class MarkerUnansweredTest(RemoteMaintenanceCase):
    """A remote that stops answering as its marker is read ends a verification and a repair as
    the remote stopping, never as a marker that does not match."""

    def unanswered(self):
        return mock.patch.multiple(RemoteStorage, read_file=unanswered_at(MARKER_RELATIVE, RemoteStorage.read_file),
                                   entry=unanswered_at(MARKER_RELATIVE, RemoteStorage.entry))

    def test_a_verification_ends_as_the_remote_stopping(self):
        job, _root = self.seeded("local")
        with self.unanswered(), self.assertRaises(VerifyError) as raised:
            self.verify(job)
        self.assertEqual(str(raised.exception), "the remote stopped answering: the sync engine did not answer: "
                                                "invented; the next verification continues from here")

    def test_a_repair_ends_as_the_remote_stopping(self):
        job, _root = self.seeded("local")
        plant(self.vol_s / "Work" / "c.txt", "CHARLIE")
        self.assertEqual(self.verify(job).differ_total, 1)
        with self.unanswered(), self.assertRaises(RepairError) as raised:
            self.repair(job)
        self.assertEqual(str(raised.exception), "the remote stopped answering: the sync engine did not answer: "
                                                "invented")


class SharedRecordUnansweredTest(RemoteMaintenanceCase):
    """A remote that stops answering as a shared destination's record is read ends a
    verification and a repair as the remote stopping; a record it will not serve while it
    answers is one that cannot be used."""

    def shared(self):
        """A job whose first run left what its remote folder held there, its copies verified
        once and one of them since found to differ."""
        job, root = self.job_onto("local")
        self.session.write(root, "theirs.txt", b"not the job's")
        write(self.vol_s / "Work", "a.txt", "alpha")
        write(self.vol_s / "Work", "c.txt", "charlie")
        ticket = self.preview(job).ticket
        self.assertEqual(self.only(self.run_now(job, ticket_id=ticket.ticket_id)).outcome, Outcome.COMPLETED)
        plant(self.vol_s / "Work" / "c.txt", "CHARLIE")
        self.assertEqual(self.verify(job).differ_total, 1)
        return job

    def unanswered(self, *, answers: bool):
        operations = {"read_file": unanswered_at(SHARED_RELATIVE, RemoteStorage.read_file)}
        if not answers:
            operations["entry"] = unanswered_at(SHARED_RELATIVE, RemoteStorage.entry)
        return mock.patch.multiple(RemoteStorage, **operations)

    def test_a_verification_ends_as_the_remote_stopping(self):
        job = self.shared()
        with self.unanswered(answers=False), self.assertRaises(VerifyError) as raised:
            self.verify(job)
        self.assertEqual(str(raised.exception), "the remote stopped answering: the sync engine did not answer: "
                                                "invented; the next verification continues from here")

    def test_a_repair_ends_as_the_remote_stopping(self):
        job = self.shared()
        with self.unanswered(answers=False), self.assertRaises(RepairError) as raised:
            self.repair(job)
        self.assertEqual(str(raised.exception), "the remote stopped answering: the sync engine did not answer: "
                                                "invented")

    def test_a_record_the_remote_will_not_serve_while_it_answers_cannot_be_used(self):
        job = self.shared()
        unusable = "the destination's record of what to leave where it is cannot be used: "
        with self.unanswered(answers=True), self.assertRaises(VerifyError) as verified:
            self.verify(job)
        with self.unanswered(answers=True), self.assertRaises(RepairError) as repaired:
            self.repair(job)
        self.assertTrue(str(verified.exception).startswith(unusable), verified.exception)
        self.assertTrue(str(repaired.exception).startswith(unusable), repaired.exception)


class EngineFingerprintTest(RemoteMaintenanceCase):
    def test_the_engine_computes_a_kind_this_tool_does_not_as_a_local_remote_keeps_it(self):
        job, root = self.seeded("local")
        listed = {e.path: e.hashes["quickxor"] for e in
                  self.session.list_tree(RemoteRoot(root.remote, root.folder), kinds=("quickxor",)) if not e.is_dir}
        computed = self.session.local_fingerprint(self.vol_s / "Work" / "sub" / "b.txt", "quickxor")
        self.assertEqual(computed, listed["sub\\b.txt"].lower())
        self.assertIsNone(compare.hasher("quickxor"))


def _record(**fields) -> VerificationRecord:
    return VerificationRecord("v" * 32, "j" * 32, "Job", "m" * 32, 1, "2026-10-29T10:00:00Z", "2026-10-29T11:00:00Z",
                              VerificationState.COMPLETED, compared_files=10, compared_bytes=100 * 2 ** 20,
                              seconds=10.0, **fields)


class WithoutEngineTest(unittest.TestCase):
    def test_schema_3_keeps_how_a_pass_compared_and_older_records_read_as_read_in_full(self):
        record = _record(measure=Measure.FINGERPRINT, fingerprint="md5", read_files=2, read_bytes=20)
        doc = record.to_document()
        self.assertEqual(doc["schema_version"], VERIFICATION_SCHEMA)
        self.assertGreaterEqual(VERIFICATION_SCHEMA, 3, "schema 3 and every later one keep how a pass compared")
        self.assertEqual(VerificationRecord.from_document(doc), record)
        older = {k: v for k, v in doc.items() if k not in ("measure", "fingerprint", "read_files", "read_bytes")}
        older["schema_version"] = 2
        read = VerificationRecord.from_document(older)
        self.assertEqual((read.measure, read.fingerprint, read.read_files), (Measure.READ, None, 0))
        from mirrorhold.contract_errors import ContractError
        with self.assertRaises(ContractError):
            VerificationRecord.from_document({**doc, "measure": "guessed"})
        with self.assertRaises(ContractError):
            VerificationRecord.from_document({**older, "schema_version": 3})

    def test_throughputs_are_those_of_passes_compared_by_the_same_measure(self):
        records = [_record(), _record(measure=Measure.FINGERPRINT, fingerprint="md5")]
        self.assertEqual(len(verification_throughputs(records, "m" * 32)), 1)
        self.assertEqual(len(verification_throughputs(records, "m" * 32, Measure.FINGERPRINT)), 1)
        self.assertEqual(verification_throughputs(records, "m" * 32, Measure.ENCRYPTED), [])

    def test_a_remote_read_by_download_with_no_earlier_pass_takes_the_rate_its_runs_copied_at(self):
        s = volume(1, label="S")
        remote = new_job("Work", endpoint(s, "Work"), [RemoteEndpoint("cloud", "Backups")])
        drive = new_job("Work", endpoint(s, "Work"), [endpoint(volume(2, label="D"), "Backup")])
        run = RunRecord("r" * 32, remote.job_id, "Work", remote.destinations[0].marker_id, 1, "manual",
                        "2026-10-06T10:00:00Z", "2026-10-06T10:05:00Z", Outcome.COMPLETED,
                        planned=dict.fromkeys(PLANNED_KEYS, 0), done=dict.fromkeys(DONE_KEYS, 0),
                        left=dict.fromkeys(LEFT_KEYS, 0))
        layout = runtime_layout(Path(self.id().replace(".", "_")) / "mirrorhold.json")
        for job, measure, expected in ((remote, Measure.READ, [5.0]), (remote, Measure.FINGERPRINT, []),
                                       (drive, Measure.READ, [])):
            seat = SimpleNamespace(layout=layout, job=job, index=1, marker_id=job.destinations[0].marker_id,
                                   record=SimpleNamespace(measure=measure))
            with mock.patch("mirrorhold.maintenance.verifier.read_verifications", return_value=[]), \
                    mock.patch("mirrorhold.maintenance.verifier.read_records", return_value=[run]), \
                    mock.patch("mirrorhold.maintenance.verifier.throughputs", return_value=[5.0]):
                self.assertEqual(_Verifier._rates(seat), expected)

    def test_the_fingerprint_kind_prefers_one_computed_here(self):
        kinds = lambda *found: SimpleNamespace(fingerprint_kinds=found)
        self.assertEqual(compare.fingerprint_kind(kinds("quickxor", "sha1", "md5")), "md5")
        self.assertEqual(compare.fingerprint_kind(kinds("quickxor")), "quickxor")
        self.assertIsNone(compare.fingerprint_kind(kinds()))

    def test_a_remote_destination_is_reminded_of_a_full_verification_as_a_drive_is(self):
        s = volume(1, label="S")
        job = new_job("Work", endpoint(s, "Work"), [RemoteEndpoint("cloud", "Backups")])
        record = RunRecord("r" * 32, job.job_id, "Work", job.destinations[0].marker_id, 1, "manual",
                           "2025-01-06T10:00:00Z", "2025-01-06T10:05:00Z", Outcome.COMPLETED,
                           planned=dict.fromkeys(PLANNED_KEYS, 0), done=dict.fromkeys(DONE_KEYS, 0),
                           left=dict.fromkeys(LEFT_KEYS, 0))
        shown = []
        layout = runtime_layout(Path(self.id().replace(".", "_")) / "mirrorhold.json")
        with mock.patch("mirrorhold.maintenance.verification.read_records", return_value=[record]), \
                mock.patch("mirrorhold.maintenance.verification.read_verifications", return_value=[]):
            announce_run(layout, job, [record], Notifications(on_completion=True), SimpleNamespace(show=shown.append))
        self.assertEqual(shown[0].lines[-1], "Destination 1: a full verification is due (last: never)")

    def test_the_check_of_an_encrypted_remote_is_read_alike_by_a_verification_and_a_repair(self):
        from mirrorhold.engine.rclone import Checked, CryptCheckReport
        here = Path(__file__)
        report = CryptCheckReport(1, {"a": Checked.SAME, "b": Checked.DIFFERS, "c": Checked.MISSING,
                                      "d": Checked.ERROR}, {"d": "failed to read nonce"})
        self.assertEqual(compare.encrypted_outcome(report, "a", here), (True, ""))
        self.assertEqual(compare.encrypted_outcome(report, "b", here), (False, ""))
        self.assertEqual(compare.encrypted_outcome(report, "c", here), (None, "the destination copy is no longer there"))
        self.assertEqual(compare.encrypted_outcome(report, "d", here),
                         (None, "the engine could not check it: failed to read nonce"))
        self.assertEqual(compare.encrypted_outcome(report, "e", here.with_name("no-such-file")),
                         (None, "the source copy could not be read: it is no longer there"))

    def test_a_copy_that_still_differs_on_a_remote_is_said_in_a_remotes_words(self):
        self.assertEqual(left_words(LeftReason.STILL_DIFFERS, remote=True),
                         "still differs after it was replaced: the service did not keep it as it was sent")
        self.assertTrue(left_words(LeftReason.STILL_DIFFERS).endswith("the destination drive may be failing"))


if __name__ == "__main__":
    unittest.main()
