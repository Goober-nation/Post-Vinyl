"""A ListenBrainz token reaches the user's Navidrome account, best effort."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.clients.navidrome import NavidromeUnreachable, Session
from app.clients.navidrome_fake import FakeNavidromeClient
from app.services.auth import AuthService
from app.services.credentials import CredentialsService
from app.services.encryption import SecretBox, generate_key
from app.services.sources import SourcePreferences
from app.stores.database import Database


class DownNavidrome(FakeNavidromeClient):
    def link_listenbrainz(self, session: Session, token: str | None) -> None:
        raise NavidromeUnreachable("down")


def setup(tmp_path: Path, nd: FakeNavidromeClient) -> tuple[CredentialsService, int]:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd.users["alice"] = ("pw", False)
    lb = FakeListenBrainzClient()
    lb.tokens["tok"] = "alice-lb"
    prefs = SourcePreferences(db, {"catalog": ("listenbrainz",)})
    user_id = AuthService(db, nd, box).login("alice", "pw").user.id
    return CredentialsService(db, lb, box, prefs, nd), user_id


def test_token_is_linked_then_unlinked(tmp_path: Path) -> None:
    nd = FakeNavidromeClient()
    svc, user_id = setup(tmp_path, nd)
    assert svc.set_listenbrainz(user_id, "tok").navidrome_linked is True
    assert nd.lb_links == {"alice": "tok"}
    assert svc.set_listenbrainz(user_id, None).navidrome_linked is True
    assert nd.lb_links == {}


def test_navidrome_down_still_stores_the_token(tmp_path: Path) -> None:
    svc, user_id = setup(tmp_path, DownNavidrome())
    view = svc.set_listenbrainz(user_id, "tok")
    assert view.listenbrainz and view.navidrome_linked is False
    assert svc.view(user_id).listenbrainz


def test_invalid_token_never_reaches_navidrome(tmp_path: Path) -> None:
    nd = FakeNavidromeClient()
    svc, user_id = setup(tmp_path, nd)
    with pytest.raises(ValueError):
        svc.set_listenbrainz(user_id, "bad")
    assert nd.lb_links == {}
