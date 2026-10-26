# Logo font

The header logo (`.app-name`) uses **Forta** by Andrzej Wróż (`logo.ttf`), bundled here under the
SIL Open Font License 1.1 (see `logo-LICENSE.txt`) — free for commercial and non-commercial use, no
restrictions. It's an all-caps display font; `.app-name` in `styles.css` applies
`text-transform: uppercase` to match.

To swap in a different font, replace `logo.ttf` (or point `styles.css`'s `@font-face` `src`/`format`
at a different filename/format) and drop the matching license file alongside it. If a font's license
doesn't permit redistribution (e.g. DaFont's many "free for personal use only" listings), don't commit
the file — this directory is gitignored except for `logo.ttf`/`logo-LICENSE.txt`/this README, so a
personally-licensed replacement stays local to your checkout instead of being redistributed.

If `logo.ttf` is ever missing, the logo just falls back to the app's normal sans-serif stack —
nothing breaks.
