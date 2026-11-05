"""
Snapshots — reading a live source from a point-in-time copy of its volume.

Background
----------
Some sources are live working documents: a mailbox, a database, an editor's file that a
program keeps open, a folder a sync client writes into while it is read. Read as
they are, such files are skipped when another program holds them without sharing (as a
scheduled run's process measurably does) or copied mid-change. A volume snapshot is a
frozen, read-only copy of the whole volume at one instant: nothing in it is held, and
nothing in it changes while it is read. A run reads its source from one when it can,
and as it is otherwise, saying why.

Structure
---------
    provider.py  the seam: SnapshotProvider (list a volume's snapshots, make one, remove
                 one by identifier, read the volume's room for snapshots), Snapshot (its
                 device path), SnapshotRoom, SnapshotError with one of four kinds;
                 UnavailableProvider; default_snapshot_provider(), which
                 MIRRORHOLD_SNAPSHOTS=off turns into one that makes nothing.
    windows.py   WmiSnapshotProvider: Windows' Volume Shadow Copy Service through WMI
                 (Win32_ShadowCopy, the ClientAccessible context; Win32_ShadowStorage for
                 the room), by pywin32's COM client.
    own.py       the snapshots this tool made, one file per run beside the store's other
                 runtime files (``snapshots/<run id>.json``), written before a snapshot is
                 asked for and again once it exists.
    reading.py   SourceSnapshot: the decision for one run (snapshot, or as it is and why),
                 the snapshot's source folder, its removal at the run's end;
                 settled_without_asking(): the decision's steps that need nothing from
                 Windows, which a preview states; remove_leftovers(): what an earlier run
                 left, by recorded identifier only.

Architecture
------------
``run.runner`` owns the lifetime: once the run is admitted (``records.admission``) it
removes what runs that died left, then asks ``SourceSnapshot.take`` how to read the
source, lists and plans and copies from the folder it names, and releases the snapshot
in the same ``finally`` that closes its status. Runs work at the same time, so making,
recording and removing a snapshot hold the snapshot lock, one run at a time, and a
leftover is removed only when its run's liveness lock is free. Nothing else changes:
planning, the gate and the engine adapter take a source root, and a snapshot's device path
(``\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy<n>\\<path>``) is one — the listing, the
engine-style open and the engine all read it like any folder (measured). The preview
reads the source as it is: it changes nothing, and the run lists again before it applies.
It says how the run will read it from ``settled_without_asking`` alone, asking Windows
nothing, so what the snapshots already on a volume or a refusal decide stays the run's.

Design patterns
---------------
- An injected provider, as ``VolumeSource`` is for volumes: the run's decisions are
  tested against a provider in memory, and only ``windows`` touches the operating system.
- Record before acting: the request is on disk before the snapshot is asked for, its
  identifier as soon as it exists, so a crash at any point leaves either nothing or a
  record the next run acts on.

Caveats
-------
- A volume holding snapshots this tool did not make (restore points, previous versions)
  gets a snapshot only while its room for snapshots has room to spare
  (``reading.room_to_spare``), and is read as it is otherwise: once that room fills,
  Windows deletes the oldest snapshots to make space, and the user's would go first (the
  maintainer's decisions of 2026-10-06 and 2026-10-17). The spare is a margin, not a
  guarantee: what the run's own snapshot grows by is known only afterwards. A system drive
  whose room is full of restore points is such a volume, and sources on it are read as
  they are.
- Making a snapshot needs administrator rights; without them the run reads as it is.
- A crash while a snapshot is being made leaves a request with no identifier; a
  snapshot made in that instant cannot be told from others and is left, and reported.
- The snapshot's room on its volume grows with what is written to the volume while the
  run reads it; a destination on the source's own volume adds to it.
- Runs at the same time whose sources lie on one volume each make a snapshot of their
  own, so the volume holds one per run while they last.
- Snapshots of a DrivePool's member disks are out of scope; the pool volume itself is
  tried like any NTFS volume and read as it is if the pool refuses.

Future direction
----------------
Full verification reads its source as it is, never from a snapshot: a pass can last
days across shutdowns, longer than a snapshot should. Reading it through the same
``SourceSnapshot`` per sitting would be the route if held files prove common.
"""
