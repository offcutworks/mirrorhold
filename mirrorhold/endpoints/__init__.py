"""
Endpoints — where a job's data lives, by volume identity, never by drive letter.

Structure
---------
    endpoint.py    VolumeIdentity (anchor + file-system serial), Endpoint (identity +
                   path relative to the volume root), relative-path rules;
                   RemoteEndpoint (a remote's name and a folder inside it), and the
                   decoding of a destination's endpoint of either kind.
    volumes.py     VolumeInfo, VolumeScan, the VolumeSource seam, FixedVolumeSource,
                   WindowsVolumeSource, the decoders for the disk queries; a volume's
                   drive (disk_identity, drive_name, lasting_drive) and its kind
                   (DriveKind, decode_seek_penalty, device_kind); the low-free-space rule
                   (low_on_space, below LOW_FREE_PERCENT).
    resolution.py  resolve(): present / absent / ambiguous; endpoint_from_path().
    marker.py      The management folder and the marker proving a destination
                   belongs to a job, and saying whether the job owns it whole.
    shared.py      A shared destination's record: the top-level names its job owns
                   there, and what it leaves where it is beneath them.
    cloud.py       The folders cloud clients synchronise — Windows' Cloud Files
                   registrations and Google Drive for desktop's own list — what
                   each client does with its folder (SyncKind), and their endpoints.
    system.py      The folders Windows and this tool need, which no run moves or
                   writes into, and their endpoints on the present volumes.

Architecture
------------
An endpoint resolves when exactly one present volume carries its identity; a
destination is usable only when, in addition, its marker matches. Identity anchors:
the GPT partition GUID; the MBR disk signature plus partition offset; for a volume no
disk partition carries, the mount manager's volume GUID. The file-system serial is
always part of the identity, so a reformatted volume stops matching and a cloned one
matches twice and is refused.

A destination may instead be a folder of a remote of the user's rclone configuration
(``RemoteEndpoint``): it is found by the remote's name and the folder, proved the job's by
its marker as a drive's folder is, and never among this computer's volumes — ``resolve``
answers it absent, and whether it can be reached is the engine's to say (``remotes``). Its
document holds ``remote`` and ``folder`` where a drive's holds ``volume`` and ``path``, so
every document written for a drive reads as it did. The two kinds never contain one
another.

A volume's **drive** (``VolumeInfo.drive``) is what admission takes turns on: the physical
disk it lies on, named by the identity the disk's layout carries (GPT disk GUID, MBR
signature) or by its number when that cannot be read, so two letters on one disk are one
drive; a volume no single disk carries is a drive of its own. ``drive_name`` gives a
drive the words a person knows it by, from the letters of the volumes on it. Its **kind**
(``VolumeInfo.drive_kind``) is what the disk answers, asked with no access requested on
the handle its layout is read through, of a seek penalty: spinning, solid, or not known
when it does not answer; a volume whose device answers as a file system rather than a disk
is a cloud drive. Admission takes turns, under the user's automatic choice, on every drive
not known to be solid or a cloud drive, a drive's kind being what the user marked it, where
they did, before what its disk answers; only a drive whose name lasts (``lasting_drive``) —
not a disk known by its number — can keep a mark.

The run writes the marker at a destination's confirmed first run, and keeps in the
management folder the versions batches (``versions/``), a copy of each run record
(``runs/``) and the job's self-describing definition (``job.json``); a finished full
verification leaves a copy of its record there too (``verifications/``). The marker
names its schema: one without a schema version predates the field and reads as schema 1,
and a schema this copy does not know reads as unreadable, which stops the destination.

A destination whose first run left what it already held where it is is **shared**: its
marker takes schema 2 and says so, and ``shared.json`` beside it records what the job
owns there. The record lives with the destination because it describes the
destination's content and must outlive a lost job file; a copy of the tool that does not
know it stops at the marker instead of moving what it must leave. A destination that is
its job's whole keeps a schema-1 marker, read alike by every copy.

The marker and a shared destination's record are read and written through the
destination's storage (``storage``), so they prove and describe a destination wherever it
lies; given a folder of this computer, they reach it as a drive's. A remote that stops
answering as its marker or its record is read is never told as either: the storage's error
is raised for the caller to tell as the remote (``storage.destination.raise_if_unanswered``),
and only a marker or a record the remote will not serve while it answers reads as one that
cannot be read.

Design patterns
---------------
``VolumeSource`` is the injection seam; the operating system is touched only by
``WindowsVolumeSource``, ``sync_roots_on_this_computer`` and ``system_folders`` (which reads the
process's environment, injectable, and lists the profiles folder). Disk-query buffers are
decoded by pure functions, so the MBR path is tested without an MBR disk; Drive's folder
list is read from a data folder passed in, so it is tested on a database the test writes.

Caveats
-------
- The MBR anchor is exercised only through its decoders: no MBR volume has been measured.
- A USB disk behind an enclosure or a dock may answer the seek-penalty query as an
  invalid function (measured on the development machine, 2026-10-21, with every enclosure
  and dock tried), and is then of a kind not known; an internal NVMe drive answers solid,
  and StableBit DrivePool's pool disk answers spinning.
- A cloud drive is recognised by having no disk partition behind it (the Google Drive
  volume is exposed as a disk file system device without a partition). A cloud
  drive presented as a partitioned virtual disk — StableBit CloudDrive presents one —
  is not: it is read as an ordinary disk, which may be a destination, a snapshot of
  which is tried, and which is a drive of its own, whose kind is what that disk reports
  unless the user marks it a cloud drive. What such a disk reports of itself is
  unmeasured, so nothing is matched against a guess.
- A drive is taken for a cloud drive only when its device answers as a disk file system
  (``device_kind``), as Google Drive for desktop's does (measured 2026-11-01); a volume
  that answers no device type — one spanning disks, or one that cannot be opened — stays
  of a kind not known, so a failed query never removes turns. Another cloud client's
  drive that answers otherwise is not known until the user marks it.
- Google Drive for desktop's folder list is its own undocumented database, measured on
  Drive 128 (2026-10-28): a folder from this computer is a row naming its volume GUID, and
  My Drive in streaming mode a virtual volume. My Drive's folder in mirror mode is read by
  the row's My Drive flag, which is unmeasured. A Drive version that changes the table is
  said, as a list that could not be read.
- Paths compare case-insensitively; Unicode forms are not normalised (NTFS keeps NFC
  and NFD spellings apart). A remote's name and folder compare case-insensitively too,
  which refuses more overlaps rather than fewer; a remote that wraps another (an
  encrypting remote, an alias) is compared by its own name alone, since what it lies over
  is in the configuration, which is never read.
- ``VolumeInfo.snapshot_eligible`` says which volumes a snapshot is tried on (an NTFS
  or ReFS partition); a source still resolves to its volume's mount point, and the run
  maps it into a snapshot when it makes one (``snapshots``).

Future direction
----------------
Network shares, which are not supported, would need an identity this package cannot
establish today.
"""
