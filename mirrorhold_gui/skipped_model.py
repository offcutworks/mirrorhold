"""The files-not-backed-up report of one destination, from its last run that read the source. No toolkit.

Lists every file that run left out which a later run could still back up — a file that
could not be read, listed or copied, a rename or folder that could not be made — with
why, and how many runs in a row it has gone unbacked-up, most first. Cloud-only files
and links are left out by design and are not listed here; the run's details list them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from mirrorhold.records.record import ESCALATE_AFTER, MAX_LISTED, RunRecord
from mirrorhold.wording import agreeing, counted

from .formatting import count, files, local_time


@dataclass(frozen=True)
class UnbackedRow:
    path: str
    why: str
    runs: str


@dataclass(frozen=True)
class UnbackedReport:
    title: str
    summary: Tuple[str, ...]
    rows: Tuple[UnbackedRow, ...]
    note: str


def whole_words(row: UnbackedRow) -> str:
    """A file and why it was not backed up, whole: what the list's columns cut."""
    return f"{row.path} - {row.why}"


def unbacked_report(record: RunRecord, job_name: str) -> UnbackedReport:
    why = {path.casefold(): reason for path, reason in record.skipped}
    ordered = sorted(record.not_backed_up, key=lambda item: (-item[1], item[0].casefold()))
    rows = tuple(UnbackedRow(path, why.get(path.casefold(), "not backed up"), count(runs)) for path, runs in ordered)
    repeated = sum(1 for _path, runs in record.not_backed_up if runs >= ESCALATE_AFTER)
    summary = [f"{files(len(rows))} {agreeing(len(rows), 'was', 'were')} not backed up by the last run "
               f"({local_time(record.started)})" + (";" if repeated else ".")]
    if repeated:
        them = "it has" if len(rows) == 1 else f"{count(repeated)} of them {agreeing(repeated, 'has', 'have')}"
        summary.append(f"{them} not been backed up for {counted(ESCALATE_AFTER, 'run')} or more in a row.")
    if len(record.not_backed_up) >= MAX_LISTED:
        summary.append(f"Only the first {count(MAX_LISTED)} are listed.")
    return UnbackedReport(f"Not backed up: {record.job_name or job_name}, destination {record.destination_index}",
                          tuple(summary), rows,
                          "Cloud-only files and links are left out by design; the run's details list them.")
