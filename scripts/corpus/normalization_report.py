"""Normalization stage 1 report (#70): how often sources agree after each rule.

Offline: reads the snapshot from ``fetch_names.py`` (#69), normalizes every
song's names per source and compares them per source pair and per field.

- **Rule configs**: no rules, all rules, all-but-one (what each rule is
  needed for) and only-one (what each rule fixes on its own).
- **Fields**: track, artists (sorted set of main artists), album. MusicBrainz
  has many releases per recording; its album counts as equal when any
  release title matches. A missing ListenBrainz release is not compared.
- **Catalogue**: how often each raw pattern (feat., version suffix, "&",
  leading "The", accents) appears per source.
- **Mismatches**: every field that still differs with all rules on.

No pass target: the user judges (#71).

    python -m scripts.corpus.normalization_report
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.corpus import normalize as n

SNAPSHOT = Path("tests/fixtures/normalization/songs.json")
REPORT = Path("tests/fixtures/normalization/report.md")
SOURCES = ("deezer", "musicbrainz", "listenbrainz")
PAIRS = (
    ("deezer", "musicbrainz"),
    ("deezer", "listenbrainz"),
    ("musicbrainz", "listenbrainz"),
)
FIELDS = ("track", "artists", "album")

CATALOGUE = {
    "feat. in title": re.compile(
        r"[(\[]\s*(feat|ft|with)\b|\s(feat|ft|featuring)\.?\s", re.IGNORECASE
    ),
    "version suffix": re.compile(
        r"remaster|deluxe|album version|single version|[(\[]\d{4}[)\]]", re.IGNORECASE
    ),
    "& in name": re.compile(r"&"),
    "leading The": re.compile(r"^the\s", re.IGNORECASE),
    "apostrophe": re.compile(r"['’ʼ`´]"),
    "non-ASCII Latin": re.compile(r"[À-ɏ]"),
}

Raw = dict[str, Any]


def raw_names(song: Raw) -> dict[str, Raw | None]:
    """Per source: track, credits, album (or albums), plus strings for display."""
    dz, mb = song["deezer"], song["musicbrainz"][0]
    lb = song["listenbrainz"].get(mb["id"])
    dz_credits = [
        n.Credit(c["name"], c.get("role") != "Main") for c in dz["contributors"]
    ] or [n.Credit(dz["artist"])]
    out: dict[str, Raw | None] = {
        "deezer": {
            "track": dz["title"],
            "credits": dz_credits,
            "albums": [dz["album"]],
            "artist_str": ", ".join(
                f"{c.name}{'*' if c.featured else ''}" for c in dz_credits
            ),
        },
        "musicbrainz": {
            "track": mb["title"],
            "credits": n.credits_from_joined(
                (a["name"], a["joinphrase"]) for a in mb["artist_credit"]
            ),
            "albums": sorted({r["title"] for r in mb["releases"] if r["title"]}),
            "artist_str": "".join(
                a["name"] + a["joinphrase"] for a in mb["artist_credit"]
            ),
        },
        "listenbrainz": None,
    }
    if lb:
        out["listenbrainz"] = {
            "track": lb["title"],
            "credits": n.credits_from_joined(
                (a["name"], a["join_phrase"]) for a in lb["artists"]
            )
            or [n.Credit(lb["artist_credit"] or "")],
            "albums": [lb["release"]] if lb["release"] else [],
            "artist_str": lb["artist_credit"],
        }
    return out


def norm_fields(raw: Raw, rules: frozenset[str]) -> dict[str, Any]:
    return {
        "track": n.norm_title(raw["track"], rules),
        "artists": n.norm_artists(raw["credits"], rules),
        "album": {n.norm_title(a, rules) for a in raw["albums"]},
    }


def equal(field: str, a: dict[str, Any], b: dict[str, Any]) -> bool | None:
    """None when the field can't be compared (no album on one side)."""
    if field == "album":
        if not a["album"] or not b["album"]:
            return None
        return bool(a["album"] & b["album"])
    return bool(a[field] == b[field])


def rates(
    songs: list[dict[str, Raw | None]], rules: frozenset[str]
) -> dict[tuple[str, str, str], tuple[int, int]]:
    out: dict[tuple[str, str, str], tuple[int, int]] = {}
    for s1, s2 in PAIRS:
        for field in FIELDS:
            eq = total = 0
            for song in songs:
                r1, r2 = song[s1], song[s2]
                if r1 is None or r2 is None:
                    continue
                verdict = equal(field, norm_fields(r1, rules), norm_fields(r2, rules))
                if verdict is None:
                    continue
                total += 1
                eq += verdict
            out[(s1, s2, field)] = (eq, total)
    return out


def pct(eq: int, total: int) -> str:
    return f"{100 * eq / total:.1f}%" if total else "n/a"


def short(source: str) -> str:
    return {"deezer": "DZ", "musicbrainz": "MB", "listenbrainz": "LB"}[source]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    ap.add_argument("--out", type=Path, default=REPORT)
    args = ap.parse_args()

    data = json.loads(args.snapshot.read_text())
    songs_raw = data["songs"]
    songs = [raw_names(s) for s in songs_raw]
    cols = [(s1, s2, f) for s1, s2 in PAIRS for f in FIELDS]
    header = (
        "| config | "
        + " | ".join(f"{short(a)}–{short(b)} {f}" for a, b, f in cols)
        + " |"
    )
    sep = "|---" * (len(cols) + 1) + "|"

    configs: list[tuple[str, frozenset[str]]] = [
        ("no rules", frozenset()),
        ("**all rules**", n.ALL_RULES),
    ]
    configs += [(f"all − {r}", n.ALL_RULES - {r}) for r in sorted(n.ALL_RULES)]
    configs += [(f"only {r}", frozenset({r})) for r in sorted(n.ALL_RULES)]

    lines = [
        "# Normalization stage 1 report (#70)",
        "",
        (
            f"Snapshot: `{args.snapshot}`, fetched {data['meta']['fetched_at']}, "
            f"{len(songs)} songs. Generated by "
            "`python -m scripts.corpus.normalization_report`."
        ),
        "",
        (
            "Equality rate per source pair and field (equal / comparable). Artists = "
            "sorted set of main artists. MB album = any release title. Songs without LB "
            "metadata or an LB release are not compared there."
        ),
        "",
        header,
        sep,
    ]
    for name, rules in configs:
        r = rates(songs, rules)
        lines.append(f"| {name} | " + " | ".join(pct(*r[c]) for c in cols) + " |")
    all_r = rates(songs, n.ALL_RULES)
    lines += [
        "",
        "Comparable counts (all rules): "
        + ", ".join(
            f"{short(a)}–{short(b)} {f} {all_r[(a, b, f)][1]}" for a, b, f in cols
        )
        + ".",
    ]

    lines += [
        "",
        "## Raw pattern catalogue",
        "",
        "Songs whose raw names contain the pattern, per source.",
        "",
    ]
    lines += [
        "| pattern | " + " | ".join(SOURCES) + " |",
        "|---" * (len(SOURCES) + 1) + "|",
    ]
    for label, rx in CATALOGUE.items():
        counts: Counter[str] = Counter()
        for song in songs:
            for src in SOURCES:
                raw = song[src]
                if raw is None:
                    continue
                text = " ".join([raw["track"], raw["artist_str"] or "", *raw["albums"]])
                counts[src] += bool(rx.search(text))
        lines.append(
            f"| {label} | " + " | ".join(str(counts[s]) for s in SOURCES) + " |"
        )

    by_origin = Counter(s["origin"].split(":")[0] for s in songs_raw)
    lines += ["", f"Origins: {dict(by_origin)}."]

    lines += ["", "## Mismatches with all rules on", ""]
    for field in FIELDS:
        rows = []
        for raw_song, song in zip(songs_raw, songs, strict=True):
            for s1, s2 in PAIRS:
                r1, r2 = song[s1], song[s2]
                if r1 is None or r2 is None:
                    continue
                n1, n2 = norm_fields(r1, n.ALL_RULES), norm_fields(r2, n.ALL_RULES)
                if equal(field, n1, n2) is False:
                    shown = {
                        "track": lambda r: r["track"],
                        "artists": lambda r: r["artist_str"],
                        "album": lambda r: (
                            " / ".join(r["albums"][:4])
                            + (" …" if len(r["albums"]) > 4 else "")
                        ),
                    }[field]
                    rows.append(
                        f"| {raw_song['isrc']} | {short(s1)}–{short(s2)} | {shown(r1)} | {shown(r2)} |"
                    )
        lines += [
            f"### {field} ({len(rows)})",
            "",
            "| isrc | pair | left | right |",
            "|---|---|---|---|",
        ]
        lines += [row.replace("\n", " ") for row in rows] or ["| – | – | – | – |"]
        lines.append("")

    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
