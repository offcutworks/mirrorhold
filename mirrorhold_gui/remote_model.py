"""Reaching the user's remotes from the window, off its thread. No toolkit.

The window never reads the rclone configuration and never asks a remote at a look: it asks
the engine when the user acts — adding a remote destination, opening or changing a job that
has one, browsing a remote's folders, checking the configuration — always on a thread
(``Background``), since the engine takes seconds to start and a service may take far
longer to answer. ``RemoteAccess`` keeps one session for one window's length, so the
configuration is unlocked once however many folders are browsed.

The core's refusals name the command line's commands; ``window_reason`` says the same in
terms of the window.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Generic, Optional, Sequence, Tuple, TypeVar

from mirrorhold.endpoints.endpoint import RemoteEndpoint
from mirrorhold.engine.remote import RemoteError, RemoteSession
from mirrorhold.jobs.model import Preferences
from mirrorhold.remotes.facts import RemoteSetup, SetupState
from mirrorhold.remotes.reach import (NO_CONFIGURATION, NO_REMEMBERED_PASSWORD, RemoteSetupError, folders, gather,
                                      open_session)

from .formatting import sentence

NAME_CONFIGURATION = "Name the rclone configuration in File > Preferences first."
ASKING_REMOTES = "Asking the engine for the remotes..."
LOOKING_FOLDER = "Looking at this folder..."
NOT_THERE_YET = "This folder does not exist yet."
NO_FOLDERS = "No folders here."

T = TypeVar("T")


class Background(Generic[T]):
    """One piece of work on a thread of its own, which the window polls: ``done``, then
    ``value``, or ``failure`` holding why it raised."""

    def __init__(self, work: Callable[[], T]):
        self.done = False
        self.value: Optional[T] = None
        self.failure = ""
        self._work = work
        threading.Thread(target=self._run, name="mirrorhold-remote", daemon=True).start()

    def _run(self) -> None:
        try:
            self.value = self._work()
        except Exception as exc:  # shown in the window: work that fails says why
            self.failure = str(exc) or type(exc).__name__
        self._work = None                # it may hold what it was handed: a password, for one
        self.done = True


def window_reason(reason: str) -> str:
    """A core refusal as a clause the window says: its own way to the same remedy where the
    core's words name a command."""
    if reason == NO_CONFIGURATION:
        return "name the rclone configuration in File > Preferences first"
    if reason == NO_REMEMBERED_PASSWORD:
        return "no password is remembered: type it in File > Preferences"
    return reason


def window_message(reason: str) -> str:
    """A core refusal as a sentence of its own."""
    return sentence(window_reason(reason).rstrip(".")) + "."


def ask_setup(preferences: Preferences, store_path: Path, endpoints: Sequence[RemoteEndpoint]) -> RemoteSetup:
    """What the engine says of ``endpoints``, for the editor's checks; a failure of the
    asking itself reads as a configuration that cannot be used, with why."""
    try:
        return gather(preferences, store_path, endpoints)
    except (OSError, RemoteError) as exc:
        return RemoteSetup(SetupState.UNUSABLE, str(exc))


class RemoteAccess:
    """One session on the user's configuration for one window's length, opened by ``open``
    and ended by ``close``, both off the window's thread. A window closed while it opens
    ends the session as soon as it is open."""

    def __init__(self, preferences: Preferences, store_path: Path,
                 opener: Callable[[Preferences, Path], RemoteSession] = open_session):
        self._preferences = preferences
        self._store_path = store_path
        self._opener = opener
        self._lock = threading.Lock()
        self._session: Optional[RemoteSession] = None
        self._closed = False

    def open(self) -> Tuple[str, ...]:
        """The remotes the configuration holds; RemoteSetupError when it cannot be opened."""
        with self._lock:
            if self._closed:
                raise RemoteSetupError(SetupState.UNUSABLE, "the window was closed")
            if self._session is None:
                self._session = self._opener(self._preferences, self._store_path)
            return self._session.remotes

    def folders(self, endpoint: RemoteEndpoint) -> Optional[Tuple[str, ...]]:
        """The folders inside a remote's folder; None when it is not there."""
        with self._lock:
            session = self._session
        if session is None:
            raise RemoteSetupError(SetupState.UNUSABLE, "the configuration is not open")
        return folders(session, endpoint)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            session, self._session = self._session, None
        if session is not None:
            session.close()


def child_folder(folder: str, name: str) -> str:
    """``name`` inside ``folder``; a folder given from the remote's top keeps its '/'."""
    return f"{folder.rstrip('/')}/{name}" if folder.strip("/") else f"{folder}{name}"


def parent_folder(folder: str) -> str:
    """The folder holding ``folder``, itself at the remote's top."""
    inner = folder.rstrip("/")
    if not inner:
        return folder
    head, _, _name = inner.rpartition("/")
    return head or ("/" if inner.startswith("/") else "")


def at_top(folder: str) -> bool:
    """Whether ``folder`` is the remote's top, with nothing above it."""
    return not folder.strip("/")
