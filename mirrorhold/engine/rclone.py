"""The engine adapter: the only code that knows rclone exists (this module and
``engine.remote``).

Every start of the engine goes through ``engine_command`` and ``engine_environment``:

- Every start names its configuration, and the engine never searches for one. A start
  that reaches no remote passes ``--config ""``, which keeps rclone's configuration in
  memory: without it rclone creates ``%APPDATA%\\rclone`` on its first start (measured),
  and it would read any remotes a user configured there. A start that reaches a remote
  names the configuration file the user made with rclone (``Configuration``) and passes
  ``--ask-password=false``: with standard input closed, as every start has it, a command
  given an encrypted configuration it cannot unlock fails at once with its reason, and a
  remote-control server fails the first operation that needs it (measured;
  ``engine.remote``). Such a configuration is unlocked only by its password command, never
  by a variable or an argument holding the password.
- Every ``RCLONE_*`` variable is removed from the child's environment, because rclone
  reads each flag from one, and a variable set for another purpose would silently
  change what a run does.
- The child is started with no console window (``CREATE_NO_WINDOW``): a scheduled or
  GUI-started run must never flash one.
- An engine with a remote control (``RemoteControl``) is told its credentials only as a
  hash in a file (``--rc-htpasswd``): the password is in no command line and in no
  environment, so none of the engine's own children — a configuration's password command
  among them — inherits it.

The engine is one pinned copy (``PINNED_VERSION``) kept in the git-ignored vendor
directory. ``open_engine`` refuses any other version: the engine-contract suite proves
the behaviour the core relies on for the pinned version only, so moving to another
means moving the pin and re-running that suite.

A run uses the engine for one thing on a drive: copying an explicit list of files
(``Transfer``). The core plans, judges and performs every rename and every move into
versions itself; the engine copies the listed files, moving each destination file it
replaces into the run's versions batch. A folder of a remote (``RemoteRoot``) takes the
same copy, its batch on that remote, and its other files are reached through
``engine.remote``. The measured behaviour this rests on:

- ``copy --files-from-raw LIST --no-traverse`` lists neither side; it addresses each
  named file alone, so the core's own listing is the only one a run pays for. On a
  remote each such address is a request, two for each listed file (measured on loopback
  S3 and WebDAV servers), while without ``--no-traverse`` the engine lists only the
  folders the list names: a copy onto a remote leaves the flag out.
- Under ``--files-from-raw`` a backup directory inside the destination is accepted
  without a filter rule (the list is the filter), and a replaced file moves into it.
- ``--local-encoding None`` makes the engine take local names as they are. With its
  default it reads the fullwidth characters it uses as stand-ins (colon, question mark,
  asterisk, vertical line, quotation mark, less-than and greater-than signs, reverse
  solidus, U+FF1A and the like, and a trailing fullwidth full stop or symbol for space)
  as the characters they stand for: such a source name is passed over without an error,
  and a root folder so named is read from, or written to, a sibling folder. Under
  ``None`` the list's names are still the engine's internal spelling, where the fullwidth
  solidus (U+FF0F), the control pictures U+2401-U+241F and U+2421 stand for a slash and
  control characters (passed over, or an error naming no file): ``engine_name`` quotes
  each with ``QUOTE`` (U+201B), which carries it, and ``local_name`` reads a logged name
  back. Names ending in a space or a period are carried as they are. What no spelling
  carries — a name holding ``QUOTE``, one made of fullwidth full stops alone, an unpaired
  surrogate — ``uncarried`` names, and the plan leaves such paths out.
- A listed file of the destination's size whose modification time differs is read in
  full on both sides; when the contents agree the engine only sets the destination's
  time and logs ``Updated modification time in destination``: nothing is copied and
  nothing moves into the backup directory. ``TransferReport.identical`` lists them.
- The log names a copied file by its source object and a file moved into the backup
  directory by its destination object, each by its path below its side's root, the
  object's type naming its backend (``*local.Object``, ``*s3.Object``, ...). A remote that
  moves within itself logs ``Moved (server-side)``; one that only copies within itself,
  as S3 does, logs ``Copied (server-side copy)`` and then ``Deleted`` for that file. A
  copy from a local source makes no other server-side copy.
- A changed file the engine cannot open has its destination copy moved into the backup
  directory before the open fails. ``open_source_file`` opens a file the way the engine
  does, so the core can keep such a file out of the list.
- Run control: suspending the process pauses it; ``options/set`` with a one-byte
  transfer limit and a soft cutoff finishes the files in flight, starts no other and
  exits 8, but does not end the comparisons; ``core/quit`` ends the engine about 1.5 s
  later and removes the temporary file, which is how a graceful stop ends once no file
  is in flight (``end_stop``).
- A temporary file is ``<name>.<8 hex digits>.partial`` beside its final name.

A verification of an encrypted remote uses it for one more (``CryptCheck``): the engine's
own check, ``cryptcheck --one-way --files-from-raw LIST --combined -``, measured on a local
source against an encrypted remote over a local folder:

- It reports each listed file on standard output as it finishes it — ``=`` the same, ``*``
  differing, ``+`` not on the encrypted remote, ``!`` not checked for an error — a listed
  file not at the source not at all; it exits 0 only when every reported file is the same.
- Started with an empty list it reads nothing and says, at once, either the fingerprint kind
  it would compare (``Using md5 for hash comparisons``) or that the remote beneath keeps none
  (``NO_FINGERPRINT_BENEATH``, exit 1): how a verification knows before it begins whether
  the encrypted form can be compared.

Every transfer's and check's process is placed in a job object that kills it when the job
handle closes, so an engine never outlives the core process that started it.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..locations import vendor_dir

PINNED_VERSION = "v1.75.1"
PINNED_RELEASE = f"rclone-{PINNED_VERSION}-windows-amd64"
PINNED_DOWNLOAD = f"https://downloads.rclone.org/{PINNED_VERSION}/{PINNED_RELEASE}.zip"
PINNED_DOWNLOAD_SHA256 = "200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e"
"""The release file's digest as listed in the release's ``SHA256SUMS``, whose signature
verifies against the rclone release key ``FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA``."""
PINNED_EXECUTABLE_SHA256 = "033eee51c9ad47c2de2624b6674d355274bcd6cf0027a5f85db4437ba24ae81c"
"""The digest of ``rclone.exe`` inside that release file."""

SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Engine:
    path: Path
    version: str


def pinned_engine_path() -> Path:
    return vendor_dir() / PINNED_RELEASE / "rclone.exe"


def locate_engine(preferred: Optional[str] = None) -> Path:
    """The engine executable: ``preferred`` (the user's preference) when given, else
    the pinned copy. Raises EngineError naming where it looked."""
    path = Path(preferred) if preferred else pinned_engine_path()
    if not path.is_file():
        raise EngineError(f"the sync engine was not found at {path}")
    return path


@dataclass(frozen=True)
class Configuration:
    """An rclone configuration file the user made with rclone, named to the engine and
    never read here, and its unlock: ``unlock`` is a command — program and arguments —
    that prints the password of an encrypted file, empty for a file that is not encrypted
    or that is to be refused while it is."""

    path: Path
    unlock: Tuple[str, ...] = ()


def space_separated(words: Sequence[str]) -> str:
    """Words as the engine's space-separated list reads them, a CSV record with a space
    for its separator: a word that is empty or holds a space or a quotation mark is
    enclosed in quotation marks, each of its own doubled."""
    return " ".join('"' + w.replace('"', '""') + '"' if not w or " " in w or '"' in w else w for w in words)


def configuration_arguments(configuration: Optional[Configuration]) -> List[str]:
    """The arguments that name a start's configuration: ``--config ""`` (in memory) for a
    start that reaches no remote, else the user's file, never asking for a password, with
    its password command when it has one."""
    if configuration is None:
        return ["--config", ""]
    arguments = ["--config", str(configuration.path), "--ask-password=false"]
    if configuration.unlock:
        arguments += ["--password-command", space_separated(configuration.unlock)]
    return arguments


def engine_environment(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if not k.upper().startswith("RCLONE_")}


def engine_command(engine: Path, args: Sequence[str], configuration: Optional[Configuration] = None) -> list:
    return [str(engine), *configuration_arguments(configuration), *(str(a) for a in args)]


def run_engine(engine: Path, args: Sequence[str], *, timeout: Optional[float] = None,
               configuration: Optional[Configuration] = None) -> subprocess.CompletedProcess:
    """Run the engine to completion and capture its output as text."""
    return subprocess.run(engine_command(engine, args, configuration), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=SUBPROCESS_FLAGS,
                          env=engine_environment(), timeout=timeout, stdin=subprocess.DEVNULL)


def start_engine(engine: Path, args: Sequence[str], *, configuration: Optional[Configuration] = None,
                 **popen_options) -> subprocess.Popen:
    """Start the engine and return at once; output pipes are the caller's choice."""
    popen_options.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(engine_command(engine, args, configuration), creationflags=SUBPROCESS_FLAGS,
                            env=engine_environment(), **popen_options)


_VERSION = re.compile(r"^rclone (v\d+\.\d+\.\d+\S*)", re.M)


def parse_version(text: str) -> str:
    match = _VERSION.search(text or "")
    if not match:
        raise EngineError("the engine did not report a version")
    return match.group(1)


def open_engine(preferred: Optional[str] = None) -> Engine:
    """The located engine with its version checked against the pin."""
    path = locate_engine(preferred)
    try:
        result = run_engine(path, ["version"], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineError(f"the sync engine at {path} could not be started: {exc}") from None
    version = parse_version(result.stdout)
    if version != PINNED_VERSION:
        raise EngineError(f"the sync engine at {path} is {version}; this tool is proven against "
                          f"{PINNED_VERSION} only")
    return Engine(path, version)


# --- the engine's own temporaries ----------------------------------------------------

_TEMPORARY = re.compile(r"^.+\.[0-9a-f]{8}\.partial$")


def is_engine_temporary(name: str) -> bool:
    """Whether a file name has the shape of the engine's unfinished copy of a file."""
    return bool(_TEMPORARY.match(name))


# --- names ---------------------------------------------------------------------------

LOCAL_ENCODING = "None"
"""The engine's ``--local-encoding``: local names are taken as they are."""

QUOTE = "\u201b"
"""The engine's quote character: before a stand-in, it makes that character itself."""

_QUOTED = frozenset("\uff0f" + "".join(chr(c) for c in range(0x2401, 0x2420)) + "\u2421")
"""Characters the engine's internal spelling reads as others even under ``LOCAL_ENCODING``:
the fullwidth slash, the control pictures but ``\u2400``, and ``\u2421`` (measured)."""

_FULLWIDTH_DOTS = frozenset(("\uff0e", "\uff0e\uff0e"))


def uncarried(relative: str) -> Optional[str]:
    """Why the engine cannot carry a relative path (backslash-separated) under its own
    name, or None when it can. Measured under ``LOCAL_ENCODING`` with ``engine_name``'s
    spelling: a name holding the quote character is passed over however it is spelled; a
    name of fullwidth periods alone is read as ``.`` or ``..``; and the list, being UTF-8,
    cannot hold an unpaired surrogate."""
    for part in relative.split("\\"):
        if any("\ud800" <= c <= "\udfff" for c in part):
            return "its name is not valid Unicode (it holds an unpaired surrogate); the copy engine cannot read it"
        if QUOTE in part:
            return f"its name holds {QUOTE} (U+201B), which the copy engine cannot keep"
        if part in _FULLWIDTH_DOTS:
            return "its name is fullwidth periods alone, which the copy engine reads as . or .."
    return None


def engine_name(relative: str) -> str:
    """A carried relative path as the engine's list spells it: '/'-separated, every
    character in ``_QUOTED`` preceded by ``QUOTE``."""
    return "".join(QUOTE + c if c in _QUOTED else c for c in relative.replace("\\", "/"))


def local_name(name: str) -> str:
    """A name the engine reported, as the relative path it is on disk (backslash-separated):
    the inverse of ``engine_name``. A carried path holds no ``QUOTE`` of its own, so every
    one is the list's."""
    out, i = [], 0
    while i < len(name):
        c = name[i]
        if c == QUOTE and i + 1 < len(name) and name[i + 1] in _QUOTED:
            out.append(name[i + 1])
            i += 2
            continue
        out.append("\\" if c == "/" else c)
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class RemoteRoot:
    """A folder of a remote of the user's configuration: ``remote`` is the remote's name
    as the configuration spells it, ``folder`` the path inside it as the user gave it,
    '/'-separated, empty for the remote's root."""

    remote: str
    folder: str = ""

    @property
    def fs(self) -> str:
        """The folder as the engine names it: ``remote:folder``."""
        return f"{self.remote}:{self.folder}"

    def joined(self, relative: str) -> "RemoteRoot":
        """The folder at ``relative`` (a carried path, backslash-separated) below this one;
        a folder given from the remote's top (``/``) stays so."""
        inner = remote_path(relative)
        base = self.folder.rstrip("/") or self.folder[:1]
        if not inner:
            return RemoteRoot(self.remote, base)
        return RemoteRoot(self.remote, f"{base}{'' if not base or base.endswith('/') else '/'}{inner}")

    def __str__(self) -> str:
        return self.fs


def remote_path(relative: str) -> str:
    """A carried relative path (backslash-separated) as the engine spells it below a remote
    folder: '/'-separated, quoted as ``engine_name`` quotes, '' for the folder itself."""
    return engine_name(relative).strip("/")


Root = Union[Path, RemoteRoot]
"""A transfer's destination or backup directory: a folder of this computer, or of a remote."""

_QUERY = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#\"'<>]*)\?[^\s#\"'<>]*", re.I)


def without_queries(text: str) -> str:
    """An engine message with each web address's query part removed: some services carry a
    short-lived authorisation there, and no file this tool writes may keep one."""
    return _QUERY.sub(r"\1", text)


# --- Windows entry points ------------------------------------------------------------

_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4             # read, write, delete
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _Windows:
    """kernel32 and ntdll entry points the adapter uses, bound once on first use."""

    _instance = None

    @classmethod
    def get(cls) -> "_Windows":
        if sys.platform != "win32":
            raise EngineError("this operation needs Windows")
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.k32, self.ntdll = k32, ntdll
        self.invalid = wintypes.HANDLE(-1).value

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.Extended, self.Io = Extended, Io

    def open_read(self, path: str) -> int:
        """A handle opened with backup semantics and every sharing mode; raises OSError."""
        text = str(path)
        if len(text) >= 248 and not text.startswith("\\\\?\\") and os.path.isabs(text):
            text = "\\\\?\\" + text
        handle = self.k32.CreateFileW(text, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING,
                                      _FILE_FLAG_BACKUP_SEMANTICS, None)
        if handle in (None, self.invalid):
            error = self.ct.get_last_error()
            raise OSError(None, self.ct.FormatError(error).strip(), str(path), error)
        return handle

    def _process_handle(self, pid: int, access: int) -> int:
        handle = self.k32.OpenProcess(access, False, pid)
        if not handle:
            raise EngineError(f"the engine process {pid} cannot be reached")
        return handle

    def suspend(self, pid: int) -> None:
        handle = self._process_handle(pid, _PROCESS_SUSPEND_RESUME)
        try:
            self.ntdll.NtSuspendProcess(handle)
        finally:
            self.k32.CloseHandle(handle)

    def resume(self, pid: int) -> None:
        handle = self._process_handle(pid, _PROCESS_SUSPEND_RESUME)
        try:
            self.ntdll.NtResumeProcess(handle)
        finally:
            self.k32.CloseHandle(handle)

    def io_bytes(self, pid: int) -> int:
        """The bytes the process has read and written so far. Its remote control answering
        moves neither count (measured on the development machine, 2026-10-21)."""
        handle = self._process_handle(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        try:
            counters = self.Io()
            if not self.k32.GetProcessIoCounters(handle, self.ct.byref(counters)):
                raise EngineError(f"the engine process {pid} cannot be read")
            return counters.ReadTransferCount + counters.WriteTransferCount
        finally:
            self.k32.CloseHandle(handle)

    def kill_with_owner(self, pid: int) -> int:
        """A job object holding the process and ending it when the job handle closes —
        explicitly, or because the process holding the handle ended. Returns the handle."""
        job = self.k32.CreateJobObjectW(None, None)
        if not job:
            raise self.ct.WinError(self.ct.get_last_error())
        try:
            info = self.Extended()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                    self.ct.byref(info), self.ct.sizeof(info)):
                raise self.ct.WinError(self.ct.get_last_error())
            process = self._process_handle(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE)
            try:
                if not self.k32.AssignProcessToJobObject(job, process):
                    raise self.ct.WinError(self.ct.get_last_error())
            finally:
                self.k32.CloseHandle(process)
        except BaseException:
            self.k32.CloseHandle(job)
            raise
        return job

    def close(self, handle: int) -> None:
        self.k32.CloseHandle(handle)


def open_source_file(path):
    """A source file opened for reading the way the engine opens one: on Windows with
    backup semantics and every sharing mode, so a file this cannot open is one the engine
    cannot open either, and a file it can open, the engine can. Raises OSError."""
    if sys.platform != "win32":
        return open(path, "rb")
    import msvcrt
    windows = _Windows.get()
    handle = windows.open_read(str(path))
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        windows.close(handle)
        raise
    return os.fdopen(descriptor, "rb")


def source_file_unreadable(path) -> Optional[str]:
    """None when the engine could open the file now, else the reason it could not."""
    try:
        with open_source_file(path):
            return None
    except OSError as exc:
        return exc.strerror or str(exc)


# --- one transfer of an explicit file list -------------------------------------------

GRACEFUL_STOP_EXIT = 8
"""Measured: the exit status after a transfer limit stops the engine with a soft cutoff."""

IMMEDIATE_STOP_EXIT = 97
"""The exit status the adapter asks ``core/quit`` for; a status the engine itself uses
would be ambiguous."""

GRACEFUL_END_EXIT = 98
"""The exit status the adapter asks ``core/quit`` for when a graceful stop finds no file
in flight (``Transfer.end_stop``)."""


class StopKind(str, Enum):
    NONE = "none"
    GRACEFUL = "graceful"
    IMMEDIATE = "immediate"


@dataclass(frozen=True)
class InFlight:
    """A file the engine is copying now; ``name`` is relative and backslash-separated."""

    name: str
    size: int
    bytes: int


@dataclass(frozen=True)
class TransferStats:
    """The engine's live statistics, with the files it found identical so far (from its
    log). ``bytes`` and ``transfers`` count copies only; ``checking`` names the files it is
    comparing now; ``checks``: the files it has compared, ``errors``: the errors it met;
    ``io_bytes``: the bytes its process has read and written, by Windows' count (0 where not
    known) — comparing one large file reads both copies while no other figure moves."""

    bytes: int
    total_bytes: int
    transfers: int
    total_transfers: int
    speed: float
    eta: Optional[float]
    in_flight: Tuple[InFlight, ...] = ()
    checking: Tuple[str, ...] = ()
    identical: int = 0
    identical_bytes: int = 0
    checks: int = 0
    errors: int = 0
    io_bytes: int = 0


@dataclass(frozen=True)
class TransferReport:
    """What one transfer did, from the engine's per-file log. Paths are relative and
    backslash-separated. ``identical``: files found identical to their destination copy,
    whose time alone was set, with their sizes as the caller gave them."""

    exit_code: int
    stop: StopKind
    copied: Dict[str, int] = field(default_factory=dict)       # path -> bytes
    moved_to_backup: Tuple[str, ...] = ()                       # destination files moved into the backup directory
    failed: Dict[str, str] = field(default_factory=dict)       # path -> the engine's message
    errors: Tuple[str, ...] = ()                                # messages naming no file
    seconds: float = 0.0
    identical: Dict[str, int] = field(default_factory=dict)    # path -> bytes

    @property
    def bytes_copied(self) -> int:
        return sum(self.copied.values())

    @property
    def bytes_identical(self) -> int:
        return sum(self.identical.values())

    @property
    def ended_as_expected(self) -> bool:
        """Finished, every failure attributed to a file, or stopped as the core asked."""
        if self.stop is StopKind.GRACEFUL and self.exit_code in (0, 1, GRACEFUL_STOP_EXIT, GRACEFUL_END_EXIT):
            return True
        if self.stop is StopKind.IMMEDIATE:
            return True
        if self.exit_code == 0:
            return True
        return self.exit_code == 1 and bool(self.failed) and not self.errors


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


UNANSWERED = (OSError, ValueError, http.client.HTTPException)
"""What a call to an engine's remote control raises when no answer came whole: not reached,
broken off, or not JSON (``RemoteControl.request``, ``RemoteControl.call``)."""


class RemoteControl:
    """One engine's remote control: listening on the loopback address, with a user and a
    password made for that engine alone. The engine is given them as a password file
    holding the password's SHA-1 hash (``--rc-htpasswd``, measured to be honoured), so the
    password is in no command line and in no environment, and the engine's own children —
    a configuration's password command among them — inherit nothing of it. Whoever holds it
    can do anything the engine can — read and write every file the user can, and every
    remote of its configuration (the engine's own documentation) — and only this process
    does. ``close`` removes the file."""

    def __init__(self):
        self.port = _free_loopback_port()
        self.user, self.password = uuid.uuid4().hex, uuid.uuid4().hex
        digest = base64.b64encode(hashlib.sha1(self.password.encode()).digest()).decode()
        handle, name = tempfile.mkstemp(prefix="mirrorhold-rc-", suffix=".htpasswd")
        with os.fdopen(handle, "w", encoding="ascii", newline="\n") as file:
            file.write(f"{self.user}:{{SHA}}{digest}\n")
        self.password_file = Path(name)

    @property
    def arguments(self) -> List[str]:
        return ["--rc-addr", f"127.0.0.1:{self.port}", "--rc-htpasswd", str(self.password_file)]

    @property
    def authorization(self) -> str:
        return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def request(self, path: str, *, data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None,
                timeout: float = 5.0):
        """An authenticated request to the engine, its response open (a context manager).
        Raises urllib's HTTPError for an answer the engine refused, OSError when it cannot be
        reached, http.client's HTTPException for an answer broken off."""
        request = urllib.request.Request(self.url(path), data=data,
                                         headers={**(headers or {}), "Authorization": self.authorization})
        return urllib.request.urlopen(request, timeout=timeout)

    def call(self, method: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
        """The method's answer; raises as ``request`` does, and ValueError for an answer that
        is not JSON."""
        with self.request(method, data=json.dumps(body or {}).encode(),
                          headers={"Content-Type": "application/json"}, timeout=timeout) as response:
            return json.loads(response.read())

    def close(self) -> None:
        try:
            self.password_file.unlink()
        except OSError:
            pass


def start_controlled(engine: Engine, args: Sequence[str], control: RemoteControl, *,
                     configuration: Optional[Configuration] = None) -> Tuple[subprocess.Popen, Optional[int]]:
    """Start the engine with ``control`` as its remote control and its error output piped as
    text, and place it in a job object that ends it when the job handle closes — explicitly,
    or because the process holding the handle ended. Returns the process and the job handle,
    None where none could be made. Raises EngineError when the engine cannot be started."""
    try:
        process = start_engine(engine.path, [*args, *control.arguments], configuration=configuration,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace")
    except OSError as exc:
        raise EngineError(f"the sync engine could not be started: {exc}") from None
    job = None
    if sys.platform == "win32":
        try:
            job = _Windows.get().kill_with_owner(process.pid)
        except (OSError, EngineError):
            job = None  # the engine still works; it merely could outlive a crashed core
    return process, job


class Transfer:
    """One running copy of an explicit list of files from a source folder to a destination
    folder — of this computer, or of a remote (``RemoteRoot``) with the ``configuration``
    that names it — every replaced destination file moving into ``backup_dir``, which lies
    on the destination's side.

    Control goes through suspending the process and through the engine's remote control
    (``RemoteControl``). The list file names the user's files, so its place is the
    caller's to choose.

    ``files`` are relative, backslash-separated paths the engine can carry (``uncarried``
    is None for each); ``sizes`` gives their sizes, which the engine does not log for a
    file it finds identical."""

    def __init__(self, engine: Engine, source_root, destination_root: Root, files: Sequence[str], *,
                 backup_dir: Root, list_path, bandwidth_limit: Optional[str] = None,
                 sizes: Optional[Mapping[str, int]] = None, configuration: Optional[Configuration] = None):
        remote = isinstance(destination_root, RemoteRoot)
        if remote != isinstance(backup_dir, RemoteRoot):
            raise ValueError("the backup directory lies on the destination's side")
        if remote and configuration is None:
            raise ValueError("a remote destination is reached through the configuration that names it")
        self.list_path = Path(list_path)
        self.list_path.parent.mkdir(parents=True, exist_ok=True)
        self.list_path.write_text("".join(engine_name(f) + "\n" for f in files), encoding="utf-8", newline="\n")
        self._control = RemoteControl()
        args = ["copy", str(source_root), str(destination_root), "--files-from-raw", str(self.list_path),
                *(() if remote else ("--no-traverse",)), "--backup-dir", str(backup_dir),
                "--local-encoding", LOCAL_ENCODING, "--use-json-log", "-v", "--stats", "0", "--rc"]
        if bandwidth_limit:
            args += ["--bwlimit", bandwidth_limit]
        self._sizes = dict(sizes or {})
        self._copied: Dict[str, int] = {}
        self._identical: Dict[str, int] = {}
        self._identical_count = 0
        self._identical_bytes = 0
        self._moved: List[str] = []
        self._failed: Dict[str, str] = {}
        self._errors: List[str] = []
        self._stop = StopKind.NONE
        self._suspended = False
        self._job = None
        self._started = time.monotonic()
        self._ended: Optional[float] = None
        try:
            self._process, self._job = start_controlled(engine, args, self._control, configuration=configuration)
        except EngineError:
            self._control.close()
            raise
        self._reader = threading.Thread(target=self._read_log, name="engine-log", daemon=True)
        self._reader.start()

    # --- the log -----------------------------------------------------------------

    def _read_log(self) -> None:
        for line in self._process.stderr:
            self._consume(line)

    def _consume(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            entry = json.loads(line)
        except ValueError:
            self._errors.append(without_queries(line))
            return
        if not isinstance(entry, dict) or "stats" in entry:
            return
        level, message, name = entry.get("level"), without_queries(str(entry.get("msg") or "")), entry.get("object")
        if isinstance(name, str) and str(entry.get("objectType") or "").endswith(".Object"):
            path = local_name(name)
            if message.startswith("Copied (server-side copy)") or message.startswith("Moved"):
                self._moved.append(path)
            elif message.startswith("Deleted"):
                pass                     # the second half of a move made by a server-side copy
            elif message.startswith("Copied"):
                self._copied[path] = int(entry.get("size") or 0)
                self._failed.pop(path, None)
            elif message.startswith("Updated modification time"):
                size = self._sizes.get(path, 0)
                if path not in self._identical:
                    self._identical[path] = size
                    self._identical_bytes += size
                    self._identical_count += 1
                self._failed.pop(path, None)
            elif level == "error":
                self._failed[path] = message
            return
        if level == "error" and not message.startswith("Attempt "):
            self._errors.append(f"{name}: {message}" if isinstance(name, str) else message)

    # --- the remote control ------------------------------------------------------

    def _call(self, method: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
        return self._control.call(method, body, timeout)

    def _call_when_up(self, method: str, body: Optional[dict], patience: float) -> bool:
        """Retry until the remote control answers; False when the engine ended first or
        did not answer within ``patience`` seconds."""
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            if self.poll() is not None:
                return False
            try:
                self._call(method, body)
                return True
            except UNANSWERED:
                time.sleep(0.1)
        return False

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def suspended(self) -> bool:
        return self._suspended

    @property
    def stop_requested(self) -> StopKind:
        return self._stop

    def stats(self, timeout: float = 2.0) -> Optional[TransferStats]:
        """The engine's live statistics, or None while it is suspended, starting or ended, or
        when it gives no whole answer."""
        if self._suspended or self.poll() is not None:
            return None
        try:
            raw = self._call("core/stats", timeout=timeout)
        except UNANSWERED:
            return None
        flight = tuple(InFlight(local_name(str(t.get("name", ""))), int(t.get("size") or 0), int(t.get("bytes") or 0))
                       for t in raw.get("transferring") or ())
        checking = tuple(local_name(str(name)) for name in raw.get("checking") or ())
        eta = raw.get("eta")
        return TransferStats(int(raw.get("bytes") or 0), int(raw.get("totalBytes") or 0),
                             int(raw.get("transfers") or 0), int(raw.get("totalTransfers") or 0),
                             float(raw.get("speed") or 0.0), float(eta) if eta is not None else None, flight,
                             checking, self._identical_count, self._identical_bytes,
                             int(raw.get("checks") or 0), int(raw.get("errors") or 0), self._io_bytes())

    def _io_bytes(self) -> int:
        if sys.platform != "win32":
            return 0
        try:
            return _Windows.get().io_bytes(self.pid)
        except (EngineError, OSError):
            return 0

    # --- control -----------------------------------------------------------------

    def suspend(self) -> None:
        """Pause: no data moves until ``resume``."""
        if self.poll() is None and not self._suspended:
            _Windows.get().suspend(self.pid)
            self._suspended = True

    def resume(self) -> None:
        if self._suspended:
            if self.poll() is None:
                _Windows.get().resume(self.pid)
            self._suspended = False

    def stop_gracefully(self) -> None:
        """Start no new file and let every file in flight finish. Raises EngineError when
        the engine, still running, cannot be reached."""
        self.resume()
        self._stop = StopKind.GRACEFUL
        body = {"main": {"MaxTransfer": 1, "CutoffMode": "SOFT"}}
        if not self._call_when_up("options/set", body, patience=30.0) and self.poll() is None:
            raise EngineError("the engine did not accept the request to stop after the files in flight")

    def end_stop(self) -> None:
        """End a graceful stop once no file is in flight: the soft cutoff starts no new
        file but lets the comparisons run on, so the engine is asked to quit. The stop
        stays graceful — nothing in flight is lost. Raises EngineError when the engine,
        still running, cannot be reached."""
        self._stop = StopKind.GRACEFUL
        if not self._call_when_up("core/quit", {"exitCode": GRACEFUL_END_EXIT}, patience=5.0) \
                and self.poll() is None:
            raise EngineError("the engine did not accept the request to end its stop")

    def stop_now(self) -> None:
        """Stop at once: the copies in flight end and their temporary files are removed.
        When the engine cannot be asked, its process is ended, which leaves them."""
        self.resume()
        self._stop = StopKind.IMMEDIATE
        if not self._call_when_up("core/quit", {"exitCode": IMMEDIATE_STOP_EXIT}, patience=5.0) \
                and self.poll() is None:
            self._process.kill()

    # --- the end -----------------------------------------------------------------

    def poll(self) -> Optional[int]:
        code = self._process.poll()
        if code is not None and self._ended is None:
            self._ended = time.monotonic()
        return code

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        return self.poll()

    def report(self) -> TransferReport:
        """The transfer's outcome; waits for the engine to end."""
        code = self.wait()
        self._reader.join(timeout=30)
        if self._process.stderr is not None:
            self._process.stderr.close()
        self.close()
        return TransferReport(code, self._stop, dict(self._copied), tuple(self._moved), dict(self._failed),
                              tuple(self._errors), (self._ended or time.monotonic()) - self._started,
                              dict(self._identical))

    def close(self) -> None:
        """Release the job object (ending the engine if it still runs) and remove the list
        and the remote control's password file."""
        if self._job is not None:
            _Windows.get().close(self._job)
            self._job = None
        self._control.close()
        try:
            self.list_path.unlink()
        except OSError:
            pass


# --- one check of an encrypted remote -------------------------------------------------

NO_FINGERPRINT_BENEATH = "does not support any hashes"
"""What the engine says when the remote beneath an encrypted one keeps no fingerprint, as
it starts a check and before it reads any file (measured)."""

_KIND_IN_USE = re.compile(r"^Using (\S+) for hash comparisons")


class Checked(str, Enum):
    """A file's outcome in a check of an encrypted remote (``CryptCheck``), as the engine's
    combined report marks it."""

    SAME = "="
    DIFFERS = "*"
    MISSING = "+"                 # not on the encrypted remote
    ERROR = "!"                   # it could not be read or its fingerprint computed


@dataclass(frozen=True)
class CryptCheckReport:
    """What a check of an encrypted remote found. ``checked``: each listed file the engine
    reported, by its relative path (backslash-separated); ``messages``: the engine's words for
    a file it reported with an error; ``kind``: the fingerprint kind the service beneath
    keeps, as the engine named it; ``unsupported``: the service beneath keeps none, and
    nothing was checked."""

    exit_code: Optional[int]
    checked: Dict[str, Checked] = field(default_factory=dict)
    messages: Dict[str, str] = field(default_factory=dict)
    kind: str = ""
    unsupported: bool = False
    errors: Tuple[str, ...] = ()


class CryptCheck:
    """One check of listed files of a source folder against an encrypted remote's folder,
    with the ``configuration`` that names it (``cryptcheck``): the engine reads each source
    file, encrypts it with the nonce it reads from the start of the encrypted copy, and
    compares the result's fingerprint with the one the service beneath keeps. Of the copy it
    reads nothing but that start; where the service beneath keeps no fingerprint it checks
    nothing (``CryptCheckReport.unsupported``).

    The engine reports each file as it finishes it, so ``checked`` grows while it works.
    Suspending the process pauses it; ``stop`` ends it, the files not reported then left
    unchecked. ``files`` are relative, backslash-separated paths the engine can carry."""

    def __init__(self, engine: Engine, source_root, destination_root: RemoteRoot, files: Sequence[str], *,
                 list_path, configuration: Configuration):
        self.list_path = Path(list_path)
        self.list_path.parent.mkdir(parents=True, exist_ok=True)
        self.list_path.write_text("".join(engine_name(f) + "\n" for f in files), encoding="utf-8", newline="\n")
        args = ["cryptcheck", str(source_root), destination_root.fs, "--one-way", "--files-from-raw",
                str(self.list_path), "--combined", "-", "--local-encoding", LOCAL_ENCODING, "--use-json-log", "-v",
                "--stats", "0"]
        self._lock = threading.Lock()
        self._checked: Dict[str, Checked] = {}
        self._messages: Dict[str, str] = {}
        self._errors: List[str] = []
        self._kind = ""
        self._unsupported = False
        self._suspended = False
        self._job = None
        try:
            self._process = start_engine(engine.path, args, configuration=configuration, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            self._remove_list()
            raise EngineError(f"the sync engine could not be started: {exc}") from None
        if sys.platform == "win32":
            try:
                self._job = _Windows.get().kill_with_owner(self._process.pid)
            except (OSError, EngineError):
                self._job = None  # the engine still works; it merely could outlive a crashed core
        self._readers = [threading.Thread(target=self._read_report, name="cryptcheck-report", daemon=True),
                         threading.Thread(target=self._read_log, name="cryptcheck-log", daemon=True)]
        for reader in self._readers:
            reader.start()

    def _read_report(self) -> None:
        for line in self._process.stdout:
            line = line.rstrip("\r\n")
            if len(line) < 3 or line[1] != " ":
                continue
            try:
                outcome = Checked(line[0])
            except ValueError:
                continue
            with self._lock:
                self._checked[local_name(line[2:])] = outcome

    def _read_log(self) -> None:
        for line in self._process.stderr:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = {"msg": line, "level": "error"}
            if not isinstance(entry, dict) or "stats" in entry:
                continue
            message, name = without_queries(str(entry.get("msg") or "")), entry.get("object")
            found = _KIND_IN_USE.match(message)
            with self._lock:
                if found:
                    self._kind = found.group(1)
                elif NO_FINGERPRINT_BENEATH in message:
                    self._unsupported = True
                elif entry.get("level") == "error" and isinstance(name, str) and name:
                    self._messages[local_name(name)] = message
                elif entry.get("level") == "error":
                    self._errors.append(message)

    @property
    def checked(self) -> Dict[str, Checked]:
        """The files reported so far."""
        with self._lock:
            return dict(self._checked)

    def suspend(self) -> None:
        if self.poll() is None and not self._suspended:
            _Windows.get().suspend(self._process.pid)
            self._suspended = True

    def resume(self) -> None:
        if self._suspended:
            if self.poll() is None:
                _Windows.get().resume(self._process.pid)
            self._suspended = False

    def stop(self) -> None:
        """End the check now; what it reported stays."""
        self.resume()
        if self.poll() is None:
            self._process.kill()

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def report(self, timeout: Optional[float] = None) -> CryptCheckReport:
        """The check's outcome once the engine ended; it is ended when it has not within
        ``timeout`` seconds."""
        try:
            self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            self._process.wait()
        for reader in self._readers:
            reader.join(timeout=30)
        for stream in (self._process.stdout, self._process.stderr):
            stream.close()
        self.close()
        with self._lock:
            return CryptCheckReport(self._process.returncode, dict(self._checked), dict(self._messages), self._kind,
                                    self._unsupported, tuple(self._errors))

    def close(self) -> None:
        """Release the job object (ending the engine if it still runs) and remove the list."""
        if self._job is not None:
            _Windows.get().close(self._job)
            self._job = None
        self._remove_list()

    def _remove_list(self) -> None:
        try:
            self.list_path.unlink()
        except OSError:
            pass
