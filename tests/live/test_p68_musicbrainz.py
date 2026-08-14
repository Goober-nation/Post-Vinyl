"""
P6.8 — MusicBrainz search & discovery, against the live stack.

Two halves with deliberately different assertion discipline:

Part A (`TestMusicBrainzSearch`) is deterministic and needs nothing but a
reachable MusicBrainz API (public, no auth). It asserts on mechanism — status
codes, non-empty results, the presence of the field names the frontend is
built against — never on *which* recordings/albums came back.

Part B (`TestMusicBrainzDownload`) queues a real download from a real
Soulseek peer via the resolve job. The resolve -> search-ladder -> queue
pipeline is the mechanism under test, so what gets asserted is: 202 + job_id,
`mb.resolve_started` with count >= 1, `mb.resolve_completed`, and that the job
emitted *either* `mb.track_queued` or `mb.track_failed` (a "no viable
candidate" is a legitimate outcome — Soulseek availability is a gamble). When
a track is queued, the `downloads` row must carry `is_library_download = 1`
and `mb_recording_id = <mbid>`. Whether the bytes actually moved and landed in
the beets "library" tree is *reported*, never hard-asserted.
"""

from __future__ import annotations

import pytest

from tests.live.harness import wait_until
from tests.live.probes import is_audio, tree_path

#: A track chosen for near-certain Soulseek availability, not for elegance of
#: the MusicBrainz ranking: the resolve job searches by title+artist, which is
#: the same regardless of which recording (studio/live/compilation) the search
#: surfaces first.
DOWNLOAD_TITLE = "Bohemian Rhapsody"
DOWNLOAD_ARTIST = "Queen"


class TestMusicBrainzSearch:
    """Search endpoints — the canonical public data paths."""

    def test_search_recordings_returns_canonical(self, stack):
        call = stack.client.get(
            "/api/musicbrainz/search/recordings",
            params={"title": "Joga", "artist": "Bjork", "limit": 20},
        )
        assert call.status == 200, (
            f"recordings search failed: {call.status} {call.body}"
        )
        results = call.body.get("results", [])
        assert results, "no recordings returned for Joga / Bjork"

        for r in results:
            assert r.get("mbid"), f"recording missing mbid: {r}"
            assert r.get("title"), f"recording missing title: {r}"
            assert r.get("artist"), f"recording missing artist: {r}"
            assert "album" in r, f"recording missing album field: {r}"
            assert "artist_credit" in r, f"recording missing artist_credit: {r}"

        stack.marker(
            "mb_recordings",
            count=len(results),
            first={
                "mbid": results[0]["mbid"],
                "title": results[0]["title"],
                "artist": results[0]["artist"],
            },
        )

    def test_search_albums_and_artists(self, stack):
        albums = stack.client.get(
            "/api/musicbrainz/search/albums",
            params={"title": "Homogenic", "limit": 20},
        )
        assert albums.status == 200, (
            f"album search failed: {albums.status} {albums.body}"
        )
        album_results = albums.body.get("results", [])
        assert album_results, "no albums returned for Homogenic"

        artists = stack.client.get(
            "/api/musicbrainz/search/artists",
            params={"name": "Nirvana", "limit": 20},
        )
        assert artists.status == 200, (
            f"artist search failed: {artists.status} {artists.body}"
        )
        artist_results = artists.body.get("results", [])
        assert artist_results, "no artists returned for Nirvana"
        for a in artist_results:
            assert a.get("name"), f"artist missing name: {a}"
            assert "disambiguation" in a, f"artist missing disambiguation: {a}"

        stack.marker(
            "mb_albums_artists",
            albums=len(album_results),
            artists=len(artist_results),
            first_artist=artist_results[0].get("name"),
            first_artist_disambiguation=artist_results[0].get("disambiguation"),
        )

    def test_discography_and_tracks(self, stack):
        artists = stack.client.get(
            "/api/musicbrainz/search/artists",
            params={"name": "Nirvana", "limit": 5},
        )
        assert artists.status == 200 and artists.body.get("results")
        artist_mbid = artists.body["results"][0]["mbid"]

        albums = stack.client.get(f"/api/musicbrainz/artists/{artist_mbid}/albums")
        assert albums.status == 200, (
            f"discography failed: {albums.status} {albums.body}"
        )
        album_results = albums.body.get("results", [])
        assert album_results, f"no release groups for artist {artist_mbid}"
        rg_mbid = album_results[0]["mbid"]

        tracks = stack.client.get(f"/api/musicbrainz/albums/{rg_mbid}/tracks")
        assert tracks.status == 200, f"track list failed: {tracks.status} {tracks.body}"
        track_results = tracks.body.get("results", [])
        assert track_results, f"no tracks for release group {rg_mbid}"
        for t in track_results:
            assert t.get("mbid"), f"track missing mbid: {t}"
            assert t.get("title"), f"track missing title: {t}"
            assert t.get("artist"), f"track missing artist: {t}"

        stack.marker(
            "mb_discography",
            artist_mbid=artist_mbid,
            album=album_results[0].get("title"),
            rg_mbid=rg_mbid,
            track_count=len(track_results),
        )


class TestMusicBrainzDownload:
    """Resolve-and-queue for a single recording — mechanism assertions, outcome
    reported."""

    def test_recording_download_resolves_and_queues(self, stack, budget):
        search = stack.client.get(
            "/api/musicbrainz/search/recordings",
            params={"title": DOWNLOAD_TITLE, "artist": DOWNLOAD_ARTIST, "limit": 5},
        )
        assert search.status == 200, f"search failed: {search.status} {search.body}"
        results = search.body.get("results", [])
        assert results, (
            f"no recordings returned for {DOWNLOAD_TITLE} / {DOWNLOAD_ARTIST}"
        )
        mbid = results[0]["mbid"]
        stack.marker(
            "mb_recording_chosen",
            mbid=mbid,
            title=results[0]["title"],
            artist=results[0]["artist"],
        )

        if not budget.take(1):
            pytest.skip(
                f"download budget exhausted ({budget.spent()}/{budget.total}) — "
                "this single-track test never exceeds the real-download ceiling"
            )

        call = stack.client.post(f"/api/musicbrainz/recordings/{mbid}/download")
        assert call.status == 202, f"download start failed: {call.status} {call.body}"
        job_id = call.body.get("job_id")
        assert job_id, f"no job_id in download response: {call.body}"
        stack.marker("mb_job_started", job_id=job_id, mbid=mbid)

        completed = stack.events.wait_for(
            lambda e: (
                e.type == "mb.resolve_completed" and e.data.get("job_id") == job_id
            ),
            timeout=180.0,
            description="mb.resolve_completed",
        )

        started = [
            e
            for e in stack.events.of_type("mb.resolve_started")
            if e.data.get("job_id") == job_id
        ]
        assert started, "mb.resolve_started never fired for this job"
        assert started[0].data.get("count", 0) >= 1, (
            f"mb.resolve_started reported count={started[0].data.get('count')}, expected >= 1"
        )

        queued = [
            e
            for e in stack.events.of_type("mb.track_queued")
            if e.data.get("job_id") == job_id
        ]
        failed = [
            e
            for e in stack.events.of_type("mb.track_failed")
            if e.data.get("job_id") == job_id
        ]
        assert queued or failed, (
            "the resolve job completed but neither mb.track_queued nor "
            "mb.track_failed fired — the pipeline did not run"
        )

        stack.marker(
            "mb_resolve_result",
            job_id=job_id,
            queued=completed.data.get("queued"),
            failed=completed.data.get("failed"),
            queued_events=len(queued),
            failed_events=len(failed),
        )

        if queued:
            row = self._library_row(stack, mbid)
            assert row is not None, (
                "mb.track_queued fired but no downloads row has "
                f"is_library_download=1 and mb_recording_id={mbid}"
            )
            assert row.get("is_library_download") == 1, f"bad flag on row: {row}"
            assert row.get("mb_recording_id") == mbid, f"bad mb_recording_id: {row}"
            self._report_outcome(stack, mbid, row)
        else:
            errors = [e.data.get("error") for e in failed]
            print(f"\n[live] mb resolve failed for {DOWNLOAD_TITLE}: {errors}")
            print(
                "[live] (no viable candidate is a legitimate outcome — "
                "Soulseek availability, not a code defect)"
            )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _library_row(stack, mbid: str) -> dict | None:
        for row in stack.db.downloads():
            if row.get("mb_recording_id") == mbid and row.get("is_library_download"):
                return row
        return None

    def _report_outcome(self, stack, mbid: str, row: dict) -> None:
        """Report (never assert) whether the bytes moved and landed in the
        beets "library" tree."""

        def _latest() -> dict:
            for r in stack.db.downloads():
                if r.get("mb_recording_id") == mbid and r.get("is_library_download"):
                    return r
            return row

        try:
            wait_until(
                lambda: _latest().get("state") in ("completed", "failed", "cancelled"),
                timeout=180.0,
                interval=3.0,
                description="mb download terminal state",
            )
        except TimeoutError:
            pass

        latest = _latest()
        target = (latest.get("target_dir") or "").strip()
        moved = bool(latest.get("file_moved"))
        lib = tree_path("library")
        files = (
            sorted(p for p in lib.rglob("*") if p.is_file() and is_audio(p))
            if lib.is_dir()
            else []
        )
        stack.marker(
            "mb_download_outcome",
            state=latest.get("state"),
            file_moved=moved,
            target_dir=target,
            filename=latest.get("filename"),
            library_audio_files=len(files),
        )
        print(
            f"\n[live] mb download: state={latest.get('state')}, "
            f"file_moved={moved}, target_dir={target!r}"
        )
        print(f"[live] library tree ({lib}) has {len(files)} audio file(s)")
        for f in files[-5:]:
            print(f"[live]   {f}")
