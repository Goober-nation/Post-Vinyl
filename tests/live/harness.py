"""
Live-stack test harness — drives the real Docker stack over its own API.

Everything here talks to running containers. Nothing is mocked: `musica` on
:8092, `slskd` on :8091, and the SQLite file bind-mounted at ./app_data.

The pieces:

- `MusicaClient`  — REST wrapper; every call is timed and recorded
- `EventRecorder` — background SSE consumer with `wait_for(predicate)`
- `DockerControl` — restart/stop/start containers, read their logs
- `LogScraper`    — parse musica's own log lines for facts the API doesn't
                    expose (chiefly the query strings sent to slskd)
- `DbInspector`   — read musica.db directly, for state with no API surface
                    (`worker_state`, raw `downloads` rows)
- `Timeline`      — one merged, timestamped record of everything the run did,
                    written to JSONL so a failed run is analyzable afterwards

Read `tests/live/README.md` before running any of this — these tests queue
real downloads from real Soulseek peers and can restart your containers.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_MUSICA_URL = "http://localhost:8092"
DEFAULT_SLSKD_URL = "http://localhost:8091"
DEFAULT_DB_PATH = REPO_ROOT / "app_data" / "musica.db"


# ---------------------------------------------------------------------------
# Timeline — the shared record every component writes to
# ---------------------------------------------------------------------------


class Timeline:
    """Append-only, timestamped record of everything a run observed.

    Both wall-clock and a monotonic offset from run start are recorded:
    wall-clock so entries line up with container logs, monotonic because
    that's what the timing assertions actually compare.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def record(self, kind: str, **fields: Any) -> dict:
        entry = {
            "kind": kind,
            "t": round(time.monotonic() - self._t0, 4),
            "wall": time.time(),
            **fields,
        }
        with self._lock:
            self._entries.append(entry)
            if self._path is not None:
                with self._path.open("a") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        return entry

    @property
    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.entries if e["kind"] == kind]

    def elapsed(self) -> float:
        return time.monotonic() - self._t0


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


@dataclass
class ApiCall:
    method: str
    path: str
    status: int
    duration: float
    body: Any


class MusicaClient:
    """Thin, timed wrapper over musica's REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_MUSICA_URL,
        timeline: Timeline | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeline = timeline or Timeline()
        self.timeout = timeout
        self._session = requests.Session()
        if auth:
            self._session.auth = auth

    # -- plumbing ----------------------------------------------------------

    def _call(self, method: str, path: str, retries: int = 2, **kwargs: Any) -> ApiCall:
        """Issue one request, timed and recorded.

        `retries` covers requests that never got a response — a keep-alive
        race, not an application failure.

        uvicorn closes idle connections after `timeout_keep_alive` (5s by
        default) and `requests.Session` reuses pooled connections without
        retrying, so a gap longer than that between calls — which pytest
        setup/teardown easily produces — lands a request on a socket the
        server is closing. Diagnosed 2026-08-11: it surfaced first as a GET
        that never reached the route handler (the handler's own first log
        line never appeared) and then, on the next run, as an explicit
        `RemoteDisconnected`. Both are the same thing seen from different
        sides of the race.

        Retrying is safe here because the request demonstrably never
        produced a response. Read timeouts are retried too, but only once —
        beyond that a slow server is a finding, not noise. Every retry is
        recorded as `api_retry` so it stays visible in the timeline instead
        of silently papering over a server that is genuinely misbehaving.
        """
        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", self.timeout)
        attempt = 0
        while True:
            start = time.monotonic()
            try:
                resp = self._session.request(method, url, timeout=timeout, **kwargs)
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as e:
                self.timeline.record(
                    "api_retry",
                    method=method,
                    path=path,
                    error=type(e).__name__,
                    timeout=timeout,
                    attempt=attempt,
                )
                if attempt >= retries:
                    raise
                attempt += 1
                # Drop the poisoned pooled connection before retrying.
                self._session.close()
                time.sleep(0.5)
                continue
            break

        duration = time.monotonic() - start
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        call = ApiCall(method, path, resp.status_code, duration, body)
        self.timeline.record(
            "api",
            method=method,
            path=path,
            status=resp.status_code,
            duration=round(duration, 4),
            attempt=attempt,
        )
        return call

    def get(self, path: str, **kw: Any) -> ApiCall:
        return self._call("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> ApiCall:
        return self._call("POST", path, **kw)

    def delete(self, path: str, **kw: Any) -> ApiCall:
        return self._call("DELETE", path, **kw)

    # -- health ------------------------------------------------------------

    def is_up(self, timeout: float = 10.0) -> bool:
        """Is the process alive and its event loop turning?

        Probes `/api/system/ping`, not `/api/system/status`. Status
        live-checks slskd and Navidrome, so it can take ~15s while musica is
        entirely healthy — asking it "are you up?" with a 5s timeout was
        measuring slskd's mood, not musica's.
        """
        try:
            return self.get("/api/system/ping", timeout=timeout).status == 200
        except requests.RequestException:
            return False

    def wait_until_up(self, timeout: float = 120.0) -> float:
        """Block until the API answers. Returns seconds waited."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.is_up():
                waited = time.monotonic() - start
                self.timeline.record("musica_up", waited=round(waited, 2))
                return waited
            time.sleep(1.0)
        raise TimeoutError(f"musica did not come up within {timeout}s")

    def system_status(self) -> dict:
        return self.get("/api/system/status").body

    # -- search ------------------------------------------------------------

    def search(self, query: str, artist: str | None = None) -> dict:
        call = self.post("/api/search", json={"query": query, "artist": artist})
        assert call.status == 201, f"search failed: {call.status} {call.body}"
        return call.body

    def search_detail(self, search_id: str, timeout: float = 180.0) -> dict:
        """Drive the search to completion and return its results.

        This blocks server-side for far longer than the config suggests.
        `search.wait_seconds` (10) bounds the *poll* loop, but the flush that
        follows — `SlskdSearch._fetch_responses` — retries 10 times with a
        15s HTTP timeout each, so a slow slskd can hold this request for
        ~155s while the code's own log line claims "Responses not available
        after 5s". Hence a timeout well above the client default.
        """
        call = self.get(f"/api/searches/{search_id}", timeout=timeout)
        assert call.status == 200, f"search detail failed: {call.body}"
        return call.body

    def search_progress(self, search_id: str) -> dict:
        return self.get(f"/api/searches/{search_id}/progress").body

    def list_searches(self) -> list[dict]:
        return self.get("/api/searches").body

    # -- downloads ---------------------------------------------------------

    def queue(
        self,
        username: str,
        files: list[dict],
        search_id: str | None = None,
        destination: str | None = None,
    ) -> ApiCall:
        payload: dict[str, Any] = {"username": username, "files": files}
        if search_id:
            payload["search_id"] = search_id
        if destination:
            payload["destination"] = destination
        return self.post("/api/queue", json=payload)

    def transfers(self) -> list[dict]:
        return self.get("/api/transfers").body

    def transfers_in_state(self, *states: str) -> list[dict]:
        return [t for t in self.transfers() if t.get("state") in states]

    def cancel_transfer(self, transfer_id: str) -> ApiCall:
        return self.delete(f"/api/transfers/{transfer_id}")

    def delete_finished(self) -> ApiCall:
        """`state` is a required query param, not a filter — the route only
        accepts the literal "finished"."""
        return self.delete("/api/transfers", params={"state": "finished"})

    # -- recs --------------------------------------------------------------

    def recs_status(self) -> dict:
        return self.get("/api/recs/status").body

    def pull_recs(self) -> ApiCall:
        """202 when a pull started, 409 when one is already running."""
        return self.post("/api/recs/pull")

    def abort_recs(self) -> ApiCall:
        return self.post("/api/recs/abort")

    def recs_pending(self) -> dict:
        return self.get("/api/recs/pending").body

    # -- config ------------------------------------------------------------

    def get_config(self) -> dict:
        return self.get("/api/config").body

    def set_config(self, section: str, **values: Any) -> ApiCall:
        return self.post("/api/config", json={section: values})


# ---------------------------------------------------------------------------
# slskd (direct)
# ---------------------------------------------------------------------------


class SlskdClient:
    """Direct access to slskd, bypassing musica.

    Needed for two things musica's API deliberately can't do:

    1. Confirm slskd is the durable system of record for search results —
       the premise migration 005 rests on.
    2. Make slskd *forget* a transfer musica still believes is live, which
       is the only deterministic way to exercise orphan reconciliation.
       Waiting for a real transfer to vanish on its own means racing the
       download: a small mp3 finishes in seconds and there is nothing left
       to orphan.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_SLSKD_URL,
        api_key: str | None = None,
        timeline: Timeline | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v0"
        self.api_key = api_key or self._key_from_env()
        self.timeline = timeline or Timeline()
        self._session = requests.Session()

    @staticmethod
    def _key_from_env() -> str:
        """Read SLSKD_API_KEY from the repo's .env — the same file docker
        compose reads, so the harness never needs its own copy."""
        env = REPO_ROOT / ".env"
        if not env.exists():
            return ""
        for line in env.read_text().splitlines():
            if line.startswith("SLSKD_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    @property
    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def is_up(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/application", headers=self._headers, timeout=5
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def wait_until_up(self, timeout: float = 120.0) -> float:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.is_up():
                return time.monotonic() - start
            time.sleep(2.0)
        raise TimeoutError(f"slskd did not come up within {timeout}s")

    def search_responses(self, search_id: str) -> list[dict] | None:
        """Peers slskd still holds for a search. None if it 404s."""
        resp = self._session.get(
            f"{self.base_url}/searches/{search_id}/responses",
            headers=self._headers,
            timeout=30,
        )
        return resp.json() if resp.status_code == 200 else None

    def downloads(self) -> list[dict]:
        """Flattened transfer list — slskd nests user > directory > file."""
        resp = self._session.get(
            f"{self.base_url}/transfers/downloads", headers=self._headers, timeout=20
        )
        if resp.status_code != 200:
            return []
        files = []
        for user in resp.json():
            for directory in user.get("directories", []):
                files.extend(directory.get("files", []))
        return files

    def forget_transfer(self, username: str, transfer_id: str) -> bool:
        """Make slskd drop a transfer record entirely (`remove=true`).

        This is what manufactures an orphan: musica's row stays live while
        slskd stops reporting it.
        """
        resp = self._session.delete(
            f"{self.base_url}/transfers/downloads/{username}/{transfer_id}",
            params={"remove": "true"},
            headers=self._headers,
            timeout=20,
        )
        self.timeline.record(
            "slskd_forget",
            username=username,
            transfer_id=transfer_id,
            status=resp.status_code,
        )
        return resp.status_code in (200, 204)

    def cancel_transfer(self, username: str, transfer_id: str) -> bool:
        """Cancel without removing — slskd keeps reporting it as cancelled."""
        resp = self._session.delete(
            f"{self.base_url}/transfers/downloads/{username}/{transfer_id}",
            headers=self._headers,
            timeout=20,
        )
        return resp.status_code in (200, 204)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


@dataclass
class Event:
    type: str
    data: dict
    t: float


class EventRecorder:
    """Consumes musica's SSE stream on a background thread.

    Every event lands in the shared Timeline, so an assertion that fails on
    ordering can be diagnosed from the JSONL afterwards rather than re-run.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_MUSICA_URL,
        timeline: Timeline | None = None,
        types: str | None = None,
        auth: tuple[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeline = timeline or Timeline()
        self.types = types
        self.auth = auth
        self.events: list[Event] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._new_event = threading.Condition(self._lock)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give the subscription a moment to land, otherwise an event
        # published immediately after start() can be missed.
        time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        url = f"{self.base_url}/api/events"
        params = {"types": self.types} if self.types else None
        while not self._stop.is_set():
            try:
                with requests.get(
                    url,
                    params=params,
                    stream=True,
                    timeout=(10, None),
                    auth=self.auth,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    resp.raise_for_status()
                    self._consume(resp)
            except requests.RequestException as e:
                if self._stop.is_set():
                    return
                # Musica restarting mid-run is expected in these tests —
                # reconnect rather than losing the rest of the stream.
                self.timeline.record("sse_reconnect", error=str(e))
                time.sleep(1.0)

    def _consume(self, resp: requests.Response) -> None:
        event_type: str | None = None
        for raw in resp.iter_lines(decode_unicode=True):
            if self._stop.is_set():
                return
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                event_type = None
                continue
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                payload = line[len("data:") :].strip()
                try:
                    data = json.loads(payload)
                except ValueError:
                    data = {"raw": payload}
                self._append(event_type or data.get("type", "unknown"), data)

    def _append(self, event_type: str, data: dict) -> None:
        entry = self.timeline.record("sse", event=event_type, data=data)
        with self._new_event:
            self.events.append(Event(event_type, data, entry["t"]))
            self._new_event.notify_all()

    # -- querying ----------------------------------------------------------

    def snapshot(self) -> list[Event]:
        with self._lock:
            return list(self.events)

    def of_type(self, event_type: str) -> list[Event]:
        return [e for e in self.snapshot() if e.type == event_type]

    def wait_for(
        self,
        predicate: Callable[[Event], bool],
        timeout: float = 60.0,
        description: str = "event",
    ) -> Event:
        """Block until an event matches. Matches events already received."""
        deadline = time.monotonic() + timeout
        seen = 0
        with self._new_event:
            while True:
                while seen < len(self.events):
                    event = self.events[seen]
                    seen += 1
                    if predicate(event):
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no {description} within {timeout}s "
                        f"({len(self.events)} events seen)"
                    )
                self._new_event.wait(min(remaining, 1.0))


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


#: Another published port on the same host-side forwarder. Probing it is how
#: "musica is unreachable" is told apart from "everything is unreachable".
NAVIDROME_URL = "http://localhost:8090/ping"


def diagnose_unreachable(musica_url: str = DEFAULT_MUSICA_URL) -> str:
    """Say which layer is actually broken when musica stops answering.

    Written after 2026-08-13, where four live runs died with "musica is not
    answering at http://localhost:8092 after 90s" while musica was answering
    its own health endpoint inside the container in 13ms, continuously, the
    entire time. The app was never involved.

    On Docker Desktop for macOS every published port is forwarded in
    userspace by a single `com.docker.backend` process, which also serves the
    Docker API socket. slskd publishes the Soulseek peer ports and shares the
    library, so a search burst opens thousands of proxied peer connections
    and pins that process at >100% CPU. Everything the host reaches through
    it degrades together: musica on 8092, Navidrome on 8090, slskd on 8091,
    and the docker CLI itself (`docker inspect` took 19s, `docker compose
    stop` 38s, `docker compose exec py-spy` 2.5 minutes).

    From pytest all of that is indistinguishable from a dead app, so check
    the vantages that can tell them apart:

      - the container's own healthcheck runs *inside* the container every
        15s and needs no `docker exec`, so it survives the congestion;
      - a neighbour's published port shares the forwarder but shares nothing
        with musica's code;
      - the latency of `docker inspect` measures the daemon path, which
        never touches a published port at all.

    Returns a printable multi-line diagnosis. Never raises: this runs on the
    failure path, and a diagnostic that blows up takes the real error with
    it.
    """
    lines: list[str] = []

    cli_started = time.monotonic()
    health = "unknown"
    try:
        proc = subprocess.run(
            ["docker", "inspect", "musica", "--format",
             "{{.State.Health.Status}}|{{.State.Running}}|{{.RestartCount}}|{{.State.OOMKilled}}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=False,
        )
        health = proc.stdout.strip() or "no output"
    except (subprocess.SubprocessError, OSError) as e:
        health = f"docker inspect failed: {e}"
    cli_ms = (time.monotonic() - cli_started) * 1000
    lines.append(f"  container state (health|running|restarts|oomkilled): {health}")
    lines.append(f"  `docker inspect` took {cli_ms:.0f}ms")

    neighbour = "unreachable"
    neighbour_started = time.monotonic()
    try:
        resp = requests.get(NAVIDROME_URL, timeout=20)
        neighbour = f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        neighbour = f"{type(e).__name__}"
    neighbour_ms = (time.monotonic() - neighbour_started) * 1000
    lines.append(f"  navidrome on :8090 -> {neighbour} in {neighbour_ms:.0f}ms")

    # The verdict. `healthy` is the strongest single signal available without
    # an exec: docker ran the probe inside the container and it passed.
    congested = neighbour_ms > 2000 or cli_ms > 5000
    if health.startswith("healthy") and congested:
        lines.append(
            "  VERDICT: musica is healthy inside its container. The host-side\n"
            "  port forwarder (com.docker.backend) is congested — Navidrome\n"
            "  and/or the docker CLI stalled by the same amount, and neither\n"
            "  shares any code with musica. Check slskd's peer connection\n"
            "  count; see tests/live/tools/probe_layers.py."
        )
    elif health.startswith("healthy"):
        lines.append(
            "  VERDICT: the container healthcheck passes but the host cannot\n"
            "  reach musica, while its neighbours are fine. Suspect musica's\n"
            "  published-port mapping specifically."
        )
    else:
        lines.append(
            "  VERDICT: the container itself is not healthy — this one really\n"
            "  is musica (or the stack is down)."
        )
    return "\n".join(lines)


class DockerControl:
    """Container lifecycle + logs, via `docker compose` in the repo root."""

    def __init__(self, timeline: Timeline | None = None) -> None:
        self.timeline = timeline or Timeline()
        if shutil.which("docker") is None:
            raise RuntimeError("docker not on PATH")

    def _compose(self, *args: str, timeout: float = 120.0) -> str:
        cmd = ["docker", "compose", *args]
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def running_services(self) -> list[str]:
        out = self._compose("ps", "--services", "--filter", "status=running")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def restart(self, service: str) -> None:
        self.timeline.record("docker", action="restart", service=service)
        self._compose("restart", service)

    def stop(self, service: str) -> None:
        self.timeline.record("docker", action="stop", service=service)
        self._compose("stop", service)

    def start(self, service: str) -> None:
        self.timeline.record("docker", action="start", service=service)
        self._compose("start", service)

    def kill(self, service: str, signal: str = "SIGKILL") -> None:
        """Hard kill — the honest simulation of a crash mid-transfer.

        `restart` sends SIGTERM first, which lets musica's lifespan shut the
        workers down cleanly; that is a *different* scenario from a crash and
        should be tested separately.
        """
        self.timeline.record("docker", action="kill", service=service, signal=signal)
        self._compose("kill", "-s", signal, service)

    def logs(self, service: str, since: str | None = None, tail: str = "all") -> str:
        args = ["logs", "--no-color", "--tail", tail]
        if since:
            args += ["--since", since]
        args.append(service)
        return self._compose(*args)


# ---------------------------------------------------------------------------
# Log scraping
# ---------------------------------------------------------------------------

# RecPuller logs every ladder rung it tries. These are the only place the
# literal query string sent to slskd is observable — the REST API never
# echoes it back, so P6.5-6's "2-word cap" claim can't be checked without
# reading them.
QUERY_LINE = "RecPuller: query "
RUNG_LINE = "RecPuller: pass-ratio re-query"
ORPHAN_LINE = "Download orphaned"
STALE_PENDING_LINE = "Pending download never adopted"
SEARCH_INITIATED = "Search initiated: id="

# Greedy up to "' ->" so a query containing an apostrophe ("stayin'") isn't
# truncated at the wrong quote.
_LADDER_RE = re.compile(
    r"RecPuller: query '(?P<query>.*)' -> (?P<results>\d+) results, "
    r"(?P<viable>\d+) viable \(pass ratio (?P<ratio>[\d.]+)\)"
)


@dataclass
class LadderAttempt:
    query: str
    results: int
    viable: int
    ratio: float

    @property
    def word_count(self) -> int:
        return len(self.query.split())


def parse_ladder_attempts(text: str) -> list[LadderAttempt]:
    """Pure parser, split out so it's unit-testable without a live stack.

    If this silently returns [] the live assertions become vacuous, so it has
    its own coverage in tests/test_live_harness.py.
    """
    return [
        LadderAttempt(
            query=m.group("query"),
            results=int(m.group("results")),
            viable=int(m.group("viable")),
            ratio=float(m.group("ratio")),
        )
        for m in _LADDER_RE.finditer(text)
    ]


def parse_search_ids(text: str) -> list[str]:
    """slskd search ids musica logged as initiated, in order.

    Note both `app/routes/search.py` and `app/services/search.py` log this
    line, so one *manual* search yields two entries while a *rec* search
    yields one. Callers compare counts before/after an operation, and an
    unchanged count still means "no search fired" either way — but don't
    read the length as a search count.
    """
    return [
        line.split(SEARCH_INITIATED, 1)[1].strip()
        for line in text.splitlines()
        if SEARCH_INITIATED in line
    ]


class LogScraper:
    """Pulls structured facts out of musica's log stream."""

    def __init__(self, docker: DockerControl, service: str = "musica") -> None:
        self.docker = docker
        self.service = service

    def raw(self, since: str | None = None) -> str:
        return self.docker.logs(self.service, since=since)

    def ladder_attempts(self, since: str | None = None) -> list[LadderAttempt]:
        """Every `query '...' -> N results, M viable (pass ratio X)` line."""
        return parse_ladder_attempts(self.raw(since))

    def count_lines(self, needle: str, since: str | None = None) -> int:
        return sum(1 for line in self.raw(since).splitlines() if needle in line)

    def searches_issued(self, since: str | None = None) -> list[str]:
        """slskd search ids musica initiated, in order.

        This is how "did a search fire?" gets answered — the negative that
        P6.5-4 hinges on (retry must NOT re-search) has no API surface.
        """
        return parse_search_ids(self.raw(since))


# ---------------------------------------------------------------------------
# Direct DB access
# ---------------------------------------------------------------------------


class DbInspector:
    """Read-only view of musica.db through the host bind-mount.

    Opened read-only and per-query so the running container's writes are
    always visible and nothing here can corrupt live state.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def searches(self) -> list[dict]:
        """User-initiated search headers. musica stores nothing else about a
        search — no peer responses; slskd owns those (migration 005)."""
        return self.query("SELECT * FROM searches ORDER BY created_at")

    def search_by_id(self, search_id: str) -> dict | None:
        rows = self.query("SELECT * FROM searches WHERE id = ?", (search_id,))
        return rows[0] if rows else None

    def worker_state(self) -> dict[str, str]:
        return {
            r["key"]: r["value"]
            for r in self.query("SELECT key, value FROM worker_state")
        }

    def downloads(self) -> list[dict]:
        return self.query("SELECT * FROM downloads ORDER BY created_at")

    def download_by_file(self, username: str, filename: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM downloads WHERE username = ? AND filename = ?",
            (username, filename),
        )
        return rows[0] if rows else None

    def tables(self) -> set[str]:
        return {
            r["name"]
            for r in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    def missing_tables(self) -> set[str]:
        """Tables the live tests need, if the image is out of date.

        Worth checking explicitly: a container built before these landed runs
        happily without them, and every assertion would otherwise fail with a
        bare OperationalError that reads like a harness bug rather than "you
        didn't rebuild the image".
        """
        return {"searches", "worker_state", "downloads"} - self.tables()

    def stale_tables(self) -> set[str]:
        """Tables migration 005 should have dropped. Their presence means the
        image predates the revert, so the app is still duplicating slskd."""
        return {"search_jobs", "search_responses"} & self.tables()

    def table_counts(self) -> dict[str, int]:
        """Row counts, with `None` for tables that don't exist yet."""
        wanted = ("searches", "worker_state", "downloads")
        present = self.tables()
        return {
            t: (
                self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                if t in present
                else None
            )
            for t in wanted
        }

    def applied_migrations(self) -> list[str]:
        if "applied_migrations" not in self.tables():
            return []
        return [
            r["filename"]
            for r in self.query(
                "SELECT filename FROM applied_migrations ORDER BY filename"
            )
        ]


# ---------------------------------------------------------------------------
# Waiting helpers
# ---------------------------------------------------------------------------


def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 60.0,
    interval: float = 1.0,
    description: str = "condition",
) -> float:
    """Poll until true. Returns seconds waited; raises TimeoutError."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    raise TimeoutError(f"{description} not met within {timeout}s")


def settle(seconds: float, reason: str = "") -> None:
    """Deliberate wait for background workers to act (or fail to).

    Distinct from `wait_until`: those assert something *happens*; this is for
    the cases where the point is that nothing should — you have to give the
    monitor its polls before you can claim it didn't misfire.
    """
    time.sleep(seconds)


def viable_results(results: list[dict]) -> list[dict]:
    """Candidates worth queueing, best first — free slot before no slot.

    Mirrors RecPuller's own preference. Returns a *list* because the first
    choice frequently can't be queued at all: peers go offline between the
    search and the queue call, and slskd answers with a 500 or 404. A live
    test that gives up on the first candidate fails for reasons that have
    nothing to do with what it's testing.
    """
    with_slot = [r for r in results if r.get("has_free_slot") and r.get("filename")]
    without = [r for r in results if not r.get("has_free_slot") and r.get("filename")]
    return with_slot + without


def first_viable_result(results: list[dict]) -> dict | None:
    """Best single candidate, or None. See `viable_results`."""
    candidates = viable_results(results)
    return candidates[0] if candidates else None


def queue_first_available(
    stack, search_id: str, results: list[dict], limit: int = 6
) -> dict | None:
    """Queue the first candidate slskd actually accepts.

    Peers go offline between the search and the queue call; slskd then
    answers 500 (or 404 for an unknown username) and musica surfaces that.
    Walk the candidate list rather than letting one dead peer fail a test
    about persistence or priority. Returns the queued result, or None if
    every candidate was refused.
    """
    for candidate in viable_results(results)[:limit]:
        call = stack.client.queue(
            candidate["username"],
            [{"filename": candidate["filename"], "size": candidate["size"]}],
            search_id=search_id,
        )
        if call.status in (201, 207):
            stack.marker("queued", username=candidate["username"])
            return candidate
        stack.marker(
            "queue_refused", username=candidate["username"], status=call.status
        )
    return None


@dataclass
class Stack:
    """Everything a live test needs, wired to one shared Timeline."""

    client: MusicaClient
    slskd: SlskdClient
    events: EventRecorder
    docker: DockerControl
    logs: LogScraper
    db: DbInspector
    timeline: Timeline
    _cleanup: list[Callable[[], None]] = field(default_factory=list)

    def restart_musica(self, hard: bool = False) -> float:
        """Restart musica and wait for the API to answer again.

        `hard=True` sends SIGKILL — no clean worker shutdown, which is what
        an actual crash looks like. Returns seconds of downtime.
        """
        start = time.monotonic()
        if hard:
            self.docker.kill("musica")
            self.docker.start("musica")
        else:
            self.docker.restart("musica")
        self.client.wait_until_up()
        downtime = time.monotonic() - start
        self.timeline.record("musica_restarted", hard=hard, downtime=round(downtime, 2))
        return downtime

    def marker(self, label: str, **fields: Any) -> None:
        """Drop a labelled point in the timeline — makes the JSONL readable."""
        self.timeline.record("marker", label=label, **fields)


def build_stack(
    musica_url: str = DEFAULT_MUSICA_URL,
    db_path: Path = DEFAULT_DB_PATH,
    artifact_dir: Path | None = None,
    auth: tuple[str, str] | None = None,
    event_types: str | None = None,
) -> Iterator[Stack]:
    """Context-manager-ish factory: yields a wired Stack, then tears down."""
    timeline = Timeline(artifact_dir / "timeline.jsonl" if artifact_dir else None)
    client = MusicaClient(musica_url, timeline=timeline, auth=auth)
    docker = DockerControl(timeline=timeline)
    recorder = EventRecorder(
        musica_url, timeline=timeline, types=event_types, auth=auth
    )
    stack = Stack(
        client=client,
        slskd=SlskdClient(timeline=timeline),
        events=recorder,
        docker=docker,
        logs=LogScraper(docker),
        db=DbInspector(db_path),
        timeline=timeline,
    )
    recorder.start()
    try:
        yield stack
    finally:
        recorder.stop()
