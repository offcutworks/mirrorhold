"""MirrorHold's icon, drawn from its geometry by this module alone, with the standard
library: the icon is reviewed as code and never committed as an image.

The mark is two rounded blocks side by side on a teal tile: the left one solid, the right
one its outline — a file and the copy that holds it. Three consumers draw it here: the
release build and the source installation write it as a Windows icon file through
``tools/make_icon.py`` (started as a process of its own, so the file is the commit's
drawing), a copy that ships no icon file — a working copy — draws the sizes its
windows use when the GUI starts (``mirrorhold_gui.common_view.apply_icon``), and the
notification-area icon draws it at the size Windows gives that area while an activity
works, its outlined block — the copy — filled from the bottom as the work progresses and
its tile grey while the work is paused, amber while a finished activity's reminder stands
(``render``'s ``fill`` and ``tile``).

Every size is drawn on its own pixel grid. From 48 pixels down, each edge of the two
blocks lands on a pixel boundary and the outline is a whole number of pixels wide, so
the small sizes Windows shows in lists and on the taskbar stay sharp; larger sizes keep
the geometry as drawn. Edges are smoothed by sampling each pixel on a sub-grid. In an icon
file, sizes up to 64 pixels are stored as bitmaps and the largest as PNG, the layout
Windows' own icons use.

Drawing is pure Python and its cost grows with the area: every size up to 64 pixels takes
about a tenth of a second, the 256-pixel one most of a second (measured on the
development machine), which is why a window draws ``WINDOW_SIZES`` only.
"""
from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Tuple

TEAL = (0x0F, 0x6E, 0x56)
WHITE = (0xFF, 0xFF, 0xFF)
GREY = (0x6D, 0x6D, 0x6D)
"""The tile of a paused activity's icon: the grey the window gives what is not available now."""
AMBER = (0xB0, 0x5A, 0x00)
"""The tile of the icon while a finished activity's reminder stands: a colour that asks to be
looked at, dark enough for the white blocks to stay plain."""
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
"""The sizes an icon file holds."""
WINDOW_SIZES = tuple(size for size in SIZES if size <= 64)
"""The sizes a window's title bar and taskbar button take theirs from, at every display
scale up to 200 %; Tk picks the nearest for each."""
SNAPPED_UP_TO = 48
PNG_FROM = 128
SAMPLES = 4

# The design, on a 256-unit square: (left, top, right, bottom, corner radius).
DESIGN = 256
TILE = (16, 16, 240, 240, 52)
SOLID = (54, 74, 118, 182, 12)
OUTLINE = (138, 74, 202, 182, 12)
OUTLINE_WIDTH = 14

Box = Tuple[float, float, float, float, float]
Pixels = List[Tuple[int, int, int, int]]


def _scaled(box: Box, size: int, snap: bool) -> Box:
    s = size / DESIGN
    left, top, right, bottom, radius = (v * s for v in box)
    if snap:
        left, top, right, bottom = (float(round(v)) for v in (left, top, right, bottom))
    return left, top, right, bottom, radius


def _inside(box: Box, x: float, y: float) -> bool:
    left, top, right, bottom, radius = box
    if not (left <= x < right and top <= y < bottom):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def geometry(size: int) -> Tuple[Box, Box, Box, Box]:
    """The tile, the solid block, and the outline's outer and inner edges at ``size`` pixels."""
    snap = size <= SNAPPED_UP_TO
    tile = _scaled(TILE, size, snap)
    solid = _scaled(SOLID, size, snap)
    outer = _scaled(OUTLINE, size, snap)
    width = OUTLINE_WIDTH * size / DESIGN
    if snap:
        width = float(max(1, round(width)))
    left, top, right, bottom, radius = outer
    inner = (left + width, top + width, right - width, bottom - width, max(0.0, radius - width))
    return tile, solid, outer, inner


def fill_top(inner: Box, fill: float, snap: bool) -> float:
    """Where the filled part of the outline's inside begins, ``fill`` (0 to 1) of its height
    from the bottom; on a whole pixel at the snapped sizes, so the fill's edge stays sharp."""
    _left, top, _right, bottom, _radius = inner
    level = bottom - max(0.0, min(1.0, fill)) * (bottom - top)
    return float(round(level)) if snap else level


def render(size: int, *, fill: float = 0.0, tile: Tuple[int, int, int] = TEAL) -> Pixels:
    """The icon at ``size`` pixels: rows top to bottom, each pixel (red, green, blue, alpha).
    ``fill``: how much of the outline's inside — the copy — is filled from the bottom, 0 to
    1; ``tile``: the tile's colour. The defaults draw the program's icon."""
    tiled, solid, outer, inner = geometry(size)
    filled_from = fill_top(inner, fill, size <= SNAPPED_UP_TO) if fill > 0 else None
    step = 1.0 / SAMPLES
    offsets = [(i + 0.5) * step for i in range(SAMPLES)]
    pixels: Pixels = []
    for row in range(size):
        for column in range(size):
            red = green = blue = alpha = 0
            for dy in offsets:
                for dx in offsets:
                    x, y = column + dx, row + dy
                    if _inside(solid, x, y) or (_inside(outer, x, y) and not _inside(inner, x, y)):
                        colour = WHITE
                    elif filled_from is not None and y >= filled_from and _inside(inner, x, y):
                        colour = WHITE
                    elif _inside(tiled, x, y):
                        colour = tile
                    else:
                        continue
                    red, green, blue, alpha = red + colour[0], green + colour[1], blue + colour[2], alpha + 1
            if alpha:
                pixels.append((round(red / alpha), round(green / alpha), round(blue / alpha),
                               round(255 * alpha / SAMPLES ** 2)))
            else:
                pixels.append((0, 0, 0, 0))
    return pixels


def png(size: int, pixels: Pixels) -> bytes:
    """A PNG image of ``pixels``, compressed the same way every time."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes(v for p in pixels[r * size:(r + 1) * size] for v in p) for r in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def bitmap(size: int, pixels: Pixels) -> bytes:
    """An icon bitmap of ``pixels``: 32-bit colour bottom row first, then the transparency mask."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    colour = b"".join(bytes((b, g, r, a)) for row in range(size - 1, -1, -1)
                      for r, g, b, a in pixels[row * size:(row + 1) * size])
    stride = (size + 31) // 32 * 4
    mask = bytearray()
    for row in range(size - 1, -1, -1):
        bits = bytearray(stride)
        for column in range(size):
            if pixels[row * size + column][3] == 0:
                bits[column // 8] |= 0x80 >> (column % 8)
        mask += bits
    return header + colour + bytes(mask)


def icon(images: Dict[int, Pixels]) -> bytes:
    """A Windows icon file holding every image of ``images`` (size -> pixels)."""
    sizes = sorted(images)
    entries = [png(s, images[s]) if s >= PNG_FROM else bitmap(s, images[s]) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(head) + 16 * len(sizes)
    directory = b""
    for size, data in zip(sizes, entries):
        side = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + directory + b"".join(entries)
