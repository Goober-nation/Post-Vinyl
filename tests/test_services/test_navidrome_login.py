"""One way to a user's Navidrome session."""

from __future__ import annotations

from pathlib import Path

from app.clients.navidrome_fake import FakeNavidromeClient
from app.services.encryption import SecretBox, generate_key
from app.services.navidrome_login import NavidromeLogins
from app.stores.database import Database
from app.stores.users import UsersStore


def test_a_refused_login_is_marked_and_not_tried_again(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    with db.transaction(write=True) as conn:
        user = UsersStore().upsert_on_login(conn, "a", box.encrypt("old"), False, 1)
    logins = NavidromeLogins(db, FakeNavidromeClient({"a": ("new", False)}), box)
    assert logins.session_for(user) is None
    with db.transaction() as conn:
        marked = UsersStore().get(conn, user.id)
    assert marked is not None and marked.status == "needs_reauth"
    assert logins.session_for(marked) is None
