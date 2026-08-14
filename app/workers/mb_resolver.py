"""
MBResolver — background "resolve & queue" job for MusicBrainz downloads.

Turns a MusicBrainz recording or album into Soulseek downloads: look up the
recording(s), run each through the shared search ladder, and queue the first
viable free-slot peer. Runs entirely in a daemon thread — the route starts it
and returns a job_id immediately; progress is surfaced over SSE
(`mb.resolve_started` / `mb.track_queued` / `mb.track_failed` /
`mb.resolve_completed`).

Deliberately separate from RecPuller: a MusicBrainz download is a user action
("get me this album"), not a recommendation pull. It shares only the search
ladder (see `app.services.track_requester`).
"""

import threading
import time
import uuid

from app.db.download_store import DownloadStore
from app.exceptions import ServiceConnectionError
from app.logging_config import get_logger
from app.services import track_requester

logger = get_logger(__name__)

# Pace between an album's tracks, mirroring RecPuller.DOWNLOAD_PACE_SECONDS:
# firing every track's search+queue within the same few seconds would
# superimpose slskd peer-connection spikes the same way a fast rec pull does.
DOWNLOAD_PACE_SECONDS = 2.0


def start_resolve_job(
    scope: str,
    mbid: str,
    musicbrainz_service,
    search_service,
    download_service,
    config,
    db,
    event_hub,
) -> str:
    """Start a background resolve-and-queue job; returns its `job_id`.

    `scope` is "recording" (one track) or "album" (every track of the
    release group's canonical release). The job runs in a daemon thread so
    the calling route returns 202 immediately; it never raises out of the
    thread.
    """
    job_id = uuid.uuid4().hex

    def _run() -> None:
        _resolve(
            job_id,
            scope,
            mbid,
            musicbrainz_service,
            search_service,
            download_service,
            config,
            db,
            event_hub,
        )

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _collect_recordings(scope: str, mbid: str, musicbrainz_service) -> list:
    """The recording(s) a job resolves, per its scope."""
    if scope == "recording":
        recording = musicbrainz_service.lookup_recording(mbid)
        return [recording] if recording is not None else []
    return musicbrainz_service.lookup_release_group_tracks(mbid)


def _resolve_one(
    job_id: str,
    recording,
    search_service,
    download_service,
    config,
    db,
    event_hub,
    used_peers: set[str],
) -> bool:
    """Queue one recording; returns True if queued, False otherwise.

    Publishes `mb.track_failed` (with an `error` string) on every failure
    path and `mb.track_queued` on success.
    """
    title = recording.title
    artist = recording.artist

    best_job, best_filtered, search_error = track_requester.run_ladder(
        search_service, config, title, artist
    )

    if search_error is not None:
        event_hub.publish(
            "mb.track_failed",
            {
                "job_id": job_id,
                "mbid": recording.mbid,
                "title": title,
                "artist": artist,
                "error": search_error,
            },
        )
        return False

    if not best_filtered or best_job is None:
        event_hub.publish(
            "mb.track_failed",
            {
                "job_id": job_id,
                "mbid": recording.mbid,
                "title": title,
                "artist": artist,
                "error": "no viable candidate",
            },
        )
        return False

    peer_store = DownloadStore(db) if db is not None else None
    candidates = [result for result in best_filtered if result.has_free_slot]
    if peer_store is not None:
        ban_seconds = (
            getattr(getattr(config, "download", None), "peer_ban_days", 2) * 86400
        )
        threshold = getattr(getattr(config, "download", None), "bad_peer_threshold", 1)
        candidates = [
            result
            for result in candidates
            if not peer_store.is_peer_blocked(result.username, ban_seconds)
            and peer_store.get_peer_failure_count(result.username) < threshold
        ]

    unused = [result for result in candidates if result.username not in used_peers]
    ordered = unused + [
        result for result in candidates if result.username in used_peers
    ]
    max_attempts = max(
        1,
        int(getattr(getattr(config, "download", None), "max_retries_per_track", 5)),
    )
    for result in ordered[:max_attempts]:
        try:
            queue_result = download_service.queue(
                result.username,
                [{"filename": result.filename, "size": result.size}],
                search_id=best_job.search_id,
            )
        except Exception as e:  # noqa: BLE001 — queue() can raise impl-specific errors
            logger.warning(
                "mb resolver: queue failed for %s - %s via %s: %s",
                artist,
                title,
                result.username,
                e,
            )
            continue
        if queue_result.enqueued_count > 0:
            if peer_store is not None:
                peer_store.insert_pending(
                    search_id=best_job.search_id,
                    username=result.username,
                    filename=result.filename,
                    size=result.size,
                    is_rec_download=False,
                    is_library_download=True,
                    mb_recording_id=recording.mbid,
                )
            used_peers.add(result.username)
            event_hub.publish(
                "mb.track_queued",
                {
                    "job_id": job_id,
                    "mbid": recording.mbid,
                    "title": title,
                    "artist": artist,
                },
            )
            return True

    event_hub.publish(
        "mb.track_failed",
        {
            "job_id": job_id,
            "mbid": recording.mbid,
            "title": title,
            "artist": artist,
            "error": "all candidates exhausted",
        },
    )
    return False


def _resolve(
    job_id: str,
    scope: str,
    mbid: str,
    musicbrainz_service,
    search_service,
    download_service,
    config,
    db,
    event_hub,
) -> None:
    """The full resolve job. Never raises — every failure is logged and
    counted, and `mb.resolve_completed` always fires."""
    queued = 0
    failed = 0
    used_peers: set[str] = set()
    try:
        recordings = _collect_recordings(scope, mbid, musicbrainz_service)
        event_hub.publish(
            "mb.resolve_started",
            {"job_id": job_id, "scope": scope, "count": len(recordings)},
        )
        for i, recording in enumerate(recordings):
            if i > 0:
                time.sleep(DOWNLOAD_PACE_SECONDS)
            try:
                if _resolve_one(
                    job_id,
                    recording,
                    search_service,
                    download_service,
                    config,
                    db,
                    event_hub,
                    used_peers,
                ):
                    queued += 1
                else:
                    failed += 1
            except Exception:  # one track must not kill the job
                logger.exception(
                    "mb resolver: track %s (%s - %s) failed unexpectedly",
                    recording.mbid,
                    recording.artist,
                    recording.title,
                )
                failed += 1
    except ServiceConnectionError as e:
        # A downstream service (MusicBrainz, slskd, ...) being transiently
        # unreachable is expected/environmental, not a bug — a full
        # traceback here reads as a crash when it's really just "try again
        # later". logger.exception() is reserved for genuinely unexpected
        # failures below.
        logger.warning("mb resolver job %s: %s", job_id, e)
    except Exception:  # the thread must never raise
        logger.exception("mb resolver job %s failed", job_id)
    finally:
        event_hub.publish(
            "mb.resolve_completed",
            {"job_id": job_id, "queued": queued, "failed": failed},
        )
