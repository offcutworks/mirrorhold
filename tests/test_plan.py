"""Planning, the safety gate, confirmation tickets, records, status, locks and admission —
without the engine: listings of temporary folders, plans built from listings, the gate's
verdicts on plans, the files a run keeps beside its store, and which runs and
verifications may work at the same time.

Every expectation is one of the maintainer's design requirements (those for runs at the
same time from the design of 2026-10-09), the maintainer's decision of
2026-10-13 that activities on one drive work one at a time unless the user lets them work
at the same time, or the documented contract of the module under test."""
import ctypes
import json
import os
import subprocess
import sys
import textwrap
import threading
import unittest
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path

from support import GIB, PROJECT, TempDirTest, endpoint, live_activity, volume

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.engine.rclone import InFlight, TransferStats, engine_name, local_name, uncarried
from mirrorhold.jobs.exclusions import EntryFilter, UserPattern
from mirrorhold.jobs.model import DriveTurns, HoldThresholds, new_job
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import RuntimeLayout
from mirrorhold.planning.estimate import DEFAULT_HIGH, DEFAULT_LOW, MiB, estimate, format_duration
from mirrorhold.planning.listing import (Entry, Occupancy, Scope, Side, SkipReason, Skipped, TreeListing, enumerable,
                                         list_tree, occupancy, path_key)
from mirrorhold.planning.plan import build_plan, left_out, time_window_ns
from mirrorhold.planning.sharing import listing_scope, recorded_scope, share
from mirrorhold.planning.signatures import assess_signatures, lost_signature, matches_known
from mirrorhold.records.admission import (Activity, Claim, ClaimedDrive, WaitedFor, admit, blockers, conflict,
                                          gives_way, line_words, place_in_line, run_claim, verification_claim)
from mirrorhold.records.lock import FileLock, holding, is_held_elsewhere
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, RECORD_SCHEMA, Outcome, RunRecord,
                                       capped, destination_state, read_records, throughputs, write_record)
from mirrorhold.records.status import (CancelMode, LiveStatus, Phase, StatusBoard, Wanted, find_run, observe_runs,
                                       reap_interrupted, request)
from mirrorhold.endpoints.marker import MarkerState
from mirrorhold.run.confirmation import (Content, PairConfirmation, TicketError, discard_ticket, load_ticket,
                                         new_ticket, save_ticket)
from mirrorhold.run.control import MIN_RATE_SECONDS, RunControl, StatusPump, time_left
from mirrorhold.run.gate import SPACE_RESERVE_BYTES, Hold, Stop, Verdict, judge
from mirrorhold.run.runner import preview_job

WINDOWS = sys.platform == "win32"
T0 = 1_600_000_000_000_000_000
SECOND = 1_000_000_000


def write(root, rel, data=b"x", mtime_ns=None):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def set_attributes(path, attributes):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    if not k32.SetFileAttributesW(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def listing(side, files=(), dirs=(), skipped=(), present=True):
    """A listing as ``list_tree`` would build it: every file's folders are listed too."""
    result = TreeListing(Path("root"), side, present)
    for path, size, mtime in files:
        result.files[path_key(path)] = Entry(path, size, mtime)
        parts = path.split("\\")[:-1]
        for n in range(1, len(parts) + 1):
            folder = "\\".join(parts[:n])
            result.dirs.setdefault(path_key(folder), folder)
    for folder in dirs:
        result.dirs[path_key(folder)] = folder
    for path, reason in skipped:
        result.skipped.append(Skipped(path, reason))
    return result


def src(*files, **kw):
    return listing(Side.SOURCE, files, **kw)


def dst(*files, **kw):
    return listing(Side.DESTINATION, files, **kw)


# --- listing -------------------------------------------------------------------------

class ListingTest(TempDirTest):
    def test_files_and_folders_without_the_management_folder(self):
        write(self.tmp, "Photos\\a.jpg", b"aa", T0)
        write(self.tmp, ".mirrorhold\\marker.json", b"{}")
        write(self.tmp, "Inner\\Backup\\.mirrorhold\\versions\\x.txt", b"v")
        result = list_tree(self.tmp, Side.SOURCE, drive_root=False)
        self.assertEqual(sorted(e.path for e in result.files.values()), ["Photos\\a.jpg"])
        self.assertEqual(result.files["photos\\a.jpg"], Entry("Photos\\a.jpg", 2, T0))
        self.assertEqual(sorted(result.dirs.values()), ["Inner", "Inner\\Backup", "Photos"])

    def test_the_default_categories_and_the_users_patterns_are_excluded(self):
        for rel in ("~$Report.docx", "~WRL0001.tmp", ".~lock.Sheet.ods#", ".dropbox.cache\\c.bin",
                    "Work\\.stversions\\old.txt", "a.TMP", "Cache\\big.bin", "Work\\Cache\\kept.bin",
                    "Work\\skip.txt", "Report.docx"):
            write(self.tmp, rel)
        entry_filter = EntryFilter(True, True,
                                   tuple(UserPattern.compile(p) for p in ("*.tmp", "/Cache/", "Work/skip.txt")))
        result = list_tree(self.tmp, Side.SOURCE, drive_root=False, entry_filter=entry_filter)
        self.assertEqual(sorted(e.path for e in result.files.values()), ["Report.docx", "Work\\Cache\\kept.bin"])
        relaxed = list_tree(self.tmp, Side.SOURCE, drive_root=False, entry_filter=EntryFilter(False, False, ()))
        self.assertIn("~$report.docx", relaxed.files)
        self.assertIn(".dropbox.cache\\c.bin", relaxed.files)

    def test_a_folder_pattern_excludes_folders_only(self):
        pattern = UserPattern.compile("cache\\")
        self.assertTrue(pattern.matches("x\\Cache", "Cache", True))
        self.assertFalse(pattern.matches("x\\cache", "cache", False))

    def test_what_hides_an_entry_is_the_top_most_excluded_entry_a_listing_meets_on_its_path(self):
        files = ("Tools\\App\\State\\a.txt", "Tools\\App\\code.py", "Cache\\State\\b.txt", "Work\\State")
        for rel in files:
            write(self.tmp, rel)
        entry_filter = EntryFilter(True, True, tuple(UserPattern.compile(p) for p in ("/Tools/App/State/", "/Cache/",
                                                                                     "Work/State/")))
        self.assertEqual(entry_filter.hiding("Tools\\App\\State", True), "Tools\\App\\State")
        self.assertEqual(entry_filter.hiding("tools\\app\\state\\a.txt", False), "tools\\app\\state")
        self.assertEqual(entry_filter.hiding("Cache\\State", True), "Cache", "the folder holding it hides it")
        self.assertIsNone(entry_filter.hiding("Tools\\App", True))
        self.assertIsNone(entry_filter.hiding("Work\\State", False), "a folder pattern does not hide a file")
        self.assertIsNone(entry_filter.hiding("", True), "the root is never hidden")
        listed = list_tree(self.tmp, Side.SOURCE, drive_root=False, entry_filter=entry_filter)
        self.assertEqual(sorted(e.path for e in listed.files.values()), ["Tools\\App\\code.py", "Work\\State"])
        self.assertEqual({rel for rel in files if entry_filter.hiding(rel, False) is None},
                         {e.path for e in listed.files.values()}, "what hides an entry is what the listing skips")

    @unittest.skipUnless(WINDOWS, "file attributes")
    def test_placeholders_are_skipped_on_the_source_and_listed_on_a_destination(self):
        path = write(self.tmp, "cloud.docx", b"c")
        set_attributes(path, 0x1000)
        self.addCleanup(set_attributes, path, 0x80)
        source = list_tree(self.tmp, Side.SOURCE, drive_root=False)
        self.assertEqual(source.files, {})
        self.assertEqual(source.skipped, [Skipped("cloud.docx", SkipReason.PLACEHOLDER)])
        self.assertIn("cloud.docx", list_tree(self.tmp, Side.DESTINATION, drive_root=False).files)

    @unittest.skipUnless(WINDOWS, "file attributes")
    def test_drive_root_artefacts_are_left_out_at_the_root_only(self):
        for rel in ("pagefile.sys", "deep\\pagefile.sys"):
            set_attributes(write(self.tmp, rel), 0x2 | 0x4)
        rooted = list_tree(self.tmp, Side.SOURCE, drive_root=True)
        self.assertEqual(sorted(rooted.files), ["deep\\pagefile.sys"])
        self.assertEqual(len(list_tree(self.tmp, Side.SOURCE, drive_root=False).files), 2)
        for rel in ("pagefile.sys", "deep\\pagefile.sys"):
            set_attributes(self.tmp / rel, 0x80)

    @unittest.skipUnless(WINDOWS, "junctions")
    def test_links_are_not_followed_and_are_recorded(self):
        write(self.tmp, "real\\f.txt")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.tmp / "jn"), str(self.tmp / "real")],
                              capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(made.returncode, 0)
        result = list_tree(self.tmp, Side.SOURCE, drive_root=False)
        self.assertEqual(sorted(result.files), ["real\\f.txt"])
        self.assertEqual(result.skipped, [Skipped("jn", SkipReason.LINK)])

    def test_a_missing_root_is_an_absent_listing(self):
        result = list_tree(self.tmp / "nowhere", Side.DESTINATION, drive_root=False)
        self.assertFalse(result.present)
        self.assertEqual(result.files, {})


class OccupancyTest(TempDirTest):
    """A destination holds nothing when its listing keeps nothing — no file, no folder,
    no link, no unreadable entry — or when its folder does not exist yet. ``occupancy``
    answers from the top level alone, and its answer is the full listing's and the plan's
    in every case."""

    def assert_agrees(self, root, expected, *, drive_root=False, entry_filter=None):
        self.assertIs(occupancy(root, drive_root=drive_root, entry_filter=entry_filter), expected)
        listed = list_tree(root, Side.DESTINATION, drive_root=drive_root, entry_filter=entry_filter)
        self.assertEqual(listed.holds_nothing, expected.holds_nothing)
        self.assertEqual(build_plan(src(), listed).destination_empty, expected.holds_nothing)

    def test_a_missing_or_an_empty_folder_holds_nothing(self):
        self.assert_agrees(self.tmp / "not-yet", Occupancy.MISSING)
        (self.tmp / "empty").mkdir()
        self.assert_agrees(self.tmp / "empty", Occupancy.EMPTY)

    def test_the_management_folder_and_what_the_exclusions_hide_are_nothing(self):
        root = self.tmp / "dst"
        write(root, ".mirrorhold\\job.json", b"{}")
        write(root, "~$Report.docx")
        write(root, "Cache\\big.bin")
        hiding = EntryFilter(True, True, (UserPattern.compile("/Cache/"),))
        self.assert_agrees(root, Occupancy.EMPTY, entry_filter=hiding)
        self.assert_agrees(root, Occupancy.OCCUPIED, entry_filter=EntryFilter(False, False, ()))

    def test_a_file_or_an_empty_folder_is_something(self):
        write(self.tmp / "a", "f.txt")
        self.assert_agrees(self.tmp / "a", Occupancy.OCCUPIED)
        (self.tmp / "b" / "Empty").mkdir(parents=True)
        self.assert_agrees(self.tmp / "b", Occupancy.OCCUPIED)

    @unittest.skipUnless(WINDOWS, "file attributes")
    def test_drive_root_artefacts_are_nothing_at_a_drive_root_only(self):
        root = self.tmp / "vol"
        path = write(root, "pagefile.sys")
        set_attributes(path, 0x2 | 0x4)
        self.addCleanup(set_attributes, path, 0x80)
        self.assert_agrees(root, Occupancy.EMPTY, drive_root=True)
        self.assert_agrees(root, Occupancy.OCCUPIED, drive_root=False)

    @unittest.skipUnless(WINDOWS, "junctions")
    def test_a_link_is_something(self):
        write(self.tmp, "real\\f.txt")
        root = self.tmp / "dst"
        root.mkdir()
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jn"), str(self.tmp / "real")],
                              capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(made.returncode, 0)
        self.assert_agrees(root, Occupancy.OCCUPIED)

    def test_a_file_where_the_folder_should_be_cannot_be_told(self):
        write(self.tmp, "dst")
        self.assertIs(occupancy(self.tmp / "dst", drive_root=False), Occupancy.UNKNOWN)
        self.assertFalse(Occupancy.UNKNOWN.holds_nothing)


# --- the plan ------------------------------------------------------------------------

class PlanTest(unittest.TestCase):
    def test_categories(self):
        plan = build_plan(src(("same.txt", 1, T0), ("changed.txt", 2, T0 + SECOND), ("new.txt", 3, T0)),
                          dst(("same.txt", 1, T0), ("changed.txt", 2, T0), ("gone.txt", 4, T0)))
        self.assertEqual([e.path for e in plan.new], ["new.txt"])
        self.assertEqual([c.source.path for c in plan.changed], ["changed.txt"])
        self.assertEqual([e.path for e in plan.deleted], ["gone.txt"])
        self.assertEqual(plan.unchanged, 1)
        self.assertEqual((plan.files_to_copy, plan.bytes_to_copy, plan.removal_count), (2, 5, 2))
        self.assertEqual(plan.removal_keys(), {"changed.txt", "gone.txt"})
        self.assertEqual(plan.destination_files, 3)

    def test_a_moved_or_renamed_file_is_a_rename_not_a_removal(self):
        plan = build_plan(src(("sorted\\one.jpg", 100, T0), ("album\\two_renamed.jpg", 200, T0 + 1)),
                          dst(("album\\one.jpg", 100, T0), ("album\\two.jpg", 200, T0 + 1)))
        self.assertEqual(sorted((r.old, r.new) for r in plan.renames),
                         [("album\\one.jpg", "sorted\\one.jpg"), ("album\\two.jpg", "album\\two_renamed.jpg")])
        self.assertEqual((plan.new, plan.deleted, plan.removal_count), ((), (), 0))

    def test_a_tie_of_size_and_time_is_broken_by_name_and_otherwise_copied(self):
        plan = build_plan(src(("b\\x.jpg", 5, T0), ("b\\y.jpg", 5, T0)),
                          dst(("a\\x.jpg", 5, T0), ("a\\z.jpg", 5, T0)))
        self.assertEqual([(r.old, r.new) for r in plan.renames], [("a\\x.jpg", "b\\x.jpg")])
        self.assertEqual([e.path for e in plan.new], ["b\\y.jpg"])
        self.assertEqual([e.path for e in plan.deleted], ["a\\z.jpg"])

    def test_a_changed_time_is_not_a_rename(self):
        plan = build_plan(src(("new.jpg", 5, T0 + SECOND)), dst(("old.jpg", 5, T0)))
        self.assertEqual(plan.renames, ())
        self.assertEqual((len(plan.new), len(plan.deleted)), (1, 1))

    def test_names_differing_in_case_alone_are_renamed(self):
        plan = build_plan(src(("photos\\IMG.jpg", 5, T0)), dst(("Photos\\img.JPG", 5, T0)))
        self.assertEqual(plan.dir_case_renames, (("Photos", "photos"),))
        self.assertEqual([(r.old, r.new, r.case_only) for r in plan.renames],
                         [("Photos\\img.JPG", "photos\\IMG.jpg", True)])
        self.assertEqual(plan.unchanged, 1)
        self.assertEqual(plan.removal_count, 0)

    def test_nothing_is_removed_beneath_a_source_path_that_could_not_be_read(self):
        source = src(("ok.txt", 1, T0), skipped=[("locked", SkipReason.UNLISTABLE),
                                                  ("cloud.docx", SkipReason.PLACEHOLDER),
                                                  ("link", SkipReason.LINK), ("odd.bin", SkipReason.UNREADABLE)])
        destination = dst(("ok.txt", 1, T0), ("locked\\inner.txt", 1, T0), ("locked\\deep\\x.txt", 1, T0),
                          ("cloud.docx", 9, T0), ("link\\f.txt", 1, T0), ("odd.bin", 2, T0), ("gone.txt", 1, T0))
        plan = build_plan(source, destination)
        self.assertEqual([e.path for e in plan.deleted], ["gone.txt"])
        self.assertEqual(sorted(e.path for e in plan.kept),
                         ["cloud.docx", "link\\f.txt", "locked\\deep\\x.txt", "locked\\inner.txt", "odd.bin"])
        self.assertEqual(plan.deleted_dirs, ())

    def test_the_engines_leftovers_are_removed_only_where_the_tool_already_owns_the_destination(self):
        destination = dst(("video.bin.4f3a2b1c.partial", 7, T0))
        trusted = build_plan(src(), destination, trust_temporaries=True)
        self.assertEqual((trusted.temporaries, trusted.deleted), (("video.bin.4f3a2b1c.partial",), ()))
        untrusted = build_plan(src(), destination, trust_temporaries=False)
        self.assertEqual(([e.path for e in untrusted.deleted], untrusted.temporaries),
                         (["video.bin.4f3a2b1c.partial"], ()))

    def test_nothing_is_copied_through_a_link_on_the_destination(self):
        plan = build_plan(src(("out\\f.txt", 1, T0), ("in.txt", 1, T0)),
                          dst(skipped=[("out", SkipReason.LINK)]))
        self.assertEqual([e.path for e in plan.new], ["in.txt"])
        self.assertEqual([s.path for s in plan.blocked], ["out\\f.txt"])
        self.assertNotIn("out", plan.dirs_to_create)

    def test_destination_only_folders_move_whole_from_the_topmost(self):
        plan = build_plan(src(("keep\\a.txt", 1, T0), dirs=["empty"]),
                          dst(("keep\\a.txt", 1, T0), ("old\\b.txt", 1, T0), ("old\\deeper\\c.txt", 1, T0)))
        self.assertEqual(plan.deleted_dirs, ("old",))
        self.assertEqual(sorted(e.path for e in plan.deleted), ["old\\b.txt", "old\\deeper\\c.txt"])
        self.assertEqual(plan.dirs_to_create, ("empty",))

    def test_coarse_times_compare_within_their_precision(self):
        self.assertEqual(time_window_ns("NTFS", "NTFS"), 0)
        self.assertEqual(time_window_ns("NTFS", "FAT32"), 2 * SECOND)
        plan = build_plan(src(("a.txt", 1, T0 + SECOND)), dst(("a.txt", 1, T0)), time_window_ns=2 * SECOND)
        self.assertEqual((plan.unchanged, plan.changed), (1, ()))

    def test_a_folder_renamed_over_coarse_times_is_renamed_across_a_precision_boundary(self):
        # T0 is a multiple of two seconds: each pair's times lie within the window but on
        # either side of a two-second boundary.
        plan = build_plan(src(("trip 2024\\a.jpg", 5, T0 - SECOND // 2), ("trip 2024\\b.jpg", 6, T0 + SECOND)),
                          dst(("trip\\a.jpg", 5, T0), ("trip\\b.jpg", 6, T0 + 2 * SECOND)),
                          time_window_ns=2 * SECOND)
        self.assertEqual(sorted((r.old, r.new) for r in plan.renames),
                         [("trip\\a.jpg", "trip 2024\\a.jpg"), ("trip\\b.jpg", "trip 2024\\b.jpg")])
        self.assertEqual((plan.new, plan.deleted, plan.removal_count), ((), (), 0))

    def test_a_tie_within_coarse_times_is_broken_by_name(self):
        plan = build_plan(src(("b\\x.jpg", 5, T0 - SECOND // 2), ("b\\y.jpg", 5, T0 + SECOND // 2)),
                          dst(("a\\x.jpg", 5, T0), ("a\\y.jpg", 5, T0)), time_window_ns=2 * SECOND)
        self.assertEqual(sorted((r.old, r.new) for r in plan.renames),
                         [("a\\x.jpg", "b\\x.jpg"), ("a\\y.jpg", "b\\y.jpg")])
        self.assertEqual((plan.new, plan.deleted), ((), ()))

    def test_a_time_beyond_the_coarse_window_is_not_a_rename(self):
        plan = build_plan(src(("new.jpg", 5, T0 + 3 * SECOND)), dst(("old.jpg", 5, T0)), time_window_ns=2 * SECOND)
        self.assertEqual(plan.renames, ())
        self.assertEqual((len(plan.new), len(plan.deleted)), (1, 1))


# --- signatures ----------------------------------------------------------------------

class SignatureTest(TempDirTest):
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    def test_known_and_unknown_heads(self):
        for head in (self.PNG, b"\xff\xd8\xff\xe0", b"PK\x03\x04", b"%PDF-1.7", b"\x00\x00\x00\x18ftypheic"):
            self.assertTrue(matches_known(head), head)
        for head in (b"BMW notes", b"MZ text", b"plain text", os.urandom(0)):
            self.assertFalse(matches_known(head), head)
        self.assertTrue(lost_signature(self.PNG, b"\x13\x37" * 8))
        self.assertFalse(lost_signature(b"plain", b"\x13\x37" * 8))
        self.assertFalse(lost_signature(self.PNG, self.PNG))

    def test_only_changed_files_that_lost_their_signature_are_named(self):
        for name, before, after in (("photo.png", self.PNG, b"\x9e\x01" * 8), ("note.txt", b"text", b"\x9e\x01" * 8),
                                    ("kept.png", self.PNG, self.PNG + b"more")):
            write(self.tmp / "d", name, before, T0)
            write(self.tmp / "s", name, after, T0 + SECOND)
        plan = build_plan(list_tree(self.tmp / "s", Side.SOURCE, drive_root=False),
                          list_tree(self.tmp / "d", Side.DESTINATION, drive_root=False))

        def read(path):
            return Path(path).read_bytes()[:16]

        self.assertEqual(assess_signatures(plan, self.tmp / "s", self.tmp / "d", read, read), {"photo.png"})


# --- the safety gate -----------------------------------------------------------------

def plan_with(*, deleted=0, changed=0, new=0, keep=20, size=10, lost=()):
    destination = [(f"keep{i}.txt", size, T0) for i in range(keep)]
    destination += [(f"gone{i}.txt", size, T0) for i in range(deleted)]
    destination += [(f"chg{i}.txt", size, T0) for i in range(changed)]
    source = [(f"keep{i}.txt", size, T0) for i in range(keep)]
    source += [(f"chg{i}.txt", size, T0 + SECOND) for i in range(changed)]
    source += [(f"new{i}.txt", size + 1, T0 + 5) for i in range(new)]
    return build_plan(src(*source), dst(*destination)).with_signature_losses(lost)


class GateTest(unittest.TestCase):
    holds = HoldThresholds(count_floor=3, percent=5)
    disk = volume(2, letter="E")

    def verdict(self, plan, **kw):
        kw.setdefault("holds", self.holds)
        kw.setdefault("first_run", False)
        kw.setdefault("destination", self.disk)
        return judge(plan, **kw)

    def test_removals_within_the_bar_proceed(self):
        decision = self.verdict(plan_with(deleted=2, changed=1, keep=40))
        self.assertEqual((decision.verdict, decision.holds), (Verdict.PROCEED, ()))
        self.assertTrue(decision.applies_removals)

    def test_more_than_the_larger_of_floor_and_percentage_is_held(self):
        self.assertEqual(self.verdict(plan_with(deleted=4, keep=40)).holds, (Hold.ABOVE_THRESHOLD,))
        many = plan_with(deleted=6, keep=200)                     # bar = max(3, 5 % of 206) = 10.3
        self.assertEqual(self.verdict(many).verdict, Verdict.PROCEED)

    def test_overwrites_count_as_removals(self):
        self.assertEqual(self.verdict(plan_with(changed=4, keep=40)).holds, (Hold.ABOVE_THRESHOLD,))

    def test_half_of_the_destination_is_held_by_default_and_the_additions_still_apply(self):
        lenient = HoldThresholds(count_floor=1000, percent=100)
        decision = self.verdict(plan_with(deleted=10, keep=10), holds=lenient)
        self.assertEqual((decision.verdict, decision.holds), (Verdict.ADDITIONS_ONLY, (Hold.SHARE_OF_DESTINATION,)))
        self.assertFalse(decision.applies_removals)
        self.assertTrue(decision.applies_additions)

    def test_a_lost_signature_is_held_whatever_the_count(self):
        decision = self.verdict(plan_with(changed=1, keep=40, lost={"chg0.txt"}))
        self.assertEqual(decision.holds, (Hold.LOST_SIGNATURES,))

    def test_a_first_run_waits_whole_and_a_ticket_confirms_it(self):
        plan = plan_with(deleted=8, keep=2)
        waiting = self.verdict(plan, first_run=True)
        self.assertEqual((waiting.verdict, waiting.holds), (Verdict.AWAIT_CONFIRMATION, (Hold.FIRST_RUN,)))
        self.assertTrue(waiting.adoption)
        confirmed = self.verdict(plan, first_run=True, confirmed=plan.removal_keys())
        self.assertEqual(confirmed.verdict, Verdict.PROCEED)
        partly = self.verdict(plan, first_run=True, confirmed=frozenset(list(plan.removal_keys())[:3]))
        self.assertEqual((partly.verdict, partly.holds),
                         (Verdict.AWAIT_CONFIRMATION, (Hold.FIRST_RUN, Hold.NOT_CONFIRMED)))

    def test_confirmed_removals_are_not_judged_again(self):
        plan = plan_with(deleted=30, keep=10)
        self.assertEqual(self.verdict(plan, confirmed=plan.removal_keys()).verdict, Verdict.PROCEED)
        one_more = plan_with(deleted=31, keep=10)
        self.assertEqual(self.verdict(one_more, confirmed=plan.removal_keys()).verdict, Verdict.PROCEED)

    def test_the_share_hold_is_the_jobs_and_can_be_switched_off(self):
        lenient = HoldThresholds(count_floor=1000, percent=100)
        self.assertEqual(self.verdict(plan_with(deleted=10, keep=10), holds=lenient).holds,
                         (Hold.SHARE_OF_DESTINATION,), "half by default")
        thirty = HoldThresholds(count_floor=1000, percent=100, share=30)
        self.assertEqual(self.verdict(plan_with(deleted=3, keep=7), holds=thirty).holds, (Hold.SHARE_OF_DESTINATION,))
        self.assertEqual(self.verdict(plan_with(deleted=2, keep=8), holds=thirty).verdict, Verdict.PROCEED)
        off = HoldThresholds(count_floor=1000, percent=100, share=None)
        self.assertEqual(self.verdict(plan_with(deleted=10, keep=0), holds=off).verdict, Verdict.PROCEED)

    def test_one_edit_of_a_one_file_job_proceeds_only_with_the_share_hold_off(self):
        plan = plan_with(changed=1, keep=0)
        self.assertEqual(self.verdict(plan, holds=HoldThresholds()).holds, (Hold.SHARE_OF_DESTINATION,))
        self.assertEqual(self.verdict(plan, holds=HoldThresholds(share=100)).holds, (Hold.SHARE_OF_DESTINATION,))
        self.assertEqual(self.verdict(plan, holds=HoldThresholds(share=None)).verdict, Verdict.PROCEED)

    def test_a_held_share_is_named_in_words_and_by_its_code(self):
        held = self.verdict(plan_with(deleted=3, keep=7), holds=HoldThresholds(1000, 100, 30))
        self.assertIn("30 % or more of the destination's files would move into versions", held.describe())
        half = self.verdict(plan_with(deleted=10, keep=10), holds=HoldThresholds(1000, 100))
        self.assertIn("half or more of the destination's files would move into versions", half.describe())
        self.assertEqual(half.reasons, ("share-of-destination",))

    def test_a_first_backup_onto_a_destination_holding_nothing_needs_only_the_go_ahead(self):
        plan = build_plan(src(("a.txt", 1, T0)), dst())
        waiting = self.verdict(plan, first_run=True)
        self.assertEqual((waiting.verdict, waiting.holds, waiting.reasons, waiting.adoption),
                         (Verdict.AWAIT_CONFIRMATION, (Hold.FIRST_RUN,), ("first-run",), False))
        self.assertEqual(self.verdict(plan, first_run=True, first_backup=True).verdict, Verdict.PROCEED)
        missing = build_plan(src(("a.txt", 1, T0)), dst(present=False))
        self.assertEqual(self.verdict(missing, first_run=True, first_backup=True).verdict, Verdict.PROCEED)

    def test_the_go_ahead_does_not_cover_a_destination_holding_something(self):
        for holding in (dst(("old.txt", 1, T0)), dst(dirs=("Empty",)), dst(skipped=(("jn", SkipReason.LINK),))):
            decision = self.verdict(build_plan(src(("a.txt", 1, T0)), holding), first_run=True, first_backup=True)
            self.assertEqual((decision.verdict, decision.holds, decision.adoption),
                             (Verdict.AWAIT_CONFIRMATION, (Hold.FIRST_RUN,), True))
            self.assertEqual(decision.reasons, ("first-run", "destination-holds-content"))
            self.assertIn("already holds files or folders", decision.describe())

    def test_a_file_too_large_for_fat32_stops_the_pair(self):
        fat = volume(3, letter="F", fs="FAT32")
        plan = build_plan(src(("big.mkv", 4 * GIB, T0)), dst())
        decision = self.verdict(plan, destination=fat)
        self.assertEqual((decision.verdict, decision.stop), (Verdict.STOP, Stop.FAT32_LARGE_FILE))

    def test_a_copy_that_would_not_fit_stops_the_pair(self):
        plan = plan_with(new=2, keep=40)
        small = volume(4, letter="G", free=SPACE_RESERVE_BYTES)
        self.assertEqual(self.verdict(plan, destination=small).stop, Stop.INSUFFICIENT_SPACE)
        self.assertEqual(self.verdict(plan_with(keep=40), destination=small).verdict, Verdict.PROCEED)

    def test_the_stop_gives_the_free_space_and_what_other_runs_still_write_in_bytes(self):
        small = volume(4, letter="G", free=SPACE_RESERVE_BYTES)
        decision = self.verdict(plan_with(new=2, keep=40), destination=small, reserved=1000)
        self.assertRegex(decision.detail, rf"^the run needs \d+ bytes with its margin and the destination has "
                                          rf"{SPACE_RESERVE_BYTES} bytes free, 1000 bytes of them still to be "
                                          rf"written by other runs in progress$")


# --- the estimate --------------------------------------------------------------------

class EstimateTest(unittest.TestCase):
    def test_without_history_the_basis_is_the_stated_default(self):
        guess = estimate(1500 * MiB, 12.0, [])
        self.assertEqual((guess.basis, guess.low_rate, guess.high_rate), ("default", DEFAULT_LOW, DEFAULT_HIGH))
        self.assertAlmostEqual(guess.shortest_seconds, 10.0)
        self.assertIn("default", guess.describe())
        self.assertIn("listing took 12 s", guess.describe())

    def test_history_gives_the_range_of_the_recent_runs(self):
        guess = estimate(100 * MiB, 1.0, [1 * MiB, 50 * MiB, 80 * MiB, 60 * MiB, 70 * MiB, 90 * MiB])
        self.assertEqual((guess.basis, guess.samples), ("history", 5))
        self.assertEqual((guess.low_rate, guess.high_rate), (50 * MiB, 90 * MiB))
        single = estimate(100 * MiB, 1.0, [40 * MiB])
        self.assertEqual((single.low_rate, single.high_rate), (30 * MiB, 50 * MiB))

    def test_a_cost_per_file_is_added_to_each_end_and_a_preview_counts_none(self):
        size = 5000 * 4096
        guess = estimate(size, 0.0, [], files=5000, file_seconds=(0.0025, 0.015))
        self.assertAlmostEqual(guess.shortest_seconds, size / DEFAULT_HIGH + 12.5)
        self.assertAlmostEqual(guess.longest_seconds, size / DEFAULT_LOW + 75.0)
        self.assertEqual((format_duration(guess.shortest_seconds), format_duration(guess.longest_seconds)),
                         ("13 s", "1 min"))
        empty = estimate(0, 0.0, [80 * MiB, 95 * MiB], files=10, file_seconds=(0.0025, 0.015))
        self.assertAlmostEqual(empty.shortest_seconds, 0.025)
        self.assertAlmostEqual(empty.longest_seconds, 0.15)
        preview = estimate(size, 0.0, [])
        self.assertEqual((preview.files, preview.shortest_seconds, preview.longest_seconds),
                         (0, size / DEFAULT_HIGH, size / DEFAULT_LOW))


# --- tickets, records, status, locks -------------------------------------------------

class TicketTest(TempDirTest):
    def test_save_load_and_discard(self):
        layout = RuntimeLayout(self.tmp)
        ticket = new_ticket("j" * 32, {"m" * 32: PairConfirmation(True, frozenset({"a.txt"}))})
        save_ticket(layout, ticket)
        self.assertEqual(load_ticket(layout, ticket.job_id, ticket.ticket_id), ticket)
        with self.assertRaises(TicketError):
            load_ticket(layout, ticket.job_id, "other")
        discard_ticket(layout, ticket.job_id, "other")
        self.assertEqual(load_ticket(layout, ticket.job_id, ticket.ticket_id), ticket)
        discard_ticket(layout, ticket.job_id, ticket.ticket_id)
        with self.assertRaises(TicketError):
            load_ticket(layout, ticket.job_id, ticket.ticket_id)


def record(outcome=Outcome.COMPLETED, *, marker="m" * 32, started="2026-10-06T10:00:00Z", copied=0, seconds=0.0,
           reasons=()):
    done = dict.fromkeys(DONE_KEYS, 0)
    done["bytes_copied"] = copied
    return RunRecord("r" * 32, "j" * 32, "Job", marker, 1, "manual", started, started, outcome, reasons,
                     planned=dict.fromkeys(PLANNED_KEYS, 0), done=done, left=dict.fromkeys(LEFT_KEYS, 0),
                     transfer_seconds=seconds)


class RecordTest(TempDirTest):
    def test_round_trip_and_the_destinations_copy(self):
        layout = RuntimeLayout(self.tmp / "Config")
        first = record(started="2026-10-06T10:00:00Z")
        second = record(Outcome.HELD, started="2026-10-06T11:00:00Z", reasons=("above-threshold",))
        write_record(layout, second, self.tmp / "dest")
        write_record(layout, first)
        self.assertEqual(read_records(layout, "j" * 32), [first, second])
        copies = list((self.tmp / "dest" / ".mirrorhold" / "runs").iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(json.loads(copies[0].read_text(encoding="utf-8"))["outcome"], "held")

    def test_throughput_needs_enough_bytes(self):
        records = [record(copied=10 * MiB, seconds=1.0), record(copied=200 * MiB, seconds=4.0)]
        self.assertEqual(throughputs(records, "m" * 32), [50 * MiB])

    def test_destination_state(self):
        records = [record(Outcome.HELD, reasons=("first-run",)), record(started="2026-10-07T09:00:00Z")]
        state = destination_state(records, "m" * 32)
        self.assertEqual((state.first_run_confirmed, state.last_success), (True, "2026-10-07T09:00:00Z"))
        self.assertFalse(destination_state(records[:1], "m" * 32).first_run_confirmed)
        self.assertFalse(destination_state(records, "m" * 32, MarkerState.MISSING).first_run_confirmed)

    def test_long_lists_are_capped_with_their_total(self):
        self.assertEqual(capped(range(5), 3), ((0, 1, 2), 5))


class StatusTest(TempDirTest):
    def board(self, layout, run_id="a" * 32):
        status = LiveStatus(run_id, os.getpid(), "j" * 32, "Job", "manual", "2026-10-06T10:00:00Z", pair_index=1,
                            marker_id="m" * 32, pair_started="2026-10-06T10:00:01Z")
        return StatusBoard(layout, status)

    def test_a_live_run_is_seen_alive_and_its_files_go_with_it(self):
        layout = RuntimeLayout(self.tmp)
        board = self.board(layout)
        board.open()
        runs = observe_runs(layout)
        self.assertEqual([(r.run_id, r.alive) for r in runs], [("a" * 32, True)])
        self.assertEqual(find_run(layout, None).run_id, "a" * 32)
        board.close()
        self.assertEqual(observe_runs(layout), [])
        self.assertEqual(list(layout.status_dir.iterdir()), [])

    def test_requests_compose_and_an_immediate_cancel_stays_immediate(self):
        layout = RuntimeLayout(self.tmp)
        board = self.board(layout)
        board.open()
        self.addCleanup(board.close)
        self.assertEqual(request(layout, "a" * 32, pause=True), Wanted(True, None))
        self.assertEqual(request(layout, "a" * 32, cancel=CancelMode.IMMEDIATE), Wanted(True, CancelMode.IMMEDIATE))
        self.assertEqual(request(layout, "a" * 32, cancel=CancelMode.GRACEFUL).cancel, CancelMode.IMMEDIATE)
        self.assertEqual(board.wanted(), Wanted(True, CancelMode.IMMEDIATE))

    def test_a_dead_runs_status_becomes_an_interrupted_record(self):
        layout = RuntimeLayout(self.tmp)
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            status = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-06T10:00:00Z",
                                pair_index=1, marker_id="m" * 32, pair_started="2026-10-06T10:00:01Z")
            status.planned["files_to_copy"] = 5
            status.done["files_copied"] = 2
            StatusBoard(RuntimeLayout(Path({str(self.tmp)!r})), status).open()
            os._exit(0)
        """)
        died = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60)
        self.assertEqual(died.returncode, 0, died.stderr)
        self.assertEqual([r.alive for r in observe_runs(layout)], [False])
        reaped = reap_interrupted(layout)
        self.assertEqual([(r.outcome, r.left["files_to_copy"]) for r in reaped], [(Outcome.INTERRUPTED, 3)])
        self.assertEqual(read_records(layout, "j" * 32), reaped)
        self.assertEqual(observe_runs(layout), [])


class LockTest(TempDirTest):
    def test_a_lock_is_exclusive_until_released(self):
        first, second = FileLock(self.tmp / "run.lock"), FileLock(self.tmp / "run.lock")
        self.assertIsNone(is_held_elsewhere(self.tmp / "run.lock"))
        self.assertTrue(first.try_acquire())
        self.assertFalse(second.try_acquire())
        self.assertTrue(is_held_elsewhere(self.tmp / "run.lock"))
        first.release()
        self.assertTrue(second.try_acquire())
        second.release(remove=True)
        self.assertFalse((self.tmp / "run.lock").exists())

    def test_holding_waits_while_another_holder_has_the_lock(self):
        path = self.tmp / "admission.lock"
        other = FileLock(path)
        self.assertTrue(other.try_acquire())
        entered = threading.Event()

        def hold():
            with holding(path):
                entered.set()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.assertFalse(entered.wait(0.3))
        other.release()
        self.assertTrue(entered.wait(5))
        thread.join(5)
        self.assertFalse(is_held_elsewhere(path))

    def test_two_threads_publishing_one_status_never_collide(self):
        board = StatusBoard(RuntimeLayout(self.tmp), LiveStatus("a" * 32, 1, "j" * 32, "Job", "manual",
                                                                "2026-10-09T10:00:00Z"))
        board.open()
        self.addCleanup(board.close)
        failures = []

        def publish():
            for _ in range(300):
                try:
                    board.publish()
                except OSError as exc:
                    failures.append(exc)

        threads = [threading.Thread(target=publish) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(failures, [])


# --- admission: which runs and verifications work at the same time ---------------------

S, D, E = volume(1, "S"), volume(2, "D"), volume(3, "E")


def activity(kind="run", job="a", reads=(), writes=(), *, admitted=True, queued="2026-10-09T10:00:00.000000Z",
             marker="", run_id=None, paused=False, claim=True):
    return Activity(run_id or f"{job}-{kind}-{marker}-{queued}", kind, job, job.title(), marker, queued, admitted,
                    paused, Claim(tuple(reads), tuple(writes)) if claim else None)


class ConflictTest(unittest.TestCase):
    """What two activities conflict over."""

    def test_folders_conflict_only_where_one_is_written_and_the_two_nest(self):
        work = activity(job="work", reads=[endpoint(S, "Work")], writes=[endpoint(D, "Backup\\Work")])
        photos = activity(job="photos", reads=[endpoint(S, "Photos")], writes=[endpoint(D, "Backup\\Photos")])
        self.assertFalse(conflict(work, photos))
        reader = activity(job="copy", reads=[endpoint(S, "Work")], writes=[endpoint(E, "Copy")])
        self.assertFalse(conflict(work, reader))
        outer = activity(job="outer", reads=[endpoint(D, "backup")], writes=[endpoint(E, "Outer")])
        self.assertTrue(conflict(work, outer))
        self.assertTrue(conflict(outer, work))
        inner = activity(job="inner", reads=[endpoint(S, "Other")], writes=[endpoint(D, "BACKUP\\Work\\Deep")])
        self.assertTrue(conflict(work, inner))
        elsewhere = activity(job="else", reads=[endpoint(E, "Backup\\Work")], writes=[endpoint(S, "Elsewhere")])
        self.assertFalse(conflict(work, elsewhere))

    def test_two_runs_of_one_job_conflict_and_a_verification_of_another_destination_does_not(self):
        first = activity(job="work", reads=[endpoint(S, "Work")], writes=[endpoint(D, "B1")])
        second = activity(job="work", reads=[endpoint(S, "Work")], writes=[endpoint(E, "B2")])
        self.assertTrue(conflict(first, second))
        check_b2 = activity("verification", job="work", reads=[endpoint(S, "Work"), endpoint(E, "B2")], marker="m2")
        self.assertFalse(conflict(first, check_b2))
        self.assertTrue(conflict(second, check_b2))
        check_b1 = activity("verification", job="work", reads=[endpoint(S, "Work"), endpoint(D, "B1")], marker="m1")
        self.assertFalse(conflict(check_b1, check_b2))
        again = activity("verification", job="work", reads=[endpoint(S, "Work"), endpoint(E, "B2")], marker="m2",
                         run_id="again")
        self.assertTrue(conflict(check_b2, again))

    def test_an_activity_without_a_claim_conflicts_with_every_other(self):
        unknown = activity(job="old", claim=False)
        self.assertTrue(conflict(unknown, activity(job="x", reads=[endpoint(E, "X")])))
        doc = LiveStatus("o" * 32, 1, "old", "Old", "manual", "2026-10-09T10:00:00Z", phase=Phase.COPYING).to_document()
        for key in ("claim", "admitted", "queued"):
            del doc[key]                                  # as an older copy of the tool publishes it
        seen = Activity.from_status(doc)
        self.assertEqual((seen.claim, seen.admitted, seen.queued), (None, True, "2026-10-09T10:00:00Z"))
        doc["phase"] = Phase.WAITING.value
        self.assertFalse(Activity.from_status(doc).admitted)

    def test_a_run_claims_its_source_and_the_selected_destinations_present(self):
        job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "B1"), endpoint(E, "B2"), endpoint(D, "B3")])
        claim = run_claim(job, [1, 2], (S, D))
        self.assertEqual((claim.reads, claim.writes), ((endpoint(S, "Work"),), (endpoint(D, "B1"),)))
        self.assertEqual(run_claim(job, [1, 2, 3], (S, D, E)).writes,
                         (endpoint(D, "B1"), endpoint(E, "B2"), endpoint(D, "B3")))
        check = verification_claim(job, 2)
        self.assertEqual((check.reads, check.writes), ((endpoint(S, "Work"), endpoint(E, "B2")), ()))
        self.assertEqual(Claim.from_document(claim.to_document()), claim)
        self.assertIsNone(Claim.from_document({"reads": [{"volume": {}}], "writes": []}))


class TurnOrderTest(unittest.TestCase):
    """Who goes first."""

    def test_an_activity_waits_for_the_admitted_ones_then_the_waiting_ones_that_go_first(self):
        holder = activity(job="h", reads=[endpoint(S, "H")], writes=[endpoint(D, "X")], queued="T3")
        older = activity(job="o", reads=[endpoint(S, "O")], writes=[endpoint(D, "X")], admitted=False, queued="T1")
        me = activity(job="me", reads=[endpoint(S, "Me")], writes=[endpoint(D, "X\\Y")], admitted=False, queued="T2")
        self.assertEqual(blockers(me, [holder, older]), [holder, older])
        self.assertEqual(blockers(older, [holder, me]), [holder])

    def test_a_newcomer_never_overtakes_an_older_waiting_activity_it_conflicts_with(self):
        holder = activity(job="h", reads=[endpoint(S, "H")], writes=[endpoint(D, "Older")], queued="T0")
        older = activity(job="o", reads=[endpoint(S, "O")], writes=[endpoint(D, "Older"), endpoint(E, "Shared")],
                         admitted=False, queued="T1")
        newcomer = activity(job="n", reads=[endpoint(S, "N")], writes=[endpoint(E, "Shared")], admitted=False,
                            queued="T2")
        self.assertFalse(conflict(newcomer, holder))
        self.assertEqual(blockers(newcomer, [holder, older]), [older])
        self.assertEqual(blockers(newcomer, [holder]), [])

    def test_a_waiting_run_goes_before_a_waiting_verification_whatever_their_ages(self):
        check = activity("verification", job="w", reads=[endpoint(S, "W"), endpoint(D, "B")], marker="m",
                         admitted=False, queued="T1")
        run = activity(job="w", reads=[endpoint(S, "W")], writes=[endpoint(D, "B")], admitted=False, queued="T2")
        self.assertEqual(blockers(check, [run]), [run])
        self.assertEqual(blockers(run, [check]), [])


class AdmissionTest(TempDirTest):
    """Admission through the status channel, activities published beside a store."""

    def setUp(self):
        super().setUp()
        self.layout = RuntimeLayout(self.tmp)

    def activity(self, job_id, **kwargs):
        board = live_activity(self.layout, job_id, **kwargs)
        self.addCleanup(board.close)
        return board

    def published(self, board):
        return next(r.status for r in observe_runs(self.layout) if r.run_id == board.status.run_id)

    def test_an_attempt_publishes_what_it_waits_for_and_admits_once_nothing_holds_it_back(self):
        holder = self.activity("h" * 32, job_name="Holder", writes=[endpoint(D, "X")], paused=True)
        me = self.activity("m" * 32, job_name="Me", admitted=False)
        claim = Claim((endpoint(S, "Me"),), (endpoint(D, "x\\Y"),))
        turn = admit(me, claim)
        self.assertEqual((turn.admitted, turn.waits_for.run_id), (False, holder.status.run_id))
        status = self.published(me)
        self.assertEqual((Claim.from_document(status["claim"]), status["admitted"]), (claim, False))
        self.assertEqual(WaitedFor.from_status(status), WaitedFor(holder.status.run_id, "run", "Holder", paused=True))
        holder.close()
        self.assertTrue(admit(me, claim).admitted)
        status = self.published(me)
        self.assertEqual((status["admitted"], status["waiting_for"]), (True, None))

    def test_an_activity_that_has_not_asked_for_its_turn_holds_no_one_back(self):
        starting = self.activity("s" * 32, job_name="Starting", admitted=False)
        starting.status.claim = None                                   # its first status, before its first attempt
        starting.publish()
        self.assertEqual((self.published(starting)["admitted"], self.published(starting)["claim"]), (False, None))
        me = self.activity("m" * 32, job_name="Me", admitted=False)
        self.assertTrue(admit(me, Claim((endpoint(S, "Me"),), (endpoint(D, "Me"),))).admitted)
        older = self.published(starting)
        del older["admitted"], older["claim"]                          # as an older copy of the tool publishes it
        (self.layout.status_dir / f"{starting.status.run_id}.json").write_text(json.dumps(older), encoding="utf-8")
        later = self.activity("l" * 32, job_name="Later", admitted=False)
        turn = admit(later, Claim((endpoint(S, "Later"),), (endpoint(E, "Later"),)))
        self.assertEqual((turn.admitted, turn.waits_for.run_id), (False, starting.status.run_id),
                         "a status that names no claim and never will still holds everyone back")

    def test_admission_records_a_dead_run_once(self):
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            status = LiveStatus("d" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-06T10:00:00Z",
                                pair_index=1, marker_id="m" * 32, pair_started="2026-10-06T10:00:01Z")
            StatusBoard(RuntimeLayout(Path({str(self.tmp)!r})), status).open()
            os._exit(0)
        """)
        died = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60)
        self.assertEqual(died.returncode, 0, died.stderr)
        first = self.activity("a" * 32, admitted=False)
        self.assertTrue(admit(first, Claim((endpoint(S, "A"),), (endpoint(D, "A"),))).admitted)
        second = self.activity("b" * 32, admitted=False)
        self.assertTrue(admit(second, Claim((endpoint(S, "B"),), (endpoint(D, "B"),))).admitted)
        self.assertEqual([r.outcome for r in read_records(self.layout, "j" * 32)], [Outcome.INTERRUPTED])

    def test_a_verification_gives_way_only_to_a_waiting_run_that_verifications_alone_hold_back(self):
        check = self.activity("w" * 32, kind="verification", marker_id="m",
                              reads=[endpoint(S, "Work"), endpoint(D, "Backup")])
        other = self.activity("o" * 32, writes=[endpoint(D, "Other")], reads=[endpoint(S, "Other")])
        self.assertFalse(gives_way(self.layout, check.status))
        waiting = self.activity("w" * 32, admitted=False, reads=[endpoint(S, "Work")], writes=[endpoint(D, "Backup")])
        self.assertTrue(gives_way(self.layout, check.status))
        blocking = self.activity("w" * 32, reads=[endpoint(S, "Work")], writes=[endpoint(E, "Second")])
        self.assertFalse(conflict(Activity.from_status(blocking.status.to_document()),
                                  Activity.from_status(check.status.to_document())))
        self.assertFalse(gives_way(self.layout, check.status))         # the run waits for its job's other run
        blocking.close()
        waiting.status.claim = None
        waiting.publish()
        self.assertFalse(gives_way(self.layout, check.status))         # it has not asked for anything yet
        other.close()


# --- admission: one drive at a time -------------------------------------------------------

DS, DD = "disk:gpt:00000000-0000-4000-8000-0000000000d1", "disk:gpt:00000000-0000-4000-8000-0000000000d2"


def drives(*pairs):
    return tuple(ClaimedDrive(drive, name) for drive, name in pairs)


ON_S, ON_D = (DS, "drive S:"), (DD, "drive D:")


class DriveConflictTest(unittest.TestCase):
    """The maintainer's decision of 2026-10-13: runs and verifications that use one drive
    work one at a time unless the user lets them work at the same time; a drive is a disk,
    or a volume no single disk carries. Here each drive's kind is not known, and a claim
    takes turns on every drive or on none as its choice says."""

    @staticmethod
    def one(job, folder, *, admitted=True, queued="T1", kind="run", turns=DriveTurns.ONE_AT_A_TIME,
            on=(ON_S, ON_D)):
        reads = (endpoint(S, folder),) if kind == "run" else (endpoint(S, folder), endpoint(D, folder))
        writes = (endpoint(D, folder),) if kind == "run" else ()
        return Activity(f"{job}-{queued}", kind, job, job.title(), f"m-{job}" if kind != "run" else "", queued,
                        admitted, False, Claim(reads, writes, drives(*on), turns))

    def test_a_drive_both_use_conflicts_only_where_drives_are_taken_one_at_a_time(self):
        work, photos = self.one("work", "Work"), self.one("photos", "Photos")
        self.assertFalse(conflict(work, photos))
        self.assertTrue(conflict(work, photos, by_drive=True))
        self.assertTrue(conflict(photos, work, by_drive=True))
        elsewhere = self.one("else", "Else", on=((DD.replace("d2", "e3"), "drive E:"),))
        self.assertFalse(conflict(work, elsewhere, by_drive=True))
        older = activity(job="old", reads=[endpoint(S, "Old")], writes=[endpoint(D, "Old")])
        self.assertFalse(conflict(work, older, by_drive=True), "a claim naming no drives is compared by its folders")

    def test_the_waiting_activity_judges_by_its_own_choice(self):
        holder = self.one("holder", "Holder", queued="T0")
        alone = self.one("me", "Me", admitted=False, queued="T1")
        beside = self.one("me", "Me", admitted=False, queued="T1", turns=DriveTurns.AT_THE_SAME_TIME)
        self.assertEqual(blockers(alone, [holder]), [holder])
        self.assertEqual(blockers(beside, [holder]), [])

    def test_its_place_is_one_more_than_the_waiting_ones_it_conflicts_with_that_go_first(self):
        holder = self.one("holder", "Holder", queued="T0")
        first = self.one("first", "First", admitted=False, queued="T1")
        second = self.one("second", "Second", admitted=False, queued="T2")
        me = self.one("me", "Me", admitted=False, queued="T3")
        self.assertEqual(place_in_line(first, [holder, second, me]), 1)
        self.assertEqual(place_in_line(me, [holder, first, second]), 3)
        apart = self.one("apart", "Apart", admitted=False, queued="T0", on=((DD.replace("d2", "e3"), "drive E:"),))
        self.assertEqual(place_in_line(me, [holder, first, second, apart]), 3, "one that shares nothing is not ahead")
        check = self.one("check", "Check", admitted=False, queued="T0", kind="verification")
        self.assertEqual(place_in_line(check, [first, second]), 3, "runs go before a verification")

    def test_a_place_in_words(self):
        self.assertEqual([line_words(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111, 112)],
                         ["next in line", "2nd in line", "3rd in line", "4th in line", "11th in line", "12th in line",
                          "13th in line", "21st in line", "22nd in line", "23rd in line", "101st in line",
                          "111th in line", "112th in line"])

    def test_a_claim_names_the_drives_of_what_is_present_each_once_and_by_its_letters(self):
        s, d, e = replace(S, disk=DS), replace(D, disk=DD), replace(E, disk=DS)
        job = new_job("Work", endpoint(S, "Work"), [endpoint(D, "B1"), endpoint(E, "B2")])
        claim = run_claim(job, [1, 2], (s, d, e), turns=DriveTurns.ONE_AT_A_TIME)
        self.assertEqual(claim.drives, drives((DS, "the drive of E: and S:"), (DD, "drive D:")))
        self.assertIs(claim.turns, DriveTurns.ONE_AT_A_TIME)
        self.assertEqual(run_claim(job, [1, 2], (s, e)).drives, drives((DS, "the drive of E: and S:")))
        self.assertIs(run_claim(job, [1, 2], (s, e)).turns, DriveTurns.AT_THE_SAME_TIME)
        check = verification_claim(job, 1, (s, d), turns=DriveTurns.ONE_AT_A_TIME)
        self.assertEqual((check.drives, check.turns),
                         (drives((DS, "drive S:"), (DD, "drive D:")), DriveTurns.ONE_AT_A_TIME))
        self.assertEqual(verification_claim(job, 1).drives, ())
        self.assertEqual(Claim.from_document(claim.to_document()), claim)
        older = {"reads": [endpoint(S, "Work").to_document()], "writes": []}
        self.assertEqual(Claim.from_document(older), Claim((endpoint(S, "Work"),), ()))
        for bad in ({"drives": [{"drive": 1, "name": "x"}]}, {"drives": [{"drive": DS}]}, {"one_per_drive": "yes"},
                    {"turns": "sometimes"}):
            self.assertIsNone(Claim.from_document(dict(older, **bad)), bad)


class DriveAdmissionTest(TempDirTest):
    """One drive at a time through the status channel: what a waiting activity publishes,
    and a verification giving way as the waiting run judges its own turn."""

    ONE = DriveTurns.ONE_AT_A_TIME

    def setUp(self):
        super().setUp()
        self.layout = RuntimeLayout(self.tmp)

    def publish(self, letter, claim, *, job_name="Other", admitted=True, kind="run", queued=None):
        status = LiveStatus(letter * 32, os.getpid(), letter * 32, job_name, "manual", "2026-10-14T10:00:00Z",
                            kind=kind, marker_id=f"m{letter}" if kind != "run" else None, admitted=admitted,
                            claim=claim.to_document() if claim is not None else None,
                            phase=Phase.COPYING if admitted else Phase.WAITING)
        if queued is not None:
            status.queued = queued
        board = StatusBoard(self.layout, status)
        board.open()
        self.addCleanup(board.close)
        return board

    def published(self, board):
        return next(r.status for r in observe_runs(self.layout) if r.run_id == board.status.run_id)

    def test_a_waiting_activity_names_the_drive_it_shares_and_its_place_in_line(self):
        holder = self.publish("h", Claim((endpoint(S, "H"),), (endpoint(D, "H"),), drives(ON_S, ON_D), self.ONE),
                              job_name="Holder")
        first = self.publish("f", Claim((endpoint(S, "F"),), (endpoint(D, "F"),), drives(ON_S, ON_D), self.ONE),
                             job_name="First", admitted=False, queued="2026-10-14T10:00:01.000000Z")
        me = self.publish("m", None, job_name="Me", admitted=False, queued="2026-10-14T10:00:02.000000Z")
        claim = Claim((endpoint(S, "Me"),), (endpoint(D, "Me"),), drives(ON_S, ON_D), self.ONE)
        turn = admit(me, claim)
        self.assertEqual((turn.admitted, turn.waits_for.run_id), (False, holder.status.run_id))
        status = self.published(me)
        self.assertEqual(WaitedFor.from_status(status),
                         WaitedFor(holder.status.run_id, "run", "Holder", drive="drive S:"))
        self.assertEqual(status["place"], 2)
        holder.close()
        first.close()
        self.assertTrue(admit(me, claim).admitted)
        status = self.published(me)
        self.assertEqual((status["waiting_for"], status["place"]), (None, 0))

    def test_a_folder_both_need_names_no_drive(self):
        holder = self.publish("h", Claim((endpoint(S, "H"),), (endpoint(D, "Me\\Deep"),), drives(ON_S, ON_D), self.ONE),
                              job_name="Holder")
        me = self.publish("m", None, job_name="Me", admitted=False)
        claim = Claim((endpoint(S, "Me"),), (endpoint(D, "Me"),), drives(ON_S, ON_D), self.ONE)
        self.assertFalse(admit(me, claim).admitted)
        status = self.published(me)
        self.assertEqual((WaitedFor.from_status(status).drive, status["place"]), ("", 1))
        holder.close()

    def test_switched_to_the_same_time_it_works_beside_one_on_its_drive(self):
        self.publish("h", Claim((endpoint(S, "H"),), (endpoint(D, "H"),), drives(ON_S, ON_D), self.ONE),
                     job_name="Holder")
        me = self.publish("m", None, job_name="Me", admitted=False)
        self.assertTrue(admit(me, Claim((endpoint(S, "Me"),), (endpoint(D, "Me"),), drives(ON_S, ON_D))).admitted)

    def test_a_verification_gives_way_to_a_run_on_its_drive_that_takes_drives_one_at_a_time(self):
        check = self.publish("c", Claim((endpoint(S, "Work"), endpoint(D, "Backup")), (), drives(ON_S, ON_D), self.ONE),
                             kind="verification")
        waiting = self.publish("w", Claim((endpoint(S, "Other"),), (endpoint(D, "Other"),), drives(ON_D), self.ONE),
                               admitted=False)
        self.assertTrue(gives_way(self.layout, check.status))
        waiting.status.claim = Claim((endpoint(S, "Other"),), (endpoint(D, "Other"),), drives(ON_D)).to_document()
        waiting.publish()
        self.assertFalse(gives_way(self.layout, check.status), "the run's own choice lets them work at once")


class _Stop(Exception):
    pass


class ListingProgressTest(TempDirTest):
    def test_progress_counts_the_entries_seen_and_can_end_the_listing(self):
        for i in range(12):
            write(self.tmp, f"d{i % 3}\\f{i}.txt")
        seen = []
        result = list_tree(self.tmp, Side.SOURCE, drive_root=False, progress=seen.append)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 15)                           # three folders and twelve files
        self.assertEqual(len(result.files), 12)

        def stop(entries):
            if entries:
                raise _Stop()
        with self.assertRaises(_Stop):
            list_tree(self.tmp, Side.SOURCE, drive_root=False, progress=stop)


class PreviewProgressTest(TempDirTest):
    """``preview_job`` reports where it is and is ended by an exception from its callback."""

    def setUp(self):
        super().setUp()
        vol_s, vol_d = self.tmp / "volS", self.tmp / "volD"
        for i in range(5):
            write(vol_s / "Work", f"f{i}.txt")
        vol_d.mkdir()
        s = volume(1, mounts=(str(vol_s) + "\\",))
        d = volume(2, mounts=(str(vol_d) + "\\",))
        self.volumes = FixedVolumeSource((s, d))
        self.store = ConfigStore(self.tmp / "state" / "store.json")
        self.job = new_job("Work", endpoint(s, "Work"), [endpoint(d, "Backup")])
        self.store.save(StoreContents((self.job,)))
        self.layout = RuntimeLayout(self.store.path.parent)

    def test_a_preview_reports_its_phases_and_its_source(self):
        steps = []
        preview = preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=(),
                              progress=steps.append)
        self.assertEqual((steps[0].phase, steps[0].destination), (Phase.LISTING, 0))
        self.assertIn(5, [s.entries for s in steps if s.destination == 0])
        self.assertEqual(steps[-1].phase, Phase.PLANNING)
        self.assertEqual(steps[-1].destination, 1)
        self.assertEqual(preview.source_files, 5)
        self.assertEqual(preview.source.location, self.tmp / "volS" / "Work")

    def test_an_exception_from_the_callback_ends_the_preview_before_its_ticket(self):
        def stop(step):
            if step.phase is Phase.PLANNING:
                raise _Stop()
        with self.assertRaises(_Stop):
            preview_job(self.store, self.job.job_id, volume_source=self.volumes, sync_roots=(), progress=stop)
        self.assertFalse((self.layout.tickets_dir / f"{self.job.job_id}.json").exists())


# --- names a run cannot keep (engine contract: test_engine_contract.NameTest) -------------

CARRIED = (
    "plain.jpg",                                           # plain
    "\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8.jpg",            # fullwidth colon
    "\u3057\u3064\u3082\u3093\uff1f.jpg",                  # fullwidth question mark
    "\u307b\u3057\uff0a\u3057\u308b\u3057.jpg",            # fullwidth asterisk
    "\u305f\u3066\uff5c\u305b\u3093.jpg",                  # fullwidth vertical line
    "\u3044\u3093\uff02\u3088\u3046.jpg",                  # fullwidth quotation mark
    "\u3057\u3087\u3046\uff1c\u305f\u3044\uff1e.jpg",      # fullwidth less-than and greater-than signs
    "\u30d5\u30eb\uff3c\u30ca\u30ca\u30e1.jpg",            # fullwidth reverse solidus
    "\u30d5\u30eb\uff0f\u30ca\u30ca\u30e1.jpg",            # fullwidth solidus
    "\u30d5\u30eb\uff0e\u30c6\u30f3.jpg",                  # fullwidth full stop inside a name
    "\u304a\u308f\u308a\u306e\u3066\u3093\uff0e",          # ending in a fullwidth full stop
    "\u304a\u308f\u308a\u306e\u3042\u304d\u2420",          # ending in the symbol for space
    "\u2400control.jpg",                                   # control picture NUL
    "x\u2401y.jpg",                                        # control picture SOH
    "x\u241fy.jpg",                                        # control picture US
    "x\u2421y.jpg",                                        # control picture DEL
    " leading space.jpg",                                  # leading space
    "\u3057\u3083\u3057\u3093\uff1a2019\\IMG_1.jpg",       # a folder named with a fullwidth colon
    "dir\uff0fsub\\x\u2401.jpg",                           # fullwidth solidus and a control picture in a path
)
UNCARRIED = ("\u201bquote.jpg", "mid\u201bquote.jpg", "\uff0e\uff0e", "\uff0e", "dir\\\uff0e\uff0e\\x.jpg",
             "bad\udc80name.jpg")
TRAILING = ("trailing space.jpg ", "trailing period.jpg.", "Album \\in.jpg", "Album.\\in.jpg")


class LeftOutNamesTest(unittest.TestCase):
    def test_what_the_engine_cannot_carry_is_named_with_why(self):
        for path in CARRIED + TRAILING:
            self.assertIsNone(uncarried(path), path)
        for path in UNCARRIED:
            self.assertIsNotNone(uncarried(path), path)
        self.assertIn("U+201B", uncarried("mid\u201bquote.jpg"))
        self.assertIn("unpaired surrogate", uncarried("bad\udc80name.jpg"))

    def test_a_run_leaves_out_those_and_names_ending_in_a_space_or_a_period(self):
        for path in CARRIED:
            self.assertIsNone(left_out(path), path)
        for path in UNCARRIED:
            self.assertEqual(left_out(path), uncarried(path), path)
        for path in TRAILING:
            self.assertIn("ends in a space or a period", left_out(path), path)

    def test_the_lists_spelling_quotes_what_the_engine_reads_as_another_character_and_reads_back(self):
        self.assertEqual(engine_name("dir\uff0fsub\\x\u2401.jpg"), "dir\u201b\uff0fsub/x\u201b\u2401.jpg")
        self.assertEqual(engine_name("\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8\\\u2400control.jpg"),
                         "\u30e1\u30e2\uff1a\u30ce\u30fc\u30c8/\u2400control.jpg")
        for path in CARRIED:
            self.assertEqual(local_name(engine_name(path)), path)

    def test_the_plan_leaves_them_out_of_every_step_and_names_them(self):
        plan = build_plan(src(("ok.jpg", 1, T0), ("trailing.jpg.", 2, T0), ("Album \\in.jpg", 3, T0),
                              ("bad\udc80.jpg", 4, T0), ("\u201bchanged.jpg", 5, T0 + SECOND),
                              ("renamed.jpg.", 7, T0 + 3), dirs=("Empty ", "Fine")),
                          dst(("\u201bchanged.jpg", 5, T0), ("gone.txt", 6, T0), ("stray.jpg.", 8, T0),
                              ("orig.jpg", 7, T0 + 3), dirs=("Old ",)))
        self.assertEqual([e.path for e in plan.new], ["ok.jpg"])
        self.assertEqual((plan.changed, plan.renames), ((), ()))
        self.assertEqual(sorted(e.path for e in plan.deleted), ["gone.txt", "orig.jpg"])
        self.assertEqual([e.path for e in plan.kept], ["stray.jpg."])
        self.assertEqual(sorted(s.path for s in plan.left_out),
                         sorted(["trailing.jpg.", "Album \\in.jpg", "bad\udc80.jpg", "\u201bchanged.jpg",
                                 "renamed.jpg.", "Empty "]))
        self.assertTrue(all(s.reason is SkipReason.LEFT_OUT and s.detail for s in plan.left_out))
        self.assertEqual(plan.dirs_to_create, ("Fine",))
        self.assertEqual(plan.deleted_dirs, ())
        self.assertEqual((plan.files_to_copy, plan.removal_count), (1, 2))

    def test_a_folder_holding_a_kept_name_does_not_move_whole(self):
        plan = build_plan(src(("keep.txt", 1, T0)),
                          dst(("keep.txt", 1, T0), ("Old\\a.txt", 2, T0), ("Old\\stray.jpg.", 3, T0),
                              ("Old\\Sub\\b.txt", 4, T0)))
        self.assertEqual([e.path for e in plan.kept], ["Old\\stray.jpg."])
        self.assertEqual(sorted(e.path for e in plan.deleted), ["Old\\Sub\\b.txt", "Old\\a.txt"])
        self.assertEqual(plan.deleted_dirs, ("Old\\Sub",))
        self.assertEqual(plan.moved_with_folders(), {"old\\sub\\b.txt": "Old\\Sub"})

    def test_changed_files_of_the_same_size_are_counted(self):
        plan = build_plan(src(("same.jpg", 5, T0 + SECOND), ("grown.jpg", 6, T0 + SECOND)),
                          dst(("same.jpg", 5, T0), ("grown.jpg", 5, T0)))
        self.assertEqual((len(plan.changed), plan.same_size_changed), (2, 1))


class SignatureProgressTest(TempDirTest):
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    def test_the_reads_report_how_far_they_are_and_end_on_an_exception(self):
        for i in range(3):
            write(self.tmp / "d", f"p{i}.png", self.PNG, T0)
            write(self.tmp / "s", f"p{i}.png", self.PNG + b"x", T0 + SECOND)
        plan = build_plan(list_tree(self.tmp / "s", Side.SOURCE, drive_root=False),
                          list_tree(self.tmp / "d", Side.DESTINATION, drive_root=False))
        reads = []

        def read(path):
            reads.append(path)
            return Path(path).read_bytes()[:16]

        seen = []
        assess_signatures(plan, self.tmp / "s", self.tmp / "d", read, read, lambda n, total: seen.append((n, total)))
        self.assertEqual(seen, [(0, 3), (1, 3), (2, 3), (3, 3)])
        reads.clear()

        def stop(examined, _total):
            if examined == 1:
                raise _Stop()
        with self.assertRaises(_Stop):
            assess_signatures(plan, self.tmp / "s", self.tmp / "d", read, read, stop)
        self.assertEqual(len(reads), 2)                 # the first file's two reads, then nothing


# --- the live status of a transfer and the time left -----------------------------------

class TimeLeftTest(unittest.TestCase):
    def test_the_larger_of_the_files_and_the_bytes_at_the_measured_pace(self):
        self.assertEqual(time_left(900, 9 * MiB, 100, 100 * MiB, 10.0), 90.0)       # bound by the files
        self.assertEqual(time_left(10, 1000 * MiB, 10, 10 * MiB, 10.0), 1000.0)     # bound by the bytes
        self.assertEqual(time_left(0, 0, 10, 10 * MiB, 10.0), 0.0)
        self.assertEqual(time_left(5, 0, 5, 0, 10.0), 10.0)                         # empty files: the count alone

    def test_nothing_is_said_before_the_pace_is_measured(self):
        self.assertIsNone(time_left(10, 100, 5, 50, MIN_RATE_SECONDS - 0.1))
        self.assertIsNone(time_left(10, 100, 0, 0, 60.0))


class _StatsOnly:
    """The engine adapter's ``Transfer`` as the status pump reads it: live statistics only."""

    pid = 4242
    suspended = False

    def __init__(self, stats):
        self.figures = stats

    def stats(self):
        return self.figures


class PumpTest(TempDirTest):
    def test_files_found_identical_count_as_done_and_the_files_compared_are_named(self):
        layout = RuntimeLayout(self.tmp)
        status = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-09T10:00:00Z", pair_index=1)
        board = StatusBoard(layout, status)
        board.open()
        self.addCleanup(board.close)
        pump = StatusPump(board, RunControl(status))
        transfer = _StatsOnly(TransferStats(2 * MiB, 0, 1, 0, 0.0, 6.0, (InFlight("sub\\a.jpg", 4 * MiB, 2 * MiB),),
                                            ("sub\\b.jpg", "c.jpg"), 30, 90 * MiB))
        pump.watch(transfer, 100, 300 * MiB)
        pump.tick()
        published = json.loads((layout.status_dir / f"{'a' * 32}.json").read_text(encoding="utf-8"))
        done = published["done"]
        self.assertEqual((done["files_copied"], done["bytes_copied"], done["files_identical"], done["bytes_identical"]),
                         (1, 2 * MiB, 30, 90 * MiB))
        self.assertEqual(published["comparing"], ["sub\\b.jpg", "c.jpg"])
        self.assertEqual(published["in_flight"], [["sub\\a.jpg", 4 * MiB, 2 * MiB]])
        self.assertEqual((published["rate"], published["eta_seconds"]), (0.0, None))   # not the engine's eta
        pump.watch(None)
        self.assertEqual((status.comparing, status.in_flight, status.engine_pid), ([], [], None))


# --- run records of schema 4 -------------------------------------------------------------

def identical_record(files, size, *, copied=0, seconds=0.0):
    done = dict.fromkeys(DONE_KEYS, 0)
    done.update(bytes_copied=copied, files_identical=files, bytes_identical=size)
    return RunRecord("r" * 32, "j" * 32, "Job", "m" * 32, 1, "manual", "2026-10-09T10:00:00Z", "2026-10-09T10:00:00Z",
                     Outcome.COMPLETED, planned=dict.fromkeys(PLANNED_KEYS, 0), done=done,
                     left=dict.fromkeys(LEFT_KEYS, 0), transfer_seconds=seconds)


class RecordSchemaFourTest(TempDirTest):
    def test_files_found_identical_are_recorded_and_older_records_read_with_none(self):
        layout = RuntimeLayout(self.tmp)
        rec = identical_record(3, 300)
        path = write_record(layout, rec)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["done"]["files_identical"], doc["done"]["bytes_identical"]),
                         (RECORD_SCHEMA, 3, 300))
        self.assertEqual(read_records(layout, "j" * 32), [rec])
        del doc["encrypted"]                                      # schema 6's; this record cleaned nothing
        for key in ("files_identical", "bytes_identical"):
            del doc["done"][key]
        for schema, gone in ((3, ()), (2, ("versions_cleaned", "space")), (1, ("source_read", "not_backed_up"))):
            for key in gone:
                del doc[key]
            doc["schema_version"] = schema
            older = RunRecord.from_document(doc)
            self.assertEqual((older.done["files_identical"], older.done["bytes_identical"]), (0, 0), schema)
            self.assertEqual(older.done["files_copied"], 0)

    def test_a_newer_schema_and_misplaced_fields_are_refused(self):
        rec = identical_record(1, 1)
        doc = rec.to_document()
        self.assertEqual(RunRecord.from_document(doc), rec)
        with self.assertRaises(ContractError) as refused:
            RunRecord.from_document({**doc, "schema_version": RECORD_SCHEMA + 1})
        self.assertEqual(refused.exception.where, "format")
        del doc["encrypted"]                                      # schema 6's; now only the identical files are extra
        doc["schema_version"] = 3
        with self.assertRaises(ContractError) as refused:         # schema 3 knows no identical files
            RunRecord.from_document(doc)
        self.assertEqual(refused.exception.where, "done")
        self.assertIn("files_identical", refused.exception.message)

    def test_the_throughput_counts_the_bytes_found_identical(self):
        self.assertEqual(identical_record(10, 100 * MiB, copied=10 * MiB, seconds=2.0).throughput, 55 * MiB)
        self.assertEqual(identical_record(100, 128 * MiB, seconds=2.0).throughput, 64 * MiB)
        self.assertIsNone(identical_record(1, MiB, seconds=2.0).throughput)

    def test_a_name_holding_an_unpaired_surrogate_is_written_and_read_back(self):
        layout = RuntimeLayout(self.tmp)
        rec = replace(identical_record(0, 0), skipped=(("bad\udc80name.jpg", "why"),), skipped_total=1,
                      not_backed_up=(("bad\udc80name.jpg", 1),))
        path = write_record(layout, rec, self.tmp / "dest")
        path.read_bytes().decode("utf-8")                         # valid UTF-8 throughout
        self.assertEqual(read_records(layout, "j" * 32), [rec])
        ticket = new_ticket("j" * 32, {"m" * 32: PairConfirmation(False, frozenset({"bad\udc80name.jpg"}))})
        save_ticket(layout, ticket)
        self.assertEqual(load_ticket(layout, ticket.job_id, ticket.ticket_id), ticket)

    def test_a_dead_runs_files_found_identical_are_not_left(self):
        layout = RuntimeLayout(self.tmp)
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PROJECT)!r})
            from pathlib import Path
            from mirrorhold.locations import RuntimeLayout
            from mirrorhold.records.status import LiveStatus, StatusBoard
            status = LiveStatus("a" * 32, os.getpid(), "j" * 32, "Job", "manual", "2026-10-09T10:00:00Z",
                                pair_index=1, marker_id="m" * 32, pair_started="2026-10-09T10:00:01Z")
            status.planned.update(files_to_copy=10, bytes_to_copy=1000, changed_files=8)
            status.done.update(files_copied=2, bytes_copied=200, files_identical=5, bytes_identical=500)
            StatusBoard(RuntimeLayout(Path({str(self.tmp)!r})), status).open()
            os._exit(0)
        """)
        died = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60)
        self.assertEqual(died.returncode, 0, died.stderr)
        reaped = reap_interrupted(layout)
        left = reaped[0].left
        self.assertEqual((left["files_to_copy"], left["bytes_to_copy"], left["removals"]), (3, 300, 3))
        self.assertEqual(reaped[0].done["files_identical"], 5)


# --- a shared destination: what its job owns and what it leaves where it is ---------------

def keys(listing_):
    return sorted(list(listing_.files) + list(listing_.dirs) + [path_key(s.path) for s in listing_.skipped])


class SharingTest(unittest.TestCase):
    """What a run of a shared destination covers, from listings alone (the maintainer's
    decisions of 2026-10-10: what a folder already holds is left where it is unless the
    user chooses otherwise)."""

    def root_shaped(self):
        return dst(("Windows\\System32\\kernel.dll", 9, T0), ("Users\\someone\\letter.docx", 9, T0),
                   ("Photos\\old.jpg", 9, T0), ("Photos\\a.jpg", 9, T0), ("Photos\\2019\\b.jpg", 9, T0),
                   ("Photos\\Trip\\c.jpg", 9, T0), ("top.txt", 9, T0))

    def test_a_first_run_claims_the_sources_names_and_leaves_the_rest(self):
        source = src(("Photos\\a.jpg", 9, T0 + SECOND), ("Photos\\Trip\\c.jpg", 9, T0), ("letter.txt", 3, T0))
        shared = share(self.root_shaped(), source)
        self.assertEqual(shared.own, ("letter.txt", "Photos"))
        self.assertEqual(shared.left, ("Photos\\2019", "Photos\\old.jpg"))
        self.assertEqual(keys(shared.listing), ["photos", "photos\\a.jpg", "photos\\trip", "photos\\trip\\c.jpg"])
        self.assertEqual(shared.left_files, 5)
        self.assertEqual(shared.left_items, ("top.txt", "Users", "Windows", "Photos\\2019", "Photos\\old.jpg"))
        self.assertEqual(shared.meeting, (("Photos", 2),))
        self.assertTrue(shared.changed and shared.leaves_anything)
        plan = build_plan(source, shared.listing, leave=shared.left_keys)
        self.assertEqual(([c.destination.path for c in plan.changed], plan.deleted, plan.deleted_dirs),
                         (["Photos\\a.jpg"], (), ()))
        self.assertEqual([e.path for e in plan.new], ["letter.txt"])

    def test_a_later_run_leaves_what_appeared_outside_the_jobs_names_and_mirrors_inside_them(self):
        source = src(("Photos\\a.jpg", 9, T0), ("Music\\song.mp3", 5, T0))
        destination = dst(("Photos\\a.jpg", 9, T0), ("Photos\\old.jpg", 9, T0), ("Photos\\hand.txt", 9, T0),
                          ("letter.txt", 3, T0), ("Program Files\\new.exe", 9, T0), ("Music\\theirs.mp3", 9, T0))
        shared = share(destination, source, own=("letter.txt", "Photos"), left=("Photos\\old.jpg",))
        self.assertEqual(keys(shared.listing), ["letter.txt", "music", "photos", "photos\\a.jpg", "photos\\hand.txt"])
        plan = build_plan(source, shared.listing, leave=shared.left_keys)
        self.assertEqual(sorted(e.path for e in plan.deleted), ["Photos\\hand.txt", "letter.txt"],
                         "inside the job's names it mirrors: a file put there by hand and one the source dropped")
        self.assertEqual(shared.own, ("letter.txt", "Music", "Photos"))
        self.assertEqual(shared.left, ("Music\\theirs.mp3", "Photos\\old.jpg"))
        self.assertIn("Program Files", shared.left_items)
        self.assertEqual(shared.meeting, (("Music", 1),))

    def test_a_left_entry_the_source_reaches_is_left_no_longer(self):
        source = src(("Photos\\2019\\b.jpg", 9, T0 + SECOND), ("Photos\\old.jpg", 9, T0 + SECOND))
        destination = dst(("Photos\\2019\\b.jpg", 9, T0), ("Photos\\2019\\c.jpg", 9, T0), ("Photos\\old.jpg", 9, T0))
        shared = share(destination, source, own=("Photos",), left=("Photos\\2019", "Photos\\old.jpg"))
        self.assertEqual(shared.left, ("Photos\\2019\\c.jpg",))
        plan = build_plan(source, shared.listing, leave=shared.left_keys)
        self.assertEqual(sorted(c.destination.path for c in plan.changed), ["Photos\\2019\\b.jpg", "Photos\\old.jpg"],
                         "what the source holds at a left path replaces the copy there, the old one into versions")
        self.assertEqual(plan.deleted, ())

    def test_a_name_neither_side_holds_any_longer_is_forgotten(self):
        shared = share(dst(("Photos\\a.jpg", 9, T0)), src(("Photos\\a.jpg", 9, T0)), own=("Photos", "Gone"),
                       left=("Gone\\x.txt",))
        self.assertEqual((shared.own, shared.left, shared.changed), (("Photos",), (), True))

    def test_a_source_file_where_a_folder_was_takes_its_place_whole(self):
        source = src(("Photos", 4, T0))
        destination = dst(("Photos\\old.jpg", 9, T0), ("Photos\\deep\\x.jpg", 9, T0))
        shared = share(destination, source)
        self.assertEqual(shared.left, ())
        plan = build_plan(source, shared.listing, leave=shared.left_keys)
        self.assertEqual(plan.deleted_dirs, ("Photos",))

    def test_the_listing_scope_names_the_jobs_part(self):
        source = src(("Photos\\2019\\a.jpg", 9, T0), ("new.txt", 1, T0))
        scope = listing_scope(source, own=("Photos", "Old"), left=("Photos\\2019", "Photos\\x.jpg"))
        self.assertEqual(scope.names, frozenset({"photos", "old", "new.txt"}))
        self.assertEqual(scope.skip, frozenset({"photos\\x.jpg"}), "a left entry the source reaches is listed")


class RecordedScopeTest(unittest.TestCase):
    """What a verification lists of a shared destination: what its record says the job
    has backed up, whatever the source holds now."""

    def test_the_recorded_part_is_what_the_job_owns_less_what_it_leaves(self):
        scope = recorded_scope(("Photos", "Old"), ("Photos\\2019", "Photos\\x.jpg"))
        self.assertEqual(scope.names, frozenset({"photos", "old"}))
        self.assertEqual(scope.skip, frozenset({"photos\\2019", "photos\\x.jpg"}))
        self.assertFalse(scope.covers("new.txt", True), "a name the source gained is not the job's yet")
        self.assertFalse(scope.covers("photos\\x.jpg", False), "a left entry stays left, whatever the source holds")
        self.assertTrue(scope.covers("photos\\a.jpg", False))


class ScopedListingTest(TempDirTest):
    def test_a_scoped_listing_reads_only_the_jobs_part(self):
        for rel in ("Windows\\a.dll", "Photos\\a.jpg", "Photos\\old\\b.jpg", "top.txt"):
            write(self.tmp, rel, b"x", T0)
        scoped = list_tree(self.tmp, Side.DESTINATION, drive_root=False,
                           scope=Scope(frozenset({"photos"}), frozenset({"photos\\old"})))
        self.assertEqual(keys(scoped), ["photos", "photos\\a.jpg"])
        self.assertEqual(scoped.file_count, 1)


class LeavePlanTest(unittest.TestCase):
    def test_a_folder_holding_a_left_entry_never_moves_whole(self):
        source = src(("keep.txt", 1, T0))
        destination = dst(("keep.txt", 1, T0), ("Photos\\a.jpg", 9, T0), ("Photos\\sub\\b.jpg", 9, T0))
        plan = build_plan(source, destination, leave=frozenset({"photos\\old.jpg"}))
        self.assertEqual(plan.deleted_dirs, ("Photos\\sub",))
        self.assertEqual(sorted(e.path for e in plan.deleted), ["Photos\\a.jpg", "Photos\\sub\\b.jpg"])
        self.assertEqual(build_plan(source, destination).deleted_dirs, ("Photos",))


class FirstRunConfirmationTest(unittest.TestCase):
    disk = volume(2, letter="E")

    def test_a_confirmed_first_run_the_destination_outgrew_waits_whole(self):
        confirmed = build_plan(src(("a.txt", 1, T0)), dst())
        grown = build_plan(src(("a.txt", 1, T0)), dst(("stray.txt", 7, T0)))
        decision = judge(grown, holds=HoldThresholds(), first_run=True, destination=self.disk,
                         confirmed=confirmed.removal_keys())
        self.assertEqual((decision.verdict, decision.holds),
                         (Verdict.AWAIT_CONFIRMATION, (Hold.FIRST_RUN, Hold.NOT_CONFIRMED)))
        self.assertEqual(decision.reasons, ("first-run", "not-in-confirmed-plan", "destination-holds-content"))
        self.assertFalse(decision.applies_additions)

    def test_a_shared_destinations_first_run_says_whether_the_destination_holds_anything(self):
        part = build_plan(src(("a.txt", 1, T0)), dst())
        waiting = judge(part, holds=HoldThresholds(), first_run=True, destination=self.disk, holds_content=True)
        self.assertTrue(waiting.adoption)
        self.assertEqual(waiting.reasons, ("first-run", "destination-holds-content"))
        go_ahead = judge(part, holds=HoldThresholds(), first_run=True, destination=self.disk, first_backup=True,
                         holds_content=True)
        self.assertEqual(go_ahead.verdict, Verdict.AWAIT_CONFIRMATION, "the go-ahead covers a folder holding nothing")


class TicketChoiceTest(TempDirTest):
    def test_a_ticket_carries_a_first_runs_choice(self):
        layout = RuntimeLayout(self.tmp)
        ticket = new_ticket("j" * 32, {"m" * 32: PairConfirmation(True, frozenset({"a.txt"}), Content.LEAVE),
                                       "n" * 32: PairConfirmation(False, frozenset())})
        save_ticket(layout, ticket)
        self.assertEqual(load_ticket(layout, ticket.job_id, ticket.ticket_id), ticket)
        document = json.loads((layout.tickets_dir / f"{'j' * 32}.json").read_text(encoding="utf-8"))
        self.assertEqual((document["schema_version"], document["pairs"]["m" * 32]["content"],
                          document["pairs"]["n" * 32]["content"]), (2, "leave", None))
        document["pairs"]["m" * 32]["content"] = "keep"
        (layout.tickets_dir / f"{'j' * 32}.json").write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(TicketError):
            load_ticket(layout, ticket.job_id, ticket.ticket_id)


@unittest.skipUnless(WINDOWS, "Windows names")
class FolderNameEndingTest(TempDirTest):
    """Folders whose names end in a space or a period are listed as written, whatever the
    interpreter normalises, so the plan leaves each file in them out by name."""

    LONG = "\\\\?\\"

    def folder(self, *parts):
        os.makedirs(self.LONG + str(self.tmp.joinpath(*parts)), exist_ok=True)

    def file(self, *parts):
        with open(self.LONG + str(self.tmp.joinpath(*parts)), "wb") as handle:
            handle.write(b"photo")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.LONG + str(self.tmp), ignore_errors=True)

    def test_the_spelling_listings_use(self):
        self.assertEqual(enumerable("C:\\x\\Album "), "\\\\?\\C:\\x\\Album ")
        for already in ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\x", "\\\\.\\C:\\x"):
            self.assertEqual(enumerable(already), already)
        self.assertEqual(enumerable("\\\\server\\share\\x"), "\\\\?\\UNC\\server\\share\\x")

    def test_such_folders_are_listed_and_their_files_left_out_by_name(self):
        for name in ("Album ", "Dots."):
            self.folder("src", name)
            self.file("src", name, "in.jpg")
        self.folder("src", "Empty ")
        self.file("src", "ok.jpg")
        self.folder("dst")
        source = list_tree(self.tmp / "src", Side.SOURCE, drive_root=False)
        self.assertEqual(sorted(e.path for e in source.files.values()), ["Album \\in.jpg", "Dots.\\in.jpg", "ok.jpg"])
        self.assertEqual(sorted(source.dirs.values()), ["Album ", "Dots.", "Empty "])
        self.assertEqual(source.skipped, [])
        plan = build_plan(source, list_tree(self.tmp / "dst", Side.DESTINATION, drive_root=False))
        self.assertEqual(sorted(s.path for s in plan.left_out), ["Album \\in.jpg", "Dots.\\in.jpg", "Empty "])
        self.assertEqual([e.path for e in plan.new], ["ok.jpg"])
        self.assertEqual(plan.dirs_to_create, ())

    def test_a_destination_holding_only_such_a_folder_holds_something_and_keeps_it(self):
        self.folder("src")
        self.folder("dst", "Old.")
        self.file("dst", "Old.", "kept.jpg")
        self.assertIs(occupancy(self.tmp / "dst", drive_root=False), Occupancy.OCCUPIED)
        destination = list_tree(self.tmp / "dst", Side.DESTINATION, drive_root=False)
        plan = build_plan(list_tree(self.tmp / "src", Side.SOURCE, drive_root=False), destination)
        self.assertEqual(([e.path for e in plan.kept], plan.deleted, plan.deleted_dirs), (["Old.\\kept.jpg"], (), ()))


if __name__ == "__main__":
    unittest.main()
