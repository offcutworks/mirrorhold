"""Files one process replaces while another reads them: the one way they are written and read.

The core's processes and the window share no memory; they share files beside the store
— the job file, each activity's status, control request and liveness lock, the run and
verification records, the preview's tickets — and a destination's marker and record of
what its job owns there. Windows refuses to replace a file another process has open,
and refuses to open one while it is being replaced; a refusal that reaches a caller
ends an activity, loses a request or reads as a file that is not there. So every such
file is written and read here, in two halves that keep the program's own processes out
of each other's way (measured on the development machine, two processes in tight
loops for 6 s: neither side refused once, against a third of the writes and of the
reads refused the ordinary way; a dozen such pairs beside a dozen busy processes had no
write of 98,000 refused, and about one read in 9,000 for a moment, which a read's patience
bears):

- **A reader opens the file sharing reading, writing and deletion** (``open_shared``):
  it never stands in a writer's way, and it reads either the old file whole or the new
  one whole.
- **A writer writes a staging file beside the target and renames it over the target
  with POSIX semantics** (``write_bytes``): Windows lets that happen while readers that
  share deletion hold the old file. A volume that does not support such a rename (FAT,
  exFAT) is renamed over the ordinary way.

What neither half controls — another program holding the file without sharing it, as
an antivirus scanning a file just written does — is borne for a bounded patience: a
refusal is tried again until it lands or the patience runs out, and ``FileBusy`` then
reaches the caller, whose decision it is. A waiting activity tries again at its next
attempt; a verification's progress waits for its next save; a request a person made is
reported as not written. A file that is not there is not a refusal: ``FileNotFoundError``
reaches the caller at once. A file being renamed over can be not there for a moment: the
rename takes the old file's name away just before it gives it to the new one, a moment a
reader meets only under load (measured on the development machine, two misses in 110,000
reads beside a dozen busy processes). A caller to whom a missing file means something looks
again — under the writers' lock where they hold one (the job file, a run's control file
as a request changes it), else for a moment: a run's status while the run's lock is held,
and through a read's ``missing`` patience (``RENAME_PATIENCE``) a file that, once written,
is only ever renamed over — a drive destination's marker and record, the remembered
password, a record a folder's listing named — or keeps what it read last where the file,
once written, stays (a run's control file, as its run reads it).

``in_use`` answers whether any process has a file open, by asking Windows to open it
unshared for a moment: the window tells a diagnostic log whose process still writes it
from one whose process ended without saying so.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Optional

READ_PATIENCE = 2.0
"""Seconds a read is tried again while the file is refused."""

WRITE_PATIENCE = 2.0
"""Seconds a write is tried again while the file is refused, unless the caller says."""

RENAME_PATIENCE = 0.2
"""Seconds a read looks again for a file that is not there, where the file, once written, is
only ever renamed over: the moment a rename takes the old file's name away before it gives
it to the new one. A file truly not there is then told so this much later."""

_RETRY_FIRST = 0.01
_RETRY_MOST = 0.1


class FileBusy(OSError):
    """A file another program kept refused for the whole patience."""

    def __init__(self, path: Path, reading: bool):
        self.path = Path(path)
        super().__init__(f"another program kept {self.path.name} open"
                         + ("" if reading else "; it was not written"))


def _refused(exc: OSError) -> bool:
    """Whether an error is Windows refusing a file another process holds (or one being
    deleted), rather than the file missing or the operation failing for good."""
    return isinstance(exc, PermissionError)


def _pause(attempt: int) -> None:
    time.sleep(min(_RETRY_MOST, _RETRY_FIRST * (2 ** attempt)))


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.SetFileInformationByHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _SHARE_ALL = 0x1 | 0x2 | 0x4                 # read, write, delete
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_RENAME_INFO_EX = 22
    _REPLACE_IF_EXISTS, _POSIX_SEMANTICS = 0x1, 0x2
    _UNSUPPORTED = (1, 50, 87)                   # invalid function, not supported, invalid parameter

    def _create(path: str, access: int) -> int:
        handle = _k32.CreateFileW(path, access, _SHARE_ALL, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if handle == _INVALID_HANDLE:
            code = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(code).strip(), path, code)
        return handle

    def open_shared(path: Path) -> BinaryIO:
        """``path`` opened for reading, sharing reading, writing and deletion."""
        handle = _create(str(path), _GENERIC_READ)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError:
            _k32.CloseHandle(handle)
            raise
        return os.fdopen(fd, "rb")

    def _nt_name(path: Path) -> str:
        full = os.path.abspath(str(path))
        if full.startswith("\\\\?\\") or full.startswith("\\\\.\\"):
            return "\\??\\" + full[4:]
        if full.startswith("\\\\"):
            return "\\??\\UNC\\" + full[2:]
        return "\\??\\" + full

    def _rename_over(staging: Path, target: Path) -> None:
        name = _nt_name(target)

        class _RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                        ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * (len(name) + 1))]

        info = _RenameInfo(_REPLACE_IF_EXISTS | _POSIX_SEMANTICS, None, len(name) * 2, name)
        handle = _create(str(staging), _DELETE)
        try:
            if _k32.SetFileInformationByHandle(handle, _FILE_RENAME_INFO_EX, ctypes.byref(info), ctypes.sizeof(info)):
                return
            code = ctypes.get_last_error()
        finally:
            _k32.CloseHandle(handle)
        if code in _UNSUPPORTED:
            os.replace(staging, target)
            return
        raise OSError(None, ctypes.FormatError(code).strip(), str(target), code)

    _SHARING_VIOLATION = 32

    def in_use(path: Path) -> Optional[bool]:
        """Whether some process has ``path`` open: Windows refuses to open it unshared while
        one does. Opened alone for a moment, read access only. None when that cannot be
        told (the file gone, or refused for another reason)."""
        handle = _k32.CreateFileW(str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if handle == _INVALID_HANDLE:
            return True if ctypes.get_last_error() == _SHARING_VIOLATION else None
        _k32.CloseHandle(handle)
        return False
else:
    def in_use(path: Path) -> Optional[bool]:
        """Whether some process has ``path`` open; not known here."""
        return None

    def open_shared(path: Path) -> BinaryIO:
        """``path`` opened for reading."""
        return open(path, "rb")

    def _rename_over(staging: Path, target: Path) -> None:
        os.replace(staging, target)


def read_bytes(path: Path, *, patience: float = READ_PATIENCE, missing: float = 0.0) -> bytes:
    """The file's content, read whole. FileNotFoundError when it is not there, once it has
    been looked for again for ``missing`` seconds (``RENAME_PATIENCE`` for a file only ever
    renamed over); FileBusy when it stayed refused for ``patience`` seconds."""
    deadline = time.monotonic() + patience
    gone_until: Optional[float] = None
    attempt = 0
    while True:
        try:
            with open_shared(Path(path)) as handle:
                return handle.read()
        except FileNotFoundError:
            now = time.monotonic()
            if gone_until is None:
                gone_until = now + missing
            if now >= gone_until:
                raise
        except OSError as exc:
            if not _refused(exc):
                raise
            if time.monotonic() >= deadline:
                raise FileBusy(path, reading=True) from exc
        _pause(attempt)
        attempt += 1


def read_text(path: Path, *, patience: float = READ_PATIENCE, missing: float = 0.0) -> str:
    """The file's content as UTF-8 text; as ``read_bytes``."""
    return read_bytes(path, patience=patience, missing=missing).decode("utf-8")


def write_bytes(path: Path, data: bytes, *, patience: float = WRITE_PATIENCE, durable: bool = False,
                staging: Optional[Path] = None) -> None:
    """Replace ``path`` with ``data`` at once: no reader ever sees a part of it.

    ``durable``: flushed to the disk before it replaces the file, for a record that must
    survive a power cut. ``staging``: the file it is written to first, beside the target
    (default ``<name>.writing``); a file several processes write takes one of its own
    (``private_staging``). FileBusy when the file stayed refused for ``patience``
    seconds, the target then as it was."""
    path = Path(path)
    staging = Path(staging) if staging is not None else path.with_name(path.name + ".writing")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + patience
    attempt = 0
    written = False
    try:
        while True:
            try:
                if not written:
                    with open(staging, "wb") as handle:
                        handle.write(data)
                        if durable:
                            handle.flush()
                            os.fsync(handle.fileno())
                    written = True
                _rename_over(staging, path)
                return
            except OSError as exc:
                if not _refused(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise FileBusy(path, reading=False) from exc
            _pause(attempt)
            attempt += 1
    except BaseException:
        try:
            os.remove(staging)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str, **options) -> None:
    """``text`` as UTF-8, with the line endings it holds; as ``write_bytes``."""
    write_bytes(path, text.encode("utf-8"), **options)


def private_staging(path: Path) -> Path:
    """A staging file beside ``path`` no other writer uses: for a file several processes
    write, whose writers would otherwise write one staging file at once."""
    path = Path(path)
    return path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.writing")


def remove(path: Path, *, patience: float = WRITE_PATIENCE) -> bool:
    """Remove the file; True when it is gone (or was never there), False when it stayed
    refused for ``patience`` seconds."""
    deadline = time.monotonic() + patience
    attempt = 0
    while True:
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not _refused(exc) or time.monotonic() >= deadline:
                return False
        _pause(attempt)
        attempt += 1


__all__ = ["FileBusy", "READ_PATIENCE", "RENAME_PATIENCE", "WRITE_PATIENCE", "in_use", "open_shared", "private_staging", "read_bytes",
           "read_text", "remove", "write_bytes", "write_text"]
