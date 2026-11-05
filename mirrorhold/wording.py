"""How a count is written with what it counts: one rule for the command line, the records,
the notices and the window alike.

A count of one takes the singular and every other count, none included, the plural (the
maintainer's decision of 2026-10-19); a word that agrees with a count — "differs" or
"differ", "is" or "are", "it" or "them" — follows its number. In "3 of 10 files" the noun
counts the 10. The window and the notices write thousands apart (2,104,331); the command
line and the records write the bare number.

The program's counts of things — files, bytes, entries, batches, days, runs — are written
through here, so a change of wording has one place to change; a duration or a fixed limit
written with its unit is not such a count. The window imports it too; nothing here imports
the window.
"""
from __future__ import annotations


def number(n: int, *, separated: bool = False) -> str:
    """'2104331', or '2,104,331' with ``separated``."""
    return f"{n:,}" if separated else str(n)


def counted(n: int, noun: str, plural: str = "", *, separated: bool = False) -> str:
    """'1 file', '2 files', '0 files', '1 batch', '2 batches': ``n`` with ``noun`` in the number
    it needs; ``plural`` for a noun whose plural is not its singular with an s."""
    return f"{number(n, separated=separated)} {noun if n == 1 else plural or noun + 's'}"


def agreeing(n: int, one: str, other: str) -> str:
    """``one`` for a count of one, ``other`` for any other: 'differs' or 'differ', 'is' or 'are'."""
    return one if n == 1 else other


__all__ = ["agreeing", "counted", "number"]
