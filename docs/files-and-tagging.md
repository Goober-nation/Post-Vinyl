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

**Recording.** The song's MusicBrainz id, or ListenBrainz's name lookup with the **requester's**
token. A lookup is accepted only when our main artist is in the credit and the length is within
±6 s, so a wrong guess stays untagged, not mistagged.

**Release** (`core/release_choice.py`): the earliest official plain album, then EP, then single.
Plain means no secondary type such as Compilation or Live. With none of those, the earliest official
release of any kind. The earliest edition's own disc and track layout is kept. If those details
can't be had, the album is written without numbers, so the file still lands in the album folder.

**Tags written**: title, artist, album artist, album, date, original date, track and disc with
totals, MusicBrainz ids, ISRC when there is exactly one, and the Cover Art Archive front cover.

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
   checks as tagging, then a MusicBrainz search filtered by length. It answers the full tag set, or
   `{match: false}`.
2. **Apply** writes the set and moves the file where the tags put it. Or **strip** removes every tag
   and moves it to `<Artist>/<Title>`. Then Navidrome rescans.

A rename needs MusicBrainz on for the admin. The server rebuilds tags from the recording and release
ids, and refuses a release that isn't the recording's. The song's Navidrome id changes, because it
follows the path.

Files the pipeline still holds aren't in Navidrome yet, so renaming them answers `404`.

**Retag** tries the same lookup from the names in the path, then from the current tags. Nothing
matched answers `422`, and the file is unchanged.

## Delete

Admin only. The file goes from disk, inside the library only, and empty folders are pruned. The
record becomes `deleted` and stays as history, and Navidrome rescans. A later request downloads the
song again.

## Missing

The hourly check marks files that vanished as `missing`. Files deleted through the app are `deleted`.
Compose sets `ND_SCANNER_PURGEMISSING=always`, so Navidrome drops them on its next scan.
