"""Build a release of MirrorHold from a commit: its portable zip, its setup program and
``SHA256SUMS``.

    python tools/build_release.py --label YEAR.MONTH.N [--commit REV] [--inputs DIR]
                                  [--out DIR] [--no-suite]

from any folder, with any Python 3.14 or later; the build itself needs the inputs
``fetch_build_inputs.py`` obtains, the engine ``fetch_engine.py`` obtains, git, and the
.NET Framework C# compiler that Windows 10 and 11 carry. It downloads nothing: every
input is checked against its pin before it is used.

The order of the build and every step that belongs to no one tool are
``release_core.py``'s, beside this file: the commit's tree read through ``git archive``
(the snapshot exporter's mechanism, so a build and a snapshot of one commit carry the
same files), the label, ``runtime/`` — CPython's embeddable package with Tk — the
bytecode bound to each source's hash, the refusal of a file naming the build folder or
the builder's home, the whole suite under the assembled interpreter judged against
``tests/TEST_LEDGER.md`` (``--no-suite`` skips it and says so), the zip with the first day
of the label's month as every file's time, and ``SHA256SUMS``. That file is shared byte
for byte with the maintainer's other published tools; this one holds what is MirrorHold's:

- what a release ships from the commit: the two packages, ``LICENSE`` and ``README.md``;
- the parts of pywin32 the program uses, in ``runtime/Lib/site-packages``;
- the pinned engine in ``vendor/``;
- the icon the commit's ``tools/make_icon.py`` draws;
- the third-party notices of pywin32 and the engine, after Python's;
- ``build.json``, the build record naming the label and the interpreter's version, and no
  commit (``mirrorhold.installation.release``);
- ``MirrorHold.exe``, compiled from the commit's ``tools/launcher/MirrorHold.cs``, the
  file a user double-clicks;
- how ``tests/TEST_LEDGER.md`` names the reds it records;
- the setup program, its one step after the zip, and Inno Setup's pinned installer, its
  one pinned input beyond pywin32.

It writes ``<out>/<label>/MirrorHold-<label>.zip``, the release's files at its top level.
The zip holds no folder of its own around the files, so the copy a package manager sets
up in the one folder it keeps for the program keeps working across updates
(``mirrorhold.locations.as_reached``).

Its one step after the zip compiles the setup program, ``MirrorHold-<label>-setup.exe``,
from the commit's ``tools/setup/MirrorHold.iss`` and the same assembled files the zip
holds, with Inno Setup unpacked from its pinned installer into a folder of the build's
own; the zip is written before it and is the zip a build without it writes. ``SHA256SUMS``
names both.

Two builds with one label of commits holding the same files make the same zip, byte for
byte, wherever the repository and the build lie, so a build of the published repository's
tag for a label is that release: the build record names no commit, the compiler's own
stamps in ``MirrorHold.exe`` are settled (``settle_stamps``), and the core reads the
commit's files as the commit holds them. A build that is to reproduce another runs under
the same CPython version — the zlib of the interpreter running the build compresses the
zip and the icon — and the same version of the .NET Framework's compiler. The setup
program is compiled from those settled files and the label alone, and two builds of one
commit with one label into two folders made the same setup program too.

Exit status: 0 built, 1 refused or failed, 2 usage.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, List, Optional, Sequence, Set, Tuple

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent
for _path in (TOOLS, PROJECT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from build_inputs import ENGINE_README_SHA256, INNO_SETUP, PYWIN32  # noqa: E402
from mirrorhold.engine.rclone import PINNED_EXECUTABLE_SHA256, PINNED_RELEASE, PINNED_VERSION  # noqa: E402
from mirrorhold.installation.release import Release, write_release  # noqa: E402
from mirrorhold.locations import VENDOR_DIR_NAME  # noqa: E402
from mirrorhold_gui.shipped_model import ICON_FILE_NAME, LICENCE_FILE_NAME, NOTICES_FILE_NAME  # noqa: E402
from release_core import (PYTHON_VERSION, SITE_PACKAGES, AfterZip, Assembly, BuildError,  # noqa: E402
                          BuildResult, Component, EMBEDDABLE, FULL_PYTHON, InputError, Suite, SuiteResult, Tool,
                          check_label, checked, extract_member, failed_tests, label_time, python_licences, sha256_of,
                          verdict)
import release_core  # noqa: E402

NAME = "MirrorHold"
PUBLISHER = "offcutworks"
LAUNCHER_NAME = "MirrorHold.exe"
LAUNCHER_SOURCE = "tools/launcher/MirrorHold.cs"
ICON_SCRIPT = "tools/make_icon.py"
SETUP_SCRIPT = "tools/setup/MirrorHold.iss"
SETUP_COMPILER = "ISCC.exe"
UNPACK_TIMEOUT = 300
COMPILE_TIMEOUT = 1800
LEDGER = "tests/TEST_LEDGER.md"
SUITE = "tests/run_all.py"
SHIPPED = ("mirrorhold", "mirrorhold_gui", LICENCE_FILE_NAME, "README.md")
PACKAGES = ("mirrorhold", "mirrorhold_gui")

PYWIN32_FILES = ("pythoncom.py", "pywin32.pth", "pywin32.version.txt")
PYWIN32_FOLDERS = ("pywin32_system32", "win32", "win32com", "win32comext")
PYWIN32_COMEXT = ("shell", "propsys")
PYWIN32_DROPPED = {"demos", "test", "tests", "include", "libs", "scripts"}
PYWIN32_LICENCES = ("win32/License.txt", "win32com/License.txt")
"""pywin32 as the program uses it: COM (``pythoncom``, ``win32com.client``), the shell's
shortcut and property interfaces (``win32com.shell``, ``win32com.propsys``) and the
modules beneath them. Left out: Pythonwin and its MFC and Scintilla libraries, the
ADO, ISAPI and other COM extensions, demos, tests, headers and link libraries."""

NOTICES_INTRO = ("This release of MirrorHold carries the following software, each under its own",
                 "licence, reproduced below. MirrorHold itself is released under the MIT License",
                 "in LICENSE.")

CSC = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows",
           "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")
_LEDGER_ENTRY = re.compile(r"^### `([^`]+)`\s*$")
_SUMMARY = re.compile(r"^(\d+) tests, (\d+) failed, (\d+) skipped$")


# --- assembling ------------------------------------------------------------------------

def pywin32_kept(name: str) -> bool:
    """Whether the release keeps the wheel member ``name``."""
    parts = PurePosixPath(name).parts
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in PYWIN32_FILES
    if parts[0] not in PYWIN32_FOLDERS or any(part.lower() in PYWIN32_DROPPED for part in parts[1:]):
        return False
    return not (parts[0] == "win32comext" and len(parts) > 2 and parts[1] not in PYWIN32_COMEXT)


def assemble_pywin32(runtime: Path, wheel: Path) -> None:
    """The parts of pywin32 the program uses, from its wheel, in the runtime's ``site-packages``."""
    site = runtime / SITE_PACKAGES
    with zipfile.ZipFile(wheel) as bundle:
        for name in sorted(n for n in bundle.namelist() if not n.endswith("/") and pywin32_kept(n)):
            extract_member(bundle, name, site / name)


def copy_engine(engine_dir: Path, target: Path) -> Tuple[Path, Path]:
    """The pinned engine's executable copied to ``target``; returns it and the release
    folder's ``README.txt``, which carries the engine's licence, both checked."""
    executable = checked(engine_dir / "rclone.exe", PINNED_EXECUTABLE_SHA256)
    readme = checked(engine_dir / "README.txt", ENGINE_README_SHA256)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(executable, target / "rclone.exe")
    if sha256_of(target / "rclone.exe") != PINNED_EXECUTABLE_SHA256:
        raise BuildError("the engine copied into the release differs from the pinned one")
    return target / "rclone.exe", readme


def compile_launcher(source: Path, icon: Path, target: Path, label: str, copyright_text: str, *,
                     compiler: Path = CSC) -> Path:
    """``MirrorHold.exe`` compiled from ``source`` with the icon, its file properties
    naming the program, its publisher and the release, and its compiler's stamps settled
    (``settle_stamps``)."""
    if not compiler.is_file():
        raise BuildError(f"the C# compiler of the .NET Framework is not at {compiler}")
    with tempfile.TemporaryDirectory(prefix="mirrorhold-launcher-") as scratch:
        attributes = Path(scratch) / "Release.cs"
        attributes.write_text(assembly_attributes(label, copyright_text), encoding="utf-8")
        done = subprocess.run([str(compiler), "-nologo", "-target:winexe", "-optimize+", "-platform:anycpu",
                               f"-win32icon:{icon}", f"-out:{target}", "-r:System.Windows.Forms.dll",
                               str(source), str(attributes)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    if done.returncode != 0 or not target.is_file():
        raise BuildError(f"the launcher did not compile: {(done.stdout + done.stderr).strip()[-2000:]}")
    return settle_stamps(target, label)


_BLANK_GUID = "{00000000-0000-0000-0000-000000000000}".encode("ascii")


def _braced(guid: uuid.UUID) -> bytes:
    return f"{{{guid}}}".upper().encode("ascii")


def settle_stamps(path: Path, label: str) -> Path:
    """The compiled launcher at ``path`` made a function of its source and the label alone.

    The .NET Framework's compiler has no deterministic switch, and stamps every build with
    three things of its own: the PE header's time; the module's identifier, its MVID, the one
    GUID of the metadata's GUID heap; and the name of the ``<PrivateImplementationDetails>``
    type it generates for static data, which spells that identifier in braces (compiler
    4.8.9032.0, measured on the development machine, 2026-11-02). The time becomes the
    label's, the first day of its month at midnight UTC, as the zip's files are dated; the
    identifier becomes a GUID drawn from the SHA-256 of the file with all three blanked. A
    debug directory, a signature — Authenticode's or a strong name — or a checksum would
    carry a further stamp or a digest of the stamped bytes, so a file holding any of them, or
    not laid out as that compiler writes it, raises BuildError."""
    data = bytearray(path.read_bytes())
    malformed = BuildError(f"{path.name} is not laid out as the .NET Framework's compiler writes it")
    try:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise malformed
        section_count = struct.unpack_from("<H", data, pe + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        directories = optional + {0x10B: 96, 0x20B: 112}[struct.unpack_from("<H", data, optional)[0]]
        sections = [struct.unpack_from("<IIII", data, optional + optional_size + 40 * i + 8)
                    for i in range(section_count)]

        def directory(index: int) -> Tuple[int, int]:
            return struct.unpack_from("<II", data, directories + 8 * index)

        def offset(rva: int) -> int:
            for size, address, raw_size, raw in sections:
                if address <= rva < address + max(size, raw_size):
                    return raw + rva - address
            raise malformed

        cli = offset(directory(14)[0])
        if (directory(4) != (0, 0) or directory(6) != (0, 0) or struct.unpack_from("<II", data, cli + 32) != (0, 0)
                or struct.unpack_from("<I", data, optional + 64)[0] != 0):
            raise BuildError(f"{path.name} carries a signature, a debug directory or a checksum, which settling its "
                             "stamps would leave wrong")
        metadata = offset(struct.unpack_from("<I", data, cli + 8)[0])
        if data[metadata:metadata + 4] != b"BSJB":
            raise malformed
        at = metadata + 16 + struct.unpack_from("<I", data, metadata + 12)[0]   # past the version string
        stream_count = struct.unpack_from("<H", data, at + 2)[0]
        at += 4
        heaps = {}
        for _ in range(stream_count):
            start, size = struct.unpack_from("<II", data, at)
            end = data.index(b"\0", at + 8)
            heaps[bytes(data[at + 8:end]).decode("ascii")] = (metadata + start, size)
            at += 8 + ((end - at - 8 + 1 + 3) & ~3)
        guids, guids_size = heaps["#GUID"]
    except (struct.error, KeyError, ValueError, UnicodeDecodeError):
        raise malformed from None
    if guids_size != 16:
        raise malformed
    stamped = _braced(uuid.UUID(bytes_le=bytes(data[guids:guids + 16])))
    names = []
    found = data.find(stamped)
    while found >= 0:
        names.append(found)
        found = data.find(stamped, found + len(stamped))

    def write(stamp: int, identifier: bytes, braced: bytes) -> None:
        struct.pack_into("<I", data, pe + 8, stamp)
        data[guids:guids + 16] = identifier
        for name in names:
            data[name:name + len(braced)] = braced

    write(0, bytes(16), _BLANK_GUID)
    settled = uuid.UUID(bytes=hashlib.sha256(data).digest()[:16], version=4)
    write(calendar.timegm(label_time(label)), settled.bytes_le, _braced(settled))
    path.write_bytes(data)
    return path


def assembly_attributes(label: str, copyright_text: str) -> str:
    quoted = copyright_text.replace("\\", "\\\\").replace('"', '\\"')
    return ("using System.Reflection;\n"
            '[assembly: AssemblyTitle("MirrorHold")]\n'
            '[assembly: AssemblyProduct("MirrorHold")]\n'
            f'[assembly: AssemblyCompany("{PUBLISHER}")]\n'
            f'[assembly: AssemblyCopyright("{quoted}")]\n'
            '[assembly: AssemblyVersion("1.0.0.0")]\n'
            f'[assembly: AssemblyFileVersion("{check_label(label)}.0")]\n'
            f'[assembly: AssemblyInformationalVersion("{label}")]\n')


def copyright_of(licence: str) -> str:
    for line in licence.splitlines():
        if line.strip().lower().startswith("copyright"):
            return line.strip()
    raise BuildError(f"{LICENCE_FILE_NAME} carries no copyright line")


# --- the third-party notices -----------------------------------------------------------

def licence_section(readme: str) -> str:
    """The licence section of rclone's ``README.txt``: from its ``License`` heading to the
    heading after it."""
    lines = readme.replace("\r\n", "\n").split("\n")
    try:
        start = lines.index("License")
        end = lines.index("Authors and contributors", start)
    except ValueError:
        raise BuildError("the engine's README.txt holds no licence section") from None
    return "\n".join(lines[start + 1:end]).strip("\n")


def notices_text(label: str, *, python_licence: str, incorporated: str, pywin32_licences: Sequence[str],
                 engine_licence: str) -> str:
    """The third-party notices a release ships: each component it carries, and its licence."""
    return release_core.notices_text(
        f"Third-party notices for MirrorHold {label}", NOTICES_INTRO,
        python_licence=python_licence, incorporated=incorporated,
        components=(Component("pywin32 312", "runtime\\Lib\\site-packages\\", tuple(pywin32_licences)),
                    Component(f"rclone {PINNED_VERSION}", f"vendor\\{PINNED_RELEASE}\\", (engine_licence,))))


def write_notices(target: Path, label: str, embeddable: Path, full: Path, wheel: Path, engine_readme: Path) -> Path:
    python_licence, incorporated = python_licences(embeddable, full)
    with zipfile.ZipFile(wheel) as bundle:
        pywin32 = [bundle.read(name).decode("utf-8") for name in PYWIN32_LICENCES]
    engine = licence_section(engine_readme.read_text(encoding="utf-8"))
    target.write_text(notices_text(label, python_licence=python_licence, incorporated=incorporated,
                                   pywin32_licences=pywin32, engine_licence=engine), encoding="utf-8", newline="\n")
    return target


# --- the setup program -----------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]


def setup_name(label: str) -> str:
    return f"{NAME}-{check_label(label)}-setup.exe"


def _finished(run: Runner, command: Sequence[str], timeout: int, what: str) -> subprocess.CompletedProcess:
    """``command`` run to its end with its output kept, or BuildError when it outlasts
    ``timeout``: a program waiting on a question nobody can answer must refuse the build,
    not hang it."""
    try:
        return run(list(command), capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BuildError(f"{what} did not end within {timeout} s") from None


def _tail(done: subprocess.CompletedProcess) -> str:
    return ((done.stdout or "") + (done.stderr or "")).strip()[-2000:]


def unpack_compiler(installer: Path, folder: Path, *, run: Runner = subprocess.run) -> Path:
    """Inno Setup's compiler, unpacked from its pinned ``installer`` into ``folder`` by the
    installer's portable mode, for this account and asking nothing: it registers no
    installed app, no shortcut and no file association, and writes nothing outside
    ``folder`` (measured on the development machine, 2026-11-01)."""
    done = _finished(run, [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                           "/PORTABLE=1", f"/DIR={folder}"], UNPACK_TIMEOUT, "unpacking Inno Setup")
    compiler = folder / SETUP_COMPILER
    if done.returncode != 0 or not compiler.is_file():
        raise BuildError(f"Inno Setup could not be unpacked from {installer.name} (exit status {done.returncode})"
                         + (f": {_tail(done)}" if _tail(done) else ""))
    return compiler


def setup_defines(label: str, release: Path, copyright_text: str) -> List[str]:
    """What the setup script is told: the assembled files, the label, the date every file
    carries — the first day of the label's month, the zip's date, which the script stamps at
    midnight UTC — and the copyright line."""
    year, month, day = label_time(label)[:3]
    return [f"/DRelease={release}", f"/DLabel={label}", f"/DTouchDate={year:04d}-{month:02d}-{day:02d}",
            f"/DCopyright={copyright_text}"]


def compile_setup(compiler: Path, script: Path, label: str, release: Path, destination: Path, copyright_text: str,
                  *, run: Runner = subprocess.run) -> Path:
    """``MirrorHold-<label>-setup.exe`` in ``destination``, compiled from ``script`` over the
    assembled files in ``release``."""
    target = destination / setup_name(label)
    done = _finished(run, [str(compiler), "/Q", f"/O{destination}", f"/F{target.stem}",
                           *setup_defines(label, release, copyright_text), str(script)], COMPILE_TIMEOUT,
                     "compiling the setup program")
    if done.returncode != 0 or not target.is_file():
        raise BuildError(f"the setup program did not compile: {_tail(done)}")
    return target


def make_setup(a: AfterZip, *, run: Runner = subprocess.run) -> List[Path]:
    """The step after the zip: the setup program, compiled from the commit's setup script and
    the same assembled files the zip holds, by a compiler unpacked for this build alone and
    deleted with it. It writes nothing into the release's files, and the zip is the one the
    build wrote before it, which is checked rather than trusted."""
    zipped = sha256_of(a.zip_path)
    copyright_text = copyright_of((a.release / LICENCE_FILE_NAME).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="mirrorhold-setup-") as scratch:
        compiler = unpack_compiler(a.inputs[INNO_SETUP.name], Path(scratch) / "inno-setup", run=run)
        setup = compile_setup(compiler, a.source / SETUP_SCRIPT, a.label, a.release, a.destination, copyright_text,
                              run=run)
    if sha256_of(a.zip_path) != zipped:
        raise BuildError("the zip changed while the setup program was made")
    return [setup]


# --- the suite -------------------------------------------------------------------------

def ledger_tests(ledger: str) -> Set[str]:
    """The tests ``TEST_LEDGER.md`` records as red: one entry heading per test."""
    return {m.group(1) for line in ledger.splitlines() if (m := _LEDGER_ENTRY.match(line))}


def judge_suite(output: str, ledger: str) -> SuiteResult:
    """A ``run_all.py`` transcript against ``TEST_LEDGER.md``: its summary line, and the tests
    unittest reports failed or in error, against those the ledger records."""
    summary = next((line.strip() for line in output.splitlines() if _SUMMARY.match(line.strip())), "")
    if not summary:
        raise BuildError("the suite ended without its summary line")
    return verdict(summary, failed_tests(output), ledger_tests(ledger))


# --- the build -------------------------------------------------------------------------

def mirrorhold(engine_dir: Path) -> Tool:
    """MirrorHold's part of a release build, the pinned engine taken from ``engine_dir``."""

    def check() -> None:
        checked(engine_dir / "rclone.exe", PINNED_EXECUTABLE_SHA256)
        checked(engine_dir / "README.txt", ENGINE_README_SHA256)
        if not CSC.is_file():
            raise BuildError(f"the C# compiler of the .NET Framework is not at {CSC}")

    def assemble(a: Assembly) -> None:
        assemble_pywin32(a.runtime, a.inputs[PYWIN32.name])
        _, engine_readme = copy_engine(engine_dir, a.release / VENDOR_DIR_NAME / PINNED_RELEASE)
        icon = a.release / ICON_FILE_NAME
        drawn = subprocess.run([sys.executable, str(a.source / ICON_SCRIPT), str(icon)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
        if drawn.returncode != 0 or not icon.is_file():
            raise BuildError(f"the icon was not drawn: {(drawn.stdout + drawn.stderr).strip()[-2000:]}")
        write_notices(a.release / NOTICES_FILE_NAME, a.label, a.inputs[EMBEDDABLE.name], a.inputs[FULL_PYTHON.name],
                      a.inputs[PYWIN32.name], engine_readme)
        write_release(Release(a.release, a.label, PYTHON_VERSION))
        licence = (a.release / LICENCE_FILE_NAME).read_text(encoding="utf-8")
        compile_launcher(a.source / LAUNCHER_SOURCE, icon, a.release / LAUNCHER_NAME, a.label, copyright_of(licence))

    def prepare(source: Path) -> None:
        shutil.copytree(engine_dir, source / VENDOR_DIR_NAME / PINNED_RELEASE)

    return Tool(NAME, SHIPPED, Suite(SUITE, LEDGER, judge_suite, prepare, skipped="the suite was not run (--no-suite)"),
                required=(LAUNCHER_SOURCE, ICON_SCRIPT, SETUP_SCRIPT), inputs=(PYWIN32, INNO_SETUP), check=check,
                assembly=(assemble,), bytecode=PACKAGES, after_zip=(make_setup,))


def build(project: Path, *, label: str, commit: str = "HEAD", inputs: Optional[Path] = None,
          out: Optional[Path] = None, engine_dir: Optional[Path] = None, suite: bool = True,
          say: Callable[[str], None] = lambda _: None) -> BuildResult:
    """Build the release ``label`` of ``commit``. Raises BuildError (or InputError)."""
    project = Path(project).resolve()
    engine_dir = Path(engine_dir) if engine_dir else project / VENDOR_DIR_NAME / PINNED_RELEASE
    return release_core.build(project, mirrorhold(engine_dir), label=label, commit=commit, inputs=inputs, out=out,
                              suite=suite, say=say)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a release of MirrorHold from a commit: its zip and its setup "
                                                 "program.")
    parser.add_argument("--label", required=True, help="the release label, YEAR.MONTH.N")
    parser.add_argument("--commit", default="HEAD", help="the commit to build (default HEAD)")
    parser.add_argument("--inputs", type=Path, default=None,
                        help=f"the folder holding the pinned inputs (default: {release_core.INPUTS_DIR_NAME} "
                             "beside tools/)")
    parser.add_argument("--out", type=Path, default=None, help="where to write <label>/ (default: dist beside tools/)")
    parser.add_argument("--no-suite", action="store_true", help="skip the suite; the build is then not verified")
    args = parser.parse_args(argv)
    try:
        build(PROJECT, label=args.label, commit=args.commit, inputs=args.inputs, out=args.out,
              suite=not args.no_suite, say=lambda line: print(line, flush=True))
    except (BuildError, InputError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
