"""Label harvested search responses and score query shapes by helpful peers (#34, #76).

Offline: reads ``<corpus>/raw/*.json.gz`` + ``<corpus>/seeds.json`` from a
harvest, never touches slskd. The unit of success is a **helpful peer** — a
peer with at least one correct, downloadable file — not a file count: one
peer is all a download needs, and 40 duplicates on one peer are worth one.

A file is **correct** for a track seed when (decided 2026-10-04, #34):

1. it is an audio file in the peer's unlocked ``files`` (locked files can't
   be downloaded and are only counted),
2. no excluded version word appears in its filename or parent folder unless
   the seed's own title/album contains it (the AGENTS.md list: cover, live,
   acoustic, a cappella, mix/remix, extended, solo, reference, home session,
   soundtrack/OST, ambient, edit, karaoke, nightcore, sped up, slowed,
   8d audio, mashup, bootleg, tribute, "in the style of", instrumental,
   demo); "Original Mix" is the normal version, not a mix,
3. the title is an in-order phrase in the **filename** (remaster/deluxe
   suffixes ignored, they're the same song) and the main artist is somewhere
   in the path,
4. its length is within ±3 s of the MusicBrainz length. A file with no
   reported length is ``no_length``, not correct: its peer counts as
   *unverified* in its own column (user, 2026-10-27).

Both sides are folded the same way before comparing: case, accents on Latin
letters only (Cyrillic й and kana voicing marks stay), punctuation, "&" as
"and" (data: ``docs/research/slskd-matching.md``). Seeds are the search test
v2 seeds (``track``/``length_s``) or the old ones (``title``/``duration``).

Album seeds: a peer is helpful when one folder holds the album — the folder
path carries the album and artist words, has no excluded word, and holds at
least 80% of the MusicBrainz track count in audio files. Artist seeds: any
audio file with the artist in its path. Negative seeds use the track rule;
any helpful peer there is a false positive.

Per query it also counts helpful peers with a free upload slot and the
format mix of their best files; downloadability is scored only after the run.
The report holds counts only; the spot-check (peer names, paths) goes to a
local file, never committed (public repo).

    python -m scripts.corpus.label --corpus <dir> --report <report.md>
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXTS = {
    "flac", "mp3", "m4a", "aac", "ogg", "opus", "wav", "aiff", "aif",
    "alac", "ape", "wv", "wma",
}  # fmt: skip

# Matched as whole words against the normalised filename + parent folder.
# Multi-word entries are matched as phrases.
EXCLUDED_VERSIONS = (
    "cover", "covers", "covered", "live", "acoustic", "acapella", "acappella",
    "a cappella", "a capella", "mix", "mixes", "mixed", "remix", "remixes",
    "remixed", "rmx", "extended", "solo", "reference", "home session",
    "home sessions", "soundtrack", "ost", "ambient", "edit", "radio edit",
    "karaoke", "nightcore", "sped up", "slowed", "8d audio", "8d", "mashup",
    "bootleg", "tribute", "in the style of", "instrumental", "demo",
)  # fmt: skip
# The normal version, though it says "mix".
SAME_SONG_PHRASES = ("original mix",)

# Suffixes that don't change which song it is (decided 2026-10-03: a
# remaster/deluxe/year tag is the same song; live/remix/acoustic is not).
_SAME_SONG_RE = re.compile(
    r"\s*[\(\[\-–]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|deluxe(?: edition)?|"
    r"expanded(?: edition)?|bonus track|single version|album version|"
    r"collectors edition)(?:\s+\d{4})?\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
_APOS_RE = re.compile(r"['’ʼ`´]")
_ACRONYM_RE = re.compile(r"\b(?:\w\.){1,}\w\b\.?")
# Peers write "$uicideboy$" as "suicideboys"; same map as the query plan.
_SYMBOL_MAP = str.maketrans({"$": "s", "@": "a", "€": "e", "£": "l"})


def norm(text: str) -> str:
    """Compare-key: case-folded, accents off Latin letters only, punctuation
    and whitespace collapsed, "&" as "and", "$" as "s", dotted acronyms
    as one word (G.O.M.D → gomd). Non-Latin letters stay whole:
    NFKD on everything would turn Cyrillic й into и and split Hangul."""
    out = []
    for ch in unicodedata.normalize("NFC", text or "").casefold():
        base = unicodedata.normalize("NFD", ch)
        if len(base) > 1 and unicodedata.name(base[0], "").startswith("LATIN"):
            ch = "".join(c for c in base if not unicodedata.combining(c))
        out.append(ch)
    text = _APOS_RE.sub("", "".join(out)).replace("&", " and ")
    text = text.translate(_SYMBOL_MAP)
    # Dotted acronyms are one word: peers write G.O.M.D as "gomd".
    text = _ACRONYM_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def norm_title(text: str) -> str:
    prev = None
    while prev != text:
        prev, text = text, _SAME_SONG_RE.sub("", text)
    return norm(text)


def seed_title(seed: dict) -> str:
    return seed.get("track") or seed.get("title") or ""


def seed_length(seed: dict) -> float | None:
    return seed.get("length_s") or seed.get("duration")


DURATION_TOLERANCE_S = 3
ALBUM_COMPLETENESS = 0.8


def split_path(filename: str) -> tuple[list[str], str, str]:
    """Soulseek paths use backslashes; return (folders, basename, extension)."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else ""
    stem, dot, ext = base.rpartition(".")
    return parts[:-1], stem if dot else base, ext.lower() if dot else ""


# Version words in other languages, read as their English form on both sides,
# so "(ремикс)" in a seed matches "(remix)" in a file and Russian remixes are
# excluded like English ones (live 2026-10-27).
VERSION_SYNONYMS = {
    "ремикс": "remix", "ремиксы": "remixes", "кавер": "cover", "караоке": "karaoke",
    "акустика": "acoustic", "акустическая": "acoustic", "инструментал": "instrumental",
    "минусовка": "instrumental", "лайв": "live", "концерт": "live", "демо": "demo",
}  # fmt: skip


def _words(text: str) -> list[str]:
    return [VERSION_SYNONYMS.get(w, w) for w in norm(text).split()]


def _has_phrase(haystack: list[str], phrase: str) -> bool:
    needle = phrase.split()
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# "live" glued to the next word or a year: Livebox, LiveAt, Live1999. A plain
# prefix test would also hit liver/lively/olive, so the tail is whitelisted.
_JOINED_LIVE_RE = re.compile(r"^live(?:box|at|in|from|on|set|show|tour|\d{2,4})$")


def _version_hit(local: list[str], own: list[str]) -> str | None:
    text = " ".join(local)
    for phrase in SAME_SONG_PHRASES:
        text = f" {text} ".replace(f" {phrase} ", " ")
    local = text.split()
    for phrase in EXCLUDED_VERSIONS:
        if _has_phrase(local, phrase) and not _has_phrase(own, phrase):
            return phrase
    if "live" not in own and any(_JOINED_LIVE_RE.match(w) for w in local):
        return "live"
    return None


def excluded_word(filename: str, seed: dict) -> str | None:
    """The first excluded version word in the file's name or parent folder
    that the seed itself doesn't carry, else None. "Live" is also checked one
    folder higher: live albums often sit there ("Live At The Paramount\\
    Music\\10. Sliver.flac"). Other words one level up were mostly category
    folders ("Tagged with cover art", "OST\\Drive" holding the original
    recording), so they're not (measured 2026-10-27 on 4 harvests).
    """
    folders, stem, _ = split_path(filename)
    local = _words(stem) + (_words(folders[-1]) if folders else [])
    own = _words(seed_title(seed)) + _words(seed.get("album", ""))
    if hit := _version_hit(local, own):
        return hit
    above = _words(folders[-2]) if len(folders) > 1 else []
    return "live" if _version_hit(above, own) == "live" else None


def _contains_all(haystack: list[str], needle: list[str]) -> bool:
    pool = set(haystack)
    return bool(needle) and all(w in pool for w in needle)


_PAREN_RE = re.compile(r"[(\[{][^)\]}]*[)\]}]")


def title_matches(stem: str, title: str) -> bool:
    """The title's main part as an in-order phrase in the filename; words in
    its brackets anywhere in it.

    Loose words let "South City Midnight Lady" pass for "Midnight City"
    (live 2026-10-04, a false positive on a trap seed). Bracketed parts are
    checked loosely because peers move them around: "Old Town Road (Remix)"
    is often "Old Town Road (feat. Billy Ray Cyrus) [Remix]".
    """
    main = _words(norm_title(_PAREN_RE.sub(" ", title)))
    extra = _words(norm_title(" ".join(_PAREN_RE.findall(title))))
    words = _words(stem)
    if not main:  # title is all brackets
        return _contains_all(words, extra)
    return _has_phrase(words, " ".join(main)) and (
        not extra or _contains_all(words, extra)
    )


def artist_matches(folders: list[str], stem: str, artist: str) -> bool:
    """The artist as an in-order phrase in the path. A name of 2 characters
    or fewer ("xx", "X", "U2") must stand as its own segment — a whole
    folder, or bounded by separators — or "The xx" matches the "xx" in a
    date like "1975 09 xx Memphis" (live 2026-10-04)."""
    want = artist_words({"artist": artist})
    if not want:
        return False
    segments = folders + [stem]
    if len("".join(want)) > 2:
        return _has_phrase(_words(" ".join(segments)), " ".join(want))
    for seg in segments:
        for part in re.split(r"\s+[-–—]\s+|[_\\/]|^\d+[.)]?\s+", seg):
            if norm(part) == " ".join(want):
                return True
        if norm(seg) == " ".join(want):
            return True
    return False


def artist_words(seed: dict) -> list[str]:
    return _words(seed.get("artist", ""))


@dataclass
class FileLabel:
    label: (
        str  # correct | excluded:<word> | no_title | no_artist | duration | not_audio
    )
    filename: str
    detail: str = ""


def label_track_file(f: dict, seed: dict) -> FileLabel:
    name = f.get("filename", "")
    folders, stem, ext = split_path(name)
    if ext not in AUDIO_EXTS:
        return FileLabel("not_audio", name)
    if word := excluded_word(name, seed):
        return FileLabel(f"excluded:{word}", name)
    if not title_matches(stem, seed_title(seed)):
        return FileLabel("no_title", name)
    if not artist_matches(folders, stem, seed["artist"]):
        return FileLabel("no_artist", name)
    want, got = seed_length(seed), f.get("length")
    if want and got is None:
        # Can't be length-checked: every v2 trap false positive was such a
        # file (2026-10-27). Counted apart, never as correct (user decision).
        return FileLabel("no_length", name)
    if want and got and abs(int(got) - int(want)) > DURATION_TOLERANCE_S:
        return FileLabel("duration", name, f"{got}s vs {want}s")
    return FileLabel("correct", name)


def _quality(f: dict) -> dict:
    _, _, ext = split_path(f.get("filename", ""))
    return {
        "ext": ext,
        "bitrate": f.get("bitRate"),
        "sample_rate": f.get("sampleRate"),
        "bit_depth": f.get("bitDepth"),
        "size": f.get("size"),
    }


def album_folder_ok(folder: str, files: list[dict], seed: dict) -> bool:
    words = _words(folder)
    if not _contains_all(words, _words(seed["album"])):
        return False
    va = seed["artist"].casefold() == "various artists"
    if not va and not _contains_all(words, artist_words(seed)):
        return False
    own = _words(seed["album"])
    last = _words(folder.replace("\\", "/").rsplit("/", 1)[-1])
    if _version_hit(last, own):
        return False
    audio = [f for f in files if split_path(f["filename"])[2] in AUDIO_EXTS]
    need = max(1, round(ALBUM_COMPLETENESS * (seed.get("track_count") or 1)))
    return len(audio) >= need


@dataclass
class PeerLabel:
    username: str
    helpful: bool
    files: int
    locked: int
    free_slot: bool = False
    # Only no-length matches: the right name, length unknown.
    unverified: bool = False
    correct: list[str] = field(default_factory=list)
    best: dict | None = None
    reasons: dict[str, int] = field(default_factory=dict)


def label_peer(peer: dict, seed: dict) -> PeerLabel:
    files = peer.get("files") or []
    out = PeerLabel(
        username=peer.get("username", ""),
        helpful=False,
        files=len(files),
        locked=len(peer.get("lockedFiles") or []),
        free_slot=bool(peer.get("hasFreeUploadSlot")),
    )
    kind = seed.get("type", "track")
    if kind == "track":
        reasons: dict[str, int] = defaultdict(int)
        for f in files:
            fl = label_track_file(f, seed)
            reasons[fl.label.split(":")[0]] += 1
            if fl.label == "correct":
                out.correct.append(fl.filename)
                q = _quality(f)
                if out.best is None or _rank(q) > _rank(out.best):
                    out.best = q
        out.reasons = dict(reasons)
        out.helpful = bool(out.correct)
        out.unverified = not out.helpful and reasons.get("no_length", 0) > 0
    elif kind == "album":
        by_folder: dict[str, list[dict]] = defaultdict(list)
        for f in files:
            folder = f.get("filename", "").replace("\\", "/").rsplit("/", 1)[0]
            by_folder[folder].append(f)
        good = [d for d, fs in by_folder.items() if album_folder_ok(d, fs, seed)]
        out.correct = good
        out.helpful = bool(good)
    elif kind == "artist":
        want = artist_words(seed)
        for f in files:
            folders, stem, ext = split_path(f.get("filename", ""))
            if ext in AUDIO_EXTS and _contains_all(
                _words(" ".join(folders + [stem])), want
            ):
                out.correct.append(f["filename"])
        out.helpful = bool(out.correct)
    return out


def _rank(q: dict) -> tuple:
    lossless = q["ext"] in {"flac", "alac", "ape", "wv", "wav", "aiff", "aif"}
    return (lossless, q.get("bitrate") or 0, q.get("size") or 0)


def format_band(q: dict | None) -> str:
    """flac/lossless, mp3 by bitrate band, or other."""
    if not q:
        return "none"
    if q["ext"] in {"flac", "alac", "ape", "wv", "wav", "aiff", "aif"}:
        return "lossless"
    if q["ext"] == "mp3":
        br = q.get("bitrate") or 0
        return "mp3 320" if br >= 320 else "mp3 256-319" if br >= 256 else "mp3 <256"
    return "other"


def shape_family(shape: str) -> str:
    """L0/L1+/D/… — rungs past the first and delimiter parts are grouped."""
    if shape.startswith("L") and shape[1:].isdigit():
        return "L0" if shape == "L0" else "L1+"
    if shape.startswith("D") and shape[1:].isdigit():
        return "D"
    return shape


def score_record(record: dict, seeds: dict[str, dict]) -> list[dict]:
    rows = []
    responses = record.get("responses") or []
    for use in record["uses"]:
        seed = seeds[use["seed"]]
        peers = [label_peer(p, seed) for p in responses]
        helpful = [p for p in peers if p.helpful]
        rows.append(
            {
                "seed": use["seed"],
                "kind": "negative"
                if seed.get("expect") == "none"
                else seed.get("type", "track"),
                "bucket": seed.get("bucket", ""),
                "shape": use["shape"],
                "family": shape_family(use["shape"]),
                "query": record["query"],
                "peers": len(peers),
                "helpful_peers": len(helpful),
                "helpful_free_slot": sum(p.free_slot for p in helpful),
                "unverified_peers": sum(p.unverified for p in peers),
                "formats": dict(Counter(format_band(p.best) for p in helpful)),
                "files": sum(p.files for p in peers),
                "correct_files": sum(len(p.correct) for p in helpful),
                "stop_reason": record.get("stop_reason"),
                "best_quality": max(
                    (p.best for p in helpful if p.best), key=_rank, default=None
                ),
                "examples": [
                    {"user": p.username, "files": p.correct[:2]} for p in helpful[:3]
                ],
            }
        )
    return rows


def load(corpus: Path) -> tuple[dict[str, dict], list[dict]]:
    seeds = {s["id"]: s for s in json.loads((corpus / "seeds.json").read_text())}
    records = []
    for path in sorted((corpus / "raw").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            records.append(json.load(fh))
    return seeds, records


def merge_plan_uses(records: list[dict], plans: list[dict]) -> None:
    """Attach every (seed, shape) any plan assigns to a record's query.

    A query string searched once serves every shape that produces it, but
    the record only lists the shapes of the plan it was harvested under —
    an LW query already run as an album seed's LW must also score as the
    track seeds' LW (#34, 2026-10-04).
    """
    by_qid: dict[str, list[dict]] = defaultdict(list)
    for p in plans:
        for q in p["queries"]:
            by_qid[q["qid"]].extend(q["uses"])
    for rec in records:
        for use in by_qid.get(rec["qid"], []):
            if use not in rec["uses"]:
                rec["uses"].append(use)


def _stats(rs: list[dict]) -> dict:
    ratios = [r["helpful_peers"] / r["peers"] for r in rs if r["peers"]]
    return {
        "n": len(rs),
        "found_pct": 100 * sum(r["helpful_peers"] > 0 for r in rs) / len(rs),
        "found_incl_unverified_pct": 100
        * sum(r["helpful_peers"] + r.get("unverified_peers", 0) > 0 for r in rs)
        / len(rs),
        "median_helpful_peers": statistics.median(r["helpful_peers"] for r in rs),
        "mean_helpful_peers": statistics.mean(r["helpful_peers"] for r in rs),
        "median_free_slot": statistics.median(
            r.get("helpful_free_slot", 0) for r in rs
        ),
        "median_peers": statistics.median(r["peers"] for r in rs),
        "median_peer_precision_pct": 100 * statistics.median(ratios) if ratios else 0.0,
    }


def summarise(rows: list[dict]) -> dict[str, dict[str, dict]]:
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r["kind"]][r["family"]].append(r)
    return {
        kind: {fam: _stats(rs) for fam, rs in sorted(fams.items())}
        for kind, fams in by.items()
    }


def by_bucket(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Found % per (bucket, shape family) for match seeds."""
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["kind"] != "negative":
            by[r.get("bucket") or "-"][r["family"]].append(r)
    return {
        b: {
            f: 100 * sum(r["helpful_peers"] > 0 for r in rs) / len(rs)
            for f, rs in fams.items()
        }
        for b, fams in sorted(by.items())
    }


def render(summary: dict, rows: list[dict]) -> str:
    """Counts only: no peer names or paths (public repo)."""
    lines = [
        "# Search benchmark: helpful peers per query shape",
        "",
        "Generated by `python -m scripts.corpus.label`. A *helpful peer* has at",
        "least one correct, downloadable file (rules in `scripts/corpus/label.py`).",
        "*Found* = the query returned ≥1 helpful peer. *Peer precision* = helpful",
        "peers / all peers that answered. *Free slot* = helpful peers with a free",
        "upload slot. *Unverified length*: peers whose only match reports no length",
        "(counted apart, not helpful). Negative seeds (traps): any helpful peer is a",
        "false positive.",
        "",
    ]
    for kind in ("track", "album", "artist", "negative"):
        if kind not in summary:
            continue
        lines += [
            f"## {kind}",
            "",
            (
                "| Shape | n | Found | Found incl. unverified length "
                "| Helpful peers (median) | (mean) "
                "| Free slot (median) | Peers (median) | Peer precision |"
            ),
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for fam, st in summary[kind].items():
            lines.append(
                f"| {fam} | {st['n']} | {st['found_pct']:.0f}% "
                f"| {st['found_incl_unverified_pct']:.0f}% "
                f"| {st['median_helpful_peers']:.0f} | {st['mean_helpful_peers']:.0f} "
                f"| {st['median_free_slot']:.0f} | {st['median_peers']:.0f} "
                f"| {st['median_peer_precision_pct']:.1f}% |"
            )
        lines.append("")

    buckets = by_bucket(rows)
    fams = sorted({f for b in buckets.values() for f in b})
    if buckets:
        lines += [
            "## Found per seed bucket",
            "",
            "| Bucket | " + " | ".join(fams) + " |",
        ]
        lines.append("|---" * (len(fams) + 1) + "|")
        for b, row in buckets.items():
            cells = [f"{row[f]:.0f}%" if f in row else "–" for f in fams]
            lines.append(f"| {b} | " + " | ".join(cells) + " |")
        lines.append("")

    formats: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        if r["kind"] != "negative":
            formats[r["family"]].update(r.get("formats", {}))
    bands = ["lossless", "mp3 320", "mp3 256-319", "mp3 <256", "other"]
    lines += [
        "## Format of helpful peers' best file",
        "",
        "| Shape | " + " | ".join(bands) + " |",
        "|---" * (len(bands) + 1) + "|",
    ]
    for fam, c in sorted(formats.items()):
        total = sum(c[b] for b in bands) or 1
        lines.append(
            f"| {fam} | "
            + " | ".join(f"{100 * c[b] / total:.0f}%" for b in bands)
            + " |"
        )

    empty = sorted({r["query"] for r in rows if r["peers"] == 0})
    fps = [r for r in rows if r["kind"] == "negative" and r["helpful_peers"]]
    lines += [
        "",
        (
            f"Queries with no peers at all: {len(empty)} (likely Soulseek-excluded "
            "phrases or a cold network)."
        ),
        (
            f"Trap (seed, shape) pairs with a false positive: {len(fps)} of "
            f"{sum(r['kind'] == 'negative' for r in rows)}."
        ),
    ]
    return "\n".join(lines) + "\n"


def render_spot_check(rows: list[dict], seeds: dict[str, dict], sample: int) -> str:
    """Random verdicts both ways plus every trap false positive, with paths:
    for a human check, kept local (it names peers and their files)."""
    rng = random.Random(0)
    lines = [
        "# Labeller spot-check (local only)",
        "",
        "## Helpful verdicts (should all be right)",
        "",
    ]
    pos = [r for r in rows if r["helpful_peers"] and r["kind"] == "track"]
    for r in rng.sample(pos, min(sample, len(pos))):
        st = seeds[r["seed"]]
        lines.append(
            f"- `{r['shape']}` {st['artist']} – {seed_title(st)}: `{r['examples'][0]['files'][0]}`"
        )
    lines += ["", "## Zero helpful peers (check none were missed)", ""]
    neg = [
        r
        for r in rows
        if not r["helpful_peers"] and r["kind"] == "track" and r["peers"]
    ]
    for r in rng.sample(neg, min(sample, len(neg))):
        st = seeds[r["seed"]]
        lines.append(
            f"- `{r['shape']}` {st['artist']} – {seed_title(st)} (query `{r['query']}`, {r['peers']} peers)"
        )
    lines += ["", "## Trap false positives", ""]
    for r in rows:
        if r["kind"] == "negative" and r["helpful_peers"]:
            st = seeds[r["seed"]]
            lines.append(
                f"- `{r['shape']}` {st['artist']} – {seed_title(st)}: `{r['examples'][0]['files'][0]}`"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument(
        "--spot-check", type=Path, default=Path(".cache/label_spotcheck.md")
    )
    ap.add_argument(
        "--rows", type=Path, default=None, help="write per-query rows as JSON"
    )
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument(
        "--plan",
        type=Path,
        action="append",
        default=[],
        help="plan file(s) whose (seed, shape) uses apply to matching queries",
    )
    args = ap.parse_args(argv)

    seeds, records = load(args.corpus)
    merge_plan_uses(records, [json.loads(p.read_text()) for p in args.plan])
    rows = [row for rec in records for row in score_record(rec, seeds)]
    summary = summarise(rows)
    args.report.write_text(render(summary, rows))
    args.spot_check.parent.mkdir(parents=True, exist_ok=True)
    args.spot_check.write_text(render_spot_check(rows, seeds, args.sample))
    if args.rows:
        args.rows.write_text(json.dumps(rows, ensure_ascii=False))
    print(f"{len(records)} records, {len(rows)} (seed, shape) rows → {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
