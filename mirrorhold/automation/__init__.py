"""
Automation — jobs that run by themselves: when one of their drives is attached, or on a
schedule, through Windows Task Scheduler, with no process of the tool's left running but the
notification-area icon of a reminder the user has still to look at (``reminders``).

Background
----------
A job runs only when the user starts it unless the user switches on one of its two
automatic triggers: when any of its drives is attached (the computer starting with the
drive attached counts), or a schedule — daily, weekly or monthly, at fixed times or every
few hours (``jobs.schedule``). The scheduler is the trigger; the tool keeps nothing
resident to watch for one. Every task starts the installed copy's core, never the working
copy, so an arrival during development never runs half-edited code.

Structure
---------
    tasks.py       TaskSpec and desired_tasks(): the tasks a configuration calls for
                   (per job ``<job id>-arrival`` and ``<job id>-schedule``), and
                   task_xml(), their Task Scheduler definitions; Launcher (the
                   installed interpreter, folder and store a task starts).
    scheduler.py   Scheduler (the seam) and WindowsScheduler (COM, one folder).
    reconcile.py   reconcile(): the folder made to hold exactly the desired tasks;
                   apply_automation(): what every caller runs after the jobs change;
                   remove_all() for uninstalling.
    arrival.py     Which of a job's drives arrived: presence kept per job and per
                   session of the computer, the session's boot time, the drives
                   settling after an arrival, and what an arrival runs.
    automatic.py   run_on_arrival() and run_scheduled(): the command line's
                   ``automatic`` command, which the tasks start.

Architecture
------------
The configuration is the only input: ``apply_automation`` derives the tasks from the
jobs and the installed copy that serves the store — the source installation's copy, or
the portable release whose setup record sits beside the store — and reconciles; nothing
else registers or removes a task (the GUI, the command line and the installer all call it
after a change, whichever copy they run in). A task starts ``pythonw -m mirrorhold
--store <store> automatic <job> --by arrival|schedule`` in the installed copy's folder,
with its interpreter (``invocation`` composes the command); that process decides what to
run and runs it in itself, as an unattended run named by its trigger in the status and
the records, and raises the run's notice.

Design patterns
---------------
- A fingerprint in each task's description: only a changed definition is registered
  again, which keeps a schedule's missed-run catch-up.
- Presence over events: an arrival task reacts to every volume arrival and removal and
  to logon, and the core compares what it sees with what it saw, so event details are
  never parsed and a drive's arrival is judged by the same identity rules as a run.
- Injected scheduler, volume source, boot time, settling and runner: every rule is
  tested in memory; only the live check uses the real scheduler.

Caveats
-------
- Tasks run only while the user is logged on, with the user's own rights; arrivals
  before logon are served by the logon trigger.
- Task Scheduler ignores a second start of a task whose run is going; the arrival
  handler therefore looks again after each run and serves what arrived meanwhile.
- The tasks live in the ``\\MirrorHold`` folder, and reconciliation removes any
  other task found there.
- A job edited while its arrival run is going takes effect at the next arrival.
- A portable release moved or deleted after it was set up: reconciliation registers
  nothing and says so (``GONE``); the tasks already registered keep pointing where it was
  until a copy is set up again or the setup is removed.

Future direction
----------------
Maintenance adds no task: version cleanup runs inside a job's runs, a full
verification is started by the user, and its reminder is shown in the GUI and in the
notice of a run that backs a due destination up. A task of a new kind would join the
same folder through the same reconciliation.
"""
