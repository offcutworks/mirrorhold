"""A remote's folders and files, reached through one running engine.

``RemoteSession`` starts the engine as a remote-control server (``rcd``) with the user's
configuration (``rclone.Configuration``) and answers every operation a remote destination
needs besides the explicit-list copy and the check of an encrypted remote, which stay
commands of their own (``rclone.Transfer``, ``rclone.CryptCheck``): the remote's features,
its free space, a folder's listing or a file's entry — with sizes, times and the fingerprints
the service keeps, every kind or the kinds asked for — a file's leading bytes, its whole
content or its content read as a stream, writing a small file, moving a file or a folder
within the remote, making a folder, and deleting a file or a folder whole; and the
fingerprint of a file of this computer of a kind only the engine computes. The core decides
every one of them; the engine only performs it.

One engine serves a whole session, so an encrypted configuration is unlocked once, by its
password command, and every connection the services allow is kept between operations. The
engine reads its configuration only when an operation first needs it, and one it cannot
read or unlock fails that operation while the engine runs on (measured), so the session
opens it as it starts, by listing its remotes' names (``remotes``): a configuration that
cannot be opened ends the session's start with ``RemoteError`` and the engine's reason,
within seconds. The remote control listens on the loopback address with credentials made
for the session, the engine holding only a hash of them (``rclone.RemoteControl``);
whoever holds them reaches every remote of the configuration, and only this process does.
The session's process sits in a job object, so it ends with the process that started it.

Measured on the pinned engine against its own loopback S3, WebDAV and SFTP servers and its
local and encrypted backends (``tests/test_engine_contract.py``):

- A remote's features come from ``operations/fsinfo``. One that cannot move within itself
  but copies within itself (S3) moves a file by a copy and a delete on the service, keeping
  its modification time; every backend measured kept a file's time through a move of the
  file or of its folder.
- An encrypted remote answers the ``encode`` backend command and no other kind does, which
  is how ``RemoteFeatures.encrypted`` is known without reading the configuration.
- S3 reports that it empties a trash of its own (``CleanUp``, which on S3 removes uploads
  never finished); a local remote does not. A service reporting it may keep what is
  deleted until it is emptied (``RemoteFeatures.empties_trash``).
- ``operations/about`` reports the free space of SFTP and of a local or encrypted folder
  that exists; S3 reports none, and WebDAV, served by the engine, answers with no figure.
- A listing (``operations/list``) gives each entry's path below the folder listed, its size,
  its time to the service's precision and, when asked, the fingerprints the service keeps
  (MD5 on S3; none over WebDAV or SFTP as served here, none for an encrypted remote); a
  local remote computes every kind by reading the file, and only the kinds asked for. A
  folder that is not there is an error naming it not found — but a service with no
  folders of its own (S3) lists any folder it is asked for as if it were there and empty;
  it shows the folders its files' names imply, and loses a folder once it holds nothing.
- The remote control serves a file's content (``--rc-serve``) and honours a byte range, so a
  file's leading bytes are read without the rest. A file written through
  ``operations/uploadfile`` replaces one already there under its name.

What a listing or a move does on a real service — its request limits, its precision, names
it treats differently, a trash that keeps what is deleted — is measured only on that
service.
"""
from __future__ import annotations

import http.client
import io
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Sequence, Tuple

from .rclone import (LOCAL_ENCODING, UNANSWERED, Configuration, Engine, EngineError, RemoteControl, RemoteRoot,
                     _Windows, engine_name, local_name, remote_path, start_controlled, without_queries)

MODTIME_NOT_SUPPORTED_NS = 100 * 365 * 24 * 3600 * 10 ** 9
"""The precision a backend reports when it keeps no modification time (rclone's
``fs.ModTimeNotSupported``, a hundred years of 365 days)."""

START_PATIENCE = 30.0
"""Seconds a session's engine has to answer before its start is given up."""
CALL_TIMEOUT = 120.0
"""Seconds one operation may take; a listing of a large folder takes the longest."""

ListingProgress = Callable[[int], None]
"""Told the number of entries seen so far, after each folder; an exception it raises ends
the listing and reaches the caller, and while it does not return, nothing is listed."""


class RemoteError(EngineError):
    """An operation on a remote that failed, with the engine's reason."""


class RemoteUnanswered(RemoteError):
    """An operation the session's engine gave no whole answer to: not reached, or broken off."""


@dataclass(frozen=True)
class RemoteFeatures:
    """What a remote can do, from the engine's report of it. ``precision_ns``: the finest
    difference of modification times it keeps; ``hashes``: the fingerprint kinds it keeps
    for each file, as the engine names them (``md5``, ``sha1``, ...)."""

    moves: bool                  # moves a file within itself
    copies: bool                 # copies a file within itself
    moves_folders: bool          # moves a folder whole within itself
    precision_ns: int
    hashes: Tuple[str, ...]
    reports_space: bool
    keeps_empty_folders: bool
    case_insensitive: bool
    duplicate_names: bool        # one folder may hold two entries of one name
    partial_uploads: bool        # writes under a temporary name first
    deletes_folders: bool        # deletes a folder whole in one operation
    encrypted: bool
    empties_trash: bool = False  # keeps what it deletes until it is emptied (the engine's ``CleanUp``)

    @property
    def moves_within(self) -> bool:
        """A file moves without leaving the service: by a move, or by a copy and a delete."""
        return self.moves or self.copies

    @property
    def keeps_times(self) -> bool:
        return self.precision_ns < MODTIME_NOT_SUPPORTED_NS


@dataclass(frozen=True)
class Space:
    """A remote's space as its service reports it; either figure is None when it gives none."""

    free: Optional[int]
    total: Optional[int]


@dataclass(frozen=True)
class RemoteEntry:
    """A file or folder of a remote: its path below the folder listed (backslash-separated,
    as the core spells relative paths), size, modification time and fingerprints."""

    path: str
    is_dir: bool
    size: int
    mtime_ns: int
    hashes: Mapping[str, str] = field(default_factory=dict)


_TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


def parse_time(text: str) -> int:
    """An RFC 3339 time as the engine writes it, to nanoseconds since the epoch."""
    match = _TIME.match(text or "")
    if match is None:
        raise RemoteError(f"the engine reported a time it does not write: {text!r}")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    offset = 0 if zone == "Z" else (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
    seconds = int(moment.timestamp()) - offset
    return seconds * 10 ** 9 + int((fraction or "").ljust(9, "0"))


def _entry(item: Mapping) -> RemoteEntry:
    """An entry of ``operations/list`` or ``operations/stat``, whose path the engine gives
    below the folder it was asked about. A folder's time is not used, so one the engine
    reports in a form it does not write reads as 0; a file's must be read."""
    path = str(item.get("Path") or "")
    hashes = item.get("Hashes") or {}
    if item.get("IsDir"):
        try:
            when = parse_time(str(item.get("ModTime") or ""))
        except RemoteError:
            when = 0
        return RemoteEntry(local_name(path), True, 0, when)
    return RemoteEntry(local_name(path), False, int(item.get("Size") or 0), parse_time(str(item.get("ModTime") or "")),
                       {str(k): str(v) for k, v in hashes.items() if v})


def _missing(reason: str) -> bool:
    """Whether an engine's reason says the file or folder asked about is not there."""
    return "not found" in reason


_NETWORK_FAILURES = ("dial tcp", "dial udp", "no such host", "connectex:", "connection refused", "i/o timeout",
                     "network is unreachable", "no route to host")
"""What the engine's reason holds when it could not reach the service at all: the
connection itself failed (Go's network errors, measured against a loopback remote whose
server is not running), not the service refusing what it was asked."""


def unreachable(reason: str) -> bool:
    """Whether an engine's reason says the service could not be reached — the computer
    offline, the server down — rather than that it answered with a refusal. Any other
    failure, a refused sign-in among them, is the service's answer and is reported as such."""
    text = reason.lower()
    return any(marker in text for marker in _NETWORK_FAILURES)


class _Stream(io.RawIOBase):
    """A served file's content read as it arrives, each read the engine breaks off raised as
    ``RemoteUnanswered`` naming the file."""

    def __init__(self, response, relative: str):
        super().__init__()
        self._response = response
        self._relative = relative

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        try:
            return self._response.read() if size is None or size < 0 else self._response.read(size)
        except UNANSWERED as exc:
            raise RemoteUnanswered(f"{self._relative}: the sync engine did not answer: {exc}") from None

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            try:
                self._response.close()
            finally:
                super().close()


def _configuration_reason(reason: str) -> str:
    """Why the engine could not open a configuration, as it says it, without the wrapping of
    the failure it raises and without its advice to set ``RCLONE_CONFIG_PASS``: this tool
    removes every such variable, so the advice could not work."""
    reason = re.sub(r"^(panic received: )?(fatal error: )?", "", reason)
    reason = re.sub(r"^Failed to load config file \"[^\"]*\": ", "", reason)
    return re.sub(r"\s+-\s+set RCLONE_CONFIG_PASS\b.*$", "", reason)


class RemoteSession:
    """One engine answering for every remote of a configuration until ``close``.

    Every path is a carried relative path (backslash-separated) below the ``RemoteRoot``
    it is given with; '' is that folder itself. Each operation raises ``RemoteError`` with
    the engine's reason when it fails; a path that is not there is an answer (None or
    False) where the operation says so."""

    def __init__(self, engine: Engine, configuration: Configuration, *, patience: float = START_PATIENCE):
        self.engine = engine
        self.configuration = configuration
        self._control = RemoteControl()
        self._said: deque = deque(maxlen=20)
        self._job = None
        try:
            self._process, self._job = start_controlled(
                engine, ["rcd", "--rc-serve", "--local-encoding", LOCAL_ENCODING], self._control,
                configuration=configuration)
        except EngineError as exc:
            self._control.close()
            raise RemoteError(str(exc)) from None
        self._reader = threading.Thread(target=self._read_log, name="engine-session-log", daemon=True)
        self._reader.start()
        self._wait_up(patience)
        self.remotes = self._open()

    # --- the engine --------------------------------------------------------------

    def _read_log(self) -> None:
        for line in self._process.stderr:
            line = line.strip()
            if line:
                self._said.append(without_queries(line))

    def _wait_up(self, patience: float) -> None:
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._reader.join(timeout=5)
                said = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+", "", self._said[-1]) if self._said else ""
                self.close()
                raise RemoteError(f"the sync engine ended as it started: {said or 'no message'}")
            try:
                self._control.call("rc/noopauth", timeout=2.0)
                return
            except UNANSWERED:
                time.sleep(0.1)
        self.close()
        raise RemoteError(f"the sync engine did not answer within {patience:.0f} seconds")

    def _open(self) -> Tuple[str, ...]:
        """Open the configuration — unlocked by its password command, once — and read its
        remotes' names. The engine reads its configuration only when an operation first
        needs it, and a configuration it cannot read or unlock then fails that operation
        while the engine runs on (measured), so the session opens it before anything else."""
        try:
            listed = self._call("config/listremotes", {})
        except RemoteError as exc:
            self.close()
            raise RemoteError(f"the configuration {self.configuration.path} cannot be opened: "
                              f"{_configuration_reason(str(exc))}") from None
        return tuple(str(name) for name in listed.get("remotes") or ())

    @property
    def pid(self) -> int:
        return self._process.pid

    def close(self) -> None:
        """End the engine: asked to quit, and ended when it does not."""
        if self._process.poll() is None:
            try:
                self._control.call("core/quit", timeout=2.0)
            except UNANSWERED:
                pass
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(10)
        if self._job is not None:
            _Windows.get().close(self._job)
            self._job = None
        self._control.close()
        if self._process.stderr is not None:
            self._reader.join(timeout=5)
            self._process.stderr.close()

    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- calls -------------------------------------------------------------------

    def _call(self, method: str, body: dict, timeout: float = CALL_TIMEOUT) -> dict:
        try:
            return self._control.call(method, body, timeout)
        except urllib.error.HTTPError as exc:
            raise RemoteError(self._refusal(exc)) from None
        except UNANSWERED as exc:
            raise RemoteUnanswered(f"the sync engine did not answer: {exc}") from None

    @staticmethod
    def _refusal(exc: urllib.error.HTTPError) -> str:
        try:
            reason = str(json.loads(exc.read()).get("error") or "")
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            reason = ""
        finally:
            exc.close()
        return without_queries(reason or f"the engine refused the request ({exc.code})")

    # --- what a remote is --------------------------------------------------------

    def features(self, root: RemoteRoot) -> RemoteFeatures:
        info = self._call("operations/fsinfo", {"fs": root.fs})
        found = info.get("Features") or {}
        return RemoteFeatures(
            moves=bool(found.get("Move")), copies=bool(found.get("Copy")),
            moves_folders=bool(found.get("DirMove")), precision_ns=int(info.get("Precision") or 0),
            hashes=tuple(str(h) for h in info.get("Hashes") or ()), reports_space=bool(found.get("About")),
            keeps_empty_folders=bool(found.get("CanHaveEmptyDirectories")),
            case_insensitive=bool(found.get("CaseInsensitive")), duplicate_names=bool(found.get("DuplicateFiles")),
            partial_uploads=bool(found.get("PartialUploads")), deletes_folders=bool(found.get("Purge")),
            encrypted=self._encrypts(root), empties_trash=bool(found.get("CleanUp")))

    def _encrypts(self, root: RemoteRoot) -> bool:
        try:
            self._call("backend/command", {"command": "encode", "fs": root.fs, "arg": ["mirrorhold"]})
        except RemoteError as exc:
            reason = str(exc)
            if "command not found" in reason or "doesn't support backend commands" in reason:
                return False
            raise
        return True

    def space(self, root: RemoteRoot) -> Optional[Space]:
        """The space the service reports for the folder, or None when it reports none."""
        try:
            about = self._call("operations/about", {"fs": root.fs})
        except RemoteError as exc:
            if "doesn't support about" in str(exc):
                return None
            raise
        free, total = about.get("free"), about.get("total")
        if free is None and total is None:
            return None
        return Space(int(free) if free is not None else None, int(total) if total is not None else None)

    # --- reading -----------------------------------------------------------------

    def stat(self, root: RemoteRoot, relative: str, *, kinds: Sequence[str] = ()) -> Optional[RemoteEntry]:
        """The file or folder at ``relative``, or None when nothing is there; with the
        fingerprints of ``kinds`` the service keeps for a file."""
        opt = {"noMimeType": True}
        if kinds:
            opt.update(showHash=True, hashTypes=list(kinds))
        try:
            item = self._call("operations/stat", {"fs": root.fs, "remote": remote_path(relative),
                                                  "opt": opt}).get("item")
        except RemoteError as exc:
            if _missing(str(exc)):
                return None
            raise
        return _entry(item) if item else None

    def list_folder(self, root: RemoteRoot, relative: str = "", *, hashes: bool = False,
                    kinds: Sequence[str] = ()) -> Optional[Tuple[RemoteEntry, ...]]:
        """The entries of one folder, their paths below ``root``; None when the folder is
        not there, which a service with no folders of its own never says: there a folder
        not there lists as empty. ``hashes``: with every fingerprint the service keeps;
        ``kinds``: with those kinds alone — a local remote computes each kind it is asked for
        by reading the file, so a caller asks only for the kind it compares."""
        below = remote_path(relative)
        opt = {"showHash": hashes or bool(kinds), "noMimeType": True}
        if kinds:
            opt["hashTypes"] = list(kinds)
        try:
            listed = self._call("operations/list", {"fs": root.fs, "remote": below, "opt": opt})
        except RemoteError as exc:
            if _missing(str(exc)):
                return None
            raise
        return tuple(_entry(item) for item in listed.get("list") or ())

    def list_tree(self, root: RemoteRoot, relative: str = "", *, hashes: bool = False, kinds: Sequence[str] = (),
                  progress: Optional[ListingProgress] = None) -> Optional[Tuple[RemoteEntry, ...]]:
        """Every entry below the folder at ``relative``, folder by folder: ``progress`` is
        told after each folder, as a local listing tells it, so a pause holds the listing
        and a cancel ends it between two folders. None when the folder is not there.

        One listing request a folder, measured against a recursive listing's one for the
        whole tree on a service that offers it: what a pause and a cancel cost."""
        stack, found, seen = [relative], [], 0
        while stack:
            if progress is not None:
                progress(seen)
            folder = stack.pop()
            entries = self.list_folder(root, folder, hashes=hashes, kinds=kinds)
            if entries is None:
                if folder == relative:
                    return None
                continue                 # gone since its parent was listed
            seen += len(entries)
            for entry in entries:
                found.append(entry)
                if entry.is_dir:
                    stack.append(entry.path)
        if progress is not None:
            progress(seen)
        return tuple(found)

    def read(self, root: RemoteRoot, relative: str, limit: Optional[int] = None) -> Optional[bytes]:
        """The file's content, its first ``limit`` bytes when given; None when it is not there."""
        if limit is not None and limit <= 0:
            return b""
        response = self._served(root, relative, {"Range": f"bytes=0-{limit - 1}"} if limit is not None else {})
        if response is None:
            return None
        try:
            with response:
                data = response.read()
        except UNANSWERED as exc:
            raise RemoteUnanswered(f"{relative}: the sync engine did not answer: {exc}") from None
        return data if limit is None else data[:limit]

    def open_file(self, root: RemoteRoot, relative: str) -> Optional[BinaryIO]:
        """The file's content as a stream read from its start, for a file too large to hold
        whole; None when it is not there. A read of the stream that the engine breaks off
        raises ``RemoteError``; the caller closes it."""
        response = self._served(root, relative, {})
        return _Stream(response, relative) if response is not None else None

    def _served(self, root: RemoteRoot, relative: str, headers: Mapping[str, str]):
        """The engine's response serving the file (``--rc-serve``), open; None when it is not there."""
        target = urllib.parse.quote(f"[{root.fs}]", safe="[]:/") + "/" + urllib.parse.quote(
            remote_path(relative), safe="/")
        try:
            return self._control.request(target, headers=headers, timeout=CALL_TIMEOUT)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            if exc.code == 416:
                return io.BytesIO()          # a range past the end of an empty file
            raise RemoteError(f"{relative}: the engine refused to read it ({exc.code}); a folder reads so too") \
                from None
        except UNANSWERED as exc:
            raise RemoteUnanswered(f"{relative}: the sync engine did not answer: {exc}") from None

    def local_fingerprint(self, path: Path, kind: str, *, give_up: Callable[[], bool] = lambda: False,
                          look: float = 0.1) -> Optional[str]:
        """The ``kind`` fingerprint of a file of this computer, as the engine computes it by
        reading the file (``operations/hashsumfile``, run as a job of the engine's): for a kind
        this tool does not compute itself. None when ``give_up`` said so before it was done,
        the engine's job then told to stop; RemoteError when the engine could not compute it."""
        started = self._call("operations/hashsumfile", {"fs": str(path.parent), "remote": engine_name(path.name),
                                                        "hashType": kind, "_async": True})
        job = started.get("jobid")
        while True:
            status = self._call("job/status", {"jobid": job})
            if status.get("finished"):
                found = str((status.get("output") or {}).get("hash") or "")
                if not status.get("success") or not found:
                    raise RemoteError(f"{path.name}: {without_queries(str(status.get('error') or 'no fingerprint'))}")
                return found.lower()
            if give_up():
                try:
                    self._call("job/stop", {"jobid": job})
                except RemoteError:
                    pass                     # the engine ends with the session in any case
                return None
            time.sleep(look)

    # --- writing -----------------------------------------------------------------

    def write(self, root: RemoteRoot, relative: str, data: bytes) -> None:
        """Write a small file whole, replacing one there under its name; its folder is made."""
        folder, _, name = remote_path(relative).rpartition("/")
        boundary = uuid.uuid4().hex
        quoted = name.replace("\\", "\\\\").replace('"', '\\"')
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{quoted}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") + bytes(data) \
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        query = urllib.parse.urlencode({"fs": root.fs, "remote": folder})
        try:
            with self._control.request(f"operations/uploadfile?{query}", data=body, timeout=CALL_TIMEOUT,
                                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}) \
                    as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise RemoteError(f"{relative}: {self._refusal(exc)}") from None
        except UNANSWERED as exc:
            raise RemoteUnanswered(f"{relative}: the sync engine did not answer: {exc}") from None

    def move_file(self, root: RemoteRoot, old: str, new: str) -> None:
        """Move a file within the remote. A file already at ``new`` is replaced: the caller
        that must not replace one looks first."""
        self._call("operations/movefile", {"srcFs": root.fs, "srcRemote": remote_path(old),
                                           "dstFs": root.fs, "dstRemote": remote_path(new)})

    def move_folder(self, root: RemoteRoot, old: str, new: str) -> None:
        """Move a folder whole within the remote, its files keeping their times: in one
        operation where the service moves folders, else file by file, the emptied folders
        removed. Into a folder already at ``new`` its files are merged, a file of the same
        name replaced: the caller that must not replace one looks first. Moving file by file
        leaves the emptied folder itself (measured on SFTP, WebDAV and a local remote), which
        is then removed."""
        self._call("sync/move", {"srcFs": root.joined(old).fs, "dstFs": root.joined(new).fs,
                                 "deleteEmptySrcDirs": True})
        left = self.stat(root, old)
        if left is not None and left.is_dir:
            self._call("operations/rmdirs", {"fs": root.joined(old).fs, "remote": "", "leaveRoot": False})

    def make_folder(self, root: RemoteRoot, relative: str = "") -> None:
        """Make the folder and those it lies in; a service with no folders of its own makes
        none, and the folder exists once a file lies in it."""
        self._call("operations/mkdir", {"fs": root.fs, "remote": remote_path(relative)})

    def remove_empty_folders(self, root: RemoteRoot, relative: str) -> None:
        """Remove the folder at ``relative`` and every folder in it that holds nothing,
        deepest first (``operations/rmdirs``); a folder holding a file, and the file, stay."""
        self._call("operations/rmdirs", {"fs": root.joined(relative).fs, "remote": "", "leaveRoot": False})

    def delete_file(self, root: RemoteRoot, relative: str) -> bool:
        """Delete one file; False when it was not there."""
        try:
            self._call("operations/deletefile", {"fs": root.fs, "remote": remote_path(relative)})
        except RemoteError as exc:
            if _missing(str(exc)):
                return False
            raise
        return True

    def delete_folder(self, root: RemoteRoot, relative: str) -> bool:
        """Delete a folder and everything in it; False when it was not there. The folder is
        looked for first: deleting one that is not there fails in words of each service's
        own, and not at all on a service with no folders of its own (measured)."""
        if self.stat(root, relative) is None:
            return False
        self._call("operations/purge", {"fs": root.fs, "remote": remote_path(relative)})
        return True
