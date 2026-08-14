"""
Turn a scorecard into an answer.

The user's question is not "did the tests pass". It is: *what fraction of the
time does each part of this pipeline actually work, and where does it break?*
So this module never reports a single bottom-line number. It reports, for
every one of the thirteen stages:

    tested and worked   |   tested and broke   |   never got far enough

That third column is the one that matters most and the one a normal test
report throws away. Twenty symptoms with one upstream cause look like twenty
problems until you can see that nineteen of them were never actually reached.

Two outputs, from the same data:

- `summary.json` — machine-readable, consumed by the visual scorecard page
- `report.md`    — human-readable, ranked worst-first

Run it standalone against any finished (or half-finished) run:

    python3 tests/live/report.py live-artifacts/<run>/
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.live.probes.contract import STAGE_ORDER, Scorecard, Stage, Verdict
from tests.live.scenarios import (
    FAIL_EXTERNAL,
    SCENARIOS,
    SCENARIOS_BY_ID,
    SKIP_PREFIXES,
    tier_weight,
)

STAGE_TITLES: dict[str, str] = {
    Stage.S1_SEARCH_ACCEPTED.value: "search accepted",
    Stage.S2_SEARCH_COMPLETED.value: "search completed",
    Stage.S3_RESULTS_RELEVANT.value: "results are the right track",
    Stage.S4_QUEUE_ACCEPTED.value: "queue accepted",
    Stage.S5_TRANSFER_COMPLETED.value: "transfer completed",
    Stage.S6_FILE_ON_DISK.value: "file on disk",
    Stage.S7_BEETS_IMPORT.value: "beets imported it",
    Stage.S8_TAGS_CORRECT.value: "tags correct",
    Stage.S9_PLACEMENT_CORRECT.value: "placed correctly",
    Stage.S10_DEDUP_CORRECT.value: "exactly one copy",
    Stage.S11_NAVIDROME_INDEXED.value: "Navidrome indexed it",
    Stage.S12_PLAYLIST_CORRECT.value: "playlist correct",
    Stage.S13_USER_CAN_FIND.value: "user can find it",
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(row: dict) -> str:
    """One row -> one of seven outcomes.

    The prefixes come from `scenarios.py`, which writes them deliberately so
    that a skip's *reason* survives into the report. An unprefixed skip is a
    downstream skip: the journey died earlier and never reached this stage.
    """
    verdict = row.get("verdict")
    detail = row.get("detail") or ""
    if verdict == Verdict.ERROR.value:
        return "harness_error"
    if verdict == Verdict.PASS.value:
        return "pass"
    if verdict == Verdict.FAIL.value:
        return "fail_external" if detail.startswith(FAIL_EXTERNAL) else "fail"
    for prefix, name in SKIP_PREFIXES.items():
        if detail.startswith(prefix):
            return f"skip_{name}"
    return "skip_downstream"


#: Outcomes that mean "we actually measured this stage".
MEASURED = {"pass", "fail", "fail_external"}
#: Outcomes that mean "musica got it wrong" (external failures excluded —
#: an empty Soulseek is not a defect in this codebase).
BLAMED = {"fail"}


def journey_key(row: dict) -> tuple:
    """A journey is one (scenario, run, track) triple."""
    return (row.get("scenario"), row.get("run_id"), row.get("track"))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class StageStats:
    stage: str
    title: str
    passed: int = 0
    failed: int = 0
    failed_external: int = 0
    skipped_downstream: int = 0
    skipped_budget: int = 0
    skipped_precondition: int = 0
    skipped_not_applicable: int = 0
    harness_errors: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def measured(self) -> int:
        return self.passed + self.failed + self.failed_external

    @property
    def pass_ratio(self) -> float | None:
        """Of the times we actually reached this stage, how often it worked.

        `None` when the stage was never reached — which is *not* 0%, and
        printing it as 0% would be a lie about a stage nobody tested.
        """
        if not self.measured:
            return None
        return self.passed / self.measured

    @property
    def blame_ratio(self) -> float | None:
        """Pass ratio excluding failures blamed on the network."""
        attributable = self.passed + self.failed
        if not attributable:
            return None
        return self.passed / attributable

    def latency(self, pct: float) -> float | None:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        if len(ordered) == 1:
            return ordered[0]
        idx = min(len(ordered) - 1, round(pct * (len(ordered) - 1)))
        return ordered[idx]

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "title": self.title,
            "passed": self.passed,
            "failed": self.failed,
            "failed_external": self.failed_external,
            "never_reached": self.skipped_downstream,
            "skipped_budget": self.skipped_budget,
            "skipped_precondition": self.skipped_precondition,
            "not_applicable": self.skipped_not_applicable,
            "harness_errors": self.harness_errors,
            "measured": self.measured,
            "pass_ratio": self.pass_ratio,
            "blame_ratio": self.blame_ratio,
            "latency_p50": self.latency(0.5),
            "latency_p95": self.latency(0.95),
            "latency_max": max(self.latencies) if self.latencies else None,
        }


_COUNTER_FIELD = {
    "pass": "passed",
    "fail": "failed",
    "fail_external": "failed_external",
    "skip_downstream": "skipped_downstream",
    "skip_budget": "skipped_budget",
    "skip_precondition": "skipped_precondition",
    "skip_not_applicable": "skipped_not_applicable",
    "harness_error": "harness_errors",
}


def stage_stats(rows: Iterable[dict]) -> dict[str, StageStats]:
    stats = {
        s.value: StageStats(s.value, STAGE_TITLES.get(s.value, s.value))
        for s in STAGE_ORDER
    }
    for row in rows:
        stage = row.get("stage")
        if stage not in stats:
            continue
        st = stats[stage]
        setattr(
            st,
            _COUNTER_FIELD[classify(row)],
            getattr(st, _COUNTER_FIELD[classify(row)]) + 1,
        )
        latency = row.get("latency_s")
        if latency is not None and classify(row) in MEASURED:
            st.latencies.append(float(latency))
    return stats


def funnel(rows: Iterable[dict]) -> list[dict]:
    """How many journeys reached each stage, in order.

    A journey "reached" a stage if that stage was measured for it. Stages the
    scenario never intended to exercise (`n/a`) are excluded from its
    denominator entirely — U9 not doing a search is not U9 failing a search.
    """
    rows = list(rows)
    reached: dict[str, set[tuple]] = defaultdict(set)
    applicable: dict[str, set[tuple]] = defaultdict(set)
    for row in rows:
        stage, outcome, key = row.get("stage"), classify(row), journey_key(row)
        if outcome == "skip_not_applicable":
            continue
        applicable[stage].add(key)
        if outcome in MEASURED:
            reached[stage].add(key)

    out = []
    for stage in STAGE_ORDER:
        total = len(applicable.get(stage.value, ()))
        got = len(reached.get(stage.value, ()))
        out.append(
            {
                "stage": stage.value,
                "title": STAGE_TITLES.get(stage.value, stage.value),
                "journeys_applicable": total,
                "journeys_reached": got,
                "reach_ratio": (got / total) if total else None,
            }
        )
    return out


def tier_breakdown(rows: Iterable[dict]) -> dict[str, dict]:
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "failed_external": 0, "never_reached": 0}
    )
    for row in rows:
        tier = row.get("tier") or "untiered"
        outcome = classify(row)
        if outcome == "pass":
            buckets[tier]["passed"] += 1
        elif outcome == "fail":
            buckets[tier]["failed"] += 1
        elif outcome == "fail_external":
            buckets[tier]["failed_external"] += 1
        elif outcome == "skip_downstream":
            buckets[tier]["never_reached"] += 1
    out = {}
    for tier, counts in buckets.items():
        measured = counts["passed"] + counts["failed"] + counts["failed_external"]
        out[tier] = {
            **counts,
            "measured": measured,
            "pass_ratio": (counts["passed"] / measured) if measured else None,
            "weight": tier_weight(tier if tier != "untiered" else None),
        }
    return out


def defects(rows: Iterable[dict], limit: int = 40) -> list[dict]:
    """Failures grouped into distinct defects, ranked worst-first.

    Ranking is `occurrences x tier weight`, so a defect that only ever bites
    rare tracks outranks a more frequent one that only bites popular ones —
    the user's explicit weighting. External failures are listed separately at
    the bottom rather than mixed in, because "Soulseek had nothing" is not
    something anyone can fix in this repo.
    """
    groups: dict[tuple, dict] = {}
    for row in rows:
        outcome = classify(row)
        if outcome not in ("fail", "fail_external"):
            continue
        detail = (row.get("detail") or "").strip()
        signature = detail.split(" — ")[0].split(":")[0][:110] or "(no detail)"
        key = (row.get("stage"), signature, outcome)
        entry = groups.setdefault(
            key,
            {
                "stage": row.get("stage"),
                "stage_title": STAGE_TITLES.get(row.get("stage"), row.get("stage")),
                "signature": signature,
                "external": outcome == "fail_external",
                "occurrences": 0,
                "score": 0.0,
                "tracks": Counter(),
                "scenarios": Counter(),
                "examples": [],
            },
        )
        entry["occurrences"] += 1
        entry["score"] += tier_weight(row.get("tier"))
        if row.get("track"):
            entry["tracks"][row["track"]] += 1
        if row.get("scenario"):
            entry["scenarios"][row["scenario"]] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(
                {
                    "detail": detail,
                    "track": row.get("track"),
                    "scenario": row.get("scenario"),
                    "run_id": row.get("run_id"),
                    "evidence": row.get("evidence") or {},
                }
            )

    ranked = sorted(
        groups.values(), key=lambda e: (e["external"], -e["score"], -e["occurrences"])
    )
    for entry in ranked:
        entry["tracks"] = dict(entry["tracks"])
        entry["scenarios"] = dict(entry["scenarios"])
        entry["score"] = round(entry["score"], 2)
    return ranked[:limit]


def scenario_coverage(rows: Iterable[dict]) -> list[dict]:
    """Which registered scenarios produced results, and which never ran.

    A scenario missing from the scorecard entirely is the most dangerous
    thing in a report — it looks like silence, reads like success.
    """
    seen: dict[str, set[tuple]] = defaultdict(set)
    outcomes: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        scenario = row.get("scenario")
        if not scenario:
            continue
        seen[scenario].add(journey_key(row))
        outcomes[scenario][classify(row)] += 1

    out = []
    for spec in SCENARIOS:
        journeys = seen.get(spec.id, set())
        counts = outcomes.get(spec.id, Counter())
        out.append(
            {
                "id": spec.id,
                "title": spec.title,
                "intent": spec.intent,
                "journeys": len(journeys),
                "ran": bool(journeys),
                "passed": counts.get("pass", 0),
                "failed": counts.get("fail", 0) + counts.get("fail_external", 0),
                "never_reached": counts.get("skip_downstream", 0),
            }
        )
    for scenario in sorted(set(seen) - set(SCENARIOS_BY_ID)):
        counts = outcomes[scenario]
        out.append(
            {
                "id": scenario,
                "title": "(unregistered)",
                "intent": "",
                "journeys": len(seen[scenario]),
                "ran": True,
                "passed": counts.get("pass", 0),
                "failed": counts.get("fail", 0) + counts.get("fail_external", 0),
                "never_reached": counts.get("skip_downstream", 0),
            }
        )
    return out


def build_summary(rows: list[dict]) -> dict:
    stats = stage_stats(rows)
    runs = sorted({r.get("run_id") for r in rows if r.get("run_id")})
    journeys = {journey_key(r) for r in rows}
    return {
        "totals": {
            "graded_stages": len(rows),
            "journeys": len(journeys),
            "runs": len(runs),
            "run_ids": runs,
            "outcomes": dict(Counter(classify(r) for r in rows)),
        },
        "stages": [stats[s.value].to_dict() for s in STAGE_ORDER],
        "funnel": funnel(rows),
        "tiers": tier_breakdown(rows),
        "scenarios": scenario_coverage(rows),
        "defects": defects(rows),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}s"


def render_markdown(summary: dict) -> str:
    t = summary["totals"]
    lines: list[str] = [
        "# Pipeline reality check",
        "",
        (f"{t['graded_stages']} graded stages across {t['journeys']} journeys "
        f"and {t['runs']} run(s)."),
        "",
        ("Every stage is reported three ways: **worked**, **broke**, and "
        "**never reached**. A stage that was never reached is not a pass — "
        "it is a question nobody got to ask."),
        "",
        "## Stage scorecard",
        "",
        "| Stage | Worked | Broke | Network | Never reached | Pass rate | p50 | p95 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for st in summary["stages"]:
        lines.append(
            f"| {st['stage'][:3]} {st['title']} | {st['passed']} | {st['failed']} | "
            f"{st['failed_external']} | {st['never_reached']} | "
            f"{_pct(st['pass_ratio'])} | {_secs(st['latency_p50'])} | "
            f"{_secs(st['latency_p95'])} |"
        )

    lines += ["", "## Funnel — where journeys die", ""]
    for step in summary["funnel"]:
        if not step["journeys_applicable"]:
            continue
        bar = "#" * round((step["reach_ratio"] or 0) * 30)
        lines.append(
            f"- `{step['stage'][:3]}` {step['title']:<28} "
            f"{step['journeys_reached']:>3}/{step['journeys_applicable']:<3} "
            f"{bar} {_pct(step['reach_ratio'])}"
        )

    lines += [
        "",
        "## By tier",
        "",
        "| Tier | Worked | Broke | Network | Pass rate |",
        "|---|---|---|---|---|",
    ]
    for tier, counts in sorted(
        summary["tiers"].items(), key=lambda kv: -kv[1]["weight"]
    ):
        lines.append(
            f"| {tier} | {counts['passed']} | {counts['failed']} | "
            f"{counts['failed_external']} | {_pct(counts['pass_ratio'])} |"
        )

    lines += ["", "## Scenario coverage", ""]
    for sc in summary["scenarios"]:
        if not sc["ran"]:
            lines.append(f"- **{sc['id']}** — NEVER RAN. {sc['intent']}")
        else:
            lines.append(
                f"- {sc['id']} — {sc['journeys']} journey(s): "
                f"{sc['passed']} worked, {sc['failed']} broke, "
                f"{sc['never_reached']} never reached"
            )

    lines += ["", "## Ranked defects", ""]
    internal = [d for d in summary["defects"] if not d["external"]]
    external = [d for d in summary["defects"] if d["external"]]
    if not internal:
        lines.append("_No defects attributable to musica in this run._")
    for i, d in enumerate(internal, 1):
        lines += [
            f"### {i}. `{d['stage'][:3]}` {d['signature']}",
            "",
            f"- **Stage**: {d['stage_title']}",
            f"- **Occurrences**: {d['occurrences']} (weighted score {d['score']})",
            f"- **Scenarios**: {', '.join(d['scenarios']) or '—'}",
            f"- **Tracks**: {', '.join(list(d['tracks'])[:5]) or '—'}",
            f"- **Example**: {d['examples'][0]['detail'] if d['examples'] else '—'}",
            "",
        ]
    if external:
        lines += ["### Not musica's fault (network/availability)", ""]
        for d in external:
            lines.append(f"- `{d['stage'][:3]}` {d['signature']} — {d['occurrences']}x")

    lines += [
        "",
        "---",
        "",
        ("Every number above traces to `scorecard.jsonl` in this directory; "
        "per-test `timeline.jsonl` files carry the raw API/SSE/log record."),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate(artifact_dir: Path) -> dict:
    """Read `scorecard.jsonl`, write `summary.json` + `report.md`."""
    rows = Scorecard.load(artifact_dir / "scorecard.jsonl")
    summary = build_summary(rows)
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    (artifact_dir / "report.md").write_text(render_markdown(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    args = parser.parse_args(argv)
    if not (args.artifact_dir / "scorecard.jsonl").exists():
        print(f"no scorecard.jsonl in {args.artifact_dir}", file=sys.stderr)
        return 1
    summary = generate(args.artifact_dir)
    print(render_markdown(summary))
    print(f"\nwrote {args.artifact_dir / 'summary.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
