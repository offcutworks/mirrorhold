"""
Maintenance — version cleanup, verification of a job's destinations, in full or of their
new copies, and the repair of the copies a verification found to differ.

Background
----------
Every removal or replacement a run makes on a destination is a move into that run's
versions batch, so versions accumulate. A job may opt in to deleting batches older than
N days: the only permanent deletion the tool performs. And a run judges change by
size and modification time alone, so a copy damaged on its drive — a bad sector, a
flipped bit — keeps both and no run ever copies it again; a full verification reads
both copies of every file in full and compares their contents, with a per-job
reminder when one is due. On a large library that takes hours, so a verification of
new copies reads only the copies no earlier pass found the same as they are now — those
a run wrote since, above all — and leaves the rest to the next full one. A verification
cannot tell which copy is damaged; a repair is the user's answer when they trust the
source's copies. Low free space on a destination's drive is measured by each run and
kept in its record (``records.record``). A remote's copies are compared without being
downloaded wherever the service allows — by the fingerprints it keeps, or by the engine's
check of an encrypted remote's encrypted form — and read as a drive's are where it allows
neither (``compare``).

Structure
---------
    cleanup.py       expired_batches(), plan_cleanup() for the preview, clean_versions()
                     for the run: whole batches older than the job's days, deleted only
                     inside a marker-verified versions folder and never through a link.
    verification.py  VerificationRecord, one per pass over one destination, full or of new
                     copies, with how it compared (Measure), rewritten as the pass goes;
                     reading them back; the pass of each kind to continue, the
                     destination's last full verification, the reminder
                     (verification_due) and the destinations a run's notice reminds of
                     (due_after_run), throughputs by measure.
    compare.py       How a copy is compared with its source: read on both sides, by the
                     fingerprint a remote's service keeps (the kinds computed here), or by
                     the engine's check of an encrypted remote; measure_of() a destination.
    verified.py      The list of verified copies: a fingerprint of each file a pass found
                     the same, written as it goes and kept sorted once it finished, beside
                     the store; the baseline a pass of new copies leaves unread.
    verifier.py      verify_destination(): a verification in a core process of its own —
                     its live status and control, its turn (records.admission), given up
                     whenever a waiting run writes what it reads, the listing of both
                     sides (a shared destination's backed-up part alone), each file
                     compared by the destination's measure — on a drive read in full on
                     both sides at once and its digests compared — but those its baseline
                     holds, in a pass of new copies — progress and its list saved as it
                     goes.
    repair.py        RepairRecord, one per repair of one destination: the pass it took
                     up, the copies it replaced and those it did not with why, and those
                     it is replacing; reading them back; what of a pass is pending, and
                     whether all of it is replaced.
    repairer.py      repair_destination(): a repair in a core process of its own — its
                     live status and control, its turn as a run of that destination, each
                     copy checked, the backup copies moved into versions, the source's
                     copied in their place by the engine and read back by the
                     destination's measure.

Architecture
------------
Cleanup runs inside a run (``run.runner``): once a destination's marker has verified and
before the destination is listed, so the space it frees counts in that run's space
check; the preview measures the same selection without deleting. Verification is a
manual action of its own, started as a core process like a run (``run.launch``) and
watched and controlled through the same status channel (``records.status``, kind
"verification"); it writes nothing on either side but its own records, so it works
beside every run that does not write what it reads and beside the verifications of
other destinations — those on other drives, while the user's choice takes drives one at
a time (``records.admission``). A repair is a manual action of its own too (kind
"repair"), admitted as a run of its one destination, before waiting verifications; it
writes that destination through the run's own steps (``run.apply``), so its replacements
move into a versions batch as a run's do. Each reaches the destination only through its
storage (``storage``): the cleanup's fences, the verification's reads and the repair's
checks and moves are written once, above the seam. None depends on the GUI; the GUI shows
their state and starts them.

Design patterns
---------------
- One record per pass, saved as it goes: a stopped, failed or dead pass keeps its cursor
  and the next verification of that destination and that kind continues it — a pass of
  new copies leaves an unfinished full pass to be continued; the destination's state
  (last full verification, a verification due, copies that differ) is derived from the
  records and never stored apart. A finished full pass dates the verification by when it
  began, the earliest moment any of its files was read; a pass of new copies never does.
- What a pass found the same is a cache, not a record: the list of verified copies is
  kept beside the store alone, one per destination, and a list lost or unreadable only
  makes the next pass of new copies read every file, as a full one.
- Fences re-checked at the moment of deletion: the marker read again, each folder tested
  for being a link immediately before it is entered.
- Backups first: a verification gives its claim up to a waiting run or repair that writes
  what it reads and that verifications alone hold back, and waits behind it.
- A verification records what it found and a repair what it did: the verification's
  record is never rewritten, and whether a pass's copies still differ is derived from the
  repairs that took it up (``repair.all_replaced``). Only a finished pass stands for the
  destination: the window's *Copies differ* and a repair both read the latest finished
  pass, of either kind, never one in progress or left unfinished.
- A backup copy a repair moves into versions is named in the repair's record, saved, before
  it moves, and stays named until it is back in its place or its outcome is recorded,
  whatever ends the repair, its process dying included: one left in versions unnamed would
  be deleted by the job's version cleanup with nothing saying it is there.

Caveats
-------
- A batch is dated by its name, when the pair that filled it began to be applied (or the
  repair its copies); a long run's batch therefore ages from that moment.
- A verification reports a difference and repairs nothing: it cannot tell which copy is
  the damaged one, and copying the source over the backup could replace a good copy with
  a damaged original. A repair does it on the user's word alone, keeps every backup copy
  it replaces in versions (which the job's version cleanup deletes after its days), and
  leaves as it is a copy whose source lost the file-format signature its backup has, and
  one inside a folder no run moves or writes into (``run.runner.protected_folders``),
  whatever a verification read there.
- A repair takes up what the pass listed — its first thousand copies — so a pass that
  found more leaves the rest to a verification started again. It replaces only a copy
  still the size and time the verification compared; one changed since is the next run's.
- The source is read as it is, never from a snapshot: a file another program holds
  without sharing is reported as not compared.
- A pass that continues after a stop compares only the files after its cursor; a file
  added before the cursor since is left to the next pass, and the files it found to
  differ stay listed until a pass starts again.
- A pass of new copies trusts its list for what it leaves unread: a copy damaged on its
  drive since a pass read it, or replaced by another program with one of the same name,
  size and time, is found only by a full pass. A file renamed — at the source, or by a
  run matching it by size and time — is new to the list and read again.
- A read that fails is followed by a look at both sides' folders. One gone — a drive
  removed or reset, a remote whose engine no longer answers — is looked for again for
  ``verifier.SIDE_PATIENCE``: back, the file is read again; still gone, the sitting ends as
  failed, in words, to be continued, rather than counting every remaining file as
  unreadable.
- A comparison by fingerprint proves that the service keeps, of each file, what it recorded
  as the source holds it — not that the bytes it stores are still those: damage the service
  did not record is not seen. No verification of a remote reads its copies back in full
  where a fingerprint serves (the maintainer's decision, 2026-10-28). A remote's
  verification and repair each take the turn every activity reaching a remote takes.
- A stop ends a sitting even while a read does not return (a drive that hangs): the
  readers are threads the process does not wait for, the read is given up after
  ``verifier.STOP_PATIENCE`` and asked to cancel, and that file is compared again next
  time. A save of progress another program's hold refuses is made at the next one; it
  never ends the pass.
- A shared destination (``endpoints.shared``) is listed as its record says its job has
  backed it up (``planning.sharing.recorded_scope``): what its job leaves where it is is
  neither extra nor compared, and one whose record cannot be used is not verified. A
  verification reads the record and never updates it, so a name the source gained since
  the last run is not the job's yet: the source's files there count as not backed up, and
  what the destination holds there is not compared. A repair reads the record alike.
"""
