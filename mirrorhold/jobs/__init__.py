"""
Jobs — what a job is, where jobs are kept, and which jobs may exist.

Structure
---------
    model.py       The contract: Job, Source, Destination (a drive's folder or a
                   remote's), Triggers, HoldThresholds, VersionCleanup,
                   DefaultExclusions, Group, Preferences, Notifications, Activities
                   (DriveTurns and DriveMark among them), RemoteSettings (Unlock),
                   DestinationState;
                   strict document conversion;
                   new_job(), new_group().
    store.py       ConfigStore and StoreContents: jobs and their groups, atomic save
                   refused over a change made since the file was read, previous
                   copies, corrupt file moved aside, newer schema refused, migrations,
                   export and import (JobList, merge_imported()).
    schedule.py    The schedule grammar (days — daily, weekly, monthly — then times — a
                   list, or every N hours in a window), its canonical text and when its
                   runs fall.
    validation.py  validate_job(): refused / warned / noticed, by stable code — a
                   remote destination from what the engine said of it
                   (``remotes.facts``); contained_folders(): what lies inside a
                   destination that its runs never write into.
    survey.py      survey_source(): file count, total and largest size, from
                   directory listings alone.
    exclusions.py  Built-in classifications of entries (drive-root artefacts,
                   cloud placeholders, links), the switchable exclusion categories
                   (sync-client artefacts, transient editor files), the user's
                   patterns, and EntryFilter, which applies a source's exclusions.

Architecture
------------
The model is the cross-layer contract between the GUI and the core. Field names and
encoding are this package's; the vocabulary — what a user can set per job — is
fixed by the maintainer's decisions and changes only by another of them. The
store holds jobs, the groups they are arranged in and preferences together in one
file; per-destination state is not configuration and is not stored there. A group is
part of the contract, so the job file and an export carry it, but nothing in the core
acts on it: a run, a verification and a scheduled task each concern one job.

Design patterns
---------------
Immutable values with validation in ``__post_init__``; documents decoded strictly
and errors located by field path; numbered migrations; validation context passed in
(volumes, sync roots, other jobs, the folders Windows and this tool need, how to tell a
file) rather than read from the system — ``ValidationContext.on_this_computer`` is the one
place a caller has it read from this computer.

Caveats
-------
- Creation-time checks that need a volume run only while it is present, and the two
  that need the source's size run only with a survey; the run re-checks both.
- A new job runs only when it is started: neither automatic trigger is set by default.
  ``Triggers.schedule`` is checked against the schedule grammar and kept in its
  canonical text, so an unreadable schedule is a contract error at its field.
- Validation goes beyond the plain overlap rules in two places, each derived from the
  rule that nothing inside a source is changed and from the pool being written only
  through its own drive: a destination may not be another job's source (nor a source
  another job's destination), and may not sit anywhere on a DrivePool member disk.
- Another job's source or a cloud sync root strictly inside a destination is allowed and
  noticed: what protects it is how the destination's first run treated what it held,
  recorded on the destination (``endpoints.shared``), which creation cannot see. So the
  run enforces it (``run.runner``): such a destination's first run only leaves what it
  holds, and a run stops at it rather than write into the folder, unless the job's
  exclusions hide the folder from both sides. A destination that is itself a folder
  Windows or this tool needs is refused instead: leaving what it holds would not keep its
  runs out.

- Per-destination state is derived from the run records and the marker
  (``records.record.destination_state``), not stored with the jobs.
- The rules that span groups and jobs — a job in at most one group, every member a
  stored job, no two groups of one name — are checked whenever store contents are built,
  so a file breaking one is corrupt; removing a job removes it from its group.
- A job file of schema 9 (a drive may be marked a cloud drive), and an export of schema 9
  though its content did not change, are refused by a copy of the tool that reads an
  earlier schema — left untouched, never moved aside: an installed copy built before it
  stops running jobs until it is installed again. The step from 8 changes nothing, every
  earlier mark being a hard disk's or a solid-state drive's. Schema 8 lets a schedule run
  monthly, at several times a day or every N hours; the step from 7 changes nothing, the
  daily and weekly schedules keeping their text. Schema 7 lets
  a destination be a remote's folder and has the preferences name the rclone
  configuration; a drive's destination is written as it was in schema 6, so the step from
  6 only adds the preferences' remotes, none named. Schema 4's one at a time on a drive,
  on, migrates to automatic turns by the drive's kind — a file cannot tell that first
  value from a choice made — and off to always at the same time; schema 5 migrates to no
  drive marked. A drive's mark is kept by the name the drive itself carries
  (``DriveMark``), so it follows a disk from letter to letter and port to port, is lost
  when the disk is given a new layout, and is never kept for a disk known only by the
  number Windows hands out as disks attach. A migration writes the
  literal document of the schema it produces, never the current model's, so each step
  stays what it was when later steps are added. The share hold may be switched off per job
  (``HoldThresholds.share`` None); the count floor and the percentage may not, and the
  hold on changed files that lost their signature is no setting at all.
- Exclusions are applied by the core's own listing, on both sides of a pair; the
  engine is handed an explicit file list and never sees a filter.
- The preferences keep the rclone configuration's path and the unlock chosen, and nothing
  the configuration holds: a password the user chose to have remembered is a sealed file
  beside the store (``remotes.seal``), never a field of the job file.
- The job file is shared by every process of the tool: waiting runs and verifications
  read it twice a second, and admitted ones every five seconds for the choices for the
  computer (``mirrorhold.power``), while a window or a command saves it. It is read and
  written through ``mirrorhold.exchange``; saves are serialised by a lock beside it, and a
  save given what the file held when it was read is refused (``StoreConflict``) when it
  changed since. A file another program keeps open past the patience is ``StoreBusy`` —
  never moved aside as corrupt, which only a file read whole and unparseable is. That
  file is moved aside under the save lock, once however many processes read it at
  once: the others report it moved aside, and a file a save replaced in between is read.
  A save's rename leaves the file's name empty for a moment, so a load or a fingerprint
  that finds no file reads again under the save lock, which every save holds across its
  rename, before it reports a fresh store or none.

Future direction
----------------
Every setting of the contract takes effect and the GUI offers each. A new setting enters
through the contract, its strict document and a store migration together.
"""
