"""
A scripted pass over musica's frontend at http://localhost:8092.

Four tabs — Search, Transfers, Recs, Config — and for each one:

    * how long the tab takes to load (page load, tab switch, and the API calls
      the tab fires)
    * JavaScript console errors
    * failed network requests
    * layout defects that are visible rather than theoretical: content clipped
      out of its box, elements past the right edge of the viewport, `.panel`
      siblings with no gap between them, and form rows whose controls do not
      line up

The last group exists because of two standing complaints — "frontend clipping
and lack of panel gaps", and a messy field layout on the Recs tab — that were
never measured, only felt. `LAYOUT_AUDIT_JS` turns them into numbers.

Three ways to run it, in order of fidelity
------------------------------------------

1. **Playwright** (`pip install playwright && playwright install chromium`).
   Full fidelity: real console errors, real failed requests, real layout audit
   at several viewport widths.

       python -m tests.live.browser_sweep --artifacts live-artifacts/browser

2. **HTTP only** (no extra dependencies — this is what runs if Playwright is
   missing). Loads the document and every asset it references, times each of
   the per-tab API calls, and reports status codes. It cannot see console
   errors or layout, and says so in the report rather than scoring those
   checks as passes.

3. **Agent-driven.** An agent with browser tooling evaluates `LAYOUT_AUDIT_JS`
   in the page (once per tab, optionally at several widths) and feeds the JSON
   back in:

       from tests.live.browser_sweep import ingest_agent_findings
       ingest_agent_findings(artifact_dir, {"recs": {...}, "config": {...}})

   This is how the sweep was first calibrated against the live app.

Output
------
`browser_sweep.json` in the artifact directory, plus one `StageResult`-shaped
row per tab appended to the scorecard when one is passed. The frontend is not
one of the thirteen pipeline stages — it is graded separately and reported
alongside, because "the pipeline worked but the page is unusable" is still a
failure the user feels.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

DEFAULT_URL = "http://localhost:8092"

TABS: tuple[str, ...] = ("search", "transfers", "recs", "config")

#: The API calls each tab depends on. Timed individually in HTTP mode so a slow
#: tab can be blamed on the endpoint that is actually slow.
TAB_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "search": ("/api/searches",),
    "transfers": ("/api/transfers",),
    "recs": ("/api/recs/status", "/api/recs/pending?limit=50&offset=0"),
    "config": ("/api/system/status", "/api/config"),
}

#: Viewport widths the layout audit runs at. 1280 is the desktop case; 768 and
#: 375 are where "clipping" complaints usually come from.
VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1280, 800),
    ("tablet", 768, 1024),
    ("mobile", 375, 812),
)

#: A tab is slow if it takes longer than this from click to settled.
TAB_BUDGET_S = 1.5

#: `.panel` siblings closer than this read as one undifferentiated slab.
MIN_PANEL_GAP_PX = 8


# ---------------------------------------------------------------------------
# The layout audit, as it runs inside the page
# ---------------------------------------------------------------------------

#: Evaluated in the page, once per tab. Returns
#: `{tab, viewport, counts, findings[]}`.
#:
#: Deliberate exclusions, so the output is signal:
#:   * `.truncate` cells clip on purpose (long Soulseek paths). They are
#:     reported separately as `truncated_no_title` only when the cell has no
#:     `title` attribute, i.e. the full text is unreachable.
#:   * elements inside an `overflow-x: auto` ancestor are allowed to exceed the
#:     viewport — that container scrolls, which is a design, not a defect.
LAYOUT_AUDIT_JS = r"""
(tabName) => {
  const MIN_GAP = %(min_gap)d, TOL = 4;
  const de = document.documentElement;
  const root = document.querySelector('#panel-' + tabName);
  const out = [];
  if (!root) return { tab: tabName, error: 'panel not found', findings: out };

  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const desc = (el) => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    const t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return el.tagName.toLowerCase() + id + cls + (t ? ' "' + t + '"' : '');
  };
  const inScroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll') return true;
    }
    return false;
  };
  const push = (kind, el, detail, px) =>
    out.push({ kind, el: typeof el === 'string' ? el : desc(el), detail, px });

  if (de.scrollWidth > de.clientWidth + 1)
    push('page_h_overflow', 'html',
      'the page itself scrolls sideways: scrollWidth ' + de.scrollWidth +
      ' > clientWidth ' + de.clientWidth, de.scrollWidth - de.clientWidth);

  const all = Array.from(root.querySelectorAll('*')).filter(vis);

  for (const el of all) {
    const cs = getComputedStyle(el);
    const clipX = ['hidden', 'clip'].includes(cs.overflowX);
    const clipY = ['hidden', 'clip'].includes(cs.overflowY);
    const intentional = el.classList.contains('truncate');
    if (clipX && el.scrollWidth > el.clientWidth + 1) {
      if (intentional) {
        if (!el.title)
          push('truncated_no_title', el,
            'text is cut off with no title attribute, so the full value is ' +
            'unreachable', el.scrollWidth - el.clientWidth);
      } else {
        push('clipped_x', el, 'content is wider than its box and hidden',
          el.scrollWidth - el.clientWidth);
      }
    }
    if (clipY && el.scrollHeight > el.clientHeight + 1 && !intentional)
      push('clipped_y', el, 'content is taller than its box and hidden',
        el.scrollHeight - el.clientHeight);

    const r = el.getBoundingClientRect();
    if (r.right > de.clientWidth + 1 && !inScroller(el))
      push('offscreen_right', el, 'extends past the right edge of the viewport',
        Math.round(r.right - de.clientWidth));
  }

  const panels = Array.from(root.querySelectorAll('.panel')).filter(vis);
  for (let i = 0; i < panels.length - 1; i++) {
    const a = panels[i].getBoundingClientRect();
    const b = panels[i + 1].getBoundingClientRect();
    if (b.top < a.bottom) continue;
    const gap = Math.round(b.top - a.bottom);
    if (gap < MIN_GAP)
      push('panel_gap', panels[i],
        'only ' + gap + 'px before ' + desc(panels[i + 1]) +
        ' — the panels read as one slab', gap);
  }

  for (const grid of root.querySelectorAll('.form-grid, .card-grid, .row')) {
    if (!vis(grid)) continue;
    const kids = Array.from(grid.children).filter(vis);
    const rows = [];
    for (const k of kids) {
      const r = k.getBoundingClientRect();
      let row = rows.find((w) => Math.abs(w.top - r.top) < 6);
      if (!row) rows.push((row = { top: r.top, items: [] }));
      row.items.push(k);
    }
    for (const row of rows) {
      if (row.items.length < 2) continue;
      const tops = row.items
        .map((k) => { const c = k.querySelector('input,select,textarea,button');
                      return c ? c.getBoundingClientRect().top : null; })
        .filter((v) => v !== null);
      if (tops.length < 2) continue;
      const spread = Math.round(Math.max.apply(null, tops) - Math.min.apply(null, tops));
      if (spread > TOL)
        push('row_control_misalign', grid,
          tops.length + ' controls sit in one row but their tops differ by ' +
          spread + 'px — the row does not line up', spread);
    }
  }

  const counts = out.reduce((a, f) => (a[f.kind] = (a[f.kind] || 0) + 1, a), {});
  return {
    tab: tabName,
    viewport: { w: de.clientWidth, h: de.clientHeight },
    counts,
    findings: out.slice(0, 60),
    total_findings: out.length,
  };
}
""" % {"min_gap": MIN_PANEL_GAP_PX}


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class TabReport:
    tab: str
    #: Seconds from clicking the tab until its requests settled. None in HTTP
    #: mode, where there is no click to time.
    switch_s: float | None = None
    #: Per-endpoint latency for the API calls this tab makes.
    api_ms: dict[str, float] = field(default_factory=dict)
    #: Endpoints that answered anything other than 2xx.
    api_failures: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    #: `{viewport_name: audit_result}` from LAYOUT_AUDIT_JS.
    layout: dict[str, Any] = field(default_factory=dict)
    #: Checks this mode could not perform. Never silently a pass.
    unmeasured: list[str] = field(default_factory=list)

    @property
    def layout_findings(self) -> list[dict]:
        return [
            {**f, "viewport": name}
            for name, audit in self.layout.items()
            for f in audit.get("findings", [])
        ]

    @property
    def ok(self) -> bool:
        return not (
            self.api_failures
            or self.console_errors
            or self.failed_requests
            or self.layout_findings
        )


@dataclass
class SweepReport:
    url: str
    mode: str
    started_at: float
    page_load_s: float | None = None
    document_status: int | None = None
    asset_failures: list[str] = field(default_factory=list)
    tabs: dict[str, TabReport] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "mode": self.mode,
            "started_at": self.started_at,
            "page_load_s": self.page_load_s,
            "document_status": self.document_status,
            "asset_failures": self.asset_failures,
            "notes": self.notes,
            "tabs": {name: asdict(rep) for name, rep in self.tabs.items()},
            "summary": self.summary(),
        }

    def summary(self) -> dict:
        findings: dict[str, int] = {}
        for rep in self.tabs.values():
            for finding in rep.layout_findings:
                findings[finding["kind"]] = findings.get(finding["kind"], 0) + 1
        return {
            "tabs_ok": [n for n, r in self.tabs.items() if r.ok],
            "tabs_with_findings": [n for n, r in self.tabs.items() if not r.ok],
            "console_errors": sum(len(r.console_errors) for r in self.tabs.values()),
            "failed_requests": sum(len(r.failed_requests) for r in self.tabs.values()),
            "api_failures": sum(len(r.api_failures) for r in self.tabs.values()),
            "layout_findings_by_kind": findings,
            "slow_tabs": {
                n: r.switch_s
                for n, r in self.tabs.items()
                if r.switch_s and r.switch_s > TAB_BUDGET_S
            },
            "unmeasured": sorted(
                {u for r in self.tabs.values() for u in r.unmeasured}
            ),
        }


# ---------------------------------------------------------------------------
# HTTP mode — always available
# ---------------------------------------------------------------------------

_ASSET_RE = re.compile(r'(?:src|href)="(/static/[^"]+)"')

_HTTP_UNMEASURED = [
    "JavaScript console errors (needs a browser)",
    "runtime network failures (needs a browser)",
    "layout: clipping, panel gaps, row alignment (needs a rendered page)",
]


def sweep_http(url: str = DEFAULT_URL, timeout: float = 20.0) -> SweepReport:
    """Load the document, its assets and every tab's API calls over plain HTTP.

    Catches the failures that do not need a renderer — a 500 from an endpoint a
    tab depends on, a missing bundle, an endpoint that takes seconds — and is
    explicit about the ones it cannot see.
    """
    report = SweepReport(url=url, mode="http", started_at=time.time())
    session = requests.Session()

    start = time.monotonic()
    doc = session.get(url, timeout=timeout)
    report.page_load_s = round(time.monotonic() - start, 3)
    report.document_status = doc.status_code

    for asset in sorted(set(_ASSET_RE.findall(doc.text))):
        resp = session.get(f"{url}{asset}", timeout=timeout)
        if resp.status_code != 200:
            report.asset_failures.append(f"{asset} -> {resp.status_code}")

    # index.html loads state.js as a module; the rest are imported from there
    # and never appear as a src attribute, so check them explicitly.
    for extra in (
        "/static/js/api.js",
        "/static/js/components.js",
        "/static/js/config.js",
        "/static/js/field_builder.js",
        "/static/js/recs.js",
        "/static/js/recs_fields.js",
        "/static/js/search.js",
        "/static/js/state.js",
        "/static/js/transfers.js",
        "/static/css/styles.css",
    ):
        resp = session.get(f"{url}{extra}", timeout=timeout)
        if resp.status_code != 200:
            report.asset_failures.append(f"{extra} -> {resp.status_code}")

    for tab in TABS:
        rep = TabReport(tab=tab, unmeasured=list(_HTTP_UNMEASURED))
        for endpoint in TAB_ENDPOINTS[tab]:
            t0 = time.monotonic()
            try:
                resp = session.get(f"{url}{endpoint}", timeout=timeout)
                rep.api_ms[endpoint] = round((time.monotonic() - t0) * 1000, 1)
                if not (200 <= resp.status_code < 300):
                    rep.api_failures.append(f"{endpoint} -> {resp.status_code}")
            except requests.RequestException as exc:
                rep.api_ms[endpoint] = round((time.monotonic() - t0) * 1000, 1)
                rep.api_failures.append(f"{endpoint} -> {type(exc).__name__}: {exc}")
        rep.switch_s = round(sum(rep.api_ms.values()) / 1000, 3)
        report.tabs[tab] = rep

    report.notes.append(
        "HTTP mode: console errors and layout were not measured, not passed."
    )
    return report


# ---------------------------------------------------------------------------
# Playwright mode — full fidelity when available
# ---------------------------------------------------------------------------


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def sweep_playwright(url: str = DEFAULT_URL) -> SweepReport:
    """Drive a real browser: click each tab, collect console + network, audit
    the layout at three widths.

    Clicks tabs and expands `<details>` panels only. It never clicks Search,
    Pull Now, Re-run, or any transfer control — this sweep must be safe to run
    at any point in a live wave, and every one of those starts real work.
    """
    from playwright.sync_api import sync_playwright

    report = SweepReport(url=url, mode="playwright", started_at=time.time())

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on(
            "console",
            lambda msg: console_errors.append(f"{msg.type}: {msg.text}")
            if msg.type in ("error", "warning")
            else None,
        )
        page.on(
            "pageerror",
            lambda exc: console_errors.append(f"pageerror: {exc}"),
        )
        page.on(
            "requestfailed",
            lambda req: failed_requests.append(
                f"{req.method} {req.url} -> {req.failure}"
            ),
        )
        page.on(
            "response",
            lambda resp: failed_requests.append(f"{resp.status} {resp.url}")
            if resp.status >= 400
            else None,
        )

        start = time.monotonic()
        page.goto(url, wait_until="networkidle")
        report.page_load_s = round(time.monotonic() - start, 3)
        report.document_status = 200

        for tab in TABS:
            before_console = len(console_errors)
            before_failed = len(failed_requests)
            rep = TabReport(tab=tab)

            t0 = time.monotonic()
            page.click(f"#tab-{tab}")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001 — SSE keeps a connection open forever
                rep.unmeasured.append(
                    "networkidle never reached (the SSE stream stays open); "
                    "switch_s is a ceiling, not a measurement"
                )
            rep.switch_s = round(time.monotonic() - t0, 3)

            # Settings live in collapsed <details>; the Recs field layout is
            # only auditable once it is open.
            page.eval_on_selector_all(
                f"#panel-{tab} details", "els => els.forEach(d => d.open = true)"
            )
            page.wait_for_timeout(300)

            for name, width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(400)
                rep.layout[name] = page.evaluate(LAYOUT_AUDIT_JS, tab)
            page.set_viewport_size({"width": 1280, "height": 800})

            rep.console_errors = console_errors[before_console:]
            rep.failed_requests = failed_requests[before_failed:]
            report.tabs[tab] = rep

        browser.close()

    return report


# ---------------------------------------------------------------------------
# Agent-driven ingest
# ---------------------------------------------------------------------------


def ingest_agent_findings(
    artifact_dir: Path,
    per_tab: dict[str, dict],
    *,
    url: str = DEFAULT_URL,
    viewport: str = "desktop",
) -> SweepReport:
    """Fold layout audits collected by an agent's browser tooling into a report.

    `per_tab` maps a tab name to the object `LAYOUT_AUDIT_JS` returned. Use
    this when Playwright is not installed but a browser is driveable another
    way — the numbers are identical, only the transport differs.
    """
    report = sweep_http(url)
    report.mode = f"http+agent[{viewport}]"
    for tab, audit in per_tab.items():
        rep = report.tabs.setdefault(tab, TabReport(tab=tab))
        rep.layout[viewport] = audit
        rep.unmeasured = [
            u for u in rep.unmeasured if not u.startswith("layout:")
        ]
    write_report(artifact_dir, report)
    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_report(artifact_dir: Path, report: SweepReport) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "browser_sweep.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    return path


def record_to_scorecard(report: SweepReport, scorecard, *, run_id: str) -> None:
    """Append one row per tab to the scorecard.

    The frontend is not one of the thirteen pipeline stages, so these are
    recorded against `S13_USER_CAN_FIND` with a `frontend:` scenario prefix —
    the stage that is already about whether the user can get to their music,
    which is the same question a broken page answers "no" to. The report
    generator can split them out on the scenario prefix.
    """
    from tests.live.probes.contract import Stage  # local: keeps CLI use dep-free

    for tab, rep in report.tabs.items():
        problems: list[str] = []
        if rep.api_failures:
            problems.append(f"API failures: {rep.api_failures}")
        if rep.console_errors:
            problems.append(f"{len(rep.console_errors)} console errors")
        if rep.failed_requests:
            problems.append(f"{len(rep.failed_requests)} failed requests")
        findings = rep.layout_findings
        if findings:
            kinds: dict[str, int] = {}
            for finding in findings:
                kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
            problems.append(f"layout: {kinds}")
        scorecard.grade(
            Stage.S13_USER_CAN_FIND,
            not problems,
            scenario=f"frontend:{tab}",
            run_id=run_id,
            latency_s=rep.switch_s,
            detail="; ".join(problems) if problems else f"{tab} tab is clean",
            evidence={
                "api_ms": rep.api_ms,
                "console_errors": rep.console_errors[:10],
                "failed_requests": rep.failed_requests[:10],
                "layout_findings": findings[:20],
                "unmeasured": rep.unmeasured,
            },
        )


def format_text(report: SweepReport) -> str:
    lines = [
        f"browser sweep — {report.url} [{report.mode}]",
        f"  document        : {report.document_status} in {report.page_load_s}s",
    ]
    if report.asset_failures:
        lines.append(f"  ASSET FAILURES  : {report.asset_failures}")
    for tab, rep in report.tabs.items():
        lines.append(f"  [{tab}] switch {rep.switch_s}s  api {rep.api_ms}")
        for problem in rep.api_failures:
            lines.append(f"      API FAIL   {problem}")
        for err in rep.console_errors[:5]:
            lines.append(f"      CONSOLE    {err}")
        for req in rep.failed_requests[:5]:
            lines.append(f"      NET FAIL   {req}")
        for finding in rep.layout_findings[:15]:
            lines.append(
                f"      LAYOUT     [{finding.get('viewport')}] "
                f"{finding['kind']} ({finding.get('px')}px) "
                f"{finding.get('el')} — {finding.get('detail')}"
            )
        for note in rep.unmeasured:
            lines.append(f"      unmeasured {note}")
    lines.append(f"  summary: {json.dumps(report.summary())}")
    return "\n".join(lines)


def run_sweep(url: str = DEFAULT_URL, artifact_dir: Path | None = None) -> SweepReport:
    """Best available mode, report written to `artifact_dir`."""
    report = sweep_playwright(url) if playwright_available() else sweep_http(url)
    if not playwright_available():
        report.notes.append(
            "Playwright is not installed — console errors and layout were not "
            "measured. `pip install playwright && playwright install chromium` "
            "for the full sweep, or feed agent-collected audits through "
            "ingest_agent_findings()."
        )
    if artifact_dir:
        write_report(artifact_dir, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--artifacts", default="live-artifacts/browser", help="output directory"
    )
    parser.add_argument(
        "--http-only", action="store_true", help="skip Playwright even if present"
    )
    args = parser.parse_args()

    artifact_dir = Path(args.artifacts).expanduser().resolve()
    report = (
        sweep_http(args.url)
        if args.http_only
        else run_sweep(args.url, artifact_dir=None)
    )
    path = write_report(artifact_dir, report)
    print(format_text(report))
    print(f"\nwrote {path}")
    summary = report.summary()
    broken = (
        report.asset_failures
        or summary["api_failures"]
        or summary["console_errors"]
        or summary["failed_requests"]
    )
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
