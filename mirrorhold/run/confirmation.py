"""Confirmation tickets: the user's confirmation of a previewed plan, carried to a run.

A preview writes one ticket per job (a newer preview replaces it) naming, for each
pair it planned, whether that pair's run is its first, which files the plan would
move into versions and — for a first run onto a destination that already holds what
the source does not — what the user chose to do with that (``Content``).
``run --confirm <ticket>`` hands the ticket to the run, which re-lists and re-plans by
that choice and lets the gate grant exactly the removals the ticket names: a removal
that appeared since the preview is judged afresh, never confirmed by accident. The
ticket is consumed by the run that used it.

An unattended run never takes a ticket: confirming is the user's act.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, FrozenSet, Optional

from .. import exchange
from ..contract_errors import ContractError, require_keys
from ..locations import RuntimeLayout
from ..records.record import json_text, utc_now

TICKET_FORMAT = "mirrorhold-confirmation"
TICKET_SCHEMA = 2


class TicketError(Exception):
    """A ticket that cannot be used for this run."""


class Content(str, Enum):
    """What a first run does with what its destination already holds and the source does not."""

    LEAVE = "leave"     # left where it is, now and by every later run: the destination is shared
    MOVE = "move"       # moved into versions: the destination becomes the job's whole


@dataclass(frozen=True)
class PairConfirmation:
    first_run: bool
    removals: FrozenSet[str]
    content: Optional[Content] = None    # a first run's choice over what the destination already held


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    job_id: str
    created: str
    pairs: Dict[str, PairConfirmation] = field(default_factory=dict)   # marker id -> confirmation

    def to_document(self) -> dict:
        return {"format": TICKET_FORMAT, "schema_version": TICKET_SCHEMA, "ticket_id": self.ticket_id,
                "job_id": self.job_id, "created": self.created,
                "pairs": {m: {"first_run": c.first_run, "removals": sorted(c.removals),
                              "content": c.content.value if c.content is not None else None}
                          for m, c in self.pairs.items()}}

    @classmethod
    def from_document(cls, doc) -> "Ticket":
        require_keys(doc, "", ("format", "schema_version", "ticket_id", "job_id", "created", "pairs"))
        if doc["format"] != TICKET_FORMAT or doc["schema_version"] != TICKET_SCHEMA:
            raise ContractError("format", "not a confirmation ticket of this version")
        pairs = {}
        for marker, entry in doc["pairs"].items():
            require_keys(entry, f"pairs.{marker}", ("first_run", "removals", "content"))
            try:
                content = Content(entry["content"]) if entry["content"] is not None else None
            except ValueError:
                raise ContractError(f"pairs.{marker}.content", f"not a choice: {entry['content']!r}") from None
            pairs[marker] = PairConfirmation(bool(entry["first_run"]), frozenset(entry["removals"]), content)
        return cls(doc["ticket_id"], doc["job_id"], doc["created"], pairs)


def new_ticket(job_id: str, pairs: Dict[str, PairConfirmation]) -> Ticket:
    return Ticket(uuid.uuid4().hex[:10], job_id, utc_now(), dict(pairs))


def _path(layout: RuntimeLayout, job_id: str) -> Path:
    return layout.tickets_dir / f"{job_id}.json"


def save_ticket(layout: RuntimeLayout, ticket: Ticket) -> Path:
    """Write the job's ticket; a run may be reading the previous one (``exchange``)."""
    path = _path(layout, ticket.job_id)
    exchange.write_text(path, json_text(ticket.to_document()), durable=True,
                        staging=exchange.private_staging(path))
    return path


def load_ticket(layout: RuntimeLayout, job_id: str, ticket_id: str) -> Ticket:
    path = _path(layout, job_id)
    try:
        ticket = Ticket.from_document(json.loads(exchange.read_text(path)))
    except FileNotFoundError:
        raise TicketError("there is no preview to confirm for this job; run a preview first") from None
    except (OSError, ValueError, ContractError) as exc:
        raise TicketError(f"the preview's ticket cannot be read: {exc}") from None
    if ticket.job_id != job_id or ticket.ticket_id != ticket_id:
        raise TicketError(f"'{ticket_id}' is not this job's latest preview (that is '{ticket.ticket_id}')")
    return ticket


def discard_ticket(layout: RuntimeLayout, job_id: str, ticket_id: Optional[str] = None) -> None:
    """Remove the job's ticket — only the named one, when a name is given."""
    path = _path(layout, job_id)
    if ticket_id is not None:
        try:
            current = json.loads(exchange.read_text(path)).get("ticket_id")
        except (OSError, ValueError, AttributeError):
            return
        if current != ticket_id:
            return
    exchange.remove(path)
