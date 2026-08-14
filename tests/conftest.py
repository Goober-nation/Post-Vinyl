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


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
