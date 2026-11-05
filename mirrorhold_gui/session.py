"""The GUI's connection to the core: one store, the volumes, the runs it started. No toolkit.

Everything the windows do to the configuration goes through ``GuiSession``, which
remembers what the job file held when it was read. A save hands that to the store, which
refuses it — never saving over the top — when the file changed since (``StoreConflict``):
the command line, or a second window, may have saved in between.

Runs are never applied here. ``start_run`` hands a job to ``mirrorhold.run.launch``,
with the destinations to run when the user chose some — by marker, which the run finds in
the job as it reads the job file itself, so a save that reorders the job meanwhile changes
nothing of which it serves — and a preview's ticket or the go-ahead for first backups when
the user gave one,
which starts a core process of its own; the session keeps the launch only to report a
run that could not start at all. ``start_verification`` does the same for a full
verification of one destination, and ``start_repair`` for a repair of the copies a
destination's verification found to differ, each destination named by its marker.

Scheduled tasks are never registered here either: after every save the session asks the
core's automation (``apply_automation``) to bring them into line with the saved jobs,
and keeps its outcome for the window to report — except a save of the window's
remembered state alone (``save_view_state``) or of a new order of the jobs and groups
(``save_order``), each of which leaves the jobs as they were.
Installing and uninstalling — in a portable release, setting it up and removing that — are
the core's installer, called with the session's store.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from mirrorhold.automation.reconcile import AutomationOutcome, apply_automation
from mirrorhold.automation.scheduler import Scheduler
from mirrorhold.endpoints.cloud import SyncRoot, sync_roots_on_this_computer
from mirrorhold.endpoints.volumes import VolumeScan, VolumeSource, default_volume_source
from mirrorhold.installation.installer import (InstallationState, InstallResult, UninstallResult, install,
                                               installation_state, uninstall)
from mirrorhold.jobs.store import ConfigStore, LoadResult, LoadStatus, StoreBusy, StoreContents
from mirrorhold.jobs.store import StoreConflict as CoreConflict
from mirrorhold.locations import PROJECT_ROOT, RuntimeLayout, default_store_path, runtime_layout
from mirrorhold.remotes import seal
from mirrorhold.run.launch import Launch, start_repair, start_run, start_verification

Opener = Callable[[Path], None]
Starter = Callable[..., Launch]


class StoreConflict(Exception):
    """The job file changed since this window read it."""


class NotStarted(Exception):
    """A run or preview the window asked for that was not started; the message says why."""


def open_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        raise OSError("opening a folder needs Windows")


@dataclass(frozen=True)
class LaunchProblem:
    job_name: str
    message: str
    what: str = "run"                # "run", "verification" or "repair"


class GuiSession:
    def __init__(self, store_path: Optional[Path] = None, *, volume_source: Optional[VolumeSource] = None,
                 sync_roots: Optional[Sequence[SyncRoot]] = None, opener: Opener = open_in_explorer,
                 starter: Starter = start_run, scheduler: Optional[Scheduler] = None,
                 install_root: Optional[Path] = None, shortcut: Optional[Path] = None,
                 verifier: Starter = start_verification, account_store: Optional[Path] = None,
                 source: Path = PROJECT_ROOT, repairer: Starter = start_repair):
        """``scheduler``, ``install_root``, ``shortcut`` and ``account_store`` name another
        Task Scheduler, installed copy, Start menu entry and portable job file than the
        account's own, and ``source`` another copy than the running one; ``starter``,
        ``verifier`` and ``repairer`` start runs, verifications and repairs (tests)."""
        self.store = ConfigStore(Path(store_path) if store_path else default_store_path())
        self.layout: RuntimeLayout = runtime_layout(self.store.path)
        self._volume_source = volume_source
        self._sync_roots = tuple(sync_roots) if sync_roots is not None else None
        self.opener = opener
        self._starter = starter
        self._verifier = verifier
        self._repairer = repairer
        self._scheduler = scheduler
        self._install_root = install_root
        self._shortcut = shortcut
        self._account_store = account_store
        self._source = Path(source)
        self.loaded: Optional[LoadResult] = None
        self.contents = StoreContents()
        self._fingerprint: Optional[str] = None
        self._launches: List[Tuple[str, Launch, str]] = []     # (job name, launch, what it started)
        self.automation: Optional[AutomationOutcome] = None

    # --- the job file ----------------------------------------------------------------

    def load(self) -> LoadResult:
        self.loaded = self.store.load()
        self.contents = self.loaded.contents
        self._fingerprint = self.loaded.fingerprint
        return self.loaded

    @property
    def writable(self) -> bool:
        return self.loaded is not None and self.loaded.status is not LoadStatus.UNSUPPORTED

    def changed_outside(self) -> bool:
        """Whether the job file differs from what this session last read or wrote; False
        while another program keeps it, the next look telling."""
        try:
            return self.store.fingerprint() != self._fingerprint
        except StoreBusy:
            return False

    def save(self, contents: StoreContents) -> None:
        """Save, unless the file changed since it was read (StoreConflict)."""
        self._write(contents)
        self.automation = apply_automation(self.store.path, contents.jobs, root=self._install_root,
                                           scheduler=self._scheduler, source=self._source)

    def save_order(self, contents: StoreContents) -> None:
        """Save a new order of the jobs and groups, refused as ``save`` is. No job changed,
        so the scheduled tasks are left alone."""
        self._write(contents)

    def save_view_state(self, gui_state: Mapping[str, Any]) -> None:
        """Save the window's remembered state and nothing else, refused as ``save`` is. The
        jobs are as they were, so the scheduled tasks are left alone."""
        preferences = replace(self.contents.preferences, gui_state=dict(gui_state))
        self._write(replace(self.contents, preferences=preferences))

    def _write(self, contents: StoreContents) -> None:
        if not self.writable:
            raise StoreConflict("the job file belongs to a newer version of MirrorHold and is not changed")
        try:
            self._fingerprint = self.store.save(contents, expected=self._fingerprint)
        except CoreConflict:
            raise StoreConflict("the job file changed since this window read it") from None
        self.contents = contents

    # --- installation ------------------------------------------------------------------

    def installation(self) -> InstallationState:
        return installation_state(self.store.path, source=self._source, target=self._install_root,
                                  shortcut=self._shortcut, account_store=self._account_store)

    def install(self) -> InstallResult:
        """Install or update the copy automatic runs use — in a portable release, set this
        release up. Raises InstallError."""
        result = install(self.store.path, self.contents.jobs, source=self._source, target=self._install_root,
                         shortcut=self._shortcut, scheduler=self._scheduler, account_store=self._account_store)
        self.automation = result.automation
        return result

    def uninstall(self) -> UninstallResult:
        """Remove the installed copy (a portable release: its setup), its tasks and its Start
        menu entry. Raises InstallError."""
        return uninstall(source=self._source, target=self._install_root, shortcut=self._shortcut,
                         scheduler=self._scheduler, store_path=self.store.path)

    def restore(self, copy: Path) -> LoadResult:
        self.loaded = self.store.restore(copy)
        self.contents = self.loaded.contents
        self._fingerprint = self.loaded.fingerprint
        return self.loaded

    def password_kept(self) -> bool:
        """Whether a password is remembered beside the job file, looked for again for the
        moment a new one is renamed over it (``seal.present``); it is not opened to tell."""
        return seal.present(self.layout.remote_password)

    # --- the machine -----------------------------------------------------------------

    def volume_source(self) -> VolumeSource:
        return self._volume_source or default_volume_source()

    def scan(self) -> VolumeScan:
        return self.volume_source().scan()

    def sync_roots(self) -> Tuple[SyncRoot, ...]:
        """The synchronised folders, read afresh each time (a few milliseconds), so a folder
        a cloud client took up while the window was open is seen; the injected ones in tests."""
        return self._sync_roots if self._sync_roots is not None else sync_roots_on_this_computer()

    # --- runs --------------------------------------------------------------------------

    def start_run(self, job_id: str, *, destinations: Optional[Sequence[str]] = None, ticket_id: Optional[str] = None,
                  first_backup: bool = False) -> Launch:
        """``destinations``: the markers of the destinations to run, None for every one —
        which the starter is then not told, as the command line names none; NotStarted when
        the job file no longer holds one of them (``check_chosen``). ``first_backup``: the
        user's go-ahead for the run's first backups onto folders that hold nothing, which the
        run applies only while they still do."""
        narrowed = {}
        if destinations is not None:
            self.check_chosen(job_id, destinations)
            narrowed = {"destinations": tuple(destinations)}
        return self._keep(job_id, self._starter(self.store.path, job_id, ticket_id=ticket_id,
                                                first_backup=first_backup, **narrowed), "run")

    def check_chosen(self, job_id: str, markers: Sequence[str]) -> None:
        """NotStarted when the job file as it stands now cannot be read, or no longer holds
        the job or one of the destinations ``markers``, so that the user hears why at once;
        the run itself finds them by marker in its own read of the file."""
        try:
            contents = self.store.load().contents
        except StoreBusy:
            raise NotStarted("the job file could not be read: another program kept it open; try again") from None
        job = contents.job(job_id)
        if job is None or any(job.number_of(marker) is None for marker in markers):
            raise NotStarted("a destination chosen is no longer in the job, which changed since this window read it")

    def start_verification(self, job_id: str, destination: str, *, restart: bool = False,
                           new_copies: bool = False) -> Launch:
        """Start a verification of a job's destination, named by its marker — full, or of its
        new copies — as a core process."""
        return self._keep(job_id, self._verifier(self.store.path, job_id, destination, restart=restart,
                                                 new_copies=new_copies), "verification")

    def start_repair(self, job_id: str, destination: str) -> Launch:
        """Start a repair of a job's destination, named by its marker, as a core process: the
        copies its latest finished verification found to differ, replaced by the source's."""
        return self._keep(job_id, self._repairer(self.store.path, job_id, destination), "repair")

    def _keep(self, job_id: str, launch: Launch, what: str) -> Launch:
        job = self.contents.job(job_id)
        self._launches.append((job.name if job else job_id, launch, what))
        return launch

    def launch_problems(self) -> List[LaunchProblem]:
        """Runs, verifications and repairs this session started that ended without running, each
        reported once. A launch that ran, or never will be asked about again, has its file
        removed."""
        problems, still = [], []
        for name, launch, what in self._launches:
            if launch.ended() is None:
                still.append((name, launch, what))
                continue
            message = launch.problem()
            if message:
                problems.append(LaunchProblem(name, message, what))
            launch.discard()
        self._launches = still
        return problems

    @property
    def launches_running(self) -> int:
        return sum(1 for _, launch, _what in self._launches if launch.ended() is None)
