"""
`FsProbe` — the music tree on the host, walked directly.

Nothing here asks musica, beets or Navidrome what the tree looks like. The
filesystem is the only thing all three of them are supposed to agree with,
so it is the only thing worth measuring: a beets row, a Navidrome song entry
and a downloads row can all be confidently wrong about the same file at the
same time, and only `os.walk` settles it.

`audit()` returns a `TreeAudit` whose every field is a list of defects, so
an empty audit *is* a clean tree and `audit.clean` is the S9 verdict.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from tests.live.probes.contract import FsProbe, TreeAudit
from tests.live.probes.naming import (
    artist_key,
    group_artist_variants,
    merge_variant_groups,
    text_key,
    title_key_loose,
)
from tests.live.probes.paths import artist_tree_paths, music_host_root, tree_path
from tests.live.probes.tags import AUDIO_EXTS, LiveTagProbe, TagReadError

#: Non-audio files that legitimately live in an album folder.
EXPECTED_NON_AUDIO = frozenset(
    {"cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", ".nomedia"}
)

#: Suffixes that mean "a transfer that never finished".
PARTIAL_SUFFIXES = frozenset(
    {".part", ".partial", ".incomplete", ".tmp", ".temp", ".crdownload", ".!qb"}
)

#: Directory names that hold in-flight transfers rather than finished files.
PARTIAL_DIR_NAMES = frozenset({"incomplete", "incompletes", "temp", "tmp"})


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


class LiveFsProbe(FsProbe):
    def __init__(
        self,
        root: Path | None = None,
        tag_probe: LiveTagProbe | None = None,
    ) -> None:
        self.root = Path(root) if root else music_host_root()
        self.tags = tag_probe or LiveTagProbe()

    # -- walking -----------------------------------------------------------

    def _walk(self, root: Path) -> Iterable[tuple[Path, list[str], list[str]]]:
        """`os.walk` over real directories only, newest-safe.

        Symlinks are not followed: the host tree is bind-mounted into three
        containers and a followed link would double-count every file.
        """
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            dirnames.sort()
            filenames.sort()
            yield here, dirnames, filenames

    def files(self, root: Path | None = None) -> list[Path]:
        base = Path(root) if root else self.root
        if not base.exists():
            return []
        found: list[Path] = []
        for here, _dirs, filenames in self._walk(base):
            found.extend(here / name for name in filenames)
        return found

    def snapshot(self) -> set[Path]:
        return set(self.files(self.root))

    def audio_files(self, root: Path | None = None) -> list[Path]:
        return [p for p in self.files(root) if is_audio(p)]

    # -- classification ----------------------------------------------------

    @staticmethod
    def _is_partial(path: Path) -> bool:
        if path.suffix.lower() in PARTIAL_SUFFIXES:
            return True
        if any(part.lower() in PARTIAL_DIR_NAMES for part in path.parent.parts):
            return True
        if is_audio(path):
            try:
                # A zero-byte audio file is a transfer that never delivered a
                # byte; Navidrome will happily index it as a silent track.
                return path.stat().st_size == 0
            except OSError:
                return False
        return False

    def _empty_dirs(self, root: Path) -> list[Path]:
        """Directories with no file anywhere beneath them.

        Only the *topmost* directory of each empty subtree is reported —
        listing every level of `a/b/c/d` when all four are empty is four
        lines of the same fact.
        """
        has_files: dict[Path, bool] = {}
        for here, _dirs, filenames in self._walk(root):
            has_files[here] = bool(filenames)
        # os.walk is top-down, so a directory is seen before its children:
        # roll the answer back up, deepest first, once every directory is
        # known.
        for here in sorted(has_files, key=lambda p: len(p.parts), reverse=True):
            if has_files[here]:
                parent = here.parent
                while parent in has_files and not has_files[parent]:
                    has_files[parent] = True
                    parent = parent.parent

        empty = []
        for here, present in has_files.items():
            if present or here == root:
                continue
            if has_files.get(here.parent, True):
                empty.append(here)
        return sorted(empty)

    def _artist_folders(self, root: Path) -> list[list[str]]:
        """Artist-folder names, grouped per managed tree.

        Grouped per tree because "the same artist in Searches and in
        Discovery" is two trees each holding one folder, not one artist
        split in two — see `naming.merge_variant_groups`.
        """
        downloads = tree_path("downloads")
        trees = [
            t for t in artist_tree_paths() if t.exists() and (t == root or root in t.parents)
        ]
        if not trees:
            trees = [root] if root != downloads else []

        per_tree: list[list[str]] = []
        for tree in trees:
            if tree == downloads or not tree.is_dir():
                continue
            try:
                names = [
                    e.name
                    for e in tree.iterdir()
                    if e.is_dir() and not e.name.startswith(".")
                ]
            except OSError:
                continue
            if names:
                per_tree.append(names)
        return per_tree

    # -- the audit ---------------------------------------------------------

    def audit(self, root: Path | None = None) -> TreeAudit:
        base = Path(root) if root else self.root
        downloads = tree_path("downloads")

        audio: list[Path] = []
        stray: list[Path] = []
        partial: list[Path] = []
        stranded: list[Path] = []

        for path in self.files(base):
            if self._is_partial(path):
                partial.append(path)
                continue
            if is_audio(path):
                audio.append(path)
                if path == downloads or downloads in path.parents:
                    stranded.append(path)
                continue
            if path.name.lower() in EXPECTED_NON_AUDIO:
                continue
            stray.append(path)

        variants = merge_variant_groups(
            group_artist_variants(names) for names in self._artist_folders(base)
        )

        return TreeAudit(
            root=base,
            audio_files=sorted(audio),
            stray_files=sorted(stray),
            empty_dirs=self._empty_dirs(base) if base.exists() else [],
            partial_files=sorted(partial),
            artist_folder_variants=variants,
            stranded_downloads=sorted(stranded),
        )

    # -- lookups -----------------------------------------------------------

    def find_by_title(self, title: str) -> list[Path]:
        """Every audio file on disk that looks like this track.

        Filename first (cheap), tags second (authoritative). The loose key
        is used here on purpose: the job is to find *every copy* however a
        peer named it, including `Feather [Explicit].flac`. Grading whether
        that name is acceptable is S8's job, not this one's.
        """
        wanted = title_key_loose(title)
        if not wanted:
            return []
        hits: list[Path] = []
        for path in self.audio_files():
            if wanted in title_key_loose(path.stem):
                hits.append(path)
                continue
            try:
                tags = self.tags.read(path)
            except (FileNotFoundError, TagReadError, OSError):
                continue
            if tags.title and text_key(tags.title) == text_key(title):
                hits.append(path)
        return sorted(hits)

    def find_by_artist_folder(self, artist: str) -> list[Path]:
        """Artist folders matching `artist` across every managed tree —
        including the case/punctuation/feat. variants that should not exist."""
        wanted = artist_key(artist)
        found: list[Path] = []
        for tree in artist_tree_paths():
            if not tree.is_dir():
                continue
            try:
                found.extend(
                    e
                    for e in tree.iterdir()
                    if e.is_dir() and artist_key(e.name) == wanted
                )
            except OSError:
                continue
        return sorted(found)
