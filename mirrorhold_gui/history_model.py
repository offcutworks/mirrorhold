"""The History tab and a run's details, from the run and verification records. No toolkit.

A run's details say how its source was read — from a snapshot of the drive and when it
was taken, or as it is and why — and give each skipped file a later run could back up
the number of runs in a row it has gone unbacked-up, once that is more than one; what
the job's version cleanup deleted; and the destination drive's free space when the pair
ended, "low" below the warning's share. Changed files the engine found identical count
as done in the details' table, and the summary says how many there were. Of a remote
destination they say its reason where it could not be reached, what its service may keep of
what was deleted and that the free space is the service's; its versions batch, which no
folder of this computer holds, is its address (``jobs_model.place_under``).

Full verifications are listed among the runs (``history_entries``), newest first: what
was compared in the *Copied* column, the files not compared under *Skipped*; their
details are ``verify_model``'s. So are repairs of the copies a verification found to
differ: the copies replaced and their bytes under *Copied*, the backup copies moved into
versions, the copies not replaced — failed or left as they are — under *Skipped*; their
details are ``repair_model``'s.

History shows what is selected (``selection_entries``): one job, a group's jobs, the jobs
several selected rows reach, or every job when nothing is; each row names its job, which
the tab shows as a column of its own when more than one job is shown. A start whose log
says it left no record (``records.diagnostics``) is a row of its own — *Could not
start*, or *Ended without a record* when its process ended without a word — whose details
say why (``start_details``). What History shows can be saved as a CSV file for a person
to read (``save_history``): the words as shown, each row's details' first line beside
them, UTF-8 with a byte-order mark so that a spreadsheet program reads every name right.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Collection, Mapping, Optional, Sequence, Tuple, Union

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.endpoints.volumes import free_percent, low_on_space
from mirrorhold.jobs.validation import REMOTE_UNREACHABLE
from mirrorhold.maintenance.repair import RepairRecord, unsettled
from mirrorhold.maintenance.verification import VerificationRecord
from mirrorhold.records.diagnostics import ActivityLog
from mirrorhold.records.record import (ESCALATE_AFTER, FIRST_RUN_REASON, Outcome, ReadMode, RunRecord, SourceRead,
                                       VersionsCleaned)
from mirrorhold.run.runner import unreached_reason
from mirrorhold.wording import agreeing, counted

from .formatting import count, duration, elapsed, files, local_time, parse_utc, sentence, size
from .jobs_model import Place, Root, left_no_record, live_reading_words, place_under, reason_words
from .repair_model import result_words as repair_result_words, summary as repair_summary
from .verify_model import result_words as verification_result_words, verification_summary


def source_words(read: Optional[SourceRead]) -> str:
    """'Source: read from a snapshot of D: taken 21:10:04', 'Source: read as it is - ...',
    or "" when the run did not read its source."""
    if read is None:
        return ""
    if read.mode is ReadMode.SNAPSHOT:
        taken = parse_utc(read.taken)
        when = f" taken {taken.astimezone().strftime('%H:%M:%S')}" if taken else ""
        return f"Source: read from a snapshot of {read.drive or 'its drive'}{when}"
    words = live_reading_words(read.reason)
    if read.reason == "snapshot-failed" and read.detail:
        words += f" ({read.detail})"
    return f"Source: read as it is - {words}"


def skipped_words(record: RunRecord) -> Tuple[Tuple[str, str], ...]:
    """The run's skipped files, each why followed by its runs in a row once more than one."""
    runs = {path.casefold(): n for path, n in record.not_backed_up}
    return tuple((path, why + (f"; {counted(runs[path.casefold()], 'run')} in a row"
                               if runs.get(path.casefold(), 0) >= ESCALATE_AFTER else ""))
                 for path, why in record.skipped)


def _unreached(record: RunRecord) -> bool:
    """Whether the run could not reach its remote."""
    return record.outcome is Outcome.ABSENT and REMOTE_UNREACHABLE in record.reasons


def result_words(record: RunRecord) -> str:
    if _unreached(record):
        return "Not reached"
    if record.outcome is Outcome.HELD:
        return "Waiting for first-run confirmation" if FIRST_RUN_REASON in record.reasons else "Removals held"
    return {Outcome.COMPLETED: "Completed", Outcome.CANCELLED: "Cancelled", Outcome.INTERRUPTED: "Interrupted",
            Outcome.FAILED: "Failed", Outcome.STOPPED: "Stopped", Outcome.ABSENT: "Drive not attached"}[record.outcome]


_STARTED_BY = {"manual": "by you", "unattended": "automatic", "arrival": "drive attached", "schedule": "on schedule"}


def started_by(record: RunRecord) -> str:
    return _STARTED_BY.get(record.trigger, "automatic")


@dataclass(frozen=True)
class HistoryRow:
    iid: str
    started: str
    destination: str
    how: str
    result: str
    copied: str
    into_versions: str
    renamed: str
    skipped: str
    took: str
    job: str = ""                    # the job's name
    details: str = ""                # the first line of its details


def history_rows(records: Sequence[RunRecord], names: Mapping[int, str], job: str = "") -> Tuple[HistoryRow, ...]:
    """The job's records, newest first. ``names``: a short name per destination index;
    ``job``: the job's name, else each record's own."""
    rows = []
    for position, r in enumerate(reversed(records)):
        destination = f"{r.destination_index} {names.get(r.destination_index, '')}".rstrip()
        if r.outcome in (Outcome.ABSENT, Outcome.STOPPED) and not any(r.done.values()):
            copied = into = renamed = skipped = took = "-"
        else:
            copied = f"{count(r.done['files_copied'])} / {size(r.done['bytes_copied'])}"
            into, renamed = count(r.done["moved_to_versions"]), count(r.done["renamed"])
            skipped, took = count(r.skipped_total), elapsed(r.started, r.ended)
        rows.append(HistoryRow(f"{position}:{r.run_id}:{r.destination_index}", local_time(r.started), destination,
                               started_by(r), result_words(r), copied, into, renamed, skipped, took,
                               job or r.job_name, _summary(r)))
    return tuple(rows)


def verification_row(v: VerificationRecord, names: Mapping[int, str], live: Collection[str] = (),
                     job: str = "") -> HistoryRow:
    destination = f"{v.destination_index} {names.get(v.destination_index, '')}".rstrip()
    return HistoryRow(f"v:{v.verification_id}", local_time(v.started), destination, "by you",
                      verification_result_words(v, live),
                      f"{count(v.compared_files)} compared / {size(v.compared_bytes)}", "-", "-",
                      count(v.not_compared), duration(v.seconds) if v.seconds else "-", job or v.job_name,
                      verification_summary(v, live))


def repair_row(r: RepairRecord, names: Mapping[int, str], live: Collection[str] = (), job: str = "",
               *, remote: bool = False) -> HistoryRow:
    destination = f"{r.destination_index} {names.get(r.destination_index, '')}".rstrip()
    return HistoryRow(f"r:{r.repair_id}", local_time(r.started), destination, "by you", repair_result_words(r, live),
                      f"{count(len(r.replaced))} / {size(r.bytes_copied)}", count(r.into_versions), "-",
                      count(len(r.left) + len(unsettled(r, live))), elapsed(r.started, r.ended) if r.ended else "-",
                      job or r.job_name,
                      repair_summary(r, live, remote=remote))


_LOG_STARTED_BY = {"manual": "by you", "unattended": "automatic", "arrival": "drive attached",
                   "schedule": "on schedule"}
COULD_NOT_START = "Could not start"
ENDED_WITHOUT_RECORD = "Ended without a record"


def shown_logs(logs: Sequence[ActivityLog], records: Sequence[RunRecord], verifications: Sequence[VerificationRecord],
               repairs: Sequence[RepairRecord] = ()) -> Tuple[ActivityLog, ...]:
    """The job's starts History lists on their own: each that left no record, because it
    could not start or its process ended without a word."""
    return tuple(log for log in logs
                 if (log.failed or log.vanished) and left_no_record(log, records, verifications, repairs))


@dataclass(frozen=True)
class StartDetails:
    """A start that left no record, in words."""

    title: str
    text: str
    log: Path


def start_details(log: ActivityLog, job: str) -> StartDetails:
    result = ENDED_WITHOUT_RECORD if log.vanished else COULD_NOT_START
    if log.vanished:
        text = "Its process ended without a record: a shutdown or a crash."
    else:
        what = {"verify": "The verification", "repair": "The repair",
                "automatic": "The automatic run"}.get(log.command, "The run")
        text = f"{what} could not start: {log.failure}"
    return StartDetails(f"{result} - {job}, {local_time(log.started)}", text, log.path)


def log_row(log: ActivityLog, names: Mapping[int, str], job: str) -> HistoryRow:
    destination = ", ".join(f"{i} {names.get(i, '')}".rstrip() for i in log.destinations) or "-"
    return HistoryRow(f"log:{log.path.name}", local_time(log.started), destination,
                      _LOG_STARTED_BY.get(log.trigger, "by you"),
                      ENDED_WITHOUT_RECORD if log.vanished else COULD_NOT_START, "-", "-", "-", "-", "-", job,
                      start_details(log, job).text)


Entry = Union[RunRecord, VerificationRecord, RepairRecord, ActivityLog]


def history_entries(records: Sequence[RunRecord], verifications: Sequence[VerificationRecord],
                    names: Mapping[int, str], live: Collection[str] = (), *, job: str = "",
                    logs: Sequence[ActivityLog] = (), repairs: Sequence[RepairRecord] = (),
                    remotes: Collection[str] = ()) -> Tuple[Tuple[HistoryRow, Entry], ...]:
    """Runs, verifications, repairs and the starts that left no record together, newest
    first, each row with what it shows. ``live``: the identifiers of the activities in
    progress; ``logs``: the job's logs, read back; ``remotes``: the markers of the job's
    remote destinations."""
    rows = list(zip(history_rows(records, names, job), reversed(records)))
    rows += [(verification_row(v, names, live, job), v) for v in verifications]
    rows += [(repair_row(r, names, live, job, remote=r.marker_id in remotes), r) for r in repairs]
    rows += [(log_row(log, names, job), log) for log in shown_logs(logs, records, verifications, repairs)]
    return tuple(sorted(rows, key=lambda item: item[1].started, reverse=True))


@dataclass(frozen=True)
class JobHistory:
    """One job's part of what History shows."""

    job_id: str
    name: str
    records: Tuple[RunRecord, ...] = ()
    verifications: Tuple[VerificationRecord, ...] = ()
    names: Optional[Mapping[int, str]] = None  # a short name per destination index
    logs: Tuple[ActivityLog, ...] = ()
    repairs: Tuple[RepairRecord, ...] = ()
    remotes: Tuple[str, ...] = ()              # the markers of its remote destinations


def selection_entries(histories: Sequence[JobHistory],
                      live: Collection[str] = ()) -> Tuple[Tuple[HistoryRow, Entry], ...]:
    """Every row of the jobs History shows, newest first."""
    rows = []
    for h in histories:
        rows += history_entries(h.records, h.verifications, h.names or {}, live, job=h.name, logs=h.logs,
                                repairs=h.repairs, remotes=h.remotes)
    return tuple(sorted(rows, key=lambda item: item[1].started, reverse=True))


def empty_words(jobs: Sequence[str], *, group: Optional[str] = None, every: bool = False) -> str:
    """What History says while it shows no row: of every job, a group's, one job's
    (``jobs``: their names) or the selection's."""
    if every:
        return "No run is recorded yet."
    if group is not None:
        return f'No run of the jobs in "{group}" is recorded yet.'
    if len(jobs) == 1:
        return f'No run of "{jobs[0]}" is recorded yet.'
    return "No run of the selected jobs is recorded yet."


def log_of(entry: Entry, logs: Sequence[ActivityLog]) -> Optional[Path]:
    """The log of the process that wrote ``entry`` — the latest, for a pass continued over
    several sittings; None when no log kept names it."""
    if isinstance(entry, ActivityLog):
        return entry.path
    wanted = (entry.run_id if isinstance(entry, RunRecord) else entry.repair_id if isinstance(entry, RepairRecord)
              else entry.verification_id)
    found = [log for log in logs if any(activity == wanted for activity, _kind in log.activities)]
    return max(found, key=lambda log: (log.started, log.path.name)).path if found else None


HISTORY_COLUMNS = ("Job", "Started", "Destination", "How started", "Result", "Copied", "Into versions", "Renamed",
                   "Skipped", "Took", "Details")


def history_file_name(today: date) -> str:
    return f"MirrorHold history {today.isoformat()}.csv"


def log_file_name(today: date) -> str:
    return f"MirrorHold log {today.isoformat()}.txt"


def save_history(path: Path, rows: Sequence[HistoryRow]) -> None:
    """Write ``rows`` as History shows them to a CSV file a person reads in a spreadsheet
    program: the words as shown, never a format promised to other programs."""
    with open(path, "w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(HISTORY_COLUMNS)
        for r in rows:
            writer.writerow((r.job, r.started, r.destination, r.how, r.result, r.copied, r.into_versions, r.renamed,
                             r.skipped, r.took, r.details))


@dataclass(frozen=True)
class RecordDetails:
    title: str
    summary: str
    table: Tuple[Tuple[str, str, str, str], ...]     # (what, planned, done, left)
    skipped: Tuple[Tuple[str, str], ...]
    skipped_total: int
    errors: Tuple[str, ...]
    versions_batch: str                               # relative to the destination, "" for none
    versions_path: Optional[Place]                    # where it is now, when the destination is there
    engine: str
    source: str = ""                                  # how the source was read; "" when it was not
    cleanup: str = ""                                 # what the version cleanup deleted; "" when it did nothing
    space: str = ""                                   # the drive's free space when the pair ended; "" unknown
    space_low: bool = False


def _summary(record: RunRecord) -> str:
    if _unreached(record):
        why = unreached_reason(record.detail)
        return "Its remote could not be reached; it waits." + (f" {sentence(why)}." if why else "")
    reasons = "; ".join(reason_words(r) for r in record.reasons if r != FIRST_RUN_REASON)
    if record.outcome is Outcome.CANCELLED:
        text = ("Cancelled by you after the files in flight." if "cancelled-graceful" in record.reasons
                else "Cancelled by you at once." if "cancelled-immediate" in record.reasons
                else "Cancelled before it started.")
    elif record.outcome is Outcome.COMPLETED:
        text = "Completed."
    elif record.outcome is Outcome.HELD and FIRST_RUN_REASON in record.reasons:
        text = "The first run of this destination waits for your confirmation; nothing was written."
    elif record.outcome is Outcome.HELD:
        text = f"New and changed files were copied; removals were held ({reasons})."
    elif record.outcome is Outcome.ABSENT:
        text = "Its drive was not attached; it waits."
    elif record.outcome is Outcome.INTERRUPTED:
        text = "Interrupted (a shutdown, an unplugged drive or a crash); the next run continues."
    else:
        text = f"{result_words(record)}: {reasons or 'see below'}."
    if record.detail and record.outcome not in (Outcome.COMPLETED, Outcome.CANCELLED, Outcome.HELD):
        text += f" {sentence(record.detail)}."
    identical = record.done.get("files_identical", 0)
    if identical:
        text += (f" {files(identical)} {agreeing(identical, 'was', 'were')} already identical to "
                 f"{agreeing(identical, 'its backup copy: only its', 'their backup copy: only their')} date was "
                 f"updated.")
    return text


def cleanup_words(cleaned: Optional[VersionsCleaned]) -> str:
    """'Old versions deleted: 3 batches older than 90 days (12.4 GiB)', with what it could
    not delete; "" when the cleanup is off or had nothing to do."""
    if cleaned is None or not (cleaned.batch_total or cleaned.problems):
        return ""
    if cleaned.batch_total:
        text = (f"Old versions deleted: {counted(cleaned.batch_total, 'batch', 'batches', separated=True)} older than "
                f"{counted(cleaned.keep_days, 'day')} ({size(cleaned.bytes)})")
        if cleaned.trash:
            text += "; the service may keep them until it removes them"
    else:
        text = "Old versions: none deleted"
    if cleaned.problems:
        text += f"; {counted(len(cleaned.problems), 'problem')}: {cleaned.problems[0]}"
    return text


def space_words(space: Optional[Tuple[int, int]], *, remote: bool = False) -> str:
    """'Free after the run: 212 GiB of 2.9 TiB (7 %) - low', or "" when it is unknown;
    ``remote``: said as the service reports it."""
    if space is None:
        return ""
    free, capacity = space
    lead = "Free after the run, as the service reports it" if remote else "Free after the run"
    text = f"{lead}: {size(free)} of {size(capacity)} ({free_percent(free, capacity):.0f} %)"
    return text + (" - low" if low_on_space(free, capacity) else "")


def record_details(record: RunRecord, destination_root: Optional[Root], destination_name: str = "") -> RecordDetails:
    p, d, left = record.planned, record.done, record.left
    table = (
        ("Files to copy", count(p["files_to_copy"]), count(d["files_copied"] + d["files_identical"]),
         count(left["files_to_copy"])),
        ("Bytes to copy", size(p["bytes_to_copy"]), size(d["bytes_copied"] + d["bytes_identical"]),
         size(left["bytes_to_copy"])),
        ("Into versions", count(p["deleted_files"] + p["changed_files"]), count(d["moved_to_versions"]),
         count(left["removals"])),
        ("Renamed", count(p["renames"]), count(d["renamed"]), count(left["renames"])),
    )
    batch = record.versions_batch or ""
    path = place_under(destination_root, batch) if batch and destination_root is not None else None
    title = f"Run {local_time(record.started)} - {record.job_name}, destination {record.destination_index}"
    if destination_name:
        title += f" ({destination_name})"
    return RecordDetails(title, _summary(record), table, skipped_words(record), record.skipped_total, record.errors,
                         batch, path, record.engine_version or "not started", source_words(record.source_read),
                         cleanup_words(record.versions_cleaned),
                         space_words(record.space, remote=isinstance(destination_root, RemoteEndpoint)),
                         record.low_on_space)
