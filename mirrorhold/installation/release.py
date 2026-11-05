"""A portable release's build record: which release this copy is.

``build.json`` sits at a portable release's root. The build that made the release writes
it; the program only reads it. Its presence is what makes a copy a portable release
(``locations.copy_kind``), and its label — ``YEAR.MONTH.N``, the N-th release of that
month — is the only place the program learns which release it is: the in-code version
stays 1.0.0 and promises nothing.

A ``build.json`` that cannot be read still marks a portable release; its label is then
unknown and is said so wherever the release is named.

The record names no commit: a commit belongs to one repository's history, and nothing in
a release may depend on which repository it was built from, so that the release rebuilt
from the published repository's tag for its label is the same zip, byte for byte (the
maintainer's decision, 2026-11-02). The label is what leads from a release to its
source.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..contract_errors import ContractError, require_keys
from ..locations import BUILD_FILE_NAME

BUILD_FORMAT = "mirrorhold-build"
BUILD_SCHEMA = 2
LABEL_PATTERN = re.compile(r"\d{4}\.(?:[1-9]|1[0-2])\.[1-9]\d*")


@dataclass(frozen=True)
class Release:
    root: Path                       # the release's folder
    label: Optional[str]             # YEAR.MONTH.N; None when the record cannot be read
    python: Optional[str] = None     # the version of the interpreter it carries

    @property
    def readable(self) -> bool:
        return self.label is not None

    def to_document(self) -> dict:
        return {"format": BUILD_FORMAT, "schema_version": BUILD_SCHEMA, "label": self.label, "python": self.python}

    @classmethod
    def from_document(cls, root: Path, doc) -> "Release":
        require_keys(doc, "", ("format", "schema_version", "label", "python"))
        if doc["format"] != BUILD_FORMAT or doc["schema_version"] != BUILD_SCHEMA:
            raise ContractError("format", f"not a build record of schema {BUILD_SCHEMA}")
        label, python = doc["label"], doc["python"]
        if not isinstance(label, str) or not LABEL_PATTERN.fullmatch(label):
            raise ContractError("label", "not a release label (YEAR.MONTH.N)")
        if not isinstance(python, str) or not python:
            raise ContractError("python", "not an interpreter version")
        return cls(Path(root), label, python)


def release_path(root: Path) -> Path:
    return Path(root) / BUILD_FILE_NAME


def read_release(root: Path) -> Optional[Release]:
    """The release at ``root``; None when ``root`` holds no build record, and a release
    with an unknown label when it holds one that cannot be read."""
    path = release_path(root)
    if not path.is_file():
        return None
    try:
        return Release.from_document(root, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ContractError):
        return Release(Path(root), None)


def write_release(release: Release) -> None:
    """Write a release's build record — the build's step, never the program's."""
    Release.from_document(release.root, release.to_document())
    path = release_path(release.root)
    staging = path.with_name(path.name + ".writing")
    with open(staging, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(release.to_document(), handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)


def release_name(release: Optional[Release]) -> str:
    """How a release is named to the user: ``release 2026.10.1``, or that its label is unknown."""
    if release is None:
        return "run from source"
    return f"release {release.label}" if release.readable else "release unknown (its build record cannot be read)"
