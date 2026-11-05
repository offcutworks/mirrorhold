"""
Storage — the destination-storage seam: the one place that reaches a destination's files.

Background
----------
A destination is where a backup is kept: a folder on a drive of this computer, or a folder
of a remote of the user's rclone configuration. The run, the preview, the version cleanup,
the verification, the repair, the records' copies and the window all read and write a
destination's files; they do it through this seam, so none of them assumes a path of this
computer, and a remote destination is reached with the same safety as a drive.

Structure
---------
    destination.py  The seam (``DestinationStorage``): what a destination is (in words, as
                    the engine is handed it, its time precision, its free space), reading
                    (entries, a listing, what a folder holds, a small file, a file's leading
                    bytes, a file as a stream), comparing a copy without reading it (the
                    fingerprints it keeps, whether it is encrypted and the engine's check of
                    the encrypted form) and changing it (a small file written whole, the
                    management folder, folders, moves, removals, a batch deleted whole);
                    ``StoredEntry``, ``StorageError`` (``StorageUnanswered`` where a
                    remote's engine gave no whole answer), ``raise_if_unanswered`` (a read's
                    failure told as a remote that stopped answering or as the file) and the
                    management folder's name.
    drive.py        The drive kind: a folder of this computer, each operation the one the
                    core performs on a drive; ``destination_storage``, which takes a folder
                    of this computer given as a path as a drive's.
    remote.py       The remote kind: a folder of a remote, through the engine serving a
                    session (``engine.remote.RemoteSession``).

Architecture
------------
The core still lists, plans and judges every change; a kind performs only the operation it
is handed, and decides nothing. The policy that guards a destination — the marker's proof,
a shared destination's record, the fences around the version cleanup, what a removal moves
into versions, what reappeared after such a move — is written once, above the seam, and
holds for both kinds. Paths are carried relative paths, backslash-separated, below the
destination's folder; every failure is an ``OSError`` (``FileNotFoundError`` for what is not
there), so the code above handles a remote's refusal as a drive's.

Dependencies run one way: ``planning.listing`` and ``exchange`` below the drive kind, the
engine adapter below the remote kind, and the seam below ``endpoints``, ``run``,
``maintenance``, ``records`` and the window. ``destination.py`` imports neither kind, so
``planning.listing`` takes the management folder's name from it without a cycle.

Design patterns
---------------
A strategy behind an abstract base: callers hold a ``DestinationStorage`` and never test
its kind. A caller that holds only a folder of this computer passes the path, and the
functions above the seam make it a drive's (``destination_storage``). A drive's storage is
not a path: ``storage / name`` gives a path below its folder, and a remote's storage has no
such join, so code that still builds a path from a storage fails on a remote at once.

Caveats
-------
- On a drive a move is a rename on the destination's own volume, atomic; on a remote it is
  the service's move, or a copy and a delete where the service only copies within itself
  (S3), each keeping the file's time (measured on the engine's loopback backends).
- A service with no folders of its own lists any folder as if it were there, keeps no empty
  folder and makes a file's folders as the file arrives: ``is_folder`` there means a folder
  that holds something, and a listing cannot tell a missing destination folder from an
  empty one — the marker decides a first run.
- A remote's small files are written by one upload, which the service makes visible whole;
  no other program holds a remote's file open, so a write there has nothing to wait for.
- A drive's small files — the marker, a shared destination's record — are only ever renamed
  over, which takes the old file's name away for a moment, so a drive's ``read_file`` looks
  again for one not there for ``exchange.RENAME_PATIENCE``: a destination with none yet is
  told so that much later.
- A service may tell names apart by case alone, or keep two of one name in a folder; the
  plan compares names without case, so a remote listing sets such names apart
  (``TreeListing.ambiguous``) and the run stops the pair rather than guess.
- A service with a trash, or older versions, of its own may keep what is deleted there and
  count it against its space until it removes it (``may_keep_deleted``); the version cleanup
  says so where it deletes there.

Design direction
----------------
Every unit that reaches a destination's files goes through the seam; the job contract
names a remote destination, the run, the preview, the verification and the repair reach
one, and the window reaches one as it comes to know it. A network share, if one is ever a
destination, is a kind of its own here.
"""
