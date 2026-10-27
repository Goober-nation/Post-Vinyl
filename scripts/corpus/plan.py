"""Turn the search test v2 seeds into the harvest's query plan (#75).

Writes a self-contained ``plan.json`` that the standalone harvester (#74)
consumes on the server. Shapes, decided 2026-10-27 (#75):

- ``L0``  longest title word + longest artist word; when the title has a
          bracket qualifier ("(Remix)", "(Live)"), it goes in between
- ``L1``  2nd-longest title word + longest artist word (the next rung)
- ``T``   full title          ``TA``  full title + main artist
- ``B``   full album (skewed for tracks, recorded for the data)
- ``F:T`` the title accent-folded and symbol-mapped, only where that
          changes it: tells us whether accents need more investigation

Word picking keeps the original script and accents (milestone 1): words are
split on non-word characters in any script, stop words and version
qualifiers are dropped, and a word needs 3+ letters in Latin script, 2+ in
others ("안녕"). Names are the seeds' query names (original script, main
artist); there is no Deezer-name variant (the MusicBrainz name already did
better on slskd).

Every query is run: no ladder, no early stop. Identical query strings are
searched once and carry every (seed, shape) that produced them.

    python -m scripts.corpus.plan
    python -m scripts.corpus.plan --sample 12 --out .cache/plan_test.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

SEEDS_PATH = Path("tests/fixtures/search_test_v2/seeds.json")
OUT_PATH = Path("tests/fixtures/search_test_v2/plan.json")

# Ported from legacy/app/services/query_builder.py.
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "to",
        "at",
        "by",
        "is",
        "or",
        "for",
        "with",
        "vs",
        "and",
        "feat",
        "ft",
        "featuring",
        "presents",
        "intro",
        "produced",
        "prod",
        "mixed",
        "w",
        "remix",
        "rmx",
        "edit",
        "edition",
        "version",
        "original",
        "extended",
        "mix",
        "radio",
        "instrumental",
        "inst",
        "acoustic",
        "acoust",
        "live",
        "demo",
        "explicit",
        "clean",
        "bonus",
        "alternate",
        "alt",
        "cover",
        "single",
        "album",
        "ep",
        "lp",
        "deluxe",
        "special",
        "limited",
        "compilation",
        "comp",
        "collection",
        "anthology",
        "greatest",
        "hits",
        "best",
        "release",
        "reissue",
        "remastered",
        "remaster",
        "anniversary",
        "complete",
        "ultimate",
        "essential",
        "definitive",
        "part",
        "pt",
        "vol",
        "volume",
        "disc",
        "disk",
        "cd",
        "vinyl",
        "digital",
        "side",
        "chapter",
        "act",
        "scene",
        "ost",
        "soundtrack",
        "score",
        "theme",
        "opening",
        "ending",
        "background",
        "bgm",
        "insert",
        "character",
        "image",
        "track",
        "unknown",
        "various",
        "artist",
        "music",
        "song",
        "audio",
        "file",
        "type",
        "beat",
        "beats",
        "bpm",
        "kbps",
        "hz",
        "khz",
        "mp3",
        "flac",
        "m4a",
        "wav",
        "ogg",
        "aac",
        "wma",
        "lossless",
        "hires",
        "hd",
        "master",
        "stereo",
        "mono",
    ]
)
# Version qualifiers that can go into L0. Multi-word ones only match filenames
# later (the labeller), so they're left out here.
QUALIFIERS = [
    "instrumental",
    "acoustic",
    "remastered",
    "nightcore",
    "extended",
    "remaster",
    "acapella",
    "karaoke",
    "bootleg",
    "tribute",
    "slowed",
    "mashup",
    "remix",
    "radio",
    "cover",
    "demo",
    "edit",
    "live",
    "inst",
    "loop",
    "flip",
    "rmx",
]

_FEAT_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\b", re.IGNORECASE)
_BRACKET_RE = re.compile(r"[(\[]([^)\]]*)[)\]]")
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
# Soulseek peers write "$uicideboy$" as "suicideboys": map symbols to the
# letter they stand for rather than deleting them.
_SYMBOL_MAP = str.maketrans({"$": "s", "@": "a", "€": "e", "£": "l"})


def is_latin(word: str) -> bool:
    return all(unicodedata.name(c, "").startswith("LATIN") or c.isdigit() for c in word)


def select_words(text: str) -> list[str]:
    """Query words of a field, longest first, in the field's own script.

    The feat. clause and bracket contents are left out; stop words, version
    qualifiers and digit-only words are dropped; a Latin word needs 3+
    letters, a word in another script 2+.
    """
    text = _BRACKET_RE.sub(" ", _FEAT_RE.split(text or "", maxsplit=1)[0])
    out: list[str] = []
    for word in _WORD_RE.findall(unicodedata.normalize("NFC", text)):
        low = word.casefold()
        if low in STOP_WORDS or word.isdigit() or low in out:
            continue
        if len(word) < (3 if is_latin(word) else 2):
            continue
        out.append(low)
    return sorted(out, key=len, reverse=True)


def bracket_qualifier(title: str) -> str | None:
    """First version qualifier inside the title's brackets ("remix")."""
    for group in _BRACKET_RE.findall(title or ""):
        words = {w.casefold() for w in _WORD_RE.findall(group)}
        for q in QUALIFIERS:
            if q in words:
                return q
    return None


def ladder(title: str, artist: str) -> list[str]:
    """L0 and L1: title word × artist word, plus the qualifier in L0."""
    tw, aw = select_words(title), select_words(artist)
    rungs: list[list[str]] = []
    if tw:
        qualifier = bracket_qualifier(title)
        rungs.append([tw[0], *([qualifier] if qualifier else []), *aw[:1]])
        if len(tw) > 1:
            rungs.append([tw[1], *aw[:1]])
    elif aw:
        rungs.append(aw[:1])  # a title with no usable word: the artist word alone
    return [" ".join(dict.fromkeys(r)) for r in rungs]


def strip_punct(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']+", " ", text).split())


def fold_query(text: str) -> str:
    """Symbol-mapped, accent-folded form of a field (shape F)."""
    text = unicodedata.normalize("NFKD", text.translate(_SYMBOL_MAP))
    # Strip a combining mark only from a Latin base letter: elsewhere the
    # mark is part of the letter (サディ ≠ サティ, Рыжий ≠ Рыжии; live
    # 2026-10-04, both returned 0 peers).
    out: list[str] = []
    for c in text:
        if unicodedata.combining(c) and out and out[-1].isascii():
            continue
        out.append(c)
    return strip_punct(unicodedata.normalize("NFC", "".join(out)))


def seed_queries(seed: dict[str, Any]) -> list[tuple[str, str]]:
    """All (shape, query) pairs for one seed, in a stable order."""
    title, artist, album = seed["track"], seed["artist"], seed.get("album") or ""
    out: list[tuple[str, str]] = []

    def add(shape: str, query: str) -> None:
        query = " ".join(query.split())
        if query and (shape, query) not in out:
            out.append((shape, query))

    for i, q in enumerate(ladder(title, artist)):
        add(f"L{i}", q)
    add("T", title)
    folded = fold_query(title)
    # Soulseek ignores case and punctuation, so only accents and symbols count.
    if folded.casefold() != strip_punct(title).casefold():
        add("F:T", folded)
    add("TA", f"{title} {artist}")
    if album:
        add("B", album)
    return out


def sample_seeds(seeds: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Up to ``n`` seeds, one per bucket in turn, deterministic."""
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in seeds:
        by_bucket[s["bucket"]].append(s)
    picked: list[dict[str, Any]] = []
    while len(picked) < n and any(by_bucket.values()):
        for bucket in list(by_bucket):
            if by_bucket[bucket] and len(picked) < n:
                picked.append(by_bucket[bucket].pop(0))
    return picked


def build_plan(seeds: list[dict[str, Any]]) -> dict[str, Any]:
    queries: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        for shape, query in seed_queries(seed):
            entry = queries.setdefault(
                query,
                {
                    "qid": hashlib.sha1(query.encode()).hexdigest()[:12],
                    "query": query,
                    "uses": [],
                },
            )
            use = {"seed": seed["id"], "shape": shape}
            if use not in entry["uses"]:
                entry["uses"].append(use)
    return {"seeds": seeds, "queries": list(queries.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--sample", type=int, default=None, help="only N seeds")
    args = ap.parse_args(argv)

    data = json.loads(args.seeds.read_text())
    seeds = data["seeds"] + data["traps"]
    if args.sample:
        seeds = sample_seeds(seeds, args.sample)
    plan = build_plan(seeds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    uses = sum(len(q["uses"]) for q in plan["queries"])
    print(
        f"{len(seeds)} seeds → {uses} (seed, shape) pairs → "
        f"{len(plan['queries'])} unique queries → {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
