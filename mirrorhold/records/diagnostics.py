"""The diagnostic log: what a core process said as it worked, and every failure it did not
foresee, with its traceback.

A run, a full verification or a repair started by the window, and every automatic run,
has no console: its standard output is discarded, so what it says as it works would go
nowhere, and a failure the code does not foresee would leave nothing but its last words
to the window that started it, if one did. So each such process keeps a log of its own beside
the store (``RuntimeLayout.logs_dir``): ``<UTC start>-<command>-<process>.log``, one line
per thing said, each with its time, and a failure's whole traceback. It holds the user's
paths, so it lives where every other runtime file does — under ``Config/`` in a working
copy — and is named ``*.log``.

The log is the process's, not the caller's: the command line opens it for the command
it runs (``process_log``), and the core writes to it wherever it is through ``note`` and
``note_failure``, which write nothing while no log is open (a window's process, a test
that opened none). Its size is bounded: a log stops growing at ``MAX_LOG_BYTES``, saying
so, and each new log first removes the oldest ones beyond ``MAX_LOGS`` files or
``MAX_TOTAL_BYTES`` together. A log another process still has open is left to a later
removal.

The window reads the logs back (``LogWatch``), so that an activity which ended without a
record of its own — refused before it began, failed before it recorded anything, or its
process gone without a word — is shown in whichever window is open then. What it reads
are the log's marks (``mark``), lines of the form ``@<name> <JSON>`` written beside the
words: the command and the job it was given as the process begins (``START``), the job
it resolved (``JOB``) — with the numbers there of the destinations it was given by marker,
when it was — each activity it began (``ACTIVITY``), and how it
ended (``END``: its exit status, and why it could not do its work when it could not). A
log with no end mark whose process no longer has it open belongs to a process that ended
without saying so. A log written before marks existed has none and is only ever saved
whole (``write_joined``).
"""
from __future__ import annotations

import json
import os
import re
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, TextIO, Tuple

from .. import exchange
from ..locations import RuntimeLayout

LOG_SUFFIX = ".log"
MAX_LOG_BYTES = 4 * 1024 * 1024
"""A log stops growing here: a pass that lists millions of files that differ says so in
its record, which keeps the first thousand."""
MAX_LOGS = 200
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DiagnosticLog:
    """One process's log, appended to line by line."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: Optional[TextIO] = open(self.path, "a", encoding="utf-8", errors="backslashreplace",
                                              newline="\n")
        self._size = self.path.stat().st_size
        self._full = False
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        stamp = _now().isoformat(timespec="seconds").replace("+00:00", "Z")
        lines = "".join(f"{stamp} {line}\n" for line in (text.splitlines() or [""]))
        with self._lock:
            if self._handle is None or self._full:
                return
            size = len(lines.encode("utf-8", errors="backslashreplace"))
            if self._size + size > MAX_LOG_BYTES:
                lines, self._full = f"{stamp} the log reached its size limit; nothing more is written\n", True
            self._handle.write(lines)
            self._handle.flush()
            self._size += size

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


_current: Optional[DiagnosticLog] = None


def logs(layout: RuntimeLayout) -> List[Path]:
    """The logs beside a store, oldest first (their names begin with when they began)."""
    folder = layout.logs_dir
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.suffix == LOG_SUFFIX and p.is_file()), key=lambda p: p.name)


def prune(layout: RuntimeLayout, *, keep: int = MAX_LOGS, total: int = MAX_TOTAL_BYTES) -> None:
    """Remove the oldest logs until at most ``keep`` remain and they hold at most
    ``total`` bytes together. A log another process has open stays."""
    found = []
    for path in logs(layout):
        try:
            found.append((path, path.stat().st_size))
        except OSError:
            continue
    size = sum(s for _p, s in found)
    count = len(found)
    for path, length in found:
        if count <= keep and size <= total:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        count -= 1
        size -= length


def _new_path(layout: RuntimeLayout, what: str) -> Path:
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    return layout.logs_dir / f"{stamp}-{what}-{os.getpid()}{LOG_SUFFIX}"


@contextmanager
def process_log(layout: RuntimeLayout, what: str) -> Iterator[Optional[Path]]:
    """Open this process's log for the block (``what``: the command, a name part); the
    path it writes, or None when it cannot be written — the work goes on unlogged."""
    global _current
    previous = _current
    try:
        prune(layout, keep=MAX_LOGS - 1)
        log: Optional[DiagnosticLog] = DiagnosticLog(_new_path(layout, what))
    except OSError:
        log = None
    _current = log
    try:
        yield log.path if log is not None else None
    finally:
        _current = previous
        if log is not None:
            log.close()


def current_path() -> Optional[Path]:
    """The log this process writes, or None."""
    return _current.path if _current is not None else None


def note(text: str) -> None:
    """Write ``text`` to this process's log, if one is open."""
    log = _current
    if log is not None:
        try:
            log.write(text)
        except OSError:
            pass


MARK = "@"
START, JOB, ACTIVITY, END = "start", "job", "activity", "end"
"""The marks a log carries for the window (``LogWatch``)."""


def mark(name: str, **facts) -> None:
    """Write a mark — ``@<name>`` and ``facts`` as JSON, one line — to this process's log,
    if one is open."""
    note(f"{MARK}{name} {json.dumps(facts, ensure_ascii=False, sort_keys=True)}")


def note_failure(context: str) -> Optional[Path]:
    """Write ``context`` and the traceback of the exception being handled to this
    process's log; the log's path, or None when none is open."""
    note(f"{context}\n{traceback.format_exc().rstrip()}")
    return current_path()


def failure_log(layout: RuntimeLayout, what: str, context: str) -> Optional[Path]:
    """Write ``context`` and the traceback being handled to a log of its own, for a
    process that opened none; its path, or None when it cannot be written."""
    try:
        prune(layout, keep=MAX_LOGS - 1)
        log = DiagnosticLog(_new_path(layout, what))
    except OSError:
        return None
    try:
        log.write(f"{context}\n{traceback.format_exc().rstrip()}")
    except OSError:
        return None
    finally:
        log.close()
    return log.path


_LOG_NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-([A-Za-z]+)-\d+\.log$")


@dataclass(frozen=True)
class ActivityLog:
    """What one process's log says of it, from its marks."""

    path: Path
    started: str                             # UTC, from the log's name: 2026-10-14T09:12:00Z
    command: str                             # run, verify, repair or automatic
    trigger: str = ""                        # manual, unattended, arrival or schedule
    job: str = ""                            # the job it resolved, else the job it was given
    destinations: Tuple[int, ...] = ()       # the destinations it was given; none: every one
    activities: Tuple[Tuple[str, str], ...] = ()   # (identifier, kind) of each activity it began
    status: Optional[int] = None             # its exit status, once it said how it ended
    failure: str = ""                        # why it could not do its work, when it could not
    in_use: Optional[bool] = None            # whether its process still has it open, while it has not said
    written: str = ""                        # UTC, when it was last written: 2026-10-14T09:14:31Z

    @property
    def ended(self) -> bool:
        return self.status is not None

    @property
    def failed(self) -> bool:
        """It ended without doing its work, and said why."""
        return self.ended and bool(self.failure)

    @property
    def vanished(self) -> bool:
        """It ended without saying so: no end mark, and no process has it open."""
        return not self.ended and self.in_use is False


class _Reading:
    """One log as far as it has been read: what was appended since is read next."""

    LEGACY_AFTER = 2
    """Lines after which a log with no start mark is taken for one written before marks."""

    def __init__(self, path: Path, started: str, command: str):
        self.path = path
        self.started = started
        self.command = command
        self.offset = 0
        self.partial = b""
        self.lines = 0
        self.marked = False
        self.legacy = False
        self.written = ""
        self.facts: Dict[str, object] = {"activities": []}

    def advance(self) -> None:
        if self.legacy:
            return
        info = self.path.stat()
        size = info.st_size
        self.written = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds") \
            .replace("+00:00", "Z")
        if size <= self.offset:
            return
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(size - self.offset)
        self.offset += len(data)
        *complete, self.partial = (self.partial + data).split(b"\n")
        for raw in complete:
            self._line(raw.decode("utf-8", errors="replace"))
            self.lines += 1
            if not self.marked and self.lines >= self.LEGACY_AFTER:
                self.legacy = True
                return

    def _line(self, text: str) -> None:
        _stamp, _, said = text.partition(" ")
        if not said.startswith(MARK):
            return
        name, _, payload = said[len(MARK):].partition(" ")
        try:
            facts = json.loads(payload)
        except ValueError:
            return
        if not isinstance(facts, dict):
            return
        if name == START:
            self.marked = True
            self.facts.update(command=str(facts.get("command") or self.command),
                              trigger=str(facts.get("trigger") or ""), given=str(facts.get("job") or ""),
                              destinations=_indexes(facts.get("destinations")))
        elif name == JOB:
            self.facts["job"] = str(facts.get("job") or "")
            if "destinations" in facts:
                self.facts["destinations"] = _indexes(facts.get("destinations"))
        elif name == ACTIVITY:
            self.facts["activities"].append((str(facts.get("id") or ""), str(facts.get("kind") or "")))
        elif name == END:
            status = facts.get("status")
            self.facts.update(status=status if isinstance(status, int) else -1,
                              failure=str(facts.get("failure") or ""))

    def summary(self) -> Optional[ActivityLog]:
        if not self.marked:
            return None
        f = self.facts
        status = f.get("status")
        return ActivityLog(self.path, self.started, str(f.get("command") or self.command), str(f.get("trigger") or ""),
                           str(f.get("job") or f.get("given") or ""), tuple(f.get("destinations") or ()),
                           tuple(f["activities"]), status, str(f.get("failure") or ""),
                           None if status is not None else exchange.in_use(self.path), self.written)


def _indexes(value) -> Tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(int(v) for v in value if isinstance(v, int) and not isinstance(v, bool))


class LogWatch:
    """The logs beside a store, followed: each read once, then only what was appended since.
    ``look`` gives what every log written with marks says, oldest first; a log that cannot
    be read now is left to the next look."""

    def __init__(self, layout: RuntimeLayout):
        self.layout = layout
        self._readings: Dict[str, _Reading] = {}

    def look(self) -> Tuple[ActivityLog, ...]:
        found: List[ActivityLog] = []
        present = set()
        for path in logs(self.layout):
            match = _LOG_NAME.match(path.name)
            if match is None:
                continue
            present.add(path.name)
            reading = self._readings.get(path.name)
            if reading is None:
                y, mo, d, h, mi, s, command = match.groups()
                reading = self._readings[path.name] = _Reading(path, f"{y}-{mo}-{d}T{h}:{mi}:{s}Z", command)
            try:
                reading.advance()
            except OSError:
                continue
            summary = reading.summary()
            if summary is not None:
                found.append(summary)
        for name in set(self._readings) - present:
            del self._readings[name]
        return tuple(found)


def write_joined(layout: RuntimeLayout, target: Path) -> int:
    """Write every log beside the store into ``target`` as one text, oldest first, each
    under a line naming it; how many there were. A log that cannot be read says so in its
    place."""
    found = logs(layout)
    with open(target, "w", encoding="utf-8") as out:
        for path in found:
            out.write(f"===== {path.name} =====\n")
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = f"(it could not be read: {exc})\n"
            out.write(text if not text or text.endswith("\n") else text + "\n")
            out.write("\n")
    return len(found)


__all__ = ["ACTIVITY", "ActivityLog", "DiagnosticLog", "END", "JOB", "LogWatch", "MARK", "MAX_LOGS", "MAX_LOG_BYTES",
           "MAX_TOTAL_BYTES", "START", "current_path", "failure_log", "logs", "mark", "note", "note_failure",
           "process_log", "prune", "write_joined"]
