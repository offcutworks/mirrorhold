"""What a MirrorHold release is built from beyond its runtime, each input pinned by the
SHA-256 its publisher lists.

Every release's runtime — CPython's embeddable package, with Tk from CPython's full
package of the same version — is pinned in ``release_core.py``, the release build's
shared core. A MirrorHold release also carries pywin32 beside the runtime, pinned below,
and the pinned engine (``mirrorhold.engine.rclone``), whose release folder's ``README.txt``
carries its licence. Its setup program is compiled by Inno Setup, whose installer is pinned
below: each build unpacks the compiler from it into a folder of its own, in the installer's
portable mode, which registers nothing on the computer, and deletes it afterwards, so the
pinned file is the only compiler a build trusts. ``fetch_build_inputs.py`` downloads
``INPUTS`` into ``build-inputs/`` (git-ignored) and checks them; ``build_release.py``
checks them again before it uses them and downloads nothing.

Moving to another pywin32, or another Inno Setup, means changing its pin, fetching again,
and building: a build runs the whole suite under the interpreter it assembled, and
compiles the setup program anew. Moving to another interpreter changes the shared core's
pins, and with them the interpreter tag the wheel's name below carries.
"""
from __future__ import annotations

from release_core import PYTHON_TAG, RUNTIME_INPUTS, Input

PYWIN32 = Input(
    f"pywin32-312-cp{PYTHON_TAG}-cp{PYTHON_TAG}-win_amd64.whl",
    "https://files.pythonhosted.org/packages/21/82/3bf86d2e2808902013132e1ce905a7da0da53790f3836c64bf44d55e24f3/"
    f"pywin32-312-cp{PYTHON_TAG}-cp{PYTHON_TAG}-win_amd64.whl",
    "a4dd3a848290ef724347b19f301045831d8e802fa4464f491b98b1e0a081432e", 7_024_157,
    "https://pypi.org/pypi/pywin32/312/json",
    "pywin32: COM, Task Scheduler, shortcuts and WMI")
INNO_SETUP = Input(
    "innosetup-7.1.1-x64.exe",
    "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe",
    "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0", 14_336_568,
    "https://api.github.com/repos/jrsoftware/issrc/releases/tags/is-7_1_1",
    "Inno Setup 7.1.1, which compiles the setup program (its installer, signed by Pyrsys B.V.)")
INPUTS = (*RUNTIME_INPUTS, PYWIN32, INNO_SETUP)
"""Every input ``fetch_build_inputs.py`` obtains: the runtime's, then MirrorHold's own."""

ENGINE_README_SHA256 = "4aca294b8ddd7bd634a3619b7f997d9addc45a75770384b4cd32c41fef157f57"
"""The digest of ``README.txt`` in the pinned engine's release folder, which carries the
engine's licence; it was unpacked from the release file whose digest is pinned beside
``PINNED_VERSION``."""
