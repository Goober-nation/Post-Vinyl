"""Draft the search test v2 seed list from the normalization snapshot (#73).

Picks ~100 seeds and ~20 traps from ``tests/fixtures/normalization/songs.json``
(#69); nothing new is fetched (user, 2026-10-27). Seeds are stratified, and a
song goes to the first bucket it fits, in this order:

- ``cyrillic``, ``other_script``  every one in the snapshot (few of them);
                            other_script = CJK, Hangul, kana, Hebrew, Arabic, Greek
- ``multi_artist``          more than one credited artist (feat., &, with)
- ``version``               Deezer has a version label (title_version)
- ``accented``              Latin with accents
- ``chart`` / ``long_tail`` plain Latin, Deezer chart hit vs random MusicBrainz

Query names come from ``normalize.query_names`` on the ListenBrainz names
(MusicBrainz when ListenBrainz has none); the Deezer name is kept alongside
when it differs, for the name-source variant (#75).

Traps (the right answer is "no match"), all built from snapshot names:

- ``wrong_artist``  a real title with another seed's artist
- ``wrong_length``  a real song with its length moved by 90 s, so only the
                    length rule can reject the files
- ``title_mix``     the first half of one title + the second half of another,
                    same artist

Approved by the user 2026-10-27, after every trap was checked against
MusicBrainz (no recording with that title by that artist; no version near a
shifted length). Rerunning overwrites the file with the same list.

    python -m scripts.corpus.seeds_v2
"""

from __future__ import annotations

import argparse
import json
import random
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from app.core import normalize as n

SNAPSHOT = Path("tests/fixtures/normalization/songs.json")
OUT = Path("tests/fixtures/search_test_v2/seeds.json")

# Target per bucket; cyrillic and other_script take everything there is.
MAX_PER_ARTIST = 2
QUOTAS = {
    "cyrillic": 99,
    "other_script": 99,
    "multi_artist": 14,
    "version": 8,
    "accented": 16,
    "chart": 15,
    "long_tail": 15,
}
TRAPS = {"wrong_artist": 10, "wrong_length": 5, "title_mix": 5}
LENGTH_SHIFT_S = 90


def scripts_in(text: str) -> set[str]:
    return {unicodedata.name(c, "?").split()[0] for c in text if c.isalpha()}


def names(song: dict[str, Any]) -> dict[str, Any]:
    """Query names from ListenBrainz (MusicBrainz fallback), plus Deezer's."""
    mb = song["musicbrainz"][0]
    lb = song["listenbrainz"].get(mb["id"])
    if lb:
        credits = n.credits_from_joined(
            (a["name"], a["join_phrase"]) for a in lb["artists"]
        ) or [n.Credit(lb["artist_credit"] or "")]
        q = n.query_names(lb["title"], credits, lb["release"], lb["artist_credit"])
        source = "listenbrainz"
    else:
        credits = n.credits_from_joined(
            (a["name"], a["joinphrase"]) for a in mb["artist_credit"]
        )
        credit = "".join(a["name"] + a["joinphrase"] for a in mb["artist_credit"])
        album = mb["releases"][0]["title"] if mb["releases"] else None
        q = n.query_names(mb["title"], credits, album, credit)
        source = "musicbrainz"
    dz = song["deezer"]
    dz_credits = [
        n.Credit(c["name"], c.get("role") != "Main") for c in dz["contributors"]
    ] or [n.Credit(dz["artist"])]
    dz_q = n.query_names(dz["title"], dz_credits, dz["album"])
    length = mb["length"] / 1000 if mb.get("length") else dz.get("duration")
    out: dict[str, Any] = {
        "track": q.track,
        "artist": q.artist,
        "album": q.album,
        "credit": q.credit,
        "names_from": source,
        "length_s": round(length) if length else None,
        "n_credits": len(credits),
    }
    deezer = {"track": dz_q.track, "artist": dz_q.artist, "album": dz_q.album}
    if any(deezer[k].casefold() != out[k].casefold() for k in deezer):
        out["deezer"] = deezer
    return out


def bucket_of(song: dict[str, Any], nm: dict[str, Any]) -> str:
    text = nm["track"] + nm["artist"]
    sc = scripts_in(text)
    if "CYRILLIC" in sc:
        return "cyrillic"
    if sc & {"CJK", "HANGUL", "HIRAGANA", "KATAKANA", "HEBREW", "ARABIC", "GREEK"}:
        return "other_script"
    if nm["n_credits"] > 1:
        return "multi_artist"
    if song["deezer"].get("title_version"):
        return "version"
    if any(ord(c) > 127 for c in text if c.isalpha()):
        return "accented"
    return "chart" if song["origin"].startswith("chart") else "long_tail"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=73)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    songs = json.loads(SNAPSHOT.read_text())["songs"]
    pool: dict[str, list[dict[str, Any]]] = {b: [] for b in QUOTAS}
    for song in songs:
        nm = names(song)
        if not nm["track"] or not nm["artist"] or not nm["length_s"]:
            continue
        nm |= {"id": song["isrc"], "isrc": song["isrc"], "origin": song["origin"]}
        nm["mbid"] = song["musicbrainz"][0]["id"]
        pool[bucket_of(song, nm)].append(nm)

    seeds: list[dict[str, Any]] = []
    per_artist: Counter[str] = Counter()
    for bucket, quota in QUOTAS.items():
        candidates = rng.sample(pool[bucket], len(pool[bucket]))
        picked: list[dict[str, Any]] = []
        for s in candidates:
            if len(picked) < quota and per_artist[s["artist"]] < MAX_PER_ARTIST:
                per_artist[s["artist"]] += 1
                picked.append(s)
        seeds += [s | {"bucket": bucket, "expect": "match"} for s in picked]
    seeds.sort(key=lambda s: (list(QUOTAS).index(s["bucket"]), s["artist"]))

    # Traps come from songs that aren't seeds, so no seed's answer changes.
    seed_ids = {s["id"] for s in seeds}
    spare = [s for b in pool.values() for s in b if s["id"] not in seed_ids]
    spare = [s for s in spare if len(s["track"].split()) >= 2]
    rng.shuffle(spare)
    traps: list[dict[str, Any]] = []
    for i in range(TRAPS["wrong_artist"]):
        song, other = spare.pop(), spare.pop()
        traps.append(
            {
                "id": f"trap-artist-{i:02d}",
                "bucket": "wrong_artist",
                "expect": "none",
                "track": song["track"],
                "artist": other["artist"],
                "album": "",
                "length_s": song["length_s"],
                "built_from": [song["isrc"], other["isrc"]],
            }
        )
    for i in range(TRAPS["wrong_length"]):
        song = spare.pop()
        traps.append(
            {
                "id": f"trap-length-{i:02d}",
                "bucket": "wrong_length",
                "expect": "none",
                "track": song["track"],
                "artist": song["artist"],
                "album": song["album"],
                "length_s": song["length_s"] + LENGTH_SHIFT_S,
                "built_from": [song["isrc"]],
            }
        )
    by_artist: dict[str, list[dict[str, Any]]] = {}
    for s in spare + seeds:
        by_artist.setdefault(s["artist"], []).append(s)
    pairs = [v[:2] for v in by_artist.values() if len(v) >= 2]
    rng.shuffle(pairs)
    for i, (a, b) in enumerate(pairs[: TRAPS["title_mix"]]):
        wa, wb = a["track"].split(), b["track"].split()
        mixed = " ".join(wa[: max(1, len(wa) // 2)] + wb[len(wb) // 2 :])
        traps.append(
            {
                "id": f"trap-mix-{i:02d}",
                "bucket": "title_mix",
                "expect": "none",
                "track": mixed,
                "artist": a["artist"],
                "album": "",
                "length_s": a["length_s"],
                "built_from": [a["isrc"], b["isrc"]],
            }
        )

    for s in seeds:
        del s["n_credits"]
    counts = Counter(s["bucket"] for s in seeds + traps)
    meta = {
        "approved": "2026-10-27",
        "trap_check": (
            "MusicBrainz: no recording with the trap's title by its artist; "
            "wrong_length traps have no version within 3 s of the shifted length"
        ),
        "source": str(SNAPSHOT),
        "random_seed": args.seed,
        "buckets": dict(counts),
        "seeds": len(seeds),
        "traps": len(traps),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"meta": meta, "seeds": seeds, "traps": traps}, ensure_ascii=False, indent=1
        )
        + "\n"
    )
    print(json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
