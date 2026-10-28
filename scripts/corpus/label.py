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
4. its length is within ±6 s of the MusicBrainz length (was ±3 s until
   2026-10-29). A file with no
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
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# The file rules live in the app's matcher (#90); this module adds the
# benchmark's seed dicts, peer scoring and report around them.
from app.core import matcher as _matcher
from app.core.matcher import (
    AUDIO_EXTS,
    Song,
    _contains_all,
    artist_matches,
    label_file,
    norm,
    split_path,
    title_matches,
)
from app.core.matcher import (
    words as _words,
)

__all__ = ["artist_matches", "norm", "title_matches"]


def seed_title(seed: dict) -> str:
    return seed.get("track") or seed.get("title") or ""


def seed_length(seed: dict) -> float | None:
    return seed.get("length_s") or seed.get("duration")


def excluded_word(filename: str, seed: dict) -> str | None:
    return _matcher.excluded_word(filename, seed_title(seed), seed.get("album", ""))


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
    song = Song(
        seed_title(seed), seed["artist"], seed.get("album", ""), seed_length(seed)
    )
    return FileLabel(label_file(name, f.get("length"), song), name)


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
    return _matcher.album_folder_ok(
        folder,
        (f["filename"] for f in files),
        seed["album"],
        seed["artist"],
        seed.get("track_count"),
    )


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
