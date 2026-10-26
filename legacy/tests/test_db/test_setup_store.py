"""Tests for SetupStore — first-run setup wizard state."""

from pathlib import Path

import pytest

from app.db.database import Database
from app.db.setup_store import TUTORIAL_DISMISSED, WIZARD_COMPLETED, SetupStore


def _make_config(tmpdir):
    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmpdir) / "data")

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    return cfg


@pytest.fixture
def store(tmp_path):
    config = _make_config(str(tmp_path))
    db = Database(config)
    db.initialize_schema()
    store = SetupStore(db)
    yield store
    db.close()


class TestSetupStore:
    def test_unset_key_returns_none(self, store):
        assert store.get("nope") is None
        assert store.is_flag_set(WIZARD_COMPLETED) is False

    def test_set_and_get(self, store):
        store.set("foo", "bar")
        assert store.get("foo") == "bar"

    def test_set_overwrites(self, store):
        store.set("foo", "bar")
        store.set("foo", "baz")
        assert store.get("foo") == "baz"

    def test_flag_helpers(self, store):
        assert store.is_flag_set(TUTORIAL_DISMISSED) is False
        store.set_flag(TUTORIAL_DISMISSED)
        assert store.is_flag_set(TUTORIAL_DISMISSED) is True

    def test_wizard_completed_flag(self, store):
        assert store.is_flag_set(WIZARD_COMPLETED) is False
        store.set_flag(WIZARD_COMPLETED)
        assert store.is_flag_set(WIZARD_COMPLETED) is True
