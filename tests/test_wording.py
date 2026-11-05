"""How a count is written with what it counts (``mirrorhold.wording``), and the lines that
count one of a thing in the singular through it: a run's record, a verification's record
and the window's words."""
from __future__ import annotations

import unittest

from support import endpoint, volume

from mirrorhold.jobs.model import new_job
from mirrorhold.maintenance.verification import VerificationRecord, VerificationState
from mirrorhold.records.record import DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, Outcome, RunRecord, VersionsCleaned
from mirrorhold.records.status import ObservedRun
from mirrorhold.wording import agreeing, counted, number
from mirrorhold_gui.runs_model import run_panel


class CountedTest(unittest.TestCase):
    def test_one_is_singular_and_every_other_count_plural(self):
        self.assertEqual([counted(n, "file") for n in (0, 1, 2)], ["0 files", "1 file", "2 files"])
        self.assertEqual([counted(n, "batch", "batches") for n in (1, 3)], ["1 batch", "3 batches"])
        self.assertEqual([counted(n, "entry", "entries") for n in (1, 2)], ["1 entry", "2 entries"])

    def test_the_window_separates_thousands_and_the_command_line_does_not(self):
        self.assertEqual((counted(2104331, "file"), counted(2104331, "file", separated=True)),
                         ("2104331 files", "2,104,331 files"))
        self.assertEqual((number(1052), number(1052, separated=True)), ("1052", "1,052"))

    def test_a_word_agrees_with_the_number_of_its_count(self):
        self.assertEqual([agreeing(n, "differs", "differ") for n in (0, 1, 2)], ["differ", "differs", "differ"])


class CountsOfOneTest(unittest.TestCase):
    """Lines that wrote a count of one in the plural."""

    def setUp(self):
        self.job = new_job("Photos", endpoint(volume(1, "S"), "P"), [endpoint(volume(2, "F"), "M")])

    def record(self, **fields) -> RunRecord:
        left = dict.fromkeys(LEFT_KEYS, 1)
        return RunRecord("r" * 32, self.job.job_id, self.job.name, self.job.destinations[0].marker_id, 1, "manual",
                         "2026-10-21T10:00:00Z", "2026-10-21T10:05:00Z", Outcome.INTERRUPTED, ("process-ended",), "",
                         "1.0.0", dict.fromkeys(PLANNED_KEYS, 1), dict.fromkeys(DONE_KEYS, 0), left, None, (), 0,
                         **fields)

    def test_a_runs_line_counts_what_it_left_and_its_cleanup_in_the_number_each_needs(self):
        cleaned = VersionsCleaned(1, ("2026-09-01",), 1, 1, 1)
        line = self.record(versions_cleaned=cleaned).describe()
        self.assertIn("; left: 1 file (1 byte) to copy, 1 removal, 1 rename", line)
        self.assertIn("; 1 versions batch older than 1 day deleted (1 file, 1 byte)", line)

    def test_a_verifications_line_counts_one_file_of_one(self):
        record = VerificationRecord("v" * 32, self.job.job_id, self.job.name, self.job.destinations[0].marker_id, 1,
                                    "2026-10-21T10:00:00Z", "2026-10-21T10:01:00Z", VerificationState.COMPLETED,
                                    files_total=1, bytes_total=1, compared_files=1, compared_bytes=1)
        self.assertIn(": 1 of 1 file compared (1 of 1 byte), 0 differ", record.describe())

    def test_the_runs_tab_counts_one_entry_listed(self):
        status = {"run_id": "a" * 32, "job_id": self.job.job_id, "job_name": "Photos", "phase": "listing",
                  "pair_index": 0, "listed": 1}
        self.assertEqual(run_panel(ObservedRun(status, True)).phase, "listing the source   1 entry")


if __name__ == "__main__":
    unittest.main()
