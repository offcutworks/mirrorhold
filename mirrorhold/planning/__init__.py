"""
Planning — what a run of one pair would do, computed without doing it.

Structure
---------
    listing.py     list_tree(): one side of a pair from directory listings alone —
                   files, folders, what was skipped and why, what was excluded; a
                   progress callback sees the entries counted and may end it.
                   occupancy(): whether a destination holds anything a listing
                   keeps, read from its top level alone. Scope: the part of a
                   destination a listing covers.
    plan.py        build_plan(): the pair's plan — new, changed, deleted, renamed,
                   kept, blocked, left out, the engine's leftovers — and its
                   quantities; left_out(): why a path is left out of every step.
    sharing.py     share(): a shared destination's part that is its job's, what it
                   leaves where it is, and its record as a run leaves it;
                   listing_scope(): the part its run lists; recorded_scope(): the
                   part its record says is backed up, which a verification lists.
    signatures.py  Known file signatures; which changed files lost theirs, a progress
                   callback seeing each one and able to end the reads.
    estimate.py    The duration estimate: a range with its basis.

Architecture
------------
The plan is what the safety gate judges and exactly what a run applies: the core lists
each side once, computes the plan, and hands the engine an explicit list of the files
to copy; it performs every rename and every move into versions itself. So nothing the
gate did not judge can be applied, and no side is listed twice in one run.

A shared destination's plan is of its job's part alone (``sharing``): what the job
leaves where it is never enters the plan, so it is never counted, moved, renamed onto
or compared, and a folder holding it never moves whole. A later run lists only that
part (``Scope``), which is what makes a destination at a system drive's root affordable.
A run's part reaches what the source holds now; a verification's stops at what the
record says the job has backed up, so the user's own content at a name the source has
just gained is never compared.

Design patterns
---------------
Listings are plain data; the plan is a pure function of two listings and a time
window, and a shared destination's part a pure function of two listings and its record,
so every category is tested without a volume or the engine. Paths keep the
volume's spelling and compare by a case-folded key.

Caveats
-------
- A listing holds every entry of its side in memory: measured at about 370 bytes a
  file for a 46-character path (200,000 synthetic entries), so a million-file library
  costs about a third of a gigabyte per side while it is planned.
- Rename recognition reads no content: a destination-only and a source-only file of
  the same size and modification time are taken to be one file moved. A tie between
  several is broken by name only; what stays ambiguous is copied and moved into
  versions instead, which is safe.
- Signatures are read only for changed files, through the engine's way of opening a
  file; a file that cannot be read is not judged.
- A path a run cannot carry under its own name is left out of the plan, recorded with
  why and counted as not backed up: a name holding ``engine.rclone.QUOTE``, one of
  fullwidth full stops alone or holding an unpaired surrogate (the engine's limits),
  and a name ending in a space or a period, which the engine carries but which this
  tool's ordinary Windows paths would shorten when it opens, renames or creates. Its
  destination copy, if any, stays as it is, and a folder holding it never moves whole.
  Listings themselves read every folder in its
  extended-length spelling (``listing.enumerable``), so a folder so named is listed and
  each file in it is left out by name, whatever the interpreter normalises.
- A source read from a volume snapshot is listed at the source folder inside it (a
  ``\\\\?\\GLOBALROOT\\Device\\...`` path), which lists like any folder; the planning is
  unchanged.

Future direction
----------------
Full verification (``maintenance.verifier``) lists both sides with this package's
``list_tree`` and the plan's time window, and compares content where planning compares
size and time.
"""
