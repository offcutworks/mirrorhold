"""The destination-storage seam: what every unit that reads or writes a backup's files asks of
the place the backup is kept.

``DestinationStorage`` is the one surface through which the run, the preview, the version
cleanup, the verification, the repair, the records and the window reach a destination's
files. Two kinds sit behind it: a folder of this computer (``drive.DriveStorage``) and a
folder of a remote of the user's rclone configuration reached through one engine
(``remote.RemoteStorage``). The core still lists, plans and judges every change; a kind
only performs the operation it is handed.

Every path is a carried relative path, backslash-separated as the core spells one, below
the destination's folder; '' is that folder itself. A path that is not there raises
``FileNotFoundError`` where the operation says so, and any other failure ``OSError`` with
its reason in ``strerror``, on either kind, so the code above the seam handles a refusal
the same way wherever the destination lies.

What a kind cannot have — a remote has no link, no hidden attribute and no folder of this
computer — it answers as absent (``StoredEntry.is_link`` false, ``local_folder`` None);
nothing above the seam guesses a kind from its answers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Callable, Optional, Sequence, Tuple

from .. import exchange

if TYPE_CHECKING:
    from ..engine.rclone import Configuration, CryptCheck, Root
    from ..jobs.exclusions import EntryFilter
    from ..planning.listing import ListingProgress, Occupancy, Scope, TreeListing

MANAGEMENT_DIR = ".mirrorhold"
"""The destination's management folder, at its root: the marker, the job's definition, the
records, a shared destination's record and the versions batches."""

WRITE_PATIENCE = exchange.WRITE_PATIENCE
"""Seconds a write is tried again while another program holds the file."""


class StorageError(OSError):
    """A destination's operation that failed, its reason in ``strerror``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.strerror = reason


class StorageUnanswered(StorageError):
    """An operation a remote's engine gave no whole answer to — not reached, or broken off —
    rather than one the service answered with a refusal."""


def raise_if_unanswered(storage: "DestinationStorage", relative: str, failure: OSError) -> None:
    """Raise when ``failure``, met reading the file at ``relative``, is a remote that stopped
    answering; return when it is the file that cannot be read.

    A read the engine gave no whole answer to (``StorageUnanswered``) is raised as it is, though
    the next operation may be answered. Any other ``StorageError`` (only a remote raises one) is
    either the service no longer reached or a file it will not serve, so the file's entry is
    asked for next: answered, the file is one that cannot be read; not answered, that question's
    ``StorageError`` is raised. Every other ``OSError`` — a drive's — is the file's."""
    if isinstance(failure, StorageUnanswered):
        raise failure
    if isinstance(failure, StorageError):
        storage.entry(relative)


@dataclass(frozen=True)
class StoredEntry:
    """One entry of a destination folder: its name, whether it is a folder, a link (never
    entered or followed), its size and modification time; ``attributes`` are the Windows
    file attributes where the kind has them, else 0. ``problem``: why what the entry is
    could not be read, its other fields then unknown."""

    name: str
    is_dir: bool
    is_link: bool = False
    size: int = 0
    mtime_ns: int = 0
    attributes: int = 0
    problem: str = ""


class DestinationStorage(ABC):
    """A destination's folder and everything in it."""

    # --- what it is ------------------------------------------------------------------

    @abstractmethod
    def describe(self, relative: str = "") -> str:
        """``relative`` as a person reads it in a message: a path of this computer, or a
        remote's ``name:folder``."""

    @property
    def local_folder(self) -> Optional[Path]:
        """The destination's folder on this computer, which the window can open; None where
        the destination has none."""
        return None

    @abstractmethod
    def locate(self, relative: str = "") -> "Root":
        """``relative`` as the engine is handed it for a copy: a folder of this computer or
        of a remote."""

    @property
    def configuration(self) -> Optional["Configuration"]:
        """The configuration the engine reaches the destination through; None for a folder
        of this computer."""
        return None

    @property
    @abstractmethod
    def time_window_ns(self) -> int:
        """The coarsest difference of modification times the destination may hold between a
        file and its copy that is still the same time."""

    @abstractmethod
    def space(self) -> Optional[Tuple[int, int]]:
        """(free, capacity) in bytes as the destination reports them; None where it does not."""

    def free_bytes(self) -> Optional[int]:
        """The free bytes the destination reports now, whatever it says of its capacity;
        None where it reports none. OSError when it cannot answer now (a remote's engine
        failing), never None: a space that could not be asked is not one that is unknown."""
        space = self.space()
        return space[0] if space is not None else None

    @property
    def may_keep_deleted(self) -> bool:
        """Whether what is deleted there may stay, counted against its space, until the
        destination itself removes it: a service with a trash, or older versions, of its own."""
        return False

    # --- reading ---------------------------------------------------------------------

    @abstractmethod
    def entry(self, relative: str) -> Optional[StoredEntry]:
        """The entry at ``relative``, a link as itself; None when nothing is there."""

    def file_entry(self, parts: Sequence[str]) -> Optional[StoredEntry]:
        """The entry at the path ``parts`` names, or the first link on the way to it — a link
        as a listing passes one over — which is never followed; None when the path is not
        there."""
        return self.entry("\\".join(parts))

    def exists(self, relative: str) -> bool:
        """Whether anything is at ``relative``, a link included. OSError when the destination
        cannot answer (a remote's engine failing); on a drive, a path whose metadata cannot
        be read counts as not there."""
        return self.entry(relative) is not None

    @abstractmethod
    def is_folder(self, relative: str = "") -> bool:
        """Whether a folder is at ``relative`` (on a drive, a link to one counts as the
        folder it leads to). OSError when the destination cannot answer; on a drive, a
        folder that cannot be read is not one."""

    @abstractmethod
    def entries(self, relative: str) -> Tuple[StoredEntry, ...]:
        """The entries of the folder at ``relative``; FileNotFoundError when it is not there."""

    @abstractmethod
    def list_tree(self, *, drive_root: bool, entry_filter: Optional["EntryFilter"] = None,
                  progress: Optional["ListingProgress"] = None, scope: Optional["Scope"] = None,
                  fingerprint: Optional[str] = None) -> "TreeListing":
        """The destination's listing as ``planning.listing.list_tree`` gives one of a folder;
        with ``fingerprint``, a kind among ``fingerprint_kinds``, each file's fingerprint of
        that kind the destination keeps in ``TreeListing.fingerprints``."""

    @abstractmethod
    def occupancy(self, *, drive_root: bool, entry_filter: Optional["EntryFilter"] = None) -> "Occupancy":
        """What the destination holds, as ``planning.listing.occupancy`` answers it."""

    @abstractmethod
    def read_file(self, relative: str) -> bytes:
        """A small file's content, read whole: a file of the management folder."""

    @abstractmethod
    def read_head(self, relative: str, size: int) -> bytes:
        """A file's first ``size`` bytes."""

    @abstractmethod
    def open_file(self, relative: str) -> BinaryIO:
        """A file opened for reading from its start, as a stream to read in chunks."""

    # --- comparing without reading -----------------------------------------------------

    @property
    def fingerprint_kinds(self) -> Tuple[str, ...]:
        """The fingerprint kinds the destination keeps for each file, as the engine names them
        (``md5``, ``sha1``, ...), compared with the source's without reading the copy: none on
        a drive, whose copies a verification reads."""
        return ()

    @property
    def encrypted(self) -> bool:
        """Whether the destination keeps its files encrypted, so that a copy is compared with
        its source only through the engine's check of the encrypted form (``check_encrypted``)."""
        return False

    def fingerprint(self, relative: str, kind: str) -> Optional[str]:
        """The ``kind`` fingerprint the destination keeps for the file at ``relative``, in
        lower case; None where it keeps none for it. FileNotFoundError when it is not there."""
        return None

    def fingerprint_local(self, path: Path, kind: str, *,
                          give_up: Callable[[], bool] = lambda: False) -> Optional[str]:
        """The ``kind`` fingerprint of a file of this computer, computed by the engine that
        reaches the destination — for a kind this tool does not compute itself; None when
        ``give_up`` said so first. ``StorageError`` where no engine reaches the destination or
        it could not compute it."""
        raise StorageError("no engine reaches this destination to compute a fingerprint")

    def check_encrypted(self, source_root: Path, files: Sequence[str], *, list_path) -> "CryptCheck":
        """The engine's check, started, of ``files`` of ``source_root`` against their
        encrypted copies (``engine.rclone.CryptCheck``). ``StorageError`` where the destination
        is not encrypted or the check cannot start."""
        raise StorageError("this destination keeps nothing encrypted")

    # --- writing ---------------------------------------------------------------------

    @abstractmethod
    def write_file(self, relative: str, data: bytes, *, patience: float = WRITE_PATIENCE) -> None:
        """Replace a small file with ``data`` whole — a reader never meets a part of it —
        the folders it lies in made."""

    @abstractmethod
    def make_management_folder(self) -> None:
        """The management folder, made as the kind keeps it (hidden on a drive) when it is
        not there yet."""

    @abstractmethod
    def make_folder(self, relative: str = "") -> None:
        """The folder and those it lies in."""

    @abstractmethod
    def move(self, old: str, new: str, *, parents: bool = True) -> None:
        """Move a file or a folder whole within the destination — a change of case alone
        included — keeping its time; with ``parents``, the folders ``new`` lies in are made
        first. FileNotFoundError when nothing is at ``old``, or, without ``parents``, when
        the folder ``new`` lies in is not there; the caller that must not replace what is at
        ``new`` looks first."""

    @abstractmethod
    def remove_file(self, relative: str) -> None:
        """Delete one file; FileNotFoundError when it is not there."""

    @abstractmethod
    def remove_empty_tree(self, relative: str) -> bool:
        """Remove the folder at ``relative`` when it holds folders only — no file, no link —
        and say whether it is gone; one that holds anything else is left whole or in part."""

    @abstractmethod
    def delete_tree(self, relative: str) -> Optional[str]:
        """Delete a folder and everything in it, never entering a link; what could not be
        deleted, or None."""

    @abstractmethod
    def measure(self, relative: str) -> Tuple[int, int]:
        """The files and bytes in a folder tree, links neither followed nor counted."""

    def folder_problem(self, relative: str) -> Optional[str]:
        """Why the folder at ``relative`` must not be entered, or None when it is a real
        folder. A folder that is not there is no problem: there is nothing in it."""
        name = relative.rsplit("\\", 1)[-1]
        try:
            found = self.entry(relative)
        except OSError as exc:
            return f"{name} cannot be read: {exc.strerror or exc}"
        if found is None:
            return None
        if found.is_link:
            return f"{name} is a link or junction; nothing in it is deleted"
        if not found.is_dir:
            return f"{name} is not a folder; nothing is deleted"
        return None
