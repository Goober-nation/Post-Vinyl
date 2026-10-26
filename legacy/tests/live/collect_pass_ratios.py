#!/usr/bin/env python3
"""
Pass-ratio data collection for tuning `search.pass_ratio_threshold`.

Why this is a script and not a test: which peers answer a query depends on who
is online, so a single measurement means nothing. Worse, `search.response_threshold`
(default 10) cancels the search as soon as 10 peers have answered — so the pass
ratio's denominator is a ~10-peer *sample*, not the full response set, however
popular the track is. Small samples, high variance.

So: run the same handful of tracks several times, record every rung, and look
at the spread rather than any single number.

Usage:
    python3 tests/live/collect_pass_ratios.py --runs 5
    python3 tests/live/collect_pass_ratios.py --runs 5 --out ratios.json

Pick tracks by whether they're *hard to match* — long titles, feat clauses,
parens, non-ASCII — not by popularity. Popularity barely affects the sample
size, because of the early cutoff above.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.live.harness import (
    DEFAULT_MUSICA_URL,
    DockerControl,
    LogScraper,
    MusicaClient,
    Timeline,
    diagnose_unreachable,
    first_viable_result,
)

# Deliberately awkward titles — each exercises a different pipeline branch.
DEFAULT_TRACKS = [
    ("Smells Like Teen Spirit", "Nirvana"),
    ("Heroes (We Could Be)", "Alesso feat. Tove Lo"),
    ("Everything In Its Right Place", "Radiohead"),
]


def collect_run(
    client: MusicaClient, logs: LogScraper, track: str, artist: str
) -> dict:
    """One search for one track; returns what came back."""
    start = time.monotonic()
    job = client.search(track, artist=artist)
    detail = client.search_detail(job["search_id"])
    duration = time.monotonic() - start

    results = detail["results"]
    peers = {r["username"] for r in results}
    with_slot = [r for r in results if r.get("has_free_slot")]

    return {
        "track": track,
        "artist": artist,
        "search_id": job["search_id"],
        "duration": round(duration, 2),
        "results": len(results),
        "peers": len(peers),
        "with_free_slot": len(with_slot),
        "picked": bool(first_viable_result(results)),
    }


def summarize(samples: list[dict]) -> dict:
    """Spread matters more than the mean here — a threshold tuned to an
    average that swings between 0.1 and 0.9 is tuned to nothing."""
    counts = [s["results"] for s in samples]
    if not counts:
        return {}
    summary = {
        "runs": len(samples),
        "results_min": min(counts),
        "results_max": max(counts),
        "results_mean": round(statistics.mean(counts), 2),
        "results_median": statistics.median(counts),
        "empty_runs": sum(1 for c in counts if c == 0),
        "duration_mean": round(statistics.mean(s["duration"] for s in samples), 2),
    }
    if len(counts) > 1:
        summary["results_stdev"] = round(statistics.stdev(counts), 2)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Repeats per track")
    parser.add_argument("--url", default=DEFAULT_MUSICA_URL)
    parser.add_argument("--out", type=Path, help="Write raw samples as JSON")
    parser.add_argument(
        "--gap",
        type=float,
        default=5.0,
        help="Seconds between searches (be polite to the network)",
    )
    parser.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="TRACK|ARTIST",
        help="Override the default track list; repeatable.",
    )
    args = parser.parse_args()

    tracks = DEFAULT_TRACKS
    if args.track:
        tracks = []
        for spec in args.track:
            if "|" not in spec:
                parser.error(f"--track needs TRACK|ARTIST, got {spec!r}")
            title, _, artist = spec.partition("|")
            tracks.append((title.strip(), artist.strip()))

    timeline = Timeline()
    client = MusicaClient(args.url, timeline=timeline)
    if not client.is_up():
        print(f"musica did not answer at {args.url}", file=sys.stderr)
        print(diagnose_unreachable(args.url), file=sys.stderr)
        return 1
    logs = LogScraper(DockerControl(timeline=timeline))

    samples: list[dict] = []
    by_track: dict[tuple[str, str], list[dict]] = defaultdict(list)

    total = args.runs * len(tracks)
    done = 0
    for run in range(args.runs):
        for track, artist in tracks:
            done += 1
            print(
                f"[{done}/{total}] run {run + 1}: {track!r} — {artist!r} ... ",
                end="",
                flush=True,
            )
            try:
                sample = collect_run(client, logs, track, artist)
            except Exception as e:  # noqa: BLE001 — a bad run shouldn't lose the rest
                print(f"FAILED: {e}")
                continue
            sample["run"] = run + 1
            samples.append(sample)
            by_track[(track, artist)].append(sample)
            print(
                f"{sample['results']} results, "
                f"{sample['with_free_slot']} with a free slot, "
                f"{sample['duration']}s"
            )
            time.sleep(args.gap)

    print("\n" + "=" * 72)
    print("SUMMARY (per track, across runs)")
    print("=" * 72)
    for (track, artist), track_samples in by_track.items():
        summary = summarize(track_samples)
        print(f"\n{track!r} — {artist!r}")
        for key, value in summary.items():
            print(f"    {key:18} {value}")
        if summary.get("empty_runs"):
            print(
                f"    !! {summary['empty_runs']}/{summary['runs']} runs returned "
                f"nothing — variance, not necessarily a pipeline failure"
            )

    ladder = logs.ladder_attempts(since="30m")
    if ladder:
        print("\n" + "=" * 72)
        print("RECENT LADDER ATTEMPTS (from rec pulls, last 30m)")
        print("=" * 72)
        ratios = [a.ratio for a in ladder]
        print(f"  attempts     {len(ladder)}")
        print(f"  ratio min    {min(ratios):.2f}")
        print(f"  ratio max    {max(ratios):.2f}")
        print(f"  ratio median {statistics.median(ratios):.2f}")
        print(f"  word counts  {sorted({a.word_count for a in ladder})}")
        print("\n  Suggested threshold: at or just below the median of rungs")
        print("  that produced a usable download. Too high and every track")
        print("  walks all 5 rungs; too low and the first rung always wins.")
    else:
        print("\n(no rec-pull ladder attempts in the logs — enable a category")
        print(" and run a pull to collect ladder data)")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "samples": samples,
                    "summaries": {
                        f"{t} | {a}": summarize(s) for (t, a), s in by_track.items()
                    },
                    "ladder": [vars(a) for a in ladder],
                },
                indent=2,
            )
        )
        print(f"\nRaw samples written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
