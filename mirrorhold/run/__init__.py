"""
Run — applying a job's plans safely, one pair at a time, under the user's control.

Structure
---------
    gate.py          judge(): proceed, additions only (removals held), await a first
                     run's confirmation — a ticket's, or a first backup's go-ahead onto
                     a destination that holds nothing — or stop, on the plan's exact
                     quantities, the space other runs still have to write counted as
                     used; a remote's space (Space) judged where its service reports
                     one.
    confirmation.py  Tickets: a preview's plan, confirmed by the user, carried to a run,
                     with a first run's choice over what its destination already held
                     (Content: leave it where it is, or move it into versions).
    apply.py         apply_pair(): the only code that changes a destination — the
                     engine's leftovers, renames, moves into the versions batch, each
                     changed file opened before the copy, the engine's copy of an
                     explicit list (files it finds identical only dated, files it
                     passes over recorded as skipped), folders.
    control.py       RunControl (pause, resume, cancel as the run sees them) and
                     StatusPump (the thread that publishes status and reads requests,
                     counting files found identical as done); time_left().
    runner.py        run_job() and preview_job(): a job's pairs in order, once the run
                     is admitted (records.admission), the source read from a volume
                     snapshot when one can be made, a verified destination's expired
                     versions batches deleted when the job's cleanup is on, the gate's
                     space check made on the drive measured then and under the
                     admission lock, each pair ending in a record; pause and cancel
                     honoured in the listings and the signature reads too
                     (RunCancelled); a preview measures the
                     cleanup without making it, reports its progress and can be
                     cancelled through its progress callback. A first run onto what
                     the source does not hold is previewed both ways (Alternative),
                     ticket_for() confirming the other; a shared destination is run
                     on its job's part, its record written before anything is
                     applied; protected_folders(): what no run moves or writes into.
    put_back.py      put_back(): a preview's removals that reappeared after being moved
                     into the destination's newest versions batch, read from that batch
                     alone; they move into versions again like any other removal.
    launch.py        start_run(), start_verification() and start_repair(): a run, a
                     full verification or a repair started as a windowless core process
                     of its own, outliving its caller, its start-up errors kept for it.

Architecture
------------
A run is a core process of its own, whoever started it (the command line, the GUI's
"Run now" through ``launch``, a scheduled task through ``automation``, which names the
run's trigger in its options: "arrival" or "schedule"). It lists the source once, and
each destination once; the plan is judged by the gate and then applied exactly — the
core renames and moves into versions itself, and hands the engine an explicit list to
copy, so the engine lists nothing and deletes nothing. Callers reach a run only
through the status channel (``records.status``); the run reaches the engine only
through its adapter. A destination is reached only through its storage (``storage``):
its marker and shared record, its listing, every change applied, the job's definition and
the record's copy kept there, and the engine is handed the destination and the batch where
the storage locates them. A remote destination's storage comes from one engine session the
run opens once admitted, and a preview once it starts (``remotes.reach``), kept to its end
so an encrypted configuration is unlocked once. A run waits for its turn before anything else
(``records.admission``) and then works beside every run and verification it does not
conflict with; its gate counts as used what those runs still have to write on its
destination's drive, and publishes what it lets this pair copy there in turn.

Design patterns
---------------
- Judge, then apply exactly what was judged: no removal the gate did not allow can
  happen, because the engine is never given a removal to make.
- Safe points: every step of the core's own work is an atomic rename and a checkpoint
  for pause and cancel — every folder of a listing, every changed file's signature read
  and every changed file opened before the copy included, each publishing a count; the
  engine's copy is paused by suspending its process and stopped through its remote
  control, a graceful stop ending the engine once no file is in flight.
- Done is what was processed: a changed file the engine found identical counts as done
  in the status, the record and what is left, and the time left is measured against
  what the engine was handed, at the pace measured so far.
- Pairs are independent; one pair's absence, stop or failure does not stop the next.
- A first run claims nothing it was not shown: a ticket confirms exactly the removals
  and the choice it names, and a first run whose plan outgrew them waits whole.
- Leave by default: what a destination holds before its first run, and what appears
  later outside its job's own top-level names, is left where it is unless the user chose
  to move it; folders a run must never touch (a synchronised folder, another job's
  source, a folder Windows or this tool needs) make that the only choice, and a run
  stops, untouched, rather than write into one.

Caveats
-------
- A pause lasts within the session: a shutdown while paused is an interruption, and
  the pair's next run converges (the versions batch of the interrupted run stays).
- An engine that makes no progress at all for ``apply.STALL_LIMIT`` (30 minutes) while
  not paused — no byte copied, no file checked, nothing found identical or failed,
  nothing read or written by its process, or its remote control not answering — is
  ended and its pair fails (``engine-failed``), saying why, the run going on to its next
  pair as after any failure; the run gives its turn up when it ends, and the next
  scheduled or arrival run tries the pair again. Time the
  computer spent asleep is not counted; under a cancel already asked for the engine is
  ended the same way and the pair stays cancelled. A repair's copy is bounded alike.
- A waiting run reads the configuration store at each attempt — one another program
  keeps open is read again at the next — and works with what it read as it was
  admitted, so an edit during a run applies to the next one.
- A run that fails outside a pair in a way no one foresaw still records each selected
  destination it had not recorded, as failed (``unexpected-error``), and its traceback
  goes to the process's diagnostic log (``records.diagnostics``); a refusal of the run
  itself (``RunError``) and an interruption at the console reach its caller unchanged.
- A run reading its source from a volume snapshot (``snapshots``) lists, judges and
  copies from the source folder inside it; only the folder changes, never the plan.
  The snapshot lasts the whole run, a pause included, and is removed in the same
  ``finally`` that closes the run's status, which gives its claim up. The preview
  always reads as it is.
- The version cleanup (``maintenance.cleanup``) runs before the destination is listed,
  so the space it frees is in the drive the gate measures; a preview, which deletes
  nothing, counts the files' logical sizes, the cluster slack they free being margin.
- What reappeared after a move into versions is recognised in the newest batch alone
  (``put_back``): a file put back from an older batch, a later run or repair having made
  a batch since, reads as an ordinary removal. Recognition changes the preview's words
  only; the gate, the ticket and the run treat such a file as any other removal.
- What another run still has to write is counted from what its gate let it copy, less
  what it has done, with the gate's margin: a changed file it will find identical is
  counted until then, so the count errs towards less room, never more.
- The gate counts a changed file of the same size as bytes to copy, though the engine
  may find it identical and copy nothing: before the comparison nothing tells them apart.
  Onto a remote that keeps no fingerprint the source's can be compared with — WebDAV and
  SFTP as the engine serves them, an encrypted remote — such a file is copied again, its
  previous copy moving into the batch (measured on the engine's loopback backends).
- A remote is recorded absent while its service cannot be reached, and waits like a drive
  not attached; one that stops answering fails the pair (``remote-failed``) — said as before
  any of the plan was applied where its marker or a shared destination's record was read,
  its changed files' leading bytes read or its space asked, and with what
  was done recorded, the next run converging, once the run wrote there. No other
  activity writes on a remote meanwhile, so its gate counts no other run's share and
  publishes none. A remote listing names the plan cannot tell apart stops the pair
  untouched (``destination-names-ambiguous``).

Future direction
----------------
A run is the one thing that changes a destination; maintenance adds no second writer.
"""
