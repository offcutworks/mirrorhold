"""Obtain what a release is built from: download each pinned input and check it.

    python tools/fetch_build_inputs.py [--from DIR] [--to DIR]

from any folder. Each input ``build_inputs.INPUTS`` names is downloaded from its
publisher, or copied from DIR when a copy is there already, into ``build-inputs/``
beside this folder (or ``--to``), and put in place only when its SHA-256 matches the pin.
An input already in place is checked and left as it is. The downloading and the checking
are ``release_core.py``'s, beside this file, which every published tool shares; this file
names MirrorHold's inputs and the user agent its downloads send. The engine is obtained
by ``fetch_engine.py``.

Exit status: 0 every input is in place, 1 refused or failed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Tuple

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from build_inputs import INPUTS  # noqa: E402
from release_core import INPUTS_DIR_NAME, Input, fetch_main  # noqa: E402
import release_core  # noqa: E402

USER_AGENT = "mirrorhold-fetch-build-inputs/1.0.0"


def default_folder() -> Path:
    return TOOLS.parent / INPUTS_DIR_NAME


def obtain(item: Input, folder: Path, **options) -> Tuple[Path, bool]:
    """One pinned input in ``folder``, and whether it was there already: ``release_core.obtain``
    under MirrorHold's user agent."""
    return release_core.obtain(item, folder, user_agent=USER_AGENT, **options)


def main(argv: Optional[list] = None) -> int:
    return fetch_main(argv, INPUTS, user_agent=USER_AGENT, folder=default_folder())


if __name__ == "__main__":
    sys.exit(main())
