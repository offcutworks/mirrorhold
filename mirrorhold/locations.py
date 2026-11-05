"""Where the tool's own files live, and which kind of copy this code is.

The code runs as one of three kinds of copy (``copy_kind``), told apart by a file at the
root of the folder the packages are imported from:

- a **working copy** — a source checkout — holds neither file;
- the **source installation's copy** (``installation``), built by ``install`` into
  ``installed/`` beside a working copy, holds ``installation.json``, naming the store
  it serves;
- a **portable release**, unpacked from a release zip and run where it lies, holds
  ``build.json``, written by the build that made it and never by the program.

Every runtime file that carries the user's paths (the configuration store, its previous
copies, a corrupt store moved aside, run records, live run status, file lists handed to
the engine, confirmation tickets, what a launched run wrote before it could start, the
volume snapshots runs made, full verification records, the core's diagnostic logs, a
portable release's setup record) sits beside the store (``RuntimeLayout``), so a session
that names another store — a test in a temporary folder — never touches the live
records, status or locks. So does the icon a set-up release's Start menu entry names, and
the rclone configuration's password where the user chose to have it remembered, sealed for
their Windows account.
The default store depends on the kind:

- a working copy's is ``Config/mirrorhold.json`` beside the package. That directory is
  ignored by the project's ``.gitignore``, so a runtime file placed anywhere else in a
  checkout would carry the user's paths into version control;
- the source installation's copy serves the store its record names, so every entry
  point of it reaches the same store as the working copy it was installed from;
- a portable release's is in ``%LOCALAPPDATA%\\MirrorHold``: nothing is written into a
  release's own folder, which a newer release replaces, and the job file names this
  computer's volumes, so it stays out of a roaming profile.

Nothing outside this module composes these paths: the scheduled runs must reach the
same store, and this is the one place that decides where it is.

A copy is known by the path it was reached by (``as_reached``), never by where links on
that path lead: a package manager keeps one folder for a program across its updates — a
junction it points at each new version, or a folder it unpacks each version into — and
the copy set up under that name keeps serving the tasks and the Start menu entry when
the folder's content changes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


def as_reached(path) -> Path:
    """``path`` made absolute as written, ``..`` folded away, with no link on it followed."""
    return Path(os.path.abspath(path))


PROJECT_ROOT = as_reached(__file__).parents[1]

RUNTIME_DIR_NAME = "Config"
STORE_FILE_NAME = "mirrorhold.json"
VENDOR_DIR_NAME = "vendor"
INSTALL_DIR_NAME = "installed"
INSTALLATION_FILE_NAME = "installation.json"
BUILD_FILE_NAME = "build.json"
PORTABLE_DATA_DIR_NAME = "MirrorHold"

# What a copy ships at the root of its folder, beside the packages: the licence (every
# copy), the third-party notices (a portable release) and the program's icon (a portable
# release and the source installation's copy). The installer names the icon in the Start
# menu entry and the window reads all three, so their names are decided here, once.
LICENCE_FILE_NAME = "LICENSE"
NOTICES_FILE_NAME = "THIRD-PARTY-NOTICES.txt"
ICON_FILE_NAME = "mirrorhold.ico"


class CopyKind(str, Enum):
    WORKING = "working"          # a source checkout
    INSTALLED = "installed"      # the source installation's copy, in ``installed/``
    PORTABLE = "portable"        # a portable release


def copy_kind(root: Path = PROJECT_ROOT) -> CopyKind:
    """The kind of the copy whose packages sit in ``root``."""
    if (Path(root) / BUILD_FILE_NAME).is_file():
        return CopyKind.PORTABLE
    if (Path(root) / INSTALLATION_FILE_NAME).is_file():
        return CopyKind.INSTALLED
    return CopyKind.WORKING


def runtime_dir() -> Path:
    """A working copy's directory of private runtime files."""
    return PROJECT_ROOT / RUNTIME_DIR_NAME


def running_installed() -> bool:
    """Whether this code is the source installation's copy."""
    return copy_kind() is CopyKind.INSTALLED


def portable_data_dir() -> Path:
    """Where a portable release keeps its job file and everything beside it."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / PORTABLE_DATA_DIR_NAME


def portable_store_path() -> Path:
    """The job file a portable release serves unless told otherwise."""
    return portable_data_dir() / STORE_FILE_NAME


def install_dir(root: Path = PROJECT_ROOT) -> Path:
    """The source installation's copy for the copy at ``root``: that copy's own folder when
    it is the installed one, else ``installed/`` beside it."""
    return Path(root) if copy_kind(root) is CopyKind.INSTALLED else Path(root) / INSTALL_DIR_NAME


def _served_store() -> Optional[Path]:
    try:
        doc = json.loads((PROJECT_ROOT / INSTALLATION_FILE_NAME).read_text(encoding="utf-8"))
        return Path(doc["store"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def default_store_path() -> Path:
    """The configuration store the command line and the GUI use unless told otherwise:
    a working copy's own, the one the source installation's copy was installed to
    serve, or a portable release's in ``%LOCALAPPDATA%``."""
    kind = copy_kind()
    if kind is CopyKind.PORTABLE:
        return portable_store_path()
    if kind is CopyKind.INSTALLED:
        served = _served_store()
        if served is not None:
            return served
    return runtime_dir() / STORE_FILE_NAME


def vendor_dir() -> Path:
    """The git-ignored directory holding the pinned engine copy."""
    return PROJECT_ROOT / VENDOR_DIR_NAME


@dataclass(frozen=True)
class RuntimeLayout:
    """The runtime files that belong to one configuration store, all in its folder."""

    root: Path

    @property
    def records_dir(self) -> Path:
        """Run records, one folder per job."""
        return self.root / "runs"

    @property
    def status_dir(self) -> Path:
        """Live status, liveness locks, control requests and engine file lists of runs in progress."""
        return self.root / "status"

    @property
    def tickets_dir(self) -> Path:
        """Confirmation tickets written by a preview, one per job."""
        return self.root / "plans"

    @property
    def admission_lock(self) -> Path:
        """Held for the moment a run or a verification decides whether it may start, or a
        run judges its destination's free space (``records.admission``)."""
        return self.root / "admission.lock"

    @property
    def snapshot_lock(self) -> Path:
        """Held while a run makes or removes a volume snapshot: one at a time."""
        return self.root / "snapshot.lock"

    @property
    def tray_lock(self) -> Path:
        """Held by the process showing the notification-area icon for this store's activities
        (``mirrorhold.tray``): one such process per store. Outside ``status_dir``, where every
        copy — an older one included — reads each lock as an activity's."""
        return self.root / "tray.lock"

    @property
    def reminders_dir(self) -> Path:
        """What finished activities left for the user to look at, one per job, until the user
        opens the window (``reminders``)."""
        return self.root / "reminders"

    @property
    def launches_dir(self) -> Path:
        """What a run process started by a caller wrote before it could publish a status."""
        return self.root / "launches"

    @property
    def automation_dir(self) -> Path:
        """Per job: which of its drives were present when arrivals were last handled, the
        lock that serialises that handling, and the log of an automatic start that failed."""
        return self.root / "automation"

    @property
    def snapshots_dir(self) -> Path:
        """Per run that made one: the volume snapshot it made, so that a snapshot a run left
        behind is removed by the next one — that snapshot and no other."""
        return self.root / "snapshots"

    @property
    def verifications_dir(self) -> Path:
        """Full verification records, one folder per job, one record per pass of a destination."""
        return self.root / "verifications"

    @property
    def repairs_dir(self) -> Path:
        """Repair records, one folder per job, one record per replacement of a destination's
        copies a verification found to differ."""
        return self.root / "repairs"

    @property
    def logs_dir(self) -> Path:
        """The diagnostic logs of the core's processes, one per process (``records.diagnostics``)."""
        return self.root / "logs"

    @property
    def remote_password(self) -> Path:
        """The rclone configuration's password, sealed for the user's Windows account, where
        the user chose to have it remembered (``remotes.seal``)."""
        return self.root / "remote-password.dpapi"


def runtime_layout(store_path: Path) -> RuntimeLayout:
    return RuntimeLayout(Path(store_path).resolve().parent)
