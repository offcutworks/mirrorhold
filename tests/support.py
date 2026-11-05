"""Shared helpers for the MirrorHold suites: import path, volume fixtures, temporary folders,
a live run or verification of another process, a file held as another program holds it,
and a second Python process saving the job file.

No helper reads the live configuration store, and none touches a volume other than
through a temporary folder on the system drive. Importing this module switches
notifications, volume snapshots, the shutdown screen and the notification-area icon off for
the test process and every run it starts, so no test raises a Windows notification on the
desktop, makes, lists or removes a real volume's snapshot, holds up the machine's shutdown or
shows an icon; tests of snapshots pass a provider of their own, tests of the shutdown screen
switch it on for themselves, and tests of the icon's start pass a start of their own.

A test's temporary folder sits in the user's temporary folder beside a lock file its
process holds while the folder is in use (``temporary_folder``). What a test's removal
cannot take — a file a process it started still holds, or a whole folder of a run that was
killed — stays behind with its lock let go, and the next run removes it before it makes
its first folder. A folder of the suite with no lock file beside it was made before
folders had one, and is removed the same way."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from mirrorhold.endpoints.endpoint import Endpoint, VolumeIdentity, gpt_anchor, virtual_anchor  # noqa: E402
from mirrorhold.endpoints.volumes import Backing, VolumeInfo  # noqa: E402
from mirrorhold.notifications.toast import SWITCH_VARIABLE  # noqa: E402
from mirrorhold.power import SCREEN_SWITCH_VARIABLE  # noqa: E402
from mirrorhold.records.lock import FileLock  # noqa: E402
from mirrorhold.snapshots.provider import SWITCH_VARIABLE as SNAPSHOT_SWITCH_VARIABLE  # noqa: E402
from mirrorhold.tray import SWITCH_VARIABLE as TRAY_SWITCH_VARIABLE  # noqa: E402

os.environ[SWITCH_VARIABLE] = "off"
os.environ[SNAPSHOT_SWITCH_VARIABLE] = "off"
os.environ[SCREEN_SWITCH_VARIABLE] = "off"
os.environ[TRAY_SWITCH_VARIABLE] = "off"

GIB = 1024 ** 3


def guid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def identity(n: int, serial: str = None) -> VolumeIdentity:
    return VolumeIdentity(gpt_anchor(guid(n)), serial or f"{n:08X}")


def volume(n: int, letter: str = None, *, fs: str = "NTFS", free: int = 500 * GIB, capacity: int = 1000 * GIB,
           backing: Backing = Backing.PARTITION, pool_member: bool = False, label: str = "",
           serial: str = None, mounts=None) -> VolumeInfo:
    ident = (identity(n, serial) if backing is Backing.PARTITION
             else VolumeIdentity(virtual_anchor(guid(n)), serial or f"{n:08X}"))
    if mounts is None:
        mounts = (f"{letter}:\\",) if letter else ()
    return VolumeInfo(ident, f"\\\\?\\Volume{{{guid(n)}}}\\", tuple(mounts), label, fs, capacity, free, backing,
                      pool_member)


def endpoint(vol: VolumeInfo, path: str = "") -> Endpoint:
    return Endpoint(vol.identity, path)


TEMPORARY_PREFIX = "mirrorhold_test_"
"""The name of every folder the suite works in starts so; its lock file is the folder's name
with ``.lock`` added."""

_swept = False


def remove_tree(path: Path) -> None:
    """Remove ``path`` whole, as far as no process holds what is in it: read-only entries too
    (git's objects, and packages copied from a source tree whose folders are read-only), and
    names Windows' ordinary paths cannot reach (past 260 characters, or ending in a space or
    a period)."""
    def writable_again(function, name, _):
        try:
            os.chmod(name, stat.S_IRWXU)
            function(name)
        except OSError:
            pass

    target = "\\\\?\\" + str(path) if os.name == "nt" else str(path)
    shutil.rmtree(target, onexc=writable_again)


def _sweep(parent: Path) -> None:
    """Remove the suite's folders earlier runs left in ``parent``: each whose lock nobody
    holds, and each with no lock file. A folder whose lock is held belongs to a run still at
    work, and is left; so is one whose lock file Windows refuses to open — its owner, or
    another run's sweep, removing it, or a program holding it — for a later run to try, as
    a look at a run's lock reads that refusal (``records.lock.is_held_elsewhere``)."""
    names = {p.name[:-len(".lock")] if p.name.endswith(".lock") else p.name
             for p in parent.glob(TEMPORARY_PREFIX + "*")}
    for name in sorted(names):
        folder, lock = parent / name, FileLock(parent / (name + ".lock"))
        try:
            if lock.path.exists() and not lock.try_acquire():
                continue
        except PermissionError:
            continue
        if folder.is_dir():
            remove_tree(folder)
        lock.release(remove=not folder.exists())


def temporary_folder() -> Tuple[Path, FileLock]:
    """A new, empty folder in the user's temporary folder, and the lock this process holds
    while it is in use; ``remove_temporary_folder`` takes both once done. The first one a
    process makes removes what earlier runs left (``_sweep``)."""
    global _swept
    parent = Path(tempfile.gettempdir())
    if not _swept:
        _swept = True
        _sweep(parent)
    while True:
        name = TEMPORARY_PREFIX + uuid.uuid4().hex[:8]
        lock = FileLock(parent / (name + ".lock"))
        if not lock.try_acquire():
            continue
        try:
            (parent / name).mkdir()
        except FileExistsError:
            lock.release()
            continue
        return parent / name, lock


def remove_temporary_folder(folder: Path, lock: FileLock) -> None:
    """Remove ``folder`` and let its lock go. What a process still holds stays, with the lock
    file, for the next run to remove."""
    remove_tree(folder)
    lock.release(remove=not folder.exists())


class TempDirTest(unittest.TestCase):
    """A fresh temporary folder per test, removed afterwards (``temporary_folder``).

    The removal is the first cleanup registered, so it runs last: a cleanup a test
    adds (restoring a permission it denied) still finds its files."""

    def setUp(self):
        self.tmp, lock = temporary_folder()
        self.addCleanup(remove_temporary_folder, self.tmp, lock)


def live_activity(layout, job_id: str, *, reads: Sequence[Endpoint] = (), writes: Sequence[Endpoint] = (),
                  kind: str = "run", admitted: bool = True, job_name: str = "Other", marker_id: Optional[str] = None,
                  paused: bool = False):
    """A run or verification as another process publishes it: its status, with its claim,
    beside the store of ``layout``, and its liveness lock held until the returned board
    is closed (the caller closes it)."""
    from mirrorhold.records.admission import Claim
    from mirrorhold.records.record import utc_now
    from mirrorhold.records.status import LiveStatus, Phase, StatusBoard

    status = LiveStatus(uuid.uuid4().hex, os.getpid(), job_id, job_name, "manual", utc_now(), kind=kind,
                        marker_id=marker_id, paused=paused, admitted=admitted,
                        claim=Claim(tuple(reads), tuple(writes)).to_document(),
                        phase=Phase.COPYING if admitted else Phase.WAITING)
    board = StatusBoard(layout, status)
    board.open()
    return board


@contextmanager
def held(path: Path):
    """``path`` held open for reading without sharing anything, as a program that does not
    share deletion holds a file: an antivirus scanning it, a backup reading it (Windows)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(f"{path} could not be held")
    try:
        yield
    finally:
        k32.CloseHandle(handle)


def let_go_after(seconds: float, context) -> threading.Thread:
    """Enter ``context`` now and leave it after ``seconds``, on a thread of its own."""
    entered = threading.Event()

    def hold():
        with context:
            entered.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    entered.wait(5)
    return thread


def other_process(test: unittest.TestCase, body: str) -> subprocess.Popen:
    """A Python process running ``body`` with the project importable; it reports with
    one line of JSON as the last line of its standard output (``result_of``)."""
    script = f"import sys\nsys.path.insert(0, {str(PROJECT)!r})\n" + textwrap.dedent(body)
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    test.addCleanup(lambda: process.poll() is None and process.kill())
    return process


def result_of(test: unittest.TestCase, process: subprocess.Popen) -> dict:
    out, err = process.communicate(timeout=120)
    test.assertEqual(process.returncode, 0, err)
    return json.loads(out.strip().splitlines()[-1])


def job_file_saver(test: unittest.TestCase, store_path: Path, seconds: float) -> subprocess.Popen:
    """Another process saving the job file at ``store_path`` as it holds it, over and
    over for ``seconds``, as a window saving or a command would; it reports how many saves
    it made and how many another process's hold refused."""
    return other_process(test, f"""
        import json, time
        from pathlib import Path
        from mirrorhold.jobs.store import ConfigStore, StoreBusy
        store = ConfigStore(Path({str(store_path)!r}), keep_previous=1)
        contents = store.load().contents
        failures, n = 0, 0
        stop = time.monotonic() + {seconds}
        while time.monotonic() < stop:
            n += 1
            try:
                store.save(contents)
            except StoreBusy:
                failures += 1
        print(json.dumps({{"saves": n, "failures": failures}}))
    """)
