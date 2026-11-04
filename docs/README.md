# Post-Vinyl documentation

Post-Vinyl is a self-hosted music downloader and library manager. You ask for a song, and it finds
the right file on Soulseek through slskd, tags it, files it and adds it to your Navidrome library.

These pages describe the app as it works today. Where they disagree with the code, the code is
right and the page needs fixing. The [project README](../README.md) is the short introduction.

## Use it

| Page | For |
|---|---|
| [User guide](user-guide.md) | What each tab does and how to use it |
| [Troubleshooting](troubleshooting.md) | Why something didn't download, and known limits |

## Run it

| Page | For |
|---|---|
| [Self-hosting](self-hosting.md) | Install, folders, reverse proxy, backups, upgrades |
| [Configuration](configuration.md) | Every environment value and setting |

## Understand it

| Page | For |
|---|---|
| [Architecture](architecture.md) | Layers, modules, who owns what, startup and workers |
| [Download pipeline](pipeline.md) | One song from request to library, with every failure branch |
| [Search and matching](search-and-matching.md) | Queries, the file matcher, peer and file choice |
| [Files and tagging](files-and-tagging.md) | Verifying, tagging, placing, renaming, deleting |
| [ListenBrainz pulls](listenbrainz-pulls.md) | Recommendations, radio and their playlists |
| [Accounts and security](accounts-and-security.md) | Login, sessions, admins, secrets, request guards |
| [Data model](data-model.md) | Tables, states and what lives elsewhere |
| [Outside services](outside-services.md) | What was learned about slskd, Navidrome and the catalogs |

## Build on it

| Page | For |
|---|---|
| [Contributing](contributing.md) | Setup, rules, tests, adding a source, migrations |
| [Frontend](frontend.md) | Stack, structure, design rules |
| [API reference](api.md) | Every endpoint (a test keeps it complete) |
| [Roadmap](roadmap.md) | Designed but not built |

## Research

The measurements behind the search design:

- [Name normalization](research/normalization.md)
- [How peers name files](research/slskd-matching.md)
- [Search test v2](research/search-strategy.md)
- [Earlier benchmark](research/first-benchmark.md)
