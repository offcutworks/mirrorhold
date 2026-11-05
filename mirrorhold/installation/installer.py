"""Installing: making one copy the one scheduled tasks and the Start menu entry start.

What ``install`` and ``uninstall`` do depends on the kind of copy they run in
(``locations.copy_kind``):

- **A working copy** installs a copy of itself: ``install`` copies the two packages, the
  licence and the pinned engine into ``installed/`` (git-ignored), draws the program's
  icon there with the working copy's own drawing script — the one a release build runs —
  records what it installed and for which store, writes the Start menu entry, and has the
  core's automation register the tasks the jobs call for, now pointing at the new copy.
  An icon that cannot be drawn is reported and the copy installed without it: the icon
  names the program and decides nothing. Updating is installing again.
  ``uninstall`` removes the tasks and their folder, the Start menu entry and the installed
  copy. The copy is replaced as a whole: the new one is built beside it
  (``installed.new``), the old one renamed aside and the new one renamed into place. A
  folder an open window or a running task works in cannot be renamed on Windows, so a
  copy in use is refused before anything changes, rather than replaced under a running
  process. Both are refused from the installed copy itself, which would replace or
  delete the very files it runs from.
- **A portable release** is set up where it lies, copying nothing of the program: ``install``
  writes the setup record beside the store, naming this release folder as it was reached
  (``locations.as_reached``), then the Start menu entry and the tasks, pointing at that
  folder and the interpreter it carries. Setting up a newer release, or the same one moved,
  re-points all of it there; a package manager that puts a newer release in the same
  folder needs nothing set up again. ``uninstall`` removes the tasks, the entry and the
  record, and leaves the folder for the user to delete. The setup program's uninstaller,
  which then deletes the folder itself, asks for the removal only where this release is
  the one set up (``only_if_here``), so another copy's setup outlives it.

The Start menu entry names the icon the copy it starts carries, where it carries one, so
the entry and every notice under its identity show MirrorHold's icon. A set-up release's
icon is copied beside the store and named there, so the entry keeps it when the release's
folder is moved or deleted.

Neither touches the store, the run records or any destination, and both are refused
while a run is in progress. **One installation per account:** the two kinds share the
task folder, the Start menu entry and the notification identity, so each refuses while
the other holds them — a working copy while a portable release is set up on this account
(its record beside the portable store), a portable release while the Start menu entry
starts the source installation's copy.

A caller that names its own Start menu entry or installed copy (a test) stands for an
account other than the user's: the user's portable record is then consulted only when
the caller names it too (``account_store``).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .. import __version__
from ..automation.reconcile import AutomationOutcome, apply_automation, remove_all
from ..automation.scheduler import Scheduler, SchedulerError, WindowsScheduler
from ..engine.rclone import PINNED_RELEASE, EngineError, open_engine
from ..invocation import gui_arguments, windowless_interpreter
from ..jobs.model import Job
from ..locations import (ICON_FILE_NAME, INSTALL_DIR_NAME, LICENCE_FILE_NAME, PROJECT_ROOT, VENDOR_DIR_NAME,
                         CopyKind, as_reached, copy_kind, install_dir, portable_store_path, runtime_layout)
from ..records.status import observe_runs
from .manifest import InstalledCopy, InstallKind, read_installation, remove_installation, write_installation
from .release import Release, read_release
from .shortcut import Shortcut, read_shortcut, start_menu_shortcut, write_shortcut

PACKAGES = ("mirrorhold", "mirrorhold_gui")
SHORTCUT_DESCRIPTION = "MirrorHold: one-way backups onto backup drives"
RENAME_PATIENCE = 5.0
ICON_SCRIPT = Path("tools") / "make_icon.py"     # in the working copy installed from
ICON_TIMEOUT = 120
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class InstallError(RuntimeError):
    """An install, update or uninstall refused before it changed anything."""


@dataclass(frozen=True)
class InstallationState:
    copy: Optional[InstalledCopy]    # the installation automatic runs use, for the store asked about
    running_installed: bool          # this code is the source installation's copy
    serves_store: bool               # that installation serves the store asked about
    release: Optional[Release] = None            # this code's release, when it is a portable release
    blocker: Optional[InstalledCopy] = None      # the other kind's installation, holding the entry and the tasks
    store_folder: Optional[Path] = None          # where the store asked about, and its records, are kept

    @property
    def installed(self) -> bool:
        return self.copy is not None

    @property
    def portable(self) -> bool:
        """This code is a portable release."""
        return self.release is not None

    @property
    def here(self) -> bool:
        """This portable release is the copy set up."""
        return self.portable and self.copy is not None and self.copy.runs_from(self.release.root)

    @property
    def gone(self) -> bool:
        """The portable release set up no longer exists where its record says."""
        return self.copy is not None and self.copy.portable and not self.copy.present


@dataclass(frozen=True)
class InstallResult:
    copy: InstalledCopy
    shortcut: Optional[Path]
    automation: AutomationOutcome
    problems: Tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    removed_copy: bool
    removed_tasks: int
    problems: Tuple[str, ...] = ()
    portable: bool = False           # a portable release's setup removed; its folder kept
    not_here: bool = False           # asked to remove this release's setup only, and it is not the one set up
    set_up: Optional[InstalledCopy] = None       # then, the copy that is set up, if any


def installation_state(store_path: Path, *, source: Path = PROJECT_ROOT, target: Optional[Path] = None,
                       shortcut: Optional[Path] = None, account_store: Optional[Path] = None) -> InstallationState:
    """The installation automatic runs use for ``store_path``, seen from the copy at ``source``."""
    source = Path(source)
    folder = runtime_layout(store_path).root
    if copy_kind(source) is CopyKind.PORTABLE:
        copy = _portable_record(folder)
        return InstallationState(copy, False, copy is not None and copy.serves(store_path), read_release(source),
                                 _source_holding_entry(shortcut), folder)
    copy = read_installation(Path(target) if target is not None else install_dir(source))
    blocker = _portable_set_up(_account_store(account_store, target, shortcut))
    return InstallationState(copy, copy_kind(source) is CopyKind.INSTALLED,
                             copy is not None and copy.serves(store_path), None, blocker, folder)


def default_target(source: Path = PROJECT_ROOT) -> Path:
    """Where a working copy installs: ``installed/`` beside its packages."""
    return Path(source) / INSTALL_DIR_NAME


def _account_store(account_store: Optional[Path], target: Optional[Path], shortcut: Optional[Path]) -> Optional[Path]:
    """The job file a portable release set up on this account serves: the one named, or the
    user's own unless the caller named an entry or a copy of its own."""
    if account_store is not None:
        return Path(account_store)
    return portable_store_path() if target is None and shortcut is None else None


def _portable_record(folder: Path) -> Optional[InstalledCopy]:
    copy = read_installation(folder)
    return copy if copy is not None and copy.portable else None


def _portable_set_up(store: Optional[Path]) -> Optional[InstalledCopy]:
    """The portable release set up for ``store``, when its folder still holds it."""
    if store is None:
        return None
    copy = _portable_record(runtime_layout(store).root)
    return copy if copy is not None and copy.present else None


def _source_holding_entry(shortcut: Optional[Path]) -> Optional[InstalledCopy]:
    """The source installation's copy the Start menu entry starts, if it starts one."""
    entry = Path(shortcut) if shortcut is not None else start_menu_shortcut()
    if not entry.is_file():
        return None
    try:
        folder = read_shortcut(entry).working_dir
    except OSError:
        return None
    copy = read_installation(Path(folder)) if folder else None
    return copy if copy is not None and copy.kind is InstallKind.SOURCE else None


def _refuse_from_installed_copy(source: Path) -> None:
    if copy_kind(source) is CopyKind.INSTALLED:
        copy = read_installation(source)
        where = f" at {copy.source}" if copy is not None else ""
        raise InstallError(f"this is the installed copy; install, update and uninstall from the working copy{where}")


def _refuse_while_running(store_path: Path) -> None:
    if any(r.alive for r in observe_runs(runtime_layout(store_path))):
        raise InstallError("a run is in progress; try again when it has ended")


def _sibling(target: Path, suffix: str) -> Path:
    return target.with_name(target.name + suffix)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _rename(source: Path, target: Path, *, sleep=time.sleep, clock=time.monotonic) -> None:
    """Rename a folder, waiting out a brief hold (a virus scanner reading a file just
    written) for up to ``RENAME_PATIENCE`` seconds. A folder a process works in stays
    held, and the last OSError is raised."""
    deadline = clock() + RENAME_PATIENCE
    while True:
        try:
            source.rename(target)
            return
        except OSError:
            if clock() >= deadline:
                raise
            sleep(0.25)


def _make_writable(function, path, _error) -> None:
    """``shutil.rmtree``'s error handler: clear the read-only attribute and try once more."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except OSError:
        pass


def _clear_read_only(root: Path) -> None:
    """Windows' read-only attribute, which a copy takes from its source's folders, makes an
    emptied folder refuse deletion; the installed copy carries none."""
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            os.chmod(os.path.join(folder, name), stat.S_IWRITE | stat.S_IREAD)
    os.chmod(root, stat.S_IWRITE | stat.S_IREAD)


def _remove_tree(path: Path, *, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Delete a folder, read-only entries included, waiting out brief holds as
    ``_rename`` does; whether it is gone."""
    deadline = clock() + RENAME_PATIENCE
    while True:
        shutil.rmtree(path, onerror=_make_writable)
        if not path.exists():
            return True
        if clock() >= deadline:
            return False
        sleep(0.25)


def _draw_icon(source: Path, stage: Path, problems: List[str]) -> None:
    """Draw the program's icon into ``stage`` with the working copy's own drawing script,
    started in a process of its own: the script is build tooling, never imported by the
    program. A failure is told in ``problems``, and the copy then carries no icon."""
    icon = stage / ICON_FILE_NAME
    try:
        done = subprocess.run([sys.executable, str(source / ICON_SCRIPT), str(icon)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=ICON_TIMEOUT, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        problems.append(f"the icon could not be drawn: {exc}")
        return
    if done.returncode != 0 or not icon.is_file():
        detail = (done.stdout + done.stderr).strip().splitlines()
        problems.append("the icon could not be drawn" + (f": {detail[-1]}" if detail else ""))


def _build(source: Path, stage: Path, target: Path, store_path: Path, interpreter: Path, now: datetime,
           problems: List[str]) -> InstalledCopy:
    """Build the copy that will become ``target`` in ``stage``, its record naming ``target``."""
    licence = source / LICENCE_FILE_NAME
    if not licence.is_file():
        raise InstallError(f"the working copy holds no {LICENCE_FILE_NAME} ({licence}), which every copy carries")
    if stage.exists():
        _remove_tree(stage)
    stage.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for package in PACKAGES:
        shutil.copytree(source / package, stage / package, ignore=ignore)
    shutil.copy2(licence, stage / LICENCE_FILE_NAME)
    engine = source / VENDOR_DIR_NAME / PINNED_RELEASE / "rclone.exe"
    if not engine.is_file():
        raise InstallError(f"the pinned engine is not in the working copy ({engine}); PROJECT.md says how to "
                           "obtain it")
    try:
        open_engine(str(engine))
    except EngineError as exc:
        raise InstallError(f"the working copy's engine does not work: {exc}") from None
    copied = stage / VENDOR_DIR_NAME / PINNED_RELEASE / "rclone.exe"
    copied.parent.mkdir(parents=True)
    shutil.copy2(engine, copied)
    if _sha256(copied) != _sha256(engine):
        raise InstallError("the engine copied into the installation differs from the working copy's")
    _draw_icon(source, stage, problems)
    _clear_read_only(stage)
    copy = InstalledCopy(Path(target), __version__, _utc(now), Path(store_path).resolve(), interpreter,
                         as_reached(source))
    write_installation(copy, stage)
    return copy


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _keep_icon(copy: InstalledCopy, problems: List[str]) -> Path:
    """The icon a set-up release's entry names: the release's own, copied beside the store,
    where moving or deleting the release's folder leaves it; none beside the store where the
    release ships none. The copy is written beside and renamed into place, so no half-written
    icon is ever named. One that cannot be made is told in ``problems``: the entry then names
    the icon already kept there, which is the program's too, or else the release's own."""
    shipped, kept = Path(copy.root) / ICON_FILE_NAME, copy.record_folder / ICON_FILE_NAME
    staging = kept.with_name(kept.name + ".writing")
    try:
        if shipped.is_file():
            shutil.copyfile(shipped, staging)
            os.replace(staging, kept)
        elif kept.exists():
            kept.unlink()
    except OSError as exc:
        problems.append(f"the icon could not be kept beside the job file: {exc}")
        try:
            staging.unlink()
        except OSError:
            pass
        return kept if shipped.is_file() and kept.is_file() else shipped
    return kept


def _write_entry(copy: InstalledCopy, shortcut: Optional[Path], problems: List[str],
                 icon: Optional[Path] = None) -> Optional[Path]:
    """Write the Start menu entry opening ``copy``'s GUI on the store it serves, showing
    ``icon`` (default: the one the copy carries), or its interpreter's where there is none."""
    entry = Path(shortcut) if shortcut is not None else start_menu_shortcut()
    arguments = subprocess.list2cmdline(gui_arguments("--store", copy.store))
    icon = Path(icon) if icon is not None else Path(copy.root) / ICON_FILE_NAME
    try:
        write_shortcut(entry, Shortcut(str(copy.interpreter), arguments, str(copy.root), SHORTCUT_DESCRIPTION,
                                       icon=str(icon) if icon.is_file() else ""))
    except OSError as exc:
        problems.append(f"the Start menu entry could not be written: {exc}")
        return None
    return entry


def _remove_entry(shortcut: Optional[Path], problems: List[str]) -> None:
    entry = Path(shortcut) if shortcut is not None else start_menu_shortcut()
    try:
        entry.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        problems.append(f"the Start menu entry could not be removed: {exc}")


def install(store_path: Path, jobs: Iterable[Job], *, source: Path = PROJECT_ROOT, target: Optional[Path] = None,
            interpreter: Optional[str] = None, shortcut: Optional[Path] = None, write_entry: bool = True,
            scheduler: Optional[Scheduler] = None, now: Optional[datetime] = None,
            account_store: Optional[Path] = None) -> InstallResult:
    """From a working copy, install or update the copy at ``target`` (default:
    ``installed/``) serving ``store_path``; in a portable release, set that release up
    where it lies. Raises InstallError when refused; then nothing has changed."""
    source = as_reached(source)
    now = now or datetime.now(timezone.utc)
    if copy_kind(source) is CopyKind.PORTABLE:
        return _set_up_release(store_path, jobs, source, interpreter, shortcut, write_entry, scheduler, now)
    blocker = _portable_set_up(_account_store(account_store, target, shortcut))
    target = Path(target) if target is not None else default_target(source)
    _refuse_from_installed_copy(source)
    _refuse_while_running(store_path)
    if blocker is not None:
        raise InstallError(f"automatic runs use the copy of MirrorHold at {blocker.root}; remove it there first")
    existing = read_installation(target)
    if existing is not None and not existing.serves(store_path):
        raise InstallError(f"the installed copy serves another job file ({existing.store}); uninstall it first")
    stage, old = _sibling(target, ".new"), _sibling(target, ".old")
    problems: List[str] = []
    try:
        _build(source, stage, target, store_path, windowless_interpreter(interpreter), now, problems)
    except (InstallError, OSError) as exc:
        _remove_tree(stage)
        if isinstance(exc, InstallError):
            raise
        raise InstallError(f"the installation could not be built: {exc}") from None
    if old.exists():
        _remove_tree(old)
    if target.exists():
        try:
            _rename(target, old)
        except OSError:
            _remove_tree(stage)
            raise InstallError("the installed copy is in use (a MirrorHold window or task started from it is "
                               "open); close it and try again") from None
    try:
        _rename(stage, target)
    except OSError as exc:
        if old.exists():
            _rename(old, target)
        _remove_tree(stage)
        raise InstallError(f"the new copy could not be put in place: {exc}") from None
    copy = read_installation(target)
    if copy is None:
        raise InstallError(f"the installed copy's record cannot be read back from {target}")
    if old.exists() and not _remove_tree(old):
        problems.append(f"the previous copy could not be removed completely: {old}")
    entry = _write_entry(copy, shortcut, problems) if write_entry else None
    automation = apply_automation(store_path, jobs, root=target, scheduler=scheduler)
    if not automation.ok:
        problems.append(automation.describe())
    return InstallResult(copy, entry, automation, tuple(problems))


def _set_up_release(store_path: Path, jobs: Iterable[Job], root: Path, interpreter: Optional[str],
                    shortcut: Optional[Path], write_entry: bool, scheduler: Optional[Scheduler],
                    now: datetime) -> InstallResult:
    """Make the portable release at ``root`` the copy the tasks and the entry start."""
    _refuse_while_running(store_path)
    holder = _source_holding_entry(shortcut)
    if holder is not None:
        raise InstallError(f"MirrorHold is installed from source on this account ({holder.source}); uninstall it "
                           "there first")
    release = read_release(root)
    copy = InstalledCopy(root, __version__, _utc(now), Path(store_path).resolve(), windowless_interpreter(interpreter),
                         None, InstallKind.PORTABLE, release.label if release is not None else None)
    try:
        write_installation(copy)
    except OSError as exc:
        raise InstallError(f"the setup record could not be written beside the job file: {exc}") from None
    problems: List[str] = []
    entry = _write_entry(copy, shortcut, problems, _keep_icon(copy, problems)) if write_entry else None
    automation = apply_automation(store_path, jobs, root=copy.record_folder, scheduler=scheduler)
    if not automation.ok:
        problems.append(automation.describe())
    return InstallResult(copy, entry, automation, tuple(problems))


def _remove_release(store_path: Path, shortcut: Optional[Path], scheduler: Optional[Scheduler],
                    only_from: Optional[Path] = None) -> UninstallResult:
    """Remove a portable release's setup: the tasks, the entry, the record and the icon kept
    beside the store. Its folder, and the folder of whichever release was set up, stay.

    With ``only_from``, a release's folder, the setup is removed only when it names that
    release; otherwise nothing changes, and nothing is refused, since nothing would be."""
    folder = runtime_layout(store_path).root
    if only_from is not None:
        copy = _portable_record(folder)
        if copy is None or not copy.runs_from(only_from):
            return UninstallResult(False, 0, portable=True, not_here=True, set_up=copy)
    _refuse_while_running(store_path)
    holder = _source_holding_entry(shortcut)
    if holder is not None and _portable_record(folder) is None:
        raise InstallError(f"MirrorHold is installed from source on this account ({holder.source}); uninstall it "
                           "there")
    try:
        removed = remove_all(scheduler or WindowsScheduler())
    except SchedulerError as exc:
        raise InstallError(f"the scheduled tasks could not be removed, so nothing was removed: {exc}") from None
    problems: List[str] = []
    if holder is None:
        _remove_entry(shortcut, problems)
    remove_installation(folder)
    try:
        (folder / ICON_FILE_NAME).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        problems.append(f"the icon kept beside the job file could not be removed: {exc}")
    return UninstallResult(False, len(removed.removed), tuple(problems), portable=True)


def uninstall(*, source: Path = PROJECT_ROOT, target: Optional[Path] = None, shortcut: Optional[Path] = None,
              scheduler: Optional[Scheduler] = None, store_path: Optional[Path] = None,
              only_if_here: bool = False) -> UninstallResult:
    """From a working copy, remove the tasks, the Start menu entry and the installed copy at
    ``target``; in a portable release, remove the setup of the store at ``store_path``
    (default: the portable store) whichever release it names — or, with ``only_if_here``,
    only when it names this one. Raises InstallError when refused; then nothing has
    changed.

    ``only_if_here`` is what the setup program's uninstaller asks before it deletes the
    release's folder: a release that is not the one set up must leave the other copy's
    automatic runs as they are. A working copy refuses it, its installed copy being always
    its own."""
    source = as_reached(source)
    if copy_kind(source) is CopyKind.PORTABLE:
        return _remove_release(Path(store_path) if store_path is not None else portable_store_path(), shortcut,
                               scheduler, source if only_if_here else None)
    if only_if_here:
        raise InstallError("only a release removes its setup only if it is the copy set up; a working copy's "
                           "uninstall removes its own installed copy")
    target = Path(target) if target is not None else default_target(source)
    _refuse_from_installed_copy(source)
    copy = read_installation(target)
    if copy is not None:
        _refuse_while_running(copy.store)
    removing = _sibling(target, ".removing")
    had_copy = target.exists()
    if had_copy:
        try:
            _rename(target, removing)
        except OSError:
            raise InstallError("the installed copy is in use (a MirrorHold window or task started from it is "
                               "open); close it and try again") from None
    try:
        removed = remove_all(scheduler or WindowsScheduler())
    except SchedulerError as exc:
        if removing.exists():
            _rename(removing, target)
        raise InstallError(f"the scheduled tasks could not be removed, so nothing was uninstalled: {exc}") from None
    problems: List[str] = []
    _remove_entry(shortcut, problems)
    if removing.exists() and not _remove_tree(removing):
        problems.append(f"some files of the installed copy could not be removed: {removing}")
    return UninstallResult(had_copy, len(removed.removed), tuple(problems))
