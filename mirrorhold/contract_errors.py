"""The one error type for a document or value that breaks the configuration contract.

``where`` is a dotted path to the offending field (``jobs[2].source.endpoint.path``),
built up as the error passes outward through the decoders: each layer prefixes its
own position with ``within``. A caller shows ``str(error)`` and the user can find
the field without reading code.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


class ContractError(ValueError):
    def __init__(self, where: str, message: str):
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}" if where else message)

    def within(self, prefix: str) -> "ContractError":
        """The same error, located one level further out."""
        if not prefix:
            return self
        return ContractError(f"{prefix}.{self.where}" if self.where else prefix, self.message)


def require_keys(doc: Any, where: str, keys: Iterable[str]) -> Mapping:
    """``doc`` as an object holding exactly ``keys``: a missing key and an unknown one
    are both errors, so a hand-edited typo never passes silently as a default."""
    if not isinstance(doc, Mapping):
        raise ContractError(where, "expected an object")
    expected = list(keys)
    missing = [k for k in expected if k not in doc]
    if missing:
        raise ContractError(where, f"missing {', '.join(missing)}")
    unknown = sorted(set(doc) - set(expected))
    if unknown:
        raise ContractError(where, f"unknown {', '.join(unknown)}")
    return doc
