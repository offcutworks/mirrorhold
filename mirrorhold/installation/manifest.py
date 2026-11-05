"""The installation record: which copy automatic runs and the Start menu entry start.

``installation.json`` names the copy set up, the store it serves and the windowless
interpreter its tasks and entry start — only the tool's own paths, no file of the
user's. One format serves both kinds of installation, told apart by ``kind``:

- ``source``: the record sits at the root of the copy ``install`` built in ``installed/``
  beside a working copy. Its presence is what makes that copy the source installation's
  (``locations.copy_kind``), its ``store`` is the store every entry point of it serves, and
  ``source`` names the working copy it was installed from. The folder holding the record
  is the copy: the record is written while the copy is still being built beside it.
- ``portable``: the record sits beside the store it serves (``RuntimeLayout``), never in
  the release folder it names in ``program``, which a newer release replaces; it carries
  that release's label. Whichever copy saves the jobs reconciles the tasks toward the copy
  this record names.

Schema 1, the source installation's record before portable releases, reads as ``kind``
``source``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from ..contract_errors import ContractError, require_keys
from ..locations import BUILD_FILE_NAME, INSTALLATION_FILE_NAME

MANIFEST_FORMAT = "mirrorhold-installation"
MANIFEST_SCHEMA = 2
_SCHEMA_ONE_KEYS = ("format", "schema_version", "version", "installed_at", "store", "interpreter", "source")
_KEYS = ("format", "schema_version", "kind", "version", "label", "installed_at", "store", "interpreter", "program",
         "source")


class InstallKind(str, Enum):
    SOURCE = "source"            # a copy ``install`` built beside a working copy
    PORTABLE = "portable"        # a portable release set up where it lies


@dataclass(frozen=True)
class InstalledCopy:
    root: Path               # the folder the tasks and the Start menu entry start the copy in
    version: str             # mirrorhold.__version__ of the copy
    installed_at: str        # UTC, ISO 8601
    store: Path              # the configuration store it serves
    interpreter: Path        # the windowless interpreter its tasks and shortcut start
    source: Optional[Path]   # the working copy it was installed from; None for a portable release
    kind: InstallKind = InstallKind.SOURCE
    label: Optional[str] = None      # a portable release's label

    @property
    def portable(self) -> bool:
        return self.kind is InstallKind.PORTABLE

    @property
    def record_folder(self) -> Path:
        """Where the record lives: the source copy's root, or beside a portable release's store."""
        return Path(self.store).resolve().parent if self.portable else Path(self.root)

    @property
    def present(self) -> bool:
        """Whether the copy's folder still holds it: a portable release can be moved or deleted."""
        return (Path(self.root) / BUILD_FILE_NAME).is_file() if self.portable else Path(self.root).is_dir()

    def to_document(self) -> dict:
        return {"format": MANIFEST_FORMAT, "schema_version": MANIFEST_SCHEMA, "kind": self.kind.value,
                "version": self.version, "label": self.label, "installed_at": self.installed_at,
                "store": str(self.store), "interpreter": str(self.interpreter), "program": str(self.root),
                "source": str(self.source) if self.source is not None else None}

    @classmethod
    def from_document(cls, folder: Path, doc) -> "InstalledCopy":
        """The record read from ``folder``."""
        if isinstance(doc, dict) and doc.get("schema_version") == 1:
            require_keys(doc, "", _SCHEMA_ONE_KEYS)
            if doc["format"] != MANIFEST_FORMAT:
                raise ContractError("format", "not an installation record")
            return cls(Path(folder), str(doc["version"]), str(doc["installed_at"]), Path(doc["store"]),
                       Path(doc["interpreter"]), Path(doc["source"]))
        require_keys(doc, "", _KEYS)
        if doc["format"] != MANIFEST_FORMAT or doc["schema_version"] != MANIFEST_SCHEMA:
            raise ContractError("format", f"not an installation record of schema {MANIFEST_SCHEMA}")
        try:
            kind = InstallKind(doc["kind"])
        except ValueError:
            raise ContractError("kind", "neither source nor portable") from None
        if kind is InstallKind.PORTABLE:
            if not isinstance(doc["label"], str) and doc["label"] is not None:
                raise ContractError("label", "expected text")
            return cls(Path(doc["program"]), str(doc["version"]), str(doc["installed_at"]), Path(doc["store"]),
                       Path(doc["interpreter"]), None, kind, doc["label"])
        if doc["source"] is None:
            raise ContractError("source", "a source installation names the working copy it came from")
        return cls(Path(folder), str(doc["version"]), str(doc["installed_at"]), Path(doc["store"]),
                   Path(doc["interpreter"]), Path(doc["source"]))

    def serves(self, store: Path) -> bool:
        return same_path(self.store, store)

    def runs_from(self, root: Path) -> bool:
        return same_path(self.root, root)


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def manifest_path(folder: Path) -> Path:
    return Path(folder) / INSTALLATION_FILE_NAME


def read_installation(folder: Path) -> Optional[InstalledCopy]:
    """The installation recorded in ``folder``, or None when there is none (or its record is
    unreadable)."""
    try:
        doc = json.loads(manifest_path(folder).read_text(encoding="utf-8"))
        return InstalledCopy.from_document(folder, doc)
    except (OSError, ValueError, ContractError):
        return None


def write_installation(copy: InstalledCopy, folder: Optional[Path] = None) -> None:
    """Write the record into ``folder`` (default: where ``copy`` keeps it)."""
    path = manifest_path(folder if folder is not None else copy.record_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".writing")
    with open(staging, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(copy.to_document(), handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)


def remove_installation(folder: Path) -> bool:
    """Remove the record in ``folder``; whether one was there."""
    try:
        manifest_path(folder).unlink()
        return True
    except FileNotFoundError:
        return False
