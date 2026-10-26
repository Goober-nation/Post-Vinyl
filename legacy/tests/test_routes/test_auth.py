"""
Integration tests for HTTP Basic Auth middleware (Phase 6.4d).
"""

import base64

from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app


def _config_with_auth(username: str, password: str) -> Config:
    config = Config()
    config.auth.username = username
    config.auth.password = password
    return config


def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class TestAuthDisabled:
    def test_no_credentials_configured_allows_requests(self):
        """When auth.username/password aren't both set, no auth is required."""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/transfers")
        assert resp.status_code != 401


class TestAuthEnabled:
    def test_request_without_credentials_401(self):
        config = _config_with_auth("admin", "secret")
        app = create_app(config=config)
        client = TestClient(app)

        resp = client.get("/api/transfers")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_request_with_correct_credentials_passes(self):
        config = _config_with_auth("admin", "secret")
        app = create_app(config=config)
        client = TestClient(app)

        resp = client.get(
            "/api/transfers",
            headers={"Authorization": _basic_header("admin", "secret")},
        )
        assert resp.status_code != 401

    def test_request_with_incorrect_credentials_401(self):
        config = _config_with_auth("admin", "secret")
        app = create_app(config=config)
        client = TestClient(app)

        resp = client.get(
            "/api/transfers",
            headers={"Authorization": _basic_header("admin", "wrong")},
        )
        assert resp.status_code == 401

    def test_malformed_authorization_header_401(self):
        config = _config_with_auth("admin", "secret")
        app = create_app(config=config)
        client = TestClient(app)

        resp = client.get(
            "/api/transfers",
            headers={"Authorization": "Basic not-valid-base64!!"},
        )
        assert resp.status_code == 401
