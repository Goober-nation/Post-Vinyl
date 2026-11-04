"""Is an outside service answering? Any answer below 500 means it is up."""

from __future__ import annotations

import requests


class Unreachable(Exception):
    """No answer, or the service answered that it's failing."""


def check_reachable(url: str, timeout_s: float = 5) -> None:
    try:
        status = requests.get(url, timeout=timeout_s, allow_redirects=False).status_code
    except requests.RequestException as e:
        raise Unreachable(url) from e
    if status >= 500:
        raise Unreachable(f"{url}: HTTP {status}")
