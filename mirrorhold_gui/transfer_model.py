"""Import and export of the job list. No toolkit.

Import reads a file the store wrote (``ConfigStore.read_export``), says for each job
whether it is new, already here, or here with other settings, and folds it in through
the store's own ``merge_imported``, a job added joining its group; every job it would
add or replace is validated like an edit, and nothing is imported while one is refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple

from mirrorhold.endpoints.cloud import SyncRoot
from mirrorhold.endpoints.volumes import VolumeInfo
from mirrorhold.jobs.model import Group, Job
from mirrorhold.jobs.store import ImportOutcome, StoreContents, merge_imported
from mirrorhold.jobs.validation import Severity, ValidationContext, validate_job


class ImportState(str, Enum):
    NEW = "new"
    SAME = "already here"
    DIFFERENT = "here, with other settings"


@dataclass(frozen=True)
class ImportLine:
    job: Job
    state: ImportState


def import_lines(contents: StoreContents, imported: Sequence[Job]) -> Tuple[ImportLine, ...]:
    lines = []
    for job in imported:
        existing = contents.job(job.job_id)
        state = ImportState.NEW if existing is None else ImportState.SAME if existing == job else ImportState.DIFFERENT
        lines.append(ImportLine(job, state))
    return tuple(lines)


@dataclass(frozen=True)
class ImportResult:
    contents: StoreContents
    outcome: ImportOutcome
    refused: Tuple[Tuple[str, str], ...]          # (job name, why) for each job that may not be imported

    @property
    def ok(self) -> bool:
        return not self.refused


def apply_import(contents: StoreContents, imported: Sequence[Job], *, replace_existing: bool,
                 volumes: Sequence[VolumeInfo], sync_roots: Sequence[SyncRoot],
                 groups: Sequence[Group] = (), store_path: Optional[Path] = None) -> ImportResult:
    """``groups``: the imported file's groups. ``store_path``: the job file in use — with
    it, the checks look at this computer too (``ValidationContext.on_this_computer``)."""
    merged, outcome = merge_imported(contents, imported, groups=groups, replace_existing=replace_existing)
    refused = []
    for job_id in outcome.added + outcome.replaced:
        job = merged.job(job_id)
        others = tuple(j for j in merged.jobs if j.job_id != job_id)
        context = (ValidationContext.on_this_computer(volumes, sync_roots, others, store_path)
                   if store_path is not None else ValidationContext(tuple(volumes), tuple(sync_roots), others))
        report = validate_job(job, context, None)
        for finding in report.findings:
            if finding.severity is Severity.REFUSED:
                refused.append((job.name, f"{finding.subject}: {finding.message}"))
    return ImportResult(merged, outcome, tuple(refused))
