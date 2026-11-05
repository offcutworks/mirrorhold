"""The list of verified copies: the files of a destination a pass read on both sides and
found the same, as they were then — what a verification of new copies leaves unread.

Each entry is a fingerprint of one file as the pass compared it: its key, its size and its
modification time on each side, hashed to 64 bits. A file whose fingerprint is in a list
is the copy a pass found the same, unchanged since by every measure a run uses: a run that
copies it again changes its size or time, a rename its key, a time set by the engine its
time. What a list cannot tell is a copy damaged on its drive since it was read — which is
what a full pass is for — or a destination copy another program replaced with one of the
same name, size and time. A lookup never answers yes for a fingerprint the list does not
hold; what a pass risks is a new or changed file taking, by chance, a fingerprint its
baseline holds and being left unread: about the files it compares times the files the
baseline holds, over 2^64 — 5.4 × 10^-10 for ten thousand against a million. Two files
of one list sharing a fingerprint is a larger chance — n²/2^65, 2.7 × 10^-8 for a
million — and costs nothing: both were found the same.

No timestamp serves instead (measured on the development machine, 2026-10-19): a copy the
engine replaces within about 15 s of moving the old one into versions takes the old copy's
creation time (Windows' file-name tunnelling), so a destination's creation times miss most
edited files; copying keeps a source's modification time, so a photo imported with an old
date reads as old; and NTFS's change time, which catches both, exFAT and FAT do not have.

The files lie beside the store, never on the destination, in
``verifications/<job id>/verified/``: per pass, ``<record name>.partial`` while it works —
fingerprints appended at every save of its progress in the order compared, a continued pass
appending to its own — and, once it finished, ``<record name>.list``, the same fingerprints
sorted, each once. Both begin with ``SIGNATURE``. A pass of new copies writes into its own
list every fingerprint of its baseline it found unchanged as well, so the latest finished
list holds every copy verified and unchanged since. Once it is written, the lists of the
destination's other passes are removed, but for the partial list of a pass still to be
continued.

A fingerprint missing from a list only makes a later pass read that file again, which is
why nothing here waits or fails a pass: the partial list is appended to before the record's
cursor is saved past what it holds, what a refused or failed append did not write whole is
written again in its place at the next, a torn last entry is dropped, and a finished pass
whose list was not sorted is read from its partial list. A list another program holds is
waited for, as long as ``READ_PATIENCE``, before it counts as none. A destination none of
whose finished passes has a list that can be read has none: its next verification of new
copies reads every file, as a full one.
"""
from __future__ import annotations

import bisect
import hashlib
import struct
import sys
from array import array
from pathlib import Path
from typing import Collection, Iterable, Optional, Sequence, Tuple

from .. import exchange
from ..locations import RuntimeLayout
from .verification import VerificationMode, VerificationRecord, resumable

SIGNATURE = b"MHVLIST1"
LIST_DIR = "verified"
READ_PATIENCE = 30.0
"""Seconds a list another program holds is waited for before a pass reads every file instead."""
_ENTRY = 8
_LENGTH = struct.Struct("<q")
_TIMES = struct.Struct("<qqq")


def fingerprint(key: str, size: int, source_mtime_ns: int, destination_mtime_ns: int) -> int:
    """The 64-bit fingerprint of one file as a pass compares it."""
    name = key.encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(_LENGTH.pack(len(name)), digest_size=_ENTRY)
    digest.update(name)
    digest.update(_TIMES.pack(size, source_mtime_ns, destination_mtime_ns))
    return int.from_bytes(digest.digest(), "little")


def _stem(record: VerificationRecord) -> str:
    return record.file_name().removesuffix(".json")


def list_path(layout: RuntimeLayout, record: VerificationRecord) -> Path:
    return layout.verifications_dir / record.job_id / LIST_DIR / f"{_stem(record)}.list"


def partial_path(layout: RuntimeLayout, record: VerificationRecord) -> Path:
    return layout.verifications_dir / record.job_id / LIST_DIR / f"{_stem(record)}.partial"


def _encoded(entries: array) -> bytes:
    if sys.byteorder != "little":
        entries = array("Q", entries)
        entries.byteswap()
    return entries.tobytes()


def _decoded(data: bytes) -> Optional[array]:
    """The entries a list's bytes hold, a torn last one dropped; None without the signature."""
    if not data.startswith(SIGNATURE):
        return None
    body = memoryview(data)[len(SIGNATURE):]
    entries = array("Q")
    entries.frombytes(body[:len(body) - len(body) % _ENTRY])
    if sys.byteorder != "little":
        entries.byteswap()
    return entries


def _sorted_once(entries: Iterable[int]) -> array:
    ordered = array("Q")
    last = None
    for value in sorted(entries):
        if value != last:
            ordered.append(value)
            last = value
    return ordered


class VerifiedCopies:
    """A finished pass's list, sorted, for lookups."""

    def __init__(self, entries: array):
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, value: int) -> bool:
        i = bisect.bisect_left(self._entries, value)
        return i < len(self._entries) and self._entries[i] == value


def read_list(layout: RuntimeLayout, record: VerificationRecord) -> Optional[VerifiedCopies]:
    """A finished pass's list — read from its partial list when the pass ended before it
    sorted it — or None when neither can be read. A list another program holds, as an
    antivirus scanning a file just written does, is waited for up to ``READ_PATIENCE``."""
    try:
        entries = _decoded(exchange.read_bytes(list_path(layout, record), patience=READ_PATIENCE))
        if entries is not None:
            return VerifiedCopies(entries)
    except OSError:
        pass
    try:
        entries = _decoded(exchange.read_bytes(partial_path(layout, record), patience=READ_PATIENCE))
    except OSError:
        return None
    return VerifiedCopies(_sorted_once(entries)) if entries is not None else None


def _signed(path: Path) -> bool:
    """Whether ``path`` is a list: its signature read — or, held by another program that
    refuses it now, there to be read once that program lets it go."""
    try:
        with exchange.open_shared(path) as handle:
            return handle.read(len(SIGNATURE)) == SIGNATURE
    except PermissionError:
        return True
    except OSError:
        return False


def _finished_latest_first(records: Sequence[VerificationRecord], marker_id: str):
    return sorted((r for r in records if r.marker_id == marker_id and r.finished),
                  key=lambda r: (r.ended or r.updated, r.started), reverse=True)


def kept(layout: RuntimeLayout, records: Sequence[VerificationRecord],
         marker_id: str) -> Optional[VerificationRecord]:
    """The latest finished pass of the destination with a list of verified copies — its
    signature read, not the list — or None: a verification of new copies has nothing to
    build on, and reads every file."""
    return next((r for r in _finished_latest_first(records, marker_id)
                 if _signed(list_path(layout, r)) or _signed(partial_path(layout, r))), None)


def baseline(layout: RuntimeLayout, records: Sequence[VerificationRecord], marker_id: str,
             prefer: Optional[str] = None) -> Optional[Tuple[VerificationRecord, VerifiedCopies]]:
    """The finished pass a verification of new copies builds on, with its list: the one
    named ``prefer`` (a continued pass's) when its list can still be read, else the latest
    whose list can. Any list serves — each fingerprint in it is a copy a pass found the same
    and unchanged since — the latest only holds the most."""
    finished = _finished_latest_first(records, marker_id)
    finished.sort(key=lambda r: r.verification_id != prefer)
    for record in finished:
        copies = read_list(layout, record)
        if copies is not None:
            return record, copies
    return None


class ListWriter:
    """The list a pass writes as it works: fingerprints added as files are found the same,
    appended to its partial list at each save, sorted into its list once it finished. Each
    append is written at the end of what the last one wrote whole, and whatever lies past it
    is cut, so an append that failed partway — a full disk — never shifts the entries after
    it."""

    def __init__(self, layout: RuntimeLayout, record: VerificationRecord, *, continuing: bool):
        self.path = partial_path(layout, record)
        self.final = list_path(layout, record)
        self._continuing = continuing
        self._length: Optional[int] = None      # the bytes of the partial list written whole; None until it is open
        self._pending = array("Q")

    def add(self, value: int) -> None:
        self._pending.append(value)

    def _prepare(self) -> int:
        """The partial list's whole length: a continued pass's own, its torn last entry cut,
        else a new one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._continuing:
            try:
                with open(self.path, "r+b") as handle:
                    if handle.read(len(SIGNATURE)) == SIGNATURE:
                        body = handle.seek(0, 2) - len(SIGNATURE)
                        return len(SIGNATURE) + body - body % _ENTRY
            except FileNotFoundError:
                pass
        with open(self.path, "wb") as handle:
            handle.write(SIGNATURE)
        return len(SIGNATURE)

    def flush(self) -> bool:
        """Append what was added since the last flush; False when the file refused it, which
        is kept for the next flush. A partial list gone since is begun again: the copies it
        held are only read again by the next pass of new copies."""
        try:
            if self._length is None:
                self._length = self._prepare()
            if self._pending:
                data = _encoded(self._pending)
                with open(self.path, "r+b") as handle:
                    handle.seek(self._length)
                    handle.write(data)
                    handle.truncate()
                self._length += len(data)
                self._pending = array("Q")
        except FileNotFoundError:
            self._length, self._continuing = None, False
            return False
        except OSError:
            return False
        return True

    def finish(self) -> bool:
        """Sort the partial list into the pass's list, each fingerprint once, and remove the
        partial list; False when that could not be done, the partial list then left to be
        read in its place."""
        if not self.flush():
            return False
        try:
            entries = _decoded(exchange.read_bytes(self.path))
            if entries is None:
                return False
            exchange.write_bytes(self.final, SIGNATURE + _encoded(_sorted_once(entries)), durable=True)
        except OSError:
            return False
        exchange.remove(self.path)
        return True


def prune(layout: RuntimeLayout, records: Sequence[VerificationRecord], keep: VerificationRecord,
          live: Collection[str] = ()) -> None:
    """Remove the lists of the destination's passes other than ``keep``, which holds every
    copy theirs do that is still unchanged, but the partial list of a pass still to be
    continued or at work; a file another program holds stays for the next time."""
    marker = keep.marker_id
    waiting = {r.verification_id for r in (resumable(records, marker, live, mode) for mode in VerificationMode)
               if r is not None} | set(live)
    for record in records:
        if record.marker_id != marker or record.verification_id == keep.verification_id:
            continue
        exchange.remove(list_path(layout, record))
        if record.verification_id not in waiting:
            exchange.remove(partial_path(layout, record))
