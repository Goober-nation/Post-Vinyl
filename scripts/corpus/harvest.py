"""Run every query in a plan against slskd and record everything (#34).

Standalone on purpose — stdlib + ``requests`` only — so it runs in a
throwaway container next to a dedicated slskd instance, not inside the app.

For each query it records, in ``<out>/raw/<qid>.json.gz``:

- the query, and every (seed, shape) in the plan that produced it
- a poll trace: (elapsed, responseCount, fileCount) through the search
- slskd's final search metadata, and why the search stopped
  (``complete`` | ``response_cap`` | ``deadline``)
- the full, unmodified response list from slskd

It does **no matching or filtering**: labelling and scoring run offline on
these files, so a bug in match code is fixed by re-scoring, never by
re-searching. A query whose file exists is skipped, so the run resumes after
a crash or Ctrl-C. Each run's settings and slskd version go to
``<out>/runs.jsonl``.

There is no early stop on a "good enough" response count: a search ends only
when slskd completes it, the response cap is hit, or the deadline passes —
then it is cancelled so slskd flushes its responses (the cancel-to-flush
pattern `SlskdSearch.drive_search` uses).

    python harvest.py --plan plan.json --out data/corpus \\
        --slskd http://slskd-corpus:5030 --per-minute 6
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

FORMAT_VERSION = 1


class Slskd:
    def __init__(self, base_url: str, api_key: str, session: Any = None) -> None:
        self.base = base_url.rstrip("/") + "/api/v0"
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def _check(self, resp: Any, what: str) -> Any:
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    def server_state(self) -> dict:
        return self._check(
            self.session.get(f"{self.base}/server", timeout=10), "server"
        )

    def version(self) -> Any:
        resp = self.session.get(f"{self.base}/application", timeout=10)
        return resp.json() if resp.status_code == 200 else None

    def start(self, query: str, response_limit: int, timeout_ms: int) -> str:
        body: dict[str, Any] = {
            "searchText": query,
            "responseLimit": response_limit,
            "searchTimeout": timeout_ms,
            # slskd's own default (100) would silently truncate a peer's
            # file list; keep everything a peer sends.
            "fileLimit": 100000,
        }
        data = self._check(
            self.session.post(f"{self.base}/searches", json=body, timeout=15), "start"
        )
        return data["id"]

    def meta(self, search_id: str) -> dict:
        return self._check(
            self.session.get(f"{self.base}/searches/{search_id}", timeout=10), "meta"
        )

    def cancel(self, search_id: str) -> None:
        self.session.put(f"{self.base}/searches/{search_id}", timeout=10)

    def responses(self, search_id: str) -> list:
        return self._check(
            self.session.get(f"{self.base}/searches/{search_id}/responses", timeout=30),
            "responses",
        )


def wait_logged_in(slskd: Slskd, max_wait: float, sleep: Any = time.sleep) -> dict:
    """Block until slskd is connected and logged in to Soulseek.

    A search fired while disconnected returns nothing and would be recorded
    as a real zero-result search — exactly the silent corruption the
    research must not have.
    """
    deadline = time.monotonic() + max_wait
    while True:
        try:
            state = slskd.server_state()
            if state.get("isConnected") and state.get("isLoggedIn"):
                return state
        except (requests.RequestException, RuntimeError) as e:
            state = {"error": str(e)}
        if time.monotonic() >= deadline:
            raise RuntimeError(f"slskd not logged in after {max_wait}s: {state}")
        sleep(5)


def run_search(
    slskd: Slskd,
    query: str,
    *,
    response_limit: int,
    max_wait: float,
    poll: float = 0.5,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict:
    """Drive one search to its end and return the full record (minus plan data)."""
    started_at = datetime.now(UTC).isoformat()
    t0 = clock()
    search_id = slskd.start(query, response_limit, int(max_wait * 1000) + 5000)
    trace: list[list[float]] = []
    stop_reason = None
    meta: dict = {}
    while True:
        elapsed = clock() - t0
        meta = slskd.meta(search_id)
        count = meta.get("responseCount", 0)
        trace.append([round(elapsed, 2), count, meta.get("fileCount", 0)])
        if meta.get("isComplete"):
            stop_reason = stop_reason or "complete"
            break
        if stop_reason is None and count >= response_limit:
            stop_reason = "response_cap"
            slskd.cancel(search_id)
        elif stop_reason is None and elapsed >= max_wait:
            stop_reason = "deadline"
            slskd.cancel(search_id)
        elif stop_reason is not None and elapsed >= max_wait + 8:
            break  # cancelled but never finalised; fetch what is there
        sleep(poll)

    # Responses can lag the cancel by a moment; poll briefly when the
    # metadata says there are some but the list is still empty.
    responses = slskd.responses(search_id)
    flush_deadline = clock() + 5
    while (
        not responses and meta.get("responseCount", 0) > 0 and clock() < flush_deadline
    ):
        sleep(0.5)
        responses = slskd.responses(search_id)

    return {
        "search_id": search_id,
        "started_at": started_at,
        "duration_s": round(clock() - t0, 2),
        "stop_reason": stop_reason,
        "meta": meta,
        "trace": trace,
        "responses": responses,
        # slskd's responseCount routinely runs a few above the list it
        # returns (seen live 2026-10-03: 300 vs 296), so a difference is
        # recorded for study, not treated as failure. Only an empty list
        # despite a non-zero count means the flush never landed.
        "response_count_mismatch": len(responses) != meta.get("responseCount", 0),
        "flush_failed": not responses and meta.get("responseCount", 0) > 0,
    }


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file


def main(argv: list[str] | None = None, slskd: Slskd | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--slskd", default=os.environ.get("SLSKD_URL", "http://slskd-corpus:5030")
    )
    ap.add_argument("--per-minute", type=float, default=6)
    ap.add_argument("--response-limit", type=int, default=300)
    ap.add_argument("--max-wait", type=float, default=10, help="search.wait_seconds")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text())
    slskd = slskd or Slskd(args.slskd, os.environ["SLSKD_API_KEY"])
    raw_dir = args.out / "raw"
    args.out.mkdir(parents=True, exist_ok=True)

    state = wait_logged_in(slskd, max_wait=300)
    run = {
        "format": FORMAT_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "plan": str(args.plan),
        "queries": len(plan["queries"]),
        "per_minute": args.per_minute,
        "response_limit": args.response_limit,
        "max_wait": args.max_wait,
        "slskd_version": slskd.version(),
        "server_state": state,
    }
    with (args.out / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(run) + "\n")
    (args.out / "seeds.json").write_text(json.dumps(plan["seeds"], ensure_ascii=False))

    interval = 60.0 / args.per_minute
    todo = [
        q for q in plan["queries"] if not (raw_dir / f"{q['qid']}.json.gz").exists()
    ]
    print(f"{len(todo)} of {len(plan['queries'])} queries to run", flush=True)
    last_start = 0.0
    for n, q in enumerate(todo, 1):
        for attempt in range(1, args.retries + 2):
            time.sleep(max(0.0, last_start + interval - time.monotonic()))
            last_start = time.monotonic()
            try:
                wait_logged_in(slskd, max_wait=600)
                result = run_search(
                    slskd,
                    q["query"],
                    response_limit=args.response_limit,
                    max_wait=args.max_wait,
                )
                # A disconnect mid-search yields a truncated or empty list
                # that looks real (live 2026-10-03: 'first' saved 0 peers
                # while slskd was dropping its connection) — discard it.
                after = slskd.server_state()
                if not (after.get("isConnected") and after.get("isLoggedIn")):
                    raise RuntimeError(f"connection lost during search: {after}")
                result["server_state_after"] = after
            except (requests.RequestException, RuntimeError, KeyError) as e:
                print(f"  attempt {attempt} failed: {e}", file=sys.stderr, flush=True)
                continue
            # Peers answered but no list came back: retry rather than record
            # a lost flush as if it were a real zero-result search.
            if result["flush_failed"] and attempt <= args.retries:
                print(
                    f"  attempt {attempt}: responses never flushed, retrying",
                    flush=True,
                )
                continue
            record = {"format": FORMAT_VERSION, "attempt": attempt, **q, **result}
            save(raw_dir / f"{q['qid']}.json.gz", record)
            files = sum(len(r.get("files", [])) for r in result["responses"])
            print(
                f"[{n}/{len(todo)}] {result['stop_reason']:<12} "
                f"{len(result['responses']):>3} peers {files:>5} files  {q['query']!r}",
                flush=True,
            )
            break
        else:
            print(
                f"[{n}/{len(todo)}] GAVE UP {q['query']!r}", file=sys.stderr, flush=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
