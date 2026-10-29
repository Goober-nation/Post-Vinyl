"""docs/api.md lists every endpoint the app serves, and nothing it doesn't (#109)."""

from __future__ import annotations

import re
from pathlib import Path

from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import Settings
from app.main import create_app
from app.services.auth import AuthService
from app.services.encryption import SecretBox, generate_key
from app.stores.database import Database

DOC = Path(__file__).parents[2] / "docs" / "api.md"


def test_the_api_doc_matches_the_app(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    auth = AuthService(db, FakeNavidromeClient(), SecretBox(generate_key()))
    app = create_app(
        Settings(str(tmp_path / "pv.db"), "http://nd", "https://x", True), auth
    )
    served = {
        f"{m.upper()} {p}" for p, ops in app.openapi()["paths"].items() for m in ops
    }
    documented = set(re.findall(r"`((?:GET|POST|PUT|PATCH|DELETE) /api/[^`\s]*)`",
                                DOC.read_text(encoding="utf-8")))  # fmt: skip
    assert sorted(served - documented) == [], "served but not in docs/api.md"
    assert sorted(documented - served) == [], "in docs/api.md but not served"
