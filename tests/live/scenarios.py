"""
Full-pipeline scenarios — the ten end-to-end journeys a user actually takes.

The stage tests (`test_stages_search.py`, `test_stages_import.py`,
`test_stages_library.py`) grade one link of the chain at a time. These grade
the *whole chain*, because a pipeline can pass every stage in isolation and
still never carry a single track from "I typed a name" to "I can play it".
Both write into the same `Scorecard`, so the report can say "S7 passes 9/10
times on its own, but only 3/10 times inside a real journey" — which is the
difference between a component bug and a wiring bug.

    U1   manual search -> playable file
    U2   Deep Cuts pull -> playlist + downloads -> playable, and in the playlist
    U3   Comfort Zone pull (same shape)
    U4   Fresh Picks pull — availability is the measurement, not a pass/fail
    U5   the same track twice: same-tree and cross-tree duplicate handling
    U6   peer fails -> retry to an alternative peer -> still lands correctly
    U7   SIGKILL mid-transfer -> recovery with no partials, orphans, or lost rows
    U8   a stale beets row must not strand a fresh download
    U9   playlist deleted in Navidrome -> recreated only when there is something
         to put in it
    U10  several concurrent downloads -> no cross-contamination

Nothing in here is mocked and nothing here runs itself. `test_scenarios.py`
exposes them to pytest; `run_suite.py` is the only thing allowed to drive them
for real.

Reading a SKIP
--------------
`Verdict.SKIP` means "never reached", and the funnel in `report.py` depends on
that. But there are four different reasons a stage can go unreached, and
collapsing them makes the report lie in the user's favour:

    (no prefix)      an earlier stage failed — this is a real funnel death
    "n/a: "          the stage does not apply to this journey at all
                     (U1 has no playlist, so S12 is meaningless for it)
    "budget: "       we ran out of the real-download budget before getting here
    "precondition: " the environment could not be set up (recs disabled, no
                     ListenBrainz credentials, no peer would accept a queue)

`report.py` imports these prefixes and classifies accordingly, so "we never
got far enough to find out" never reads as "this works".

One more prefix, on FAIL rather than SKIP:

    "external: "     the stage failed for a reason outside musica — chiefly
                     "no peer on Soulseek has this file". Still a failure of
                     the journey, still counted in the funnel, but ranked as
                     an environment finding rather than a defect. U4 exists
                     almost entirely to measure this.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tests.live.corpus import CORPUS, TIER_WEIGHT, Tier, Track, tracks_in_run_order
from tests.live.harness import queue_first_available, viable_results, wait_until
from tests.live.probes.contract import (
    STAGE_ORDER,
    Scorecard,
    Stage,
    StageResult,
    Verdict,
)

# --- skip/fail classification prefixes (see the module docstring) -----------

SKIP_DOWNSTREAM = ""
SKIP_NOT_APPLICABLE = "n/a: "
SKIP_BUDGET = "budget: "
SKIP_PRECONDITION = "precondition: "
FAIL_EXTERNAL = "external: "

#: Every prefix `report.py` knows how to classify. Order matters: longest
#: match first would be equivalent here, but keeping them distinct-prefixed
#: means a plain `startswith` is unambiguous.
SKIP_PREFIXES: dict[str, str] = {
    SKIP_NOT_APPLICABLE: "not_applicable",
    SKIP_BUDGET: "budget",
    SKIP_PRECONDITION: "precondition",
}

#: Wall-clock ceilings. Generous on purpose: a live transfer from a slow peer
#: is not a failure, and a test that gives up early manufactures findings.
SEARCH_TIMEOUT = 200.0
TRANSFER_TIMEOUT = 600.0
IMPORT_TIMEOUT = 180.0
SCAN_TIMEOUT = 300.0
PULL_TIMEOUT = 900.0

#: Default number of real downloads the whole suite may spend. The user's
#: budget: ~40 downloads, 2-3 hours.
DEFAULT_DOWNLOAD_BUDGET = 40


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_pipeline_suite.py)
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Fold to comparable ASCII-ish lowercase words.

    `Björk` and `Bjork`, `Jóga` and `Joga`, `ALICE_` and `alice` all have to
    compare equal — half the AWKWARD and RARE tiers exist precisely because
    peers spell these differently from the way the user typed them.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.lower()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t]


def strip_parenthetical(title: str) -> str:
    """Drop `(feat. X)` / `(Remix)` clauses.

    A peer's filename rarely carries them, so requiring them would fail
    every AWKWARD entry for a reason that has nothing to do with musica.
    """
    return re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title).strip()


def _significant(words: Sequence[str]) -> list[str]:
    """Words worth matching on — 3+ chars, or all of them if none are."""
    long_enough = [w for w in words if len(w) >= 3]
    return long_enough or list(words)


def filename_matches(filename: str, track: Track) -> tuple[bool, str]:
    """Does this peer file plausibly hold the track that was asked for?

    Deliberately generous on the title (peers append `[Explicit]`, bitrates,
    track numbers) and strict-ish on the artist, because the failure mode
    that actually bites is *wrong artist, right title*.

    Returns (ok, reason); the reason is quoted verbatim in the report.
    """
    hay = normalize(filename)
    title_words = _significant(tokens(strip_parenthetical(track.title)))
    artist_words = _significant(tokens(track.expect_albumartist))

    if not title_words:
        return False, f"corpus title {track.title!r} normalises to nothing"

    hit = [w for w in title_words if w in hay]
    needed = max(1, math.ceil(0.6 * len(title_words)))
    if len(hit) < needed:
        return (
            False,
            f"title miss: {len(hit)}/{len(title_words)} words of "
            f"{track.title!r} in {filename!r}",
        )

    if artist_words and not any(w in hay for w in artist_words):
        return (
            False,
            f"artist miss: none of {artist_words} in {filename!r} "
            f"(right title, wrong artist is the failure that matters)",
        )
    return True, f"matched {track.artist} - {track.title}"


def relevance(results: Sequence[dict], track: Track) -> tuple[list[dict], list[str]]:
    """Split a candidate set into (matching results, reasons for the rest)."""
    matched: list[dict] = []
    reasons: list[str] = []
    for r in results:
        ok, why = filename_matches(str(r.get("filename", "")), track)
        if ok:
            matched.append(r)
        else:
            reasons.append(why)
    return matched, reasons


def audio_extension(filename: str) -> str:
    return Path(str(filename).replace("\\", "/")).suffix.lower()


def basename(filename: str) -> str:
    """slskd reports Windows-style peer paths; `Path` alone won't split them."""
    return str(filename).replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Download budget — shared across pytest processes
# ---------------------------------------------------------------------------


class DownloadBudget:
    """A hard ceiling on real downloads, survivable across processes.

    `run_suite.py` launches pytest several times; a per-process counter would
    reset each time and quietly blow through the user's 40-download budget.
    Backed by a small JSON file with an exclusive lock when `path` is given,
    in-memory when it isn't (which is what the unit tests use).

    Exhaustion is *recorded*, not raised: a journey that stops because the
    budget ran out is a `SKIP_BUDGET`, which the report shows as unmeasured
    rather than as a failure. That distinction is the whole point.
    """

    def __init__(self, total: int = DEFAULT_DOWNLOAD_BUDGET, path: Path | None = None):
        self.total = int(total)
        self.path = path
        self._spent = 0
        if self.path is not None and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write({"total": self.total, "spent": 0})

    # -- file plumbing -----------------------------------------------------

    def _read(self) -> dict:
        if self.path is None:
            return {"total": self.total, "spent": self._spent}
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"total": self.total, "spent": 0}

    def _write(self, state: dict) -> None:
        if self.path is None:
            self._spent = int(state["spent"])
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if self.path is None:
            yield
            return
        lock = self.path.with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "w") as fh:
            try:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX)
            except (ImportError, OSError):  # pragma: no cover - non-POSIX
                pass
            yield

    # -- api ---------------------------------------------------------------

    def take(self, n: int = 1) -> bool:
        """Reserve `n` downloads. False (and nothing reserved) if short."""
        with self._locked():
            state = self._read()
            if state["spent"] + n > state.get("total", self.total):
                return False
            state["spent"] += n
            self._write(state)
            return True

    def spent(self) -> int:
        return int(self._read()["spent"])

    def remaining(self) -> int:
        state = self._read()
        return max(0, int(state.get("total", self.total)) - int(state["spent"]))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DownloadBudget(spent={self.spent()}, total={self.total})"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class StageAborted(Exception):
    """Control flow: this journey cannot continue past the stage that failed.

    Caught by `run_scenario`. Never surfaces to pytest as an error, because a
    journey dying at S5 is a *finding*, not a broken test.
    """

    def __init__(self, stage: Stage, why: str):
        super().__init__(f"{stage.value}: {why}")
        self.stage = stage
        self.why = why


@dataclass
class Step:
    """Mutable result holder handed to the body of `Grader.step`."""

    ok: bool = False
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def fail(self, detail: str, **evidence: Any) -> None:
        self.ok = False
        self.detail = detail
        self.evidence.update(evidence)

    def pass_(self, detail: str = "", **evidence: Any) -> None:
        self.ok = True
        if detail:
            self.detail = detail
        self.evidence.update(evidence)


class Grader:
    """Records one journey's stages into the shared scorecard.

    A journey is identified by (scenario, run_id, track) — that triple is what
    `report.py` walks to build the funnel, so every result a Grader emits
    carries all three.

    Stages outside `stages` are recorded as `n/a` SKIPs the moment the Grader
    is built. Doing it up front rather than at the end means a journey that
    dies early still declares what it was never going to measure, so the
    funnel never blames a playlist stage on a search failure.
    """

    def __init__(
        self,
        scorecard: Scorecard,
        scenario: str,
        run_id: str,
        *,
        track: Track | None = None,
        stages: Sequence[Stage] | None = None,
        timeline: Any = None,
    ) -> None:
        self.scorecard = scorecard
        self.scenario = scenario
        self.run_id = run_id
        self.track = track
        self.stages: tuple[Stage, ...] = tuple(stages or STAGE_ORDER)
        self.timeline = timeline
        self.recorded: dict[Stage, Verdict] = {}
        for stage in STAGE_ORDER:
            if stage not in self.stages:
                self._record(
                    stage,
                    Verdict.SKIP,
                    f"{SKIP_NOT_APPLICABLE}{scenario} does not exercise this stage",
                )

    # -- plumbing ----------------------------------------------------------

    @property
    def track_name(self) -> str | None:
        if self.track is None:
            return None
        return f"{self.track.artist} - {self.track.title}"

    @property
    def tier(self) -> str | None:
        return self.track.tier.value if self.track is not None else None

    def _record(
        self,
        stage: Stage,
        verdict: Verdict,
        detail: str,
        *,
        latency_s: float | None = None,
        evidence: dict | None = None,
    ) -> StageResult:
        result = self.scorecard.record(
            StageResult(
                stage=stage,
                verdict=verdict,
                scenario=self.scenario,
                run_id=self.run_id,
                track=self.track_name,
                tier=self.tier,
                latency_s=latency_s,
                detail=detail,
                evidence=evidence or {},
            )
        )
        self.recorded[stage] = verdict
        return result

    def _skip_tail(self, first: Stage, why: str) -> None:
        """Mark `first` and every later *applicable* stage as never-reached.

        `Scorecard.skip_from` does the same walk but over all thirteen
        stages; a journey that legitimately doesn't have (say) a playlist
        would then record a downstream SKIP for a stage it already declared
        as `n/a`, and the funnel would double-count it.
        """
        start = STAGE_ORDER.index(first)
        for later in STAGE_ORDER[start:]:
            if later in self.stages and later not in self.recorded:
                self._record(later, Verdict.SKIP, why)

    def _next_stage(self, stage: Stage) -> Stage | None:
        index = STAGE_ORDER.index(stage) + 1
        return STAGE_ORDER[index] if index < len(STAGE_ORDER) else None

    # -- public grading api ------------------------------------------------

    @contextlib.contextmanager
    def step(self, stage: Stage, *, fatal: bool = True) -> Iterator[Step]:
        """Time and grade one stage.

        The body sets `step.ok` / `step.detail` / `step.evidence`. An
        exception inside the body is an ERROR (the probe broke, not musica),
        which is deliberately *not* the same verdict as a FAIL.
        """
        step = Step()
        started = time.monotonic()
        try:
            yield step
        except StageAborted:
            raise
        except Exception as exc:  # probe/harness fault, not a musica finding
            elapsed = time.monotonic() - started
            self._record(
                stage,
                Verdict.ERROR,
                f"{type(exc).__name__}: {exc}",
                latency_s=round(elapsed, 3),
                evidence=step.evidence,
            )
            nxt = self._next_stage(stage)
            if nxt is not None:
                self._skip_tail(nxt, f"{stage.value} errored")
            raise StageAborted(stage, f"{type(exc).__name__}: {exc}") from exc

        elapsed = time.monotonic() - started
        self._record(
            stage,
            Verdict.PASS if step.ok else Verdict.FAIL,
            step.detail,
            latency_s=round(elapsed, 3),
            evidence=step.evidence,
        )
        if not step.ok and fatal:
            nxt = self._next_stage(stage)
            if nxt is not None:
                self._skip_tail(nxt, f"{stage.value} failed")
            raise StageAborted(stage, step.detail)

    def stop(self, first_unreached: Stage, why: str) -> None:
        """Abandon the journey without grading `first_unreached` as a failure.

        For budget exhaustion and unmet preconditions: nothing was measured,
        so nothing may be claimed. `why` must carry one of the SKIP_ prefixes.
        """
        self._skip_tail(first_unreached, why)
        raise StageAborted(first_unreached, why)

    def note(self, kind: str, **fields: Any) -> None:
        """Drop an observation into the timeline (not the scorecard).

        Used for measurements that aren't stage verdicts — SSE lag, retry
        counts, availability rates. `report.py` aggregates these by `kind`.
        """
        if self.timeline is not None:
            self.timeline.record(
                kind, scenario=self.scenario, run_id=self.run_id, **fields
            )


# ---------------------------------------------------------------------------
# Scenario context
# ---------------------------------------------------------------------------


class Probes(Protocol):
    """Structural type for the `probes` fixture (owned by the probes agent)."""

    navidrome: Any
    fs: Any
    tags: Any
    beets: Any
    lb: Any


@dataclass
class ScenarioContext:
    """Everything a scenario needs. Built by the `scenario_ctx` fixture."""

    stack: Any
    probes: Any
    scorecard: Scorecard
    run_id: str
    budget: DownloadBudget
    artifact_dir: Path | None = None

    @property
    def timeline(self) -> Any:
        return self.stack.timeline

    def grader(
        self,
        scenario: str,
        *,
        track: Track | None = None,
        stages: Sequence[Stage] | None = None,
    ) -> Grader:
        return Grader(
            self.scorecard,
            scenario,
            self.run_id,
            track=track,
            stages=stages,
            timeline=self.timeline,
        )


def current_run_id() -> str:
    """The id every stage of this process's run is filed under.

    `run_suite.py` sets `MUSICA_LIVE_RUN_ID` per repetition so results from
    the same cycle group together across the several pytest invocations it
    makes. A bare `pytest --live` gets a fresh one.
    """
    return os.environ.get("MUSICA_LIVE_RUN_ID") or f"adhoc-{uuid.uuid4().hex[:8]}"


def run_scenario(fn: Callable[..., None], *args: Any, **kwargs: Any) -> str | None:
    """Run a scenario, swallowing its `StageAborted`.

    Returns the abort reason, or None if the journey completed. A journey
    dying is data, not a test error — the scorecard already recorded exactly
    where and why.
    """
    try:
        fn(*args, **kwargs)
    except StageAborted as abort:
        return str(abort)
    return None


# ---------------------------------------------------------------------------
# Shared journey fragments
# ---------------------------------------------------------------------------

MANUAL_STAGES: tuple[Stage, ...] = tuple(
    s for s in STAGE_ORDER if s is not Stage.S12_PLAYLIST_CORRECT
)


def _config_paths(stack) -> dict:
    """Container-side music paths, straight from musica's own config.

    Hardcoding `/music/Discovery` here would silently rot the moment the
    user edits `paths.discovery_dir`, and the queue route's destination
    allowlist compares against the *configured* value.
    """
    body = stack.client.get_config()
    paths = body.get("paths", {}) if isinstance(body, dict) else {}
    music = paths.get("music_dir", "/music")
    return {
        "music": music,
        "downloads": f"{music}/{paths.get('download_dir', 'downloads')}",
        "searches": f"{music}/{paths.get('searches_dir', 'Searches')}",
        "discovery": f"{music}/{paths.get('discovery_dir', 'Discovery')}",
        "library": f"{music}/{paths.get('library_dir', 'library')}",
    }


def stage_search(g: Grader, stack, track: Track) -> tuple[str, list[dict]]:
    """S1 -> S2 -> S3. Returns (search_id, relevant candidates)."""
    search_id = ""

    with g.step(Stage.S1_SEARCH_ACCEPTED) as s:
        call = stack.client.post(
            "/api/search", json={"query": track.title, "artist": track.artist}
        )
        body = call.body if isinstance(call.body, dict) else {}
        search_id = str(body.get("search_id") or "")
        row = stack.db.search_by_id(search_id) if search_id else None
        s.ok = call.status == 201 and bool(search_id) and row is not None
        s.detail = (
            f"POST /api/search -> {call.status}, search_id={search_id or 'none'}, "
            f"db row {'present' if row else 'MISSING'}"
        )
        s.evidence = {
            "status": call.status,
            "search_id": search_id,
            "query": track.title,
            "artist": track.artist,
            "db_row": bool(row),
        }

    results: list[dict] = []
    with g.step(Stage.S2_SEARCH_COMPLETED) as s:
        detail = stack.client.search_detail(search_id, timeout=SEARCH_TIMEOUT)
        results = list(detail.get("results") or [])
        expired = bool(detail.get("expired"))
        s.ok = not expired
        s.detail = (
            f"search returned {len(results)} candidate files, expired={expired}"
        )
        s.evidence = {
            "search_id": search_id,
            "result_count": len(results),
            "expired": expired,
            "peers": sorted({r.get("username", "") for r in results})[:10],
        }

    matched: list[dict] = []
    with g.step(Stage.S3_RESULTS_RELEVANT) as s:
        matched, reasons = relevance(results, track)
        s.ok = bool(matched)
        ratio = (len(matched) / len(results)) if results else 0.0
        if matched:
            s.detail = (
                f"{len(matched)}/{len(results)} candidates are actually "
                f"{track.artist} - {track.title} (precision {ratio:.2f})"
            )
        elif not results:
            s.detail = (
                f"{FAIL_EXTERNAL}no peer returned anything for "
                f"{track.artist} - {track.title} ({track.tier.value} tier) — "
                f"Soulseek availability, not a musica defect"
            )
        else:
            s.detail = (
                f"{len(results)} candidates, none of them the requested track. "
                f"First rejections: {reasons[:3]}"
            )
        s.evidence = {
            "search_id": search_id,
            "result_count": len(results),
            "relevant_count": len(matched),
            "precision": round(ratio, 3),
            "stresses": track.stresses,
            "rejections": reasons[:5],
        }
        g.note(
            "query_precision",
            track=g.track_name,
            tier=g.tier,
            results=len(results),
            relevant=len(matched),
            precision=round(ratio, 3),
        )

    return search_id, matched


def stage_queue(
    g: Grader,
    ctx: ScenarioContext,
    search_id: str,
    candidates: list[dict],
    *,
    destination: str | None = None,
) -> dict:
    """S4. Reserves budget first — an exhausted budget is a SKIP, not a FAIL."""
    if not ctx.budget.take(1):
        g.stop(
            Stage.S4_QUEUE_ACCEPTED,
            f"{SKIP_BUDGET}download budget exhausted "
            f"({ctx.budget.spent()}/{ctx.budget.total} used)",
        )

    queued: dict = {}
    with g.step(Stage.S4_QUEUE_ACCEPTED) as s:
        if destination is None:
            queued = queue_first_available(ctx.stack, search_id, candidates) or {}
        else:
            queued = _queue_to_destination(ctx.stack, search_id, candidates, destination)
        s.ok = bool(queued)
        if queued:
            row = ctx.stack.db.download_by_file(
                queued["username"], queued["filename"]
            )
            s.ok = row is not None
            s.detail = (
                f"queued {basename(queued['filename'])} from "
                f"{queued['username']}; downloads row "
                f"{'present' if row else 'MISSING (queue accepted but nothing persisted)'}"
            )
            s.evidence = {
                "username": queued["username"],
                "filename": queued["filename"],
                "destination": destination,
                "db_row": bool(row),
            }
        else:
            s.detail = (
                f"{FAIL_EXTERNAL}every candidate peer refused the queue "
                f"({len(candidates)} tried)"
            )
            s.evidence = {"candidates": len(candidates), "destination": destination}
    return queued


def _queue_to_destination(
    stack, search_id: str, candidates: list[dict], destination: str
) -> dict:
    """`queue_first_available` with a destination override.

    Kept separate rather than widening the harness helper: the destination
    is what makes musica treat a download as a rec (see
    `app/routes/downloads.py` — `is_rec = "discovery" in destination`), so
    it is load-bearing for U5's cross-tree case and nothing else.
    """
    for candidate in viable_results(candidates)[:6]:
        call = stack.client.queue(
            candidate["username"],
            [{"filename": candidate["filename"], "size": candidate["size"]}],
            search_id=search_id,
            destination=destination,
        )
        if call.status in (201, 207):
            stack.marker(
                "queued", username=candidate["username"], destination=destination
            )
            return candidate
        stack.marker(
            "queue_refused", username=candidate["username"], status=call.status
        )
    return {}


def stage_transfer(g: Grader, stack, queued: dict, *, timeout: float = TRANSFER_TIMEOUT) -> dict:
    """S5 — the row reaches `completed`, by whatever route (retry included)."""
    row: dict = {}
    with g.step(Stage.S5_TRANSFER_COMPLETED) as s:
        started = time.monotonic()

        def _row() -> dict | None:
            return stack.db.download_by_file(queued["username"], queued["filename"])

        def _terminal() -> bool:
            current = _row()
            return bool(current and current["state"] in ("completed", "failed", "cancelled"))

        try:
            wait_until(_terminal, timeout=timeout, interval=3.0, description="transfer")
        except TimeoutError:
            pass
        row = _row() or {}
        state = row.get("state", "missing")
        s.ok = state == "completed"
        s.detail = (
            f"transfer ended as {state!r} after {time.monotonic() - started:.0f}s "
            f"(retries {row.get('retry_count', '?')})"
        )
        if state in ("failed", "cancelled"):
            s.detail = f"{FAIL_EXTERNAL}{s.detail}" if state == "cancelled" else s.detail
        s.evidence = {
            "download_id": row.get("id"),
            "state": state,
            "retry_count": row.get("retry_count"),
            "username": queued["username"],
            "filename": queued["filename"],
        }
        g.note(
            "transfer_time",
            track=g.track_name,
            tier=g.tier,
            seconds=round(time.monotonic() - started, 1),
            state=state,
            retries=row.get("retry_count"),
        )
    return row


def stage_import(g: Grader, ctx: ScenarioContext, queued: dict, row: dict) -> dict:
    """S6 (file exists) and S7 (beets consumed it and put it somewhere)."""
    with g.step(Stage.S6_FILE_ON_DISK) as s:
        # DownloadMonitor writes the downloads row's state to 'completed' the
        # moment slskd reports the transfer done, then hands off to beets —
        # a subprocess that can run 5-20s (see S7 latency stats) — to tag,
        # rename and move the file. That write and the file actually landing
        # on disk are not the same instant: the file may sit in the download
        # tree pre-import, be mid-move inside beets, or (in the pre-import
        # window) not be findable by name/tags anywhere yet. Checking once,
        # immediately, races that window and reports "file not on disk" for
        # what is really just "beets hasn't finished". So: poll for up to
        # IMPORT_TIMEOUT, same ceiling S7 already waits on, before failing.
        # Before beets runs, the file sits in the download tree; after, it is
        # gone from there. Either is fine for S6 — what is not fine is
        # neither: nothing on disk anywhere, size zero, or a `.part` remnant.
        def _scan():
            audit = ctx.probes.fs.audit()
            found = ctx.probes.fs.find_by_title(strip_parenthetical(queued["filename"]))
            name = basename(queued["filename"])
            anywhere = [p for p in ctx.probes.fs.snapshot() if p.name == name]
            candidates = list(found) + anywhere
            sized = [p for p in candidates if p.exists() and p.stat().st_size > 0]
            current = ctx.stack.db.download_by_file(
                queued["username"], queued["filename"]
            )
            return audit, name, sized, bool(current and current.get("file_moved"))

        audit, name, sized, file_moved = _scan()
        if not sized and not file_moved and not audit.partial_files:
            try:
                wait_until(
                    lambda: _scan()[2] or _scan()[3],
                    timeout=IMPORT_TIMEOUT,
                    interval=3.0,
                    description="file on disk",
                )
            except TimeoutError:
                pass
            audit, name, sized, file_moved = _scan()
        s.ok = bool(sized) or file_moved
        s.detail = (
            f"{len(sized)} non-empty file(s) matching {name!r} on disk; "
            f"{len(audit.partial_files)} partial file(s) in the tree"
        )
        s.evidence = {
            "matches": [str(p) for p in sized[:5]],
            "partial_files": [str(p) for p in audit.partial_files[:5]],
            "expected_size": queued.get("size"),
        }
        if audit.partial_files:
            s.ok = False
            s.detail = f"partial files present: {audit.partial_files[:3]}"

    imported: dict = {}
    with g.step(Stage.S7_BEETS_IMPORT) as s:
        download_id = row.get("id")

        def _imported() -> bool:
            current = ctx.stack.db.download_by_file(
                queued["username"], queued["filename"]
            )
            return bool(current and current.get("file_moved"))

        try:
            wait_until(
                _imported,
                timeout=IMPORT_TIMEOUT,
                interval=5.0,
                description="beets import",
            )
        except TimeoutError:
            pass
        imported = (
            ctx.stack.db.download_by_file(queued["username"], queued["filename"]) or {}
        )
        target = (imported.get("target_dir") or "").strip()
        moved = bool(imported.get("file_moved"))
        # `file_moved=1` with an empty target_dir is how DownloadMonitor
        # records "beets refused this as a duplicate and left it in place"
        # — terminal, but not an import.
        s.ok = moved and bool(target)
        if moved and not target:
            s.detail = (
                "beets declined the import as a duplicate and left the file in "
                "downloads/ (file_moved=1, target_dir empty)"
            )
        elif not moved:
            s.detail = "beets never imported this download (file_moved still 0)"
        else:
            s.detail = f"imported into {target}"
        s.evidence = {
            "download_id": download_id,
            "target_dir": target,
            "file_moved": moved,
            "import_unmatched": imported.get("import_unmatched"),
        }
    return imported


def stage_tags_and_placement(
    g: Grader, ctx: ScenarioContext, track: Track, imported: dict
) -> Path | None:
    """S8 (tags) and S9 (strict canonical placement)."""
    target_dir = (imported.get("target_dir") or "").strip()
    placed: Path | None = None

    with g.step(Stage.S8_TAGS_CORRECT) as s:
        candidates = ctx.probes.fs.find_by_title(track.title)
        in_target = [p for p in candidates if target_dir and target_dir in str(p)]
        pool = in_target or candidates
        if not pool:
            s.fail(
                f"no file on disk matches {track.title!r}, so its tags cannot "
                f"be read (beets reported target_dir={target_dir!r})",
                target_dir=target_dir,
            )
        else:
            placed = pool[0]
            ok, why = ctx.probes.tags.grade(placed, track)
            tags = ctx.probes.tags.read(placed)
            s.ok = ok
            s.detail = why
            s.evidence = {
                "path": str(placed),
                "albumartist": tags.albumartist,
                "artist": tags.artist,
                "album": tags.album,
                "title": tags.title,
                "mb_trackid": tags.mb_trackid,
                "expected_albumartist": track.expect_albumartist,
                "expected_album": track.expect_album,
                "import_unmatched": imported.get("import_unmatched"),
            }

    with g.step(Stage.S9_PLACEMENT_CORRECT) as s:
        audit = ctx.probes.fs.audit()
        s.ok = audit.clean
        s.detail = (
            "tree is canonical"
            if audit.clean
            else (
                f"{len(audit.artist_folder_variants)} artist-folder variant(s), "
                f"{len(audit.stray_files)} stray, {len(audit.empty_dirs)} empty dir(s), "
                f"{len(audit.partial_files)} partial, "
                f"{len(audit.stranded_downloads)} stranded in downloads/"
            )
        )
        s.evidence = {
            "path": str(placed) if placed else None,
            "artist_folder_variants": {
                k: v for k, v in list(audit.artist_folder_variants.items())[:5]
            },
            "stray_files": [str(p) for p in audit.stray_files[:5]],
            "empty_dirs": [str(p) for p in audit.empty_dirs[:5]],
            "partial_files": [str(p) for p in audit.partial_files[:5]],
            "stranded_downloads": [str(p) for p in audit.stranded_downloads[:5]],
            "stranded_count": len(audit.stranded_downloads),
            "orphan_count": len(audit.partial_files),
        }
        g.note(
            "tree_audit",
            stranded=len(audit.stranded_downloads),
            partials=len(audit.partial_files),
            variants=len(audit.artist_folder_variants),
            strays=len(audit.stray_files),
            empty_dirs=len(audit.empty_dirs),
        )
    return placed


def stage_dedup(g: Grader, ctx: ScenarioContext, track: Track, *, expect_copies: int = 1) -> None:
    """S10 — exactly `expect_copies` playable copies, and beets agrees."""
    with g.step(Stage.S10_DEDUP_CORRECT) as s:
        copies = ctx.probes.fs.find_by_title(track.title)
        s.ok = len(copies) == expect_copies
        s.detail = (
            f"{len(copies)} cop{'y' if len(copies) == 1 else 'ies'} of "
            f"{track.title!r} on disk (expected {expect_copies})"
        )
        s.evidence = {"copies": [str(p) for p in copies[:6]], "expected": expect_copies}


def stage_navidrome(
    g: Grader, ctx: ScenarioContext, track: Track, *, scan: bool = True
) -> dict | None:
    """S11 (indexed) and S13 (the user can find it and the metadata is right)."""
    song: dict | None = None

    with g.step(Stage.S11_NAVIDROME_INDEXED) as s:
        started = time.monotonic()
        if scan:
            ctx.probes.navidrome.trigger_scan(wait=True, timeout=SCAN_TIMEOUT)
        song = ctx.probes.navidrome.find_song(track.title, track.expect_albumartist)
        s.ok = song is not None
        s.detail = (
            f"Navidrome {'indexed' if song else 'did NOT index'} "
            f"{track.artist} - {track.title} after a scan "
            f"({time.monotonic() - started:.0f}s)"
        )
        s.evidence = {"song": song, "song_count": ctx.probes.navidrome.song_count()}
        g.note(
            "scan_time",
            track=g.track_name,
            seconds=round(time.monotonic() - started, 1),
            indexed=song is not None,
        )

    with g.step(Stage.S13_USER_CAN_FIND) as s:
        found = ctx.probes.navidrome.find_song(track.title, track.expect_albumartist)
        if found is None:
            s.fail(f"search for {track.title!r} by {track.expect_albumartist!r} returns nothing")
        else:
            artist = str(found.get("artist") or found.get("albumArtist") or "")
            title = str(found.get("title") or "")
            artist_ok = normalize(track.expect_albumartist) in normalize(artist) or (
                normalize(artist) in normalize(track.artist)
            )
            title_ok = bool(filename_matches(title, track)[0]) or normalize(
                strip_parenthetical(track.title)
            ) in normalize(title)
            s.ok = artist_ok and title_ok
            s.detail = (
                f"Navidrome shows {artist!r} - {title!r}; expected "
                f"{track.expect_albumartist!r} - {track.title!r}"
            )
            s.evidence = {
                "found": found,
                "artist_ok": artist_ok,
                "title_ok": title_ok,
                "duration": found.get("duration"),
                "suffix": found.get("suffix"),
            }
    return song


# ---------------------------------------------------------------------------
# U1 — manual search all the way to playable
# ---------------------------------------------------------------------------


def u1_manual_pipeline(ctx: ScenarioContext, track: Track) -> None:
    """The journey the user takes most often, graded at every link.

    No playlist stage: a manual search has nothing to do with playlists, and
    recording S12 as a downstream SKIP here would make the funnel claim the
    journey died before a stage it never had.
    """
    g = ctx.grader("U1_manual_pipeline", track=track, stages=MANUAL_STAGES)
    ctx.stack.marker("scenario_start", scenario="U1", track=g.track_name)
    started = time.monotonic()

    search_id, candidates = stage_search(g, ctx.stack, track)
    queued = stage_queue(g, ctx, search_id, candidates)
    row = stage_transfer(g, ctx.stack, queued)
    imported = stage_import(g, ctx, queued, row)
    stage_tags_and_placement(g, ctx, track, imported)
    stage_dedup(g, ctx, track)
    stage_navidrome(g, ctx, track)

    g.note(
        "journey_complete",
        scenario="U1_manual_pipeline",
        track=g.track_name,
        tier=g.tier,
        seconds=round(time.monotonic() - started, 1),
    )


# ---------------------------------------------------------------------------
# U2 / U3 / U4 — recommendation pulls
# ---------------------------------------------------------------------------

REC_STAGES: tuple[Stage, ...] = STAGE_ORDER


def _set_only_category(stack, category: str) -> dict:
    """Enable exactly one rec category and return the previous settings.

    Pulls are all-categories-at-once, so measuring Deep Cuts on its own means
    turning the other two off for the duration. The caller restores.
    """
    status = stack.client.recs_status()
    previous = {
        "comfort_zone_enabled": bool(status.get("comfort_zone_enabled")),
        "fresh_picks_enabled": bool(status.get("fresh_picks_enabled")),
        "deep_cuts_enabled": bool(status.get("deep_cuts_enabled")),
    }
    stack.client.post(
        "/api/recs/settings",
        json={
            "comfort_zone_enabled": category == "comfort_zone",
            "fresh_picks_enabled": category == "fresh_picks",
            "deep_cuts_enabled": category == "deep_cuts",
        },
    )
    return previous


def _restore_categories(stack, previous: dict) -> None:
    with contextlib.suppress(Exception):
        stack.client.post("/api/recs/settings", json=previous)


def _wait_for_pull(stack, timeout: float = PULL_TIMEOUT) -> bool:
    """Block until the running pull finishes. False on timeout."""
    try:
        wait_until(
            lambda: not stack.client.recs_status().get("running"),
            timeout=timeout,
            interval=5.0,
            description="rec pull to finish",
        )
        return True
    except TimeoutError:
        return False


def _recs_from_source(stack, source: str, since: float) -> list[dict]:
    body = stack.client.recs_pending()
    items = body.get("items", []) if isinstance(body, dict) else []
    return [
        r
        for r in items
        if r.get("source") == source and float(r.get("created_at") or 0) >= since
    ]


def _rec_pull_journey(
    ctx: ScenarioContext,
    *,
    scenario: str,
    category: str,
    playlist_name_hint: str | None = None,
) -> None:
    """Shared body of U2/U3/U4 — the three pulls differ only in category.

    Graded as one journey per *pull*, not per track: the user's unit of
    experience here is "I pressed Pull and then...", and the per-track
    detail lands in the evidence.
    """
    stack = ctx.stack
    g = ctx.grader(scenario)
    stack.marker("scenario_start", scenario=scenario, category=category)
    started = time.monotonic()

    status = stack.client.recs_status()
    if not status.get("listenbrainz_enabled"):
        g.stop(
            Stage.S1_SEARCH_ACCEPTED,
            f"{SKIP_PRECONDITION}ListenBrainz has no credentials, so no "
            f"{category} pull is possible",
        )

    previous = _set_only_category(stack, category)
    pull_started_at = time.time()
    try:
        with g.step(Stage.S1_SEARCH_ACCEPTED) as s:
            call = stack.client.pull_recs()
            s.ok = call.status == 202
            s.detail = f"POST /api/recs/pull -> {call.status}"
            s.evidence = {"status": call.status, "body": call.body, "category": category}

        with g.step(Stage.S2_SEARCH_COMPLETED) as s:
            finished = _wait_for_pull(stack)
            recs = _recs_from_source(stack, category, pull_started_at)
            s.ok = finished and bool(recs)
            outcome = "finished" if finished else f"DID NOT finish in {PULL_TIMEOUT:.0f}s"
            s.detail = (
                f"pull {outcome}; {len(recs)} {category} recommendation "
                f"row(s) created"
            )
            if finished and not recs:
                s.detail = (
                    f"{FAIL_EXTERNAL}pull finished but ListenBrainz returned no "
                    f"{category} tracks to work with"
                )
            s.evidence = {
                "category": category,
                "rec_count": len(recs),
                "statuses": _count_by(recs, "status"),
                "seconds": round(time.monotonic() - started, 1),
            }
            g.note(
                "pull_time",
                category=category,
                seconds=round(time.monotonic() - started, 1),
                recs=len(recs),
            )

        recs = _recs_from_source(stack, category, pull_started_at)
        with g.step(Stage.S3_RESULTS_RELEVANT) as s:
            by_status = _count_by(recs, "status")
            searchable = len(recs)
            found = sum(
                by_status.get(k, 0) for k in ("in_library", "queued", "downloaded")
            )
            s.ok = found > 0
            availability = (found / searchable) if searchable else 0.0
            s.detail = (
                f"{found}/{searchable} {category} recommendations resolved to "
                f"something obtainable (availability {availability:.0%}); "
                f"statuses {by_status}"
            )
            if found == 0 and searchable:
                s.detail = (
                    f"{FAIL_EXTERNAL}0/{searchable} {category} recommendations "
                    f"were obtainable — every one failed to find a Soulseek "
                    f"candidate. Statuses: {by_status}"
                )
            s.evidence = {
                "category": category,
                "availability": round(availability, 3),
                "statuses": by_status,
                "tracks": [f"{r.get('artist')} - {r.get('track')}" for r in recs[:10]],
            }
            g.note(
                "rec_availability",
                category=category,
                requested=searchable,
                obtainable=found,
                availability=round(availability, 3),
                statuses=by_status,
            )

        queued_recs = [r for r in recs if r.get("status") in ("queued", "downloaded")]
        with g.step(Stage.S4_QUEUE_ACCEPTED) as s:
            s.ok = bool(queued_recs)
            s.detail = (
                f"{len(queued_recs)} {category} track(s) reached a download row"
            )
            if not queued_recs:
                s.detail = (
                    f"{FAIL_EXTERNAL}nothing from this {category} pull was "
                    f"queueable; the ones that matched were already in the library"
                )
            s.evidence = {
                "queued": [f"{r.get('artist')} - {r.get('track')}" for r in queued_recs[:10]],
                "download_ids": [r.get("download_id") for r in queued_recs[:10]],
            }

        with g.step(Stage.S5_TRANSFER_COMPLETED) as s:
            ids = {r.get("download_id") for r in queued_recs if r.get("download_id")}

            def _settled() -> bool:
                rows = [
                    row
                    for row in stack.db.downloads()
                    if row["id"] in ids or row.get("slskd_id") in ids
                ]
                return bool(rows) and all(
                    row["state"] in ("completed", "failed", "cancelled") for row in rows
                )

            with contextlib.suppress(TimeoutError):
                wait_until(_settled, timeout=TRANSFER_TIMEOUT, interval=5.0)
            rows = [
                row
                for row in stack.db.downloads()
                if row["id"] in ids or row.get("slskd_id") in ids
            ]
            done = [r for r in rows if r["state"] == "completed"]
            s.ok = bool(done)
            s.detail = (
                f"{len(done)}/{len(rows)} rec download(s) completed "
                f"({_count_by(rows, 'state')})"
            )
            s.evidence = {"states": _count_by(rows, "state"), "rows": len(rows)}

        completed_rows = [
            row
            for row in stack.db.downloads()
            if row["state"] == "completed" and row.get("is_rec_download")
        ]
        with g.step(Stage.S6_FILE_ON_DISK) as s:
            audit = ctx.probes.fs.audit()
            s.ok = not audit.partial_files
            s.detail = (
                f"{len(completed_rows)} completed rec download(s); "
                f"{len(audit.partial_files)} partial file(s) in the tree"
            )
            s.evidence = {"partial_files": [str(p) for p in audit.partial_files[:5]]}

        with g.step(Stage.S7_BEETS_IMPORT) as s:
            def _all_imported() -> bool:
                return all(
                    r.get("file_moved") for r in stack.db.downloads() if r["id"] in {c["id"] for c in completed_rows}
                )

            with contextlib.suppress(TimeoutError):
                wait_until(_all_imported, timeout=IMPORT_TIMEOUT, interval=5.0)
            after = [
                r
                for r in stack.db.downloads()
                if r["id"] in {c["id"] for c in completed_rows}
            ]
            imported = [r for r in after if r.get("file_moved") and (r.get("target_dir") or "").strip()]
            discovery = _config_paths(stack)["discovery"]
            wrong_tree = [
                r["target_dir"]
                for r in imported
                if discovery not in (r.get("target_dir") or "")
            ]
            s.ok = bool(imported) and not wrong_tree
            s.detail = (
                f"{len(imported)}/{len(after)} rec download(s) imported; "
                f"{len(wrong_tree)} landed outside {discovery}"
            )
            s.evidence = {
                "imported": len(imported),
                "expected_tree": discovery,
                "wrong_tree": wrong_tree[:5],
                "unmatched": sum(1 for r in after if r.get("import_unmatched")),
            }

        with g.step(Stage.S8_TAGS_CORRECT, fatal=False) as s:
            graded = []
            for row in stack.db.downloads():
                if not row.get("file_moved") or not row.get("is_rec_download"):
                    continue
                name = basename(row["filename"])
                for path in ctx.probes.fs.snapshot():
                    if path.name == name or normalize(path.stem) in normalize(name):
                        tags = ctx.probes.tags.read(path)
                        graded.append(
                            {
                                "path": str(path),
                                "albumartist": tags.albumartist,
                                "title": tags.title,
                                "mb_trackid": tags.mb_trackid,
                            }
                        )
                        break
            tagged = [t for t in graded if t["albumartist"] and t["title"]]
            s.ok = bool(graded) and len(tagged) == len(graded)
            s.detail = (
                f"{len(tagged)}/{len(graded)} imported rec file(s) carry both an "
                f"albumartist and a title"
            )
            s.evidence = {"files": graded[:8]}

        with g.step(Stage.S9_PLACEMENT_CORRECT) as s:
            audit = ctx.probes.fs.audit()
            s.ok = audit.clean
            s.detail = (
                "tree is canonical"
                if audit.clean
                else (
                    f"{len(audit.artist_folder_variants)} artist-folder variant(s), "
                    f"{len(audit.stranded_downloads)} stranded download(s)"
                )
            )
            s.evidence = {
                "artist_folder_variants": dict(
                    list(audit.artist_folder_variants.items())[:5]
                ),
                "stranded_downloads": [str(p) for p in audit.stranded_downloads[:5]],
                "stranded_count": len(audit.stranded_downloads),
            }

        with g.step(Stage.S10_DEDUP_CORRECT) as s:
            recon = ctx.probes.beets.reconcile("discovery")
            s.ok = recon.consistent
            s.detail = (
                f"discovery beets library: {len(recon.rows_without_files)} row(s) "
                f"with no file, {len(recon.files_without_rows)} file(s) with no row "
                f"({recon.total_rows} rows / {recon.total_files} files)"
            )
            s.evidence = {
                "rows_without_files": recon.rows_without_files[:5],
                "files_without_rows": [str(p) for p in recon.files_without_rows[:5]],
                "total_rows": recon.total_rows,
                "total_files": recon.total_files,
            }

        with g.step(Stage.S11_NAVIDROME_INDEXED) as s:
            ctx.probes.navidrome.trigger_scan(wait=True, timeout=SCAN_TIMEOUT)
            hits = []
            for rec in queued_recs:
                song = ctx.probes.navidrome.find_song(
                    str(rec.get("track") or ""), str(rec.get("artist") or "")
                )
                if song:
                    hits.append(song)
            s.ok = bool(hits)
            s.detail = f"{len(hits)}/{len(queued_recs)} downloaded rec(s) are in Navidrome"
            s.evidence = {"indexed": len(hits), "expected": len(queued_recs)}

        # P6.7-1: per-category playlist names. The category's own name is
        # preferred; the old single name is long gone, but callers may pass
        # a hint for a specific category.
        category_playlist_key = f"{category}_playlist_name"
        playlist_name = playlist_name_hint or status.get(category_playlist_key)
        if not playlist_name:
            s_fail = g.step(Stage.S12_PLAYLIST_CORRECT)
            s_fail.fail(
                f"no playlist name in status for category {category!r} "
                f"(key {category_playlist_key} missing from {sorted(status.keys())})"
            )
            return
        with g.step(Stage.S12_PLAYLIST_CORRECT) as s:
            playlists = ctx.probes.navidrome.list_playlists()
            match = next(
                (p for p in playlists if str(p.get("name", "")) == playlist_name), None
            )
            if match is None:
                s.fail(
                    f"no Navidrome playlist named {playlist_name!r} after a "
                    f"{category} pull that produced {len(recs)} recommendation(s)",
                    playlists=[p.get("name") for p in playlists],
                )
            else:
                songs = ctx.probes.navidrome.playlist_songs(
                    str(match.get("id") or match.get("playlist_id"))
                )
                titles = {normalize(str(s_.get("title", ""))) for s_ in songs}
                expected = [
                    r for r in recs if r.get("status") in ("in_library", "downloaded")
                ]
                present = [
                    r
                    for r in expected
                    if normalize(str(r.get("track", ""))) in titles
                ]
                s.ok = bool(expected) and len(present) == len(expected)
                s.detail = (
                    f"playlist {playlist_name!r} holds {len(songs)} track(s); "
                    f"{len(present)}/{len(expected)} of this pull's obtainable "
                    f"recommendations are in it"
                )
                s.evidence = {
                    "playlist": playlist_name,
                    "playlist_size": len(songs),
                    "expected": [f"{r.get('artist')} - {r.get('track')}" for r in expected[:10]],
                    "missing": [
                        f"{r.get('artist')} - {r.get('track')}"
                        for r in expected
                        if normalize(str(r.get("track", ""))) not in titles
                    ][:10],
                }

        with g.step(Stage.S13_USER_CAN_FIND) as s:
            findable = []
            for rec in recs:
                song = ctx.probes.navidrome.find_song(
                    str(rec.get("track") or ""), str(rec.get("artist") or "")
                )
                if song:
                    findable.append(f"{rec.get('artist')} - {rec.get('track')}")
            s.ok = bool(findable)
            s.detail = (
                f"{len(findable)}/{len(recs)} recommendation(s) from this pull are "
                f"findable by artist+title in Navidrome"
            )
            s.evidence = {"findable": findable[:10], "total": len(recs)}
    finally:
        _restore_categories(stack, previous)
        g.note(
            "journey_complete",
            scenario=scenario,
            category=category,
            seconds=round(time.monotonic() - started, 1),
        )


def _count_by(rows: Sequence[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return counts


def u2_deep_cuts(ctx: ScenarioContext) -> None:
    """Deep Cuts pull -> playlist + queued downloads -> playable and listed."""
    _rec_pull_journey(ctx, scenario="U2_deep_cuts", category="deep_cuts")


def u3_comfort_zone(ctx: ScenarioContext) -> None:
    """Comfort Zone pull — same shape as U2, different LB endpoint and pool."""
    _rec_pull_journey(ctx, scenario="U3_comfort_zone", category="comfort_zone")


def u4_fresh_picks(ctx: ScenarioContext) -> None:
    """Fresh Picks — known-hard, so the number is the point.

    A previous live pull went 5/5 fail: brand-new releases are largely absent
    from Soulseek. That is not a musica defect and grading it as a plain
    failure would bury a real finding under a fake one. So the pull runs
    exactly like U2/U3, and *in addition* this scenario measures availability
    directly against ListenBrainz's own feed — how many of the N newest
    releases anyone on Soulseek is sharing at all.
    """
    ctx.stack.marker("scenario_start", scenario="U4_fresh_picks")
    g = ctx.grader("U4_fresh_picks_availability", stages=(Stage.S3_RESULTS_RELEVANT,))

    with g.step(Stage.S3_RESULTS_RELEVANT, fatal=False) as s:
        try:
            picks = ctx.probes.lb.fresh_picks(days=7) or []
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            picks = []
            s.evidence["lb_error"] = f"{type(exc).__name__}: {exc}"

        probed = picks[:5]
        available: list[dict] = []
        empty: list[str] = []
        for pick in probed:
            artist = str(pick.get("artist_credit_name") or pick.get("artist") or "")
            title = str(pick.get("release_name") or pick.get("title") or "")
            if not (artist or title):
                continue
            job = ctx.stack.client.search(f"{artist} {title}".strip())
            detail = ctx.stack.client.search_detail(
                job["search_id"], timeout=SEARCH_TIMEOUT
            )
            results = detail.get("results") or []
            if results:
                available.append({"artist": artist, "title": title, "peers": len(results)})
            else:
                empty.append(f"{artist} - {title}")

        rate = (len(available) / len(probed)) if probed else 0.0
        s.ok = bool(probed) and rate > 0
        s.detail = (
            f"{FAIL_EXTERNAL if rate == 0 else ''}"
            f"{len(available)}/{len(probed)} of ListenBrainz's newest releases "
            f"have any Soulseek candidate at all (availability {rate:.0%})"
        )
        s.evidence = {
            "probed": len(probed),
            "available": available,
            "unavailable": empty,
            "availability": round(rate, 3),
        }
        g.note(
            "fresh_picks_availability",
            probed=len(probed),
            available=len(available),
            availability=round(rate, 3),
        )

    _rec_pull_journey(ctx, scenario="U4_fresh_picks", category="fresh_picks")


# ---------------------------------------------------------------------------
# U5 — duplicates, same tree and across trees
# ---------------------------------------------------------------------------

DEDUP_STAGES: tuple[Stage, ...] = (
    Stage.S1_SEARCH_ACCEPTED,
    Stage.S2_SEARCH_COMPLETED,
    Stage.S3_RESULTS_RELEVANT,
    Stage.S4_QUEUE_ACCEPTED,
    Stage.S5_TRANSFER_COMPLETED,
    Stage.S6_FILE_ON_DISK,
    Stage.S7_BEETS_IMPORT,
    Stage.S10_DEDUP_CORRECT,
)


def u5_duplicate_download(ctx: ScenarioContext, track: Track) -> None:
    """The same track downloaded twice — once into the same tree, once across.

    Same-tree is beets' own `duplicate_action: skip`. Cross-tree is P6.6-5's
    post-import `mb_trackid` check, which only exists because each profile
    has its own `library.db` and neither can see the other's. Both are graded
    here because both produce the same user-visible symptom — two copies of
    one song — and the user cannot tell them apart from the outside.
    """
    g = ctx.grader("U5_duplicate", track=track, stages=DEDUP_STAGES)
    ctx.stack.marker("scenario_start", scenario="U5", track=g.track_name)

    search_id, candidates = stage_search(g, ctx.stack, track)
    first = stage_queue(g, ctx, search_id, candidates)
    row = stage_transfer(g, ctx.stack, first)
    stage_import(g, ctx, first, row)

    baseline = ctx.probes.fs.find_by_title(track.title)
    paths = _config_paths(ctx.stack)

    # -- second copy, same tree -------------------------------------------
    same_tree_copies: list[Path] = list(baseline)
    if ctx.budget.take(1):
        second = _queue_to_destination(
            ctx.stack, search_id, candidates, paths["searches"]
        ) or _requeue_same(ctx.stack, search_id, first)
        if second:
            _await_terminal(ctx.stack, second)
            _await_import_attempt(ctx.stack, second)
            same_tree_copies = ctx.probes.fs.find_by_title(track.title)
    else:
        g.note("budget_skipped", scenario="U5", phase="same_tree")

    # -- third copy, cross tree (rec profile) -----------------------------
    cross_tree_copies: list[Path] = list(same_tree_copies)
    if ctx.budget.take(1):
        third = _queue_to_destination(
            ctx.stack, search_id, candidates, paths["discovery"]
        )
        if third:
            _await_terminal(ctx.stack, third)
            _await_import_attempt(ctx.stack, third)
            cross_tree_copies = ctx.probes.fs.find_by_title(track.title)
    else:
        g.note("budget_skipped", scenario="U5", phase="cross_tree")

    with g.step(Stage.S10_DEDUP_CORRECT) as s:
        copies = ctx.probes.fs.find_by_title(track.title)
        trees = {("Discovery" if paths["discovery"].split("/")[-1] in str(p) else "Searches") for p in copies}
        s.ok = len(copies) <= 1
        s.detail = (
            f"{len(copies)} copies of {track.title!r} survive after downloading "
            f"it three times (same tree twice, then across trees); expected 1"
        )
        s.evidence = {
            "copies": [str(p) for p in copies],
            "trees": sorted(trees),
            "after_first": [str(p) for p in baseline],
            "after_same_tree": [str(p) for p in same_tree_copies],
            "after_cross_tree": [str(p) for p in cross_tree_copies],
        }


def _requeue_same(stack, search_id: str, queued: dict) -> dict:
    """Queue the identical file from the identical peer a second time."""
    call = stack.client.queue(
        queued["username"],
        [{"filename": queued["filename"], "size": queued.get("size", 0)}],
        search_id=search_id,
    )
    return queued if call.status in (201, 207) else {}


def _await_terminal(stack, queued: dict, timeout: float = TRANSFER_TIMEOUT) -> dict:
    def _done() -> bool:
        row = stack.db.download_by_file(queued["username"], queued["filename"])
        return bool(row and row["state"] in ("completed", "failed", "cancelled"))

    with contextlib.suppress(TimeoutError):
        wait_until(_done, timeout=timeout, interval=3.0)
    return stack.db.download_by_file(queued["username"], queued["filename"]) or {}


def _await_import_attempt(stack, queued: dict, timeout: float = IMPORT_TIMEOUT) -> dict:
    """Wait for the monitor to have *tried* the import.

    `file_moved` flips to 1 for both a real import and a refused duplicate
    (the latter with an empty `target_dir`) — see
    `DownloadMonitor._import_via_beets`. Either is a completed attempt.
    """

    def _attempted() -> bool:
        row = stack.db.download_by_file(queued["username"], queued["filename"])
        return bool(row and row.get("file_moved"))

    with contextlib.suppress(TimeoutError):
        wait_until(_attempted, timeout=timeout, interval=5.0)
    return stack.db.download_by_file(queued["username"], queued["filename"]) or {}


# ---------------------------------------------------------------------------
# U6 — peer failure, retry to an alternative peer
# ---------------------------------------------------------------------------


def u6_peer_failure_retry(ctx: ScenarioContext, track: Track) -> None:
    """Kill the transfer at the peer and check the retry lands somewhere else.

    The negative half matters as much as the positive: retry must re-use the
    existing slskd search rather than starting a new one (AGENTS.md, "Never
    start a *new* search on retry"), and that has no API surface — the only
    observable is the count of `Search initiated:` lines in musica's log.
    """
    g = ctx.grader("U6_peer_retry", track=track, stages=MANUAL_STAGES)
    ctx.stack.marker("scenario_start", scenario="U6", track=g.track_name)

    search_id, candidates = stage_search(g, ctx.stack, track)
    if len({c["username"] for c in candidates}) < 2:
        g.stop(
            Stage.S4_QUEUE_ACCEPTED,
            f"{SKIP_PRECONDITION}only one peer has {track.title!r}; there is no "
            f"alternative peer to retry to",
        )

    queued = stage_queue(g, ctx, search_id, candidates)
    first_peer = queued["username"]

    # Wait for slskd to actually adopt it — cancelling a row musica only
    # holds as `pending:` exercises nothing, because retry keys off the
    # adopted row.
    def _adopted() -> bool:
        row = ctx.stack.db.download_by_file(first_peer, queued["filename"])
        return bool(row and row.get("slskd_id"))

    with contextlib.suppress(TimeoutError):
        wait_until(_adopted, timeout=180.0, interval=3.0)
    row = ctx.stack.db.download_by_file(first_peer, queued["filename"]) or {}

    searches_before = len(ctx.stack.logs.searches_issued(since="20m"))
    if row.get("slskd_id") and row.get("state") in ("queued", "downloading"):
        ctx.stack.marker("failing_peer", username=first_peer)
        ctx.stack.slskd.cancel_transfer(first_peer, str(row["slskd_id"]))
    else:
        g.note(
            "retry_not_forced",
            reason=f"transfer was {row.get('state')!r} before it could be failed",
        )

    with g.step(Stage.S5_TRANSFER_COMPLETED) as s:
        name = basename(queued["filename"])

        def _landed() -> bool:
            return any(
                basename(r["filename"]) == name and r["state"] == "completed"
                for r in ctx.stack.db.downloads()
            )

        with contextlib.suppress(TimeoutError):
            wait_until(_landed, timeout=TRANSFER_TIMEOUT, interval=5.0)
        attempts = [
            r for r in ctx.stack.db.downloads() if basename(r["filename"]) == name
        ]
        completed = [r for r in attempts if r["state"] == "completed"]
        peers = {r["username"] for r in attempts}
        searches_after = len(ctx.stack.logs.searches_issued(since="20m"))
        re_searched = searches_after > searches_before

        s.ok = bool(completed) and not re_searched
        s.detail = (
            f"{len(attempts)} attempt(s) across {len(peers)} peer(s); "
            f"{len(completed)} completed; "
            f"{'a NEW slskd search fired during retry (it must not)' if re_searched else 'no new search fired'}"
        )
        s.evidence = {
            "first_peer": first_peer,
            "peers": sorted(peers),
            "states": _count_by(attempts, "state"),
            "searches_before": searches_before,
            "searches_after": searches_after,
            "re_searched": re_searched,
        }
        g.note(
            "retry_observed",
            track=g.track_name,
            attempts=len(attempts),
            peers=len(peers),
            re_searched=re_searched,
        )

    landed = next(
        (
            r
            for r in ctx.stack.db.downloads()
            if basename(r["filename"]) == basename(queued["filename"])
            and r["state"] == "completed"
        ),
        {},
    )
    final = {"username": landed.get("username", first_peer), "filename": landed.get("filename", queued["filename"]), "size": queued.get("size", 0)}
    imported = stage_import(g, ctx, final, landed)
    stage_tags_and_placement(g, ctx, track, imported)
    stage_dedup(g, ctx, track)
    stage_navidrome(g, ctx, track)


# ---------------------------------------------------------------------------
# U7 — SIGKILL mid-transfer
# ---------------------------------------------------------------------------


def u7_crash_recovery(ctx: ScenarioContext, track: Track) -> None:
    """Hard-kill musica while a transfer is live and audit what survived.

    `restart_musica(hard=True)` is a SIGKILL: no lifespan shutdown, no clean
    worker stop. Three things must hold afterwards — no partial file left for
    Navidrome to index, no orphaned download row, and no lost row (the
    transfer slskd is still carrying must still be musica's).
    """
    g = ctx.grader("U7_crash_recovery", track=track, stages=MANUAL_STAGES)
    ctx.stack.marker("scenario_start", scenario="U7", track=g.track_name)

    search_id, candidates = stage_search(g, ctx.stack, track)
    queued = stage_queue(g, ctx, search_id, candidates)

    def _live() -> bool:
        row = ctx.stack.db.download_by_file(queued["username"], queued["filename"])
        return bool(row and row.get("slskd_id") and row["state"] in ("queued", "downloading"))

    with contextlib.suppress(TimeoutError):
        wait_until(_live, timeout=180.0, interval=2.0)

    before = ctx.stack.db.download_by_file(queued["username"], queued["filename"]) or {}
    rows_before = len(ctx.stack.db.downloads())
    if before.get("state") not in ("queued", "downloading"):
        g.note(
            "crash_timing",
            note=f"transfer was already {before.get('state')!r} at kill time — "
            f"the crash lands after the transfer, not during it",
        )

    ctx.stack.marker("sigkill", state=before.get("state"))
    downtime = ctx.stack.restart_musica(hard=True)
    g.note("crash_downtime", seconds=round(downtime, 1))

    with g.step(Stage.S5_TRANSFER_COMPLETED) as s:
        def _settled() -> bool:
            row = ctx.stack.db.download_by_file(queued["username"], queued["filename"])
            return bool(row and row["state"] in ("completed", "failed", "cancelled"))

        with contextlib.suppress(TimeoutError):
            wait_until(_settled, timeout=TRANSFER_TIMEOUT, interval=5.0)
        after = ctx.stack.db.download_by_file(queued["username"], queued["filename"]) or {}
        rows_after = len(ctx.stack.db.downloads())
        orphans = ctx.stack.logs.count_lines("Download orphaned", since="10m")
        lost = rows_after < rows_before
        s.ok = after.get("state") == "completed" and not lost
        s.detail = (
            f"after SIGKILL ({downtime:.0f}s down) the row is {after.get('state')!r}; "
            f"rows {rows_before} -> {rows_after}; {orphans} orphan log line(s)"
        )
        s.evidence = {
            "state_before_kill": before.get("state"),
            "state_after": after.get("state"),
            "rows_before": rows_before,
            "rows_after": rows_after,
            "orphan_count": orphans,
            "downtime_s": round(downtime, 1),
        }
        g.note("crash_recovery", orphans=orphans, lost_rows=max(0, rows_before - rows_after))

    row = ctx.stack.db.download_by_file(queued["username"], queued["filename"]) or {}
    imported = stage_import(g, ctx, queued, row)
    stage_tags_and_placement(g, ctx, track, imported)
    stage_dedup(g, ctx, track)
    stage_navidrome(g, ctx, track)


# ---------------------------------------------------------------------------
# U8 — a stale beets row must not strand a new download
# ---------------------------------------------------------------------------

STALE_STAGES: tuple[Stage, ...] = DEDUP_STAGES


def u8_stale_beets_row(ctx: ScenarioContext, track: Track) -> None:
    """Delete a file behind beets' back, then download the same track again.

    This is the failure the reconciliation check was written for: beets still
    holds a library row, the file is gone, and `duplicate_action: skip` then
    refuses the *replacement* — the user asks for a track they no longer have
    and silently gets nothing.

    Only ever deletes a file this scenario itself just downloaded.
    """
    g = ctx.grader("U8_stale_beets_row", track=track, stages=STALE_STAGES)
    ctx.stack.marker("scenario_start", scenario="U8", track=g.track_name)

    search_id, candidates = stage_search(g, ctx.stack, track)
    first = stage_queue(g, ctx, search_id, candidates)
    row = stage_transfer(g, ctx.stack, first)
    imported = stage_import(g, ctx, first, row)

    placed: Path | None = None
    target = (imported.get("target_dir") or "").strip()
    for path in ctx.probes.fs.find_by_title(track.title):
        if not target or target.split("/")[-1] in str(path):
            placed = path
            break
    if placed is None or not placed.exists():
        g.stop(
            Stage.S10_DEDUP_CORRECT,
            f"{SKIP_PRECONDITION}the first copy never landed on disk, so there "
            f"is no row to make stale",
        )

    ctx.stack.marker("deleting_behind_beets", path=str(placed))
    placed.unlink()
    g.note("stale_row_created", path=str(placed))

    if not ctx.budget.take(1):
        g.stop(
            Stage.S10_DEDUP_CORRECT,
            f"{SKIP_BUDGET}no budget left for the replacement download",
        )

    second = _queue_to_destination(
        ctx.stack, search_id, candidates, _config_paths(ctx.stack)["searches"]
    ) or _requeue_same(ctx.stack, search_id, first)

    with g.step(Stage.S10_DEDUP_CORRECT) as s:
        recon_before = ctx.probes.beets.reconcile("searches")
        if second:
            _await_terminal(ctx.stack, second)
            _await_import_attempt(ctx.stack, second)
        copies = ctx.probes.fs.find_by_title(track.title)
        recon_after = ctx.probes.beets.reconcile("searches")
        s.ok = len(copies) == 1
        s.detail = (
            f"after deleting the only copy behind beets' back, re-downloading "
            f"left {len(copies)} file(s) on disk (expected 1 — a stale row must "
            f"not make the replacement look like a duplicate)"
        )
        s.evidence = {
            "deleted": str(placed),
            "copies": [str(p) for p in copies],
            "stale_rows_before": recon_before.rows_without_files[:5],
            "stale_rows_after": recon_after.rows_without_files[:5],
            "stale_row_count_before": len(recon_before.rows_without_files),
            "stale_row_count_after": len(recon_after.rows_without_files),
            "requeued": bool(second),
        }


# ---------------------------------------------------------------------------
# U9 — playlist deleted in Navidrome
# ---------------------------------------------------------------------------

PLAYLIST_STAGES: tuple[Stage, ...] = (Stage.S12_PLAYLIST_CORRECT,)


def u9_playlist_lifecycle(ctx: ScenarioContext) -> None:
    """Delete the playlist in Navidrome, pull again, and check the rule.

    The user's rule (P6.7-1 note): a deleted playlist is recreated **only
    when there is something to put in it**, not eagerly on the next pull.
    Both outcomes are gradable, and which one applies depends on what
    ListenBrainz happens to return — so the scenario decides the expectation
    *after* seeing the pull, from the recommendation rows, rather than
    assuming one.
    """
    g = ctx.grader("U9_playlist_lifecycle", stages=PLAYLIST_STAGES)
    stack = ctx.stack
    stack.marker("scenario_start", scenario="U9")

    status = stack.client.recs_status()
    # P6.7-1: playlists are per-category; U9 exercises the category that is
    # enabled in config (fall back through all three names).
    playlist_name = next(
        (
            str(status.get(k) or "")
            for k in (
                "comfort_zone_playlist_name",
                "fresh_picks_playlist_name",
                "deep_cuts_playlist_name",
            )
            if status.get(k)
        ),
        "",
    )
    if not status.get("listenbrainz_enabled"):
        g.stop(
            Stage.S12_PLAYLIST_CORRECT,
            f"{SKIP_PRECONDITION}ListenBrainz has no credentials, so no pull "
            f"can be triggered to test recreation",
        )

    existing = [
        p
        for p in ctx.probes.navidrome.list_playlists()
        if str(p.get("name", "")) == playlist_name
    ]
    for playlist in existing:
        ctx.probes.navidrome.delete_playlist(str(playlist.get("id") or playlist.get("playlist_id")))
    stack.marker("playlist_deleted", name=playlist_name, count=len(existing))

    pull_started_at = time.time()
    stack.client.pull_recs()
    finished = _wait_for_pull(stack)

    with g.step(Stage.S12_PLAYLIST_CORRECT) as s:
        body = stack.client.recs_pending()
        items = body.get("items", []) if isinstance(body, dict) else []
        fresh = [r for r in items if float(r.get("created_at") or 0) >= pull_started_at]
        addable = [r for r in fresh if r.get("status") in ("in_library", "downloaded")]
        after = [
            p
            for p in ctx.probes.navidrome.list_playlists()
            if str(p.get("name", "")) == playlist_name
        ]
        recreated = bool(after)

        if addable:
            songs = (
                ctx.probes.navidrome.playlist_songs(
                    str(after[0].get("id") or after[0].get("playlist_id"))
                )
                if recreated
                else []
            )
            s.ok = recreated and bool(songs)
            s.detail = (
                f"pull produced {len(addable)} addable track(s), so the deleted "
                f"playlist had to come back: recreated={recreated}, "
                f"holding {len(songs)} track(s)"
            )
            s.evidence = {
                "branch": "had_tracks_to_add",
                "addable": len(addable),
                "recreated": recreated,
                "songs": len(songs),
            }
        else:
            s.ok = not recreated
            s.detail = (
                f"pull produced nothing addable ({len(fresh)} rec row(s), none "
                f"in_library/downloaded), so the playlist must stay deleted: "
                f"recreated={recreated}"
            )
            s.evidence = {
                "branch": "nothing_to_add",
                "fresh_recs": len(fresh),
                "statuses": _count_by(fresh, "status"),
                "recreated": recreated,
            }
        s.evidence["pull_finished"] = finished
        s.evidence["deleted_count"] = len(existing)


# ---------------------------------------------------------------------------
# U10 — concurrency
# ---------------------------------------------------------------------------

CONCURRENCY_STAGES: tuple[Stage, ...] = (
    Stage.S4_QUEUE_ACCEPTED,
    Stage.S5_TRANSFER_COMPLETED,
    Stage.S6_FILE_ON_DISK,
    Stage.S7_BEETS_IMPORT,
    Stage.S8_TAGS_CORRECT,
    Stage.S9_PLACEMENT_CORRECT,
    Stage.S10_DEDUP_CORRECT,
)


def u10_concurrent_downloads(ctx: ScenarioContext, tracks: Sequence[Track]) -> None:
    """Several downloads in flight at once, each graded on landing correctly.

    The defect this exists for is cross-contamination: the retired
    `_move_file()` globbed for the first basename match, so two simultaneous
    downloads sharing a filename could move the wrong file. beets matches on
    the exact source path instead — this is the test that says whether that
    is true under real concurrency rather than in a unit test.
    """
    g = ctx.grader("U10_concurrent", stages=CONCURRENCY_STAGES)
    stack = ctx.stack
    stack.marker("scenario_start", scenario="U10", tracks=len(tracks))

    if not ctx.budget.take(len(tracks)):
        g.stop(
            Stage.S4_QUEUE_ACCEPTED,
            f"{SKIP_BUDGET}needs {len(tracks)} downloads, "
            f"{ctx.budget.remaining()} left in the budget",
        )

    queued: list[tuple[Track, dict]] = []
    with g.step(Stage.S4_QUEUE_ACCEPTED) as s:
        for track in tracks:
            job = stack.client.search(track.title, artist=track.artist)
            detail = stack.client.search_detail(job["search_id"], timeout=SEARCH_TIMEOUT)
            matched, _ = relevance(detail.get("results") or [], track)
            if not matched:
                continue
            picked = queue_first_available(stack, job["search_id"], matched)
            if picked:
                queued.append((track, picked))
        s.ok = len(queued) >= 2
        s.detail = (
            f"{len(queued)}/{len(tracks)} tracks queued back-to-back "
            f"(need at least 2 in flight for this to mean anything)"
        )
        s.evidence = {
            "queued": [f"{t.artist} - {t.title}" for t, _ in queued],
            "peers": [q["username"] for _, q in queued],
        }

    with g.step(Stage.S5_TRANSFER_COMPLETED) as s:
        def _all_settled() -> bool:
            return all(
                (stack.db.download_by_file(q["username"], q["filename"]) or {}).get("state")
                in ("completed", "failed", "cancelled")
                for _, q in queued
            )

        with contextlib.suppress(TimeoutError):
            wait_until(_all_settled, timeout=TRANSFER_TIMEOUT, interval=5.0)
        rows = [
            (t, stack.db.download_by_file(q["username"], q["filename"]) or {})
            for t, q in queued
        ]
        done = [(t, r) for t, r in rows if r.get("state") == "completed"]
        s.ok = bool(done)
        s.detail = f"{len(done)}/{len(rows)} concurrent transfers completed"
        s.evidence = {"states": {f"{t.artist} - {t.title}": r.get("state") for t, r in rows}}

    with g.step(Stage.S6_FILE_ON_DISK) as s:
        audit = ctx.probes.fs.audit()
        s.ok = not audit.partial_files
        s.detail = f"{len(audit.partial_files)} partial file(s) after concurrent transfers"
        s.evidence = {"partial_files": [str(p) for p in audit.partial_files[:5]]}

    with g.step(Stage.S7_BEETS_IMPORT) as s:
        def _all_imported() -> bool:
            return all(
                (stack.db.download_by_file(q["username"], q["filename"]) or {}).get("file_moved")
                for _, q in queued
            )

        with contextlib.suppress(TimeoutError):
            wait_until(_all_imported, timeout=IMPORT_TIMEOUT, interval=5.0)
        rows = [
            (t, stack.db.download_by_file(q["username"], q["filename"]) or {})
            for t, q in queued
        ]
        imported = [(t, r) for t, r in rows if r.get("file_moved") and (r.get("target_dir") or "").strip()]
        s.ok = len(imported) == len(rows) and bool(rows)
        s.detail = f"{len(imported)}/{len(rows)} concurrent downloads imported"
        s.evidence = {
            "targets": {f"{t.artist} - {t.title}": r.get("target_dir") for t, r in rows}
        }

    with g.step(Stage.S8_TAGS_CORRECT) as s:
        wrong: list[str] = []
        checked = 0
        for track, _q in queued:
            paths = ctx.probes.fs.find_by_title(track.title)
            if not paths:
                wrong.append(f"{track.artist} - {track.title}: no file on disk")
                continue
            checked += 1
            ok, why = ctx.probes.tags.grade(paths[0], track)
            if not ok:
                wrong.append(f"{track.artist} - {track.title}: {why}")
        s.ok = not wrong and checked > 0
        s.detail = (
            f"{checked - len(wrong)}/{len(queued)} concurrent downloads carry "
            f"their OWN metadata (cross-contamination is the failure here)"
        )
        s.evidence = {"mismatches": wrong[:6]}

    with g.step(Stage.S9_PLACEMENT_CORRECT) as s:
        audit = ctx.probes.fs.audit()
        s.ok = audit.clean
        s.detail = (
            "tree is canonical after concurrent imports"
            if audit.clean
            else f"{len(audit.artist_folder_variants)} artist-folder variant(s), "
            f"{len(audit.stranded_downloads)} stranded"
        )
        s.evidence = {
            "artist_folder_variants": dict(list(audit.artist_folder_variants.items())[:5]),
            "stranded_downloads": [str(p) for p in audit.stranded_downloads[:5]],
            "stranded_count": len(audit.stranded_downloads),
        }

    with g.step(Stage.S10_DEDUP_CORRECT) as s:
        duplicated = {
            f"{t.artist} - {t.title}": [str(p) for p in ctx.probes.fs.find_by_title(t.title)]
            for t, _ in queued
        }
        offenders = {k: v for k, v in duplicated.items() if len(v) > 1}
        s.ok = not offenders
        s.detail = (
            f"{len(offenders)}/{len(queued)} concurrently-downloaded tracks left "
            f"more than one copy on disk"
        )
        s.evidence = {"duplicates": offenders}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSpec:
    """One journey, described well enough for the report to name it.

    `report.py` reads this registry so it can list scenarios that produced
    *no* results at all — the difference between "U7 passed", "U7 failed"
    and "U7 never ran" is exactly what the user cannot currently tell.
    """

    id: str
    title: str
    intent: str
    stages: tuple[Stage, ...]
    est_downloads: int
    per_track: bool = False


SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        "U1_manual_pipeline",
        "Manual search to playable file",
        "type a name, get a tagged, correctly placed, playable track",
        MANUAL_STAGES,
        est_downloads=1,
        per_track=True,
    ),
    ScenarioSpec(
        "U2_deep_cuts",
        "Deep Cuts pull to playlist",
        "a Deep Cuts pull fills a playlist and downloads what is missing",
        REC_STAGES,
        est_downloads=5,
    ),
    ScenarioSpec(
        "U3_comfort_zone",
        "Comfort Zone pull to playlist",
        "a Comfort Zone pull fills a playlist and downloads what is missing",
        REC_STAGES,
        est_downloads=5,
    ),
    ScenarioSpec(
        "U4_fresh_picks",
        "Fresh Picks pull",
        "measure how much of ListenBrainz's newest-release feed Soulseek "
        "actually has, rather than scoring an empty network as a musica bug",
        REC_STAGES,
        est_downloads=5,
    ),
    ScenarioSpec(
        "U4_fresh_picks_availability",
        "Fresh Picks availability probe",
        "search-only: what fraction of brand-new releases has any peer at all",
        (Stage.S3_RESULTS_RELEVANT,),
        est_downloads=0,
    ),
    ScenarioSpec(
        "U5_duplicate",
        "Same track twice, same tree and across trees",
        "downloading one song three times must leave exactly one copy",
        DEDUP_STAGES,
        est_downloads=3,
        per_track=True,
    ),
    ScenarioSpec(
        "U6_peer_retry",
        "Peer failure to alternative peer",
        "a dead peer must be retried elsewhere without starting a new search",
        MANUAL_STAGES,
        est_downloads=2,
        per_track=True,
    ),
    ScenarioSpec(
        "U7_crash_recovery",
        "SIGKILL mid-transfer",
        "a crash mid-transfer leaves no partial file, no orphan, no lost row",
        MANUAL_STAGES,
        est_downloads=1,
        per_track=True,
    ),
    ScenarioSpec(
        "U8_stale_beets_row",
        "Stale beets row must not strand a download",
        "a library row whose file is gone must not make the replacement look "
        "like a duplicate",
        STALE_STAGES,
        est_downloads=2,
        per_track=True,
    ),
    ScenarioSpec(
        "U9_playlist_lifecycle",
        "Deleted playlist recreated only when needed",
        "a playlist deleted in Navidrome comes back only when there is "
        "something to put in it",
        PLAYLIST_STAGES,
        est_downloads=0,
    ),
    ScenarioSpec(
        "U10_concurrent",
        "Concurrent downloads",
        "several downloads at once each land in the right place with their "
        "own metadata",
        CONCURRENCY_STAGES,
        est_downloads=3,
    ),
)

SCENARIOS_BY_ID: dict[str, ScenarioSpec] = {s.id: s for s in SCENARIOS}


def default_tracks_for(scenario_id: str) -> list[Track]:
    """Which corpus entries a per-track scenario runs against.

    Deliberately narrow for the expensive resilience journeys: U7 kills a
    container, so running it twelve times would eat the whole time budget on
    one question. Run order is always popular -> awkward -> rare.
    """
    ordered = tracks_in_run_order()
    if scenario_id == "U1_manual_pipeline":
        return ordered
    if scenario_id == "U5_duplicate":
        return [t for t in ordered if t.tier is Tier.POPULAR][:1]
    if scenario_id == "U6_peer_retry":
        return [t for t in ordered if t.tier is Tier.POPULAR][:1]
    if scenario_id == "U7_crash_recovery":
        return [t for t in ordered if t.tier is Tier.POPULAR][:1]
    if scenario_id == "U8_stale_beets_row":
        return [t for t in ordered if t.tier is Tier.AWKWARD][:1]
    return []


def concurrent_track_set() -> list[Track]:
    """Three popular tracks — U10 needs availability, not difficulty."""
    return [t for t in CORPUS if t.tier is Tier.POPULAR][:3]


def tier_weight(tier: str | None) -> float:
    """Report-side ranking weight for a tier name. Unknown tiers weigh 1."""
    for t, w in TIER_WEIGHT.items():
        if t.value == tier:
            return w
    return 1.0
