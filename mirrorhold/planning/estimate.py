"""The duration estimate a preview gives: quantities are exact, the time is a range
with its basis stated.

The only uncertain term is throughput, so the range comes from what this pair achieved
before: the copy phase's bytes per second — copied, or compared and found identical — in
its earlier runs that processed enough data to measure (``MIN_SAMPLE_BYTES``). With no
such run, the range is a stated default for a hard disk on USB 3 — the slowest
destination this tool expects, so a faster pair finishes early rather than late. Listing
time is reported apart: it depends on the number of files and the drive, not on bytes
(measured when the tool was designed: about 1,500 to 48,000 entries a second, cold,
across the development machine's drives).

A caller whose work also costs time per file — a verification, which opens and reads
both copies of each — gives the number of files and that cost's range, which is added to
each end; a preview gives neither, so its estimate is bytes over the rates alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

from ..wording import counted

MiB = 1024 * 1024

MIN_SAMPLE_BYTES = 64 * MiB
"""A copy phase that processed less than this measures start-up costs, not throughput."""

RECENT_SAMPLES = 5

DEFAULT_LOW = 30 * MiB
DEFAULT_HIGH = 150 * MiB
"""Bytes per second. A stated default, not a measurement: the range assumed for a hard
disk on USB 3, from many small files (low) to large ones (high)."""

SINGLE_SAMPLE_SPREAD = 0.25
"""With one earlier run, the range is that run's throughput plus and minus this share."""


@dataclass(frozen=True)
class Estimate:
    bytes_to_copy: int
    listing_seconds: float
    low_rate: float            # bytes per second
    high_rate: float
    samples: int               # earlier runs the rates come from; 0 for the default
    files: int = 0             # files that each cost time of their own; 0 when none is counted
    low_file_seconds: float = 0.0
    high_file_seconds: float = 0.0

    @property
    def basis(self) -> str:
        return "history" if self.samples else "default"

    @property
    def shortest_seconds(self) -> float:
        reading = self.bytes_to_copy / self.high_rate if self.bytes_to_copy else 0.0
        return reading + self.files * self.low_file_seconds

    @property
    def longest_seconds(self) -> float:
        reading = self.bytes_to_copy / self.low_rate if self.bytes_to_copy else 0.0
        return reading + self.files * self.high_file_seconds

    def describe(self) -> str:
        rates = f"{self.low_rate / MiB:.0f}-{self.high_rate / MiB:.0f} MiB/s"
        if self.samples:
            basis = f"this pair's {counted(self.samples, 'earlier run')}: {rates}"
        else:
            basis = f"default for a USB 3 hard disk, no earlier run on this pair to measure: {rates}"
        copy = ("nothing to copy" if not self.bytes_to_copy else
                f"copy {format_duration(self.shortest_seconds)} to {format_duration(self.longest_seconds)}")
        return f"{copy} (basis: {basis}); listing took {format_duration(self.listing_seconds)}"


def estimate(bytes_to_copy: int, listing_seconds: float, throughputs: Sequence[float], *, files: int = 0,
             file_seconds: Tuple[float, float] = (0.0, 0.0)) -> Estimate:
    """``throughputs``: bytes per second of this pair's earlier qualifying runs, oldest first.
    ``files`` and ``file_seconds`` (shortest, longest): the files the work handles and what
    each costs beside its bytes, which ``throughputs`` must already leave out."""
    per_file = (files, *file_seconds)
    recent = [t for t in throughputs if t > 0][-RECENT_SAMPLES:]
    if not recent:
        return Estimate(bytes_to_copy, listing_seconds, DEFAULT_LOW, DEFAULT_HIGH, 0, *per_file)
    if len(recent) == 1:
        return Estimate(bytes_to_copy, listing_seconds, recent[0] * (1 - SINGLE_SAMPLE_SPREAD),
                        recent[0] * (1 + SINGLE_SAMPLE_SPREAD), 1, *per_file)
    return Estimate(bytes_to_copy, listing_seconds, min(recent), max(recent), len(recent), *per_file)


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"
