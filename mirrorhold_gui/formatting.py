"""How the GUI writes quantities, sizes, times, durations and lists. No toolkit.

A count with its noun follows the core's one rule (``mirrorhold.wording``): the window writes
it with thousands separated (``counted(n, noun, separated=True)``), and a word that agrees
with a count through ``agreeing``."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from mirrorhold.planning.estimate import format_duration
from mirrorhold.wording import counted, number

_UNITS = ("KiB", "MiB", "GiB", "TiB", "PiB")


def count(n: int) -> str:
    """A whole number with thousands separated: 2,104,331."""
    return number(n, separated=True)


def size(n: float) -> str:
    """A byte count in binary units: 512 bytes, 48.1 MiB, 2.1 TiB."""
    if n < 1024:
        return counted(int(n), "byte")
    value = float(n)
    for unit in _UNITS:
        value /= 1024
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def rate(bytes_per_second: float) -> str:
    return f"{size(bytes_per_second)}/s"


def duration(seconds: float) -> str:
    """The core's own way of writing a duration, so the GUI and the command line agree."""
    return format_duration(seconds)


def files(n: int) -> str:
    return counted(n, "file", separated=True)


def parse_utc(text: Optional[str]) -> Optional[datetime]:
    """An ISO 8601 UTC time as the records write it (``2026-10-04T20:10:00Z``)."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_time(text: Optional[str], *, never: str = "never") -> str:
    """A recorded UTC time in this computer's time zone, to the minute."""
    moment = parse_utc(text)
    if moment is None:
        return never
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def elapsed(start: Optional[str], end: Optional[str]) -> str:
    began, ended = parse_utc(start), parse_utc(end)
    if began is None or ended is None:
        return "-"
    return duration((ended - began).total_seconds())


def sentence(text: str) -> str:
    """The text with its first letter in upper case."""
    return text[:1].upper() + text[1:] if text else text


def joined(parts: Sequence[str]) -> str:
    """'A', 'A and B', 'A, B and C'; "" for none."""
    if len(parts) < 2:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"
