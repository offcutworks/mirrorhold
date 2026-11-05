"""
MirrorHold — one-way backup of local folders and drives onto backup drives.

Background
----------
A job copies one source to one or more destinations, one way, when a backup drive
is attached, on a schedule or by hand. A plain mirror is not a backup: it carries an
accidental deletion, an encrypted file or an empty mis-mounted source straight into
the copy. The tool's value is therefore the safety around the copy — identity
checks, holds, versions — and the copy itself is delegated to one pinned rclone
binary. The governing rule: nothing inside a source is ever changed, and nothing on
a destination is destroyed except by the user's opt-in version cleanup; every
removal or replacement on a destination is a move into that destination's versions.

Structure
---------
    mirrorhold/
    |-- __main__.py        `python -m mirrorhold` -> cli.run
    |-- cli.py             Command line: volumes, jobs, add, remove, validate,
    |                      resolve, export, import, engine; preview, run, verify,
    |                      status, history, pause, resume, cancel; when, install,
    |                      uninstall, installation, reconcile, automatic.
    |-- locations.py       The one place runtime paths are decided, and which kind of
    |                      copy this code is (a working copy, the source installation's
    |                      copy, a portable release): Config/, vendor/, installed/, a
    |                      portable release's store in %LOCALAPPDATA%, RuntimeLayout
    |                      (everything beside a store), and the files a copy ships
    |                      beside its packages: the licence, the notices, the icon.
    |-- icon.py            The program's icon, drawn from its geometry: written as a
    |                      file by the build and the installer, drawn by a working
    |                      copy's windows, which ship no file, and by the
    |                      notification-area icon, its copy filled with the progress.
    |-- invocation.py      How this copy starts its own core and GUI: interpreter and
    |                      arguments, for runs, scheduled tasks, the Start menu entry
    |                      and the notification-area icon.
    |-- tray.py            An activity bringing up the window unit's notification-area
    |                      icon, one process per store, as a courtesy.
    |-- reminders.py       What a finished activity left for the user to look at, one
    |                      per job, kept until the user opens the window.
    |-- exchange.py        The one way a file the core's processes share is written and
    |                      read: readers share deletion, writers rename over with POSIX
    |                      semantics, another program's hold borne for a bounded patience,
    |                      a file only ever renamed over looked for again for a moment.
    |-- power.py           What a working run or verification asks of the computer: kept
    |                      from falling asleep by itself and the work named on Windows'
    |                      shutdown screen, as the user chooses.
    |-- contract_errors.py ContractError: a contract breach located at its field.
    |-- wording.py         A count with what it counts, and the word that agrees with
    |                      it: one rule for the command line, the records, the notices
    |                      and the window.
    |-- endpoints/         Where data lives: volume identity, endpoints, the
    |                      present volumes, resolution, markers, what a shared
    |                      destination's job owns, cloud sync roots, the folders
    |                      Windows and this tool need.
    |-- jobs/              What a job and a group of jobs are (the contract), the
    |                      configuration store, creation-time validation, the source
    |                      survey, the built-in classifications and exclusions of entries.
    |-- storage/           The one place that reaches a destination's files: a folder
    |                      of this computer, or of a remote through the engine.
    |-- remotes/           The user's rclone configuration as it is reached: its unlock,
    |                      the remembered password, what the engine says of a remote
    |                      folder, the capability floor.
    |-- planning/          What a pair's run would do: listings, the plan, a shared
    |                      destination's part, file signatures, the duration estimate.
    |-- run/               The safety gate, confirmation tickets, applying a plan,
    |                      run control, the runner and preview of a job, and the
    |                      one way a caller starts a run process (launch).
    |-- records/           Run records, live status and its control channel, admission
    |                      (which runs and verifications work at once), locks, the
    |                      diagnostic log.
    |-- snapshots/         Reading a live source from a volume snapshot: the provider
    |                      seam, Windows' through WMI, the snapshots runs made, the
    |                      decision per run and the removal of what a run left.
    |-- maintenance/       Version cleanup (expired batches, deleted inside a run),
    |                      full verification (its records, the reminder, and the
    |                      verification process itself).
    |-- automation/        Jobs that run by themselves: Task Scheduler tasks derived
    |                      from the jobs and reconciled, what arrived, the scheduler's
    |                      entry.
    |-- installation/      The installed copy tasks and the Start menu entry start: the
    |                      source installation, or a portable release set up in place.
    |-- notifications/     The notice a run raises when it ends, and the reminder one
    |                      that asks something of the user hands on.
    |-- engine/            The engine adapter; the only code that knows rclone.

Architecture
------------
Three units with one dependency direction: GUI -> core -> engine (an external
process). This package is the core; it has no GUI dependency, and the GUI
(``mirrorhold_gui``, beside it) imports it to validate, to preview and to read
state, while every run executes in a core process of its own (``run.launch``). The
configuration (``jobs.model``) is the seam between the two: the core never trusts
that the GUI validated, and re-validates a job whenever it loads one. Inside the
core, decomposition is by sub-domain, never by layer.

A run lists each side of a pair once, plans, has the plan judged by the safety gate,
and applies exactly what the gate allowed: the core makes every rename and every move
into versions itself, and the engine copies an explicit list of files, so the engine
never deletes and never lists. A pair's first run waits for the user: a preview's ticket
confirms it, or, onto a folder that holds nothing, the user's go-ahead for a first
backup, which needs no preview and lapses should the folder hold something by the time
the run lists it. What a folder already holds and the source does not is left where it
is, by that first run and every later one, unless the user chose to move it into
versions: the destination is then shared, its job owning only the top-level names it put
there (``endpoints.shared``, ``planning.sharing``), and a folder no run may touch inside
it — a synchronised folder, another job's source, one Windows or this tool needs — leaves
no other choice. Callers watch and control a run through files beside
the store (``records.status``), never through the engine. A run reads its source from a
snapshot of the source's volume made for it when it can (``snapshots``), as it is
otherwise, and records which and why; a file it could not back up is counted run by
run, and one left unbacked-up twice in a row is brought to the user's attention. When
the job has opted in, a run deletes a verified destination's versions batches older
than the job's days — the one permanent deletion — and every run records the free
space it left (``maintenance``). A full verification, a manual action, reads both
copies of every file of a destination and compares them, in a core process of its own
that gives way to a waiting run it holds back. Runs and verifications work at the same
time unless they conflict — over a folder one of them writes, or over a drive both use
that they take turns on: by the user's choice, automatically a drive that reports a seek
penalty or does not say what it is (``endpoints.volumes.DriveKind``), unless the user
says what it is (``jobs.model.DriveMark``) — and wait their
turn otherwise (``records.admission``). While one works its process keeps
the computer from falling asleep by itself and has Windows' shutdown screen name it and
offer to cancel the shutdown, unless the user chose otherwise (``power``).

The core's processes and the window share no memory, only files: beside the store the
job file, each activity's status, control file and liveness lock, the records and the
tickets; on a destination its marker and a shared destination's record. Every such file
is written and read through ``exchange``, so that a process replacing such a file and
another reading it never refuse each other, and another program holding it is borne
for a bounded patience before the caller is told. Every activity's end carries a reason
a person can read, and what a run or verification process says, with any failure the
code did not foresee, goes to its diagnostic log beside the store
(``records.diagnostics``), whose marks the window reads back to show an activity that
left no record of its own.

A job runs only when it is started unless the user switches on its automatic runs. Those
are Windows Task Scheduler tasks that the core alone registers, derived from the jobs
whenever they change (``automation``); each starts the installed copy's core
(``installation``) — the source installation's copy or a portable release set up where it
lies — never a working copy, and nothing of the tool starts at sign-in or stays resident
beyond the notification-area icon of a reminder the user has still to look at. How a copy
starts itself again, for a run, a task, the Start menu entry or the notification-area icon,
is decided in one place (``invocation``). Every run's process raises one notice when it ends
(``notifications``), which also says when a destination it backed up is due a full
verification.

While a run, a verification or a repair works, an icon in the notification area shows it
and offers the window, pause, resume and cancel. The icon is the GUI's
(``mirrorhold_gui.tray_view``), in a short-lived process of its own, one per store, which
each activity brings up once its status is published (``tray``) and which ends a few
seconds after the last activity: the core starts it by its module's name and imports
nothing of the GUI, and an icon that fails costs the icon alone. An activity whose notice
asks something of the user — a review, or a look at what failed — leaves a reminder
(``reminders``) and brings the icon up again; the icon then stays, showing it, until the user
opens the window, which clears the store's reminders (the maintainer's decision, 2026-11-03).

Design patterns
---------------
- Injected volume source: everything above ``endpoints.volumes`` asks a
  ``VolumeSource`` for the present volumes, so identity, resolution and validation
  are exercised against ``FixedVolumeSource`` in tests and ``WindowsVolumeSource``
  in production.
- Strict documents: every key required, unknown keys refused, errors located by
  field path; schema evolution goes through the store's numbered migrations.
- Stable finding codes: validation reports codes, so callers and tests never bind
  wording.
- Courtesies, never conditions: keeping the computer awake, the shutdown screen
  (``power``), a notice (``notifications``) and the notification-area icon (``tray``) each
  leave the activity as it would have been when Windows refuses them; each has an
  environment switch the test suite sets off.
- Single resolution points: runtime paths and the kind of copy (``locations``), how a
  copy starts itself (``invocation``), how a file processes share is written and read
  (``exchange``), engine invocation (``engine.rclone``), the built-in entry
  classifications (``jobs.exclusions``).
- Every rule is stated in full where it binds. Where a rule was the maintainer's choice, a
  comment says so with the date ("the maintainer's decision, 2026-10-15"), and the rule
  beside it stands on its own.

Caveats
-------
- A working copy's runtime files must live under ``Config/`` (or be named ``*.log``):
  only there does the project's ``.gitignore`` keep them, and the user's paths they
  carry, out of version control. A portable release writes nothing into its own folder;
  its runtime files sit beside its store in ``%LOCALAPPDATA%\\MirrorHold``.
- Volume metadata is read without administrator rights; a volume whose file system
  cannot be read (a locked BitLocker volume) is reported unavailable, which every
  rule treats as absent.
- The engine's measured behaviour, which the run is designed around, is pinned by
  ``tests/test_engine_contract.py``. Among it: a changed source file that cannot be
  opened has its destination copy moved into the backup directory before the copy
  fails (so the run opens each changed file the engine's way first); any read error
  skips every deletion of the run and an unlistable directory reads as
  destination-only entries (so the run never lets the engine delete); a case-only
  rename is not propagated (so the run renames by case itself); ``--max-delete``
  counts deletions but not overwrites and stops part-way (so the gate judges the plan).
- With the backup privilege enabled in the launching token, the engine — and the
  run's way of opening a file — read files that an access control list denies or that
  another program holds without sharing. A scheduled task's process does not have it
  enabled; reading from a snapshot is what backs up a held file there.
- A volume that holds snapshots the tool did not make (Windows' restore points) gets a
  snapshot only while its room for snapshots has room to spare
  (``snapshots.reading.room_to_spare``), and is read as it is otherwise, so that a
  snapshot of it does not cost the user one of theirs.

- A full verification reports a copy that differs and repairs nothing: it cannot tell
  which copy is the damaged one. Replacing the backup copies it found with the source's
  is the user's own act, a repair (``maintenance.repairer``), and every copy it replaces
  moves into versions first.

Future direction
----------------
Every part of the original design is built.
"""

__version__ = "1.0.0"
