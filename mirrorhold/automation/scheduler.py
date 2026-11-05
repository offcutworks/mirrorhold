"""Task Scheduler, reached through one folder the tool owns.

``Scheduler`` is the seam: reconciliation lists, registers and deletes tasks in one
folder and never touches another, so every test runs against a scheduler in memory and
only ``WindowsScheduler`` reaches the real one (through its COM interface, ``pywin32``).
The folder is ``tasks.TASK_FOLDER`` in production; the live check of this tool's tasks
uses a test folder of its own, so it can never disturb a real installation's tasks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol

from .tasks import TASK_FOLDER

_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3
_TASK_ENUM_HIDDEN = 1
_NOT_FOUND = (-2147024894, -2147024893)      # HRESULTs of ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND


class SchedulerError(RuntimeError):
    """Task Scheduler refused or could not be reached."""


@dataclass(frozen=True)
class RegisteredTask:
    name: str
    description: str


class Scheduler(Protocol):
    folder: str

    def tasks(self) -> List[RegisteredTask]: ...

    def register(self, name: str, xml: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def remove_folder(self) -> None: ...


def _code(exc: Exception):
    """The error's HRESULT. A failure inside a COM call arrives as "exception occurred",
    with the real code in its exception detail (measured: a missing folder is reported
    as -2147352567 with -2147024894 in the detail)."""
    info = getattr(exc, "excepinfo", None)
    if info and len(info) > 5 and info[5]:
        return info[5]
    args = getattr(exc, "args", ())
    return args[0] if args else None


def _message(exc: Exception) -> str:
    info = getattr(exc, "excepinfo", None)
    if info and len(info) > 2 and info[2]:
        text = str(info[2]).strip()
    else:
        args = getattr(exc, "args", ())
        text = str(args[1]) if len(args) > 1 else str(exc)
    code = _code(exc)
    return f"{text} (0x{code & 0xFFFFFFFF:08X})" if isinstance(code, int) else text


def _not_found(exc: Exception) -> bool:
    return _code(exc) in _NOT_FOUND


class WindowsScheduler:
    """The real Task Scheduler, in ``folder`` (created when first registered into)."""

    def __init__(self, folder: str = TASK_FOLDER):
        self.folder = folder if folder.startswith("\\") else "\\" + folder

    def _service(self):
        try:
            import win32com.client
            service = win32com.client.Dispatch("Schedule.Service")
            service.Connect()
            return service
        except ImportError as exc:
            raise SchedulerError(f"Task Scheduler needs Windows and pywin32: {exc}") from None
        except Exception as exc:
            raise SchedulerError(f"Task Scheduler cannot be reached: {_message(exc)}") from None

    def _folder(self, service, *, create: bool):
        try:
            return service.GetFolder(self.folder)
        except Exception as exc:
            if not _not_found(exc):
                raise SchedulerError(f"the task folder {self.folder} cannot be opened: {_message(exc)}") from None
            if not create:
                return None
        try:
            return service.GetFolder("\\").CreateFolder(self.folder.lstrip("\\"))
        except Exception as exc:
            raise SchedulerError(f"the task folder {self.folder} cannot be created: {_message(exc)}") from None

    def tasks(self) -> List[RegisteredTask]:
        folder = self._folder(self._service(), create=False)
        if folder is None:
            return []
        try:
            return [RegisteredTask(t.Name, t.Definition.RegistrationInfo.Description or "")
                    for t in folder.GetTasks(_TASK_ENUM_HIDDEN)]
        except Exception as exc:
            raise SchedulerError(f"the tasks in {self.folder} cannot be listed: {_message(exc)}") from None

    def register(self, name: str, xml: str) -> None:
        folder = self._folder(self._service(), create=True)
        try:
            folder.RegisterTask(name, xml, _TASK_CREATE_OR_UPDATE, None, None, _TASK_LOGON_INTERACTIVE_TOKEN)
        except Exception as exc:
            raise SchedulerError(f"the task {name} cannot be registered: {_message(exc)}") from None

    def delete(self, name: str) -> None:
        folder = self._folder(self._service(), create=False)
        if folder is None:
            return
        try:
            folder.DeleteTask(name, 0)
        except Exception as exc:
            if not _not_found(exc):
                raise SchedulerError(f"the task {name} cannot be removed: {_message(exc)}") from None

    def remove_folder(self) -> None:
        """Remove the folder once it holds no task; a folder still holding one stays."""
        service = self._service()
        folder = self._folder(service, create=False)
        if folder is None:
            return
        try:
            if folder.GetTasks(_TASK_ENUM_HIDDEN).Count:
                return
            service.GetFolder("\\").DeleteFolder(self.folder.lstrip("\\"), 0)
        except Exception as exc:
            if not _not_found(exc):
                raise SchedulerError(f"the task folder {self.folder} cannot be removed: {_message(exc)}") from None
