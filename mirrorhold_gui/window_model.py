"""Where the main window was when it last closed, and where it opens next. No toolkit.

The window keeps its place — size, position, whether it was maximised, and the divider
between the job list and the tabs — in the GUI's own remembered state
(``Preferences.gui_state``, which the core stores and never reads), under one key beside
the groups closed in the list. It is a convenience: a remembered value that cannot be
used is passed over, never reported, and the window then opens at its content's size.

Sizes and positions are Tk's for a toplevel: the size is the inside of the window, the
position the top-left corner of its frame, in the physical pixels of a DPI-aware process.
The divider is kept as the list's share of the height the list and the tabs share, not as
pixels, so it survives a window that had to be made smaller to fit.

A remembered place is fitted before it is used (``fit``): onto the monitor whose working
area (the screen less the taskbar) it overlaps most, the main monitor when it overlaps
none — a monitor no longer attached — shrunk to that area and moved wholly onto it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

WINDOW_KEY = "main_window"
"""The key of the GUI's remembered state that holds the main window's place."""

MIN_SHARE, MAX_SHARE = 0.05, 0.95
"""The divider's bounds as the list's share: neither the list nor the tabs is ever
remembered closed."""

_MAX_PIXELS = 100_000
_GEOMETRY = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")


@dataclass(frozen=True)
class Area:
    """A rectangle of the desktop, right and bottom exclusive."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def overlap(self, other: "Area") -> int:
        """The area, in square pixels, that the two rectangles share."""
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return w * h if w > 0 and h > 0 else 0


@dataclass(frozen=True)
class Place:
    """The main window's place: ``x``, ``y`` its frame's top-left corner, ``width`` and
    ``height`` its inside, ``divider`` the list's share of the height it shares with the
    tabs (None: as the window lays it out)."""

    x: int
    y: int
    width: int
    height: int
    maximised: bool = False
    divider: Optional[float] = None

    def to_document(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {"x": self.x, "y": self.y, "width": self.width, "height": self.height,
                               "maximised": self.maximised}
        if self.divider is not None:
            doc["divider"] = self.divider
        return doc


def _whole(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _MAX_PIXELS else None


def share(value: Any) -> Optional[float]:
    """A divider share kept within its bounds, rounded so a window closed where it opened
    keeps the same value; None for anything that is not a number between 0 and 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
        return None
    return round(min(max(float(value), MIN_SHARE), MAX_SHARE), 3)


def remembered_place(gui_state: Mapping[str, Any]) -> Optional[Place]:
    """The main window's remembered place; None when none is remembered or its size and
    position cannot be used. A divider or a maximised flag that cannot be used is dropped
    alone."""
    doc = gui_state.get(WINDOW_KEY)
    if not isinstance(doc, Mapping):
        return None
    x, y, width, height = (_whole(doc.get(k)) for k in ("x", "y", "width", "height"))
    if None in (x, y, width, height) or width < 1 or height < 1:
        return None
    maximised = doc.get("maximised")
    return Place(x, y, width, height, maximised if isinstance(maximised, bool) else False,
                 share(doc.get("divider")))


def with_place(gui_state: Mapping[str, Any], place: Place) -> Dict[str, Any]:
    """The GUI's remembered state holding ``place`` as the main window's."""
    state = dict(gui_state)
    state[WINDOW_KEY] = place.to_document()
    return state


def fit(place: Place, areas: Sequence[Area], frame: Tuple[int, int] = (0, 0)) -> Place:
    """``place`` moved and shrunk to lie wholly inside one working area. ``areas``: each
    monitor's working area, the main monitor's first; ``frame``: how much wider and taller
    the window's frame is than its inside."""
    if not areas:
        return place
    frame_w, frame_h = max(0, frame[0]), max(0, frame[1])
    outer = Area(place.x, place.y, place.x + place.width + frame_w, place.y + place.height + frame_h)
    best = max(areas, key=outer.overlap)
    area = best if best.overlap(outer) else areas[0]
    width = max(1, min(place.width, area.width - frame_w))
    height = max(1, min(place.height, area.height - frame_h))
    x = max(area.left, min(place.x, area.right - width - frame_w))
    y = max(area.top, min(place.y, area.bottom - height - frame_h))
    return Place(x, y, width, height, place.maximised, place.divider)


def parse_geometry(text: str) -> Optional[Tuple[int, int, int, int]]:
    """(width, height, x, y) from Tk's ``WxH+X+Y``; None for any other form."""
    match = _GEOMETRY.match(text.strip())
    return tuple(int(g) for g in match.groups()) if match else None


def geometry_text(place: Place) -> str:
    """``place`` as Tk's ``WxH+X+Y``."""
    return f"{place.width}x{place.height}+{place.x}+{place.y}"
