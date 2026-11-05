"""
MirrorHold GUI — the Tk window through which the user defines jobs and arranges them
in groups, previews and confirms runs, starts them, watches and controls them, reads
and saves their history and the diagnostic logs, verifies destinations fully, and
replaces the backup copies a verification found to differ.

Background
----------
The core (``mirrorhold``) does everything that touches data; this unit is one of its
callers, beside the command line and the scheduled tasks. It is a separate unit so
that automation never depends on it: every run, "Run now" included, executes in a core
process of its own, and closing this window stops nothing. The configuration is the
seam between the two: this unit edits jobs only through the core's job contract and its
validation, and never re-implements a rule.

Structure
---------
    __main__.py        python -m mirrorhold_gui -> app_view.main (the project root's
                       main.py is the same call, runnable from any folder)
    app_view.py        The composition root: DPI awareness, the application identity,
                       the icon, the job file at start (asked about again while another
                       program holds it), the main window and its mark, a portable
                       release's question at start.
    session.py         GuiSession: the store (a save refused when the file changed
                       since it was read, and the scheduled tasks brought into line
                       after each save), the volumes, the sync roots, the runs it
                       started — runs, verifications, repairs — and why one could not
                       start, install and uninstall
                       (a portable release: set up and remove).
    refresh.py         What is read off the window's thread: Refresher (drives, markers,
                       what a destination without a marker holds, run, verification and
                       repair records, the activities' logs read back) and RunWatcher
                       (the runs' status files), a look that fails keeping the last one
                       and saying why; Settling, an activity that ended held in the job
                       list until the refresher has looked at what it left.
    formatting.py      Counts, sizes, times and durations in words.
    jobs_model.py      The job list's rows, status words and tones — groups above their
                       jobs, a first backup waiting for the go-ahead, files not backed
                       up twice in a row, copies a verification found to differ until a
                       repair replaced them, how far a verification at work is, a pass
                       that did not finish, a repair at work or waiting, the
                       destinations a run serves, a start that left no record and a
                       drive not attached included; free space and the last full
                       verification in their own columns; the question before a job's
                       first backups; why a source is read as it is, in words.
    groups_model.py    What a selection of groups, jobs and destinations allows, moving
                       a group or a job up or down, the questions before several jobs
                       run or are removed, before several destinations are verified and
                       before a group is deleted, group names, the groups remembered
                       closed.
    groups_view.py     The New group and Rename group windows.
    main_view.py       The main window: toolbar, job list and its right-click menu
                       (Continue verification and Replace backup copies included),
                       destination actions, the
                       divider, the Runs and History tabs and what History shows, the
                       status bar and its looks that failed, the File menu and its
                       saves; where it opens and what it remembers.
    window_model.py    The main window's remembered place — size, position, maximised,
                       divider — read, checked and fitted to the monitors attached.
    editor_model.py    JobDraft and check(): the editor's state, the snapshot choice,
                       When to run and Versions and verification included, and the
                       core's checks; the question a save asks before old versions are
                       deleted; a typed folder read as the command line reads one.
    editor_view.py     The New job / Edit job window, and the one adding a destination.
    remote_model.py    Reaching the user's remotes off the window's thread: one piece of
                       work at a time, one session for one window's length, the core's
                       refusals in the window's terms, a remote's folders walked up and
                       down.
    remote_view.py     Choosing a remote's folder, and where a remote's folder is, which
                       every button opening a destination's folder shows in its place.
    preview_model.py   PreviewWorker (a preview on its own thread, cancellable) and
                       summarise(): a preview's result in words (how the run will read
                       the source, changed files of the same size compared first, names
                       left out listed, a first run's two answers over what its folder
                       already holds), its run buttons; ticket_to_run(): the ticket for
                       the answers given.
    preview_view.py    The preview and confirmation window, its choice, and its lists.
    runs_model.py      A run's, a verification's or a repair's status in words — the counts of
                       the steps before the copy, files found identical counted as
                       done, the files being compared, what a waiting one waits
                       for, the drive they share and its place in line, one starting,
                       one whose status cannot be read now — and what can be asked of
                       it; the rows in line order; a request
                       that did not reach its run, the starts that ended without
                       running, and the question when the window closes, in words.
    runs_view.py       The Runs tab; RunRequests, what a row's buttons ask — requests
                       written off the window's thread, the cancel dialog and the
                       questions before a verification or a repair stops — which the
                       notification-area icon asks through too.
    tray_model.py      The notification-area icon in words: each activity's line from
                       its Runs tab panel and each standing reminder's, the tooltip
                       within Windows' length, the fill, the grey and the amber of the
                       picture, when its process ends and hands the store's icon lock
                       back, the mark a main window carries.
    tray_view.py       The notification-area icon's own process (python -m
                       mirrorhold_gui.tray_view --store FILE, started by an activity):
                       the icon through Tk's tray, its menu, the questions over an
                       invisible host, the main window found by its mark or opened.
    history_model.py   The selection's history: run, verification and repair records and the
                       starts that left no record as rows, each naming its job, newest
                       first; a run's details (how the source was read, skipped files
                       with their runs in a row, files found identical, old versions
                       deleted, the free space at the end) and a start's; the log that
                       wrote a record; the history saved as a CSV file.
    history_view.py    The History tab, the run details and start details windows.
    verify_model.py    The Verify... dialog (why a failed pass did not finish included)
                       and a verification's details in words, what a repair replaced
                       since among them.
    verify_view.py     The Verify... dialog and the verification details window, from
                       which a repair of the copies that differ is asked for.
    repair_model.py    The question before a repair of the copies a verification found
                       to differ, a repair's result and details in words, and what a
                       verification's details say a repair replaced.
    repair_view.py     The question before a repair, and a repair's details window.
    skipped_model.py   A destination's files not backed up by its last run, with why
                       and the runs in a row each has gone unbacked-up; one shown whole.
    skipped_view.py    The Files not backed up window, the selected file whole below it.
    prefs_model.py     The sync engine (checked before it is saved), the rclone
                       configuration with its unlock, the notification
                       choices, when runs on one drive take turns (automatic, always,
                       never) and what each drive is (drive_rows),
                       the computer kept awake while one works and the work
                       named on Windows' shutdown screen,
                       the installation in words for each kind of copy and
                       what may be done to it, the question a portable release asks at
                       start.
    prefs_view.py      The Preferences window; setting up, and the question at start.
    about_model.py     The About window in words: the copy, the copyright line read
                       from the licence file, that the program is free, the official
                       address, the engine, the third-party notices a release ships.
    about_view.py      The About window (Help menu) and the licence and notices
                       windows it opens.
    shipped_model.py   What a copy ships beside its packages for the window to read:
                       the licence, a release's third-party notices, the icon; the
                       release build names them through it.
    transfer_model.py  Import (new, already here, different) and its validation.
    transfer_view.py   The Import jobs and Export jobs windows.
    startup_model.py   A corrupt, newer or held job file at start, in words.
    startup_view.py    Restoring an earlier copy, opening read-only, or reading a held
                       job file again.
    common_view.py     Display preparation, the application identity, the icon, list
                       styles and tones, the link style, the divider's grip, sizing
                       windows to their content, the monitors' working areas, the
                       scrolling frame, the question and list windows, the hints.

Architecture
------------
One dependency direction: GUI -> core -> engine. Inside this unit the decomposition is
by sub-feature, each a vertical slice: a ``*_model.py`` file holds the sub-feature's
logic and imports no toolkit, and its ``*_view.py`` file builds the widgets. A view
reads models and calls the core; a model never imports a view. Only ``*_view.py``
files import ``tkinter``.

The unit has two processes. The window (``app_view``) is started by the user. The
notification-area icon (``tray_view``) is started by a working activity of the core
(``mirrorhold.tray``) — never at sign-in — one per store, shows every activity of that
store, and ends a few seconds after the last one, unless a finished activity left a
reminder (``mirrorhold.reminders``): it then stays, showing it, until the window, opened or
in the focus, clears the store's reminders. It reads the statuses and asks its requests
exactly as the window's Runs tab does, so it is one more caller of the core, and its failure
costs the icon and never the work.

Design patterns
---------------
- Snapshot in, words out: a model is a function of what was observed at one moment
  (jobs, volumes, markers, records, run status documents), so every surface of one
  state — the list, its status words, the enabled actions — agrees.
- Background observation, foreground widgets: whatever may block on a drive or on
  another program's hold (volume scans, markers, a run's status, a preview's listings, a
  request) runs on a thread; widgets are touched only on the window's thread, which
  polls. A look that fails is never the last: each clock reschedules itself whatever
  happened, the window keeps what it saw and the status bar says since when it has not
  looked, and why.
- Requests, not commands: pause, resume and cancel write what is wanted of a run
  (``records.status.request``); the run reconciles.
- Read back, never told: what a core process could not record — a start refused, a
  process gone without a word — reaches whichever window is open from the process's
  diagnostic log (``records.diagnostics.LogWatch``), never from the window that started
  it alone.
- Optimistic store: what the job file held when read (``LoadResult.fingerprint``) goes
  with every save; a save over a changed file is refused (``StoreConflict``) and the
  window reloads.
- One selection, every action: the list takes several rows at once, and what the
  selection reaches — one job, one destination, one group, several items — decides
  every button and menu entry (``groups_model.Selection``); a group or several jobs are
  started one run each, after one question, and the destinations a job, a group or
  several items reach are verified one verification each, after one question. A
  destination selected is acted on alone: *Run now* runs a job whole only when its own
  row or its group's is selected, and the selected destinations alone otherwise
  (``Selection.chosen``), as a preview opened from a destination previews and runs it alone.

Caveats
-------
- A preview runs in this process and holds both sides' listings while its window is
  open: about 370 bytes a file per side (the core's measurement).
- The job file is shared with every process of the tool: the store serialises saves
  and refuses one over a change made elsewhere since this window read it
  (``jobs.store``); a file another program keeps open past the store's patience is
  ``StoreBusy``, never read as corrupt.
- The window's import validates a remote destination without asking the engine, which says
  so; the run checks it again.
- The window declares system DPI awareness: on a second monitor of another scale
  Windows stretches it as it would a DPI-unaware window.
- A job file from a newer version cannot be read, so the read-only window lists no
  job; runs in progress can still be watched and controlled.
- Every setting of the job contract is offered. The snapshot choice is shown disabled,
  and kept as stored, for a source on a drive that cannot be snapshotted.
- A verification is never applied here either: *Verify...* starts it as a core process
  of its own, and its details report what differs. Replacing the backup copies it found
  to differ is the user's own act — *Replace backup copies...*, after one question, on
  the verification's details or in the destination's right-click menu — and runs as a
  core process of its own too (``mirrorhold.maintenance.repairer``); the copies it
  replaced are said on the verification's details, and the destination's "Copies differ"
  ends once every copy the verification found is replaced. A verification that found
  more copies than it lists leaves the rest to be found by verifying again.
- The verification reminder is shown here (the *Verified* column) and, beyond the window,
  only in the notice of a run that backs the destination up: no process runs to raise it
  while nothing else happens.
- A destination's "Files not backed up" report reads its latest run that read the
  source; a run's details read that run's own record.
- An activity that ended is listed in the job list as it was last seen at work until a
  look of the refresher's begun after its end succeeds (``refresh.Settling``): its
  destinations' markers are read again only then, and a first backup just made would
  otherwise be offered again. While the drives cannot be checked the job stays listed at
  work and *Run now* does not start it; the status bar says since when. The Runs tab, the
  status bar, the closing question and History take the runs as they are.
- A live activity whose status cannot be read now is known by the last status the run
  watcher read of it (``records.status.LastRead``), and only for what it is: the Runs tab
  titles it from it, the closing question counts it by its kind, and the job list shows it
  at work on its job and the destinations it serves, never how far it has come. One the
  window never read is of no known kind ("a run, verification or repair"), and the job list
  shows nothing of it, as nothing names its job.
- Tk on Windows rebuilds a whole native menu at every entry configured, even to the state
  it already has, and a menu held open shows each rebuild as a flicker (Tk 9.0.4, measured
  on the development machine). The window re-derives its menu entries' states at every
  look, so an entry's state is written only when it changes (``main_view._set_entry``).
- A start that left no record is known only from its log: one whose log the logs' bound
  removed, or whose process ended without a word where Windows cannot be asked whether
  the log is still open, shows nowhere.
- The preview's line on how the run reads the source asks Windows nothing
  (``snapshots.reading.settled_without_asking``): a drive whose snapshots the run then
  declines to displace still reads "from a snapshot ... when it can make one".
- The saved history is a document for a person — the words as shown — and promises no
  format to other programs.
- A remote destination is never asked at a look (``refresh`` passes over it): what the
  list says of one comes from the run records, its first run counting as confirmed once a
  run was applied there, so a marker that went wrong on the service since shows only when
  an activity reaches it. The window asks the engine when the user acts, on a thread:
  adding a remote destination, opening a job that has one (whose *Save* waits for the
  answer), browsing, *Check* in the preferences, a preview. It reads the configuration
  through the engine alone. *Open folder*, *Open versions* and *Open batch* on a remote show
  its address to copy, since nothing on this computer can open it.
- A password typed in the preferences lives in the entry until *Save* hands it over, and
  is then sealed from this process (``remotes.reach.remember_password``), as ``password
  set`` seals it from its own; no other copy is kept.
- A new job runs only when the user starts it: the editor's automatic runs are
  additions, off by default, and take effect once a copy is installed — from a working
  copy, the copy built beside it; a portable release, set up where it lies. The
  installed copy's own window shows its installation but cannot install, update or
  uninstall it. A portable release names its section *Automatic runs and Start menu*
  and offers *Set up* and *Remove*, always shown and enabled by the state; opened while
  another copy is set up (a newer release unpacked beside an older one, or the folder
  moved), it asks once at start whether to use this copy instead. *About* names a
  portable release by its label; a working copy runs from source and shows no version.
- *About* reads its copyright line from the licence file beside the packages, which a
  portable release carries at the root of its folder and the source installation's copy
  beside its packages; a copy without one says so and offers no licence to open. A
  release also carries the third-party notices of the interpreter, Tk, pywin32 and the
  engine it bundles; a working copy and the source installation run the user's own
  interpreter and engine, so *About* offers no notices there. A release and the source
  installation's copy carry the program's icon, which every window takes; a working
  copy, which carries none, draws the same icon at start (``mirrorhold.icon``), the
  sizes up to 64 pixels only, since the larger take most of a second.
- The GUI's process takes the application identity the Start menu entry carries before
  its first window opens, so the taskbar groups the windows with the entry, whichever
  file started them.
- A first backup into a folder that held nothing when the refresher last looked is
  started after one question, without a preview; the run looks again, and a folder that
  holds something by then waits for a review in the preview instead. Whether a folder
  holds anything is not known before its drive's marker has been read: such a first run
  is confirmed through the preview, as one into a folder holding something is.
- A first run into a folder holding what the source does not is answered in the preview:
  leave that where it is (the preview's own answer, and the only one offered where the
  core protects a folder inside) or move it into versions. Another answer than the
  preview's has the core write the ticket for it (``runner.ticket_for``); the run is
  started as always, so the way a run is started never carries the answer.
- Groups only arrange the list: a group's run is its jobs' runs, admitted like any
  others, so jobs on one drive take turns there or share its speed, as the user's choice
  and the drive's kind — as Windows reports it or as the user marked it — say
  (Preferences). Jobs move between groups from the
  right-click menu alone; a group moves among the groups, and a job within its group or
  among the jobs in no group, one place at a time from the same menu or with Alt+Up and
  Alt+Down. The order is the job file's, read alike by every surface; each move is saved
  at once, with no previous copy kept and the scheduled tasks left alone. Which groups
  are closed, and the main window's place — size, position, maximised, divider — are the
  window's remembered state, saved as the window closes when it can be (never over a
  file changed elsewhere), and a save of that state alone keeps no previous copy of the
  job file. A remembered place is fitted to the monitors attached when the window opens;
  one it cannot use is passed over silently.
- A window keeps on its face the words a choice needs; what explains a choice is the
  hint of its control (``common_view.Hint``), shown while the pointer rests on it. A
  hint is not reached from the keyboard, so nothing a window asks may depend on one.
- A drive not attached greys its items in the list (the absent tone) and leaves their
  words as they were; a row asking something of the user keeps its own tone. The grey
  follows the refresher, so it lags a drive's arrival or removal by up to one refresh.
- The divider's grip is a named style over the system theme's own divider, which draws
  nothing; a paned window elsewhere that wants it takes the style by name.
"""
