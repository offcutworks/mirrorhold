"""A destination on a remote: a folder of a remote of the user's rclone configuration,
reached only through the engine serving a session (``engine.remote.RemoteSession``).

The session is the caller's: it is opened with the configuration the preferences name and
closed when the activity that reaches the destination ends, so one unlock serves every
operation. This kind turns each operation the seam names into the session's, and each
failure the engine reports into ``StorageError`` with the engine's reason —
``StorageUnanswered`` where the engine gave no whole answer — or ``FileNotFoundError`` where
the engine says nothing is there.

What a remote is not, it answers as such: it has no link and no hidden attribute, no
folder of this computer to open, and no drive root's artefacts. A service with no folders
of its own (an object store) lists a folder that is not there as empty, and makes the
folders a file's path needs as the file arrives, so a move into a folder it does not show
yet succeeds there as on a drive whose folders the move first made.

The listing walks the remote folder by folder, so the pause, cancel and progress a listing
of a drive has are kept, and a scope (a shared destination's part) and the job's
exclusions prune what is never listed. Names the plan cannot tell apart — two in one folder
differing by case alone, or one name held twice — are set apart in the listing
(``TreeListing.ambiguous``), and the run stops the pair rather than guess which is which; the
leftovers of an engine stopped mid-copy reach the planning as the remote lists them.

A verification compares a copy here without reading it wherever the service allows: by the
fingerprint the service keeps for each file, asked for in the listing — of the one kind
compared, since a local remote computes every kind it is asked for by reading the file — or,
on an encrypted remote, by the engine's check of the encrypted form (``check_encrypted``).
"""
from __future__ import annotations

import errno
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..engine.rclone import Configuration, CryptCheck, EngineError, RemoteRoot
from ..engine.remote import RemoteEntry, RemoteError, RemoteFeatures, RemoteSession, RemoteUnanswered, unreachable
from ..jobs.exclusions import EntryFilter
from ..planning.listing import (Entry, ListingError, ListingProgress, Occupancy, Scope, Side, SkipReason, Skipped,
                                TreeListing, path_key)
from .destination import (MANAGEMENT_DIR, WRITE_PATIENCE, DestinationStorage, StorageError, StorageUnanswered,
                          StoredEntry)


@contextmanager
def _reasons() -> Iterator[None]:
    try:
        yield
    except RemoteUnanswered as exc:
        raise StorageUnanswered(str(exc)) from None
    except RemoteError as exc:
        raise StorageError(str(exc)) from None


def _absent(where: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, "it is not there", where)


def _stored(entry: RemoteEntry) -> StoredEntry:
    return StoredEntry(entry.path.rsplit("\\", 1)[-1], entry.is_dir, False, entry.size, entry.mtime_ns)


class RemoteStorage(DestinationStorage):
    """The folder ``root`` of a remote, through ``session``. ``features``: the remote's, as
    the engine reports them; asked of the engine when first needed when not given."""

    def __init__(self, session: RemoteSession, root: RemoteRoot, features: Optional[RemoteFeatures] = None):
        self.session = session
        self.root = root
        self._features = features

    def __repr__(self) -> str:
        return f"RemoteStorage({self.root.fs!r})"

    @property
    def features(self) -> RemoteFeatures:
        if self._features is None:
            with _reasons():
                self._features = self.session.features(self.root)
        return self._features

    # --- what it is ------------------------------------------------------------------

    def describe(self, relative: str = "") -> str:
        return self.root.joined(relative).fs

    def locate(self, relative: str = "") -> RemoteRoot:
        return self.root.joined(relative)

    @property
    def configuration(self) -> Configuration:
        return self.session.configuration

    @property
    def time_window_ns(self) -> int:
        return self.features.precision_ns

    def space(self) -> Optional[Tuple[int, int]]:
        """(free, total) where the service reports both; None where it reports either not, or
        cannot answer now."""
        try:
            found = self.session.space(self.root)
        except RemoteError:
            return None
        if found is None or found.free is None or found.total is None:
            return None
        return found.free, found.total

    def free_bytes(self) -> Optional[int]:
        """The free bytes the service reports, with or without a total; None where it
        reports none, or answers that it cannot here (a local remote's folder not made yet,
        measured); ``StorageError`` when the engine gave no answer or the service could not
        be reached."""
        try:
            found = self.session.space(self.root)
        except RemoteUnanswered as exc:
            raise StorageError(str(exc)) from None
        except RemoteError as exc:
            if unreachable(str(exc)):
                raise StorageError(str(exc)) from None
            return None
        return found.free if found is not None else None

    @property
    def may_keep_deleted(self) -> bool:
        """A service that reports a trash it empties (the engine's ``CleanUp``) may keep what
        is deleted: S3 reports one, for the uploads it never finished, and a bucket may keep
        older versions too, so this is said as a possibility, never as a fact."""
        return self.features.empties_trash

    # --- reading ---------------------------------------------------------------------

    def entry(self, relative: str) -> Optional[StoredEntry]:
        with _reasons():
            found = self.session.stat(self.root, relative)
        return _stored(found) if found is not None else None

    def is_folder(self, relative: str = "") -> bool:
        """A folder the remote shows as one, or, on a service with no folders of its own —
        which lists any folder it is asked for as if it were there — one that holds
        something. The destination's own folder is there when it can be listed. An engine
        that cannot answer is ``StorageError``, never an absent folder."""
        with _reasons():
            if not relative:
                return self.session.list_folder(self.root, "") is not None
            found = self.session.stat(self.root, relative)
            if found is not None:
                return found.is_dir
            return bool(self.session.list_folder(self.root, relative))

    def entries(self, relative: str) -> Tuple[StoredEntry, ...]:
        with _reasons():
            found = self.session.list_folder(self.root, relative)
        if found is None:
            raise _absent(self.describe(relative))
        return tuple(_stored(e) for e in found)

    def list_tree(self, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None,
                  progress: Optional[ListingProgress] = None, scope: Optional[Scope] = None,
                  fingerprint: Optional[str] = None) -> TreeListing:
        """As a drive's listing: the management folder at any depth and what the exclusions
        or the scope leave out are never listed; a folder below the top that cannot be listed
        is recorded, so nothing beneath it reads as deleted at the source. A remote has no
        drive root's artefacts to pass over, so ``drive_root`` changes nothing. Two entries
        one comparison key names — a service telling names apart by case alone, or keeping
        two of one name in a folder — are kept apart in ``ambiguous``, the first of them
        listed and the second not entered, so the run can stop rather than guess."""
        entry_filter = entry_filter or EntryFilter()
        listing = TreeListing(self.root, Side.DESTINATION)
        started = time.monotonic()
        stack: List[str] = [""]
        seen = 0
        spelled: Dict[str, str] = {}
        while stack:
            if progress is not None:
                progress(seen)
            relative = stack.pop()
            at_root = not relative
            try:
                found = self.session.list_folder(self.root, relative, kinds=(fingerprint,) if fingerprint else ())
            except RemoteError as exc:
                if at_root:
                    raise ListingError(f"{self.describe()} cannot be listed: {exc}") from None
                listing.skipped.append(Skipped(relative, SkipReason.UNLISTABLE, str(exc)))
                continue
            if found is None:
                if at_root:
                    listing.present = False
                    listing.seconds = time.monotonic() - started
                    return listing
                listing.skipped.append(Skipped(relative, SkipReason.UNLISTABLE, "the folder is no longer there"))
                continue
            seen += len(found)
            for entry in found:
                path = entry.path
                name = path.rsplit("\\", 1)[-1]
                if scope is not None and not scope.covers(path_key(path), at_root):
                    continue
                if entry.is_dir and name.casefold() == MANAGEMENT_DIR:
                    continue
                if entry_filter.excludes(path, name, entry.is_dir):
                    listing.excluded += 1
                    continue
                earlier = spelled.get(path_key(path))
                if earlier is not None:
                    listing.ambiguous.append((earlier, path))
                    continue
                spelled[path_key(path)] = path
                if entry.is_dir:
                    listing.dirs[path_key(path)] = path
                    stack.append(path)
                    continue
                listing.files[path_key(path)] = Entry(path, entry.size, entry.mtime_ns)
                kept = entry.hashes.get(fingerprint) if fingerprint else None
                if kept:
                    listing.fingerprints[path_key(path)] = kept.lower()
        if progress is not None:
            progress(seen)
        listing.seconds = time.monotonic() - started
        return listing

    def occupancy(self, *, drive_root: bool, entry_filter: Optional[EntryFilter] = None) -> Occupancy:
        """From the top level alone, as a drive's: the first entry the listing would keep
        makes it occupied. A service with no folders of its own answers EMPTY for a folder
        that is not there; either way it holds nothing."""
        entry_filter = entry_filter or EntryFilter()
        try:
            found = self.session.list_folder(self.root, "")
        except RemoteError:
            return Occupancy.UNKNOWN
        if found is None:
            return Occupancy.MISSING
        for entry in found:
            name = entry.path.rsplit("\\", 1)[-1]
            if entry.is_dir and name.casefold() == MANAGEMENT_DIR:
                continue
            if entry_filter.excludes(name, name, entry.is_dir):
                continue
            return Occupancy.OCCUPIED
        return Occupancy.EMPTY

    def read_file(self, relative: str) -> bytes:
        with _reasons():
            data = self.session.read(self.root, relative)
        if data is None:
            raise _absent(self.describe(relative))
        return data

    def read_head(self, relative: str, size: int) -> bytes:
        with _reasons():
            data = self.session.read(self.root, relative, size)
        if data is None:
            raise _absent(self.describe(relative))
        return data

    def open_file(self, relative: str) -> BinaryIO:
        with _reasons():
            stream = self.session.open_file(self.root, relative)
        if stream is None:
            raise _absent(self.describe(relative))
        return _Reasons(stream)

    # --- comparing without reading -----------------------------------------------------

    @property
    def fingerprint_kinds(self) -> Tuple[str, ...]:
        return self.features.hashes

    @property
    def encrypted(self) -> bool:
        return self.features.encrypted

    def fingerprint(self, relative: str, kind: str) -> Optional[str]:
        with _reasons():
            found = self.session.stat(self.root, relative, kinds=(kind,))
        if found is None:
            raise _absent(self.describe(relative))
        kept = found.hashes.get(kind)
        return kept.lower() if kept else None

    def fingerprint_local(self, path: Path, kind: str, *,
                          give_up: Callable[[], bool] = lambda: False) -> Optional[str]:
        with _reasons():
            return self.session.local_fingerprint(path, kind, give_up=give_up)

    def check_encrypted(self, source_root: Path, files: Sequence[str], *, list_path) -> CryptCheck:
        if not self.encrypted:
            raise StorageError(f"{self.describe()} is not encrypted")
        try:
            return CryptCheck(self.session.engine, source_root, self.root, files, list_path=list_path,
                              configuration=self.configuration)
        except EngineError as exc:
            raise StorageError(str(exc)) from None

    # --- writing ---------------------------------------------------------------------

    def write_file(self, relative: str, data: bytes, *, patience: float = WRITE_PATIENCE) -> None:
        """Written whole by one upload, which the service makes visible whole; ``patience``
        has nothing to wait for, no other program holding a remote's file open."""
        with _reasons():
            self.session.write(self.root, relative, data)

    def make_management_folder(self) -> None:
        self.make_folder(MANAGEMENT_DIR)

    def make_folder(self, relative: str = "") -> None:
        with _reasons():
            self.session.make_folder(self.root, relative)

    def move(self, old: str, new: str, *, parents: bool = True) -> None:
        """A remote makes the folders a file's new path needs as it moves it, so ``parents``
        changes nothing here."""
        with _reasons():
            found = self.session.stat(self.root, old)
            if found is None:
                raise _absent(self.describe(old))
            if found.is_dir:
                self.session.move_folder(self.root, old, new)
            else:
                self.session.move_file(self.root, old, new)

    def remove_file(self, relative: str) -> None:
        with _reasons():
            if not self.session.delete_file(self.root, relative):
                raise _absent(self.describe(relative))

    def remove_empty_tree(self, relative: str) -> bool:
        """Only empty folders are removed (``RemoteSession.remove_empty_folders``), so a file
        that arrives after the look stays, and so does every folder on its way; the tree is
        gone only when nothing is left at ``relative``. One the engine cannot answer for is
        left as it is."""
        try:
            found = self.session.list_tree(self.root, relative)
            if found is None or any(not e.is_dir for e in found):
                return False
            self.session.remove_empty_folders(self.root, relative)
            return not self.is_folder(relative)
        except (RemoteError, StorageError):
            return False

    def delete_tree(self, relative: str) -> Optional[str]:
        try:
            self.session.delete_folder(self.root, relative)
        except RemoteError as exc:
            return str(exc)
        return None

    def measure(self, relative: str) -> Tuple[int, int]:
        """An engine that cannot answer is ``StorageError``, never an empty tree."""
        with _reasons():
            found = self.session.list_tree(self.root, relative)
        files = [e for e in found or () if not e.is_dir]
        return len(files), sum(e.size for e in files)


class _Reasons:
    """A remote file's stream whose failures reach the caller as ``StorageError``."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        with _reasons():
            return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "_Reasons":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
