"""The *About* window in words: which copy this is, its copyright and licence, that it is
free and where its official copies come from, the engine it drives, and the third-party
notices a release ships. No toolkit.

The copyright line is read from the licence file shipped beside the packages — the
notice the MIT License asks to travel with every copy — so it is written once, there,
and the window shows what the licence says. A portable release carries the same file at
the root of its folder. A copy whose licence file is missing or cannot be read says so,
and offers no licence to open. Every copy names the same official source, so that a
user sold a copy can find out it is free. A release also carries the notices of the
software it bundles (``shipped_model``), which *About* opens; a copy that ships none
offers none.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mirrorhold.engine.rclone import PINNED_VERSION
from mirrorhold.installation.release import Release

from .shipped_model import LICENCE_FILE_NAME, licence_path, notices_path, read_text  # noqa: F401

OFFICIAL_COPIES = "https://github.com/offcutworks/mirrorhold"

WINDOW_TITLE = "About MirrorHold"
LICENCE_TITLE = "MirrorHold licence"
NOTICES_TITLE = "MirrorHold third-party notices"
TAGLINE = "One-way backups of folders and drives onto backup drives."
FREE = "MirrorHold is free: it costs nothing, and it is released under the MIT License."
OFFICIAL = "Official copies come only from"
MISSING_LICENCE = "The licence file is missing from this copy."
ENGINE = f"Sync engine: rclone {PINNED_VERSION} (pinned)"


def about_title(release: Optional[Release]) -> str:
    """*About*'s first line: the release a portable copy is; a working copy runs from source."""
    if release is None:
        return "MirrorHold, run from source"
    return f"MirrorHold {release.label}" if release.readable else "MirrorHold, release unknown"


def copyright_line(licence: Optional[str]) -> Optional[str]:
    """The licence's copyright notice, as written there."""
    for line in (licence or "").splitlines():
        if line.strip().lower().startswith("copyright"):
            return line.strip()
    return None


@dataclass(frozen=True)
class AboutContent:
    title: str
    licence: Optional[str]           # the licence's text; None when it cannot be read
    notices: Optional[str] = None    # the third-party notices' text; None when the copy ships none

    @property
    def copyright(self) -> str:
        """The copyright line, or why there is none to show."""
        if self.licence is None:
            return MISSING_LICENCE
        return copyright_line(self.licence) or ""


def about_content(release: Optional[Release], licence_file: Path, notices_file: Optional[Path] = None) -> AboutContent:
    notices = read_text(notices_file) if notices_file is not None else None
    return AboutContent(about_title(release), read_text(licence_file), notices)
