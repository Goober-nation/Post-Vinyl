# Files and tagging

What happens to a file after it arrives. The state flow is in
[the pipeline](pipeline.md#6-file-pipeline). Code: `services/files.py`, `tagging.py`, `pipeline.py`.

## Verify

| Code | Meaning |
|---|---|
| `success` | Passed |
| `not_found` | Not on disk |
| `not_audio` | The audio library can't read it, or it has no length |
| `duration` | More than ±6 s from the song |
| `truncated` | Smaller than 0.8 × bitrate × length. Not applied to FLAC, whose bitrate comes from its size. A cut-off MP3's header can claim the full length |

## Tag

No beets, no Picard. **Only tags known to be true**, from MusicBrainz and ListenBrainz.

**Recording.** Downloads, renames and retags all use one lookup, `TaggingService._match`:

1. The song's MusicBrainz id, if known.
2. ListenBrainz's name lookup, accepted only when the artist is in the credit and the length is
   within ±6 s.
3. MusicBrainz search, up to 5 recordings of a fitting length.

The first recording with an official release wins, so one with only withdrawn releases is skipped.
Each step runs for the artist as written, then for the first named artist if that finds nothing:
`Timbaland; Elton John`, then `Timbaland`. The whole name goes first so `Tyler, The Creator` stays
one artist.

A download uses the **requester's** token, and no one reviews its result, so its search must also
name the same song. A wrong guess stays untagged, not mistagged. A rename uses the admin's token and
is reviewed before it's applied.

**Release** (`core/release_choice.py`): the earliest official plain album, then EP, then single.
Plain means no secondary type such as Compilation or Live. With none of those, the earliest official
release of any kind. First, though, if the recording is on an official release the library already
has songs of, it joins the one with the most, because Navidrome splits an album by release. A
recording that isn't on that release stays on its own. The earliest edition's own disc and track layout is kept. If those details
can't be had, the album is written without numbers, so the file still lands in the album folder.

**Tags written**: title, artist, album artist, album, date, original date, track and disc with
totals, MusicBrainz ids, ISRC when there is exactly one, and a cover.

**Cover.** It follows the user's catalog order. Deezer ahead of MusicBrainz tries Deezer's album
first, used only when artist and album both match, then the Cover Art Archive. MusicBrainz ahead
tries the archive first, then Deezer. At the archive the release group's front comes before this
release's, because uploads for one edition are community scans. A failing source only moves on: a
cover never stops a song being tagged. A source that's off is skipped.

**Kept from the peer's file**: ReplayGain, Opus loudness (`R128_*`) and lyrics. Everything else is
replaced. A file with no new cover keeps its old one.

**Formats**: FLAC, MP3, M4A/MP4, Ogg Vorbis and Opus. No cover is written for Ogg or Opus.

A failure writes a `failTag` row and never blocks the file. It's tried once more after
`retag_after_seconds`, then the file moves untagged. Failure lives only in the `tagging` table.

## Place

| Case | Path inside the library |
|---|---|
| Tagged | `<Artist>/<Album> (<Year>)/[Disc N/]<NN> <Title>.<ext>` |
| Untagged | `<Artist>/<Title>.<ext>`, from the song's normalized names |

- A peer's own tags are never trusted for the path.
- The disc folder appears only for multi-disc releases.
- **Never overwrite**: a taken name becomes ` (2)`, ` (3)`. A file already at one of its target's
  numbered names stays put.
- Deleting only happens inside the library folder.
- A replacement finds the old copy through `navidrome_id` and deletes it before moving the new one.

One lock covers moves, renames, deletes and the missing check, so the check can't mistake a file
mid-rename for a missing one.

## Index

After a batch of moves there is one Navidrome scan, waited for up to 120 s. Each file is then found
by path and linked by `navidrome_id`. Paths aren't stored, since Navidrome owns them. Navidrome
keeps a song's id when the same path is replaced, and the id changes when a file moves.

Navidrome is reached as the first admin with a working saved login.

## Rename

Admin only, in two steps.

1. **Suggest**: looks up the new names. ListenBrainz first with the admin's token, using the same
   checks as tagging, then a MusicBrainz search filtered by length. It answers the full tag set and
   the exact cover with its source, or `{match: false}`.
2. **Apply** writes the set and moves the file where the tags put it. Or **strip** removes every tag
   and moves it to `<Artist>/<Title>`. Then Navidrome rescans.

A rename needs MusicBrainz on for the admin. The server rebuilds tags from the recording and release
ids, and refuses a release that isn't the recording's.

A plain move kept the Navidrome id on 0.64.2, but a tag change gives the song a new one. So each
user's star and rating are read first and written to the new id, for every user with a saved login.
Play counts aren't carried. The old entry stays as a Missing row until it's removed in Navidrome.

Files the pipeline still holds aren't in Navidrome yet, so renaming them answers `404`.

**Retag** tries the same lookup from the names in the path, then from the current tags. Nothing
matched answers `422`, and the file is unchanged.

## Delete

Admin only. The file goes from disk, inside the library only, and empty folders are pruned. The
record becomes `deleted` and stays as history, and Navidrome rescans. A later request downloads the
song again.

## Missing

The hourly check marks files that vanished as `missing`. Files deleted through the app are `deleted`.
Compose sets `ND_SCANNER_PURGEMISSING=never`, so Navidrome keeps a Missing row for them until it's
removed on its Missing Files page. Post-Vinyl's Delete can't remove one: there is no file to find.
