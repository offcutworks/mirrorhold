"""
Records — what runs did, what runs are doing, and which may work at the same time.

Structure
---------
    record.py   RunRecord (one per pair per run, with how its source was read —
                SourceRead — the files left unbacked-up with their runs in a row,
                what the version cleanup deleted — VersionsCleaned — and the
                destination drive's free space at the end, and among what was done
                the files found identical), write_record(), read_records(),
                throughputs(), destination_state(), first_success(), last_counted(),
                unbacked_streaks(); json_text() (JSON any name can be written in).
    status.py   LiveStatus and StatusBoard (a run, a verification or a repair publishing
                its status: its phase, the destinations a run serves, the counts of the
                step in progress, the files in flight and being compared, its claim
                and its turn), Wanted and
                request() (callers asking one to pause, resume or cancel),
                observe_runs(), find_run(), is_live(), reap_interrupted(); line_order(),
                the one order the window and the command line list activities in;
                LastRead, what an observer that looks again and again last read of
                each activity, which names one whose status it cannot read now.
    admission.py  which runs, verifications and repairs work at the same time: Claim
                (the folders one reads and writes, the drives they lie on with their
                kinds, and when it takes turns on a drive), conflict(), the order of turns
                (blockers(), place_in_line()), admit() under the admission lock,
                gives_way() for a verification, WaitedFor (what a waiting one names:
                the activity, whether its status could be read, and the drive they share
                when that alone is why); starting(), whether a waiting one is starting.
    lock.py     FileLock: locks the operating system releases when their holder ends;
                holding() for a lock held a moment; is_held_elsewhere(), a look that
                never raises and never makes a lock again.
    diagnostics.py  the diagnostic log each core process keeps beside the store: what
                it says, its marks (what it was given, the job it resolved, each
                activity it began, how it ended), and a failure no one foresaw with its
                traceback; bounded. LogWatch reads the marks back for the window
                (ActivityLog); write_joined() saves every log as one text.

Architecture
------------
Everything here is files beside the configuration store (``locations.RuntimeLayout``),
under ``Config/``, which the repository ignores as private: records and status name the
user's files. The GUI and the command line read state through these
functions and change a run only by writing what they want of it; the run itself, in
its own core process, reconciles. A full verification (``maintenance.verifier``) uses
the same status channel, its kind "verification"; its own records are
``maintenance.verification``'s. So does a repair (``maintenance.repairer``), its kind
"repair", its records ``maintenance.repair``'s. The same channel carries admission: each
activity publishes the folders it claims and whether it holds them, and every decision
about which may start reads them all under one short-held lock, so activities that do
not conflict work at the same time — and those that use one drive take turns on it where
the user's choice in the job file and the drive's kind say so, while any two that reach a
remote take turns whatever that choice, every remote being reached through the one rclone
configuration the engine rewrites when it refreshes a sign-in. Nothing here
imports the GUI
or the engine. Beside them, each core process that runs an activity keeps a diagnostic
log of its own (``diagnostics``): what it says, and every failure it did not foresee
with its traceback. Its marks — one line each, ``@<name>`` and JSON, beside the words —
say what the process was given, the job it resolved, each activity it began
and how it ended; the window follows them (``LogWatch``) to show, in whichever window is
open, an activity that left no record of its own. One module writes and reads them.

Design patterns
---------------
- Every file a reader may open at any moment is written and read through
  ``mirrorhold.exchange``: written aside, then renamed over, by a writer that readers
  never stand in the way of.
- Liveness by lock, not by process number: a run's lock is released by the operating
  system however its process ends, so a dead run is recognised without guessing.
- Wanted state, not commands: a control file holds what callers want, so repeated
  and concurrent requests compose; requests to one run take turns under a lock of their
  own (``<run>.request``), so two made at one instant each keep the other's change.
- Claim everything at once, in a strict order: an activity holds all it claimed from its
  admission to its end and waits behind every conflicting one that goes first, so no
  two can each hold part of what the other needs and none waits for ever.

Caveats
-------
- A run killed outright leaves its status files until the next run reaps them; until
  then ``observe_runs`` reports it as not alive.
- Records list skipped files and errors up to ``MAX_LISTED``; the totals are complete.
  The files left unbacked-up are counted from their own list, also up to
  ``MAX_LISTED``, so a run that skips many cloud-only files still counts every file it
  could not back up up to that bound.
- A run that does not reach its copies (an absent drive, a stop, a cancel) neither
  extends a file's streak nor breaks it.
- Schema 2 added how the source was read and the unbacked-up files, schema 3 what the
  version cleanup deleted and the free space, schema 4 the files (and bytes) found
  identical among the done counts, schema 5 whether the service of a remote destination
  may keep what the cleanup deleted, schema 6 whether a remote the run reached encrypts; an
  older record reads with the later fields absent, none found identical and no such keeping
  or encryption said.
- An NTFS name may hold an unpaired surrogate, which UTF-8 cannot: every JSON file
  holding names (records, status, tickets, verification records) is written through
  ``json_text``, which escapes it, so the name reads back unchanged.
- A dead verification's or repair's status files are removed by the next activity to be
  admitted, with no run record: its own record keeps its progress.
- A status published by an older copy of the tool carries no claim; admission takes such
  an activity to conflict with every other, and to hold its claim unless it waits. A
  claim that names no drives is compared by its folders alone; one that names drives
  without their kind or the choice of turns takes turns on all of them or on none, as its
  one choice of on or off says, and every claim keeps publishing that one choice — on when
  it takes turns on any of its drives — for an older copy to read.
- A drive is what ``VolumeInfo.drive`` says: a physical disk, so two letters on one disk
  are one drive, or a volume no single disk carries; its kind is what the user marked it
  (``Activities.drive_kinds``), else what ``VolumeInfo.drive_kind`` says, and the claim
  (``admission._drives``) is the one place the two meet. Whether an activity takes turns
  on a drive is decided by the waiting activity's own choice and marks, read from the job
  file at each attempt, and the drive's kind as its own scan read it: a newcomer judges by
  what it read, and one
  admitted under another choice keeps what it holds. Under the automatic choice a drive
  of a kind not known takes turns, as a spinning one does.
- A run is found by its liveness lock as well as by its status file: a look can meet a
  status as it is renamed over and find nothing, so a held lock whose status is not
  there is looked for again for ``MISSING_PATIENCE`` and then observed as alive and not
  readable, never as a run that ended.
- Another program holding a status file without sharing it (an antivirus scanning it)
  is borne for the exchange's patience: the status pump's publication waits for its
  next tick, and an admission's is tried for a moment, the activity not admitted if it
  never lands. A status that stays unreadable while its lock is held is a live activity
  whose claim is unknown, so admission takes it to conflict with every other; a control
  file that cannot be read leaves what its run wants as it was; a request that cannot
  take its turn or be written within its patience reaches its caller as ``FileBusy``. A dead run is recorded only once its
  status file is removed, so it is never recorded twice. ``read_records`` names a record
  another program kept past the patience (``busy=``), so a reader can look again.
- A log with no end mark is taken to belong to a process that ended without a word only
  when no process has it open: Windows refuses to open it unshared while one does
  (``exchange.in_use``), and its writer keeps it open for its whole life. Elsewhere that
  cannot be told, and it is never reported. A log written before marks existed (no start
  mark in its first two lines) is passed over by ``LogWatch``.

Future direction
----------------
The destination's state stays derived from the records — runs' and verifications' —
and is never stored apart.
"""
