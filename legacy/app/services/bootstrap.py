"""Startup filesystem checks — ensures Navidrome-facing files exist under
music_dir. Runs once per app start; never raises, only logs, since a missing
file here degrades a feature (favorites playlist, downloads-dir scanning)
rather than breaking the app.
"""

import logging
from pathlib import Path

import requests

from app.config import Config

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_FAVORITES_TEMPLATE = _ASSETS_DIR / "favorites.nsp"
_NDIGNORE_NAME = ".ndignore"
_FAVORITES_NAME = "favorites.nsp"
# Marks a .ndignore as bootstrap-owned so it can be safely rewritten when
# download_dir changes. A file without this marker is assumed hand-edited
# and is never touched, even if its content is stale.
_NDIGNORE_MARKER = "# Managed by Post-Vinyl — regenerated when download_dir changes\n"


def ensure_navidrome_files(config: Config) -> None:
    """Auto-fill music_dir with the .ndignore and favorites.nsp files
    Navidrome needs, if they aren't already there. Logs a warning (does not
    raise) if a file is still missing after attempting to write it.
    """
    music_dir = config.paths.music_dir
    _ensure_ndignore(music_dir, config.paths.download_dir)
    _ensure_favorites_nsp(music_dir)


def _ensure_ndignore(music_dir: Path, download_dir: str) -> None:
    path = music_dir / _NDIGNORE_NAME
    desired = f"{_NDIGNORE_MARKER}/{download_dir}/\n"

    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "Could not read %s to check for a stale download_dir",
                path,
                exc_info=True,
            )
            return
        if not current.startswith(_NDIGNORE_MARKER):
            return  # Hand-edited — never overwrite.
        if current == desired:
            return
        try:
            path.write_text(desired, encoding="utf-8")
            logger.info("Updated %s for new download_dir /%s/", path, download_dir)
        except OSError:
            logger.warning(
                "Could not update %s for new download_dir /%s/",
                path,
                download_dir,
                exc_info=True,
            )
        return

    try:
        path.write_text(desired, encoding="utf-8")
        logger.info(
            "Created %s excluding /%s/ from Navidrome scans", path, download_dir
        )
    except OSError:
        logger.warning(
            "Could not create %s — Navidrome may scan in-progress downloads",
            path,
            exc_info=True,
        )
    if not path.exists():
        logger.warning("%s still missing after attempting to create it", path)


def check_slskd_download_dir(config: Config) -> None:
    """Warn loudly at startup if slskd's configured download directory
    doesn't match where DownloadMonitor._resolve_source_path() looks for
    completed transfers — and self-heal `.env`'s SLSKD_DOWNLOADS_DIR so the
    *next* restart picks up the right value automatically.

    slskd's `directories.downloads` has no runtime-patchable API (its
    `PATCH /api/v0/options` endpoint only supports listenPort/
    listenIpAddress — confirmed against slskd's own docs), so a mismatch
    right now can only be detected, not fixed live — slskd itself must
    restart to pick up a new SLSKD_DOWNLOADS_DIR. But since that env var is
    generated from `paths.download_dir` (see
    Config.slskd_downloads_dir_env_value()) rather than hand-maintained,
    rewriting it here means the fix requires nothing more than restarting
    the stack — no manual slskd.yml editing. Found live 2026-08-14 in a
    naked-clone test: a fresh clone's default slskd config wrote completed
    downloads somewhere postvinyl could never see, silently dropping every
    transfer.
    """
    expected = str(config.paths.slskd_downloads_path)
    try:
        resp = requests.get(
            f"{config.slskd.url}/api/v0/options",
            headers=(
                {"X-API-Key": config.slskd.api_key} if config.slskd.api_key else {}
            ),
            timeout=5,
        )
        resp.raise_for_status()
        actual = resp.json().get("directories", {}).get("downloads")
    except Exception:
        logger.warning(
            "Could not verify slskd's download directory at startup "
            "(slskd may still be starting) — skipping check",
            exc_info=True,
        )
        return

    if actual != expected:
        logger.error(
            "slskd's download directory (%s) does not match what postvinyl "
            "expects (%s) — every completed Soulseek transfer will silently "
            "time out and be dropped instead of reaching beets/Navidrome. "
            "Rewriting SLSKD_DOWNLOADS_DIR in .env to '%s' — restart the "
            "stack (`docker compose up -d`) to apply it.",
            actual,
            expected,
            expected,
        )
        try:
            config.write_env_values({"SLSKD_DOWNLOADS_DIR": expected})
        except Exception:
            logger.warning(
                "Could not auto-fix SLSKD_DOWNLOADS_DIR in .env — set it "
                "manually to '%s' and restart the stack",
                expected,
                exc_info=True,
            )


def ensure_listenbrainz_linked(config: Config) -> None:
    """Best-effort: (re)link Navidrome's ListenBrainz scrobbling to the
    currently-configured LISTENBRAINZ_TOKEN on every startup.

    Covers the case config.write_env_values() alone doesn't: a token
    already sitting in .env from before the app ever started (hand-edited,
    or restored from a backup) rather than saved through
    POST /api/config/secrets, which is the only other place this link is
    attempted (see app.routes.config.update_secrets). Also self-heals a
    Navidrome that lost its link — e.g. its data volume was reset — without
    requiring the user to re-save the token just to re-trigger it. Cheap
    and idempotent (Navidrome's own PUT /api/listenbrainz/link re-validates
    and re-accepts the same token every time), so no "was it already
    linked" check first.
    """
    if not (config.listenbrainz.token and config.navidrome.username):
        return
    try:
        from app.services.navidrome_library import NavidromeLibrary

        if not NavidromeLibrary(config).link_listenbrainz(config.listenbrainz.token):
            logger.warning(
                "Could not link Navidrome ListenBrainz scrobbling at startup "
                "(Navidrome may still be starting, or the token may be "
                "invalid) — enable it by hand in Navidrome's Personal "
                "Settings if this persists"
            )
    except Exception:
        logger.warning("Navidrome ListenBrainz link at startup failed", exc_info=True)


def _ensure_favorites_nsp(music_dir: Path) -> None:
    path = music_dir / _FAVORITES_NAME
    if path.exists():
        return
    try:
        path.write_text(
            _FAVORITES_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        logger.info(
            "Created %s — Navidrome will pick up the Favorites smart playlist", path
        )
    except OSError:
        logger.warning(
            "Could not create %s — Favorites smart playlist won't appear in Navidrome",
            path,
            exc_info=True,
        )
    if not path.exists():
        logger.warning("%s still missing after attempting to create it", path)
