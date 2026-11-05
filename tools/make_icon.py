"""Write MirrorHold's icon as a Windows icon file.

    python tools/make_icon.py ICO [--png DIR]

from any folder. The icon is drawn by the program itself (``mirrorhold/icon.py``, whose
docstring describes the mark); this script is how the release build and the source
installation write it, each starting it as a process of its own on the tree it builds or
installs from, so the file is that tree's drawing. ``--png`` also writes each size as a
PNG file, for review.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mirrorhold.icon import SIZES, icon, png, render  # noqa: E402


def write_icon(path: Path, *, png_dir: Optional[Path] = None, sizes: Sequence[int] = SIZES,
               say: Callable[[str], None] = lambda _: None) -> Path:
    """Draw every size and write the icon file at ``path`` (and each size as a PNG into
    ``png_dir`` when given). Returns ``path``."""
    images = {size: render(size) for size in sizes}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(icon(images))
    say(f"Icon: {path} ({', '.join(str(s) for s in sizes)} pixels)")
    if png_dir is not None:
        Path(png_dir).mkdir(parents=True, exist_ok=True)
        for size, pixels in images.items():
            Path(png_dir, f"mirrorhold-{size}.png").write_bytes(png(size, pixels))
    return Path(path)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Draw MirrorHold's icon.")
    parser.add_argument("icon", type=Path, help="the icon file to write")
    parser.add_argument("--png", type=Path, metavar="DIR", help="also write each size as a PNG file here")
    args = parser.parse_args(argv)
    write_icon(args.icon, png_dir=args.png, say=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
