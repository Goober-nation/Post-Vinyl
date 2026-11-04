"""Service pings: a source that's off isn't called."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.reach import Unreachable
from app.services.sources import SourcePreferences
from app.services.status import Check, StatusService, UnknownService
from app.stores.database import Database


def broken() -> None:
    raise Unreachable("x")


def test_ping_reports_up_down_and_off_without_calling_off_sources(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    prefs = SourcePreferences(db, {"catalog": ("deezer",)})
    calls: list[str] = []
    service = StatusService(
        prefs,
        [
            Check("navidrome", "Navidrome", lambda: calls.append("nd")),
            Check("slskd", "slskd", broken),
            Check(
                "musicbrainz",
                "MusicBrainz",
                lambda: calls.append("mb"),
                ("catalog", "musicbrainz"),
            ),
        ],
    )
    assert [s.name for s in service.services() if s.main] == ["navidrome", "slskd"]
    assert service.ping(1, "navidrome").state == "ok"
    assert service.ping(1, "slskd").state == "down"
    assert service.ping(1, "musicbrainz").state == "off"
    assert calls == ["nd"]
    with pytest.raises(UnknownService):
        service.ping(1, "nope")
