"""Shared fixtures for the store tests: a real SQLite database with every
migration applied, so triggers, cascades and constraints are exercised."""

from types import SimpleNamespace

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.file_store import FileStore
from app.db.peer_store import PeerStore
from app.db.request_store import RequestStore


@pytest.fixture
def db(tmp_path):
    database = Database(SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path))))
    database.initialize_schema()
    yield database
    database.close()


@pytest.fixture
def requests_(db):
    return RequestStore(db)


@pytest.fixture
def downloads(db):
    return DownloadStore(db)


@pytest.fixture
def files(db):
    return FileStore(db)


@pytest.fixture
def peers(db):
    return PeerStore(db)
