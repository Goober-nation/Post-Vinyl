from pathlib import Path
from types import SimpleNamespace

from app.services.bootstrap import _NDIGNORE_MARKER, ensure_navidrome_files


def _config(music_dir: Path, download_dir: str = "downloads"):
    return SimpleNamespace(
        paths=SimpleNamespace(music_dir=music_dir, download_dir=download_dir)
    )


class TestEnsureNavidromeFiles:
    def test_creates_ndignore_and_favorites_when_missing(self, tmp_path):
        ensure_navidrome_files(_config(tmp_path))

        ndignore = tmp_path / ".ndignore"
        favorites = tmp_path / "favorites.nsp"
        assert ndignore.exists()
        assert ndignore.read_text() == f"{_NDIGNORE_MARKER}/downloads/\n"
        assert favorites.exists()
        assert '"loved": true' in favorites.read_text()

    def test_uses_configured_download_dir(self, tmp_path):
        ensure_navidrome_files(_config(tmp_path, download_dir="Grabbed"))

        assert (tmp_path / ".ndignore").read_text() == f"{_NDIGNORE_MARKER}/Grabbed/\n"

    def test_does_not_overwrite_hand_edited_ndignore(self, tmp_path):
        (tmp_path / ".ndignore").write_text("custom\n", encoding="utf-8")
        (tmp_path / "favorites.nsp").write_text("{}", encoding="utf-8")

        ensure_navidrome_files(_config(tmp_path))

        assert (tmp_path / ".ndignore").read_text() == "custom\n"
        assert (tmp_path / "favorites.nsp").read_text() == "{}"

    def test_rewrites_bootstrap_owned_ndignore_when_download_dir_changes(
        self, tmp_path
    ):
        (tmp_path / ".ndignore").write_text(
            f"{_NDIGNORE_MARKER}/downloads/\n", encoding="utf-8"
        )

        ensure_navidrome_files(_config(tmp_path, download_dir="Grabbed"))

        assert (tmp_path / ".ndignore").read_text() == f"{_NDIGNORE_MARKER}/Grabbed/\n"

    def test_leaves_bootstrap_owned_ndignore_alone_when_already_current(self, tmp_path):
        path = tmp_path / ".ndignore"
        path.write_text(f"{_NDIGNORE_MARKER}/downloads/\n", encoding="utf-8")
        before = path.stat().st_mtime_ns

        ensure_navidrome_files(_config(tmp_path))

        assert path.stat().st_mtime_ns == before

    def test_missing_music_dir_logs_warning_not_raise(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        ensure_navidrome_files(_config(missing))  # must not raise
