"""File signatures: the leading bytes that identify a file's format.

Encryption by ransomware leaves a file's name and often its size alone and replaces its
content with bytes that match no format. So a changed file whose old copy began with a
known signature and whose new content begins with none is the mark removals are held
on, whatever their number. Only changed files are examined, and only their first
``LEADING_BYTES`` bytes on each side; a file that cannot be read is not judged.

The table lists formats whose signature is distinctive enough that encrypted bytes
match it only by vanishing chance, and that ordinary text does not begin with — a
two-letter signature such as BMP's ``BM`` or an executable's ``MZ`` would turn the edit
of a text file beginning with those letters into a hold. Text formats have no signature
and cannot be judged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, FrozenSet, Optional, Tuple

from .plan import PairPlan

LEADING_BYTES = 16


@dataclass(frozen=True)
class Signature:
    name: str
    offset: int
    magic: bytes

    def matches(self, head: bytes) -> bool:
        return head[self.offset:self.offset + len(self.magic)] == self.magic


KNOWN_SIGNATURES: Tuple[Signature, ...] = (
    Signature("JPEG", 0, b"\xff\xd8\xff"),
    Signature("PNG", 0, b"\x89PNG\r\n\x1a\n"),
    Signature("GIF", 0, b"GIF87a"),
    Signature("GIF", 0, b"GIF89a"),
    Signature("TIFF (and camera raw)", 0, b"II*\x00"),
    Signature("TIFF (and camera raw)", 0, b"MM\x00*"),
    Signature("WebP, WAV, AVI (RIFF)", 0, b"RIFF"),
    Signature("Photoshop", 0, b"8BPS"),
    Signature("ISO media: MP4, MOV, HEIC, CR3, M4A", 4, b"ftyp"),
    Signature("Matroska, WebM", 0, b"\x1a\x45\xdf\xa3"),
    Signature("ASF, WMV, WMA", 0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"),
    Signature("MPEG program stream", 0, b"\x00\x00\x01\xba"),
    Signature("MP3 with ID3 tag", 0, b"ID3"),
    Signature("FLAC", 0, b"fLaC"),
    Signature("Ogg", 0, b"OggS"),
    Signature("PDF", 0, b"%PDF-"),
    Signature("ZIP (and Office, OpenDocument, EPUB)", 0, b"PK\x03\x04"),
    Signature("ZIP, empty", 0, b"PK\x05\x06"),
    Signature("OLE compound file (older Office, Outlook)", 0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    Signature("RAR", 0, b"Rar!\x1a\x07"),
    Signature("7-Zip", 0, b"7z\xbc\xaf\x27\x1c"),
    Signature("gzip", 0, b"\x1f\x8b"),
    Signature("bzip2", 0, b"BZh"),
    Signature("xz", 0, b"\xfd7zXZ\x00"),
    Signature("Cabinet", 0, b"MSCF"),
    Signature("SQLite", 0, b"SQLite format 3\x00"),
    Signature("ELF executable", 0, b"\x7fELF"),
    Signature("Binary property list", 0, b"bplist"),
)


def matches_known(head: bytes) -> bool:
    return any(s.matches(head) for s in KNOWN_SIGNATURES)


def lost_signature(before: bytes, after: bytes) -> bool:
    """Whether content that began with a known signature now begins with none."""
    return matches_known(before) and not matches_known(after)


Reader = Callable[[Path], Optional[bytes]]
Progress = Callable[[int, int], None]
"""Told (files examined, files to examine) before each changed file and once at the end."""


def assess_signatures(plan: PairPlan, source_root: Path, destination_root: Optional[Path],
                      read_source: Reader, read_destination: Reader,
                      progress: Optional[Progress] = None) -> FrozenSet[str]:
    """The keys of the plan's changed files that lost their signature.

    ``destination_root`` None: ``read_destination`` is given each file's path relative to
    the destination, as a destination reached through the storage seam reads it
    (``storage.DestinationStorage.read_head``).

    A reader answers None for a file that cannot be read, which is not judged; an
    exception it raises ends the assessment and reaches the caller unchanged, which is how
    a destination that stopped answering is never taken for files that cannot be read.

    With many changed files on a slow drive the reads take long; ``progress`` follows
    them, and an exception it raises ends the assessment and reaches the caller
    unchanged, which is how a caller pauses (by waiting in it) or cancels one."""
    lost = set()
    total = len(plan.changed)
    for n, change in enumerate(plan.changed):
        if progress is not None:
            progress(n, total)
        relative = change.destination.path
        before = read_destination(relative if destination_root is None else Path(destination_root) / relative)
        if before is None or not matches_known(before):
            continue
        after = read_source(Path(source_root) / change.source.path)
        if after is not None and not matches_known(after):
            lost.add(change.destination.key)
    if progress is not None:
        progress(total, total)
    return frozenset(lost)
