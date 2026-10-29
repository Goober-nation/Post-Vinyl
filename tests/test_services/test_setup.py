"""First Navidrome admin (#97) with the fake Navidrome and a real SQLite file."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.navidrome import NavidromeUnreachable, Session
from app.clients.navidrome_fake import FakeNavidromeClient
from app.services.encryption import SecretBox, generate_key
from app.services.setup import (
    WAIT_FOR_NAVIDROME_S,
    AlreadySetUp,
    NavidromeNotReady,
    create_first_admin,
)
from app.stores.database import Database
from app.stores.users import UsersStore


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class SlowStart(FakeNavidromeClient):
    """Refuses connections for the first ``down`` calls, like a booting container."""

    def __init__(self, down: int) -> None:
        super().__init__()
        self.down = down

    def create_admin(self, username: str, password: str) -> Session:
        if self.down:
            self.down -= 1
            raise NavidromeUnreachable("connection refused")
        return super().create_admin(username, password)


def test_creates_navidrome_admin_and_pv_admin_row(tmp_path: Path) -> None:
    nd, db, box = (
        FakeNavidromeClient(),
        Database(tmp_path / "pv.db"),
        SecretBox(generate_key()),
    )
    user = create_first_admin(nd, db, box, " admin ", "s3cret", Clock())
    assert nd.users == {"admin": ("s3cret", True)}
    with db.transaction() as conn:
        row = UsersStore().get_by_username(conn, "admin")
    assert row and row.id == user.id and row.is_admin
    assert box.decrypt(row.nd_password_enc) == "s3cret"


def test_refuses_when_navidrome_has_users(tmp_path: Path) -> None:
    nd = FakeNavidromeClient({"someone": ("pw", True)})
    db = Database(tmp_path / "pv.db")
    with pytest.raises(AlreadySetUp):
        create_first_admin(nd, db, SecretBox(generate_key()), "admin", "pw", Clock())
    assert nd.users == {"someone": ("pw", True)}
    assert not (tmp_path / "pv.db").exists()  # nothing written


def test_waits_for_a_starting_navidrome(tmp_path: Path) -> None:
    clock, nd = Clock(), SlowStart(down=3)
    create_first_admin(
        nd, Database(tmp_path / "pv.db"), SecretBox(generate_key()), "admin", "pw",
        clock, clock.sleep,
    )  # fmt: skip
    assert "admin" in nd.users


def test_gives_up_when_navidrome_never_answers(tmp_path: Path) -> None:
    clock = Clock()
    with pytest.raises(NavidromeNotReady):
        create_first_admin(
            SlowStart(down=10_000), Database(tmp_path / "pv.db"),
            SecretBox(generate_key()), "admin", "pw", clock, clock.sleep,
        )  # fmt: skip
    assert clock.t - 1_800_000_000.0 >= WAIT_FOR_NAVIDROME_S


@pytest.mark.parametrize("username, password", [("", "pw"), ("  ", "pw"), ("a", "")])
def test_requires_username_and_password(
    tmp_path: Path, username: str, password: str
) -> None:
    nd = FakeNavidromeClient()
    with pytest.raises(ValueError):
        create_first_admin(
            nd, Database(tmp_path / "pv.db"), SecretBox(generate_key()),
            username, password, Clock(),
        )  # fmt: skip
    assert nd.users == {}
