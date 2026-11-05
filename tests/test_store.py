"""The configuration store: saved, reloaded, exported, imported; never half-written; a
file found missing in a save's rename looked for again under the save lock; a
corrupt file moved aside and reported, never silently replaced; a newer schema
never overwritten; older schemas migrated forward. Groups of jobs beside the jobs, with
the invariants that span both, carried by an export and placed by an import; one order
of jobs and groups and the moves within it; a save that changes only the GUI's
remembered state or that order keeping no previous copy; the choices for activities
among the preferences, each on in a file written before them; the schedule forms of
schema 8, a schema-7 file's schedules kept as they were; a cloud drive's mark in schema 9,
a schema-8 file kept as it was."""
import json
import os
import threading
import unittest
from dataclasses import replace

from support import TempDirTest, identity

from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.endpoint import Endpoint, RemoteEndpoint
from mirrorhold.endpoints.volumes import DriveKind
from mirrorhold.jobs.model import (Activities, DriveMark, DriveTurns, HoldThresholds, Preferences, RemoteSettings,
                                   Triggers, Unlock, new_group, new_job)
from mirrorhold.jobs.store import (MIGRATIONS, SCHEMA_VERSION, ConfigStore, JobList, LoadStatus, StoreContents,
                                   StoreError, merge_imported)
from mirrorhold.records.lock import holding


def job(name="Photos", n=1):
    return new_job(name, Endpoint(identity(n), "src"), [Endpoint(identity(n + 100), "dst")])


class SaveLoadTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path, keep_previous=3)

    def test_fresh_store(self):
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.FRESH)
        self.assertEqual(result.contents, StoreContents())
        self.assertFalse(self.path.exists())

    def test_saved_jobs_and_preferences_reload_equal(self):
        contents = StoreContents((job("A", 1), job("B", 2)), Preferences(gui_state={"geometry": "1x1"}))
        self.store.save(contents)
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.LOADED)
        self.assertEqual(result.contents, contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((doc["format"], doc["schema_version"]), ("mirrorhold-store", SCHEMA_VERSION))

    def test_a_save_leaves_no_staging_file_and_keeps_previous_copies(self):
        contents = StoreContents()
        for i in range(5):
            contents = contents.with_job(job(f"J{i}", i + 1))
            self.store.save(contents)
        self.assertFalse(self.path.with_name(self.path.name + ".saving").exists())
        copies = self.store.previous_copies()
        self.assertEqual(len(copies), 3)
        newest = json.loads(copies[0].read_text(encoding="utf-8"))
        self.assertEqual(len(newest["jobs"]), 4, "the newest previous copy is the state the last save replaced")

    def test_an_interrupted_save_leaves_the_old_file_intact(self):
        self.store.save(StoreContents((job("A", 1),)))
        before = self.path.read_bytes()
        staging = self.path.with_name(self.path.name + ".saving")
        staging.write_text('{"format": "mirrorhold-store", "sch', encoding="utf-8")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIs(self.store.load().status, LoadStatus.LOADED)
        self.store.save(StoreContents((job("B", 2),)))
        self.assertFalse(staging.exists())

    def test_a_job_file_with_a_byte_order_mark_loads_and_is_saved_without_one(self):
        contents = StoreContents((job("A", 1),))
        self.store.save(contents)
        self.path.write_bytes(b"\xef\xbb\xbf" + self.path.read_bytes())
        result = self.store.load()
        self.assertEqual((result.status, result.contents), (LoadStatus.LOADED, contents))
        self.assertTrue(self.path.exists(), "a byte-order mark is no reason to move the file aside")
        self.store.save(contents.with_job(job("B", 2)), expected=result.fingerprint)
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        restored = self.store.restore(self.store.previous_copies()[0])
        self.assertEqual((restored.status, restored.contents), (LoadStatus.LOADED, contents),
                         "a previous copy with the mark is restored")

    def test_a_peek_reads_the_file_and_never_acts_on_it(self):
        self.assertIsNone(self.store.peek(), "no file: nothing to read")
        contents = StoreContents((job("A", 1), job("B", 2)))
        self.store.save(contents)
        self.assertEqual(self.store.peek(), contents)
        self.path.write_text("{ broken", encoding="utf-8")
        self.assertIsNone(self.store.peek())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ broken", "a watcher moves nothing aside")
        self.assertEqual([p for p in self.path.parent.iterdir() if ".corrupt-" in p.name], [])
        self.store.save(contents)
        newer = json.loads(self.path.read_text(encoding="utf-8"))
        newer["schema_version"] = SCHEMA_VERSION + 1
        self.path.write_text(json.dumps(newer), encoding="utf-8")
        self.assertIsNone(self.store.peek(), "a newer version's file is not read")

    def test_corrupt_file_is_moved_aside_and_reported(self):
        self.store.save(StoreContents((job("A", 1),)))
        self.store.save(StoreContents((job("A", 1), job("B", 2))))
        self.path.write_text("{ broken", encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.CORRUPT)
        self.assertEqual(result.contents, StoreContents())
        self.assertFalse(self.path.exists(), "the corrupt file must not stay where a save would replace it")
        self.assertTrue(result.moved_to.exists())
        self.assertEqual(result.moved_to.read_text(encoding="utf-8"), "{ broken")
        self.assertIn(str(result.moved_to), result.detail)
        self.assertTrue(result.previous_copies)

    def test_a_document_breaking_the_contract_is_corrupt_too(self):
        self.store.save(StoreContents((job("A", 1),)))
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["jobs"][0]["holds"]["percent"] = 0
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.CORRUPT)
        self.assertIn("jobs[0].holds.percent", result.detail)

    def test_restore_a_previous_copy(self):
        self.store.save(StoreContents((job("A", 1),)))
        self.store.save(StoreContents((job("A", 1), job("B", 2))))
        self.path.write_text("garbage", encoding="utf-8")
        corrupt = self.store.load()
        restored = self.store.restore(corrupt.previous_copies[0])
        self.assertIs(restored.status, LoadStatus.LOADED)
        self.assertEqual([j.name for j in restored.contents.jobs], ["A"])
        with self.assertRaises(StoreError):
            self.store.restore(corrupt.moved_to)

    def test_newer_schema_is_left_untouched_and_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        newer = json.dumps({"format": "mirrorhold-store", "schema_version": 99, "jobs": [], "anything": 1})
        self.path.write_text(newer, encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.UNSUPPORTED)
        self.assertFalse(result.writable)
        self.assertEqual(self.path.read_text(encoding="utf-8"), newer)
        with self.assertRaises(StoreError):
            self.store.save(StoreContents((job("A", 1),)))
        self.assertEqual(self.path.read_text(encoding="utf-8"), newer)

    def test_migration_runs_forward_and_the_next_save_writes_the_new_schema(self):
        self.store.save(StoreContents((job("A", 1),)))
        current = self.path.read_text(encoding="utf-8")

        def to_next(doc):
            doc["preferences"]["gui_state"] = {"migrated": True}
            return doc

        store2 = ConfigStore(self.path, schema_version=SCHEMA_VERSION + 1, migrations={SCHEMA_VERSION: to_next})
        result = store2.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(dict(result.contents.preferences.gui_state), {"migrated": True})
        self.assertEqual(self.path.read_text(encoding="utf-8"), current, "loading never rewrites the file")
        store2.save(result.contents)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema_version"], SCHEMA_VERSION + 1)
        self.assertEqual(json.loads(store2.previous_copies()[0].read_text(encoding="utf-8"))["schema_version"],
                         SCHEMA_VERSION)

    def test_a_gap_in_the_migrations_is_unsupported_not_corrupt(self):
        self.store.save(StoreContents((job("A", 1),)))
        result = ConfigStore(self.path, schema_version=SCHEMA_VERSION + 2,
                             migrations={SCHEMA_VERSION: lambda d: d}).load()
        self.assertIs(result.status, LoadStatus.UNSUPPORTED)
        self.assertTrue(self.path.exists())


class CorruptFileSharedTest(TempDirTest):
    """Every waiting activity reloads the job file twice a second, so two processes can read
    one corrupt file at once: it is moved aside once, and the other load says so rather than
    ending on an error; a file a save replaced in between is read, never moved aside. Threads
    stand in for the processes: the save lock is a lock on a file, held per handle."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ broken", encoding="utf-8")

    def corrupt_files(self):
        return sorted(p.name for p in self.path.parent.iterdir() if ".corrupt-" in p.name)

    def between_read_and_move(self, act):
        """The store, with ``act`` done once just after its first read of the file."""
        reads = []
        original = self.store._read

        def read():
            raw = original()
            if not reads:
                reads.append(raw)
                act()
            return raw

        self.store._read = read
        return self.store

    def test_a_file_another_process_moved_aside_first_is_reported_as_moved_aside(self):
        other = ConfigStore(self.path)
        result = self.between_read_and_move(other.load).load()
        self.assertIs(result.status, LoadStatus.CORRUPT)
        self.assertIsNone(result.moved_to)
        self.assertIn("another process reading it at the same time moved it aside", result.detail)
        self.assertEqual(len(self.corrupt_files()), 1, "moved aside once")

    def test_a_file_a_save_replaced_in_between_is_read_and_stays(self):
        saved = StoreContents((job("A", 1),))
        result = self.between_read_and_move(lambda: ConfigStore(self.path).save(saved)).load()
        self.assertIs(result.status, LoadStatus.LOADED)
        self.assertEqual(result.contents, saved)
        self.assertEqual(self.corrupt_files(), [])
        self.assertEqual(ConfigStore(self.path).load().contents, saved)

    def test_loads_at_the_same_time_move_it_aside_once_and_none_fails(self):
        for round_ in range(20):
            self.path.write_text("{ broken", encoding="utf-8")
            start, results, errors = threading.Barrier(3), [], []

            def load():
                start.wait()
                try:
                    results.append(ConfigStore(self.path).load())
                except BaseException as exc:  # reported by the test
                    errors.append(exc)

            threads = [threading.Thread(target=load, daemon=True) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(errors, [], f"round {round_}")
            # one reading after the move finds no file at all
            self.assertTrue(all(r.status in (LoadStatus.CORRUPT, LoadStatus.FRESH) for r in results), f"round {round_}")
            self.assertEqual(sum(r.moved_to is not None for r in results), 1, f"round {round_}")
            self.assertFalse(self.path.exists())
            for moved in self.corrupt_files():
                (self.path.parent / moved).unlink()


class NoFileWhileSavedTest(TempDirTest):
    """A save's rename takes the job file's name away a moment before it gives it to the new
    file, so a read can find no file while one is there before and after: a load or a
    fingerprint that finds none looks again under the save lock, which every save holds
    across its rename. A thread holding the lock with the file moved away stands in for
    that moment."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)

    def while_a_save_renames(self, look):
        """What ``look`` returns when its first read meets the empty name while a save holds
        the lock; the save gives the name back only once that read has found nothing."""
        aside = self.path.with_name("aside.json")
        in_the_moment, found_nothing = threading.Event(), threading.Event()
        reads = []
        original = self.store._read

        def read():
            raw = original()
            reads.append(raw)
            found_nothing.set()
            return raw

        def save():
            with holding(self.store.save_lock):
                os.replace(self.path, aside)
                in_the_moment.set()
                found_nothing.wait(10)
                os.replace(aside, self.path)

        thread = threading.Thread(target=save, daemon=True)
        thread.start()
        self.assertTrue(in_the_moment.wait(10))
        self.store._read = read
        seen = look()
        thread.join(10)
        self.assertEqual(len(reads), 2, "found nothing, then read again")
        self.assertIsNone(reads[0], "the first read met the empty name")
        return seen

    def test_a_load_in_a_saves_rename_reads_the_file(self):
        saved = StoreContents((job("A", 1),))
        self.store.save(saved)
        result = self.while_a_save_renames(self.store.load)
        self.assertIs(result.status, LoadStatus.LOADED)
        self.assertEqual(result.contents, saved)

    def test_a_fingerprint_in_a_saves_rename_is_the_files(self):
        written = self.store.save(StoreContents((job("A", 1),)))
        self.assertEqual(self.while_a_save_renames(self.store.fingerprint), written)

    def test_no_file_beside_a_lock_nobody_holds_is_a_fresh_store(self):
        self.store.save(StoreContents((job("A", 1),)))
        self.path.unlink()
        self.assertIs(self.store.load().status, LoadStatus.FRESH)
        self.assertIsNone(self.store.fingerprint())

    def test_a_load_with_no_file_and_no_lock_makes_nothing(self):
        self.assertIs(self.store.load().status, LoadStatus.FRESH)
        self.assertIsNone(self.store.fingerprint())
        self.assertEqual(list(self.tmp.iterdir()), [])


class ExportImportTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")

    def test_export_then_import_into_another_store(self):
        a, b = job("A", 1), job("B", 2)
        target = self.tmp / "jobs-export.json"
        self.store.export_jobs([a, b], target)
        other = ConfigStore(self.tmp / "other" / "mirrorhold.json")
        imported = other.read_export(target)
        self.assertEqual(imported, JobList((a, b), ()))
        merged, outcome = merge_imported(StoreContents(), imported.jobs, groups=imported.groups)
        self.assertEqual(outcome.added, (a.job_id, b.job_id))
        other.save(merged)
        self.assertEqual(other.load().contents.jobs, (a, b))

    def test_merge_outcomes(self):
        a, b = job("A", 1), job("B", 2)
        contents = StoreContents((a, b))
        merged, outcome = merge_imported(contents, (a, replace(b, name="B2")))
        self.assertEqual(outcome.unchanged, (a.job_id,))
        self.assertEqual(outcome.conflicts, (b.job_id,))
        self.assertEqual(merged, contents)
        changed_a = replace(a, name="A renamed")
        merged, outcome = merge_imported(contents, (changed_a,), replace_existing=True)
        self.assertEqual(outcome.replaced, (a.job_id,))
        self.assertEqual(merged.job(a.job_id).name, "A renamed")
        self.assertEqual([j.job_id for j in merged.jobs], [a.job_id, b.job_id], "a replacement keeps its place")

    def test_import_refuses_a_bad_file(self):
        for name, text in (("not-json.json", "{"),
                           ("wrong-format.json", json.dumps({"format": "mirrorhold-store", "schema_version": 1,
                                                             "jobs": [], "preferences": {}})),
                           ("bad-job.json", json.dumps({"format": "mirrorhold-jobs", "schema_version": 1,
                                                        "jobs": [{"id": "x"}]}))):
            with self.subTest(name=name):
                path = self.tmp / name
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(StoreError):
                    self.store.read_export(path)

    def test_an_export_with_a_byte_order_mark_is_imported(self):
        a = job("A", 1)
        target = self.tmp / "jobs-export.json"
        self.store.export_jobs([a], target)
        marked = self.tmp / "marked.json"
        marked.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
        self.assertEqual(self.store.read_export(marked), JobList((a,), ()))

    def test_duplicate_identifiers_in_an_export_are_refused(self):
        a = job("A", 1)
        path = self.tmp / "dup.json"
        path.write_text(json.dumps({"format": "mirrorhold-jobs", "schema_version": 1,
                                    "jobs": [a.to_document(), a.to_document()]}), encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.read_export(path)


class GroupsTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.a, self.b, self.c = job("A", 1), job("B", 2), job("C", 3)

    def test_groups_are_saved_and_reloaded_beside_the_jobs(self):
        pictures = new_group("Pictures", (self.b.job_id, self.a.job_id))
        contents = StoreContents((self.a, self.b, self.c), groups=(pictures, new_group("Empty")))
        self.store.save(contents)
        self.assertEqual(self.store.load().contents, contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema_version"], SCHEMA_VERSION)
        self.assertEqual(doc["groups"][0], {"id": pictures.group_id, "name": "Pictures",
                                            "jobs": [self.b.job_id, self.a.job_id]})
        self.assertEqual((contents.group_of(self.a.job_id), contents.group_of(self.c.job_id)), (pictures, None))
        self.assertIs(contents.group_named(" PICTURES "), pictures)

    def test_a_schema_1_file_is_read_with_no_groups(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 1,
                                         "jobs": [self.a.to_document()],
                                         "preferences": Preferences().to_document()}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual((result.contents.jobs, result.contents.groups), ((self.a,), ()))

    def test_the_invariants_that_span_jobs_and_groups(self):
        jobs = (self.a, self.b)
        cases = [((new_group("P", (self.c.job_id,)),), "groups[0].jobs"),
                 ((new_group("P", (self.a.job_id,)), new_group("Q", (self.a.job_id,))), "groups[1].jobs"),
                 ((new_group("Pictures"), new_group(" PICTURES ")), "groups[1].name")]
        for groups, where in cases:
            with self.subTest(where=where):
                with self.assertRaises(ContractError) as ctx:
                    StoreContents(jobs, groups=groups)
                self.assertEqual(ctx.exception.where, where)
        same = new_group("P")
        with self.assertRaises(ContractError):
            StoreContents(jobs, groups=(same, replace(same, name="Q")))
        self.store.save(StoreContents(jobs))
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["groups"] = [new_group("P", (self.c.job_id,)).to_document()]
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.CORRUPT)
        self.assertIn("groups[0].jobs", result.detail)

    def test_moving_jobs_between_groups_renaming_and_removing(self):
        pictures, work = new_group("Pictures", (self.a.job_id,)), new_group("Work")
        contents = StoreContents((self.a, self.b, self.c), groups=(pictures, work))
        moved = contents.regrouped((self.c.job_id, self.a.job_id, self.b.job_id), work.group_id)
        self.assertEqual((moved.group(pictures.group_id).job_ids, moved.group(work.group_id).job_ids),
                         ((), (self.c.job_id, self.a.job_id, self.b.job_id)))
        kept = contents.regrouped((self.b.job_id, self.a.job_id), pictures.group_id)
        self.assertEqual(kept.group(pictures.group_id).job_ids, (self.a.job_id, self.b.job_id),
                         "a job already in the group keeps its place")
        self.assertIsNone(moved.regrouped((self.a.job_id,), None).group_of(self.a.job_id))
        self.assertEqual(moved.without_job(self.c.job_id).group(work.group_id).job_ids, (self.a.job_id, self.b.job_id))
        deleted = moved.without_group(work.group_id)
        self.assertEqual((deleted.jobs, [g.name for g in deleted.groups]), (contents.jobs, ["Pictures"]))
        renamed = contents.with_group(replace(work, name="Office"))
        self.assertEqual([g.name for g in renamed.groups], ["Pictures", "Office"], "a renamed group keeps its place")
        with self.assertRaises(ContractError):
            contents.regrouped((self.a.job_id,), "f" * 32)

    def test_a_save_changing_only_the_guis_remembered_state_keeps_no_previous_copy(self):
        self.store.save(StoreContents((self.a,)))
        self.store.save(StoreContents((self.a, self.b)))
        self.assertEqual(len(self.store.previous_copies()), 1)
        remembered = replace(self.store.load().contents, preferences=Preferences(gui_state={"collapsed_groups": ["x"]}))
        self.store.save(remembered)
        self.assertEqual(len(self.store.previous_copies()), 1, "the copy holding a real change is not pushed out")
        self.assertEqual(self.store.load().contents, remembered)
        self.store.save(remembered.with_job(self.c))
        self.assertEqual(len(self.store.previous_copies()), 2)

    def test_one_order_groups_first_and_each_moves_within_its_level(self):
        d = job("D", 4)
        pictures, work = new_group("Pictures", (self.c.job_id, self.a.job_id)), new_group("Work")
        contents = StoreContents((self.a, self.b, self.c, d), groups=(pictures, work))
        self.assertEqual([j.name for j in contents.in_order()], ["C", "A", "B", "D"])
        groups = contents.with_group_moved(work.group_id, -1)
        self.assertEqual([g.name for g in groups.groups], ["Work", "Pictures"])
        self.assertEqual([j.name for j in groups.in_order()], ["C", "A", "B", "D"], "an empty group holds no job")
        inside = contents.with_job_moved(self.a.job_id, -1)
        self.assertEqual(inside.group(pictures.group_id).job_ids, (self.a.job_id, self.c.job_id))
        self.assertEqual(inside.jobs, contents.jobs, "a move inside a group leaves the file's job order")
        loose = contents.with_job_moved(d.job_id, -1)
        self.assertEqual([j.name for j in loose.jobs], ["A", "D", "C", "B"],
                         "a job in no group trades places with the one in no group above it")
        self.assertEqual([j.name for j in loose.in_order()], ["C", "A", "D", "B"])
        self.assertEqual(loose.groups, contents.groups)
        for edge in ((contents.with_group_moved, pictures.group_id, -1), (contents.with_group_moved, work.group_id, 1),
                     (contents.with_job_moved, self.c.job_id, -1), (contents.with_job_moved, self.a.job_id, 1),
                     (contents.with_job_moved, self.b.job_id, -1), (contents.with_job_moved, d.job_id, 1),
                     (contents.with_job_moved, "f" * 32, 1), (contents.with_group_moved, "f" * 32, 1)):
            with self.subTest(edge=edge[1:]):
                with self.assertRaises(ContractError):
                    edge[0](edge[1], edge[2])

    def test_a_save_changing_only_the_order_keeps_no_previous_copy(self):
        pictures, work = new_group("Pictures", (self.a.job_id, self.b.job_id)), new_group("Work")
        self.store.save(StoreContents((self.a, self.b), groups=(pictures, work)))
        self.store.save(StoreContents((self.a, self.b, self.c), groups=(pictures, work)))
        self.assertEqual(len(self.store.previous_copies()), 1)
        contents = self.store.load().contents
        for moved in (contents.with_group_moved(work.group_id, -1), contents.with_job_moved(self.b.job_id, -1)):
            self.store.save(moved)
            self.assertEqual(len(self.store.previous_copies()), 1, "the copy holding a real change is not pushed out")
            self.assertEqual(self.store.load().contents, moved)
        self.store.save(contents.regrouped((self.c.job_id,), work.group_id))
        self.assertEqual(len(self.store.previous_copies()), 2, "a job moved into another group is a change")


class ShareHoldSchemaTest(TempDirTest):
    """Schema 3: the share hold a value of each job. A job file or an export written before
    it reads with every job holding at half, as every job then did; the next save writes
    the current schema and keeps the file it replaces as a previous copy."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.a = job("A", 1)

    @staticmethod
    def schema_2(j):
        doc = j.to_document()
        del doc["holds"]["share"]
        return doc

    def test_a_schema_2_file_reads_with_the_share_hold_at_half_and_the_next_save_writes_the_current_schema(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 2,
                                         "jobs": [self.schema_2(self.a)], "groups": [],
                                         "preferences": Preferences().to_document()}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(result.contents.jobs, (self.a,))
        self.assertEqual(result.contents.jobs[0].holds.share, 50)
        self.store.save(result.contents)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema_version"], SCHEMA_VERSION)
        self.assertEqual(json.loads(self.store.previous_copies()[0].read_text(encoding="utf-8"))["schema_version"], 2)

    def test_a_schema_2_export_reads_with_the_share_hold_at_half(self):
        target = self.tmp / "export.json"
        target.write_text(json.dumps({"format": "mirrorhold-jobs", "schema_version": 2,
                                      "jobs": [self.schema_2(self.a)], "groups": []}), encoding="utf-8")
        listed = self.store.read_export(target)
        self.assertEqual(listed, JobList((self.a,), ()))
        self.assertEqual(listed.jobs[0].holds.share, 50)

    def test_the_step_writes_the_half_every_job_then_held_at_as_a_literal(self):
        self.assertEqual(MIGRATIONS[2]({"jobs": [{"holds": {}}, {"holds": {"share": 20}}]})["jobs"],
                         [{"holds": {"share": 50}}, {"holds": {"share": 20}}])

    def test_a_share_switched_off_or_adjusted_is_saved_as_it_is(self):
        off = replace(self.a, holds=HoldThresholds(share=None))
        adjusted = replace(job("B", 2), holds=HoldThresholds(share=20))
        self.store.save(StoreContents((off, adjusted)))
        self.assertEqual(self.store.load().contents.jobs, (off, adjusted))
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([j["holds"]["share"] for j in doc["jobs"]], [None, 20])

    def test_a_missing_migration_leaves_the_file_unsupported_not_corrupt(self):
        self.store.save(StoreContents((self.a,)))
        before = self.path.read_text(encoding="utf-8")
        result = ConfigStore(self.path, schema_version=SCHEMA_VERSION + 2, migrations=MIGRATIONS).load()
        self.assertIs(result.status, LoadStatus.UNSUPPORTED)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class ActivitiesSchemaTest(TempDirTest):
    """Schema 4: the choices for activities among the preferences. A job file written before
    them reads with each as it is at first — turns on a drive automatic, the others on; the
    next save writes the current schema and keeps the file it replaces as a previous copy. An
    export carries no preferences and reads as it was."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.a = job("A", 1)

    def test_a_schema_3_file_reads_with_every_choice_as_at_first_and_the_next_save_writes_the_current_schema(self):
        self.path.parent.mkdir(parents=True)
        preferences = Preferences().to_document()
        del preferences["activities"]
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 3,
                                         "jobs": [self.a.to_document()], "groups": [],
                                         "preferences": preferences}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(result.contents.preferences.activities, Activities(DriveTurns.AUTOMATIC, True, True))
        self.store.save(result.contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["preferences"]["activities"]),
                         (SCHEMA_VERSION, {"drive_turns": "automatic", "keep_awake": True, "name_at_shutdown": True,
                                           "drive_kinds": []}))
        self.assertEqual(json.loads(self.store.previous_copies()[0].read_text(encoding="utf-8"))["schema_version"], 3)

    def test_a_choice_switched_off_is_saved_as_it_is(self):
        off = Preferences(activities=Activities(drive_turns=DriveTurns.AT_THE_SAME_TIME, keep_awake=False))
        self.store.save(StoreContents((self.a,), off))
        self.assertEqual(self.store.load().contents.preferences, off)
        before = len(self.store.previous_copies())
        self.store.save(StoreContents((self.a,)))
        self.assertEqual(len(self.store.previous_copies()), before + 1, "a changed choice is a change")

    def test_a_schema_3_export_reads_as_it_was_and_an_export_writes_the_current_schema(self):
        target = self.tmp / "export.json"
        target.write_text(json.dumps({"format": "mirrorhold-jobs", "schema_version": 3,
                                      "jobs": [self.a.to_document()], "groups": []}), encoding="utf-8")
        self.assertEqual(self.store.read_export(target), JobList((self.a,), ()))
        self.store.export_jobs([self.a], target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], sorted(doc)),
                         (SCHEMA_VERSION, ["format", "groups", "jobs", "schema_version"]))


class GroupsExportImportTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "Config" / "mirrorhold.json")
        self.a, self.b, self.c = job("A", 1), job("B", 2), job("C", 3)
        self.pictures = new_group("Pictures", (self.a.job_id, self.b.job_id))
        self.target = self.tmp / "jobs-export.json"

    def test_an_export_carries_the_groups_of_the_jobs_it_holds(self):
        self.store.export_jobs([self.a, self.c], self.target, groups=(self.pictures, new_group("Empty")))
        listed = ConfigStore(self.tmp / "Other" / "mirrorhold.json").read_export(self.target)
        self.assertEqual(listed, JobList((self.a, self.c), (replace(self.pictures, job_ids=(self.a.job_id,)),)))
        doc = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual((doc["format"], doc["schema_version"]), ("mirrorhold-jobs", SCHEMA_VERSION))

    def test_a_schema_1_export_is_read_with_no_groups(self):
        self.target.write_text(json.dumps({"format": "mirrorhold-jobs", "schema_version": 1,
                                           "jobs": [self.a.to_document()]}), encoding="utf-8")
        self.assertEqual(self.store.read_export(self.target), JobList((self.a,), ()))

    def test_an_added_job_joins_its_group_here_and_a_job_already_here_keeps_its_own(self):
        here = new_group("pictures", (self.c.job_id,))
        merged, outcome = merge_imported(StoreContents((self.c, self.b), groups=(here,)), (self.a, self.b),
                                         groups=(self.pictures,))
        self.assertEqual(outcome.added, (self.a.job_id,))
        self.assertEqual(merged.group(here.group_id).job_ids, (self.c.job_id, self.a.job_id), "joined by name")
        self.assertIsNone(merged.group_of(self.b.job_id), "a job already here keeps its group")
        fresh, _ = merge_imported(StoreContents(), (self.a, self.b), groups=(self.pictures,))
        self.assertEqual(fresh.groups, (self.pictures,))
        renamed_here = replace(self.pictures, name="Photos", job_ids=(self.c.job_id,))
        joined, _ = merge_imported(StoreContents((self.c,), groups=(renamed_here,)), (self.a,), groups=(self.pictures,))
        self.assertEqual(joined.group(self.pictures.group_id),
                         replace(renamed_here, job_ids=(self.c.job_id, self.a.job_id)),
                         "joined by identifier; the group here keeps its name")


class RemoteSchemaTest(TempDirTest):
    """Schema 7: a destination may be a remote's folder, and the preferences name the rclone
    configuration remotes are reached through. A file written before them reads with every
    destination a drive's, written as it still is, and no configuration named; a copy that
    knows only schema 6 leaves a schema-7 file untouched rather than move it aside."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)
        self.a = job("A", 1)

    def test_a_schema_6_file_reads_with_no_configuration_and_its_drive_destinations_written_as_they_were(self):
        self.path.parent.mkdir(parents=True)
        preferences = Preferences().to_document()
        del preferences["remotes"]
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 6,
                                         "jobs": [self.a.to_document()], "groups": [],
                                         "preferences": preferences}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual((result.contents.jobs, result.contents.preferences.remotes), ((self.a,), RemoteSettings()))
        self.store.save(result.contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["preferences"]["remotes"]),
                         (SCHEMA_VERSION, {"configuration": None, "unlock": "none", "unlock_command": []}))
        self.assertEqual(doc["jobs"][0]["destinations"], self.a.to_document()["destinations"])
        self.assertEqual(sorted(doc["jobs"][0]["destinations"][0]["endpoint"]), ["path", "volume"])

    def test_a_remote_destination_and_the_configuration_are_saved_read_back_and_exported(self):
        remote = new_job("R", Endpoint(identity(1), "Work"), [RemoteEndpoint("cloud", "Backups/Work"),
                                                             Endpoint(identity(2), "B")])
        settings = RemoteSettings("C:\\Users\\me\\rclone.conf", Unlock.COMMAND, ("pass", "show", "rclone"))
        contents = StoreContents((remote,), Preferences(remotes=settings))
        self.store.save(contents)
        self.assertEqual(self.store.load().contents, contents)
        target = self.tmp / "export.json"
        self.store.export_jobs([remote], target)
        self.assertEqual(self.store.read_export(target).jobs, (remote,))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["jobs"][0]["destinations"][0]["endpoint"],
                         {"remote": "cloud", "folder": "Backups/Work"})

    def test_a_copy_that_knows_only_schema_6_leaves_the_file_untouched(self):
        self.store.save(StoreContents((new_job("R", Endpoint(identity(1), "W"), [RemoteEndpoint("cloud", "W")]),)))
        before = self.path.read_bytes()
        older = ConfigStore(self.path, schema_version=6, migrations={k: v for k, v in MIGRATIONS.items() if k < 6})
        self.assertIs(older.load().status, LoadStatus.UNSUPPORTED)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_destination_of_neither_kind_is_a_corrupt_file(self):
        self.store.save(StoreContents((self.a,)))
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["jobs"][0]["destinations"][0]["endpoint"] = {"remote": "cloud", "folder": "W", "path": ""}
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIs(self.store.load().status, LoadStatus.CORRUPT)


class ScheduleFormsSchemaTest(TempDirTest):
    """Schema 8: a schedule may run monthly, at several times a day or every N hours. A
    schema-7 file's daily and weekly schedules read and save with their text unchanged; a
    copy that knows only schema 7 leaves a schema-8 file untouched rather than move it aside."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)

    def test_a_schema_7_file_keeps_its_schedules_as_they_were(self):
        daily = replace(job("A", 1), triggers=Triggers(False, "daily 21:00"))
        weekly = replace(job("B", 3), triggers=Triggers(True, "weekly mon,thu 07:30"))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 7,
                                         "jobs": [daily.to_document(), weekly.to_document()], "groups": [],
                                         "preferences": Preferences().to_document()}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual(result.contents.jobs, (daily, weekly))
        self.store.save(result.contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], [j["triggers"]["schedule"] for j in doc["jobs"]]),
                         (SCHEMA_VERSION, ["daily 21:00", "weekly mon,thu 07:30"]))

    def test_the_new_forms_are_saved_read_back_and_exported(self):
        forms = ("daily 09:00,21:00", "monthly last 22:00", "monthly 15 06:30", "daily every 2h 08:00-23:00",
                 "weekly mon,tue,wed,thu,fri every 1h 09:00-18:00", "weekly sat every 3h 22:00-06:00")
        jobs = tuple(replace(job(f"J{i}", 2 * i + 1), triggers=Triggers(False, text)) for i, text in enumerate(forms))
        self.store.save(StoreContents(jobs))
        self.assertEqual(self.store.load().contents.jobs, jobs)
        target = self.tmp / "export.json"
        self.store.export_jobs(list(jobs), target)
        self.assertEqual(self.store.read_export(target).jobs, jobs)
        self.assertEqual([j.triggers.schedule for j in jobs], list(forms))

    def test_a_copy_that_knows_only_schema_7_leaves_the_file_untouched(self):
        self.store.save(StoreContents((replace(job("A", 1), triggers=Triggers(False, "monthly last 22:00")),)))
        before = self.path.read_bytes()
        older = ConfigStore(self.path, schema_version=7, migrations={k: v for k, v in MIGRATIONS.items() if k < 7})
        self.assertIs(older.load().status, LoadStatus.UNSUPPORTED)
        self.assertEqual(self.path.read_bytes(), before)


class CloudMarkSchemaTest(TempDirTest):
    """Schema 9: a drive may be marked a cloud drive. A schema-8 file's jobs, schedules and
    marks read and save unchanged; a copy that knows only schema 8 leaves a schema-9 file
    untouched rather than move it aside."""

    CLOUD = "volume:00000000-0000-4000-8000-0000000000c1"
    DISK = "disk:gpt:00000000-0000-4000-8000-0000000000d1"

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "Config" / "mirrorhold.json"
        self.store = ConfigStore(self.path)

    def test_a_schema_8_file_keeps_its_jobs_and_marks_as_they_were(self):
        monthly = replace(job("A", 1), triggers=Triggers(False, "monthly last 22:00"))
        marks = (DriveMark(self.DISK, "drive D:", DriveKind.SOLID),)
        preferences = Preferences(activities=Activities(drive_kinds=marks))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"format": "mirrorhold-store", "schema_version": 8,
                                         "jobs": [monthly.to_document()], "groups": [],
                                         "preferences": preferences.to_document()}), encoding="utf-8")
        result = self.store.load()
        self.assertIs(result.status, LoadStatus.MIGRATED)
        self.assertEqual((result.contents.jobs, result.contents.preferences), ((monthly,), preferences))
        self.store.save(result.contents)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["jobs"], doc["preferences"]["activities"]["drive_kinds"]),
                         (SCHEMA_VERSION, [monthly.to_document()],
                          [{"drive": self.DISK, "name": "drive D:", "kind": "solid"}]))

    def test_a_cloud_drives_mark_is_saved_and_read_back(self):
        marks = (DriveMark(self.CLOUD, "drive G:", DriveKind.CLOUD),)
        contents = StoreContents((job("A", 1),), Preferences(activities=Activities(drive_kinds=marks)))
        self.store.save(contents)
        self.assertEqual(self.store.load().contents, contents)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["preferences"]["activities"]["drive_kinds"],
                         [{"drive": self.CLOUD, "name": "drive G:", "kind": "cloud"}])

    def test_a_copy_that_knows_only_schema_8_leaves_the_file_untouched(self):
        marks = (DriveMark(self.CLOUD, "drive G:", DriveKind.CLOUD),)
        self.store.save(StoreContents((job("A", 1),), Preferences(activities=Activities(drive_kinds=marks))))
        before = self.path.read_bytes()
        older = ConfigStore(self.path, schema_version=8, migrations={k: v for k, v in MIGRATIONS.items() if k < 8})
        self.assertIs(older.load().status, LoadStatus.UNSUPPORTED)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
