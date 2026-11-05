"""Reaching the user's rclone configuration and the remote folders jobs name, through one
engine serving a session (``engine.remote.RemoteSession``).

``configuration_for`` turns the preferences into what the engine is handed: the file's
path and its unlock — nothing, the user's own command, or this copy's ``unlock`` command
where the password is remembered (``invocation.unlock_command``). ``open_session`` starts
the engine with it, refusing at once, with why, when no configuration is named, the
remembered password is missing, the engine cannot be used or the configuration cannot be
opened. ``look`` asks the engine about one remote folder; ``reach`` does both and keeps
the session open for a run or a preview, whose remote folders it then serves as storage
(``Reached``), and ``gather`` does both for validation alone. ``folders`` lists the folders
inside a remote's folder, and ``has_a_password`` and ``remember_password`` remember the
configuration's password, for the command line and the window alike.

A failure to reach a remote is told apart from the service's own refusal by the engine's
reason (``engine.remote.unreachable``): the first waits, as a drive not attached does; the
second — a refused sign-in among them — is reported, never taken for an absence.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional, Tuple

from ..endpoints.endpoint import RemoteEndpoint
from ..engine.rclone import Configuration, Engine, EngineError, RemoteRoot, open_engine
from ..engine.remote import RemoteError, RemoteSession, unreachable
from ..invocation import unlock_command
from ..jobs.model import Preferences, RemoteSettings, Unlock
from ..locations import runtime_layout
from ..storage.remote import RemoteStorage
from . import seal
from .facts import Reach, RemoteFacts, RemoteSetup, SetupState

NO_CONFIGURATION = ("no rclone configuration is set: name the file you made with rclone with "
                    "'mirrorhold remotes --configuration FILE'")
NO_REMEMBERED_PASSWORD = "no password is remembered for this job file: set it with 'mirrorhold password set'"


class RemoteSetupError(Exception):
    """The configuration cannot be used now: ``state`` says how, the message why."""

    def __init__(self, state: SetupState, reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


class PasswordRefused(Exception):
    """A password was not remembered; why, in words."""


def configuration_for(settings: RemoteSettings, store_path: Path, *, sealed: Optional[Path] = None) -> Configuration:
    """The configuration the preferences name, with its unlock; RemoteSetupError when none
    is named, or the password is to be remembered and none is. ``sealed``: the seal a
    remembered password is read from instead, a password on trial (``remember_password``)."""
    if settings.configuration is None:
        raise RemoteSetupError(SetupState.MISSING, NO_CONFIGURATION)
    if settings.unlock is Unlock.COMMAND:
        unlock: Tuple[str, ...] = tuple(settings.unlock_command)
    elif settings.unlock is Unlock.REMEMBERED:
        if not seal.present(sealed or runtime_layout(store_path).remote_password):
            raise RemoteSetupError(SetupState.UNUSABLE, NO_REMEMBERED_PASSWORD)
        unlock = tuple(unlock_command(store_path, sealed=sealed))
    else:
        unlock = ()
    return Configuration(Path(settings.configuration), unlock)


def open_session(preferences: Preferences, store_path: Path, *, engine: Optional[Engine] = None,
                 sealed: Optional[Path] = None) -> RemoteSession:
    """A session on the configuration the preferences name, opened; RemoteSetupError, with
    why, when it cannot be. ``sealed``: as ``configuration_for``."""
    configuration = configuration_for(preferences.remotes, store_path, sealed=sealed)
    try:
        engine = engine or open_engine(preferences.engine_path)
    except EngineError as exc:
        raise RemoteSetupError(SetupState.UNUSABLE, f"the sync engine cannot be used: {exc}") from None
    try:
        return RemoteSession(engine, configuration)
    except RemoteError as exc:
        raise RemoteSetupError(SetupState.UNUSABLE, str(exc)) from None


def _parent(endpoint: RemoteEndpoint) -> Optional[Tuple[RemoteRoot, str]]:
    """The folder ``endpoint`` lies in and its own name, or None for a remote's top."""
    inner = endpoint.folder.rstrip("/")
    if not inner.strip("/"):
        return None
    head, _, name = inner.rpartition("/")
    return RemoteRoot(endpoint.remote, head if head or not inner.startswith("/") else "/"), name


def look(session: RemoteSession, endpoint: RemoteEndpoint) -> RemoteFacts:
    """What the engine says of one remote folder now: its remote's features, whether a file
    stands where the folder is named, and the space the service reports — or, when the
    engine could not ask, whether the service could not be reached or refused, with why."""
    root = RemoteRoot(endpoint.remote, endpoint.folder)
    try:
        parent = _parent(endpoint)
        if parent is None:
            session.list_folder(root, "")
        else:
            found = session.stat(parent[0], parent[1])
            if found is not None and not found.is_dir:
                return RemoteFacts(Reach.REACHED, is_file=True)     # the engine reports no features of a file
        features = session.features(root)
    except RemoteError as exc:
        reason = str(exc)
        return RemoteFacts(Reach.UNREACHABLE if unreachable(reason) else Reach.REFUSED, reason)
    # A free space the service cannot report — a folder not made yet among them: a local
    # remote asked about one fails to read its disk usage (measured) — is left unjudged.
    return RemoteFacts(Reach.REACHED, features=features, space=RemoteStorage(session, root, features).space())


def folders(session: RemoteSession, endpoint: RemoteEndpoint) -> Optional[Tuple[str, ...]]:
    """The names of the folders inside a remote's folder, by name; None when it is not there,
    which a service with no folders of its own never says. A RemoteError when the engine
    cannot answer."""
    entries = session.list_folder(RemoteRoot(endpoint.remote, endpoint.folder))
    if entries is None:
        return None
    return tuple(sorted((e.path for e in entries if e.is_dir), key=str.casefold))


def has_a_password(preferences: Preferences, store_path: Path) -> None:
    """Pass when the configuration the preferences name needs a password to open;
    RemoteSetupError when none is named, PasswordRefused when it opens without one."""
    settings = preferences.remotes
    plain = replace(settings, unlock=Unlock.NONE, unlock_command=())
    configuration_for(plain, store_path)
    try:
        open_session(replace(preferences, remotes=plain), store_path).close()
    except RemoteSetupError:
        return                                        # locked, as a configuration with a password is
    raise PasswordRefused(f"the configuration {settings.configuration} opens without a password: it is not "
                          "encrypted, so there is no password to remember")


def remember_password(preferences: Preferences, store_path: Path, password: str) -> RemoteSettings:
    """Seal ``password`` beside the store and open the configuration the preferences name
    with it; the settings that use it. The password is tried from a seal of its own
    (``seal.trial``) and replaces the remembered one only once it has opened the
    configuration, so an activity unlocking meanwhile reads what was remembered before. A
    password that cannot be sealed, does not open the configuration or cannot replace the
    remembered one is not kept, what was remembered before staying as it was
    (PasswordRefused)."""
    path = runtime_layout(store_path).remote_password
    remembered = RemoteSettings(preferences.remotes.configuration, Unlock.REMEMBERED)
    seal.sweep_trials(path)
    tried = seal.trial(path)
    try:
        try:
            seal.remember(tried, password)
        except seal.SealError as exc:
            raise PasswordRefused(f"the password was not remembered: {exc}") from None
        try:
            open_session(replace(preferences, remotes=remembered), store_path, sealed=tried).close()
        except RemoteSetupError as exc:
            raise PasswordRefused("the configuration could not be opened with this password, so it was not "
                                  f"remembered: {exc.reason}") from None
        try:
            seal.take_up(tried, path)
        except OSError as exc:
            raise PasswordRefused(f"the password was not remembered: {path} could not be written: "
                                  f"{exc.strerror or exc}") from None
    finally:
        seal.forget(tried)
    return remembered


class Reached:
    """The configuration opened for an activity's whole length — one unlock for all its
    work — and what the engine said of each remote folder it was asked about. ``session``
    is None when the configuration could not be opened, ``setup`` saying why; ``close``
    ends the engine."""

    def __init__(self, setup: RemoteSetup, session: Optional[RemoteSession] = None):
        self.setup = setup
        self.session = session

    def facts(self, endpoint: RemoteEndpoint) -> Optional[RemoteFacts]:
        return self.setup.facts.get(endpoint)

    def storage(self, endpoint: RemoteEndpoint) -> Optional[RemoteStorage]:
        """The remote folder's files through the session, when the engine reached it and no
        file stands where the folder is named; None otherwise."""
        found = self.facts(endpoint)
        if self.session is None or found is None or found.reach is not Reach.REACHED or found.features is None:
            return None
        return RemoteStorage(self.session, RemoteRoot(endpoint.remote, endpoint.folder), found.features)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def __enter__(self) -> "Reached":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def reach(preferences: Preferences, store_path: Path, endpoints: Iterable[RemoteEndpoint], *,
          engine: Optional[Engine] = None) -> Reached:
    """The configuration's state and the facts of each of ``endpoints`` whose remote it
    holds, through one session left open for the caller, who closes it."""
    try:
        session = open_session(preferences, store_path, engine=engine)
    except RemoteSetupError as exc:
        return Reached(RemoteSetup(exc.state, exc.reason))
    try:
        facts = {e: look(session, e) for e in dict.fromkeys(endpoints) if e.remote in session.remotes}
    except BaseException:
        session.close()
        raise
    return Reached(RemoteSetup(SetupState.READY, remotes=session.remotes, facts=facts), session)


def gather(preferences: Preferences, store_path: Path, endpoints: Iterable[RemoteEndpoint], *,
           engine: Optional[Engine] = None) -> RemoteSetup:
    """``reach``'s facts, the session closed before returning."""
    with reach(preferences, store_path, endpoints, engine=engine) as reached:
        return reached.setup
