"""
SlskdSearch — Concrete implementation of SearchService using slskd REST API.

Implements the cancel-to-flush pattern required by slskd 0.26.0+.
"""

import re
import time
import uuid
from datetime import datetime

import requests

from app.config import Config
from app.exceptions import (
    SearchInitiationError,
    SearchNotFoundError,
    SlskdConnectionError,
)
from app.logging_config import get_logger
from app.services.interfaces.search import SearchJob, SearchResult, SearchService
from app.services.query_builder import STOP_WORDS, fold_for_matching, strip_feat
from app.services.search_limiter import SearchRateLimiter

logger = get_logger(__name__)


class SlskdSearch(SearchService):
    """
    slskd-based search implementation.

    Uses the slskd REST API to search for music on Soulseek.
    Implements the cancel-to-flush pattern for slskd 0.26.0+.
    """

    def __init__(self, config: Config, store=None):
        """
        Initialize SlskdSearch.

        Args:
            config: Config object with slskd settings
            store: Optional SearchStore (SQLite-backed). When given, the
                in-memory job map below is rebuilt from the persisted search
                *headers* on first use, so a saved search still resolves
                after a restart. Peer responses are never persisted — slskd
                owns those and serves them by search_id (see migration 005).
                When None (e.g. tests or no database), behavior is purely
                in-memory.
        """
        self.config = config
        self.base_url = config.slskd.url
        self.api_key = config.slskd.api_key
        self.session = requests.Session()

        # In-memory caches. `_searches` is rebuilt from the `searches` table
        # when a store is present; `_responses` is a within-process cache
        # only — on a miss the results are re-fetched from slskd, which is
        # where they actually live.
        self._store = store
        self._hydrated = False
        self._searches: dict[str, SearchJob] = {}
        self._responses: dict[str, list[SearchResult]] = {}
        self._progress: dict[str, dict] = {}

        # Diagnosed 2026-08-13 (see live-artifacts/hang-probe-1): a single
        # search fans out network-wide and briefly opens on the order of
        # thousands of peer connections. That is normal and it drains on its
        # own within about a minute — the actual failure was searches fired
        # back to back with nothing pacing them, so the spikes superimposed
        # and never drained. These two guards sit in front of every new
        # broadcast to slskd; see search_limiter.py for the full account.
        search_cfg = getattr(config, "search", None)
        self._response_limit = getattr(search_cfg, "response_limit", 60)
        self._response_cap = getattr(search_cfg, "response_cap", 250)
        self._rate_limiter = SearchRateLimiter(
            max_searches=getattr(search_cfg, "rate_limit_max_searches", 4),
            window_seconds=getattr(search_cfg, "rate_limit_window_seconds", 60),
        )
        self._rate_limit_wait_timeout = getattr(
            search_cfg, "rate_limit_wait_timeout_seconds", 45
        )
        self._query_cache_ttl = getattr(search_cfg, "query_cache_ttl_seconds", 600)

        # Query cache: normalized query text -> (expires_at monotonic, raw
        # slskd response dicts). Populated only from a real slskd drive,
        # never from a cache hit — refreshing the TTL off another cache hit
        # would let a single real answer live forever without ever being
        # re-verified against the network.
        self._query_cache: dict[str, tuple[float, list[dict]]] = {}

        # search_ids minted by a cache hit. slskd has never heard of these —
        # cancel()/get_status()/get_progress() must never make a network
        # call for one, or they'd just get a 404 back.
        self._local_search_ids: set[str] = set()

        # Raw (pre-artist-filter) responses seeded by a cache hit, consumed
        # the first time get_results() runs for that search_id — mirrors
        # exactly what _drive_search() would have produced, just without
        # asking slskd again.
        self._pending_raw: dict[str, list[dict]] = {}

    def _hydrate(self) -> None:
        """Rebuild the job map from persisted search headers (lazy, once).

        Must be lazy: the SQLite schema is initialized by the app lifespan,
        which runs after services are constructed — touching the DB before
        that would hit missing tables. Without a store this is a no-op.

        Headers only, and user-initiated searches only, so this stays cheap
        no matter how long the app has been running. The version this
        replaced also loaded and JSON-parsed every peer response ever
        stored, making startup proportional to all history.
        """
        if self._hydrated or self._store is None:
            return
        self._hydrated = True
        for row in self._store.all_searches():
            try:
                job = SearchJob(
                    search_id=row["id"],
                    query=row["query"],
                    artist=row["artist"],
                    created_at=datetime.fromtimestamp(row["created_at"]),
                    status=row["status"],
                )
            except Exception:  # noqa: BLE001 — a corrupt row must not kill hydration
                logger.warning("Skipping corrupt searches row: %s", row.get("id"))
                continue
            self._searches[job.search_id] = job
        if self._searches:
            logger.info("Hydrated %d search headers from SQLite", len(self._searches))

    def _persist_status(self, job: SearchJob) -> None:
        """Record a job's status change (no-op without a store).

        Silently affects zero rows for rec-puller searches, which never get
        a header row — that's intended, not a failure.
        """
        if self._store is not None:
            self._store.update_status(job.search_id, job.status)

    def _get_headers(self) -> dict:
        """Get HTTP headers for slskd API."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _cache_key(query: str) -> str:
        return query.strip().lower()

    def _make_local_job(
        self, query: str, artist: str | None, raw_responses: list[dict]
    ) -> SearchJob:
        """Build a SearchJob for a query-cache hit — no slskd round trip.

        `raw_responses` is shared (not copied) across every job that reuses
        this cache entry; that's safe because nothing downstream mutates the
        list or its dicts — `_filter_by_artist` builds a new filtered list.
        """
        search_id = f"cache-{uuid.uuid4().hex[:12]}"
        job = SearchJob(
            search_id=search_id,
            query=query,
            artist=artist,
            created_at=datetime.now(),
            status="searching",
        )
        self._searches[search_id] = job
        self._local_search_ids.add(search_id)
        self._pending_raw[search_id] = raw_responses
        return job

    def search(self, query: str, artist: str | None = None) -> SearchJob:
        """
        Initiate a search on slskd — or reuse a cached one.

        Args:
            query: Track or album name to search for
            artist: Optional artist name for post-filtering

        Returns:
            SearchJob with search_id and metadata

        Raises:
            SearchRateLimitedError: no rate-limit slot freed up within
                `search.rate_limit_wait_timeout_seconds`. Never raised on a
                query-cache hit — a cache hit doesn't touch slskd at all, so
                it doesn't need a slot.

        Two guards sit in front of every new broadcast to the Soulseek
        network (diagnosed 2026-08-13; see search_limiter.py):

        1. Query cache: an identical `query` string searched within the
           last `search.query_cache_ttl_seconds` is served from the prior
           search's raw responses instead of re-broadcasting. This is the
           common case in practice — musica's own search log showed the
           same literal text re-searched ten times in a single run (the
           rec-puller's re-query ladder revisiting a rung, a user re-running
           a search).
        2. Rate limiter: everything that isn't a cache hit still has to
           acquire a slot in a sliding window before it reaches slskd. This
           blocks the caller (both call sites — the manual search route and
           RecPuller — can tolerate a wait) rather than failing outright,
           because a wait is far cheaper than the alternative measured on
           2026-08-13: superimposed search spikes that never got a chance
           to drain.
        """
        logger.info(f"Initiating search: query='{query}', artist='{artist}'")

        self._hydrate()

        cache_key = self._cache_key(query)
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            expires_at, raw_responses = cached
            if expires_at > time.monotonic():
                logger.info(
                    f"Search cache hit: query='{query}' — reusing "
                    f"{len(raw_responses)} prior response(s) without "
                    f"touching slskd"
                )
                return self._make_local_job(query, artist, raw_responses)
            del self._query_cache[cache_key]

        self._rate_limiter.acquire(timeout=self._rate_limit_wait_timeout)

        # Initiate search on slskd
        url = f"{self.base_url}/api/v0/searches"
        payload = {"searchText": query, "responseLimit": self._response_limit}

        try:
            resp = self.session.post(
                url, json=payload, headers=self._get_headers(), timeout=10
            )

            if resp.status_code not in (200, 201):
                raise SearchInitiationError(
                    query, f"HTTP {resp.status_code}: {resp.text}"
                )

            data = resp.json()
            search_id = data.get("id")

            if not search_id:
                raise SearchInitiationError(query, "No search ID returned")

            # Create SearchJob
            job = SearchJob(
                search_id=search_id,
                query=query,
                artist=artist,
                created_at=datetime.now(),
                status="searching",
            )

            # In-memory only; the header row (user searches) is written
            # by the search route, which owns the user-facing history.
            self._searches[search_id] = job

            logger.info(f"Search initiated: id={search_id}")
            return job

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to initiate search: {e}")
            raise SlskdConnectionError(self.base_url, str(e))

    def get_results(self, search_id: str) -> list[SearchResult]:
        """
        Fetch search results from slskd.

        Args:
            search_id: ID from SearchJob

        Returns:
            List of SearchResult objects
        """
        logger.debug(f"Fetching results for search: {search_id}")

        self._hydrate()

        # Check if search exists
        if search_id not in self._searches:
            raise SearchNotFoundError(search_id)

        job = self._searches[search_id]

        # If results already fetched, return them
        if search_id in self._responses:
            return self._responses[search_id]

        # A query-cache hit seeded raw responses at search()-time; use them
        # instead of driving a (nonexistent, from slskd's perspective) search.
        pending_raw = self._pending_raw.pop(search_id, None)
        if pending_raw is not None:
            raw_responses = pending_raw
        else:
            # Drive search to completion (raw slskd response dicts)
            raw_responses, meta = self._drive_search(search_id, job.query, job.artist)

            # An empty meta means the drive itself failed (slskd unreachable
            # mid-poll), not that the search legitimately found nothing. Bail
            # before caching: caching [] here would mark the job completed
            # and make every later call return nothing without re-asking.
            if not meta:
                logger.warning(
                    "Search drive failed for %s; not caching or persisting "
                    "an empty result set",
                    search_id,
                )
                return []

            # Seed the query cache from this real answer — pre-filter, so a
            # later cache hit with a different artist still filters
            # correctly against the full response set.
            if self._query_cache_ttl > 0:
                self._query_cache[self._cache_key(job.query)] = (
                    time.monotonic() + self._query_cache_ttl,
                    raw_responses,
                )

        # Apply artist filter if specified.
        if job.artist:
            raw_responses = self._filter_by_artist(raw_responses, job.artist)

        results = [self._to_search_result(r) for r in raw_responses]

        # Cache within this process only. On a miss — including after a
        # restart — the results are re-fetched from slskd by search_id.
        # _drive_search short-circuits on an already-complete search, so
        # that costs one metadata call plus one responses call, never a
        # new search.
        self._responses[search_id] = results

        job.status = "completed"
        self._persist_status(job)

        logger.info(f"Search completed: id={search_id}, results={len(results)}")
        return results

    def cancel(self, search_id: str) -> bool:
        """
        Cancel an in-progress search.

        Args:
            search_id: ID from SearchJob

        Returns:
            True if cancelled successfully
        """
        logger.info(f"Cancelling search: {search_id}")

        self._hydrate()

        if search_id not in self._searches:
            raise SearchNotFoundError(search_id)

        if search_id in self._local_search_ids:
            # slskd has never heard of a query-cache-hit search_id — nothing
            # to cancel over the network.
            self._searches[search_id].status = "cancelled"
            self._persist_status(self._searches[search_id])
            self._pending_raw.pop(search_id, None)
            logger.info(f"Search cancelled (cache hit, local only): {search_id}")
            return True

        url = f"{self.base_url}/api/v0/searches/{search_id}"

        try:
            resp = self.session.put(url, headers=self._get_headers(), timeout=10)

            if resp.status_code in (200, 204, 304):
                self._searches[search_id].status = "cancelled"
                self._persist_status(self._searches[search_id])
                logger.info(f"Search cancelled: {search_id}")
                return True
            else:
                logger.warning(f"Cancel failed: HTTP {resp.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to cancel search: {e}")
            return False

    def get_status(self, search_id: str) -> SearchJob:
        """
        Get current status of a search job.

        Args:
            search_id: ID from SearchJob

        Returns:
            SearchJob with updated status
        """
        self._hydrate()

        if search_id not in self._searches:
            raise SearchNotFoundError(search_id)

        if search_id in self._local_search_ids:
            # slskd has never heard of a query-cache-hit search_id —
            # nothing to poll.
            return self._searches[search_id]

        # Fetch latest metadata from slskd
        url = f"{self.base_url}/api/v0/searches/{search_id}"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=10)

            if resp.status_code != 200:
                raise SearchNotFoundError(search_id)

            meta = resp.json()
            job = self._searches[search_id]

            # Update status based on slskd response
            if meta.get("isComplete"):
                job.status = "completed"
                self._persist_status(job)

            return job

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get search status: {e}")
            raise SlskdConnectionError(self.base_url, str(e))

    def list_searches(self) -> list[SearchJob]:
        """
        List all search jobs, newest first (ties broken by search_id).

        Returns:
            List of SearchJob objects
        """
        self._hydrate()
        return sorted(
            self._searches.values(),
            key=lambda job: (job.created_at, job.search_id),
            reverse=True,
        )

    def get_progress(self, search_id: str) -> dict:
        """
        Peek at a search's live progress without driving it to completion.

        Args:
            search_id: ID from SearchJob

        Returns:
            Dict with response_count, file_count, is_complete,
            elapsed_seconds, threshold, max_wait_seconds
        """
        self._hydrate()

        if search_id not in self._searches:
            raise SearchNotFoundError(search_id)

        job = self._searches[search_id]
        elapsed = (datetime.now() - job.created_at).total_seconds()
        cached_progress = self._progress.get(search_id)

        if job.status != "searching" or search_id in self._local_search_ids:
            # Already driven to completion (or cancelled), or a query-cache
            # hit that slskd has never heard of — no need to hit slskd
            # again, just report what we already know. A cache hit's count
            # comes from `_pending_raw` until get_results() runs and moves
            # it into `_responses`.
            known = self._responses.get(search_id) or self._pending_raw.get(
                search_id, []
            )
            cap_progress = (
                cached_progress
                if cached_progress
                and cached_progress.get("stop_reason") == "response_cap"
                else None
            )
            response_count = (
                cap_progress.get("response_count") if cap_progress else len(known)
            )
            file_count = cap_progress.get("file_count") if cap_progress else len(known)
            return {
                "response_count": response_count,
                "file_count": file_count,
                "is_complete": True,
                "elapsed_seconds": round(elapsed, 1),
                "threshold": self.config.search.response_threshold,
                "response_cap": self._response_cap,
                "max_wait_seconds": self.config.search.wait_seconds,
                "stop_reason": cap_progress.get("stop_reason")
                if cap_progress
                else None,
            }

        try:
            meta = self._get_search_meta(search_id)
        except SearchNotFoundError:
            raise
        except Exception as e:
            logger.warning(f"Progress poll error for {search_id}: {e}")
            meta = {}

        response_count = meta.get("responseCount", 0)
        file_count = meta.get("fileCount", 0)
        stop_reason = None
        self._progress[search_id] = {
            "response_count": response_count,
            "file_count": file_count,
            "is_complete": meta.get("isComplete", False),
        }
        if not meta.get("isComplete") and response_count >= self._response_cap:
            logger.info(
                "Response cap reached: %s >= %s, cancelling to flush",
                response_count,
                self._response_cap,
            )
            if self.cancel(search_id):
                stop_reason = "response_cap"
                self._progress[search_id]["stop_reason"] = stop_reason

        return {
            "response_count": response_count,
            "file_count": file_count,
            "is_complete": meta.get("isComplete", False) or stop_reason is not None,
            "elapsed_seconds": round(elapsed, 1),
            "threshold": self.config.search.response_threshold,
            "response_cap": self._response_cap,
            "max_wait_seconds": self.config.search.wait_seconds,
            "stop_reason": stop_reason,
        }

    def _drive_search(
        self,
        search_id: str,
        query: str,
        artist: str | None,
        threshold: int | None = None,
        min_wait: int | None = None,
        max_wait: int | None = None,
        poll_interval: int | None = None,
    ) -> tuple[list[dict], dict]:
        """
        Drive a search to completion using cancel-to-flush pattern.

        Polls metadata, cancels at threshold/deadline, then fetches responses.

        Returns:
            Tuple of (raw slskd response dicts, metadata). The caller is
            responsible for converting to SearchResult via _to_search_result.

            An empty metadata dict is the failure signal: it means polling
            slskd raised, so the empty response list carries no information
            about what the search would have found. Callers must not treat
            that as a completed, zero-result search.
        """
        threshold = threshold or self.config.search.response_threshold
        min_wait = min_wait or self.config.search.min_wait_seconds
        max_wait = max_wait or self.config.search.wait_seconds
        poll_interval = poll_interval or self.config.search.poll_interval

        start = time.time()
        job = self._searches.get(search_id)
        cancelled = job is not None and job.status == "cancelled"

        while True:
            elapsed = time.time() - start

            try:
                meta = self._get_search_meta(search_id)
            except Exception as e:
                logger.error(f"Poll error: {e}")
                return ([], {})

            response_count = meta.get("responseCount", 0)
            file_count = meta.get("fileCount", 0)
            is_complete = meta.get("isComplete", False)
            self._progress[search_id] = {
                "response_count": response_count,
                "file_count": file_count,
                "is_complete": is_complete,
            }

            # Natural completion
            if is_complete and not cancelled:
                logger.info(
                    f"Search completed naturally: {elapsed:.1f}s, {response_count} responses"
                )
                break

            # Hard response cap reached
            if not cancelled and response_count >= self._response_cap:
                logger.info(
                    f"Response cap reached: {response_count} >= {self._response_cap}, cancelling to flush"
                )
                self.cancel(search_id)
                self._progress[search_id]["stop_reason"] = "response_cap"
                cancelled = True
                continue

            # Threshold reached
            if not cancelled and response_count >= threshold and elapsed >= min_wait:
                logger.info(
                    f"Threshold reached: {response_count} >= {threshold}, cancelling to flush"
                )
                self.cancel(search_id)
                cancelled = True
                continue

            # Deadline reached
            if not cancelled and elapsed >= max_wait:
                logger.warning(f"Deadline reached: {max_wait}s, cancelling to flush")
                self.cancel(search_id)
                cancelled = True
                continue

            # Finalized after cancel
            if cancelled and is_complete:
                logger.info(f"Search finalized after cancel: {elapsed:.1f}s")
                break

            # Timeout after cancel
            if cancelled and time.time() - start >= max_wait + 8:
                logger.warning(
                    "Search did not finalize within 8s after cancel, proceeding anyway"
                )
                break

            time.sleep(poll_interval)

        # Fetch responses
        responses = self._fetch_responses(search_id)

        return (responses, meta)

    def _get_search_meta(self, search_id: str) -> dict:
        """Get search metadata from slskd."""
        url = f"{self.base_url}/api/v0/searches/{search_id}"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=10)

            if resp.status_code != 200:
                raise SearchNotFoundError(search_id)

            return resp.json()

        except requests.exceptions.RequestException as e:
            raise SlskdConnectionError(self.base_url, str(e))

    # Poll budget for the post-cancel flush. Bounded by wall clock, not by
    # an attempt count: the previous `for attempt in range(10)` with a 15s
    # per-request timeout meant a slow slskd could hold this — and the API
    # request waiting on it — for ~155s, while the code logged "not
    # available after 5s". A slow answer to "have you flushed yet?" *means*
    # not-yet, so the per-request timeout is short and we simply re-ask.
    _FLUSH_DEADLINE_SECONDS = 5.0
    _FLUSH_POLL_INTERVAL = 0.5
    _FLUSH_REQUEST_TIMEOUT = 3.0

    def _fetch_responses(self, search_id: str) -> list[dict]:
        """Fetch search responses from slskd, polling until the flush lands."""
        url = f"{self.base_url}/api/v0/searches/{search_id}/responses"
        deadline = time.time() + self._FLUSH_DEADLINE_SECONDS

        while True:
            try:
                resp = self.session.get(
                    url,
                    headers=self._get_headers(),
                    timeout=self._FLUSH_REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Failed to fetch responses: HTTP {resp.status_code}"
                    )
                else:
                    responses = resp.json()
                    if responses:
                        elapsed = self._FLUSH_DEADLINE_SECONDS - (
                            deadline - time.time()
                        )
                        logger.debug(f"Responses available after {elapsed:.1f}s")
                        return responses
            except Exception as e:  # noqa: BLE001 — any failure here just means "retry"
                logger.warning(f"Fetch error: {e}")

            if time.time() >= deadline:
                break
            time.sleep(min(self._FLUSH_POLL_INTERVAL, max(0.0, deadline - time.time())))

        logger.warning(
            f"Responses not available after {self._FLUSH_DEADLINE_SECONDS:.0f}s"
        )
        return []

    def _to_search_result(self, response: dict) -> SearchResult:
        """Convert slskd response to SearchResult."""
        # slskd response structure:
        # {
        #   "username": "peer1",
        #   "files": [
        #     {
        #       "filename": "song.mp3",
        #       "size": 5242880,
        #       "bitRate": 320,
        #       "duration": 240
        #     }
        #   ],
        #   "hasFreeUploadSlot": true,
        #   "uploadSpeed": 102400
        # }

        files = response.get("files", [])
        first_file = files[0] if files else {}

        return SearchResult(
            username=response.get("username", ""),
            filename=first_file.get("filename", ""),
            size=first_file.get("size", 0),
            has_free_slot=response.get("hasFreeUploadSlot", False),
            upload_speed=response.get("uploadSpeed"),
            bitrate=str(first_file.get("bitRate", ""))
            if first_file.get("bitRate")
            else None,
            duration=first_file.get("duration"),
        )

    def _filter_by_artist(self, results: list[dict], artist: str) -> list[dict]:
        """
        Filter raw slskd responses by artist words.

        Post-filters search responses to only include peers whose filenames
        contain the artist words.
        """
        artist_words = self._extract_artist_words(artist)

        if not artist_words:
            return results

        filtered = []
        for result in results:
            files = result.get("files", [])
            raw_filename = files[0].get("filename", "") if files else ""
            # Fold the filename too, not just the artist words (2026-08-12):
            # a peer who kept an accent in their filename ("Björk") would
            # never match a folded word ("bjork") otherwise — folding both
            # sides puts the comparison in one consistent space regardless
            # of which way a given peer happened to spell it.
            filename_lower = fold_for_matching(raw_filename).lower()

            # Check if all artist words are present in filename
            if all(word in filename_lower for word in artist_words):
                filtered.append(result)

        logger.debug(f"Artist filter: {len(results)} → {len(filtered)} results")
        return filtered

    def _extract_artist_words(self, artist: str) -> list[str]:
        """
        Extract meaningful words from artist name.

        Feat-clause truncated first (P6.5-6): "Alesso feat. Katy Perry"
        must become "Alesso" — the featured artist's name must not win
        the post-filter. Shares the query pipeline's stop-word set so the
        post-filter and the query construction agree.

        Accent-folded (2026-08-12) via the shared `fold_for_matching`, so
        an artist word here matches the same folded, ASCII form used both
        by the query itself and by `_filter_by_artist`'s now-also-folded
        filename check — "björk" -> "bjork" on both sides of the
        comparison, regardless of which way a given peer spelled it.
        """
        # Normalize (feat-clause truncated, accents folded)
        artist = fold_for_matching(strip_feat(artist)).lower().strip()

        # Split into words
        words = re.split(r"[\s\-\_\.\(\)\[\]]+", artist)

        # Filter stopwords and short words
        meaningful = [
            word for word in words if len(word) > 2 and word not in STOP_WORDS
        ]

        return meaningful
