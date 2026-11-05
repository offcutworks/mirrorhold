"""
Remotes — the user's rclone configuration as MirrorHold reaches it: which file, how it is
unlocked, the remembered password, and what the engine says of each remote folder a job
names.

Background
----------
A destination may be a folder of a remote the user made with rclone (``endpoints.endpoint.
RemoteEndpoint``). MirrorHold never signs in to a service and never reads or copies the
configuration: the preferences name the file and how it is unlocked (``jobs.model.
RemoteSettings``), and the engine, handed that file, does the rest. An encrypted
configuration is unlocked for an unattended run in one of two ways the user chooses — a
command of their own that prints its password, or the password remembered here, sealed for
their Windows account — or not at all, and is then refused while it is encrypted.

Structure
---------
    seal.py     The remembered password: sealed with Windows' per-user data protection and
                this tool's entropy, written whole, read back and opened to verify, removed
                when the user turns the choice off; a new one sealed beside it on trial and
                taken up whole only once it has opened the configuration.
    facts.py    What the engine said of the configuration and of each remote folder — the
                facts validation judges by — and the capability floor a remote must meet.
    reach.py    The configuration as the engine is handed it, a session opened on it, and a
                look at one remote folder: reached, unreachable or refused, with why; a
                session kept for a run's, a preview's, a verification's or a repair's
                length (``Reached``), which serves each remote folder it reached as storage.

Architecture
------------
Dependencies run one way: this package below the command line and the run, above the job
contract (for the preferences), the engine adapter and ``invocation`` (for the unlock
command). Validation imports only ``facts``: it judges what it is handed and never starts
the engine, so it is tested without one.

The remembered password leaves its sealed file only in the ``unlock`` command, which the
engine runs as the configuration's password command and which prints it to the engine and
exits, and in what sets it (``reach.remember_password``: ``password set`` and the window's
preferences, the window keeping no copy), which opens the configuration with it to verify:
sealed on trial beside the remembered one, which an activity unlocking meanwhile still
reads, and taken up only once that opens. It reaches no command line, no environment
variable and no file but its own and, while it is tried, its trial's.

Design patterns
---------------
A failure the engine reports is classified once, here, by its reason: the service could
not be reached (the remote waits), or it answered with a refusal (the remote fails, with
why). Nothing above reads the engine's words.

Caveats
-------
- A remote that wraps another — an encrypting remote, an alias, a union — may lie over a
  folder another destination names through the other remote. The configuration says so
  and is never read, so two such destinations are not refused as overlapping; the holds,
  the versions and the first run's question still stand between them.
- A configuration whose services rotate their sign-in tokens is rewritten by the engine
  when it refreshes one; two engines on one configuration at once could lose a fresh token.
  Every activity that reaches a remote therefore takes its turn among the others that do
  (``records.admission``). A look — validation, ``resolve``, the window asking for the
  remotes or a remote's folders — and a preview do not wait: a
  preview keeps its session for its whole length, listing the remote, so it can overlap a
  run that reaches a remote. What overlapping can cost is a sign-in to make again, never a
  file; a preview that waited behind a run of hours would cost more each time.

Design direction
----------------
A destination-storage kind beside the drive and the remote stays the seam a network share
would take. The run, the preview, the verification and the repair reach a remote folder
through ``storage.remote``, opened by ``reach``.
"""
