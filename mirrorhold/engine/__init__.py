"""
Engine — the adapter around the pinned rclone binary.

Background
----------
MirrorHold copies files through one engine, rclone, at one pinned version, and reaches a
folder of a remote the user made with rclone through the same binary. The core lists,
plans, judges and performs every change itself; the engine copies the files it is given,
checks an encrypted remote's files, and answers the operations a remote destination needs.
What the core relies on of it is measured against the pinned version and nothing else.

Structure
---------
    rclone.py   The engine itself: finding and opening the pinned binary, every start of it
                (its command, its environment, the configuration it is named), the spelling a
                local path takes in the engine's list and back, opening a source file the
                engine's way, and the two commands a run or a verification drives while they
                work — the copy of an explicit file list and the check of an encrypted
                remote — each with what it reports as it goes, its pause and its stops.
    remote.py   A session on a configuration: one engine serving its remote control, through
                which every other operation on a remote's folders and files goes — its
                features and free space, listings and entries, reads, small writes, moves,
                deletions — and the fingerprint of a file of this computer of a kind only the
                engine computes.

``remote`` builds on ``rclone``: a session is an engine started through the same command and
environment, and controlled through the same kind of loopback remote control, as a copy.
Nothing in this package imports the core but the location of the vendored binary.

Architecture
------------
The only package that knows rclone's name, flags and output; everything else speaks in jobs,
endpoints, plans and outcomes. Above it, the configuration's reach (``remotes``) opens
sessions, and the destination-storage seam (``storage``) reaches a remote's files through
them; a run's application (``run.apply``) drives a copy; a verification
(``maintenance.verifier``) drives the check of an encrypted remote; the plan asks which names
the engine cannot carry. The core still lists, plans and judges every change, on a drive and
on a remote alike.

Every start is windowless, strips ``RCLONE_*`` variables from the environment and names its
configuration: in memory for a start that reaches no remote, the user's own file — never
asked a password for, unlocked only by its password command — for one that does. A run uses
the engine for one operation on a drive: copying an explicit list (``copy --files-from-raw
--no-traverse --local-encoding None``) with a backup directory inside the destination, read
through the engine's JSON log and controlled by suspending its process and through its
remote control on the loopback address, with credentials made for that transfer, of which the
engine is given only a hash in a file. A folder of a remote takes the same copy, without
``--no-traverse``, its batch on that remote, and every other operation on it goes through one
``RemoteSession``. Each process the adapter starts sits in a job object that ends it when the
core process that started it ends.

Design patterns
---------------
- **One way to start.** Every start goes through one command and one environment, so no
  start can forget the configuration, the console window or a stray variable.
- **Measure, then pin.** Each behaviour the core relies on is measured on the pinned binary
  by the engine-contract suite and written beside the code that relies on it; another
  version is refused until the pin moves and that suite has been re-run.
- **One definition of no answer.** An engine that gives no whole answer — not reached, or an
  answer broken off — is one class of failure, shared by a copy's remote control and a
  session, so every caller tells an engine not answering from a refusal the same way.
- **Control by the process and the remote control.** A pause suspends the process. A copy's
  stops, graceful or immediate, are told through its remote control, its process ended only
  where the engine cannot be asked; the check of an encrypted remote is stopped by ending it.

Caveats
-------
The binary lives in the git-ignored ``vendor/`` directory, obtained per machine from the
official release by ``tools/fetch_engine.py``, which checks the release file and the
executable against the pinned digests. ``open_source_file`` mirrors the engine's way of
opening a file (backup semantics, every sharing mode); the engine-contract suite checks that
the two agree.

Local names are taken as they are; the list quotes the few characters the engine's internal
spelling would read as others, and what no spelling carries is left out by the plan before
the engine sees it.

A run that reads its source from a volume snapshot gives a transfer the source folder inside
it, a ``\\\\?\\GLOBALROOT\\Device\\...`` path; the engine copies from such a root as from any
folder (measured on a real snapshot, and pinned by the contract suite on the system volume's
device path).

A listed file of the destination's size whose time differs is read in full on both sides
before the engine decides; when the contents agree it only sets the time. A first run onto a
library whose earlier copy did not keep the times therefore reads that library twice over,
once. A graceful stop's soft cutoff starts no new transfer but does not end the comparisons,
so the run ends the engine once no file is in flight. While it compares, no byte counts as
copied; the count of files checked rises as each comparison ends, and the bytes its process
reads rise throughout (Windows' I/O counters, which its remote control answering does not
move) — what a run reads to tell a long comparison from an engine making no progress.
Sending over the network moves none of those counters; an upload reads its source as it
sends, so the read count rises with it.

Remotes are measured against the engine's own loopback servers (S3, WebDAV, SFTP) and its
local and encrypted backends, never a real service: a real service's limits, precision,
names and trash are known only from a run on it.

Verification reads the source with ``open_source_file`` and a drive destination through its
storage, opened the same way, and never starts the engine for a drive: it hashes the files
itself, so it can pause, stop and continue file by file. On a remote it hashes the source
itself too, of the fingerprint kind the service keeps where it computes that kind; the engine
computes any other kind of a source file (``RemoteSession.local_fingerprint``), and checks an
encrypted remote's files (``CryptCheck``) in batches it pauses by suspending the process and
stops by ending it.

Design direction
----------------
rclone stays behind this package alone: a new need of the engine becomes an operation here,
measured on the pinned binary, never rclone's words or flags in a caller. An operation on a
remote's files joins the session; only the explicit-list copy and the check of an encrypted
remote stay commands of their own. The engine performs; the core keeps every decision.
"""
