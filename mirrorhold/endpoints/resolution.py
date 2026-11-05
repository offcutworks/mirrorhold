"""Resolving an endpoint to a present volume, and capturing an endpoint from a path.

Resolution matches the stored identity against the volumes present now: no match is
ABSENT (the pair waits), one match is PRESENT, two or more are AMBIGUOUS — a cloned
disk carries the same partition GUID and serial — and a run against an ambiguous
endpoint is refused rather than guessed. Letters and labels play no part; the
location returned is built from whatever mount point the volume has today.

A remote's folder (``RemoteEndpoint``) is never among this computer's volumes, so it
resolves here as ABSENT, whatever the remote's state: whether it can be reached is the
engine's to say (``remotes.reach``), never the scan's.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..contract_errors import ContractError
from ..wording import agreeing, counted
from .endpoint import AnyEndpoint, Endpoint, RemoteEndpoint
from .volumes import VolumeInfo, find_by_mount


class Presence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Resolution:
    endpoint: AnyEndpoint
    presence: Presence
    candidates: Tuple[VolumeInfo, ...] = ()

    @property
    def volume(self) -> Optional[VolumeInfo]:
        return self.candidates[0] if self.presence is Presence.PRESENT else None

    @property
    def location(self) -> Optional[Path]:
        """The endpoint's folder as reachable now, or None unless present."""
        volume = self.volume
        if volume is None:
            return None
        root = Path(volume.root)
        return root / self.endpoint.path if self.endpoint.path else root

    def describe(self) -> str:
        if self.presence is Presence.PRESENT:
            return f"present at {self.location}"
        if self.presence is Presence.ABSENT:
            return "absent"
        where = "; ".join(v.describe() for v in self.candidates)
        many = len(self.candidates)
        return (f"ambiguous: {counted(many, 'present volume')} {agreeing(many, 'carries', 'carry')} this identity "
                f"({where})")


def resolve(endpoint: AnyEndpoint, volumes: Sequence[VolumeInfo]) -> Resolution:
    if isinstance(endpoint, RemoteEndpoint):
        return Resolution(endpoint, Presence.ABSENT)
    matches = tuple(v for v in volumes if v.identity == endpoint.volume)
    if not matches:
        return Resolution(endpoint, Presence.ABSENT)
    if len(matches) > 1:
        return Resolution(endpoint, Presence.AMBIGUOUS, matches)
    return Resolution(endpoint, Presence.PRESENT, matches)


class EndpointError(ValueError):
    pass


def endpoint_from_path(path, volumes: Sequence[VolumeInfo]) -> Endpoint:
    """The endpoint for an absolute path on a present local volume.

    A network path, a relative path and a path on no present volume are refused:
    an endpoint must be identifiable by its volume."""
    text = str(path)
    if text.startswith("\\\\") and not text.startswith("\\\\?\\"):
        raise EndpointError(f"{text}: a network path is not an endpoint; choose a folder on a local drive")
    found = find_by_mount(volumes, text)
    if found is None:
        raise EndpointError(f"{text}: not on any present local volume")
    volume, remainder = found
    try:
        return Endpoint(volume.identity, remainder)
    except ContractError as exc:
        raise EndpointError(f"{text}: {exc.message}") from None
