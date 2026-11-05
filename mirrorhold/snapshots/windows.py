"""Volume snapshots through Windows' Volume Shadow Copy Service, reached by WMI.

``Win32_ShadowCopy.Create`` with the ``ClientAccessible`` context — the one context a
client edition of Windows accepts — makes a persistent snapshot: it outlives the process
that made it until it is removed, which is why the run records each one it makes
(``own``). Measured on the development machine: a snapshot of a 2 GiB NTFS volume took a
fraction of a second; its device path,
``\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy<n>``, lists, opens and is
copied from by the engine like any folder, a file another program holds without sharing
included; removing it makes the path unreadable at once; snapshots of other volumes are
untouched.

Making a snapshot needs administrator rights; listing them, removing one by its
identifier and reading a volume's room for snapshots (``Win32_ShadowStorage``) go through
the same service. A process whose token does not hold the
administrators group enabled is told so before the service is asked: asked anyway, the
service answers with a generic failure (``0x80041014``, measured under a token with the
group disabled), which would hide the reason. Everything is reached through ``pywin32``'s
COM client and ``ctypes``, so the module adds no dependency.
"""
from __future__ import annotations

import re
from typing import Callable, FrozenSet, Optional, Tuple

from .provider import Snapshot, SnapshotError, SnapshotFailure, SnapshotRoom

_ID = re.compile(r"^\{[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}$")
_DEVICE_ID = re.compile(r'DeviceID="(.*)"')

UNBOUNDED = 1 << 63
"""A room limit at or above this is WMI's way of saying the room has none."""

_RETURN_CODES = {
    1: (SnapshotFailure.DENIED, "access was denied: making a snapshot needs administrator rights"),
    2: (SnapshotFailure.FAILED, "the request was not valid"),
    3: (SnapshotFailure.REFUSED, "the snapshot service does not know this volume"),
    4: (SnapshotFailure.REFUSED, "the volume does not support snapshots"),
    5: (SnapshotFailure.FAILED, "this kind of snapshot is not supported here"),
    6: (SnapshotFailure.FAILED, "there is not enough room for a snapshot"),
    7: (SnapshotFailure.FAILED, "the volume is in use"),
    8: (SnapshotFailure.FAILED, "the volume holds as many snapshots as it can"),
    9: (SnapshotFailure.FAILED, "another snapshot is being made"),
    10: (SnapshotFailure.REFUSED, "the snapshot provider refused"),
    11: (SnapshotFailure.REFUSED, "no snapshot provider serves this volume"),
    12: (SnapshotFailure.FAILED, "the snapshot provider failed"),
    13: (SnapshotFailure.FAILED, "the snapshot service reported an unknown error"),
}
"""``Win32_ShadowCopy.Create``'s documented return values."""

_ACCESS_DENIED = (0x80041003, 0x80070005)   # WBEM_E_ACCESS_DENIED, E_ACCESSDENIED
_NOT_FOUND = 0x80041002                      # WBEM_E_NOT_FOUND


def failure_for_return(code: int) -> Tuple[SnapshotFailure, str]:
    """What a non-zero return value of ``Create`` means."""
    return _RETURN_CODES.get(code, (SnapshotFailure.FAILED, f"the snapshot service returned {code}"))


def com_code(exc: Exception) -> int:
    """The status a COM error carries: the WMI status in its exception information when
    there is one, else its own result, as an unsigned 32-bit number."""
    args = getattr(exc, "args", ())
    info = args[2] if len(args) > 2 else None
    code = info[5] if isinstance(info, tuple) and len(info) > 5 and info[5] else (args[0] if args else 0)
    try:
        return int(code or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def failure_for_com(exc: Exception) -> SnapshotError:
    code = com_code(exc)
    if code in _ACCESS_DENIED:
        return SnapshotError(SnapshotFailure.DENIED, "access was denied: making a snapshot needs administrator rights")
    if not code:
        return SnapshotError(SnapshotFailure.FAILED, f"the snapshot service failed ({type(exc).__name__}: {exc})")
    return SnapshotError(SnapshotFailure.FAILED, f"the snapshot service failed (0x{code:08X})")


def valid_id(shadow_id: str) -> bool:
    return isinstance(shadow_id, str) and bool(_ID.match(shadow_id))


def referenced_device(reference: str) -> str:
    """The volume device path a WMI reference to a ``Win32_Volume`` names, the backslashes
    WMI doubles in it undone; "" when it names none."""
    match = _DEVICE_ID.search(reference or "")
    return match.group(1).replace("\\\\", "\\") if match else ""


def holds_administrators() -> bool:
    """Whether this process's token holds the administrators group enabled. True when it
    cannot be told, so that the service itself answers."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    sid = ctypes.create_string_buffer(68)
    size = wintypes.DWORD(ctypes.sizeof(sid))
    member = wintypes.BOOL()
    if not advapi.CreateWellKnownSid(26, None, sid, ctypes.byref(size)):       # WinBuiltinAdministratorsSid
        return True
    if not advapi.CheckTokenMembership(None, sid, ctypes.byref(member)):
        return True
    return bool(member.value)


class WmiSnapshotProvider:
    """``SnapshotProvider`` over ``root\\cimv2``'s ``Win32_ShadowCopy``. Connects on first use,
    in the calling thread. ``is_administrator`` is asked first (``holds_administrators``
    unless given)."""

    def __init__(self, is_administrator: Callable[[], bool] = holds_administrators):
        self._service = None
        self._is_administrator = is_administrator

    def _wmi(self):
        if self._service is None:
            if not self._is_administrator():
                raise SnapshotError(SnapshotFailure.DENIED, "making a snapshot needs administrator rights, which this "
                                                            "run does not have")
            try:
                import pythoncom
                import win32com.client
            except ImportError as exc:
                raise SnapshotError(SnapshotFailure.UNAVAILABLE, f"pywin32 is not available: {exc}") from None
            pythoncom.CoInitialize()
            try:
                locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
                self._service = locator.ConnectServer(".", "root\\cimv2")
            except Exception as exc:  # a COM error: the service cannot be reached by this process
                error = failure_for_com(exc)
                raise SnapshotError(SnapshotFailure.DENIED if error.failure is SnapshotFailure.DENIED
                                    else SnapshotFailure.UNAVAILABLE, error.detail) from None
        return self._service

    def _all(self):
        try:
            return [(str(s.ID), str(s.DeviceObject), str(s.VolumeName))
                    for s in self._wmi().ExecQuery("SELECT ID, DeviceObject, VolumeName FROM Win32_ShadowCopy")]
        except SnapshotError:
            raise
        except Exception as exc:
            raise failure_for_com(exc) from None

    def shadow_ids(self, volume: str) -> FrozenSet[str]:
        return frozenset(i for i, _device, v in self._all() if v.casefold() == volume.casefold())

    def room(self, volume: str) -> Optional[SnapshotRoom]:
        service = self._wmi()
        try:
            for storage in service.ExecQuery("SELECT UsedSpace, MaxSpace, Volume, DiffVolume FROM Win32_ShadowStorage"):
                if referenced_device(str(storage.Volume)).casefold() != volume.casefold():
                    continue
                holder = service.Get(str(storage.DiffVolume))
                limit = int(storage.MaxSpace)
                return SnapshotRoom(int(storage.UsedSpace), None if limit >= UNBOUNDED else limit,
                                    int(holder.FreeSpace), int(holder.Capacity))
        except Exception as exc:
            raise failure_for_com(exc) from None
        return None

    def create(self, volume: str) -> Snapshot:
        service = self._wmi()
        try:
            cls = service.Get("Win32_ShadowCopy")
            params = cls.Methods_("Create").InParameters.SpawnInstance_()
            params.Volume = volume
            params.Context = "ClientAccessible"
            out = cls.ExecMethod_("Create", params)
            code, shadow_id = int(out.ReturnValue), str(out.ShadowID or "")
        except Exception as exc:
            raise failure_for_com(exc) from None
        if code != 0:
            raise SnapshotError(*failure_for_return(code))
        device = next((d for i, d, _v in self._all() if i == shadow_id), None)
        if not valid_id(shadow_id) or not device:
            if valid_id(shadow_id):
                self.delete(shadow_id)
            raise SnapshotError(SnapshotFailure.FAILED, "the snapshot was made but cannot be found")
        return Snapshot(shadow_id, device.rstrip("\\"), volume)

    def delete(self, shadow_id: str) -> bool:
        if not valid_id(shadow_id):
            raise SnapshotError(SnapshotFailure.FAILED, f"not a snapshot identifier: {shadow_id!r}")
        service = self._wmi()
        try:
            snapshot = service.Get(f"Win32_ShadowCopy.ID='{shadow_id}'")
        except Exception as exc:
            if com_code(exc) == _NOT_FOUND:
                return False
            raise failure_for_com(exc) from None
        try:
            snapshot.Delete_()
        except Exception as exc:
            raise failure_for_com(exc) from None
        return True
