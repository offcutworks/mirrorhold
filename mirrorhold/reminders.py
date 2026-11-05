"""Reminders: what a finished activity left for the user to look at, kept until the user opens
the window.

A notice that asks something of the user (``notifications.message.Notice.asks``) — removals or
a first run held for review, a run that failed or keeps missing files, a verification that found
differences or did not finish, a repair that left copies or did not finish, an automatic run
that could not start — also leaves a reminder in the store's runtime folder
(``RuntimeLayout.reminders_dir``), one per job, the job's latest replacing the one before. Its
process then brings up the notification-area icon (``tray.offer``), which stays while any
reminder stands, shows each, and goes once the user has opened the window: the window clears
every reminder of its store as it opens and whenever it has the focus (the maintainer's
decision, 2026-11-03). The icon's menu also dismisses them.

A reminder follows the notice it came from: the user's choices of which notices to hear
decide it, and a process that raises no notice leaves none. Like the icon, it is a courtesy: a
reminder that cannot be written or read costs the reminder, never the activity, and a file that
cannot be read is still shown, in general words, until it is cleared.

Nothing starts at sign-in, so a reminder outlives a restart only as a file: the icon shows it
again with the store's next activity, and the window clears it as ever.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import exchange
from .contract_errors import ContractError, require_keys
from .locations import RuntimeLayout, runtime_layout
from .notifications.message import Notice
from .records import diagnostics
from .records.record import json_text
from .tray import offer

REMINDER_FORMAT = "mirrorhold-reminder"
REMINDER_SCHEMA = 1
SUFFIX = ".json"
STAGING_SUFFIX = ".writing"
STALE_STAGING_SECONDS = 60.0
"""How old a staging file (``exchange.private_staging``) is before ``clear`` takes it for one a
writer left as it died: a write in progress takes a fraction of a second."""
JOB_ID = re.compile(r"[0-9a-f]{32}")
UNREADABLE_TITLE = "an activity needs your attention"
"""What a reminder whose file cannot be read is shown as."""


@dataclass(frozen=True)
class Reminder:
    job_id: str
    title: str
    lines: Tuple[str, ...] = ()
    created: str = ""           # UTC, to the microsecond: reminders left in one second keep their order

    def to_document(self) -> dict:
        return {"format": REMINDER_FORMAT, "schema_version": REMINDER_SCHEMA, "job_id": self.job_id,
                "title": self.title, "lines": list(self.lines), "created": self.created}

    @classmethod
    def from_document(cls, doc) -> "Reminder":
        require_keys(doc, "", ("format", "schema_version", "job_id", "title", "lines", "created"))
        if doc["format"] != REMINDER_FORMAT or doc["schema_version"] != REMINDER_SCHEMA:
            raise ContractError("format", "not a reminder of this version")
        if not isinstance(doc["title"], str) or not isinstance(doc["lines"], list):
            raise ContractError("title", "not a reminder's words")
        return cls(str(doc["job_id"]), doc["title"], tuple(str(line) for line in doc["lines"]), str(doc["created"]))


def _path(layout: RuntimeLayout, job_id: str) -> Path:
    """The job's reminder file; ValueError for anything but a job's id (``jobs.model.new_id``) —
    an automatic start names its job as it was given."""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    return layout.reminders_dir / f"{job_id}{SUFFIX}"


def leave(layout: RuntimeLayout, job_id: str, notice: Notice) -> None:
    """Write the job's reminder of ``notice``, replacing the job's earlier one. Raises OSError."""
    path = _path(layout, job_id)
    created = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    reminder = Reminder(job_id, notice.title, tuple(notice.lines), created)
    exchange.write_text(path, json_text(reminder.to_document()), staging=exchange.private_staging(path))


def standing(layout: RuntimeLayout, *, patience: float = exchange.READ_PATIENCE) -> List[Reminder]:
    """Every reminder of the store, oldest first, each of the job its file is named for; one
    whose file cannot be read — another program keeping it refused for ``patience`` seconds
    included — in general words. Raises OSError when the folder cannot be listed."""
    try:
        paths = [p for p in layout.reminders_dir.iterdir() if p.name.endswith(SUFFIX) and p.is_file()]
    except FileNotFoundError:
        return []
    found = []
    for path in paths:
        job_id = path.name[:-len(SUFFIX)]
        try:
            read = Reminder.from_document(json.loads(exchange.read_text(path, patience=patience)))
            found.append(replace(read, job_id=job_id))
        except FileNotFoundError:
            continue                    # cleared while it was listed
        except (OSError, ValueError, ContractError):
            found.append(Reminder(job_id, UNREADABLE_TITLE))
    return sorted(found, key=lambda r: (r.created, r.job_id))


def clear(layout: RuntimeLayout, *, now: Optional[float] = None) -> bool:
    """Remove every reminder of the store, and the staging file of a writer that died before
    its rename, once ``STALE_STAGING_SECONDS`` old; whether any reminder was there. Never
    raises and never waits: a file another program keeps refused stays, cleared at the next
    call."""
    try:
        entries = list(layout.reminders_dir.iterdir())
    except OSError:
        return False
    now = time.time() if now is None else now
    found = False
    for path in entries:
        if path.name.endswith(SUFFIX):
            found = True
        elif not path.name.endswith(STAGING_SUFFIX) or not _stale(path, now):
            continue
        try:
            exchange.remove(path, patience=0)
        except OSError:
            pass
    return found


def _stale(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime >= STALE_STAGING_SECONDS
    except OSError:
        return False


def reminding(store_path: Path, job_id: str, *,
              bring_up: Optional[Callable[[RuntimeLayout, Path], object]] = None) -> Callable[[Notice], None]:
    """What a notice of ``job_id`` is handed to as it is raised: one that asks something of the
    user is left as the job's reminder beside the store at ``store_path``, then the icon is
    brought up for that store — written first, so an icon process letting go meanwhile sees it
    at its last look (``mirrorhold_gui.tray_model.hand_back``). Never raises."""
    layout = runtime_layout(store_path)

    def remind(notice: Notice) -> None:
        if not notice.asks:
            return
        try:
            leave(layout, job_id, notice)
        except Exception as exc:  # a courtesy: the activity and its notice stand without it
            diagnostics.note(f"the reminder could not be left: {type(exc).__name__}: {exc}")
            return
        (bring_up or offer)(layout, store_path)

    return remind
