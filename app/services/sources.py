"""Which sources a user's work goes through, in what order (#95).

The config lists the install's sources per kind (catalog, download, request);
order = priority, left out = off. Each user may reorder them and switch some
off (user, 2026-10-29), stored as the ``sources.<kind>`` setting:
``{"order": [...], "off": [...]}``. A source the user hasn't placed yet (new in
the config) is on and comes after the ones they placed, in config order.

Every call to an outside service goes through its client and only while that
source is on for the user the work is for; otherwise it's refused
(``SourceOff``, user 2026-10-30).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.stores.database import Database
from app.stores.users import UsersStore


class UnknownSource(ValueError):
    """A source name the config doesn't list for that kind."""


class SourceOff(Exception):
    """The source is turned off (left out of the config, or off for this user);
    nothing is sent to it (user, 2026-10-30)."""


class SourcePreferences:
    def __init__(
        self,
        db: Database,
        configured: dict[str, tuple[str, ...]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._configured = configured
        self._clock = clock
        self._users = UsersStore()

    def configured(self, kind: str) -> tuple[str, ...]:
        return self._configured.get(kind, ())

    def for_user(self, user_id: int, kind: str) -> list[str]:
        """The user's enabled sources of ``kind``, most preferred first."""
        configured = self.configured(kind)
        with self._db.transaction() as conn:
            pref = self._users.get_setting(conn, user_id, f"sources.{kind}")
        order, off_list = _parse(pref)
        placed = [s for s in order if s in configured]
        ordered = placed + [s for s in configured if s not in placed]
        off = set(off_list)
        return [s for s in ordered if s not in off]

    def is_on(self, user_id: int, kind: str, source: str) -> bool:
        return source in self.for_user(user_id, kind)

    def require(self, user_id: int, kind: str, source: str) -> None:
        """Refuse work that would reach ``source`` while it is off."""
        if not self.is_on(user_id, kind, source):
            raise SourceOff(f"{source} is turned off")

    def set_for_user(
        self, user_id: int, kind: str, order: list[str], off: list[str]
    ) -> None:
        configured = self.configured(kind)
        bad = [s for s in [*order, *off] if s not in configured]
        if bad:
            raise UnknownSource(f"{kind} sources not in the config: {bad}")
        with self._db.transaction(write=True) as conn:
            self._users.set_setting(
                conn,
                user_id,
                f"sources.{kind}",
                {"order": list(dict.fromkeys(order)), "off": sorted(set(off))},
                int(self._clock()),
            )


def _parse(pref: object) -> tuple[list[str], list[str]]:
    """``(order, off)`` from the stored value; anything malformed counts as unset.

    Only ``set_for_user`` writes it (the frontend moves fixed options around),
    but a value read on every search must never crash it.
    """
    if not isinstance(pref, dict):
        return [], []
    order, off = pref.get("order"), pref.get("off")
    if not (isinstance(order, list) and all(isinstance(s, str) for s in order)):
        return [], []
    if not (isinstance(off, list) and all(isinstance(s, str) for s in off)):
        return [], []
    return order, off
