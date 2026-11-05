"""The destination-storage seam's drive kind (``mirrorhold.storage.drive``): a destination
folder of this computer, as every unit reaching a destination's files reaches it.

Each operation is checked against the core's own way with a drive: the listing's, the
exchange's writes, a move as a rename, nothing followed through a link. The remote kind is
checked against the pinned engine's loopback remotes in ``test_engine_contract``
(``RemoteStorageTest``).
"""
import os
import sys
import unittest
from pathlib import Path

from support import TempDirTest, volume

from mirrorhold.endpoints.marker import Marker, MarkerState, marker_check, read_marker, write_marker
from mirrorhold.endpoints.shared import SharedRecord, read_shared, write_shared
from mirrorhold.planning.listing import Occupancy, Side, list_tree
from mirrorhold.storage.destination import MANAGEMENT_DIR, DestinationStorage
from mirrorhold.storage.drive import DriveStorage, destination_storage

WINDOWS = sys.platform == "win32"
JOB, MARKER = "a" * 32, "b" * 32


def write(root: Path, relative: str, data: bytes = b"x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def junction(link: Path, target: Path) -> None:
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


class DriveStorageTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.dst = self.tmp / "dst"
        self.dst.mkdir()
        self.storage = DriveStorage(self.dst, volume(1, "E"))

    def test_a_folder_of_this_computer_is_a_drive_storage_and_a_storage_is_itself(self):
        self.assertIsInstance(destination_storage(self.dst), DriveStorage)
        self.assertIsInstance(destination_storage(str(self.dst)), DriveStorage)
        self.assertIs(destination_storage(self.storage), self.storage)
        self.assertIsInstance(self.storage, DestinationStorage)

    def test_it_names_its_folder_and_the_paths_below_it(self):
        self.assertEqual(self.storage / "a.txt", self.dst / "a.txt")
        self.assertEqual(self.storage.local_folder, self.dst)
        self.assertEqual(self.storage.locate("x\\y.txt"), self.dst / "x" / "y.txt")
        self.assertEqual(self.storage.describe(), str(self.dst))
        self.assertEqual(self.storage.describe("x\\y.txt"), str(self.dst / "x" / "y.txt"))
        self.assertIsNone(self.storage.configuration)

    def test_times_compare_within_the_volumes_file_system_and_space_is_the_volumes(self):
        self.assertEqual(self.storage.time_window_ns, 0)
        self.assertEqual(DriveStorage(self.dst, volume(2, "F", fs="FAT32")).time_window_ns, 2_000_000_000)
        self.assertEqual(DriveStorage(self.dst, volume(3, "G", fs="exFAT")).time_window_ns, 10_000_000)
        self.assertEqual(self.storage.space(), (500 * 1024 ** 3, 1000 * 1024 ** 3))
        self.assertIsNone(DriveStorage(self.dst).space())
        self.assertEqual(DriveStorage(self.dst).time_window_ns, 0)

    def test_entries_are_read_as_themselves(self):
        write(self.dst, "a.txt", b"12345")
        (self.dst / "sub").mkdir()
        self.assertIsNone(self.storage.entry("none.txt"))
        found = self.storage.entry("a.txt")
        self.assertEqual((found.name, found.is_dir, found.is_link, found.size), ("a.txt", False, False, 5))
        self.assertEqual(found.mtime_ns, os.stat(self.dst / "a.txt").st_mtime_ns)
        self.assertTrue(self.storage.exists("sub") and self.storage.is_folder("sub") and self.storage.is_folder())
        self.assertFalse(self.storage.is_folder("a.txt") or self.storage.exists("none"))
        self.assertEqual(sorted((e.name, e.is_dir) for e in self.storage.entries("")), [("a.txt", False), ("sub", True)])
        with self.assertRaises(FileNotFoundError):
            self.storage.entries("none")

    @unittest.skipUnless(WINDOWS, "a junction")
    def test_a_link_is_never_followed(self):
        target = self.tmp / "elsewhere"
        write(target, "inside.txt")
        junction(self.dst / "link", target)
        self.assertTrue(self.storage.entry("link").is_link)
        self.assertTrue(self.storage.file_entry(["link", "inside.txt"]).is_link, "the link on the way")
        self.assertEqual(self.storage.folder_problem("link"), "link is a link or junction; nothing in it is deleted")
        self.assertEqual(self.storage.measure(""), (0, 0))
        (self.dst / "batch").mkdir()
        junction(self.dst / "batch" / "inner", target)
        self.assertIsNone(self.storage.delete_tree("batch"))
        self.assertFalse((self.dst / "batch").exists())
        self.assertTrue((target / "inside.txt").is_file(), "what the link inside pointed at is untouched")

    def test_a_folder_that_is_a_file_or_missing_is_said_so(self):
        write(self.dst, "plain")
        self.assertEqual(self.storage.folder_problem("plain"), "plain is not a folder; nothing is deleted")
        self.assertIsNone(self.storage.folder_problem("none"))
        self.assertIsNone(self.storage.folder_problem(""))

    def test_the_listing_and_the_occupancy_are_the_folders(self):
        self.assertIs(self.storage.occupancy(drive_root=False), Occupancy.EMPTY)
        write(self.dst, f"{MANAGEMENT_DIR}\\marker.json")
        self.assertIs(self.storage.occupancy(drive_root=False), Occupancy.EMPTY)
        write(self.dst, "d\\b.txt", b"bb")
        listing = self.storage.list_tree(drive_root=False)
        direct = list_tree(self.dst, Side.DESTINATION, drive_root=False)
        self.assertEqual((listing.files, listing.dirs, listing.side), (direct.files, direct.dirs, Side.DESTINATION))
        self.assertIs(self.storage.occupancy(drive_root=False), Occupancy.OCCUPIED)
        self.assertIs(DriveStorage(self.tmp / "none").occupancy(drive_root=False), Occupancy.MISSING)

    def test_files_are_written_whole_read_whole_by_their_head_and_as_a_stream(self):
        self.storage.write_file("x\\y.bin", b"0123456789")
        self.assertEqual(self.storage.read_file("x\\y.bin"), b"0123456789")
        self.assertEqual(self.storage.read_head("x\\y.bin", 4), b"0123")
        with self.storage.open_file("x\\y.bin") as handle:
            self.assertEqual(handle.read(3) + handle.read(), b"0123456789")
        self.assertEqual([p.name for p in (self.dst / "x").iterdir()], ["y.bin"], "no staging file is left")
        with self.assertRaises(FileNotFoundError):
            self.storage.read_file("none")

    @unittest.skipUnless(WINDOWS, "the hidden attribute")
    def test_the_management_folder_is_made_hidden(self):
        self.storage.make_management_folder()
        self.assertTrue(os.stat(self.dst / MANAGEMENT_DIR).st_file_attributes & 0x2)
        self.storage.make_management_folder()

    def test_moves_keep_the_time_and_make_the_folders_on_the_way_unless_told_not_to(self):
        made = write(self.dst, "a.txt", b"a")
        os.utime(made, ns=(1_600_000_000_000_000_000,) * 2)
        self.storage.move("a.txt", "deep\\er\\a.txt")
        self.assertEqual(os.stat(self.dst / "deep" / "er" / "a.txt").st_mtime_ns, 1_600_000_000_000_000_000)
        with self.assertRaises(FileNotFoundError):
            self.storage.move("deep\\er\\a.txt", "gone\\a.txt", parents=False)
        with self.assertRaises(FileNotFoundError):
            self.storage.move("none.txt", "b.txt")
        self.storage.move("deep", "Deep", parents=False)
        self.assertEqual(os.listdir(self.dst), ["Deep"])

    def test_removals_delete_only_what_they_are_asked_for(self):
        write(self.dst, "tmp.partial")
        self.storage.remove_file("tmp.partial")
        with self.assertRaises(FileNotFoundError):
            self.storage.remove_file("tmp.partial")
        (self.dst / "empty" / "inner").mkdir(parents=True)
        write(self.dst, "held\\inner\\f.txt")
        self.assertTrue(self.storage.remove_empty_tree("empty"))
        self.assertFalse(self.storage.remove_empty_tree("held"))
        self.assertTrue((self.dst / "held" / "inner" / "f.txt").is_file())
        write(self.dst, "batch\\a.txt", b"aaa")
        write(self.dst, "batch\\b\\c.txt", b"cc")
        os.chmod(self.dst / "batch" / "a.txt", 0o444)
        self.assertEqual(self.storage.measure("batch"), (2, 5))
        self.assertIsNone(self.storage.delete_tree("batch"))
        self.assertFalse((self.dst / "batch").exists())
        self.assertIsNotNone(self.storage.delete_tree("batch"), "a folder not there cannot be deleted")


class PolicyThroughStorageTest(TempDirTest):
    """The marker, a shared destination's record and the records' copies, reached through a
    storage, land where they did as paths."""

    def setUp(self):
        super().setUp()
        self.dst = self.tmp / "dst"
        self.dst.mkdir()
        self.storage = DriveStorage(self.dst)

    def test_the_marker_written_through_a_storage_reads_as_a_paths(self):
        self.assertIs(marker_check(self.storage, JOB, MARKER)[0], MarkerState.MISSING)
        where = write_marker(self.storage, Marker(JOB, MARKER))
        self.assertEqual(where, self.dst / MANAGEMENT_DIR / "marker.json")
        self.assertEqual(read_marker(self.dst), Marker(JOB, MARKER))
        self.assertIs(marker_check(self.storage, JOB, MARKER)[0], MarkerState.MATCH)
        (self.dst / MANAGEMENT_DIR / "marker.json").write_text("{not json", encoding="utf-8")
        self.assertIs(marker_check(self.storage, JOB, MARKER)[0], MarkerState.UNREADABLE)

    def test_a_shared_record_round_trips_through_a_storage(self):
        record = SharedRecord(JOB, MARKER, ("Photos",), ("Photos\\old",))
        self.assertEqual(write_shared(self.storage, record), self.dst / MANAGEMENT_DIR / "shared.json")
        self.assertEqual(read_shared(self.dst, JOB, MARKER), record)
        self.assertEqual(read_shared(self.storage, JOB, MARKER), record)


if __name__ == "__main__":
    unittest.main()
