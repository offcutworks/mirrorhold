"""The tool-neutral core of a portable release build: the order a release is built in from
a commit, and every step that belongs to no one tool.

This file is shared, byte for byte, by every tool its maintainer publishes: each such
tool's ``tools/`` folder holds a copy beside its own ``build_release.py``, which composes
it with what is the tool's own. A change is made to the shared copy and copied out to
every tool, never made in one tool's copy alone.

A release is a zip a user unpacks and runs without Python. ``build`` makes it from a
commit, never from a working tree, and downloads nothing. In order, it:

- checks every pinned input — the runtime's (``RUNTIME_INPUTS``) and the tool's own —
  against its SHA-256 before anything is built, then runs the tool's own checks;
- reads the commit's tree of the tool's folder through ``git archive``, the way a
  snapshot of the tool is made, so a release carries only committed files and nothing
  export-ignored (a configuration, a log, a downloaded engine cannot reach it);
- assembles the release folder: what the tool ships, copied from that tree, and
  ``runtime/``, CPython's embeddable package with Tk from CPython's full package of the
  same version (``assemble_runtime``);
- runs the tool's assembly steps, which add what the tool needs beside them — further
  libraries, its notices, a build record, a launcher — each given the pinned inputs;
- compiles every module of the folders the tool names, and the runtime's ``Lib``, to
  bytecode bound to its source's hash, so that running the release writes nothing into
  its own folder (``compile_bytecode``);
- refuses a release any of whose files names the folder it was assembled in or the home
  folder of the account building it: compiled code records file names, and those paths
  name the account;
- runs the tool's whole suite under the assembled interpreter on the commit's tree, and
  refuses the release unless the tool's reading of the transcript against its ledger
  finds the failing tests exactly those the ledger records (``run_suite``; a build told
  not to run it says the build is not verified);
- writes ``<out>/<label>/<Name>-<label>.zip``, the release's files at its top level, its
  entries in one order with the first day of the label's month as every file's time;
- runs the tool's steps after the zip, each of which may write further assets beside it
  from the same assembled files;
- writes ``SHA256SUMS`` beside them, naming the zip and every further asset.

The zip holds no folder of its own around the files: Windows' *Extract All* unpacks it
into a folder named after the zip, and a package manager unpacks it into the one folder it
keeps for the program. Two builds of one commit with one label carry the same files with
the same content, but for whatever a tool's own step stamps with the moment of its build.

What a tool supplies is a ``Tool``: its name, what it ships, the paths its commit must
hold, its pinned inputs beyond the runtime's (the extra-inputs slot), its own checks, its
assembly steps, the folders of its packages, its suite and how its ledger names reds, and
its steps after the zip (the after-zip slot). Python's own notices — CPython's licence,
which covers the libraries its Windows build links, Tcl and Tk among them, and the section
of its documentation listing the software it incorporates — are read here
(``python_licences``) and set at the head of a tool's notices by ``notices_text``.

Obtaining the pinned inputs is no step of the build, which downloads nothing, but it is the
same for every tool, so it is here too: ``fetch_main`` is the command line of each tool's
``tools/fetch_build_inputs.py``, which names the tool's inputs and the user agent its
downloads send, and ``obtain`` puts one input in place, downloaded or copied, only once its
SHA-256 matches the pin.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

PYTHON_VERSION = "3.14.8"
PYTHON_TAG = "314"
INPUTS_DIR_NAME = "build-inputs"
RUNTIME_DIR_NAME = "runtime"
SUMS_NAME = "SHA256SUMS"
CHUNK = 1 << 20


class BuildError(Exception):
    """The release cannot be built; the message says why."""


# --- the pinned inputs -----------------------------------------------------------------

@dataclass(frozen=True)
class Input:
    name: str          # the file's name in the inputs folder
    url: str
    sha256: str
    size: int
    listed_by: str     # where the publisher lists the digest
    purpose: str


EMBEDDABLE = Input(
    f"python-{PYTHON_VERSION}-embed-amd64.zip",
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
    "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310", 12_601_679,
    "https://www.python.org/downloads/release/python-3148/",
    "the interpreter: CPython's embeddable package")
FULL_PYTHON = Input(
    f"python-{PYTHON_VERSION}-amd64.zip",
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.zip",
    "4873947a8afc037846b180312b83c744a4146a851cfd316a75c3125a4d8299da", 32_879_974,
    "https://www.python.org/ftp/python/index-windows.json",
    "Tk, and the licence page of CPython's documentation")
RUNTIME_INPUTS = (EMBEDDABLE, FULL_PYTHON)
"""What every release's runtime is built from, each pinned by the SHA-256 its publisher
lists. Tk is taken from CPython's full package of the same version, which python.org
publishes for its install manager: its Tcl and Tk libraries carry their own script
libraries and are signed, as the rest of the interpreter is, so a release needs no folder
of Tcl scripts. That package also carries the licence page of CPython's documentation,
which names the software CPython incorporates.

Moving to another interpreter means changing these pins together with the interpreter's
version and tag, and every pin a tool derives from the tag, fetching again, and building:
a build runs the tool's whole suite under the interpreter it assembled."""


class InputError(Exception):
    """An input is missing or is not the pinned one; the message says which."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def checked(path: Path, sha256: str) -> Path:
    """``path`` when it holds the pinned content; raises InputError otherwise."""
    path = Path(path)
    if not path.is_file():
        raise InputError(f"{path} is missing")
    found = sha256_of(path)
    if found != sha256:
        raise InputError(f"{path} is not the pinned file: its SHA-256 is {found}, the pin is {sha256}")
    return path


def checked_input(folder: Path, item: Input) -> Path:
    """The pinned input ``item`` in ``folder``; raises InputError when it is missing or differs."""
    return checked(Path(folder) / item.name, item.sha256)


# --- obtaining the pinned inputs -------------------------------------------------------

FETCH_TIMEOUT = 120.0

Opener = Callable[..., object]


def _download(url: str, target: Path, opener: Opener, user_agent: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with opener(request, timeout=FETCH_TIMEOUT) as response, open(target, "wb") as handle:
            shutil.copyfileobj(response, handle, CHUNK)
    except OSError as exc:
        raise InputError(f"the download of {url} failed: {exc}") from None


def obtain(item: Input, folder: Path, *, user_agent: str, source: Optional[Path] = None,
           opener: Opener = urllib.request.urlopen,
           say: Callable[[str], None] = lambda _: None) -> Tuple[Path, bool]:
    """The pinned input in ``folder``, and whether it was there already. Copies it from
    ``source`` when that folder holds it, downloads it otherwise, sending ``user_agent``.
    Raises InputError and leaves nothing in place unless the digest matches."""
    final = Path(folder) / item.name
    if final.exists():
        found = sha256_of(final)
        if found != item.sha256:
            raise InputError(f"{final} is not the pinned file (SHA-256 {found}); remove it and run again")
        return final, True
    Path(folder).mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f"{item.name}.", suffix=".obtaining", dir=folder)
    os.close(handle)
    staged = Path(name)
    try:
        copy = Path(source) / item.name if source is not None else None
        if copy is not None and copy.is_file():
            say(f"Copying {copy}")
            shutil.copyfile(copy, staged)
        else:
            say(f"Downloading {item.url}")
            _download(item.url, staged, opener, user_agent)
        found = sha256_of(staged)
        if found != item.sha256:
            raise InputError(f"{item.name} is not the pinned file: its SHA-256 is {found}, the pin is {item.sha256} "
                             f"(listed by {item.listed_by})")
        os.replace(staged, final)
    finally:
        staged.unlink(missing_ok=True)
    return final, False


def obtain_all(folder: Path, items: Iterable[Input], *, user_agent: str, source: Optional[Path] = None,
               opener: Opener = urllib.request.urlopen,
               say: Callable[[str], None] = lambda _: None) -> List[Path]:
    """Every input of ``items`` in ``folder``; raises InputError at the first that cannot be obtained."""
    paths = []
    for item in items:
        path, present = obtain(item, folder, user_agent=user_agent, source=source, opener=opener, say=say)
        say(f"{'Already in place' if present else 'In place'}: {path} ({item.purpose})")
        paths.append(path)
    return paths


def fetch_main(argv: Optional[Sequence[str]], items: Iterable[Input], *, user_agent: str, folder: Path) -> int:
    """The command line of a tool's ``fetch_build_inputs.py``: every input of ``items`` in
    ``folder`` (or the folder ``--to`` names), a copy in the folder ``--from`` names taken
    instead of a download. Exit status 0 when every input is in place, 1 when one was
    refused or failed."""
    parser = argparse.ArgumentParser(description="Obtain the pinned inputs a release is built from.")
    parser.add_argument("--from", dest="source", type=Path, metavar="DIR",
                        help="take an input from this folder when it holds a copy, instead of downloading it")
    parser.add_argument("--to", type=Path, metavar="DIR", default=None,
                        help=f"where to put the inputs (default: {INPUTS_DIR_NAME} beside tools/)")
    args = parser.parse_args(argv)
    try:
        obtain_all(args.to or folder, items, user_agent=user_agent, source=args.source, say=print)
    except InputError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0


# --- the commit ------------------------------------------------------------------------

def git(args: Sequence[str], cwd: Path, *, text: bool = True):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=text,
                            encoding="utf-8" if text else None)
    if result.returncode != 0:
        error = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise BuildError(f"git {' '.join(args)}: {error.strip()}")
    return result.stdout.strip() if text else result.stdout


def resolve_commit(project: Path, rev: str) -> Tuple[str, str]:
    """The full identifier of the commit ``rev`` names, and the project's tree in it."""
    root = Path(git(["rev-parse", "--show-toplevel"], project))
    prefix = git(["rev-parse", "--show-prefix"], project).rstrip("/")
    commit = git(["rev-parse", "--verify", f"{rev}^{{commit}}"], root)
    tree = git(["rev-parse", f"{commit}:{prefix}" if prefix else f"{commit}^{{tree}}"], root)
    return commit, tree


def archive(project: Path, tree: str, target: Path) -> List[str]:
    """Write ``git archive``'s view of ``tree`` into ``target``; return the files' paths.
    It runs at the repository's root: run in a sub-folder, git archives only that
    sub-folder's part of the tree it is given. Its files hold the commit's bytes, as the
    commit's own attributes leave them, whatever the builder's line-ending settings:
    ``core.autocrlf``, ``core.eol`` and an attributes file of the builder's own
    (``core.attributesFile``) otherwise rewrite line endings on the way out (measured with
    git 2.54), so two builders would make two releases. Only an attributes file written into
    the repository's own ``info`` folder still applies."""
    root = Path(git(["rev-parse", "--show-toplevel"], project))
    data = git(["-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.attributesFile=/dev/null",
                "archive", "--format=tar", tree], root, text=False)
    files = []
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise BuildError(f"the archive holds {member.name!r}")
            if member.isfile():
                files.append(member.name)
            elif not member.isdir():
                raise BuildError(f"the archive holds {member.name!r}, which is neither a file nor a folder")
        bundle.extractall(target)
    return sorted(files)


# --- the label -------------------------------------------------------------------------

LABEL_PATTERN = re.compile(r"\d{4}\.(?:[1-9]|1[0-2])\.[1-9]\d*")
"""A release label: YEAR.MONTH.N, the N-th release of that month."""


def check_label(label: str) -> str:
    if not LABEL_PATTERN.fullmatch(label):
        raise BuildError(f"{label!r} is not a release label (YEAR.MONTH.N, the N-th release of that month)")
    return label


def label_time(label: str) -> Tuple[int, int, int, int, int, int]:
    """The time every file of the release's zip carries: the first day of the label's
    month, at midnight. It names no time zone and no moment of the build."""
    year, month, _ = (int(part) for part in check_label(label).split("."))
    return year, month, 1, 0, 0, 0


# --- the runtime -----------------------------------------------------------------------

PATH_FILE = f"python{PYTHON_TAG}._pth"
PATH_FILE_TEXT = f"python{PYTHON_TAG}.zip\n.\nLib\n..\nimport site\n"
"""The interpreter's search path: its standard library, its own folder, ``Lib`` (Tk's
module, and whatever a tool puts in ``site-packages``), and the release's folder above
it, where the tool's packages are; ``import site`` reads the ``.pth`` files a library in
``site-packages`` brings."""
SITE_PACKAGES = PurePosixPath("Lib", "site-packages")
"""Where, below ``runtime/``, a tool puts a library the release carries."""

TK_FILES = ("DLLs/_tkinter.pyd", "DLLs/tcl90.dll", "DLLs/tcl9tk90.dll", "DLLs/zlib1.dll")
TK_MODULE = "Lib/tkinter/"


def extract_member(bundle: zipfile.ZipFile, name: str, target: Path) -> None:
    """The member ``name`` of ``bundle`` written to ``target``, its folders made."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open(name) as source, open(target, "wb") as handle:
        shutil.copyfileobj(source, handle)


def assemble_runtime(runtime: Path, embeddable: Path, full: Path) -> None:
    """CPython's embeddable package in ``runtime``, with its path file and Tk from the full
    package beside it."""
    with zipfile.ZipFile(embeddable) as bundle:
        bundle.extractall(runtime)
    (runtime / PATH_FILE).write_text(PATH_FILE_TEXT, encoding="utf-8", newline="\n")
    with zipfile.ZipFile(full) as bundle:
        names = set(bundle.namelist())
        for name in TK_FILES:
            if name not in names:
                raise BuildError(f"{full.name} holds no {name}")
            extract_member(bundle, name, runtime / PurePosixPath(name).name)
        module = sorted(n for n in names if n.startswith(TK_MODULE) and not n.endswith("/"))
        if not module:
            raise BuildError(f"{full.name} holds no {TK_MODULE}")
        for name in module:
            extract_member(bundle, name, runtime / name)


# --- the notices -----------------------------------------------------------------------

LICENCE_PAGE = "Doc/html/license.html"
LICENCE_PAGE_SECTION = "licenses-and-acknowledgements-for-incorporated-software"


class _PageText(html.parser.HTMLParser):
    """The text of one section of a documentation page: headings underlined, paragraphs
    and list items wrapped, preformatted blocks kept as they are."""

    BLOCKS = {"p", "li", "dt", "dd", "h1", "h2", "h3", "h4"}

    def __init__(self, section_id: str):
        super().__init__(convert_charrefs=True)
        self.section_id = section_id
        self.depth = 0            # sections open since the wanted one began; 0 outside it
        self.done = False
        self.skip = 0             # inside a heading's link mark
        self.pre = 0
        self.block: Optional[str] = None
        self.buffer: List[str] = []
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.done:
            return
        if tag == "section":
            if self.depth:
                self.depth += 1
            elif attrs.get("id") == self.section_id:
                self.depth = 1
            return
        if not self.depth:
            return
        if tag == "a" and "headerlink" in (attrs.get("class") or ""):
            self.skip += 1
        elif tag == "pre":
            self._flush()
            self.pre += 1
        elif tag in self.BLOCKS:
            self._flush()
            self.block = tag

    def handle_endtag(self, tag):
        if self.done or not self.depth:
            return
        if tag == "section":
            self.depth -= 1
            if not self.depth:
                self._flush()
                self.done = True
        elif tag == "a" and self.skip:
            self.skip -= 1
        elif tag == "pre" and self.pre:
            self.pre -= 1
            text = "".join(self.buffer).strip("\n")
            self.buffer = []
            if text:
                self.parts.append(text)
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self.depth and not self.done and not self.skip:
            self.buffer.append(data)

    def _flush(self):
        if self.pre:
            return
        text = " ".join("".join(self.buffer).split())
        self.buffer = []
        block, self.block = self.block, None
        if not text:
            return
        if block in ("h1", "h2", "h3", "h4"):
            self.parts.append(f"{text}\n{('=' if block in ('h1', 'h2') else '-') * len(text)}")
        elif block == "li":
            self.parts.append(textwrap.fill(text, 79, initial_indent="- ", subsequent_indent="  "))
        else:
            self.parts.append(textwrap.fill(text, 79))


def page_section_text(page: str, section_id: str) -> str:
    """The plain text of the section ``section_id`` of an HTML page, its subsections included."""
    parser = _PageText(section_id)
    parser.feed(page)
    parser.close()
    if not parser.parts:
        raise BuildError(f"the page holds no section {section_id!r}")
    return "\n\n".join(parser.parts)


def python_licences(embeddable: Path, full: Path) -> Tuple[str, str]:
    """CPython's licence, from the embeddable package, and the incorporated-software
    section of its documentation's licence page, from the full package, as text."""
    with zipfile.ZipFile(embeddable) as bundle:
        licence = bundle.read("LICENSE.txt").decode("utf-8")
    with zipfile.ZipFile(full) as bundle:
        incorporated = page_section_text(bundle.read(LICENCE_PAGE).decode("utf-8"), LICENCE_PAGE_SECTION)
    return licence, incorporated


def heading(text: str) -> str:
    """A component's heading, underlined to its own length: a full-width rule would wrap in
    a window whose font is proportional."""
    return f"{text}\n{'=' * len(text)}"


@dataclass(frozen=True)
class Component:
    """Software a release carries beside Python, as its notices name it."""
    name: str                  # its name and version, as the notices' heading
    where: str                 # the folder of the release that holds it, as Windows writes it
    licences: Tuple[str, ...]  # its licences' texts; one repeated is reproduced once


def notices_text(title: str, intro: Sequence[str], *, python_licence: str, incorporated: str,
                 components: Sequence[Component]) -> str:
    """The third-party notices a release ships: ``title`` and the tool's ``intro`` lines,
    then every component it carries — Python first, with Tcl/Tk — and each one's licence."""
    parts = [title, "", *intro, "", f"- Python {PYTHON_VERSION}, with Tcl/Tk, in {RUNTIME_DIR_NAME}\\"]
    parts += [f"- {component.name}, in {component.where}" for component in components]
    parts += ["", heading(f"Python {PYTHON_VERSION}"), "", python_licence.strip("\n"), "",
              heading("Software incorporated in Python, as Python's documentation lists it"), "",
              incorporated.strip("\n")]
    for component in components:
        parts += ["", heading(component.name)]
        distinct: List[str] = []
        for text in component.licences:
            if text.strip() not in (t.strip() for t in distinct):
                distinct.append(text)
        for text in distinct:
            parts += ["", text.strip("\n")]
    parts.append("")
    return "\n".join(parts).replace("\r\n", "\n")


# --- bytecode, and the paths a release must not name -----------------------------------

def interpreter_environment() -> dict:
    """This process's environment less whatever would point an interpreter elsewhere."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "TCL_", "TK_"))}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def compile_bytecode(release: Path, folders: Iterable[Path]) -> None:
    """Every module in ``folders`` compiled by the release's own interpreter, its bytecode
    bound to the source's hash and never checked against its time: the interpreter then
    reads it and writes no cache of its own into the release. Each module's recorded file
    name is its path inside the release (``-s``), never the build folder's: compiled code
    carries that name, and the build folder's path names the account that built it. Every
    module is compiled afresh (``-f``): starting the interpreter has already imported
    whatever a ``.pth`` file in ``site-packages`` bootstraps and cached it the ordinary way.

    In one process, never with workers (``-j``): each worker is a fresh start of this
    interpreter that imports such a bootstrap again while it is being compiled, so Windows
    refuses to replace its compiled file, or a worker's ordinary cache, naming the build
    folder, lands over it (both measured on the development machine with pywin32's; one
    process was also the faster)."""
    python = release / RUNTIME_DIR_NAME / "python.exe"
    done = subprocess.run([str(python), "-m", "compileall", "-q", "-f", "--invalidation-mode",
                           "unchecked-hash", "-s", str(release), *(str(f) for f in folders)], cwd=str(release),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=interpreter_environment())
    if done.returncode != 0:
        raise BuildError(f"bytecode compilation failed: {(done.stdout + done.stderr).strip()[-2000:]}")


def release_files(folder: Path) -> List[str]:
    """Every file under ``folder``, as sorted relative paths with forward slashes."""
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def files_naming(folder: Path, paths: Iterable[str]) -> List[str]:
    """The files under ``folder`` whose bytes hold any of ``paths`` — with either slash, in
    any case, as UTF-8 or as UTF-16 — as relative paths."""
    needles = set()
    for path in paths:
        for spelling in {path.replace("/", "\\"), path.replace("\\", "/")}:
            needles.add(spelling.lower().encode("utf-8"))
            needles.add(spelling.lower().encode("utf-16-le"))
    found = []
    for name in release_files(folder):
        data = (folder / name).read_bytes().lower()
        if any(needle in data for needle in needles):
            found.append(name)
    return found


# --- the suite -------------------------------------------------------------------------

SUITE_TIMEOUT = 3600
_FAILURE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)\s]+)\)")


def failed_tests(output: str) -> Set[str]:
    """The tests a unittest transcript reports failed or in error, as module.Class.test
    (unittest names them ``test (module.Class)`` before Python 3.11 and
    ``test (module.Class.test)`` since)."""
    found = set()
    for line in output.splitlines():
        m = _FAILURE.match(line)
        if m:
            name, where = m.group(2), m.group(3)
            found.add(where if where.endswith(f".{name}") else f"{where}.{name}")
    return found


@dataclass(frozen=True)
class SuiteResult:
    summary: str                          # the suite's own count line
    failed: Tuple[str, ...]
    unexpected: Tuple[str, ...]           # red and not in the ledger
    recovered: Tuple[str, ...]            # in the ledger and no longer red

    @property
    def matches_ledger(self) -> bool:
        return not self.unexpected and not self.recovered


def verdict(summary: str, failed: Iterable[str], expected: Iterable[str]) -> SuiteResult:
    """The suite's result against the reds its ledger records: a release needs the failing
    tests to be exactly those, so a red the ledger does not hold and a ledgered test that
    passes each refuse it."""
    failed, expected = set(failed), set(expected)
    return SuiteResult(summary, tuple(sorted(failed)), tuple(sorted(failed - expected)),
                       tuple(sorted(expected - failed)))


def _nothing(*_) -> None:
    return None


@dataclass(frozen=True)
class Suite:
    """A tool's suite, as a build runs it."""
    path: str                                        # its entry, relative to the tool's folder
    ledger: str                                      # its ledger of reds left standing, likewise
    judge: Callable[[str, str], SuiteResult]         # (transcript, ledger's text) -> the result
    prepare: Callable[[Path], None] = _nothing       # puts what the suite needs beside the runtime into the tree
    skipped: str = "the suite was not run"           # what a build told not to run it says, before "not verified"


def run_suite(source: Path, runtime: Path, suite: Suite, transcript: Path, *,
              say: Callable[[str], None] = lambda _: None) -> SuiteResult:
    """The whole suite on the commit's tree (``source``) under the release's interpreter,
    copied beside it with whatever the tool's ``prepare`` adds. The tree holds no build
    record, so the program in it is a working copy; the suite's output is written to
    ``transcript`` and judged by the tool against its ledger."""
    shutil.copytree(runtime, source / RUNTIME_DIR_NAME)
    suite.prepare(source)
    python = source / RUNTIME_DIR_NAME / "python.exe"
    say(f"Running the whole suite under Python {PYTHON_VERSION}")
    started = time.monotonic()
    with open(transcript, "w", encoding="utf-8", newline="\n") as handle:
        try:
            subprocess.run([str(python), suite.path], cwd=str(source), stdout=handle, stderr=subprocess.STDOUT,
                           env=interpreter_environment(), timeout=SUITE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise BuildError(f"the suite did not end within {SUITE_TIMEOUT} s; see {transcript}") from None
    result = suite.judge(transcript.read_text(encoding="utf-8", errors="replace"),
                         (source / suite.ledger).read_text(encoding="utf-8"))
    say(f"Suite: {result.summary} in {time.monotonic() - started:.0f} s; "
        f"{'the failures are exactly the ledger' if result.matches_ledger else 'the failures differ from the ledger'}")
    return result


# --- the zip and its sums --------------------------------------------------------------

def write_zip(folder: Path, target: Path, date_time: Tuple[int, int, int, int, int, int]) -> int:
    """Zip ``folder``'s files at the zip's top level, in one order, each with ``date_time``
    and no attributes, so equal folders make equal zips. Returns the number of files."""
    names = release_files(folder)
    with zipfile.ZipFile(target, "w") as bundle:
        for name in names:
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            bundle.writestr(info, (folder / name).read_bytes(), compresslevel=9)
    return len(names)


def write_sums(target: Path, files: Sequence[Path]) -> Path:
    target.write_text("".join(f"{sha256_of(f)}  {f.name}\n" for f in files), encoding="utf-8", newline="\n")
    return target


# --- what a tool supplies, and the build -----------------------------------------------

@dataclass(frozen=True)
class Assembly:
    """What a tool's assembly step works on."""
    label: str
    commit: str                   # the commit's full identifier
    source: Path                  # the commit's tree of the tool's folder
    release: Path                 # the release folder being assembled
    runtime: Path                 # its runtime/, assembled
    inputs: Mapping[str, Path]    # every pinned input, checked, by its file name


@dataclass(frozen=True)
class AfterZip:
    """What a tool's step after the zip works on: the same assembled files, and the folder
    its assets go into, beside the zip."""
    label: str
    commit: str
    source: Path
    release: Path
    inputs: Mapping[str, Path]
    zip_path: Path
    destination: Path


@dataclass(frozen=True)
class Tool:
    """What a tool supplies to its release build."""
    name: str                                   # its name as written: the zip is <name>-<label>.zip
    shipped: Tuple[str, ...]                    # files and folders of its tree the release carries
    suite: Suite
    required: Tuple[str, ...] = ()              # further paths its build reads from the commit
    inputs: Tuple[Input, ...] = ()              # its pinned inputs beyond the runtime's
    check: Callable[[], None] = _nothing        # its own checks before anything is built
    assembly: Tuple[Callable[[Assembly], None], ...] = ()
    bytecode: Tuple[str, ...] = ()              # its folders of the release compiled to bytecode
    after_zip: Tuple[Callable[[AfterZip], Sequence[Path]], ...] = ()   # each returns the assets it wrote


@dataclass(frozen=True)
class BuildResult:
    label: str
    commit: str
    zip_path: Path
    sums_path: Path
    sha256: str
    files: int
    assets: Tuple[Path, ...] = ()               # what the steps after the zip wrote
    suite: Optional[SuiteResult] = None
    notes: Tuple[str, ...] = field(default_factory=tuple)


def checked_assets(destination: Path, paths: Iterable[Path], taken: Iterable[str]) -> List[Path]:
    """The assets a step after the zip names, each a file directly in ``destination`` under a
    name none of ``taken`` holds in any case — Windows' file names ignore it, so an asset
    named as the sums are in another case is the sums' file; raises BuildError at the first
    that is not."""
    names = {name.casefold() for name in taken}
    assets = []
    for path in paths:
        path = Path(path)
        if not path.is_file() or path.resolve().parent != destination.resolve():
            raise BuildError(f"an asset after the zip is not a file beside it: {path}")
        if path.name.casefold() in names:
            raise BuildError(f"an asset after the zip takes a name already taken: {path.name}")
        names.add(path.name.casefold())
        assets.append(path)
    return assets


def build(project: Path, tool: Tool, *, label: str, commit: str = "HEAD", inputs: Optional[Path] = None,
          out: Optional[Path] = None, suite: bool = True,
          say: Callable[[str], None] = lambda _: None) -> BuildResult:
    """Build the release ``label`` of ``commit`` of the tool in ``project``. Raises
    BuildError or InputError, or whatever a tool's own step raises."""
    project = Path(project).resolve()
    check_label(label)
    names = [item.name.casefold() for item in (*RUNTIME_INPUTS, *tool.inputs)]
    if len(set(names)) != len(names):
        raise BuildError(f"{tool.name}'s pinned inputs share a file name with each other or with the runtime's")
    inputs = Path(inputs) if inputs else project / INPUTS_DIR_NAME
    out = Path(out) if out else project / "dist"
    destination = out / label
    if destination.exists():
        raise BuildError(f"{destination} exists already; remove it or choose another label")
    pinned = {item.name: checked_input(inputs, item) for item in (*RUNTIME_INPUTS, *tool.inputs)}
    tool.check()
    full_commit, tree = resolve_commit(project, commit)
    say(f"Building release {label} of commit {full_commit}")
    root_name = f"{tool.name}-{label}"
    with tempfile.TemporaryDirectory(prefix=f"{tool.name.lower()}-build-") as scratch:
        scratch = Path(scratch)
        source = scratch / "source"
        archive(project, tree, source)
        missing = [name for name in (*tool.shipped, *tool.required, tool.suite.ledger, tool.suite.path)
                   if not (source / name).exists()]
        if missing:
            raise BuildError(f"commit {full_commit} holds no {', '.join(missing)}")
        release = scratch / root_name
        release.mkdir()
        for name in tool.shipped:
            if (source / name).is_dir():
                shutil.copytree(source / name, release / name)
            else:
                shutil.copyfile(source / name, release / name)
        runtime = release / RUNTIME_DIR_NAME
        assemble_runtime(runtime, pinned[EMBEDDABLE.name], pinned[FULL_PYTHON.name])
        assembly = Assembly(label, full_commit, source, release, runtime, pinned)
        for step in tool.assembly:
            step(assembly)
        compile_bytecode(release, [*(release / folder for folder in tool.bytecode), runtime / "Lib"])
        leaks = files_naming(release, [str(scratch), str(Path.home())])
        if leaks:
            raise BuildError(f"{len(leaks)} files of the release name the folder it was built in or the builder's "
                             f"home folder: {', '.join(leaks[:10])}")
        say(f"Assembled {len(release_files(release))} files; none names the build folder or the builder's home")
        suite_result = None
        notes: List[str] = []
        destination.mkdir(parents=True)
        transcript = destination / f"suite-{label}.txt"
        if suite:
            suite_result = run_suite(source, runtime, tool.suite, transcript, say=say)
            if not suite_result.matches_ledger:
                raise BuildError("the suite's failures differ from the ledger: "
                                 f"red and not in the ledger {list(suite_result.unexpected)}, "
                                 f"in the ledger and passing {list(suite_result.recovered)}; "
                                 f"see {transcript}")
        else:
            note = f"{tool.suite.skipped}: this build is not verified"
            notes.append(note)
            say(note[0].upper() + note[1:])
        zip_path = destination / f"{root_name}.zip"
        files = write_zip(release, zip_path, label_time(label))
        assets: List[Path] = []
        try:
            for step in tool.after_zip:
                written = step(AfterZip(label, full_commit, source, release, pinned, zip_path, destination))
                assets += checked_assets(destination, written,
                                         [zip_path.name, SUMS_NAME, transcript.name, *(a.name for a in assets)])
        except BaseException:
            # A release whose steps after the zip did not all finish has no sums: leave no zip
            # or asset of it that looks like one ready to publish.
            for path in (zip_path, *assets):
                path.unlink(missing_ok=True)
            raise
    sums = write_sums(destination / SUMS_NAME, [zip_path, *assets])
    digest = sha256_of(zip_path)
    say(f"Release: {zip_path} ({files} files, {zip_path.stat().st_size} bytes)")
    say(f"SHA-256: {digest}")
    for asset in assets:
        say(f"Asset: {asset} ({asset.stat().st_size} bytes), SHA-256 {sha256_of(asset)}")
    return BuildResult(label, full_commit, zip_path, sums, digest, files, tuple(assets), suite_result, tuple(notes))
