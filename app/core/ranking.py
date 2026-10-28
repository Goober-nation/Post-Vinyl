"""Order helpful peers: free upload slot first, then upload speed (#92)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar


class Peer(Protocol):
    @property
    def has_free_slot(self) -> bool: ...
    @property
    def upload_speed(self) -> int: ...


P = TypeVar("P", bound=Peer)


def rank_peers(peers: Iterable[P]) -> list[P]:
    """Best first; ties keep their input order."""
    return sorted(peers, key=lambda p: (not p.has_free_slot, -p.upload_speed))
