"""Obtain the pinned rclone engine: download the official release file, check it, unpack it.

    python tools/fetch_engine.py [--from ZIP]

from any folder. The release file is downloaded from rclone's own server, or taken from
ZIP when one was downloaded already; its SHA-256 is checked against the digest pinned in
``mirrorhold.engine.rclone``, its release folder is unpacked into a staging folder
inside ``vendor/``, and the executable is checked against its own pinned digest before
the folder is renamed into place. Nothing is put in place unless both digests match, and
an engine already in place is left as it is.

Exit status: 0 the engine is in place, 1 refused or failed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mirrorhold.engine.rclone import (PINNED_DOWNLOAD, PINNED_DOWNLOAD_SHA256,  # noqa: E402
                                      PINNED_EXECUTABLE_SHA256, PINNED_RELEASE)
from mirrorhold.locations import vendor_dir  # noqa: E402

EXECUTABLE = "rclone.exe"
CHUNK = 1 << 20
TIMEOUT = 60.0
USER_AGENT = "mirrorhold-fetch-engine/1.0.0"
"""rclone's download server refuses Python's default identity (measured: HTTP 403 for
``Python-urllib/3.10``, 200 for this one), so every request names the tool."""

Opener = Callable[..., object]


class FetchError(Exception):
    """The engine could not be put in place; the message says why."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, *, opener: Opener = urllib.request.urlopen) -> None:
    """Write what ``url`` serves into ``target``."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=TIMEOUT) as response, open(target, "wb") as handle:
            shutil.copyfileobj(response, handle, CHUNK)
    except OSError as exc:
        raise FetchError(f"the download of {url} failed: {exc}") from None


def _member_parts(name: str, release: str) -> Tuple[str, ...]:
    """A member's path inside the release folder, refusing any path that leaves it."""
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or parts[0] != release or ".." in parts or any(":" in p for p in parts):
        raise FetchError(f"the release file holds {name!r}, outside its release folder {release}")
    return parts


def install_from_zip(archive: Path, vendor: Path, *, release: str = PINNED_RELEASE,
                     archive_sha256: str = PINNED_DOWNLOAD_SHA256,
                     executable_sha256: str = PINNED_EXECUTABLE_SHA256) -> Path:
    """Unpack ``release``'s folder from ``archive`` into ``vendor`` once both digests
    match, and return the executable's path. Raises FetchError and leaves nothing."""
    found = sha256_of(archive)
    if found != archive_sha256:
        raise FetchError(f"{archive} is not the pinned release file: its SHA-256 is {found}, "
                         f"the pin is {archive_sha256}")
    final = vendor / release
    if final.exists():
        raise FetchError(f"{final} exists already; remove it and run again")
    vendor.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{release}.", suffix=".unpacking", dir=vendor))
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = staging.joinpath(*_member_parts(member.filename, release))
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle, CHUNK)
        executable = staging / release / EXECUTABLE
        if not executable.is_file():
            raise FetchError(f"the release file holds no {release}/{EXECUTABLE}")
        found = sha256_of(executable)
        if found != executable_sha256:
            raise FetchError(f"the release file's {EXECUTABLE} is not the pinned one: its SHA-256 is {found}, "
                             f"the pin is {executable_sha256}")
        os.replace(staging / release, final)
    except zipfile.BadZipFile as exc:
        raise FetchError(f"{archive} is not a readable zip file: {exc}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return final / EXECUTABLE


def ensure_engine(vendor: Path, *, archive: Optional[Path] = None, opener: Opener = urllib.request.urlopen,
                  url: str = PINNED_DOWNLOAD, release: str = PINNED_RELEASE,
                  archive_sha256: str = PINNED_DOWNLOAD_SHA256,
                  executable_sha256: str = PINNED_EXECUTABLE_SHA256,
                  say: Callable[[str], None] = lambda _: None) -> Tuple[Path, bool]:
    """The pinned engine's path, and whether it was in place already. Downloads the
    release file unless ``archive`` names one. Raises FetchError."""
    executable = vendor / release / EXECUTABLE
    if executable.is_file():
        found = sha256_of(executable)
        if found != executable_sha256:
            raise FetchError(f"{executable} is not the pinned engine (SHA-256 {found}); "
                             f"remove {executable.parent} and run again")
        return executable, True
    pins = dict(release=release, archive_sha256=archive_sha256, executable_sha256=executable_sha256)
    if archive is not None:
        return install_from_zip(archive, vendor, **pins), False
    vendor.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f"{release}.", suffix=".zip.downloading", dir=vendor)
    os.close(handle)
    downloaded = Path(name)
    try:
        say(f"Downloading {url}")
        download(url, downloaded, opener=opener)
        return install_from_zip(downloaded, vendor, **pins), False
    finally:
        downloaded.unlink(missing_ok=True)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Obtain the pinned rclone engine into vendor/.")
    parser.add_argument("--from", dest="archive", metavar="ZIP",
                        help=f"use this copy of {PINNED_RELEASE}.zip instead of downloading it")
    args = parser.parse_args(argv)
    try:
        executable, present = ensure_engine(vendor_dir(), archive=Path(args.archive) if args.archive else None,
                                            say=print)
    except FetchError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    print(f"{'Already in place' if present else 'In place'}: {executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
