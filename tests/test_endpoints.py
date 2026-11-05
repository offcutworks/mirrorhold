"""Endpoints: identity, relative paths, the volume source, resolution, markers, sync roots, drives.

Binds the endpoint contract: a destination is identified by volume identity + path +
marker and never by letter or label; an absent volume reports absent, a duplicated
one refuses; GPT and MBR volumes both get an identity. A volume's drive is the disk it
lies on, named by what the disk carries, or the volume itself, and a person meets it by
its letters."""
import json
import os
import sqlite3
import struct
import sys
import time
import unittest
import uuid
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from support import TempDirTest, endpoint, guid, held, identity, volume

from mirrorhold import exchange
from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.cloud import (DRIVE_ROOTS_FILE, GOOGLE_DRIVE, SyncKind, SyncRoot, drive_sync_roots,
                                       sync_root_endpoints)
from mirrorhold.endpoints.endpoint import (Endpoint, RemoteEndpoint, VolumeIdentity, endpoint_from_document,
                                           gpt_anchor, mbr_anchor, normalise_relative, virtual_anchor)
from mirrorhold.endpoints.marker import (MANAGEMENT_DIR, Marker, MarkerError, MarkerState, check_marker,
                                         marker_check, marker_path, read_marker, write_marker)
from mirrorhold.endpoints.resolution import EndpointError, Presence, endpoint_from_path, resolve
from mirrorhold.endpoints.shared import SharedError, SharedLost, SharedRecord, read_shared, shared_path, write_shared
from mirrorhold.endpoints.system import SystemFolder, system_folder_endpoints, system_folders
from mirrorhold.endpoints.volumes import (Backing, FixedVolumeSource, PartitionInfo, WindowsVolumeSource, anchor_for,
                                          decode_mbr_signature, decode_partition_info, disk_identity, drive_name,
                                          find_by_mount)
from mirrorhold.storage.destination import StorageError, StorageUnanswered
from mirrorhold.storage.drive import DriveStorage


@contextmanager
def missing_once(target: Path):
    """``target`` not there at its first opening, as at the moment a rename takes its name
    away before it gives it to the new file; the openings refused so, listed."""
    real, missed = exchange.open_shared, []

    def opening(path):
        if Path(path) == Path(target) and not missed:
            missed.append(path)
            raise FileNotFoundError(2, "renamed over", str(path))
        return real(path)

    with mock.patch.object(exchange, "open_shared", side_effect=opening):
        yield missed


class IdentityTest(unittest.TestCase):
    def test_anchor_forms(self):
        self.assertEqual(gpt_anchor(guid(1).upper()), f"gpt:{guid(1)}")
        self.assertEqual(mbr_anchor(0x1A2B3C4D, 1048576), "mbr:1a2b3c4d:1048576")
        self.assertEqual(virtual_anchor(guid(2)), f"volume:{guid(2)}")
        for anchor in (gpt_anchor(guid(1)), mbr_anchor(7, 0), virtual_anchor(guid(2))):
            self.assertEqual(VolumeIdentity(anchor, "0000ABCD").anchor, anchor)

    def test_malformed_identity_is_refused_at_its_field(self):
        with self.assertRaises(ContractError) as ctx:
            VolumeIdentity("D:", "0000ABCD")
        self.assertEqual(ctx.exception.where, "anchor")
        with self.assertRaises(ContractError) as ctx:
            VolumeIdentity(gpt_anchor(guid(1)), "abc")
        self.assertEqual(ctx.exception.where, "serial")

    def test_document_round_trip_and_error_location(self):
        e = Endpoint(identity(3), "Photos\\2024")
        self.assertEqual(Endpoint.from_document(e.to_document()), e)
        doc = e.to_document()
        doc["volume"]["serial"] = "nothex!!"
        with self.assertRaises(ContractError) as ctx:
            Endpoint.from_document(doc)
        self.assertEqual(ctx.exception.where, "volume.serial")


class RelativePathTest(unittest.TestCase):
    def test_normal_form(self):
        self.assertEqual(normalise_relative("/Photos/2024/"), "Photos\\2024")
        self.assertEqual(normalise_relative("\\"), "")
        self.assertEqual(normalise_relative("\u3057\u3083\u3057\u3093\\e\u0301"), "\u3057\u3083\u3057\u3093\\e\u0301")

    def test_refusals(self):
        for bad in ("a\\..\\b", "a\\.\\b", "a\\\\b", "a:b", "x\\name.", "x\\name ", "what?"):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                normalise_relative(bad)

    def test_containment_is_case_insensitive_and_component_wise(self):
        v = identity(1)
        root, photos, inner = Endpoint(v, ""), Endpoint(v, "Photos"), Endpoint(v, "photos\\2024")
        self.assertTrue(root.contains(photos))
        self.assertTrue(photos.contains(inner))
        self.assertTrue(photos.contains(Endpoint(v, "PHOTOS")))
        self.assertFalse(photos.contains(Endpoint(v, "Photos2")))
        self.assertFalse(inner.contains(photos))
        self.assertTrue(inner.overlaps(photos))
        self.assertFalse(photos.contains(Endpoint(identity(2), "Photos")))

    def test_unicode_forms_stay_distinct(self):
        v = identity(1)
        self.assertFalse(Endpoint(v, "\u00e9").contains(Endpoint(v, "e\u0301")))


class DecoderTest(unittest.TestCase):
    """The two disk-query buffers, built from the documented winioctl.h layouts."""

    @staticmethod
    def partition_info(style, offset, gpt_id=None):
        raw = bytearray(144)
        struct.pack_into("<I", raw, 0, style)
        struct.pack_into("<q", raw, 8, offset)
        if gpt_id:
            raw[48:64] = uuid.UUID(gpt_id).bytes_le
        return bytes(raw)

    def test_gpt_partition(self):
        info = decode_partition_info(self.partition_info(1, 16777216, guid(9)))
        self.assertEqual((info.style, info.starting_offset, info.gpt_partition_id), (1, 16777216, guid(9)))
        self.assertEqual(anchor_for(info, None, guid(5)), (gpt_anchor(guid(9)), Backing.PARTITION))

    def test_mbr_partition_uses_disk_signature_and_offset(self):
        info = decode_partition_info(self.partition_info(0, 1048576))
        layout = bytearray(48)
        struct.pack_into("<III", layout, 0, 0, 1, 0xCAFEBABE)
        signature = decode_mbr_signature(bytes(layout))
        self.assertEqual(signature, 0xCAFEBABE)
        self.assertEqual(anchor_for(info, signature, guid(5)), ("mbr:cafebabe:1048576", Backing.PARTITION))

    def test_gpt_layout_has_no_mbr_signature(self):
        layout = bytearray(48)
        struct.pack_into("<I", layout, 0, 1)
        self.assertIsNone(decode_mbr_signature(bytes(layout)))

    def test_no_partition_is_a_virtual_volume(self):
        self.assertEqual(anchor_for(None, None, guid(5)), (virtual_anchor(guid(5)), Backing.VIRTUAL))
        # An MBR answer without a signature cannot anchor on the disk either.
        self.assertEqual(anchor_for(PartitionInfo(0, 0, None), None, guid(5))[1], Backing.VIRTUAL)


class VolumeSourceTest(unittest.TestCase):
    def test_fixed_source_reports_what_it_was_given(self):
        a, b = volume(1, "D"), volume(2, "E")
        scan = FixedVolumeSource((a, b), (("\\\\?\\Volume{x}\\", "locked"),)).scan()
        self.assertEqual(scan.volumes, (a, b))
        self.assertEqual(scan.unavailable, (("\\\\?\\Volume{x}\\", "locked"),))

    def test_find_by_mount_takes_the_longest_mount_point(self):
        c = volume(1, "C")
        mounted = volume(2, mounts=("C:\\mnt\\disk\\",))
        found = find_by_mount((c, mounted), "c:\\MNT\\disk\\photos")
        self.assertEqual((found[0], found[1]), (mounted, "photos"))
        self.assertEqual(find_by_mount((c, mounted), "C:\\Users")[0], c)
        self.assertEqual(find_by_mount((c,), "\\\\?\\C:\\long\\path")[1], "long\\path")

    def test_letterless_volume_by_device_path(self):
        member = volume(4, None)
        found = find_by_mount((member,), member.device + "Folder")
        self.assertEqual((found[0], found[1]), (member, "Folder"))

    @unittest.skipUnless(sys.platform == "win32", "WindowsVolumeSource needs Windows")
    def test_live_windows_source_reads_metadata_only(self):
        """Live path: the real source on this machine. Reads identity, capacity and
        root names only; asserts nothing about any particular volume except the one
        holding this suite."""
        from mirrorhold.endpoints.volumes import WindowsVolumeSource
        scan = WindowsVolumeSource().scan()
        self.assertTrue(scan.volumes)
        here = find_by_mount(scan.volumes, __file__)
        self.assertIsNotNone(here, "the volume holding this suite was not found")
        vol = here[0]
        self.assertEqual(vol.backing, Backing.PARTITION)
        self.assertIn(vol.identity.kind, ("gpt", "mbr"))
        self.assertGreater(vol.capacity_bytes, 0)
        identities = [v.identity for v in scan.volumes]
        self.assertEqual(len(identities), len(set(identities)), "two volumes reported one identity")


class ResolutionTest(TempDirTest):
    def test_present_absent_ambiguous(self):
        d = volume(1, "D")
        target = endpoint(d, "Backup")
        present = resolve(target, (d, volume(2, "E")))
        self.assertIs(present.presence, Presence.PRESENT)
        self.assertEqual(str(present.location), "D:\\Backup")
        self.assertIs(resolve(target, (volume(2, "E"),)).presence, Presence.ABSENT)
        clone = volume(1, "F")  # same partition GUID and serial: a sector-level clone
        ambiguous = resolve(target, (d, clone))
        self.assertIs(ambiguous.presence, Presence.AMBIGUOUS)
        self.assertIsNone(ambiguous.location)
        self.assertEqual(len(ambiguous.candidates), 2)

    def test_letter_and_label_play_no_part(self):
        d = volume(1, "D", label="Backup")
        moved = volume(1, "K", label="Renamed")
        self.assertEqual(str(resolve(endpoint(d, "x"), (moved,)).location), "K:\\x")

    def test_reformatted_volume_no_longer_matches(self):
        d = volume(1, "D")
        reformatted = volume(1, "D", serial="DEADBEEF")
        self.assertIs(resolve(endpoint(d), (reformatted,)).presence, Presence.ABSENT)

    def test_a_locked_volume_is_absent(self):
        """A locked BitLocker volume is not in the scan's volume list at all."""
        d = volume(1, "D")
        scan = FixedVolumeSource((), (("\\\\?\\Volume{locked}\\", "access denied"),)).scan()
        self.assertIs(resolve(endpoint(d), scan.volumes).presence, Presence.ABSENT)

    def test_endpoint_from_path(self):
        d = volume(1, "D")
        self.assertEqual(endpoint_from_path("D:\\Work\\Docs\\", (d,)), Endpoint(d.identity, "Work\\Docs"))
        self.assertEqual(endpoint_from_path("D:\\", (d,)), Endpoint(d.identity, ""))
        with self.assertRaises(EndpointError):
            endpoint_from_path("\\\\nas\\share\\x", (d,))
        with self.assertRaises(EndpointError):
            endpoint_from_path("Z:\\x", (d,))
        with self.assertRaises(EndpointError):
            endpoint_from_path("D:\\bad|name", (d,))


class _ReadRefused(DriveStorage):
    """A destination whose files cannot be read, as a remote's engine fails a read
    (``StorageError``); ``answers``: whether it still answers for an entry."""

    def __init__(self, folder, *, answers: bool):
        super().__init__(folder)
        self.answers = answers

    def read_file(self, relative: str) -> bytes:
        raise StorageError("the sync engine did not answer: invented")

    def entry(self, relative: str):
        if not self.answers:
            raise StorageError("the sync engine did not answer: invented")
        return super().entry(relative)


class MarkerTest(TempDirTest):
    def test_missing_match_mismatch_unreadable(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.MISSING)
        write_marker(dest, Marker("a" * 32, "b" * 32))
        self.assertEqual(read_marker(dest), Marker("a" * 32, "b" * 32))
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.MATCH)
        self.assertIs(check_marker(dest, "a" * 32, "c" * 32), MarkerState.MISMATCH)
        self.assertIs(check_marker(dest, "f" * 32, "b" * 32), MarkerState.MISMATCH)
        marker_path(dest).write_text("{not json", encoding="utf-8")
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.UNREADABLE)
        marker_path(dest).write_text(json.dumps({"format": "other", "job_id": "x", "marker_id": "y"}),
                                     encoding="utf-8")
        self.assertIs(check_marker(dest, "x", "y"), MarkerState.UNREADABLE)

    def test_a_marker_met_as_a_run_renames_a_new_one_over_it_is_read_not_missed(self):
        """A look that meets the moment the marker's name is away looks again, as it does
        for a shared destination's record, rather than finding the folder without one."""
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        with missing_once(marker_path(dest)) as missed:
            self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.MATCH)
        self.assertEqual(len(missed), 1)

    def test_a_marker_that_is_not_there_is_told_so_once_looked_for_again(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        started = time.monotonic()
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.MISSING)
        self.assertGreaterEqual(time.monotonic() - started, exchange.RENAME_PATIENCE)

    def test_a_marker_whose_bytes_are_not_utf8_is_unreadable(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        marker_path(dest).write_bytes(b'{"format": "mirrorhold-marker\xff\xfe", "job_id": "a"}')
        with self.assertRaises(MarkerError):
            read_marker(dest)
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.UNREADABLE)

    def test_a_remote_that_stops_answering_as_its_marker_is_read_is_told_as_the_remote(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        with self.assertRaises(StorageError) as raised:
            marker_check(_ReadRefused(dest, answers=False), "a" * 32, "b" * 32)
        self.assertEqual(raised.exception.strerror, "the sync engine did not answer: invented")

    def test_a_marker_read_given_no_whole_answer_is_the_remote_though_it_answers_next(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        storage = _ReadRefused(dest, answers=True)
        broken = StorageUnanswered("the sync engine did not answer: broken off")
        with mock.patch.object(storage, "read_file", side_effect=broken), self.assertRaises(StorageUnanswered):
            marker_check(storage, "a" * 32, "b" * 32)

    def test_a_marker_a_remote_will_not_serve_while_it_answers_is_unreadable(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        storage = _ReadRefused(dest, answers=True)
        with self.assertRaises(MarkerError):
            read_marker(storage)
        self.assertIs(check_marker(storage, "a" * 32, "b" * 32), MarkerState.UNREADABLE)

    def test_the_marker_names_its_schema_and_one_without_it_reads_as_schema_one(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        self.assertEqual(json.loads(marker_path(dest).read_text(encoding="utf-8")),
                         {"format": "mirrorhold-marker", "schema_version": 1, "job_id": "a" * 32,
                          "marker_id": "b" * 32})
        marker_path(dest).write_text(json.dumps({"format": "mirrorhold-marker", "job_id": "a" * 32,
                                                 "marker_id": "b" * 32}), encoding="utf-8")
        self.assertEqual(read_marker(dest), Marker("a" * 32, "b" * 32))
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.MATCH)

    def test_a_schema_this_copy_does_not_know_is_unreadable(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        for schema in (2, 0, "1", True, None):
            with self.subTest(schema=schema):
                marker_path(dest).parent.mkdir(exist_ok=True)
                marker_path(dest).write_text(json.dumps({"format": "mirrorhold-marker", "schema_version": schema,
                                                         "job_id": "a" * 32, "marker_id": "b" * 32}),
                                             encoding="utf-8")
                self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.UNREADABLE)
        marker_path(dest).write_text(json.dumps({"format": "mirrorhold-marker", "schema_version": 1,
                                                 "job_id": "a" * 32, "marker_id": "b" * 32, "extra": 1}),
                                     encoding="utf-8")
        self.assertIs(check_marker(dest, "a" * 32, "b" * 32), MarkerState.UNREADABLE)

    @unittest.skipUnless(sys.platform == "win32", "hidden attribute is Windows-only")
    def test_management_folder_is_hidden(self):
        import os
        dest = self.tmp / "dest"
        dest.mkdir()
        write_marker(dest, Marker("a" * 32, "b" * 32))
        self.assertTrue(os.stat(dest / MANAGEMENT_DIR).st_file_attributes & 0x2)
        self.assertFalse(any(p.name.endswith(".writing") for p in (dest / MANAGEMENT_DIR).iterdir()))


class SyncRootTest(unittest.TestCase):
    def test_roots_map_to_endpoints_on_present_volumes(self):
        d, g = volume(1, "D"), volume(7, "G", fs="FAT32", backing=Backing.VIRTUAL)
        roots = (SyncRoot("OneDrive", "D:\\OneDrive - Org"), SyncRoot("BaiduSyncStorageProvider", "G:\\BaiduSyncdisk"),
                 SyncRoot("Gone", "X:\\Sync"))
        mapped = sync_root_endpoints(roots, (d, g))
        self.assertEqual([(r.provider, e) for r, e in mapped],
                         [("OneDrive", Endpoint(d.identity, "OneDrive - Org")),
                          ("BaiduSyncStorageProvider", Endpoint(g.identity, "BaiduSyncdisk"))])
        self.assertEqual({r.kind for r, _ in mapped}, {SyncKind.DEVICES})

    def test_an_entry_naming_no_folder_maps_to_none(self):
        d = volume(1, "D")
        self.assertEqual(sync_root_endpoints((SyncRoot(GOOGLE_DRIVE, "", problem="unreadable"),), (d,)), ())


DRIVE_ROOTS_TABLE = ("CREATE TABLE roots (root_id INTEGER PRIMARY KEY, metadata BLOB, media_id TEXT NOT NULL, "
                     "title TEXT NOT NULL, root_path TEXT NOT NULL, account_token TEXT NOT NULL, "
                     "sync_type INTEGER NOT NULL, destination INTEGER NOT NULL, medium INTEGER NOT NULL, "
                     "state INTEGER NOT NULL, one_shot BOOL NOT NULL, is_my_drive BOOL NOT NULL, doc_id TEXT NOT NULL, "
                     "last_seen_absolute_path TEXT NOT NULL, UNIQUE (media_id, root_path, destination))")
"""The table as Google Drive for desktop 128 keeps it (measured on the development machine, 2026-10-28)."""


class DriveRootsTest(TempDirTest):
    """Google Drive for desktop's own list of the folders it synchronises: read from a
    database the test writes, by volume where a row names one, its My Drive flag giving
    the kind; a list that cannot be read is said, never taken for an empty one."""

    def setUp(self):
        super().setUp()
        self.data = self.tmp / "DriveFS"
        self.data.mkdir()

    def write(self, rows, table=DRIVE_ROOTS_TABLE):
        with closing(sqlite3.connect(self.data / DRIVE_ROOTS_FILE)) as connection:
            connection.execute(table)
            for n, (media, relative, my_drive, last_seen) in enumerate(rows, 1):
                connection.execute("INSERT INTO roots VALUES (?, NULL, ?, ?, ?, 'account', 1, 1, 1, 2, 0, ?, '', ?)",
                                   (n, media, relative, relative, my_drive, last_seen))
            connection.commit()

    def test_no_data_folder_means_drive_is_not_used(self):
        self.assertEqual(drive_sync_roots(self.tmp / "absent"), ())

    def test_folders_are_read_by_volume_and_kind(self):
        e, m = volume(2, "E"), volume(3, "M")
        self.write([(guid(2).upper(), "Backups/Recipes", 0, "X:\\Backups\\Recipes"),
                    (guid(3), "My Drive", 1, "M:\\My Drive"),
                    ("NAS-1", "Share", 0, "Z:\\Share"),
                    ("USB|phone", "Camera", 0, "")])
        roots = drive_sync_roots(self.data)
        self.assertEqual([(r.provider, r.path, r.kind) for r in roots],
                         [(GOOGLE_DRIVE, "X:\\Backups\\Recipes", SyncKind.CLOUD_COPY),
                          (GOOGLE_DRIVE, "M:\\My Drive", SyncKind.DEVICES),
                          (GOOGLE_DRIVE, "Z:\\Share", SyncKind.CLOUD_COPY)])
        self.assertEqual(roots[0].locator, f"\\\\?\\Volume{{{guid(2)}}}\\Backups\\Recipes")
        mapped = sync_root_endpoints(roots, (e, m))
        self.assertEqual([found for _, found in mapped],
                         [Endpoint(e.identity, "Backups\\Recipes"), Endpoint(m.identity, "My Drive")],
                         "a folder is found on its volume whatever letter Drive last saw it at")

    def test_a_folder_never_seen_at_a_path_is_still_found_on_its_volume(self):
        e = volume(2, "E")
        self.write([(guid(2), "Photos", 1, "")])
        (root,) = drive_sync_roots(self.data)
        self.assertEqual((root.path, root.kind), (f"\\\\?\\Volume{{{guid(2)}}}\\Photos", SyncKind.DEVICES))
        self.assertEqual([found for _, found in sync_root_endpoints((root,), (e,))], [Endpoint(e.identity, "Photos")])

    def test_a_folder_on_an_absent_volume_maps_to_none(self):
        self.write([(guid(9), "Backups", 0, "E:\\Backups")])
        self.assertEqual(sync_root_endpoints(drive_sync_roots(self.data), (volume(2, "E"),)), (),
                         "another volume now carrying the letter is not taken for it")

    def test_a_missing_list_is_said(self):
        (root,) = drive_sync_roots(self.data)
        self.assertEqual((root.provider, root.path), (GOOGLE_DRIVE, ""))
        self.assertIn(DRIVE_ROOTS_FILE, root.problem)

    def test_a_list_of_another_shape_is_said(self):
        self.write([], table="CREATE TABLE roots (root_id INTEGER PRIMARY KEY, root_path TEXT)")
        (root,) = drive_sync_roots(self.data)
        self.assertIn("could not be read", root.problem)

    def test_a_file_that_is_not_a_database_is_said(self):
        (self.data / DRIVE_ROOTS_FILE).write_bytes(b"not a database at all" * 100)
        (root,) = drive_sync_roots(self.data)
        self.assertIn("could not be read", root.problem)

    def test_the_list_is_read_without_writing_it(self):
        self.write([(guid(2), "Backups", 0, "E:\\Backups")])
        before = {p.name: p.stat().st_mtime_ns for p in self.data.iterdir()}
        drive_sync_roots(self.data)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.data.iterdir()}, before)


class SharedMarkerTest(TempDirTest):
    """A shared destination's marker takes the schema that says so; a whole one's keeps the
    schema every copy of the tool reads."""

    def test_a_shared_destinations_marker_is_schema_two(self):
        dest = self.tmp / "dest"
        write_marker(dest, Marker("a" * 32, "b" * 32, shared=True))
        self.assertEqual(json.loads(marker_path(dest).read_text(encoding="utf-8")),
                         {"format": "mirrorhold-marker", "schema_version": 2, "job_id": "a" * 32,
                          "marker_id": "b" * 32, "content": "shared"})
        self.assertEqual(marker_check(dest, "a" * 32, "b" * 32), (MarkerState.MATCH, Marker("a" * 32, "b" * 32, True)))
        self.assertEqual(marker_check(dest, "a" * 32, "c" * 32), (MarkerState.MISMATCH, None))

    def test_schema_two_reads_whole_or_shared_and_nothing_else(self):
        dest = self.tmp / "dest"
        write_marker(dest, Marker("a" * 32, "b" * 32))
        for content, expected in (("whole", MarkerState.MATCH), ("shared", MarkerState.MATCH),
                                  ("half", MarkerState.UNREADABLE), (None, MarkerState.UNREADABLE)):
            with self.subTest(content=content):
                marker_path(dest).write_text(json.dumps({"format": "mirrorhold-marker", "schema_version": 2,
                                                         "job_id": "a" * 32, "marker_id": "b" * 32,
                                                         "content": content}), encoding="utf-8")
                self.assertIs(check_marker(dest, "a" * 32, "b" * 32), expected)
        self.assertFalse(Marker.from_document({"format": "mirrorhold-marker", "schema_version": 2,
                                               "job_id": "a" * 32, "marker_id": "b" * 32, "content": "whole"}).shared)


class SharedRecordTest(TempDirTest):
    def test_round_trip_names_as_ntfs_spells_them(self):
        dest = self.tmp / "dest"
        record = SharedRecord("a" * 32, "b" * 32, ("Photos", "bad\udc80name.txt"), ("Photos\\old.jpg",))
        write_shared(dest, record)
        self.assertEqual(read_shared(dest, "a" * 32, "b" * 32), record)
        shared_path(dest).read_bytes().decode("ascii")
        self.assertFalse(any(p.name.endswith(".writing") for p in (dest / MANAGEMENT_DIR).iterdir()))

    def test_a_record_that_cannot_be_used_is_refused(self):
        dest = self.tmp / "dest"
        with self.assertRaises(SharedError):
            read_shared(dest, "a" * 32, "b" * 32)
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with self.assertRaises(SharedError):
            read_shared(dest, "a" * 32, "c" * 32)
        good = SharedRecord("a" * 32, "b" * 32, ("Photos",), ("Photos\\x",)).to_document()
        for field, value in (("left", ["Music\\x"]), ("own", ["Photos\\x"]), ("own", ["Photos", "photos"]),
                             ("left", ["Photos\\\\x"]), ("schema_version", 2), ("format", "x")):
            with self.subTest(field=field, value=value):
                shared_path(dest).write_text(json.dumps(dict(good, **{field: value})), encoding="utf-8")
                with self.assertRaises(SharedError):
                    read_shared(dest, "a" * 32, "b" * 32)


class SharedRecordLossTest(TempDirTest):
    """A record that says nothing any more about what is the job's is lost; one another
    program holds, or a remote answering will not serve, is intact and only cannot be read
    now; a remote that stops answering as it is read is the remote's failure."""

    def test_a_missing_unreadable_or_foreign_record_is_lost(self):
        dest = self.tmp / "dest"
        with self.assertRaises(SharedLost):
            read_shared(dest, "a" * 32, "b" * 32)
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with self.assertRaises(SharedLost):
            read_shared(dest, "a" * 32, "c" * 32)
        for data in (b"\xff\xfe not text", b"{not json", json.dumps({"format": "x"}).encode()):
            with self.subTest(data=data):
                shared_path(dest).write_bytes(data)
                with self.assertRaises(SharedLost):
                    read_shared(dest, "a" * 32, "b" * 32)

    def test_a_record_met_as_a_run_renames_a_new_one_over_it_is_not_lost(self):
        dest = self.tmp / "dest"
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with missing_once(shared_path(dest)) as missed:
            self.assertEqual(read_shared(dest, "a" * 32, "b" * 32).own, ("Photos",))
        self.assertEqual(len(missed), 1)

    @unittest.skipUnless(sys.platform == "win32", "holding a file without sharing")
    def test_a_record_another_program_holds_is_not_lost(self):
        dest = self.tmp / "dest"
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with held(shared_path(dest)):
            with self.assertRaises(SharedError) as raised:
                read_shared(dest, "a" * 32, "b" * 32)
        self.assertNotIsInstance(raised.exception, SharedLost)
        self.assertEqual(read_shared(dest, "a" * 32, "b" * 32).own, ("Photos",))

    def test_a_remote_that_stops_answering_as_the_record_is_read_is_told_as_the_remote(self):
        dest = self.tmp / "dest"
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with self.assertRaises(StorageError) as raised:
            read_shared(_ReadRefused(dest, answers=False), "a" * 32, "b" * 32)
        self.assertEqual(raised.exception.strerror, "the sync engine did not answer: invented")

    def test_a_record_read_given_no_whole_answer_is_the_remote_though_it_answers_next(self):
        dest = self.tmp / "dest"
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        storage = _ReadRefused(dest, answers=True)
        broken = StorageUnanswered("the sync engine did not answer: broken off")
        with mock.patch.object(storage, "read_file", side_effect=broken), self.assertRaises(StorageUnanswered):
            read_shared(storage, "a" * 32, "b" * 32)

    def test_a_record_a_remote_will_not_serve_while_it_answers_cannot_be_read_now(self):
        dest = self.tmp / "dest"
        write_shared(dest, SharedRecord("a" * 32, "b" * 32, ("Photos",)))
        with self.assertRaises(SharedError) as raised:
            read_shared(_ReadRefused(dest, answers=True), "a" * 32, "b" * 32)
        self.assertNotIsInstance(raised.exception, SharedLost)


class SystemFolderEndpointsTest(TempDirTest):
    def test_each_folder_on_a_present_volume_becomes_an_endpoint(self):
        vol = volume(1, mounts=(str(self.tmp) + "\\",))
        here = SystemFolder(str(self.tmp / "Users" / "me"), "a user profile")
        elsewhere = SystemFolder("Q:\\Nowhere", "a folder on no volume present")
        found = system_folder_endpoints((here, elsewhere), (vol,))
        self.assertEqual([(f.words, e) for f, e in found], [("a user profile", endpoint(vol, "Users\\me"))])


class SystemFoldersTest(TempDirTest):
    def test_the_folders_windows_programs_profiles_and_the_tool_need(self):
        profiles = self.tmp / "Users"
        (profiles / "me").mkdir(parents=True)
        (profiles / "Public").mkdir()
        environ = {"SystemRoot": "C:\\Windows", "ProgramFiles": "C:\\Program Files", "ProgramData": "C:\\ProgramData",
                   "USERPROFILE": str(profiles / "me")}
        store = self.tmp / "Config" / "mirrorhold.json"
        found = {f.path: f.words for f in system_folders(store, environ=environ, root=self.tmp / "Tool")}
        self.assertEqual(found["C:\\Windows"], "C:\\Windows, which Windows needs")
        self.assertEqual(found["C:\\Program Files"], "C:\\Program Files, which installed programs need")
        self.assertIn("C:\\ProgramData", found)
        self.assertEqual(found[str(profiles)], f"{profiles}, which holds the user profiles")
        self.assertEqual(found[str(profiles / "me")], f"{profiles / 'me'}, a user profile")
        self.assertIn(str(profiles / "Public"), found)
        self.assertEqual(found[str(self.tmp / "Tool")], f"{self.tmp / 'Tool'}, which MirrorHold needs")
        self.assertIn(str(store.resolve().parent), found)
        self.assertEqual(len(found), 8, "a variable that is not set names nothing")


class DriveTest(unittest.TestCase):
    """A volume's drive: the disk Windows names it as lying on, by the identity the disk
    carries, or the volume itself when no single disk carries it; and its name for a
    person."""

    @staticmethod
    def layout(style, body: bytes) -> bytes:
        return struct.pack("<II", style, 1) + body + b"\x00" * 64

    def test_a_disk_is_named_by_its_gpt_guid_or_its_mbr_signature_else_its_number(self):
        disk = uuid.UUID("5e2b7c40-91d3-4a68-b0f5-6c1e8a3d2f97")
        self.assertEqual(disk_identity(4, self.layout(1, disk.bytes_le)), f"disk:gpt:{disk}")
        self.assertEqual(disk_identity(4, self.layout(0, struct.pack("<I", 0x1A2B3C4D))), "disk:mbr:1a2b3c4d")
        self.assertEqual(disk_identity(4, self.layout(0, struct.pack("<I", 0))), "disk:number:4")
        self.assertEqual(disk_identity(4, self.layout(2, b"")), "disk:number:4")
        self.assertEqual(disk_identity(4, None), "disk:number:4")

    def test_two_letters_on_one_disk_are_one_drive_and_a_volume_no_disk_carries_is_its_own(self):
        c = replace(volume(1, "C"), disk="disk:gpt:a")
        d = replace(volume(2, "D"), disk="disk:gpt:a")
        e = replace(volume(3, "E"), disk="disk:gpt:b")
        cloud = volume(4, "G", backing=Backing.VIRTUAL)
        self.assertEqual(c.drive, d.drive)
        self.assertNotEqual(c.drive, e.drive)
        self.assertEqual(cloud.drive, cloud.identity.anchor)
        self.assertEqual(volume(5, "H").drive, volume(5, "H").identity.anchor, "a volume whose disk is not known")

    def test_a_drive_is_called_by_its_letters_else_its_label(self):
        c = replace(volume(1, "C"), disk="disk:gpt:a")
        d = replace(volume(2, "D"), disk="disk:gpt:a")
        hidden = replace(volume(3, mounts=(), label="Recovery"), disk="disk:gpt:a")
        i = replace(volume(4, "I"), disk="disk:gpt:b")
        pool = replace(volume(5, mounts=("D:\\Pool\\",), label="Members"), disk="disk:gpt:c")
        bare = replace(volume(6, mounts=()), disk="disk:gpt:d")
        volumes = (c, d, hidden, i, pool, bare)
        self.assertEqual(drive_name(volumes, "disk:gpt:a"), "the drive of C: and D:")
        self.assertEqual(drive_name(volumes, "disk:gpt:b"), "drive I:")
        self.assertEqual(drive_name(volumes, "disk:gpt:c"), "drive 'Members'")
        self.assertEqual(drive_name(volumes, "disk:gpt:d"), "a drive with no letter")
        self.assertEqual(drive_name(volumes + (replace(volume(7, "K"), disk="disk:gpt:a"),), "disk:gpt:a"),
                         "the drive of C:, D: and K:")

    @unittest.skipUnless(sys.platform == "win32", "Windows' volumes")
    def test_the_system_drive_lies_on_a_disk_named_by_what_it_carries(self):
        volumes = WindowsVolumeSource().scan().volumes
        system = find_by_mount(volumes, os.environ.get("SystemDrive", "C:") + "\\")
        self.assertIsNotNone(system)
        disk = system[0].disk
        self.assertRegex(disk or "", r"^disk:(gpt:[0-9a-f-]{36}|mbr:[0-9a-f]{8}|number:\d+)$")
        self.assertEqual(system[0].drive, disk)
        for other in volumes:
            if other.backing is Backing.PARTITION:
                self.assertIsNotNone(other.disk, other.describe())


class RemoteEndpointTest(unittest.TestCase):
    def test_a_remote_folder_is_kept_with_slashes_and_its_name_as_rclone_allows_one(self):
        self.assertEqual(RemoteEndpoint("my cloud", "a/b/").folder, "a/b")
        self.assertEqual(RemoteEndpoint("x.y", "/").folder, "/")
        self.assertEqual(RemoteEndpoint("x_y", "/srv/backup").folder, "/srv/backup")
        self.assertEqual(RemoteEndpoint.parse("cloud:C:/Users/me").folder, "C:/Users/me")
        self.assertEqual(RemoteEndpoint.parse("cloud:").describe(), "cloud:")
        for remote, folder in (("C", "x"), ("-x", "x"), ("x ", "x"), ("a:b", "x"), ("", "x"), ("x", "a\\b"),
                               ("x", "a//b"), ("x", "a/../b"), ("x", "a/./b"), ("x", "a\tb")):
            with self.subTest(remote=remote, folder=folder):
                with self.assertRaises(ContractError):
                    RemoteEndpoint(remote, folder)
        with self.assertRaises(ContractError):
            RemoteEndpoint.parse("no colon")

    def test_either_kind_is_decoded_by_its_keys_and_a_remote_resolves_as_no_volume(self):
        remote = RemoteEndpoint("cloud", "W")
        self.assertEqual(endpoint_from_document(remote.to_document()), remote)
        drive = Endpoint(identity(1), "W")
        self.assertEqual(endpoint_from_document(drive.to_document()), drive)
        resolution = resolve(remote, (volume(1, "D"),))
        self.assertEqual((resolution.presence, resolution.location, resolution.volume), (Presence.ABSENT, None, None))


if __name__ == "__main__":
    unittest.main()
