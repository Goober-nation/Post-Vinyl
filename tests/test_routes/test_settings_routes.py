"""Settings routes (#109): the schema for everyone, install settings admin-only."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import Settings, load_sources
from app.dependencies import Core
from app.main import create_app
from app.services.auth import AuthService
from app.services.catalog_fake import FakeCatalogService
from app.services.dispatch_fake import FakeDownloadDispatch
from app.services.encryption import SecretBox, generate_key
from app.services.requests import RequestService
from app.services.settings import InstallSettings, UserSettings
from app.services.sources import SourcePreferences
from app.stores.database import Database
from tests.test_routes.test_catalog_requests import ORIGIN, SAME, logged_in
from tests.test_services.test_settings import EXAMPLE


def test_schema_for_all_install_settings_for_admins_only(tmp_path: Path) -> None:
    cfg = tmp_path / "post-vinyl.yaml"
    shutil.copy(EXAMPLE, cfg)
    db = Database(tmp_path / "pv.db")
    db.migrate()
    auth = AuthService(db, FakeNavidromeClient({"alice": ("pw", True), "bob": ("pw", False)}),
                       SecretBox(generate_key()))  # fmt: skip
    settings = Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN,
                        sources=load_sources(cfg), config_path=str(cfg))  # fmt: skip
    prefs = SourcePreferences(db, settings.sources)
    core = Core(FakeCatalogService(), RequestService(db), FakeDownloadDispatch(),
                settings=UserSettings(db, prefs), install=InstallSettings(settings))  # fmt: skip
    app = create_app(settings, auth, core)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    schema = bob.get("/api/settings/schema").json()
    assert {f["key"] for f in schema["user"]} >= {
        "sources.catalog",
        "downloads.page_size",
    }
    assert any(f["key"] == "slskd.searches_per_minute" and f["max"] == 10
               for f in schema["install"])  # fmt: skip
    change = {"changes": {"slskd.searches_per_minute": 3}}
    assert bob.get("/api/admin/settings").status_code == 403
    assert bob.put("/api/admin/settings", json=change, headers=SAME).status_code == 403
    r = alice.put("/api/admin/settings", json=change, headers=SAME)
    assert r.status_code == 200 and r.json()["restart_pending"] == [
        "slskd.searches_per_minute"
    ]
    assert "searches_per_minute: 3" in cfg.read_text(encoding="utf-8")
    bad = {"changes": {"files.library_dir": "elsewhere"}}
    assert alice.put("/api/admin/settings", json=bad, headers=SAME).status_code == 422
