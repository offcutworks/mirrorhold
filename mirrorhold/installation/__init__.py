"""
Installation — the copy of the tool that scheduled tasks and the Start menu entry start.

Background
----------
Automatic runs must not run a working copy, which may be half-edited at any moment.
They start an installed copy instead, of one of two kinds:

- the **source installation**: the two packages, the licence and the pinned engine,
  copied into ``installed/`` beside a working copy (git-ignored), with the program's icon
  drawn there and a record of what was installed and for which store. Updating is
  installing again; uninstalling removes the tasks, the Start menu entry and the copy;
- a **portable release**, unpacked from a release zip — the packages, the interpreter it
  carries and the engine — set up where it lies: nothing of the program is copied, a record
  beside its store names its folder as it was reached, and its icon is kept beside the
  store for the Start menu entry. A newer release, unpacked anywhere, is set up the same way
  and takes everything over; one a package manager puts in the folder set up — a junction
  it points at the new version, a folder it unpacks the new version into — is served there
  with nothing set up again. Removing the setup leaves the folder for the user to delete.

A release's setup program, built beside its zip from the same files (``tools/setup/``),
makes no third kind: it puts a portable release in ``%LOCALAPPDATA%\\Programs\\MirrorHold`` and
has it set itself up with ``install``; a newer one replaces the files in that folder, which
the setup record already names, so nothing is set up again. Its uninstaller asks the copy to
remove its setup only if it is the one set up (``uninstall`` with ``only_if_here``), then
deletes the folder, so another copy's setup outlives it. The package knows nothing of the
setup program beyond answering those two commands.

Neither kind ever touches the store, the records or any destination.

Structure
---------
    manifest.py    InstalledCopy and ``installation.json`` (schema 2, ``kind`` source or
                   portable): version, time, the store served, the windowless
                   interpreter, the folder started, the working copy installed from or
                   the release's label; schema 1 read as a source installation.
    release.py     Release and ``build.json``: a portable release's label and
                   interpreter version, written by the build, read here.
    shortcut.py    The Start menu entry (opening the installed GUI), the application
                   identity it carries, which Windows notifications need, and the
                   icon it names, which the entry and those notifications show.
    installer.py   install(), uninstall(), installation_state(); InstallError.

Architecture
------------
The installer copies files (from a working copy only), writes the record and the Start
menu entry, which names the program's icon — a set-up release's own, kept beside its
store, or the one drawn into the source installation's copy by the working copy's
drawing script, which the installer starts as a process and never imports. It registers no task itself:
afterwards it calls the core's automation (``automation.reconcile``), which derives the
tasks from the jobs and points them at the copy installed, and uninstalling asks the
same automation to remove them. What the entry
and the tasks start is composed by ``invocation``. Which kind of copy the code is comes
from ``locations.copy_kind``: a source installation's copy carries its record at its
root, so it knows it is installed and serves the store named there; a portable release
carries its build record, serves the store in ``%LOCALAPPDATA%\\MirrorHold``, and keeps
its setup record beside that store, never in its own folder.

The two kinds share the task folder, the Start menu entry and the notification identity,
so one account holds one of them: a working copy does not install while a portable
release is set up (the record beside the portable store), and a portable release is not
set up while the Start menu entry starts the source installation's copy.

Design patterns
---------------
- Build beside, then swap by renaming: a copy in use (an open window, a running task)
  cannot be renamed, so it is refused before anything changes.
- Refused from the source installation's copy itself, and while a run is in progress.
- The Start menu entry is the account's token of which installation holds the shared
  names: the folder it starts says whose it is.
- A caller naming its own entry or installed copy (a test) stands for another account,
  so the user's portable record is consulted only where the caller names it.

Caveats
-------
- The source installation runs the interpreter the working copy ran under; moving that
  environment breaks the tasks and the Start menu entry until the next install.
- The installed GUI cannot install, update or uninstall: those are the working copy's.
- A portable release moved or deleted after it was set up leaves its tasks pointing at
  where it was; nothing is registered for it then (``AutomationState.GONE``) until a copy
  is set up again, which the release opened next offers at start.
- A ``build.json`` that cannot be read still marks a portable release; its label is then
  said to be unknown.
- A copy is known by the path it was reached by: a release set up through a package
  manager's junction stays set up there, while one opened from the versioned folder the
  junction points at is another copy, and asks to be used instead.
- A release moved or deleted after it was set up leaves the Start menu entry starting
  nothing until a copy is set up again, though it keeps its icon, which lies beside the
  store. A source installation whose icon could not be drawn says so, and its entry shows
  the interpreter's icon.

Future direction
----------------
The build that writes ``build.json`` and assembles a portable release lives outside the
packages; this package only reads what it writes. Nothing else is planned beyond keeping
the copied set in step with the packages.
"""
