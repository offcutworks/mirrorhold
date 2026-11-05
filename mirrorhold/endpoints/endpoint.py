"""Volume identity and endpoints: where a job's data lives, independent of drive letters.

A volume is identified by an ANCHOR (where the file system sits) plus the file
system's SERIAL number:

- ``gpt:<partition guid>``           the GPT partition's own GUID, stored on the disk;
- ``mbr:<disk signature>:<offset>``  the MBR disk signature and the partition's byte
                                     offset, both stored on the disk;
- ``volume:<volume guid>``           a volume no disk partition carries (a cloud or other
                                     virtual drive); the GUID is the mount manager's.

The serial changes when the volume is formatted, so a reformatted volume no longer
matches, and a sector-level clone matches twice — which resolution refuses. Letters
and labels never take part.

An endpoint is a volume identity plus a path relative to that volume's root, stored
with backslash separators and no leading or trailing separator; the empty path is
the drive root. Paths compare case-insensitively, as NTFS does by default; Unicode
forms are not normalised, because NTFS keeps NFC and NFD spellings as two names.

A destination may instead be a folder of a remote of the user's rclone configuration
(``RemoteEndpoint``): the remote's name and a '/'-separated folder inside it. Its document
holds ``remote`` and ``folder`` where a drive's holds ``volume`` and ``path``, so a drive's
document reads exactly as before (``endpoint_from_document`` tells the two apart). The two
kinds never contain one another: a remote is reached only through the engine, and what a
remote's folder is on this computer — a local remote, or one wrapping another remote — is
in the user's configuration, which is never read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

from ..contract_errors import ContractError, require_keys

_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ANCHOR = re.compile(rf"^(gpt:{_GUID}|mbr:[0-9a-f]{{8}}:\d+|volume:{_GUID})$")
_SERIAL = re.compile(r"^[0-9A-F]{8}$")
_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')


def gpt_anchor(partition_guid: str) -> str:
    return f"gpt:{partition_guid.lower()}"


def mbr_anchor(disk_signature: int, offset: int) -> str:
    return f"mbr:{disk_signature & 0xFFFFFFFF:08x}:{offset}"


def virtual_anchor(volume_guid: str) -> str:
    return f"volume:{volume_guid.lower()}"


def format_serial(serial: int) -> str:
    return f"{serial & 0xFFFFFFFF:08X}"


@dataclass(frozen=True)
class VolumeIdentity:
    """Where a file system sits, and which file system it is."""

    anchor: str
    serial: str

    def __post_init__(self):
        if not isinstance(self.anchor, str) or not _ANCHOR.match(self.anchor):
            raise ContractError("anchor", f"not a volume anchor: {self.anchor!r}")
        if not isinstance(self.serial, str) or not _SERIAL.match(self.serial):
            raise ContractError("serial", f"not an eight-digit hexadecimal serial: {self.serial!r}")

    @property
    def kind(self) -> str:
        """``gpt``, ``mbr`` or ``volume``."""
        return self.anchor.split(":", 1)[0]

    def describe(self) -> str:
        return f"{self.anchor} serial {self.serial}"

    def to_document(self) -> dict:
        return {"anchor": self.anchor, "serial": self.serial}

    @classmethod
    def from_document(cls, doc: Any) -> "VolumeIdentity":
        require_keys(doc, "", ("anchor", "serial"))
        return cls(doc["anchor"], doc["serial"])


def normalise_relative(path: str) -> str:
    """The stored form of a path relative to a volume root.

    Accepts either separator; refuses a ``.`` or ``..`` component, an empty
    component, a name ending in a space or a dot, and characters Windows forbids in
    a name (a colon would also address an alternate data stream)."""
    if not isinstance(path, str):
        raise ContractError("", f"not a path: {path!r}")
    text = path.replace("/", "\\").strip("\\")
    if not text:
        return ""
    parts = text.split("\\")
    for part in parts:
        if part in ("", ".", ".."):
            raise ContractError("", f"a relative path may not hold an empty, '.' or '..' component: {path!r}")
        if _FORBIDDEN.search(part):
            raise ContractError("", f"a name holds a character Windows forbids: {part!r}")
        if part != part.rstrip(" ."):
            raise ContractError("", f"a name may not end in a space or a dot: {part!r}")
    return "\\".join(parts)


def path_key(path: str) -> Tuple[str, ...]:
    """Components of a stored relative path in their comparison form."""
    return tuple(part.casefold() for part in path.split("\\")) if path else ()


def first_component(path: str) -> Optional[str]:
    return path.split("\\", 1)[0] if path else None


@dataclass(frozen=True)
class Endpoint:
    """A folder or a drive root, by volume identity and path relative to its root."""

    volume: VolumeIdentity
    path: str = ""

    def __post_init__(self):
        try:
            object.__setattr__(self, "path", normalise_relative(self.path))
        except ContractError as exc:
            raise exc.within("path") from None

    @property
    def is_drive_root(self) -> bool:
        return self.path == ""

    def contains(self, other: "AnyEndpoint") -> bool:
        """True when ``other`` is this endpoint or lies beneath it."""
        if not isinstance(other, Endpoint) or self.volume != other.volume:
            return False
        mine, theirs = path_key(self.path), path_key(other.path)
        return theirs[:len(mine)] == mine

    def overlaps(self, other: "AnyEndpoint") -> bool:
        """Equal, or either one inside the other."""
        return self.contains(other) or other.contains(self)

    def describe(self) -> str:
        return f"{self.volume.describe()} \\{self.path}"

    def to_document(self) -> dict:
        return {"volume": self.volume.to_document(), "path": self.path}

    @classmethod
    def from_document(cls, doc: Any) -> "Endpoint":
        require_keys(doc, "", ("volume", "path"))
        try:
            volume = VolumeIdentity.from_document(doc["volume"])
        except ContractError as exc:
            raise exc.within("volume") from None
        return cls(volume, doc["path"])


_REMOTE_NAME = re.compile(r"^[\w.+@-][\w. +@-]*$")
"""A remote's name as rclone accepts one: letters, digits, '_', '-', '.', '+', '@' and
spaces, not starting with '-' or a space (rclone's own rule), nor ending with a space."""


def check_remote_name(name: Any) -> str:
    """``name`` when it can name a remote here; ContractError otherwise. A name of one
    letter is refused: on Windows the engine reads ``X:`` as a drive, not a remote."""
    if not isinstance(name, str) or not _REMOTE_NAME.match(name) or name.startswith("-") or name.endswith(" "):
        raise ContractError("", f"not a remote's name: {name!r}")
    if len(name) == 1 and name.isascii() and name.isalpha():
        raise ContractError("", f"a remote of one letter cannot be told from a drive: {name!r}")
    return name


def normalise_remote_folder(folder: Any) -> str:
    """The stored form of a folder inside a remote: '/'-separated, no trailing separator,
    one leading '/' kept where given — some services (SFTP, a local remote) read it as the
    top of their file system rather than the account's home. A backslash, a control
    character, and an empty, '.' or '..' component are refused."""
    if not isinstance(folder, str):
        raise ContractError("", f"not a folder: {folder!r}")
    if "\\" in folder or any(ord(c) < 0x20 or c == "\x7f" for c in folder):
        raise ContractError("", f"a remote's folder is written with '/' and holds no control character: {folder!r}")
    top = folder.startswith("/")
    text = folder.strip("/")
    if not text:
        return "/" if top else ""
    for part in text.split("/"):
        if part in ("", ".", ".."):
            raise ContractError("", f"a folder may not hold an empty, '.' or '..' component: {folder!r}")
    return ("/" if top else "") + text


@dataclass(frozen=True)
class RemoteEndpoint:
    """A folder of a remote of the user's rclone configuration: the remote's name as the
    configuration spells it, and the folder inside it ('' for the remote's top). Reached
    only through the engine; proved the job's by its marker, as a drive's folder is.

    Two remote folders contain one another by name and folder, compared without case — a
    service may fold case, and treating two spellings as one refuses more, never less. A
    leading '/' takes no part: whether it names another folder is the service's to say."""

    remote: str
    folder: str = ""

    def __post_init__(self):
        try:
            check_remote_name(self.remote)
        except ContractError as exc:
            raise exc.within("remote") from None
        try:
            object.__setattr__(self, "folder", normalise_remote_folder(self.folder))
        except ContractError as exc:
            raise exc.within("folder") from None

    @property
    def is_drive_root(self) -> bool:
        """A remote's folder holds none of a drive root's artefacts."""
        return False

    def _key(self) -> Tuple[str, ...]:
        return (self.remote.casefold(),) + tuple(p.casefold() for p in self.folder.split("/") if p)

    def contains(self, other: "AnyEndpoint") -> bool:
        """True when ``other`` is this folder or lies beneath it, on the same remote."""
        if not isinstance(other, RemoteEndpoint):
            return False
        mine, theirs = self._key(), other._key()
        return theirs[:len(mine)] == mine

    def overlaps(self, other: "AnyEndpoint") -> bool:
        return self.contains(other) or other.contains(self)

    def describe(self) -> str:
        """The folder as rclone names it: ``remote:folder``."""
        return f"{self.remote}:{self.folder}"

    def to_document(self) -> dict:
        return {"remote": self.remote, "folder": self.folder}

    @classmethod
    def from_document(cls, doc: Any) -> "RemoteEndpoint":
        require_keys(doc, "", ("remote", "folder"))
        return cls(doc["remote"], doc["folder"])

    @classmethod
    def parse(cls, text: Any) -> "RemoteEndpoint":
        """``REMOTE:FOLDER`` as the command line takes it; ContractError when it is not one."""
        if not isinstance(text, str) or ":" not in text:
            raise ContractError("", f"expected REMOTE:FOLDER, got {text!r}")
        remote, folder = text.split(":", 1)
        return cls(remote, folder)


AnyEndpoint = Union[Endpoint, RemoteEndpoint]
"""Where a destination lies: a folder of this computer's drives, or of a remote."""


def endpoint_from_document(doc: Any) -> AnyEndpoint:
    """A destination's endpoint, of either kind, by the keys its document holds."""
    if isinstance(doc, dict) and "remote" in doc:
        return RemoteEndpoint.from_document(doc)
    return Endpoint.from_document(doc)
