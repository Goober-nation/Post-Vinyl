"""
Host-side path resolution for the probes.

The probes run on the **host**; musica, beets and Navidrome all speak
**container** paths. Every path a probe reads out of a database, a config
file or an API response therefore has to be translated before it can be
opened, and getting that translation wrong is indistinguishable from "the
file isn't there" — which is exactly the verdict the probes exist to hand
out. So it lives in one place, with the mapping stated explicitly:

    /music/<anything>   ->   $MUSIC_HOST_DIR/<anything>
    /app/data/<any>     ->   <repo>/app_data/<any>

Both come from `docker-compose.yml` (`${MUSIC_HOST_DIR}:/music` and
`./app_data:/app/data`), and the tree names under them come from
`config/config.toml` (`[paths]`), not from hardcoded strings — the user can
rename `Searches`/`Discovery` and an audit that kept looking at the old
names would report a spotless empty tree.

One macOS wrinkle that matters: the configured tree names are `Searches`
and `Discovery` while the directories that actually exist on this host are
`searches` and `discovery`. APFS is case-insensitive by default so both the
container and `Path.exists()` are happy, but `os.walk` yields the real
on-disk spelling. `resolve_ci` closes that gap so a probe never reports a
missing tree that is sitting right there under a different case.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Container-side mount points, from docker-compose.yml.
CONTAINER_MUSIC_ROOT = Path("/music")
CONTAINER_DATA_ROOT = Path("/app/data")

#: Host side of the `./app_data:/app/data` bind mount.
HOST_DATA_ROOT = REPO_ROOT / "app_data"

DEFAULT_MUSIC_HOST_DIR = Path.home() / "Music" / "library"


@lru_cache(maxsize=1)
def read_env(env_path: Path | None = None) -> dict[str, str]:
    """Parse the repo's `.env` — the same file `docker compose` reads.

    Deliberately not `python-dotenv`: this must never mutate `os.environ`,
    because a probe that leaks secrets into the process environment changes
    the behaviour of the very app it is measuring.
    """
    path = env_path or (REPO_ROOT / ".env")
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


@lru_cache(maxsize=1)
def read_config(config_path: Path | None = None) -> dict:
    """The app's own `config/config.toml`, as the container sees it."""
    path = config_path or (REPO_ROOT / "config" / "config.toml")
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def env_value(key: str, default: str = "") -> str:
    """`.env` first, then the real environment. `.env` wins because that is
    what the running containers were started with."""
    return read_env().get(key) or os.environ.get(key) or default


def resolve_ci(parent: Path, name: str) -> Path:
    """`parent/name`, corrected to the spelling that actually exists.

    Returns the exact-case path when nothing matches, so callers can still
    use the result to create or to report a missing directory.
    """
    exact = parent / name
    if exact.exists():
        # On a case-insensitive filesystem `exact` exists even when the real
        # entry is spelled differently — find the real spelling so walks and
        # string comparisons agree.
        try:
            for entry in parent.iterdir():
                if entry.name == name:
                    return entry
            for entry in parent.iterdir():
                if entry.name.casefold() == name.casefold():
                    return entry
        except OSError:
            return exact
        return exact
    if not parent.is_dir():
        return exact
    try:
        for entry in parent.iterdir():
            if entry.name.casefold() == name.casefold():
                return entry
    except OSError:
        pass
    return exact


def music_host_root() -> Path:
    """Host directory bind-mounted at `/music` (`MUSIC_HOST_DIR` in .env)."""
    raw = env_value("MUSIC_HOST_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_MUSIC_HOST_DIR


def _paths_section() -> dict:
    return read_config().get("paths", {}) or {}


def container_music_dir() -> Path:
    return Path(_paths_section().get("music_dir", str(CONTAINER_MUSIC_ROOT)))


def container_data_dir() -> Path:
    return Path(_paths_section().get("data_dir", str(CONTAINER_DATA_ROOT)))


def to_host(path: Path | str) -> Path:
    """Translate a container path to its host equivalent.

    Anything that isn't under a known mount is returned unchanged — a
    relative path or an already-host path passes straight through, which is
    what lets callers hand this whatever a database happened to store.
    """
    p = Path(path)
    if not p.is_absolute():
        return p
    for container_root, host_root in (
        (container_music_dir(), music_host_root()),
        (CONTAINER_MUSIC_ROOT, music_host_root()),
        (container_data_dir(), HOST_DATA_ROOT),
        (CONTAINER_DATA_ROOT, HOST_DATA_ROOT),
    ):
        try:
            relative = p.relative_to(container_root)
        except ValueError:
            continue
        resolved = host_root
        for part in relative.parts:
            resolved = resolve_ci(resolved, part)
        return resolved
    return p


def tree_path(kind: str) -> Path:
    """Host path of one managed tree: searches | discovery | library |
    downloads."""
    section = _paths_section()
    names = {
        "searches": section.get("searches_dir", "Searches"),
        "discovery": section.get("discovery_dir", "Discovery"),
        "library": section.get("library_dir", "library"),
        "downloads": section.get("download_dir", "downloads"),
    }
    if kind not in names:
        raise KeyError(f"unknown tree {kind!r}; expected one of {sorted(names)}")
    return resolve_ci(music_host_root(), names[kind])


def artist_tree_paths() -> list[Path]:
    """The trees whose immediate children are *artist folders*.

    `downloads` is deliberately absent: its top level is peer usernames, and
    grading those as artist folders would report a defect for every stranger
    on Soulseek.
    """
    return [tree_path(k) for k in ("searches", "discovery", "library")]


def beets_profiles_dir() -> Path:
    """Host path of the directory holding `<profile>.db` / `<profile>.yaml`."""
    return to_host(container_data_dir() / "beets")


def musica_db_path() -> Path:
    return to_host(container_data_dir() / "musica.db")
