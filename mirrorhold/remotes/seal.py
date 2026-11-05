"""The remembered password: the rclone configuration's password sealed for the user's
Windows account, beside the store, only where the user chose it.

The seal is Windows' per-user data protection (``CryptProtectData``) with an entropy of
this tool's own, so only a process running as the same Windows account can open it, and
only by asking for this tool's entropy. It is written whole (``exchange``), then read back
and opened to verify; a seal that does not open to the password it was given is removed
and refused. A new password is sealed beside the remembered one first (``trial``), the
configuration opened with that, and only then does it replace the remembered one whole
(``take_up``): an activity unlocking meanwhile reads the password remembered before, never
one on trial. Nothing but the ``unlock`` command, which the engine runs as the
configuration's password command, and what sets it — ``password set``, the window's
preferences — ever opens it.

A password Windows alone remembers is lost when the account's protection keys are — an
administrator resetting the account's password, for one (Windows' documented behaviour) —
so the user is told to keep it elsewhere too.
"""
from __future__ import annotations

import ctypes
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .. import exchange

REMINDER = ("Remembered for your Windows account only. Keep it somewhere else too: if Windows loses this "
            "account's keys - when an administrator resets its password, for one - the configuration "
            "cannot be opened, and an encrypted backup behind it is lost with it.")
"""What the user is told as a password is remembered."""

ENTROPY = b"MirrorHold remote configuration password"
"""What the seal is bound to besides the Windows account: another program of the same
account opening it must name this too."""

_UI_FORBIDDEN = 0x1
_LOOK = 0.02
"""Seconds between two looks for a seal being renamed over."""
STALE_TRIAL_SECONDS = 600.0
"""How old a password on trial (``trial``) is before it is taken for one a process ending
mid-trial left: a trial lasts as long as opening the configuration, seconds."""


class SealError(Exception):
    """The password cannot be sealed, or the sealed one cannot be opened; why, in words."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(protect: bool, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise SealError("a password can be remembered only on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    given, _kept = _blob(data)
    entropy, _kept_entropy = _blob(ENTROPY)
    out = _Blob()
    if not function(ctypes.byref(given), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        error = ctypes.get_last_error()
        raise SealError(f"Windows refused to {'seal' if protect else 'open'} it: {ctypes.FormatError(error).strip()} "
                        f"({error})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def seal(data: bytes) -> bytes:
    """``data`` sealed for this Windows account and this tool's entropy."""
    return _crypt(True, data)


def unseal(sealed: bytes) -> bytes:
    """What ``seal`` sealed; SealError when this account cannot open it."""
    return _crypt(False, sealed)


def check_password(password: str) -> bytes:
    """The password as the engine is handed it (UTF-8); SealError when it is empty or holds
    a line break, which a password command's output cannot carry."""
    if not password or any(c in password for c in "\r\n\x00"):
        raise SealError("the password is empty or holds a line break")
    return password.encode("utf-8")


def remember(path: Path, password: str) -> None:
    """Seal ``password`` into ``path``, replacing what is there, and verify it opens to the
    same password; SealError, nothing kept, when it does not."""
    data = check_password(password)
    exchange.write_bytes(Path(path), seal(data), durable=True)
    try:
        opened = unseal(exchange.read_bytes(Path(path)))
    except (OSError, SealError) as exc:
        forget(path)
        raise SealError(f"the sealed password could not be read back: {exc}") from None
    if opened != data:
        forget(path)
        raise SealError("the sealed password did not open to the password given")


def present(path: Path) -> bool:
    """Whether a password is remembered in ``path``; a seal not there is looked for again for
    the moment a new one is renamed over it (``exchange.RENAME_PATIENCE``). Nothing is
    opened."""
    deadline = time.monotonic() + exchange.RENAME_PATIENCE
    while not Path(path).is_file():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOOK)
    return True


def remembered(path: Path) -> bytes:
    """The remembered password (UTF-8); SealError when none is remembered or this account
    cannot open it. A seal not there is looked for again for the moment a new one is renamed
    over it (``exchange.RENAME_PATIENCE``)."""
    try:
        sealed = exchange.read_bytes(Path(path), missing=exchange.RENAME_PATIENCE)
    except FileNotFoundError:
        raise SealError("no password is remembered") from None
    except OSError as exc:
        raise SealError(f"the remembered password cannot be read: {exc.strerror or exc}") from None
    return unseal(sealed)


def trial(path: Path) -> Path:
    """A file beside ``path`` for a password on trial: sealed there and tried, then taken up
    (``take_up``), so that ``path`` never holds a password that has not opened the
    configuration."""
    path = Path(path)
    return path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.trial")


def sweep_trials(path: Path, *, now: Optional[float] = None) -> None:
    """Remove the trials beside ``path`` that a process ending mid-trial left, once
    ``STALE_TRIAL_SECONDS`` old: a trial lasts as long as opening the configuration takes."""
    path = Path(path)
    now = time.time() if now is None else now
    try:
        left = list(path.parent.glob(f"{path.name}.*.trial"))
    except OSError:
        return
    for tried in left:
        try:
            stale = now - tried.stat().st_mtime >= STALE_TRIAL_SECONDS
        except OSError:
            continue
        if stale:
            forget(tried)


def take_up(tried: Path, path: Path) -> None:
    """Make the password sealed in ``tried`` the remembered one, replacing ``path`` whole;
    OSError, ``path`` as it was, when it cannot be written."""
    exchange.write_bytes(Path(path), exchange.read_bytes(Path(tried)), durable=True)


def kept(path: Path) -> Optional[bytes]:
    """The sealed file as it stands, for ``put_back``; None when there is none, once looked
    for again for the moment a new one is renamed over it (``exchange.RENAME_PATIENCE``), so
    that a ``put_back`` never takes a password being replaced for none. Nothing is opened."""
    try:
        return exchange.read_bytes(Path(path), missing=exchange.RENAME_PATIENCE)
    except FileNotFoundError:
        return None


def put_back(path: Path, previous: Optional[bytes]) -> None:
    """Leave ``path`` as ``kept`` found it: a password that was not taken up after all gives
    way to what was remembered before."""
    if previous is None:
        forget(path)
    else:
        exchange.write_bytes(Path(path), previous, durable=True)


def forget(path: Path) -> bool:
    """Remove the remembered password; True when none is left."""
    return exchange.remove(Path(path))
