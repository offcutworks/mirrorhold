"""Obtaining the pinned engine (``tools/fetch_engine.py``), without the network.

Release files are built in a temporary folder with digests of their own, and a download
is served by an opener in memory. The one live check compares the pinned executable
digest with the engine in ``vendor/`` when it is there."""
import hashlib
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path

from support import PROJECT, TempDirTest

from mirrorhold.engine.rclone import PINNED_DOWNLOAD, PINNED_EXECUTABLE_SHA256, PINNED_VERSION, pinned_engine_path

_spec = importlib.util.spec_from_file_location("fetch_engine", PROJECT / "tools" / "fetch_engine.py")
fetch_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_engine)

RELEASE = "rclone-v0.0.0-windows-amd64"
ENGINE = b"an engine"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_zip(path: Path, members=None) -> str:
    """Write a release file holding ``members`` (name -> bytes); return its digest."""
    members = members if members is not None else {f"{RELEASE}/rclone.exe": ENGINE,
                                                  f"{RELEASE}/README.txt": b"licence text"}
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"{RELEASE}/", b"")
        for name, data in members.items():
            bundle.writestr(name, data)
    return sha256(path.read_bytes())


class InstallTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vendor = self.tmp / "vendor"
        self.archive = self.tmp / f"{RELEASE}.zip"

    def install(self, archive_sha256: str, executable_sha256: str = sha256(ENGINE)) -> Path:
        return fetch_engine.install_from_zip(self.archive, self.vendor, release=RELEASE,
                                             archive_sha256=archive_sha256, executable_sha256=executable_sha256)

    def test_a_release_file_with_both_digests_is_put_in_place_and_nothing_else_is_left(self):
        executable = self.install(release_zip(self.archive))
        self.assertEqual(executable, self.vendor / RELEASE / "rclone.exe")
        self.assertEqual(executable.read_bytes(), ENGINE)
        self.assertEqual((self.vendor / RELEASE / "README.txt").read_bytes(), b"licence text")
        self.assertEqual([p.name for p in self.vendor.iterdir()], [RELEASE])

    def test_a_release_file_of_another_digest_is_refused_before_anything_is_written(self):
        release_zip(self.archive)
        with self.assertRaises(fetch_engine.FetchError):
            self.install("0" * 64)
        self.assertFalse(self.vendor.exists())

    def test_an_executable_of_another_digest_is_refused_and_its_unpacking_removed(self):
        with self.assertRaises(fetch_engine.FetchError):
            self.install(release_zip(self.archive), executable_sha256="0" * 64)
        self.assertEqual(list(self.vendor.iterdir()), [])

    def test_a_member_outside_the_release_folder_is_refused(self):
        for name in ("../outside.txt", "other/rclone.exe", f"{RELEASE}/../outside.txt", "/rooted.txt",
                     f"{RELEASE}/C:x.txt"):
            with self.subTest(name=name):
                digest = release_zip(self.archive, {f"{RELEASE}/rclone.exe": ENGINE, name: b"x"})
                with self.assertRaises(fetch_engine.FetchError):
                    self.install(digest)
                self.assertFalse((self.vendor / RELEASE).exists())
                self.assertFalse((self.tmp / "outside.txt").exists())

    def test_a_release_file_without_the_executable_or_not_a_zip_is_refused(self):
        digest = release_zip(self.archive, {f"{RELEASE}/README.txt": b"x"})
        with self.assertRaises(fetch_engine.FetchError):
            self.install(digest)
        self.archive.write_bytes(b"not a zip")
        with self.assertRaises(fetch_engine.FetchError):
            self.install(sha256(b"not a zip"))
        self.assertEqual(list(self.vendor.iterdir()), [])


class EnsureTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vendor = self.tmp / "vendor"
        source = self.tmp / "served.zip"
        self.archive_sha256 = release_zip(source)
        self.served = source.read_bytes()
        self.requests = []

    def opener(self, request, timeout):
        self.requests.append((request.full_url, request.get_header("User-agent")))
        return io.BytesIO(self.served)

    def ensure(self, **options):
        return fetch_engine.ensure_engine(self.vendor, opener=self.opener, url="https://example.invalid/r.zip",
                                          release=RELEASE, archive_sha256=self.archive_sha256,
                                          executable_sha256=sha256(ENGINE), **options)

    def test_it_downloads_once_and_leaves_an_engine_in_place_alone(self):
        said = []
        executable, present = self.ensure(say=said.append)
        self.assertEqual((executable.read_bytes(), present), (ENGINE, False))
        self.assertEqual(self.requests, [("https://example.invalid/r.zip", fetch_engine.USER_AGENT)])
        self.assertEqual([p.name for p in self.vendor.iterdir()], [RELEASE])
        self.assertEqual(said, ["Downloading https://example.invalid/r.zip"])
        executable, present = self.ensure(say=said.append)
        self.assertTrue(present)
        self.assertEqual((len(self.requests), len(said)), (1, 1))

    def test_a_release_file_given_is_used_without_a_download(self):
        archive = self.tmp / "given.zip"
        archive.write_bytes(self.served)
        executable, present = self.ensure(archive=archive)
        self.assertEqual((executable.read_bytes(), present, self.requests), (ENGINE, False, []))

    def test_a_failed_download_leaves_nothing(self):
        def failing(request, timeout):
            raise OSError("no route")
        with self.assertRaises(fetch_engine.FetchError):
            fetch_engine.ensure_engine(self.vendor, opener=failing, url="https://example.invalid/r.zip",
                                       release=RELEASE, archive_sha256=self.archive_sha256,
                                       executable_sha256=sha256(ENGINE))
        self.assertEqual(list(self.vendor.iterdir()), [])

    def test_an_engine_in_place_with_another_digest_is_refused(self):
        (self.vendor / RELEASE).mkdir(parents=True)
        (self.vendor / RELEASE / "rclone.exe").write_bytes(b"another engine")
        with self.assertRaises(fetch_engine.FetchError):
            self.ensure()
        self.assertEqual(self.requests, [])


class PinTest(unittest.TestCase):
    def test_the_download_names_the_pinned_version(self):
        self.assertIn(f"/{PINNED_VERSION}/rclone-{PINNED_VERSION}-windows-amd64.zip", PINNED_DOWNLOAD)

    @unittest.skipUnless(pinned_engine_path().is_file(), "the pinned engine is not in vendor/")
    def test_the_pinned_executable_digest_is_the_engine_in_vendor(self):
        self.assertEqual(fetch_engine.sha256_of(pinned_engine_path()), PINNED_EXECUTABLE_SHA256)


if __name__ == "__main__":
    unittest.main()
