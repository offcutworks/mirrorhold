"""The Preferences dialog: the sync engine, remote destinations, the notifications, runs on one
drive, the computer while work goes on, the installation. No toolkit.

The engine is checked before it is saved. Remote destinations are the rclone configuration
every remote is reached through and how it is unlocked (``read_remotes``): no password, the
password remembered for the user's Windows account, or a command of theirs that prints it.
The file is the user's, made with rclone, and is never read here: the engine opens it
(``check_configuration``). A password typed here goes straight to be sealed
(``remember``) and is kept nowhere else; choosing otherwise removes what was remembered, as
``password clear`` does, once the job file no longer names it. The notification choices say which run
endings raise a Windows notification. Runs on one drive is the user's choice when runs,
verifications and repairs that need one drive take it one at a time rather than work at the
same time — automatically, by what the drive is, or always, or never
(``Activities.drive_turns``); every waiting one reads it at its next attempt. Beneath it
each drive attached now, and each the user marked that is not, may be said to be a hard
disk, a solid-state drive or a cloud drive in place of what its disk reports
(``Activities.drive_kinds``, ``drive_rows``). Keeping the computer awake while a run,
verification or repair works is another (``Activities.keep_awake``), and naming that work
on Windows' shutdown screen a last (``Activities.name_at_shutdown``); one already working
follows a change of either within seconds (``mirrorhold.power``). The installation is the copy automatic runs and the
Start menu entry use: its state in words, and what this window may do to it.

- In a working copy the section is *Installation*: install, update or uninstall the copy
  built beside it — never from the installed copy's own window, which would replace or
  delete the files it runs from, nor while a portable release is set up on this account.
- In a portable release it is *Automatic runs and Start menu*: set this release up where
  it lies, set it up here when another copy was (a newer release unpacked beside an older
  one, or the folder moved), or remove the setup; never while MirrorHold is installed
  from source on this account. Its buttons are always shown, enabled by the state.
"""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from mirrorhold.automation.reconcile import AutomationOutcome, AutomationState
from mirrorhold.endpoints.volumes import DriveKind, VolumeInfo, drive_name, lasting_drive
from mirrorhold.engine.rclone import Engine, EngineError, PINNED_VERSION, open_engine, pinned_engine_path
from mirrorhold.installation.installer import InstallationState, InstallResult
from mirrorhold.installation.release import release_name
from mirrorhold.jobs.model import DriveMark, DriveTurns, Notifications, Preferences, RemoteSettings, Unlock
from mirrorhold.contract_errors import ContractError
from mirrorhold.locations import as_reached
from mirrorhold.remotes.reach import PasswordRefused, RemoteSetupError, has_a_password, open_session, remember_password
from mirrorhold.remotes.seal import REMINDER

from .formatting import local_time, sentence, size
from .remote_model import window_message


@dataclass(frozen=True)
class EngineCheck:
    usable: bool
    text: str


def check_engine(path: Optional[str], *, opener: Callable[[Optional[str]], Engine] = open_engine) -> EngineCheck:
    """``path`` None: the pinned copy shipped with the tool."""
    try:
        engine = opener(path)
    except EngineError as exc:
        return EngineCheck(False, str(exc))
    which = "the pinned copy" if path is None else str(engine.path)
    return EngineCheck(True, f"rclone {engine.version} - checked ({which})")


def with_engine(preferences: Preferences, path: Optional[str]) -> Preferences:
    return replace(preferences, engine_path=path.strip() if path and path.strip() else None)


REMOTES_TITLE = "Remote destinations"
CONFIGURATION_LABEL = "rclone configuration"
NO_PASSWORD = "No password"
REMEMBER_PASSWORD = "Remember its password"
COMMAND_PASSWORD = "A command prints it"
COMMAND_NOTE = "What the command prints is the configuration's password."
REMEMBER_NOTE = REMINDER
REMEMBERED_TITLE = "Password remembered"
PASSWORD_KEPT = "A password is remembered."
SAVE_CHECKS = "Save checks a new password."
NO_COMMAND = "Type the command that prints the password."
TYPE_PASSWORD = "Type the password of this configuration."


@dataclass(frozen=True)
class RemotesDraft:
    """What the section holds as typed."""

    configuration: str = ""
    unlock: Unlock = Unlock.NONE
    command: str = ""
    password: str = ""


@dataclass(frozen=True)
class RemotesPlan:
    """What saving the section does: the settings to keep, and whether ``draft.password`` is
    remembered first; ``problem`` says why it cannot be saved instead."""

    settings: Optional[RemoteSettings] = None
    remember: bool = False
    problem: str = ""


def remotes_draft(settings: RemoteSettings) -> RemotesDraft:
    return RemotesDraft(settings.configuration or "", settings.unlock, command_text(settings.unlock_command))


def command_text(words: Sequence[str]) -> str:
    """A command as one line, as Windows quotes it."""
    return subprocess.list2cmdline(words)


def command_words(text: str) -> Tuple[str, ...]:
    """A command typed on one line, read by Windows' own rule: what ``command_text`` writes,
    and a program's arguments, are read back as the program would read them."""
    text = text.strip()
    if not text:
        return ()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    words = shell32.CommandLineToArgvW(text, ctypes.byref(count))
    if not words:
        return ()
    try:
        return tuple(words[i] for i in range(count.value))
    finally:
        kernel32.LocalFree(words)


def read_remotes(draft: RemotesDraft, before: RemoteSettings) -> RemotesPlan:
    """The section as it would be saved. ``before``: what the preferences hold, which a
    section left as it is keeps, whatever became of the file: a drive of its own may be
    away, and that is no reason to refuse the window's other choices. A password remembered
    opens the file it was remembered for, so another file needs its own."""
    if draft == remotes_draft(before):
        return RemotesPlan(before)
    text = draft.configuration.strip().strip('"').strip()
    if not text:
        return RemotesPlan(RemoteSettings())
    path = str(as_reached(text))
    if not Path(path).is_file():
        return RemotesPlan(problem=f"{path}: no such file; make the configuration with rclone first.")
    try:
        if draft.unlock is Unlock.COMMAND:
            words = command_words(draft.command)
            if not words:
                return RemotesPlan(problem=NO_COMMAND)
            return RemotesPlan(RemoteSettings(path, Unlock.COMMAND, words))
        if draft.unlock is Unlock.REMEMBERED:
            if draft.password:
                return RemotesPlan(RemoteSettings(path, Unlock.REMEMBERED), remember=True)
            if before.unlock is Unlock.REMEMBERED and before.configuration == path:
                return RemotesPlan(before)
            return RemotesPlan(problem=TYPE_PASSWORD)
        return RemotesPlan(RemoteSettings(path))
    except ContractError as exc:
        return RemotesPlan(problem=sentence(f"{exc}."))


def check_configuration(preferences: Preferences, store_path: Path, settings: RemoteSettings) -> Tuple[bool, str]:
    """Whether the engine opens the configuration ``settings`` name, and what to say of it:
    the remotes it holds, or why it cannot be opened. The remote services are not asked."""
    try:
        with open_session(replace(preferences, remotes=settings), store_path) as session:
            names = session.remotes
    except RemoteSetupError as exc:
        return False, window_message(exc.reason)
    return True, f"Remotes in it: {', '.join(names)}" if names else "It holds no remote."


def remember(preferences: Preferences, store_path: Path, settings: RemoteSettings, password: str) -> str:
    """Remember ``password`` for the configuration ``settings`` name; "" when it opens it,
    else why it was not remembered."""
    candidate = replace(preferences, remotes=settings)
    try:
        has_a_password(candidate, store_path)
        remember_password(candidate, store_path, password)
    except RemoteSetupError as exc:
        return window_message(exc.reason)
    except PasswordRefused as exc:
        return window_message(str(exc))
    return ""


def with_remotes(preferences: Preferences, settings: RemoteSettings) -> Preferences:
    return replace(preferences, remotes=settings)


def with_notifications(preferences: Preferences, *, on_completion: bool, on_hold: bool,
                       on_failure: bool) -> Preferences:
    return replace(preferences, notifications=Notifications(on_completion, on_hold, on_failure))


def with_drive_turns(preferences: Preferences, turns: DriveTurns) -> Preferences:
    """The preferences with ``turns`` for runs on one drive; the other choices for activities
    as they were."""
    return replace(preferences, activities=replace(preferences.activities, drive_turns=turns))


def with_keep_awake(preferences: Preferences, keep_awake: bool) -> Preferences:
    """The preferences with the computer kept awake while a run, verification or repair
    works, or not; the other choices for activities as they were."""
    return replace(preferences, activities=replace(preferences.activities, keep_awake=keep_awake))


def with_name_at_shutdown(preferences: Preferences, name_at_shutdown: bool) -> Preferences:
    """The preferences with the work named on Windows' shutdown screen, or not; the other
    choices for activities as they were."""
    return replace(preferences, activities=replace(preferences.activities, name_at_shutdown=name_at_shutdown))


DRIVE_TITLE = "Runs on one drive"
DRIVE_QUESTION = "When runs, verifications or repairs need the same drive:"
AUTOMATIC = "Automatic: one at a time on a hard disk, at the same time on a solid-state or cloud drive"
ONE_AT_A_TIME = "Always one at a time: each waits for its turn"
AT_THE_SAME_TIME = "Always at the same time: they share the drive's speed"
DRIVE_CHOICES = ((DriveTurns.AUTOMATIC, AUTOMATIC), (DriveTurns.ONE_AT_A_TIME, ONE_AT_A_TIME),
                 (DriveTurns.AT_THE_SAME_TIME, AT_THE_SAME_TIME))
DRIVE_NOTE = ("A drive is one disk: two letters on one disk are one drive. Automatic takes a drive that does "
              "not say what it is, as many USB drives do not, as a hard disk, unless you say below what it is. "
              "Activities that reach a remote always take turns with each other.")

KINDS_TITLE = "What each drive is, for Automatic:"
LOOKING = "Looking at the drives..."
NOT_ATTACHED = " (not attached)"
AS_REPORTED = "As Windows reports"
REPORTED = {DriveKind.SOLID: f"{AS_REPORTED}: solid-state drive", DriveKind.SPINNING: f"{AS_REPORTED}: hard disk",
            DriveKind.CLOUD: f"{AS_REPORTED}: no disk beneath it, taken for a cloud drive",
            DriveKind.UNKNOWN: f"{AS_REPORTED}: not said, taken for a hard disk"}
MARKS = ((DriveKind.SPINNING, "Hard disk"), (DriveKind.SOLID, "Solid-state drive"), (DriveKind.CLOUD, "Cloud drive"))
NO_LASTING_NAME = "Windows gives this drive no lasting name, so what it is cannot be kept."


def drives_unreadable(reason: str) -> str:
    return f"The drives could not be read: {reason}."


@dataclass(frozen=True)
class DriveRow:
    """One drive in *Runs on one drive*: attached now, or marked and not attached. ``name``
    is how a person calls it now, or as it was when it was last seen; ``reported`` is what
    its disk reports, not known for a drive not attached; ``mark`` what the user says it is."""

    drive: str
    name: str
    attached: bool
    reported: DriveKind = DriveKind.UNKNOWN
    mark: Optional[DriveKind] = None

    @property
    def markable(self) -> bool:
        return lasting_drive(self.drive)

    @property
    def label(self) -> str:
        return self.name if self.attached else self.name + NOT_ATTACHED

    @property
    def choices(self) -> Tuple[Tuple[Optional[DriveKind], str], ...]:
        """What it may be set to: as its disk reports (no mark), or one of ``MARKS``."""
        return ((None, REPORTED[self.reported] if self.attached else AS_REPORTED),) + MARKS

    @property
    def chosen(self) -> str:
        return next(text for kind, text in self.choices if kind is self.mark)


def _told_apart(volumes: Sequence[VolumeInfo], drive: str) -> str:
    """A drive's name with its size and file systems after it — "a drive with no letter
    (1.8 TiB, NTFS)" — for a drive whose name another attached drive shares."""
    on = [v for v in volumes if v.drive == drive]
    systems = ", ".join(sorted({v.file_system for v in on if v.file_system}))
    told = size(sum(v.capacity_bytes for v in on)) + (f", {systems}" if systems else "")
    return f"{drive_name(volumes, drive)} ({told})"


def drive_rows(volumes: Sequence[VolumeInfo], marks: Sequence[DriveMark]) -> Tuple[DriveRow, ...]:
    """Every drive attached now, by name, then every marked drive not attached, by name. A
    name two attached drives share — "a drive with no letter" — is told apart by each one's
    size and file systems (``_told_apart``)."""
    marked = {mark.drive: mark for mark in marks}
    drives = list(dict.fromkeys(volume.drive for volume in volumes))
    names = {drive: drive_name(volumes, drive) for drive in drives}
    shared = {name for name in names.values() if list(names.values()).count(name) > 1}
    attached: Dict[str, DriveRow] = {}
    for drive in drives:
        mark = marked.get(drive)
        kind = next(v.drive_kind for v in volumes if v.drive == drive)
        name = _told_apart(volumes, drive) if names[drive] in shared else names[drive]
        attached[drive] = DriveRow(drive, name, True, kind, mark.kind if mark is not None else None)
    away = [DriveRow(m.drive, m.name, False, mark=m.kind) for m in marks if m.drive not in attached]
    by_name = lambda row: (row.name.casefold(), row.drive)
    return tuple(sorted(attached.values(), key=by_name)) + tuple(sorted(away, key=by_name))


def kind_box_width(rows: Sequence[DriveRow]) -> int:
    """The width in characters every drive's box takes: that of the longest choice of any of
    ``rows``, so that the boxes line up whatever each drive reports."""
    return max((len(text) for row in rows for _kind, text in row.choices), default=0)


def with_drive_kinds(preferences: Preferences, rows: Sequence[DriveRow],
                     chosen: Mapping[str, Optional[DriveKind]]) -> Preferences:
    """The preferences with each of ``rows`` marked as ``chosen`` says by its drive — None
    for as its disk reports — and a row ``chosen`` does not name as it was; a drive that
    cannot keep a mark keeps none, and a mark ``preferences`` holds for a drive no row shows
    — one saved since the rows were read — is kept. The other choices for activities as
    they were."""
    shown = {row.drive for row in rows}
    marks = [mark for mark in preferences.activities.drive_kinds if mark.drive not in shown]
    for row in rows:
        kind = chosen.get(row.drive, row.mark)
        if kind is not None and row.markable:
            marks.append(DriveMark(row.drive, row.name, kind))
    return replace(preferences, activities=replace(preferences.activities, drive_kinds=tuple(marks)))

WORK_TITLE = "While a run, verification or repair works"
KEEP_AWAKE = "Keep the computer from going to sleep"
KEEP_AWAKE_NOTE = "The computer's own sleep timer waits until the work ends; Sleep from the Start menu still works."
NAME_AT_SHUTDOWN = "Show it on Windows' shutdown screen"
NAME_AT_SHUTDOWN_NOTE = ("A shutdown then waits for you to choose Shut down anyway or Cancel. If the work ends first, "
                         "MirrorHold stops holding it up; work a shutdown cuts continues next time.")


PINNED_DESCRIPTION = f"The pinned copy shipped with MirrorHold (rclone {PINNED_VERSION})"


def pinned_location() -> str:
    return str(pinned_engine_path())


SOURCE_NOTE = ("Uninstalling removes the scheduled tasks, the Start menu entry and the installed copy; it never "
               "touches a job, a record or a destination.")
PORTABLE_TITLE = "Automatic runs and Start menu"
PORTABLE_NOTE = ("Remove takes away the scheduled tasks and the Start menu entry; your jobs, their records and every "
                 "destination stay. Delete this folder afterwards to remove MirrorHold completely.")


@dataclass(frozen=True)
class InstallationView:
    text: str                 # the state in words
    can_install: bool
    can_uninstall: bool
    install_label: str        # "Install", "Install / update", "Set up", "Set up again" or "Set up here"
    title: str = "Installation"
    uninstall_label: str = "Uninstall"
    note: str = SOURCE_NOTE   # the hint of the uninstall (or remove) button
    busy: str = "Installing..."
    remove_question: str = ("Uninstall MirrorHold? The scheduled tasks, the Start menu entry and the installed copy "
                            "are removed, so no job runs by itself any more. Your jobs, their records and every "
                            "destination are kept.")
    done_title: str = "Install"   # the title of what the window says once it is done


def _label(label: Optional[str]) -> str:
    return f"release {label}" if label else "release unknown"


def _portable(text: str, can_set_up: bool, can_remove: bool, set_up: str) -> InstallationView:
    return InstallationView(text, can_set_up, can_remove, set_up, PORTABLE_TITLE, "Remove", PORTABLE_NOTE,
                            "Setting up...",
                            "Remove MirrorHold's automatic runs and Start menu entry? No job runs by itself any "
                            "more. Your jobs, their records, every destination and this folder are kept.", "Set up")


def _portable_view(state: InstallationState) -> InstallationView:
    copy = state.copy
    kept = f"Your jobs and their records are kept in {state.store_folder}."
    if copy is None:
        if state.blocker is not None:
            return _portable(f"Automatic runs and the Start menu entry use MirrorHold installed from source "
                             f"({state.blocker.source}). Uninstall it there before setting up this copy.",
                             False, False, "Set up")
        return _portable("MirrorHold runs from this folder without installing; jobs run only when you start them. "
                         "Set it up to have jobs run when a drive is attached or on a schedule, and to add "
                         f"MirrorHold to the Start menu.\n\n{kept}", True, False, "Set up")
    if state.here:
        return _portable(f"Automatic runs and the Start menu entry use this copy of MirrorHold "
                         f"({release_name(state.release)}), set up on {local_time(copy.installed_at)}. Keep this "
                         f"folder where it is: moving or deleting it stops automatic runs.\n\n{kept}", True, True,
                         "Set up again")
    if state.gone:
        return _portable(f"Automatic runs cannot start: the copy of MirrorHold they use is gone ({copy.root}). Set "
                         "up this copy to run them from here.", True, True, "Set up here")
    return _portable(f"Automatic runs and the Start menu entry use the copy of MirrorHold at {copy.root} "
                     f"({_label(copy.label)}). Set up this copy to use it instead; you can then delete the other "
                     "folder.", True, True, "Set up here")


def installation_view(state: InstallationState) -> InstallationView:
    if state.portable:
        return _portable_view(state)
    copy = state.copy
    if state.running_installed:
        where = f" in {copy.source}" if copy is not None else ""
        return InstallationView(f"This window runs the installed copy (version {copy.version if copy else '?'}). "
                                f"Install, update and uninstall from the working copy{where}.", False, False,
                                "Install / update")
    if copy is None:
        if state.blocker is not None:
            return InstallationView(f"Automatic runs and the Start menu entry use the copy of MirrorHold at "
                                    f"{state.blocker.root} ({_label(state.blocker.label)}). Installing from this "
                                    "working copy is not available while it is: remove it there first.", False, False,
                                    "Install")
        return InstallationView("Not installed. Automatic runs and the Start menu entry need the installed copy; "
                                "until then jobs run only when you start them.", True, False, "Install")
    when = local_time(copy.installed_at)
    if not state.serves_store:
        return InstallationView(f"Installed on {when} for another job file ({copy.store}); automatic runs of these "
                                "jobs do not take effect.", False, True, "Install / update")
    return InstallationView(f"Installed: version {copy.version}, on {when}. Automatic runs and the Start menu "
                            "entry use this copy; install again after changing MirrorHold.", True, True,
                            "Install / update")


def automation_words(state: InstallationState) -> str:
    """The *When to run* box's last line, which says why automatic choices would not take
    effect; "" when they take effect as saved, and the box then has no such line."""
    if state.portable:
        if state.copy is None:
            return "Automatic runs need MirrorHold set up (File > Preferences)."
        if state.gone:
            return "Automatic runs cannot start: the copy of MirrorHold they use is gone (File > Preferences)."
        return ""
    if state.installed and state.serves_store:
        return ""
    if state.installed or state.blocker is not None:
        return "Automatic runs of this job do not take effect: the installed copy serves another job file."
    return "Automatic runs need MirrorHold installed (File > Preferences)."


def install_done(result: InstallResult) -> str:
    """What the window says once an install, or a portable release's setup, is done."""
    automation = result.automation.describe().capitalize()
    if result.copy.portable:
        text = f"This copy is set up ({_label(result.copy.label)}). {automation}."
    else:
        text = f"Installed version {result.copy.version}. {automation}."
    if result.problems:
        text += "\n\n" + "\n".join(result.problems)
    return text


def set_up_here_question(state: InstallationState) -> Optional[str]:
    """What a portable release asks when it opens while another copy is set up: whether to
    use this copy instead. None when there is nothing to ask."""
    copy = state.copy
    if not state.portable or copy is None or state.here:
        return None
    if state.gone:
        first = f"Automatic runs cannot start: the copy of MirrorHold they use is gone ({copy.root})."
    else:
        named = f"MirrorHold {copy.label}" if copy.label else "MirrorHold"
        first = f"Automatic runs and the Start menu entry use {named} at {copy.root}."
    return f"{first} Use this copy ({release_name(state.release)}) instead?"


def automation_problem(outcome: Optional[AutomationOutcome]) -> Optional[str]:
    """What the window must tell the user after a save, if anything."""
    if outcome is None or outcome.state is not AutomationState.FAILED:
        return None
    return f"The jobs were saved, but the scheduled tasks could not be updated: {outcome.problem}"
