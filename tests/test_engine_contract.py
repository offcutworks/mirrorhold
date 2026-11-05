"""Engine contract: the pinned rclone binary's behaviour that the core relies on or must
design around, measured on temporary folders.

Each test states what it settles about the engine and its verdict: CONFIRMED (rclone's
documentation claims it, and it holds), CONTRADICTED (the documentation claims otherwise;
the test pins what does happen) or MEASURED (no document claimed it). Every test is an observation of an external
program — the upgrade tripwire: a new engine version moves the pin only when this suite
passes.

The pinned binary is required. Without it one test fails, naming where it looked, and
the rest skip; nothing here passes vacuously.

Two process-level facts the suite arranges for:
- The account that runs a session may hold the backup and restore privileges
  enabled; with them, rclone opens files and directories that an access control list
  denies (measured), so the tests about unreadable entries disable both privileges in
  this process first. Children inherit the change; nothing outside the process does.
- Every remote-control server listens on the loopback address with credentials made
  for that one test.

Remotes are the engine's own S3, WebDAV and SFTP servers on the loopback address and its
local and encrypted backends, named in a throwaway configuration whose secrets are
invented (``Loopback``): nothing reaches the internet, and no start names the user's
configuration.
"""
import base64
import ctypes
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path

from support import TempDirTest, remove_temporary_folder, temporary_folder

from mirrorhold.engine.rclone import (GRACEFUL_END_EXIT, PINNED_VERSION, Configuration, RemoteRoot, StopKind,
                                       Transfer, open_engine, pinned_engine_path, run_engine, source_file_unreadable,
                                       start_engine, uncarried)
from mirrorhold.engine.remote import RemoteError, RemoteSession

ENGINE = pinned_engine_path()
INSTALLED = ENGINE.is_file()
WINDOWS = sys.platform == "win32"
MiB = 1024 * 1024
needs_engine = unittest.skipUnless(INSTALLED, f"the pinned engine is not installed at {ENGINE}")
needs_windows = unittest.skipUnless(WINDOWS, "Windows-only behaviour")


# --- helpers -----------------------------------------------------------------------

def engine(*args, timeout=180):
    return run_engine(ENGINE, [str(a) for a in args], timeout=timeout)


def start(*args):
    return start_engine(ENGINE, [str(a) for a in args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace")


def files(root):
    root = Path(root)
    if not root.exists():
        return []
    out = []
    for directory, _dirs, names in os.walk(root):
        for name in names:
            out.append(os.path.relpath(os.path.join(directory, name), root).replace("\\", "/"))
    return sorted(out)


def write(root, rel, data, mtime_ns=None):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def wait_for(predicate, timeout=30.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def partials(root):
    return [f for f in files(root) if f.endswith(".partial")]


def growing_partial(root):
    """A temporary file that already holds some bytes."""
    found = [f for f in partials(root) if (Path(root) / f).stat().st_size > 0]
    return found[0] if found else None


class Rc:
    """A remote-control endpoint on the loopback address with fresh credentials."""

    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.user, self.password = uuid.uuid4().hex, uuid.uuid4().hex

    @property
    def flags(self):
        return ["--rc", "--rc-addr", f"127.0.0.1:{self.port}", "--rc-user", self.user, "--rc-pass", self.password]

    def call(self, method, body=None, timeout=10, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/{method}",
                                         data=json.dumps(body or {}).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def wait_up(self):
        def up():
            try:
                return self.call("core/version", timeout=2)
            except OSError:
                return None
        return wait_for(up, timeout=20)


def refusal(ask, attempts=3):
    """What a remote control answers ``ask``, a request without credentials that closes
    whatever it opens: the HTTP status of its refusal, "served" when it answered the request,
    or None when no attempt got an answer, with every broken attempt. A connection broken
    before any answer — reset, closed or timed out, seen in whole-suite runs and not with the
    test alone — says nothing about the credentials, so it is asked again, as the core asks
    a remote control again whatever leaves a call unanswered; an answer is never asked
    again."""
    broken = []
    for attempt in range(attempts):
        try:
            ask()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, broken
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            broken.append(repr(exc))
            if attempt + 1 < attempts:
                time.sleep(0.5)
            continue
        return "served", broken
    return None, broken


def drop_backup_privileges():
    """Disable SeBackupPrivilege and SeRestorePrivilege in this process's token."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    k32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                             wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for name in ("SeBackupPrivilege", "SeRestorePrivilege"):
            luid = LUID()
            if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                raise ctypes.WinError(ctypes.get_last_error())
            privileges = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 0)))
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
    finally:
        k32.CloseHandle(token)


def current_user():
    return subprocess.run(["whoami"], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


class hold_exclusively:
    """A file held open by this process for reading, sharing nothing."""

    def __init__(self, path):
        self.path = path
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def __enter__(self):
        self.handle = self.k32.CreateFileW(str(self.path), 0x80000000, 0, None, 3, 0x80, None)
        if self.handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return self

    def __exit__(self, *exc):
        self.k32.CloseHandle(self.handle)


def copy_list(test, names, *extra):
    """The run's own invocation: copy an explicit list, with the backup directory inside
    the destination and the JSON log on."""
    listing = test.tmp / "list.txt"
    listing.write_text("".join(n + "\n" for n in names), encoding="utf-8")
    return engine("copy", test.src, test.dst, "--files-from-raw", listing, "--no-traverse", "--backup-dir",
                  test.dst / ".mgmt" / "B1", "--use-json-log", "-v", "--stats", "0", *extra)


def start_copy_list(test, names, *extra):
    listing = test.tmp / "list.txt"
    listing.write_text("".join(n + "\n" for n in names), encoding="utf-8")
    return start("copy", test.src, test.dst, "--files-from-raw", listing, "--no-traverse", "--backup-dir",
                 test.dst / ".mgmt" / "B1", *extra)


def json_log(result):
    return [json.loads(line) for line in result.stderr.splitlines() if line.strip()]


def icacls(path, *args):
    result = subprocess.run(["icacls", str(path), *args], capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise OSError(f"icacls failed: {result.stdout} {result.stderr}")


def device_path(path) -> str:
    """``path`` spelled through the object manager's device namespace,
    ``\\\\?\\GLOBALROOT\\Device\\HarddiskVolume<n>\\...`` — the shape a volume snapshot's path
    has (``...\\HarddiskVolumeShadowCopy<n>\\...``)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(1024)
    text = str(path)
    if not k32.QueryDosDeviceW(text[:2], buffer, 1024):
        raise ctypes.WinError(ctypes.get_last_error())
    return "\\\\?\\GLOBALROOT" + buffer.value + text[2:]


class EngineTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir()

    def seed(self, layout):
        for rel, data in layout.items():
            write(self.src, rel, data)
        result = engine("sync", self.src, self.dst)
        self.assertEqual(result.returncode, 0, result.stderr)


# --- the binary itself -------------------------------------------------------------

class InstalledTest(unittest.TestCase):
    def test_the_pinned_engine_is_installed(self):
        """CONFIRMED: v1.75.1 is the pinned release. The engine is required."""
        self.assertTrue(INSTALLED, f"the pinned engine {PINNED_VERSION} is missing at {ENGINE}")
        self.assertTrue(engine("version").stdout.startswith(f"rclone {PINNED_VERSION}\n"))

    @needs_engine
    def test_an_in_memory_configuration_creates_no_configuration_folder(self):
        """MEASURED: without ``--config ""`` the engine creates %APPDATA%\\rclone on its
        first start; the adapter always passes it, and then nothing is created."""
        folder = Path(os.environ.get("APPDATA", "")) / "rclone"
        if folder.exists():
            self.skipTest(f"{folder} already exists; the absence cannot be measured")
        result = engine("version")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(folder.exists())


# --- versions inside the destination -----------------------------------------------

@needs_engine
class BackupDirTest(EngineTest):
    def test_a_backup_dir_inside_the_destination_needs_a_filter_exclusion(self):
        """CONFIRMED: an overlapping backup directory is a fatal error (exit 7) and
        nothing changes; excluded by a filter rule, it is accepted."""
        self.seed({"a.txt": "a"})
        write(self.src, "a.txt", "changed")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.dst / ".mgmt/versions/B1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("mustn't overlap", result.stderr)
        self.assertEqual((self.dst / "a.txt").read_text(), "a")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.dst / ".mgmt/versions/B1",
                        "--filter", "- /.mgmt/**")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dst / ".mgmt/versions/B1/a.txt").read_text(), "a")

    def test_overwritten_and_deleted_files_move_with_their_hierarchy(self):
        """CONFIRMED: overwritten and deleted files move into the backup directory in
        their original hierarchy; nothing is deleted."""
        self.seed({"keep.txt": "k", "sub/changed.txt": "old", "sub/deep/gone.txt": "gone"})
        write(self.src, "sub/changed.txt", "new content")
        os.remove(self.src / "sub/deep/gone.txt")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.dst / ".mgmt/versions/B1",
                        "--filter", "- /.mgmt/**")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.dst / ".mgmt/versions/B1"), ["sub/changed.txt", "sub/deep/gone.txt"])
        self.assertEqual((self.dst / ".mgmt/versions/B1/sub/changed.txt").read_text(), "old")
        self.assertEqual((self.dst / "sub/changed.txt").read_text(), "new content")
        self.assertEqual(files(self.dst), [".mgmt/versions/B1/sub/changed.txt", ".mgmt/versions/B1/sub/deep/gone.txt",
                                           "keep.txt", "sub/changed.txt"])


# --- temporaries and interruption --------------------------------------------------

@needs_engine
class TemporaryFileTest(EngineTest):
    def slow_copy(self, name="video.bin", size=8 * MiB):
        write(self.src, name, os.urandom(size))
        return start("sync", self.src, self.dst, "--bwlimit", "2M")

    def test_a_copy_is_written_under_a_temporary_name_then_renamed(self):
        """CONFIRMED: the file is written as ``<name>.<8 hex>.partial`` beside its final
        name and renamed when complete; no file appears under its final name before."""
        process = self.slow_copy()
        partial = wait_for(lambda: growing_partial(self.dst))
        self.assertRegex(partial, r"^video\.bin\.[0-9a-f]{8}\.partial$")
        self.assertFalse((self.dst / "video.bin").exists())
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(files(self.dst), ["video.bin"])

    def test_the_copy_is_verified_before_the_rename(self):
        """MEASURED: between local disks the temporary file is checked by size and MD5
        before it is renamed; ``--ignore-checksum`` drops the MD5 check."""
        write(self.src, "a.bin", os.urandom(300000))
        log = engine("sync", self.src, self.dst, "-vv").stderr
        md5 = log.index("a.bin: md5 = ")
        self.assertIn("OK", log[md5:log.index("\n", md5)])
        self.assertLess(md5, log.index("renamed to: a.bin"))
        log = engine("sync", self.src, self.tmp / "dst2", "-vv", "--ignore-checksum").stderr
        self.assertNotIn("md5 =", log)

    def test_the_temporary_name_follows_the_files_name_size_and_time(self):
        """MEASURED: the eight hex digits are fixed by the file's name, size and
        modification time — not by its folders or its content. An interrupted copy of an
        unchanged file is retried under the same temporary name and overwrites the
        leftover; once the file's size or time changes, the leftover is orphaned."""
        path = write(self.src, "video.bin", os.urandom(8 * MiB), mtime_ns=1700000000000000000)

        def attempt(dst):
            for leftover in partials(dst):
                (Path(dst) / leftover).unlink()
            process = start("sync", self.src, dst, "--bwlimit", "2M")
            name = wait_for(lambda: growing_partial(dst))
            process.kill()
            process.communicate(timeout=30)
            return name

        first = attempt(self.dst)
        self.assertIsNotNone(first)
        self.assertEqual(attempt(self.dst), first)
        self.assertEqual(attempt(self.tmp / "elsewhere"), first)
        os.utime(path, ns=(1700000005000000000, 1700000005000000000))
        self.assertNotEqual(attempt(self.dst), first)

    def test_a_killed_copy_leaves_no_final_file_and_the_next_run_converges(self):
        """MEASURED: killing the engine mid-copy leaves the temporary file and nothing
        under the final name; the next run rewrites the same temporary name, completes
        the file, and moves nothing into versions."""
        process = self.slow_copy()
        wait_for(lambda: growing_partial(self.dst))
        process.kill()
        process.communicate(timeout=30)
        self.assertFalse((self.dst / "video.bin").exists())
        self.assertEqual(len(partials(self.dst)), 1)
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "versions")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.dst), ["video.bin"])
        self.assertEqual((self.dst / "video.bin").read_bytes(), (self.src / "video.bin").read_bytes())
        self.assertEqual(files(self.tmp / "versions"), [])

    def test_a_stray_temporary_file_is_treated_as_data(self):
        """MEASURED: a temporary file whose source file has gone is just a
        destination-only file to the engine — it is moved into versions as if it were
        the user's. The run must exclude or remove the engine's own leftovers itself."""
        self.seed({"a.txt": "a"})
        write(self.dst, "video.bin.4f3a2b1c.partial", "half")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "versions")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.tmp / "versions"), ["video.bin.4f3a2b1c.partial"])


# --- links, long and Unicode names, times ------------------------------------------

@needs_engine
@needs_windows
class NamesAndLinksTest(EngineTest):
    def test_links_and_junctions_are_not_followed_and_are_reported(self):
        """CONFIRMED: symbolic links and junctions are skipped with a notice each and
        never followed; ``--skip-links`` silences the notice."""
        write(self.src, "real/file.txt", "real")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.src / "junction"), str(self.src / "real")],
                              capture_output=True, creationflags=flags)
        self.assertEqual(made.returncode, 0)
        links = ["junction"]
        try:
            os.symlink(self.src / "real/file.txt", self.src / "filelink.txt")
            links.append("filelink.txt")
        except OSError:
            pass  # creating a symbolic link needs a privilege this account may lack
        result = engine("sync", self.src, self.dst)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.dst), ["real/file.txt"])
        for link in links:
            self.assertIn(f"{link}: Can't follow symlink without -L/--copy-links", result.stderr)
        quiet = engine("sync", self.src, self.dst, "--skip-links")
        self.assertNotIn("Can't follow symlink", quiet.stderr)

    def test_long_paths_and_unicode_names(self):
        """CONFIRMED: paths past 260 characters and CJK, emoji, NFC and NFD names are
        copied exactly; NFC and NFD spellings stay two files."""
        names = ["\u3057\u3083\u3057\u3093_\u3066\u3059\u3068.jpg", "emoji_\U0001F600.txt", "e\u0301_nfd.txt",
                 "\u00e9_nfc.txt", "space name.txt"]
        for name in names:
            write(self.src, name, name)
        deep = "\\".join(["L" * 60] * 5) + "\\" + "deep_" + "z" * 50 + ".txt"
        write("\\\\?\\" + str(self.src), deep, "deep")
        result = engine("sync", self.src, self.dst)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files("\\\\?\\" + str(self.dst)), files("\\\\?\\" + str(self.src)))
        self.assertGreater(len(str(self.dst)) + len(deep), 300)

    def test_modification_times_keep_their_precision(self):
        """MEASURED: NTFS stores modification times to 100 ns and the copy keeps them
        exactly; an unchanged tree transfers nothing on the next run."""
        write(self.src, "t.txt", "t", mtime_ns=1700000000123456789)
        engine("sync", self.src, self.dst)
        self.assertEqual(os.stat(self.dst / "t.txt").st_mtime_ns, os.stat(self.src / "t.txt").st_mtime_ns)
        self.assertEqual(os.stat(self.dst / "t.txt").st_mtime_ns, 1700000000123456700)
        self.assertIn("There was nothing to transfer", engine("sync", self.src, self.dst, "-v").stderr)

    def test_a_case_only_rename_is_not_propagated(self):
        """MEASURED: renaming a file by case alone changes nothing on the destination —
        the old spelling stays and nothing is transferred or moved."""
        self.seed({"Photo.JPG": "photo"})
        os.rename(self.src / "Photo.JPG", self.src / "photo.jpg")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "versions", "-v")
        self.assertIn("There was nothing to transfer", result.stderr)
        self.assertEqual(os.listdir(self.dst), ["Photo.JPG"])
        self.assertEqual(files(self.tmp / "versions"), [])


# --- errors -------------------------------------------------------------------------

@needs_engine
class ChangingSourceTest(EngineTest):
    def test_a_file_changing_during_its_copy_fails_and_leaves_no_file(self):
        """CONFIRMED: a source file that grows while it is copied fails the copy and
        leaves neither a final file nor a temporary one. The report is "can't copy -
        source file is being updated" or, when the change lands at the end of the read,
        an MD5 mismatch ("corrupted on transfer"); both were measured, so the run must
        not depend on the wording."""
        path = write(self.src, "growing.bin", os.urandom(8 * MiB))
        process = start("sync", self.src, self.dst, "--bwlimit", "2M", "--retries", "1")
        wait_for(lambda: growing_partial(self.dst))
        with open(path, "ab") as handle:
            handle.write(b"more")
        _, err = process.communicate(timeout=60)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue("growing.bin: Failed to copy: can't copy - source file is being updated" in err
                        or "corrupted on transfer" in err, err)
        self.assertEqual(files(self.dst), [])


@needs_engine
@needs_windows
class UnreadableTest(EngineTest):
    @classmethod
    def setUpClass(cls):
        drop_backup_privileges()
        cls.user = current_user()

    def deny(self, path, rights):
        """Deny the user ``rights`` on ``path`` until the test ends. A file is made unreadable
        with ``(RD)``, its data: denying ``(R)`` also denies reading its permissions, which the
        cleanup's ``/reset`` needs where the folder names its owner rather than the user (a
        temporary folder under CPython 3.13 and later)."""
        icacls(path, "/deny", f"{self.user}:{rights}")
        self.addCleanup(icacls, path, "/reset")

    def test_any_read_error_skips_every_deletion(self):
        """CONFIRMED: one file that cannot be read stops the whole delete phase, so an
        unrelated deleted file stays on the destination."""
        self.seed({"secret.txt": "s", "other/gone.txt": "g"})
        os.remove(self.src / "other/gone.txt")
        write(self.src, "secret.txt", "changed and longer")
        self.deny(self.src / "secret.txt", "(RD)")
        result = engine("sync", self.src, self.dst, "--retries", "1", "--low-level-retries", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not deleting files as there were IO errors", result.stderr)
        self.assertTrue((self.dst / "other/gone.txt").exists())

    def test_an_unreadable_changed_file_loses_its_destination_copy(self):
        """MEASURED, and load-bearing: with a backup directory, the destination copy of a
        changed file is moved into it BEFORE the source is opened; when the open fails
        the destination is left without the file. The version survives, but the mirror
        has a hole until a run succeeds — the run must keep such a file out of the
        engine's work rather than let it fail."""
        self.seed({"secret.txt": "old copy"})
        write(self.src, "secret.txt", "changed and longer")
        self.deny(self.src / "secret.txt", "(RD)")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "versions",
                        "--retries", "1", "--low-level-retries", "1")
        self.assertIn("secret.txt: Failed to copy", result.stderr)
        self.assertFalse((self.dst / "secret.txt").exists())
        self.assertEqual((self.tmp / "versions/secret.txt").read_text(), "old copy")

    def test_a_file_held_open_exclusively_behaves_the_same(self):
        """MEASURED: a changed file another process holds without sharing fails with a
        sharing violation, and its destination copy is moved aside the same way."""
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.seed({"held.db": "old copy"})
        write(self.src, "held.db", "changed and longer")
        handle = k32.CreateFileW(str(self.src / "held.db"), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "versions",
                            "--retries", "1", "--low-level-retries", "1")
        finally:
            k32.CloseHandle(handle)
        self.assertIn("being used by another process", result.stderr)
        self.assertFalse((self.dst / "held.db").exists())
        self.assertEqual((self.tmp / "versions/held.db").read_text(), "old copy")

    def test_an_unlistable_directory_reads_as_destination_only_entries(self):
        """MEASURED: a source directory that cannot be listed is an error, and the per-file
        report marks everything beneath it on the destination as destination-only (``-``)
        — the report alone cannot tell a listing failure from deletions. The delete
        phase is skipped, so nothing is removed."""
        self.seed({"lockeddir/inner.txt": "i", "ok.txt": "ok"})
        write(self.dst, "lockeddir/dst_only.txt", "d")
        self.deny(self.src / "lockeddir", "(RD)")
        report = self.tmp / "report.txt"
        result = engine("sync", self.src, self.dst, "--combined", report, "--retries", "1")
        self.assertIn('failed to open directory "lockeddir"', result.stderr)
        lines = report.read_text(encoding="utf-8").splitlines()
        self.assertIn("- lockeddir/inner.txt", lines)
        self.assertIn("- lockeddir/dst_only.txt", lines)
        self.assertEqual(files(self.dst), ["lockeddir/dst_only.txt", "lockeddir/inner.txt", "ok.txt"])

    def test_the_hazard_holds_for_an_explicit_list_and_the_log_names_the_file(self):
        """MEASURED, the run's own invocation: copying an explicit list, a changed file held
        without sharing still has its destination copy moved into the backup directory
        before the open fails; the JSON log reports the failure at level ``error`` with
        the file as its ``object``, and the engine exits 1."""
        self.seed({"held.db": "old copy", "ok.txt": "ok"})
        write(self.src, "held.db", "changed and longer")
        write(self.src, "new.txt", "new")
        with hold_exclusively(self.src / "held.db"):
            result = copy_list(self, ["held.db", "new.txt"], "--retries", "1", "--low-level-retries", "1")
        self.assertEqual(result.returncode, 1)
        failures = [e for e in json_log(result) if e.get("level") == "error" and e.get("object") == "held.db"]
        self.assertTrue(failures and failures[0]["msg"].startswith("Failed to copy"))
        self.assertFalse((self.dst / "held.db").exists())
        self.assertEqual((self.dst / ".mgmt/B1/held.db").read_text(), "old copy")
        self.assertTrue((self.dst / "new.txt").exists())

    def test_the_adapters_way_of_opening_a_file_agrees_with_the_engine(self):
        """MEASURED (what the run's probe rests on): ``open_source_file`` fails on a file held
        without sharing exactly when the engine's open fails, and succeeds once it is not."""
        write(self.src, "held.db", "data")
        with hold_exclusively(self.src / "held.db"):
            self.assertIsNotNone(source_file_unreadable(self.src / "held.db"))
            self.assertEqual(copy_list(self, ["held.db"], "--retries", "1").returncode, 1)
        self.assertIsNone(source_file_unreadable(self.src / "held.db"))
        self.assertEqual(copy_list(self, ["held.db"]).returncode, 0)


# --- the per-file report -----------------------------------------------------------

@needs_engine
class ReportTest(EngineTest):
    def test_the_combined_report_in_a_dry_run(self):
        """CONFIRMED: ``--combined`` writes ``=`` identical, ``+`` source only, ``-``
        destination only, ``*`` different; a dry run changes nothing and creates no
        backup directory."""
        self.seed({"same.txt": "s", "changed.txt": "c", "gone.txt": "g"})
        write(self.src, "changed.txt", "changed now")
        os.remove(self.src / "gone.txt")
        write(self.src, "new.txt", "n")
        before = files(self.dst)
        result = engine("sync", self.src, self.dst, "--dry-run", "--combined", "-", "--backup-dir", self.tmp / "bk")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()),
                         ["* changed.txt", "+ new.txt", "- gone.txt", "= same.txt"])
        self.assertEqual(files(self.dst), before)
        self.assertFalse((self.tmp / "bk").exists())

    def test_a_tracked_rename_shows_as_removal_and_addition(self):
        """MEASURED: with rename tracking in a dry run, the report lists a renamed file as
        ``-`` old and ``+`` new; only the log's "Skipped move to" line names the rename."""
        write(self.src, "a/one.jpg", "1" * 1000, mtime_ns=1600000000000000000)
        engine("sync", self.src, self.dst)
        (self.src / "b").mkdir()
        os.rename(self.src / "a/one.jpg", self.src / "b/one_renamed.jpg")
        result = engine("sync", self.src, self.dst, "--dry-run", "--combined", "-", "--track-renames",
                        "--track-renames-strategy", "modtime")
        self.assertEqual(sorted(result.stdout.splitlines()), ["+ b/one_renamed.jpg", "- a/one.jpg"])
        self.assertIn("a/one.jpg: Skipped move to b/one_renamed.jpg as --dry-run is set", result.stderr)


# --- --max-delete ------------------------------------------------------------------

@needs_engine
class MaxDeleteTest(EngineTest):
    def test_deletions_moved_into_the_backup_dir_count_and_stop_part_way(self):
        """CONFIRMED, with a consequence: deletions turned into moves count toward
        ``--max-delete``; on reaching it the run fails (exit 7) after moving some of them,
        so the flag is no gate — the plan has to be judged before anything runs."""
        self.seed({"k1.txt": "1", "k2.txt": "2", "k3.txt": "3"})
        os.remove(self.src / "k1.txt")
        os.remove(self.src / "k2.txt")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "bk", "--max-delete", "1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("--max-delete threshold reached", result.stderr)
        self.assertEqual(len(files(self.tmp / "bk")), 1)
        self.assertEqual(len(files(self.dst)), 2)

    def test_overwrites_do_not_count(self):
        """CONFIRMED: overwrites moved into the backup directory do not count toward
        ``--max-delete``, even at 0."""
        self.seed({"o1.txt": "1", "o2.txt": "2", "o3.txt": "3"})
        for n in (1, 2, 3):
            write(self.src, f"o{n}.txt", f"changed {n}")
        result = engine("sync", self.src, self.dst, "--backup-dir", self.tmp / "bk", "--max-delete", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.tmp / "bk"), ["o1.txt", "o2.txt", "o3.txt"])


# --- rename tracking ----------------------------------------------------------------

@needs_engine
class RenameTrackingTest(EngineTest):
    def rename_tree(self, base):
        """A synced tree in ``base``, then three renames on its source side."""
        src, dst = base / "src", base / "dst"
        write(src, "album/one.jpg", "1" * 1000, mtime_ns=1600000000000000000)
        write(src, "album/two.jpg", "2" * 2000, mtime_ns=1600000001000000000)
        write(src, "album/three.jpg", "3" * 3000, mtime_ns=1600000002000000000)
        engine("sync", src, dst)
        (src / "sorted").mkdir()
        os.rename(src / "album/one.jpg", src / "sorted/one.jpg")              # moved, same name
        os.rename(src / "album/two.jpg", src / "album/two_renamed.jpg")       # renamed in place
        os.rename(src / "album/three.jpg", src / "sorted/three_renamed.jpg")  # moved and renamed
        return src, dst

    @staticmethod
    def sync_tracking(src, dst, strategy):
        return engine("sync", src, dst, "--track-renames", "--track-renames-strategy", strategy,
                      "--backup-dir", dst / ".mgmt/v/B1", "--filter", "- /.mgmt/**", "-v")

    def test_modtime_strategy_tracks_every_rename_with_a_backup_dir_set(self):
        """CONFIRMED (a backup directory does not switch rename tracking off), and
        MEASURED: the ``modtime`` strategy (size and time) moves all three renamed
        files on the destination; nothing is copied and nothing goes into versions."""
        src, dst = self.rename_tree(self.tmp / "modtime")
        result = self.sync_tracking(src, dst, "modtime")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("There was nothing to transfer", result.stderr)
        self.assertEqual(result.stderr.count("Renamed from"), 3)
        self.assertEqual(files(dst), ["album/two_renamed.jpg", "sorted/one.jpg", "sorted/three_renamed.jpg"])

    def test_leaf_strategies_track_only_moves_that_keep_the_name(self):
        """MEASURED: adding ``leaf`` (the name) tracks only the file moved under its own
        name; the renamed ones are copied again and their old copies go into versions."""
        for strategy in ("leaf", "modtime,leaf"):
            with self.subTest(strategy=strategy):
                src, dst = self.rename_tree(self.tmp / strategy.replace(",", "_"))
                result = self.sync_tracking(src, dst, strategy)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("sorted/one.jpg: Renamed from", result.stderr)
                self.assertEqual(files(dst / ".mgmt/v/B1"), ["album/three.jpg", "album/two.jpg"])

    def test_hash_strategy_tracks_every_rename(self):
        """CONFIRMED in effect: ``hash`` tracks all three; it reads both sides' content to
        do so, which the documentation states and this test does not measure."""
        src, dst = self.rename_tree(self.tmp / "hash")
        result = self.sync_tracking(src, dst, "hash")
        self.assertEqual(result.stderr.count("Renamed from"), 3)
        self.assertEqual(files(dst / ".mgmt/v/B1"), [])


# --- placeholders -------------------------------------------------------------------

@needs_engine
@needs_windows
class PlaceholderSimulationTest(EngineTest):
    def test_an_offline_attribute_does_not_stop_the_engine_reading(self):
        """MEASURED on a simulation only: a file marked offline is read and copied like any
        other, and the engine's listing carries no file attributes — a placeholder can be
        kept out only by the core's own filter. The recall attribute cannot be set from
        user mode, so a real cloud placeholder remains unmeasured."""
        path = write(self.src, "cloud.docx", "cloud")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self.assertTrue(k32.SetFileAttributesW(str(path), 0x1000))
        self.addCleanup(k32.SetFileAttributesW, str(path), 0x80)
        result = engine("sync", self.src, self.dst)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.dst), ["cloud.docx"])
        listing = json.loads(engine("lsjson", "--metadata", self.src).stdout)
        self.assertEqual(set(listing[0]["Metadata"]), {"atime", "btime", "mode", "mtime"})


# --- progress -----------------------------------------------------------------------

@needs_engine
class ProgressTest(EngineTest):
    def test_core_stats_reports_totals_and_the_files_in_flight(self):
        """CONFIRMED: ``core/stats`` gives the run's totals and a ``transferring`` list
        with each file's name, size, bytes done, percentage, speed and ETA."""
        write(self.src, "a.bin", os.urandom(6 * MiB))
        write(self.src, "b.bin", os.urandom(6 * MiB))
        rc = Rc()
        process = start("sync", self.src, self.dst, "--bwlimit", "2M", "--transfers", "1", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            stats = rc.call("core/stats")
        finally:
            process.communicate(timeout=60)
        for key in ("bytes", "totalBytes", "transfers", "totalTransfers", "checks", "listed", "speed", "eta",
                    "errors", "deletes", "renames", "elapsedTime"):
            self.assertIn(key, stats)
        self.assertEqual(stats["totalBytes"], 12 * MiB)
        self.assertEqual(len(stats["transferring"]), 1)
        flight = stats["transferring"][0]
        for key in ("name", "size", "bytes", "percentage", "speed", "eta"):
            self.assertIn(key, flight)
        self.assertEqual(flight["size"], 6 * MiB)

    def test_the_json_log_carries_periodic_stats(self):
        """MEASURED: with ``--use-json-log --stats 1s`` each stats line is a JSON object
        carrying a ``stats`` member of the same shape as ``core/stats``."""
        write(self.src, "a.bin", os.urandom(4 * MiB))
        result = engine("sync", self.src, self.dst, "--bwlimit", "2M", "--use-json-log", "--stats", "1s", "-v")
        records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
        with_stats = [r for r in records if "stats" in r]
        self.assertTrue(with_stats)
        self.assertIn("transferring", with_stats[0]["stats"])
        self.assertIn("totalBytes", with_stats[0]["stats"])


# --- run control --------------------------------------------------------------------

@needs_engine
class RunControlTest(EngineTest):
    def test_the_remote_control_refuses_a_request_without_credentials(self):
        """MEASURED: a loopback remote control started with credentials rejects an
        unauthenticated request. Under load the connection was reset once in a while before
        the refusal arrived; such an attempt is asked again (``refusal``)."""
        write(self.src, "a.bin", os.urandom(4 * MiB))
        rc = Rc()
        # 4 MiB at 64 KiB/s: about a minute, longer than every wait below (20 s for the
        # remote control, 10 s for the request), so the refusal comes from a live engine.
        process = start("sync", self.src, self.dst, "--bwlimit", "64k", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            answer, broken = refusal(lambda: rc.call("core/stats", auth=False))
            self.assertIn(answer, (401, 403), broken)
            self.assertIsNone(process.poll(), "the engine ended before it was asked")
        finally:
            process.kill()
            process.communicate(timeout=60)

    @needs_windows
    def test_suspending_the_process_pauses_and_resuming_completes(self):
        """MEASURED (pause): suspending the engine's process stops data moving; resuming it
        completes the copy intact."""
        ntdll = ctypes.WinDLL("ntdll")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        write(self.src, "a.bin", os.urandom(8 * MiB))
        process = start("sync", self.src, self.dst, "--bwlimit", "2M")
        partial = wait_for(lambda: growing_partial(self.dst))
        handle = k32.OpenProcess(0x0800, False, process.pid)
        try:
            self.assertEqual(ntdll.NtSuspendProcess(handle), 0)
            time.sleep(0.5)
            before = (self.dst / partial).stat().st_size
            time.sleep(2.0)
            self.assertEqual((self.dst / partial).stat().st_size, before)
            self.assertEqual(ntdll.NtResumeProcess(handle), 0)
        finally:
            k32.CloseHandle(handle)
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.dst / "a.bin").read_bytes(), (self.src / "a.bin").read_bytes())

    def test_lowering_the_transfer_limit_at_run_time_stops_gracefully(self):
        """MEASURED (the open question): ``options/set`` with ``MaxTransfer`` 1 and
        ``CutoffMode`` SOFT on a running sync lets the file in flight finish, starts no
        other, and ends with exit 8 and every deletion skipped. CONFIRMED that no
        dedicated command does this; this is the route."""
        for n in range(4):
            write(self.src, f"f{n}.bin", os.urandom(4 * MiB))
        write(self.dst, "gone.txt", "destination only")
        rc = Rc()
        process = start("sync", self.src, self.dst, "--bwlimit", "2M", "--transfers", "1", "--order-by", "name",
                        *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            in_flight = rc.call("core/stats")["transferring"][0]
            self.assertLess(in_flight["bytes"], in_flight["size"], "the file must still be in flight")
            rc.call("options/set", {"main": {"MaxTransfer": 1, "CutoffMode": "SOFT"}})
        finally:
            _, err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 8)
        self.assertIn("max transfer limit reached", err)
        self.assertEqual(files(self.dst), ["f0.bin", "gone.txt"])
        self.assertEqual((self.dst / "f0.bin").stat().st_size, 4 * MiB)

    def test_a_soft_cutoff_given_at_start_finishes_the_file_in_flight(self):
        """CONFIRMED: with ``--cutoff-mode soft`` the limit stops new transfers and the
        one in flight completes, past the limit."""
        for n in range(3):
            write(self.src, f"f{n}.bin", os.urandom(4 * MiB))
        result = engine("sync", self.src, self.dst, "--bwlimit", "4M", "--transfers", "1", "--order-by", "name",
                        "--cutoff-mode", "soft", "--max-transfer", "1M")
        self.assertEqual(result.returncode, 8)
        self.assertEqual(files(self.dst), ["f0.bin"])
        self.assertEqual((self.dst / "f0.bin").stat().st_size, 4 * MiB)

    def test_core_quit_stops_at_once_and_removes_the_temporary_file(self):
        """CONFIRMED (immediate stop): ``core/quit`` ends the run at once with the exit
        code given, leaving neither a final file nor a temporary one."""
        write(self.src, "a.bin", os.urandom(8 * MiB))
        rc = Rc()
        process = start("sync", self.src, self.dst, "--bwlimit", "2M", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            rc.call("core/quit", {"exitCode": 9})
        finally:
            process.communicate(timeout=30)
        self.assertEqual(process.returncode, 9)
        self.assertEqual(files(self.dst), [])

    def test_a_bandwidth_limit_is_not_a_pause(self):
        """CONFIRMED that the limit changes at run time, CONTRADICTED as a pause route:
        at the lowest limit (1 KiB/s) a request to lift it again waits behind the
        transfer's pending chunk and does not answer within ten seconds (measured once
        at 32 s), so the control channel is lost exactly when it is needed."""
        write(self.src, "a.bin", os.urandom(8 * MiB))
        rc = Rc()
        process = start("sync", self.src, self.dst, "--bwlimit", "2M", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            self.assertEqual(rc.call("core/bwlimit", {"rate": "1"})["bytesPerSecond"], 1024)
            with self.assertRaises(OSError):
                rc.call("core/bwlimit", {"rate": "off"}, timeout=10)
        finally:
            process.kill()
            process.communicate(timeout=30)


# --- the run's own invocation: an explicit list -------------------------------------

@needs_engine
class ExplicitListTest(EngineTest):
    """The one invocation a run makes: ``copy --files-from-raw LIST --no-traverse
    --backup-dir <inside the destination>``, controlled as the run controls it."""

    def test_an_explicit_list_lists_neither_side_and_needs_no_filter_for_an_inner_backup_dir(self):
        """MEASURED: with an explicit list and no traversal, the engine addresses each named
        file alone (``listed`` counts the names, not the 51 files beside them); a backup
        directory inside the destination is accepted without a filter rule — the list is
        the filter — and a replaced file moves into it. The log names each copy with its
        size and each move into the backup directory."""
        for i in range(50):
            write(self.src, f"d{i % 5}/f{i}.txt", f"s{i}")
            write(self.dst, f"x{i % 5}/g{i}.txt", f"d{i}")
        write(self.dst, "d0/f0.txt", "old")
        result = copy_list(self, ["d0/f0.txt", "d1/f1.txt"], "--stats", "1m")   # closing statistics on
        self.assertEqual(result.returncode, 0, result.stderr)
        log = json_log(result)
        self.assertLessEqual([e["stats"] for e in log if "stats" in e][-1]["listed"], 2)
        self.assertEqual((self.dst / ".mgmt/B1/d0/f0.txt").read_text(), "old")
        self.assertEqual((self.dst / "d0/f0.txt").read_text(), "s0")
        self.assertEqual(len(files(self.dst)), 53)
        messages = {(e.get("msg"), e.get("object"), e.get("size")) for e in log
                    if e.get("objectType") == "*local.Object"}
        self.assertIn(("Moved (server-side)", "d0/f0.txt", None), messages)
        self.assertIn(("Copied (new)", "d1/f1.txt", 2), messages)

    def test_a_name_missing_from_the_source_is_passed_over(self):
        """CONFIRMED (the manual's ``--files-from`` section): a listed name the source no longer
        holds is skipped without an error, so a file deleted after planning costs nothing."""
        write(self.src, "a.txt", "a")
        result = copy_list(self, ["a.txt", "gone.txt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e for e in json_log(result) if e.get("level") == "error"], [])
        self.assertEqual(files(self.dst), ["a.txt"])

    def test_a_soft_cutoff_set_at_run_time_stops_an_explicit_list_after_the_files_in_flight(self):
        """MEASURED (graceful cancel, as the run asks for it): ``options/set`` with a one-byte
        transfer limit and a soft cutoff lets the file in flight finish, starts no other,
        and exits 8."""
        for n in range(4):
            write(self.src, f"f{n}.bin", os.urandom(4 * MiB))
        rc = Rc()
        process = start_copy_list(self, [f"f{n}.bin" for n in range(4)], "--bwlimit", "2M", "--transfers", "1",
                                  "--order-by", "name", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            rc.call("options/set", {"main": {"MaxTransfer": 1, "CutoffMode": "SOFT"}})
        finally:
            process.communicate(timeout=60)
        self.assertEqual(process.returncode, 8)
        self.assertEqual(files(self.dst), ["f0.bin"])
        self.assertEqual((self.dst / "f0.bin").stat().st_size, 4 * MiB)

    def test_core_quit_stops_an_explicit_list_at_once_and_removes_the_temporary_file(self):
        """MEASURED (immediate cancel, as the run asks for it): ``core/quit`` ends the copy at
        once with the exit status asked for and leaves neither a final nor a temporary file."""
        write(self.src, "a.bin", os.urandom(8 * MiB))
        rc = Rc()
        process = start_copy_list(self, ["a.bin"], "--bwlimit", "2M", *rc.flags)
        try:
            self.assertTrue(rc.wait_up())
            wait_for(lambda: growing_partial(self.dst))
            rc.call("core/quit", {"exitCode": 97})
        finally:
            process.communicate(timeout=30)
        self.assertEqual(process.returncode, 97)
        self.assertEqual(files(self.dst), [])


@needs_engine
@needs_windows
class SnapshotRootTest(EngineTest):
    """The source root a run reading from a volume snapshot hands the engine."""

    def test_the_run_invocation_copies_from_a_device_path_root(self):
        """MEASURED: the run's own invocation copies from a
        source root spelled through the object manager's device namespace — the shape of a
        volume snapshot's path — as from any folder. Spelled here on the temporary folder's
        own volume; on a real snapshot it was measured once by hand, on the development
        machine."""
        write(self.src, "a.txt", "alpha")
        write(self.src, "sub/b.bin", os.urandom(1024))
        listing = self.tmp / "list.txt"
        listing.write_text("a.txt\nsub/b.bin\n", encoding="utf-8")
        result = engine("copy", device_path(self.src), self.dst, "--files-from-raw", listing, "--no-traverse",
                        "--backup-dir", self.dst / ".mgmt" / "B1", "--use-json-log", "-v", "--stats", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files(self.dst), ["a.txt", "sub/b.bin"])
        self.assertEqual((self.dst / "sub/b.bin").read_bytes(), (self.src / "sub/b.bin").read_bytes())


# --- names, through the run's own Transfer ------------------------------------------

CARRIED_NAMES = (
    "plain.jpg",                                           # plain
    "\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8.jpg",            # fullwidth colon
    "\u3057\u3064\u3082\u3093\uff1f.jpg",                  # fullwidth question mark
    "\u307b\u3057\uff0a\u3057\u308b\u3057.jpg",            # fullwidth asterisk
    "\u305f\u3066\uff5c\u305b\u3093.jpg",                  # fullwidth vertical line
    "\u3044\u3093\uff02\u3088\u3046.jpg",                  # fullwidth quotation mark
    "\u3057\u3087\u3046\uff1c\u305f\u3044\uff1e.jpg",      # fullwidth less-than and greater-than signs
    "\u30d5\u30eb\uff3c\u30ca\u30ca\u30e1.jpg",            # fullwidth reverse solidus
    "\u30d5\u30eb\uff0f\u30ca\u30ca\u30e1.jpg",            # fullwidth solidus
    "\u30d5\u30eb\uff0e\u30c6\u30f3.jpg",                  # fullwidth full stop inside a name
    "\u304a\u308f\u308a\u306e\u3066\u3093\uff0e",          # ending in a fullwidth full stop
    "\u304a\u308f\u308a\u306e\u3042\u304d\u2420",          # ending in the symbol for space
    "\u2400control.jpg",                                   # control picture NUL
    "x\u2401y.jpg",                                        # control picture SOH
    "x\u241fy.jpg",                                        # control picture US
    "x\u2421y.jpg",                                        # control picture DEL
    " leading space.jpg",                                  # leading space
    "\u3057\u3083\u3057\u3093\uff1a2019\\IMG_1.jpg",       # a folder named with a fullwidth colon
)
"""Every class of the engine's stand-in characters, as names its default spelling loses."""

TRAILING_NAMES = ("trailing space.jpg ", "trailing period.jpg.", "folder \\in.jpg", "folder.\\in.jpg")
"""Names ending in a space or a period, creatable only through ``\\\\?\\`` paths."""


def transfer(src, dst, names, list_path, **options):
    return Transfer(open_engine(), src, dst, names, backup_dir=Path(dst) / ".mgmt" / "B1", list_path=list_path,
                    **options)


def write_verbatim(root, rel, data):
    """A file written through the extended-length form, so Windows keeps a trailing space or
    period."""
    path = "\\\\?\\" + str(Path(root) / rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


@needs_engine
@needs_windows
class NameTest(EngineTest):
    """Which names the run's own invocation (``Transfer``: ``--local-encoding None``, the
    list's quoting) carries under their own name. Every name is checked as the directory
    lists it, never through a path function that normalises it."""

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.tmp), True)   # trailing spaces and periods included

    def test_every_stand_in_class_is_carried_under_its_own_name(self):
        """MEASURED: the fullwidth stand-ins (U+FF1A and the like, the solidus U+FF0F
        included), a trailing fullwidth full stop or symbol for space, the control pictures
        and U+2421, a leading space and a folder so named all arrive under their own name and
        are logged so; with the engine's default spelling most of them are passed over
        without an error."""
        for name in CARRIED_NAMES:
            write(self.src, name, name)
            self.assertIsNone(uncarried(name), name)
        report = transfer(self.src, self.dst, list(CARRIED_NAMES), self.tmp / "list.files").report()
        self.assertEqual((report.exit_code, report.errors, report.failed), (0, (), {}))
        self.assertEqual(sorted(report.copied), sorted(CARRIED_NAMES))
        self.assertEqual(files(self.dst), sorted(n.replace("\\", "/") for n in CARRIED_NAMES))

    def test_what_no_spelling_carries_is_named_by_uncarried(self):
        """MEASURED: a name holding the quote character U+201B is passed over without an error
        wherever the character stands, and a name of fullwidth full stops alone is read as
        ``..``. The plan leaves every such path out."""
        for name in ("\u201bquote.jpg", "mid\u201bquote.jpg", "\uff0e\uff0e"):
            write(self.src, name, name)
            self.assertIsNotNone(uncarried(name), name)
        quoted = ["\u201bquote.jpg", "mid\u201bquote.jpg"]
        report = transfer(self.src, self.dst, quoted, self.tmp / "list.files").report()
        self.assertEqual((report.exit_code, report.copied, report.failed, files(self.dst)), (0, {}, {}, []))
        report = transfer(self.src, self.dst, ["\uff0e\uff0e"], self.tmp / "list.files").report()
        self.assertEqual((report.copied, files(self.dst)), ({}, []))

    def test_names_ending_in_a_space_or_a_period_are_carried_as_they_are(self):
        """MEASURED, CONTRADICTING an earlier probe that compared names through
        ``os.path.relpath``, which shortens them: files and folders whose names end
        in a space or a period arrive under their own name. It is this tool's own ordinary
        Windows paths that would shorten them, so the plan leaves them out all the same
        (``planning.plan.left_out``)."""
        for name in TRAILING_NAMES:
            write_verbatim(self.src, name, name)
            self.assertIsNone(uncarried(name), name)
        report = transfer(self.src, self.dst, list(TRAILING_NAMES), self.tmp / "list.files").report()
        self.assertEqual((report.exit_code, report.failed, sorted(report.copied)), (0, {}, sorted(TRAILING_NAMES)))
        for name in TRAILING_NAMES:
            with open("\\\\?\\" + str(self.dst / name), "rb") as handle:
                self.assertEqual(handle.read(), name.encode("utf-8"))
        self.assertEqual(sorted(os.listdir("\\\\?\\" + str(self.dst))),
                         ["folder ", "folder.", "trailing period.jpg.", "trailing space.jpg "])

    def test_root_folders_named_with_stand_ins_are_read_and_written_as_named(self):
        """MEASURED: with the default spelling a source root so named copies nothing and a
        destination root so named is written into a sibling folder with U+201B inserted; the
        run's invocation reads and writes the folders as named."""
        source = self.tmp / "\u3082\u3068\uff1a\u3057\u3083\u3057\u3093"
        destination = self.tmp / "\u3072\u304b\u3048\uff1a\u3057\u3083\u3057\u3093"
        write(source, "a.jpg", "a")
        write(source, "sub/b.jpg", "b")
        write(destination, "a.jpg", "old a")
        report = transfer(source, destination, ["a.jpg", "sub\\b.jpg"], self.tmp / "list.files").report()
        self.assertEqual(report.exit_code, 0, report.errors)
        self.assertEqual(files(destination), [".mgmt/B1/a.jpg", "a.jpg", "sub/b.jpg"])
        self.assertEqual((destination / ".mgmt" / "B1" / "a.jpg").read_text(), "old a")
        self.assertEqual([n for n in os.listdir(self.tmp) if "\u201b" in n], [])          # no sibling folder


# --- a destination file of the same size whose time differs --------------------------

@needs_engine
class IdenticalFileTest(EngineTest):
    def test_identical_contents_are_compared_and_only_dated(self):
        """MEASURED: a listed file of the destination's size
        whose time differs is compared by content; when the contents agree the engine logs
        ``Updated modification time in destination``, copies nothing and moves nothing into
        the backup directory, and the destination takes the source's time. One whose
        contents differ is copied and its old copy moved, as any changed file."""
        names = [f"d{i % 4}\\IMG_{i:03d}.JPG" for i in range(40)]
        for i, name in enumerate(names):
            data = b"\xff\xd8\xff" + os.urandom(4093)
            write(self.src, name, data)
            target = write(self.dst, name, data)
            stamp = os.stat(self.src / name).st_mtime_ns + 3600 * 10 ** 9
            os.utime(target, ns=(stamp, stamp))
        write(self.src, "differs.bin", b"new!")
        write(self.dst, "differs.bin", b"old!", mtime_ns=1_600_000_000_000_000_000)
        report = transfer(self.src, self.dst, names + ["differs.bin"], self.tmp / "list.files",
                          sizes={**{n: 4096 for n in names}, "differs.bin": 4}).report()
        self.assertEqual(report.exit_code, 0, report.errors)
        self.assertEqual(report.identical, {n: 4096 for n in names})
        self.assertEqual((report.copied, report.moved_to_backup), ({"differs.bin": 4}, ("differs.bin",)))
        self.assertEqual(report.bytes_identical, 40 * 4096)
        for name in names:
            self.assertEqual(os.stat(self.dst / name).st_mtime_ns, os.stat(self.src / name).st_mtime_ns)
        self.assertEqual(files(self.dst / ".mgmt" / "B1"), ["differs.bin"])


@needs_engine
class ComparisonStopTest(unittest.TestCase):
    """A long comparison, as an adoption over an earlier copy of a photo library makes, and
    how a graceful stop ends it. One tree for the class: its destination times are put back
    an hour before each test."""

    COUNT = 60000
    """Enough files that the comparison outlasts ``core/quit``'s own delay (1.5 s) with
    seconds to spare should this side pause: the engine compares about ten thousand a
    second (measured on the development machine)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.lock = temporary_folder()
        cls.src, cls.dst = cls.tmp / "src", cls.tmp / "dst"
        cls.names = [f"d{i % 100}\\IMG_{i:05d}.JPG" for i in range(cls.COUNT)]
        for i, name in enumerate(cls.names):
            data = b"\xff\xd8\xff" + i.to_bytes(4, "big")
            write(cls.src, name, data)
            write(cls.dst, name, data)

    @classmethod
    def tearDownClass(cls):
        remove_temporary_folder(cls.tmp, cls.lock)

    def setUp(self):
        for name in self.names:
            stamp = os.stat(self.src / name).st_mtime_ns + 3600 * 10 ** 9
            os.utime(self.dst / name, ns=(stamp, stamp))

    def comparing(self):
        """A transfer of the whole tree, once the engine is comparing; and the names it said
        it was comparing."""
        running = transfer(self.src, self.dst, self.names, self.tmp / "list.files")
        stats = wait_for(lambda: (lambda s: s if s is not None and s.checking else None)(running.stats()), timeout=60)
        self.assertIsNotNone(stats, "the engine never reported a comparison")
        return running, stats.checking

    def test_a_soft_cutoff_does_not_end_a_comparison(self):
        """MEASURED (the fact a graceful stop works around): the soft cutoff the adapter's
        graceful stop sets starts no new transfer, but a comparison is not a transfer — the
        engine compares the whole list and ends normally. ``core/stats`` names the files
        being compared, as the list spells them."""
        running, checking = self.comparing()
        self.assertTrue(set(checking) <= set(self.names), checking)
        running.stop_gracefully()
        midway = running.stats()
        report = running.report()
        self.assertTrue(midway is None or midway.identical < self.COUNT)
        self.assertEqual((report.exit_code, len(report.identical), report.copied), (0, self.COUNT, {}))

    def test_a_comparison_counts_the_files_checked_as_it_goes(self):
        """MEASURED: during a comparison no byte moves, and ``core/stats``' count of files
        checked rises, as do the bytes the engine's process has read — figures a run's bound
        on an engine making no progress reads."""
        running, _checking = self.comparing()
        try:
            first = running.stats()
            later = wait_for(lambda: (lambda s: s if s is not None and first is not None and s.checks > first.checks
                                      else None)(running.stats()), timeout=30)
            self.assertIsNotNone(later, "the count of files checked never rose")
            self.assertEqual(later.bytes, 0)
            if sys.platform == "win32":
                self.assertGreater(later.io_bytes, first.io_bytes)
        finally:
            running.stop_now()
            running.report()

    def test_ending_a_graceful_stop_ends_the_comparison(self):
        """MEASURED: ``core/quit`` ends the engine mid-comparison (about 1.5 s after the
        request) with the status asked for; the stop stays graceful and as expected."""
        running, _checking = self.comparing()
        running.stop_gracefully()
        asked = time.monotonic()
        running.end_stop()
        report = running.report()
        self.assertLess(time.monotonic() - asked, 10)
        self.assertEqual((report.exit_code, report.stop), (GRACEFUL_END_EXIT, StopKind.GRACEFUL))
        self.assertTrue(report.ended_as_expected)
        self.assertLess(len(report.identical), self.COUNT)


# --- remotes ---------------------------------------------------------------------------

BACKENDS = ("s3", "webdav", "sftp", "local", "crypt")
"""The remotes the contract is measured on: the pinned engine's own S3, WebDAV and SFTP
servers on the loopback address, a local folder named as a remote, and an encrypted remote
over another local folder."""


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def obscured(text: str) -> str:
    """``text`` as an rclone configuration keeps a password: obscured, which rclone itself
    calls reversible."""
    return engine("obscure", text).stdout.strip()


class Loopback:
    """The engine's own servers on the loopback address, each serving a folder of ``root``,
    and a throwaway configuration naming them, every secret in it invented. Nothing reaches
    the internet, and the user's configuration is never named: every engine start names this
    file, and each server keeps its cache in ``root``."""

    def __init__(self, root: Path):
        self.root = root
        self.served = root / "served"
        for folder in ("s3/bucket", "webdav", "sftp", "local", "cryptbase"):
            (self.served / folder).mkdir(parents=True)
        cache = str(root / "cache")
        self.ports = {name: Rc().port for name in ("s3", "webdav", "sftp")}
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        self.servers = [
            start_engine(ENGINE, ["--cache-dir", cache, "serve", "s3", str(self.served / "s3"), "--addr",
                                  f"127.0.0.1:{self.ports['s3']}", "--auth-key", "loopbackaccess,loopbacksecret"],
                         **quiet),
            start_engine(ENGINE, ["--cache-dir", cache, "serve", "webdav", str(self.served / "webdav"), "--addr",
                                  f"127.0.0.1:{self.ports['webdav']}", "--user", "loopback", "--pass",
                                  "loopback-dav-pass"], **quiet),
            start_engine(ENGINE, ["--cache-dir", cache, "serve", "sftp", str(self.served / "sftp"), "--addr",
                                  f"127.0.0.1:{self.ports['sftp']}", "--user", "loopback", "--pass",
                                  "loopback-sftp-pass"], **quiet),
        ]
        for port in self.ports.values():
            if not wait_for(lambda p=port: _answers(p), timeout=30):
                self.close()
                raise AssertionError(f"a loopback server did not answer on port {port}")
        self.path = root / "throwaway.conf"
        self.path.write_text(
            f"[s3t]\ntype = s3\nprovider = Rclone\nendpoint = http://127.0.0.1:{self.ports['s3']}/\n"
            "access_key_id = loopbackaccess\nsecret_access_key = loopbacksecret\n\n"
            f"[dav]\ntype = webdav\nurl = http://127.0.0.1:{self.ports['webdav']}/\nvendor = rclone\n"
            f"user = loopback\npass = {obscured('loopback-dav-pass')}\n\n"
            f"[sftpt]\ntype = sftp\nhost = 127.0.0.1\nport = {self.ports['sftp']}\nuser = loopback\n"
            f"pass = {obscured('loopback-sftp-pass')}\nshell_type = none\nknown_hosts_file = none\n\n"
            "[loc]\ntype = local\n\n"
            f"[enc]\ntype = crypt\nremote = loc:{(self.served / 'cryptbase').as_posix()}\n"
            f"password = {obscured('loopback-crypt-pass')}\n", encoding="utf-8")
        self.configuration = Configuration(self.path)

    def root_of(self, backend: str, folder: str) -> RemoteRoot:
        """A folder of the named backend's remote, empty until written."""
        return {"s3": RemoteRoot("s3t", f"bucket/{folder}"), "webdav": RemoteRoot("dav", folder),
                "sftp": RemoteRoot("sftpt", folder),
                "local": RemoteRoot("loc", (self.served / "local" / folder).as_posix()),
                "crypt": RemoteRoot("enc", folder)}[backend]

    def put(self, source: Path, root: RemoteRoot) -> None:
        """Copy a local folder's files onto a remote folder, their times kept."""
        result = run_engine(ENGINE, ["copy", str(source), root.fs], configuration=self.configuration, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr[-2000:])

    def close(self) -> None:
        for server in self.servers:
            server.kill()
        for server in self.servers:
            try:
                server.wait(30)
            except subprocess.TimeoutExpired:
                pass


@needs_engine
class RemoteTest(unittest.TestCase):
    """One set of loopback servers and one session for the class; each test works in
    folders of its own, on every backend in turn."""

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.lock = temporary_folder()
        try:
            cls.loopback = Loopback(cls.tmp / "loopback")
            cls.session = RemoteSession(open_engine(), cls.loopback.configuration)
        except BaseException:
            remove_temporary_folder(cls.tmp, cls.lock)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.loopback.close()
        remove_temporary_folder(cls.tmp, cls.lock)

    def setUp(self):
        self.case = uuid.uuid4().hex[:8]
        self.local = self.tmp / "local" / self.case
        self.local.mkdir(parents=True)

    def roots(self):
        """Each backend with a folder of its own for this test."""
        return [(backend, self.loopback.root_of(backend, self.case)) for backend in BACKENDS]


class RemoteFeaturesTest(RemoteTest):
    def test_each_backend_reports_what_it_can_do(self):
        """MEASURED: ``operations/fsinfo`` reports per remote whether it moves and copies a
        file within itself, its time precision and the fingerprints it keeps. S3 copies but
        does not move within itself (a move is a copy and a delete), keeps no empty folder,
        dates to the nanosecond and keeps MD5; WebDAV and SFTP as the engine serves them move,
        date to the second and keep no fingerprint; an encrypted remote keeps none and is the
        only kind that answers the ``encode`` command. Every one moves a file within itself
        one way or the other."""
        expected = {  # moves, copies, precision, hashes (None: MD5 among others), empty folders, encrypted
            "s3": (False, True, 1, ("md5",), False, False),
            "webdav": (True, True, 10 ** 9, (), True, False),
            "sftp": (True, False, 10 ** 9, (), True, False),
            "local": (True, False, 100, None, True, False),
            "crypt": (True, False, 100, (), True, True),
        }
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                features = self.session.features(root)
                moves, copies, precision, hashes, empty, encrypted = expected[backend]
                self.assertEqual((features.moves, features.copies, features.precision_ns, features.keeps_empty_folders,
                                  features.encrypted), (moves, copies, precision, empty, encrypted))
                if hashes is None:
                    self.assertIn("md5", features.hashes)
                else:
                    self.assertEqual(features.hashes, hashes)
                self.assertTrue(features.moves_within and features.keeps_times)

    def test_the_configurations_remotes_are_named_as_the_session_opens_it(self):
        """CONFIRMED: ``config/listremotes`` names the configuration's remotes, which the
        session reads as it opens the configuration."""
        self.assertEqual(set(self.session.remotes), {"s3t", "dav", "sftpt", "loc", "enc"})

    def test_free_space_is_what_the_service_reports(self):
        """MEASURED: SFTP reports its free space and total; S3 reports none, and WebDAV, as
        the engine serves it, answers with no figure: neither can be judged."""
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.session.make_folder(root)
                space = self.session.space(root)
                if backend in ("s3", "webdav"):
                    self.assertIsNone(space)
                elif backend == "sftp":
                    self.assertTrue(space.free > 0 and space.total >= space.free, space)


class RemoteFilesTest(RemoteTest):
    def test_a_small_file_is_written_whole_read_in_part_and_replaced(self):
        """MEASURED: a file written through the remote control lands under its name in a
        folder made for it, replaces one there, and is read back whole or by its leading
        bytes alone (a byte range). A file that is not there reads as None."""
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.session.write(root, ".mirrorhold\\marker.json", b'{"first": true}\n')
                self.session.write(root, ".mirrorhold\\marker.json", b'{"second": true}\n')
                self.assertEqual(self.session.read(root, ".mirrorhold\\marker.json"), b'{"second": true}\n')
                self.assertEqual(self.session.read(root, ".mirrorhold\\marker.json", limit=4), b'{"se')
                self.assertIsNone(self.session.read(root, ".mirrorhold\\none.json"))
                self.assertIsNone(self.session.stat(root, ".mirrorhold\\none.json"))
                self.assertEqual(self.session.stat(root, ".mirrorhold\\marker.json").size, 17)

    def test_a_listing_gives_sizes_times_and_the_fingerprints_the_service_keeps(self):
        """MEASURED: a listing, folder by folder, gives every entry's path below the folder
        listed, a file's size and its time to the service's precision, and on S3 the MD5 of
        its content; the listing's progress is told after each folder. A folder that is not
        there lists as None, but on S3, which has no folders of its own, as empty."""
        stamps = {"a\\one.txt": 1_600_000_000_123_456_700, "a\\b\\two.bin": 1_600_000_100_987_654_300,
                  "three.txt": 1_600_000_200_000_000_000}
        for rel, stamp in stamps.items():
            write(self.local, rel, rel * 3, mtime_ns=stamp)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.loopback.put(self.local, root)
                told = []
                listed = self.session.list_tree(root, hashes=True, progress=told.append)
                found = {e.path: e for e in listed if not e.is_dir}
                self.assertEqual(set(found), set(stamps))
                precision = self.session.features(root).precision_ns
                for rel, stamp in stamps.items():
                    self.assertEqual(found[rel].size, len((rel * 3).encode()))
                    self.assertLess(abs(found[rel].mtime_ns - stamp), max(precision, 100))
                if backend == "s3":
                    self.assertEqual(found["a\\one.txt"].hashes["md5"],
                                     hashlib.md5(("a\\one.txt" * 3).encode()).hexdigest())
                self.assertGreaterEqual(len(told), 4)                     # three folders, and the end
                self.assertEqual({e.path for e in listed if e.is_dir}, {"a", "a\\b"})
                self.assertEqual(self.session.list_tree(root, "missing"), () if backend == "s3" else None)

    def test_a_cancel_ends_a_listing_between_two_folders(self):
        """The adapter's own contract: an exception raised by the listing's progress ends it
        and reaches the caller."""
        write(self.local, "a\\one.txt", "1")
        write(self.local, "b\\two.txt", "2")
        root = self.loopback.root_of("webdav", self.case)
        self.loopback.put(self.local, root)

        def cancel(seen):
            if seen:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.session.list_tree(root, progress=cancel)

    def test_files_and_folders_move_within_the_remote_keeping_their_times(self):
        """MEASURED: a file moved, and a folder moved whole into a versions batch, keep
        their modification times on every backend — S3's moves are copies and deletes on the
        service, which keep them too; nothing stays at the old place."""
        stamps = {"x\\one.txt": 1_600_000_000_000_000_000, "x\\two.bin": 1_600_000_100_000_000_000,
                  "solo.txt": 1_600_000_200_000_000_000}
        for rel, stamp in stamps.items():
            write(self.local, rel, rel, mtime_ns=stamp)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.loopback.put(self.local, root)
                self.session.move_file(root, "solo.txt", "renamed\\solo.txt")
                self.session.move_folder(root, "x", ".mirrorhold\\versions\\B1\\x")
                found = {e.path: e.mtime_ns for e in self.session.list_tree(root) if not e.is_dir}
                self.assertEqual(set(found), {"renamed\\solo.txt", ".mirrorhold\\versions\\B1\\x\\one.txt",
                                              ".mirrorhold\\versions\\B1\\x\\two.bin"})
                self.assertEqual(found["renamed\\solo.txt"] // 10 ** 9, stamps["solo.txt"] // 10 ** 9)
                self.assertEqual(found[".mirrorhold\\versions\\B1\\x\\two.bin"] // 10 ** 9,
                                 stamps["x\\two.bin"] // 10 ** 9)
                self.assertIsNone(self.session.stat(root, "x\\one.txt"))

    def test_a_folder_moved_into_one_already_there_leaves_nothing_behind(self):
        """MEASURED: a folder moved into a folder already at its new place is moved file by
        file, its files merged; the engine leaves the emptied folder itself (SFTP, WebDAV, a
        local remote), which the session removes."""
        for rel in ("x\\one.txt", "x\\sub\\two.txt", ".mirrorhold\\versions\\B1\\x\\other.txt"):
            write(self.local, rel, rel)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.loopback.put(self.local, root)
                self.session.move_folder(root, "x", ".mirrorhold\\versions\\B1\\x")
                self.assertIsNone(self.session.stat(root, "x"))
                found = {e.path for e in self.session.list_tree(root) if not e.is_dir}
                self.assertEqual(found, {".mirrorhold\\versions\\B1\\x\\one.txt",
                                         ".mirrorhold\\versions\\B1\\x\\sub\\two.txt",
                                         ".mirrorhold\\versions\\B1\\x\\other.txt"})

    def test_a_file_and_a_versions_batch_are_deleted_and_nothing_beside_them(self):
        """MEASURED: deleting a file removes that file, and deleting a folder removes it with
        everything in it and nothing beside it; deleting what is not there says so."""
        for rel in ("keep.txt", ".mirrorhold\\versions\\B1\\old.txt", ".mirrorhold\\versions\\B2\\new.txt",
                    "leftover.txt"):
            write(self.local, rel, rel)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.loopback.put(self.local, root)
                self.assertTrue(self.session.delete_file(root, "leftover.txt"))
                self.assertFalse(self.session.delete_file(root, "leftover.txt"))
                self.assertTrue(self.session.delete_folder(root, ".mirrorhold\\versions\\B1"))
                self.assertFalse(self.session.delete_folder(root, ".mirrorhold\\versions\\B1"))
                found = {e.path for e in self.session.list_tree(root) if not e.is_dir}
                self.assertEqual(found, {"keep.txt", ".mirrorhold\\versions\\B2\\new.txt"})


class RemoteTransferTest(RemoteTest):
    def transfer(self, root: RemoteRoot, names, batch="B1", **options):
        return Transfer(open_engine(), self.local, root, names,
                        backup_dir=root.joined(f".mirrorhold\\versions\\{batch}"),
                        list_path=self.tmp / f"{self.case}-{uuid.uuid4().hex[:6]}.files",
                        configuration=self.loopback.configuration, **options).report()

    def test_a_listed_copy_onto_a_remote_moves_what_it_replaces_into_the_batch_there(self):
        """MEASURED: the explicit-list copy onto a remote folder copies the listed files and
        moves each destination file it replaces into the batch on that remote, keeping its
        time; the adapter's report counts both, whether the remote moves the file or copies
        and deletes it (S3)."""
        names = ["a\\one.txt", "two.txt", "three.txt"]
        for name in names:
            write(self.local, name, name * 2, mtime_ns=1_600_000_000_000_000_000)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                first = self.transfer(root, names)
                self.assertEqual((first.exit_code, set(first.copied), first.moved_to_backup), (0, set(names), ()),
                                 first.errors)
                write(self.local, "a\\one.txt", "a longer one", mtime_ns=1_700_000_000_000_000_000)
                second = self.transfer(root, ["a\\one.txt", "two.txt"])
                self.assertEqual(second.exit_code, 0, second.errors)
                self.assertEqual((set(second.copied), second.moved_to_backup), ({"a\\one.txt"}, ("a\\one.txt",)))
                batch = {e.path: e for e in self.session.list_tree(root.joined(".mirrorhold\\versions\\B1"))
                         if not e.is_dir}
                self.assertEqual(set(batch), {"a\\one.txt"})
                self.assertEqual(batch["a\\one.txt"].mtime_ns // 10 ** 9, 1_600_000_000)
                self.assertEqual(self.session.read(root, "a\\one.txt"), b"a longer one")
                write(self.local, "a\\one.txt", "a\\one.txt" * 2, mtime_ns=1_600_000_000_000_000_000)

    def test_a_file_of_the_same_size_and_another_time_is_compared_by_fingerprint_where_one_is_kept(self):
        """MEASURED: where both sides keep a fingerprint the engine can compare (S3's MD5,
        and a local remote's), a listed file of the destination's size whose time differs is
        compared by it, nothing downloaded: its time alone is set. Where the service keeps
        none (WebDAV and SFTP as served here, an encrypted remote), the file is copied again
        and its previous copy moves into the batch."""
        write(self.local, "same.bin", b"0123456789", mtime_ns=1_600_000_000_000_000_000)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                self.assertEqual(self.transfer(root, ["same.bin"]).exit_code, 0)
                os.utime(self.local / "same.bin", ns=(1_700_000_000_000_000_000,) * 2)
                report = self.transfer(root, ["same.bin"], batch="B2", sizes={"same.bin": 10})
                self.assertEqual(report.exit_code, 0, report.errors)
                if backend in ("s3", "local"):
                    self.assertEqual((report.identical, report.copied, report.moved_to_backup),
                                     ({"same.bin": 10}, {}, ()))
                else:
                    self.assertEqual((report.identical, set(report.copied), report.moved_to_backup),
                                     ({}, {"same.bin"}, ("same.bin",)))
                self.assertEqual(self.session.stat(root, "same.bin").mtime_ns // 10 ** 9, 1_700_000_000)
                os.utime(self.local / "same.bin", ns=(1_600_000_000_000_000_000,) * 2)

    def test_without_no_traverse_a_copy_lists_only_the_folders_its_list_names(self):
        """MEASURED (why a copy onto a remote leaves ``--no-traverse`` out): with it, the
        engine looks each listed file up on the destination alone, two requests a file;
        without it, it lists the folders the list names and nothing more."""
        for d in range(4):
            for f in range(25):
                write(self.local, f"d{d}\\old{f:02d}.txt", "x" * (d + f + 1))
        new = [f"d{d}\\new{f}.txt" for d in range(2) for f in range(10)]
        counts = {}
        for mode in ("no-traverse", "traverse"):
            root = self.loopback.root_of("s3", f"{self.case}-{mode}")
            self.loopback.put(self.local, root)
            for name in new:
                write(self.local, name, name)
            list_path = self.tmp / f"{self.case}-{mode}.files"
            list_path.write_text("".join(n.replace("\\", "/") + "\n" for n in new), encoding="utf-8", newline="\n")
            args = ["copy", str(self.local), root.fs, "--files-from-raw", str(list_path), "--local-encoding", "None",
                    "--dump", "headers", *(("--no-traverse",) if mode == "no-traverse" else ())]
            result = run_engine(ENGINE, args, configuration=self.loopback.configuration, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            requests = [line.split(" : ", 1)[1].split(" ", 1)[0] for line in result.stderr.splitlines()
                        if line.endswith(" HTTP/1.1") and " : " in line]
            counts[mode] = {m: requests.count(m) for m in set(requests)}
            for name in new:
                (self.local / name).unlink()
        self.assertGreaterEqual(counts["no-traverse"].get("HEAD", 0), 2 * len(new), counts)
        self.assertEqual(counts["no-traverse"].get("GET", 0), 0, counts)
        self.assertLessEqual(counts["traverse"].get("GET", 0), 4, counts)
        self.assertLess(counts["traverse"].get("HEAD", 0), counts["no-traverse"]["HEAD"], counts)

    @needs_windows
    def test_an_upload_moves_the_engines_read_count_as_it_sends(self):
        """MEASURED (the bound on an engine making no progress): sending to a remote moves
        none of the engine process's I/O counts by itself, but the engine reads its source
        as it sends, so the bytes its process has read rise while an upload goes on, as do
        the bytes it reports copied."""
        write(self.local, "large.bin", os.urandom(24 * MiB))
        root = self.loopback.root_of("webdav", self.case)
        running = Transfer(open_engine(), self.local, root, ["large.bin"],
                           backup_dir=root.joined(".mirrorhold\\versions\\B1"), list_path=self.tmp / "large.files",
                           configuration=self.loopback.configuration, bandwidth_limit="4M")
        try:
            first = wait_for(lambda: (lambda s: s if s is not None and s.bytes > 0 else None)(running.stats()),
                             timeout=30)
            self.assertIsNotNone(first, "the upload never began")
            time.sleep(2)
            later = running.stats()
            self.assertGreater(later.bytes, first.bytes)
            self.assertGreater(later.io_bytes, first.io_bytes)
        finally:
            running.stop_now()
            running.report()


@needs_engine
class RemoteConfigurationTest(TempDirTest):
    """The configuration a remote is reached through: named, never searched for, unlocked
    only by its password command."""

    def setUp(self):
        super().setUp()
        (self.tmp / "store").mkdir()
        self.path = self.tmp / "throwaway.conf"
        self.path.write_text("[loc]\ntype = local\n", encoding="utf-8")
        script = self.tmp / "pass word" / "unlock.py"
        script.parent.mkdir()
        script.write_text("import sys\nsys.stdout.write('invented-config-pass\\n')\n", encoding="utf-8")
        self.unlock = (sys.executable, str(script))
        result = run_engine(ENGINE, ["config", "encryption", "set"],
                            configuration=Configuration(self.path, self.unlock), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("type = local", self.path.read_text(encoding="utf-8"))

    def test_a_locked_configuration_ends_the_session_at_once_with_its_reason(self):
        """MEASURED: an encrypted configuration named without its unlock ends the engine
        before it answers — standard input closed, ``--ask-password=false`` — and the session
        says why within seconds; nothing waits."""
        asked = time.monotonic()
        with self.assertRaises(RemoteError) as ctx:
            RemoteSession(open_engine(), Configuration(self.path))
        self.assertLess(time.monotonic() - asked, 15)
        self.assertIn(str(self.path), str(ctx.exception))
        self.assertIn("password", str(ctx.exception).lower())

    def test_its_password_command_unlocks_it_from_a_folder_whose_name_holds_a_space(self):
        """MEASURED: a password command whose script lies in a folder whose name holds a
        space, quoted as the engine's space-separated list reads it, unlocks the
        configuration; an engine that changes no remote's settings leaves the file as it
        was."""
        before = self.path.read_bytes()
        root = RemoteRoot("loc", (self.tmp / "store").as_posix())
        with RemoteSession(open_engine(), Configuration(self.path, self.unlock)) as session:
            session.write(root, "a.txt", b"unlocked")
            self.assertEqual(session.read(root, "a.txt"), b"unlocked")
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_sessions_password_is_given_to_the_engine_only_as_a_hash(self):
        """MEASURED: the engine takes its remote control's credentials from a password file
        holding the password's SHA-1 hash (``--rc-htpasswd``), so the password is in neither
        its command line nor its environment, nor its password command's; it answers a
        request carrying them and refuses one that does not."""
        with RemoteSession(open_engine(), Configuration(self.path, self.unlock)) as session:
            command = " ".join(session._process.args)
            self.assertNotIn(session._control.password, command)
            self.assertIn("--rc-htpasswd", command)
            self.assertIsNone(session.read(RemoteRoot("loc", (self.tmp / "store").as_posix()), "none.txt"))
            answer, broken = refusal(lambda: urllib.request.urlopen(
                urllib.request.Request(session._control.url("rc/noopauth"), data=b"{}"), timeout=10).close())
            self.assertIn(answer, (401, 403), broken)
        self.assertFalse(session._control.password_file.exists())


class RemoteStorageTest(RemoteTest):
    """The destination-storage seam's remote kind (``storage.remote.RemoteStorage``) on every
    backend: the operations the run, the cleanup, the verification and the repair reach a
    destination through, and the policy code above the seam working on a remote as on a
    drive. The drive kind's own tests are ``test_storage``."""

    def storage(self, root: RemoteRoot):
        from mirrorhold.storage.remote import RemoteStorage
        return RemoteStorage(self.session, root)

    def test_the_marker_and_a_shared_record_are_written_and_read_through_the_seam(self):
        """MEASURED: the marker and a shared destination's record land in the management
        folder on the remote and read back; a marker of another job is a mismatch, one that
        is not a marker is unreadable, none is missing."""
        from mirrorhold.endpoints.marker import Marker, MarkerState, marker_check, write_marker
        from mirrorhold.endpoints.shared import SharedRecord, read_shared, write_shared
        job, marker = "a" * 32, "b" * 32
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.assertIs(marker_check(storage, job, marker)[0], MarkerState.MISSING)
                self.assertEqual(write_marker(storage, Marker(job, marker, shared=True)),
                                 root.joined(".mirrorhold\\marker.json"))
                state, found = marker_check(storage, job, marker)
                self.assertEqual((state, found), (MarkerState.MATCH, Marker(job, marker, shared=True)))
                self.assertIs(marker_check(storage, "c" * 32, marker)[0], MarkerState.MISMATCH)
                record = SharedRecord(job, marker, ("Photos",), ("Photos\\old",))
                write_shared(storage, record)
                self.assertEqual(read_shared(storage, job, marker), record)
                storage.write_file(".mirrorhold\\marker.json", b"{not json")
                self.assertIs(marker_check(storage, job, marker)[0], MarkerState.UNREADABLE)

    def test_a_marker_whose_read_the_engine_breaks_off_is_the_remote_not_answering_though_it_answers_next(self):
        """The marker's content cut short as it is served raises http.client's IncompleteRead;
        the engine answers for the marker's entry right after, and the read is still told as
        the remote not answering, never as a marker that cannot be read."""
        import http.client
        from unittest import mock
        from mirrorhold.endpoints.marker import Marker, marker_check, write_marker
        from mirrorhold.storage.destination import StorageUnanswered
        job, marker = "a" * 32, "b" * 32
        _backend, root = self.roots()[0]
        storage = self.storage(root)
        write_marker(storage, Marker(job, marker))
        request = self.session._control.request

        def broken_off(path, **options):
            if path.startswith("[") and path.endswith("marker.json"):        # the served file, not a method
                raise http.client.IncompleteRead(b'{"format"')
            return request(path, **options)

        with mock.patch.object(self.session._control, "request", side_effect=broken_off):
            self.assertIsNotNone(storage.entry(".mirrorhold\\marker.json"))
            with self.assertRaises(StorageUnanswered) as raised:
                marker_check(storage, job, marker)
        self.assertIn("the sync engine did not answer", raised.exception.strerror)

    def test_a_listing_through_the_seam_is_a_drives_listing_of_the_same_files(self):
        """MEASURED: a remote's listing through the seam keeps the files and folders a
        drive's listing of the same tree keeps, with their sizes and their times to the
        remote's precision; the management folder and what a scope leaves out are never
        listed. A folder not there lists as absent where the service keeps folders, and as
        empty where it does not (S3); either way it holds nothing."""
        from mirrorhold.planning.listing import Scope, Side, list_tree
        write(self.local, "keep\\a.txt", "aaa", mtime_ns=1_600_000_000_000_000_000)
        write(self.local, "keep\\deep\\b.txt", "bbbb", mtime_ns=1_600_000_001_000_000_000)
        write(self.local, "other.txt", "o", mtime_ns=1_600_000_002_000_000_000)
        write(self.local, ".mirrorhold\\marker.json", "{}")
        drive = list_tree(self.local, Side.DESTINATION, drive_root=False)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.assertTrue(storage.occupancy(drive_root=False).holds_nothing)
                self.loopback.put(self.local, root)
                listing = storage.list_tree(drive_root=False)
                self.assertTrue(listing.present)
                self.assertEqual(set(listing.files), set(drive.files))
                self.assertEqual(set(listing.dirs), set(drive.dirs))
                window = max(storage.time_window_ns, 1)
                for key, entry in drive.files.items():
                    self.assertEqual(listing.files[key].size, entry.size)
                    self.assertLess(abs(listing.files[key].mtime_ns - entry.mtime_ns), window, key)
                scoped = storage.list_tree(drive_root=False, scope=Scope(frozenset({"keep"})))
                self.assertEqual(set(scoped.files), {"keep\\a.txt", "keep\\deep\\b.txt"})
                self.assertEqual(storage.occupancy(drive_root=False).value, "occupied")
                missing = self.storage(root.joined("none")).list_tree(drive_root=False)
                self.assertTrue(missing.holds_nothing)

    def test_files_are_read_by_their_head_and_as_a_stream_in_chunks(self):
        """MEASURED: a file's leading bytes are read alone, and a file larger than one read
        streams in chunks to its whole content; a file not there is FileNotFoundError."""
        data = os.urandom(3 * MiB + 17)
        write(self.local, "big.bin", data)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.loopback.put(self.local, root)
                self.assertEqual(storage.read_head("big.bin", 16), data[:16])
                read = b""
                with storage.open_file("big.bin") as stream:
                    while True:
                        chunk = stream.read(MiB)
                        if not chunk:
                            break
                        read += chunk
                self.assertEqual(hashlib.sha256(read).digest(), hashlib.sha256(data).digest())
                for missing in (storage.read_file, lambda r: storage.read_head(r, 4), storage.open_file):
                    with self.assertRaises(FileNotFoundError):
                        missing("none.bin")

    def test_a_versions_batch_moves_files_and_folders_in_and_back_keeping_their_times(self):
        """MEASURED: a run's versions batch on a remote takes a folder whole and a file in,
        each keeping its time, gives a file back to its place, and is measured and deleted
        whole; an emptied tree of folders is removed and one holding a file is not."""
        from datetime import datetime, timezone
        from mirrorhold.run.apply import VersionsBatch
        write(self.local, "d\\x.txt", "xx", mtime_ns=1_600_000_000_000_000_000)
        write(self.local, "y.txt", "yyy", mtime_ns=1_600_000_005_000_000_000)
        write(self.local, "held\\f.txt", "f")
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.loopback.put(self.local, root)
                batch = VersionsBatch(storage, datetime(2026, 10, 29, 10, tzinfo=timezone.utc))
                self.assertFalse(batch.exists)
                batch.move_in("d")
                batch.move_in("y.txt")
                self.assertTrue(batch.exists and batch.holds("y.txt") and batch.holds("d\\x.txt"))
                self.assertFalse(storage.exists("y.txt") or storage.exists("d\\x.txt"))
                self.assertEqual(storage.entry(batch.inside("d\\x.txt")).mtime_ns // 10 ** 9, 1_600_000_000)
                batch.move_back("y.txt")
                self.assertEqual(storage.entry("y.txt").mtime_ns // 10 ** 9, 1_600_000_005)
                self.assertEqual(storage.measure(batch.folder), (1, 2))
                self.assertFalse(storage.remove_empty_tree("held"))
                self.assertTrue(storage.exists("held\\f.txt"))
                storage.remove_file("held\\f.txt")
                with self.assertRaises(FileNotFoundError):
                    storage.remove_file("held\\f.txt")
                storage.make_folder("held\\inner")
                self.assertTrue(storage.remove_empty_tree("held"), "a tree of folders alone is removed")
                self.assertFalse(storage.is_folder("held"))
                self.assertIsNone(storage.delete_tree(batch.folder))
                self.assertFalse(storage.exists(batch.inside("d\\x.txt")))
                self.assertEqual(batch.path, root.joined(f".mirrorhold\\versions\\{batch.relative.name}"))

    def test_the_version_cleanup_and_the_recognition_of_what_reappeared_work_on_a_remote(self):
        """MEASURED: the version cleanup deletes a remote's expired batch, fenced by its marker,
        and keeps a recent one; a file equal in path, size and time to its copy in the newest
        batch is recognised as one that reappeared after its move into versions."""
        from datetime import datetime, timezone
        from mirrorhold.endpoints.marker import Marker, write_marker
        from mirrorhold.maintenance.cleanup import clean_versions, plan_cleanup
        from mirrorhold.planning.listing import Entry
        from mirrorhold.planning.plan import PairPlan
        from mirrorhold.run.put_back import newest_batch, put_back
        job, marker = "a" * 32, "b" * 32
        write(self.local, ".mirrorhold\\versions\\2026-01-01T00-00-00Z\\old.txt", "old")
        write(self.local, ".mirrorhold\\versions\\2026-10-28T00-00-00Z\\back.txt", "back",
              mtime_ns=1_600_000_000_000_000_000)
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.loopback.put(self.local, root)
                self.assertEqual(clean_versions(storage, 30, job_id=job, marker_id=marker, now=now).batches, (),
                                 "no marker of the job: nothing is deleted")
                write_marker(storage, Marker(job, marker))
                planned = plan_cleanup(storage, 30, now)
                self.assertEqual([(b.name, b.files, b.bytes) for b in planned.batches],
                                 [("2026-01-01T00-00-00Z", 1, 3)])
                report = clean_versions(storage, 30, job_id=job, marker_id=marker, now=now)
                self.assertEqual(([b.name for b in report.batches], report.problems), (["2026-01-01T00-00-00Z"], ()))
                self.assertEqual(newest_batch(storage)[0], "2026-10-28T00-00-00Z")
                kept = storage.entry(".mirrorhold\\versions\\2026-10-28T00-00-00Z\\back.txt")
                back = put_back(PairPlan(deleted=(Entry("back.txt", kept.size, kept.mtime_ns),)), storage)
                self.assertEqual((back.batch, back.keys), ("2026-10-28T00-00-00Z", frozenset({"back.txt"})))
                other = put_back(PairPlan(deleted=(Entry("back.txt", kept.size + 1, kept.mtime_ns),)), storage)
                self.assertIsNone(other)

    def test_a_plan_applied_onto_a_remote_renames_moves_into_versions_and_copies_through_the_seam(self):
        """MEASURED, the live path: a plan made from a local source and a remote destination's
        listing through the seam is applied there by ``apply_pair`` — a destination-only file
        moved into the run's batch on the remote, a changed file copied with its previous
        copy moved into the same batch, a new file copied, a folder only the source holds
        made — as onto a drive."""
        from datetime import datetime, timezone
        from support import volume
        from mirrorhold.jobs.model import HoldThresholds
        from mirrorhold.locations import RuntimeLayout
        from mirrorhold.planning.listing import Side, list_tree
        from mirrorhold.planning.plan import build_plan
        from mirrorhold.records.status import LiveStatus, StatusBoard
        from mirrorhold.run.apply import VersionsBatch, apply_pair
        from mirrorhold.run.control import RunControl, StatusPump
        from mirrorhold.run.gate import judge
        before = self.tmp / "before" / self.case
        for i in range(10):                  # enough files kept that the two removals are not held
            for side in (before, self.local):
                write(side, f"keep{i}.txt", "k", mtime_ns=1_600_000_000_000_000_000)
        write(before, "changed.txt", "old", mtime_ns=1_600_000_000_000_000_000)
        write(before, "only-there.txt", "gone soon", mtime_ns=1_600_000_000_000_000_000)
        write(before, "same.txt", "same", mtime_ns=1_600_000_000_000_000_000)
        write(self.local, "changed.txt", "a new content", mtime_ns=1_700_000_000_000_000_000)
        write(self.local, "same.txt", "same", mtime_ns=1_600_000_000_000_000_000)
        write(self.local, "new\\fresh.txt", "fresh", mtime_ns=1_700_000_000_000_000_000)
        (self.local / "empty-at-source").mkdir()
        for backend, root in self.roots():
            with self.subTest(backend=backend):
                storage = self.storage(root)
                self.loopback.put(before, root)
                status = LiveStatus(uuid.uuid4().hex, os.getpid(), "j" * 32, "Job", "manual", "2026-10-29T10:00:00Z",
                                    pair_index=1)
                board = StatusBoard(RuntimeLayout(self.tmp / "state" / self.case / backend), status)
                board.open()
                try:
                    control = RunControl(status)
                    plan = build_plan(list_tree(self.local, Side.SOURCE, drive_root=False),
                                      storage.list_tree(drive_root=False), time_window_ns=storage.time_window_ns,
                                      trust_temporaries=True)
                    self.assertEqual((sorted(e.path for e in plan.new), [c.source.path for c in plan.changed],
                                      [e.path for e in plan.deleted]),
                                     (["new\\fresh.txt"], ["changed.txt"], ["only-there.txt"]))
                    decision = judge(plan, holds=HoldThresholds(count_floor=1000, percent=100), first_run=False,
                                     destination=volume(2, letter="E"))
                    batch = VersionsBatch(storage, datetime.now(timezone.utc))
                    result = apply_pair(plan, decision, source_root=self.local, destination_root=storage, batch=batch,
                                        engine=open_engine(), control=control, pump=StatusPump(board, control),
                                        list_path=self.tmp / "state" / self.case / f"{backend}.files")
                finally:
                    board.close()
                self.assertEqual((result.engine_failure, result.cancelled, result.skipped), (None, None, []),
                                 result.errors)
                self.assertEqual((result.files_copied, result.moved_to_versions), (2, 2))
                self.assertEqual(storage.read_file("changed.txt"), b"a new content")
                self.assertEqual(storage.read_file("new\\fresh.txt"), b"fresh")
                self.assertEqual(storage.read_file(batch.inside("changed.txt")), b"old")
                self.assertEqual(storage.read_file(batch.inside("only-there.txt")), b"gone soon")
                self.assertFalse(storage.exists("only-there.txt"))
                self.assertEqual(result.versions_batch, str(batch.relative))


if __name__ == "__main__":
    unittest.main()
