"""
Health probe behaviour, and specifically the reconnect throttle.

`check_slskd` self-heals: when slskd answers but reports itself disconnected
from the Soulseek network, it fires a reconnect so the next poll finds it
healthy. Nothing used to bound how often that happened, and the polls are
frequent — the frontend every 30s, the container healthcheck every 15s, and
the live suite's readiness wait once a second for up to 90s.

On 2026-08-13 that turned into a feedback loop: a saturated host-side port
forwarder made slskd look disconnected, every poll fired another login to
the Soulseek server, and a socket census found 476 established connections
to the server port piled up on the forwarder that was already the
bottleneck. The self-healing was feeding the failure it was reacting to.
"""

from __future__ import annotations

import pytest

from app.config import Config
from app.services import health


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    """The throttle is module state; leaking it across tests would make them
    order-dependent in exactly the way that hides a broken throttle."""
    health._last_reconnect_at = None
    yield
    health._last_reconnect_at = None


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "config.toml").write_text(
        '[slskd]\nurl = "http://slskd:5030"\napi_key = "k"\n'
    )
    cfg = Config(config_path=str(tmp_path / "config.toml"))
    cfg.load()
    return cfg


class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class TestReconnectThrottle:
    def test_disconnected_slskd_triggers_one_reconnect(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reconnects: list[str] = []
        monkeypatch.setattr(
            health.requests, "get", lambda *a, **kw: _Resp({"isConnected": False})
        )
        monkeypatch.setattr(
            health.requests,
            "put",
            lambda *a, **kw: reconnects.append("put") or _Resp({}, 200),
        )

        result = health.check_slskd(config)

        assert result.status == "down"
        assert len(reconnects) == 1

    def test_repeated_polls_do_not_stack_up_reconnects(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression itself: 90 polls must not mean 90 server logins."""
        reconnects: list[str] = []
        monkeypatch.setattr(
            health.requests, "get", lambda *a, **kw: _Resp({"isConnected": False})
        )
        monkeypatch.setattr(
            health.requests,
            "put",
            lambda *a, **kw: reconnects.append("put") or _Resp({}, 200),
        )

        for _ in range(90):
            assert health.check_slskd(config).status == "down"

        assert len(reconnects) == 1

    def test_reconnect_is_attempted_again_after_the_interval(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Throttled, not disabled — self-healing still has to happen."""
        reconnects: list[str] = []
        monkeypatch.setattr(
            health.requests, "get", lambda *a, **kw: _Resp({"isConnected": False})
        )
        monkeypatch.setattr(
            health.requests,
            "put",
            lambda *a, **kw: reconnects.append("put") or _Resp({}, 200),
        )

        clock = [1000.0]
        monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])

        health.check_slskd(config)
        assert len(reconnects) == 1

        clock[0] += health._RECONNECT_MIN_INTERVAL_SECONDS - 1
        health.check_slskd(config)
        assert len(reconnects) == 1, "still inside the interval"

        clock[0] += 2
        health.check_slskd(config)
        assert len(reconnects) == 2, "interval elapsed, self-healing resumes"

    def test_explicit_user_reconnect_is_never_throttled(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /api/system/slskd/reconnect calls reconnect_slskd directly.

        A user clicking "reconnect" is a deliberate act, not a poll, and must
        work on the first click regardless of what the health checks have
        been doing.
        """
        reconnects: list[str] = []
        monkeypatch.setattr(
            health.requests,
            "put",
            lambda *a, **kw: reconnects.append("put") or _Resp({}, 200),
        )

        for _ in range(5):
            assert health.reconnect_slskd(config) is True

        assert len(reconnects) == 5

    def test_connected_slskd_never_reconnects(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reconnects: list[str] = []
        monkeypatch.setattr(
            health.requests, "get", lambda *a, **kw: _Resp({"isConnected": True})
        )
        monkeypatch.setattr(
            health.requests,
            "put",
            lambda *a, **kw: reconnects.append("put") or _Resp({}, 200),
        )

        assert health.check_slskd(config).status == "up"
        assert reconnects == []


class TestSlskdLoginError:
    """get_slskd_login_error — live-verified 2026-08-14 against a real slskd
    instance: a taken-username/wrong-password login failure produces exactly
    these two log lines, not exposed by GET /api/v0/server's isConnected
    flag, only by GET /api/v0/logs."""

    def test_finds_invalid_credentials_signature(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs = [
            {"level": "Information", "message": "Listening for HTTP requests"},
            {
                "level": "Error",
                "message": "Disconnected from the Soulseek server: invalid username or password",
            },
            {
                "level": "Error",
                "message": 'Failed to reconnect: "The server rejected login attempt: INVALIDPASS',
            },
        ]
        monkeypatch.setattr(health.requests, "get", lambda *a, **kw: _Resp(logs))

        result = health.get_slskd_login_error(config)

        assert result is not None
        assert "already registered" in result

    def test_no_matching_signature_returns_none(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs = [{"level": "Information", "message": "Listening for HTTP requests"}]
        monkeypatch.setattr(health.requests, "get", lambda *a, **kw: _Resp(logs))

        assert health.get_slskd_login_error(config) is None

    def test_unreachable_slskd_returns_none(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a, **kw):
            raise health.requests.RequestException("refused")

        monkeypatch.setattr(health.requests, "get", _raise)

        assert health.get_slskd_login_error(config) is None


class TestCheckNavidrome:
    """check_navidrome — a fresh, not-yet-configured instance must report
    "disabled" without ever pinging Navidrome. Pinging with an empty
    username/password reaches Navidrome as a bare `u=` and comes back as a
    cryptic "missing parameter: 'u'" error, which reads as a bug rather than
    "finish setup" (found live 2026-08-14)."""

    def test_missing_credentials_reports_disabled_without_a_request(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            health.requests,
            "get",
            lambda *a, **kw: calls.append("get") or _Resp({}, 200),
        )

        result = health.check_navidrome(config)

        assert result.status == "disabled"
        assert calls == []

    def test_partial_credentials_still_reports_disabled(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.navidrome.username = "admin"
        config.navidrome.password = ""
        calls: list[str] = []
        monkeypatch.setattr(
            health.requests,
            "get",
            lambda *a, **kw: calls.append("get") or _Resp({}, 200),
        )

        result = health.check_navidrome(config)

        assert result.status == "disabled"
        assert calls == []

    def test_configured_and_reachable_reports_up(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.navidrome.username = "admin"
        config.navidrome.password = "secret"
        monkeypatch.setattr(
            health.requests,
            "get",
            lambda *a, **kw: _Resp({"subsonic-response": {"status": "ok"}}, 200),
        )

        assert health.check_navidrome(config).status == "up"

    def test_configured_but_rejected_reports_down_with_navidrome_error(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.navidrome.username = "admin"
        config.navidrome.password = "wrong"
        monkeypatch.setattr(
            health.requests,
            "get",
            lambda *a, **kw: _Resp(
                {
                    "subsonic-response": {
                        "status": "failed",
                        "error": {"message": "Wrong username or password"},
                    }
                },
                200,
            ),
        )

        result = health.check_navidrome(config)

        assert result.status == "down"
        assert result.error == "Wrong username or password"

    def test_non_200_returns_none(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            health.requests, "get", lambda *a, **kw: _Resp([], status=401)
        )

        assert health.get_slskd_login_error(config) is None
