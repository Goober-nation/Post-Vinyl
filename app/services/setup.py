"""Creating the first Navidrome admin (#97).

Run once from ``python -m app.setup`` inside a setup-only compose run, where
Navidrome has no published port (user, 2026-10-29): nobody else can reach its
``createAdmin`` while it has no users. Navidrome itself decides whether this
is the first user (``createAdmin`` answers 403 once any user exists), so
there is no separate "has users?" check that could race it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.clients.navidrome import (
    NavidromeClientBase,
    NavidromeForbidden,
    NavidromeUnreachable,
)
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.users import User, UsersStore

WAIT_FOR_NAVIDROME_S = 60  # Navidrome needs a few seconds after its container starts
RETRY_EVERY_S = 2


class AlreadySetUp(Exception):
    """Navidrome already has users; the first admin exists."""


class NavidromeNotReady(Exception):
    """Navidrome didn't answer within WAIT_FOR_NAVIDROME_S."""


def create_first_admin(
    navidrome: NavidromeClientBase,
    db: Database,
    box: SecretBox,
    username: str,
    password: str,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> User:
    """Create the Navidrome admin and the matching Post-Vinyl admin row."""
    if not username.strip() or not password:
        raise ValueError("username and password are required")
    deadline = clock() + WAIT_FOR_NAVIDROME_S
    while True:
        try:
            session = navidrome.create_admin(username.strip(), password)
            break
        except NavidromeForbidden as e:
            raise AlreadySetUp("Navidrome already has an admin") from e
        except NavidromeUnreachable as e:
            if clock() >= deadline:
                raise NavidromeNotReady(f"Navidrome didn't answer: {e}") from e
            sleep(RETRY_EVERY_S)
    db.migrate()
    with db.transaction(write=True) as conn:
        return UsersStore().upsert_on_login(
            conn,
            session.username,
            box.encrypt(password),
            session.is_admin,
            int(clock()),
        )
