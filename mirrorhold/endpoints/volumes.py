"""The volumes present on this machine, and what each one is.

``VolumeSource`` is the seam: the core asks a source for a ``VolumeScan`` and never
calls the operating system itself, so every rule above it (resolution, validation)
runs against ``FixedVolumeSource`` in tests and against ``WindowsVolumeSource`` in
production.

``WindowsVolumeSource`` reads metadata only — identity, mount points, label, file
system, capacity, the disk it lies on, and the names at a volume's root — and needs no
administrator rights: every device is opened with no access requested, which the
queries it sends allow. A volume whose file system cannot be read (a locked BitLocker
volume, an empty card reader) is reported as unavailable rather than listed, so nothing
can resolve to it: for every rule above, a locked volume is an absent one.

A volume's **drive** (``VolumeInfo.drive``) is what two activities contend for when they
use it at once — a disk's heads and its connection, not a letter: the physical disk
Windows names the volume as lying on, so two letters on one disk are one drive; and a
volume no single disk carries (a cloud drive, a spanned volume) is a drive of its own. A
disk is named by the identity Windows reads from the disk itself — its GPT disk GUID or
its MBR signature — and by its number only when its layout cannot be read: a disk's
number is handed out as disks attach and changes between sessions (measured on the
development machine: a USB disk's number changed from one day to the next).

A drive's **kind** (``VolumeInfo.drive_kind``) is what its disk reports of a seek penalty
(``DriveKind``): a spinning disk incurs one, a solid-state drive none. A volume whose device
Windows names a file system rather than a disk is a cloud drive (``device_kind``): it has no
heads to share, only a connection. A disk that does not answer, and a volume no single disk
carries that Windows does not name a file system — a volume spanning disks, or one whose
device cannot be asked — are of a kind not known. Measured on the development machine
(2026-10-21): an internal NVMe drive reports none; a USB disk behind an enclosure or a dock
may answer the query as an invalid function, as Windows' own storage view then shows it as
of no known media type; StableBit DrivePool's pool disk reports a seek penalty; and
(2026-11-01) Google Drive for desktop's streamed drive answers as a disk file system
device. What the disk reports is not the last word: the user may say what a drive is
(``jobs.model.DriveMark``), and that takes its place. A mark is kept by the drive's name
here, so only a drive whose name lasts can carry one (``lasting_drive``).
"""
from __future__ import annotations

import os
import re
import struct
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Protocol, Sequence, Tuple

from .endpoint import VolumeIdentity, format_serial, gpt_anchor, mbr_anchor, virtual_anchor

POOL_FOLDER = re.compile(r"^poolpart\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
"""The folder StableBit DrivePool keeps at the root of each member volume."""

FAT_FILE_SYSTEMS = ("FAT32", "FAT")
"""File systems that cap a file below 4 GiB and keep two-second, zone-less times."""

FAT_MAX_FILE_BYTES = 4 * 1024 ** 3 - 1

LOW_FREE_PERCENT = 10
"""A destination's drive with less free space than this share of its capacity is
reported as low: versions accumulate there while version cleanup is off."""


def free_percent(free_bytes: int, capacity_bytes: int) -> float:
    return 100.0 * free_bytes / capacity_bytes if capacity_bytes > 0 else 100.0


def low_on_space(free_bytes: int, capacity_bytes: int) -> bool:
    return capacity_bytes > 0 and free_percent(free_bytes, capacity_bytes) < LOW_FREE_PERCENT


class Backing(str, Enum):
    PARTITION = "partition"  # a file system on a disk partition, GPT or MBR
    VIRTUAL = "virtual"      # no disk partition behind it: a cloud drive or another virtual drive


class DriveKind(str, Enum):
    """What a drive is, as two activities sharing it are concerned: what its disk reports of
    a seek penalty, or a cloud drive."""

    SPINNING = "spinning"    # it incurs one: a hard disk
    SOLID = "solid"          # none: a solid-state drive
    CLOUD = "cloud"          # its device is a file system, not a disk: a cloud drive
    UNKNOWN = "unknown"      # its disk does not say, or no single disk carries it and it is no cloud drive


@dataclass(frozen=True)
class VolumeInfo:
    """One present, readable volume."""

    identity: VolumeIdentity
    device: str                    # \\?\Volume{guid}\ — reaches the volume with or without a letter
    mount_points: Tuple[str, ...]  # 'D:\', or a folder a volume is mounted on; may be empty
    label: str
    file_system: str
    capacity_bytes: int
    free_bytes: int
    backing: Backing
    pool_member: bool = False      # holds a DrivePool pool folder at its root
    disk: Optional[str] = None     # the disk it lies on (disk_identity); None: no single disk carries it
    disk_kind: DriveKind = DriveKind.UNKNOWN   # what that disk reports of a seek penalty; with no disk, device_kind

    @property
    def drive_kind(self) -> DriveKind:
        """The kind of the drive it lies on: its disk's; for a volume no single disk carries, a
        cloud drive when its device is a file system (``device_kind``), and otherwise not
        known, whatever lies beneath it."""
        if self.disk is not None or self.disk_kind is DriveKind.CLOUD:
            return self.disk_kind
        return DriveKind.UNKNOWN

    @property
    def drive(self) -> str:
        """The drive it lies on, as two activities contend for one: its disk, or for a volume
        no single disk carries, the volume itself."""
        return self.disk if self.disk is not None else self.identity.anchor

    @property
    def root(self) -> str:
        """A path to the volume's root: its first mount point, else its device path."""
        return self.mount_points[0] if self.mount_points else self.device

    @property
    def is_fat(self) -> bool:
        return self.file_system.upper() in FAT_FILE_SYSTEMS

    @property
    def snapshot_eligible(self) -> bool:
        """Whether the file system is one a volume snapshot can be taken of. Eligibility
        only: whether a snapshot succeeds is measured when snapshots are built."""
        return self.backing is Backing.PARTITION and self.file_system.upper() in ("NTFS", "REFS")

    def describe(self) -> str:
        where = ", ".join(self.mount_points) or "no letter"
        label = f" '{self.label}'" if self.label else ""
        return f"{where}{label} {self.file_system} ({self.identity.describe()})"


@dataclass(frozen=True)
class VolumeScan:
    volumes: Tuple[VolumeInfo, ...]
    unavailable: Tuple[Tuple[str, str], ...] = ()  # (device, reason)


class VolumeSource(Protocol):
    def scan(self) -> VolumeScan: ...


@dataclass(frozen=True)
class FixedVolumeSource:
    """A source that reports exactly the volumes it was given."""

    volumes: Tuple[VolumeInfo, ...] = ()
    unavailable: Tuple[Tuple[str, str], ...] = field(default=())

    def scan(self) -> VolumeScan:
        return VolumeScan(tuple(self.volumes), tuple(self.unavailable))


# --- decoding the two disk queries -------------------------------------------------
# Layouts from winioctl.h. PARTITION_INFORMATION_EX: PartitionStyle (DWORD) at 0,
# StartingOffset (LARGE_INTEGER) at 8, and the style's union at 32; within the GPT
# member, PartitionType (GUID) then PartitionId (GUID). DRIVE_LAYOUT_INFORMATION_EX:
# PartitionStyle at 0, PartitionCount at 4, and for MBR the disk Signature at 8.

PARTITION_STYLE_MBR = 0
PARTITION_STYLE_GPT = 1

NUMBERED_DISK = "disk:number:"
"""How ``disk_identity`` begins the name of a disk known only by its number."""


@dataclass(frozen=True)
class PartitionInfo:
    style: int
    starting_offset: int
    gpt_partition_id: Optional[str]


def decode_partition_info(raw: bytes) -> PartitionInfo:
    style = struct.unpack_from("<I", raw, 0)[0]
    offset = struct.unpack_from("<q", raw, 8)[0]
    gpt_id = None
    if style == PARTITION_STYLE_GPT:
        gpt_id = str(uuid.UUID(bytes_le=bytes(raw[48:64])))
    return PartitionInfo(style, offset, gpt_id)


def decode_mbr_signature(raw: bytes) -> Optional[int]:
    """The MBR disk signature from a drive layout, or None for another style."""
    style = struct.unpack_from("<I", raw, 0)[0]
    if style != PARTITION_STYLE_MBR:
        return None
    return struct.unpack_from("<I", raw, 8)[0]


def disk_identity(number: int, layout: Optional[bytes]) -> str:
    """A disk's name as ``VolumeInfo.disk`` holds it, from its drive layout: ``disk:gpt:<disk
    guid>`` (the GUID at the union's start, offset 8), ``disk:mbr:<signature>``, or, when
    the layout holds neither, ``disk:number:<n>``. Windows keeps online disks' GUIDs and
    signatures distinct, so either names one disk among those attached."""
    if layout and len(layout) >= 24:
        style = struct.unpack_from("<I", layout, 0)[0]
        if style == PARTITION_STYLE_GPT:
            return f"disk:gpt:{uuid.UUID(bytes_le=bytes(layout[8:24]))}"
        signature = decode_mbr_signature(layout)
        if signature:
            return f"disk:mbr:{signature:08x}"
    return f"{NUMBERED_DISK}{number}"


def lasting_drive(drive: str) -> bool:
    """Whether ``drive`` (``VolumeInfo.drive``) names the same drive from one session to the
    next: a disk by its GUID or signature, or a volume no single disk carries by its own
    anchor — but not a disk by its number, which Windows hands out as disks attach."""
    return bool(drive) and not drive.startswith(NUMBERED_DISK)


def decode_seek_penalty(raw: Optional[bytes]) -> DriveKind:
    """A disk's kind from its DEVICE_SEEK_PENALTY_DESCRIPTOR — Version and Size (DWORDs),
    then the IncursSeekPenalty byte at 8 — or not known when the disk returned none."""
    if not raw or len(raw) < 9:
        return DriveKind.UNKNOWN
    return DriveKind.SPINNING if raw[8] else DriveKind.SOLID


FILE_DEVICE_DISK_FILE_SYSTEM = 8
"""The device type a volume answers when it is a file system with no disk beneath it."""


def device_kind(device_type: int) -> DriveKind:
    """The kind of a volume whose device Windows names no disk, from the device type it
    answers: a disk file system — as Google Drive for desktop's drive answers — is a cloud
    drive; any other is not known. A volume that answers no device type at all (one spanning
    disks, or one whose device cannot be asked) is not known either."""
    return DriveKind.CLOUD if device_type == FILE_DEVICE_DISK_FILE_SYSTEM else DriveKind.UNKNOWN


def drive_name(volumes: Sequence[VolumeInfo], drive: str) -> str:
    """How a person calls a drive, from the volumes on it among ``volumes``: "drive I:",
    "the drive of C: and D:", "drive 'Backup'" by its label when no volume on it has a
    letter, or "a drive with no letter"."""
    on = [v for v in volumes if v.drive == drive]
    letters = sorted({m[:2].upper() for v in on for m in v.mount_points if re.match(r"^[A-Za-z]:\\?$", m)})
    if len(letters) == 1:
        return f"drive {letters[0]}"
    if letters:
        return "the drive of " + ", ".join(letters[:-1]) + f" and {letters[-1]}"
    label = next((v.label for v in on if v.label), "")
    return f"drive '{label}'" if label else "a drive with no letter"


def anchor_for(partition: Optional[PartitionInfo], mbr_signature: Optional[int],
               volume_guid: str) -> Tuple[str, Backing]:
    """The anchor a volume is identified by, from what the disk queries returned.
    ``partition`` is None when no disk partition carries the volume."""
    if partition is not None:
        if partition.style == PARTITION_STYLE_GPT and partition.gpt_partition_id:
            return gpt_anchor(partition.gpt_partition_id), Backing.PARTITION
        if partition.style == PARTITION_STYLE_MBR and mbr_signature is not None:
            return mbr_anchor(mbr_signature, partition.starting_offset), Backing.PARTITION
    return virtual_anchor(volume_guid), Backing.VIRTUAL


# --- the Windows source ------------------------------------------------------------

class WindowsVolumeSource:
    """Every volume Windows reports, read through kernel32 with no access requested."""

    FILE_DEVICE_DISK = 7
    NO_PARTITION = 0xFFFFFFFF
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
    IOCTL_DISK_GET_PARTITION_INFO_EX = 0x00070048
    IOCTL_DISK_GET_DRIVE_LAYOUT_EX = 0x00070050
    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    SEEK_PENALTY_QUERY = struct.pack("<II4x", 7, 0)   # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("WindowsVolumeSource needs Windows")
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._wt = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        k32.FindFirstVolumeW.restype = wintypes.HANDLE
        k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
        k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
        k32.GetVolumePathNamesForVolumeNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        k32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        k32.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong)]
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.SetErrorMode.argtypes = [wintypes.UINT]
        k32.SetErrorMode.restype = wintypes.UINT
        self._k32 = k32
        self._invalid = wintypes.HANDLE(-1).value

    def scan(self) -> VolumeScan:
        # An empty removable drive would otherwise raise a "no disk" dialog.
        previous = self._k32.SetErrorMode(0x0001)
        try:
            volumes, unavailable = [], []
            disks: Dict[int, Tuple[Optional[bytes], DriveKind]] = {}   # each disk's layout and kind, read once a scan
            for device in self._volume_names():
                try:
                    volumes.append(self._read(device, disks))
                except OSError as exc:
                    unavailable.append((device, exc.strerror or str(exc)))
            return VolumeScan(tuple(volumes), tuple(unavailable))
        finally:
            self._k32.SetErrorMode(previous)

    def _volume_names(self):
        buf = self._ct.create_unicode_buffer(1024)
        handle = self._k32.FindFirstVolumeW(buf, 1024)
        if handle == self._invalid:
            raise self._ct.WinError(self._ct.get_last_error())
        try:
            names = [buf.value]
            while self._k32.FindNextVolumeW(handle, buf, 1024):
                names.append(buf.value)
            return names
        finally:
            self._k32.FindVolumeClose(handle)

    def _read(self, device: str, disks: Dict[int, Tuple[Optional[bytes], DriveKind]]) -> VolumeInfo:
        ct, wt = self._ct, self._wt
        label = ct.create_unicode_buffer(261)
        fs = ct.create_unicode_buffer(261)
        serial, max_len, flags = wt.DWORD(), wt.DWORD(), wt.DWORD()
        if not self._k32.GetVolumeInformationW(device, label, 261, ct.byref(serial), ct.byref(max_len),
                                               ct.byref(flags), fs, 261):
            raise ct.WinError(ct.get_last_error())
        free, total, total_free = ct.c_ulonglong(), ct.c_ulonglong(), ct.c_ulonglong()
        if not self._k32.GetDiskFreeSpaceExW(device, ct.byref(free), ct.byref(total), ct.byref(total_free)):
            raise ct.WinError(ct.get_last_error())
        volume_guid = device[device.index("{") + 1:device.index("}")]
        partition, signature, disk, kind = self._partition(device, disks)
        anchor, backing = anchor_for(partition, signature, volume_guid)
        mounts = self._mount_points(device)
        identity = VolumeIdentity(anchor, format_serial(serial.value))
        pool_member = backing is Backing.PARTITION and self._holds_pool_folder(device)
        return VolumeInfo(identity, device, mounts, label.value, fs.value, total.value, free.value,
                          backing, pool_member, disk, kind)

    def _mount_points(self, device: str) -> Tuple[str, ...]:
        ct, wt = self._ct, self._wt
        size = wt.DWORD(0)
        buf = ct.create_unicode_buffer(1024)
        if not self._k32.GetVolumePathNamesForVolumeNameW(device, buf, 1024, ct.byref(size)):
            buf = ct.create_unicode_buffer(size.value)
            if not self._k32.GetVolumePathNamesForVolumeNameW(device, buf, size.value, ct.byref(size)):
                return ()
        return tuple(m for m in buf[:size.value].split("\0") if m)

    def _open(self, path: str):
        handle = self._k32.CreateFileW(path, 0, 0x3, None, 3, 0, None)
        return None if handle in (None, self._invalid) else handle

    def _ioctl(self, handle, code: int, size: int, query: bytes = b"") -> Optional[bytes]:
        ct, wt = self._ct, self._wt
        given = ct.create_string_buffer(query, len(query)) if query else None
        while size <= 1 << 20:
            out = ct.create_string_buffer(size)
            got = wt.DWORD()
            if self._k32.DeviceIoControl(handle, code, given, len(query), out, size, ct.byref(got), None):
                return out.raw[:got.value]
            if ct.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
                return None
            size *= 4
        return None

    def _partition(self, device: str, disks: Dict[int, Tuple[Optional[bytes], DriveKind]]):
        """(PartitionInfo or None, MBR signature or None, the disk's identity or None, the
        disk's kind): the identity and kind whenever Windows names the disk the volume lies
        on, a partition or not; with no disk, the kind its device type says (``device_kind``)."""
        handle = self._open(device.rstrip("\\"))
        if handle is None:
            return None, None, None, DriveKind.UNKNOWN
        try:
            number = self._ioctl(handle, self.IOCTL_STORAGE_GET_DEVICE_NUMBER, 64)
            if not number:
                return None, None, None, DriveKind.UNKNOWN
            device_type, disk, part = struct.unpack_from("<III", number, 0)
            if device_type != self.FILE_DEVICE_DISK:
                return None, None, None, device_kind(device_type)
            raw = (self._ioctl(handle, self.IOCTL_DISK_GET_PARTITION_INFO_EX, 256)
                   if part != self.NO_PARTITION else None)
        finally:
            self._k32.CloseHandle(handle)
        if disk not in disks:
            disks[disk] = self._disk(disk)
        layout, kind = disks[disk]
        identity = disk_identity(disk, layout)
        if not raw:
            return None, None, identity, kind
        info = decode_partition_info(raw)
        signature = decode_mbr_signature(layout) if layout and info.style == PARTITION_STYLE_MBR else None
        return info, signature, identity, kind

    def _disk(self, disk: int) -> Tuple[Optional[bytes], DriveKind]:
        """Disk ``disk``'s drive layout, None when it cannot be read, and its kind, both read
        with no access requested."""
        handle = self._open(rf"\\.\PhysicalDrive{disk}")
        if handle is None:
            return None, DriveKind.UNKNOWN
        try:
            return (self._ioctl(handle, self.IOCTL_DISK_GET_DRIVE_LAYOUT_EX, 4096),
                    decode_seek_penalty(self._ioctl(handle, self.IOCTL_STORAGE_QUERY_PROPERTY, 64,
                                                    self.SEEK_PENALTY_QUERY)))
        finally:
            self._k32.CloseHandle(handle)

    @staticmethod
    def _holds_pool_folder(device: str) -> bool:
        try:
            with os.scandir(device) as entries:
                return any(entry.is_dir(follow_symlinks=False) and POOL_FOLDER.match(entry.name)
                           for entry in entries)
        except OSError:
            return False


def default_volume_source() -> VolumeSource:
    return WindowsVolumeSource()


def find_by_mount(volumes: Sequence[VolumeInfo], path: str) -> Optional[Tuple[VolumeInfo, str]]:
    """The volume an absolute path lies on, by the longest mount point or device path
    that prefixes it, and the remainder of the path below it."""
    text = path.replace("/", "\\")
    if re.match(r"^\\\\\?\\[A-Za-z]:", text):
        text = text[4:]
    if not text.endswith("\\"):
        text += "\\"
    folded = text.casefold()
    best = None
    for volume in volumes:
        for root in (*volume.mount_points, volume.device):
            r = root if root.endswith("\\") else root + "\\"
            if folded.startswith(r.casefold()) and (best is None or len(r) > len(best[1])):
                best = (volume, r)
    if best is None:
        return None
    volume, root = best
    return volume, text[len(root):].strip("\\")
