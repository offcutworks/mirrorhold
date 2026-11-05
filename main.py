"""MirrorHold's launcher: opens the GUI, exactly as ``pythonw -m mirrorhold_gui`` does.

    pythonw main.py [--store FILE]

from any folder: the packages are imported from this file's folder, and every run or
verification the window starts is a core process started in that folder
(``mirrorhold.run.launch``). Those processes use the interpreter that runs this file,
so start it with the environment that carries the tool's dependencies — double-clicking
it uses whatever interpreter Windows associates with ``.py`` files.

The Start menu entry ``install`` writes opens the installed copy's GUI instead; this file
is not part of that copy.
"""
import sys

from mirrorhold_gui.app_view import main

if __name__ == "__main__":
    sys.exit(main())
