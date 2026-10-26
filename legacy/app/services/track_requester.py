"""
track_requester — the shared search-and-queue driver.

The single source of truth for the candidate-filter and re-query-ladder
logic that RecPuller (recommendations) and the MusicBrainz resolve job both
need. Extracted from `RecPuller._run_pull` so the two callers can't drift.

Self-contained on purpose: no DB or worker imports. Everything here is pure
filtering/looping against the `SearchService` interface, so it can be unit
tested with a scripted fake and reused anywhere a track+artist needs turning
into a Soulseek download.
"""

import re

from app.exceptions import (
    SearchInitiationError,
    SearchNotFoundError,
    SearchRateLimitedError,
    SlskdConnectionError,
)
from app.logging_config import get_logger
from app.services.interfaces.search import SearchJob, SearchResult, SearchService
from app.services.query_builder import (
    REMIX_QUALIFIERS,
    STOP_WORDS,
    build_search_queries,
    fold_for_matching,
    strip_feat,
)

logger = get_logger(__name__)

ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}

#: Matches `recommendation._artist_words`' tokenizer: anything that isn't a
#: word char (Unicode-aware `\w`), whitespace, or one of `. ! ? &` becomes a
#: separator. Folded first, so periods and accents are already gone — this
#: pass only catches the remaining punctuation ("Artist-Name" -> "Artist Name").
_ARTIST_SEPARATOR_RE = re.compile(r"[^\w\s.!?&]")


def artist_words(artist: str) -> list[str]:
    """Extract meaningful words from an artist name.

    Identical to `recommendation._artist_words` (moved here as the shared
    single source): feat-clause truncated first ("Alesso feat. Katy Perry"
    -> "Alesso"), accents folded and periods stripped, then stop words,
    digit-only tokens and single-char tokens dropped. An empty result is
    meaningful downstream: it disables the artist-containment filter rather
    than rejecting everything.
    """
    cleaned = _ARTIST_SEPARATOR_RE.sub(" ", fold_for_matching(strip_feat(artist or "")))
    tokens = cleaned.lower().split()

    meaningful: list[str] = []
    for token in tokens:
        if token in STOP_WORDS:
            continue
        if len(token) < 2:
            continue
        if token.isdigit():
            continue
        meaningful.append(token)

    return meaningful


def is_viable_candidate(
    result: SearchResult,
    artist_words: list[str],
    min_words: int = 1,
) -> bool:
    """Client-side candidate filters (P6.5-6).

    - artist containment: the (folded) filename must contain at least
      `min_words` of the artist's words. The requirement is capped at
      `len(artist_words)`, so a single-word artist ("Björk", "Rihanna") or a
      name that filters down to one word ("The Beatles" -> "beatles") is
      never rejected by a `min_words` higher than its word count — the
      filter degrades to "any word" instead of demanding a word that doesn't
      exist. An empty `artist_words` disables the check entirely (matches
      `_filepath_contains_artist`'s pass-through on empty input).
    - remix-qualifier exclusion: reject filenames advertising a cover/live/
      remix version. Multi-word qualifiers ("8d audio") match as substrings.
    - audio extension: only `.mp3/.flac/.m4a/.wav/.ogg/.aac`.
    """
    if artist_words:
        lower = fold_for_matching(result.filename).lower()
        required = min(min_words, len(artist_words))
        if sum(1 for word in artist_words if word in lower) < required:
            return False
    if any(q in result.filename.lower() for q in REMIX_QUALIFIERS):
        return False
    ext = result.filename.lower().rsplit(".", 1)[-1] if "." in result.filename else ""
    return f".{ext}" in ALLOWED_EXTENSIONS


def run_ladder(
    search_service: SearchService,
    config,
    track: str,
    artist: str,
) -> tuple[SearchJob | None, list[SearchResult], str | None]:
    """Walk the re-query ladder until a rung clears the pass-ratio threshold.

    Reproduces RecPuller's ladder loop exactly: `build_search_queries(track,
    artist)`; for each rung `search(query)` then `get_results(search_id)`;
    filter via `is_viable_candidate`; compute the pass ratio; keep the
    best-ratio rung; stop early once a rung reaches
    `search.pass_ratio_threshold` (default 0.6).

    Error handling: a search failure on rung 0 is recorded (returned as
    `search_error`) and stops the ladder; a failure on a later rung is
    logged and stops the ladder too, falling back to the best rung seen so
    far.

    Returns `(best_job, best_filtered, search_error_or_None)`. `best_job` is
    None only when no rung ever returned results; in that case `best_filtered`
    is empty too.
    """
    queries = build_search_queries(track, artist)
    threshold = getattr(getattr(config, "search", None), "pass_ratio_threshold", 0.6)
    min_words = getattr(getattr(config, "search", None), "artist_match_min_words", 1)
    words = artist_words(artist)

    search_error: str | None = None
    best_ratio = -1.0
    best_filtered: list[SearchResult] = []
    best_job: SearchJob | None = None

    for qi, query in enumerate(queries):
        if qi > 0:
            logger.info(
                "pass-ratio re-query for %s - %s (rung %d): '%s'",
                artist,
                track,
                qi,
                query,
            )
        try:
            job = search_service.search(query)
            results = search_service.get_results(job.search_id)
        except (
            SlskdConnectionError,
            SearchNotFoundError,
            SearchInitiationError,
            SearchRateLimitedError,
        ) as e:
            if qi == 0:
                search_error = str(e)
            else:
                # A transient failure on a re-query rung doesn't invalidate
                # earlier rungs — fall back to the best seen so far.
                logger.warning(
                    "re-query rung %d failed for %s - %s: %s",
                    qi,
                    artist,
                    track,
                    e,
                )
            break

        filtered = [r for r in results if is_viable_candidate(r, words, min_words)]
        ratio = len(filtered) / len(results) if results else 0.0
        logger.info(
            "query '%s' -> %d results, %d viable (pass ratio %.2f)",
            query,
            len(results),
            len(filtered),
            ratio,
        )
        if ratio > best_ratio:
            best_ratio = ratio
            best_filtered = filtered
            best_job = job
        if ratio >= threshold:
            break

    return best_job, best_filtered, search_error
