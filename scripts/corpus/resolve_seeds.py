"""Fill MusicBrainz ground truth into the search-benchmark seed list (#34).

For every seed in ``tests/fixtures/corpus/seeds.toml`` that has no
``mb_status`` yet, look it up on MusicBrainz and write back:

- track:  ``mbid`` (recording), ``album_mbid`` (release group), ``duration``
- album:  ``mbid`` (release group), ``track_count``
- artist: ``mbid`` (artist)
- negative: nothing is resolved; the seed is *checked* — if MusicBrainz has a
  recording matching the artist/title, the trap isn't a trap and is flagged.

``mb_status`` is one of:

- ``ok``          one confident match (track: title+artist+album all agree)
- ``ambiguous``   a match was written but needs a human look (reason in
                  ``mb_note``)
- ``unresolved``  nothing matched; no IDs written
- ``trap_ok`` / ``trap_exists``  negative seeds

Seeds that already carry ``mb_status`` are skipped, so the script is
resumable and safe to re-run; ``--retry`` re-resolves everything not
``ok``/``trap_ok``, ``--force`` re-resolves everything. A
transport error leaves the seed untouched so the next run retries it.

The file is edited in place line-by-line rather than re-serialised, so
comments, bucket headers and key order survive.

Run (public MusicBrainz API, 1 req/s, nothing touches slskd or the server)::

    python -m scripts.corpus.resolve_seeds
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import tomllib

from app.services.interfaces.musicbrainz import MBRecording
from app.services.musicbrainz_client import MusicBrainzClient

SEEDS_PATH = Path("tests/fixtures/corpus/seeds.toml")
REPORT_PATH = Path("tests/fixtures/corpus/resolve_report.md")

SEED_HEADER = "[[seed]]"

# Suffixes that don't change which song it is (decided 2026-10-03: a
# remaster/deluxe/year tag is the same song; live/remix/acoustic is not).
_SAME_SONG_RE = re.compile(
    r"\s*[\(\[\-–]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|deluxe(?: edition)?|"
    r"expanded(?: edition)?|bonus track|single version|album version|"
    r"collectors edition)(?:\s+\d{4})?\s*[\)\]]?\s*$",
    re.IGNORECASE,
)


def norm(text: str) -> str:
    """Compare-key for names: accents stripped, case-folded, punctuation and
    whitespace collapsed. Keeps non-Latin letters (unlike
    `query_builder.fold_for_matching`, which is ASCII-only) so 椎名林檎 still
    compares equal to itself.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[\W_]+", " ", text)
    return " ".join(text.split())


def strip_same_song(text: str) -> str:
    """Drop same-song suffixes like "(2004 Remaster)", repeatedly."""
    prev = None
    while prev != text:
        prev, text = text, _SAME_SONG_RE.sub("", text)
    return text


def norm_title(text: str) -> str:
    return norm(strip_same_song(text))


def names_match(want: str, got: str) -> bool:
    a, b = norm(want), norm(got)
    if not a or not b:
        return False
    # "The The" vs "The The", "Kendrick Lamar" vs "Kendrick Lamar feat. X".
    return a == b or b.startswith(a + " ") or a.startswith(b + " ")


# -- file editing ---------------------------------------------------------


def split_blocks(text: str) -> tuple[str, list[list[str]]]:
    """Split the file into its preamble and one line-list per ``[[seed]]``."""
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.strip() == SEED_HEADER:
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), blocks


def set_keys(block: list[str], values: dict[str, Any]) -> list[str]:
    """Return `block` with `values` set — replacing an existing ``key =`` line
    or inserting after the last key line (before any trailing blank/comment
    lines, which belong to the next bucket header).
    """
    out = list(block)
    for key, value in values.items():
        rendered = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        pattern = re.compile(rf"^{re.escape(key)}\s*=")
        for i, line in enumerate(out):
            if pattern.match(line):
                out[i] = rendered
                break
        else:
            last_key = max(
                i for i, line in enumerate(out) if i == 0 or re.match(r"^\w+\s*=", line)
            )
            out.insert(last_key + 1, rendered)
    return out


def join_blocks(preamble: str, blocks: list[list[str]]) -> str:
    parts = [preamble] if preamble else []
    parts.extend("\n".join(b) for b in blocks)
    return "\n".join(parts).rstrip() + "\n"


# -- resolving ------------------------------------------------------------


@dataclass
class Result:
    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, status: str, note: str | None = None, **values: Any) -> Result:
        out: dict[str, Any] = {k: v for k, v in values.items() if v is not None}
        out["mb_status"] = status
        if note:
            out["mb_note"] = note
        return cls(out)


def _matching_recordings(mb: MusicBrainzClient, seed: dict) -> list[MBRecording]:
    hits = mb.search_recording(strip_same_song(seed["title"]), seed["artist"], limit=25)
    want = norm_title(seed["title"])
    return [
        r
        for r in hits
        if norm_title(r.title) == want
        and (
            names_match(seed["artist"], r.artist)
            or names_match(seed["artist"], r.artist_credit)
        )
    ]


def _album_release_group(rec: MBRecording, album: str) -> str | None:
    want = norm_title(album)
    for rel in rec.releases:
        if want and norm_title(rel.title) == want and rel.release_group_mbid:
            return rel.release_group_mbid
    return None


def _via_album(mb: MusicBrainzClient, seed: dict) -> Result | None:
    """Find the track in its album's canonical tracklist.

    Tried before a recording search: a famous song has hundreds of
    recordings (live, compilations, remasters) and the top-25 recording hits
    regularly miss the studio take entirely — Karma Police on OK Computer
    did, live 2026-10-03. The album's tracklist names exactly one.
    """
    album = seed.get("album")
    if not album:
        return None
    is_va = seed.get("album_artist", "").casefold() == "various artists"
    groups = mb.search_release_group(album, None if is_va else seed["artist"], limit=10)
    want_album = norm_title(album)
    groups = [
        g
        for g in groups
        if norm_title(g.title) == want_album
        and (is_va or names_match(seed["artist"], g.artist))
    ]
    groups.sort(key=lambda g: (g.primary_type != "Album", -(g.score or 0)))
    want = norm_title(seed["title"])
    for group in groups[:3]:
        for rec in mb.lookup_release_group_tracks(group.mbid):
            if norm_title(rec.title) == want:
                return Result.of(
                    "ok",
                    mbid=rec.mbid,
                    album_mbid=group.mbid,
                    duration=_secs(rec.length_ms),
                )
    return None


def resolve_track(mb: MusicBrainzClient, seed: dict) -> Result:
    if seed.get("mbid"):
        rec = mb.lookup_recording(seed["mbid"])
        if rec is None:
            return Result.of("ambiguous", "tagged mbid not found on MusicBrainz")
        ok = norm_title(rec.title) == norm_title(seed["title"])
        return Result.of(
            "ok" if ok else "ambiguous",
            None if ok else f"tagged mbid is '{rec.artist_credit} - {rec.title}'",
            album_mbid=_album_release_group(rec, seed.get("album", "")),
            duration=seed.get("duration") or _secs(rec.length_ms),
        )

    if (found := _via_album(mb, seed)) is not None:
        return found

    matches = _matching_recordings(mb, seed)
    if not matches:
        return Result.of("unresolved", "no recording matched title+artist")

    album = seed.get("album", "")
    on_album = [(r, rg) for r in matches if (rg := _album_release_group(r, album))]
    if on_album:
        # Several recordings can sit on the same album (reissues re-recorded,
        # mis-merges); the one on the most releases is the canonical one.
        rec, rg = max(on_album, key=lambda p: (p[0].is_official, len(p[0].releases)))
        return Result.of(
            "ok", mbid=rec.mbid, album_mbid=rg, duration=_secs(rec.length_ms)
        )

    rec = max(matches, key=lambda r: (r.is_official, len(r.releases)))
    note = f"no match on album '{album}'" if album else "seed has no album"
    return Result.of("ambiguous", note, mbid=rec.mbid, duration=_secs(rec.length_ms))


def resolve_album(mb: MusicBrainzClient, seed: dict) -> Result:
    want = norm_title(seed["album"])
    groups = mb.search_release_group(seed["album"], seed["artist"], limit=25)
    matches = [g for g in groups if norm_title(g.title) == want]
    if seed["artist"].casefold() != "various artists":
        matches = [g for g in matches if names_match(seed["artist"], g.artist)]
    if not matches:
        return Result.of("unresolved", "no release group matched title+artist")
    albums = [g for g in matches if g.primary_type == "Album"] or matches
    best = max(albums, key=lambda g: (g.score or 0, g.release_count or 0))
    tracks = mb.lookup_release_group_tracks(best.mbid)
    note = f"{len(albums)} release groups share this title" if len(albums) > 1 else None
    return Result.of(
        "ambiguous" if note else "ok",
        note,
        mbid=best.mbid,
        track_count=len(tracks) or None,
    )


def resolve_artist(mb: MusicBrainzClient, seed: dict) -> Result:
    hits = mb.search_artist(seed["artist"], limit=25)
    exact = [a for a in hits if norm(a.name) == norm(seed["artist"])]
    if not exact:
        return Result.of("unresolved", "no artist with this exact name")
    best = max(exact, key=lambda a: a.score or 0)
    if len(exact) > 1:
        others = ", ".join(a.disambiguation or "?" for a in exact if a is not best)
        return Result.of(
            "ambiguous",
            f"{len(exact)} artists share the name; picked '{best.disambiguation or best.name}' "
            f"over: {others}",
            mbid=best.mbid,
        )
    return Result.of("ok", mbid=best.mbid)


def check_negative(mb: MusicBrainzClient, seed: dict) -> Result:
    matches = _matching_recordings(mb, seed)
    if matches:
        return Result.of(
            "trap_exists",
            f"MusicBrainz has it: {matches[0].artist_credit} - {matches[0].title} "
            f"({matches[0].mbid})",
        )
    return Result.of("trap_ok")


def resolve(mb: MusicBrainzClient, seed: dict) -> Result:
    if seed.get("expect") == "none":
        return check_negative(mb, seed)
    kind = seed["type"]
    if kind == "track":
        return resolve_track(mb, seed)
    if kind == "album":
        return resolve_album(mb, seed)
    if kind == "artist":
        return resolve_artist(mb, seed)
    raise ValueError(f"unknown seed type {kind!r}")


def _secs(length_ms: int | None) -> int | None:
    return round(length_ms / 1000) if length_ms else None


def _label(seed: dict) -> str:
    return " – ".join(seed[k] for k in ("artist", "title", "album") if seed.get(k))


def write_report(seeds: list[dict], path: Path) -> None:
    counts = Counter(s.get("mb_status", "pending") for s in seeds)
    numbered = list(enumerate(seeds))
    lines = [
        "# Seed resolve report",
        "",
        "Generated by `python -m scripts.corpus.resolve_seeds`. Fix a seed by editing",
        "`seeds.toml` (set `mbid`, delete `mb_status`) and re-running.",
        "",
        " · ".join(f"**{k}**: {v}" for k, v in sorted(counts.items())),
        "",
    ]
    for status in ("trap_exists", "unresolved", "ambiguous", "pending"):
        rows = [(i, s) for i, s in numbered if s.get("mb_status", "pending") == status]
        if not rows:
            continue
        lines += [f"## {status} ({len(rows)})", ""]
        for i, s in rows:
            note = s.get("mb_note", "")
            lines.append(f"- #{i} `{s['bucket']}` {s['type']}: {_label(s)} — {note}")
        lines.append("")
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--report", type=Path, default=REPORT_PATH)
    ap.add_argument(
        "--force", action="store_true", help="re-resolve seeds that have mb_status"
    )
    ap.add_argument(
        "--retry", action="store_true", help="re-resolve seeds not ok/trap_ok"
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="resolve at most N seeds this run"
    )
    args = ap.parse_args(argv)

    mb = MusicBrainzClient(SimpleNamespace())
    preamble, blocks = split_blocks(args.seeds.read_text())
    done = 0
    for idx, block in enumerate(blocks):
        seed = tomllib.loads("\n".join(block))["seed"][0]
        status = seed.get("mb_status")
        if (
            status
            and not args.force
            and not (args.retry and status not in ("ok", "trap_ok"))
        ):
            continue
        if args.limit is not None and done >= args.limit:
            break
        try:
            result = resolve(mb, seed)
        except Exception as e:  # noqa: BLE001 — a transport error must not lose earlier work
            print(f"[{idx}] ERROR {_label(seed)}: {e}", file=sys.stderr)
            continue
        blocks[idx] = set_keys(block, result.values)
        done += 1
        print(f"[{idx}] {result.values['mb_status']:<11} {_label(seed)}")
        # Write after every seed: a Ctrl-C an hour in keeps everything so far.
        args.seeds.write_text(join_blocks(preamble, blocks))

    seeds = tomllib.loads(args.seeds.read_text())["seed"]
    write_report(seeds, args.report)
    print(f"resolved {done}; report at {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
