"""
Unit tests for health check services (P4-6).

Uses monkeypatch to stub requests.get; no real network calls.
"""

from unittest.mock import Mock

import pytest
import requests

from app.services.health import (
    ServiceHealth,
    check_all,
    check_listenbrainz,
    check_navidrome,
    check_slskd,
    reconnect_slskd,
)


class FakeConfig:
    """Minimal config stub for health checks."""

    class Slskd:
        url = "http://slskd:5030"
        api_key = "test-key"

    class Navidrome:
        url = "http://navidrome:4533"
        username = "testuser"
        password = "testpass"

    class ListenBrainz:
        enabled = True
        url = "https://api.listenbrainz.org"

    slskd = Slskd()
    navidrome = Navidrome()
    listenbrainz = ListenBrainz()


class FakeConfigLBDisabled:
    """Config with ListenBrainz disabled."""

    class Slskd:
        url = "http://slskd:5030"
        api_key = "test-key"

    class Navidrome:
        url = "http://navidrome:4533"
        username = "testuser"
        password = "testpass"

    class ListenBrainz:
        enabled = False
        url = "https://api.listenbrainz.org"

    slskd = Slskd()
    navidrome = Navidrome()
    listenbrainz = ListenBrainz()


def _make_response(status_code=200, json_data=None, ok=True):
    """Build a fake requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.ok = ok
    resp.json.return_value = json_data or {}
    return resp


# ============================================================================
# check_slskd
# ============================================================================


class TestCheckSlskd:
    def test_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200, {"isConnected": True})
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_slskd(FakeConfig())
        assert result.status == "up"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        assert result.error is None

    def test_down_not_connected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200, {"isConnected": False})
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)
        put_calls = []
        monkeypatch.setattr(
            "requests.put",
            lambda *a, **kw: put_calls.append((a, kw)) or _make_response(200),
        )

        result = check_slskd(FakeConfig())
        assert result.status == "down"
        assert result.error == "Not connected to Soulseek network"
        assert len(put_calls) == 1  # auto-reconnect fired

    def test_up_no_reconnect_attempted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200, {"isConnected": True})
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        def raiser(*a, **kw):
            raise AssertionError(
                "reconnect should not be called when already connected"
            )

        monkeypatch.setattr("requests.put", raiser)

        result = check_slskd(FakeConfig())
        assert result.status == "up"

    def test_down_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(502, ok=False)
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_slskd(FakeConfig())
        assert result.status == "down"
        assert "HTTP 502" in result.error

    def test_down_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(*a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr("requests.get", raiser)

        result = check_slskd(FakeConfig())
        assert result.status == "down"
        assert "refused" in result.error

    def test_down_bad_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = Mock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_slskd(FakeConfig())
        assert result.status == "down"
        assert "Invalid JSON" in result.error


# ============================================================================
# reconnect_slskd
# ============================================================================


class TestReconnectSlskd:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200)
        monkeypatch.setattr("requests.put", lambda *a, **kw: resp)

        assert reconnect_slskd(FakeConfig()) is True

    def test_205_reset_content_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # slskd's real PUT /api/v0/server responds 205 Reset Content on success.
        resp = _make_response(205)
        monkeypatch.setattr("requests.put", lambda *a, **kw: resp)

        assert reconnect_slskd(FakeConfig()) is True

    def test_non_2xx_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(500, ok=False)
        monkeypatch.setattr("requests.put", lambda *a, **kw: resp)

        assert reconnect_slskd(FakeConfig()) is False

    def test_connection_error_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raiser(*a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr("requests.put", raiser)

        assert reconnect_slskd(FakeConfig()) is False


# ============================================================================
# check_navidrome
# ============================================================================


class TestCheckNavidrome:
    def test_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200, {"subsonic-response": {"status": "ok"}})
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_navidrome(FakeConfig())
        assert result.status == "up"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        assert result.error is None

    def test_down_status_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(
            200,
            {
                "subsonic-response": {
                    "status": "failed",
                    "error": {"code": 40, "message": "Wrong username or password"},
                }
            },
        )
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_navidrome(FakeConfig())
        assert result.status == "down"
        assert "Wrong username or password" in result.error

    def test_down_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(502, ok=False)
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_navidrome(FakeConfig())
        assert result.status == "down"
        assert "HTTP 502" in result.error

    def test_down_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(*a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr("requests.get", raiser)

        result = check_navidrome(FakeConfig())
        assert result.status == "down"
        assert "refused" in result.error


# ============================================================================
# check_listenbrainz
# ============================================================================


class TestCheckListenBrainz:
    def test_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200)
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_listenbrainz(FakeConfig())
        assert result.status == "up"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        assert result.error is None

    def test_disabled_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        def tracker(*a, **kw):
            nonlocal called
            called = True
            return _make_response(200)

        monkeypatch.setattr("requests.get", tracker)

        result = check_listenbrainz(FakeConfigLBDisabled())
        assert result.status == "disabled"
        assert result.latency_ms is None
        assert result.error is None
        assert not called

    def test_down_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(*a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr("requests.get", raiser)

        result = check_listenbrainz(FakeConfig())
        assert result.status == "down"
        assert "refused" in result.error

    def test_up_404_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(404)
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        result = check_listenbrainz(FakeConfig())
        assert result.status == "up"


# ============================================================================
# check_all
# ============================================================================


class TestCheckAll:
    def test_returns_all_three(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _make_response(200, {"isConnected": True})
        monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

        results = check_all(FakeConfig())
        assert set(results.keys()) == {"slskd", "navidrome", "listenbrainz"}
        for h in results.values():
            assert isinstance(h, ServiceHealth)
