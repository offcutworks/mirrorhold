"""Built-in classifications of what a source holds, each defined once.

Every rule here classifies a directory entry from its attributes and reparse tag,
which a directory listing already carries, so applying one never opens a file —
opening a cloud-only placeholder would download it.

- **Drive-root artefacts** (always excluded from a drive-root source): the operating
  system's own entries at a volume root — recycle bin, system volume information,
  page, swap and hibernation files, dump files and their kin. Every such entry
  measured on the development machine's roots (recycle bin, system volume
  information, page, swap and dump files) carries both the hidden and the system
  attribute, and no user folder there does, so the class is defined by that pair rather
  than by a list of names that misses the next one.
- **Cloud placeholders**: an entry whose content is not on the disk. Classified by the
  recall and offline attributes the Cloud Files API sets; a real placeholder's
  attributes have not been measured yet.
- **Links**: a symbolic link or a junction (a name-surrogate reparse point). Never
  followed; a cloud file's reparse point is not a link.
- **Sync-client artefacts** (excluded unless the job switches the category off): the
  bookkeeping files and folders cloud and peer-to-peer sync clients keep beside the
  user's files. Matched by distinctive names only, so no ordinary file is caught.
- **Transient editor files** (excluded unless switched off): Microsoft Office owner and
  temporary files, and the lock files of LibreOffice and OpenOffice — files that exist
  only while a document is open.
- **The user's own patterns** (``Source.exclusions``), read as ``.gitignore`` reads
  them: a pattern with a separator (``\\`` or ``/``) at its start or inside it matches
  the path relative to the source root; any other pattern matches a file or folder
  name at any depth; a trailing separator restricts either to folders. ``*``, ``?`` and
  ``[...]`` are wildcards (``*`` also crosses separators in a path pattern); matching
  ignores case. An excluded folder is skipped whole.

Every exclusion applies to both sides of a pair: what a source excludes is invisible on
its destinations too, so excluding a file later never moves its existing copies into
versions. A folder a run protects inside a destination (``run.runner.protected_folders``)
that the exclusions hide (``EntryFilter.hiding``) is therefore out of the job's reach: no
run lists it, and no repair replaces a copy in it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

REPARSE_TAG_NAME_SURROGATE = 0x20000000


def is_drive_root_artefact(attributes: int) -> bool:
    """For an entry directly at a volume root."""
    both = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
    return attributes & both == both


def is_cloud_placeholder(attributes: int) -> bool:
    return bool(attributes & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN
                              | FILE_ATTRIBUTE_OFFLINE))


def is_link(attributes: int, reparse_tag: int) -> bool:
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) and bool(reparse_tag & REPARSE_TAG_NAME_SURROGATE)


SYNC_CLIENT_ARTEFACTS: Tuple[str, ...] = (
    ".dropbox", ".dropbox.attr", ".dropbox.cache",                          # Dropbox
    ".tmp.drivedownload", ".tmp.driveupload",                               # Google Drive for desktop
    ".stfolder", ".stversions", "~syncthing~*.tmp", ".syncthing.*.tmp",     # Syncthing
    ".sync_*.db", ".sync_*.db-wal", ".sync_*.db-shm", "._sync_*.db*",       # Nextcloud, ownCloud
    ".owncloudsync.log",
    "*.baiduyun.uploading.cfg", "*.baiduyun.downloading", "*.baiduyun.p.downloading",  # Baidu
)
"""Name patterns, lower case."""

TRANSIENT_EDITOR_FILES: Tuple[str, ...] = (
    "~$*",          # Office owner files
    "~wr*.tmp",     # Word temporary files
    ".~lock.*#",    # LibreOffice and OpenOffice lock files
)
"""Name patterns, lower case."""


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, p) for p in patterns)


def is_sync_client_artefact(name: str) -> bool:
    return _matches_any(name, SYNC_CLIENT_ARTEFACTS)


def is_transient_editor_file(name: str) -> bool:
    return _matches_any(name, TRANSIENT_EDITOR_FILES)


@dataclass(frozen=True)
class UserPattern:
    """One of the user's exclusion patterns, compiled to its matching form."""

    text: str
    folded: str
    on_path: bool
    folders_only: bool

    @classmethod
    def compile(cls, text: str) -> "UserPattern":
        normal = text.strip().replace("/", "\\")
        folders_only = normal.endswith("\\")
        body = normal.rstrip("\\")
        return cls(text, body.lstrip("\\").casefold(), "\\" in body, folders_only)

    def matches(self, relative_path: str, name: str, is_dir: bool) -> bool:
        if self.folders_only and not is_dir:
            return False
        subject = relative_path if self.on_path else name
        return fnmatch.fnmatchcase(subject.casefold(), self.folded)


@dataclass(frozen=True)
class EntryFilter:
    """What a job's source excludes, applied to both sides of each of its pairs: the
    switchable categories and the user's patterns. Drive-root artefacts, links and
    placeholders are classified by attributes and handled by the listing itself."""

    sync_client_artefacts: bool = True
    transient_editor_files: bool = True
    patterns: Tuple[UserPattern, ...] = ()

    @classmethod
    def for_source(cls, source) -> "EntryFilter":
        """The filter of a ``jobs.model.Source``."""
        return cls(source.default_exclusions.sync_client_artefacts,
                   source.default_exclusions.transient_editor_files,
                   tuple(UserPattern.compile(p) for p in source.exclusions))

    def excludes(self, relative_path: str, name: str, is_dir: bool) -> bool:
        if self.sync_client_artefacts and is_sync_client_artefact(name):
            return True
        if self.transient_editor_files and not is_dir and is_transient_editor_file(name):
            return True
        return any(p.matches(relative_path, name, is_dir) for p in self.patterns)

    def hiding(self, relative_path: str, is_dir: bool) -> Optional[str]:
        """The top-most entry on ``relative_path`` this filter excludes — the entry itself or
        a folder holding it — as a listing meets them from the root down; None when a
        listing reaches the entry, and for the root itself (""). A listing skips an excluded
        folder whole, so nothing beneath one is reached, whatever a pattern says of it."""
        if not relative_path:
            return None
        parts = relative_path.split("\\")
        for depth in range(1, len(parts) + 1):
            if self.excludes("\\".join(parts[:depth]), parts[depth - 1], is_dir or depth < len(parts)):
                return "\\".join(parts[:depth])
        return None
