"""
Measurement instruments for the live pipeline suite.

`contract.py` is the binding interface — the stage model, the verdicts, the
scorecard and the abstract probes. Everything else here implements it
against the real stack, with no mocks anywhere: if a probe cannot reach the
thing it measures, that is a finding, not something to stub out.

    from tests.live.probes import Probes

    probes = Probes()          # or use the session-scoped `probes` fixture
    probes.navidrome.find_song("Alright", "Kendrick Lamar")
    probes.fs.audit().clean
    probes.tags.grade(path, corpus_track)
    probes.beets.reconcile("searches")
    probes.lb.deep_cuts()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests.live.probes.beets_lib import PROFILES, LiveBeetsProbe, path_key
from tests.live.probes.contract import (
    STAGE_ORDER,
    BeetsProbe,
    BeetsReconciliation,
    FsProbe,
    LbProbe,
    NavidromeProbe,
    Scorecard,
    Stage,
    StageResult,
    TagProbe,
    TrackTags,
    TreeAudit,
    Verdict,
)
from tests.live.probes.fs import AUDIO_EXTS, LiveFsProbe, is_audio
from tests.live.probes.lb import LbProbeError, LiveLbProbe
from tests.live.probes.naming import (
    artist_key,
    artist_matches,
    canonical_artist,
    group_artist_variants,
    has_feat,
    merge_variant_groups,
    strip_feat,
    text_key,
    title_key_loose,
)
from tests.live.probes.navidrome import (
    DEFAULT_NAVIDROME_URL,
    LiveNavidromeProbe,
    NavidromeProbeError,
)
from tests.live.probes.paths import (
    REPO_ROOT,
    artist_tree_paths,
    beets_profiles_dir,
    music_host_root,
    to_host,
    tree_path,
)
from tests.live.probes.tags import LiveTagProbe, TagReadError, grade_tags


@dataclass
class Probes:
    """Every instrument, in one place.

    Construction is cheap and does no I/O, so this is safe to build in a
    session fixture that a skipped run never uses.
    """

    navidrome: LiveNavidromeProbe = field(default_factory=LiveNavidromeProbe)
    fs: LiveFsProbe = field(default_factory=LiveFsProbe)
    tags: LiveTagProbe = field(default_factory=LiveTagProbe)
    beets: LiveBeetsProbe = field(default_factory=LiveBeetsProbe)
    lb: LiveLbProbe = field(default_factory=LiveLbProbe)

    @property
    def music_root(self) -> Path:
        return self.fs.root


__all__ = [
    "AUDIO_EXTS",
    "DEFAULT_NAVIDROME_URL",
    "PROFILES",
    "REPO_ROOT",
    "STAGE_ORDER",
    "BeetsProbe",
    "BeetsReconciliation",
    "FsProbe",
    "LbProbe",
    "LbProbeError",
    "LiveBeetsProbe",
    "LiveFsProbe",
    "LiveLbProbe",
    "LiveNavidromeProbe",
    "LiveTagProbe",
    "NavidromeProbe",
    "NavidromeProbeError",
    "Probes",
    "Scorecard",
    "Stage",
    "StageResult",
    "TagProbe",
    "TagReadError",
    "TrackTags",
    "TreeAudit",
    "Verdict",
    "artist_key",
    "artist_matches",
    "artist_tree_paths",
    "beets_profiles_dir",
    "canonical_artist",
    "grade_tags",
    "group_artist_variants",
    "has_feat",
    "is_audio",
    "merge_variant_groups",
    "music_host_root",
    "path_key",
    "strip_feat",
    "text_key",
    "title_key_loose",
    "to_host",
    "tree_path",
]
