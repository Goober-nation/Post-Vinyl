"""Startup values (#88, #95), until the config redesign.

Secrets and deployment values come from the environment; settings come from
the YAML file at ``PV_CONFIG_PATH`` (every config file is YAML, user
2026-10-29; ``post-vinyl.example.yaml`` documents each value). Read
once in ``main.create_app_from_env``; a missing or invalid value refuses to
start rather than running half-configured.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# Every source name each kind accepts; a new source adds its name here and to
# the comment in post-vinyl.example.yaml in the same change.
KNOWN_SOURCES: dict[str, tuple[str, ...]] = {
    "catalog": ("musicbrainz", "deezer", "listenbrainz"),
    "download": ("slskd",),
    "request": ("postradio", "yandex"),
}


class ConfigError(Exception):
    """A required environment variable is missing or invalid."""


def setting(default: Any, help: str, lo: int | None = None, hi: int | None = None,
            step: int | None = None) -> Any:  # fmt: skip
    """A settings dataclass field with what the settings schema shows (#109)."""
    return field(
        default=default, metadata={"help": help, "min": lo, "max": hi, "step": step}
    )


@dataclass(frozen=True)
class SlskdSettings:
    """Advanced slskd values from the YAML ``slskd`` section (#100, user 2026-10-29)."""

    # live 2026-10-27: 4/min held, ~5-6/min got throttled
    searches_per_minute: int = setting(
        4, "Searches started per minute, at most. Faster gets the Soulseek account "
        "throttled (results go near-empty while slskd looks healthy).", 1, 10, 1)  # fmt: skip
    # healthy ≈ 600 responses (research run)
    throttle_check_query: str = setting(
        "2020", "The search that checks the account isn't throttled before a request "
        "is given up as not found.")  # fmt: skip
    throttle_check_min_responses: int = setting(
        300, "The check counts as healthy at this many responses or more.", 50, 600, 50
    )
    throttle_pause_minutes: int = setting(
        10,
        "A failed check pauses all searching this long, then checks again.",
        1,
        60,
        1,
    )
    # downloads (#102, user 2026-10-29)
    queue_wait_seconds: int = setting(
        15, "A peer that keeps a download queued longer is skipped (counts as a "
        "failure for that peer). Files you pick in the slskd tab wait as long as "
        "it takes.", 5, 120, 5)  # fmt: skip
    candidates_per_download: int = setting(
        5, "Peers tried for one download before it is given up.", 1, 20, 1
    )
    peer_ban_failures: int = setting(
        3, "A peer is banned after this many failed downloads…", 1, 20, 1
    )
    peer_ban_days: int = setting(7, "…for this many days.", 1, 90, 1)


@dataclass(frozen=True)
class FilesSettings:
    """Folders from the YAML ``files`` section (#103). Shown in the settings, never
    edited there: they must match the Docker mounts (user, 2026-10-30)."""

    music_dir: str = setting("/music", "The music folder as Post-Vinyl sees it (its "
                             "container mount).")  # fmt: skip
    slskd_downloads_dir: str = setting(
        "downloads/complete/soulseek", "Inside the music folder: where slskd saves "
        "finished downloads.")  # fmt: skip
    library_dir: str = setting(
        "library", "Inside the music folder: Navidrome's library (Navidrome's music "
        "folder must be exactly this one).")  # fmt: skip


@dataclass(frozen=True)
class WorkersSettings:
    """How often the background workers run (#105, user 2026-10-29)."""

    pipeline_every_seconds: int = setting(
        10, "Downloaded files: verify, tag, move into the library, then one Navidrome "
        "scan.", 5, 300, 5)  # fmt: skip
    retag_after_minutes: int = setting(
        60, "A failed tagging is tried once more this long after; then the file moves "
        "untagged.", 5, 1440, 5)  # fmt: skip
    missing_check_minutes: int = setting(
        60, "How often files deleted outside Post-Vinyl are marked missing.", 5, 1440, 5
    )
    redispatch_every_seconds: int = setting(
        60, "Requests still waiting (e.g. the queue was paused) are started again.",
        10, 600, 10)  # fmt: skip
    cleanup_every_seconds: int = setting(
        60, "Finished and failed transfers are cleared at slskd, leftovers deleted.",
        10, 600, 10)  # fmt: skip
    unverifiable_keep_hours: int = setting(
        24, "Downloads that failed verification are deleted after this long.", 1, 720, 1
    )


def load_workers(path: Path) -> WorkersSettings:
    raw = _read(path).get("workers") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'workers' must be a section")
    unknown = set(raw) - set(vars(WorkersSettings()))
    if unknown:
        raise ConfigError(f"{path}: unknown workers settings {sorted(unknown)}")
    for name, value in raw.items():
        if type(value) is not int or value < 1:
            raise ConfigError(f"{path}: workers.{name} must be a positive int")
    return WorkersSettings(**raw)


def load_files(path: Path) -> FilesSettings:
    raw = _read(path).get("files") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'files' must be a section")
    defaults = FilesSettings()
    unknown = set(raw) - set(vars(defaults))
    if unknown:
        raise ConfigError(f"{path}: unknown files settings {sorted(unknown)}")
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{path}: files.{name} must be a folder path")
    return FilesSettings(**raw)


@dataclass(frozen=True)
class Settings:
    db_path: str
    navidrome_url: str
    public_origin: str  # e.g. https://music.example.com; the CSRF check compares to it
    dev: bool = False  # serves /docs
    # enabled sources per kind, in priority order (user, 2026-10-29)
    sources: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(KNOWN_SOURCES)
    )
    slskd: SlskdSettings = field(default_factory=SlskdSettings)
    files: FilesSettings = field(default_factory=FilesSettings)
    workers: WorkersSettings = field(default_factory=WorkersSettings)
    config_path: str = ""  # the YAML the values came from; admin edits go there (#109)
    slskd_url: str = ""
    slskd_api_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        origin = required("PV_PUBLIC_ORIGIN").rstrip("/")
        if not origin.startswith(("https://", "http://")) or origin.count("/") != 2:
            raise ConfigError(
                f"PV_PUBLIC_ORIGIN must be scheme://host[:port], got {origin!r}"
            )
        return cls(
            db_path=required("PV_DB_PATH"),
            navidrome_url=required("PV_NAVIDROME_URL"),
            public_origin=origin,
            dev=os.environ.get("PV_DEV", "").strip().lower() in ("1", "true", "yes"),
            sources=load_sources(Path(required("PV_CONFIG_PATH"))),
            slskd=load_slskd(Path(required("PV_CONFIG_PATH"))),
            files=load_files(Path(required("PV_CONFIG_PATH"))),
            workers=load_workers(Path(required("PV_CONFIG_PATH"))),
            config_path=required("PV_CONFIG_PATH"),
            slskd_url=required("PV_SLSKD_URL"),
            slskd_api_key=required("PV_SLSKD_API_KEY"),
        )


def _read(path: Path) -> dict[str, Any]:
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"can't read config {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def load_slskd(path: Path) -> SlskdSettings:
    """The optional ``slskd`` section; a value left out keeps its default."""
    raw = _read(path).get("slskd") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'slskd' must be a section")
    defaults = SlskdSettings()
    unknown = set(raw) - set(vars(defaults))
    if unknown:
        raise ConfigError(f"{path}: unknown slskd settings {sorted(unknown)}")
    for name, value in raw.items():
        want = type(getattr(defaults, name))
        if type(value) is not want or (want is int and value < 1) or value == "":
            raise ConfigError(
                f"{path}: slskd.{name} must be a positive {want.__name__}"
            )
    return SlskdSettings(**raw)


def load_sources(path: Path) -> dict[str, tuple[str, ...]]:
    """The ``sources`` lists from the YAML config, checked against KNOWN_SOURCES."""
    raw = _read(path).get("sources")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: missing 'sources' section")
    unknown_kinds = set(raw) - set(KNOWN_SOURCES)
    if unknown_kinds:
        raise ConfigError(f"{path}: unknown source kinds {sorted(unknown_kinds)}")
    out: dict[str, tuple[str, ...]] = {}
    for kind, allowed in KNOWN_SOURCES.items():
        names = raw.get(kind) or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"{path}: sources.{kind} must be a list of names")
        bad = [n for n in names if n not in allowed]
        if bad:
            raise ConfigError(
                f"{path}: sources.{kind} has unknown {bad}; possible: {list(allowed)}"
            )
        if len(set(names)) != len(names):
            raise ConfigError(f"{path}: sources.{kind} lists a source twice")
        out[kind] = tuple(names)
    return out


# ------------------------------------------------------------ settings schema + edits (#109)

SECTIONS: dict[str, type] = {
    "slskd": SlskdSettings,
    "workers": WorkersSettings,
    "files": FilesSettings,
}
NOT_EDITABLE = frozenset({"files"})  # must match the Docker mounts (user, 2026-10-30)
SOURCE_HELP = {
    "catalog": "Song catalogs for searching and browsing, in the order they're tried.",
    "download": "Where files are downloaded from.",
    "request": "What else may start a request besides a user's own pick.",
}


@dataclass(frozen=True)
class SchemaField:
    """One setting as the frontend draws it (user, 2026-10-30)."""

    key: str  # "section.name"
    section: str
    type: str  # int | text | choice | ordered_list | sources
    default: Any
    help: str
    min: int | None = None
    max: int | None = None
    step: int | None = None
    choices: tuple[str, ...] = ()
    editable: bool = True
    applies: str = "restart"  # "now" | "restart"


def admin_schema() -> list[SchemaField]:
    """The install's settings: sources, then each YAML section's fields."""
    out = [
        SchemaField(
            f"sources.{kind}",
            "sources",
            "ordered_list",
            list(names),
            SOURCE_HELP[kind],
            choices=names,
        )
        for kind, names in KNOWN_SOURCES.items()
    ]
    for section, cls in SECTIONS.items():
        for f in fields(cls):
            meta = f.metadata
            out.append(SchemaField(
                f"{section}.{f.name}", section, "int" if f.type in (int, "int") else "text",
                f.default, meta.get("help", ""), meta.get("min"), meta.get("max"),
                meta.get("step"), editable=section not in NOT_EDITABLE,
            ))  # fmt: skip
    return out


def file_values(path: Path) -> dict[str, Any]:
    """What the config file says now (defaults where it says nothing)."""
    out: dict[str, Any] = {
        f"sources.{k}": list(v) for k, v in load_sources(path).items()
    }
    for section, loaded in (("slskd", load_slskd(path)), ("workers", load_workers(path)),
                            ("files", load_files(path))):  # fmt: skip
        out.update({f"{section}.{k}": v for k, v in vars(loaded).items()})
    return out


def check_value(f: SchemaField, value: Any) -> Any:
    """``value`` if it fits the field, else ConfigError (the trust boundary)."""
    if not f.editable:
        raise ConfigError(f"{f.key} is set in the config file with the Docker mounts")
    if f.type == "int":
        if type(value) is not int:
            raise ConfigError(f"{f.key} must be a whole number")
        if (f.min is not None and value < f.min) or (
            f.max is not None and value > f.max
        ):
            raise ConfigError(f"{f.key} must be between {f.min} and {f.max}")
        return value
    if f.type == "text":
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise ConfigError(f"{f.key} must be one line of text")
        return value.strip()
    # ordered_list (sources)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{f.key} must be a list of names")
    bad = [v for v in value if v not in f.choices]
    if bad or len(set(value)) != len(value):
        raise ConfigError(
            f"{f.key}: unknown or repeated {bad or value}; possible: {list(f.choices)}"
        )
    return value


def _yaml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a double-quoted YAML string
    return str(value)


_SECTION_RE = re.compile(r"^([A-Za-z_]\w*):\s*(#.*)?$")


def _edit(text: str, section: str, key: str, value: Any) -> str:
    """``text`` with ``section.key`` set; every other line, comments included,
    stays as it is (user, 2026-10-30). A value written over several lines is
    refused: that one is edited by hand."""
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^(\s+){re.escape(key)}:(\s*)(.*?)(\s+#.*)?$")
    start = next((i for i, ln in enumerate(lines)
                  if (m := _SECTION_RE.match(ln.rstrip("\n"))) and m.group(1) == section), None)  # fmt: skip
    new = _yaml_value(value)
    if start is None:
        sep = "" if not lines or lines[-1].endswith("\n") else "\n"
        return "".join(lines) + f"{sep}\n{section}:\n  {key}: {new}\n"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i][0].isspace()), len(lines))  # fmt: skip
    for i in range(start + 1, end):
        m = key_re.match(lines[i].rstrip("\n"))
        if not m:
            continue
        if not m.group(3) or m.group(3) in ("|", ">"):
            raise ConfigError(
                f"{section}.{key} is written over several lines; edit it by hand"
            )
        lines[i] = f"{m.group(1)}{key}:{m.group(2) or ' '}{new}{m.group(4) or ''}\n"
        return "".join(lines)
    indent = "  "
    for ln in lines[start + 1 : end]:
        if m := re.match(r"^(\s+)\w", ln):
            indent = m.group(1)
            break
    lines.insert(start + 1, f"{indent}{key}: {new}\n")
    return "".join(lines)


def write_values(path: Path, changes: dict[str, Any], now: int) -> dict[str, Any]:
    """Apply admin edits to the config file; they take effect at the next restart.

    Every change is checked against the schema first, the edited text must load
    with the same loaders the app starts with, and the old file is kept as
    ``<name>.<now>.bak`` before it's replaced. Returns the file's new values."""
    schema = {f.key: f for f in admin_schema()}
    unknown = sorted(set(changes) - set(schema))
    if unknown:
        raise ConfigError(f"unknown settings {unknown}")
    checked = {k: check_value(schema[k], v) for k, v in changes.items()}
    text = path.read_text(encoding="utf-8")
    for key, value in checked.items():
        section, name = key.split(".", 1)
        text = _edit(text, section, name, value)
    candidate = path.with_name(f".{path.name}.{now}.new")
    candidate.write_text(text, encoding="utf-8")
    try:
        values = file_values(candidate)  # refuses anything the app wouldn't start with
        if any(values[k] != v for k, v in checked.items()):
            raise ConfigError(
                "the edited file doesn't read back as written; nothing changed"
            )
        shutil.copy2(path, path.with_name(f"{path.name}.{now}.bak"))
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)
    return values


def running_values(settings: Settings) -> dict[str, Any]:
    """The values the app runs with, shaped like ``file_values``."""
    out: dict[str, Any] = {f"sources.{k}": list(v) for k, v in settings.sources.items()}
    for section in SECTIONS:
        out.update(
            {f"{section}.{k}": v for k, v in vars(getattr(settings, section)).items()}
        )
    return out
