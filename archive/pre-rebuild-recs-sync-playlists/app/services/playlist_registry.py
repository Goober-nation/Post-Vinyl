"""
Playlist registry — resolves a stable "role" (trash, comfort_zone,
fresh_picks, deep_cuts) to a live Navidrome playlist ID, tracking that ID in
PlaylistStore so a rename performed directly in Navidrome doesn't orphan
musica's bookkeeping the way a pure name-match would (Navidrome keeps the
same ID across a rename; musica's old find-by-name lookups did not survive
one).

Once an ID is bound to a role, the configured display name (desired_name)
becomes authoritative: if Navidrome's playlist has drifted from it — most
likely because someone renamed it by hand in Navidrome — resolve_playlist_id
renames it back on the next call. Rename a musica-tracked playlist by
editing its *_playlist_name config setting, not directly in Navidrome.
"""

from app.db.playlist_store import PlaylistStore
from app.logging_config import get_logger

logger = get_logger(__name__)


def resolve_playlist_id(
    role: str,
    desired_name: str,
    existing: list,
    store: PlaylistStore,
    library_service,
    create_if_missing: bool,
) -> str | None:
    """Resolve `role` to a Navidrome playlist ID.

    `existing` is a fresh list_playlists() result the caller already has —
    passed in rather than fetched here to avoid a redundant API call per
    role per cycle.

    Resolution order: stored ID (re-asserting desired_name onto Navidrome if
    it drifted) -> name match (adopts and stores that ID) -> create (only
    when create_if_missing).
    """
    stored_id = store.get(role)
    if stored_id:
        match = next((p for p in existing if p.playlist_id == stored_id), None)
        if match is not None:
            if match.name != desired_name:
                try:
                    library_service.rename_playlist(stored_id, desired_name)
                    logger.info(
                        "Playlist registry: renamed '%s' -> '%s' (role=%s)",
                        match.name,
                        desired_name,
                        role,
                    )
                except Exception:
                    logger.warning(
                        "Playlist registry: rename to '%s' failed (role=%s)",
                        desired_name,
                        role,
                        exc_info=True,
                    )
            return stored_id
        # Stored ID no longer resolves (playlist deleted in Navidrome) —
        # fall through and re-resolve by name/create like a fresh role.

    name_match = next(
        (p for p in existing if p.name.lower() == desired_name.lower()), None
    )
    if name_match is not None:
        store.set(role, name_match.playlist_id)
        return name_match.playlist_id

    if not create_if_missing:
        return None

    try:
        playlist_id = library_service.create_playlist(desired_name)
    except Exception:
        logger.exception(
            "Playlist registry: create_playlist failed for '%s' (role=%s)",
            desired_name,
            role,
        )
        return None
    store.set(role, playlist_id)
    return playlist_id
