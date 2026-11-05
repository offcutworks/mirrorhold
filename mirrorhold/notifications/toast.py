"""Showing a notice as a Windows notification, from a process with no window of its own.

A notice is raised by the core process that ran the job — a scheduled run, a run the
GUI started, a run from the command line — so it reaches the user with the GUI closed,
and stays in the notification centre afterwards. The process hands the toast to Windows
itself, through the Windows Runtime (``windows_runtime``): it starts no other program. A notice
that asks something of the user (``Notice.asks``) is a reminder toast, which Windows keeps on the
screen until the user closes it rather than for a few seconds; its one button is Windows' own
dismissal, which needs nothing of this process once the toast is shown. The button is named
Dismiss: left unnamed, Windows names it in the language of its own shell, which need not be
the account's preferred language: in a program written in English alone that leaves one
foreign word — measured in Windows Sandbox, with the account's preferred language en-US and
the shell in another language, a word meaning Cancel.

Windows shows a notification only for a registered application identity. Once
MirrorHold is installed its Start menu entry carries ``installation.shortcut.APP_ID`` and
notices appear under its name; before that they are shown under Windows PowerShell's
own identity rather than not at all.

``MIRRORHOLD_NOTIFICATIONS=off`` in the environment silences every notice of the
process and of the runs it starts: the test suites set it, so no test reaches the
desktop.
"""
from __future__ import annotations

import os
from typing import Callable, Optional, Protocol
from xml.sax.saxutils import escape

from ..installation.shortcut import APP_ID, start_menu_shortcut
from . import windows_runtime
from .message import Notice

SWITCH_VARIABLE = "MIRRORHOLD_NOTIFICATIONS"
POWERSHELL_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
"""Windows PowerShell's own application identity, registered on every Windows: the one a
notice is shown under before MirrorHold has a Start menu entry of its own."""


class Notifier(Protocol):
    def show(self, notice: Notice) -> None: ...


class NotificationError(RuntimeError):
    pass


def _shown(text: str) -> str:
    """``text`` with each unpaired surrogate — which a file name may hold, and neither XML
    nor a Windows string can — shown as the replacement character."""
    return "".join("\ufffd" if "\ud800" <= c <= "\udfff" else c for c in text)


def toast_xml(notice: Notice) -> str:
    """The notification's content in the toast schema: the title, then the lines. One that asks
    something of the user is a reminder, which stays on the screen until the user closes it with
    its button, Windows' own dismissal, named Dismiss."""
    body = "\n".join(notice.lines)
    visual = ('<visual><binding template="ToastGeneric">'
              f"<text>{escape(_shown(notice.title))}</text><text>{escape(_shown(body))}</text>"
              "</binding></visual>")
    if notice.asks:
        return (f'<toast scenario="reminder">{visual}'
                '<actions><action activationType="system" arguments="dismiss" content="Dismiss"/></actions></toast>')
    return f"<toast>{visual}</toast>"


class ToastNotifier:
    """Shows each notice as a Windows notification under ``app_id``, handing its toast to
    ``deliver`` (Windows itself unless given)."""

    def __init__(self, app_id: str, deliver: Callable[[str, str], None] = windows_runtime.show):
        self.app_id = app_id
        self._deliver = deliver

    def show(self, notice: Notice) -> None:
        """Raises NotificationError when Windows refused the notification."""
        try:
            self._deliver(toast_xml(notice), self.app_id)
        except windows_runtime.ToastRefused as exc:
            raise NotificationError(f"the notification could not be shown: {exc}") from None


class SilentNotifier:
    """Shows nothing: notifications switched off for this process."""

    def show(self, notice: Notice) -> None:
        pass


def notifications_switched_off() -> bool:
    return os.environ.get(SWITCH_VARIABLE, "").strip().lower() in ("off", "0", "no")


def default_notifier(shortcut: Optional[os.PathLike] = None) -> Notifier:
    """The notifier a core process uses: silent when switched off by the environment;
    under MirrorHold's identity once its Start menu entry exists, else PowerShell's."""
    if notifications_switched_off():
        return SilentNotifier()
    entry = shortcut if shortcut is not None else start_menu_shortcut()
    return ToastNotifier(APP_ID if os.path.isfile(entry) else POWERSHELL_APP_ID)
