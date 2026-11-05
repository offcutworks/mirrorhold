# MirrorHold

A Windows tool that keeps one-way mirrors of the user's data on backup drives. A job
copies one source (a folder or a drive root) to one or more destinations; runs happen
when a backup drive is attached, on a schedule, or by hand. A plain mirror is not a
backup — it carries an accidental deletion, an encrypted file or an empty mis-mounted
source straight into the copy — so the tool's value is the safety around the copy:
identity checks, holds, versions. The copy itself is delegated to one pinned rclone
binary.

**Governing rule.** Nothing inside a source is ever changed, and nothing on a
destination is destroyed except by the user's opt-in version cleanup: every removal or
replacement on a destination is a move into that destination's versions. Where safety
and automation conflict, safety wins.

**Stage.** Pre-release, with no users and no promise of compatibility between versions —
of their files, settings or commands. The design may describe a part before it is built,
which is not a defect.

## What exists

| Part | State |
|---|---|
| Job contract (a destination a folder of this computer's drives or of a remote of the user's rclone configuration), groups of jobs and configuration store | Built (`mirrorhold/jobs/model.py`, `store.py`) |
| Endpoint identity, resolution, destination markers, shared destinations and the folders no run touches | Built (`mirrorhold/endpoints/`) |
| Creation-time validation and the source survey | Built (`mirrorhold/jobs/validation.py`, `survey.py`) |
| Engine adapter (locate, version pin, windowless start with its configuration named, explicit-list transfer and its control, onto a folder of this computer or of a remote; a remote's folders and files through one engine serving a session, which a run, a preview, a verification and a repair reach; the engine's check of an encrypted remote's copies) | Built (`mirrorhold/engine/rclone.py`, `remote.py`) |
| The destination-storage seam, the one place that reaches a destination's files: a folder of this computer, or a folder of a remote through one engine serving a session; the run, the preview, the version cleanup, verification, repair, the records' copies on a destination and the window's look at one reach a drive's through it, and a remote's the run, the preview, the version cleanup, verification, repair, the records' copies there and `resolve`'s look at its marker | Built (`mirrorhold/storage/`) |
| The user's rclone configuration as it is reached: which file, its unlock, the remembered password sealed for the user's Windows account, a look at a remote folder through the engine, the capability floor a remote must meet | Built (`mirrorhold/remotes/`) |
| Command line: volumes, jobs, add, remove, validate, resolve, export, import, engine, remotes, password, unlock, preview, run, verify, repair, status, history, pause, resume, cancel, when, install, uninstall, installation, reconcile, automatic | Built (`mirrorhold/cli.py`) |
| Planning, the run and its safety gate, status, records, run control | Built (`mirrorhold/planning/`, `mirrorhold/run/`, `mirrorhold/records/`) |
| The computer kept from falling asleep by itself, and the work named on Windows' shutdown screen, while a run, verification or repair works, as the user chooses | Built (`mirrorhold/power.py`) |
| An icon in the notification area while a run, verification or repair works — its progress, the window, pause, resume and cancel — brought up by the activity and shown by the window's unit in a short-lived process of its own | Built (`mirrorhold/tray.py`, `mirrorhold_gui/tray_model.py`, `tray_view.py`) |
| Automatic runs on arrival and on a schedule (Task Scheduler), installation — from a working copy, or a portable release set up where it lies, unpacked by the user or installed by the release's setup program — and notifications | Built (`mirrorhold/automation/`, `mirrorhold/installation/`, `mirrorhold/notifications/`, the schedule grammar in `mirrorhold/jobs/schedule.py`, how a copy starts itself in `mirrorhold/invocation.py`) |
| The release's build: the runtime it carries, the file a user double-clicks, the icon, the third-party notices, the build record, the zip, the setup program that installs the zip's files, and their checksums | Built (`tools/build_release.py`, on the shared build core `tools/release_core.py`, its inputs obtained by `tools/fetch_build_inputs.py`; the setup program's definition `tools/setup/MirrorHold.iss`; *Building a release*) |
| GUI: jobs arranged in groups and moved up and down (free space and last verification included), several jobs or a group run or removed at once, editor (snapshot reading, When to run, Versions and verification included), preview and confirmation, the one question before a first backup into a folder that holds nothing, the choice a first run into a folder that holds something offers, Run now, Verify (the new copies only, or in full), runs and verifications in progress with pause, resume and cancel, what a waiting one waits for and its place in line, history and run details (how the source was read, files found identical, old versions deleted, free space), verification details, replacing the backup copies a verification found to differ and a repair's details, files not backed up, preferences (the engine, the rclone configuration and its unlock, notifications, runs on one drive, the computer kept awake and the work named on Windows' shutdown screen while one works, installation), import and export, the history of a group or of every job and its saving, the diagnostic logs' saving, an unfinished verification and a start that left no record shown in the list, a remote destination set up through the engine and followed from the run records, its address shown where a drive's folder opens | Built (`mirrorhold_gui/`) |
| Reading live sources from a volume snapshot; files not backed up counted run by run | Built (`mirrorhold/snapshots/`, `mirrorhold/records/record.py`) |
| Version cleanup, full verification and its reminder, the verification of new copies and the list of verified copies it builds on, the repair of the copies a verification found to differ, the low free-space warning | Built (`mirrorhold/maintenance/`, `mirrorhold/records/record.py`) |
| Obtaining the pinned engine: download, both digests checked, unpacked into `vendor/` | Built (`tools/fetch_engine.py`) |

## Architecture

Three units, one dependency direction: **GUI → core → engine** (an external process).
`mirrorhold` is the core. It has no GUI dependency; the GUI (`mirrorhold_gui`)
imports it to validate, to preview and to read state, and every run executes in a core
process of its own, so a GUI failure never stops a run. The configuration — the job
contract in `mirrorhold/jobs/model.py` — is the seam between GUI and core: the core
never trusts that its caller validated and re-validates a job whenever it loads one.

Inside the core, decomposition is by sub-domain:

```
mirrorhold/
|-- __main__.py         python -m mirrorhold -> cli.run
|-- cli.py              the command line
|-- locations.py        the one place runtime paths are decided, the kind of copy, the
|                       files a copy ships beside its packages
|-- invocation.py       how a copy starts its own core and GUI
|-- icon.py             the program's icon, drawn from its geometry
|-- exchange.py         the one way a file the core's processes share is written and read
|-- power.py            what a working activity asks of the computer: kept awake
|-- tray.py             an activity bringing up the notification-area icon's process
|-- reminders.py        what a finished activity left for the user to look at, one per
|                       job, kept until the user opens the window
|-- contract_errors.py  ContractError, located at its field
|-- wording.py          a count with what it counts, one for one and the plural for any other
|-- endpoints/          volume identity, endpoints, the present volumes (VolumeSource),
|                       resolution, the management folder and marker, a shared
|                       destination's record, the folders cloud clients synchronise and
|                       what each does with its folder, the folders Windows and this
|                       tool need
|-- jobs/               the job contract, the store, validation, the survey, the
|                       built-in classifications and exclusions of entries
|-- storage/            the one place that reaches a destination's files: a folder of
|                       this computer, or a folder of a remote through the engine
|-- remotes/            the user's rclone configuration as it is reached: its unlock, the
|                       remembered password, what the engine says of a remote folder,
|                       the capability floor
|-- planning/           one side's listing, a pair's plan, a shared destination's part,
|                       file signatures, the estimate
|-- run/                the safety gate, confirmation tickets, applying a plan, run
|                       control, running and previewing a job, starting a run process
|-- records/            run records, live status and its control channel, admission
|                       (which runs, verifications and repairs work at once), locks, the
|                       diagnostic log and its reading back
|-- snapshots/          reading a source from a volume snapshot: the provider seam,
|                       Windows' through WMI, the snapshots runs made, the decision per
|                       run, the removal of what a run left behind
|-- maintenance/        version cleanup (expired batches, deleted inside a run),
|                       verification, full or of new copies (its records, how a copy is
|                       compared, the list of verified copies, the reminder, the
|                       verification process), the
|                       repair of the copies one found to differ (its records, the
|                       repair process)
|-- automation/         scheduled tasks derived from the jobs and reconciled, which
|                       drives arrived, the entry a scheduled task runs
|-- installation/       the installed copy (from a working copy, or a portable release
|                       set up in place), its record, a release's build record, the
|                       Start menu entry
|-- notifications/      the notice a run raises when it ends, and the reminder one that
|                       asks something of the user hands on
|-- engine/             the engine adapter — the only code that knows rclone: its starts
|                       and the explicit-list copy; a remote's folders and files through
|                       one engine serving a session
```

Every unit that reads or writes a destination's files — the run and the preview, the
version cleanup, the verification, the repair, the copies of the records a destination
keeps, and the window's look at a destination's marker and at what it holds — reaches them
through one seam, `mirrorhold/storage/`. The policy that guards a destination (the marker's
proof, a shared destination's record, the fences of the version cleanup, every move into
versions, what reappeared after one) is written once, above it. Below it a destination is
a folder of this computer, each operation a drive's own — a listing by directory reads, a
small file written whole through the shared files' writer, a move a rename on the
destination's volume — or a folder of a remote, each operation one the engine performs
(*The engine*). A job may name a remote's folder as a destination (*The job contract*):
validation, `resolve` and the window, when the user acts, look at one through the engine
(`mirrorhold/remotes/`), and a run
and a preview back one up through one engine session kept for their whole length (*A
remote destination*, under *The run*); a verification and a repair reach one through a
session of their own and compare its copies by the fingerprints its service keeps wherever
it allows (*Maintenance*). A drive destination's
free space, as the gate, the record and the low-space warning judge it, is still read from
the scan of the volumes, and so is where it lies; a remote's is asked of its service.

The GUI is a unit of its own beside the core, decomposed by sub-feature; each
sub-feature's `*_model.py` holds its logic and imports no toolkit, its `*_view.py`
builds the Tk widgets:

```
mirrorhold_gui/
|-- __main__.py, app_view.py      python -m mirrorhold_gui; display, identity, icon, job file
|-- session.py, refresh.py        the store, volumes and launched runs; observation threads
|                                 (drives, records, the activities' logs; the runs' status)
|-- formatting.py                 quantities, sizes, times and lists in words
|-- jobs_model.py, main_view.py   the job list and the main window
|-- window_model.py               where the main window opens: its remembered place
|-- groups_model.py, groups_view.py      groups, what a selection allows, New / Rename group
|-- editor_model.py, editor_view.py      New job / Edit job
|-- preview_model.py, preview_view.py    preview and confirmation
|-- runs_model.py, runs_view.py          runs, verifications and repairs in progress, cancel, stop
|-- tray_model.py, tray_view.py          the notification-area icon while an activity works or a
|                                        reminder stands, in a process of its own
|-- history_model.py, history_view.py    the selection's history, run and start details, saving it
|-- verify_model.py, verify_view.py      Verify... and a verification's details
|-- repair_model.py, repair_view.py      the question before a repair, and a repair's details
|-- skipped_model.py, skipped_view.py    a destination's files not backed up
|-- prefs_model.py, prefs_view.py        preferences (engine, rclone configuration, notifications,
|                                        runs on one drive, the computer while one works,
|                                        installation)
|-- remote_model.py, remote_view.py      a remote reached off the window's thread, choosing its
|                                        folder, where one is
|-- about_model.py, about_view.py        About, the licence and a release's notices
|-- shipped_model.py              what a copy ships for the window: licence, notices, icon
|-- transfer_model.py, transfer_view.py  import and export
|-- startup_model.py, startup_view.py    a corrupt, newer or held job file at start
|-- common_view.py                sizing, scrolling, styles, monitors, shared dialogs, hints
```

The GUI unit runs as two kinds of process. The window is started by the user. The icon in
the notification area (`tray_view`) is started by a working activity's core process, which
composes its command as it composes every start of a copy (`mirrorhold/invocation.py`) and
imports nothing of the GUI (*The notification-area icon*, under *The run*); once up, it reads
the statuses and asks its requests as the window does, and reads the job file beside the
statuses without ever acting on it — a file it cannot read is neither moved aside nor waited
for — to number each activity's destination as its job stands now; so a fault in it costs the
icon and never the work, as a window's failure never stops a run.

Each package's `__init__.py` carries its own architecture record.

### Files the processes share

The core's processes and the window share no memory: they share files beside the store —
the job file, each activity's status, control file and liveness lock, the run,
verification and repair records, the preview's tickets, the lock the notification-area
icon's process holds, the reminders finished activities left — and, on a destination, its
marker and a shared destination's record.
Windows refuses to replace a file another process has open,
and to open one while it is being replaced, so every such file is written and read
through one exchange (`mirrorhold/exchange.py`): a reader opens it sharing reading,
writing and deletion, and a writer writes a staging file beside it and renames it over
with POSIX semantics, which Windows allows while such readers hold the old file (a volume
that does not support that rename — FAT, exFAT — is renamed over the ordinary way).
Measured on the development machine, two processes replacing and reading one file in
tight loops for 6 s: no refusal on either side, against a third of the writes and of the
reads refused the ordinary way; a dozen such pairs beside a dozen busy processes had no
write of 98,000 refused, and about one read in 9,000 for a moment, which a read's patience
bears. What the exchange does not control — another program
holding the file without sharing, as an antivirus scanning a new file does — is tried
again for a bounded patience, and past it the caller decides: a waiting run,
verification or repair reads the job file again at its next attempt, and an admitted one keeps the
choices for the computer it last read; a verification's progress
waits for its next save; a request a person made is reported as not written; the window
keeps what it last read, says so in its status bar and reads again at its next look; a
status that stays unreadable while its lock is held is an activity alive whose claim is unknown,
so it conflicts with every other, and so is a status that is not there while its lock is held —
a look can meet a status at the instant it is renamed over, so an activity is found by its lock
as well as by its status, and a missing one is looked for again for 0.2 s first; a control file that cannot be read,
or that is not there for the moment a request renames a new one over it, leaves what its run
wants as it was. Requests to one run take turns: each reads, changes and writes the control
file holding a lock of requests alone (`<run>.request`, there only while one holds it), so
two made at one instant each keep the other's change and a request never meets the file
being renamed over, and one that waits for its turn past its patience is reported as not
written. A run that ends takes that turn before it removes any of its files, its status
included, and keeps it until its liveness lock is gone, and a request writes only while
that lock is held: one made as the run ends lands before the control file goes or finds
the run ended and writes nothing, so no control file outlives its run — unless a request
keeps the turn longer than the ending run waits for it (eleven seconds), when that request
alone may leave its file. A file that is not there is never a refusal; a file being renamed over is
not there for a moment, which a reader meets only under load, so a reader to whom absence
means something looks again — the job file's under its save lock (*The configuration
store*, below), a status's as above, and for 0.2 s a file that, once written, is only ever
renamed over: a drive destination's marker and shared record, the remembered password, and
a verification's or a repair's record its folder's listing named — or keeps what it read
last where the file, once written, stays, as a run's control file does for its run. A drive
destination with no marker yet is therefore told so 0.2 s later, which the window bears on
the thread that looks at the destinations. A file one process alone
writes and reads, or that a lock of its own serialises — an arrival's state, a snapshot's
record, the installation's records — is written the ordinary way.

### Identity

A volume is identified by an anchor plus its file system's serial number, never by
letter or label: the GPT partition GUID; or the MBR disk signature and partition
offset; or, for a volume no disk partition carries, the mount manager's volume GUID.
A reformatted volume stops matching; a sector-level clone matches twice, and
resolution refuses rather than guess. A destination is usable only when, in addition,
the marker in its hidden management folder (`.mirrorhold/marker.json`, format
`mirrorhold-marker`) names the job and the destination's marker identity. A marker is
written in the lowest schema that says what it must: schema 1 for a destination that is
its job's whole, schema 2 (`content: shared`) for a shared one (*What a first run does with
what a folder holds*, below). A marker without a schema version predates the field and
reads as schema 1; one of a schema this copy does not know reads as unreadable, which
stops the destination untouched — so a copy of the tool that does not know shared
destinations stops at one rather than move what it must leave.

### The job contract

A job has an identifier and a name, an enabled switch, one source (endpoint, snapshot
reading on by default, the switchable default exclusions — sync-client artefacts and
transient editor files — and the user's own patterns), one or more destinations (each
an endpoint and a marker identity; the endpoint a folder of this computer's drives, or a
folder of a remote, below), triggers (on arrival; a schedule, its days and then its
times: `daily`, `weekly DAY[,DAY...]`, `monthly D` (1 to 28) or `monthly last`, then
`HH:MM[,HH:MM...]` (at most 48 times) or `every Nh` (N from 1 to 12) with an optional
window `HH:MM-HH:MM` that may cross midnight and belongs to the day it opens on — kept in
its canonical text, `jobs/schedule.py`), hold
thresholds (count floor 1000, 5 per cent, and the share hold: half of the destination's
files, adjustable from above 0 to 100 per cent or switched off), version cleanup (off, or
keep N days) and a verification reminder (90 days). Neither trigger is set for a new job: it runs only
when the user starts it. The enabled switch governs automatic runs only (the GUI shows
it as "Pause automatic runs"); a run the user starts always runs. A drive-root source
always excludes the operating system's root artefacts, which are therefore not a
setting. Per-destination state (first run confirmed, last success, last full
verification) is not configuration and is not stored with it: it is derived from the
run records, the verification records and the marker.
Application preferences sit beside the jobs: the engine's location, notification
choices, the choices for runs and verifications as they work, the GUI's remembered state,
and the rclone configuration remote destinations are reached through with its unlock
(`RemoteSettings`, below). The choices for activities (`Activities`) are three: when activities that use one
drive take turns on it — automatic, by what the drive is, unless the user chooses always
or never (the maintainer's decision of 2026-10-21; *Turns on a drive*, below), with what
the user says each drive is where they said it (`Activities.drive_kinds`); the
computer kept from falling asleep by itself while an activity works (*The computer while
work goes on*, below); and Windows' shutdown screen naming the activity at work and
offering to cancel the shutdown (the same section). The last two are on unless the user
changes them (the maintainer's decisions, 2026-10-13).

A **remote destination** (the maintainer's decisions of 2026-10-28 and 2026-10-29) is a
folder of a remote the user made with rclone: the remote's name as the configuration spells
it and a folder inside it, `/`-separated, written `remote:folder` as rclone writes it
(`endpoints/endpoint.py`, `RemoteEndpoint`). A name rclone would not accept is refused, and so
is a name of one letter, which the engine on Windows reads as a drive; a folder holding a
backslash, a control character or an empty, `.` or `..` component is refused; a leading `/`
is kept, since some services read it as their file system's top, and plays no part when two
folders are compared. Its document holds `remote` and `folder` where a drive's holds `volume`
and `path`, so a drive's destination is written exactly as before. A source is always a
folder of this computer. The configuration is one for every job: the preferences keep its
path and its unlock — none, the password remembered (*Runtime files and privacy*), or a
command of the user's own that prints it, kept as its program and arguments — and nothing
the configuration holds. MirrorHold never reads or copies that file and never signs in to a
service; the engine, handed the file and its unlock, does. A remote's folder is proved the
job's by its marker as a drive's is (*Identity*), and two remote folders contain one another
by remote and folder compared without case, which refuses more overlaps rather than fewer.

A **group** is a name the user keeps jobs together under (the maintainer's decision of
2026-10-10: part of the job file, not of the window alone): an identifier, a name and its
jobs in the order the user arranged them — the order they were put in it until one is
moved. A job belongs to at most one group, every member is a stored job, and no two groups
share a name, compared without case. A group arranges jobs and nothing more: no run,
verification or scheduled task is a group's. The job file holds one order of the jobs
(`StoreContents.in_order`), which the window and the command line read alike: the groups
in their order, each holding its jobs in its order, then the jobs in no group in the
file's order. A group moves among the groups, and a job within its group or among the jobs
in no group, one place at a time; groups stay above the jobs in no group.

### The configuration store

One file, `Config/mirrorhold.json` (format `mirrorhold-store`, schema version 7),
holding the jobs, their groups and the preferences. Every waiting run, verification and
repair reads it twice a second, and every admitted one every five seconds, while a window or a
command may save it, so it is read and
written through the exchange (*Files the processes share*, above). A save is atomic
(written to a `.saving` file, flushed, renamed over the old one) and first writes the
file it replaces, as the save read it, into `Config/previous/` (the newest five are kept,
the older removed once the save is written; one another program holds is left for a later
save to remove) — unless the save
only rearranges: it changes the GUI's remembered state, which the window saves as it
closes, or the order of the jobs and groups, which it saves at each move, and nothing
else; their copies would otherwise push out the copies holding a change. Saves are serialised by a
lock beside the file (`mirrorhold.json.lock`), and a save is given what the file held
when its caller read it: when the file holds anything else — another window or command
saved in between — the save is refused and nothing written. The window and the command
line save that way; a restore of a previous copy saves over whatever is there. A file
another program keeps open for the whole patience is reported as busy, never as damaged.
A save's rename leaves the file's name empty for a moment, which a read under load can meet
(measured on the development machine), so a load or a look that finds no file while the
save lock's file is there reads again under that lock, which every save holds across its
rename: a job file another process is saving is never read as a store with no jobs.
A file that was read whole and cannot be parsed, or
that breaks a rule spanning groups and jobs, is moved to
`Config/mirrorhold.corrupt-<UTC time>.json`, reported, and never silently replaced —
under the save lock and only while it still holds what could not be parsed, so of the
waiting activities that read one such file at once one moves it aside and the others
report it moved aside, and a file a save replaced in between is read instead;
a file from a newer schema is left untouched and never overwritten. Older schemas pass
through numbered migrations on load (schema 1 to 2: no groups; 2 to 3: every job's
share hold at half, as every job then held; 3 to 4: the choices for activities, each on;
4 to 5: one at a time on a drive, on, read as automatic turns by the drive's kind, and
off as always at the same time; 5 to 6: no drive marked; 6 to 7: no rclone configuration
named, every destination a drive's and written as it was; 7 to 8: nothing changes, every
schedule then written being a daily or weekly one, which keeps its text; 8 to 9: nothing
changes, every drive then marked being marked a hard disk or a solid-state drive);
the file is rewritten by the next save. Schema 7 writes a drive's destination as schema 6
did and adds only the preferences' remotes; its number moved so that a copy that knows only schema 6 leaves a file naming a remote destination
untouched, as a newer schema, rather than move it aside as one it cannot parse. Schema 8
lets a schedule run monthly, at several times a day or every N hours; its number moved
for the same reason, so that a copy that knows only schema 7 leaves a file holding such a
schedule untouched. Schema 9 lets a drive be marked a cloud drive, and its number moved so
that a copy that knows only schema 8 leaves a file holding such a mark untouched rather
than move it aside as one it cannot parse. Export and import carry a job list and the groups
of its jobs (format `mirrorhold-jobs`, migrated alike, and written in the store's schema,
whose number it shares though it carries no preferences): a job the import adds joins the group
here with its group's identifier, else its group's name, else its group, added; a job
already here keeps its own. The job file, a previous copy and an export are read as UTF-8
with or without a byte-order mark, which Windows PowerShell 5.1 writes wherever it is told
UTF-8; the store writes none.

### Creation-time validation

`validate_job` reports findings by stable code — refused, warned or noticed — and is
documented in full in `mirrorhold/jobs/validation.py`. Besides the plain overlap,
sync-root, pool-folder and FAT32 rules, it refuses a destination that is another job's
source (or a source that is another job's destination), any destination on a DrivePool
member disk, and any destination on a volume no disk partition carries — cloud drives
among them. It refuses a source that is a file rather than a folder or a drive
(`source-not-a-folder`), looked at while the source's drive is present and then given
none of the findings a survey of the source feeds, and a destination
that is itself one of the folders Windows and this tool need (`destination-is-system-folder`;
*What a first run does with what a folder holds*, below): leaving what such a folder holds
would still have every run put the source's folders into it (the maintainer's decisions of
2026-10-17). It refuses a destination that is a file too (`destination-not-a-folder`: `this
is a file, not a folder; a backup is written into a folder or onto a whole drive`), looked
at while the destination's drive is present, its other findings still made (the
maintainer's decision of 2026-10-19). These three alone look at this computer — its disk,
and the folders its environment names — and the job editor, an import in the window, the
command line's `add`, `validate` and `import`, and every preview and run make them alike
(`ValidationContext.on_this_computer`). Another job's source or a cloud sync root strictly inside a destination is
allowed and noticed (`destination-contains-other-source`, `source-inside-other-destination`,
`destination-contains-sync-root`): what protects it is how the destination's first run
treated what it held, which is recorded on the destination and not in the job, so the
run protects it (*What a first run does with what a folder holds*, below). Checks that
need a volume run only while it is present; the two that need the source's size (FAT32
file size, free space) run only with a survey, which lists the source without opening a
file. The run re-checks both on its plan.

A destination inside a folder a cloud client synchronises is judged by what the cloud does
with that folder (`endpoints.cloud`). Two records name such folders: Windows' Cloud Files
registrations, where OneDrive and every other client built on that interface registers
its folders, and Google Drive for desktop's own list, which registers none there — the
`roots` table of `root_preference_sqlite.db` in its data folder, read through a read-only
connection. A folder kept on every computer the account reaches — every registered one,
and My Drive in mirror mode — cannot hold a destination (`destination-in-sync-root`): each
run's writes would spread to all of them. A folder of this computer that the client keeps
a copy of in its cloud only — Drive's other folders — may, since that copy can be how a
user keeps a backup in the cloud, and is noticed (`destination-in-cloud-copy`): the client
may put back what a run moves into versions, and the next preview then names what
reappeared, which its run moves into versions again (*Confirmation*, below; the
maintainer's decision of 2026-10-28: Google Drive for desktop was measured putting back
folders a run had just moved into versions). A list that cannot be read — Drive's data
folder present, its list missing, locked or of another shape — is noticed at each present
destination (`sync-roots-unreadable`), never taken for an empty one; with no data folder,
Drive is not used and nothing is said. These two notices, and a remote's that does not
encrypt (below), are the ones a preview repeats beside their destination, in the window and
in `preview` (`validation.PREVIEW_NOTICES`), since its removals are confirmed there; no run
record carries them. A command reads the synchronised folders once; the window reads them
afresh for each preview and import, and an editor once as it opens, so a folder a client
took up while the window was open is seen.

A remote destination is judged from what the engine says of it (`mirrorhold/remotes/`), asked
by the command line's `add`, `validate` and `import` and by every preview through one engine
opened on the configuration; validation itself never starts the engine. It is refused when the preferences
name no configuration (`remote-configuration-missing`), when the configuration cannot be
opened or unlocked, the engine cannot be used or the service refused what it was asked
(`remote-configuration-unusable`, with the engine's reason), when the configuration holds no
remote of its name (`remote-unknown`), when a file stands where its folder is named
(`destination-not-a-folder`), and when the remote is below the **capability floor**
(`remote-below-floor`): it must keep modification times no coarser than two seconds — FAT's,
the coarsest a drive keeps, which the plan already compares within — and move a file within
itself, by a move or by a copy and a delete, since every removal or replacement is a move
into versions (on the engine's own servers every backend measured meets it, *The engine*). A remote whose service could not be reached at all
— the engine's reason a network failure: no route, no host, a connection refused or timed
out — is noticed and waits, as a drive not attached does (`remote-unreachable`); so is one
not asked, as an import in the window is not, and the run checks again. A remote that does
not encrypt is noticed (`destination-not-encrypted`): `this remote does not encrypt: the
service, and anyone signed in to its account, can read every file and its name, and the job's
definition kept with the backup, which names the source folder's path on this computer`
(the maintainer's decision of 2026-10-28: encryption is the user's choice, said plainly).
Free space is judged against a survey where the service reports it, and not otherwise. A
remote's folder is never another's source or destination by containment with a folder of
this computer: the two kinds never contain one another, and a remote that wraps another — an
encrypting remote, an alias — is compared by its own name alone, since what it lies over is
in the configuration, which is never read.

### The run

A run executes in a core process of its own, whoever started it. It publishes a live
status at once and waits for its turn (*Runs at the same time*, below: it works beside
every activity it does not conflict with; a waiting run shows what it waits
for, the drive they share when that is why, and its place in line, and can be cancelled), records any earlier run whose process died as interrupted,
removes any volume snapshot such a run left, works with the snapshot of the store and
the volumes present at the moment it was admitted and re-validates the job, decides how
the source is read — from a volume snapshot made for this run, or as it is (*Live
sources*, below) — and lists the source once. A run of some destinations — the command
line's `--dest` by number or `--marker` by marker, or *Run now* on destinations selected
in the window, which names them by marker — finds them in the job as it stood when the run
began and keeps them by marker: a job reordered while the run waits changes nothing of
which it serves, and one of them removed meanwhile, or a marker the job does not hold as
the run begins, refuses the run, which leaves no record of its own (*The log's marks*,
below). Then, destination by
destination in the job's order:

1. **Resolve and verify.** An absent destination is recorded `absent` and waits; one
   that two volumes carry, or whose marker names another job, is `stopped` untouched.
   No marker yet means this pair's first run. A shared destination's record is read with
   its marker: one that cannot be read now — another program holds it, or a remote that
   answers will not serve it — stops the pair untouched (`shared-record-unreadable`) and is
   read again by the next run, while a remote that stops answering as it is read fails the
   pair as a remote that stopped answering (`remote-failed`, below); one that is
   lost — missing, not a record this copy can read, or another job's or destination's —
   makes the pair a first run again (*What a first run does with what a folder holds*,
   below). A run that would move or write into a folder no run may touch stops the pair
   untouched too (the same section). When the
   job's version cleanup is on and the marker matches, the destination's versions
   batches older than the job's days are deleted now (*Maintenance*, below), before the
   destination is listed, so the space they free counts in step 3.
2. **List and plan** (`planning/`). The destination is listed once — a shared one's
   job's part alone; the plan sorts every file into new, changed, deleted, renamed
   (same size and modification time — times
   compared within the coarser side's precision, two seconds on FAT and ten
   milliseconds on exFAT, as for a file unchanged — the name breaking a tie; and names
   differing in case alone), kept (beneath a source path that could not be read: an
   unlistable folder, a cloud-only file, a link, an unreadable entry), blocked (a
   destination path through a link), left out (a path a run cannot carry under its own
   name: one holding `‛`, one of fullwidth full stops alone or holding an unpaired
   surrogate, which the engine cannot carry, and one ending in a space or a period,
   which this tool's own Windows paths would shorten — recorded with why and counted as
   not backed up, its destination copy left as it is) and the engine's leftovers.
   Exclusions apply to both sides. Changed files' leading bytes are read on both sides
   to find files that lost a known file-format signature, the reads counted in the
   status.
3. **Judge** (`run/gate.py`). A file of 4 GiB or more for a FAT destination, or bytes to
   copy that do not fit the free space with a margin (1 GiB plus 64 KiB a file), stop the
   pair. The free space is the drive's, measured as the gate judges — so what the cleanup
   freed and what other runs have written are in it — less what other runs and repairs in
   progress still have to write on that drive, with the same margin; the judgement is made
   under the admission lock, so no two of them judge one drive at the same instant. A first run
   waits whole for confirmation (*Confirmation*, below); one onto a destination that
   already holds something says so in its record (`destination-holds-content`).
   Removals — files deleted at the source or overwritten — are held when they number
   more than the larger of the job's count floor and percentage of the destination's
   files, or when they reach the job's share of them (half unless the user changed it;
   a job may switch this hold off), or when a changed file lost its signature, whatever
   the job's values. Held removals let the run copy its new files and nothing else.
4. **Apply** (`run/apply.py`). In order: the engine's leftovers are removed (only on a
   destination the tool already owned); renames are made on the destination;
   destination-only folders move whole, and destination-only files one by one, into the
   run's versions batch, `.mirrorhold/versions/<UTC start, YYYY-MM-DDTHH-MM-SSZ>/` with
   the original hierarchy (a destination-only folder holding no file at all is removed
   instead, and one holding what a shared destination leaves, or a file a run cannot
   carry under its own name, never moves whole: its
   other files move one by one); each changed file is opened the engine's way (phase
   *checking changed files*, counted); the engine copies the new and changed files from an explicit list,
   moving every file it replaces into the same batch; folders that exist only at the
   source are created. A changed file that cannot be opened the engine's way is left out
   and keeps its previous copy; one that still fails in the engine has its copy moved
   back out of the batch. A changed file of the same size whose contents the engine
   finds identical only has its time set: nothing is copied or moved into the batch, and
   it counts as done. When the engine has gone through its whole list, a listed file it
   reported neither copied, nor identical, nor failed is recorded as skipped.
5. **Record** (`records/record.py`, format `mirrorhold-run-record`, schema 6):
   completed, held, cancelled, interrupted, failed, stopped or absent, with what was
   planned, done (the files and bytes found identical among it) and left, the versions
   batch, the files skipped and why, the files among them a later run could still back
   up with the runs in a row each has gone unbacked-up, how the source was read, what the
   version cleanup deleted — and whether the service of a remote destination may keep it
   until it removes it itself —, the free space and capacity of the destination's drive
   when the pair ended (a remote's as its service reports both), whether a remote the pair
   reached encrypts, and the engine version — in the store's records and, once the
   destination is verified, in its management folder too, beside the job's
   self-describing definition (`.mirrorhold/job.json`) and, for a shared destination,
   its record (`.mirrorhold/shared.json`). An older record reads with the
   later fields absent (schema 2 added the source's reading and the unbacked-up files,
   schema 3 the cleanup and the free space, schema 4 the files found identical, schema 5
   what a remote's service may keep of the cleanup, schema 6 whether a remote encrypts, an
   older record saying nothing of either). Records
   read back oldest first by start time, runs that started in the same second in the
   order their records were written. The copy's throughput, which the next preview's
   estimate uses, counts the bytes found identical with the bytes copied.

**Confirmation.** `preview` runs steps 1–3 without changing anything and without waiting
for a turn — the cleanup is measured, not made, and listed with what it would delete, and
the gate counts what it would free on the free space measured as the preview began, other
runs not counted — adds the duration estimate
(a range: this pair's earlier copy throughput, else a stated default of 30–150 MiB/s;
listing time apart) and writes a ticket naming each pair's removals and, for a first run
into a folder that already holds what the source does not, the choice it confirms (below;
ticket schema 2). `run --confirm <ticket>` re-plans by that choice and lets the gate grant
exactly the removals the ticket names; any other is judged afresh — and a first run whose
plan holds one the ticket does not name waits whole, nothing written, so a destination
that gained something since the preview is never claimed unseen. A first run onto a
destination that holds nothing — no file, no folder, no link and no entry that cannot be read, beside the
management folder, a drive root's artefacts and what the job's exclusions hide — or whose
folder does not exist yet needs no preview: `run --first-backup` (the GUI's question
before a first backup) confirms it, since nothing there can be moved or replaced. That
confirmation covers the destination as it was: one that holds something when the run
lists it waits as without it, and is adopted only through a preview's ticket. An
unattended run confirms nothing, and a run takes a ticket or the first-backup
confirmation, never both.

**What reappeared after a move into versions** (`run/put_back.py`, the maintainer's
decision of 2026-10-28). At a destination whose marker matches and that is past its first
run, the preview names apart the files deleted at the source that equal, in relative path,
size and modification time, a copy in the destination's newest versions batch: files a run
moved into versions that have since reappeared, as when a cloud client keeping a copy of
the folder writes them back. The times are compared exactly — a move into versions is a
rename on the destination's own volume, which keeps the time, and Google Drive was
measured putting files back with their times to the 100 ns. The batch is found and entered
as the version cleanup finds one (*Maintenance*, below), and only the newest is read, only
its folders on the way to a file deleted at the source: one partial listing per destination
per preview — measured on the development machine, a batch of some twenty thousand files
listed whole in under a second. A file put back from an older batch, a later run or repair having made one since,
reads as an ordinary removal, and so does one in a folder of the batch that cannot be
listed. Recognition changes the preview's words alone: the gate counts these files toward
the holds, the ticket names them, and the run moves them into versions again like any
other removal.

**What a first run does with what a folder holds** (the maintainer's decisions of
2026-10-10 and 2026-10-11). A first run into a folder that already holds what the source
does not is previewed both ways: *leave it where it is* — the default — or *move it into
versions*, after which the folder is the job's whole. Where the folder holds nothing the
source does not, there is nothing to choose and the first run adopts it whole.
`preview --move` plans moving; the GUI's preview offers both answers and has the core
write the ticket for the one given (`run.runner.ticket_for`). Left, the destination is
**shared** (`endpoints.shared`, `planning.sharing`):

- the job owns the top-level names it puts there — the source's, run by run; a name
  neither the source nor the destination holds any longer is forgotten;
- everything at the top level outside those names is not the job's, whenever it
  appeared: never listed, planned, moved or compared;
- beneath a name the source shares with what was already there, what the source does
  not hold is left — the top-most such entries, a folder whole — and everything else is
  mirrored as on any destination, what the user puts there later included;
- a source file at a path left content occupies is backed up there, what it replaces
  moving into versions (the preview counts it); a source file where the destination
  holds a folder takes that folder's place whole;
- the record (`.mirrorhold/shared.json`, format `mirrorhold-shared`, schema 1: the
  names owned and what is left) is written before anything is applied, by the first run
  and whenever it changes, and the marker takes schema 2.

A later run lists only the job's part, which is what makes a destination at a system
drive's root affordable. **Some folders no run touches**: a synchronised folder or another
job's source strictly inside the destination, and the folders Windows and this tool need —
the Windows folder, the program folders and their shared data, the profiles folder and
each profile, the folder this tool runs from and the folder of its job file
(`endpoints.system`, read from the environment). Such a folder makes leaving the only
answer — moving is shown, not offered, with why — and a destination holding one is shared
even when its first run found it empty. A run stops at it, nothing touched, with the
folder's code as its reason (`destination-contains-sync-root`,
`destination-contains-other-source`, `destination-contains-system-folder`), when the
destination is its job's whole, when its first run was confirmed to move, or when the
folder lies beneath a top-level name the job owns or the source holds: backing the source
up there would write into it. Where the folder lies inside the destination, the stop says
that excluding it from the job leaves it where it is. A folder the job's exclusions hide —
the folder itself, or a folder holding it — stops none of these runs, a destination that is
its job's whole included (the maintainer's decision of 2026-10-21): neither listing reaches
it, and no folder holding it moves into versions whole — what else such a folder holds that
the source does not moves one by one, and the folder stays. A run stops at a hidden one,
with its code, only where the source holds a file where the excluded entry or a folder
holding it lies: copying that file would move the folder into versions. Leaving stays a
first run's only answer all the same, and a repair leaves a copy inside any of these
folders as it is (*Maintenance*, below). A destination that is itself one of the folders Windows and
this tool need is refused when its job is made (*Creation-time validation*, above); the
run of a job saved before that check stops on the same code (`destination-is-system-folder`),
nothing touched.

**A shared destination whose record was lost** (the maintainer's decision of 2026-10-17).
A record that is missing, is not a record this copy can read, or names another job or
destination no longer says what is the job's there, so the destination's run is a first
run again, judged afresh against what the folder holds: it leaves what the source does not
hold where it is — moving it into versions is shown and not offered, the folder's first run
having left it — and waits, untouched, for the user to confirm it in a preview, its record
giving `shared-record-lost` after its other reasons. Confirmed, the run moves what remains
of the lost record into its versions batch and writes the record anew from what it finds.
What the job had backed up there whose source copies are gone since is then left as well,
as anything a shared destination leaves is: from that run on it is the user's. A record
another program holds, or one a remote that answers will not serve, is intact and only
cannot be read now: that run stops untouched, and the next reads it again.

**A remote destination** (`run/runner.py`, the maintainer's decisions of 2026-10-28 and
2026-10-29). A run reaches a remote's folder through one engine session it opens once it is
admitted, and a preview through one it opens as it starts (`remotes/reach.py`), each kept
until it ends, so an encrypted configuration is unlocked once; the job is validated with
what the engine said of each selected remote folder. One whose service cannot be reached is
recorded `absent` (`remote-unreachable`: `cloud:Backups/Work cannot be reached: <the
engine's reason>; the next run tries again`) and waits, as a drive not attached does. One the
validation refuses — no configuration named, one that cannot be opened or unlocked, a
service that refused, no remote of its name, a file where its folder is named, a remote
below the capability floor (*Creation-time validation*) — is `stopped` untouched, its
reasons the refusal's codes and its detail the refusal's own words. Reached, the pair goes
through the steps above as onto a drive, everything it writes — the marker, a shared
destination's record, the job's definition, the records' copies and the versions batch —
in the remote folder's management folder, and the source read from a snapshot only when a
drive or a reached remote is there to copy to. On a remote:

- **Names the plan cannot tell apart** — two in one folder differing by case alone, or
  one name held twice, which some services allow — stop the pair untouched before it is
  planned (`destination-names-ambiguous`): `the remote holds names this tool cannot tell
  apart: Photos\a.jpg and Photos\A.jpg; rename or remove one of them on the service`, with
  `(and 2 other such pairs)` where there are more. The engine's leftovers from a copy
  stopped part-way are removed as on a drive (measured on a local remote and on SFTP).
- **Free space** is the service's, as it reports it — with or without a total — judged
  with the drive's margin; what the version cleanup would free is counted only where the
  service does not keep what is deleted (below). A service that reports none (S3, WebDAV as
  the engine serves it, a local remote's folder not made yet) is not judged: a copy that
  does not fit there fails file by file, recorded, and the preview says `free space: the
  service reports none, so it is not checked; a copy that does not fit fails file by
  file`. No FAT limit applies, and no other run's share is counted or published: every
  activity that reaches a remote takes its turn (*Runs at the same time*, below). A service
  that cannot be asked for its space now is never taken for one that reports none: the
  pair fails.
- **A file the service will not keep** fails alone, as a file the engine fails on a drive
  does, its previous copy moved back out of the batch (measured: an encrypted remote's name
  grown past what the folder beneath it keeps).
- **A remote that stops answering** fails the pair (`remote-failed`). Where it stops as the
  pair is assessed — its marker or a shared destination's record read, its changed files'
  leading bytes read for their signatures, its space asked — before any of the plan is
  applied, the pair says so: `the remote stopped answering before any of the plan was
  applied: <reason>; the next run tries again`. Once the run writes there — its marker or
  record or the job's definition written, the plan applied — it says `the remote stopped
  answering while the run worked: <reason>; what was done is recorded and the next run
  converges`. A marker, a record or a changed file whose content the engine breaks off as
  it serves it is the remote not answering, and one that otherwise cannot be read there is
  looked at again: a remote that no longer answers for it fails the pair as the remote, and
  only one the service will not serve while it answers is one that cannot be read — a
  marker stops the pair as unreadable, a record stops it as one that cannot be read now,
  and a changed file is left out of the signature check.
- **The version cleanup** deletes each expired batch whole through the engine, after the
  same fences; a remote that stops answering as the marker is read again leaves every batch
  (`the remote stopped answering: <reason>; nothing was deleted and the next run tries
  again`), and one that stops answering while the batches are read leaves them all, said as
  a problem (`the versions batches cannot be read now: <reason>; the next run tries
  again`). A service that reports a trash it empties (the engine's `CleanUp`; measured: S3
  reports one, for uploads never finished, and a local remote none) may keep what is
  deleted, counted against its space, until it removes it itself, so the preview adds to its
  line `; the service may keep deleted files (a trash or older versions) and count them
  against its space until it removes them` and the record's line `; the service may keep
  them until it removes them`.

Each backend's times are kept within its precision, so the next run plans nothing again
(measured on all five of *The engine*'s backends, below), and a move into versions keeps
them, so what reappeared after one is recognised there too (measured on a local remote).
A preview takes no turn (*Runs at
the same time*, below), and so may overlap a run that reaches a remote: what that can cost
is a sign-in to make again, never a file.

**Control.** `pause` suspends the engine's process (or holds the core between two of its
own steps — after every folder of either listing, before every changed file's signature
read and before every changed file is opened ahead of the copy included); `resume`
continues; `cancel` lets the files in flight finish and starts no other, ending the
engine once none is in flight, since its comparisons would otherwise run on; `cancel
--now` stops the copies in flight at once, their temporary files removed. A cancel before
the copy records the pair, and the pairs not started, as cancelled. Callers write what
they want into the run's control file; the run reconciles. A pause lasts within the
session: a shutdown while paused is an interruption, and the next run converges. A paused
run keeps the folders it claimed: the runs that conflict with it wait, every other works.
A verification, full or of new copies, is watched and controlled through the same files
(*Maintenance*, below).

**An engine that makes no progress** (the maintainer's decision of 2026-10-21). An engine
that for 30 minutes while not paused copies no byte, checks no file, finds nothing
identical, meets no error and whose process reads and writes nothing — or whose remote
control does not answer — is ended, and its pair fails (`engine-failed`) with the detail
"the engine made no progress for 30 minutes (no byte copied, no file checked) and was
ended; the next run tries again". The run goes on to its next pair, gives its turn up when
it ends, and the next scheduled or arrival run tries the pair again. The engine's figures
are read every ten seconds; time the computer spent asleep is not counted. Under a cancel
already asked for the engine is ended the same way and the pair is recorded cancelled,
the reason among its errors. A repair's copy is bounded alike. Its process's reads count
because comparing one large file reads both copies while no other figure moves.

**Progress.** While the engine works, the status counts as done the files it copied and
the files it found identical, with their bytes; its rate is those bytes over the time it
worked unpaused, and the time left the larger of the bytes left over that rate and the
files left over the file rate, against what the engine was handed — published after
three seconds of work. The steps before the copy publish how far they are: entries
listed, changed files read or opened.

**Runs at the same time** (`records/admission.py`; the maintainer's decisions of 2026-10-09
and 2026-10-13). Runs, verifications and repairs work at the same time unless they
conflict; a job's destinations are still copied one after another within its run. Each
claims, before it starts, the folders it reads and writes and the drives they lie on: a run
reads its source and writes each selected destination present when it is admitted; a
verification reads its source and its destination and writes nothing; a repair
(*Maintenance*, below) claims as a run of its one destination does. Two conflict when a folder one
writes and a folder the other reads or writes lie on one volume identity with one
containing the other, compared case-insensitively — the case validation leaves is a
destination inside another job's source; two runs of one job always conflict, as do two
verifications of one destination; an activity whose claim cannot be read, as a run of an
older copy of the tool publishes none, conflicts with every other, while one of this copy
that has published its first status and not yet made its first attempt at its turn claims
nothing and holds no one back; and two conflict when one uses — to read or to write — a
drive the other uses and the one judging takes turns on that drive (*Turns on a drive*,
below). A remote's folder in a claim lies on no drive and conflicts by remote and folder
like any other, and any two activities that both reach a remote take turns whatever the
choice for drives: every remote is reached through the one configuration, which the engine
rewrites when it refreshes a service's sign-in, and two engines refreshing one at once could
lose a fresh sign-in where the service lets each be used only once (not measured: it needs a
real service); one waiting for that alone names `the rclone configuration` where a shared
drive would stand. An older copy reads a claim naming a remote's folder as one it cannot read,
and so waits for it. A run claims each selected remote destination, reached or not, and a
verification or a repair its one remote destination. A preview claims nothing
and keeps its engine session for its whole length, so it may overlap a run that reaches a
remote: what that can cost is a sign-in to make again, never a file, while a preview made
to wait behind a run of hours would cost more each time.

**Turns on a drive** (the maintainer's decision of 2026-10-21, which replaced the one of
2026-10-13 that took every drive one at a time unless switched off). What two activities on
a hard disk contend for is its heads, which a seek moves between them, so there they take
turns; a solid-state drive has no seek to share, and there they work at the same time,
sharing its rate, as they do on a cloud drive, which has no heads at all and whose
activities share a connection rather than a disk (the maintainer's decision of 2026-11-01).
Whether two activities that use one drive take turns is therefore decided
per drive, by what the drive is, never by the job or by one value for every drive. A drive
is the physical disk Windows names a volume as lying on — two letters on one disk are one
drive — known by the identity the disk carries, its GPT disk GUID or MBR signature, and by
its number only when its layout cannot be read, since Windows hands numbers out as disks
attach (measured on the development machine: a USB disk's number changed from one day to
the next). A volume no single disk carries, such as a cloud drive, is a drive of its own; a
pooled drive is the one disk its pool software presents, apart from the disks beneath it
(measured with StableBit DrivePool). Its **kind**
(`endpoints/volumes.py`, `DriveKind`) is what its disk answers when asked, with no access
requested, whether it incurs a seek penalty: spinning when it does, solid when it does not,
and not known when it does not answer. A volume Windows names no disk for is a cloud drive
when its device answers as a disk file system rather than a disk, and otherwise — a volume
spanning disks, or one whose device cannot be asked — not known, so a question that fails
never removes turns. Measured on the development machine (2026-10-21): an internal NVMe
drive answers none, so it is solid; a USB disk behind an enclosure or a dock may answer the
question as a function it does not have, as Windows' own storage view then shows it of no
known media type, so it is not known; StableBit DrivePool's pool disk answers that it
incurs one, so it is spinning; and (2026-11-01) Google Drive for desktop's
streamed drive lies on no disk Windows names and its device answers as a disk file system,
so it is a cloud drive. A claim names the
drives of its folders present at the attempt, each with its kind and the words a person
knows it by — `drive I:`, `the drive of C: and D:`, `drive 'Backup'` by its label when no
volume on it has a letter, `a drive with no letter`. *Runs on one drive* in the preferences
holds the choice (`Activities.drive_turns`): **Automatic**, at first, takes turns on a drive
that is spinning or not known and none on a solid one or a cloud drive
(`records/admission.py`, `SHARED_KINDS`) — a drive that does not say what it is
is taken for a hard disk, which leaves turns on every USB drive that does not answer; **Always one
at a time** takes turns on every drive; **Always at the same time** on none. The choice is
kept in the job file, read by every run, verification and repair at each attempt at its
turn, whoever started it, and published in its claim with the drives' kinds. Both are the
waiting activity's own: a newcomer judges by the choice and the kinds it read, an activity
admitted under another keeps what it holds, and a claim that names no drives — an older
copy's — is compared by its folders alone. A claim that names drives without their kind
or the choice — also an older copy's — takes turns on all of its drives or on none, as its
one choice of on or off says; and every claim still publishes that one choice, on when it
takes turns on any of its drives, for an older copy to read. An older copy reads a cloud
drive's kind in a claim as a kind not known, and so takes turns there.

**What the user says a drive is** (the maintainer's decision of 2026-10-21). A drive that
does not say what it is is taken for a hard disk, so the user may mark any drive a hard disk,
a solid-state drive or, since the maintainer's decision of 2026-11-01, a cloud drive
(`Activities.drive_kinds`, kept in the job file beside the choice, at most one mark a
drive) — a cloud client's drive whose device answers otherwise than Google Drive for
desktop's is not known until marked — and the mark takes the place of what its disk answers wherever
*Automatic* decides by the drive's kind; *Always one at a time* and *Always at the same
time* ignore it as they ignore the kind. A mark names its drive as a claim does, by the
identity the disk carries or the anchor of a volume no single disk carries, so it follows
the drive whatever its letter or port and is lost when the disk is partitioned anew; a disk
known only by its number cannot be marked, since that number may name another disk the next
day. A mark also keeps the words the drive was known by when the preferences were last saved
with it attached, shown while it is not. The mark and the disk's answer meet in one place (`records/admission.py`,
`_drives`), which every run, verification and repair reaches through one claim builder
(`activity_claim`) from the job file it reads at each attempt, so a claim publishes the kind
it judged by. Measured on the development machine (2026-10-22, read-only): Windows read a
GPT or MBR identity from every drive attached, internal and USB alike, so each could be
marked.

Every activity publishes its claim in its status and tries for its turn under the
admission lock (`admission.lock` beside the store, held for the moment a decision takes,
the decision published before it is released): it starts when no admitted activity
conflicts with it and no waiting one it conflicts with goes first — a waiting run or repair
before any verification, otherwise the one queued first — so a newcomer never overtakes an older
waiting activity it conflicts with. Everything is claimed at once and held until the
activity ends, a pause included, so none waits for ever: a run holds every drive it claimed
until its last destination is done. A waiting activity names in its status the first of
those that hold it back and whether that one is paused or waits too, the drive they share
when sharing it is the only reason, and its place in line — one more than the waiting
activities it conflicts with that go first. One that holds it back and whose status it
cannot read now it names by the last status its attempts read of it, its kind and job, or
by its identifier alone where they read none, saying that its status cannot be read now;
that last status names it and nothing more, its claim and its turn never taken from it.
The Runs tab, the job list and `status` say so (*The GUI*, *Command line*).

**The computer while work goes on** (`power.py`; the maintainer's decisions of 2026-10-13
and 2026-10-15). While a run, a verification or a repair works — from the moment it is
admitted to its end, but not while it waits for its turn nor while the user has paused it — its
process keeps the computer from falling asleep by itself, unless the user chose otherwise
(`Activities.keep_awake`, *While a run, verification or repair works* in the preferences). It holds
a power request of its own (`PowerCreateRequest`, then `PowerSetRequest` for the system)
carrying its reason, `Backing up "<job>"`, `Verifying the backup of "<job>"` or `Replacing
backup copies of "<job>"`, released
when the activity ends; a process that dies takes its request with it. Windows lists the
request under SYSTEM in `powercfg /requests`, with its reason, and holds the system
required while it lasts (measured on the development machine, from a windowless process
as the core's are). Only the computer's own idle sleep is held off: sleep chosen from the
Start menu, the display's timer and every power setting are left as they are. The choice
is read as the activity is admitted and again every five seconds until it ends, so a
change saved in the window reaches an activity already working within seconds; a job file
that cannot be read then leaves the last choice standing. A request Windows refuses is
noted once in the activity's diagnostic log, and the activity goes on without it.

The other choice for the computer, on at first, has Windows' shutdown screen name the
activity at work and offer to cancel the shutdown (`Activities.name_at_shutdown`, *Show it
on Windows' shutdown screen* in the preferences; built by the maintainer's decision of
2026-10-21). It holds while the activity works, as the first does, is read with it and
follows a change in the same seconds, and a refusal of it is noted once and leaves the
other choice standing. The activity's process opens a hidden top-level window titled
`MirrorHold`, on a thread of its own that does nothing else, and while the choice holds
registers the activity's reason on it (`ShutdownBlockReasonCreate`) and answers no when
Windows asks whether the session may end (`WM_QUERYENDSESSION`); otherwise it answers yes,
and an installer's Restart Manager asking the program to close is always answered yes. The
window is real because a process with none takes no part in that screen: a message-only
window is not asked, and a console's handler is not called once the window library is
loaded. At the activity's end the reason and the window go. A shutdown the user lets go
ends the process, and the activity reads as interrupted and continues at its next run.
Measured on the development machine: a windowless process registers the reason and reads it
back. Not measured: whether the screen lists such a window, and what it does when the work
ends while it shows. In Windows Sandbox, a remote
session, a shutdown first asked whether to shut the remote computer down and then ended the
session about ten seconds later with no shutdown screen. `MIRRORHOLD_SHUTDOWN_SCREEN=off`
makes a process, and every activity it starts, take no part in the screen.

**The notification-area icon** (`tray.py`; the maintainer's decisions of 2026-10-31). While
a run, a verification or a repair works — from its start to its end, waiting its turn and
paused included — an icon in Windows' notification area shows it (*The GUI*). The icon
belongs to the window's unit and lives in a short-lived process of its own, one per store,
which shows every activity of that store at once; nothing of it starts at sign-in. Every
activity, however it was started — the window, the command line, a scheduled task — brings
it up once its status is published (`tray.offer`), unless a process already holds the
store's icon lock (`tray.lock` beside the store): it starts `pythonw -m
mirrorhold_gui.tray_view --store <store>` in the copy's folder with the copy's windowless
interpreter (`invocation.py`), the store's path made absolute, without a console, in a
process group of its own and outside the caller's job object where that job allows it, as a
run is started, so the activity that started it can end or be killed without taking the
icon with it. Two activities starting together may each start one; the second to begin
finds the lock held and ends at once. The icon's process ends a few seconds after the last
activity of its store has ended, judged by the liveness locks Windows releases when a
process ends, so a killed activity has ended too. Ending, it first lets the lock go and then
looks once more: an activity whose status was published before that look keeps the
process, and one published after it finds the lock free and starts a process of its own, so
no activity is left without an icon. The lock lies beside the store rather than among the
statuses, where every copy, an older one included, reads each lock as an activity's. The
icon is a courtesy, never a condition: a process that cannot be started is noted in the
activity's diagnostic log and the activity goes on, and whatever the icon's process fails at
costs the icon alone. Measured on the development machine, with a store in a temporary
folder: one icon process for two activities, Windows holding its icon, a pause and a resume
asked through the same requests as the Runs tab reaching the activity, the process
outliving the activity that started it and gone 4.5 to 4.8 s after the last one ended or was
killed, and every one of its windows answering yes when Windows asks whether the session
may end, so it never holds up a shutdown and takes no part in the shutdown screen. Started
inside a job object that ends its processes when it closes, it ended with the job where the
job did not allow leaving it and survived and then ended by itself where it did; whether a
scheduled task's job allows it is not measured. `MIRRORHOLD_TRAY=off` makes a process, and
every activity it starts, bring up no icon.

**Reminders** (`reminders.py`; the maintainer's decision, 2026-11-03, which keeps the icon
past the work for this alone). A notice that asks something of the user (`Notice.asks`) —
a run held for review, failed, stopped or interrupted, or keeping files from being backed up;
an automatic run that could not start; a full verification that found differences or did
not finish; a repair that failed to replace or left copies, or did not finish — is shown as
a reminder toast, which Windows keeps on the screen until the user closes it, and the
process that raised it leaves the job's reminder (`reminders/<job id>.json` beside the
store: the notice's title and lines, one per job, the job's latest replacing the one
before), then brings the icon up as an activity does. A notice the user's choices keep
quiet leaves none, and one Windows refused leaves it all the same. The icon's process reads
the reminders with each of its looks and stays while any stands: its tile amber, its tooltip
counting them (`MirrorHold - 1 at work, 2 to look at`) and giving each one's title, its menu
listing them under the activities with *Dismiss reminder* (*Dismiss reminders* for several),
which clears them. The main window clears every reminder of its store as it opens and,
while it has the focus, with each look at the runs; the icon then goes as it goes after the
work. A look whose statuses cannot be read still shows the standing reminders, and the
look the process takes once it has let the lock go reads the reminders before the
statuses, so a reminder written before that look keeps the icon whatever the statuses say,
and one written after it, the lock being free, starts a process of its own. A reminder is
a courtesy too: one that cannot be written is noted in the diagnostic log, one that cannot
be read is shown as `an activity needs your attention`, and a
reminder that cannot be cleared stays until the next clearing. Nothing starts at sign-in,
so a reminder standing at a restart shows again with the store's next activity.

**Starting a run from another program.** `run.launch.start_run` is the one way a
caller starts a run: `python -m mirrorhold --store <store> run <job> [--marker <id> ...]
[--confirm <ticket> | --first-backup]` as a process of its own, without a console window, in a process group of its
own and outside the caller's job object where that job allows it, so closing the caller
ends no run. It names the destinations it is given by marker, never by number: the run
finds them in the job as it reads the job file itself, so a save that reorders the job's
destinations while the window starts it changes nothing of which it serves. The window
reads the job file again before it starts a run of the destinations the user chose; a run
of chosen destinations that file, or its job, no longer holds is not started. The process's error output goes to `launches/<id>.log` beside the store,
because a run that cannot start at all (a stale ticket, an unusable store) says why
only there; the caller reads it once the process has ended without running, then
removes it. `run.launch.start_verification` starts a verification, full or of new copies,
the same way (`python -m mirrorhold --store <store> verify <job> --marker <id> [--new-copies]
[--restart]`), and
`run.launch.start_repair` a repair (`python -m mirrorhold --store <store> repair <job> --marker
<id>`), each of the destination selected in the window, which the process finds by its
marker in the job as it reads the job file itself.

**Every end has a reason.** `run`, `verify`, `repair` and `automatic` each keep a diagnostic log
beside the store (`records/diagnostics.py`; `logs/<UTC start>-<command>-<process>.log`):
the command line first, then everything the process says, an unattended run's words
included, each line timed, and every failure the code does not foresee with its whole
traceback. A log stops growing at 4 MiB, saying so, and each new log first removes the
oldest beyond 200 logs or 32 MiB together. A run that fails outside a pair in a way the
code does not foresee records each selected destination it had not recorded yet as
failed (`unexpected-error`, the exception's text as its detail), as a pair that fails so
always was. A command that fails so ends with one line saying so and naming the log
that holds the traceback — its own, or for a command that keeps none, one written for
the failure (exit status 1).

**The log's marks.** Beside its words, each such log carries marks for the window: lines
`@start`, `@job`, `@activity` and `@end`, each followed by JSON — the command, the job and
the destinations it was given by number and what started it; the job it resolved, with the
numbers there of the destinations it was given by marker; each run,
verification or repair it began, by identifier; its exit status and, when it could not do its
work, why. One module writes and reads them (`records/diagnostics.py`), and the window
reads them back (`LogWatch`: each log once, then only what was appended), so that an
activity which left no record of its own — refused before it began, failed before it
recorded anything, or its process gone without a word — is shown in whichever window is
open then. A log with no end mark that no process has open belongs to a process that
ended without a word: the writer keeps its log open for its whole life, and Windows
refuses to open a file unshared while any process has it open (`exchange.in_use`); on
another system that is never told. A log written before marks existed (no `@start` in
its first two lines) is passed over by the window and only ever saved whole.

## Live sources

A source that changes while it is read — a mailbox, a database, a document a program
keeps open, a folder a sync client writes into — is read from a **volume snapshot** when
one can be made: a frozen, read-only copy of the whole volume at one instant, in which
no file is held and none changes (`snapshots/`).

- **The decision, once per run** (`snapshots/reading.py`), before the source is listed.
  The source is read as it is, and the run's records say why, when: the job's snapshot
  reading is off; no selected destination is present (nothing will be copied); the
  volume cannot have a snapshot (not an NTFS or ReFS partition); the volume already
  holds snapshots this tool did not make — Windows' restore points, earlier versions —
  and the room Windows keeps for snapshots on it has too little to spare, or cannot be
  read; or making one fails (no administrator rights, the volume refuses, any other
  failure). Beside others' snapshots: once that room fills, Windows deletes the oldest
  snapshots to make space, and the user's would go first, so a snapshot is made there only
  with room to spare (the maintainer's decisions of 2026-10-06 and 2026-10-17) — at least
  4 GiB, and at least a tenth of the room's limit where that is more, or of the capacity of
  the volume holding the room when it has no limit (`reading.room_to_spare`). What is spare
  is the limit less what the room's snapshots use, never more than that volume's free
  space. It is a margin, not a guarantee: what the run's own snapshot grows by is known
  only afterwards. A snapshot is never a precondition of a run. The preview always reads as it is, and says how the run will: as it is and why,
  where the job's choice or the drive's kind settles it (`settled_without_asking`),
  otherwise from a snapshot when the run can make one as it starts; nothing while the
  source's drive, or every destination's, is away. It asks Windows nothing about
  snapshots, so the last two causes above are the run's to find.
- **The snapshot** is made through Windows' Volume Shadow Copy Service by WMI
  (`Win32_ShadowCopy`, the client-accessible context; the room for snapshots from
  `Win32_ShadowStorage`; `snapshots/windows.py`). A process
  without the administrators group enabled is told so before the service is asked. The
  run lists, judges and copies from the source folder inside the snapshot, a
  `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy<n>\...` path, which the listing, the
  engine-style open and the engine read like any folder. The snapshot lasts the whole
  run, every destination and a pause included, and is removed when the run ends,
  whatever its outcome (live status: *taking a snapshot*, *removing the snapshot*).
- **The snapshots this tool made** are recorded beside the store
  (`snapshots/<run id>.json`): written before the snapshot is asked for, and again once it
  exists with its identifier. A run that died leaves its snapshot on the volume; the next
  run to start removes it by that identifier, and never a snapshot it has no record of,
  nor one of a run still in progress. A run that died while its snapshot was being made
  leaves a request with no identifier, which the next run reports and acts on no further.
  Runs work at the same time, so looking at a volume's snapshots, asking for one and
  recording it, and every removal, hold the snapshot lock (`snapshot.lock` beside the
  store): Windows makes one snapshot set at a time, and another run's snapshot not yet
  recorded would otherwise read as one made by another program.
- **Files not backed up.** Every skipped file a later run could still back up — one that
  could not be read, listed or copied, a rename or folder that could not be made; never a
  cloud-only file or a link, which are left out by design — is recorded with the number
  of runs in a row it has gone unbacked-up. Only runs that reached their copies (completed,
  or with removals held while additions were copied) count; an absent drive, a stop or a
  cancel neither extends a streak nor breaks it. A file left unbacked-up by two runs in a
  row turns a completed destination's status to *Completed - N files not backed up* and its
  job's to *Files not backed up*, and the run's notice says so under the failure choice.

## Maintenance

Versions accumulate on every destination, and a run judges change by size and
modification time alone, so a copy damaged on its drive keeps both and no run copies it
again. `maintenance/` keeps both in hand.

- **Version cleanup** (`maintenance/cleanup.py`), when the job keeps N days rather than
  every version: at each run that reaches a destination whose marker matches, before the
  destination is listed (live status: *removing old versions*), every versions batch
  whose name dates it — in UTC, when the pair that filled it began to be applied or the
  repair its copies, `YYYY-MM-DDTHH-MM-SSZ[-n]` — more than N
  days ago is deleted whole. It is the only permanent deletion the tool performs, and it
  is fenced: the marker is read again first; the management folder and its `versions`
  folder must be real folders, not links; an entry not named as a batch, or a batch that
  is itself a link, is left alone and reported; inside a batch a link or junction is
  removed as a link and what it points at is not touched; read-only files and folders
  are made writable first. A pause holds it between two batches, a cancel leaves the
  rest to the next run, and a batch that cannot be wholly deleted keeps its remainder and
  is reported. The run's record lists what was deleted; the preview lists what would be.
- **Full verification** (`maintenance/verifier.py`), a manual action: *Verify...* in the
  GUI, `verify` on the command line. It runs as a core process of its own, publishes its
  status through the run's channel (kind `verification`) and waits for its turn like a
  run (*Runs at the same time*, above): reading both sides and writing neither, it works
  beside every run that does not write what it reads and beside the verifications of
  other destinations — those that share with it no drive it takes turns on. It gives way —
  paused or not — to a waiting run or repair that conflicts with it, as that one judges its
  own turn — one that writes what it reads, or one that uses a drive it uses and takes turns
  on that drive — and that verifications alone hold back, and
  continues once that one has had its turn, listing both sides again. Both sides are listed with the job's exclusions — a
  shared destination as its record says its job has backed it up: beneath the names it
  owns, less what it leaves, which is neither extra nor compared. A name the source gained
  since the last run, and a left entry the source holds something at now, are not the
  job's until a run claims them: the source's files there count as missing, not backed up
  yet, and what the destination holds there — perhaps the user's own — is not compared.
  One whose record cannot be used is not verified. On a drive (a remote's below) each
  file the same size and time on both sides is read in full on both at once (4 MiB
  reads, one thread a side, the engine's way of opening a file) and its SHA-256 digests
  compared; a file that changed while it was read, or differs in size or time, is counted
  as changed and not compared, as are files on one side only and files that cannot be
  read. Its expected time, published as the comparing begins, counts the files as well as
  the bytes: the bytes over the rates this destination's earlier passes measured, else the
  stated default of 30–150 MiB/s, plus 2.5 ms to 15 ms a file — 2.5 ms the program's own
  cost, measured on the development machine with the files cached; 15 ms assumed, a USB 3
  hard disk's seek — so a destination of many small files is not promised a large file's
  pace. The rate a pass measures is its bytes over its comparing time less 2.5 ms for each
  file it compared (the maintainer's decision, 2026-10-19); a preview's estimate counts no
  files. The pass makes its entries one at a time, in key order, from the two listings,
  keeping no second copy of them. Pause holds the readers between two reads; a stop,
  gentle or not, ends the sitting and keeps every file handled — at once, or, when a read
  does not return (a drive that hangs), after 10 s, the read given up and asked to cancel,
  and the record saying which file was being read; that file is compared again next time.
  Its record (`maintenance/verification.py`) is saved every few seconds with a cursor —
  the last entry handled in key order — so a stopped, failed or dead pass is continued by
  the next verification of that destination and that kind unless the user starts again; a
  save another program's hold refuses is made at the next one, never ending the pass; a
  finished one is copied into the destination's management folder
  (`.mirrorhold/verifications/`). A read
  that fails is followed by a look at both sides' folders: one gone — its drive removed or
  reset — is looked for again for 60 s; back, the file is read again and the pass goes on;
  still gone, the sitting ends as failed with that reason, the file counted nowhere, so
  the pass is continued later rather than finished with every remaining file unreadable.
  A verification that cannot check the drives says why in words. A verification reports
  what differs and repairs nothing: it cannot tell which copy is damaged. Replacing the
  backup copies with the source's is the user's own act, a repair (below).
- **Verification of new copies** (`maintenance/verified.py`; the maintainer's decisions of
  2026-10-19), the routine check after backups: *New copies only* in the Verify dialog and
  in the question before several destinations, `verify --new-copies` on the command line.
  On a large library a full pass takes hours; this one reads only the copies no earlier
  pass found the same as they are now — those a run wrote since, above all — and counts
  the others as verified earlier and unchanged, not read. It is admitted, gives way, takes
  turns on a drive and is controlled exactly as a full pass, and two verifications of
  one destination never run at once, whatever their kinds. What is new is decided by a
  list, not by a time. Every pass of either kind keeps a list of verified copies: a 64-bit
  fingerprint (BLAKE2b) of each file it found the same, over its key, its size and its
  modification time on each side, appended at every save of its progress before the
  record's cursor passes it (`verified/<record name>.partial`, in the record's folder), and
  once the pass finished sorted, each fingerprint once, into `verified/<record name>.list`. A
  pass of new copies builds on the latest finished pass whose list can be read — its
  baseline, fixed as it begins and kept by a pass continued while that list can still be
  read; a finished pass whose list was never sorted is read from its partial list. A file
  whose fingerprint is in the baseline was found the same and has not changed since by any
  measure a run uses: a run that copies it again changes its size or time, a rename its
  key. It goes into the new pass's own list with the files the pass read and found the
  same, so the latest list holds every copy verified and unchanged since; a copy found to
  differ is never in a list, and a later pass reads it again. No time on the destination
  serves instead (measured on the development machine, 2026-10-19, with the pinned engine
  and a run's own copy options): a copy the engine replaced within about 15 s of moving the
  old one into versions kept the old copy's creation time — Windows' file-name tunnelling —
  while one replaced more slowly got a new one; a copy keeps its source's modification
  time, so a file imported with an old date reads as old; NTFS's change time moved in
  every case, but exFAT and FAT keep none and no listing carries it. Where no list is kept
  — a destination not verified since lists were kept, or whose lists are lost — a pass of
  new copies is a full pass and counts as one: the command line says so as it begins and
  the question before several destinations in advance, and both continue the destination's
  unfinished full pass unless a restart is asked for; the Verify dialog says so under *New
  copies only*, which then starts a full verification from the beginning. A list another
  program holds — an antivirus scanning a file just written — counts as kept and is waited
  for up to 30 s before the pass reads every file instead. Once a finished pass's list is
  written, the destination's other lists are removed, but for the partial list of a pass
  still to be continued or at work. Each kind continues its own unfinished pass: a pass of
  new copies leaves an unfinished full pass and its partial list to be continued, and a
  full pass that finished after a pass of new copies began ends that one, the next being
  begun anew. A pass of new copies never dates the destination's verification (*The
  reminder*, below); *Copies differ* and a repair take the latest finished pass of either
  kind, never one in progress, stopped, interrupted or failed, and the estimate the
  throughputs of both. Schema 2 of the verification record
  added the pass's kind (`mode`: `full` or `new-copies`), its baseline and the files
  left unchanged (`unchanged_files`, `unchanged_bytes`); a schema 1 record reads as a full
  pass. Schema 3 adds how the pass compared its files (below).
- **A remote destination's verification** (`maintenance/compare.py`; the maintainer's
  decisions of 2026-10-28), of either kind, compares each copy without downloading it
  wherever the service allows, so the pass reads the source alone. It is admitted, gives
  way, continues and is controlled as a drive's, and takes the turn every activity reaching
  a remote takes (*Runs at the same time*, above); it opens one engine session on the
  configuration the preferences name once its turn comes, closed when it gives way or ends.
  Where the service keeps a fingerprint of each file (S3's MD5, for one), the listing asks
  for that one kind — a local remote computes every kind it is asked for by reading the file
  — and the source's of the same kind is computed as the source is read: MD5, SHA-1, SHA-256
  or SHA-512 in this process, preferred in that order, read in 4 MiB chunks so pause, stop
  and a read that hangs behave as on a drive; any other kind by the engine from the source
  file, which a stop ends but a pause does not hold, its progress told only as each file
  ends. A copy the service lists with no fingerprint is read on both sides and counted. An
  encrypted remote keeps no fingerprint of what it holds: there the engine's own check
  (`cryptcheck`) encrypts each source file with the nonce at the start of its copy and
  compares the result with the fingerprint the service beneath keeps, reading of the copy
  only that start. Whether the service beneath keeps one is asked as the pass begins, and
  where it keeps none — and on a remote that keeps no fingerprint at all — both copies of
  every file are read, the destination's by download. The check is handed the files in
  batches of at most 64 files, about 256 MiB or 4,096 entries; a pause suspends the
  engine's process, a stop or giving way ends it, and the cursor passes a batch only once it
  is checked, so a batch a stop cut short is checked again. A comparison by fingerprint
  proves that the service keeps, of each file, what it recorded as the source holds it, not
  that the bytes it stores are still those: damage the service did not record is not seen,
  and no verification of a remote reads its copies back in full where a fingerprint serves.
  A remote that stops answering is a side gone: looked for again for 60 s, then the sitting
  ends as failed, `the remote stopped answering: <reason>; the next verification continues
  from here`; one that cannot be reached at all is not verified, `cloud:Backups/Work cannot
  be reached: <reason>; verify again later`. It says how it compares as it begins —
  `verifying destination 1 of 'Work' (cloud:Backups/Work) by the md5 fingerprints its
  service keeps; nothing is downloaded`, `... by its encrypted form against the md5
  fingerprints the service beneath keeps; only each file's first bytes are read there`, or
  `...: its service keeps no fingerprint, so both copies of every file are read, the
  destination's by download` — and its line in `history` ends `; compared by md5
  fingerprint`, `; compared by the encrypted form (md5)` and, for copies read in full,
  `; 2 files read in full (5000 bytes): the service kept no fingerprint for them`. The
  estimate takes the rates of earlier passes compared the same way, since a pass by
  fingerprint reads the source drive alone; one read by download with none takes the rates
  the destination's runs copied onto it at (the maintainer's decision, 2026-10-29), the
  network setting both, else the stated default. A pass continued after how the copies are
  compared changed — the service, or the configuration — is ended, `how the destination's
  copies are compared changed since it began; a new pass began`, and a new one begins: its
  counts and its rate mean nothing mixed. Schema 3 of the verification record keeps the
  pass's `measure` (`read`, `fingerprint` or `encrypted`), the `fingerprint` kind compared,
  and the files and bytes read in full in a pass by fingerprint (`read_files`,
  `read_bytes`); a record of an earlier schema reads as read in full.
- **Repair** (`maintenance/repairer.py`, its records `maintenance/repair.py`; the
  maintainer's decisions of 2026-10-17), a manual action: *Replace backup copies...* in the
  GUI, `repair` on the command line. It is the user saying the source's copies are the good
  ones, and it never acts by itself. It takes up the copies the destination's latest
  finished pass listed as differing — the first 1,000 it found — less those an earlier
  repair of that pass replaced; it does not start while a verification of the destination
  is in progress, when the latest finished pass found no copy that differs, or when every
  copy it listed was replaced already. It reads that pass again once its turn comes, and a
  verification that gave way to it meanwhile, unfinished, is never what it takes up (the
  maintainer's decision, 2026-10-26). It runs as a core process of its own, publishes its status through the
  run's channel (kind `repair`), and waits for its turn as a run of its one destination
  does — its source read, its destination written — going before waiting verifications, a
  working one giving way to it (*Runs at the same time*, above). Both sides must resolve,
  the marker must match, and a shared destination's record must be usable; its record is
  then written before anything changes. It reads the source as a run would: from a volume
  snapshot made for it when the job's choice and the drive allow one, else as it is. Each
  copy is checked before it is touched and left as it is, with why, when the job excludes
  it now; on a shared destination it is not the job's; it lies in a folder no run moves or
  writes into — a synchronised folder, another job's source, a folder Windows or this tool
  needs (*Some folders no run touches*, above); either side's path goes through a
  link or junction; it is gone from either side; the source copy is a cloud-only file;
  either side changed size or time since the verification (the next run copies it); a copy
  cannot be read; or the backup copy begins with a known file-format signature the source
  copy lacks — the mark encryption leaves, when replacing would put a damaged original over
  a good backup. The copies' bytes, with the gate's margin, must fit the destination's free
  space less what other runs and repairs still have to write there, judged under the
  admission lock; the backup copies move on the same drive and free nothing. On a remote
  the free space is the service's, judged only where it reports one, and nothing else
  writes there meanwhile, since every activity reaching a remote waits for this one. Before the
  first of them moves, the record names a versions batch of the repair's own
  (`.mirrorhold/versions/<UTC start>/`, aged and cleaned up as any batch) and every copy
  whose backup copy is to move into it, and is saved. Then each backup copy moves into the
  batch, the engine copies the source's copy into its place, and a backup copy whose new
  copy the engine did not complete moves back out of the batch — so too when the copying
  ends by an error or an interruption, the copies the engine did complete then recorded as
  not read back and left pending for the next repair. A backup copy that cannot be moved
  back is recorded as in versions, with why, whatever ended the repair. A copy stays named
  as one being replaced until it is settled — replaced, recorded as not replaced, or its
  backup copy moved back — so that none lies in the batch unnamed when the job's cleanup
  deletes it, even when the process dies: a kill, a crash, a power cut, or a second Ctrl+C
  while the first one's clean-up moves the copies back (the maintainer's decision,
  2026-10-28). Each replaced file is then read back by the measure a verification of the
  destination compares by: on a drive both copies read in full and their SHA-256 digests
  compared; on a remote the fingerprint its service keeps for the new copy against the
  source's, or the engine's check of the encrypted form, a copy with neither read on both
  sides. One that still differs, or cannot be read back, is recorded so — the destination
  drive may be failing, or, on a remote, `still differs after it was replaced: the service
  did not keep it as it was sent`. A copy whose new copy a remote that stopped answering
  cannot say is in place stays named as one being replaced, pending as a dead repair's
  copies are. A repair onto a remote opens one engine session for its whole length, and one
  that cannot be reached is not started, `cloud:Backups/Work cannot be reached: <reason>;
  repair again later`. Pause holds it; a stop ends it before the copies or,
  while the engine copies, after the files in flight, the copies already replaced read back
  and kept, the rest left pending for the next repair. Its record (format
  `mirrorhold-repair`, schema 2, `repairs/<job id>/` beside the store, copied into the
  destination's `.mirrorhold/repairs/` when it ends) names the pass it took up, each copy
  replaced with its size, each copy it did not replace with why — a failure (not copied,
  its backup copy left in versions, or its new copy differing or not read back) or a copy
  left as it is — the copies it is replacing, its versions batch and how it ended —
  completed, stopped, failed, or interrupted when its process died without saying so. A
  repair that no longer runs and still names copies it was replacing died before it was
  done with them: each is a failure, its backup copy perhaps in the batch, and stays
  pending for the next repair. A schema 1 record reads with no copy being replaced; a copy
  of MirrorHold older than schema 2 passes over a record this version writes, and one older
  than the reason `in-versions` (the maintainer's decision of 2026-10-26) over a schema 1
  record naming one. The
  verification's record is never rewritten: a destination's `Copies differ`, read from its
  latest finished pass, ends once repairs have replaced every copy that pass found, which a
  pass that found more than it lists never allows until a verification starts again. While
  it works it keeps the computer awake as a run does (*The computer while work goes on*,
  above).
- **The reminder**: a destination is due a verification the job's reminder days after
  its last finished full one began — a pass continued over several sittings read its first
  files then — or after its first completed run when it was never verified in full; the GUI
  shows that date in its *Verified* column. A verification of new copies never counts
  there: it leaves unread the copies an earlier pass read (the maintainer's decision,
  2026-10-19). A run that backs a due destination up — its copy completed, or its removals
  held — says so in its notice, one line per such destination after the outcomes:
  `Destination 2: a full verification is due (last: 2026-07-01)`, the local date its last
  finished full pass began, or `(last: never)`; at every such run until the destination is
  verified in full, and only in a notice the run raises anyway, so a run the user's choices
  keep quiet stays quiet (the maintainer's decision, 2026-10-15). A remote destination is
  reminded as a drive's is.
- **Low free space**: a destination's drive with less than 10 % free is low. Each run
  measures it when a pair ends (the volume scanned again; a remote's as its service reports
  free and total, when it reports both) and keeps it in the record; the run's notice says
  so — `only 6 % free on its drive`, or `at its remote`; the GUI shows every attached
  destination's free space live, and a remote's as a run last recorded it.
- **Notices**: a finished verification that found differences, or one that failed,
  raises a notice under the failure choice; one that found none under the completion
  choice; a stopped one none. One differing file is counted in the singular — `Photos:
  verification found 1 file that differs`, its line `Destination 1: 1 file differs from the
  source although size and date match (for example photo.raw)` — and two or more in the
  plural (the maintainer's decision, 2026-10-19). A verification of new copies says so in
  its title — `Photos: verification of new copies found no differences`, `... found 2 files
  that differ`,
  `Photos: a verification of new copies did not finish` — and its line counts the copies
  it left unread: `Destination 1: 2 files compared (2.0 KiB); 1,234 verified earlier and
  unchanged, not read`. A repair follows the same rule: every copy it took up
  replaced, under the completion choice — `Photos, destination 1: 3 copies replaced`; any
  it failed to replace or left as it is, under the failure choice —
  `..., 1 failed, 2 left - see History`, `Photos, destination 1: 0 copies replaced, 5
  failed - see History`; one that did not finish,
  under the failure choice too — `...; the repair did not finish - see History`, with why
  on its second line; a stopped one none. Every one of these raised under the failure
  choice asks something of the user and leaves its reminder (*Reminders*, under *The run*).

## Automatic runs, installation and notifications

A job runs by itself only when the user switches that on, per job: **when any of its
drives is attached** (the computer starting with the drive attached counts), **on a
schedule** (daily, on chosen weekdays or on a day of every month; at one or more times,
or every few hours, all day or inside a window), or both; *pause* keeps the choices
and stops both. Nothing of the tool stays resident to watch for them: the triggers are
Windows Task Scheduler tasks, in the folder `\MirrorHold`, that only the core registers, and
the icon an automatic run shows in the notification area goes with the work, unless the run
left a reminder, which keeps it until the user has opened the window or dismissed it
(*The notification-area icon* and *Reminders*, under *The run*).

- **Reconciliation** (`automation/reconcile.py`). Every command that changes the jobs
  — the command line's and the GUI's — ends by bringing the folder into line with them:
  per job with automatic runs, `<job id>-arrival` and/or `<job id>-schedule`; anything
  else in the folder is removed. A task's description carries a fingerprint of its
  definition, and only a changed task is registered again, so a schedule's missed-run
  catch-up survives. Nothing is registered unless an installed copy serves this store,
  and whichever copy saves the jobs reconciles toward that one: a copy that is not the
  one installed never takes the tasks over by saving.
- **The tasks** run `pythonw -m mirrorhold --store <store> automatic <job> --by
  arrival|schedule` in the installed copy's folder, with its interpreter
  (`invocation.py` composes every command by which a copy starts itself), as the
  logged-on user with the user's own rights (no password stored), never a second
  instance at once, no time limit. A schedule's task has calendar triggers — by day, by
  week on its weekdays, or by month on its day (`Last` for the last day, the one spelling
  Task Scheduler accepts, measured) in all twelve months — one for each time of its list,
  or one repeating every N hours from the window's start to its last run (the duration a
  minute past it); a missed occurrence runs at the next opportunity. A task holds at most
  48 triggers (measured: 48 validate, 49 are refused), hence a list's 48 times. An
  `every Nh` schedule registered while one of its days is under way gets a one-off trigger
  for that day's remaining runs, since a calendar trigger's first run fixes its time of
  day. The daily and weekly forms written before monthly and repeating schedules existed
  keep the definition they had, so their tasks are not registered again. An arrival's task has an event trigger on a volume device started
  or removed (`Kernel-PnP/Configuration` 410 and 420, volume class) and a logon trigger.
  Measured on the development machine: those events fire when a drive is attached or removed, but
  not at an ordinary start of the computer; the logon trigger is what makes start-up an
  arrival.
- **What an arrival runs** (`automation/arrival.py`). The core keeps per job which of its
  drives were present at its last look, and in which session of the computer (its boot
  time), under `automation/` beside the store. A drive present now and not then has
  arrived; at the first look of a session, every present drive has. The source's
  arrival runs every present destination and every remote one; a destination's arrival
  runs that destination; nothing runs without the source. A remote destination never
  arrives — no device event marks a network store, and it is never counted present — so
  the source's arrival, a schedule or the user runs it, and a run that cannot reach it
  then records it as waiting. After each run it looks again, so a
  drive attached during a long run is served too. A scheduled run covers every
  destination, an absent one recorded as waiting.
- **Kinds of copy** (`locations.py`). The code runs as a *working copy* (a source
  checkout), as the *source installation's copy* (`installed/`, holding
  `installation.json`), or as a *portable release* (unpacked from a release zip, or
  installed by the release's setup program, holding `build.json`: its label `YEAR.MONTH.N`
  and the version of the interpreter it carries, written by the build and never by the
  program). A portable release's job file, records and every other runtime file sit in
  `%LOCALAPPDATA%\MirrorHold`; nothing is written into its own folder, which a newer
  release replaces. A `build.json` that cannot be read still marks a portable release,
  whose label is then said to be unknown.
- **Installation** (`installation/`). From a working copy, `install` copies
  `mirrorhold`, `mirrorhold_gui`, `LICENSE` and the pinned engine into `installed/`
  (git-ignored), writes the program's icon there with the working copy's
  `tools/make_icon.py`, which draws it with `mirrorhold/icon.py` (started as a process of
  its own, as the release build starts it;
  an icon that cannot be drawn is reported and the copy installed without it), writes
  `installed/installation.json`, writes the Start menu entry `MirrorHold` (opening the
  installed GUI on this store, carrying the application identity notifications need, and
  naming the copy's icon), and reconciles the tasks onto the new copy. A working copy
  without `LICENSE` is refused: every copy carries the licence. Updating is
  installing again; the new copy is built beside the old and renamed into place, so a
  copy an open window or a running task uses is refused, as is any install while a run
  is in progress, or from the installed copy itself. `uninstall` removes the tasks and
  their folder, the Start menu entry and `installed/`, and nothing else. Running from the
  installed copy, `default_store_path` is the store it serves.
- **Setting a portable release up.** In a portable release, `install` (*Set up* in
  *Preferences*) copies nothing of the program: it writes the setup record beside the
  store, naming the release's folder as it was reached and its label, copies the
  release's icon beside the store, then writes the Start menu entry and the tasks, both
  starting that folder with the interpreter it carries, the entry naming the icon kept
  beside the store. A copy is known by the path it was reached by, never by where a link
  on that path leads (`locations.as_reached`): a package manager keeps one folder for a
  program across its updates — a junction it points at each new version, as Scoop's
  `current`, or a folder it unpacks each version into, as winget's package folder — so a
  release set up under that name is served there by the next version with nothing set up
  again (measured with a junction made as Scoop makes one; no package manager has
  installed a release yet). A newer release unpacked anywhere else reads the same job
  file; setting it up re-points the entry and every task at it, and the older folder can
  be deleted. Opened while another copy is set up — the versioned folder behind such a
  junction included — a release asks once at start whether to use it instead. A set-up
  release that was moved or deleted is reported as gone: nothing is registered for it,
  the tasks already registered stay until a copy is set up again, and the entry keeps
  its icon. `uninstall` (*Remove*) removes the tasks, the entry, the record and the kept
  icon, and leaves the folder for the user to delete. `uninstall --if-set-up-here` removes
  them only where this release is the one set up, and otherwise changes nothing and refuses
  nothing — not a run in progress, nor a source installation — since nothing would change;
  a working copy refuses it (the maintainer's decision, 2026-11-01). It is what the setup
  program's uninstaller asks.
- **The setup program** (`tools/setup/MirrorHold.iss`; the maintainer's decision,
  2026-10-31). A release's setup program, built beside its zip from the same files
  (*Building a release*), is another way to install a portable release, not another kind
  of copy. It installs for the user alone, without administrator rights, always into
  `%LOCALAPPDATA%\Programs\MirrorHold`: the zip's files byte for byte, each dated midnight
  UTC on the first day of the label's month — the zip's files carry that day's midnight with
  no time zone, which Windows shows as local time, so the two read alike only where local time
  is UTC — and Inno Setup's uninstaller (`unins000.exe`, `unins000.dat`) beside them. It lists
  the copy among Windows' installed apps for this account alone — MirrorHold, by
  offcutworks, the release's label as its version, under one identity generated once, by
  which an update finds the installation it replaces — and makes no Start menu entry,
  shortcut or file association of its own: the program's own `install` writes the entry.
  Told to install anywhere else — Inno Setup takes a folder named by `/DIR=`, which
  `winget install --location` passes, or by a `Dir` in the file `/LOADINF=` names, though no
  folder page is shown — it refuses at its start, saying why, before anything changes, and
  ends with Inno Setup's status for a setup that did not initialize, 1: a copy elsewhere would
  lie outside the folder every later setup program updates, which would take itself for an
  update, find no copy there and set nothing up. A
  first install then runs `install` in the new copy; a refusal (a run in progress, the
  source installation on this account) leaves nothing — the setup program removes the new
  folder and its list entry itself, shows the program's words on its last page and exits
  with status 20 — since Inno Setup lets its code start no rollback of its own in a silent
  install (read in Inno Setup 7.1.1's source). What `install` writes to its error stream on
  success — a warning, a note on the job file — is shown on that page too. An
  update, which the list entry's presence marks, moves the folder aside
  (`MirrorHold.previous`), installs the newer files into a fresh folder and deletes the old
  one, or puts it back when the update does not finish, so no file of the older release
  stays; it runs no `install`, the setup record already naming the folder, so a copy that
  was not set up stays as it was. The uninstall the list starts asks first; then it asks the
  copy `installation`, which answers with status 0 whenever the copy runs, and `uninstall
  --if-set-up-here`, and deletes the folder, so another copy's setup outlives it. A copy
  that runs and refuses keeps everything; a copy that cannot run is removed all the same,
  with a message that its automatic runs, if it was the copy set up, are removed with
  *Remove* in the copy kept (the maintainer's decision, 2026-11-02). The jobs, their records
  and every backup stay.
- **A copy at work refuses the setup program.** Install over an existing folder, update and
  uninstall are each refused before anything changes while any process runs from the copy —
  the window, a run, a verification, a repair, the icon one of them shows. Each such process
  has loaded the copy's interpreter, and Windows refuses to open a loaded program file for
  writing without sharing: the setup program tries that on the interpreter's files,
  `MirrorHold.exe` and the engine (the uninstaller beside them left out, since it runs while
  it asks), waiting up to five seconds for a hold that passes, and looks again on the folder
  once it is moved aside, putting it back and refusing when a process started in between. A
  rename of the folder is no such test: measured on the development machine, a folder a
  process runs an interpreter from can still be renamed unless it is that process's working
  folder, while the interpreter's library cannot be opened so for as long as any interpreter
  of the copy runs. Inno Setup's own offer to close the applications that hold files is
  switched off: the program's work is never cut short by its setup.
- **The installation record** (`installation.json`, schema 2) holds its `kind` (`source`
  or `portable`), the version, the time, the store served, the windowless interpreter,
  the folder the tasks and the entry start, and either the working copy installed from or
  the release's label. A schema-1 record reads as a source installation. A source
  installation's record sits at its copy's root; a portable release's beside its store.
- **One installation per account.** The two kinds share the task folder, the Start menu
  entry and the notification identity. A working copy refuses to install while a portable
  release is set up on this account (a portable record in `%LOCALAPPDATA%\MirrorHold`
  whose folder still holds a release); a portable release refuses to be set up, or to
  remove a setup, while the Start menu entry starts the source installation's copy. An
  entry that starts nothing that exists is replaced by whichever copy is set up.
- **Notifications** (`notifications/`). The core process of every run — the command
  line's `run` (which *Run now* starts) and the scheduler's `automatic` — raises one
  Windows notification when the run ends, by the user's choices: completed, removals
  held (or a first run waiting — for the go-ahead, for a review of what a folder holds, or
  for the review that makes a shared destination's lost record again: `Destination 2: its
  record of what to leave was lost - review it in MirrorHold`), failed, or files not backed up for two runs in a row
  (under the failure choice), with a line for each destination it backed up whose full
  verification is due (*Maintenance*). The process hands it to Windows itself, through
  the Windows Runtime (`notifications/windows_runtime.py`, calling `combase` through
  `ctypes`) on a thread of its own, and starts no other program: a hidden shell given an
  encoded script is what behaviour-based antivirus watches for. It appears under
  MirrorHold's name once its Start menu entry exists (a copy installed, or a portable
  release set up), under Windows PowerShell's identity before. Windows takes a notice's
  icon from that entry, so it shows the icon the entry names: MirrorHold's (measured on
  the development machine). A notice Windows refuses fails nothing, and no run record
  names it: `run`, `verify` and `repair` say why — `note: the notification could not be
  shown: <Windows' reason>` — and keep the line in their diagnostic log with everything
  they say; an unattended `run` and the scheduler's `automatic`, which no one watches,
  write it to their log too — `automatic` whether its run ran or could not start (the
  maintainer's decision, 2026-10-19).
  A notice that asks something of the user — removals held or a first run waiting, a run
  that failed, stopped or was interrupted, files not backed up, an automatic run that could
  not start — is a reminder toast, which stays on the screen until the user closes it with
  its one button, Windows' own dismissal, and leaves the job's reminder, which keeps the
  notification-area icon until the user opens the window (*Reminders*, under *The run*; the
  maintainer's decision, 2026-11-03). The button is named *Dismiss*: left unnamed, Windows
  names it in the language of its own shell, which need not be the account's preferred
  language — one foreign word in a program written in English alone (the maintainer's
  decision, 2026-11-03). One that only tells, a completed run's, is
  the toast Windows shows for a few seconds and keeps in its notification centre.
  `MIRRORHOLD_NOTIFICATIONS=off` silences a process and the runs it starts (the suite
  sets it).
- **Records** name how a run was started: `manual`, `unattended`, `arrival`,
  `schedule`. An automatic start that fails before running leaves a notice, its reminder
  and `automation/<job id>.log` beside the store.

## The GUI

```
pythonw -m mirrorhold_gui [--store FILE]
pythonw main.py [--store FILE]
```

the first run from this directory, the second — the launcher `main.py` at the project
root — from any. Here and below, `python` and `pythonw` stand for the interpreters of
the environment that carries the tool's dependencies (`README.md` makes one in
`.venv\`). One window: the groups, each above its jobs, then the jobs in no
group; each job with its destinations, their drive (present, not attached, two drives
match, `remote`), the time of the last successful backup (headed *Last backup*), free space (`low` below 10 %), last full verification (`- due`
once the reminder has passed) and status in words (`First backup: confirm`, `First run:
review`, `First run: confirm`, `Removals held: review`, `Record lost: review`, `Completed`, `Completed - 2 files not backed up`, `Copies differ: see
verification`, `Verifying 42 %`, `Verification stopped at 42 %`, `Replacing copies`, `Waits for its drive`,
`Waits for its remote`,
`2nd in line, waiting for 'Work' (drive I:)`, `Cancelled - next run continues`, ...); below it, across a divider the user drags, the
runs, verifications and repairs in progress — whoever started them — and the history of the
selection, verifications and repairs among the runs. Its windows: the job editor, the
preview and confirmation, the cancel dialog, run details, a start's details, the Verify
dialog, verification details, the question before a repair, repair details, a
destination's files not backed up, the preferences, choosing a remote's folder and where
one is, import and export, the New group and Rename group windows, and the questions asked when
the job file cannot be used at start. Every window is the maintainer's decision. On
2026-10-10 the maintainer decided the groups and acting on several jobs at once, as the
bullet below describes, the question before a first backup, the first-run words and the
share hold in the editor; on 2026-10-11, the choice a first run into a folder holding
something offers, the grey of a drive not attached, the divider, the main window's
remembered place and the editor's opening height; on 2026-10-14, what the window tells of
every activity, the history of the selection and its saving, as the bullets below
describe, the choice of runs on one drive in the preferences, the words of a run or
verification waiting its turn, moving jobs and groups up and down, the question before
several jobs run under each choice for one drive, and windows that keep on their face only
the words a choice needs, what explains it being the hint of its control; on 2026-10-15,
the choice of keeping the computer awake in the preferences; on 2026-10-17, replacing the
backup copies a verification found to differ — the verification details' advice and button,
the right-click entry, the question before a repair, its row in the Runs tab and the
question before it stops, its words in the list, its History row and details window — and
the words of a shared destination whose record was lost — its row, its preview, its notice
— and of the two refusals a job meets when it is made; on 2026-10-19, verifying every
destination a job, a group or several items reach after one question, the words of
activities started together — one starting, the Runs tab's titles, one window for several
starts that failed, the waiting verifications in the status bar — the job list putting a
waiting row's place in line first, and the verification of new copies — the Verify
dialog's choice of the new copies only, a full verification or continuing, the question
before several destinations answered with either, and the *Verified* column counting full
verifications alone; on 2026-10-21, the words for an activity whose status cannot be read
now — its row in the Runs tab, and what the Runs tab, the job list and `status` say of it
where something waits for it — and for one that has asked for its turn, and the names of
the three answers of the choice of runs on one drive: *Automatic*, *Always one at a time*,
*Always at the same time*; on 2026-10-22, the words for such an activity of no known kind,
one never read — `a run, verification or repair` where something waits for it, its row's
title and the status bar's count — the closing question counting each one by the kind the
window last read, the job list showing it on its job and destinations as last read, the
words of what each drive is in the preferences and of the questions before several jobs
and verifications under *Automatic*, and drives that share a name being told apart by size
and file system. On 2026-10-31 the maintainer decided the icon in the notification area
while a run, verification or repair works — its picture, its tooltip, its menu for one
activity and for several, its left click and when it shows: it shows only while something
works, and the window's unit shows it, in a process of its own. On 2026-11-03 the
maintainer decided that it also stays, amber, while a reminder of a notice that asks
something of the user stands, until the window is opened or the reminders are dismissed
(*Reminders*, under *The run*).

- **A drive not attached.** Its items stay listed and are drawn grey (`#6D6D6D`, the grey
  Windows gives unavailable text, 5.2:1 on the list's white): a destination whose drive
  is not attached (`Waits for its drive`), a job whose source drive is not (`Waits for its
  source drive`) or whose every destination's drive is not (`Waits for its drive`), and a
  group whose most pressing job is grey. The words carry the state and the grey reinforces
  it; a row asking something of the user keeps its own colour while a drive is away — a
  run or verification in progress, a review, a problem such as two drives matching — its
  Drive column still reading `not attached`. The grey comes and goes at the refresh after
  the drive leaves or returns. Grey rows stay selectable, editable and removable.
- **A remote destination** (the maintainer's decision, 2026-10-30, with the instruction to
  keep its surfaces tidy). The window sets one up and follows it without
  reading the rclone configuration and without asking a remote at a look: the refresher
  passes over it, and what the list says comes from the run records. Its folder reads as
  rclone names it with `(remote)` after it, `(remote, not encrypted)` once a run that reached
  it found it does not encrypt; its short name is the folder's last name or the remote's, its
  Drive column `remote`, its free space the last a run recorded. A run applied there stands in
  for its marker: until one was, it reads `First run: confirm` and *Review and confirm...*
  opens the preview, unless its last run stopped or failed, which then shows, since a review
  cannot mend that. A last run that could not reach it reads `Waits for its remote`, drawn
  grey as a drive not attached is, and its job reads the same when every destination is a
  remote not reached; *Verify...* and *Replace backup copies...* stay enabled, since the
  activity itself finds out, and a stale record must not lock the user out.
  The window asks the engine only when the user acts, on a thread: adding a remote
  destination, opening a job that has one, browsing, *Check* in the preferences, a preview.
  Nothing on this computer opens a remote's folder, so *Open folder*, *Open versions* and
  *Open batch* show `This folder is on the service, not on this computer.` and its address as
  rclone writes it, to copy. The core's refusals that name a command read in the window's
  terms: `Name the rclone configuration in File > Preferences first.`, `No password is
  remembered: type it in File > Preferences.` A preview names it `Destination 1 -
  cloud:Backups/Work (remote)`, says `Could not be reached - the next run tries again
  (<reason>).` where it could not be reached, and `Nothing can run now: no destination's
  drive is attached or its remote reached.` when nothing can; it adds a *Free space* line, `the
  service reports none, so it is not checked`, where that is so, and `; the service may keep
  them until it removes them` to old versions deleted where it may; it shows the remote's
  notices — that it does not encrypt, above all — even where the pair cannot run, and estimates
  from `a stated default` before a first run. History reads `Not reached`, and a run's details
  `Its remote could not be reached; it waits. <Reason>.`, `Free after the run, as the service
  reports it: ...` and that same clause on old versions. An import in the window does not ask
  the engine, so it notices a remote destination as not asked, and the run checks again.
- **The divider and the window's place.** The job list and the tabs share the window
  across a divider drawn as a band about half a line tall with a row of dots; the pointer
  turns into an up-down arrow over it and dragging it gives the list more or less of the
  window. Growing the window grows both, the list by three parts to the tabs' two. As the
  window closes it keeps its size, position, whether it is maximised, and the divider (as
  the list's share) in the job file's remembered state beside the groups closed, unless the
  file cannot be saved (read-only, or changed elsewhere). It reopens there: a place that no
  longer fits is shrunk to the working area of the monitor it overlaps most (the screen
  less the taskbar) and moved wholly onto it, the main monitor taking the place of one no
  longer attached; with nothing usable remembered it opens at its content's size.

- **First backups.** A destination without a marker reads `First backup: confirm` when
  its folder held nothing (or did not exist) when the window last looked — the refresher
  reads its top level whenever it reads the marker (`planning.listing.occupancy`) —
  `First run: review` when it held something, and `First run: confirm` while that is not
  known. *Run now* on a job with such a first backup or on such a destination, and *Review
  and confirm...* on such a destination, ask one question first: where no backup exists
  yet (and that the backup makes the folder, when it does not exist), what is copied there,
  that nothing there is moved or replaced, which of the run's other destinations hold
  files and wait for a review, and which run as usual — a run of the destinations selected
  (*Groups and several jobs at once*, below), or of the one *Review and confirm...* is on,
  naming those alone. *Start first backup* starts that run with the first-backup
  confirmation; *Cancel* starts nothing. The run looks again: a folder that holds something
  by then waits, and the list then reads `First run: review`. *Review and confirm...* on a
  folder holding something opens the preview of that destination alone. Running several
  jobs names their first backups in the one question, and starting confirms them.

- **Groups and several jobs at once.** The list takes several rows (Ctrl and Shift). A
  group's row reads its most pressing job's status — `Phone: Running`, `Needs your
  review (2 jobs)`, `Phone: Running (+1)`, `Up to date` — and its oldest last success.
  *New group...* makes an empty group; jobs move into a group, out of it or into a new
  one from the right-click menu (*Move to group*). *Move up* and *Move down* in the same
  menu, or Alt+Up and Alt+Down in the list, move the selected group among the groups, or
  the selected job (or the job of a selected destination) within its group or among the
  jobs in no group, one place, keeping it selected; each is shown disabled at its level's
  edge and with several items selected. A move is saved at once, keeps no previous copy of
  the job file and leaves the scheduled tasks alone. With one group selected *Run now*
  runs its jobs, *Verify...* verifies its jobs' destinations, *Edit...* renames it and
  *Remove...* deletes it, its jobs staying in no group; with several items selected *Run
  now* runs every job they reach, *Verify...* verifies every destination they reach and
  *Remove...* removes them when every one is a job. *Run now* runs a job whole when its own
  row or its group's is selected, and otherwise the destinations selected of it alone (the
  maintainer's decision of 2026-11-04): a selection that reaches one job starts that one
  run without a question, unless it is one group's row alone. Running several asks once, naming the jobs that
  start — a job run for some destinations with their numbers, `Starts: Alpha (destination
  2), Beta, Gamma (destination 1).` —, those already running (not started again) and those needing review, whose run
  holds what waits for confirmation, and says what the choice for one drive means — `Jobs
  that use the same drive run one at a time unless it is a solid-state or cloud drive, as
  Windows reports or as set in Preferences; the others run at the same time.` under *Automatic*,
  `Jobs that use the same drive
  run one at a time; the others run at the same time.` under *Always one at a time*, or
  `They run at the same time; jobs that use the same drive share its speed.` under *Always
  at the same time*; they start together and are admitted like any run, so those on one
  drive they take turns on run one after another.
  *Verify...* on a job verifies each of its destinations; one destination selected, or a
  job that has only one, keeps the Verify dialog (below). Verifying several asks once,
  titled `Verify 4 destinations of "Photos"?` (`Verify 4 destinations?` when no one group
  or job is selected): the destinations verified (`Verifies: Alpha 1, Beta 1.`), what the
  new copies are and which destinations have no list of verified copies yet, read in full
  whichever the answer (`New copies only reads what was backed up or changed since it was
  last verified; Gamma 1 has no list of verified copies yet and is read in full.`), which
  unfinished pass each answer continues (`A full verification continues the unfinished one
  of Beta 1.`, `New copies only continues the unfinished ones of Gamma 1 and Delta 1.`) —
  starting one again from the beginning is the one destination's dialog's choice — and
  those left out with why (`Not verified: Delta 1 (its drive is not
  attached), Gamma 2 (its first run is not confirmed yet).`, or that it is being verified,
  that two drives match it or the source, that the source's drive is not attached, that
  the folder belongs to another job, that its marker cannot be read, that its drive has not
  been checked yet); for several starts, what the choice for one drive means, in the
  words the question before several jobs uses with `Verifications` for `Jobs`
  (`Verifications that use the same drive run one at a time unless it is a solid-state or
  cloud drive, as Windows reports or as set in Preferences; the others run at the same time.`
  under *Automatic*) and `A backup that is due runs first: the verifications step aside and
  continue.` *New copies only* or *Full verification* starts a verification of that kind of
  each, admitted like any — the full one the default answer when a destination reached is
  due one or has no list of verified copies —; *Cancel* starts nothing. A group's, a job's
  and several items' right-click menus offer *Verify...* too.
  *Preview...* and *Edit...* of a job need one job or destination — *Preview...* with a
  destination selected previews that destination alone, titled `Preview: Photos -> 2`, and
  its run buttons run it alone — and the destination
  buttons other than *Verify...* one destination; History shows whatever the selection
  reaches (below). Which groups are closed is remembered as the window closes.

- **Every activity's state.** A destination being verified reads how far the pass has
  compared, by bytes rounded down (by files when the pass has no bytes): `Verifying 42 %`,
  `Verifying 42 % (paused)`, and `Verifying` alone while it lists — `Verifying new copies
  42 %` for a verification of new copies; its job's row reads `Verifying`. Its pass waiting
  to be continued — its unfinished full pass, else its unfinished pass of new copies —
  reads so, with how far it got — `Verification stopped at 42 %` or `Verification
  interrupted at 42 %` (its process gone) in the attention tone, `Verification failed at
  42 %` in the problem tone, `Verification of new copies stopped at 42 %` and so on for one
  of new copies — wherever the row would otherwise read in the normal tone: a problem, a
  review, copies that differ and a drive not attached come first, and the pass stays
  continuable whatever the row shows. The right-click menu on one destination offers
  *Continue verification* while it has such a pass, taking that one up at once, of its own
  kind, without a question (enabled when the destination can be verified), then
  *Verify...*, then *Replace backup copies...* while its
  latest finished pass found copies that differ (enabled while a repair of them can start: both
  drives attached, or the source's for a remote, the marker matching, nothing of the job's at
  work on it and a copy left to replace), then *Move up* and *Move down* (its job's), then *Move to group*. A
  destination being repaired reads `Replacing copies`, or `Waiting to replace copies` while
  its repair waits its turn, and its job reads the same. A run started for
  some of a job's destinations publishes them, and the others keep their own status; a
  run of an older copy, naming none, serves every one. A live activity whose status cannot
  be read now stands where the window last read it, which says what it is and never how far
  it has come: its destinations — a run's every one it serves and has not finished — read
  `Run in progress - its status cannot be read now`, `Verification in progress - its status
  cannot be read now` or `Repair in progress - its status cannot be read now`, and its job
  reads the same, a verification's `Verifying`; one the window never read names no job, and
  the job list shows nothing of it. A job whose latest start of a run,
  verification or repair left no record — refused, failed before it recorded anything, or
  its process gone without a word, as its log's marks say (*The run*, *The log's marks*) —
  reads `Run could not start - see History`, `Verification could not start - see History`,
  `Repair could not start - see History` or `Ended without a record - see History` in the
  problem tone, until a run record ends or a verification or repair record is written after
  that log was last written; only work in progress and a review come before it.
- **Waiting its turn.** A job whose run waits its turn, and each destination that run
  serves, reads its place in line, whom it waits for and the drive they share when that
  alone is why — the place first, so that a Status column too narrow for the rest cuts it
  last: `2nd in line, waiting for 'Work' (drive I:)`, `Next in line, waiting for a
  verification of 'Work'`, `Waiting for a repair of 'Work'`; a destination whose
  verification waits reads `Verification next in line, waiting for 'Work' (drive I:)`. Two
  that reach a remote name what they share the same way: `2nd in line, waiting for 'Work' (the
  rclone configuration)`. One
  waited for whose status cannot be read now is named as the waiting one last read it,
  `Next in line, waiting for 'Work' (its status cannot be read now)`, or, where it read
  nothing of it, `Next in line, waiting for a run, verification or repair whose status
  cannot be read now`. A run or a verification that names neither and has not yet asked for its
  turn, or has just been admitted and is not yet at its first step, reads `Starting`, on
  its job's row too when it is a run. One that has asked and names neither — a
  verification giving way, until its next attempt, or one whose admission could not be
  published — reads `Waiting to run` or `Waiting to verify`, as does a status that names
  neither and says nothing of its turn, as an older copy publishes; a destination a working
  run has not reached yet reads `Waiting to run`. A run numbers its destinations as the job
  stood when it began and names the marker behind each number, so the list finds a
  destination's run by its marker: a job edited while a run works — a destination moved,
  removed or added — shows the run on the destination it is on wherever that destination
  now stands, those it finished with their own status, and one added since with none of
  the run's. A run of an older copy, naming no markers, is numbered as the job stands.
- **History** shows the records of what is selected: one job, a group's jobs, the jobs
  several selected rows reach, or every job when nothing is selected, newest first, with a
  *Job* column while it shows more than one job; each destination's record shows as soon
  as it lands. A repair is a row of its own, started `by you`: the copies replaced and
  their bytes under *Copied*, the backup copies moved into versions — those of the copies
  it is replacing, or was when its process died, among them — the copies not replaced —
  failed, left as they are, or not finished by a repair whose process died — under
  *Skipped*, and its result — `Copies
  replaced`, `Copies replaced, 1 left`, `Copies replaced, 1 failed`, `No copy replaced, 2
  failed`, `Copies replaced, 1 failed, 2 left`, `Stopped`, `Failed`, `Interrupted`, or
  `Replacing copies` while it works; a failure is never counted among the copies left. Each start that left no
  record is a row of its own, `Could not start` or `Ended without a record`, whose details
  window says why — `The verification could not start: <reason>`, `The repair could not
  start: <reason>`, or `Its process ended without a record: a shutdown or a crash.` — with
  *Open log*. A run's, a verification's and a repair's details open the log of the process
  that wrote them too (a pass continued over several sittings: the latest), while that log
  is kept.
  With nothing to show it says `No run is recorded yet.`, `No run of the jobs in "<group>"
  is recorded yet.`, `No run of "<job>" is recorded yet.` or `No run of the selected jobs
  is recorded yet.`
- **Saving the history and the log.** *File*, *Save history...* writes what History shows
  to a CSV file the user names (`MirrorHold history <date>.csv`): the columns as shown, the
  job first, and the first line of each row's details, in UTF-8 with a byte-order mark so
  that a spreadsheet program reads every name right — a document for a person, not a
  format promised to other programs (the maintainer's decision, 2026-10-14); it is
  disabled while History is empty. *Save log...* writes every diagnostic log kept beside
  the store into one text file (`MirrorHold log <date>.txt`), oldest first, each under
  `===== <name> =====`. A save that fails says `Not saved` with the system's reason.

- **It never applies a run.** *Run now* and a confirmed preview start a core process
  through `run.launch`, as *Verify...* starts a verification and *Replace backup copies...*
  a repair; pause, resume, cancel and stop are requests through `records.status.request`,
  written off the window's thread so that the window answers while another program holds
  the control file. One that did not land says so — `Not paused`, `Not resumed`, `Not
  cancelled` or `Not stopped`: `The request did not reach the run: another program kept its
  control file open. Try again.` (`the verification` or `the repair` for one), `The request
  did not reach the run: it had ended.` for one that ended as it was asked, or for any
  other failure the system's own reason. A run the window would not start — the job file
  could not be read at that moment, or no longer holds a destination chosen — says so in
  `Run not started`: `The run of "Photos" was not started: a destination chosen is no
  longer in the job, which changed since this window read it.`, and in a preview `The run
  could not start: ...`. Closing the window, with runs, verifications or
  repairs going, names how many of each are in progress — `1 run and 1 repair are in
  progress.` — says that they go on and leaves them going; one whose status cannot be read
  now counts as the kind the window last read it as, and one it never read as `1 run,
  verification or repair whose status cannot be read now`.
- **The preview runs in the window's process**, through `run.runner.preview_job` on a
  thread of its own: it only lists and reads, reports where it is, and is cancelled
  through its progress callback before any ticket is written. *Run and apply everything
  shown* starts the run with that preview's ticket; *Run, keep held removals waiting*
  starts it without one. It says how many changed files have the same size as their
  copy — compared first, only dated where identical — and lists the source files left
  out, names a run cannot keep among them (*Left out at the source*). What would move into
  versions reads by folder (*Into versions, by folder*), each with how many of its files and
  a few of their names, but a folder that moves into versions whole is one line under its own
  name — `whole folder deleted at the source`, at a first run `whole folder not in the
  source` — never one per subfolder beneath it (the maintainer's decision of 2026-10-27);
  *Show all...* lists every file under its folder, by its path within it. The files that
  reappeared after a move into versions (*Confirmation*) are never grouped with the other
  removals: they follow as *Into versions again, by folder*, under `These reappeared after
  being moved into versions on <date> at <time>.` in this computer's time — `...: Google
  Drive, which keeps a copy of this folder, may have put them back.` where the destination
  carries the cloud-copy notice — read by the same rule, a folder whose every file
  reappeared being `whole folder reappeared`, with a *Show all...* of their own; a folder
  moving whole only some of whose files reappeared is a line on each side under its name,
  neither called whole. The *Into versions* quantity counts them among the deletions, as in
  `4 deleted at the source (3 of them reappeared), 0 replaced by a newer version`. A first
  run into a
  folder that already holds what the source does not asks what happens to that: *Leave
  them where they are* (chosen) or *Move them into versions*, each answer saying what it
  means, and the quantities, the files by folder and a note on any top-level folder the
  source shares with what is there following the answer chosen; *Left as they are* counts
  and names what stays. A line under the source says how the run will read it — `The run
  reads the source from a snapshot of D: when it can make one as it starts.` or `The run
  reads the source as it is: <why>.` (*Live sources*). Beneath what a run would do at a
  destination, the notices to read before confirming follow in the attention tone, each a
  sentence: a cloud client that keeps a copy of the folder and may put back what a run
  moves into versions, or one whose list of folders could not be read (*Creation-time
  validation*). Where the core protects a folder inside, moving is shown disabled
  with why. A shared destination whose record was lost reads `Record lost: review` in the
  attention tone, and *Review and confirm...* opens its preview as such a first run: `THE
  RECORD OF WHAT THIS FOLDER KEEPS AS IT IS WAS LOST - its first run left what the folder
  held where it is.`, with how many files there are not in the source; leaving is its only
  answer, saying too that any files the job backed up there whose source copies are gone
  stay as they are from then on ("if any": the lost record cannot tell), and moving is
  shown disabled with why. Confirming takes the ticket for the answer given. A pair the
  run stopped to
  protect a folder reads `Stopped: a synchronised folder would be written into`,
  `Stopped: another job's source would be written into` or `Stopped: a folder Windows or
  MirrorHold needs would be written into`.
- **The Runs tab** lists the activities in line order, as `status` does: those at work or
  paused first, then the waiting ones by their place in line, those with no place yet —
  starting ones among them — last, each group in the order they joined the line (an older
  copy's by when it started). A row's title names its job,
  the destination and where it lies, a verification's and a repair's beginning with what
  they are — `Verification: Photos -> 1  F:\Backups`, `Verification (new copies): Photos -> 1
  F:\Backups`, `Repair: Photos -> 1` — and a run
  serving every one of its job's several destinations saying which it is on, `Photos -> 1
  of 2  F:\Backups`, and a run of some of them naming the destination alone, `Photos -> 3
  F:\Backups`, since its count is not the job's — the
  destination's number as its job stands now, found by the marker the status names, which
  the cancel question and the icon's line use too; a destination the job no longer has is
  named without a number, and the run's count follows only a number the run gave itself,
  since once the job was edited the two count different things. It
  shows a run's files and bytes done against its plan, files the engine
  found identical counted as done and named in a note, its rate and time left, the files
  in flight or, while it only compares, the files being compared, and the counts of the
  listings and of the steps over changed files before the copy; until the run has a plan
  — while it takes a snapshot, removes old versions, lists or plans — no files or bytes are
  counted against one. A waiting row names the
  job it waits for — `waiting for 'Work'`, with `(paused)` or `(waiting too)`, or `waiting
  for a verification of 'Work'`, and one whose status cannot be read now as the window
  last read it, `waiting for 'Work' (its status cannot be read now)`, or `waiting for a run,
  verification or repair whose status cannot be read now` where it read nothing of it; a
  verification that gave way reads `gave way, waiting for 'Work'` — an activity that names
  none and has not yet asked for its turn, or has just been admitted, reads `starting`, one
  that has asked and names none `waiting for its turn` (a verification giving way, `waiting:
  a backup runs first`), and a status that names none and says nothing of its turn, as an
  older copy's runs publish, reads as before.
  Its note names the drive they share when that alone is why it waits, and its place in
  line: `Both use drive I:. 2nd in line.`, `Both use the drive of C: and D:. Next in line.`,
  `Both use the rclone configuration. 2nd in line.`, or `Next in line.` alone when a folder or the job itself is why. A live activity whose
  status cannot be read now has a row among those at work, titled as the window last read
  it — `Run, verification or repair 1a2b3c4d5e6f` by its identifier where it read nothing of it —
  reading `its status cannot be read now`, with no bar, no counts and its buttons disabled:
  it holds its turn, so whatever asks for one waits for it. The status bar counts
  the verifications waiting among those in progress — `4 verifications (3 waiting)` — and
  counts an activity starting as in progress, not waiting, and one that has asked for its
  turn and names nothing as waiting. Runs, verifications and repairs
  the window started that ended without running are said in one window: `Run not started`
  with the reason for one, or for several `3 runs not started` (`1 run and 2 verifications
  not started`), a line per job — `Alpha: <reason>`, naming each one's kind when they are
  not all of one.
  A repair's row reads `replacing copies` with the copies copied of those it replaces and
  its bar over their bytes, `paused`, `stopping after the files in flight`, or what it waits
  for; it has *Pause* and *Stop...*, whose question — `Stop replacing copies? The copies
  already replaced stay replaced; the rest are left as they are.`, *Stop* or *Keep
  replacing* — asks it to stop after the files in flight.
  With nothing in progress the tab says so, naming verifications too, its words wrapped to
  the tab's width. A run's details count the files found identical as done and say how
  many there were.
- **The icon in the notification area** (`tray_model`, `tray_view`; started as *The
  notification-area icon*, under *The run*, says) shows every activity of the store while
  one is alive, whether or not the window is open, and says and offers only what the Runs
  tab says and offers: it reads the statuses on a run watcher of its own, as the window does,
  and takes each activity's words from the Runs tab's own rows. Its picture is the
  program's mark at the size Windows draws notification-area icons, the outlined block — the
  copy — filled from the bottom by the activities' progress, the mean of the fractions of
  those whose progress can be measured; it is empty while none can (waiting, starting,
  before a plan), and its tile turns the grey of a drive not attached while every activity at
  work is paused. Pointed at, it reads `MirrorHold`, or `MirrorHold - 3 at work` for several,
  then a line per activity in line order — `Backing up Photos -> 1: 42 %, about 12 min left`,
  `Verifying Photos -> 1: paused at 42 %`, `Backing up Music -> 1: waiting for 'Work'`, a
  repair's beginning `Replacing copies of`, one whose status cannot be read now as the Runs
  tab names it — within the 127 UTF-16 code units Windows shows: a line too long is
  shortened with `...` and the lines that no longer fit are counted, `and 2 more`. Its
  menu, built from the latest look as it opens so that it never changes under the pointer,
  holds *Open MirrorHold* in bold, which a left click also does, then for one activity a
  greyed line naming it and its progress with *Pause* or *Resume* and *Cancel...*,
  *Stop now...* or *Stop...*, and for several one entry per activity in line order, each
  opening those two; each is enabled where the row's button is and asks what it asks, with
  its question (`runs_view.RunRequests`), over a window that cannot be seen near the
  screen's centre, since this process has no other. While reminders stand (*Reminders*,
  under *The run*) the icon shows too with no activity alive: its tile amber, over the grey of
  a pause; its tooltip `MirrorHold - 2 to look at` (`MirrorHold - 1 at work, 2 to look at`
  beside activities, plain `MirrorHold` for one line alone) with a line per reminder, the
  title of the notice that left it, after the activities'; its menu, after a separator, a
  greyed line per reminder and *Dismiss reminder* (*Dismiss reminders* for several), which
  clears them as opening the window does. The main window clears them as it opens and, while
  it has the focus, with each look at the runs. *Open MirrorHold* brings forward the
  main window open on the store — every main window marks itself with its store, a property
  of its own on its frame, by which the icon finds it — or opens one, and opens none again
  for 15 s while that window is not yet found. The process asks Windows every 5 s whether it
  holds the icon, by a modification that changes nothing, and makes it again after two
  refusals running, since Tk adds an icon again after the notification area restarts only
  where its first add succeeded. Looks that fail leave the icon as it was and end the
  process once they have failed for 30 s running; a Tk without its tray command shows no
  icon. Like the window, the process keeps no log.
- **The editor shows the core's own validation** as the user types and saves only when
  nothing is refused. It offers what takes effect today: the name, the source and
  whether it is read from a snapshot of its drive when possible (disabled, the stored
  choice kept, for a drive that cannot have one, a line under it then saying `This drive
  cannot be snapshotted.`), the destinations, the two default exclusion categories, the
  user's patterns, the hold values (`Hold removals for review above <files> files and
  <percent> % of the destination's files`, and the share hold a switch with its
  percentage, the entry disabled and its value kept while off) and *When to run* — which
  says first that the job runs when started, offers the arrival and the schedule as
  additions (both off for a new job) — the schedule's days `Daily`, `Weekly` with a row of
  day boxes, or `Monthly` with its day (1 to 28 or `last day`), and its times either `At`
  one or more times separated by commas or `Every` 1 to 12 `hours`, optionally `between`
  two times; a control the choices made do not use is disabled, its value kept, never
  hidden — enables *Pause automatic runs* only while one is
  chosen, and says in one line why automatic runs would not take effect (MirrorHold not
  installed or set up, its copy gone, or the installed copy serving another job file),
  nothing when they would — and *Versions and verification*: keep all versions (a new
  job's choice) or delete version batches older than a number of days, a save that
  switches deletion on or lowers its days asking first, and the reminder's days. What
  explains a choice — what a snapshot does, which files the default exclusions name (the
  sync clients' hint names Dropbox, Google Drive, Syncthing and Nextcloud and calls the rest
  other sync clients, whose files are excluded all the same: the maintainer's decision,
  2026-11-04), how the user's patterns are read and what is always excluded, how the holds judge, what
  deleting versions does — is the hint of its control, shown while the pointer rests on
  it. *Checks* lists every finding of the core's but the one that holds for every job
  (free space and FAT32 file sizes are checked at every preview and run). The window opens
  at most two-thirds of the screen's height; the sections scroll inside it and *Save* and
  *Cancel* stay below them, and a check appearing as the user types does not grow it past
  that, nor at all once the user has resized it: from then on it keeps the size they gave
  it. The source of a job that has run is not editable (`A job that has run keeps its
  source.`), and a destination is never moved in place.
  A folder is typed or chosen, and a typed one is read as the command line's `add` reads
  its paths — spaces and one pair of double quotes around it dropped, as Explorer's *Copy as
  path* adds them — so a wrong path is refused in the same words. The source's field takes a
  typed path beside *Browse...*, under `Type or choose the folder or drive to back up.`
  while empty; it is checked when the field is left (not for its own *Browse...*, nor for
  another program), when Enter is pressed and before a save, and a wrong path is said beneath
  the field in the core's words (`Z:\Work: not on any present local volume.`), the typed text
  kept and the job left with no source until it is right. The source must exist
  (`<path>: this folder does not exist.`): a run on a missing source only stops. *Add...*
  opens a small window, *Add destination*: `Folder on this computer`, with a field and a
  *Browse...* that accepts a folder not made yet, or `Folder of a remote`, with the remotes the
  engine lists and a folder typed or browsed; beneath them `A folder that does not exist yet
  is made by the first backup.`, replaced by a wrong entry's words until it changes, the
  window staying open; *Add* adds the folder and closes it. Choosing the remote's answer has
  the engine open the configuration on a thread (`Asking the engine for the remotes...`), and
  that one session serves the window's browsing until it closes; where no configuration is
  named the line says so, and where it holds none `The configuration holds no remote.`
  *Browse...* of a remote, `Choose a folder of cloud:`, goes into a folder by a double click
  or Enter and out by *Up*, and *Choose this folder* takes the folder shown, made yet or not
  (`This folder does not exist yet.`). A job with a remote destination, added or opened, has
  the engine asked about it once, on a thread: `Asking the engine about the remote folders...`
  stands in *Checks* and *Save* waits for the answer, so that a remote the core refuses is
  refused when the job is made. Removing the destination forgets the answer, so adding it
  again asks again, and an opened job's refusal stands until the editor is opened again. The
  list of destinations shows `cloud:Backups/Work`, `remote` and the free space its service
  reported.
- **Verify...** on one destination (enabled when its drive and source are attached and its
  marker matches — for a remote, when the source's drive is and its first run is confirmed —
  and it is not being verified already; on a job, a group or several items
  while a destination they reach is so, *Groups and several jobs at once* above) asks
  first, offering three choices: `New copies only: the files backed up or changed since
  they were last verified`, `Full verification: every file, to find copies damaged on the
  drive since they were written`, and, when the destination has a pass waiting to be
  continued, `Continue the full verification stopped <time> (12 of 14 files)` (`the
  verification of new copies`; `interrupted`, `that did not finish`) — saying under it when
  that pass began, from which a continued pass counts, why a pass that failed did not
  finish (`Why it did not finish: <reason>`), and that the files it found to differ stay
  listed if it continues. Where no list of verified copies is kept, *New copies only* says
  under it `No list of verified copies is kept for this destination yet: this time every
  file is read, and it counts as a full verification.` and then starts a full verification
  from the beginning. Beside the last full verification and its reminder the dialog says
  `New copies last verified: <date>.` when the latest finished pass is of new copies. It
  selects at first continuing, else the full verification when one is due or no list is
  kept, else the new copies; either of those two begins its kind from the beginning when
  the pass offered for continuing is of that kind, and otherwise continues an unfinished
  pass of its own kind. It says backups go first. For a remote it says `Compares the backup's
  copies with the source's by the fingerprints the service keeps, reading both copies where
  it keeps none. It changes nothing on either side.`, and its full choice ends `to find copies
  damaged on the service since they were written`. The Runs tab shows
  the verification's files and bytes compared, the estimate it began with — `Expected at
  start: 13 s to 1 min (default for a USB 3 hard disk, 30.0 MiB/s to 150.0 MiB/s, 2.5 ms
  to 15 ms a file); listing took 0 s`, or with its basis `from this destination's last 3
  verifications, 80.0 MiB/s to 95.0 MiB/s, 2.5 ms to 15 ms a file` — and the files found to
  differ so far, with *Pause* and *Stop...*. Its details, from the History tab,
  list the files that differ and say what the user can do — `MirrorHold cannot tell which
  copy is damaged. If the source's copies are the good ones, replace the backup's: each
  backup copy moves into versions first.` — with *Replace backup copies...* (enabled while a
  repair of them can start, as in the right-click menu) and, once a repair took them up,
  which it replaced: `Replaced <time>: 2 of these, each read back identical to the source; 1
  left as it is (see its details in History).`, or, where a repair failed on a copy or none
  reached one yet, `...; 1 failed and 1 left as it is (see their details in History); 2 not
  reached by a repair yet.` — a copy any repair of the pass failed on, or whose process
  died before it was done with it, counted failed, whatever a later one found. Where the
  pass's repairs replaced none of its copies the line opens `Repair <time>: none of these
  replaced`, never `Replaced`. The counts are every repair of the pass's; the time is the
  last repair's that replaced one of its copies, or, where none did, the last repair's, and
  where those are several the line says so: `Replaced by 2 repairs, the last <time>: 3 of
  these, ...`, `2 repairs, the last <time>: none of these replaced` (the maintainer's
  decision, 2026-11-03). A verification of new copies is named so in
  History (`New copies verified: no differences`, `New copies verified: 1 differs`,
  `Verification of new copies stopped`, `Verifying new copies`) and in its details, titled
  `Verification of new copies <time> - Photos, destination 1`, which begin `Finished: 3 new
  copies compared (3.0 KiB) in 1 min; 98,765 verified earlier and unchanged, not read.` or
  `Stopped by you: 3 of 5 new copies compared; ...`; a finished pass of a remote adds how it
  compared, `; by their MD5 fingerprints, nothing downloaded`, `; by their encrypted form
  against the service's MD5 fingerprints`, and `; 2 files read in full (2.0 KiB): the service
  kept no fingerprint for them`. The destination reads `Copies differ: see
  verification` until a later verification finds none or repairs have replaced every copy
  it found, unless its last run's status is a problem or needs review, which is shown
  first.
- **Replacing backup copies** asks one question first, titled `Replace backup copies:
  <job>, destination <n> "<name>"`: `Replace the backup copies of 3 files that differ from
  the source? Each backup copy moves into this destination's versions, the source's copy is
  copied in its place, and both are read back to check they now match.`, with a line on how
  long versions are kept while the job's cleanup is on (`Versions are kept 30 days: if a
  source copy turns out to be the damaged one, take the backup copy out of versions before
  then.`) and one when the verification found more than it lists (`The verification listed
  1,000 of the 1,234 files that differ; verify again from the beginning afterwards to find
  the rest.`); *Replace*, whose hint says which copies are left as they are, or *Cancel*.
  The repair then runs as a core process of its own and shows in the Runs tab; a repair
  that could not start says so in a message as a run does. A repair's details window says
  what it replaced, read back identical, of the copies the verification found — `Stopped by
  you:`, `Interrupted (a shutdown, a crash or a closed console):` or `Did not finish:
  <why>.` before it when so; one that replaced none says so (`None of the 3 copies the
  verification of <time> found to differ was replaced.`, `The copy ... was not replaced.`,
  or `No copy was replaced.` where it took none up) — lists the copies it failed to replace
  under `Failed:`, in the problem style, those its process died before it was done with
  among them once it no longer runs (`<path> - the repair ended before it was done with it:
  its backup copy may be in this repair's versions`), then those it left as they are, on
  purpose, under `Left as they are:`, in plain bold, each with why, three of each shown
  and *Show all...* beside them listing the rest (`Failed - <title>`, headed `2 failed`), and
  opens its versions batch and its log. On a remote the question ends `and the new copy is
  checked against the source the way this destination is verified.`, the details and a
  verification's line say `each found identical to the source`, and a copy that still differs
  is told as the service's doing rather than a failing drive's.
- **Files not backed up.** *Files not backed up...* (enabled for a destination whose
  latest run that read the source left any) lists those files with why and the runs in
  a row each has gone unbacked-up, most first; the selected file — the first, as the window
  opens — is shown whole beneath the list, `<path> - <why>`, wrapped to the window's
  width, since the list's columns cut a long path or reason. A run's details say how its source was
  read — from a snapshot of which drive and when, or as it is and why — and give a
  skipped file its runs in a row once there are two or more.
- **The preferences** keep the engine, the rclone configuration with its unlock (the next
  bullet), the three notification choices, *Runs on one
  drive* and *While a run, verification or repair works* (*Save*) — `When runs,
  verifications or repairs need the same drive:`, `Automatic: one at a time on a hard disk, at the same time on a
  solid-state or cloud drive` (chosen at first), `Always one at a time: each waits for its turn` or
  `Always at the same time: they share the drive's speed`, the question's hint saying `A
  drive is one disk: two letters on one disk are one drive. Automatic takes a drive that
  does not say what it is, as many USB drives do not, as a hard disk, unless you say below
  what it is. Activities that reach a remote always take turns with each other.`; beneath the
  three, `What each drive is, for Automatic:` and a row for each
  drive attached, by name, then each marked drive not attached (`drive F: (not attached)`),
  each a list of `As Windows reports: solid-state drive`, `As Windows reports: hard disk`,
  `As Windows reports: no disk beneath it, taken for a cloud drive` or `As Windows reports:
  not said, taken for a hard disk` (`As Windows reports` for a drive not attached), `Hard
  disk`, `Solid-state drive` and `Cloud drive`, every list as wide as the longest choice of
  any row, usable only while *Automatic* is chosen. A name two attached drives share is told
  apart by each one's size and file systems
  (`a drive with no letter (1.8 TiB, NTFS)`). The drives are read on a thread of the
  window's own (`Looking at the drives...`; `The drives could not be read: <reason>.` when
  they cannot be), and *Save* changes no mark until they have been, keeping a mark saved
  since for a drive no row shows; a disk known only by its number is listed disabled, its
  hint `Windows gives this drive no lasting name, so what it is cannot be kept.` All of it a
  run, verification or repair waiting its
  turn reads at its next attempt; `Keep the computer from going to sleep` (ticked at
  first), its hint saying `The computer's own sleep timer waits until the work ends; Sleep
  from the Start menu still works.`, and beneath it `Show it on Windows' shutdown screen`
  (ticked at first), its hint saying `A shutdown then waits for you to choose Shut down
  anyway or Cancel. If the work ends first, MirrorHold stops holding it up; work a shutdown
  cuts continues next time.` (the maintainer's decisions of 2026-10-21 to build it and of
  2026-10-23 on its hint's second sentence), both of which a run, verification or repair already working follows
  within seconds — and show
  the installation in words with *Install* (or *Install / update*) and *Uninstall*, which
  act at once, uninstall after a question, its hint saying what uninstalling removes and
  what it never touches. That another version of the engine is refused is the hint of
  *Another copy of the same version*. A window of the installed copy shows
  its installation but cannot change it: that is the working copy's. While a portable
  release is set up on this account, a working copy's buttons are disabled and say why.
- **The preferences' remote destinations.** `rclone configuration`, a field with *Browse...*,
  then `No password`, `Remember its password`, its entry showing no characters, and `A command
  prints it`, with a field of its own, whose hint says `What the command prints is the
  configuration's password.`; the hint of remembering is the reminder `password set` prints.
  *Check* has the engine open the file on a thread (`Asking the engine...`) and says `Remotes
  in it: cloud, photos`, `It holds no remote.` or why it cannot be opened, no service asked,
  and `Save checks a new password.` while one is typed. *Save* takes an empty field as no
  configuration, refuses a file not there (`<path>: no such file; make the configuration with
  rclone first.`), and asks for a password (`Type the password of this configuration.`) or a
  command (`Type the command that prints the password.`) where the answer needs one; a section
  left as it was is kept unjudged, so a configuration on a drive that is away stops none of
  the window's other choices. A typed password leaves its entry as *Save* is pressed and is
  sealed from the window's process on a thread (`Checking the password...`), *Save*, *Cancel*,
  Escape and the close box doing nothing until that ends, so that no password stays sealed
  that the job file does not name: one that does not open the configuration, or whose seal
  cannot be read back, says why, the window staying open and what was remembered before
  standing; one that does is saved, and `Password remembered` then shows the reminder, at
  the moment it matters and where the keyboard reaches it, as a hint is reached by the
  pointer alone; a save refused over a job
  file changed elsewhere puts back what was remembered before. `A password is remembered.`
  says one is, and choosing another answer removes it once the job file no longer names it
  (`Password not removed`, where another program kept it open). A command is read as Windows
  reads a command line, and what the field shows is written back the same way. The sections
  scroll inside the window where the screen is too short for them, *Save* and *Cancel* staying
  below them.
- **A portable release's preferences** title that section *Automatic runs and Start
  menu*: nothing set up (*Set up*), this copy set up — keep the folder where it is —
  (*Set up again*, *Remove*), another copy set up or that copy gone (*Set up here*,
  *Remove*), or MirrorHold installed from source on this account (both disabled). Each
  says where the jobs and their records are kept; *Remove* asks first, and its hint says
  to delete the folder afterwards to remove MirrorHold completely.
  Opened while another copy is set up, a release asks once at start whether to use it
  instead (*Use this copy* or *Not now*).
- **About** (Help menu) is a window of its own (`about_model`, `about_view`): its first
  line names a portable release by its label (`MirrorHold 2026.10.1`), while a working
  copy and the source installation say they run from source, showing no version; then
  what the program does, the copyright line as the licence file beside the packages
  writes it, that the program is free and under the MIT License, the one address its
  official copies come from (a link the browser opens), and the pinned engine. *Licence*
  shows the licence file in a window of its own, since the file has no extension Windows
  would open it by. A copy without its licence file says so and offers no licence to
  open. A release also ships `THIRD-PARTY-NOTICES.txt`, which *Third-party notices*
  shows in the same kind of window; a copy that ships none — a working copy, the source
  installation — shows no such button, since it runs the user's own interpreter and
  engine. The source installation's copy carries `LICENSE` beside its packages, so its
  *About* shows the copyright line and opens the licence. A release and the source
  installation's copy carry the program's icon too (`shipped_model`), which every window
  takes from the start; a working copy, which carries none, draws the same icon as the
  window opens (`mirrorhold/icon.py`), at the sizes up to 64 pixels a window uses — the
  256-pixel one would take most of a second to draw.
- **The taskbar.** Before its first window opens, the GUI's process takes the
  application identity its Start menu entry carries (`common_view.claim_identity`), so
  that the taskbar groups its windows with the entry, whichever file started it, and a
  window pinned there pins the entry rather than the bare interpreter. That the process
  holds the identity is in the suite; the taskbar's own behaviour is not yet checked.
- **Saving reconciles the tasks** (`session.GuiSession.save`): the window reports a
  reconciliation that failed; the jobs stay saved. A save of the window's remembered
  state alone (`save_view_state`), or of a new order of the jobs and groups
  (`save_order`), leaves the tasks as they are.
- **A save over a job file changed elsewhere is refused**: the GUI keeps what the file
  held when it read it, shows a bar offering to reload when it changes, and hands the
  store a save that the store refuses, nothing written, when the file changed since
  (*The configuration store*, above); the window then reloads instead of overwriting. The
  command line saves the same way.
- **A job file another program holds** past the store's patience at start opens no window
  yet: *The job file is in use* says `MirrorHold could not read its job file: another
  program kept it open. Close that program, then try again.`, with *Try again* and *Quit*,
  and asks again until the file can be read or the user quits. A reload it stops says
  `Not reloaded`.
- **Drives and runs are watched off the window's thread** (`refresh.Refresher`, every
  4 s): the volume scan, each present destination's marker (read again only when its drive
  has just appeared, after a run ended or the jobs changed, when its job's run-records
  folder changed — a run serving several destinations records each as it finishes it, so
  a first backup just made there stops asking for its go-ahead while the run goes on to
  the next — or once a minute), the run,
  verification and repair records (read again when their folder changed, or when the last
  read passed over a file another program held) and the activities' logs (each read once,
  then only what was appended). Runs' status files are read every 500 ms on a thread of
  their own (`refresh.RunWatcher`), so a status another program holds keeps that thread
  waiting, never the window; the job file is compared every 2 s, on the window's thread.
  As an activity ends, the job list shows it as last seen at work until a look at the
  drives begun after its end succeeds, so that a first backup just made is not offered
  again before its destination's marker has been read. A look that fails keeps what was
  seen last, and the status bar says so until a look succeeds — `drives and records not checked
  since 10:12:01: <why>` in place of `drives checked ...`, `runs not checked since ...`,
  `the job file not checked since ...`, or `... could not be checked: <why>` before any
  look succeeded — and says `the status of 1 repair cannot be read now` while a live
  activity's status cannot be read, naming it by its kind as the window last read it, like
  the Runs tab and the closing question, and one it never read as `1 run, verification or
  repair`. The thread keeps the last status it read of each activity it still finds,
  which names one whose status it cannot read now.
- **Display.** The process declares system DPI awareness before Tk starts, so text is
  sharp at the display's scale (measured on the development machine: at 200 %, Tk scaling 2.67);
  list rows are sized from the font; every window opens at what its content requests,
  within 90 % of the screen — the main window, once it has a remembered place, there
  instead, the editor within two-thirds of the screen's height and the preferences within
  85 % of it — and the editor and
  the preview's progress window grow when their content does, the editor within that and
  only until the user resizes it. The wheel over a dropdown scrolls the window, as over any
  other control, and neither it nor a touchpad changes the choice, which a click or the
  keyboard does.
- **Structure.** One sub-feature per pair of files: `*_model.py` holds the logic and
  imports no toolkit (tested headless in `tests/test_gui.py`), `*_view.py` builds the
  widgets, which the suite does not run (checked under real Tk on the development machine).

## Runtime files and privacy

Every runtime file that carries the user's paths lives under `Config/` beside the
package (store, its save lock `mirrorhold.json.lock`, previous copies, corrupt files
moved aside) or is named `*.log`. Every
runtime file a run writes sits beside the store it was given (`locations.RuntimeLayout`),
so with the default store all of them are under `Config/` too: run records
(`runs/<job id>/<start>-<marker>-<run>.json`), each live run's status, liveness lock,
control requests and engine file list (`status/<run id>.json`, `.lock`, `.control`,
`.files`, removed when the run ends), preview tickets (`plans/<job id>.json`), the
admission lock, the snapshot lock and the lock of the notification-area icon's process
(`admission.lock`, `snapshot.lock`, `tray.lock`; an earlier
version's global run lock, `run.lock`, is no longer used), the error output of a run
another program started (`launches/<id>.log`, removed by that program), per job with
arrival runs the drives
it last saw present, the lock serialising its arrival handling and the log of an
automatic start that failed (`automation/<job id>.json`, `.lock`, `.log`), per run
that makes a volume snapshot the record of it (`snapshots/<run id>.json`, removed with
the snapshot), the verification records
(`verifications/<job id>/<start>-<marker>-<verification>.json`) and their lists of verified
copies (`verifications/<job id>/verified/<start>-<marker>-<verification>.partial` while a
pass works, `.list` once it finished — fingerprints alone, never written to the
destination), the repair records
(`repairs/<job id>/<start>-<marker>-<repair>.json`), the reminders finished activities left
for the user, a notice's title and lines (`reminders/<job id>.json`, removed when the window
opens or the icon's menu dismisses them), and the diagnostic logs
of runs, verifications, repairs and automatic starts, and of any command that failed unforeseen
(`logs/<UTC start>-<command>-<process>.log`), which the window reads back for their marks,
and, only where the user chose to have it remembered, the rclone configuration's password
(`remote-password.dpapi`). That file is sealed with Windows' per-user data protection and an
entropy of this tool's own, so only a process of the same Windows account naming that
entropy opens it; it is written whole, then read back and opened to verify, and removed when
the user forgets it or chooses another unlock. A new password is sealed first in a file of
its own beside it (`remote-password.dpapi.<12 hex digits>.trial`), the configuration opened
with that, and only then does it replace the remembered one whole, so an activity unlocking
meanwhile is handed the password remembered before, never one on trial; the trial is
removed as the attempt ends, and one a process left as it ended mid-attempt is removed by
the next attempt once ten minutes old. Only the `unlock` command, which the engine
runs as the configuration's password command and which prints it to the engine and exits,
and what sets it — `password set`, and *Save* in the preferences from the window's own
process — which opens the configuration with it to verify it, ever open it: the
password reaches no command line, no environment variable, no log and no other file. A
password Windows alone remembers is lost with the account's protection keys — when an
administrator resets the account's password, for one — so both tell the user to
keep it elsewhere too.
Only there does this folder's `.gitignore` (`/Config/`, `*.log`) keep them out of version
control. A history or log the window saves goes only where the user chooses, and is
the user's file from then on: it carries the same paths. Tests never use the live store: each names a store in a temporary folder, and
every runtime file of its runs lands beside it.

The installed copy (`installed/`, and the `installed.new` / `.old` / `.removing` folders
an install or uninstall builds or sets aside) holds tracked code, the engine and
`installation.json`, which names only the tool's own paths: the store, the interpreter
and the working copy. It is not private and is readable; it is ignored by the
project's `.gitignore` (`git check-ignore -v` matches `/installed/` and
`/installed.*/`). The scheduled tasks and the Start menu entry live outside the
repository, in Task Scheduler and in the user's Start menu.

A portable release keeps every runtime file in `%LOCALAPPDATA%\MirrorHold`, the
setup record (`installation.json`) and the icon a set-up release's Start menu entry names
(`mirrorhold.ico`) among them, laid out exactly as `Config/` is; that
folder is outside any repository, so no ignore rule is needed and none applies. Its own
folder holds only what its zip held: the build ships every module compiled, so running a
release writes no cache into it. A release its setup program installed lies in
`%LOCALAPPDATA%\Programs\MirrorHold`, which holds besides only Inno Setup's uninstaller
(`unins000.exe`, `unins000.dat`); its entry in Windows' list of installed apps is in the
user's own part of the registry.

## The engine

One pinned portable rclone, kept in the git-ignored `vendor/` directory and found
there unless the preferences name another path:

| | |
|---|---|
| Version | v1.75.1 |
| Release file | `rclone-v1.75.1-windows-amd64.zip` from `https://downloads.rclone.org/v1.75.1/` |
| Release file SHA-256 | `200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e` (as listed in the release's `SHA256SUMS`, whose signature verifies against the release key `FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA`) |
| Executable | `vendor/rclone-v1.75.1-windows-amd64/rclone.exe`, SHA-256 `033eee51c9ad47c2de2624b6674d355274bcd6cf0027a5f85db4437ba24ae81c` |

`python tools/fetch_engine.py` downloads that file, checks it and the executable inside
against the digests pinned beside `PINNED_VERSION` in `mirrorhold/engine/rclone.py`,
and unpacks it into `vendor/`; `--from ZIP` takes a copy already downloaded, and an
engine already in place is left alone. rclone's download server refuses Python's
default request identity (measured: HTTP 403), so the script names itself.
`open_engine` refuses any other version: the engine-contract suite proves the
behaviour the core relies on for the pinned version only. Moving to a newer engine
means changing the pin and its digests, obtaining the new release and running
`tests/test_engine_contract.py` until it passes.

Every start of the engine is windowless, removes every `RCLONE_*` variable from the
child's environment and names its configuration, so the engine never searches for one. A
start that reaches no remote — every start a run onto a drive makes — passes `--config ""` (without it
rclone creates `%APPDATA%\rclone` and would read any remotes configured there). A start
that reaches a remote names the configuration file the user made with rclone and passes
`--ask-password=false`; an encrypted configuration is unlocked only by its password
command (`--password-command`), never by a variable or an argument holding the password.
With standard input closed, as every start has it, a command given an encrypted
configuration it cannot unlock fails at once with its reason (measured: under a third of a
second).

A run uses the engine for one thing: `copy --files-from-raw <list> --no-traverse
--backup-dir <the run's versions batch> --local-encoding None`, read through
`--use-json-log`. Measured: it lists neither side (each named file is addressed alone),
accepts the backup directory inside the destination without a filter rule, and passes
over a listed name the source no longer holds. Its remote control listens on
`127.0.0.1` with a user and a password made for the transfer, of which the engine is
given only the password's SHA-1 hash, in a temporary file (`--rc-htpasswd`, measured to be
honoured) removed when the transfer closes: the password is in no command line and in no
environment, so none of the engine's own children — a configuration's password command
among them — inherits it. The transfer's process sits in a job object, so it ends with the
core process that started it. An answer its remote control breaks off — a progress asked,
a stop told — reads as the engine not answering, as a session's does (below): the progress
is not shown for that moment, and a stop it cannot be told takes the path of an engine that
does not answer.

**A remote.** The adapter also reaches a folder of a remote of the user's configuration,
which a run, a preview, a verification and a repair reach. The explicit-list copy takes such a
folder as its destination, with its backup directory on the same remote, and leaves
`--no-traverse` out: on a remote each lookup the flag makes is a request, two for every
listed file, while without it the engine lists only the folders the list names (measured
against the engine's own S3 server: 102 lookups for 50 listed files with the flag, 52
lookups and 6 listings without it). Every other operation goes through one engine per
session (`mirrorhold/engine/remote.py`), started as a remote-control server
(`rcd --rc-serve`), its configuration named and its credentials passed as a transfer's
are: a remote's features, its free space, a folder's listing or a file's entry — folder by
folder, with sizes, times and the fingerprints the service keeps, every kind or only the
kinds asked for, its progress told after each folder — a file's leading bytes, its whole
content or its content read as a stream, writing a small file, moving a file or a folder
within the remote, making a folder, deleting a file or a folder whole, and removing a tree's
empty folders, a folder holding a file left with it; and the fingerprint of a file of this
computer of a kind only the engine computes (`operations/hashsumfile`, run as a job of the
engine's, which a stop tells to end).
The engine reads its configuration only when an operation first needs it, so the session
opens it as it starts, by listing its remotes, and a configuration it cannot open ends the
start with the engine's reason. A failed operation is read as the service not reached when
the engine's reason is a network failure — a dial, a connection refused, no such host, a
timeout, no route (measured: a WebDAV remote whose server does not run fails with the
dial's own error) — and as the service's refusal otherwise. An encrypted configuration
whose password is remembered is unlocked by this copy's own `unlock` command as the password
command, started on the windowless interpreter in isolated mode and naming the folder its
packages lie in, since the engine runs it in the engine's own working folder (measured with
the development machine's windowless interpreter: the engine ran it and the configuration
opened; a release's own interpreter as that command is not measured yet). A
local remote asked about a folder not made yet reports no free space (measured), which is
then not judged. A request the engine gives no whole answer to — not reached, or broken
off — is told apart from one the service answered with a refusal, so a space that could not
be asked is never read as one the service does not report.

Measured against the engine's own S3, WebDAV and SFTP servers on the loopback address and
its local and encrypted backends (`tests/test_engine_contract.py`, on the development
machine, 2026-10-28):

| Backend | Moves within itself | Copies within itself | Time precision | Fingerprints | Free space |
|---|---|---|---|---|---|
| S3 | no | yes | 1 ns | MD5 | not reported |
| WebDAV, as the engine serves it | yes | yes | 1 s | none | answered with no figure |
| SFTP, as the engine serves it | yes | no | 1 s | none | free and total |
| Local folder | yes | no | 100 ns | every kind the engine knows | reported for a folder that exists |
| Encrypted, over a local folder | yes | no | 100 ns | none | reported for a folder that exists |

Every backend moved a file within itself one way or the other — S3 by a copy and a delete
on the service — and every move, of a file or of a folder whole, kept the files' times. S3
has no folders of its own: it lists any folder it is asked for as if it were there and
empty. Only an encrypted remote answers the engine's `encode` command, which is how one is
known without reading the configuration. Where both sides keep a fingerprint the engine can
compare (S3's MD5, a local folder), a listed file of the destination's size whose time
differs is compared by it and only its time set; where the service keeps none, it is copied
again and its previous copy moves into the batch. The log names a file moved into the batch
by its destination object: `Moved (server-side)`, or on S3 `Copied (server-side copy)` and
then `Deleted`. Sending over the network moves none of the engine process's Windows I/O
counters, but an upload reads its source as it sends, so the read count rises with it. A
configuration holding no token the engine refreshes was left byte for byte as it was by
sessions and transfers; what an engine does to a configuration whose service rotates its
refresh tokens, and two engines sharing one, is not measured. A real service's limits,
precision, names and trash are known only from a run on it.

The destination-storage seam's remote kind (`mirrorhold/storage/remote.py`) is built on
these operations. Measured on the same five backends (`tests/test_engine_contract.py`,
`RemoteStorageTest`, on the development machine, 2026-10-29): the marker and a shared
destination's record written there and read back; a listing through the seam holding the
files and folders a drive's listing of the same tree holds, with their sizes, and their
times to the remote's precision, the management folder and what a scope leaves out never
listed; a file read by its leading bytes, and streamed whole in chunks; a versions batch
taking a folder whole and a file and giving a file back, the times kept, then measured and
deleted whole, and the folders a file's new path needs made by the move itself; a tree of
empty folders removed and one holding a file left; the version cleanup, fenced by the
marker, and the recognition of what reappeared after a move into versions; and a plan
applied there by the run's own steps — a removal and a changed file's previous copy moved
into the run's batch, the changed and the new file copied. On S3, which has no folders of
its own, a folder that is not there lists as empty, so a folder is taken to be there when
it holds something. An engine that cannot answer is never read as a file or a folder that
is not there.

A verification of an encrypted remote, and a repair's read-back there, use the engine for
one more command of their own, its process in a job object as a transfer's is:
`cryptcheck <source folder> <remote folder> --one-way --files-from-raw <list> --combined -
--local-encoding None`, read through `--use-json-log -v`. Measured on the development
machine, 2026-10-29, on a local source against an encrypted remote over a local folder: it
reports each listed file on its standard output as it finishes it — `=` the same, `*`
differing, `+` not on the encrypted remote, `!` not checked for an error — and a listed
file the source no longer holds not at all; it exits 0 only when every file it reported is
the same; a start costs about a third of a second. Started with an empty list it reads
nothing and says at once the fingerprint kind it would compare (`Using md5 for hash
comparisons`), or that the remote beneath keeps none (`does not support any hashes`,
exit 1), which is how a verification knows before it begins whether the encrypted form can
be compared. A local remote asked for a listing's fingerprints computes each kind asked for
by reading the file, so a verification asks for the one kind it compares. For a kind this
program does not compute, the engine's fingerprint of a file of this computer agrees with what a local
remote lists for its copy (`tests/test_remote_maintenance.py`).

Measured about names (`tests/test_engine_contract.py`, `NameTest`): with its default
local encoding the engine reads the fullwidth characters it uses as stand-ins (colon,
question mark, asterisk, vertical line, quotation mark, angle brackets, reverse solidus,
a trailing full stop or symbol for space) as the characters they stand for, so such a
source name is passed over without an error and a root folder so named is read from, or
written to, a sibling folder with `‛` inserted. With `--local-encoding None` names are
taken as they are, but the list is still read in the engine's internal spelling, where
the fullwidth solidus, the control pictures U+2401–U+241F and U+2421 stand for a slash and
control characters; the run's list quotes each with `‛`, which carries it. A name ending
in a space or a period is carried as it is. Not carried however spelled: a name holding
`‛`, one of fullwidth full stops alone, and one holding an unpaired surrogate (the list
is UTF-8). The plan leaves those out.

Measured about a changed file of the destination's size whose time differs: the engine
reads both copies in full and, when they agree, only sets the destination's time and
logs `Updated modification time in destination` — nothing copied, nothing moved into
the backup directory. A graceful stop's soft cutoff starts no new transfer but does not
end such comparisons; `core/quit` ends the engine about 1.5 s after it is asked, which is
how the run ends a graceful stop once no file is in flight. During comparisons no byte
counts as copied, while `core/stats`' count of files checked rises and so do the bytes
the engine's process has read by Windows' I/O counters, which its remote control
answering does not move (measured on the development machine, 2026-10-21).

## Command line

```
python -m mirrorhold [--store FILE] <command>
```

run from this directory. Commands: `volumes`, `jobs`, `add NAME --source DIR (--dest DIR |
--remote-dest REMOTE:FOLDER) [...] [--no-survey]` (the destinations in the order given),
`remove JOB`, `validate [JOB] [--no-survey]`, `resolve JOB`, `export FILE [JOB ...]`,
`import FILE [--replace]`, `engine`; `remotes [--configuration FILE|none] [--unlock
none|remembered|command] [--unlock-command PROGRAM ARG ...]` (the configuration remote
destinations are reached through, its unlock and the remotes it holds, each option changing
the setting it names; `--unlock-command` takes the rest of the line), `password set` (asks
for the configuration's password without showing it — refused for a configuration that
opens without one — seals it beside the remembered one and opens the configuration with it,
taking it up as the remembered password and choosing it as the unlock only when that opens,
the one remembered before standing when it does not or when the new seal cannot be read
back), `password clear` (forgets it, the unlock then none), and `unlock [--seal FILE]` (prints
the remembered password: what the engine runs as the password command; `--seal` prints
instead the password on trial in that file, as `password set` tries it; choosing another
unlock, or another configuration while the password is remembered, forgets it too);
`preview JOB [--dest N ... | --marker ID ...] [--move]` (`--move` plans moving into versions
what a first run's folder holds and the source does not, where that is offered; without it,
that is left where it is), `run JOB [--dest N ... | --marker ID ...] [--confirm TICKET |
--first-backup] [--unattended]` (`--first-backup` confirms the first runs onto folders that
hold nothing), `verify JOB [--dest N | --marker ID] [--new-copies] [--restart]` (a
destination named when the job has more than one; without `--new-copies` a full
verification, with it a verification of new copies — every file read where no list of
verified copies is kept, as it says), `repair JOB [--dest N | --marker ID]` (the copies the
destination's latest finished verification found to differ, as *Maintenance* describes; a
destination named likewise), `status` (each run,
verification and repair in progress, in the Runs tab's order; `verification of new copies
of destination 1` for one of new copies; a waiting one's line names
what it waits for, the drive they share when that alone is why, and its place in line —
`waiting for the run of 'Work' (1a2b3c4d5e6f) to finish; both use drive I:; 2nd in line`,
and for one whose status cannot be read now `...; its status cannot be read now; next in
line`, named as the waiting one last read it, or `waiting for a run, verification or
repair (1a2b3c4d5e6f) to finish; ...` where it read nothing of it — or reads `starting` before it
has asked for its turn and as it is admitted, and `waiting for its turn` once it has asked
and names nothing; a live activity whose status cannot be read now is listed by its
identifier alone, `run 1a2b3c4d5e6f  - alive; its status cannot be read now, so every run,
verification or repair asking for its turn waits for it`),
`history JOB [--limit N]` (runs, verifications and repairs, a verification or repair
whose process died read as `interrupted`; a verification of new copies
reads `verification of new copies completed: 1 of 1 file compared (1 of 1 byte), 0 differ;
2 verified earlier and unchanged, not read`, and a repair whose process died before it was
done with some copies says where their backup copies may be: `repair interrupted: 1 of 3
copies replaced, 2 not finished: their backup copies may be in
.mirrorhold\versions\2026-10-17T11-00-00Z`), `pause
[RUN]`, `resume [RUN]`, `cancel [RUN] [--now]` (a run or a repair after its files in
flight, or at once; a verification stops at once either way; one whose status cannot be
read now is reached by its identifier, which the refusal names when none is given);
`when JOB [--arrival on|off] [--schedule SPEC|none] [--automatic on|off]` (SPEC in the
schedule grammar of *The job contract*, such as `"weekly mon,thu 09:00,21:00"` or
`"monthly last every 2h 08:00-23:00"`),
`install`, `uninstall [--if-set-up-here]`, `installation` (in a portable release: set it up
where it lies, remove that setup — with `--if-set-up-here` only if this release is the one
set up, which the setup program's uninstaller asks — say which copy is set up, with status
0 whatever the answer), `reconcile`, and `automatic JOB --by
arrival|schedule` (what a scheduled task runs). A destination is named by its number in
the job's order (`--dest N`) or by its marker's identifier (`--marker ID`), which names it
wherever the job's order puts it: the identifier `resolve` prints on a line of its own
beneath each destination (`    marker identifier <id>`), or a unique prefix of it of four
characters or more. A shorter one is refused as too short, one that names no destination's
marker or several is refused, and `--dest` with `--marker` is a usage error; `verify` and
`repair` given neither on a job of several destinations are refused, asking for one. `jobs`
lists the jobs in the order the
window shows them (*The job contract*) and names each job's group; `export`
carries the groups of the jobs it writes and `import` places a job it adds in its
group; groups are made and changed in the GUI. A remote destination is listed by `jobs` as
`cloud:Backups/Work (remote)`; `resolve` says of it `reachable; marker missing` (the marker's
state, as for a drive), or why not — `cannot be reached: <reason>`, `the service refused:
<reason>`, `the configuration cannot be opened: <reason>`, no such remote, a file standing
where the folder is named, `stopped answering: <reason>` where it stopped answering as its
marker was read. `preview` and `run` name it by its folder —
`destination 1 at cloud:Backups/Work: first run onto an empty destination` — and say why
where it cannot be backed up now: `destination 1: absent (remote-unreachable) -
cloud:Backups/Work cannot be reached: <reason>; the next run tries again`, a refusal by its
code and its words (`destination 1: stopped (remote-configuration-unusable) - ...`), names it
cannot tell apart (`stopped (destination-names-ambiguous)`) and a remote that stopped
answering (`failed (remote-failed)`), as *A remote destination*, under *The run*, words
them; a preview prints its notices under it, the free-space line where the service reports
none and, in its old-versions line, what the service may keep. `verify` says as it begins
how a remote's copies are compared, and `verify`, `repair` and `history` word a remote's
verification and repair as *A remote destination's verification*, under *Maintenance*, has
them. `add`, `validate` and `import` print each
finding by its code, a file given as the source — which `add` and `validate` do not
survey, so it is refused alone — a file given as a destination, and a destination that is
itself a folder Windows or this tool needs among the refusals. `preview` says of a shared destination whose
record was lost that it is planned as a first run that leaves what is there, moving it not
offered (`--move` changes nothing there), and what that is — `left as they are: 2 files -
Windows, Photos\old.jpg`, a line per name of the source the folder already held, and that
any files the job backed up there whose source copies are gone are among them — and `run
--confirm` with its ticket makes the record again; it prints the notices a preview repeats
(*Creation-time validation*) under their destination's line, each as
`    notice: <message>`. It tells the files that
reappeared after a move into versions (*Confirmation*) apart from the other deletions: the
count reads `into versions: 4 deleted at the source (3 of them reappeared), 0 overwritten`,
the samples of what was deleted at the source leave them out, and a line of their own
names them, a folder whose every file reappeared by its name, with the batch's time in
UTC — `reappeared after being moved into versions at 2026-10-27T22:14:31Z: 3 -
tree\ (whole folder, 2), gone.txt` — followed, where the destination carries the
cloud-copy notice, by `Google Drive keeps a copy of this folder and may have put them
back`. `add`, `remove`, `import` and `when`
reconcile the scheduled tasks after saving, and each saves only over the job file it
read: a window or another command that saved in between refuses the save, nothing
written. `run`, `verify`, `repair` and `automatic` mark in their log what they were given,
the job they resolved, each run, verification or repair they began and how they ended (*The
log's marks*, under *The run*). Exit status: 0 done, 1 refused (an install or uninstall
refused, a verification or a repair that cannot start, a save over a changed job file, or a pause, resume or cancel the run's
control file did not take, included) or failed unforeseen, 2 usage, 3 store unusable
(another program keeping the job file included), 4 engine unusable; for `run` also 5 a
pair held or waiting for confirmation, 6 a pair stopped, failed or interrupted, 7
cancelled; for `verify` 6 files differ or the verification failed, 7 stopped; for `repair`
5 a copy left as it is, 6 a copy that could not be replaced or still differs or a repair
that failed, 7 stopped (the maintainer's decision, 2026-10-17). To install
from this working copy:

```
python -m mirrorhold install
```

## Building a release

A release is a zip a user unpacks and runs without Python — the program, the interpreter
it runs on, the pinned engine and the file a user double-clicks — and a setup program
that installs those same files (*The setup program*, under *Automatic runs, installation
and notifications*). It is built from a commit, never from a working tree:

```
python tools/fetch_engine.py
python tools/fetch_build_inputs.py
python tools/build_release.py --label 2026.10.1 [--commit REV]
```

`fetch_build_inputs.py` downloads the four inputs into the git-ignored `build-inputs/` —
CPython 3.14.8's embeddable package and CPython's full package of the same version (for
Tk and the licence page of its documentation), both from python.org and pinned in
`tools/release_core.py`; the pywin32 312 wheel from PyPI; and Inno Setup 7.1.1's 64-bit
installer from its GitHub release, signed by its publisher; the last two pinned in
`tools/build_inputs.py` — and puts each in place only when its SHA-256 matches the digest
its publisher lists; `--from DIR` takes copies already downloaded. `build_release.py`
downloads nothing: it checks every input against its pin before it uses it. It needs git
and the .NET Framework C# compiler that Windows 10 and 11 carry, and runs under any
Python 3.14 or later. It installs nothing either: each build unpacks Inno Setup's compiler
from the pinned installer into a folder of its own, by the installer's portable mode, and
deletes it with the build (measured on the development machine: the portable mode
registers no installed app, no file type and no Start menu folder, and leaves the per-user
programs folder as it was).

The build's order and every step of it that belongs to no one program — the commit's
tree, the runtime, the bytecode and the refusal of a path naming the build, the suite's
verdict against the ledger, the zip and its sums — are `tools/release_core.py`'s, a file
kept byte for byte the same in every program its maintainer publishes and changed in all
of them at once. `tools/build_release.py` composes it with what is MirrorHold's own: what
a release ships, the parts of pywin32 it keeps, the engine, the icon, the third-party
notices of pywin32 and the engine after Python's, the build record, the launcher, and how
`tests/TEST_LEDGER.md` names the reds it records, and the setup program. The shared build
can also run a program's own steps after the zip, each writing a further asset that
`SHA256SUMS` lists beside it; a MirrorHold release takes one, the setup program. In order,
the build:

- reads the commit's tree through `git archive`, the way a snapshot of the project is
  made, so a release holds only committed files, each as the commit holds it, whatever
  git's line-ending settings;
- assembles `MirrorHold-<label>/`: `mirrorhold/`, `mirrorhold_gui/`, `LICENSE` and
  `README.md` from the commit; `runtime/`, the embeddable package with Tk's module,
  `_tkinter` and Tcl/Tk libraries from the full package (those libraries carry their own
  scripts and the Python Software Foundation's signature) and the parts of pywin32 the
  program uses — COM and the shell's shortcut and property interfaces; not Pythonwin with
  its MFC and Scintilla libraries, the other COM extensions, demos, tests, headers or
  link libraries — with a path file naming the standard library, `Lib` and the release's
  folder; the engine in `vendor/`; `mirrorhold.ico`, written by `tools/make_icon.py` from
  the commit's `mirrorhold/icon.py`;
  `THIRD-PARTY-NOTICES.txt`; `build.json` with the label and the interpreter's version,
  and no commit, so that nothing in a release depends on which repository built it; and
  `MirrorHold.exe`, compiled from `tools/launcher/MirrorHold.cs`, with the time and the
  identifier its compiler stamps on every build settled — the time to the label's first
  day, the identifier to one drawn from the file's own digest — since the .NET Framework's
  compiler has no switch that leaves them out;
- compiles every module to bytecode bound to its source's hash, each recording its path
  inside the release, so a release writes no cache into its own folder and carries no
  path of the machine that built it. It compiles in one process: each worker of a
  parallel compilation is a fresh start of the release's interpreter, which imports
  pywin32's bootstrap while the bootstrap is being compiled, and Windows then refused to
  replace its compiled file, or a cache naming the build folder landed over it (measured
  on the development machine). The build refuses a release any of whose files names the
  folder it was assembled in or the builder's home folder;
- runs the whole suite under the assembled interpreter on the commit's tree — about
  thirty-five minutes on the development machine — and refuses the release unless the
  failing tests are exactly those
  `tests/TEST_LEDGER.md` records (`--no-suite` skips it and says the build is not
  verified);
- writes `dist/<label>/MirrorHold-<label>.zip`, the release's files at its top level, its
  entries in one order and every file dated the first day of the label's month at
  midnight;
- compiles `MirrorHold-<label>-setup.exe` beside it from the commit's
  `tools/setup/MirrorHold.iss` and the same assembled files, telling the script the label,
  that date — which every installed file carries too, at midnight UTC — and the licence's
  copyright line;
  the compiler comes from the pinned installer, unpacked for this build alone. A compiler
  that cannot be unpacked, fails, writes nothing or does not end refuses the build, saying
  why, and so does a zip whose digest changed while the setup program was made: the zip is
  the one a build without it writes;
- writes `SHA256SUMS` naming the zip and the setup program, and prints their digests.

The zip holds no folder of its own around the files: Windows' *Extract All* unpacks it
into a folder named after the zip, and a package manager unpacks it into the one folder it
keeps for the program, which then holds the next release in the same place.

The label is `YEAR.MONTH.N`, the N-th release of that month; the in-code version stays
1.0.0.

Two builds with one label of commits holding the same files make the same zip, byte for
byte, wherever the repository and the build lie (two copies of a repository at different
paths, and two repositories holding the same files in commits of their own, measured on
the development machine, 2026-11-02). To rebuild a release and check it, build the commit
this repository tags with the release's label, with that label — the suite checks a build
and adds nothing to its zip, so `--no-suite` may skip it here:

```
python tools/build_release.py --label 2026.10.1 --commit 2026.10.1 --no-suite
```

and compare the rebuilt zip's SHA-256 with the one the release's `SHA256SUMS` gives, as
`README.md` checks a download: the two are the same. The inputs and the engine are pinned
and checked by the two fetch scripts; what the build does not pin must be held equal:
CPython 3.14.8 running the build, whose zlib compresses the zip and the icon and whose
HTML parser writes the third-party notices; and the version of the .NET Framework's C#
compiler the release was built with, which differs between Windows installations
(4.8.9032.0 on the development machine; whether another version writes another launcher
has not been measured). The setup program is compiled from the same files and the label
alone: two builds of one commit with one label into two folders made the same setup
program too (measured on the development machine, 2026-11-03), and the check above
compares the zip.

The setup program is a 64-bit Inno Setup installer in its modern style, compressed as one
LZMA2 stream. Inno Setup's licence permits any use, commercial included, on the condition
that its notices stay where a compiled setup program carries them, which an unmodified one
does; it asks for nothing in the zip, whose notices it leaves as they are, and an
acknowledgment in the documentation is welcome, which `README.md` gives.

`MirrorHold.exe` starts the release's own `runtime\pythonw.exe -m mirrorhold_gui` in the
release's folder, passing its arguments on, and exits. The release's folder is the one it
was started from, as that path names it — through a junction, the junction's name; only
where that folder holds no interpreter, as when it is started through a symbolic link to
it (winget's command alias), does it read the path the link leads to and take that folder.
A release works from a folder whose path is at most 209 characters long: pywin32 loads its
shell extensions from `runtime\Lib\site-packages\win32comext`, 38 characters below the
folder, only while that path is shorter than 248 characters, and from a folder of 234
characters Windows refuses to load the interpreter, from 240 to start it by its path
(measured on the development machine). From a longer folder the launcher starts the
interpreter by the folder's 8.3 name where the drive keeps one that short, and the release
runs by that name; where it keeps none, the launcher says `MirrorHold cannot start from
this folder: its path is <length> characters long, and on this drive Windows can start
MirrorHold only from a folder whose path is at most 209 characters long. Move the folder
to a shorter path, for example C:\MirrorHold, and start MirrorHold there.` and starts
nothing (the maintainer's decision, 2026-10-19). It asks every path of Windows' own wide
calls, never of the .NET Framework's path checks, which refuse a path past 260 characters.
With `MIRRORHOLD_LAUNCHER_MESSAGE_FILE` set, it writes what stops it to that file instead
of showing it in a dialog, which is how the suite reads it.
It carries the program's name and icon, which the signed interpreter cannot carry without
losing its signature; every process that does the work is that interpreter, which the
Start menu entry — naming the copy of the release's `mirrorhold.ico` kept beside the
store — and the scheduled tasks start directly. The third-party notices reproduce each
bundled component's own licence: CPython's `LICENSE.txt`, which
covers the libraries its Windows build links, Tcl and Tk among them; the
incorporated-software section of CPython's documentation; pywin32's licences; and the
engine's, from its `README.txt`.

## Tests

```
python tests/run_all.py
```

The suite is headless unit tests on importable logic: tests that each check a rule this
file states, and tests that follow the program's own path end to end. Its conventions:

| Convention | Choice |
|---|---|
| Tree layout | One `tests/` directory beside the package; `support.py` puts the project on the import path |
| Runner and selection | `unittest`; `tests/run_all.py` runs every module in a fixed order. Any change is checked by the whole suite |
| Registration | One line per test file in the table below |
| Failing tests left standing | `tests/TEST_LEDGER.md`, one Markdown entry per red: tests, commit (the change that caused it, by its date and a description), delta, disposition, since |
| Snapshot testing, benchmark data, numeric tolerances | None in use |
| Check scripts older than the suite | None |
| Hand checks counted as tests | None: the checks made by hand, described below, stand beside the suite |

| File | What it binds |
|---|---|
| `test_endpoints.py` | Identity anchors and serials, relative paths and containment, the disk-query decoders (GPT, MBR, no partition), the volume sources (the live Windows one reads metadata only), resolution present / absent / ambiguous, `endpoint_from_path`, markers and their schema (one without a version read as schema 1, a shared destination's schema 2, an unknown one unreadable), a shared destination's record (names as NTFS spells them; one missing, not a record or another's lost, one another program holds or a remote that answers will not serve only unreadable now, a remote that stops answering as it is read told as the remote), a drive destination's marker and shared record met at the moment a run renames a new one over them read rather than missed and a marker truly not there told so once looked for again, the folders Windows and this tool need and their endpoints on the present volumes, sync-root mapping (an entry naming no folder skipped) and Google Drive for desktop's folder list read from a database the test writes — each folder found on its volume whatever letter Drive last saw it at, one never seen at a path still found, its kind by the My Drive flag, a missing list, one of another shape and a file that is no database said, the list read without writing it; a volume's drive — the disk it lies on, named by its GPT disk GUID, its MBR signature or else its number, two letters on one disk one drive and a volume no disk carries a drive of its own, a drive called by its letters, else its label, else as having no letter — and the system drive's disk read from this machine's real volume source; a remote's folder — its name as rclone allows one and never of one letter, its folder kept with slashes, `remote:folder` read — decoded apart from a drive's by its keys and resolving as no volume |
| `test_storage.py` | The destination-storage seam's drive kind on temporary folders: a folder of this computer taken as a drive's and a storage as itself, its paths and its words, times compared within its volume's file system and its free space the volume's, entries read as themselves, a link never followed — a junction on the way being the entry found, one inside a batch removed as itself and what it points at untouched (Windows) —, a folder that is a file or is missing said so, its listing and what it holds as the folder's, a file written whole with no staging file left and read whole, by its leading bytes and as a stream, the management folder made hidden (Windows), moves keeping the time and making the folders on the way unless told not to, a change of case alone included, removals deleting only what they are asked for (a tree of empty folders removed and one holding a file kept, a batch with a read-only file measured and deleted); the marker and a shared destination's record written through a storage reading back as a folder's |
| `test_model.py` | The job contract's defaults, strict documents, errors located at their field; the share hold (half by default, adjusted, switched off, values outside its range); a group's document and its errors; the choices for activities among the preferences (turns on a drive automatic and the others on at first, kept through the document, one missing or of the wrong kind an error at its field); a remote destination kept through the document and a source never one; the rclone configuration an absolute path and a command given exactly with the command unlock, kept through the document, and a field the preferences do not know (a password) an error; a destination found by its number or by its marker, and one the job does not have, or named both ways or neither, refused |
| `test_store.py` | Save and reload, atomicity, previous copies, a watcher's read moving nothing aside, a corrupt file moved aside — once when several processes read it at once, the others saying so, and a file a save replaced in between read instead —, a file found missing in a save's rename read, by a load and a fingerprint alike, once the save gives its name back, while no file beside a lock nobody holds is a fresh store and a load with neither makes nothing, a newer schema left untouched, migrations, restore, export, import and merge; groups saved beside the jobs, a schema-1 file and export read with none, the rules spanning groups and jobs (a file breaking one corrupt), moving jobs between groups, renaming and deleting a group, a save of the GUI's remembered state alone keeping no previous copy, one order of the jobs (the groups' jobs first, then the jobs in no group) with a group or a job moved one place within its level and refused past either end, a save changing only that order keeping no previous copy, an export carrying its jobs' groups and an import placing a job it adds; schema 3: a schema-2 file and export read with every share hold at half, the next save writing the current schema, a share switched off or adjusted saved as it is, a missing migration leaving the file unsupported; schema 4: a schema-3 file read with every choice for activities as at first, the next save writing the current schema and keeping the file it replaced, a choice switched off saved as it is, a schema-3 export read as it was and an export written in the current schema; a job file, a previous copy and an export beginning with a byte-order mark read, the next save writing none; schema 7: a schema-6 file read with no configuration named and saved with its drive destinations written as they were, a remote destination and the configuration saved, read back and exported, a copy that knows only schema 6 leaving the file untouched, a destination of neither kind a corrupt file; schema 8: a schema-7 file's schedules kept as they were, every schedule form saved, read back and exported, a copy that knows only schema 7 leaving the file untouched; schema 9: a schema-8 file's jobs and marks kept as they were, a cloud drive's mark saved and read back, a copy that knows only schema 8 leaving the file untouched |
| `test_exchange.py` | The files the processes share, under contention, a second real process wherever the claim is about two (Windows): a reader sharing deletion never in a writer's way, a file another program holds written once it lets go and busy past the patience with nothing changed, a missing file never a refusal, a file renamed in while a read looks for it again read and one truly not there told so once that patience is spent, two processes replacing and reading one file never failing each other (the writer never refused, a read refused for a moment borne by its patience, one that finds no file reading it again within that patience); a look at a lock its owner removes never raising and never making it again; a status published by another process seen alive at every look, one another program holds alive, unreadable and conflicting with everything — named by an observer that looks again and by a waiting activity from the last status each read of it, by its identifier alone where it read none, and reached by a request naming it —, what an observer remembers of each activity forgotten once it is no longer found, a control file held, or not there for a moment, leaving what was wanted, a request it does not take reported and one it takes landing, requests to one run taking turns (one that waits for its turn past its patience not landing, one that waits keeping a change made meanwhile, the turn's file never taken for a run and gone once let go), an ending run waiting for a request at work with its status still there and leaving no file, a request that has its turn once its run has ended writing nothing, an ending run waiting for a reader, a dead run whose status is held recorded once it is let go; a run found by its liveness lock while its status is not there (looked for again, then alive and unreadable and conflicting with everything, never reaped; a status that appears meanwhile read; a lock nobody holds no run); the job file: a save over a change made since it was read refused with nothing written, one another program holds busy and never corrupt, one another process saves loaded at every try |
| `test_survey.py` | The survey's counts and its line's words, a count of one in the singular; links not followed, drive-root artefacts and offline files left out, folders whose names end in a space or a period walked; the entry classifications |
| `test_wording.py` | A count with what it counts: one in the singular and every other count, none included, in the plural, an irregular plural, thousands separated or not, a word agreeing with a count; a run's line counting what it left and its cleanup, a verification's line counting one file of one, and the Runs tab counting one entry listed |
| `test_validation.py` | Every refused, warned and noticed case, by code; what lies inside a destination, by its path there; the three refusals that look at this computer — a file as the source, a file as a destination (one not made yet left to its run), a destination that is itself a folder Windows or this tool needs (a folder of its own inside one allowed) — and a context built from its fields alone looking at none; a destination in a synchronised folder by what the cloud does with it (refused where the folder is kept on every computer, noticed where the cloud keeps a copy, protected strictly inside either way), a folder found by its volume named where it lies now, a list that could not be read noticed at each present destination, and the notices a preview shows beside each destination; a remote destination from what the engine said of it — the configuration not named or unusable, a remote it does not hold and a service's refusal refused, the capability floor (times of two seconds or finer, a move or a copy within), a remote not reached or not asked noticed, one that does not encrypt noticed in every preview, a file where its folder is named refused, free space judged only where reported — and remote folders overlapping by remote and folder, never with a drive's |
| `test_engine_adapter.py` | Engine location, version pin, environment, windowless start; the configuration a start names — `--config ""`, or the user's file never asked a password for, with its password command spelled as the engine's list reads it; the remote control given only a hash of its password; a remote folder as the engine names it; a web address's query removed from an engine message; the engine's times read to the nanosecond; a configuration's failure told without advice that cannot work; a transfer's command for a drive and for a remote, and its log read whichever side its objects lie on, all with no engine started |
| `test_fetch_engine.py` | Obtaining the engine without the network: a release file put in place only when both digests match, nothing else left; members outside its release folder, a missing executable and a damaged zip refused with nothing left; a download through an opener in memory, naming the tool, made once, and a failed one leaving nothing; an engine in place left alone, or refused when it is another; the pinned executable digest against the engine in `vendor/` |
| `test_release_build.py` | The release tooling without the network and without its inputs: the icon `mirrorhold/icon.py` draws (every size, bitmaps below and PNG for the largest, the small sizes on the pixel grid with the two blocks mirrored, the smallest showing both, drawn the same every time); each input pinned by a digest and accepted only with it; fetching an input (downloaded once and then left alone, taken from a folder holding a copy, another content refused with nothing left, a tampered one in place refused); the release label and the time every file of its zip carries; the launcher's file properties, and the launcher compiled into a release folder starting its interpreter when started directly, through a junction to the folder (keeping the junction's name) and through a symbolic link to the file (finding the release the link leads to; Windows), and in a folder too long as named starting it by the folder's 8.3 name, or, where no name of it is short enough, saying why and starting nothing (Windows); the compiler's stamps in the launcher settled, one source compiled twice with one label making one file dated by the label and another label another identifier, and a file whose stamps cannot be settled — a debug directory, a strong name, a checksum, truncated or empty — refused (Windows); what of pywin32 a release keeps; a documentation page's section as text, the engine's licence from its README, the notices naming each component and carrying each licence once; the ledger's tests, failures read in both unittest spellings, a build needing exactly the ledger's failures; one folder making one zip whatever its files' times, its files at the zip's top level, a file naming the build folder found in any spelling, the sums; a commit's tree read from a git repository made in a temporary folder, without its export-ignored paths or later changes; MirrorHold's part of the build composed with the shared core (what it ships, its pinned inputs beyond the runtime's — pywin32 and Inno Setup's installer —, its suite, its one step after the zip, the setup program, every path it names present), the setup program's step with a stand-in for its compiler (the compiler unpacked by the installer's portable mode into a folder deleted with the build, the commit's script compiled over the assembled files with the label, the date every file carries and the licence's copyright line, the zip and the assembled files left as they were; a compiler that writes nothing or does not end, an installer that does not unpack, saying what it said, and a step that changes the zip each refusing the build), the setup script itself (installing for the user alone into one folder under one identity, no Start menu entry or registry value of its own, every file of the release dated the first day of the label's month, and each command it asks the program one the command line takes), a pinned input sharing another's file name in any case refusing the build before anything is written, the core's release label being the build record's, and an asset after the zip accepted only as a file beside it under a name of its own in any case; the file names the build writes being those the window reads |
| `test_cli.py` | Every command in process against volumes mapped onto temporary folders, `status` with files found identical, files being compared, a verification of new copies named so, what a waiting run or verification waits for — the drive they share and its place in line too —, a run starting and one waiting for its turn, and a live activity whose status cannot be read, listed, named by what waits for it — one never read as a run, verification or repair — and reached by a request naming it, included, `jobs` listing the jobs in the window's order and naming a job's group, and an export and import carrying it; `add` refusing a file as the source, a file as a destination and the job file's folder as a destination, saying why and saving nothing; `run --first-backup` through the engine (a held first run saying how to give it, refused unattended or with a ticket, a folder holding something still waiting for a preview); a preview leaving what a folder holds unless asked to move it, and saying when moving is not offered; a destination in a folder a cloud client keeps a copy of noticed by `add` and in every preview, and a list of synchronised folders that could not be read said in the preview; a preview naming the removals that reappeared after being moved into versions apart from the others, with when they were moved and the cloud client keeping a copy of the folder; a save over a job file changed while the command ran refused, a job file another program holds and a request a run's control file does not take reported in words, a command that fails unforeseen saying so in one line and logging its traceback; the diagnostic log (what was said, a failure whole, nothing outside an open log, its bounds in size and in number) and its marks: read back as they are written, a log whose process ended without a word and one written before marks, every log saved as one text, a logged command marking what it was given, what it resolved — with the numbers of the destinations it was given by marker — and why it could not start, a job file another program holds as that reason, a run that ran marking its run and no failure, an automatic start that could not start keeping why; why Windows refused a notice written to the log of an unattended run, printing nothing, and of an automatic run that ran or could not start, no run record naming it; `resolve` printing each destination's marker identifier, and a preview of a destination named by a prefix of its marker, one naming no destination's marker refused and one named both by number and by marker a usage error; one end-to-end run of `python -m mirrorhold` on the real volume source |
| `test_plan.py` | Without the engine: listings (management folder, exclusions, placeholders, links, drive-root artefacts), what hides an entry from a listing — the top-most excluded entry on its path, as the listing skips it —, what a destination holds read from its top level agreeing with its listing and its plan in every case, every plan category (names left out included), the names the engine cannot carry and the list's spelling, signatures and their progress, every gate verdict (the share hold at the job's value or switched off, a first backup's confirmation onto a destination holding nothing and not onto one holding something, a confirmed first run the destination outgrew waiting whole), a shared destination's part (what a first run claims and leaves, what appears later outside the job's names, a left entry the source reaches, a forgotten name, a source file where a folder was, the part its record says is backed up), a listing within a scope, a folder holding a left entry, or a name a run cannot carry, never moved whole, the estimate (a cost per file added to each end, a preview's counting none), tickets (a first run's choice among them), records (schema 4, older schemas, names holding an unpaired surrogate), the throughput, status and its control requests, the status pump and the time left, a dead run reaped, locks (one held a moment, a status published from two threads); admission: what two activities conflict over, the order of turns, an attempt published and admitted through the status channel, a dead run recorded once, when a verification gives way; one drive at a time: a drive both use conflicting only where drives are taken one at a time, the waiting activity judging by its own choice, its place in line and its words, the drives a claim names (each once, by its letters) and a claim naming none compared by its folders, and through the status channel the drive and place a waiting activity publishes, a folder both need naming no drive, an activity choosing the same time admitted beside one on its drive, a verification giving way as the waiting run judges its own turn; a listing's and a preview's progress, and ending them through it; folders whose names end in a space or a period listed as written and each file in them left out by name, and a destination holding only such a folder holding something and keeping it |
| `test_drive_turns.py` | Turns on a drive, by its kind: a disk's seek-penalty answer decoded (spinning, solid, not known when it says nothing), a volume taking its disk's kind and one no single disk carries not known, the device type a volume with no disk answers decoded (a disk file system a cloud drive, any other not known), a volume with no disk but no such answer not known and one on a disk Windows names keeping its disk's kind, every volume Windows reports having a kind and only one Windows names no disk for being a cloud drive (Windows); the three-way choice (automatic at first, each kept through the document, any other value an error at its field) and the job file's migration from schema 4 (its step from schema 5 to 6 is `test_drive_marks.py`'s), a schema-4 file's one-at-a-time on read as automatic and off as always at the same time and saved again in the current schema, one written before the choices read as automatic, a value neither on nor off a corrupt file; a claim naming each drive's kind, taking turns under automatic on a spinning drive or one not known and not on a solid one or a cloud drive, always and never ignoring the kind, a cloud drive published in a claim and read back, two activities on one cloud drive working together under automatic, folders and two runs of one job conflicting whatever the choice, the waiting activity judging by its own reading, an older copy's claim read with its one choice and kinds not known, its one choice still published, a kind this copy does not know read as not known; through the status channel a waiting run naming the spinning drive it waits for and admitted beside one on a solid drive, a verification giving way to a run waiting on a spinning drive only and to an older copy's run; a verification on temporary folders reading the choice from the job file and its drives' kinds from its scan, waiting for a run on its destination's drive or working beside it; the preferences' three answers and the questions before several jobs and verifications under each, *Automatic*'s naming the cloud drive beside the solid-state drive; a remote's folder claimed whatever the volumes say and on no drive, any two activities reaching a remote taking turns whatever the choice, and remote folders conflicting by remote and folder, never with a drive's |
| `test_drive_marks.py` | What the user says a drive is: only a drive whose name lasts marked, never a disk known by its number, a hard disk, a solid-state drive or a cloud drive and nothing else, kept through the document, none at first and one a drive, a mark that cannot be read an error at its field; the job file's schema 6, a schema-5 file read with no drive marked and saved in the current schema, a schema-4 file through both steps, marks saved and read back, one that cannot be read a corrupt file; a claim taking a mark in place of what the disk reports either way, a drive marked a cloud drive taking no turns under automatic and a cloud drive marked a hard disk taking them, naming the drive as it is called now and publishing the kind it judged by, a drive with no mark as its disk reports, always and never ignoring the marks, every activity's claim built alike from the job file's choice and marks; a verification on temporary folders working beside a run on a drive marked solid that does not say what it is, and waiting for one on a solid drive marked a hard disk; the preferences' rows (every drive attached by name, then each marked one not attached, each one's choices, a disk known by its number not markable, a cloud drive reported as one and any drive settable to one), saving them (each drive as chosen and named as now, a mark saved since for a drive no row shows kept), drives sharing a name told apart by size and file system, and the words of the drives and of the questions before several jobs and verifications under *Automatic* |
| `test_engine_contract.py` | The pinned binary's behaviour, each test naming the claim it settles, the run's own explicit-list invocation and its control included, and that invocation copying from a source root spelled as a device path (a snapshot's shape); through the run's own `Transfer`: every stand-in class of name and both roots carried, what no spelling carries, names ending in a space or a period, files found identical only dated, a soft cutoff not ending a comparison and its end through `core/quit`, a comparison counting the files checked and its process's reads as it goes; against the engine's own S3, WebDAV and SFTP servers on the loopback address and its local and encrypted backends, in a throwaway configuration: each remote's features and free space, a small file written, replaced and read whole or in part, listings with sizes, times and fingerprints and a cancel between two folders, files and folders moved keeping their times, a file and a versions batch deleted, the explicit-list copy onto a remote with its batch there, a file of the same size and another time compared by fingerprint where one is kept, the requests a copy makes with and without `--no-traverse`, an upload moving its process's reads; a locked configuration ending a session at once, its password command from a folder whose name holds a space, the session's password given to the engine only as a hash; the destination-storage seam's remote kind on every backend — the marker and a shared destination's record through it, a listing holding a drive's listing's files and folders (the management folder and what a scope leaves out never listed), a file read by its leading bytes and streamed in chunks, a versions batch taking a folder and a file in and giving a file back with their times, measured and deleted, a tree of empty folders removed and one holding a file kept, the version cleanup fenced by the marker and the recognition of what reappeared, and a plan applied onto a remote by the run's own steps; requires the engine and fails by name without it |
| `test_remotes.py` | Remote destinations up to a run: the remembered password sealed, read back, replaced and forgotten, an altered seal refused and an empty password or one with a line break never sealed (Windows); a seal met at the moment a new one is renamed over it read, found present and kept, a password on trial taken up whole, and a trial a process left removed once stale while one at work is not; the unlock command naming this copy's folder and the store, in isolated mode, and a trial's seal with `--seal`; through the pinned engine, against its local and encrypting backends and a WebDAV remote whose server does not run, in a throwaway configuration: `remotes` naming the configuration and listing its remotes, `add --remote-dest` saving schema 7 and noticing a remote that does not encrypt and no encrypting one, `jobs`, `resolve` and `validate` on it, a remote refused with no configuration or not in it, a file where its folder is named refused and resolved as one, an unreachable remote waiting with the network's reason, a locked configuration saying why, a wrong password never remembered — tried from a seal of its own, the remembered file untouched while it is tried and no trial left —, a new one whose seal cannot be read back leaving the one remembered before, a remembered one unlocking the configuration through the engine and `password clear` forgetting it, the unlock command printing the password alone and, with `--seal`, the password on trial, `password set` refused for a configuration that is not encrypted, another unlock or another configuration forgetting the password, and the facts gathered for validation; the commands' tests are skipped where the engine is not in `vendor/`, which the engine-contract module reports by name |
| `test_remote_runs.py` | Runs and previews onto a remote through the pinned engine, against its S3, WebDAV and SFTP servers on the loopback address and its local and encrypted backends, in a throwaway configuration: a first backup and the next run on every backend planning nothing again, renames, removals and replacements moving into the batch on the remote, a drive destination beside a remote one backed up as before; a held run and the preview that confirms it; the command line's words for a remote, its free space unknown and a first backup, and a confirmed preview's run; a remote whose server does not answer recorded absent, a locked configuration refused with why and unlocked by the user's command, a remote the configuration does not hold refused; a name the remote cannot keep failing alone, names the plan cannot tell apart stopping the pair, a remote that stops answering failing the pair — at its marker, a shared destination's record, its changed files' leading bytes or its space said as before any of the plan was applied, at a write said with what was done recorded — and the next run converging, a record or a changed file the remote will not serve while it answers told apart from it (the pair stopped as its record cannot be read now, the file left out of the signature check), a preview of a remote whose space cannot be asked saying the remote failed, a free space reported without a total judged; the engine's leftovers removed (local and SFTP), what reappeared after a move into versions named, a first run onto a folder holding files leaving them and sharing it; expired batches deleted on the remote and what the service may keep said (S3 and local); a scheduled run; no file the runs write holding a secret of the configuration. Without the engine: which destinations an arrival runs, the gate on a free space reported or not, what a record says the service may keep and a record older than schema 5 saying nothing, the preview's space counting the cleanup only where the service frees it, and the notice's low space at the remote |
| `test_remote_maintenance.py` | Verification and repair of a remote through the pinned engine, against its S3 and WebDAV servers on the loopback address, a local remote and encrypted remotes over a local folder and over the WebDAV server, in a throwaway configuration, no copy on the remote opened for reading where a fingerprint serves: S3 verified by MD5 and a difference planted at the source found, a difference planted on a local remote found, a pass of new copies building on a full one, a repair reading its new copy back by its fingerprint; a remote keeping no fingerprint read on both sides and repaired, a copy listed with no fingerprint read and counted; an encrypted remote compared by its encrypted form, a difference found and repaired, one over a service keeping no fingerprint read instead, many files checked in batches by files and by entries, the cursor passing each batch whole; `verify`, `repair` and `history` saying how a remote was compared; a remote that cannot be reached not verified, saying why; a remote that stops answering as its marker or a shared destination's record is read ending a verification and a repair as the remote stopping, and a record it will not serve while it answers one that cannot be used; the engine's fingerprint of a kind this program does not compute agreeing with a local remote's. Without the engine: the verification record's schema 3 and older records read as read in full, the throughputs by measure, the estimate's rates for a remote read by download, the fingerprint kind preferred, a remote destination reminded of its full verification, the engine's check read alike by a verification and a repair, and a copy that still differs on a remote in a remote's words |
| `test_remote_window.py` | The window's logic through the pinned engine, against its S3 server on the loopback address, a local remote, an encrypted remote and a WebDAV remote whose server does not run, in throwaway configurations: the editor's checks on what the engine says of a job's remote destinations; the preferences' check of a configuration and its password remembered only when it opens it, a wrong one kept nowhere; a remote's folders browsed through one session; a preview of a remote in words, reached and not; the job list and a run's details read back from real runs' records. Without the engine, the run record's schema 6: a record's round trip, older ones read and a newer one refused |
| `test_run.py` | Through the engine on temporary folders, each of the run's promised outcomes as an injected event: first run, a first backup with its confirmation (into a folder not made yet and an empty one; lapsing on a folder holding a file or an empty folder, nothing written; never unattended or with a ticket), adoption, a first run leaving what a folder holds by default and moving it when asked, the choice made after the preview, no choice where nothing is left, a confirmed first run the folder outgrew waiting whole, later runs of a shared destination (what appears beside the job's own entries left, what is put inside them mirrored, a left file the source comes to hold replaced), a shared destination whose record was lost waiting untouched for a review and, confirmed, leaving what is there, keeping the lost record in versions and making it again, and one whose record another program holds stopping untouched, folders no run touches (another job's source, the job file's folder: moving not offered, a whole destination and a source reaching the folder stopping the pair, its words saying that excluding the folder leaves it where it is; excluded — itself or a folder holding it — stopping neither and left as it is, a folder holding it staying when the source drops it while its other files move into versions, and a folder excluded only in part, or a source file where a folder holding it lies, still stopping the pair), a file as the source, a file as a destination (a first backup too) and the job file's folder as the destination stopping the run, versions batch, absent and foreign destinations, unlistable folder, held files, killed engine, the four mass removals and a lost signature held, a one-file job held at its default share and updating by itself with the share hold off, renames, pause, graceful and immediate cancel, a dead run recorded; files a run moved into versions that reappear — named by the preview where path, size and time equal their copy in the newest batch (not a copy that differs, nor one put back from an older batch), moved into versions again and counted toward the holds, the cloud client keeping a copy of the folder named — and, without the engine, the newest batch found by its name and counter and only its folders holding a removal read; runs at the same time, the choice set to let runs on one drive work together — two on disjoint folders copying together, a second run of one job and a run whose destination lies in another job's source waiting, a waiting run cancelled, a paused run blocking only the runs it conflicts with, waiters starting oldest first, two runs never overcommitting one drive's free space, a waiting run surviving the job file saved by another process, a run publishing the destinations it serves, a waiting run of some destinations keeping them when the job is reordered and refused when one is removed —; runs on one drive at the default — one after the other, each later one naming whom it waits for, the drive and its place in line, a run on other drives copying beside them, two volumes on one disk one drive, and, always one at a time chosen, two jobs on temporary folders of the system drive through this machine's real volume source; an engine making no progress for the bound ended — each figure that moves counting as progress, a pause and time asleep not counted, figures that cannot be read none, a cancel asked for staying a cancel — and one frozen from outside failing its pair, the next run completing; a first backup stopped part-way continued by the next run without asking; a run that fails outside a pair recording each destination and logging why; a tree of empty folders named with a trailing space or period removed rather than moved; names and roots with stand-ins backed up, names left out and reported, a folder holding one staying with it, an adoption over identical files, a graceful cancel ending a comparison, pause and cancel while listing (the pause asked while the run waits its turn) and while changed files are checked, a file the engine passes over; an engine outliving its core; the command line end to end; a launched run outliving the process that started it, and the arguments of one of some destinations naming them by marker; a run and a preview of destinations named by marker, each found wherever the job's order puts it, a marker the job does not hold refused with nothing run, and destinations named both by number and by marker refused |
| `test_live_sources.py` | Snapshots from a provider in memory: the decision per run (switched off, nothing to copy to, a drive that cannot have one, a drive holding others' snapshots, a failure, a snapshot made; beside others' snapshots, one made only with room to spare and none with too little or with the room unknown or unreadable; what is spare and what is wanted; the volume a WMI reference names); runs through the engine reading, judging and copying from the snapshot — onto a remote too, and no snapshot made while that remote cannot be reached —, a held file backed up through it, the snapshot kept through a pause and removed whatever the end, a dead run's snapshot removed by the next run and no other, a live run's kept, two runs making snapshots at once taking turns; a file's runs in a row (a cloud-only file never counted), records of runs started in the same second read back in order, the record's schemas, the notice; the provider's reading of Windows' answers and its administrators check; a device-path root, a device's own root included, listed and opened |
| `test_maintenance.py` | Version cleanup without the engine (batches dated by their names, only the expired deleted, a junction inside a batch removed without touching what it points at, a batch or versions folder that is a link left alone, nothing without this job's marker, read-only files and folders, the preview's measure, a stop between batches, a batch holding folders whose names end in a space or a period deleted whole) and through the engine (a run deleting and recording them, none without cleanup, the preview listing them and the space check counting them, a run's space check measuring the drive once its cleanup is done, the free space recorded and announced when low); full verification on temporary folders (a clean pass and its date, a planted corruption found, entries not compared counted, a pass continued after an interruption and after its process died, a restart, pause and stop, giving way to a waiting run, working beside a run it does not conflict with and giving way to one that writes what it reads, verifications of two destinations at the same time (the choice set so), waiting for a run on one of its drives and giving way to a waiting run on one, refusals, a second verification of one destination refused, a dead verification leaving no run record, a drive removed during a pass leaving it unfinished and continued later, a side that comes back within its patience letting the pass finish and one that stays away ending the sitting in words, a stop ending a sitting whose read does not answer, its record held by another program and the job file saved by another process while it waits not ending it, drives that cannot be checked said in words, a finished pass dated by when it began, a shared destination's job's part compared alone — as its record has it, a name the source gained and a left entry it reaches not compared — and one whose record cannot be used refused); verification of new copies on temporary folders (only what no earlier pass found the same read and the rest counted unchanged, the destination's older lists removed, a copy damaged since it was read left to a full pass and one found to differ read again, no list making it a full pass — reading every file, or continuing the unfinished full pass and saying so — a pass made before lists were kept not built on, each kind continuing its own unfinished pass, a full pass finished since ending one of new copies, what a stopped pass found the same kept for its continuation); the list of verified copies on its own (fingerprints, a list written across sittings sorted each once with a torn entry dropped, an append that failed partway written again in its place, a list another program holds waited for (Windows), a finished pass's list read from its partial list, the list a pass builds on and those removed); the kind of pass a verification record keeps from schema 2 on, a schema-2 record read with it and a schema-1 record read as a full pass; a verification record met at the moment it is renamed over still read; a pass of new copies never dating the verification; the reminder, and the destinations a run's notice reminds of (those it backed up that are due, none after a failed run); a pass's estimate counting its files beside its bytes, and the rate a pass measures leaving each file's own cost out; the run record's current schema and older ones; the notices, a pass of new copies' among them, and a verification's `history` line, one differing file counted in the singular in each; the arguments that start a verification of new copies, naming its destination by marker; a destination named by marker verified at the number the job gives it, and one the job does not hold, or named both ways or neither, refused; `verify`, `verify --new-copies`, `verify --marker` by a prefix of the marker (a prefix too short refused, neither `--dest` nor `--marker` on a job of several destinations refused, both together a usage error) and `history` in process; a verification started as a process of its own on the real volume source, one finding its destination by marker with its log numbering it, and one that cannot start saying why in one line and in its log |
| `test_repair.py` | Replacing the copies a verification found to differ: the record's form, read back beside the store and on the destination, a schema 1 record read with no copy being replaced and a later schema passed over, what of a pass is pending and whether all of it is replaced, a running record whose process ended read as interrupted, a record met at the moment it is renamed over still read; the notice by the user's choices; a waiting repair's place before a waiting verification and after an earlier run, a working verification giving way to it, a dead repair leaving no run record; through the engine on temporary folders: the copies that differ replaced, their backup copies in a versions batch and the new copies read back identical, the verification's record unchanged and a verification again finding none; a copy changed since, gone from the source, excluded now, or whose source copy lost its signature left as it is, its backup copy untouched; a second repair taking up only what the first did not replace, and none starting once every listed copy is replaced, when the latest finished pass found none, or while a verification of the destination is in progress; a later pass that did not finish leaving the latest finished one to take up, and what an unfinished pass found so far never taken up; on a shared destination what its job does not own left; a copy inside another job's source or a synchronised folder left; a copy beneath a junction left (Windows); no room failing with nothing changed; a copy the engine cannot complete (a byte-range lock, Windows) keeping its backup copy, and recorded as in versions when that cannot be moved back; a copy step that raises, an interruption (one just after a backup copy moved included) and a stop each leaving no backup copy in versions unnamed — those not copied moved back, one that cannot be recorded as in versions, a copy completed before an error recorded as not read back; a process that dies while the backup copies move or while the engine copies, and a second interruption during the clean-up, each leaving a record that names the batch and every copy not settled, those copies unsettled once the repair no longer runs — said in its `history` line, counted in versions, and pending; the source read from a snapshot (a provider in memory); a stop before its turn recording nothing; `repair` and `history` in process with their exit statuses, `repair --marker` by a prefix of the marker wherever the job puts the destination and one naming no destination's marker refused; a destination the job does not hold by marker, or named both ways, refused; a repair started as a process of its own on the real volume source, and one finding its destination by marker |
| `test_power.py` | The computer while an activity works: kept awake only while it works (not while it waits or is paused), given back at its end and nothing asked after, nothing asked with the choice off, a change of the choice reaching an activity already working once it is read again and a job file that cannot be read leaving the last choice, a request Windows refuses leaving nothing held and never asked again; what each request says; Windows itself holding the system required while a request is held (Windows); a run through the engine and a verification paused and resumed on temporary folders asking through the same seam, and a run with the choice off asking nothing; the activity named on the shutdown screen only while it works, each choice holding on its own, a change of it reaching an activity already working, a refusal of it leaving the computer kept awake and never asked again, a process with the screen switched off taking no part, on at first; a run through the engine named while it works and not named with the choice off; the screen's real window hidden, answering yes while it does not hold, refusing once closed from outside, and gone once closed, never holding the shutdown (Windows) |
| `test_tray.py` | The notification-area icon, headless: each activity's line in the Runs tab's words (a run, a verification, a repair, a run on one of several destinations, numbered as its job stands now, a run of some of them named by the destination alone; how far it has come, paused, waiting, cancelling, before a plan) and one whose status cannot be read named by its last status, or never read, offering nothing; the menu's entries worded and enabled as the row's buttons; the look of one activity and of several in line order, the fill the mean of those measured, the grey only while every one at work is paused, a waiting one empty; the tooltip within Windows' 127 code units, a line shortened and the rest counted, a character beyond the basic plane counted as two and never cut; the process ending a few seconds after the last activity, an activity starting meanwhile keeping it, looks failing for long enough ending it; the icon made again after two refusals running; the handle Windows knows Tk's icon by; the mark a main window carries naming its store; the picture's copy filling from the bottom and its tile grey, no fill drawing the program's icon; its start for a store, by an absolute path, none while another process holds the icon lock or with the switch off, and a start that fails costing nothing; the lock handed back with no activity left, kept for one published before the look, left to a process that took it meanwhile, and let go when the look fails; the process ending at once while another holds the lock, its module started as its own main; a run, a verification and a repair each bringing it up once, after its status is published, and a run whose icon cannot be started completing; reminders: a standing one keeping the icon and shown after the activities, counted in the tooltip, its tile amber over the grey of a pause and the blocks unchanged on it, the menu's dismissal worded, a reminder written before the hand-back's look keeping the icon though the statuses cannot be read, failing looks showing the standing reminders alone and ending the process only where none stands, the main window clearing them only while it has the focus; one per job, the latest replacing the one before, read back oldest first, each the job its file is named for, one that cannot be read or that another program keeps refused shown in general words without waiting, a staging file never taken for one and removed once stale, anything but a job's id refused; which notices ask, a notice that asks left before the icon is brought up, one that only tells leaving none, one that cannot be left costing the reminder alone, one Windows refused still left; a scheduled run held for review and one that could not start each leaving its reminder, a completed one and a process raising no notice leaving none, and a command-line run that did not finish leaving its job's |
| `test_automation.py` | Without Task Scheduler and without the desktop: the schedule grammar (every form of days and times, its canonical text, its words, the runs of every N hours and a window crossing midnight, the next run, a day under way, at most 48 times); the tasks a configuration calls for and their Task Scheduler definitions (a trigger for each time, one repeating across a window, a monthly day, a one-off trigger for a day under way, the daily and weekly forms' definitions unchanged); reconciliation against a scheduler in memory (registered, kept by fingerprint, re-registered, removed) and nothing registered without an installed copy serving the store; a missing task folder told apart from a failure; which drives arrived and what an arrival runs, across removals, a new session and a drive attached during a run; the scheduler's entry with a runner of its own (its notice reminding of a destination due a verification, why Windows refused its notice kept whether the run ran or could not start); a notice's words (a waiting first run asking for the go-ahead, for a review, or for the review that makes a lost record again, a count of one naming one file, a destination due a verification a line of a notice raised anyway and no notice where the user's choices keep the run quiet) and how it is handed to Windows (a name holding an unpaired surrogate included, a refusal reported in words, a notice that asks a reminder toast with Windows' own dismissal, named Dismiss, and one that only tells a plain one); the Windows Runtime taking a toast, plain or a reminder, up to its showing under either identity (Windows) — characters outside the basic plane, content that is not a toast refused at its reading, a calling thread already in a single-threaded apartment, an unforeseen failure reported as itself and a hand-over outlasting its wait told not to show; `when`, `reconcile`, `installation`; one arrival run through the engine on temporary folders |
| `test_installation.py` | Installing into a temporary folder (packages without caches, the licence, the icon drawn there, the engine, the record, the Start menu entry written there and naming the icon, the tasks in a scheduler in memory); a copy whose icon cannot be drawn installed without one and saying so; the installed copy running by itself on the store it serves; updating; a copy in use, a run in progress, the installed copy itself and another store refused with nothing changed; uninstalling, the store untouched (these require the engine and Windows); a working copy without its licence refused with nothing changed; the installation record's two kinds and schema 1, the build record (naming its label and interpreter and no commit, strict, an unreadable one still marking a release), how a copy starts itself; a portable release faked in a temporary folder (Windows): recognising itself and serving its per-user store, set up where it lies (record, entry and tasks pointing at it, nothing written into it), its entry naming the icon it ships, kept beside the job file — kept there when the release is moved, removed with the setup, the one already kept named where it cannot be refreshed — and none where it ships none, a release reached through a junction set up by that name, knowing itself by it and serving the next release put behind it with nothing set up again, a newer release re-pointing everything, a moved one read as gone with its tasks left alone, saving from a copy that is not set up, removing the setup, a run in progress refusing both, and one installation per account both ways, on the command line too; removing the setup only if this release is the one set up — removed where it is, another copy's setup left with a run in progress refusing nothing, nothing set up or a source installation's entry left as it is, a working copy refusing it, and the command line of a release not set up saying it removed nothing — and a release answering `installation` with status 0 set up here, set up elsewhere, gone or not set up |
| `test_gui.py` | The GUI's toolkit-free logic, headless: job rows and status words (files not backed up included), the editor's draft and checks (the snapshot choice and When to run included — every schedule form saved and shown again, and what a schedule refuses —, the finding that holds for every job left out; a typed folder read as the command line reads one, a typed source that must exist and a destination not made yet taken), the files-not-backed-up report, a run's details (how the source was read), the snapshot's phases in words, a preview in words — what moves into versions by folder, a folder moving whole as one line, a cloud client's notice beside its destination, what reappeared after being moved into versions grouped apart with when it was moved and the cloud client that may have put it back (a folder moving whole only partly reappeared whole on neither side) — and its run buttons (real previews on temporary folders), cancellable previews, a preview of some destinations wherever the job has put them, run status in words (what a waiting row waits for included), history (how each run was started), import, the job file at start, the session's refused overwrite and a run that cannot start, the preferences' notification choices and installation in words, a save reconciling the tasks; free space, the last verification and its reminder, copies that differ and a verification in progress in the job list; the Verify dialog, a verification in the Runs tab (its estimate naming what each file costs where it counted files) and among the history, its details; the editor's versions settings and the question before old versions are deleted; old versions in a preview and in a run's details; a large library's words: files found identical in a run's row, its details and its notice, the files being compared, the counts of the steps before the copy, a preview's same-size changed files and names left out; groups in the job list (their order, a group's status and last success), a group or one job moved within its level (a destination moving its job; nothing at an edge or with several items), what a selection of several rows allows, *Run now* running the destinations selected alone and a job or group selected whole, the questions before several jobs run (what sharing a drive means under each choice, a job run for some destinations named with them) or go and before a group is deleted, group names, the groups remembered closed and the remembered state saved alone; a cancelled run and an unfinished verification's words about starting again; first backups: the first-run words by what a folder holds, the question before a job's first backups — a run of one destination's naming it alone — and its line among several jobs, the refresher looking into a destination without a marker, the session starting a run with the go-ahead, and a run of the destinations chosen handed their markers, none for one the job no longer has, and a verification and a repair of a destination named by its marker; the editor's share hold and a preview's words for a job's own share; a first run's two answers in the preview, the destination as it would run by each, moving not offered where a folder inside is protected, the ticket for the answer given, the words for a pair stopped to protect a folder; the tone of a row whose drive is not attached (a destination, a job whose source or every destination is away, a group) and the rows asking something keeping theirs; the main window's remembered place beside the groups closed, saved alone, passed over where it cannot be used, and fitted onto the monitors; a portable release's installation in words (each state of its section, naming the program by its name, a working copy while a release is set up, the editor's line, the question at start), what a setup says once done; About in words (its first line, the copyright line as the licence file this copy ships writes it, a copy without that file, the third-party notices a release ships and a copy that ships none); the files a copy ships found at its root and read as text or not at all; the application identity the GUI's process takes (Windows, in a process of its own); what the window tells of every activity: how far a verification at work is, a pass that did not finish (stopped, interrupted, failed) with how far it got, what comes before it and the Verify dialog's reason, a run serving some destinations leaving the others theirs, a run whose job was edited while it works — a destination moved, removed or added — shown on the destination it is on by marker, and its title numbering that destination as the job stands, a run of some destinations titled by the destination alone, a start that left no record on its job's row and in History until a record follows it, a log naming its job, History of several jobs with each row's job and its empty words, the CSV it saves, the log a record opens, the closing question, a request that did not reach its run, a file not backed up shown whole, the job file held at start, the preview's line on how the source is read, the refresher reading a held record again, following the logs and keeping what it saw when a look fails, the runs read on a thread of their own (a status another program holds keeping that thread waiting, not its caller's), and an activity that ended listed as last seen at work until a look of the refresher's begun after its end succeeds; what a waiting run or verification says in the Runs tab and in the job list (whom it waits for, one whose status cannot be read now included, the drive they share, its place in line, an older copy's words, a destination its run has not reached) and the choices of runs on one drive, of keeping the computer awake and of naming the work on the shutdown screen (on at first, its words) in the preferences, each keeping the others; replacing the copies a verification found to differ: what the job list offers and says while a repair waits or works and once it has replaced them, a later pass that did not finish hiding neither the copies that differ nor their repair, the question before one and its conditional lines, what a verification's details say a repair replaced, failed on and left and what no repair reached yet, and the new advice, a repair's History row and details, its failures never called copies replaced or left, the copies a repair whose process died had not finished counted under *Skipped*, listed as failed and counted failed in a verification's details — none while it works —, its row in the Runs tab, the closing question and a request that did not reach it, a repair that could not start, and the session starting one and the refresher reading its records; a shared destination whose record was lost: its preview offering only to leave, its row and the reasons in words; the editor and an import saying why a file as the source and a folder this tool needs as a destination are refused, and a file as a destination in the editor, a preview, the job list and History; activities started together: the Runs tab in line order (an older copy's start, kept to the second, before a later one of the same second), an activity starting, and one that has asked for its turn and names nothing, in the Runs tab, the job list and the status bar's counts, the row of a live activity whose status cannot be read now, one never read called a run, verification or repair, the closing question counting each by the kind last read and the job list showing its job and destinations as last read, each row's kind and a run's place among its destinations, no counts before a plan, every start that failed in one window, a waiting row's place in line first in the job list, lists joined with "and"; verifying every destination a group, a job or several items reach and the question before it (what continues, what is left out and why, the choice for one drive); a verification of new copies: the Verify dialog's three choices, which it selects at first and what each starts, its History row and details, the job list and the Runs tab telling it, the *Verified* column counting full passes alone, and the question before several destinations answered with either (which have no list to build on, which unfinished pass each answer continues, the default answer); a job's markers read again once its run records change, and not at every look while another program holds a record; a remote destination as the window follows and sets it up: its row from the records alone (its folder, whether it encrypts, its short name, its first run, its free space, a remote not reached, what stopped a first contact), what it lets a selection do, the question before first backups, activities waiting for the rclone configuration, the preferences' section (what it would save, a command as Windows quotes it, when a password is needed, a section left as it is, a remembered password looked for again while a new one is renamed over it), the editor's checks waiting for the engine and saying what it found, the words of the history, a verification and a repair and its address, a remote's folders walked, one session for a window's length and work off the window's thread; one live path through the engine, from a job saved by the GUI session to its rows and history |

The engine-contract module, and `test_run.py`'s class about unreadable sources, drop
the backup and restore privileges in their own process before the tests about
unreadable or held files: with them enabled, rclone and the run's way of opening a
file read files an access control list denies or another program holds without
sharing. The two engine-driven modules take most of the suite's time, several minutes
each. Reds left standing deliberately, each with the change that caused it, are in
`tests/TEST_LEDGER.md`.

The GUI's widgets are not in the suite: what they promise — every window sized to its
content with its buttons inside, the status words and enabled actions of each state, the
confirmation and cancel flows, *When to run*, the notifications and the installation (a
portable release's setup and its question at start included), the snapshot choice, files
not backed up and the run details' source line, versions and verification, the grey of a
drive not attached, the divider and the window's remembered place, the history of the
selection and its saving, an unfinished verification, a start that left no record, the
status bar's looks that failed, the words of runs waiting their turn on one drive and the
choice of runs on one drive, the icon a working copy draws, moving jobs and groups, the
hints, the wheel over a dropdown and the editor keeping the size the user gives it, the
choice of keeping the computer awake and the requests that runs and verifications started
as the window starts them hold under it, replacing the backup copies a verification found
to differ with a repair started as the window starts it, a shared destination whose record was lost from its row through
its preview to the record made again, the job editor refusing a file as the source and
a folder this tool needs as a destination, and a remote destination set up in the preferences
and the editor, its folders browsed, its preview, its address and a run's details — is
checked under real Tk on the development machine, outside the suite, with every window
captured. Their logic is in the suite (`test_gui.py`). So is the icon in the
notification area's (`test_tray.py`), while its process — brought up by an activity, Windows
holding its icon, a pause and a resume through its requests, one process for two activities,
its end after the last one whether it ended or was killed, its windows' answer when the
session may end, its start inside a job object that ends its processes, and the mark a main
window carries — is measured under real Tk the same way, in a store in a temporary
folder. Real Task Scheduler tasks, a
real drive arrival and real volume snapshots are not in the suite either: they were run
by hand on the development machine, where notices handed over through the Windows Runtime
were also shown and read back from the notification centre with their text intact (the
suite takes a notice only up to its showing), and a release built by `tools/build_release.py` was
started from its `MirrorHold.exe`, set up, run through the engine, its notice shown, set up
over by a later release and removed there, its own folder unchanged throughout, its Start
menu entry and its notice showing MirrorHold's icon; a source installation made into a
temporary folder showed its icon on its entry and its windows, and its *About* the
copyright line and the licence. Releases are also checked in Windows Sandbox, a Windows
with nothing else on it, the way a person who downloads one meets them — the download
checked and unpacked, the first start and the window's remembered place, first backups
with both answers, the share hold switched off, *Set up*, a scheduled run and its notice,
a later release set up over the first through its question at start, *Remove* — under
each release's own interpreter, and then their setup programs: the earlier installed
through its pages, its folder, list entry, Start menu entry and tasks, a scheduled task's
run bringing the notification-area icon up from it, the later one refused while the
window is open and then updating it, the uninstall refused while the window is open and
then made from the list, a reinstalled copy uninstalled while the unpacked zip is the one
set up, a copy that cannot run uninstalled all the same, and a first install the program
refuses leaving nothing — by a script run before each release.
Windows Security does not run in the sandbox, which can attach no virtual disk either:
SmartScreen, Microsoft Defender and a real drive's arrival are checked outside it.

The suite passes under CPython 3.14 and under a release's own interpreter alike. A
release's interpreter imports from the release's folder alone, never from the folder it
is started in, so the installation tests that copy the packages elsewhere start
that copy with its own folder first on the import path, as any other interpreter started
there has it. A release build runs the whole suite under that interpreter (*Building a
release*).

Importing `tests/support.py` sets `MIRRORHOLD_NOTIFICATIONS=off`,
`MIRRORHOLD_SNAPSHOTS=off`, `MIRRORHOLD_SHUTDOWN_SCREEN=off` and `MIRRORHOLD_TRAY=off`, so no
test, and no run a test starts, raises a Windows notification, makes, lists or removes a real
volume's snapshot, holds up the machine's shutdown or shows an icon in the notification area;
the snapshot tests pass a provider of their own, the shutdown screen's tests switch it on for
themselves with a recorder in Windows' place, and the icon's tests pass a start of their own.
The runs, verifications and repairs the tests start ask
Windows to keep the computer awake while they work, as any does; `test_power.py` puts a recorder in
Windows' place where it checks what is asked, and opens the screen's real window only to
close it, never holding the shutdown.

The tests work on files in folders of their own in the user's temporary folder. A folder
`tests/support.py` makes is removed once its test ends, read-only entries included, and
sits beside a lock file its process holds while the folder is in use. What a removal
cannot take — a file a process the test started still holds, or the folder of a test run
that was killed — is removed by the next test run before it makes its first folder, as is
a folder of the suite made before folders had a lock file.

## Publication

The maintainer decided on 2026-10-10 that this folder is published as snapshots of its
tracked files, not as its history; under the MIT License (`LICENSE`), for Windows only;
that bug reports are taken as issues and pull requests are not accepted; that no version
promises whether it reads another version's files; and that the first publication waits
for the packaged release (*Building a release*), which the maintainer decided on 2026-10-11
is a portable zip, and on 2026-10-31 a setup program beside it that changes nothing of the
zip. A release is published from one commit by
the maintainer's own procedure: the commit's snapshot scanned for anything that must not
be published, a credential of a cloud store among it, the release built from it and checked in Windows Sandbox,
and nothing published before a reading of every comment and document it carries has found
nothing personal (the maintainer's decision, 2026-10-11). Its assets are the zip, the
setup program and `SHA256SUMS`. Migrations of the job file and readers of older record and marker schemas
are still written where they cost little.
`README.md` is the user's introduction; this file is the design. The engine is never
part of a snapshot: `tools/fetch_engine.py` obtains it (*The engine*). The pictures
`README.md` shows are in `screenshots/`: a snapshot carries them and a release does not,
so the README inside a release links pictures its zip does not hold. Each is checked by
eye before it is published, and the snapshot's scan refuses a PNG carrying text or other
metadata the eye cannot see.

## Caveats

- The run is shaped by the engine's measured behaviour, pinned by
  `tests/test_engine_contract.py`: a changed source file that cannot be opened has its
  destination copy moved into the backup directory before the copy fails (so changed
  files are opened the engine's way first, and moved back if a copy still fails); any
  read error skips every deletion and an unlistable source directory reads as
  destination-only entries (so the engine is never asked to delete); a case-only
  rename is not propagated (so the core renames by case); `--max-delete` counts no
  overwrites and stops part-way (so the gate judges the plan); a temporary file left by
  an interrupted copy is otherwise treated as data (so the core removes it).
- With the backup privilege enabled in the launching token, the engine reads files that
  an access control list denies or that another program holds without sharing, so a
  long-held file is backed up rather than skipped (measured on the development
  machine). A scheduled task's launch does not have it enabled (measured): read as it
  is, an automatic run skips a file another program holds without sharing, keeps its
  previous copy and reports it. A scheduled task's run can make a volume snapshot
  (measured), and reads such a file from it. Which privileges a GUI the user starts by
  hand holds is unmeasured.
- A volume that holds snapshots this tool did not make gets a snapshot only while the room
  Windows keeps for snapshots there has room to spare, and is read as it is otherwise. The
  spare is a margin, not a guarantee: a run's snapshot that grows past it while the run
  reads — the system's own writes to the volume, a destination on it — still has Windows
  delete the oldest snapshots, the user's among them. A system drive that keeps Windows'
  restore points is such a volume, and where their room is full a source there is read as
  it is. A pooled volume refused a snapshot (a StableBit DrivePool pool, measured once on
  the development machine: VSS answered that there was not enough room, and nothing was
  made), so a source on one may be read as it is too.
- A destination on the source's own volume adds what it writes to the snapshot's room
  on that volume while the snapshot lasts.
- Scheduled tasks run only while the user is logged on; an arrival before logon is
  served by the logon trigger. A source installation's tasks start its copy with the
  interpreter the working copy ran under: moving that environment breaks them until the
  next install. A portable release's start the interpreter it carries: moving or deleting
  the release's folder stops them until a copy is set up again.
- Until a copy is installed, or a portable release set up, notifications appear under
  Windows PowerShell's name.
- A notice Windows has not taken within 30 seconds is reported as not shown; it appears
  afterwards only if Windows had already begun showing it.
- Windows keeps its settings for a notification-area icon — whether it shows on the taskbar
  or among the hidden icons — under the executable that shows it and the icon's number
  (measured on the development machine, 2026-10-31). Run from source, MirrorHold's icon is
  shown by the base interpreter's `pythonw.exe`, which a virtual environment's starts, as
  Tk's first icon: it shares one such setting with any other program that shows a Tk icon
  under that interpreter, and the entry keeps the tooltip of whichever showed first. A
  release shows its icon through the interpreter it carries, which has an entry of its own:
  in Windows Sandbox (2026-11-02) Windows recorded one for an unpacked release's
  `runtime\pythonw.exe` with that release's tooltip and no `IsPromoted` value — the one
  Windows sets for an icon shown on the taskbar itself — so the icon starts among the
  hidden icons until the user shows it; an installed copy's icon, which lived about four
  seconds there, had no entry recorded within the check.
  What the icon does when the notification area restarts is not measured. A release's Tk
  (9.0.4) has the tray command the icon is drawn through (measured on a release build).
- A copy is known by the path it was reached by: a release set up through a package
  manager's junction stays set up there, while the same release opened from the
  versioned folder the junction points at is another copy, and asks to be used instead.
- A set-up release's Start menu entry names the copy of its icon kept beside the store: a
  release moved or deleted after it was set up leaves the entry starting nothing, as it
  leaves its tasks without a copy, until a copy is set up again, but with MirrorHold's
  icon. A source installation's entry names the icon in its copy; one whose icon could not
  be drawn says so when it is installed, and its entry and notices show the interpreter's
  icon.
- The release zip holds its files at its top level: *Extract All* puts them in a folder
  named after the zip, but another program's *extract here* spreads them into the folder
  the zip lies in.
- A release in a folder whose path is past 209 characters runs by the folder's 8.3 name, so
  it knows itself by that name and a setup made from it names that name; whether its Start
  menu entry and its tasks then start it, and what a double-click in Explorer starts, is
  unmeasured. On a drive that keeps no 8.3 names, or none short enough, it cannot run
  from such a folder at all.
- A release's `MirrorHold.exe`, its pywin32 libraries and the engine carry no signature
  (the interpreter, the rest of CPython and Tcl/Tk carry the Python Software
  Foundation's): Windows SmartScreen warns before a downloaded `MirrorHold.exe` first
  runs — *Windows protected your PC*, naming the application and an unknown publisher
  under *More info*, beside *Run anyway* (measured on the development machine) — and a
  computer that admits only signed programs may refuse them. What Microsoft Defender's
  antivirus makes of a release is unmeasured. The setup program carries no signature
  either, and what SmartScreen and Microsoft Defender make of it is unmeasured: Windows
  Security does not run in Windows Sandbox, where the setup program was checked.
- Uninstalling a copy that cannot run — its program package gone, say — removes its folder
  and its list entry but cannot ask it to remove its setup: where it was the copy set up,
  its Start menu entry and tasks stay, starting nothing, until *Remove* in another copy
  takes them away, as the uninstaller's message says (checked in Windows Sandbox,
  2026-11-02).
- The setup program sees a copy at work only through the program files a process of it has
  loaded, as every process MirrorHold starts from a copy has. A folder another program
  holds so that it cannot be moved aside stops an update all the same, before anything
  changes, the setup program saying so.
- A release's interpreter reads its search path from its path file: it imports the
  packages from the release's folder only, whatever folder it is started in.
- A listing keeps every entry of its side in memory: about 370 bytes a file for a
  46-character path (measured on 200,000 synthetic entries), a third of a gigabyte per
  side for a million files. A full verification's pass adds about 20 bytes a file to its
  two listings (measured on 500,000 files a side): it makes its entries one at a time. A
  pass of new copies holds its baseline as well, 8 bytes for each copy that list holds, and
  every pass sorts its own list in memory once, as it ends, after its listings are released.
- A pause lasts within the session; a shutdown while paused is recorded as an
  interruption by the next run.
- Activities on one drive take turns there unless it reports no seek penalty, is taken for
  a cloud drive, or the user marked it a solid-state or cloud drive, and each
  holds every drive it claimed until it ends: a run whose destinations lie on two drives
  it takes turns on keeps both from other activities while it copies to the first, and one
  run's destinations are never interleaved with another's there. Allowed to work at the
  same time, two activities on one drive share its rate: on a spinning disk each is slower
  than it would be alone.
- A drive that does not report whether it incurs a seek penalty is taken for a hard disk
  under *Automatic*. Measured on the development machine (2026-10-21), a USB disk behind an
  enclosure or a dock may not report it, and *Automatic* then takes turns on that drive — a
  USB solid-state drive included — until the user marks it a solid-state drive in the
  preferences. A volume Windows names no disk for whose device does not answer as a
  disk file system — a volume spanning disks, one whose device cannot be asked, another
  cloud client's drive that answers otherwise — is taken for a hard disk too, until the user
  marks it. A disk known only by its number cannot be marked, and a disk
  partitioned anew loses its mark. Two drives that share a name, a size and file systems
  look alike in the preferences.
- A disk whose layout cannot be read is named by its number, which Windows hands out as
  disks attach: two activities whose looks fall either side of a renumbering can take such
  a disk for two, or two such disks for one.
- A run claims the destinations present when it is admitted; a drive attached after
  that is served by a later run.
- A run or verification of an older copy of the tool publishes no claim, so this one
  treats it as conflicting with everything; the older copy's runs know nothing of this
  one's and wait only for their own global lock. A copy whose claims name no drives is
  compared by its folders alone, whatever the choice, and judges by folders alone itself.
  A copy whose claims name drives without their kind judges by its own one choice, taking
  turns on every drive it shares or on none — a solid-state drive included — and reads this
  copy's claims by the one choice they still publish. A copy that knows no cloud drive
  reads one in this copy's claims as a drive of a kind not known, and takes turns there.
  After updating, install again before scheduled tasks start runs.
- A job file this version saves is schema 9 (a drive may be marked a cloud drive), and so
  is an export, though it carries no preferences and so holds what it held before. A
  schema-8 file reads unchanged; schema 8 lets a schedule run monthly, at several times a
  day or every N hours. A schema-7 file reads unchanged. Schema 7 lets a destination be
  a remote's folder and the preferences name the rclone configuration. A schema-6 file reads with no configuration named; a
  schema-5 file reads with no drive marked; a schema-4 file's one at a time, on, reads as
  *Automatic* — the file cannot tell the first value from a choice made — and off as
  *Always at the same time*. A copy that reads an earlier schema — an installed copy built
  before it — refuses either, leaving it untouched: its scheduled and arrival runs fail and
  its window opens read-only until MirrorHold is installed again. The file keeps its schema
  until this version first saves it.
- A remote destination is part of the job contract, checked when a job is made, validated,
  resolved, previewed, backed up, verified and repaired. What the window shows of one comes
  from the run records, so a marker that went wrong on the service since shows only when an
  activity reaches it. A remote that wraps another — an encrypting remote, an alias, a
  union — may lie over a folder another destination names through that other remote; what
  it lies over is in the configuration, which is never read, so the two are not refused as
  overlapping, and the marker, the holds and the first run's question still stand between
  them.
- A run record this version writes is schema 6 (whether a remote it reached encrypts),
  whatever its destination. An installed copy built before it passes such a record over, as
  it does any record it cannot read: its window, its history and its reminder see only the
  runs made before, until MirrorHold is installed again. A record of schema 5 reads here with
  nothing said of encryption, the list then saying `(remote)` alone, and one of schema 4 or
  older with no service's keeping said either.
- A verification record this version writes is schema 3 (how the pass compared its files:
  its measure, the fingerprint kind and the files read in full). An installed copy built
  before it passes such a record over, as it does any record it cannot read: its window and
  its reminder see only the verifications made before, and its *Verify...* begins a pass
  anew rather than continuing one this version left, until MirrorHold is installed again. A
  schema 1 record reads here as a full pass and one before schema 3 as read in full; an
  unfinished one this version continues is saved as schema 3 from then on.
- A repair record this version writes is schema 2 (the copies a repair is replacing). An
  installed copy built before it passes such a record over, as it does any record it cannot
  read: its window and the repairs it starts see only the repairs made before, until
  MirrorHold is installed again. A schema 1 record reads here with no copy being replaced.
- A look at the runs finds each by its liveness lock as well as by its status: a held lock
  whose status is not there is looked for again for 0.2 s and is then an activity alive
  whose status cannot be read, which holds back every other. An ending activity removes its
  status first and its lock last, waiting up to 5 s on each file between that a reader
  holds, so for that moment it reads so — the Runs tab and `status` may list an activity
  whose status cannot be read, the window naming it from the last status it read of it, the
  job list showing its job at work and the status bar counting it — and the next look after
  its lock goes finds nothing.
- Whether a first run's folder holds anything is read by the window when it reads the
  marker, at most a minute before the question it asks; the run reads it again, so a
  folder that gained content in between waits for a review rather than being adopted.
- A destination's first run by this version writes a marker that names its schema. An
  installed copy built before markers named one reads such a marker as unreadable and
  stops at that destination until MirrorHold is installed again. Markers already on a
  drive are never rewritten, so they read alike in both.
- A shared destination's marker is schema 2: an installed copy built before shared
  destinations reads it as unreadable and stops there, untouched, until MirrorHold is
  installed again; a destination that is its job's whole keeps schema 1. A shared
  destination whose record was lost — missing, not a record, another's — is a first run
  again at its next run, which leaves what is there and makes the record anew once the
  user confirms it: what the job had backed up there whose source copies are gone since
  is left from then on, as everything a shared destination leaves is — the price of no
  longer knowing what was the job's. Moving what the folder holds is not offered then;
  removing the marker by hand makes the next run a plain first run, offering both answers.
  A record another program holds stops the run untouched until it can be read.
- A verification reads a shared destination's record and never updates it, and compares
  only what that record says the job has backed up: a top-level name the source gained
  since the last run, and a left entry the source holds something at now, count as
  missing — not backed up yet — until a run claims them, and what the destination holds
  there, perhaps the user's own, is not compared.
- The folders Windows and this tool need are read from the process's environment
  wherever a job is checked: made, edited, imported, previewed or run. A destination that
  is itself one of them — a user profile chosen as a destination — is refused; a folder of
  its own inside one is allowed, and a destination holding one is shared, its runs never
  writing into it.
- Saves of the job file are serialised by its lock, and the window and the command line
  each refuse to save over a file changed since they read it; restoring a previous copy
  saves over whatever is there. A run works with the store as it read it when it was
  admitted, so an edit made during a run applies to the next one.
- Another program that keeps a shared file open without sharing it for longer than the
  exchange's patience — seconds — still makes that read or save fail: the job file is
  then reported busy and a request as not written, and a verification's progress waits
  for its next save.
- As an activity ends, its job is listed as at work until the drives have been looked at
  again, usually a fraction of a second; while they cannot be checked, the job stays
  listed so, *Run now* does not start it, and the status bar says since when the drives
  and records have not been checked.
- A hint opens only under the pointer: the keyboard reaches none, so a window keeps on its
  face everything a choice needs, and a hint only explains.
- The window tells a start that left no record only from its log: a start whose log has
  been removed by the logs' bound, or a process that ended without a word on a system
  other than Windows, shows nowhere; History still lists every record. A job's row reads
  `Verifying` while a verification of one of its destinations works, the share on the
  destination's row alone.
- The preview's line on how the run reads the source asks Windows nothing: a source on a
  drive that keeps restore points reads `from a snapshot ... when it can make one`, and
  the run, finding that drive's snapshots and too little room to spare beside them, reads
  it as it is and says why in its records.
- The GUI's preview holds both sides' listings in the window's process while its
  window is open. A job file from a newer version cannot be read, so the read-only
  window lists no job (runs in progress can still be watched and controlled). The GUI
  is system-DPI-aware: on a second monitor of another scale Windows stretches it.
- No MBR volume has been measured: the MBR identity, and an MBR disk's name as a drive,
  are exercised through their decoders only.
- Where a destination is refused or a snapshot tried, a cloud drive is recognised by having
  no disk partition behind it; one presented as a partitioned virtual disk — StableBit CloudDrive presents one — is
  not: it is read as an ordinary disk, which may be a destination, on which a snapshot is
  tried, and which is a drive of its own, whose kind for *Automatic* is what that disk
  reports unless the user marks it a cloud drive. What such a disk reports of itself is
  unmeasured. For *Automatic*, a drive is taken
  for a cloud drive only when Windows names no disk for it and its device answers as a disk
  file system, as Google Drive for desktop's does (measured on the development machine,
  2026-11-01); no other cloud client's drive has been measured.
- A real cloud-only placeholder has not been measured; the offline attribute was
  simulated, and the recall attribute cannot be set from user mode.
- Google Drive for desktop's list of folders is a database of its own, undocumented, read
  as measured on Drive 128 on the development machine (2026-10-28): a folder from this
  computer is a row naming its volume by its GUID, so a changed drive letter still finds
  it. My Drive's folder in mirror mode is told by the row's My Drive flag, which is
  unmeasured; My Drive in streaming mode is a virtual drive, which cannot be a destination
  anyway.
  A Drive version that changes the table is said as a list that could not be read. A
  cloud client that neither registers its folders with Windows nor is Google Drive is not
  seen: a destination inside its folder is neither refused nor noticed, and that client
  may write back into it.

- A first run onto a library whose earlier copy did not keep the files' times — same
  sizes, other times — has the engine read every such file on both sides before it only
  dates it: for a large photo library that is hours of reading, once. The run counts each
  file so compared as done, so its progress moves; the next run finds them unchanged.
- The gate counts a changed file of the same size as bytes to copy, though it may prove
  identical and copy nothing.
- A name a run cannot keep is not backed up and is reported, every run: one holding `‛`,
  one of fullwidth full stops alone or holding an unpaired surrogate, and one ending in a
  space or a period — the engine carries the last, but this tool opens, renames and
  creates through ordinary Windows paths, which shorten it. Listings read every folder as
  written (its extended-length spelling), so a source folder whose name ends in a space or
  a period is listed and each file in it is reported by name — whatever the interpreter
  normalises: measured, CPython 3.13 and later would otherwise read such a folder as
  missing, and 3.10 one ending in a period. A full verification reads such files through
  ordinary paths, so it may count them missing or unreadable.
- Before `--local-encoding None`, a destination root whose folder name held fullwidth
  punctuation was written into a sibling folder named with `‛` inserted; such a sibling
  holds an earlier copy and is the user's to delete. Source names with fullwidth
  punctuation were passed over, so they are copied by the first run since.
- A versions batch is dated by its name, when the pair that filled it began to be applied
  (or the repair its copies), so a long run's batch ages from that moment.
- A verification reports a copy that differs and repairs nothing: it cannot tell which
  copy is the damaged one, and copying the source over the backup could replace a good
  copy with a damaged original. A repair does it on the user's word alone: it keeps every
  backup copy it replaces in versions — which the job's version cleanup deletes after its
  days — and leaves as it is only a copy whose source lost the file-format signature its
  backup has, not one whose source is damaged in any other way.
- A repair takes up what the verification listed, its first 1,000 copies that differ: a
  pass that found more leaves the rest to a verification started again from the beginning.
  It replaces only a copy still the size and time the verification compared; one changed
  since is the next run's to copy.
- A copy of the tool older than repairs knows a repair only by its claim: its
  verifications do not give way to a waiting repair, it does not put a waiting repair
  before its waiting verifications, and should a repair's process die, it records that as
  an interrupted run of the destination. That matters only when two copies of different
  versions serve one job file.
- A verification reads the source as it is, never from a snapshot (a pass can outlast a
  computer's session): a file another program holds without sharing is reported as not
  compared. A pass that continues compares only the files after its cursor; a file
  added before the cursor since is left to the next pass, and not counted, and the files
  it found to differ stay listed — dealt with since or not — until a pass starts again.
- A verification of new copies trusts its list for what it leaves unread: a copy damaged on
  its drive since a pass read it, or one another program replaced with a copy of the same
  name, size and time, is found only by a full verification, which the reminder keeps
  asking for. A file renamed — at the source, or by a run that matches it by size and
  time — is new to the list and read again, so a folder renamed is read again whole. A
  destination whose every verification was made before lists were kept has none, and its
  first verification of new copies reads every file, as a full one. A list lost or
  unreadable costs nothing but that: the next pass of new copies reads every file.
- A fingerprint is 64 bits, so a new or changed file can by chance take one its baseline
  holds and be left unread as unchanged: the chance is about the files a pass compares
  times the files its baseline holds, over 2^64 — one in some 1.8 × 10^9 passes of ten
  thousand new copies against a list of a million files.
- The verification reminder reaches a notice only through a run that backs the
  destination up and raises a notice anyway: a destination no run backs up, or whose runs
  the user's choices keep quiet, is told in the GUI alone.
- The computer is kept awake only while an activity works: one waiting for its turn, or
  paused by the user, holds nothing, and the computer's own timer may put it to sleep then.

## Future direction

Every part of the original design is built, and so is the portable release for users
without Python (the maintainer's decisions of 2026-10-10 and 2026-10-11): a zip, unsigned,
holding the tool, CPython's embeddable package with Tk and pywin32 beside it, and the
pinned engine, built from a commit by `tools/build_release.py` (*Building a release*). A
release recognises itself by its build record, keeps its runtime files in
`%LOCALAPPDATA%\MirrorHold` — outside its own folder, which a newer release replaces — and
is set up where it lies. Beside the zip, a release's setup program installs the same files
for the user alone, sets them up and lists them among Windows' installed apps, and a newer
one updates them in place (the maintainer's decision of 2026-10-31); it makes no third kind
of copy, so a copy it installed is served exactly as an unpacked release is.
