"""Normalization stage 2 (#72): how peers lay out and spell file paths.

Offline apart from one MusicBrainz lookup per seed (cached). Reads the old
search harvest (#34) copied into ``.cache/corpus/<corpus>/`` and measures,
for files that are the seed's song:

- **Layout**: where the title, artist and album sit (filename, parent folder,
  the folder above, higher); "Artist - Title" filenames; track-number forms;
  disc folders; year and format tags; ``_`` / ``.`` used as spaces.
- **Spelling vs the query**: whether the MusicBrainz title/artist appears as
  written, or only after folding case, accents, punctuation; script of
  non-Latin seeds' paths.

Ground truth: the old track seeds with a MusicBrainz id, minus the
``library`` and ``negative`` buckets; names re-fetched from MusicBrainz by id.
A file counts when it's audio, within ±3 s of the MusicBrainz length, its
filename has every title word and the main artist is somewhere in the path
(half the title words let in other songs: 33 of 35 sampled files without the
artist were a different song, 2026-10-27). ``--sample`` writes a random
set to a local file for a hand check of that rule.

Only counts go into the report: no peer names or paths (public repo).

    python -m scripts.corpus.path_layout
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.corpus.fetch_names import MB_DELAY, MUSICBRAINZ, get_json, save_cache

CORPUS_DIR = Path(".cache/corpus")
REPORT = Path("tests/fixtures/normalization/paths_report.md")
SAMPLE = Path(".cache/paths_handcheck.tsv")
SKIP_BUCKETS = {"library", "negative"}
AUDIO_RE = re.compile(
    r"\.(flac|mp3|m4a|ogg|opus|wav|aac|alac|ape|wma|aiff?)$", re.IGNORECASE
)
LENGTH_SLACK_S = 3

TRACK_NO = {
    "disc-track (1-01)": re.compile(r"^\d{1,2}-\d{2}\b"),
    "vinyl side (A1)": re.compile(r"^[A-D]\d{1,2}\b"),
    "nn - ": re.compile(r"^\d{1,3}\s+-\s+"),
    "nn. ": re.compile(r"^\d{1,3}\.\s*"),
    "nn_/nn-": re.compile(r"^\d{1,3}[_-]"),
    "nn ": re.compile(r"^\d{1,3}\s+"),
}
DISC_RE = re.compile(r"^(?:cd|disc|disk|диск)\s*\.?\s*\d", re.IGNORECASE)
YEAR_RE = re.compile(r"(?:^|[\s(\[_-])(?:19|20)\d{2}(?:$|[\s)\]_-])")
FORMAT_RE = re.compile(
    r"\b(?:flac|mp3|320|v0|24.?bit|16.?bit|web|vinyl|lossless|aac)\b", re.IGNORECASE
)
_APOS_RE = re.compile(r"['’ʼ`´]")
_NON_WORD_RE = re.compile(r"[\W_]+")


def fold_case(t: str) -> str:
    return t.casefold()


def fold_accents(t: str) -> str:
    out = []
    for ch in t:
        base = unicodedata.normalize("NFD", ch)
        if len(base) > 1 and unicodedata.name(base[0], "").startswith("LATIN"):
            out.append("".join(c for c in base if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def fold_punct(t: str) -> str:
    return " ".join(_NON_WORD_RE.sub(" ", _APOS_RE.sub("", t)).split())


def fold(t: str) -> str:
    return fold_punct(fold_accents(fold_case(unicodedata.normalize("NFC", t))))


def has_words(hay: str, needle: str) -> bool:
    """Every word of ``needle`` in ``hay``, in order and adjacent (both folded)."""
    return bool(needle) and f" {needle} " in f" {hay} "


def spelling_needed(text: str, want: str) -> str:
    """Least folding at which ``want`` appears in ``text``."""
    t, w = unicodedata.normalize("NFC", text), unicodedata.normalize("NFC", want)
    if w in t:
        return "as written"
    if fold_case(w) in fold_case(t):
        return "case"
    if fold_accents(fold_case(w)) in fold_accents(fold_case(t)):
        return "case+accents"
    if has_words(fold(t), fold(w)):
        return "case+accents+punct"
    return "not adjacent"


def script_of(text: str) -> str:
    names = {unicodedata.name(c, "").split()[0] for c in text if c.isalpha()}
    for s in (
        "CYRILLIC",
        "HANGUL",
        "HIRAGANA",
        "KATAKANA",
        "CJK",
        "HEBREW",
        "ARABIC",
        "GREEK",
    ):
        if s in names:
            return s.lower()
    return "latin"


@dataclass
class Seed:
    id: str
    bucket: str
    mbid: str
    title: str = ""
    artist: str = ""
    albums: tuple[str, ...] = ()
    length_s: float | None = None


def load_seeds() -> dict[str, Seed]:
    seeds: dict[str, Seed] = {}
    for f in sorted(CORPUS_DIR.glob("*/seeds.json")):
        for s in json.loads(f.read_text()):
            if (
                s["type"] == "track"
                and s.get("mbid")
                and s["bucket"] not in SKIP_BUCKETS
            ):
                seeds.setdefault(s["id"], Seed(s["id"], s["bucket"], s["mbid"]))
    return seeds


def fill_from_musicbrainz(seed: Seed) -> bool:
    q = "inc=artist-credits+releases&fmt=json"
    r = get_json(f"{MUSICBRAINZ}/recording/{seed.mbid}?{q}", MB_DELAY)
    if not r:
        return False
    seed.title = r["title"]
    credit = r.get("artist-credit", [])
    seed.artist = credit[0]["name"] if credit else ""  # main artist, as queries use
    seed.albums = tuple(
        sorted({x["title"] for x in r.get("releases", []) if x.get("title")})
    )
    seed.length_s = r["length"] / 1000 if r.get("length") else None
    return True


def path_parts(filename: str) -> list[str]:
    parts = [p for p in re.split(r"[\\/]", filename) if p]
    if parts and parts[0].startswith("@@"):
        parts = parts[1:]  # share root
    return parts


def where(parts: list[str], want: str) -> str:
    """Nearest path level holding ``want`` (folded words)."""
    w = fold(want)
    levels = [
        ("filename", parts[-1:]),
        ("parent", parts[-2:-1]),
        ("grandparent", parts[-3:-2]),
    ]
    levels.append(("higher", parts[:-3]))
    for name, comps in levels:
        if any(has_words(fold(c), w) for c in comps):
            return name
    return "absent"


def is_candidate(seed: Seed, f: dict[str, Any]) -> str | None:
    """None when the file counts, else the reason it doesn't."""
    if not AUDIO_RE.search(f["filename"]):
        return "not audio"
    if f.get("length") is None or seed.length_s is None:
        return "no length"
    if abs(f["length"] - seed.length_s) > LENGTH_SLACK_S:
        return "length off"
    parts = path_parts(f["filename"])
    base = f" {fold(parts[-1])} "
    words = fold(seed.title).split()
    if not words or not all(f" {w} " in base for w in words):
        return "title words"
    if where(parts, seed.artist) == "absent":
        return "artist not in path"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=REPORT)
    ap.add_argument("--sample", type=int, default=100, help="files for the hand check")
    args = ap.parse_args()

    seeds = load_seeds()
    missing = [s.id for s in seeds.values() if not fill_from_musicbrainz(s)]
    save_cache()
    for sid in missing:
        del seeds[sid]

    seen: set[tuple[str, str, str]] = set()
    files: list[tuple[Seed, str]] = []
    reasons: Counter[str] = Counter()
    queries = 0
    for raw in sorted(CORPUS_DIR.glob("*/raw/*.json.gz")):
        d = json.loads(gzip.decompress(raw.read_bytes()))
        uses = [seeds[u["seed"]] for u in d["uses"] if u["seed"] in seeds]
        if not uses:
            continue
        queries += 1
        for resp in d["responses"]:
            for f in resp.get("files", []):
                for seed in uses:
                    key = (seed.id, resp["username"], f["filename"])
                    if key in seen:
                        continue
                    seen.add(key)
                    why = is_candidate(seed, f)
                    reasons[why or "counted"] += 1
                    if why is None:
                        files.append((seed, f["filename"]))

    c: dict[str, Counter[str]] = {
        k: Counter()
        for k in (
            "title",
            "artist",
            "album",
            "trackno",
            "layout",
            "title_spell",
            "artist_spell",
            "script",
        )
    }
    per_seed: Counter[str] = Counter()
    for seed, name in files:
        parts = path_parts(name)
        base = AUDIO_RE.sub("", parts[-1])
        per_seed[seed.id] += 1
        c["title"][where(parts, seed.title)] += 1
        c["artist"][where(parts, seed.artist)] += 1
        # A single named like the track would "find" the album in every filename.
        albums = [a for a in seed.albums if fold(a) != fold(seed.title)]
        album_at = [where(parts, a) for a in albums]
        order = ["filename", "parent", "grandparent", "higher"]
        c["album"][next((lvl for lvl in order if lvl in album_at), "absent")] += 1
        c["trackno"][
            next((k for k, rx in TRACK_NO.items() if rx.search(base)), "none")
        ] += 1
        fb = fold(base)
        fa = fold(seed.artist)
        if fa and re.match(rf"^(?:\d{{1,3}}\s+)?{re.escape(fa)}\s", fb):
            c["layout"]["'Artist - Title' filename"] += 1
        if "_" in base and " " not in base:
            c["layout"]["_ instead of spaces"] += 1
        if base.count(".") >= 3 and " " not in base:
            c["layout"][". instead of spaces"] += 1
        if len(parts) >= 2 and DISC_RE.search(parts[-2]):
            c["layout"]["disc folder (CD1)"] += 1
        if any(YEAR_RE.search(p) for p in parts[:-1]):
            c["layout"]["year in a folder"] += 1
        if any(FORMAT_RE.search(p) for p in parts[:-1]):
            c["layout"]["format tag in a folder"] += 1
        c["title_spell"][spelling_needed(base, seed.title)] += 1
        c["artist_spell"][spelling_needed("\\".join(parts), seed.artist)] += 1
        if script_of(seed.title + seed.artist) != "latin":
            c["script"][
                f"seed {script_of(seed.title + seed.artist)}, path {script_of(name)}"
            ] += 1

    total = len(files)
    lines = [
        "# Normalization stage 2 report (#72): file paths",
        "",
        (
            f"{len(seeds)} seeds (track, MusicBrainz id, no library/negative buckets), "
            f"{queries} harvest queries, {total} counted files on "
            f"{len(per_seed)} seeds. Generated by "
            "`python -m scripts.corpus.path_layout`."
        ),
        "",
        (
            "A file counts when it's audio, within ±3 s of the MusicBrainz length, its "
            "filename has every title word and the main artist is somewhere in the path. "
            "So artist-less paths and other spellings of the artist aren't measured "
            "here (a hand check of the looser rule found ~2 in 100 right-song files "
            "without the artist). Shares are of counted files; only counts are reported "
            "(no peer names or paths)."
        ),
        "",
        "## Files seen per reason",
        "",
        "| reason | files |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in reasons.most_common()],
    ]
    sections = [
        ("title", "Where the title is"),
        ("artist", "Where the main artist is"),
        (
            "album",
            "Where an album title is (any MusicBrainz release not named like the track)",
        ),
        ("trackno", "Track-number form at the start of the filename"),
        ("layout", "Layout patterns (a file can have several)"),
        ("title_spell", "Least folding for the title to appear in the filename"),
        ("artist_spell", "Least folding for the main artist to appear in the path"),
        (
            "script",
            "Non-Latin seeds: script of the path (artist required as written, so only same-script paths can count)",
        ),
    ]
    for sec, heading in sections:
        lines += ["", f"## {heading}", "", "| value | files | share |", "|---|---|---|"]
        lines += [
            f"| {k} | {v} | {100 * v / total:.1f}% |" for k, v in c[sec].most_common()
        ]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out} ({total} files)")

    rng = random.Random(72)
    SAMPLE.parent.mkdir(exist_ok=True)
    with SAMPLE.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["ok (y/n)", "artist", "title", "length_s", "path"])
        for seed, name in rng.sample(files, min(args.sample, total)):
            w.writerow(["", seed.artist, seed.title, seed.length_s, name])
    print(f"wrote {SAMPLE} for the hand check")


if __name__ == "__main__":
    main()
