"""
The contract every pipeline probe implements, and the scorecard they all
write to.

Why a contract module: the probes, the step tests, the scenarios and the
report generator are built in parallel. They agree here, not by accident.
**Signatures in this file are load-bearing — if one has to change, change it
here and say so, don't shadow it in an implementation.**

The stage model
---------------
The pipeline is graded as thirteen stages. A run doesn't "fail" — a *stage*
fails, and every stage a run reached gets its own verdict. That is the whole
point: today a break anywhere reads as "it didn't work", which is why twenty
problems surface for every one thing looked at.

    S1  search accepted          POST /api/search -> 201, row in `searches`
    S2  search completes         slskd drives to completion, responses flushed
    S3  results are relevant     the candidate set actually contains the track
                                 that was asked for (graded against corpus
                                 `expect_*`, not against whatever a peer named
                                 its file)
    S4  queue accepted           POST /api/queue -> 201/207, `downloads` row
    S5  transfer completes       state reaches `completed` (or a retry does)
    S6  file on disk             the exact path slskd reported exists, full size
    S7  beets import             exit 0, matched vs asis, landed in the right
                                 tree (Searches for manual, Discovery for recs)
    S8  tags correct             albumartist/artist/album/title/track/MBID
                                 match the corpus expectation
    S9  placement correct        strict canonical: one folder per artist, no
                                 feat. clause in albumartist, no case variants,
                                 no strays, no partials, no leftovers in
                                 downloads/complete, no empty dirs
    S10 dedup correct            exactly one copy on disk; same-tree and
                                 cross-tree; a stale library row must NOT
                                 cause a false skip
    S11 navidrome indexes it     after a scan, the file is in Navidrome
    S12 playlist correct         playlist exists and contains what it should,
                                 including tracks that arrived by download
    S13 user can find it         search by artist/title returns it with the
                                 right metadata

Verdicts
--------
`PASS`/`FAIL` are claims about the system. `SKIP` means the stage was never
reached (an earlier stage failed) and is **not** a pass — the funnel in the
report depends on that distinction. `ERROR` means the probe itself broke, and
is a bug in the harness, not a finding about musica.
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Stage(str, Enum):
    S1_SEARCH_ACCEPTED = "S1_search_accepted"
    S2_SEARCH_COMPLETED = "S2_search_completed"
    S3_RESULTS_RELEVANT = "S3_results_relevant"
    S4_QUEUE_ACCEPTED = "S4_queue_accepted"
    S5_TRANSFER_COMPLETED = "S5_transfer_completed"
    S6_FILE_ON_DISK = "S6_file_on_disk"
    S7_BEETS_IMPORT = "S7_beets_import"
    S8_TAGS_CORRECT = "S8_tags_correct"
    S9_PLACEMENT_CORRECT = "S9_placement_correct"
    S10_DEDUP_CORRECT = "S10_dedup_correct"
    S11_NAVIDROME_INDEXED = "S11_navidrome_indexed"
    S12_PLAYLIST_CORRECT = "S12_playlist_correct"
    S13_USER_CAN_FIND = "S13_user_can_find"


#: Funnel order. The report walks this to show where runs die.
STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # never reached — NOT a pass
    ERROR = "error"  # the probe broke, not the system


@dataclass
class StageResult:
    """One graded stage of one run.

    `evidence` must be enough to re-find the proof without re-running:
    a path on disk, a `timeline.jsonl` offset, a log excerpt, an API body.
    A finding nobody can re-verify is an opinion.
    """

    stage: Stage
    verdict: Verdict
    scenario: str
    run_id: str
    #: Corpus track this stage was graded against, where applicable.
    track: str | None = None
    tier: str | None = None
    #: Seconds this stage took, when it is meaningfully timed.
    latency_s: float | None = None
    #: One line: why this verdict. Required for FAIL/ERROR.
    detail: str = ""
    #: Re-findable proof — paths, log lines, response bodies.
    evidence: dict[str, Any] = field(default_factory=dict)
    wall: float = field(default_factory=time.time)


class Scorecard:
    """Append-only JSONL of every graded stage across every run.

    Written incrementally so a run that dies at hour two still leaves
    everything it learned in the first two hours.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._results: list[StageResult] = []

    def record(self, result: StageResult) -> StageResult:
        with self._lock:
            self._results.append(result)
            with self.path.open("a") as fh:
                payload = asdict(result)
                payload["stage"] = result.stage.value
                payload["verdict"] = result.verdict.value
                fh.write(json.dumps(payload, default=str) + "\n")
        return result

    def grade(
        self,
        stage: Stage,
        ok: bool,
        *,
        scenario: str,
        run_id: str,
        detail: str = "",
        **kw: Any,
    ) -> StageResult:
        """Shorthand for the common pass/fail case."""
        return self.record(
            StageResult(
                stage=stage,
                verdict=Verdict.PASS if ok else Verdict.FAIL,
                scenario=scenario,
                run_id=run_id,
                detail=detail,
                **kw,
            )
        )

    def skip_from(self, stage: Stage, *, scenario: str, run_id: str, why: str) -> None:
        """Mark `stage` and every later stage as never-reached.

        Called when a stage fails: everything downstream is unmeasured, and
        recording that explicitly is what makes the funnel honest.
        """
        start = STAGE_ORDER.index(stage)
        for later in STAGE_ORDER[start:]:
            self.record(
                StageResult(
                    stage=later,
                    verdict=Verdict.SKIP,
                    scenario=scenario,
                    run_id=run_id,
                    detail=why,
                )
            )

    @property
    def results(self) -> list[StageResult]:
        with self._lock:
            return list(self._results)

    @classmethod
    def load(cls, path: Path) -> list[dict]:
        """Read a scorecard back — used by the report generator."""
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Probe interfaces
# ---------------------------------------------------------------------------


@dataclass
class TrackTags:
    """Tags actually written to a file on disk."""

    path: Path
    albumartist: str | None
    artist: str | None
    album: str | None
    title: str | None
    track: int | None
    mb_trackid: str | None
    mb_albumid: str | None
    duration_s: float | None
    bitrate: int | None
    format: str | None


@dataclass
class TreeAudit:
    """Everything wrong with a music tree, in one pass.

    Every field is a *defect list*, so an empty audit is a clean tree and
    `bool(audit.clean)` is the S9 verdict.
    """

    root: Path
    audio_files: list[Path]
    #: Files that are not audio and not expected (partials, .DS_Store, logs).
    stray_files: list[Path]
    #: Directories containing nothing.
    empty_dirs: list[Path]
    #: Incomplete/partial transfer remnants.
    partial_files: list[Path]
    #: Artist folders that differ only by case or by a feat. clause —
    #: {canonical_name: [actual folder names]}. The core of the strict
    #: placement spec.
    artist_folder_variants: dict[str, list[str]]
    #: Files still sitting under downloads/complete that should have been
    #: consumed by an import.
    stranded_downloads: list[Path]

    @property
    def clean(self) -> bool:
        return not (
            self.stray_files
            or self.empty_dirs
            or self.partial_files
            or self.artist_folder_variants
            or self.stranded_downloads
        )


@dataclass
class BeetsReconciliation:
    """Where a beets profile's library DB and the disk disagree.

    This is the check that did not exist, and its absence is what let stale
    rows silently start eating new downloads.
    """

    profile: str
    #: Library rows whose file is gone. These cause false "already in the
    #: library" skips.
    rows_without_files: list[str]
    #: Audio files in the tree with no library row. Invisible to dedup.
    files_without_rows: list[Path]
    total_rows: int
    total_files: int

    @property
    def consistent(self) -> bool:
        return not (self.rows_without_files or self.files_without_rows)


class NavidromeProbe(ABC):
    """Navidrome as the user experiences it, over Subsonic.

    Deliberately independent of `app/services/navidrome_library.py`: if the
    service and the probe share a bug, the test proves nothing.
    """

    @abstractmethod
    def trigger_scan(self, wait: bool = True, timeout: float = 180.0) -> bool:
        """Start a scan; when `wait`, block until it finishes."""

    @abstractmethod
    def find_song(self, title: str, artist: str) -> dict | None:
        """S11/S13: is the track in the library, and with what metadata?"""

    @abstractmethod
    def list_playlists(self) -> list[dict]: ...

    @abstractmethod
    def playlist_songs(self, playlist_id: str) -> list[dict]: ...

    @abstractmethod
    def create_playlist(self, name: str) -> str: ...

    @abstractmethod
    def delete_playlist(self, playlist_id: str) -> bool:
        """Needed by U9 — musica itself has no delete, which is part of why
        the playlist lifecycle has never been tested."""

    @abstractmethod
    def song_count(self) -> int: ...


class FsProbe(ABC):
    """The music tree on the host, read directly."""

    @abstractmethod
    def audit(self, root: Path | None = None) -> TreeAudit: ...

    @abstractmethod
    def find_by_title(self, title: str) -> list[Path]:
        """Every file on disk whose name or tags match — how S10 counts
        copies."""

    @abstractmethod
    def snapshot(self) -> set[Path]:
        """Every file under the music root, for before/after diffing."""


class TagProbe(ABC):
    """Tags as written, read with mutagen — not as beets believes them."""

    @abstractmethod
    def read(self, path: Path) -> TrackTags: ...

    @abstractmethod
    def grade(self, path: Path, track: Any) -> tuple[bool, str]:
        """S8: does this file's metadata match the corpus expectation?

        `track` is a `tests.live.corpus.Track`. Returns (ok, reason) — the
        reason is quoted verbatim in the report, so write it for a human.
        """


class BeetsProbe(ABC):
    """The per-profile beets libraries, and whether they match reality."""

    @abstractmethod
    def reconcile(self, profile: str) -> BeetsReconciliation: ...

    @abstractmethod
    def items(self, profile: str) -> list[dict]: ...

    @abstractmethod
    def find_by_mb_trackid(self, mb_trackid: str) -> dict[str, list[dict]]:
        """Which profiles hold this recording — the cross-profile dedup
        question, asked directly."""


class LbProbe(ABC):
    """ListenBrainz, called directly, to know what a pull *should* have
    produced before judging what it did produce."""

    @abstractmethod
    def comfort_zone(self, count: int) -> list[dict]: ...

    @abstractmethod
    def fresh_picks(self, days: int) -> list[dict]: ...

    @abstractmethod
    def deep_cuts(self) -> list[dict]: ...
