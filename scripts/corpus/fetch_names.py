"""Fetch raw song names from Deezer, MusicBrainz and ListenBrainz (#69).

Builds the song set for the normalization test (milestone 1, stage 1) and
saves the names each source gives, untouched, so the normalizer and its
report (#70) run offline.

Songs:
- Deezer "Top <country>" chart playlists, ``--per-country`` kept per country.
- ``--random`` random MusicBrainz recordings that carry an ISRC (a wildcard
  ISRC search for a random country code and year at a random offset).
- ``--random-ru`` random MusicBrainz recordings with RU ISRCs, since Deezer's
  "Top Russia" chart is empty.

The same song across sources is the same ISRC: Deezer track ↔ MusicBrainz
``/isrc`` lookup ↔ ListenBrainz metadata for those recording MBIDs. Songs
without a shared ISRC are dropped and counted.

Run (public APIs only, MusicBrainz at 1 req/s; a full run takes ~15 min)::

    python -m scripts.corpus.fetch_names
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OUT_PATH = Path("tests/fixtures/normalization/songs.json")
USER_AGENT = "PostVinyl-research/0.1 (https://github.com/Goober-nation/Post-Vinyl)"

DEEZER = "https://api.deezer.com"
MUSICBRAINZ = "https://musicbrainz.org/ws/2"
LISTENBRAINZ = "https://api.listenbrainz.org/1"
DEEZER_CHARTS_USER = 637006841  # "Deezer Charts" editorial account

# Playlist title per country (decided with the user 2026-10-27). "Top Russia" is
# dead (Deezer left Russia, 1 track), so Russian songs come from a random
# MusicBrainz bucket of RU ISRCs instead (user, 2026-10-27).
CHART_COUNTRIES = {
    "US": "Top USA",
    "GB": "Top UK",
    "DE": "Top Germany",
    "FR": "Top France",
    "BR": "Top Brazil",
    "ES": "Top Spain",
    "IT": "Top Italy",
    "JP": "Top Japan",
    "KR": "Top South Korea",
}
# ISRC country codes for the random sample: the chart countries plus a few more.
RANDOM_ISRC_COUNTRIES = [
    *CHART_COUNTRIES,
    "RU",
    "SE",
    "NL",
    "CA",
    "AU",
    "PL",
    "UA",
    "MX",
    "AR",
]

RETRIES = 6  # backoff 5, 10, 20, 40, 60 s: MusicBrainz sheds load with 503s
MB_DELAY = 1.1
DEEZER_DELAY = 0.15
_last_call: dict[str, float] = {}

# Every response is cached on disk, so a crashed run resumes where it stopped
# (the random sample is seeded, so a rerun asks for the same URLs).
CACHE_PATH = Path(".cache/fetch_names.json")
_cache: dict[str, Any] = (
    json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
)
_unsaved = 0


def save_cache() -> None:
    global _unsaved
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False))
    _unsaved = 0


def get_json(url: str, delay: float) -> Any:
    """Cached ``fetch_json``; saves the cache every 50 new responses."""
    global _unsaved
    if url not in _cache:
        _cache[url] = fetch_json(url, delay)
        _unsaved += 1
        if _unsaved >= 50:
            save_cache()
    return _cache[url]


def fetch_json(url: str, delay: float) -> Any:
    """GET ``url`` as JSON, spacing calls to the same host by ``delay`` seconds."""
    host = urllib.parse.urlsplit(url).netloc
    wait = _last_call.get(host, 0.0) + delay - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                _last_call[host] = time.monotonic()
                return json.load(resp)
        except urllib.error.HTTPError as e:
            _last_call[host] = time.monotonic()
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503) or attempt == RETRIES - 1:
                raise
        except OSError:  # URLError, timeouts, dropped connections
            if attempt == RETRIES - 1:
                raise
        time.sleep(min(60, 5 * 2**attempt))
    return None


def deezer_track(ref: str | int) -> dict[str, Any] | None:
    """Deezer track by id or ``isrc:<ISRC>``, trimmed to the name fields."""
    t = get_json(f"{DEEZER}/track/{ref}", DEEZER_DELAY)
    if not t or "error" in t or not t.get("isrc"):
        return None
    return {
        "id": t["id"],
        "isrc": t["isrc"],
        "title": t.get("title"),
        "title_short": t.get("title_short"),
        "title_version": t.get("title_version"),
        "artist": t.get("artist", {}).get("name"),
        "contributors": [
            {"name": c.get("name"), "role": c.get("role")}
            for c in t.get("contributors", [])
        ],
        "album": t.get("album", {}).get("title"),
        "duration": t.get("duration"),
    }


def mb_recordings(isrc: str) -> list[dict[str, Any]]:
    """Every MusicBrainz recording carrying ``isrc``, trimmed to the name fields."""
    # The /isrc lookup ignores inc=releases; the search endpoint returns them.
    q = urllib.parse.urlencode({"query": f"isrc:{isrc}", "limit": 100, "fmt": "json"})
    data = get_json(f"{MUSICBRAINZ}/recording?{q}", MB_DELAY)
    out = []
    for r in (data or {}).get("recordings", []):
        if isrc not in r.get("isrcs", []):
            continue
        out.append(
            {
                "id": r["id"],
                "title": r.get("title"),
                "disambiguation": r.get("disambiguation", ""),
                "length": r.get("length"),
                "artist_credit": [
                    {
                        "name": ac.get("name"),
                        "joinphrase": ac.get("joinphrase", ""),
                        "artist": ac.get("artist", {}).get("name"),
                    }
                    for ac in r.get("artist-credit", [])
                ],
                "releases": [
                    {
                        "title": rel.get("title"),
                        "disambiguation": rel.get("disambiguation", ""),
                        "status": rel.get("status"),
                        "date": rel.get("date"),
                        "country": rel.get("country"),
                    }
                    for rel in r.get("releases", [])
                ],
            }
        )
    return out


def lb_metadata(mbids: list[str]) -> dict[str, dict[str, Any]]:
    """ListenBrainz metadata per recording MBID, trimmed to the name fields."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(mbids), 25):
        q = urllib.parse.urlencode(
            {"recording_mbids": ",".join(mbids[i : i + 25]), "inc": "artist release"}
        )
        data = get_json(f"{LISTENBRAINZ}/metadata/recording/?{q}", 0.5) or {}
        for mbid, m in data.items():
            artist = m.get("artist", {})
            out[mbid] = {
                "title": m.get("recording", {}).get("name"),
                "artist_credit": artist.get("name"),
                "artists": [
                    {"name": a.get("name"), "join_phrase": a.get("join_phrase", "")}
                    for a in artist.get("artists", [])
                ],
                "release": m.get("release", {}).get("name"),
            }
    return out


def chart_playlists() -> dict[str, int]:
    data = get_json(
        f"{DEEZER}/user/{DEEZER_CHARTS_USER}/playlists?limit=500", DEEZER_DELAY
    )
    by_title = {p["title"]: p["id"] for p in data["data"]}
    missing = [t for t in CHART_COUNTRIES.values() if t not in by_title]
    if missing:
        sys.exit(f"Deezer chart playlists not found: {missing}")
    return {cc: by_title[title] for cc, title in CHART_COUNTRIES.items()}


def add_song(
    songs: dict[str, dict[str, Any]],
    drops: Counter[str],
    origin: str,
    dz: dict[str, Any] | None,
) -> bool:
    """Look ``dz`` up on MusicBrainz by ISRC and keep it if found. True if kept."""
    if dz is None:
        drops[f"{origin}: no deezer track/isrc"] += 1
        return False
    if dz["isrc"] in songs:
        drops[f"{origin}: duplicate isrc"] += 1
        return False
    try:
        recs = mb_recordings(dz["isrc"])
    except OSError:  # URLError, timeouts, dropped connections
        drops[f"{origin}: musicbrainz unreachable after retries"] += 1
        return False
    if not recs:
        drops[f"{origin}: isrc not on musicbrainz"] += 1
        return False
    songs[dz["isrc"]] = {
        "isrc": dz["isrc"],
        "origin": origin,
        "deezer": dz,
        "musicbrainz": recs,
    }
    return True


def random_bucket(
    songs: dict[str, dict[str, Any]],
    drops: Counter[str],
    kept: Counter[str],
    rng: random.Random,
    origin: str,
    countries: list[str],
    target: int,
) -> None:
    """Keep ``target`` random MusicBrainz recordings with an ISRC from ``countries``."""
    tries = 0
    while kept[origin] < target and tries < target * 4:
        tries += 1
        pattern = f"isrc:{rng.choice(countries)}???{rng.randint(0, 25):02d}*"
        q = urllib.parse.urlencode({"query": pattern, "limit": 1, "fmt": "json"})
        count = (get_json(f"{MUSICBRAINZ}/recording?{q}", MB_DELAY) or {}).get(
            "count", 0
        )
        if not count:
            drops[f"{origin}: empty isrc pattern"] += 1
            continue
        # 25 hits per search, up to 5 tried: one random search feeds several
        # songs instead of one, which matters at MusicBrainz's 1 req/s.
        q = urllib.parse.urlencode(
            {
                "query": pattern,
                "limit": 25,
                "offset": rng.randrange(max(1, min(count, 500) - 24)),
                "fmt": "json",
            }
        )
        recs = (get_json(f"{MUSICBRAINZ}/recording?{q}", MB_DELAY) or {}).get(
            "recordings", []
        )
        hits = [r["isrcs"][0] for r in recs if r.get("isrcs")]
        if not hits:
            drops[f"{origin}: no isrc on hit"] += 1
            continue
        for isrc in rng.sample(hits, min(5, len(hits))):
            if kept[origin] >= target:
                break
            if add_song(songs, drops, origin, deezer_track(f"isrc:{isrc}")):
                kept[origin] += 1
    print(f"{origin}: kept {kept[origin]} in {tries} tries", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--per-country", type=int, default=20, help="songs kept per chart country"
    )
    ap.add_argument(
        "--random", type=int, default=100, help="random MusicBrainz songs kept"
    )
    ap.add_argument(
        "--seed", type=int, default=69, help="random seed for the MusicBrainz sample"
    )
    ap.add_argument(
        "--random-ru",
        type=int,
        default=20,
        help="random MusicBrainz RU-ISRC songs kept",
    )
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    songs: dict[str, dict[str, Any]] = {}
    drops: Counter[str] = Counter()
    kept: Counter[str] = Counter()

    for cc, pid in chart_playlists().items():
        origin = f"chart:{cc}"
        tracks = get_json(f"{DEEZER}/playlist/{pid}/tracks?limit=100", DEEZER_DELAY)[
            "data"
        ]
        for t in tracks:
            if kept[origin] >= args.per_country:
                break
            if add_song(songs, drops, origin, deezer_track(t["id"])):
                kept[origin] += 1
        print(f"{origin}: kept {kept[origin]}", file=sys.stderr)

    for origin, countries, target in (
        ("mb_random", RANDOM_ISRC_COUNTRIES, args.random),
        ("mb_random:RU", ["RU"], args.random_ru),
    ):
        random_bucket(songs, drops, kept, rng, origin, countries, target)

    mbids = sorted({r["id"] for s in songs.values() for r in s["musicbrainz"]})
    lb = lb_metadata(mbids)
    for s in songs.values():
        s["listenbrainz"] = {
            r["id"]: lb[r["id"]] for r in s["musicbrainz"] if r["id"] in lb
        }
        if not s["listenbrainz"]:
            drops["no listenbrainz metadata (kept)"] += 1

    meta = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "args": {
            "per_country": args.per_country,
            "random": args.random,
            "random_ru": args.random_ru,
            "seed": args.seed,
        },
        "kept": dict(sorted(kept.items())),
        "dropped": dict(sorted(drops.items())),
        "songs": len(songs),
        "musicbrainz_recordings": len(mbids),
        "listenbrainz_found": len(lb),
    }
    save_cache()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"meta": meta, "songs": list(songs.values())}, ensure_ascii=False, indent=1
        )
        + "\n"
    )
    print(json.dumps(meta, indent=1), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    finally:
        save_cache()
