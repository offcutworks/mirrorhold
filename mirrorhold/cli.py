"""The command line: define, check and locate jobs; preview, run and control runs.

    python -m mirrorhold [--store FILE] <command> ...

    volumes                         the volumes present now, and those that cannot be read
    jobs                            the stored jobs, each with its group
    add NAME --source DIR (--dest DIR | --remote-dest REMOTE:FOLDER) [...] [--no-survey]
                                    destinations in the order given; REMOTE names a remote of
                                    the rclone configuration ``remotes`` sets
    remove JOB
    validate [JOB] [--no-survey]
    resolve JOB                     where each endpoint is now, and each destination's marker
                                    and its identifier
    export FILE [JOB ...]
    import FILE [--replace]
    engine                          the sync engine in use and its version
    remotes [--configuration FILE|none] [--unlock none|remembered|command]
            [--unlock-command PROGRAM ARG ...]
                                    the rclone configuration remote destinations are reached
                                    through, its unlock, and the remotes it holds
    password set / password clear   remember the configuration's password, sealed for this
                                    Windows account (asked for, never shown), or forget it
    unlock [--seal FILE]            print the remembered password: what the engine runs as
                                    the configuration's password command (--seal: another
                                    sealed file, a password on trial before it is remembered)
    preview JOB [--dest N ... | --marker ID ...] [--move]
                                    what a run would do now, its estimate, and a ticket to confirm it
    run JOB [--dest N ... | --marker ID ...] [--confirm TICKET | --first-backup] [--unattended]
    verify JOB [--dest N | --marker ID] [--new-copies] [--restart]
                                    a full verification of one destination: both copies of every
                                    file read in full and compared; continues a stopped one.
                                    --new-copies reads only the copies not verified as they are
                                    now (every file where no list of verified copies is kept)
    repair JOB [--dest N | --marker ID]
                                    replace the backup copies the destination's latest finished
                                    verification found to differ: each moves into versions, the
                                    source's copy is copied in its place, and both are read back
    status                          the runs, verifications and repairs in progress, in line order
    history JOB [--limit N]         the job's run, verification and repair records, newest last
    pause [RUN] / resume [RUN]      pause or resume a run, verification or repair in progress
    cancel [RUN] [--now]            cancel a run or a repair: after its files in flight, or at
                                    once; a verification stops at once either way
    when JOB [--arrival on|off] [--schedule SPEC|none] [--automatic on|off]
                                    when the job runs by itself (SPEC: days, then times:
                                    "daily 21:00", "weekly mon,thu 09:00,21:00",
                                    "monthly 15 22:00", "monthly last every 2h 08:00-23:00";
                                    --automatic off pauses both)
    install / uninstall             set up, or remove, the copy its tasks and Start menu entry
                                    start: from a working copy, a copy built in installed/;
                                    in a portable release, the release where it lies
    uninstall --if-set-up-here      in a portable release, remove the setup only if this
                                    release is the one set up, and otherwise change nothing:
                                    what the setup program's uninstaller asks
    installation                    which copy is set up for this store
    reconcile                       bring the scheduled tasks into line with the jobs
    automatic JOB --by arrival|schedule
                                    what a scheduled task runs (unattended)

JOB is a job's identifier, a unique prefix of it, or its exact name; RUN a unique prefix
of a run's identifier, needed only when more than one run is in progress. A destination is
named by its number in the job's order (``--dest N``) or by its marker's identifier
(``--marker ID``: the identifier ``resolve`` prints, or a unique prefix of it of four
characters or more among the job's destinations), which names it wherever the job's order
puts it — the way the window names every destination it starts something on. ``run`` waits
while a run, verification or repair it conflicts with is in progress (``records.admission``) —
one that uses one of its drives too, while the job file's preferences take drives one
at a time, as they do unless changed in the window — and works beside any other; ``status``
says what a waiting one waits for, the drive they share and its place in line. A pair's
first run onto a folder that holds nothing yet (or
does not exist) is confirmed by ``run --first-backup``, which needs no preview and
applies only while the folder holds nothing when the run lists it; a first run onto a
folder that holds something, and removals the safety gate holds, are confirmed by ``run
--confirm`` with the ticket the latest ``preview`` printed. What such a folder already
holds and the source does not is left where it is, by that run and every later one,
unless ``preview --move`` planned moving it into versions instead (not offered where it
holds a synchronised folder, another job's source or a folder Windows or this tool
needs). A shared destination whose record of what to leave was lost is confirmed the same
way, leaving what is there its only answer, and its run makes the record again. An
``--unattended`` run prints nothing and confirms nothing. A new job runs only
when it is started; ``when`` switches its automatic runs on, which take effect once a copy
is installed (a portable release: set up).
Every command that changes the jobs brings the scheduled tasks into line afterwards.

Exit status: 0 done; 1 refused (validation, a conflict, an unknown job or run, an
install refused, a verification or a repair that cannot start, a request a run's control
file did not take) or failed unexpectedly; 2 usage; 3 the store cannot be used (unreadable, or
held by another program); 4 the engine cannot be used; for ``run``: 5 a pair is held or
waits for confirmation; 6 a pair stopped, failed or was interrupted; 7 the run was
cancelled; for ``verify``: 6 files differ, or the verification failed; 7 it was
stopped; and for ``repair``: 5 a copy was left as it is; 6 a copy could not be replaced or
still differs, or the repair failed; 7 it was stopped.

A command saves the job file only over what it read: a window or another command that
saved in between refuses the save, nothing written. ``run``, ``verify``, ``repair`` and
``automatic`` keep a diagnostic log beside the store (``records.diagnostics``) of
everything they say, an unattended run included, with the marks the window reads back:
the command and job it was given, the job it resolved, and how it ended — its exit
status, and why it could not do its work when it could not. A command that fails in a
way the code does not foresee says so in one line naming the log that holds its
traceback.
"""
from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from .automation.automatic import run_on_arrival, run_scheduled
from .automation.reconcile import AutomationState, apply_automation
from .automation.scheduler import Scheduler
from .automation.tasks import ARRIVAL, SCHEDULE
from .contract_errors import ContractError
from .endpoints.cloud import SyncRoot, sync_roots_on_this_computer
from .endpoints.endpoint import RemoteEndpoint
from .endpoints.marker import MarkerState, check_marker
from .endpoints.resolution import EndpointError, Presence, endpoint_from_path, resolve
from .endpoints.volumes import VolumeSource, default_volume_source
from .engine.rclone import EngineError, RemoteRoot, open_engine
from .exchange import FileBusy
from .installation.installer import InstallationState, InstallError, install, installation_state, uninstall
from .installation.release import release_name
from .jobs.model import Job, RemoteSettings, Triggers, Unlock, new_job
from .jobs.schedule import parse_schedule
from .jobs.store import ConfigStore, LoadStatus, StoreConflict, StoreContents, StoreError, merge_imported
from .jobs.survey import describe as describe_survey, survey_source
from .jobs.validation import ValidationContext, validate_job
from .locations import as_reached, default_store_path, runtime_layout
from .maintenance.repair import RepairState, read_repairs
from .maintenance.repairer import RepairError, repair_destination
from .maintenance.verification import VerificationMode, VerificationState, read_verifications
from .maintenance.verifier import VerifyError, verify_destination
from .notifications import announce_repair, announce_run, announce_verification
from .notifications.toast import Notifier, default_notifier
from .planning.plan import PairPlan
from .planning.sharing import Sharing
from .records import diagnostics
from .records.record import FIRST_RUN_REASON, Outcome, read_records
from .records.admission import WaitedFor, line_words, starting
from .records.status import (ANY_KIND, KIND_REPAIR, KIND_VERIFICATION, UNREADABLE, CancelMode, NoSuchRun, find_run,
                             is_repair, is_verification, line_order, observe_runs, request)
from .reminders import reminding
from .remotes import seal
from .remotes.facts import Reach
from .remotes.reach import (PasswordRefused, RemoteSetupError, gather, has_a_password, look, open_session,
                            remember_password)
from .storage.destination import StorageError
from .storage.remote import RemoteStorage
from .run.confirmation import Content
from .run.gate import Verdict
from .run.put_back import PutBack
from .run.runner import JobPreview, PairAssessment, RunError, RunOptions, preview_job, run_job
from .wording import agreeing, counted

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_STORE, EXIT_ENGINE = 0, 1, 2, 3, 4
EXIT_HELD, EXIT_FAILED, EXIT_CANCELLED = 5, 6, 7
SAMPLES = 5

LOGGED_COMMANDS = ("run", "verify", "repair", "automatic")
"""The commands that are activities, often with no console: each keeps a diagnostic log."""

SAVE_CONFLICT = ("the job file changed while this command ran (another window or command saved it); nothing was "
                 "saved - run the command again")
REQUEST_NOT_WRITTEN = ("the request could not be written: another program kept the run's control file open; "
                       "try again")


class _Usage(Exception):
    pass


def _automatic(job: Job) -> bool:
    return job.enabled and job.triggers.automatic


def _when(job: Job) -> str:
    """When the job runs, in words."""
    parts = ["when you start it"]
    if job.triggers.on_arrival:
        parts.append("when one of its drives is attached (also at start-up)")
    if job.triggers.schedule is not None:
        parts.append(parse_schedule(job.triggers.schedule).describe().lower())
    text = "; also ".join(parts) if len(parts) > 1 else parts[0]
    if job.triggers.automatic and not job.enabled:
        text = f"when you start it (automatic runs paused: {'; '.join(parts[1:])})"
    return text


def _destination_options(command: argparse.ArgumentParser, *, many: bool) -> None:
    """``--dest N`` or ``--marker ID``, one of the two: each once, or each as often as there
    are destinations to name when ``many``."""
    choice = command.add_mutually_exclusive_group()
    action = "append" if many else "store"
    choice.add_argument("--dest", type=int, action=action, metavar="N")
    choice.add_argument("--marker", action=action, metavar="ID")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirrorhold", description="MirrorHold: one-way backup jobs.")
    parser.add_argument("--store", type=Path, default=None, help="the configuration store to use")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("volumes")
    sub.add_parser("jobs")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--source", required=True)
    add.add_argument("--dest", dest="destinations", action="append", type=lambda text: (False, text))
    add.add_argument("--remote-dest", dest="destinations", action="append", type=lambda text: (True, text),
                     metavar="REMOTE:FOLDER")
    add.add_argument("--no-survey", action="store_true")
    remove = sub.add_parser("remove")
    remove.add_argument("job")
    validate = sub.add_parser("validate")
    validate.add_argument("job", nargs="?")
    validate.add_argument("--no-survey", action="store_true")
    res = sub.add_parser("resolve")
    res.add_argument("job")
    export = sub.add_parser("export")
    export.add_argument("file", type=Path)
    export.add_argument("jobs", nargs="*")
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    imp.add_argument("--replace", action="store_true")
    sub.add_parser("engine")
    remotes = sub.add_parser("remotes")
    remotes.add_argument("--configuration", metavar="FILE")
    remotes.add_argument("--unlock", choices=tuple(u.value for u in Unlock))
    remotes.add_argument("--unlock-command", nargs=argparse.REMAINDER, metavar="PROGRAM ARG")
    password = sub.add_parser("password")
    password.add_argument("action", choices=("set", "clear"))
    unlock = sub.add_parser("unlock")
    unlock.add_argument("--seal", type=Path, metavar="FILE")
    preview = sub.add_parser("preview")
    preview.add_argument("job")
    _destination_options(preview, many=True)
    preview.add_argument("--move", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("job")
    _destination_options(run, many=True)
    run.add_argument("--confirm", metavar="TICKET")
    run.add_argument("--first-backup", action="store_true")
    run.add_argument("--unattended", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("job")
    _destination_options(verify, many=False)
    verify.add_argument("--new-copies", action="store_true")
    verify.add_argument("--restart", action="store_true")
    repair = sub.add_parser("repair")
    repair.add_argument("job")
    _destination_options(repair, many=False)
    sub.add_parser("status")
    history = sub.add_parser("history")
    history.add_argument("job")
    history.add_argument("--limit", type=int, default=20)
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("run", nargs="?")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("run", nargs="?")
    cancel.add_argument("--now", action="store_true")
    when = sub.add_parser("when")
    when.add_argument("job")
    when.add_argument("--arrival", choices=("on", "off"))
    when.add_argument("--schedule", metavar="SPEC")
    when.add_argument("--automatic", choices=("on", "off"))
    for name in ("install", "installation", "reconcile"):
        sub.add_parser(name)
    sub.add_parser("uninstall").add_argument("--if-set-up-here", action="store_true")
    automatic = sub.add_parser("automatic")
    automatic.add_argument("job")
    automatic.add_argument("--by", required=True, choices=(ARRIVAL, SCHEDULE))
    return parser


class _Session:
    def __init__(self, args, volume_source: Optional[VolumeSource], sync_roots: Optional[Sequence[SyncRoot]], out, err,
                 *, scheduler: Optional[Scheduler] = None, notifier: Optional[Notifier] = None,
                 install_root: Optional[Path] = None, shortcut: Optional[Path] = None,
                 account_store: Optional[Path] = None, ask_password: Optional[Callable[[str], str]] = None):
        self.args = args
        self.ask_password = ask_password or getpass.getpass
        self.out, self.err = out, err
        self.store = ConfigStore(args.store or default_store_path())
        self._volume_source = volume_source
        self._sync_roots = sync_roots
        self._scan = None
        self.scheduler = scheduler
        self.notifier = notifier
        self.install_root = install_root
        self.shortcut = shortcut
        self.account_store = account_store
        self.read_as = None              # what the job file held when this command read it
        self.failure = ""                # why the command could not do its work: its log's end keeps it

    def say(self, text: str = "") -> None:
        print(text, file=self.out)
        diagnostics.note(text)

    def warn(self, text: str) -> None:
        print(text, file=self.err)
        diagnostics.note(text)

    def refused(self, prefix: str, text: str) -> None:
        """Say why the command could not do its work; the first reason given is the one kept."""
        self.failure = self.failure or text
        self.warn(prefix + text)

    def load(self) -> StoreContents:
        result = self.store.load()
        self.read_as = result.fingerprint
        if result.status is LoadStatus.UNSUPPORTED:
            raise StoreError(result.detail)
        if result.status is LoadStatus.CORRUPT:
            self.warn(f"warning: {result.detail}")
            if result.previous_copies:
                self.warn("previous copies that can be restored: " + ", ".join(str(p) for p in result.previous_copies))
        elif result.status is LoadStatus.MIGRATED:
            self.warn(f"note: {result.detail}")
        return result.contents

    def volume_source(self) -> VolumeSource:
        return self._volume_source or default_volume_source()

    def scan(self):
        if self._scan is None:
            self._scan = self.volume_source().scan()
        return self._scan

    def sync_roots(self):
        """The synchronised folders, read once per command so every check in it sees the same."""
        if self._sync_roots is None:
            self._sync_roots = sync_roots_on_this_computer()
        return tuple(self._sync_roots)

    def find(self, contents: StoreContents, key: str) -> Job:
        by_name = [j for j in contents.jobs if j.name.casefold() == key.casefold()]
        if len(by_name) == 1:
            diagnostics.mark(diagnostics.JOB, job=by_name[0].job_id)
            return by_name[0]
        by_id = [j for j in contents.jobs if j.job_id.startswith(key.lower())] if len(key) >= 4 else []
        if len(by_id) == 1:
            diagnostics.mark(diagnostics.JOB, job=by_id[0].job_id)
            return by_id[0]
        if by_id or len(by_name) > 1:
            raise _Usage(f"'{key}' matches more than one job")
        raise _Usage(f"no job '{key}'")

    def markers(self, job: Job, keys: Optional[Sequence[str]]) -> Optional[Tuple[str, ...]]:
        """The markers of the job's destinations ``keys`` name — each a marker's identifier
        or a unique prefix of it of four characters or more — or None when none is given.
        The log's job mark gives the numbers they hold in the job as read here."""
        if keys is None:
            return None
        found = []
        for key in keys:
            if len(key) < 4:
                raise _Usage(f"'{key}' is too short: a marker is named by its identifier, or by its first four "
                             "characters or more")
            matches = [d.marker_id for d in job.destinations if d.marker_id.startswith(key.lower())]
            if len(matches) > 1:
                raise _Usage(f"'{key}' matches more than one destination's marker")
            if not matches:
                raise _Usage(f"the job has no destination with marker '{key}'")
            found.append(matches[0])
        diagnostics.mark(diagnostics.JOB, job=job.job_id, destinations=sorted({job.number_of(m) for m in found}))
        return tuple(found)

    def report(self, job: Job, survey, contents: StoreContents):
        remote = [d.remote for d in job.destinations if d.remote is not None]
        setup = gather(contents.preferences, self.store.path, remote) if remote else None
        context = ValidationContext.on_this_computer(self.scan().volumes, self.sync_roots(),
                                                     tuple(j for j in contents.jobs if j.job_id != job.job_id),
                                                     self.store.path, remote_setup=setup)
        report = validate_job(job, context, survey)
        self.say(f"job '{job.name}' ({job.job_id}): " + ("REFUSED" if report.refused else "valid"))
        for finding in report.findings:
            self.say("  " + finding.describe())
        return report

    def survey_for(self, job: Job):
        if getattr(self.args, "no_survey", False):
            return None
        resolution = resolve(job.source.endpoint, self.scan().volumes)
        if resolution.presence is not Presence.PRESENT or resolution.location.is_file():
            return None                     # a source that is a file is refused, not surveyed as a folder
        survey = survey_source(resolution.location, drive_root=job.source.endpoint.is_drive_root)
        self.say(f"source survey: {describe_survey(survey)}")
        return survey

    def save(self, contents: StoreContents) -> None:
        """Save the jobs over what this command read (StoreConflict when the file changed
        since), then bring the scheduled tasks into line with them."""
        self.read_as = self.store.save(contents, expected=self.read_as)
        outcome = apply_automation(self.store.path, contents.jobs, root=self.install_root, scheduler=self.scheduler)
        if not outcome.ok:
            self.warn(outcome.describe())
        elif outcome.state is not AutomationState.NOT_INSTALLED or any(_automatic(j) for j in contents.jobs):
            self.say(outcome.describe())


def _cmd_volumes(s: _Session) -> int:
    scan = s.scan()
    for v in scan.volumes:
        flags = [v.backing.value]
        if v.pool_member:
            flags.append("DrivePool member")
        s.say(f"{v.describe()}")
        s.say(f"    root {v.root}  free {v.free_bytes} of {counted(v.capacity_bytes, 'byte')}  [{', '.join(flags)}]")
    for device, reason in scan.unavailable:
        s.say(f"unavailable: {device} ({reason})")
    return EXIT_OK


def _cmd_jobs(s: _Session) -> int:
    contents = s.load()
    if not contents.jobs:
        s.say("no jobs")
    for job in contents.in_order():
        s.say(f"{job.job_id}  {job.name}")
        group = contents.group_of(job.job_id)
        if group is not None:
            s.say(f"    group        {group.name}")
        s.say(f"    runs         {_when(job)}")
        s.say(f"    source       {job.source.endpoint.describe()}")
        for i, d in enumerate(job.destinations, 1):
            s.say(f"    destination {i} {_destination_words(d.endpoint)}")
    return EXIT_OK


def _destination_words(endpoint) -> str:
    """A destination as ``jobs`` lists it: a remote's folder as rclone names it, marked as
    a remote's; a drive's folder by its volume and path."""
    return f"{endpoint.describe()} (remote)" if isinstance(endpoint, RemoteEndpoint) else endpoint.describe()


def _cmd_add(s: _Session) -> int:
    if not s.args.destinations:
        raise _Usage("a job needs at least one destination: --dest DIR or --remote-dest REMOTE:FOLDER")
    contents = s.load()
    volumes = s.scan().volumes
    try:
        source = endpoint_from_path(s.args.source, volumes)
        destinations = [_remote_endpoint(text) if remote else endpoint_from_path(text, volumes)
                        for remote, text in s.args.destinations]
    except EndpointError as exc:
        s.warn(f"refused: {exc}")
        return EXIT_REFUSED
    job = new_job(s.args.name, source, destinations)
    report = s.report(job, s.survey_for(job), contents)
    if report.refused:
        return EXIT_REFUSED
    s.save(contents.with_job(job))
    s.say(f"saved job {job.job_id}; it runs {_when(job)}")
    return EXIT_OK


def _remote_endpoint(text: str) -> RemoteEndpoint:
    try:
        return RemoteEndpoint.parse(text)
    except ContractError as exc:
        raise EndpointError(f"{text}: {exc}") from None


def _cmd_remove(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    s.save(contents.without_job(job.job_id))
    s.say(f"removed job '{job.name}' ({job.job_id})")
    return EXIT_OK


def _cmd_validate(s: _Session) -> int:
    contents = s.load()
    jobs = [s.find(contents, s.args.job)] if s.args.job else list(contents.jobs)
    if not jobs:
        s.say("no jobs")
    refused = False
    for job in jobs:
        refused |= s.report(job, s.survey_for(job), contents).refused
    return EXIT_REFUSED if refused else EXIT_OK


def _cmd_resolve(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    volumes = s.scan().volumes
    source = resolve(job.source.endpoint, volumes)
    s.say(f"source: {source.describe()}")
    remote = [i for i, d in enumerate(job.destinations, 1) if d.remote is not None]
    reached = _remote_lines(s, contents, job, remote) if remote else {}
    for i, d in enumerate(job.destinations, 1):
        if d.remote is not None:
            s.say(f"destination {i}: remote {d.remote.describe()} - {reached[i]}")
        else:
            resolution = resolve(d.endpoint, volumes)
            line = f"destination {i}: {resolution.describe()}"
            if resolution.presence is Presence.PRESENT:
                line += f"; marker {check_marker(resolution.location, job.job_id, d.marker_id).value}"
            s.say(line)
        s.say(f"    marker identifier {d.marker_id}")
    return EXIT_OK


def _remote_lines(s: _Session, contents: StoreContents, job: Job, indexes: Sequence[int]) -> dict:
    """What ``resolve`` says of each remote destination: reachable and its marker's state, or
    why it is not — every one through one session on the configuration."""
    try:
        session = open_session(contents.preferences, s.store.path)
    except RemoteSetupError as exc:
        return dict.fromkeys(indexes, f"the configuration cannot be opened: {exc.reason}")
    lines = {}
    with session:
        for i in indexes:
            destination = job.destinations[i - 1]
            remote = destination.remote
            if remote.remote not in session.remotes:
                lines[i] = f"the rclone configuration holds no remote named '{remote.remote}'"
                continue
            facts = look(session, remote)
            if facts.reach is Reach.UNREACHABLE:
                lines[i] = f"cannot be reached: {facts.reason}"
                continue
            if facts.reach is Reach.REFUSED:
                lines[i] = f"the service refused: {facts.reason}"
                continue
            if facts.is_file:
                lines[i] = "reachable; a file, not a folder, stands there"
                continue
            storage = RemoteStorage(session, RemoteRoot(remote.remote, remote.folder), facts.features)
            try:
                lines[i] = f"reachable; marker {check_marker(storage, job.job_id, destination.marker_id).value}"
            except StorageError as exc:
                lines[i] = f"stopped answering: {exc.strerror}"
    return lines


def _cmd_export(s: _Session) -> int:
    contents = s.load()
    jobs = [s.find(contents, k) for k in s.args.jobs] if s.args.jobs else list(contents.jobs)
    s.store.export_jobs(jobs, s.args.file, groups=contents.groups)
    s.say(f"exported {counted(len(jobs), 'job')} to {s.args.file}")
    return EXIT_OK


def _cmd_import(s: _Session) -> int:
    contents = s.load()
    listed = s.store.read_export(s.args.file)
    merged, outcome = merge_imported(contents, listed.jobs, groups=listed.groups, replace_existing=s.args.replace)
    for job_id in outcome.conflicts:
        s.say(f"conflict: job {job_id} exists with different settings (use --replace to take the imported one)")
    refused = bool(outcome.conflicts)
    for job_id in outcome.added + outcome.replaced:
        job = merged.job(job_id)
        refused |= s.report(job, None, merged).refused
    if refused:
        s.say("nothing imported")
        return EXIT_REFUSED
    s.save(merged)
    s.say(f"imported: {len(outcome.added)} added, {len(outcome.replaced)} replaced, "
          f"{len(outcome.unchanged)} already present")
    return EXIT_OK


def _cmd_engine(s: _Session) -> int:
    contents = s.load()
    try:
        engine = open_engine(contents.preferences.engine_path)
    except EngineError as exc:
        s.warn(f"engine: {exc}")
        return EXIT_ENGINE
    s.say(f"engine {engine.version} at {engine.path}")
    return EXIT_OK


def _unlock_words(settings: RemoteSettings) -> str:
    if settings.unlock is Unlock.REMEMBERED:
        return "the remembered password"
    if settings.unlock is Unlock.COMMAND:
        return "command: " + subprocess.list2cmdline(settings.unlock_command)
    return "none"


def _with_remotes(contents: StoreContents, settings: RemoteSettings) -> StoreContents:
    return replace(contents, preferences=replace(contents.preferences, remotes=settings))


def _cmd_remotes(s: _Session) -> int:
    contents = s.load()
    layout = runtime_layout(s.store.path)
    before = settings = contents.preferences.remotes
    args = s.args
    try:
        if args.unlock_command == []:
            raise _Usage("--unlock-command needs the command: --unlock-command PROGRAM ARG ..., last on the line")
        unlock = Unlock(args.unlock) if args.unlock else (Unlock.COMMAND if args.unlock_command else None)
        if args.unlock_command is not None and unlock is not Unlock.COMMAND:
            raise _Usage("--unlock-command goes with --unlock command")
        if unlock is Unlock.COMMAND and not args.unlock_command:
            raise _Usage("--unlock command needs the command: --unlock-command PROGRAM ARG ..., last on the line")
        if args.configuration is not None:
            named = None if args.configuration.strip().casefold() == "none" else str(as_reached(args.configuration))
            if named is not None and not Path(named).is_file():
                raise _Usage(f"{named}: no such file; make the configuration with rclone first")
            if named != settings.configuration and settings.unlock is Unlock.REMEMBERED:
                if unlock is Unlock.REMEMBERED:
                    raise _Usage("the remembered password is the previous configuration's: choose this one, then "
                                 "remember its password with 'mirrorhold password set'")
                settings = RemoteSettings(settings.configuration)   # the remembered password opens the old file
            settings = replace(settings, configuration=named)
        if unlock is Unlock.REMEMBERED and not seal.present(layout.remote_password):
            raise _Usage("no password is remembered for this job file: 'mirrorhold password set' remembers it and "
                         "chooses it as the unlock")
        if unlock is not None:
            settings = RemoteSettings(settings.configuration, unlock,
                                      tuple(args.unlock_command) if unlock is Unlock.COMMAND else ())
    except ContractError as exc:
        raise _Usage(str(exc)) from None
    if settings != before:
        s.save(_with_remotes(contents, settings))
        if before.unlock is Unlock.REMEMBERED and settings.unlock is not Unlock.REMEMBERED:
            if not seal.forget(layout.remote_password):
                s.refused("", f"{layout.remote_password} could not be removed: another program kept it open; "
                              "remove it with 'mirrorhold password clear'")
                return EXIT_REFUSED
            s.say("the remembered password was removed")
    s.say(f"configuration  {settings.configuration or 'none set'}")
    s.say(f"unlock         {_unlock_words(settings)}")
    if settings.configuration is None:
        return EXIT_OK
    try:
        session = open_session(replace(contents.preferences, remotes=settings), s.store.path)
    except RemoteSetupError as exc:
        s.refused("", f"the configuration cannot be opened: {exc.reason}")
        return EXIT_REFUSED
    with session:
        s.say(f"remotes        {', '.join(session.remotes) or 'none'}")
    return EXIT_OK


def _cmd_password(s: _Session) -> int:
    contents = s.load()
    settings = contents.preferences.remotes
    path = runtime_layout(s.store.path).remote_password
    if s.args.action == "clear":
        had = path.is_file()
        if settings.unlock is Unlock.REMEMBERED:      # the job file first: it never names a password that is gone
            s.save(_with_remotes(contents, RemoteSettings(settings.configuration)))
        if not seal.forget(path):
            raise _Usage(f"{path} could not be removed: another program kept it open; try again")
        if settings.unlock is Unlock.REMEMBERED:
            s.say("the remembered password was removed; the configuration is now opened with no unlock")
        else:
            s.say("the remembered password was removed" if had else "no password was remembered")
        return EXIT_OK
    try:
        has_a_password(contents.preferences, s.store.path)
    except RemoteSetupError as exc:
        raise _Usage(exc.reason) from None
    except PasswordRefused as exc:
        raise _Usage(str(exc)) from None
    password = s.ask_password(f"Password of the rclone configuration {settings.configuration} (not shown): ")
    try:
        remembered = remember_password(contents.preferences, s.store.path, password)
    except PasswordRefused as exc:
        raise _Usage(str(exc)) from None
    if settings != remembered:
        s.save(_with_remotes(contents, remembered))
    s.say(seal.REMINDER)
    return EXIT_OK


def _cmd_unlock(s: _Session) -> int:
    """The remembered password, and nothing else, on standard output as UTF-8: what the
    engine reads from the configuration's password command. ``--seal``: the password on
    trial in that file instead (``remotes.reach.remember_password``)."""
    try:
        data = seal.remembered(s.args.seal or runtime_layout(s.store.path).remote_password)
    except seal.SealError as exc:
        print(f"unlock: {exc}", file=s.err)
        return EXIT_REFUSED
    stream = getattr(s.out, "buffer", None)
    if stream is None:
        s.out.write(data.decode("utf-8") + "\n")
        s.out.flush()
    else:
        stream.write(data + b"\n")
        stream.flush()
    return EXIT_OK


def _samples(s: _Session, title: str, paths) -> None:
    paths = list(paths)
    if paths:
        shown = ", ".join(paths[:SAMPLES]) + (f" and {len(paths) - SAMPLES} more" if len(paths) > SAMPLES else "")
        s.say(f"    {title}: {shown}")


def _names(items: Sequence[str]) -> str:
    return ", ".join(items[:SAMPLES]) + (f" and {len(items) - SAMPLES} more" if len(items) > SAMPLES else "")


def _say_content(s: _Session, pair: PairAssessment) -> None:
    """What a previewed first run that offers a choice does with what its destination holds
    and the source does not."""
    head = "    what is already there and not in the source:"
    if pair.content is Content.MOVE:
        s.say(f"{head} moved into versions")
        return
    if pair.withheld:
        s.say(f"{head} left where it is - moving it is not offered here: {pair.withheld}")
    else:
        s.say(f"{head} left where it is (preview with --move to move it into versions)")
    _say_left(s, pair.sharing)


def _say_left(s: _Session, sharing: Sharing) -> None:
    """What a first run leaves where it is: what the source does not hold, and what lies
    beneath a source's name the folder already held."""
    s.say(f"    left as they are: {counted(sharing.left_files, 'file')} - {_names(sharing.left_items)}")
    for name, files in sharing.meeting:
        s.say(f"    in the source and already here: {name} - its {counted(files, 'file')} the source does not have "
              f"{agreeing(files, 'is', 'are')} left as {agreeing(files, 'it is', 'they are')}")


def _say_put_back(s: _Session, back: PutBack, plan: PairPlan) -> None:
    """The removals that reappeared after being moved into versions, apart from the others:
    a folder they fill as one name, then the other files."""
    within = plan.moved_with_folders()
    whole = plan.whole_folders(back.keys, within)
    names = [f"{folder}\\ (whole folder, {n})"
             for folder, n in sorted(whole.items(), key=lambda item: (-item[1], item[0].casefold()))]
    names += sorted((e.path for e in plan.deleted if e.key in back.keys and within.get(e.key) not in whole),
                    key=str.casefold)
    s.say(f"    reappeared after being moved into versions at {back.moved.strftime('%Y-%m-%dT%H:%M:%SZ')}: "
          f"{len(back.keys)} - {_names(names)}")
    if back.keeper:
        s.say(f"    {back.keeper} keeps a copy of this folder and may have put them back")


def _say_preview(s: _Session, preview: JobPreview) -> bool:
    """Print a preview; True when some pair needs the user's confirmation."""
    if preview.source_problem:
        s.say(f"source: {preview.source_problem}")
    confirm = False
    for item in preview.pairs:
        pair = item.assessment
        where = (f" at {pair.root}" if pair.root is not None
                 else f" at {pair.storage.describe()}" if pair.storage is not None else "")
        if pair.plan is None or pair.decision is None:      # not judged: a remote may fail after planning
            s.say(f"destination {pair.index}{where}: {pair.outcome.value} ({', '.join(pair.reasons)}) - {pair.detail}")
            if pair.destination.remote is not None:     # what its service can read is said in every preview
                for notice in item.notices:
                    s.say(f"    notice: {notice}")
            continue
        plan, decision = pair.plan, pair.decision
        whole = next((a.plan for a in pair.alternatives if a.content is Content.MOVE), plan)
        if pair.rebuild:
            state = (f"its record of what to leave where it is was lost ({pair.rebuild}); planned as a first run "
                     f"that leaves what is there - moving it into versions is not offered: {pair.withheld}")
        elif pair.marker is MarkerState.MATCH:
            state = "marker matches"
        elif decision.adoption and whole.destination_files:
            state = f"first run, adopting a destination that already holds {counted(whole.destination_files, 'file')}"
        elif decision.adoption:
            state = "first run, adopting a destination that holds no file but already holds folders or links"
        else:
            state = "first run onto an empty destination"
        s.say(f"destination {pair.index}{where}: {state}")
        for notice in item.notices:
            s.say(f"    notice: {notice}")
        if pair.alternatives:
            _say_content(s, pair)
        elif pair.rebuild and pair.sharing is not None and pair.sharing.leaves_anything:
            _say_left(s, pair.sharing)
            s.say("    files this job backed up here whose source copies are gone, if any, are among them: they stay "
                  "as they are from now on")
        back = item.put_back.keys if item.put_back is not None else frozenset()
        cleanup = pair.cleanup
        if cleanup is not None and cleanup.batches:
            kept = ("; the service may keep deleted files (a trash or older versions) and count them against its "
                    "space until it removes them" if cleanup.trash else "")
            s.say(f"    old versions: {counted(len(cleanup.batches), 'batch', 'batches')} older than "
                  f"{counted(cleanup.keep_days, 'day')}, {counted(cleanup.files, 'file')}, "
                  f"{counted(cleanup.bytes, 'byte')} - deleted permanently by the run{kept}")
        if cleanup is not None:
            for problem in cleanup.problems:
                s.say(f"    version cleanup: {problem}")
        if pair.resolution is None and decision.free_bytes is None:
            s.say("    free space: the service reports none, so it is not checked; a copy that does not fit fails "
                  "file by file")
        s.say(f"    to copy: {len(plan.new)} new, {len(plan.changed)} changed, {counted(plan.bytes_to_copy, 'byte')}")
        gone = "not in the source" if pair.first_run else "deleted at the source"
        again = f" ({len(back)} of them reappeared)" if back else ""
        s.say(f"    into versions: {len(plan.deleted)} {gone}{again}, {len(plan.changed)} overwritten")
        s.say(f"    renamed: {len(plan.renames)}; unchanged: {plan.unchanged}; engine leftovers to remove: "
              f"{len(plan.temporaries)}")
        if plan.kept:
            s.say(f"    kept, because the source could not be read there: {len(plan.kept)}")
        if plan.source_skipped or plan.blocked:
            reasons = {}
            for skip in list(plan.source_skipped) + list(plan.blocked):
                reasons[skip.reason] = reasons.get(skip.reason, 0) + 1
            s.say("    skipped at the source: " + ", ".join(f"{n} {r.value}" for r, n in reasons.items()))
        if plan.signature_losses:
            _samples(s, "lost their file signature", sorted(plan.signature_losses))
        _samples(s, gone, [e.path for e in plan.deleted if e.key not in back])
        if item.put_back is not None:
            _say_put_back(s, item.put_back, plan)
        _samples(s, "overwritten", [c.destination.path for c in plan.changed])
        s.say(f"    verdict: {decision.describe()}")
        if item.estimate is not None:
            s.say(f"    estimate: {item.estimate.describe()}")
        confirm |= decision.verdict in (Verdict.AWAIT_CONFIRMATION, Verdict.ADDITIONS_ONLY)
    return confirm


def _cmd_preview(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    preview = preview_job(s.store, job.job_id, volume_source=s.volume_source(), sync_roots=s.sync_roots(),
                          destinations=s.args.dest, markers=s.markers(job, s.args.marker),
                          content=Content.MOVE if s.args.move else Content.LEAVE)
    needs = _say_preview(s, preview)
    if preview.ticket is not None:
        s.say(f"ticket {preview.ticket.ticket_id}")
        if needs:
            s.say(f"to apply this plan as shown, held removals included: "
                  f"mirrorhold run {job.job_id[:8]} --confirm {preview.ticket.ticket_id}")
    return EXIT_OK


def _cmd_run(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    say = diagnostics.note if s.args.unattended else s.say
    options = RunOptions(tuple(s.args.dest) if s.args.dest else None, s.args.confirm, s.args.unattended,
                         first_backup=s.args.first_backup, markers=s.markers(job, s.args.marker))
    result = run_job(s.store, job.job_id, options, volume_source=s.volume_source(), sync_roots=s.sync_roots(),
                     say=say)
    problem = announce_run(runtime_layout(s.store.path), job, result.records, contents.preferences.notifications,
                           s.notifier, remind=reminding(s.store.path, job.job_id))
    if problem:
        (diagnostics.note if s.args.unattended else s.warn)(f"note: {problem}")
    if result.disabled:
        return EXIT_OK
    outcomes = {r.outcome for r in result.records}
    if Outcome.CANCELLED in outcomes:
        return EXIT_CANCELLED
    if outcomes & {Outcome.FAILED, Outcome.STOPPED, Outcome.INTERRUPTED}:
        return EXIT_FAILED
    if Outcome.HELD in outcomes:
        if not s.args.unattended:
            held = [r for r in result.records if r.outcome is Outcome.HELD]
            first = [r.destination_index for r in held if r.reasons == (FIRST_RUN_REASON,)]
            for index in first:
                s.say(f"the first backup onto destination {index} waits: mirrorhold run {job.job_id[:8]} "
                      "--first-backup")
            if len(first) < len(held):
                s.say(f"held work waits for confirmation: mirrorhold preview {job.job_id[:8]}, "
                      "then run with --confirm")
        return EXIT_HELD
    return EXIT_OK


def _one_destination(s: _Session, job: Job) -> Tuple[Optional[int], Optional[str]]:
    """The destination ``verify`` or ``repair`` was given — (its number, None) or (None, its
    marker) — or the job's only one when it was given none."""
    if s.args.marker is not None:
        return None, s.markers(job, [s.args.marker])[0]
    if s.args.dest is not None:
        return s.args.dest, None
    if len(job.destinations) != 1:
        raise _Usage(f"the job has {counted(len(job.destinations), 'destination')}; "
                     "name one with --dest or --marker")
    return 1, None


def _cmd_verify(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    index, marker = _one_destination(s, job)
    try:
        record = verify_destination(s.store, job.job_id, index, marker=marker, volume_source=s.volume_source(),
                                    restart=s.args.restart, say=s.say,
                                    mode=VerificationMode.NEW_COPIES if s.args.new_copies else VerificationMode.FULL)
    except VerifyError as exc:
        s.failure = s.failure or str(exc)
        raise _Usage(f"cannot verify: {exc}") from None
    if record is None:
        s.say("stopped before it began")
        return EXIT_CANCELLED
    problem = announce_verification(record, contents.preferences.notifications, s.notifier,
                                    remind=reminding(s.store.path, job.job_id))
    if problem:
        s.warn(f"note: {problem}")
    if record.state is VerificationState.STOPPED:
        return EXIT_CANCELLED
    if record.state is not VerificationState.COMPLETED or record.differ_total:
        return EXIT_FAILED
    return EXIT_OK


def _cmd_repair(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    index, marker = _one_destination(s, job)
    try:
        record = repair_destination(s.store, job.job_id, index, marker=marker, volume_source=s.volume_source(),
                                    sync_roots=s.sync_roots(), say=s.say)
    except RepairError as exc:
        s.failure = s.failure or str(exc)
        raise _Usage(f"cannot repair: {exc}") from None
    if record is None:
        s.say("stopped before it began")
        return EXIT_CANCELLED
    problem = announce_repair(record, contents.preferences.notifications, s.notifier,
                              remind=reminding(s.store.path, job.job_id))
    if problem:
        s.warn(f"note: {problem}")
    if record.state is RepairState.STOPPED:
        return EXIT_CANCELLED
    if record.state is not RepairState.COMPLETED or record.failures:
        return EXIT_FAILED
    return EXIT_HELD if record.left else EXIT_OK


def _waited_for(st: dict) -> Optional[str]:
    """"the run of 'Photos' (1a2b3c4d5e6f) to finish; it is paused; both use drive I:; 2nd in
    line": what a waiting run or verification waits for, when its status names it — the
    drive they share when that alone is why, and its place in line. One whose status the
    waiting one cannot read now is named as it last read it, else by ``ANY_KIND``: its kind is
    not known."""
    other = WaitedFor.from_status(st)
    if other is None:
        return None
    if other.kind:
        what = {KIND_VERIFICATION: "a verification", KIND_REPAIR: "a repair"}.get(other.kind, "the run")
        text = f"{what} of '{other.job_name}' ({other.run_id[:12]}) to finish"
    else:
        text = f"a {ANY_KIND} ({other.run_id[:12]}) to finish"
    if not other.readable:
        text += f"; {UNREADABLE}"
    elif other.paused:
        text += "; it is paused"
    elif other.waiting:
        text += "; it waits too"
    if other.drive:
        text += f"; both use {other.drive}"
    place = st.get("place")
    if isinstance(place, int) and not isinstance(place, bool) and place > 0:
        text += f"; {line_words(place)}"
    return text


def _waiting(st: dict) -> str:
    """What a waiting run or repair waits for: the activity its status names; "starting" for
    one of this copy starting (``records.admission.starting``); its turn, for one of this
    copy that has asked and names nothing; the run in progress, for an older copy's."""
    other = _waited_for(st)
    if other is not None:
        return f"waiting for {other}"
    if starting(st):
        return "starting"
    return "waiting for its turn" if isinstance(st.get("admitted"), bool) else \
        "waiting for the run in progress to finish"


def _say_verification(s: _Session, head: str, st: dict) -> None:
    verify = st.get("verify") or {}
    flags = " [paused]" if st.get("paused") else ""
    what = "verification of new copies of" if verify.get("mode") == VerificationMode.NEW_COPIES.value \
        else "verification of"
    s.say(head + f"  {what} {st.get('destination')}")
    if st.get("phase") == "waiting":
        other = _waited_for(st)
        if other is not None:
            s.say(f"    waiting for {other}" + ("; this verification gave way to it" if verify.get("gave_way") else "")
                  + flags)
        elif verify.get("gave_way"):
            s.say("    waiting for a run to finish: it gives way to runs" + flags)
        else:
            s.say(f"    {_waiting(st)}" + flags)
        return
    if st.get("phase") == "listing":
        s.say(f"    listing the {verify.get('listing') or 'source'}: {counted(st.get('listed', 0), 'entry', 'entries')}"
              + flags)
        return
    eta = st.get("eta_seconds")
    differ = verify.get("differ", 0)
    s.say(f"    {st.get('phase')}{flags}: {verify.get('files_done', 0)} of {counted(verify.get('files', 0), 'file')}, "
          f"{verify.get('bytes_done', 0)} of {counted(verify.get('bytes', 0), 'byte')}; "
          f"{differ} {agreeing(differ, 'differs', 'differ')}; "
          f"{st.get('rate', 0) / (1024 * 1024):.1f} MiB/s" + (f", about {eta:.0f} s left" if eta is not None else ""))


def _say_repair(s: _Session, head: str, st: dict) -> None:
    flags = (" [paused]" if st.get("paused") else "") + (" [stopping]" if st.get("cancelling") else "")
    s.say(head + f"  repair of {st.get('destination')}")
    if st.get("phase") == "waiting":
        s.say(f"    {_waiting(st)}" + flags)
        return
    planned, done = st.get("planned") or {}, st.get("done") or {}
    s.say(f"    {st.get('phase')}{flags}: {done.get('files_copied', 0)} of "
          f"{counted(planned.get('files_to_copy', 0), 'copy', 'copies')} copied, {done.get('bytes_copied', 0)} of "
          f"{counted(planned.get('bytes_to_copy', 0), 'byte')}")


def _cmd_status(s: _Session) -> int:
    runs = sorted(observe_runs(runtime_layout(s.store.path)), key=line_order)
    if not runs:
        s.say("no run in progress")
    for observed in runs:
        st = observed.status
        # "run <identifier>" heads every activity here, whatever its kind, as the requests'
        # RUN argument names any of them.
        if not observed.readable:
            s.say(f"run {observed.run_id[:12]}  - alive; {UNREADABLE}, so every run, verification or repair asking "
                  "for its turn waits for it")
            continue
        head = (f"run {observed.run_id[:12]}  job '{st.get('job_name')}' ({st.get('trigger')})  "
                f"started {st.get('started')}")
        if not observed.alive:
            s.say(head + "  - its process has ended; the next run records it as interrupted")
            continue
        if is_verification(st):
            _say_verification(s, head, st)
            continue
        if is_repair(st):
            _say_repair(s, head, st)
            continue
        flags = (" [paused]" if st.get("paused") else "") + \
                (f" [cancelling: {st['cancelling']}]" if st.get("cancelling") else "")
        s.say(head)
        if st.get("phase") == "waiting":
            s.say(f"    {_waiting(st)}" + flags)
            continue
        planned, done = st.get("planned") or {}, st.get("done") or {}
        eta = st.get("eta_seconds")
        identical = (f" ({done['files_identical']} already identical, "
                     f"{counted(done.get('bytes_identical', 0), 'byte')})" if done.get("files_identical") else "")
        examined = (f" ({st.get('examined', 0)} of {counted(st['to_examine'], 'changed file')} examined)"
                    if st.get("to_examine") else "")
        s.say(f"    {st.get('destination')}: {st.get('phase')}{examined}{flags}; copied {done.get('files_copied', 0)} "
              f"of {counted(planned.get('files_to_copy', 0), 'file')}, {done.get('bytes_copied', 0)} of "
              f"{counted(planned.get('bytes_to_copy', 0), 'byte')}{identical}; {done.get('moved_to_versions', 0)} into "
              f"versions, {done.get('renamed', 0)} renamed; {st.get('rate', 0) / (1024 * 1024):.1f} MiB/s"
              + (f", about {eta:.0f} s left" if eta is not None else ""))
        for name, size, got in st.get("in_flight") or []:
            s.say(f"    in flight: {name} ({got} of {counted(size, 'byte')})")
        for name in st.get("comparing") or []:
            s.say(f"    comparing: {name}")
    return EXIT_OK


def _cmd_history(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    layout = runtime_layout(s.store.path)
    live = {r.run_id for r in observe_runs(layout) if r.alive}
    entries = sorted([(r.started, r.describe()) for r in read_records(layout, job.job_id)]
                     + [(v.started, v.describe(live)) for v in read_verifications(layout, job.job_id)]
                     + [(r.started, r.describe(live)) for r in read_repairs(layout, job.job_id)],
                     key=lambda entry: entry[0])
    if not entries:
        s.say("no runs recorded")
    for _started, text in entries[-max(1, s.args.limit):]:
        s.say(text)
    return EXIT_OK


def _control(s: _Session, **wanted) -> int:
    layout = runtime_layout(s.store.path)
    observed = find_run(layout, s.args.run)
    try:
        result = request(layout, observed.run_id, **wanted)
    except FileBusy:
        raise _Usage(REQUEST_NOT_WRITTEN) from None
    state = "cancel " + result.cancel.value if result.cancel else ("pause" if result.paused else "run")
    name = f"'{observed.status.get('job_name')}'" if observed.readable else UNREADABLE
    s.say(f"run {observed.run_id[:12]} ({name}): asked to {state}")
    return EXIT_OK


def _cmd_pause(s: _Session) -> int:
    return _control(s, pause=True)


def _cmd_resume(s: _Session) -> int:
    return _control(s, pause=False)


def _cmd_cancel(s: _Session) -> int:
    return _control(s, cancel=CancelMode.IMMEDIATE if s.args.now else CancelMode.GRACEFUL)


def _cmd_when(s: _Session) -> int:
    contents = s.load()
    job = s.find(contents, s.args.job)
    on_arrival, schedule, enabled = job.triggers.on_arrival, job.triggers.schedule, job.enabled
    if s.args.arrival is not None:
        on_arrival = s.args.arrival == "on"
    if s.args.schedule is not None:
        schedule = None if s.args.schedule.strip().lower() == "none" else s.args.schedule
    if s.args.automatic is not None:
        enabled = s.args.automatic == "on"
    try:
        changed = replace(job, triggers=Triggers(on_arrival, schedule), enabled=enabled)
    except ContractError as exc:
        raise _Usage(f"not a schedule: {exc}") from None
    s.save(contents.with_job(changed))
    s.say(f"job '{changed.name}' runs {_when(changed)}")
    return EXIT_OK


def _label(label: Optional[str]) -> str:
    return f"release {label}" if label else "release unknown"


def _portable_state_line(state: InstallationState) -> str:
    kept = f"jobs and records in {state.store_folder}"
    copy = state.copy
    if copy is None:
        if state.blocker is not None:
            return (f"not set up: MirrorHold is installed from source on this account ({state.blocker.source}); "
                    "uninstall it there before setting up this copy")
        return f"not set up: jobs run only when started, and there is no Start menu entry; {kept}"
    if state.here:
        return f"set up here: {release_name(state.release)}, {copy.installed_at}; {kept}"
    return (f"set up at {copy.root} ({_label(copy.label)}), not here"
            + (", which no longer exists" if state.gone else ""))


def _install_state_line(s: _Session) -> str:
    state = installation_state(s.store.path, target=s.install_root, shortcut=s.shortcut,
                               account_store=s.account_store)
    if state.portable:
        return _portable_state_line(state)
    if not state.installed:
        if state.blocker is not None:
            return (f"not installed from this working copy; automatic runs use the copy of MirrorHold at "
                    f"{state.blocker.root} ({_label(state.blocker.label)})")
        return "not installed: jobs run only when started, and there is no Start menu entry"
    copy = state.copy
    line = f"installed at {copy.root}: version {copy.version}, {copy.installed_at}"
    if not state.serves_store:
        line += f"; it serves another job file ({copy.store})"
    return line


def _cmd_installation(s: _Session) -> int:
    s.say(_install_state_line(s))
    return EXIT_OK


def _cmd_install(s: _Session) -> int:
    contents = s.load()
    try:
        result = install(s.store.path, contents.jobs, target=s.install_root, shortcut=s.shortcut,
                         scheduler=s.scheduler, account_store=s.account_store)
    except InstallError as exc:
        raise _Usage(str(exc)) from None
    if result.copy.portable:
        s.say(f"set up this copy at {result.copy.root}: {_label(result.copy.label)}")
    else:
        s.say(f"installed version {result.copy.version} at {result.copy.root}")
    if result.shortcut is not None:
        s.say(f"Start menu entry: {result.shortcut}")
    s.say(result.automation.describe())
    for problem in result.problems:
        s.warn(f"warning: {problem}")
    return EXIT_OK


def _cmd_uninstall(s: _Session) -> int:
    try:
        result = uninstall(target=s.install_root, shortcut=s.shortcut, scheduler=s.scheduler, store_path=s.store.path,
                           only_if_here=s.args.if_set_up_here)
    except InstallError as exc:
        raise _Usage(str(exc)) from None
    tasks = counted(result.removed_tasks, "scheduled task")
    if result.not_here:
        where = f"set up at {result.set_up.root}" if result.set_up is not None else "not set up"
        s.say(f"this copy is not the one set up ({where}): nothing was removed")
    elif result.portable:
        s.say(f"removed the Start menu entry and {tasks}; the jobs, their records, every destination and this "
              "folder are kept")
    else:
        s.say(("removed the installed copy" if result.removed_copy else "no installed copy was found")
              + f" and {tasks}; the jobs, their records and every destination are kept")
    for problem in result.problems:
        s.warn(f"warning: {problem}")
    return EXIT_OK


def _cmd_reconcile(s: _Session) -> int:
    contents = s.load()
    outcome = apply_automation(s.store.path, contents.jobs, root=s.install_root, scheduler=s.scheduler)
    (s.say if outcome.ok else s.warn)(outcome.describe())
    return EXIT_OK if outcome.ok else EXIT_REFUSED


def _cmd_automatic(s: _Session) -> int:
    diagnostics.mark(diagnostics.JOB, job=s.args.job)
    common = dict(volume_source=s.volume_source(), sync_roots=s.sync_roots(), notifier=s.notifier)
    if s.args.by == ARRIVAL:
        result = run_on_arrival(s.store, s.args.job, **common)
    else:
        result = run_scheduled(s.store, s.args.job, **common)
    for run in result.runs:
        for record in run.records:
            s.say(record.describe())
    if result.nothing_because:
        s.say(f"nothing ran: {result.nothing_because}")
    for problem in result.problems:
        s.warn(f"note: {problem}")
    s.failure = s.failure or result.failure
    return EXIT_OK


_COMMANDS = {"volumes": _cmd_volumes, "jobs": _cmd_jobs, "add": _cmd_add, "remove": _cmd_remove,
             "validate": _cmd_validate, "resolve": _cmd_resolve, "export": _cmd_export,
             "import": _cmd_import, "engine": _cmd_engine, "remotes": _cmd_remotes, "password": _cmd_password,
             "unlock": _cmd_unlock, "preview": _cmd_preview, "run": _cmd_run,
             "verify": _cmd_verify, "repair": _cmd_repair,
             "status": _cmd_status, "history": _cmd_history, "pause": _cmd_pause, "resume": _cmd_resume,
             "cancel": _cmd_cancel, "when": _cmd_when, "install": _cmd_install, "uninstall": _cmd_uninstall,
             "installation": _cmd_installation, "reconcile": _cmd_reconcile, "automatic": _cmd_automatic}


def main(argv: Optional[Sequence[str]] = None, *, volume_source: Optional[VolumeSource] = None,
         sync_roots: Optional[Sequence[SyncRoot]] = None, out=None, err=None, scheduler: Optional[Scheduler] = None,
         notifier: Optional[Notifier] = None, install_root: Optional[Path] = None,
         shortcut: Optional[Path] = None, account_store: Optional[Path] = None,
         ask_password: Optional[Callable[[str], str]] = None) -> int:
    """Run one command. ``notifier`` None raises no notice; the real entry point (``run``)
    passes the Windows one. ``install_root``, ``shortcut`` and ``account_store`` name
    another installed copy, Start menu entry and portable job file than the account's own
    (tests). ``ask_password`` asks for a password without showing it; the console's own
    unless given (tests)."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    session = _Session(args, volume_source, sync_roots, out, err, scheduler=scheduler, notifier=notifier,
                       install_root=install_root, shortcut=shortcut, account_store=account_store,
                       ask_password=ask_password)
    layout = runtime_layout(session.store.path)
    logged = args.command in LOGGED_COMMANDS
    with diagnostics.process_log(layout, args.command) if logged else nullcontext():
        if logged:
            diagnostics.note("mirrorhold " + " ".join(str(a) for a in (sys.argv[1:] if argv is None else argv)))
            diagnostics.mark(diagnostics.START, **_started(args))
        status = _execute(session, layout, logged, err)
        if logged:
            diagnostics.mark(diagnostics.END, status=status, failure=session.failure)
        return status


def _started(args) -> dict:
    """What a logged command's start mark says: the command, the job and the destinations it
    was given by number, and what started it. Destinations given by marker are numbered by
    the job mark, once the job is found (``_Session.markers``)."""
    if args.command == "automatic":
        return dict(command=args.command, job=args.job, trigger=args.by, destinations=None)
    if args.command in ("verify", "repair"):
        return dict(command=args.command, job=args.job, trigger="manual",
                    destinations=[args.dest] if args.dest is not None else None)
    return dict(command=args.command, job=args.job, trigger="unattended" if args.unattended else "manual",
                destinations=list(args.dest) if args.dest else None)


def _execute(session: _Session, layout, logged: bool, err) -> int:
    args = session.args
    try:
        return _COMMANDS[args.command](session)
    except StoreConflict:
        session.refused("refused: ", SAVE_CONFLICT)
        return EXIT_REFUSED
    except (_Usage, RunError, NoSuchRun) as exc:
        session.refused("refused: ", str(exc))
        return EXIT_REFUSED
    except StoreError as exc:
        session.refused("store: ", str(exc))
        return EXIT_STORE
    except Exception as exc:  # a failure no one foresaw: its traceback to a log, one line to the person
        context = f"{args.command} failed unexpectedly"
        log = diagnostics.note_failure(context) if logged else diagnostics.failure_log(layout, args.command, context)
        if log is None:
            raise
        session.failure = session.failure or f"{type(exc).__name__}: {exc}"
        print(f"failed unexpectedly: {type(exc).__name__}: {exc}; the details are in {log}", file=err)
        return EXIT_REFUSED


def run() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(notifier=default_notifier()))
