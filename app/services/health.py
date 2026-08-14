"""
Backend health probes.

Checks connectivity to slskd, Navidrome, and ListenBrainz.
All checkers catch their own exceptions and never raise.
"""

import threading
import time
from dataclasses import dataclass

import requests

from app.config import Config
from app.logging_config import get_logger

logger = get_logger(__name__)

#: Minimum gap between two self-healing reconnect attempts.
#:
#: `check_slskd` fires a reconnect whenever slskd reports itself
#: disconnected, and nothing used to stop that from happening on *every*
#: health poll. The polls are not rare: the frontend hits
#: /api/system/status every 30s, the container healthcheck used to hit it
#: every 15s, and the live suite's readiness wait hit it once a second for
#: up to 90s. A slskd that stays disconnected for a minute therefore
#: collected ~90 reconnect requests, each one a fresh login to the Soulseek
#: server.
#:
#: That is not theoretical. A socket census taken during the 2026-08-13
#: stall found 476 established connections from the host to the Soulseek
#: server port — a re-login pile-up, on a host-side port forwarder that was
#: already saturated. The self-healing made the congestion worse, and the
#: congestion made slskd look disconnected. 60s still self-heals within one
#: user-visible poll cycle without ever stacking up.
_RECONNECT_MIN_INTERVAL_SECONDS = 60.0

_reconnect_lock = threading.Lock()
_last_reconnect_at: float | None = None


def _should_attempt_reconnect() -> bool:
    """True at most once per `_RECONNECT_MIN_INTERVAL_SECONDS`.

    Health checks run on the anyio worker threadpool, so several can be in
    flight at once — the check and the timestamp update have to happen
    together under the lock or concurrent polls all pass it.
    """
    global _last_reconnect_at
    now = time.monotonic()
    with _reconnect_lock:
        if (
            _last_reconnect_at is not None
            and now - _last_reconnect_at < _RECONNECT_MIN_INTERVAL_SECONDS
        ):
            return False
        _last_reconnect_at = now
        return True


@dataclass
class ServiceHealth:
    """Health status for a single backend service."""

    name: str
    status: str  # "up" | "down" | "disabled"
    latency_ms: int | None = None
    error: str | None = None


def _slskd_headers(config: Config) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if config.slskd.api_key:
        headers["X-API-Key"] = config.slskd.api_key
    return headers


def reconnect_slskd(config: Config) -> bool:
    """Trigger a (re)connect to the Soulseek network via PUT /api/v0/server.

    Idempotent-safe when already connected. Never raises — logs the outcome
    and returns whether the request succeeded.
    """
    url = f"{config.slskd.url}/api/v0/server"
    try:
        resp = requests.put(url, headers=_slskd_headers(config), timeout=5)
        if resp.status_code in (200, 204, 205):
            logger.info("slskd reconnect requested successfully")
            return True
        logger.warning(f"slskd reconnect failed: HTTP {resp.status_code}")
        return False
    except requests.RequestException as e:
        logger.warning(f"slskd reconnect failed: {e}")
        return False


def check_slskd(config: Config) -> ServiceHealth:
    """Check slskd connectivity via GET /api/v0/server.

    Status "up" iff HTTP 200 AND JSON ``isConnected`` is True. When reachable
    but not connected, fires a fire-and-forget reconnect attempt before
    returning "down" so the next health poll can self-heal.
    """
    url = f"{config.slskd.url}/api/v0/server"
    headers = _slskd_headers(config)

    try:
        start = time.monotonic()
        resp = requests.get(url, headers=headers, timeout=5)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            logger.warning(f"slskd health check failed: HTTP {resp.status_code}")
            return ServiceHealth(
                name="slskd",
                status="down",
                latency_ms=elapsed_ms,
                error=f"HTTP {resp.status_code}",
            )

        data = resp.json()
        if not data.get("isConnected"):
            logger.warning("slskd health check: not connected to Soulseek network")
            # Throttled: see _RECONNECT_MIN_INTERVAL_SECONDS. The health
            # result below is unaffected either way — slskd is reported down
            # whether or not this particular poll was the one that got to
            # try the reconnect.
            if _should_attempt_reconnect():
                reconnect_slskd(config)
            else:
                logger.debug("slskd reconnect skipped: attempted recently")
            return ServiceHealth(
                name="slskd",
                status="down",
                latency_ms=elapsed_ms,
                error="Not connected to Soulseek network",
            )

        logger.debug(f"slskd health check: up ({elapsed_ms}ms)")
        return ServiceHealth(name="slskd", status="up", latency_ms=elapsed_ms)

    except requests.RequestException as e:
        logger.warning(f"slskd health check failed: {e}")
        return ServiceHealth(name="slskd", status="down", error=str(e))
    except ValueError as e:
        logger.warning(f"slskd health check: invalid JSON: {e}")
        return ServiceHealth(name="slskd", status="down", error=f"Invalid JSON: {e}")


# Substrings of slskd's own log messages that indicate a login failure due
# to a username already taken by someone else's account (wrong password for
# that username), found live 2026-08-14 against a real slskd instance:
# "Disconnected from the Soulseek server: invalid username or password" and
# "Failed to reconnect: \"The server rejected login attempt: INVALIDPASS".
# Soulseek has no separate "is this username taken" check — logging in with
# a brand-new username *is* how an account gets registered, so this is the
# only way to distinguish "this username belongs to someone else" from any
# other connection failure, short of a full restart-and-see loop.
_INVALID_CREDENTIALS_SIGNATURES = ("invalid username or password", "invalidpass")


def get_slskd_login_error(config: Config) -> str | None:
    """Return the specific reason slskd failed to log in, if determinable.

    Queries slskd's own GET /api/v0/logs (API-key authenticated, no Docker
    socket needed) for a recent line matching a known credential-failure
    signature. Returns None if slskd is connected, unreachable, or the logs
    don't contain a recognizable reason (e.g. a network-level failure
    instead) — callers should fall back to a generic "not connected"
    message in that case, not assume a specific cause.
    """
    try:
        resp = requests.get(
            f"{config.slskd.url}/api/v0/logs", headers=_slskd_headers(config), timeout=5
        )
        if resp.status_code != 200:
            return None
        for entry in reversed(resp.json()):
            message = str(entry.get("message", "")).lower()
            if any(sig in message for sig in _INVALID_CREDENTIALS_SIGNATURES):
                return (
                    "slskd rejected this Soulseek login — the username is "
                    "already registered under a different password"
                )
        return None
    except (requests.RequestException, ValueError):
        return None


def check_navidrome(config: Config) -> ServiceHealth:
    """Check Navidrome connectivity via /rest/ping.view.

    Status "disabled" if no admin credentials have been set up yet — an
    empty username/password otherwise reaches Navidrome as `u=` and comes
    back as a cryptic "missing parameter: 'u'" error, which reads as a bug
    on a fresh, not-yet-configured instance rather than "finish setup".
    Otherwise "up" iff HTTP 200 AND ``subsonic-response.status == "ok"``.
    """
    if not config.navidrome.username or not config.navidrome.password:
        logger.debug("Navidrome health check: disabled (not configured)")
        return ServiceHealth(name="navidrome", status="disabled")

    import hashlib
    import random
    import string

    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    token = hashlib.md5((config.navidrome.password + salt).encode("utf-8")).hexdigest()
    params = {
        "u": config.navidrome.username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "musica",
        "f": "json",
    }

    url = f"{config.navidrome.url}/rest/ping.view"

    try:
        start = time.monotonic()
        resp = requests.get(url, params=params, timeout=5)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            logger.warning(f"Navidrome health check failed: HTTP {resp.status_code}")
            return ServiceHealth(
                name="navidrome",
                status="down",
                latency_ms=elapsed_ms,
                error=f"HTTP {resp.status_code}",
            )

        data = resp.json()
        subsonic = data.get("subsonic-response", {})
        if subsonic.get("status") != "ok":
            err_msg = subsonic.get("error", {}).get("message", "Unknown error")
            logger.warning(f"Navidrome health check failed: {err_msg}")
            return ServiceHealth(
                name="navidrome",
                status="down",
                latency_ms=elapsed_ms,
                error=str(err_msg),
            )

        logger.debug(f"Navidrome health check: up ({elapsed_ms}ms)")
        return ServiceHealth(name="navidrome", status="up", latency_ms=elapsed_ms)

    except requests.RequestException as e:
        logger.warning(f"Navidrome health check failed: {e}")
        return ServiceHealth(name="navidrome", status="down", error=str(e))
    except ValueError as e:
        logger.warning(f"Navidrome health check: invalid JSON: {e}")
        return ServiceHealth(
            name="navidrome", status="down", error=f"Invalid JSON: {e}"
        )


def check_listenbrainz(config: Config) -> ServiceHealth:
    """Check ListenBrainz connectivity via GET /1/.

    Status "disabled" if not enabled.  Otherwise "up" iff HTTP status < 500.
    """
    if not config.listenbrainz.enabled:
        logger.debug("ListenBrainz health check: disabled")
        return ServiceHealth(name="listenbrainz", status="disabled")

    url = f"{config.listenbrainz.url}/1/"

    try:
        start = time.monotonic()
        resp = requests.get(url, timeout=5)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code < 500:
            logger.debug(f"ListenBrainz health check: up ({elapsed_ms}ms)")
            return ServiceHealth(
                name="listenbrainz", status="up", latency_ms=elapsed_ms
            )

        logger.warning(f"ListenBrainz health check failed: HTTP {resp.status_code}")
        return ServiceHealth(
            name="listenbrainz",
            status="down",
            latency_ms=elapsed_ms,
            error=f"HTTP {resp.status_code}",
        )

    except requests.RequestException as e:
        logger.warning(f"ListenBrainz health check failed: {e}")
        return ServiceHealth(name="listenbrainz", status="down", error=str(e))


def check_all(
    config: Config,
    listenbrainz_cached: "ServiceHealth | None" = None,
) -> dict[str, ServiceHealth]:
    """Run slskd/Navidrome health checks live; report ListenBrainz from cache.

    ListenBrainz is deliberately NOT live-checked here — this runs on every
    30s frontend poll, and a live LB check often routes through a slow
    SOCKS5 proxy (musica-proxy), adding multi-second latency and occasional
    timeouts to a poll that otherwise finishes in milliseconds. Instead the
    caller passes the last result from an explicit POST
    /api/system/listenbrainz/check (see app/routes/system.py), or None if
    it's never been checked this run.

    Returns:
        {"slskd": ..., "navidrome": ..., "listenbrainz": ...}
    """
    if listenbrainz_cached is not None:
        listenbrainz = listenbrainz_cached
    elif not config.listenbrainz.enabled:
        listenbrainz = ServiceHealth(name="listenbrainz", status="disabled")
    else:
        listenbrainz = ServiceHealth(name="listenbrainz", status="unknown")

    return {
        "slskd": check_slskd(config),
        "navidrome": check_navidrome(config),
        "listenbrainz": listenbrainz,
    }
