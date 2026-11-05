"""The configuration store: one schema-versioned file holding the jobs, the groups they
are arranged in, and the preferences.

Rules, each one a guarantee a caller may rely on:

- **Atomic save.** The new content is written beside the file, flushed to disk, and
  then replaces it in one rename; a crash leaves either the old file or the new one,
  never a mixture.
- **Previous copies.** Before a save replaces the file, the file it replaces — as the
  save read it — is written into ``previous/`` beside it; once the save is written, the
  newest ``keep_previous`` copies are kept. Both go through ``mirrorhold.exchange``, as
  the file itself does: a copy another program holds is left for a later save to remove,
  never a reason for a save to fail. A save that only rearranges — the GUI's remembered
  state, the order of jobs and groups — keeps none: the window saves that state as it
  closes and the order one move at a time, and their copies would push out the ones
  holding a change.
- **One order.** The jobs are shown in one order everywhere (``in_order``): the groups
  in their order, each holding its jobs in its order, then the jobs in no group in the
  file's order. A group, or a job within its level, moves one place at a time
  (``with_group_moved``, ``with_job_moved``).
- **Groups beside the jobs.** Each job is in at most one group, every member of a group
  is a job of the same file, and no two groups share a name, compared without case
  (``StoreContents``); a file breaking one is corrupt like any contract breach.
- **A corrupt file is moved aside and reported, never silently replaced.** ``load``
  renames it to ``<name>.corrupt-<time>`` and returns an empty store whose status
  says so, with the previous copies a user can restore from. The rename is made under
  the save lock and only while the file still holds the bytes that could not be parsed,
  so of several processes reading one corrupt file at once one moves it aside and the
  others report it moved aside; a file a save replaced in between is read instead.
  A process that only watches the file (the notification-area icon) reads it with
  ``peek``, which moves nothing aside and waits for no save.
- **A file from a newer schema is left untouched** and the store refuses to save
  over it, so an older copy of the tool never destroys a newer one's work.
- **Forward migration.** An older document passes through ``migrations`` (one step
  per version) on load; the file itself is rewritten only by the next save, after
  its old form has gone to ``previous/``.
- **Export and import** carry a job list and the groups of the jobs in it, in a file
  that names its own format and schema version, so it can be migrated like the store.
  An imported job that is added joins its group here: the group of the same
  identifier, else of the same name, else the imported group itself; a job already
  here keeps the group it has here (``merge_imported``).
- **UTF-8, with or without a byte-order mark.** The job file, a previous copy and an
  export are read either way, since an editor may add the mark; the store writes none.
- **Shared between processes.** Every waiting run and verification reads the file
  twice a second while a window or a command may save it, so it is read and written
  through ``mirrorhold.exchange``: a reader never stands in a save's way, and another
  program holding the file is borne for a moment; past it, ``StoreBusy``. Saves are
  serialised by a lock beside the file, and a save given what the file held when it was
  read (``LoadResult.fingerprint``) is refused with ``StoreConflict`` — nothing written —
  when the file changed since: another window or command saved in between. A save's
  rename leaves the file's name empty for a moment, so a read that finds no file looks
  again under that lock before ``load`` reports a fresh store or ``fingerprint`` none.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Collection, Iterable, Mapping, Optional, Sequence, Tuple, Union

from ..contract_errors import ContractError, require_keys
from .. import exchange
from ..records.lock import holding
from ..wording import counted
from .model import Group, Job, Preferences, group_name_key

STORE_FORMAT = "mirrorhold-store"
EXPORT_FORMAT = "mirrorhold-jobs"
SCHEMA_VERSION = 9

Migration = Callable[[dict], dict]


def _add_groups(doc: dict) -> dict:
    """Schema 1 to 2, for the store and an export alike: groups of jobs; a document
    written before them has none."""
    doc["groups"] = []
    return doc


def _add_share_hold(doc: dict) -> dict:
    """Schema 2 to 3, for the store and an export alike: the share hold became a value of
    each job; a job written before it holds at half, as every job then did. A value
    already there is left to the strict decoding that follows."""
    jobs = doc.get("jobs")
    for job in jobs if isinstance(jobs, list) else ():
        holds = job.get("holds") if isinstance(job, dict) else None
        if isinstance(holds, dict):
            holds.setdefault("share", 50)
    return doc


def _add_activity_choices(doc: dict) -> dict:
    """Schema 3 to 4: the choices for activities became preferences — one at a time on each
    drive, the computer kept awake, the activity named at shutdown — each read from a file
    written before them as on, their first values. An export carries no preferences and
    passes unchanged: it shares the store's schema numbers. A value already there is left
    to the strict decoding that follows."""
    preferences = doc.get("preferences")
    if isinstance(preferences, dict):
        preferences.setdefault("activities", {"one_per_drive": True, "keep_awake": True, "name_at_shutdown": True})
    return doc


def _turns_by_drive_kind(doc: dict) -> dict:
    """Schema 4 to 5: one at a time on each drive, on or off, became when activities take
    turns on a drive (``DriveTurns``). On — the first value, which a file cannot tell from
    a choice made — reads as automatic: turns on every drive not known to have no seek
    penalty; off reads as always at the same time. An export carries no preferences and
    passes unchanged. A value that is not on or off is left to the strict decoding that
    follows."""
    preferences = doc.get("preferences")
    activities = preferences.get("activities") if isinstance(preferences, dict) else None
    if isinstance(activities, dict) and isinstance(activities.get("one_per_drive"), bool):
        on = activities.pop("one_per_drive")
        activities["drive_turns"] = "automatic" if on else "at_the_same_time"
    return doc


def _add_drive_marks(doc: dict) -> dict:
    """Schema 5 to 6: what the user says a drive is (``Activities.drive_kinds``); a file
    written before it marks no drive. An export carries no preferences and passes unchanged.
    A value already there is left to the strict decoding that follows."""
    preferences = doc.get("preferences")
    activities = preferences.get("activities") if isinstance(preferences, dict) else None
    if isinstance(activities, dict):
        activities.setdefault("drive_kinds", [])
    return doc


def _add_remotes(doc: dict) -> dict:
    """Schema 6 to 7: a destination may be a remote's folder, and the preferences name the
    rclone configuration remotes are reached through (``RemoteSettings``); a file written
    before them names none, and every destination it holds is a drive's, written as it
    still is. The number moves so that a copy of the tool that knows only schema 6 leaves a
    file holding a remote destination untouched, rather than move it aside as corrupt. An
    export carries no preferences and passes unchanged. A value already there is left to
    the strict decoding that follows."""
    preferences = doc.get("preferences")
    if isinstance(preferences, dict):
        preferences.setdefault("remotes", {"configuration": None, "unlock": "none", "unlock_command": []})
    return doc


def _add_schedule_forms(doc: dict) -> dict:
    """Schema 7 to 8: a job's schedule may run on a day of every month, at several times a
    day, or every N hours inside a window (``jobs.schedule``); every schedule a file written
    before them holds is one of the daily and weekly forms, which keep their text, so
    nothing changes. The number moves so that a copy of the tool that knows only schema 7
    leaves a file holding a new form untouched, rather than move it aside as corrupt. An
    export passes the same way."""
    return doc


def _add_cloud_mark(doc: dict) -> dict:
    """Schema 8 to 9: the user may mark a drive a cloud drive (``jobs.model.MARKED_KINDS``);
    every mark a file written before it holds is a hard disk or a solid-state drive, so
    nothing changes. The number moves so that a copy of the tool that knows only schema 8
    leaves a file holding a cloud drive's mark untouched, rather than move it aside as
    corrupt. An export carries no preferences and passes unchanged."""
    return doc


MIGRATIONS: Mapping[int, Migration] = {1: _add_groups, 2: _add_share_hold, 3: _add_activity_choices,
                                       4: _turns_by_drive_kind, 5: _add_drive_marks, 6: _add_remotes,
                                       7: _add_schedule_forms, 8: _add_cloud_mark}
"""``MIGRATIONS[n]`` turns a version-n document into a version-(n+1) document."""

PREVIOUS_DIR = "previous"
DEFAULT_KEEP_PREVIOUS = 5


SAVE_PATIENCE = 5.0
"""Seconds a save is tried again while another program holds the file."""


class StoreError(Exception):
    """A store operation that cannot be carried out safely."""


class StoreBusy(StoreError):
    """The file stayed held by another program for the whole patience; nothing changed."""


class StoreConflict(StoreError):
    """The file changed since it was read; the save was refused and nothing written."""


class _Unchecked:
    pass


UNCHECKED = _Unchecked()
"""``ConfigStore.save``'s default: save whatever the file holds now."""

Fingerprint = Optional[str]
"""What a file held when it was read: its content's SHA-256, None for no file."""


def fingerprint_of(data: Optional[bytes]) -> Fingerprint:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _document(data: bytes):
    """A file's JSON document: UTF-8, a leading byte-order mark read past — Windows
    PowerShell 5.1 writes one wherever it is told UTF-8. ValueError when it is not JSON."""
    return json.loads(data.decode("utf-8-sig"))


class LoadStatus(str, Enum):
    FRESH = "fresh"              # no file yet
    LOADED = "loaded"
    MIGRATED = "migrated"        # read from an older schema; rewritten by the next save
    CORRUPT = "corrupt"          # moved aside; the store is empty
    UNSUPPORTED = "unsupported"  # a schema this version cannot read; file untouched, saving refused


def _check_groups(groups: Sequence[Group], job_ids: Collection[str]) -> None:
    """The rules that span groups and jobs: identifiers and names unique, every member a
    job of ``job_ids``, each job in at most one group."""
    ids, names, placed = set(), set(), set()
    for i, group in enumerate(groups):
        where = f"groups[{i}]"
        if group.group_id in ids:
            raise ContractError(where, "two groups share an identifier")
        if group_name_key(group.name) in names:
            raise ContractError(f"{where}.name", f"another group is called {group.name.strip()!r}")
        for job_id in group.job_ids:
            if job_id not in job_ids:
                raise ContractError(f"{where}.jobs", f"no job {job_id}")
            if job_id in placed:
                raise ContractError(f"{where}.jobs", f"job {job_id} is in another group too")
            placed.add(job_id)
        ids.add(group.group_id)
        names.add(group_name_key(group.name))


def _moved(items: tuple, index: int, step: int, where: str) -> tuple:
    """``items`` with the one at ``index`` moved ``step`` places; ContractError when that
    would take it past either end."""
    target = index + step
    if step == 0 or not 0 <= target < len(items):
        raise ContractError(where, f"nothing lies {counted(abs(step), 'place')} {'up' if step < 0 else 'down'}")
    moved = list(items)
    moved.insert(target, moved.pop(index))
    return tuple(moved)


@dataclass(frozen=True)
class StoreContents:
    jobs: Tuple[Job, ...] = ()
    preferences: Preferences = field(default_factory=Preferences)
    groups: Tuple[Group, ...] = ()

    def __post_init__(self):
        jobs = tuple(self.jobs)
        ids = [j.job_id for j in jobs]
        if len(set(ids)) != len(ids):
            raise ContractError("jobs", "two jobs share an identifier")
        groups = tuple(self.groups)
        _check_groups(groups, set(ids))
        object.__setattr__(self, "jobs", jobs)
        object.__setattr__(self, "groups", groups)

    def job(self, job_id: str) -> Optional[Job]:
        return next((j for j in self.jobs if j.job_id == job_id), None)

    def with_job(self, job: Job) -> "StoreContents":
        """Add the job, or replace the one with its identifier in place."""
        if self.job(job.job_id) is None:
            return replace(self, jobs=self.jobs + (job,))
        return replace(self, jobs=tuple(job if j.job_id == job.job_id else j for j in self.jobs))

    def without_job(self, job_id: str) -> "StoreContents":
        """Remove the job, from its group too."""
        return replace(self, jobs=tuple(j for j in self.jobs if j.job_id != job_id),
                       groups=tuple(replace(g, job_ids=tuple(i for i in g.job_ids if i != job_id))
                                    for g in self.groups))

    def group(self, group_id: str) -> Optional[Group]:
        return next((g for g in self.groups if g.group_id == group_id), None)

    def group_of(self, job_id: str) -> Optional[Group]:
        return next((g for g in self.groups if job_id in g.job_ids), None)

    def group_named(self, name: str) -> Optional[Group]:
        key = group_name_key(name)
        return next((g for g in self.groups if group_name_key(g.name) == key), None)

    def with_group(self, group: Group) -> "StoreContents":
        """Add the group, or replace the one with its identifier in place."""
        if self.group(group.group_id) is None:
            return replace(self, groups=self.groups + (group,))
        return replace(self, groups=tuple(group if g.group_id == group.group_id else g for g in self.groups))

    def without_group(self, group_id: str) -> "StoreContents":
        """Remove the group; its jobs stay, in no group."""
        return replace(self, groups=tuple(g for g in self.groups if g.group_id != group_id))

    def in_order(self) -> Tuple[Job, ...]:
        """The jobs in the order every surface shows them: each group's jobs in the group's
        order, the groups in theirs, then the jobs in no group in the file's order."""
        by_id = {j.job_id: j for j in self.jobs}
        grouped = tuple(by_id[job_id] for g in self.groups for job_id in g.job_ids)
        placed = {j.job_id for j in grouped}
        return grouped + tuple(j for j in self.jobs if j.job_id not in placed)

    def with_group_moved(self, group_id: str, step: int) -> "StoreContents":
        """The group moved ``step`` places among the groups (negative: up)."""
        index = next((i for i, g in enumerate(self.groups) if g.group_id == group_id), None)
        if index is None:
            raise ContractError("groups", f"no group {group_id}")
        return replace(self, groups=_moved(self.groups, index, step, "groups"))

    def with_job_moved(self, job_id: str, step: int) -> "StoreContents":
        """The job moved ``step`` places within its level (negative: up): among its group's
        jobs, or among the jobs in no group. It never leaves its group: that is
        ``regrouped``."""
        if self.job(job_id) is None:
            raise ContractError("jobs", f"no job {job_id}")
        group = self.group_of(job_id)
        if group is not None:
            members = _moved(group.job_ids, group.job_ids.index(job_id), step, "groups.jobs")
            return self.with_group(replace(group, job_ids=members))
        loose = tuple(j for j in self.jobs if self.group_of(j.job_id) is None)
        order = iter(_moved(loose, next(i for i, j in enumerate(loose) if j.job_id == job_id), step, "jobs"))
        return replace(self, jobs=tuple(next(order) if self.group_of(j.job_id) is None else j for j in self.jobs))

    def regrouped(self, job_ids: Sequence[str], group_id: Optional[str]) -> "StoreContents":
        """The jobs moved into group ``group_id`` — after its members, in the order given,
        a job already in it keeping its place — or out of every group with None."""
        if group_id is not None and self.group(group_id) is None:
            raise ContractError("groups", f"no group {group_id}")
        moving = tuple(dict.fromkeys(job_ids))

        def regroup(group: Group) -> Group:
            if group.group_id == group_id:
                return replace(group, job_ids=group.job_ids + tuple(j for j in moving if j not in group.job_ids))
            return replace(group, job_ids=tuple(j for j in group.job_ids if j not in moving))

        return replace(self, groups=tuple(regroup(g) for g in self.groups))

    def to_document(self) -> dict:
        return {"format": STORE_FORMAT, "schema_version": SCHEMA_VERSION,
                "jobs": [j.to_document() for j in self.jobs],
                "groups": [g.to_document() for g in self.groups],
                "preferences": self.preferences.to_document()}


@dataclass(frozen=True)
class JobList:
    """What an export file carries: jobs, and the groups they were in where they were
    exported from, each holding only the jobs of the file."""

    jobs: Tuple[Job, ...] = ()
    groups: Tuple[Group, ...] = ()


@dataclass(frozen=True)
class LoadResult:
    contents: StoreContents
    status: LoadStatus
    detail: str = ""
    moved_to: Optional[Path] = None
    previous_copies: Tuple[Path, ...] = ()
    fingerprint: Fingerprint = None   # what the file held when read (None: no file, or moved aside)

    @property
    def writable(self) -> bool:
        return self.status is not LoadStatus.UNSUPPORTED


@dataclass(frozen=True)
class ImportOutcome:
    added: Tuple[str, ...] = ()
    replaced: Tuple[str, ...] = ()
    unchanged: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()   # same identifier, different content, not replaced


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _decode_jobs(docs, where: str) -> Tuple[Job, ...]:
    if not isinstance(docs, list):
        raise ContractError(where, "expected a list")
    jobs = []
    for i, doc in enumerate(docs):
        try:
            jobs.append(Job.from_document(doc))
        except ContractError as exc:
            raise exc.within(f"{where}[{i}]") from None
    return tuple(jobs)


def _decode_groups(docs, where: str) -> Tuple[Group, ...]:
    if not isinstance(docs, list):
        raise ContractError(where, "expected a list")
    groups = []
    for i, doc in enumerate(docs):
        try:
            groups.append(Group.from_document(doc))
        except ContractError as exc:
            raise exc.within(f"{where}[{i}]") from None
    return tuple(groups)


def _without_view_state(doc: dict) -> dict:
    preferences = doc.get("preferences")
    if not isinstance(preferences, dict):
        return doc
    return dict(doc, preferences={k: v for k, v in preferences.items() if k != "gui_state"})


def _unordered(doc: dict) -> dict:
    """``doc`` without what only arranges it: the GUI's remembered state, and the order of
    the jobs, of the groups and of each group's jobs."""
    doc = _without_view_state(doc)

    def as_set(items):
        return sorted(json.dumps(item, sort_keys=True) for item in items) if isinstance(items, list) else items

    groups = doc.get("groups")
    if isinstance(groups, list):
        groups = [dict(g, jobs=sorted(g["jobs"], key=str)) if isinstance(g, dict) and isinstance(g.get("jobs"), list)
                  else g for g in groups]
    return dict(doc, jobs=as_set(doc.get("jobs")), groups=as_set(groups))


def _arrangement_only(before: Optional[dict], after: dict) -> bool:
    """Whether a save of ``after`` over ``before`` changes how the jobs are arranged — the
    GUI's remembered state, the order of jobs and groups — and nothing else."""
    if before is None or before == after:
        return False
    return _unordered(before) == _unordered(after)


class ConfigStore:
    def __init__(self, path: Path, *, keep_previous: int = DEFAULT_KEEP_PREVIOUS,
                 schema_version: int = SCHEMA_VERSION, migrations: Mapping[int, Migration] = MIGRATIONS):
        self.path = Path(path)
        self.keep_previous = keep_previous
        self.schema_version = schema_version
        self.migrations = dict(migrations)

    @property
    def previous_dir(self) -> Path:
        return self.path.parent / PREVIOUS_DIR

    @property
    def _staging(self) -> Path:
        return self.path.with_name(self.path.name + ".saving")

    @property
    def save_lock(self) -> Path:
        """Held while a save compares the file with what its caller read and replaces it."""
        return self.path.with_name(self.path.name + ".lock")

    def _read(self) -> Optional[bytes]:
        """The file's bytes, None when there is none; StoreBusy when it stayed held."""
        try:
            return exchange.read_bytes(self.path)
        except FileNotFoundError:
            return None
        except exchange.FileBusy:
            raise StoreBusy("the job file could not be read: another program kept it open; try again") from None

    def _read_settled(self) -> Optional[bytes]:
        """As ``_read``, with no file confirmed under the save lock. A save's rename takes the
        old file's name away a moment before it gives it to the new one, and a read in that
        moment finds no file (measured on the development machine under load); every save
        holds the lock across its rename, so a read under it cannot fall there. With no lock
        file no save has begun — a save makes it before it reads — and nothing is made."""
        raw = self._read()
        if raw is None and self.save_lock.exists():
            with holding(self.save_lock):
                raw = self._read()
        return raw

    def fingerprint(self) -> Fingerprint:
        """What the file holds now (``LoadResult.fingerprint``); StoreBusy when it stayed held."""
        return fingerprint_of(self._read_settled())

    def previous_copies(self) -> Tuple[Path, ...]:
        """Previous copies, newest first."""
        if not self.previous_dir.is_dir():
            return ()
        prefix = self.path.stem + "."
        copies = [p for p in self.previous_dir.iterdir()
                  if p.is_file() and p.name.startswith(prefix) and p.suffix == self.path.suffix]
        return tuple(sorted(copies, key=lambda p: p.name, reverse=True))

    # --- reading -----------------------------------------------------------------

    def _upgrade(self, doc: dict, expected_format: str) -> Tuple[dict, bool]:
        """The document at the current schema version, and whether it was migrated.
        Raises ContractError for a document that is not this format at all, and
        StoreError for a schema version this store cannot reach."""
        if not isinstance(doc, dict) or doc.get("format") != expected_format:
            raise ContractError("format", f"not a {expected_format} document")
        version = doc.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ContractError("schema_version", f"not a schema version: {version!r}")
        if version > self.schema_version:
            raise StoreError(f"written by a newer version of the tool (schema {version}; "
                             f"this version reads up to {self.schema_version})")
        migrated = False
        while version < self.schema_version:
            step = self.migrations.get(version)
            if step is None:
                raise StoreError(f"no migration from schema {version}")
            doc = step(json.loads(json.dumps(doc)))
            version += 1
            doc["schema_version"] = version
            migrated = True
        return doc, migrated

    def _parse(self, doc: dict) -> StoreContents:
        require_keys(doc, "", ("format", "schema_version", "jobs", "groups", "preferences"))
        jobs = _decode_jobs(doc["jobs"], "jobs")
        groups = _decode_groups(doc["groups"], "groups")
        try:
            preferences = Preferences.from_document(doc["preferences"])
        except ContractError as exc:
            raise exc.within("preferences") from None
        return StoreContents(jobs, preferences, groups)

    def load(self) -> LoadResult:
        """The store as the file holds it. StoreBusy when another program kept the file
        for the whole patience: nothing is moved aside, nothing is reported as corrupt.
        A file is moved aside only once it was read whole and could not be parsed."""
        return self._load(again=True)

    def peek(self) -> Optional[StoreContents]:
        """The store as the file holds it now, for a process that only watches it (the
        notification-area icon): None where there is no file, another program kept it for the
        whole patience, or it is not a job file this version reads. Unlike ``load`` it moves
        nothing aside and waits for no save: a watcher never acts on the user's file."""
        try:
            raw = self._read()
        except (StoreBusy, OSError):
            return None
        if raw is None:
            return None
        try:
            doc, _ = self._upgrade(_document(raw), STORE_FORMAT)
            return self._parse(doc)
        except (ValueError, ContractError, StoreError):
            return None

    def _load(self, *, again: bool) -> LoadResult:
        raw = self._read_settled()
        if raw is None:
            return LoadResult(StoreContents(), LoadStatus.FRESH, previous_copies=self.previous_copies())
        try:
            doc = _document(raw)
            doc, migrated = self._upgrade(doc, STORE_FORMAT)
            contents = self._parse(doc)
        except StoreError as exc:
            return LoadResult(StoreContents(), LoadStatus.UNSUPPORTED,
                              f"{self.path} was left untouched: {exc}", previous_copies=self.previous_copies(),
                              fingerprint=fingerprint_of(raw))
        except (ValueError, ContractError) as exc:
            # Every waiting activity reloads the file twice a second, so another process may
            # move it aside, or a save replace it, between this read and the move: the move is
            # made under the save lock and only of the bytes this read could not parse.
            with holding(self.save_lock):
                now = self._read()
                moved = self._move_aside() if now == raw else None
            if moved is not None:
                return LoadResult(StoreContents(), LoadStatus.CORRUPT,
                                  f"{self.path} could not be read ({exc}); it was moved to {moved}",
                                  moved_to=moved, previous_copies=self.previous_copies())
            if now is not None and again:
                return self._load(again=False)
            return LoadResult(StoreContents(), LoadStatus.CORRUPT,
                              f"{self.path} could not be read ({exc}); another process reading it at the same "
                              "time moved it aside", previous_copies=self.previous_copies())
        if migrated:
            return LoadResult(contents, LoadStatus.MIGRATED,
                              f"read from an older schema; the next save writes schema {self.schema_version}",
                              previous_copies=self.previous_copies(), fingerprint=fingerprint_of(raw))
        return LoadResult(contents, LoadStatus.LOADED, previous_copies=self.previous_copies(),
                          fingerprint=fingerprint_of(raw))

    def _move_aside(self) -> Optional[Path]:
        """Where the file was moved; None when it is gone already. Called under the save lock."""
        target = self.path.with_name(f"{self.path.stem}.corrupt-{_now_stamp()}{self.path.suffix}")
        deadline = time.monotonic() + SAVE_PATIENCE
        while True:
            try:
                os.replace(self.path, target)
                return target
            except FileNotFoundError:
                return None
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise StoreBusy("the job file could not be read: another program kept it open; try again") \
                        from None
            time.sleep(0.05)

    # --- writing -----------------------------------------------------------------

    def _guard_newer(self, current: Optional[dict]) -> None:
        version = current.get("schema_version") if current is not None else None
        if isinstance(version, int) and not isinstance(version, bool) and version > self.schema_version:
            raise StoreError(f"{self.path} was written by a newer version of the tool (schema {version}); "
                             "it is not overwritten")

    def save(self, contents: StoreContents, *, expected: Union[Fingerprint, _Unchecked] = UNCHECKED) -> Fingerprint:
        """Save ``contents`` and return what the file holds now. ``expected``: what the
        file held when the caller read it (``LoadResult.fingerprint``, None for no file);
        when it holds anything else, StoreConflict and nothing is written. StoreBusy when
        another program kept the file for the whole patience, nothing written."""
        with holding(self.save_lock):
            raw = self._read()
            if not isinstance(expected, _Unchecked) and fingerprint_of(raw) != expected:
                raise StoreConflict("the job file changed since it was read; nothing was saved")
            try:
                current = _document(raw) if raw is not None else None
            except ValueError:
                current = None
            current = current if isinstance(current, dict) else None
            self._guard_newer(current)
            doc = contents.to_document()
            doc["schema_version"] = self.schema_version
            text = json.dumps(doc, indent=2, ensure_ascii=False)
            keep = raw is not None and not _arrangement_only(current, json.loads(text))
            self._write_atomically(text, previous=raw if keep else None)
            return fingerprint_of(text.encode("utf-8"))

    def _write_atomically(self, text: str, *, previous: Optional[bytes] = None) -> None:
        """Replace the file with ``text``; ``previous``, the file it replaces as the save read
        it, is kept in ``previous/`` first. The job file is not opened again for the copy: a
        second open is one more moment another program could refuse it in, outside the
        exchange's patience."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if previous is not None:
                self._keep_previous_copy(previous)
            exchange.write_text(self.path, text, durable=True, patience=SAVE_PATIENCE, staging=self._staging)
        except exchange.FileBusy:
            raise StoreBusy("the job file could not be saved: another program kept it open; try again") from None
        if previous is not None:
            self._prune_previous_copies()

    def _keep_previous_copy(self, data: bytes) -> None:
        self.previous_dir.mkdir(exist_ok=True)
        stamp = _now_stamp()
        target = self.previous_dir / f"{self.path.stem}.{stamp}{self.path.suffix}"
        n = 1
        while target.exists():
            target = self.previous_dir / f"{self.path.stem}.{stamp}-{n}{self.path.suffix}"
            n += 1
        exchange.write_bytes(target, data, durable=True, patience=SAVE_PATIENCE)

    def _prune_previous_copies(self) -> None:
        """Remove all but the newest ``keep_previous`` copies, once the save is written. A copy
        another program holds is left for a later save to remove: a save never fails on it."""
        for old in self.previous_copies()[self.keep_previous:]:
            exchange.remove(old, patience=0.0)

    def restore(self, copy: Path) -> LoadResult:
        """Make a previous copy (or a file moved aside) the current store. The copy is
        read and validated first; the file it replaces, if any, becomes a previous copy."""
        try:
            doc = _document(Path(copy).read_bytes())
            doc, _ = self._upgrade(doc, STORE_FORMAT)
            contents = self._parse(doc)
        except (OSError, ValueError, ContractError, StoreError) as exc:
            raise StoreError(f"{copy} cannot be restored: {exc}") from None
        self.save(contents)
        return self.load()

    # --- export and import --------------------------------------------------------

    def export_text(self, jobs: Iterable[Job], *, groups: Iterable[Group] = ()) -> str:
        """The export of ``jobs``, with those of ``groups`` that hold any of them, each
        holding only those: what ``export_jobs`` writes, and what a run keeps in a
        destination's management folder as the job's self-describing definition."""
        jobs = tuple(jobs)
        exported = {j.job_id for j in jobs}
        kept = [replace(g, job_ids=tuple(i for i in g.job_ids if i in exported)) for g in groups]
        doc = {"format": EXPORT_FORMAT, "schema_version": self.schema_version,
               "jobs": [j.to_document() for j in jobs],
               "groups": [g.to_document() for g in kept if g.job_ids]}
        return json.dumps(doc, indent=2, ensure_ascii=False)

    def export_jobs(self, jobs: Iterable[Job], target: Path, *, groups: Iterable[Group] = ()) -> None:
        """Write ``jobs`` to ``target`` as ``export_text`` gives them."""
        text = self.export_text(jobs, groups=groups)
        target = Path(target)
        staging = target.with_name(target.name + ".saving")
        with open(staging, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, target)

    def read_export(self, source: Path) -> JobList:
        """The jobs and groups in an export file, migrated and validated. Raises StoreError."""
        try:
            doc = _document(Path(source).read_bytes())
            doc, _ = self._upgrade(doc, EXPORT_FORMAT)
            require_keys(doc, "", ("format", "schema_version", "jobs", "groups"))
            jobs = _decode_jobs(doc["jobs"], "jobs")
            groups = _decode_groups(doc["groups"], "groups")
            StoreContents(jobs, groups=groups)
            return JobList(jobs, groups)
        except (OSError, ValueError, ContractError, StoreError) as exc:
            raise StoreError(f"{source} cannot be imported: {exc}") from None


def merge_imported(contents: StoreContents, imported: Sequence[Job], *, groups: Sequence[Group] = (),
                   replace_existing: bool = False) -> Tuple[StoreContents, ImportOutcome]:
    """Fold imported jobs into a store's contents. A job whose identifier is new is
    added; an identical one is left as it is; a different one with the same
    identifier replaces the existing job only when ``replace_existing`` is set and is
    reported as a conflict otherwise.

    ``groups``: the imported groups. A job added joins the group here with its imported
    group's identifier, else the one of its name, else that group, added; a job that was
    here keeps the group it has here."""
    added, replaced, unchanged, conflicts = [], [], [], []
    for job in imported:
        existing = contents.job(job.job_id)
        if existing is None:
            contents = contents.with_job(job)
            added.append(job.job_id)
        elif existing == job:
            unchanged.append(job.job_id)
        elif replace_existing:
            contents = contents.with_job(job)
            replaced.append(job.job_id)
        else:
            conflicts.append(job.job_id)
    for group in groups:
        members = [job_id for job_id in group.job_ids if job_id in added]
        if not members:
            continue
        here = contents.group(group.group_id) or contents.group_named(group.name)
        if here is None:
            here = replace(group, job_ids=())
            contents = contents.with_group(here)
        contents = contents.regrouped(members, here.group_id)
    return contents, ImportOutcome(tuple(added), tuple(replaced), tuple(unchanged), tuple(conflicts))
