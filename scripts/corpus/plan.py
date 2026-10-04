"""Turn the seed list into the harvest's query plan (#34).

Runs locally (it imports the app's query builder for the L shape) and writes
a self-contained ``plan.json`` that the standalone harvester consumes on the
server. Shapes, decided 2026-10-03 (see #34):

track / negative seeds
  L   current longest-word rungs (`build_search_queries`), each its own query
  W2  two longest title words; one title word + one album word when the
      title has only one usable word
  T   full title        A  full artist        B  full album
  D   each delimiter-separated part of the title
  F   symbol-mapped/accent-folded T, A, B (only where that changes the text)
album seeds
  B, A, D (of the album), LW (longest album word + longest artist word),
  W2 (two longest album words, else album word + artist word), F of B/A
artist seeds
  A, F of A

Every query is run — there is no ladder and no early stop. Identical query
strings are searched once and carry every (seed, shape) that produced them.

    python -m scripts.corpus.plan --out plan.json
    python -m scripts.corpus.plan --out plan_test.json --sample 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import tomllib

from app.services.query_builder import build_search_queries, select_words

SEEDS_PATH = Path("tests/fixtures/corpus/seeds.toml")

# Soulseek peers write "$uicideboy$" as "suicideboys": map symbols to the
# letter they stand for rather than deleting them.
_SYMBOL_MAP = str.maketrans({"$": "s", "@": "a", "€": "e", "£": "l"})

_DELIM_RE = re.compile(
    r"\s+[-–—]\s+|[()\[\]{}/:,&+]|\s(?:feat\.?|ft\.?|featuring|with|vs\.?|x)\s",
    re.IGNORECASE,
)


def seed_id(seed: dict) -> str:
    key = "|".join(seed.get(k, "") for k in ("type", "artist", "title", "album"))
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def fold_query(text: str) -> str:
    """Symbol-mapped, accent-folded form of a field (shape F)."""
    text = text.translate(_SYMBOL_MAP)
    text = unicodedata.normalize("NFKD", text)
    # Strip a combining mark only from a Latin base letter. Elsewhere the
    # mark is part of the letter: dropping it turned サディ into サティ and
    # Рыжий into Рыжии (live 2026-10-04: both returned 0 peers).
    out: list[str] = []
    for c in text:
        if unicodedata.combining(c) and out and out[-1].isascii():
            continue
        out.append(c)
    text = unicodedata.normalize("NFC", "".join(out))
    text = re.sub(r"[^\w\s']+", " ", text)
    return " ".join(text.split())


def delimiter_parts(text: str) -> list[str]:
    """Delimiter-separated parts of a field (shape D); [] when there are none."""
    parts = [" ".join(p.split()) for p in _DELIM_RE.split(f" {text} ")]
    parts = [p for p in parts if len(p) > 2]
    return parts if len(parts) > 1 else []


def two_longest(primary: str, secondary: str) -> str:
    """Shape W2: two longest words of `primary`; with only one usable word,
    that word plus the longest word of `secondary`."""
    words = select_words(primary)
    if len(words) >= 2:
        return " ".join(words[:2])
    picked = words[:1] + [w for w in select_words(secondary)[:1] if w not in words]
    return " ".join(picked)


def seed_queries(seed: dict) -> list[tuple[str, str]]:
    """All (shape, query) pairs for one seed, in a stable order."""
    artist = seed.get("artist", "")
    title = seed.get("title", "")
    album = seed.get("album", "")
    out: list[tuple[str, str]] = []

    def add(shape: str, query: str) -> None:
        query = " ".join(query.split())
        if query:
            out.append((shape, query))

    def add_folded(shape: str, query: str) -> None:
        folded = fold_query(query)
        if folded and folded != query:
            add(f"F:{shape}", folded)

    kind = seed["type"]
    if kind == "track":
        for i, q in enumerate(build_search_queries(title, artist)):
            add(f"L{i}", q)
        add("W2", two_longest(title, album))
        # LW: album word + artist word, filtered by title afterwards. A
        # single (or a track with no album) is its own album (user,
        # 2026-10-04). AT drops the artist for artists whose name is on
        # Soulseek's excluded-phrase list (Gorillaz, Beyoncé…).
        album_or_title = album or title
        lw = select_words(album_or_title)[:1] + select_words(artist)[:1]
        add("LW", " ".join(dict.fromkeys(lw)))
        at = (
            select_words(album_or_title)[:1]
            + [
                w
                for w in select_words(title)
                if w not in select_words(album_or_title)[:1]
            ][:1]
        )
        add("AT", " ".join(at))
        add("T", title)
        add("A", artist)
        if album:
            add("B", album)
        for i, part in enumerate(delimiter_parts(title)):
            add(f"D{i}", part)
        add_folded("T", title)
        add_folded("A", artist)
        if album:
            add_folded("B", album)
    elif kind == "album":
        add("B", album)
        add("A", artist)
        for i, part in enumerate(delimiter_parts(album)):
            add(f"D{i}", part)
        lw = select_words(album)[:1] + select_words(artist)[:1]
        add("LW", " ".join(dict.fromkeys(lw)))
        add("W2", two_longest(album, artist))
        add_folded("B", album)
        add_folded("A", artist)
    elif kind == "artist":
        add("A", artist)
        add_folded("A", artist)
    else:
        raise ValueError(f"unknown seed type {kind!r}")
    return out


def sample_seeds(seeds: list[dict], n: int) -> list[dict]:
    """Up to `n` track seeds, one per bucket first, deterministic."""
    tracks = [s for s in seeds if s["type"] == "track"]
    picked: list[dict] = []
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in tracks:
        by_bucket[s["bucket"]].append(s)
    while len(picked) < n and any(by_bucket.values()):
        for bucket in list(by_bucket):
            if by_bucket[bucket] and len(picked) < n:
                picked.append(by_bucket[bucket].pop(0))
    return picked


def build_plan(seeds: list[dict]) -> dict:
    queries: dict[str, dict] = {}
    seed_rows = []
    for seed in seeds:
        sid = seed_id(seed)
        seed_rows.append({"id": sid, **seed})
        for shape, query in seed_queries(seed):
            entry = queries.setdefault(
                query,
                {
                    "qid": hashlib.sha1(query.encode()).hexdigest()[:12],
                    "query": query,
                    "uses": [],
                },
            )
            use = {"seed": sid, "shape": shape}
            if use not in entry["uses"]:
                entry["uses"].append(use)
    return {"seeds": seed_rows, "queries": list(queries.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=None, help="only N track seeds")
    ap.add_argument("--types", default=None, help="seed types to keep, e.g. track")
    ap.add_argument(
        "--shapes", default=None, help="comma-separated shapes to keep, e.g. LW,AT"
    )
    args = ap.parse_args(argv)

    seeds = tomllib.loads(args.seeds.read_text())["seed"]
    if args.types:
        seeds = [s for s in seeds if s["type"] in args.types.split(",")]
    if args.sample:
        seeds = sample_seeds(seeds, args.sample)
    plan = build_plan(seeds)
    if args.shapes:
        keep = set(args.shapes.split(","))
        # "L" keeps every ladder rung (L0, L1, …).
        for q in plan["queries"]:
            q["uses"] = [
                u
                for u in q["uses"]
                if u["shape"] in keep
                or ("L" in keep and u["shape"][0] == "L" and u["shape"][1:].isdigit())
            ]
        plan["queries"] = [q for q in plan["queries"] if q["uses"]]
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    uses = sum(len(q["uses"]) for q in plan["queries"])
    print(
        f"{len(plan['seeds'])} seeds → {uses} (seed, shape) pairs → "
        f"{len(plan['queries'])} unique queries → {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
