"""How a verification and a repair's read-back compare a copy with its source.

On a drive both copies are read in full and their SHA-256 digests compared. On a remote a
copy is compared without reading it wherever the service allows (the maintainer's decision
of 2026-10-28), so the pass reads the source alone:

- **By fingerprint**: the service keeps a fingerprint of each file (``storage.
  fingerprint_kinds``), taken from the remote's listing, and the source's of the same kind
  is computed as the source is read. A kind this module computes (``IN_PROCESS``) is
  preferred, read in chunks so a pause, a stop and a read that hangs are handled as for a
  drive; any other kind the engine computes from the source file (``storage.
  fingerprint_local``), which a stop ends but a pause does not hold, and whose progress is
  told only as each file ends. A copy the service lists with no fingerprint is read on both
  sides.
- **By the encrypted form**: an encrypted remote keeps no fingerprint of what it holds, so
  the engine's own check encrypts each source file with the nonce at the start of its copy
  and compares the result with the fingerprint the service beneath keeps (``storage.
  check_encrypted``) — of the copy it reads only that start. Whether the service beneath
  keeps one is asked before a pass begins (``encrypted_kind``).
- **By reading**: a remote that allows neither is read as a drive is, the destination's
  copy by download.

What the comparison of a fingerprint proves is that the service keeps, of each file, what
it recorded as the source holds it; damage it did not record is not seen.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

from ..engine.rclone import Checked, CryptCheckReport
from ..storage.destination import DestinationStorage, StorageError
from .verification import Measure

CHECK_PATIENCE = 120.0
"""Seconds the engine's check of an encrypted remote, started with no file, may take to say
whether the service beneath keeps fingerprints."""

IN_PROCESS: dict = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}
"""The fingerprint kinds computed here, by the engine's names for them, in the order they are
preferred when a service keeps several."""

Hasher = Callable[[], "hashlib._Hash"]


def hasher(kind: str) -> Optional[Hasher]:
    """What computes ``kind`` here, or None for a kind only the engine computes."""
    return IN_PROCESS.get(kind)


def fingerprint_kind(storage: DestinationStorage) -> Optional[str]:
    """The fingerprint kind a comparison with ``storage`` uses: one computed here where the
    destination keeps such a kind, else the first it keeps; None where it keeps none."""
    kinds = storage.fingerprint_kinds
    return next((k for k in IN_PROCESS if k in kinds), kinds[0] if kinds else None)


def encrypted_kind(storage: DestinationStorage, source_root: Path, list_path) -> Optional[str]:
    """The fingerprint kind the engine's check of an encrypted destination compares, as it
    names it ("" where it names none); None where the service beneath keeps no fingerprint, so
    that nothing can be compared but by reading. The check is started with no file, which
    reads nothing. ``StorageError`` when it could not be asked."""
    check = storage.check_encrypted(source_root, (), list_path=list_path)
    report = check.report(timeout=CHECK_PATIENCE)
    if report.unsupported:
        return None
    if report.exit_code != 0:
        raise StorageError(report.errors[-1] if report.errors else
                           f"the engine's check of the encrypted form ended with exit status {report.exit_code}")
    return report.kind


def encrypted_outcome(report: CryptCheckReport, path: str, source: Path) -> Tuple[Optional[bool], str]:
    """What the engine's check said of the file at ``path`` (``source`` its source copy):
    (whether the copy is the source's, "") where it could tell; (None, why) where it could
    not — the copy gone, the source copy gone (which the check passes over without a word),
    or the engine's error."""
    outcome = report.checked.get(path)
    if outcome is Checked.SAME or outcome is Checked.DIFFERS:
        return outcome is Checked.SAME, ""
    if outcome is Checked.MISSING:
        return None, "the destination copy is no longer there"
    if outcome is None and not os.path.lexists(source):
        return None, "the source copy could not be read: it is no longer there"
    why = report.messages.get(path) or (report.errors[-1] if report.errors else "")
    return None, "the engine could not check it" + (f": {why}" if why else "")


def measure_of(storage: DestinationStorage, source_root: Path, list_path) -> Tuple[Measure, Optional[str]]:
    """How copies on ``storage`` are compared, and the fingerprint kind where one is."""
    if storage.encrypted:
        kind = encrypted_kind(storage, source_root, list_path)
        return (Measure.READ, None) if kind is None else (Measure.ENCRYPTED, kind or None)
    kind = fingerprint_kind(storage)
    return (Measure.READ, None) if kind is None else (Measure.FINGERPRINT, kind)
