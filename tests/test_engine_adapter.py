"""The engine adapter: one pinned copy, located, version-checked, started windowless with
its configuration named — in memory, or the user's file never asked a password for — and
no inherited RCLONE_* variables; a transfer's log read whichever side its objects lie on."""
import base64
import hashlib
import http.client
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from support import TempDirTest

from mirrorhold.engine import rclone
from mirrorhold.engine.rclone import (PINNED_VERSION, Configuration, Engine, EngineError, RemoteControl, RemoteRoot,
                                      Transfer, engine_command, engine_environment, locate_engine, open_engine,
                                      parse_version, pinned_engine_path, space_separated, without_queries)
from mirrorhold.engine.remote import RemoteError, _configuration_reason, parse_time


class AdapterTest(TempDirTest):
    def test_command_keeps_the_configuration_in_memory(self):
        self.assertEqual(engine_command(Path("r.exe"), ["sync", "a", "b"])[:3], ["r.exe", "--config", ""])

    def test_a_start_that_reaches_a_remote_names_the_users_file_and_never_asks_a_password(self):
        configuration = Configuration(Path("C:/somewhere/rclone.conf"))
        self.assertEqual(engine_command(Path("r.exe"), ["lsf", "x:"], configuration),
                         ["r.exe", "--config", str(configuration.path), "--ask-password=false", "lsf", "x:"])
        unlocked = Configuration(Path("C:/somewhere/rclone.conf"), ("C:/My Tools/py.exe", "-m", "unlock"))
        command = engine_command(Path("r.exe"), ["lsf", "x:"], unlocked)
        self.assertEqual(command[4:6], ["--password-command", '"C:/My Tools/py.exe" -m unlock'])
        self.assertNotIn("RCLONE_CONFIG_PASS", " ".join(command))

    def test_a_password_command_is_spelled_as_the_engines_space_separated_list(self):
        self.assertEqual(space_separated(["a", "b c", 'say "x"', ""]), 'a "b c" "say ""x""" ""')

    def test_rclone_variables_are_not_inherited(self):
        env = engine_environment({"PATH": "x", "RCLONE_BWLIMIT": "1", "rclone_dry_run": "true", "OTHER": "y"})
        self.assertEqual(env, {"PATH": "x", "OTHER": "y"})

    def test_a_remote_control_is_given_only_a_hash_of_its_password(self):
        control = RemoteControl()
        try:
            arguments = " ".join(control.arguments)
            self.assertNotIn(control.password, arguments)
            held = control.password_file.read_text(encoding="ascii")
            digest = base64.b64encode(hashlib.sha1(control.password.encode()).digest()).decode()
            self.assertEqual(held, f"{control.user}:{{SHA}}{digest}\n")
            self.assertIn(str(control.password_file), arguments)
        finally:
            control.close()
        self.assertFalse(control.password_file.exists())

    def test_a_remote_folder_is_named_as_the_engine_names_it(self):
        root = RemoteRoot("cloud", "backups/photos")
        self.assertEqual(root.fs, "cloud:backups/photos")
        self.assertEqual(root.joined(".mirrorhold\\versions\\B1").fs, "cloud:backups/photos/.mirrorhold/versions/B1")
        self.assertEqual(RemoteRoot("cloud").joined("a\\b").fs, "cloud:a/b")
        self.assertEqual(RemoteRoot("server", "/").joined("a\\b").fs, "server:/a/b")
        self.assertEqual(RemoteRoot("server", "/srv/").joined("a").fs, "server:/srv/a")
        self.assertEqual(RemoteRoot("server", "/").joined("").fs, "server:/")
        self.assertEqual(root.joined("x\uff0fy").fs, "cloud:backups/photos/x\u201b\uff0fy")

    def test_a_web_addresss_query_never_survives_an_engine_message(self):
        self.assertEqual(without_queries("failed: GET https://host.example/a/b?sig=SECRET&x=1 returned 403"),
                         "failed: GET https://host.example/a/b returned 403")
        self.assertEqual(without_queries("no address here?"), "no address here?")

    def test_the_engines_times_are_read_to_the_nanosecond(self):
        self.assertEqual(parse_time("2020-09-13T17:56:40.1234567+05:30"), 1_600_000_000_123_456_700)
        self.assertEqual(parse_time("2020-09-13T12:26:40Z"), 1_600_000_000_000_000_000)
        with self.assertRaises(RemoteError):
            parse_time("yesterday")

    def test_a_configurations_failure_is_told_without_advice_that_cannot_work(self):
        said = ('panic received: fatal error: Failed to load config file "C:\\\\x\\\\rclone.conf": unable to decrypt '
                'configuration and not allowed to ask for password - set RCLONE_CONFIG_PASS to your configuration '
                'password')
        self.assertEqual(_configuration_reason(said),
                         "unable to decrypt configuration and not allowed to ask for password")

    def test_children_get_no_console_window(self):
        self.assertEqual(rclone.SUBPROCESS_FLAGS, getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with mock.patch.object(subprocess, "run") as run:
            rclone.run_engine(Path("r.exe"), ["version"])
        self.assertEqual(run.call_args.kwargs["creationflags"], rclone.SUBPROCESS_FLAGS)
        self.assertNotIn("RCLONE_X", run.call_args.kwargs["env"])

    def test_version_parsing(self):
        self.assertEqual(parse_version("rclone v1.75.1\n- os/version: x"), "v1.75.1")
        self.assertEqual(parse_version("rclone v1.76.0-beta.1234\n"), "v1.76.0-beta.1234")
        with self.assertRaises(EngineError):
            parse_version("something else")

    def test_locating(self):
        self.assertEqual(pinned_engine_path().parent.name, f"rclone-{PINNED_VERSION}-windows-amd64")
        with self.assertRaises(EngineError) as ctx:
            locate_engine(str(self.tmp / "missing.exe"))
        self.assertIn("missing.exe", str(ctx.exception))
        present = self.tmp / "rclone.exe"
        present.write_bytes(b"")
        self.assertEqual(locate_engine(str(present)), present)

    def test_another_version_is_refused(self):
        present = self.tmp / "rclone.exe"
        present.write_bytes(b"")
        fake = subprocess.CompletedProcess([], 0, stdout="rclone v1.70.0\n", stderr="")
        with mock.patch.object(rclone, "run_engine", return_value=fake), self.assertRaises(EngineError) as ctx:
            open_engine(str(present))
        self.assertIn("v1.70.0", str(ctx.exception))

    @unittest.skipUnless(pinned_engine_path().is_file(), "the pinned engine is not installed")
    def test_the_pinned_engine_opens(self):
        """Live path: the real binary reports the pinned version."""
        engine = open_engine()
        self.assertEqual((engine.path, engine.version), (pinned_engine_path(), PINNED_VERSION))


class _Process:
    """An engine that said nothing and has ended: what a transfer's start returns while no
    engine runs, so its report reads only what a test hands its log."""

    pid = 0

    def __init__(self):
        self.stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class TransferTest(TempDirTest):
    """A transfer's command and its log, with no engine started."""

    def started(self, destination, backup, configuration=None):
        with mock.patch.object(rclone, "start_engine", side_effect=lambda *a, **k: _Process()) as start, \
                mock.patch.object(rclone._Windows, "get", side_effect=EngineError("no job objects here")):
            transfer = Transfer(Engine(Path("r.exe"), PINNED_VERSION), self.tmp / "src", destination, ["a.txt"],
                                backup_dir=backup, list_path=self.tmp / "list.files", configuration=configuration,
                                sizes={"same.bin": 10})
        return transfer, start.call_args

    def test_a_drive_destination_is_copied_as_it_was(self):
        _transfer, call = self.started(self.tmp / "dst", self.tmp / "dst" / "B1")
        args = call.args[1]
        self.assertIn("--no-traverse", args)
        self.assertIsNone(call.kwargs["configuration"])

    def test_a_remote_destination_names_its_configuration_and_lists_the_folders_its_list_names(self):
        root = RemoteRoot("cloud", "backup")
        configuration = Configuration(self.tmp / "rclone.conf")
        _transfer, call = self.started(root, root.joined(".mirrorhold\\versions\\B1"), configuration)
        args = call.args[1]
        self.assertEqual(args[1:3], [str(self.tmp / "src"), "cloud:backup"])
        self.assertNotIn("--no-traverse", args)
        self.assertEqual(args[args.index("--backup-dir") + 1], "cloud:backup/.mirrorhold/versions/B1")
        self.assertIs(call.kwargs["configuration"], configuration)

    def test_the_remote_controls_password_is_in_no_argument(self):
        transfer, call = self.started(self.tmp / "dst", self.tmp / "dst" / "B1")
        self.assertNotIn(transfer._control.password, " ".join(call.args[1]))
        self.assertIn(str(transfer._control.password_file), call.args[1])
        transfer.close()
        self.assertFalse(transfer._control.password_file.exists())

    def test_an_answer_the_engine_breaks_off_reads_as_the_engine_not_answering(self):
        """A response cut short raises http.client's exceptions, which are no OSError: they are
        read as a call with no whole answer, as a remote session reads them."""
        transfer, _call = self.started(self.tmp / "dst", self.tmp / "dst" / "B1")
        self.addCleanup(transfer.close)
        transfer._process.poll = lambda: None              # the engine runs on
        for broken in (http.client.IncompleteRead(b"{\"bytes\""), http.client.BadStatusLine("")):
            with self.subTest(broken=type(broken).__name__), \
                    mock.patch.object(transfer._control, "call", side_effect=broken):
                self.assertIsNone(transfer.stats())
                self.assertFalse(transfer._call_when_up("core/quit", {}, patience=0.3))

    def test_a_remote_destination_and_its_batch_lie_on_one_side(self):
        with self.assertRaises(ValueError):
            self.started(RemoteRoot("cloud", "backup"), self.tmp / "B1", Configuration(self.tmp / "c.conf"))
        with self.assertRaises(ValueError):
            self.started(RemoteRoot("cloud", "backup"), RemoteRoot("cloud", "backup/B1"))

    def test_the_log_is_read_whichever_side_its_objects_lie_on(self):
        """The events measured against loopback remotes: a copy names its source object, a
        move into the batch its destination object, by a move or by a copy and a delete."""
        transfer, _call = self.started(RemoteRoot("cloud", "backup"), RemoteRoot("cloud", "backup/B1"),
                                       Configuration(self.tmp / "c.conf"))
        for entry in (
                {"level": "notice", "msg": "No host key validation is being performed.", "object": "sftp",
                 "objectType": "string"},
                {"level": "info", "msg": "Copied (server-side copy)", "object": "f00.txt", "objectType": "*s3.Object"},
                {"level": "info", "msg": "Deleted", "object": "f00.txt", "objectType": "*s3.Object"},
                {"level": "info", "msg": "Moved (server-side)", "object": "d/f01.txt", "objectType": "*webdav.Object"},
                {"level": "info", "msg": "Copied (new)", "object": "f00.txt", "objectType": "*local.Object",
                 "size": 18},
                {"level": "info", "msg": "Updated modification time in destination", "object": "same.bin",
                 "objectType": "*s3.Object"},
                {"level": "error", "msg": 'Put "https://host.example/f?token=SECRET": 403 Forbidden',
                 "object": "f02.txt", "objectType": "*local.Object"}):
            transfer._consume(json.dumps(entry))
        report = transfer.report()
        self.assertEqual(report.copied, {"f00.txt": 18})
        self.assertEqual(report.moved_to_backup, ("f00.txt", "d\\f01.txt"))
        self.assertEqual(report.identical, {"same.bin": 10})
        self.assertEqual(report.failed, {"f02.txt": 'Put "https://host.example/f": 403 Forbidden'})
        self.assertEqual(report.errors, ())


if __name__ == "__main__":
    unittest.main()
