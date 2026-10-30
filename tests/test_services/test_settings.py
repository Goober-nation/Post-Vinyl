"""Settings (#109): the schema, comment-keeping YAML edits, user and install settings."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import (
    ConfigError,
    Settings,
    admin_schema,
    file_values,
    load_slskd,
    load_sources,
    write_values,
)
from app.services.settings import InstallSettings, UserSettings
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.users import UsersStore

EXAMPLE = Path(__file__).parents[2] / "config" / "post-vinyl.example.yaml"
NOW = 1_800_000_000


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "post-vinyl.yaml"
    shutil.copy(EXAMPLE, p)
    return p


def test_schema_comes_from_the_code_and_folders_are_read_only() -> None:
    schema = {f.key: f for f in admin_schema()}
    spm = schema["slskd.searches_per_minute"]
    assert (spm.type, spm.default, spm.min, spm.max, spm.applies) == (
        "int",
        4,
        1,
        10,
        "restart",
    )
    assert spm.help.startswith("Searches started per minute")
    assert schema["sources.catalog"].choices == (
        "musicbrainz",
        "deezer",
        "listenbrainz",
    )
    assert (
        not schema["files.music_dir"].editable
        and schema["workers.retag_after_minutes"].editable
    )


def test_an_edit_keeps_every_comment_and_untouched_line(cfg: Path) -> None:
    before = cfg.read_text(encoding="utf-8").splitlines()
    values = write_values(cfg, {"slskd.searches_per_minute": 3,
                                "sources.catalog": ["deezer", "musicbrainz"]}, NOW)  # fmt: skip
    after = cfg.read_text(encoding="utf-8").splitlines()
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert changed == [
        (
            "  catalog: [musicbrainz, deezer, listenbrainz]",
            "  catalog: [deezer, musicbrainz]",
        ),
        ("  searches_per_minute: 4", "  searches_per_minute: 3"),
    ]
    assert values["slskd.searches_per_minute"] == 3
    assert (cfg.parent / f"post-vinyl.yaml.{NOW}.bak").read_text(
        encoding="utf-8"
    ).splitlines() == before
    assert not list(cfg.parent.glob(".*.new"))


def test_inline_comments_missing_keys_and_sections(cfg: Path) -> None:
    cfg.write_text("sources:\n  catalog: [musicbrainz]  # mine\n  download: [slskd]\n",
                   encoding="utf-8")  # fmt: skip
    write_values(cfg, {"sources.catalog": ["deezer"], "workers.retag_after_minutes": 30,
                       "slskd.throttle_check_query": 'say "hi"'}, NOW)  # fmt: skip
    text = cfg.read_text(encoding="utf-8")
    assert "  catalog: [deezer]  # mine\n" in text
    assert "\nworkers:\n  retag_after_minutes: 30\n" in text
    assert load_slskd(cfg).throttle_check_query == 'say "hi"'
    write_values(cfg, {"slskd.peer_ban_days": 9}, NOW + 1)  # a key missing in a section
    assert (
        load_slskd(cfg).peer_ban_days == 9
        and load_slskd(cfg).throttle_check_query == 'say "hi"'
    )


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"slskd.searches_per_minute": 11}, "between 1 and 10"),
        ({"slskd.searches_per_minute": "4"}, "whole number"),
        ({"files.music_dir": "/elsewhere"}, "Docker mounts"),
        ({"sources.catalog": ["spotify"]}, "unknown or repeated"),
        ({"slskd.nope": 1}, "unknown settings"),
        ({"slskd.throttle_check_query": "a\nb"}, "one line"),
    ],
)
def test_bad_values_change_nothing(
    cfg: Path, changes: dict[str, object], why: str
) -> None:
    before = cfg.read_bytes()
    with pytest.raises(ConfigError, match=why):
        write_values(cfg, {"workers.retag_after_minutes": 30, **changes}, NOW)
    assert cfg.read_bytes() == before and not list(cfg.parent.glob("*.bak"))


def test_a_value_written_over_several_lines_is_left_to_the_user(cfg: Path) -> None:
    cfg.write_text("sources:\n  catalog:\n    - musicbrainz\n  download: [slskd]\n",
                   encoding="utf-8")  # fmt: skip
    with pytest.raises(ConfigError, match="edit it by hand"):
        write_values(cfg, {"sources.catalog": ["deezer"]}, NOW)


def test_install_settings_say_what_waits_for_a_restart(cfg: Path) -> None:
    running = Settings("db", "nd", "https://x", sources=load_sources(cfg),
                       config_path=str(cfg))  # as the app loads it  # fmt: skip
    svc = InstallSettings(running, lambda: NOW)
    assert svc.get().restart_pending == []
    view = svc.set({"workers.cleanup_every_seconds": 120})
    assert view.values["workers.cleanup_every_seconds"] == 120
    assert view.restart_pending == ["workers.cleanup_every_seconds"]
    assert file_values(cfg)["workers.cleanup_every_seconds"] == 120


def test_a_read_only_config_folder_refuses_without_naming_it(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = Settings("db", "nd", "https://x", sources=load_sources(cfg),
                       config_path=str(cfg))  # fmt: skip

    def read_only(*_: object) -> None:  # what a :ro mount raises (live #113)
        raise OSError(30, "Read-only file system", str(cfg.parent / ".x.new"))

    monkeypatch.setattr("app.services.settings.write_values", read_only)
    with pytest.raises(ConfigError) as e:
        InstallSettings(running, lambda: NOW).set(
            {"workers.cleanup_every_seconds": 120}
        )
    assert str(e.value) == "can't write post-vinyl.yaml: Read-only file system"


def test_user_settings_default_check_everything_first_and_drive_sources(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
    prefs = SourcePreferences(
        db, {"catalog": ("musicbrainz", "deezer"), "download": ("slskd",)}
    )
    svc = UserSettings(db, prefs, lambda: NOW)
    got = svc.get(uid)
    assert got["catalog.results_per_search"] == 25 and got["downloads.state"] == ""
    assert got["sources.catalog"] == {"order": ["musicbrainz", "deezer"], "off": []}
    with pytest.raises(ConfigError):
        svc.set(uid, {"downloads.page_size": 30, "downloads.state": "bogus"})
    assert svc.get(uid)["downloads.page_size"] == 20  # nothing stored
    got = svc.set(uid, {"downloads.page_size": 30,
                        "sources.catalog": {"order": ["deezer", "musicbrainz"], "off": ["musicbrainz"]}})  # fmt: skip
    assert got["downloads.page_size"] == 30
    assert prefs.for_user(uid, "catalog") == ["deezer"]
    assert got["sources.catalog"] == {
        "order": ["deezer", "musicbrainz"],
        "off": ["musicbrainz"],
    }
