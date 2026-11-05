"""The release tooling (``tools/``): the icon, the pinned inputs and their fetching, and
the build's parts, without the network and without the inputs.

The build's assembly is exercised whole only by building a release, which needs the
inputs, the engine and the .NET Framework compiler: here each of its parts is bound on
its own — the label, what of pywin32 a release keeps, the notices, the suite's verdict
against the ledger, the zip and its sums, the setup program's step after the zip with a
stand-in for its compiler, and the setup script's identity and the commands it asks the
program — and a commit's tree is read from a git repository made in a temporary folder."""
import calendar
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from support import PROJECT, TempDirTest

TOOLS = PROJECT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import build_inputs  # noqa: E402
import build_release  # noqa: E402
import fetch_build_inputs  # noqa: E402
import release_core  # noqa: E402
from mirrorhold import icon  # noqa: E402

from mirrorhold_gui.shipped_model import ICON_FILE_NAME, NOTICES_FILE_NAME  # noqa: E402


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class IconTest(unittest.TestCase):
    def entries(self, data: bytes):
        reserved, kind, count = struct.unpack_from("<HHH", data, 0)
        self.assertEqual((reserved, kind), (0, 1))
        found = {}
        for i in range(count):
            side, _, _, _, planes, bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
            found[side or 256] = data[offset:offset + length]
            self.assertEqual((planes, bits), (1, 32))
            self.assertLessEqual(offset + length, len(data))
        return found

    def test_the_icon_holds_every_size_bitmaps_below_and_png_for_the_largest(self):
        sizes = (16, 32, 256)
        entries = self.entries(icon.icon({s: icon.render(s) for s in sizes}))
        self.assertEqual(sorted(entries), list(sizes))
        self.assertTrue(entries[256].startswith(b"\x89PNG\r\n\x1a\n"))
        for size in (16, 32):
            header, width, height = struct.unpack_from("<Iii", entries[size], 0)
            self.assertEqual((header, width, height), (40, size, 2 * size))
        self.assertEqual(icon.SIZES[0], 16)
        self.assertEqual(icon.SIZES[-1], 256)

    def test_small_sizes_sit_on_the_pixel_grid_with_the_two_blocks_mirrored(self):
        for size in (16, 20, 24, 32, 40, 48):
            with self.subTest(size=size):
                tile, solid, outer, inner = icon.geometry(size)
                for box in (tile, solid, outer, inner):
                    self.assertTrue(all(float(v).is_integer() for v in box[:4]), box)
                self.assertEqual((solid[0] + outer[2], solid[2] + outer[0]), (size, size))
                self.assertGreaterEqual(inner[0] - outer[0], 1)

    def test_the_smallest_size_shows_a_solid_block_and_an_outline(self):
        pixels = icon.render(16)
        at = lambda x, y: pixels[y * 16 + x]  # noqa: E731
        white, teal = icon.WHITE + (255,), icon.TEAL + (255,)
        self.assertEqual(at(5, 8), white)          # inside the solid block
        self.assertEqual(at(9, 8), white)          # the outline's left edge
        self.assertEqual(at(11, 8), teal)          # inside the outline
        self.assertEqual(at(8, 8), teal)           # between the blocks
        self.assertEqual(at(0, 0), (0, 0, 0, 0))   # outside the tile

    def test_it_is_drawn_the_same_every_time(self):
        first = icon.icon({16: icon.render(16), 48: icon.render(48)})
        self.assertEqual(first, icon.icon({16: icon.render(16), 48: icon.render(48)}))


class PinsTest(TempDirTest):
    def test_each_input_is_pinned_by_a_digest_and_named_by_its_address(self):
        for item in build_inputs.INPUTS:
            with self.subTest(item=item.name):
                self.assertRegex(item.sha256, r"^[0-9a-f]{64}$")
                self.assertTrue(item.url.endswith("/" + item.name))
                self.assertGreater(item.size, 0)
        self.assertRegex(build_inputs.ENGINE_README_SHA256, r"^[0-9a-f]{64}$")

    def test_a_file_is_accepted_only_with_its_pinned_digest(self):
        path = self.tmp / "input.zip"
        path.write_bytes(b"content")
        self.assertEqual(release_core.checked(path, sha256(b"content")), path)
        with self.assertRaises(release_core.InputError):
            release_core.checked(path, "0" * 64)
        with self.assertRaises(release_core.InputError):
            release_core.checked(self.tmp / "missing.zip", sha256(b"content"))


class FetchInputsTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "build-inputs"
        self.served = b"an input"
        self.item = build_inputs.Input("input.zip", "https://example.invalid/input.zip", sha256(self.served),
                                       len(self.served), "https://example.invalid/listing", "a test input")
        self.requests = []

    def opener(self, request, timeout):
        self.requests.append((request.full_url, request.get_header("User-agent")))
        return io.BytesIO(self.served)

    def test_it_downloads_once_and_leaves_an_input_in_place_alone(self):
        path, present = fetch_build_inputs.obtain(self.item, self.folder, opener=self.opener)
        self.assertEqual((path.read_bytes(), present), (self.served, False))
        self.assertEqual(self.requests, [("https://example.invalid/input.zip", fetch_build_inputs.USER_AGENT)])
        path, present = fetch_build_inputs.obtain(self.item, self.folder, opener=self.opener)
        self.assertTrue(present)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([p.name for p in self.folder.iterdir()], ["input.zip"])

    def test_a_copy_in_the_given_folder_is_used_without_a_download(self):
        source = self.tmp / "downloads"
        source.mkdir()
        (source / "input.zip").write_bytes(self.served)
        path, present = fetch_build_inputs.obtain(self.item, self.folder, source=source, opener=self.opener)
        self.assertEqual((path.read_bytes(), present, self.requests), (self.served, False, []))

    def test_another_content_is_refused_and_nothing_is_left(self):
        self.served = b"something else"
        with self.assertRaises(release_core.InputError):
            fetch_build_inputs.obtain(self.item, self.folder, opener=self.opener)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_an_input_in_place_with_another_content_is_refused(self):
        self.folder.mkdir()
        (self.folder / "input.zip").write_bytes(b"tampered")
        with self.assertRaises(release_core.InputError):
            fetch_build_inputs.obtain(self.item, self.folder, opener=self.opener)
        self.assertEqual(self.requests, [])


class LabelTest(unittest.TestCase):
    def test_a_release_label_is_year_month_and_count(self):
        for label in ("2026.10.1", "2026.1.12", "2031.12.3"):
            self.assertEqual(build_release.check_label(label), label)
        for label in ("2026.13.1", "2026.10.0", "26.10.1", "2026.10", "2026.010.1", "1.0.0-2026"):
            with self.subTest(label=label), self.assertRaises(build_release.BuildError):
                build_release.check_label(label)

    def test_every_file_of_a_release_carries_the_first_day_of_its_month(self):
        self.assertEqual(release_core.label_time("2026.10.3"), (2026, 10, 1, 0, 0, 0))

    def test_the_launcher_names_the_program_its_publisher_and_its_release(self):
        text = build_release.assembly_attributes("2026.10.2", 'Copyright (c) 2026 "someone"')
        self.assertIn('AssemblyFileVersion("2026.10.2.0")', text)
        self.assertIn('AssemblyInformationalVersion("2026.10.2")', text)
        self.assertIn('AssemblyCompany("offcutworks")', text)
        self.assertIn('AssemblyVersion("1.0.0.0")', text)
        self.assertIn('AssemblyCopyright("Copyright (c) 2026 \\"someone\\"")', text)


class Pywin32SelectionTest(unittest.TestCase):
    def test_what_the_program_uses_is_kept(self):
        for name in ("pywin32.pth", "pythoncom.py", "pywin32.version.txt", "pywin32_system32/pythoncom314.dll",
                     "win32/win32api.pyd", "win32/lib/pywin32_bootstrap.py", "win32com/client/__init__.py",
                     "win32comext/__init__.py", "win32comext/shell/shell.pyd", "win32comext/propsys/propsys.pyd"):
            with self.subTest(name=name):
                self.assertTrue(build_release.pywin32_kept(name))

    def test_pythonwin_the_other_extensions_demos_tests_headers_and_libraries_are_left_out(self):
        for name in ("pythonwin/win32ui.pyd", "pythonwin/Scintilla.dll", "adodbapi/adodbapi.py", "isapi/install.py",
                     "PyWin32.chm", "pywin32-312.dist-info/RECORD", "pywin32-312.data/scripts/pywin32_postinstall.py",
                     "win32/Demos/eventLogDemo.py", "win32com/test/testall.py", "win32comext/mapi/mapi.pyd",
                     "win32comext/shell/demos/explorer_browser.py", "win32/include/PyWinTypes.h",
                     "win32/libs/pywintypes.lib", "win32/scripts/regsetup.py", "win32comext/shell/test/testShell.py"):
            with self.subTest(name=name):
                self.assertFalse(build_release.pywin32_kept(name))


PAGE = """<html><body>
<section id="other"><h2>Other<a class="headerlink" href="#other">&#182;</a></h2><p>not wanted</p></section>
<section id="wanted">
<h2>Incorporated   software<a class="headerlink" href="#wanted">&#182;</a></h2>
<p>An introduction that runs on
across lines.</p>
<section id="part"><h3>A part<a class="headerlink" href="#part">&#182;</a></h3>
<div class="highlight"><pre><span></span>Copyright (C) 2001 Someone
  kept    as it is
</pre></div>
</section>
</section>
<section id="after"><p>also not wanted</p></section>
</body></html>"""


class NoticesTest(unittest.TestCase):
    def test_a_page_section_becomes_text_with_its_subsections_and_nothing_else(self):
        text = release_core.page_section_text(PAGE, "wanted")
        self.assertEqual(text, "Incorporated software\n=====================\n\n"
                               "An introduction that runs on across lines.\n\n"
                               "A part\n------\n\n"
                               "Copyright (C) 2001 Someone\n  kept    as it is")
        with self.assertRaises(build_release.BuildError):
            release_core.page_section_text(PAGE, "absent")

    def test_the_engine_licence_is_read_from_its_readme(self):
        readme = ("Intro\r\n\r\nLicense\r\n\r\nThis is free software.\r\n\r\n    Copyright X\r\n\r\n"
                  "Authors and contributors\r\n")
        self.assertEqual(build_release.licence_section(readme), "This is free software.\n\n    Copyright X")
        with self.assertRaises(build_release.BuildError):
            build_release.licence_section("no such heading\n")

    def test_the_notices_name_each_component_and_carry_each_licence_once(self):
        text = build_release.notices_text("2026.10.1", python_licence="PSF LICENSE\r\n", incorporated="EXPAT TEXT",
                                          pywin32_licences=["HAMMOND\n", "COM LICENCE\n", "COM LICENCE"],
                                          engine_licence="RCLONE MIT")
        self.assertTrue(text.startswith("Third-party notices for MirrorHold 2026.10.1\n"))
        for part in ("PSF LICENSE", "EXPAT TEXT", "HAMMOND", "RCLONE MIT", "- pywin32 312, in runtime",
                     f"- Python {release_core.PYTHON_VERSION}, with Tcl/Tk, in runtime"):
            self.assertIn(part, text)
        self.assertEqual(text.count("COM LICENCE"), 1)
        self.assertNotIn("\r", text)


LEDGER = """# Test Debt Ledger

### `test_a.ATest.test_one`

- **tests:** test `test_a.ATest.test_one`

### `test_b.BTest.test_two`
"""


class SuiteVerdictTest(unittest.TestCase):
    def test_the_ledger_names_one_test_per_entry(self):
        self.assertEqual(build_release.ledger_tests(LEDGER), {"test_a.ATest.test_one", "test_b.BTest.test_two"})

    def test_failures_are_read_in_both_unittest_spellings(self):
        output = ("FAIL: test_one (test_a.ATest)\n"
                  "ERROR: test_two (test_b.BTest.test_two)\n"
                  "ERROR: test_x (unittest.loader._FailedTest.test_x)\n"
                  "ok\n")
        self.assertEqual(build_release.failed_tests(output),
                         {"test_a.ATest.test_one", "test_b.BTest.test_two", "unittest.loader._FailedTest.test_x"})

    def test_a_build_needs_exactly_the_ledgers_failures(self):
        summary = "\n10 tests, 2 failed, 0 skipped\n"
        same = build_release.judge_suite("FAIL: test_one (test_a.ATest.test_one)\n"
                                         "FAIL: test_two (test_b.BTest.test_two)\n" + summary, LEDGER)
        self.assertTrue(same.matches_ledger)
        self.assertEqual(same.summary, "10 tests, 2 failed, 0 skipped")
        other = build_release.judge_suite("FAIL: test_one (test_a.ATest.test_one)\n"
                                          "FAIL: test_new (test_c.CTest.test_new)\n" + summary, LEDGER)
        self.assertFalse(other.matches_ledger)
        self.assertEqual((other.unexpected, other.recovered), (("test_c.CTest.test_new",), ("test_b.BTest.test_two",)))
        with self.assertRaises(build_release.BuildError):
            build_release.judge_suite("FAIL: test_one (test_a.ATest.test_one)\n", LEDGER)


class ZipTest(TempDirTest):
    def folder(self) -> Path:
        folder = self.tmp / "release"
        for name, data in (("b.txt", b"b"), ("a/inner.bin", bytes(range(256))), ("a/z.txt", b"z" * 1000)):
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
        return folder

    def test_one_folder_makes_one_zip_whatever_its_files_times(self):
        folder = self.folder()
        first = self.tmp / "first.zip"
        self.assertEqual(release_core.write_zip(folder, first, (2026, 10, 1, 0, 0, 0)), 3)
        os.utime(folder / "b.txt", (1_000_000_000, 1_000_000_000))
        second = self.tmp / "second.zip"
        release_core.write_zip(folder, second, (2026, 10, 1, 0, 0, 0))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as bundle:
            infos = bundle.infolist()
            self.assertEqual([i.filename for i in infos], ["a/inner.bin",
                                                           "a/z.txt",
                                                           "b.txt"])
            self.assertEqual({i.date_time for i in infos}, {(2026, 10, 1, 0, 0, 0)})
            self.assertEqual(bundle.read("a/inner.bin"), bytes(range(256)))

    def test_the_files_sit_at_the_zips_top_level(self):
        target = self.tmp / "release.zip"
        self.assertEqual(release_core.write_zip(self.folder(), target, (2026, 10, 1, 0, 0, 0)), 3)
        with zipfile.ZipFile(target) as bundle:
            self.assertEqual(bundle.namelist(), ["a/inner.bin", "a/z.txt", "b.txt"])

    def test_a_file_naming_the_build_folder_is_found_in_any_spelling(self):
        folder = self.folder()
        built = r"C:\Builds\Some Account\mirrorhold-build-x"
        (folder / "a" / "code.pyc").write_bytes(b"\x00co_filename " + built.upper().encode("utf-8") + b"\\m.py\x00")
        (folder / "wide.exe").write_bytes(b"MZ" + built.replace("\\", "/").encode("utf-16-le"))
        self.assertEqual(release_core.files_naming(folder, [built]), ["a/code.pyc", "wide.exe"])
        self.assertEqual(release_core.files_naming(folder, [r"C:\Elsewhere"]), [])

    def test_the_sums_name_each_file_with_its_digest(self):
        asset = self.tmp / "MirrorHold-2026.10.1.zip"
        asset.write_bytes(b"zip bytes")
        sums = release_core.write_sums(self.tmp / "SHA256SUMS", [asset])
        self.assertEqual(sums.read_bytes(), f"{sha256(b'zip bytes')}  MirrorHold-2026.10.1.zip\n".encode())


class CommitTreeTest(TempDirTest):
    """A build reads the commit, never the working tree: a repository made here holds the
    project in a sub-folder, an export-ignored folder, and a change made after the commit."""

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="suite", GIT_AUTHOR_EMAIL="", GIT_COMMITTER_NAME="suite",
                   GIT_COMMITTER_EMAIL="")
        done = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                              cwd=str(self.tmp), capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_commits_tree_is_read_without_its_export_ignored_paths_or_later_changes(self):
        project = self.tmp / "project"
        for name, text in (("tool.py", "committed\n"), ("private/secret.py", "private\n"),
                           (".gitattributes", "/private export-ignore\n/.gitattributes export-ignore\n")):
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_text(text, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "one")
        (project / "tool.py").write_text("changed after the commit\n", encoding="utf-8")
        (project / "untracked.py").write_text("never committed\n", encoding="utf-8")
        commit, tree = release_core.resolve_commit(project, "HEAD")
        self.assertEqual(commit, self.git("rev-parse", "HEAD"))
        target = self.tmp / "archive"
        self.assertEqual(release_core.archive(project, tree, target), ["tool.py"])
        self.assertEqual((target / "tool.py").read_text(encoding="utf-8"), "committed\n")
        with self.assertRaises(build_release.BuildError):
            release_core.resolve_commit(project, "no-such-revision")


STAND_IN = r"""
using System;
using System.IO;

// Stands in for a release's pythonw.exe: writes down the path it was started by, the folder it
// was started in and its command line, then ends.
static class StandIn
{
    static int Main()
    {
        string record = Environment.GetEnvironmentVariable("MIRRORHOLD_LAUNCHER_RECORD");
        File.WriteAllLines(record + ".writing", new[] {
            Environment.GetCommandLineArgs()[0], Environment.CurrentDirectory, Environment.CommandLine });
        File.Move(record + ".writing", record);
        return 0;
    }
}
"""

STAND_IN_PACKAGE = r'''import os
import sys

record = os.environ["MIRRORHOLD_LAUNCHER_RECORD"]
with open(record + ".writing", "w", encoding="utf-8") as handle:
    handle.write("\n".join([sys.executable, os.getcwd(), " ".join(sys.orig_argv)]))
os.replace(record + ".writing", record)
'''
"""Stands in for the window's package beside a native interpreter: writes down what the
.NET stand-in above writes."""

LONGEST_FOLDER = 209
"""The longest path of a folder a release works from as it is named."""

TOO_LONG = ("MirrorHold cannot start from this folder: its path is {length} characters long, and on this drive "
            "Windows can start MirrorHold only from a folder whose path is at most 209 characters long. Move the "
            "folder to a shorter path, for example C:\\MirrorHold, and start MirrorHold there.")


def extended(path) -> str:
    """``path`` as Windows takes it past 260 characters."""
    return "\\\\?\\" + str(path)


def short_name(path) -> str:
    """Windows' 8.3 name for ``path``, or the path as it is where its drive keeps none."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    found = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetShortPathNameW(extended(path), found, len(found))
    name = found.value if 0 < length < len(found) else extended(path)
    return name[4:] if name.startswith("\\\\?\\") else name


@unittest.skipUnless(os.name == "nt" and build_release.CSC.is_file(), "the launcher is built by the .NET Framework's "
                                                                      "C# compiler, on Windows")
class LauncherTest(TempDirTest):
    """The launcher compiled from ``tools/launcher/MirrorHold.cs`` into a release folder whose
    interpreter is a stand-in that writes down how it was started: directly, through a
    package manager's junction to the folder, and through a symbolic link to the file — to a
    release in a folder whose path is too long to start it from as it is named, by the
    folder's 8.3 name, or, where no name of it is short enough, not at all, saying why."""

    def setUp(self):
        super().setUp()
        self.release = self.tmp / "apps" / "MirrorHold 2026.10.1"
        (self.release / "runtime").mkdir(parents=True)
        drawn = self.tmp / ICON_FILE_NAME
        drawn.write_bytes(icon.icon({16: icon.render(16)}))
        build_release.compile_launcher(PROJECT / build_release.LAUNCHER_SOURCE, drawn,
                                       self.release / build_release.LAUNCHER_NAME, "2026.10.1",
                                       "Copyright (c) 2026 offcutworks")
        source = self.tmp / "StandIn.cs"
        source.write_text(STAND_IN, encoding="utf-8")
        done = subprocess.run([str(build_release.CSC), "-nologo", "-target:winexe",
                               f"-out:{self.release / 'runtime' / 'pythonw.exe'}", str(source)],
                              capture_output=True, text=True, errors="replace")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.record = self.tmp / "started.txt"

    def start(self, launcher: Path, *args: str):
        """What the stand-in wrote down once ``launcher`` started it: its path, its folder, its
        command line."""
        env = dict(os.environ, MIRRORHOLD_LAUNCHER_RECORD=str(self.record))
        done = subprocess.run([str(launcher), *args], cwd=str(self.tmp), env=env, timeout=60)
        self.assertEqual(done.returncode, 0)
        deadline = time.monotonic() + 30
        while not self.record.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        lines = self.record.read_text(encoding="utf-8-sig").splitlines()
        self.record.unlink()
        return Path(lines[0]), Path(lines[1]), lines[2]

    def same(self, a: Path, b: Path) -> bool:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))

    def test_started_directly_it_starts_the_release_interpreter_in_the_release_folder(self):
        program, folder, line = self.start(self.release / build_release.LAUNCHER_NAME, "--store", "a b.json")
        self.assertTrue(self.same(program, self.release / "runtime" / "pythonw.exe"), program)
        self.assertTrue(self.same(folder, self.release), folder)
        self.assertTrue(line.endswith(' -m mirrorhold_gui --store "a b.json"'), line)

    def test_through_a_junction_to_its_folder_it_keeps_the_junctions_name(self):
        import _winapi
        current = self.tmp / "apps" / "current"
        _winapi.CreateJunction(str(self.release), str(current))
        self.addCleanup(os.rmdir, current)
        program, folder, _ = self.start(current / build_release.LAUNCHER_NAME)
        self.assertTrue(self.same(program, current / "runtime" / "pythonw.exe"), program)
        self.assertTrue(self.same(folder, current), folder)

    def test_through_a_symbolic_link_to_it_it_finds_the_release_the_link_leads_to(self):
        links = self.tmp / "Links"
        links.mkdir()
        alias = links / "mirrorhold.exe"
        try:
            os.symlink(self.release / build_release.LAUNCHER_NAME, alias)
        except OSError as exc:
            self.skipTest(f"this account may not make a symbolic link: {exc}")
        program, folder, _ = self.start(alias)
        self.assertTrue(self.same(program, os.path.realpath(self.release / "runtime" / "pythonw.exe")), program)
        self.assertTrue(self.same(folder, os.path.realpath(self.release)), folder)

    def long_release(self, *segments: str) -> Path:
        """A release folder ``segments`` deep holding the launcher and an interpreter that
        writes down how it was started: a copy of this interpreter's own windowless program —
        native, since a .NET stand-in in such a folder stops on the Framework's own path
        checks — whose path file names this interpreter's standard library and the release's
        folder, where a stand-in for the window's package writes the record."""
        folder = self.tmp.joinpath("apps", *segments)
        runtime = folder / "runtime"
        os.makedirs(extended(runtime))
        shutil.copyfile(self.release / build_release.LAUNCHER_NAME, extended(folder / build_release.LAUNCHER_NAME))
        base = Path(sys._base_executable).parent
        tag = f"{sys.version_info.major}{sys.version_info.minor}"
        for name in ("pythonw.exe", f"python{tag}.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            if (base / name).is_file():
                shutil.copyfile(base / name, extended(runtime / name))
        # The standard library is where ``encodings`` was imported from: ``os`` is frozen, and
        # under a release's embeddable interpreter its file name points into a Lib folder that
        # holds nothing, the library being the interpreter's zip.
        stdlib = os.path.dirname(os.path.dirname(sys.modules["encodings"].__file__))
        with open(extended(runtime / f"python{tag}._pth"), "w", encoding="utf-8") as handle:
            handle.write(f"{stdlib}\n..\n")
        os.makedirs(extended(folder / "mirrorhold_gui"))
        for name, text in (("__init__.py", ""), ("__main__.py", STAND_IN_PACKAGE)):
            with open(extended(folder / "mirrorhold_gui" / name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return folder

    def alias_to(self, launcher: Path) -> Path:
        links = self.tmp / "Links"
        links.mkdir(exist_ok=True)
        alias = links / "mirrorhold.exe"
        try:
            os.symlink(launcher, alias)
        except OSError as exc:
            self.skipTest(f"this account may not make a symbolic link: {exc}")
        return alias

    def test_to_a_release_in_a_folder_too_long_as_named_it_starts_it_by_the_folders_short_name(self):
        release = self.long_release(*["segment of a deeply nested release folder"] * 6)
        self.assertGreater(len(str(release)), 260)
        shortened = short_name(release)
        if len(shortened) > LONGEST_FOLDER:
            self.skipTest("the drive of the temporary folder keeps no short names")
        program, folder, line = self.start(self.alias_to(release / build_release.LAUNCHER_NAME))
        self.assertTrue(self.same(folder, Path(shortened)), folder)
        self.assertTrue(self.same(program, Path(shortened) / "runtime" / "pythonw.exe"), program)
        self.assertTrue(line.endswith(" -m mirrorhold_gui"), line)

    def test_to_a_release_no_name_of_whose_folder_is_short_enough_it_says_why_and_starts_nothing(self):
        release = self.long_release(*["abcdefgh"] * 26)          # 8.3 names already: none is shorter
        self.assertGreater(len(short_name(release)), LONGEST_FOLDER)
        message = self.tmp / "message.txt"
        env = dict(os.environ, MIRRORHOLD_LAUNCHER_RECORD=str(self.record),
                   MIRRORHOLD_LAUNCHER_MESSAGE_FILE=str(message))
        done = subprocess.run([str(self.alias_to(release / build_release.LAUNCHER_NAME))], cwd=str(self.tmp), env=env,
                              timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(message.read_text(encoding="utf-8-sig"), TOO_LONG.format(length=len(str(release))))
        self.assertFalse(self.record.exists(), "nothing was started")


@unittest.skipUnless(os.name == "nt" and build_release.CSC.is_file(), "the launcher is built by the .NET Framework's "
                                                                      "C# compiler, on Windows")
class LauncherStampsTest(TempDirTest):
    """The compiler stamps each build of the launcher with its own time and identifier; the
    build settles them, so one source compiled twice with one label is one file."""

    def compile(self, folder: str, *extra: str, label: str = "2026.10.1") -> Path:
        drawn = self.tmp / ICON_FILE_NAME
        if not drawn.exists():
            drawn.write_bytes(icon.icon({16: icon.render(16)}))
        target = self.tmp / folder / build_release.LAUNCHER_NAME
        target.parent.mkdir()
        if extra:
            done = subprocess.run([str(build_release.CSC), "-nologo", "-target:winexe", *extra, f"-out:{target}",
                                   "-r:System.Windows.Forms.dll", str(PROJECT / build_release.LAUNCHER_SOURCE)],
                                  capture_output=True, text=True, errors="replace")
            self.assertEqual(done.returncode, 0, done.stdout)
            return target
        return build_release.compile_launcher(PROJECT / build_release.LAUNCHER_SOURCE, drawn, target, label,
                                              "Copyright (c) 2026 offcutworks")

    @staticmethod
    def identifier(data: bytes) -> bytes:
        """The identifier the generated type's name spells."""
        start = data.index(b"<PrivateImplementationDetails>{") + len(b"<PrivateImplementationDetails>")
        return data[start:start + 38]

    @staticmethod
    def cli_header(data: bytes) -> int:
        """Where the file's CLI header lies: its data directory's address, through the sections."""
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        sections, optional_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        rva = struct.unpack_from("<I", data, pe + 24 + 96 + 8 * 14)[0]
        for i in range(sections):
            size, address, _, raw = struct.unpack_from("<IIII", data, pe + 24 + optional_size + 40 * i + 8)
            if address <= rva < address + size:
                return raw + rva - address
        raise AssertionError("no section holds the CLI header")

    def test_one_source_compiled_twice_with_one_label_is_one_file_dated_by_the_label(self):
        first, second = self.compile("first").read_bytes(), self.compile("second").read_bytes()
        self.assertEqual(sha256(first), sha256(second))
        pe = struct.unpack_from("<I", first, 0x3C)[0]
        self.assertEqual(struct.unpack_from("<I", first, pe + 8)[0], calendar.timegm((2026, 10, 1, 0, 0, 0)))
        self.assertNotEqual(self.identifier(first), b"{00000000-0000-0000-0000-000000000000}")
        other = self.compile("other", label="2026.10.2").read_bytes()
        self.assertNotEqual(self.identifier(other), self.identifier(first))

    def test_a_file_whose_stamps_it_cannot_settle_is_refused(self):
        debug = self.compile("debug", "-debug+")
        whole = self.compile("whole").read_bytes()
        pe = struct.unpack_from("<I", whole, 0x3C)[0]
        checksum, strong_name = bytearray(whole), bytearray(whole)
        struct.pack_into("<I", checksum, pe + 24 + 64, 1)
        struct.pack_into("<II", strong_name, self.cli_header(whole) + 32, 0x2000, 128)
        cases = {"debug": debug}
        for name, data in (("truncated", whole[:0x200]), ("empty", b""), ("checksum", checksum),
                           ("strong-name", strong_name)):
            cases[name] = self.tmp / f"{name}.exe"
            cases[name].write_bytes(data)
        for name, path in cases.items():
            with self.subTest(case=name), self.assertRaises(build_release.BuildError):
                build_release.settle_stamps(path, "2026.10.1")


class ComposedBuildTest(TempDirTest):
    """MirrorHold's part of the build composed with the shared core beside it."""

    def test_mirrorhold_supplies_what_it_ships_its_suite_and_every_path_it_names(self):
        tool = build_release.mirrorhold(self.tmp / "engine")
        self.assertEqual((tool.name, tool.shipped, tool.bytecode), ("MirrorHold", build_release.SHIPPED,
                                                                    ("mirrorhold", "mirrorhold_gui")))
        self.assertEqual((tool.suite.path, tool.suite.ledger), ("tests/run_all.py", "tests/TEST_LEDGER.md"))
        for name in (*tool.shipped, *tool.required, tool.suite.path, tool.suite.ledger):
            with self.subTest(name=name):
                self.assertTrue((PROJECT / name).exists())

    def test_a_pinned_input_sharing_a_file_name_refuses_the_build_before_anything_is_written(self):
        tool = build_release.mirrorhold(self.tmp / "engine")
        for twin in (release_core.EMBEDDABLE, build_inputs.PYWIN32):
            shadow = release_core.Input(twin.name.upper(), twin.url, "0" * 64, 1, twin.listed_by, "a shadow")
            with self.subTest(twin=twin.name), self.assertRaises(build_release.BuildError):
                release_core.build(self.tmp, replace(tool, inputs=(*tool.inputs, shadow)), label="2026.11.1",
                                   inputs=self.tmp / "inputs", out=self.tmp / "out")
        self.assertFalse((self.tmp / "out").exists())

    def test_the_cores_release_label_is_the_build_records(self):
        from mirrorhold.installation import release
        self.assertEqual(release_core.LABEL_PATTERN.pattern, release.LABEL_PATTERN.pattern)

    def test_an_asset_after_the_zip_is_a_file_beside_it_under_a_name_of_its_own(self):
        destination = self.tmp / "2026.11.1"
        destination.mkdir()
        (destination / "Setup.exe").write_bytes(b"setup")
        (self.tmp / "elsewhere.exe").write_bytes(b"elsewhere")
        taken = ["MirrorHold-2026.11.1.zip", "SHA256SUMS"]
        self.assertEqual(release_core.checked_assets(destination, [destination / "Setup.exe"], taken),
                         [destination / "Setup.exe"])
        (destination / "SHA256SUMS").write_bytes(b"sums")
        for refused in ([self.tmp / "elsewhere.exe"], [destination / "missing.exe"], [destination / "SHA256SUMS"],
                        [destination / "sha256sums"], [destination / "Setup.exe", destination / "Setup.exe"]):
            with self.subTest(refused=[p.name for p in refused]), self.assertRaises(build_release.BuildError):
                release_core.checked_assets(destination, refused, taken)


SETUP_IDENTITY = "D1AB086A-4E20-49D0-A170-A7D64B8AEEE2"
"""The identity the setup program registers its installation under: an update finds the
installation it replaces by it, so it is never changed."""


class SetupProgramTest(TempDirTest):
    """The step after the zip that makes the setup program, its compiler run by a stand-in:
    no test unpacks Inno Setup, compiles the script or runs a setup program."""

    def setUp(self):
        super().setUp()
        self.release = self.tmp / "assembled"
        self.release.mkdir()
        (self.release / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 offcutworks\n", encoding="utf-8")
        self.source = self.tmp / "source"
        (self.source / "tools" / "setup").mkdir(parents=True)
        (self.source / build_release.SETUP_SCRIPT).write_text("; the commit's script\n", encoding="utf-8")
        self.destination = self.tmp / "2026.11.1"
        self.destination.mkdir()
        self.zip = self.destination / "MirrorHold-2026.11.1.zip"
        self.zip.write_bytes(b"the zip")
        self.installer = self.tmp / "inputs" / build_inputs.INNO_SETUP.name
        self.installer.parent.mkdir()
        self.installer.write_bytes(b"installer")
        self.calls = []
        self.compiled = b"setup"

    def after_zip(self):
        return release_core.AfterZip("2026.11.1", "a" * 40, self.source, self.release,
                                     {build_inputs.INNO_SETUP.name: self.installer}, self.zip, self.destination)

    def run_stand_in(self, argv, **_):
        self.calls.append(list(argv))
        if Path(argv[0]) == self.installer:
            folder = Path(next(a for a in argv if a.startswith("/DIR="))[len("/DIR="):])
            folder.mkdir(parents=True)
            (folder / build_release.SETUP_COMPILER).write_bytes(b"compiler")
        elif self.compiled is not None:
            out = Path(next(a for a in argv if a.startswith("/O"))[2:])
            name = next(a for a in argv if a.startswith("/F"))[2:]
            (out / f"{name}.exe").write_bytes(self.compiled)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_mirrorhold_makes_its_setup_program_after_the_zip_with_inno_setup_pinned(self):
        tool = build_release.mirrorhold(self.tmp / "engine")
        self.assertEqual(tool.after_zip, (build_release.make_setup,))
        self.assertEqual(tool.inputs, (build_inputs.PYWIN32, build_inputs.INNO_SETUP))
        self.assertIn(build_release.SETUP_SCRIPT, tool.required)
        self.assertIn(build_inputs.INNO_SETUP, build_inputs.INPUTS)
        self.assertEqual(build_release.setup_name("2026.11.1"), "MirrorHold-2026.11.1-setup.exe")

    def test_the_compiler_is_unpacked_portably_and_compiles_the_commits_script_over_the_assembled_files(self):
        written = build_release.make_setup(self.after_zip(), run=self.run_stand_in)
        self.assertEqual(written, [self.destination / "MirrorHold-2026.11.1-setup.exe"])
        unpack, compile_ = self.calls
        self.assertEqual(unpack[:6], [str(self.installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                      "/CURRENTUSER", "/PORTABLE=1"])
        compiler_folder = Path(unpack[6][len("/DIR="):])
        self.assertEqual(Path(compile_[0]), compiler_folder / build_release.SETUP_COMPILER)
        self.assertEqual(compile_[1:], ["/Q", f"/O{self.destination}", "/FMirrorHold-2026.11.1-setup",
                                        f"/DRelease={self.release}", "/DLabel=2026.11.1", "/DTouchDate=2026-11-01",
                                        "/DCopyright=Copyright (c) 2026 offcutworks",
                                        str(self.source / build_release.SETUP_SCRIPT)])
        self.assertFalse(compiler_folder.exists(), "the compiler is deleted with the build's folder for it")
        self.assertEqual((self.zip.read_bytes(), sorted(p.name for p in self.release.iterdir())),
                         (b"the zip", ["LICENSE"]))

    def test_a_compiler_that_writes_nothing_or_a_step_that_changes_the_zip_refuses_the_build(self):
        self.compiled = None
        with self.assertRaises(build_release.BuildError):
            build_release.make_setup(self.after_zip(), run=self.run_stand_in)

        def touching_the_zip(argv, **options):
            done = self.run_stand_in(argv, **options)
            if Path(argv[0]) != self.installer:
                self.zip.write_bytes(b"another zip")
            return done
        self.compiled = b"setup"
        with self.assertRaises(build_release.BuildError) as caught:
            build_release.make_setup(self.after_zip(), run=touching_the_zip)
        self.assertIn("the zip changed", str(caught.exception))

    def test_an_installer_that_does_not_unpack_refuses_the_build_with_what_it_said(self):
        failing = lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "the folder is locked")  # noqa: E731
        with self.assertRaises(build_release.BuildError) as caught:
            build_release.make_setup(self.after_zip(), run=failing)
        self.assertIn("could not be unpacked", str(caught.exception))
        self.assertIn("the folder is locked", str(caught.exception))

    def test_a_compiler_that_hangs_refuses_the_build(self):
        def hanging(argv, **options):
            if Path(argv[0]) != self.installer:
                raise subprocess.TimeoutExpired(argv, options["timeout"])
            return self.run_stand_in(argv, **options)
        with self.assertRaises(build_release.BuildError) as caught:
            build_release.make_setup(self.after_zip(), run=hanging)
        self.assertIn("did not end within", str(caught.exception))

    def test_the_script_installs_for_the_user_alone_under_one_identity_and_lets_the_program_decide(self):
        script = (PROJECT / build_release.SETUP_SCRIPT).read_text(encoding="utf-8")
        for directive in ("PrivilegesRequired=lowest", r"DefaultDirName={autopf}\MirrorHold", "DisableDirPage=yes",
                          "UsePreviousAppDir=no", "DisableProgramGroupPage=yes", "AppName=MirrorHold",
                          "UninstallDisplayName=MirrorHold", "AppPublisher=offcutworks", "AppVersion={#Label}",
                          "CloseApplications=no", "TouchDate={#TouchDate}", "TouchTime=00:00:00",
                          "TimeStampsInUTC=yes", "AppId={{{#AppGuid}}"):
            with self.subTest(directive=directive):
                self.assertIn(f"\n{directive}\n", script)
        self.assertIn(f'#define AppGuid "{SETUP_IDENTITY}"', script)
        self.assertNotIn("[Icons]", script, "the program's own Set up writes its Start menu entry")
        self.assertNotIn("[Registry]", script)
        files = [line for line in script.splitlines() if line.startswith("Source:")]
        self.assertEqual(files, ['Source: "{#Release}\\*"; DestDir: "{app}"; '
                                 'Flags: ignoreversion recursesubdirs createallsubdirs touch'])
        from mirrorhold import cli
        for command in ("'install'", "'installation'", "'uninstall --if-set-up-here'"):
            with self.subTest(command=command):
                self.assertIn(command, script)
                cli._parser().parse_args(command.strip("'").split())

    def test_the_script_refuses_any_folder_but_its_own_before_anything_changes(self):
        # Inno Setup takes a folder named by /DIR= or a /LOADINF= file's Dir with no folder
        # page shown; the refusal compares either with DefaultDirName at InitializeSetup.
        script = (PROJECT / build_release.SETUP_SCRIPT).read_text(encoding="utf-8")
        start = script.index("function InitializeSetup: Boolean;")
        initialize = script[start:script.index("\nend;", start)]
        self.assertIn("Own := ExpandConstant('{#SetupSetting(\"DefaultDirName\")}');", initialize)
        self.assertIn("not SameFolder(Requested, Own)", initialize)
        self.assertLess(initialize.index("Result := False;"), initialize.index("RegQueryStringValue"))
        for switch in ("'/DIR='", "'/LOADINF='", "'expand:'"):
            with self.subTest(switch=switch):
                self.assertIn(switch, script)


class SharedNamesTest(unittest.TestCase):
    def test_the_build_writes_what_the_window_reads(self):
        self.assertEqual((build_release.NOTICES_FILE_NAME, build_release.ICON_FILE_NAME),
                         (NOTICES_FILE_NAME, ICON_FILE_NAME))
        self.assertIn(build_release.LICENCE_FILE_NAME, build_release.SHIPPED)


if __name__ == "__main__":
    unittest.main()
