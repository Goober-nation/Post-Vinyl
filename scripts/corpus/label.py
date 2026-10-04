"""Label harvested search responses and score query shapes by helpful peers (#34).

Offline: reads ``<corpus>/raw/*.json.gz`` + ``<corpus>/seeds.json`` from a
harvest, never touches slskd. The unit of success is a **helpful peer** — a
peer with at least one correct, downloadable file — not a file count: one
peer is all a download needs, and 40 duplicates on one peer are worth one.

A file is **correct** for a track seed when (decided 2026-10-04, #34):

1. it is an audio file in the peer's unlocked ``files`` (locked files can't
   be downloaded and are only counted),
2. no excluded version word appears in its filename or parent folder unless
   the seed's own title/album contains it (covers, live, acoustic,
   a cappella, mixes/remixes, extended, solo, reference, home sessions,
   soundtracks, ambient — the user never wants these),
3. every title word is in the **filename** (remaster/deluxe suffixes ignored,
   they're the same song) and every artist word is somewhere in the path,
4. its length is within ±3 s of the MusicBrainz duration, when both exist.

Album seeds: a peer is helpful when one folder holds the album — the folder
path carries the album and artist words, has no excluded word, and holds at
least 80% of the MusicBrainz track count in audio files. Artist seeds: any
audio file with the artist in its path. Negative seeds use the track rule;
any helpful peer there is a false positive.

Quality (format, bitrate, sample rate) is recorded per helpful peer but not
scored yet.

    python -m scripts.corpus.label --corpus <dir> --report docs/search-benchmark.md
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scripts.corpus.resolve_seeds import norm, norm_title

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
)  # fmt: skip

DURATION_TOLERANCE_S = 3
ALBUM_COMPLETENESS = 0.8


def split_path(filename: str) -> tuple[list[str], str, str]:
    """Soulseek paths use backslashes; return (folders, basename, extension)."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else ""
    stem, dot, ext = base.rpartition(".")
    return parts[:-1], stem if dot else base, ext.lower() if dot else ""


def _words(text: str) -> list[str]:
    return norm(text).split()


def _has_phrase(haystack: list[str], phrase: str) -> bool:
    needle = phrase.split()
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# "live" glued to the next word or a year: Livebox, LiveAt, Live1999. A plain
# prefix test would also hit liver/lively/olive, so the tail is whitelisted.
_JOINED_LIVE_RE = re.compile(r"^live(?:box|at|in|from|on|set|show|tour|\d{2,4})$")


def _version_hit(local: list[str], own: list[str]) -> str | None:
    for phrase in EXCLUDED_VERSIONS:
        if _has_phrase(local, phrase) and not _has_phrase(own, phrase):
            return phrase
    if "live" not in own and any(_JOINED_LIVE_RE.match(w) for w in local):
        return "live"
    return None


def excluded_word(filename: str, seed: dict) -> str | None:
    """The first excluded version word in the file's name or parent folder
    that the seed itself doesn't carry, else None."""
    folders, stem, _ = split_path(filename)
    local = _words(stem) + (_words(folders[-1]) if folders else [])
    own = _words(seed.get("title", "")) + _words(seed.get("album", ""))
    return _version_hit(local, own)


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
    main = norm_title(_PAREN_RE.sub(" ", title)).split()
    extra = norm_title(" ".join(_PAREN_RE.findall(title))).split()
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
    if not title_matches(stem, seed["title"]):
        return FileLabel("no_title", name)
    if not artist_matches(folders, stem, seed["artist"]):
        return FileLabel("no_artist", name)
    want, got = seed.get("duration"), f.get("length")
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
    )
    kind = seed["type"]
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
                "kind": "negative" if seed.get("expect") == "none" else seed["type"],
                "shape": use["shape"],
                "family": shape_family(use["shape"]),
                "query": record["query"],
                "peers": len(peers),
                "helpful_peers": len(helpful),
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


def summarise(rows: list[dict]) -> dict[str, dict[str, dict]]:
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r["kind"]][r["family"]].append(r)
    out: dict[str, dict[str, dict]] = {}
    for kind, fams in by.items():
        out[kind] = {}
        for fam, rs in sorted(fams.items()):
            ratios = [r["helpful_peers"] / r["peers"] for r in rs if r["peers"]]
            out[kind][fam] = {
                "n": len(rs),
                "found_pct": 100 * sum(r["helpful_peers"] > 0 for r in rs) / len(rs),
                "median_helpful_peers": statistics.median(
                    r["helpful_peers"] for r in rs
                ),
                "mean_helpful_peers": statistics.mean(r["helpful_peers"] for r in rs),
                "median_peers": statistics.median(r["peers"] for r in rs),
                "median_peer_precision_pct": 100 * statistics.median(ratios)
                if ratios
                else 0.0,
            }
    return out


def render(summary: dict, rows: list[dict], seeds: dict[str, dict], sample: int) -> str:
    lines = [
        "# Search benchmark — helpful peers per query shape",
        "",
        "Generated by `python -m scripts.corpus.label`. A *helpful peer* has at",
        "least one correct, downloadable file (rules in `scripts/corpus/label.py`).",
        "*Found* = the query returned ≥1 helpful peer. *Peer precision* = helpful",
        "peers / all peers that answered. Negative seeds: any helpful peer is a",
        "false positive.",
        "",
    ]
    for kind in ("track", "album", "artist", "negative"):
        if kind not in summary:
            continue
        lines += [
            f"## {kind}",
            "",
            "| Shape | n | Found | Helpful peers (median) | (mean) | Peers (median) | Peer precision |",
            "|---|---|---|---|---|---|---|",
        ]
        for fam, s in summary[kind].items():
            lines.append(
                f"| {fam} | {s['n']} | {s['found_pct']:.0f}% | {s['median_helpful_peers']:.0f} "
                f"| {s['mean_helpful_peers']:.0f} | {s['median_peers']:.0f} "
                f"| {s['median_peer_precision_pct']:.1f}% |"
            )
        lines.append("")

    # Spot-check: random helpful verdicts and random misses, so a human can
    # confirm the labeller isn't wrong in either direction.
    rng = random.Random(0)
    lines += ["## Spot-check", "", "Random helpful verdicts (should all be right):", ""]
    pos = [r for r in rows if r["helpful_peers"] and r["kind"] == "track"]
    for r in rng.sample(pos, min(sample, len(pos))):
        s = seeds[r["seed"]]
        ex = r["examples"][0]
        lines.append(
            f"- `{r['shape']}` {s['artist']} – {s.get('title')}: `{ex['files'][0]}`"
        )
    lines += ["", "Track queries with zero helpful peers (check none were missed):", ""]
    neg = [r for r in rows if not r["helpful_peers"] and r["kind"] == "track"]
    for r in rng.sample(neg, min(sample, len(neg))):
        s = seeds[r["seed"]]
        lines.append(
            f"- `{r['shape']}` {s['artist']} – {s.get('title')} (query `{r['query']}`, "
            f"{r['peers']} peers)"
        )
    # Zero peers after a full, connected search is almost always a phrase on
    # Soulseek's server-side exclusion list (live 2026-10-04: "Kendrick
    # Lamar", "Gorillaz", "Purple Rain" → 0 peers while "alright kendrick"
    # → 300). Listed apart so a blocked phrase isn't read as a bad shape.
    empty = sorted({r["query"] for r in rows if r["peers"] == 0})
    lines += [
        "",
        f"Queries that got no peers at all ({len(empty)}; likely Soulseek-excluded phrases):",
        "",
        ", ".join(f"`{q}`" for q in empty),
    ]
    fps = [r for r in rows if r["kind"] == "negative" and r["helpful_peers"]]
    lines += ["", f"False positives on negative seeds: {len(fps)}", ""]
    for r in fps:
        s = seeds[r["seed"]]
        lines.append(
            f"- `{r['shape']}` {s['artist']} – {s['title']}: `{r['examples'][0]['files'][0]}`"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--report", type=Path, default=Path("docs/search-benchmark.md"))
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
    args.report.write_text(render(summary, rows, seeds, args.sample))
    if args.rows:
        args.rows.write_text(json.dumps(rows, ensure_ascii=False))
    print(f"{len(records)} records, {len(rows)} (seed, shape) rows → {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
