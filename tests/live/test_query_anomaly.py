"""
Why does `gorillaz` return nothing when `gorilas` returns results?

The user reported this and found it inexplicable. It is worth its own module
because it is the one observation that could invalidate large parts of the
search statistics: if some queries are silently swallowed, then every "no
results" verdict elsewhere is ambiguous between "Soulseek has nothing" and
"we never really asked".

The investigation is built to *separate causes*, not to reproduce a symptom.
Every query is issued twice — once through musica and once directly against
slskd — because those two paths differ by exactly the things that could be
responsible:

    user query
      -> musica: query_builder normalisation, the ladder, the artist
         post-filter, `search.response_threshold` early cancel
      -> slskd:  cancel-to-flush, its own filters
      -> Soulseek network: whatever the server does with the term

If musica reports 0 and slskd reports peers for the same term, the fault is
ours. If both report 0 while a control term works from the same process
seconds later, it is upstream of us — and that is worth knowing precisely,
because it is not something this codebase can fix.

These are search-only. They queue nothing and cost no download budget.

    pytest tests/live/test_query_anomaly.py --live -s
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from tests.live.corpus import ANOMALY_QUERIES, SWEEP_ARTISTS

#: How many times each anomaly query is repeated. Soulseek peer availability
#: is noisy; a single zero proves nothing, and the difference between "always
#: zero" and "usually zero" points at different causes.
REPEATS = 3

#: Terms expected to work every time. If a control returns zero, the network
#: (or our connection to it) is having a bad moment and the run's other
#: zeroes cannot be interpreted — so controls are checked alongside, not once
#: at the start.
CONTROL_TERM = "radiohead"


@dataclass
class QueryOutcome:
    """One query, both ways."""

    query: str
    via_musica: int = -1
    via_slskd: int = -1
    musica_error: str = ""
    duration_s: float = 0.0
    logged_query: str | None = None

    @property
    def disagrees(self) -> bool:
        """musica found nothing where slskd found peers — our bug."""
        return self.via_musica == 0 and self.via_slskd > 0

    @property
    def both_empty(self) -> bool:
        return self.via_musica == 0 and self.via_slskd == 0


@dataclass
class Findings:
    outcomes: list[QueryOutcome] = field(default_factory=list)

    def by_query(self, query: str) -> list[QueryOutcome]:
        return [o for o in self.outcomes if o.query == query]

    def always_empty(self) -> set[str]:
        seen: dict[str, list[QueryOutcome]] = {}
        for o in self.outcomes:
            seen.setdefault(o.query, []).append(o)
        return {q for q, os_ in seen.items() if all(o.via_musica == 0 for o in os_)}


def _search_via_musica(stack, query: str) -> QueryOutcome:
    """Drive one search through musica and count what came back."""
    outcome = QueryOutcome(query=query)
    started = time.monotonic()
    try:
        job = stack.client.search(query)
        detail = stack.client.search_detail(job["search_id"])
        results = detail.get("results") or []
        outcome.via_musica = len(results)
    except Exception as exc:  # noqa: BLE001 — any failure is itself the datum
        outcome.musica_error = f"{type(exc).__name__}: {exc}"
        outcome.via_musica = 0
    outcome.duration_s = round(time.monotonic() - started, 2)
    return outcome


def _peers_via_slskd(stack, search_id: str) -> int:
    """Peers slskd itself still holds for a search musica just ran.

    This is the discriminator. slskd owns search results and retains them
    (`retention.search`), so asking it directly tells us what actually came
    back off the network, independent of anything musica did to the response
    afterwards — the artist post-filter especially.
    """
    responses = stack.slskd.search_responses(search_id)
    if not responses:
        return 0
    return sum(len(r.get("files") or []) for r in responses)


@pytest.fixture(scope="module")
def findings() -> Findings:
    """Shared across the module so the summary test sees everything."""
    return Findings()


def test_control_term_returns_results(stack, findings):
    """Establish that searching works at all right now.

    Without this, every zero below is uninterpretable. If this fails, the
    network or the connection is the story and the rest of the module's
    results should be discarded rather than believed.
    """
    outcome = _search_via_musica(stack, CONTROL_TERM)
    findings.outcomes.append(outcome)
    print(f"\n[anomaly] control {CONTROL_TERM!r} -> {outcome.via_musica} results")
    assert outcome.via_musica > 0, (
        f"control term {CONTROL_TERM!r} returned nothing — Soulseek connectivity "
        f"is bad right now, so no zero in this module can be attributed to a "
        f"query-handling bug. Re-run when the control passes."
    )


@pytest.mark.parametrize("query", ANOMALY_QUERIES)
def test_anomaly_query_musica_versus_slskd(stack, findings, query):
    """Issue one suspect term both ways and record the disagreement.

    Never asserts a result count — a term genuinely having no seeders is a
    legitimate outcome. What it asserts is the *absence of a discrepancy*:
    musica must not report zero for something slskd plainly returned.
    """
    for attempt in range(REPEATS):
        outcome = _search_via_musica(stack, query)
        try:
            job_ids = stack.logs.searches_issued(since="120s")
            if job_ids:
                outcome.via_slskd = _peers_via_slskd(stack, job_ids[-1])
        except Exception as exc:  # noqa: BLE001
            outcome.logged_query = f"slskd lookup failed: {exc}"
        findings.outcomes.append(outcome)
        print(
            f"[anomaly] {query!r} attempt {attempt + 1}: "
            f"musica={outcome.via_musica} slskd={outcome.via_slskd} "
            f"({outcome.duration_s}s) {outcome.musica_error}"
        )

    disagreements = [o for o in findings.by_query(query) if o.disagrees]
    assert not disagreements, (
        f"musica reported 0 results for {query!r} while slskd held "
        f"{disagreements[0].via_slskd} file(s) from the same search. The "
        f"responses reached us and something in musica dropped them — look at "
        f"the artist post-filter and the pass-ratio ladder in "
        f"app/services/search.py and app/services/query_builder.py."
    )


@pytest.mark.slow
def test_zero_result_sweep_over_household_names(stack, findings):
    """Is Gorillaz unique, or the tip of something?

    Twenty artists nobody would expect to be missing from Soulseek. Any that
    come back empty are as interesting as the original report, and a pattern
    among them (length? a substring? a character?) is the actual finding.

    Reported rather than asserted: this is a survey, and failing it would say
    "Soulseek is missing an artist", which is not a claim about this codebase.
    """
    empty: list[str] = []
    for artist in SWEEP_ARTISTS:
        outcome = _search_via_musica(stack, artist)
        findings.outcomes.append(outcome)
        if outcome.via_musica == 0:
            empty.append(artist)
        print(f"[sweep] {artist!r} -> {outcome.via_musica}")

    print(f"\n[sweep] {len(empty)}/{len(SWEEP_ARTISTS)} household names returned zero")
    if empty:
        print(f"[sweep] empty: {empty}")
        lengths = sorted(len(a) for a in empty)
        print(f"[sweep] term lengths of the empty ones: {lengths}")


def test_summarise_findings(findings):
    """Print the conclusion in one place. Always passes; it is a report.

    Runs last by file order so it sees every outcome the module gathered.
    """
    if not findings.outcomes:
        pytest.skip("nothing gathered — the earlier tests did not run")

    always_empty = findings.always_empty()
    disagreements = [o for o in findings.outcomes if o.disagrees]
    both_empty = {o.query for o in findings.outcomes if o.both_empty}

    print("\n=== query anomaly summary ===")
    print(f"queries issued:        {len(findings.outcomes)}")
    print(f"always empty:          {sorted(always_empty) or 'none'}")
    print(f"empty in BOTH paths:   {sorted(both_empty) or 'none'}")
    print(f"musica-only zeroes:    {[o.query for o in disagreements] or 'none'}")
    if disagreements:
        print("VERDICT: musica is dropping responses slskd returned — our bug.")
    elif always_empty:
        print(
            "VERDICT: the term(s) return nothing from the network itself, "
            "identically through both paths. Not a musica defect; the cause "
            "is upstream (Soulseek server-side term handling)."
        )
    else:
        print("VERDICT: did not reproduce — every term returned results.")
