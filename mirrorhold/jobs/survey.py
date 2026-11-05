"""How much a source holds: the figures two creation-time checks need.

A FAT32 destination cannot hold a file of 4 GiB or more, and a source larger than a
destination's free space will not fit; both checks need the source's largest file
and total size, which only a full listing gives. On a large library that listing
costs minutes (measured: about 1,800 entries a second, cold, on a pooled volume), so a
survey is its own step that a caller chooses to run; validation without one reports
the two checks as deferred to the run.

The walk reads directory listings only: it opens no file, follows no link, skips the
drive-root artefacts of a drive-root source, and counts cloud placeholders apart,
because their content is not on the disk and is never copied. It enumerates every
folder as the listing does, by its extended-length spelling
(``planning.listing.enumerable``), so a folder whose name ends in a space or a period
is walked rather than counted as one that cannot be listed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..planning.listing import enumerable
from ..wording import counted
from .exclusions import is_cloud_placeholder, is_drive_root_artefact, is_link


@dataclass(frozen=True)
class SourceSurvey:
    files: int = 0
    total_bytes: int = 0
    largest_file_bytes: int = 0
    placeholders: int = 0
    links_skipped: int = 0
    unreadable_directories: int = 0

    @property
    def complete(self) -> bool:
        """Whether every directory could be listed."""
        return self.unreadable_directories == 0


def survey_source(root: Path, *, drive_root: bool) -> SourceSurvey:
    files = total = largest = placeholders = links = unreadable = 0
    stack = [(enumerable(str(root)), True)]
    while stack:
        directory, at_root = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            unreadable += 1
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = getattr(st, "st_file_attributes", 0)
            if is_link(attributes, getattr(st, "st_reparse_tag", 0)):
                links += 1
                continue
            if at_root and drive_root and is_drive_root_artefact(attributes):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, False))
                continue
            if is_cloud_placeholder(attributes):
                placeholders += 1
                continue
            files += 1
            total += st.st_size
            largest = max(largest, st.st_size)
    return SourceSurvey(files, total, largest, placeholders, links, unreadable)


def describe(survey: Optional[SourceSurvey]) -> str:
    if survey is None:
        return "not surveyed"
    text = (f"{counted(survey.files, 'file')}, {counted(survey.total_bytes, 'byte')}, "
            f"largest {counted(survey.largest_file_bytes, 'byte')}")
    if survey.placeholders:
        text += f", {counted(survey.placeholders, 'cloud-only placeholder')} not counted"
    if not survey.complete:
        text += f", {counted(survey.unreadable_directories, 'folder')} could not be listed"
    return text
