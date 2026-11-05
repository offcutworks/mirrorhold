"""Starting a run as a core process of its own — the one place a caller does it.

Every run executes in its own process, whoever asked for it (the GUI's "Run now", the
GUI's confirmation of a preview or of a first backup), so the caller's failure or exit
never stops a run. The process is ``python -m mirrorhold run`` against the caller's
store — of the destinations the caller names by marker (``--marker``), or of every one —
the caller's own interpreter and package, as ``invocation`` composes it, started:

- without a console window (``CREATE_NO_WINDOW``): a GUI or a scheduled task must not
  flash a terminal;
- in a process group of its own, and outside the caller's job object where the caller's
  job allows it (``CREATE_BREAKAWAY_FROM_JOB``), so closing the caller — or the terminal
  or editor that started the caller — does not end the run;
- with its standard output discarded and its error output kept in a file beside the
  store (``RuntimeLayout.launches_dir``), because a run that cannot start at all (its
  ticket is stale, its store unusable) says why only there: everything after its start
  is in its status and its records.

``Launch.problem()`` reads that file once the process has ended without a run to show
for it, and ``Launch.discard()`` removes it.

A verification (``python -m mirrorhold verify``), full or of new copies, is started the same
way (``start_verification``): it too must outlive its caller, by hours. So is a repair of the
copies a verification found to differ (``python -m mirrorhold repair``, ``start_repair``).

A destination is named by its marker, never by its number: the process finds it in the job
as it reads the job file itself, so a save that reorders the job's destinations while the
caller starts it changes nothing of which destination it serves.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from ..cli import EXIT_CANCELLED, EXIT_FAILED, EXIT_HELD, EXIT_OK
from ..invocation import core_arguments, current_interpreter
from ..locations import PROJECT_ROOT, runtime_layout

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000 if sys.platform == "win32" else 0
_ACCESS_DENIED = 5

EXIT_STARTED_NORMALLY = (EXIT_OK, EXIT_HELD, EXIT_FAILED, EXIT_CANCELLED)
"""The command line's exit statuses for a run, a verification or a repair that ran (done,
held or left, stopped or failed, differences found, cancelled): its outcome is in its
records. Any other status means it never ran."""


def run_arguments(store_path: Path, job_id: str, *, destinations: Optional[Sequence[str]] = None,
                  ticket_id: Optional[str] = None, first_backup: bool = False, unattended: bool = False) -> List[str]:
    """The interpreter arguments of a run of ``job_id`` against ``store_path``: of the
    destinations whose markers ``destinations`` holds, or of every one."""
    args = core_arguments("--store", store_path, "run", job_id)
    for marker in destinations or ():
        args += ["--marker", marker]
    if ticket_id:
        args += ["--confirm", ticket_id]
    if first_backup:
        args.append("--first-backup")
    if unattended:
        args.append("--unattended")
    return args


def verify_arguments(store_path: Path, job_id: str, destination: str, *, restart: bool = False,
                     new_copies: bool = False) -> List[str]:
    """The interpreter arguments of a verification of a job's destination, named by its
    marker: full, or of its new copies."""
    args = core_arguments("--store", store_path, "verify", job_id, "--marker", destination)
    if new_copies:
        args.append("--new-copies")
    if restart:
        args.append("--restart")
    return args


def repair_arguments(store_path: Path, job_id: str, destination: str) -> List[str]:
    """The interpreter arguments of a repair of a job's destination, named by its marker."""
    return core_arguments("--store", store_path, "repair", job_id, "--marker", destination)


@dataclass
class Launch:
    """A run, verification or repair process this caller started, and where its error output goes."""

    job_id: str
    process: subprocess.Popen
    output: Path

    def ended(self) -> Optional[int]:
        """The exit status once the process has ended, else None."""
        return self.process.poll()

    def problem(self) -> Optional[str]:
        """Why the run never ran, once it has ended without running; else None."""
        code = self.ended()
        if code is None or code in EXIT_STARTED_NORMALLY:
            return None
        try:
            text = self.output.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        return text or f"the process ended with status {code} before it started"

    def discard(self) -> None:
        try:
            self.output.unlink()
        except OSError:
            pass


def start_run(store_path: Path, job_id: str, *, destinations: Optional[Sequence[str]] = None,
              ticket_id: Optional[str] = None, first_backup: bool = False, unattended: bool = False,
              interpreter: Optional[str] = None, root: Path = PROJECT_ROOT) -> Launch:
    """Start a run of ``job_id`` — of the destinations whose markers ``destinations`` holds,
    or of every one — as a process of its own and return at once.

    ``interpreter`` defaults to the caller's; ``root`` is the folder ``mirrorhold``
    is imported from."""
    store_path = Path(store_path).resolve()
    arguments = run_arguments(store_path, job_id, destinations=destinations, ticket_id=ticket_id,
                              first_backup=first_backup, unattended=unattended)
    return _start(store_path, job_id, arguments, interpreter, root)


def start_verification(store_path: Path, job_id: str, destination: str, *, restart: bool = False,
                       new_copies: bool = False, interpreter: Optional[str] = None,
                       root: Path = PROJECT_ROOT) -> Launch:
    """Start a verification of a job's destination, named by its marker — full, or of its
    new copies — as a process of its own and return at once."""
    store_path = Path(store_path).resolve()
    return _start(store_path, job_id,
                  verify_arguments(store_path, job_id, destination, restart=restart, new_copies=new_copies),
                  interpreter, root)


def start_repair(store_path: Path, job_id: str, destination: str, *, interpreter: Optional[str] = None,
                 root: Path = PROJECT_ROOT) -> Launch:
    """Start a repair of a job's destination, named by its marker, as a process of its own
    and return at once."""
    store_path = Path(store_path).resolve()
    return _start(store_path, job_id, repair_arguments(store_path, job_id, destination), interpreter, root)


def _start(store_path: Path, job_id: str, arguments: List[str], interpreter: Optional[str], root: Path) -> Launch:
    folder = runtime_layout(store_path).launches_dir
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{uuid.uuid4().hex}.log"
    command = [str(interpreter or current_interpreter())] + arguments
    flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    with open(output, "wb") as errors:
        options = dict(cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                       close_fds=True)
        try:
            process = subprocess.Popen(command, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **options)
        except OSError as exc:
            if getattr(exc, "winerror", None) != _ACCESS_DENIED:
                raise
            process = subprocess.Popen(command, creationflags=flags, **options)
    return Launch(job_id, process, output)
