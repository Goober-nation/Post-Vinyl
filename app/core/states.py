"""Allowed ``track_state`` / ``file_state`` transitions (#93).

Every state change goes through here, so an illegal jump (``requested`` →
``indexed``) raises instead of being written. Who may call which function is
in ``rebuild/architecture.md`` §3; the state lists match the CHECK constraints
in ``stores/migrations/001_initial.sql``.
"""

from __future__ import annotations

TRACK_STATES = frozenset({
    "requested", "dropped", "reRequested", "searching", "found", "notFound",
    "midDownload", "downloaded", "notDownloaded", "badDownload",
})  # fmt: skip
FILE_STATES = frozenset({
    "downloaded", "verified", "tagged", "moved", "indexed", "missing", "deleted",
})  # fmt: skip

_ENDED = ("notFound", "notDownloaded", "badDownload", "dropped", "downloaded")

# user, 2026-10-28: "downloaded" ends the request; badDownload = the transfer
# ended badly; a user may drop a request at any point before it ends
TRACK_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"searching", "dropped"}),
    "reRequested": frozenset({"searching", "dropped"}),
    "searching": frozenset({"found", "notFound", "dropped"}),
    "found": frozenset({"midDownload", "dropped"}),
    "midDownload": frozenset({"downloaded", "notDownloaded", "badDownload", "dropped"}),
    **{s: frozenset({"reRequested"}) for s in _ENDED},
}

# tagging may fail (failTag lives in the tagging table), so verified → moved is
# allowed; "missing" = vanished on its own, "deleted" = removed on purpose
FILE_TRANSITIONS: dict[str, frozenset[str]] = {
    "downloaded": frozenset({"verified", "missing"}),
    "verified": frozenset({"tagged", "moved", "missing"}),
    "tagged": frozenset({"moved", "missing"}),
    "moved": frozenset({"indexed", "missing", "deleted"}),
    "indexed": frozenset({"missing", "deleted"}),
    "missing": frozenset({"deleted"}),
    "deleted": frozenset(),
}


class IllegalTransition(ValueError):
    def __init__(self, kind: str, old: str, new: str) -> None:
        super().__init__(f"{kind}: {old!r} → {new!r} is not allowed")


def _check(table: dict[str, frozenset[str]], kind: str, old: str, new: str) -> str:
    if new not in table.get(old, frozenset()):
        raise IllegalTransition(kind, old, new)
    return new


def track(old: str, new: str) -> str:
    return _check(TRACK_TRANSITIONS, "track_state", old, new)


def file(old: str, new: str) -> str:
    return _check(FILE_TRANSITIONS, "file_state", old, new)


# One function per transition owner (architecture.md §3). Each returns the new
# state for the store to write.


def drop(old: str) -> str:  # requests service
    return track(old, "dropped")


def re_request(old: str) -> str:  # requests service
    return track(old, "reRequested")


def start_search(old: str) -> str:  # search service
    return track(old, "searching")


def search_done(old: str, *, found: bool) -> str:  # search service
    return track(old, "found" if found else "notFound")


def start_download(old: str) -> str:  # download service
    return track(old, "midDownload")


def download_done(old: str) -> str:  # download service
    return track(old, "downloaded")


def download_failed(old: str, *, bad: bool) -> str:  # download service
    """``bad``: the transfer ended badly; otherwise no candidate got the file."""
    return track(old, "badDownload" if bad else "notDownloaded")


def file_verified(old: str) -> str:  # files service
    return file(old, "verified")


def file_tagged(old: str) -> str:  # tagging service
    return file(old, "tagged")


def file_moved(old: str) -> str:  # files service
    return file(old, "moved")


def file_indexed(old: str) -> str:  # files service
    return file(old, "indexed")


def file_missing(old: str) -> str:  # files service
    return file(old, "missing")


def file_deleted(old: str) -> str:  # files service, after an approved delete proposal
    return file(old, "deleted")


def file_replaced(old: str) -> str:
    """A new copy of the song replaces the file row: any state → ``downloaded``.

    Mirrors the ``downloads_completed_handoff`` trigger, which does this in SQL.
    """
    if old not in FILE_STATES:
        raise IllegalTransition("file_state", old, "downloaded")
    return "downloaded"
