"""Icons load with no session; anything else at the root stays closed."""

from pathlib import Path

import pytest

from app.middleware import ICON_FILES, is_web_file


@pytest.mark.parametrize("path", sorted(ICON_FILES))
def test_every_icon_is_open_and_exists(path: str) -> None:
    assert is_web_file("GET", path)
    assert (Path("web/public") / path.lstrip("/")).is_file()


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/secret.png"), ("POST", "/favicon.ico")]
)
def test_other_files_stay_closed(method: str, path: str) -> None:
    assert not is_web_file(method, path)
