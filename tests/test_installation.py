"""Installation: the copy scheduled tasks and the Start menu entry run.

Installing from this working copy into a temporary folder — the packages without their
caches, the licence, the icon, the pinned engine, the record of what was installed and
for which store, the Start menu entry (written into the temporary folder, naming the
icon) and the tasks, reconciled in a scheduler kept in memory; a copy whose icon cannot
be drawn installed without one; the installed copy running by itself and serving the
store it was installed for; updating by installing again; the refusals that leave
everything as it was (a copy in use, a run in progress, the installed copy itself,
another store, a working copy without its licence);
uninstalling, which leaves the store untouched. The installation record's two kinds and
schema 1, the build record and the self-invocation seam. A portable release faked in a
temporary folder: recognising itself and serving its per-user store, set up where it lies,
its entry showing the icon it ships, a newer release repointing everything, a moved one
reading as gone, saving from a copy that is not set up, removing the setup — or, as the
setup program's uninstaller asks, removing it only where this release is the one set up —
and the refusals of one installation per account.

Nothing here writes the user's Start menu, the real installed copy, Task Scheduler or the
account's per-user data folder."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

from support import PROJECT, TempDirTest, endpoint, volume
from test_automation import NS, MemoryScheduler, parse
from test_engine_contract import INSTALLED

from mirrorhold import cli
from mirrorhold.automation.reconcile import AutomationState, apply_automation
from mirrorhold.contract_errors import ContractError
from mirrorhold.engine.rclone import PINNED_RELEASE
from mirrorhold.installation.installer import InstallError, install, installation_state, uninstall
from mirrorhold.installation.manifest import (MANIFEST_SCHEMA, InstalledCopy, InstallKind, read_installation,
                                              write_installation)
from mirrorhold.installation.release import Release, read_release, release_name, write_release
from mirrorhold.installation.shortcut import APP_ID, Shortcut, read_shortcut, write_shortcut
from mirrorhold.invocation import core_arguments, gui_arguments, windowless_interpreter
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.jobs.model import Triggers, new_job
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import ICON_FILE_NAME, CopyKind, copy_kind, runtime_layout
from mirrorhold_gui.session import GuiSession
from mirrorhold.records.record import utc_now
from mirrorhold.records.status import LiveStatus, StatusBoard

WINDOWS = sys.platform == "win32"
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROM_THE_FOLDER = "import os, sys; sys.path.insert(0, os.getcwd()); "


def code_in_folder(code):
    """This interpreter running ``code`` in the folder it is started in, importing that
    folder's packages as a copy's own interpreter does there. A release's interpreter reads
    its search path from a file and never adds the folder it starts in, so the folder is put
    first here; any other interpreter imports from it already."""
    return [sys.executable, "-c", FROM_THE_FOLDER + code]


def module_in_folder(module, *args):
    """``-m module args`` in the folder it is started in, importing as ``code_in_folder``."""
    run = f"import runpy; runpy.run_module({module!r}, run_name='__main__', alter_sys=True)"
    return code_in_folder(run) + list(args)


@unittest.skipUnless(INSTALLED and WINDOWS, "installing copies the pinned engine and writes a Windows shortcut")
class InstallTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "state" / "store.json")
        self.job = new_job("Photos", endpoint(volume(1, "S"), "Photos"), [endpoint(volume(2, "D"), "Backup")],
                           triggers=Triggers(True, "daily 21:00"))
        self.store.save(StoreContents((self.job,)))
        self.target = self.tmp / "installed"
        self.entry = self.tmp / "Start Menu" / "MirrorHold.lnk"
        self.scheduler = MemoryScheduler()

    def install(self, **overrides):
        options = dict(target=self.target, shortcut=self.entry, scheduler=self.scheduler)
        options.update(overrides)
        return install(self.store.path, self.store.load().contents.jobs, **options)

    def installed_cli(self, *args):
        """The installed copy's own command line, started in its folder as a task starts it."""
        done = subprocess.run(module_in_folder("mirrorhold", *args), cwd=self.target, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", creationflags=FLAGS, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_install_copies_records_writes_the_entry_and_registers_the_tasks(self):
        result = self.install()
        self.assertEqual(result.problems, ())
        for package in ("mirrorhold", "mirrorhold_gui"):
            self.assertTrue((self.target / package / "__init__.py").is_file())
        self.assertEqual(list(self.target.rglob("__pycache__")), [])
        self.assertTrue((self.target / "vendor" / PINNED_RELEASE / "rclone.exe").is_file())
        copy = read_installation(self.target)
        self.assertTrue(copy.serves(self.store.path))
        self.assertEqual((copy.source, copy.interpreter.name.lower()), (PROJECT, "pythonw.exe"))
        shortcut = read_shortcut(self.entry)
        self.assertEqual((Path(shortcut.target), shortcut.working_dir, shortcut.app_id),
                         (copy.interpreter, str(self.target), APP_ID))
        self.assertEqual(shortcut.arguments, subprocess.list2cmdline(["-m", "mirrorhold_gui", "--store",
                                                                      str(copy.store)]))
        self.assertEqual(len(self.scheduler.xml), 2)
        for xml in self.scheduler.xml.values():
            action = parse(xml).find("t:Actions/t:Exec", NS)
            self.assertEqual(action.find("t:WorkingDirectory", NS).text, str(self.target))
        state = installation_state(self.store.path, target=self.target)
        self.assertEqual((state.installed, state.serves_store, state.running_installed), (True, True, False))

    def test_the_installed_copy_carries_the_licence_and_the_icon_its_entry_shows(self):
        result = self.install()
        self.assertEqual(result.problems, ())
        self.assertEqual((self.target / "LICENSE").read_bytes(), (PROJECT / "LICENSE").read_bytes())
        icon = self.target / ICON_FILE_NAME
        self.assertEqual(icon.read_bytes()[:4], b"\x00\x00\x01\x00")          # an icon file's header
        self.assertEqual(read_shortcut(self.entry).icon, str(icon))

    def test_a_copy_whose_icon_cannot_be_drawn_is_installed_without_one_and_says_so(self):
        work = self.tmp / "work"                                              # no tools/ to draw with
        for package in ("mirrorhold", "mirrorhold_gui"):
            shutil.copytree(PROJECT / package, work / package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(PROJECT / "LICENSE", work / "LICENSE")
        engine = Path("vendor") / PINNED_RELEASE / "rclone.exe"
        (work / engine).parent.mkdir(parents=True)
        shutil.copyfile(PROJECT / engine, work / engine)
        result = self.install(source=work)
        self.assertEqual(len(result.problems), 1, result.problems)
        self.assertTrue(result.problems[0].startswith("the icon could not be drawn"), result.problems)
        self.assertEqual(((self.target / ICON_FILE_NAME).exists(), (self.target / "LICENSE").is_file()), (False, True))
        self.assertEqual(read_shortcut(self.entry).icon, "")
        self.assertTrue(read_installation(self.target).serves(self.store.path))

    def test_the_installed_copy_runs_by_itself_and_serves_its_store(self):
        self.install()
        code, out = self.installed_cli("jobs")                     # no --store: the store it serves
        self.assertEqual(code, 0, out)
        self.assertIn("Photos", out)
        self.assertIn("also daily at 21:00", out)
        code, out = self.installed_cli("installation")
        self.assertIn(f"installed at {self.target}", out)
        code, out = self.installed_cli("install")
        self.assertEqual(code, 1)
        self.assertIn("this is the installed copy", out)

    def test_installing_again_replaces_the_copy(self):
        self.install()
        (self.target / "stray.txt").write_text("left from an older copy", encoding="utf-8")
        first = read_installation(self.target).installed_at
        time.sleep(1.1)
        self.install()
        self.assertFalse((self.target / "stray.txt").exists())
        self.assertNotEqual(read_installation(self.target).installed_at, first)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("installed")), ["installed"])

    def test_a_copy_in_use_is_refused_and_left_as_it_was(self):
        self.install()
        (self.target / "stray.txt").write_text("still here", encoding="utf-8")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.target,
                                  creationflags=FLAGS)
        try:
            time.sleep(0.5)
            with self.assertRaises(InstallError) as caught:
                self.install()
            self.assertIn("in use", str(caught.exception))
            self.assertTrue((self.target / "stray.txt").exists())
            with self.assertRaises(InstallError):
                uninstall(target=self.target, shortcut=self.entry, scheduler=self.scheduler)
            self.assertTrue(self.target.is_dir())
            self.assertEqual(len(self.scheduler.xml), 2)
        finally:
            holder.kill()
            holder.wait(30)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("installed")), ["installed"])

    def test_refused_while_a_run_is_in_progress(self):
        board = StatusBoard(runtime_layout(self.store.path), LiveStatus("a" * 32, os.getpid(), self.job.job_id,
                                                                        self.job.name, "manual", utc_now()))
        board.open()
        try:
            with self.assertRaises(InstallError) as caught:
                self.install()
            self.assertIn("a run is in progress", str(caught.exception))
            self.assertFalse(self.target.exists())
        finally:
            board.close()

    def test_refused_from_the_installed_copy_and_for_another_store(self):
        self.install()
        with self.assertRaises(InstallError):
            install(self.store.path, (), source=self.target, target=self.tmp / "nested", shortcut=self.entry,
                    scheduler=self.scheduler)
        other = ConfigStore(self.tmp / "other" / "store.json")
        with self.assertRaises(InstallError) as caught:
            install(other.path, (), target=self.target, shortcut=self.entry, scheduler=self.scheduler)
        self.assertIn("serves another job file", str(caught.exception))
        self.assertFalse(installation_state(other.path, target=self.target).serves_store)

    def test_uninstall_removes_the_copy_the_entry_and_the_tasks_and_keeps_the_store(self):
        self.install()
        before = self.store.path.read_bytes()
        result = uninstall(target=self.target, shortcut=self.entry, scheduler=self.scheduler)
        self.assertEqual((result.removed_copy, result.removed_tasks, result.problems), (True, 2, ()))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.entry.exists())
        self.assertEqual((self.scheduler.xml, self.scheduler.folder_removed), ({}, True))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(installation_state(self.store.path, target=self.target).installed)


class WorkingCopyLicenceTest(TempDirTest):
    """Every copy carries the licence, so a working copy without it installs nothing."""

    def test_a_working_copy_without_its_licence_is_refused_with_nothing_changed(self):
        work = self.tmp / "work"
        work.mkdir()
        store = ConfigStore(self.tmp / "state" / "store.json")
        store.save(StoreContents(()))
        scheduler, entry = MemoryScheduler(), self.tmp / "Start Menu" / "MirrorHold.lnk"
        with self.assertRaises(InstallError) as caught:
            install(store.path, (), source=work, target=work / "installed", shortcut=entry, scheduler=scheduler)
        self.assertIn("the working copy holds no LICENSE", str(caught.exception))
        self.assertEqual((list(work.iterdir()), scheduler.xml, entry.exists()), ([], {}, False))


class InstallationRecordTest(TempDirTest):
    """The installation record's two kinds and its schema 1, the build record, the seam."""

    def test_a_schema_1_record_reads_as_a_source_installation(self):
        folder = self.tmp / "installed"
        folder.mkdir()
        (folder / "installation.json").write_text(json.dumps(
            {"format": "mirrorhold-installation", "schema_version": 1, "version": "1.0.0",
             "installed_at": "2026-10-06T12:00:00+00:00", "store": "X:\\s.json", "interpreter": "X:\\pythonw.exe",
             "source": "X:\\work"}), encoding="utf-8")
        copy = read_installation(folder)
        self.assertEqual((copy.kind, copy.root, copy.source, copy.label), (InstallKind.SOURCE, folder,
                                                                           Path("X:\\work"), None))

    def test_both_kinds_round_trip_and_a_portable_record_lives_beside_its_store(self):
        store = self.tmp / "data" / "mirrorhold.json"
        portable = InstalledCopy(self.tmp / "release", "1.0.0", "2026-10-11T12:00:00+00:00", store,
                                 Path("X:\\pythonw.exe"), None, InstallKind.PORTABLE, "2026.10.1")
        write_installation(portable)
        self.assertEqual(read_installation(store.parent), portable)
        self.assertFalse((self.tmp / "release").exists())
        source = InstalledCopy(self.tmp / "installed", "1.0.0", "2026-10-11T12:00:00+00:00", store,
                               Path("X:\\pythonw.exe"), self.tmp)
        write_installation(source)
        self.assertEqual(read_installation(self.tmp / "installed"), source)
        doc = json.loads((self.tmp / "installed" / "installation.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["schema_version"], doc["kind"], doc["program"]),
                         (MANIFEST_SCHEMA, "source", str(self.tmp / "installed")))

    def test_a_record_of_another_kind_or_with_an_unknown_key_is_unreadable(self):
        folder = self.tmp / "data"
        folder.mkdir()
        good = {"format": "mirrorhold-installation", "schema_version": MANIFEST_SCHEMA, "kind": "portable",
                "version": "1.0.0", "label": None, "installed_at": "t", "store": "s", "interpreter": "i", "program": "p",
                "source": None}
        for bad in (dict(good, kind="setup"), dict(good, extra=1), dict(good, schema_version=MANIFEST_SCHEMA + 1),
                    dict(good, kind="source")):
            (folder / "installation.json").write_text(json.dumps(bad), encoding="utf-8")
            self.assertIsNone(read_installation(folder), bad)
        (folder / "installation.json").write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(read_installation(folder).root, Path("p"))

    def test_the_build_record_is_strict_and_an_unreadable_one_still_marks_a_release(self):
        root = self.tmp / "release"
        root.mkdir()
        self.assertIsNone(read_release(root))
        self.assertIs(copy_kind(root), CopyKind.WORKING)
        write_release(Release(root, "2026.10.1", "3.14.8"))
        self.assertEqual(read_release(root), Release(root, "2026.10.1", "3.14.8"))
        self.assertIs(copy_kind(root), CopyKind.PORTABLE)
        for label in ("2026.13.1", "2026.10.0", "2026.010.1", "26.10.1", "2026.10"):
            with self.assertRaises(ContractError, msg=label):
                write_release(Release(root, label, "3.14.8"))
        (root / "build.json").write_text("{broken", encoding="utf-8")
        release = read_release(root)
        self.assertEqual((release.label, release.readable, copy_kind(root)), (None, False, CopyKind.PORTABLE))
        self.assertEqual(release_name(release), "release unknown (its build record cannot be read)")
        self.assertEqual(release_name(Release(root, "2026.10.1")), "release 2026.10.1")
        self.assertEqual(release_name(None), "run from source")

    def test_the_build_record_names_the_label_and_the_interpreter_and_no_commit(self):
        """Nothing in a release may depend on which repository built it, so that a rebuild
        from the published repository is the same zip."""
        root = self.tmp / "release"
        root.mkdir()
        write_release(Release(root, "2026.11.1", python="3.14.8"))
        document = json.loads((root / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"format", "schema_version", "label", "python"})
        self.assertEqual((document["label"], document["python"]), ("2026.11.1", "3.14.8"))
        (root / "build.json").write_text(json.dumps(dict(document, commit="a" * 40)), encoding="utf-8")
        self.assertFalse(read_release(root).readable)
        self.assertIs(copy_kind(root), CopyKind.PORTABLE)

    def test_the_seam_composes_how_a_copy_starts_itself(self):
        self.assertEqual(core_arguments("--store", Path("X:\\s.json"), "run", "j"),
                         ["-m", "mirrorhold", "--store", "X:\\s.json", "run", "j"])
        self.assertEqual(gui_arguments("--store", "s"), ["-m", "mirrorhold_gui", "--store", "s"])
        folder = self.tmp / "env"
        folder.mkdir()
        (folder / "python.exe").write_bytes(b"")
        self.assertEqual(windowless_interpreter(folder / "python.exe"), folder / "python.exe")
        (folder / "pythonw.exe").write_bytes(b"")
        self.assertEqual(windowless_interpreter(folder / "python.exe"), folder / "pythonw.exe")


@unittest.skipUnless(WINDOWS, "setting a release up writes a Windows shortcut")
class PortableReleaseTest(TempDirTest):
    """A portable release, faked in a temporary folder: set up where it lies, its store and
    record in a per-user data folder of the test's own, never the account's."""

    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.tmp / "local" / "MirrorHold" / "mirrorhold.json")
        self.job = new_job("Photos", endpoint(volume(1, "S"), "Photos"), [endpoint(volume(2, "D"), "Backup")],
                           triggers=Triggers(True, "daily 21:00"))
        self.store.save(StoreContents((self.job,)))
        self.entry = self.tmp / "Start Menu" / "MirrorHold.lnk"
        self.scheduler = MemoryScheduler()

    def release(self, name="MirrorHold 2026.10.1", label="2026.10.1", *, packages=False) -> Path:
        root = self.tmp / name
        root.mkdir()
        if packages:
            for package in ("mirrorhold", "mirrorhold_gui"):
                shutil.copytree(PROJECT / package, root / package,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        write_release(Release(root, label, "3.14.8"))
        return root

    def set_up(self, release, **overrides):
        options = dict(source=release, shortcut=self.entry, scheduler=self.scheduler, interpreter=sys.executable)
        options.update(overrides)
        return install(self.store.path, self.store.load().contents.jobs, **options)

    def state(self, release):
        return installation_state(self.store.path, source=release, shortcut=self.entry)

    def working_dirs(self):
        return {parse(xml).find("t:Actions/t:Exec/t:WorkingDirectory", NS).text for xml in self.scheduler.xml.values()}

    def test_a_release_recognises_itself_and_serves_the_per_user_store(self):
        release = self.release(packages=True)
        env = dict(os.environ, LOCALAPPDATA=str(self.tmp / "local"), APPDATA=str(self.tmp / "roaming"),
                   MIRRORHOLD_NOTIFICATIONS="off")
        script = ("from mirrorhold.locations import copy_kind, default_store_path; "
                  "print(copy_kind().value); print(default_store_path())")
        done = subprocess.run(code_in_folder(script), cwd=release, capture_output=True, text=True, env=env,
                              creationflags=FLAGS, timeout=120)
        self.assertEqual(done.stdout.split("\n")[:2], ["portable", str(self.store.path)], done.stderr)
        done = subprocess.run(module_in_folder("mirrorhold", "installation"), cwd=release, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env, creationflags=FLAGS, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"not set up: jobs run only when started, and there is no Start menu entry; jobs and records in "
                      f"{self.store.path.parent}", done.stdout)

    def test_setting_up_writes_the_record_the_entry_and_the_tasks_and_nothing_into_the_release(self):
        release = self.release()
        before = sorted(p.name for p in release.iterdir())
        result = self.set_up(release)
        self.assertEqual(result.problems, ())
        copy = read_installation(self.store.path.parent)
        self.assertEqual((copy.kind, copy.root, copy.label, copy.source), (InstallKind.PORTABLE, release, "2026.10.1",
                                                                           None))
        self.assertTrue(copy.serves(self.store.path))
        self.assertEqual(copy.interpreter.name.lower(), "pythonw.exe")
        shortcut = read_shortcut(self.entry)
        self.assertEqual((Path(shortcut.target), shortcut.working_dir, shortcut.app_id),
                         (copy.interpreter, str(release), APP_ID))
        self.assertEqual(shortcut.arguments, subprocess.list2cmdline(["-m", "mirrorhold_gui", "--store",
                                                                      str(copy.store)]))
        self.assertEqual((len(self.scheduler.xml), self.working_dirs()), (2, {str(release)}))
        self.assertEqual(sorted(p.name for p in release.iterdir()), before)
        state = self.state(release)
        self.assertEqual((state.portable, state.here, state.gone, state.serves_store), (True, True, False, True))
        self.assertEqual(state.release.label, "2026.10.1")

    def test_the_entry_shows_the_icon_a_release_ships_and_its_interpreters_without_one(self):
        plain, shipping = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        (shipping / ICON_FILE_NAME).write_bytes(b"\x00\x00\x01\x00")
        self.set_up(plain)
        self.assertEqual(read_shortcut(self.entry).icon, "")
        self.set_up(shipping)
        self.assertEqual(read_shortcut(self.entry).icon, str(self.store.path.parent / ICON_FILE_NAME))

    def test_the_entry_keeps_its_icon_beside_the_job_file_when_the_release_is_moved(self):
        plain, shipping = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        (shipping / ICON_FILE_NAME).write_bytes(b"\x00\x00\x01\x00shipped")
        kept = self.store.path.parent / ICON_FILE_NAME
        self.set_up(shipping)
        self.assertEqual(kept.read_bytes(), b"\x00\x00\x01\x00shipped")
        moved = shipping.rename(self.tmp / "moved")
        self.assertEqual(Path(read_shortcut(self.entry).icon).read_bytes(), b"\x00\x00\x01\x00shipped")
        self.set_up(plain)
        self.assertEqual((read_shortcut(self.entry).icon, kept.exists()), ("", False))
        self.set_up(moved)
        result = uninstall(source=moved, shortcut=self.entry, scheduler=self.scheduler, store_path=self.store.path)
        self.assertEqual(result.problems, ())
        self.assertEqual((kept.exists(), (moved / ICON_FILE_NAME).is_file()), (False, True))

    def test_an_icon_that_cannot_be_refreshed_beside_the_job_file_is_named_as_it_is(self):
        older, newer = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        for release in (older, newer):
            (release / ICON_FILE_NAME).write_bytes(b"\x00\x00\x01\x00" + release.name.encode())
        kept = self.store.path.parent / ICON_FILE_NAME
        self.set_up(older)
        os.chmod(kept, stat.S_IREAD)                      # Windows refuses to replace a read-only file
        self.addCleanup(os.chmod, kept, stat.S_IWRITE | stat.S_IREAD)
        result = self.set_up(newer)
        self.assertTrue(any(p.startswith("the icon could not be kept beside the job file") for p in result.problems),
                        result.problems)
        self.assertEqual(read_shortcut(self.entry).icon, str(kept))
        self.assertEqual(kept.read_bytes(), b"\x00\x00\x01\x00" + older.name.encode())
        self.assertEqual([p.name for p in kept.parent.iterdir() if p.name.endswith(".writing")], [])

    def junction(self, link: Path, target: Path) -> Path:
        """``link``, a directory junction to ``target``, as a package manager makes one."""
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        return link

    def test_a_release_reached_through_a_junction_is_set_up_by_that_name_and_serves_the_next_release_there(self):
        older, newer = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        current = self.junction(self.tmp / "current", older)
        self.set_up(current)
        copy = read_installation(self.store.path.parent)
        self.assertEqual((copy.root, read_shortcut(self.entry).working_dir, self.working_dirs()),
                         (current, str(current), {str(current)}))
        os.rmdir(current)
        self.junction(current, newer)
        state = self.state(current)
        self.assertEqual((state.here, state.gone, state.release.label), (True, False, "2026.10.2"))
        self.assertFalse(self.state(newer).here)
        registered = dict(self.scheduler.xml)
        outcome = apply_automation(self.store.path, self.store.load().contents.jobs, root=self.store.path.parent,
                                   scheduler=self.scheduler)
        self.assertEqual((outcome.state, outcome.result.registered, self.scheduler.xml),
                         (AutomationState.APPLIED, (), registered))

    def test_a_copy_reached_through_a_junction_knows_itself_by_that_name(self):
        release = self.release(packages=True)
        current = self.junction(self.tmp / "current", release)
        env = dict(os.environ, LOCALAPPDATA=str(self.tmp / "local"), MIRRORHOLD_NOTIFICATIONS="off")
        code = "from mirrorhold.locations import PROJECT_ROOT; print(PROJECT_ROOT)"
        done = subprocess.run(code_in_folder(code), cwd=current, capture_output=True, text=True, env=env,
                              creationflags=FLAGS, timeout=120)
        self.assertEqual(done.stdout.strip(), str(current), done.stderr)

    def test_a_newer_release_sees_the_older_one_set_up_and_setting_it_up_repoints_everything(self):
        older, newer = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        self.set_up(older)
        state = self.state(newer)
        self.assertEqual((state.here, state.gone, state.copy.root), (False, False, older))
        result = self.set_up(newer)
        self.assertEqual(len(result.automation.result.registered), 2)
        self.assertEqual(self.working_dirs(), {str(newer)})
        self.assertEqual(read_shortcut(self.entry).working_dir, str(newer))
        self.assertTrue(self.state(newer).here)
        self.assertFalse(self.state(older).here)

    def test_a_release_that_was_moved_reads_as_gone_and_its_tasks_are_left_alone(self):
        release = self.release()
        self.set_up(release)
        release.rename(self.tmp / "moved")
        state = self.state(self.release("MirrorHold 2026.10.2", "2026.10.2"))
        self.assertTrue(state.gone)
        registered = dict(self.scheduler.xml)
        outcome = apply_automation(self.store.path, (), root=self.store.path.parent, scheduler=self.scheduler)
        self.assertEqual(outcome.state, AutomationState.GONE)
        self.assertIn(f"the copy of MirrorHold they use is gone ({release})", outcome.describe())
        self.assertEqual(self.scheduler.xml, registered)

    def test_saving_from_a_copy_that_is_not_set_up_reconciles_toward_the_one_that_is(self):
        older, newer = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        self.set_up(older)
        session = GuiSession(self.store.path, volume_source=FixedVolumeSource(()), sync_roots=(),
                             scheduler=self.scheduler, shortcut=self.entry, source=newer)
        session.load()
        session.save(StoreContents((replace(self.job, triggers=Triggers(False, "daily 21:00")),)))
        self.assertEqual((session.automation.state, session.automation.result.removed),
                         (AutomationState.APPLIED, (f"{self.job.job_id}-arrival",)))
        self.assertEqual((list(self.scheduler.xml), self.working_dirs()),
                         ([f"{self.job.job_id}-schedule"], {str(older)}))
        self.assertFalse(session.installation().here)

    def test_removing_the_setup_keeps_the_release_the_store_and_its_records(self):
        release = self.release()
        self.set_up(release)
        before = self.store.path.read_bytes()
        result = uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler, store_path=self.store.path)
        self.assertEqual((result.portable, result.removed_copy, result.removed_tasks, result.problems),
                         (True, False, 2, ()))
        self.assertEqual((self.scheduler.xml, self.scheduler.folder_removed), ({}, True))
        self.assertFalse(self.entry.exists())
        self.assertIsNone(read_installation(self.store.path.parent))
        self.assertTrue((release / "build.json").is_file())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.state(release).installed)

    def test_removing_only_if_set_up_here_removes_the_setup_this_release_holds(self):
        release = self.release()
        self.set_up(release)
        result = uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler, store_path=self.store.path,
                           only_if_here=True)
        self.assertEqual((result.portable, result.not_here, result.removed_tasks, result.problems),
                         (True, False, 2, ()))
        self.assertEqual((self.scheduler.xml, self.entry.exists()), ({}, False))
        self.assertIsNone(read_installation(self.store.path.parent))

    def test_removing_only_if_set_up_here_leaves_another_copys_setup_and_refuses_nothing(self):
        older, newer = self.release(), self.release("MirrorHold 2026.10.2", "2026.10.2")
        self.set_up(older)
        registered = dict(self.scheduler.xml)
        board = StatusBoard(runtime_layout(self.store.path), LiveStatus("a" * 32, os.getpid(), self.job.job_id,
                                                                        self.job.name, "manual", utc_now()))
        board.open()
        try:
            result = uninstall(source=newer, shortcut=self.entry, scheduler=self.scheduler,
                               store_path=self.store.path, only_if_here=True)
        finally:
            board.close()
        self.assertEqual((result.not_here, result.removed_tasks, result.set_up.root), (True, 0, older))
        self.assertEqual((self.scheduler.xml, read_shortcut(self.entry).working_dir), (registered, str(older)))
        self.assertTrue(self.state(older).here)

    def test_removing_only_if_set_up_here_with_nothing_set_up_or_a_source_installation_changes_nothing(self):
        release = self.release()
        result = uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler, store_path=self.store.path,
                           only_if_here=True)
        self.assertEqual((result.not_here, result.set_up), (True, None))
        copy = self.tmp / "work" / "installed"
        copy.mkdir(parents=True)
        write_installation(InstalledCopy(copy, "1.0.0", "2026-10-06T12:00:00+00:00", self.tmp / "work.json",
                                         Path(sys.executable), self.tmp / "work"))
        write_shortcut(self.entry, Shortcut(sys.executable, "-m mirrorhold_gui", str(copy), "source entry"))
        result = uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler, store_path=self.store.path,
                           only_if_here=True)
        self.assertEqual((result.not_here, read_shortcut(self.entry).working_dir), (True, str(copy)))

    def test_the_command_line_of_a_release_not_set_up_removes_nothing_when_asked_only_if_set_up_here(self):
        release = self.release(packages=True)
        env = dict(os.environ, LOCALAPPDATA=str(self.tmp / "local"), APPDATA=str(self.tmp / "roaming"),
                   MIRRORHOLD_NOTIFICATIONS="off")
        done = subprocess.run(module_in_folder("mirrorhold", "uninstall", "--if-set-up-here"), cwd=release,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              creationflags=FLAGS, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "this copy is not the one set up (not set up): nothing was removed")

    def test_a_release_answers_installation_with_status_0_whatever_is_set_up(self):
        """The setup program's uninstaller reads status 0 from ``installation`` as "this copy
        runs": a release answers so set up here, set up elsewhere, gone or not set up."""
        release, other = self.release(packages=True), self.release("MirrorHold 2026.10.2", "2026.10.2")
        env = dict(os.environ, LOCALAPPDATA=str(self.tmp / "local"), APPDATA=str(self.tmp / "roaming"),
                   MIRRORHOLD_NOTIFICATIONS="off")

        def answer():
            done = subprocess.run(module_in_folder("mirrorhold", "installation"), cwd=release, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", env=env, creationflags=FLAGS,
                                  timeout=120)
            return done.returncode, done.stdout.strip()
        self.assertEqual(answer()[0], 0)
        self.set_up(release)
        self.assertEqual(answer()[0], 0)
        self.assertTrue(answer()[1].startswith("set up here:"), answer()[1])
        self.set_up(other)
        self.assertEqual(answer(), (0, f"set up at {other} (release 2026.10.2), not here"))
        other.rename(self.tmp / "moved")
        self.assertEqual(answer(), (0, f"set up at {other} (release 2026.10.2), not here, which no longer exists"))

    def test_a_working_copy_refuses_removing_only_if_set_up_here(self):
        target, err = self.tmp / "work" / "installed", io.StringIO()
        code = cli.main(["--store", str(self.tmp / "work.json"), "uninstall", "--if-set-up-here"], out=io.StringIO(),
                        err=err, install_root=target, shortcut=self.entry, scheduler=self.scheduler,
                        account_store=self.store.path)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("refused: only a release removes its setup only if it is the copy set up", err.getvalue())

    def test_a_run_in_progress_refuses_setting_up_and_removing(self):
        release = self.release()
        board = StatusBoard(runtime_layout(self.store.path), LiveStatus("a" * 32, os.getpid(), self.job.job_id,
                                                                        self.job.name, "manual", utc_now()))
        board.open()
        try:
            for attempt in (lambda: self.set_up(release),
                            lambda: uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler,
                                              store_path=self.store.path)):
                with self.assertRaises(InstallError) as caught:
                    attempt()
                self.assertIn("a run is in progress", str(caught.exception))
            self.assertIsNone(read_installation(self.store.path.parent))
        finally:
            board.close()

    def test_a_source_installation_holding_the_entry_refuses_a_release(self):
        release = self.release()
        copy = self.tmp / "work" / "installed"
        copy.mkdir(parents=True)
        write_installation(InstalledCopy(copy, "1.0.0", "2026-10-06T12:00:00+00:00", self.tmp / "work.json",
                                         Path(sys.executable), self.tmp / "work"))
        write_shortcut(self.entry, Shortcut(sys.executable, "-m mirrorhold_gui", str(copy), "source entry"))
        state = self.state(release)
        self.assertEqual(state.blocker.root, copy)
        for attempt in (lambda: self.set_up(release),
                        lambda: uninstall(source=release, shortcut=self.entry, scheduler=self.scheduler,
                                          store_path=self.store.path)):
            with self.assertRaises(InstallError) as caught:
                attempt()
            self.assertIn(f"installed from source on this account ({self.tmp / 'work'})", str(caught.exception))
        self.assertEqual(read_shortcut(self.entry).working_dir, str(copy))
        self.assertIsNone(read_installation(self.store.path.parent))

    def test_a_release_set_up_on_the_account_refuses_installing_from_a_working_copy(self):
        release = self.release()
        self.set_up(release)
        work = ConfigStore(self.tmp / "work" / "Config" / "mirrorhold.json")
        target = self.tmp / "work" / "installed"
        state = installation_state(work.path, target=target, account_store=self.store.path)
        self.assertEqual((state.installed, state.blocker.root), (False, release))
        with self.assertRaises(InstallError) as caught:
            install(work.path, (), target=target, shortcut=self.tmp / "other.lnk", scheduler=MemoryScheduler(),
                    account_store=self.store.path)
        self.assertIn(f"automatic runs use the copy of MirrorHold at {release}; remove it there first",
                      str(caught.exception))
        self.assertFalse(target.exists())
        out = io.StringIO()
        code = cli.main(["--store", str(work.path), "installation"], out=out, err=io.StringIO(), install_root=target,
                        shortcut=self.tmp / "other.lnk", account_store=self.store.path)
        self.assertEqual(code, 0)
        self.assertIn(f"not installed from this working copy; automatic runs use the copy of MirrorHold at {release} "
                      "(release 2026.10.1)", out.getvalue())

    def test_the_command_line_sets_a_release_up_and_removes_it(self):
        release = self.release()
        state = installation_state(self.store.path, source=release, shortcut=self.entry)
        self.assertIn("not set up", cli._portable_state_line(state))
        result = self.set_up(release)
        state = installation_state(self.store.path, source=release, shortcut=self.entry)
        self.assertEqual(cli._portable_state_line(state),
                         f"set up here: release 2026.10.1, {result.copy.installed_at}; jobs and records in "
                         f"{self.store.path.parent}")
        other = self.release("MirrorHold 2026.10.2", "2026.10.2")
        state = installation_state(self.store.path, source=other, shortcut=self.entry)
        self.assertEqual(cli._portable_state_line(state), f"set up at {release} (release 2026.10.1), not here")
        (release / "build.json").unlink()
        state = installation_state(self.store.path, source=other, shortcut=self.entry)
        self.assertTrue(cli._portable_state_line(state).endswith(", which no longer exists"))


if __name__ == "__main__":
    unittest.main()
