"""Folders the computer and this tool need to run.

A first run onto a destination that holds one of them is offered only to leave what the
destination already holds where it is, and no run or repair moves one into versions or
writes into one (the maintainer's decision of 2026-10-11): moving Windows, an installed
program, a user profile or this tool's own files would leave the computer or the profile
broken, part-moved where files are in use. A run stops rather than do either, unless the
job's exclusions hide the folder (``run.runner``; the maintainer's decision of
2026-10-21), and a repair leaves a copy inside one as it is (``maintenance.repairer``). A
destination that is itself one of them is refused when its job is made
(``jobs.validation``): every run would write into it.

They are read from the environment Windows gives every process — the Windows folder, the
program folders, the folder of programs' shared data, the folder of user profiles and each
profile in it — and are this tool's own: the folder it runs from and the folder of the job
file in use. A variable that is not set names nothing; a profiles folder that cannot be
listed names itself alone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from ..contract_errors import ContractError
from ..locations import PROJECT_ROOT
from .endpoint import Endpoint
from .volumes import VolumeInfo, find_by_mount

_NEEDED = (("SystemRoot", "which Windows needs"), ("ProgramFiles", "which installed programs need"),
           ("ProgramFiles(x86)", "which installed programs need"), ("ProgramW6432", "which installed programs need"),
           ("ProgramData", "which installed programs need"))


@dataclass(frozen=True)
class SystemFolder:
    path: str
    words: str          # the folder and what it is, as the user is told: "C:\Windows, which Windows needs"


def system_folders(store_path: Path, *, environ: Optional[Mapping[str, str]] = None,
                   root: Path = PROJECT_ROOT) -> Tuple[SystemFolder, ...]:
    """The folders no run may move or write into, for a run of the job file at
    ``store_path``. ``environ``: the process's environment unless given; ``root``: the
    folder this tool runs from."""
    environ = os.environ if environ is None else environ
    found: List[SystemFolder] = []
    for variable, need in _NEEDED:
        value = environ.get(variable)
        if value:
            found.append(SystemFolder(value, f"{value}, {need}"))
    profile = environ.get("USERPROFILE")
    if profile:
        profiles = os.path.dirname(os.path.normpath(profile))
        found.append(SystemFolder(profiles, f"{profiles}, which holds the user profiles"))
        try:
            with os.scandir(profiles) as entries:
                found += [SystemFolder(e.path, f"{e.path}, a user profile") for e in entries
                          if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
    for folder in (Path(root), Path(store_path).resolve().parent):
        found.append(SystemFolder(str(folder), f"{folder}, which MirrorHold needs"))
    return tuple(found)


def system_folder_endpoints(folders: Sequence[SystemFolder],
                            volumes: Sequence[VolumeInfo]) -> Tuple[Tuple[SystemFolder, Endpoint], ...]:
    """Each folder on a present volume, as an endpoint; folders elsewhere are skipped."""
    found = []
    for folder in folders:
        located = find_by_mount(volumes, folder.path)
        if located is None:
            continue
        try:
            found.append((folder, Endpoint(located[0].identity, located[1])))
        except ContractError:
            continue
    return tuple(found)
