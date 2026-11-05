"""Remote destinations as the job contract, the command line and the preferences know them:
the remembered password's seal, the unlock command the engine runs, what the engine says of
a remote folder, and the commands that set a remote destination up — none of which runs a
backup onto one.

Remotes are the pinned engine's local and encrypting backends, and a WebDAV remote whose
server does not run, named in a throwaway configuration in each test's temporary folder
whose secrets are invented: nothing reaches the internet, and the user's configuration is
never named. Every command names a store in the test's folder."""
import io
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import TempDirTest, volume
from test_engine_contract import ENGINE, needs_engine

from mirrorhold import exchange
from mirrorhold.cli import EXIT_OK, EXIT_REFUSED, main
from mirrorhold.endpoints.volumes import FixedVolumeSource
from mirrorhold.engine.rclone import Configuration, run_engine
from mirrorhold.engine.remote import unreachable
from mirrorhold.invocation import unlock_command, windowless_interpreter
from mirrorhold.jobs.model import Unlock
from mirrorhold.jobs.store import SCHEMA_VERSION, ConfigStore
from mirrorhold.locations import PROJECT_ROOT, runtime_layout
from mirrorhold.remotes import reach, seal
from mirrorhold.remotes.facts import Reach, SetupState
from mirrorhold.remotes.reach import gather
from mirrorhold.storage.destination import StorageError
from mirrorhold.storage.remote import RemoteStorage

WINDOWS = sys.platform == "win32"
needs_windows = unittest.skipUnless(WINDOWS, "Windows' data protection")
CONFIG_PASS = "invented-config-pass"


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@needs_windows
class SealTest(TempDirTest):
    def test_a_password_is_sealed_read_back_and_forgotten(self):
        path = self.tmp / "remote-password.dpapi"
        seal.remember(path, "pässword with spaces")
        self.assertEqual(seal.remembered(path), "pässword with spaces".encode("utf-8"))
        self.assertNotIn("password".encode("utf-8"), path.read_bytes())
        seal.remember(path, "another")
        self.assertEqual(seal.remembered(path), b"another")
        self.assertTrue(seal.forget(path))
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(seal.SealError, "no password is remembered"):
            seal.remembered(path)

    def test_a_seal_that_was_altered_is_refused(self):
        path = self.tmp / "remote-password.dpapi"
        seal.remember(path, "invented")
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
        with self.assertRaisesRegex(seal.SealError, "Windows refused to open it"):
            seal.remembered(path)

    def test_an_empty_password_or_one_with_a_line_break_is_never_sealed(self):
        path = self.tmp / "remote-password.dpapi"
        for password in ("", "two\nlines", "carriage\rreturn"):
            with self.subTest(password=password):
                with self.assertRaises(seal.SealError):
                    seal.remember(path, password)
                self.assertFalse(path.exists())


class UnlockCommandTest(unittest.TestCase):
    def test_the_command_names_this_copy_and_the_store_and_needs_no_working_folder(self):
        store = Path("Config") / "mirrorhold.json"
        program, isolated, flag, code = unlock_command(store)
        self.assertEqual((program, isolated, flag), (str(windowless_interpreter()), "-I", "-c"))
        self.assertIn(repr(str(PROJECT_ROOT)), code)
        self.assertIn(repr(os.path.abspath(store)), code)
        self.assertIn("'unlock'", code)
        self.assertNotIn("'--seal'", code)
        tried = Path("C:/Config/remote-password.bin.0123456789ab.trial")
        self.assertIn(f"'unlock', '--seal', {str(tried)!r}", unlock_command(store, sealed=tried)[3])


class SealPresenceTest(TempDirTest):
    """A seal not there for the moment a new one is renamed over it is looked for again, by
    an activity asking whether one is remembered and by the unlock reading it."""

    def missing_once(self, target):
        real, missed = exchange.open_shared, []

        def opening(path):
            if Path(path) == Path(target) and not missed:
                missed.append(path)
                raise FileNotFoundError(2, "renamed over", str(path))
            return real(path)
        return mock.patch.object(exchange, "open_shared", side_effect=opening), missed

    @needs_windows
    def test_a_seal_met_as_a_new_one_is_renamed_over_it_is_read(self):
        path = self.tmp / "remote-password.bin"
        seal.remember(path, "pässword")
        patched, missed = self.missing_once(path)
        with patched:
            self.assertEqual(seal.remembered(path), "pässword".encode("utf-8"))
        self.assertEqual(len(missed), 1)

    def test_a_seal_that_appears_while_it_is_looked_for_is_present(self):
        path = self.tmp / "remote-password.bin"
        self.assertFalse(seal.present(path))
        appearing = threading.Timer(0.05, path.write_bytes, (b"sealed",))
        appearing.start()
        self.addCleanup(appearing.join)
        self.assertTrue(seal.present(path))

    @needs_windows
    def test_a_password_on_trial_is_taken_up_whole(self):
        path = self.tmp / "remote-password.bin"
        seal.remember(path, "before")
        tried = seal.trial(path)
        self.assertEqual((tried.parent, tried.name.startswith(path.name), tried.suffix), (path.parent, True, ".trial"))
        seal.remember(tried, "after")
        self.assertEqual(seal.remembered(path), b"before", "a password on trial is not the remembered one")
        seal.take_up(tried, path)
        self.assertEqual(seal.remembered(path), b"after")

    def test_a_trial_a_process_left_is_swept_once_stale_and_a_trial_at_work_is_not(self):
        path = self.tmp / "remote-password.bin"
        left = seal.trial(path)
        left.write_bytes(b"sealed")
        written = left.stat().st_mtime
        seal.sweep_trials(path, now=written + seal.STALE_TRIAL_SECONDS - 1)
        self.assertTrue(left.exists(), "a trial at work")
        seal.sweep_trials(path, now=written + seal.STALE_TRIAL_SECONDS)
        self.assertFalse(left.exists())

    def test_a_seal_being_replaced_is_never_kept_as_none(self):
        path = self.tmp / "remote-password.bin"
        path.write_bytes(b"sealed")
        patched, missed = self.missing_once(path)
        with patched:
            self.assertEqual(seal.kept(path), b"sealed")
        self.assertEqual((len(missed), seal.kept(self.tmp / "none.bin")), (1, None))


@needs_engine
class RemoteCommandTest(TempDirTest):
    """The commands that set up and look at a remote destination, in process against a
    volume mapped onto the test's folder and remotes of a throwaway configuration."""

    def setUp(self):
        super().setUp()
        self.vol_d = self.tmp / "volD"
        (self.vol_d / "Work").mkdir(parents=True)
        (self.vol_d / "Work" / "a.txt").write_text("a", encoding="utf-8")
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_d) + "\\",), label="D"),))
        self.store_path = self.tmp / "Config" / "mirrorhold.json"
        self.remote_dir = self.tmp / "remote"
        self.remote_dir.mkdir()
        (self.tmp / "cryptbase").mkdir()
        self.configuration = self.tmp / "throwaway.conf"
        obscured = run_engine(ENGINE, ["obscure", "loopback-crypt-pass"]).stdout.strip()
        self.configuration.write_text(
            "[loc]\ntype = local\n\n"
            f"[enc]\ntype = crypt\nremote = loc:{(self.tmp / 'cryptbase').as_posix()}\npassword = {obscured}\n\n"
            f"[dav]\ntype = webdav\nurl = http://127.0.0.1:{closed_port()}/\nvendor = rclone\n", encoding="utf-8")
        self.folder = (self.remote_dir / "Work").as_posix()
        self.asked = []

    def cli(self, *args, password=None):
        out, err = io.StringIO(), io.StringIO()

        def ask(prompt):
            self.asked.append(prompt)
            return password

        code = main(["--store", str(self.store_path), *args], volume_source=self.volumes, sync_roots=(), out=out,
                    err=err, ask_password=ask)
        return code, out.getvalue(), err.getvalue()

    def configured(self):
        code, out, err = self.cli("remotes", "--configuration", str(self.configuration))
        self.assertEqual(code, EXIT_OK, out + err)
        return out

    def add(self, *destinations):
        args = ["add", "Work", "--source", str(self.vol_d / "Work"), "--no-survey"]
        for d in destinations:
            args += ["--remote-dest", d] if ":" in d and not d[1:2] == ":" else ["--dest", d]
        return self.cli(*args)

    def encrypt_configuration(self):
        script = self.tmp / "pass word" / "unlock.py"
        script.parent.mkdir()
        script.write_text(f"import sys\nsys.stdout.write({CONFIG_PASS!r} + '\\n')\n", encoding="utf-8")
        result = run_engine(ENGINE, ["config", "encryption", "set"],
                            configuration=Configuration(self.configuration, (sys.executable, str(script))),
                            timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("type = local", self.configuration.read_text(encoding="utf-8"))

    def test_a_remote_destination_is_added_listed_validated_and_resolved(self):
        out = self.configured()
        self.assertIn(f"configuration  {self.configuration}", out)
        self.assertIn("unlock         none", out)
        self.assertIn("remotes        loc, enc, dav", out)
        code, out, err = self.add(f"loc:{self.folder}")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("NOTICE [destination-not-encrypted] destination 1: this remote does not encrypt: the service, "
                      "and anyone signed in to its account, can read every file and its name", out)
        doc = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema_version"], SCHEMA_VERSION)
        self.assertEqual(doc["jobs"][0]["destinations"][0]["endpoint"], {"remote": "loc", "folder": self.folder})
        self.assertEqual(doc["preferences"]["remotes"],
                         {"configuration": str(self.configuration), "unlock": "none", "unlock_command": []})
        self.assertIn(f"    destination 1 loc:{self.folder} (remote)", self.cli("jobs")[1])
        self.assertIn(f"destination 1: remote loc:{self.folder} - reachable; marker missing",
                      self.cli("resolve", "Work")[1])
        self.assertEqual(self.cli("validate", "Work")[0], EXIT_OK)

    def test_an_encrypting_remote_has_no_notice_and_a_drive_destination_beside_it_stays_as_it_is(self):
        self.configured()
        (self.vol_d / "Backup").mkdir()
        code, out, err = self.add(str(self.vol_d / "Backup"), "enc:Work")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertNotIn("destination-not-encrypted", out)
        doc = json.loads(self.store_path.read_text(encoding="utf-8"))
        first, second = (d["endpoint"] for d in doc["jobs"][0]["destinations"])
        self.assertEqual((sorted(first), second), (["path", "volume"], {"remote": "enc", "folder": "Work"}))

    def test_a_job_naming_a_remote_is_refused_with_no_configuration_or_an_unknown_remote(self):
        code, out, _ = self.add(f"loc:{self.folder}")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("REFUSED [remote-configuration-missing] destination 1: no rclone configuration is set", out)
        self.configured()
        code, out, _ = self.add("elsewhere:Work")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("REFUSED [remote-unknown] destination 1: the rclone configuration holds no remote named "
                      "'elsewhere'", out)
        self.assertFalse(self.store_path.exists() and json.loads(self.store_path.read_text(encoding="utf-8"))["jobs"])

    def test_a_file_where_the_folder_is_named_is_refused_and_resolved_as_one(self):
        self.configured()
        self.assertEqual(self.add(f"loc:{self.folder}")[0], EXIT_OK)
        (self.remote_dir / "Work").write_text("a file", encoding="utf-8")
        self.assertIn(f"destination 1: remote loc:{self.folder} - reachable; a file, not a folder, stands there",
                      self.cli("resolve", "Work")[1])
        code, out, _ = self.cli("validate", "Work")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("REFUSED [destination-not-a-folder] destination 1: this is a file", out)

    def test_a_remote_that_stops_answering_as_its_marker_is_read_is_resolved_as_the_remote(self):
        self.configured()
        self.assertEqual(self.add(f"loc:{self.folder}")[0], EXIT_OK)
        unanswered = StorageError("the sync engine did not answer: invented")
        with mock.patch.object(RemoteStorage, "read_file", autospec=True, side_effect=unanswered), \
                mock.patch.object(RemoteStorage, "entry", autospec=True, side_effect=unanswered):
            out = self.cli("resolve", "Work")[1]
        self.assertIn(f"destination 1: remote loc:{self.folder} - stopped answering: the sync engine did not answer: "
                      "invented\n", out)

    def test_a_remote_whose_server_does_not_answer_waits_and_says_why(self):
        """MEASURED: a WebDAV remote whose server does not run fails with the network's own
        reason, which ``unreachable`` reads as the service not reached."""
        self.configured()
        code, out, err = self.add("dav:Work")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("NOTICE [remote-unreachable] destination 1: dav:Work cannot be reached: ", out)
        line = next(x for x in self.cli("resolve", "Work")[1].splitlines() if x.startswith("destination 1"))
        self.assertTrue(line.startswith("destination 1: remote dav:Work - cannot be reached: "), line)
        self.assertTrue(unreachable(line), line)

    def test_a_locked_configuration_says_why_and_a_remembered_password_unlocks_it(self):
        """MEASURED: the engine runs this copy's ``unlock`` command on the windowless
        interpreter as the password command, from no particular folder, and it unlocks the
        configuration; a wrong password is never remembered."""
        self.encrypt_configuration()
        code, out, err = self.cli("remotes", "--configuration", str(self.configuration))
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("the configuration cannot be opened: the configuration", err)
        self.assertIn("password", err)
        code, _, err = self.cli("password", "set", password="not the password")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("the configuration could not be opened with this password, so it was not remembered", err)
        layout = runtime_layout(self.store_path)
        self.assertFalse(layout.remote_password.exists())
        code, out, err = self.cli("password", "set", password=CONFIG_PASS)
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn(f"Password of the rclone configuration {self.configuration} (not shown): ", self.asked)
        self.assertIn("Remembered for your Windows account only. Keep it somewhere else too", out)
        store = ConfigStore(self.store_path).load().contents.preferences.remotes
        self.assertEqual((store.unlock, store.unlock_command), (Unlock.REMEMBERED, ()))
        self.assertNotIn(CONFIG_PASS, self.store_path.read_text(encoding="utf-8"))
        self.assertIn("unlock         the remembered password", self.cli("remotes")[1])
        self.assertEqual(self.add(f"loc:{self.folder}")[0], EXIT_OK)
        self.assertIn("reachable; marker missing", self.cli("resolve", "Work")[1])
        code, out, _ = self.cli("password", "clear")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("the remembered password was removed; the configuration is now opened with no unlock", out)
        self.assertFalse(layout.remote_password.exists())
        self.assertIn("the configuration cannot be opened", self.cli("resolve", "Work")[1])

    def test_a_new_password_whose_seal_fails_its_read_back_leaves_the_remembered_one(self):
        """The engine runs the unlock command in a process of its own, out of reach of the
        stub, so the configuration opening afterwards is opened by the password kept."""
        self.configured()
        self.encrypt_configuration()
        layout = runtime_layout(self.store_path)
        self.assertEqual(self.cli("password", "set", password=CONFIG_PASS)[0], EXIT_OK)
        kept = layout.remote_password.read_bytes()
        unreadable = seal.SealError("Windows refused to open it: invented (13)")
        with mock.patch.object(seal, "unseal", side_effect=unreadable):
            code, _, err = self.cli("password", "set", password="another password")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("the password was not remembered: the sealed password could not be read back", err)
        self.assertEqual(layout.remote_password.read_bytes(), kept)
        store = ConfigStore(self.store_path).load().contents.preferences.remotes
        self.assertIs(store.unlock, Unlock.REMEMBERED)
        self.assertEqual(self.add(f"loc:{self.folder}")[0], EXIT_OK)
        self.assertIn("reachable; marker missing", self.cli("resolve", "Work")[1])

    def test_a_password_on_trial_never_reaches_an_activity_unlocking_meanwhile(self):
        """The new password is tried from a seal of its own: while the configuration is opened
        with it the remembered file holds the password remembered before, and a password that
        does not open the configuration never touches that file."""
        self.configured()
        self.encrypt_configuration()
        layout = runtime_layout(self.store_path)
        self.assertEqual(self.cli("password", "set", password=CONFIG_PASS)[0], EXIT_OK)
        kept = layout.remote_password.read_bytes()
        real, trials = reach.open_session, []

        def opening(preferences, store_path, **options):
            if options.get("sealed") is not None:
                trials.append(layout.remote_password.read_bytes())
            return real(preferences, store_path, **options)

        with mock.patch.object(reach, "open_session", side_effect=opening):
            code, _, err = self.cli("password", "set", password="not the password")
        self.assertEqual(code, EXIT_REFUSED, err)
        self.assertEqual(trials, [kept], "tried once, the remembered file as it was")
        self.assertEqual(layout.remote_password.read_bytes(), kept)
        self.assertEqual(list(layout.root.glob("*.trial")), [], "no password on trial is left behind")

    def test_the_unlock_command_prints_the_remembered_password_alone(self):
        layout = runtime_layout(self.store_path)
        layout.root.mkdir(parents=True)
        seal.remember(layout.remote_password, "pässword")
        result = subprocess.run(unlock_command(self.store_path), capture_output=True, cwd=self.tmp, timeout=60,
                                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual((result.returncode, result.stdout), (0, "pässword\n".encode("utf-8")), result.stderr)
        tried = seal.trial(layout.remote_password)
        seal.remember(tried, "on trial")
        result = subprocess.run(unlock_command(self.store_path, sealed=tried), capture_output=True, cwd=self.tmp,
                                timeout=60, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual((result.returncode, result.stdout), (0, b"on trial\n"), result.stderr)
        seal.forget(tried)
        seal.forget(layout.remote_password)
        result = subprocess.run(unlock_command(self.store_path), capture_output=True, cwd=self.tmp, timeout=60,
                                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual((result.returncode, result.stdout), (EXIT_REFUSED, b""))

    def test_a_configuration_that_is_not_encrypted_has_no_password_to_remember(self):
        self.configured()
        code, _, err = self.cli("password", "set", password="anything")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("opens without a password: it is not encrypted, so there is no password to remember", err)
        self.assertEqual(self.asked, [])
        self.assertFalse(runtime_layout(self.store_path).remote_password.exists())

    def test_choosing_another_unlock_or_another_configuration_forgets_the_remembered_password(self):
        self.configured()
        layout = runtime_layout(self.store_path)
        code, _, err = self.cli("remotes", "--unlock", "remembered")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("no password is remembered for this job file", err)
        self.assertIn("--unlock-command needs the command", self.cli("remotes", "--unlock-command")[2])
        self.encrypt_configuration()
        self.assertEqual(self.cli("password", "set", password=CONFIG_PASS)[0], EXIT_OK)
        script = str(self.tmp / "pass word" / "unlock.py")
        code, out, err = self.cli("remotes", "--unlock-command", sys.executable, script)
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("the remembered password was removed", out)
        self.assertIn("unlock         command: ", out)
        self.assertIn("remotes        loc, enc, dav", out)
        self.assertFalse(layout.remote_password.exists())
        settings = ConfigStore(self.store_path).load().contents.preferences.remotes
        self.assertEqual((settings.unlock, settings.unlock_command), (Unlock.COMMAND, (sys.executable, script)))
        self.assertEqual(self.cli("password", "set", password=CONFIG_PASS)[0], EXIT_OK)
        other = self.tmp / "other.conf"
        other.write_text("[loc]\ntype = local\n", encoding="utf-8")
        code, _, err = self.cli("remotes", "--configuration", str(other), "--unlock", "remembered")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("the remembered password is the previous configuration's", err)
        self.assertTrue(layout.remote_password.exists())
        code, out, err = self.cli("remotes", "--configuration", str(other))
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertIn("the remembered password was removed", out)
        self.assertIn("unlock         none", out)
        self.assertFalse(layout.remote_password.exists())

    def test_gather_reports_each_folder_reached_and_a_configuration_not_named(self):
        self.configured()
        preferences = ConfigStore(self.store_path).load().contents.preferences
        from mirrorhold.endpoints.endpoint import RemoteEndpoint
        here, there = RemoteEndpoint("loc", self.folder), RemoteEndpoint("dav", "x")
        setup = gather(preferences, self.store_path, [here, there])
        self.assertEqual((setup.state, setup.remotes), (SetupState.READY, ("loc", "enc", "dav")))
        self.assertEqual(setup.facts[here].reach, Reach.REACHED)
        self.assertFalse(setup.facts[here].features.encrypted)
        self.assertEqual(setup.facts[there].reach, Reach.UNREACHABLE)
        from dataclasses import replace
        missing = replace(preferences, remotes=replace(preferences.remotes, configuration=None))
        self.assertEqual(gather(missing, self.store_path, [here]).state, SetupState.MISSING)


if __name__ == "__main__":
    unittest.main()
