"""
Query builder — search query construction pipeline (P6.5-6).

Turns a track title + artist into the short slskd queries that actually
return results. Empirical finding (P-MB-4, 2026-08-09): 2-word queries
return real, repeatable results on slskd; every 3+-word combined
artist+title query returned exactly zero. So:

1. Feat-clause truncation on BOTH fields: cut at the first
   feat/ft/featuring delimiter and discard everything after it.
   ("Alesso feat. Katy Perry" → "Alesso" — the featured artist must not
   win the longest-word pick.)
2. Parentheticals: contents are excluded from word selection unless they
   contain a known qualifier word (remix, cover, ...), which is then
   carried as an explicit extra query word — a "(Remix)" title searches
   the remix, not the original.
3. Word selection: tokenize → drop stop_words ∪ remix_qualifiers → pick
   longest remaining words.
4. Query shape: never more than 2 words (1 from track + 1 from artist),
   except for the paren-qualifier attempt below.
5. Re-query ladder: [3-word qualifier attempt (when a paren qualifier
   exists — user decision 2026-08-10: try it, and if it misses, drop
   it)], then 1-1 → 2-1 → 1-2 → 2-2 (n-th longest track word, n-th
   longest artist word, track varying first). The caller stops early once
   a rung clears the pass-ratio threshold and falls back to the best rung
   by ratio otherwise.
6. Empty fallback: if only one field yields usable words, the ladder
   degrades to that field's single words rather than the raw title — the
   raw title is only used when nothing meaningful survives on either
   side, since it's the one query shape that isn't word-capped. (The
   *filter* side of the rule — "do not filter" — lives in the caller: an
   empty artist word list matches everything.)

Shared by the recs path (RecPuller) and the MusicBrainz path (P-MB-4).
"""

import re
import unicodedata

# Words that never carry searchable meaning in a title/artist. Kept as the
# single source of truth for query construction AND the client-side artist
# post-filter.
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "to",
        "at",
        "by",
        "is",
        "or",
        "for",
        "with",
        "vs",
        "&",
        "and",
        "feat",
        "ft",
        "featuring",
        "presents",
        "intro",
        "produced",
        "prod",
        "mixed",
        "w",
        "remix",
        "rmx",
        "edit",
        "edition",
        "version",
        "original",
        "extended",
        "mix",
        "radio",
        "instrumental",
        "inst",
        "acoustic",
        "acoust",
        "live",
        "demo",
        "explicit",
        "clean",
        "bonus",
        "alternate",
        "alt",
        "cover",
        "single",
        "album",
        "ep",
        "lp",
        "deluxe",
        "special",
        "limited",
        "compilation",
        "comp",
        "collection",
        "anthology",
        "greatest",
        "hits",
        "best",
        "release",
        "reissue",
        "remastered",
        "remaster",
        "anniversary",
        "complete",
        "ultimate",
        "essential",
        "definitive",
        "part",
        "pt",
        "vol",
        "volume",
        "disc",
        "disk",
        "cd",
        "vinyl",
        "digital",
        "side",
        "chapter",
        "act",
        "scene",
        "ost",
        "soundtrack",
        "score",
        "theme",
        "opening",
        "ending",
        "background",
        "bgm",
        "insert",
        "character",
        "image",
        "track",
        "unknown",
        "various",
        "artist",
        "music",
        "song",
        "audio",
        "file",
        "type",
        "beat",
        "beats",
        "bpm",
        "kbps",
        "hz",
        "khz",
        "mp3",
        "flac",
        "m4a",
        "wav",
        "ogg",
        "aac",
        "wma",
        "lossless",
        "hires",
        "hd",
        "master",
        "stereo",
        "mono",
    }
)

# Version/cover qualifiers — dropped from word selection AND matched
# (substring) against filenames so cover-like versions are rejected as
# candidates. The multi-word entries (sped up, 8d audio, ...) only match
# filenames — a multi-word phrase never survives tokenization — which is
# the documented behavior.
REMIX_QUALIFIERS = frozenset(
    {
        "remix",
        "rmx",
        "edit",
        "extended",
        "radio",
        "acoustic",
        "live",
        "demo",
        "instrumental",
        "inst",
        "cover",
        "remaster",
        "remastered",
        # P6.5-6 additions (all approved 2026-08-10)
        "acapella",
        "loop",
        "sped up",
        "slowed",
        "nightcore",
        "8d audio",
        "karaoke",
        "backing track",
        "mashup",
        "bootleg",
        "vip mix",
        "flip",
        "tribute",
        "made famous by",
        "in the style of",
    }
)

# Feat-clause delimiters — strictly feat/ft/featuring. with/x/vs are
# deliberately excluded: they appear in legitimate titles (mashups) and
# truncating on them would cut real titles in half.
_FEAT_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\b", re.IGNORECASE)

_PAREN_RE = re.compile(r"\(([^)]*)\)")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9']+")

# Deterministic qualifier order. REMIX_QUALIFIERS is a frozenset, so
# iterating it directly makes `qualifiers[0]` — and therefore the 3-word
# rung — vary between process restarts under hash randomization. Sorted
# longest-first so the most specific match wins ("vip mix" over "mix").
_ORDERED_QUALIFIERS = tuple(sorted(REMIX_QUALIFIERS, key=lambda q: (-len(q), q)))

# Single-word qualifiers must match on word boundaries: plain substring
# matching turns "(Credits)" into `edit` and "(Alive)" into `live`, each
# of which costs a wasted slskd search on a bogus 3-word rung. Multi-word
# entries ("sped up", "8d audio") keep substring semantics.
_QUALIFIER_RES = tuple(
    (q, re.compile(rf"\b{re.escape(q)}\b") if " " not in q else None)
    for q in _ORDERED_QUALIFIERS
)


def strip_feat(text: str) -> str:
    """Cut a title/artist at the first feat/ft/featuring delimiter and
    discard everything after it."""
    return _FEAT_RE.split(text, maxsplit=1)[0]


def fold_for_matching(text: str) -> str:
    """Fold accents to ASCII and drop periods, before `_TOKEN_SPLIT_RE` runs.

    Two real live-search failures turned out to be the same bug wearing
    different clothes, both live-confirmed 2026-08-12:

    - **Accented letters** ("Björk", "Jóga", "Sigur Rós") sit outside the
      tokenizer's `[a-z0-9']` class, so they act as *separators* rather than
      letters. `select_words("Björk")` returned `[]` — "björk" split into
      "bj"/"rk", both too short to survive the length filter, and the whole
      word vanished. `select_words("Jóga")` did the same: "j"/"ga", both
      dropped. The archived P6.5-6 close-out already named this
      ("`Björk` drops entirely") but it was logged as "not a defect" and
      never turned into a fix.
    - **A dotted acronym** ("P.O.V.", "R.E.M.") splits at every period into
      single-letter tokens, destroyed the same way:
      `build_search_queries("P.O.V.", "Clipse")` returned `["clipse"]` — the
      *entire title* contributed zero words, so the empty-fallback rule
      substituted the artist alone. The search silently became "find
      anything by Clipse" instead of "find P.O.V. by Clipse", with nothing
      to indicate the title was ever dropped.

    NFKD-decomposing and stripping combining marks folds `ö`/`ó`/`á`/etc. to
    their plain-ASCII base letter — which also happens to be the more
    search-friendly form, since slskd peers' filenames are overwhelmingly
    ASCII. Removing periods outright merges a dotted run into one token
    (`"p.o.v."` → `"pov"`) without touching ordinary sentence punctuation,
    which is followed by a space and so splits on the space exactly as
    before (`"Mr. Fantastic"` → `"mr fantastic"`, unchanged).

    Deliberately narrow: this folds *decomposable* Latin diacritics, not
    every non-Latin script. A title in Cyrillic or CJK still tokenizes to
    nothing today — same failure mode, out of scope for this fix, and worth
    its own investigation with its own real cases rather than a guess.

    **Public and shared on purpose.** `select_words` uses it to build the
    query; `recommendation.py._artist_words` and
    `search.py._extract_artist_words` use it too, on both sides of their
    filename filters. Folding only the query side would have moved the same
    bug one step downstream instead of fixing it: those two post-filters
    check whether a peer's filename contains the artist's words, and if the
    artist word keeps its accent ("björk") while the peer's filename doesn't
    (the common case — most Soulseek filenames are plain ASCII), the
    substring check fails and a genuinely correct result gets silently
    discarded. Folding the filename too, not just the words being searched
    for, is what makes the comparison happen in one consistent space
    regardless of which way a given peer happened to spell it.
    """
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return folded.replace(".", "")


def paren_qualifiers(text: str) -> list[str]:
    """Qualifier words found inside parentheses, e.g. 'remix' in
    'Heroes (We Could Be) (Remix)'. Single-word qualifiers match on word
    boundaries; multi-word entries like '8d audio' match as substrings.
    Paren contents WITHOUT a qualifier are pure cruft ('(Official Video)')
    and never appear here.

    Order is deterministic (longest qualifier first), so the 3-word rung
    built from `qualifiers[0]` is reproducible across restarts."""
    found = []
    for group in _PAREN_RE.findall(text or ""):
        low = group.lower()
        for qualifier, word_re in _QUALIFIER_RES:
            hit = word_re.search(low) if word_re is not None else qualifier in low
            if hit and qualifier not in found:
                found.append(qualifier)
    return found


def select_words(text: str) -> list[str]:
    """Meaningful query words for a field, longest first.

    - feat clause truncated
    - paren contents excluded entirely (their qualifier words are carried
      separately via paren_qualifiers)
    - accents folded and periods dropped before tokenizing, so "Björk" and
      "P.O.V." survive as words instead of shattering into fragments too
      short to keep — see `fold_for_matching`
    - stop words, remix qualifiers, digit-only and short (<=2 char) tokens
      dropped — 2-char tokens are noise and never win a longest-word pick
    """
    stripped = strip_feat(text or "")
    without_parens = _PAREN_RE.sub(" ", stripped)
    normalized = fold_for_matching(without_parens)
    tokens = [t for t in _TOKEN_SPLIT_RE.split(normalized.lower()) if t]

    words = [
        t
        for t in tokens
        if t not in STOP_WORDS
        and t not in REMIX_QUALIFIERS
        and not t.isdigit()
        and len(t) > 2
    ]
    # Dedupe preserving first-seen order, then sort longest-first (stable).
    seen: set[str] = set()
    unique = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return sorted(unique, key=len, reverse=True)


def build_search_queries(track: str, artist: str) -> list[str]:
    """Ordered candidate queries for a track+artist (the re-query ladder).

    Rung 0 — only when the title has a paren qualifier: the 3-word attempt
    ``<track word> <qualifier> <artist word>``. User decision 2026-08-10:
    make the 3-word query; if it misses (pass ratio below threshold), drop
    the qualifier and continue down the 2-word ladder.

    Rungs 1-4: 1-1 → 2-1 → 1-2 → 2-2 (n-th longest track word – n-th
    longest artist word, track varying first). Rungs missing a word are
    skipped.

    If one field yields no usable word, every 2-word rung is skipped; the
    ladder then becomes the other field's top two single words. Only when
    *neither* field yields anything does it fall back to the raw title
    (empty-fallback rule) — that path emits an uncapped multi-word query,
    so it stays the last resort rather than the first.
    """
    track_text = strip_feat(track or "").strip()
    artist_text = strip_feat(artist or "").strip()
    track_words = select_words(track_text)
    artist_words = select_words(artist_text)
    qualifiers = paren_qualifiers(track)

    queries: list[str] = []

    def _join(words: list[str]) -> str:
        # Dedupe within a query too: "Album Leaf" by "The Album Leaf" would
        # otherwise produce the degenerate query "leaf leaf".
        out: list[str] = []
        for w in words:
            if w and w not in out:
                out.append(w)
        return " ".join(out)

    if qualifiers and track_words:
        queries.append(
            _join([track_words[0], qualifiers[0], *artist_words[:1]]),
        )

    for ti, ai in ((0, 0), (1, 0), (0, 1), (1, 1)):
        if ti >= len(track_words) or ai >= len(artist_words):
            continue
        queries.append(_join([track_words[ti], artist_words[ai]]))

    if not queries:
        # No usable artist word (empty artist, a <=2-char name like "U2", a
        # non-ASCII one like "Björk", or a stop-word-only one like "Master P")
        # skips every 2-word rung. Fall back to single-word track rungs before
        # the raw title — the raw title is typically 3+ words, which P-MB-4
        # proved returns zero. Same on the artist side if the title is the
        # field that came up empty.
        queries.extend(track_words[:2] or artist_words[:2])

    if not queries:
        queries.append(track_text or artist_text or "")

    # Dedupe (e.g. single-word fields produce repeated rungs), keep order.
    seen: set[str] = set()
    unique = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique
