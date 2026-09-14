"""
Shared pytest fixtures.

Isolates the working directory for every test so app.config.Config()'s
default relative paths (Path("config.toml"), Path(".env")) never resolve
to this repo's real files — regardless of what secrets a real .env
(or an accidentally re-created .env) might contain. Tests that need a real
config/env file already pass explicit tmpdir paths (see test_config.py),
so this only changes what the *default*, unspecified path resolves to.
"""

import pytest


def pytest_configure(config):
    """Register tests/live/'s cost-tier markers here too.

    tests/live/conftest.py already registers these, but that conftest only
    loads when tests/live/ itself is collected. tests/test_live_harness.py
    imports directly from tests/live/test_stages_import.py (to unit-test its
    pure parsing helpers without the stack), which executes that module's
    top-level `pytestmark = [pytest.mark.needs_existing_corpus]` even on a
    run that ignores tests/live/ entirely — e.g. `pytest tests/
    --ignore=tests/live` — triggering PytestUnknownMarkWarning without this.
    """
    for name in ("no_downloads", "needs_existing_corpus", "queues_downloads", "slow"):
        config.addinivalue_line(
            "markers", f"{name}: see tests/live/conftest.py and tests/live/INDEX.md"
        )


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
