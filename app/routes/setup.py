"""
First-run setup wizard API routes.

Endpoints:
    GET  /api/setup/status            -> What's configured, what's left (200)
    POST /api/setup/navidrome         -> Create/verify the Navidrome account (200)
    POST /api/setup/slskd             -> Save the Soulseek login (200, restart needed)
    GET  /api/setup/slskd/check       -> Connection status + reason after a restart (200)
    POST /api/setup/complete          -> Mark the wizard finished (200)
    POST /api/setup/tutorial/dismiss  -> Mark the tutorial dismissed (200)
    POST /api/setup/rerun             -> Reset wizard/tutorial flags for a replay (200)

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

import hashlib
import random
import string
from typing import Annotated

import requests
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import Config
from app.db.database import Database
from app.db.setup_store import (
    TUTORIAL_DISMISSED,
    WIZARD_COMPLETED,
    SetupStore,
)
from app.dependencies import get_config, get_db
from app.exceptions import ConfigValidationError, NavidromeConnectionError
from app.logging_config import get_logger
from app.services.health import get_slskd_login_error

logger = get_logger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

ConfigDep = Annotated[Config, Depends(get_config)]
DbDep = Annotated[Database, Depends(get_db)]


def get_setup_store(db: DbDep) -> SetupStore:
    return SetupStore(db)


SetupStoreDep = Annotated[SetupStore, Depends(get_setup_store)]


# ============================================================================
# Request/response models
# ============================================================================


class SetupStatusResponse(BaseModel):
    wizard_completed: bool
    tutorial_dismissed: bool
    navidrome_configured: bool
    slskd_configured: bool
    listenbrainz_configured: bool


class AccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class NavidromeSetupResponse(BaseModel):
    created: bool  # True if a new admin was created, False if an existing
    # account's credentials were just verified and saved


class SlskdSetupResponse(BaseModel):
    saved: bool
    requires_restart: bool
    restart_hint: str


class SimpleOkResponse(BaseModel):
    ok: bool


class SlskdCheckResponse(BaseModel):
    connected: bool
    error: str | None = None


# ============================================================================
# Routes
# ============================================================================


@router.get("/status", response_model=SetupStatusResponse)
def setup_status(config: ConfigDep, store: SetupStoreDep) -> dict:
    return {
        "wizard_completed": store.is_flag_set(WIZARD_COMPLETED),
        "tutorial_dismissed": store.is_flag_set(TUTORIAL_DISMISSED),
        "navidrome_configured": bool(
            config.navidrome.username and config.navidrome.password
        ),
        "slskd_configured": bool(
            config.slskd.network_username and config.slskd.network_password
        ),
        "listenbrainz_configured": bool(
            config.listenbrainz.token and config.listenbrainz.username
        ),
    }


@router.post("/navidrome", response_model=NavidromeSetupResponse)
def setup_navidrome(body: AccountRequest, config: ConfigDep) -> dict:
    """Create the Navidrome admin account, or verify+save existing ones.

    Navidrome's own first-run bootstrap endpoint (POST /auth/createAdmin)
    only succeeds while zero users exist. If an admin already exists (e.g.
    the user created one through Navidrome's own UI, or this is a wizard
    re-run), fall back to verifying the given credentials against Navidrome
    directly instead of creating a second admin.
    """
    create_url = f"{config.navidrome.url}/auth/createAdmin"
    try:
        resp = requests.post(
            create_url,
            json={"username": body.username, "password": body.password},
            timeout=10,
        )
    except requests.RequestException as e:
        raise NavidromeConnectionError(config.navidrome.url, str(e))

    if resp.status_code == 200:
        config.write_env_values(
            {
                "NAVIDROME_USERNAME": body.username,
                "NAVIDROME_PASSWORD": body.password,
            }
        )
        logger.info("Setup: created Navidrome admin account %s", body.username)
        return {"created": True}

    if resp.status_code == 403:
        if _verify_navidrome_credentials(config.navidrome.url, body.username, body.password):
            config.write_env_values(
                {
                    "NAVIDROME_USERNAME": body.username,
                    "NAVIDROME_PASSWORD": body.password,
                }
            )
            logger.info("Setup: verified existing Navidrome account %s", body.username)
            return {"created": False}
        raise ConfigValidationError(
            "navidrome_credentials",
            body.username,
            "Navidrome already has an admin account and these credentials "
            "don't match it — enter the existing admin's username/password",
        )

    raise NavidromeConnectionError(
        config.navidrome.url, f"HTTP {resp.status_code} from /auth/createAdmin"
    )


def _verify_navidrome_credentials(url: str, username: str, password: str) -> bool:
    """Check username/password against Navidrome's Subsonic ping endpoint."""
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    try:
        resp = requests.get(
            f"{url}/rest/ping.view",
            params={
                "u": username,
                "t": token,
                "s": salt,
                "v": "1.16.1",
                "c": "postvinyl",
                "f": "json",
            },
            timeout=10,
        )
        data = resp.json()
        return data.get("subsonic-response", {}).get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


@router.post("/slskd", response_model=SlskdSetupResponse)
def setup_slskd(body: AccountRequest, config: ConfigDep) -> dict:
    """Save the chosen Soulseek login for the slskd container.

    slskd reads its Soulseek credentials from env vars resolved from .env at
    container *creation* — musica has no way to make it reconnect with new
    credentials without the container picking up the new value (no Docker
    socket access, by design). This just saves the values; the caller is
    responsible for getting slskd to pick them up and then calling
    GET /api/setup/slskd/check to see whether the chosen username logged in
    successfully.

    `docker compose restart slskd` does NOT work for this — restart reuses
    the container's existing environment from whenever it was last created,
    it doesn't re-read .env. `docker compose up -d slskd` is required so
    Compose re-resolves ${SLSKD_NETWORK_USERNAME}/${...PASSWORD} and
    recreates the container with the new values (found live 2026-08-14: a
    plain restart silently kept the stale credentials and never connected).
    """
    config.write_env_values(
        {
            "SLSKD_NETWORK_USERNAME": body.username,
            "SLSKD_NETWORK_PASSWORD": body.password,
        }
    )
    logger.info("Setup: saved slskd Soulseek login for %s", body.username)
    return {
        "saved": True,
        "requires_restart": True,
        "restart_hint": "docker compose up -d slskd",
    }


@router.get("/slskd/check", response_model=SlskdCheckResponse)
def check_slskd_login(config: ConfigDep) -> dict:
    """Check whether slskd is connected after a restart, and why not if not.

    Soulseek has no separate "is this username taken" check — logging in
    with a brand-new username is how it gets registered, so this is only
    answerable after slskd has actually tried. Distinguishes a credential
    failure (username taken by someone else) from a generic disconnect by
    reading slskd's own logs (see app/services/health.py) when possible.

    Deliberately does its own plain connectivity check rather than reusing
    health.check_slskd() — that function fires a throttled self-healing
    reconnect on every "down" result, which is the right behavior for the
    routine 30s status poll but not for a wizard step deciding whether to
    ask the user to pick a different username.
    """
    try:
        resp = requests.get(
            f"{config.slskd.url}/api/v0/server",
            headers={"X-API-Key": config.slskd.api_key} if config.slskd.api_key else {},
            timeout=5,
        )
        connected = resp.status_code == 200 and bool(resp.json().get("isConnected"))
    except (requests.RequestException, ValueError):
        connected = False

    if connected:
        return {"connected": True, "error": None}
    return {"connected": False, "error": get_slskd_login_error(config)}


@router.post("/complete", response_model=SimpleOkResponse)
def setup_complete(store: SetupStoreDep) -> dict:
    store.set_flag(WIZARD_COMPLETED)
    return {"ok": True}


@router.post("/tutorial/dismiss", response_model=SimpleOkResponse)
def dismiss_tutorial(store: SetupStoreDep) -> dict:
    store.set_flag(TUTORIAL_DISMISSED)
    return {"ok": True}


@router.post("/rerun", response_model=SimpleOkResponse)
def rerun_setup(store: SetupStoreDep) -> dict:
    """Reset wizard/tutorial flags so the full flow replays.

    Existing saved values (Navidrome/slskd credentials already in .env,
    already-configured status) are left as-is — the wizard's own steps
    pre-fill from /api/setup/status and current config, so nothing already
    working is lost by re-running.
    """
    store.set(WIZARD_COMPLETED, "0")
    store.set(TUTORIAL_DISMISSED, "0")
    return {"ok": True}
