---
name: "anime-va-lookup"
description: "Look up anime character voice actors (Japanese seiyuu by default) with MyAnimeList links and notable roles. Use when the user asks who voices a character, what else a seiyuu voices, or for a MAL people page link."
metadata: { "includeInPrompt": true }
---

# Anime VA Lookup

## Purpose
Deterministic character → voice actor → notable-roles lookups backed by MAL data,
so answers come from a real API instead of model memory.

## Tooling
`bin/va_lookup.py` (Python 3 stdlib only, no deps). Two data sources, no API keys:

- **AniList GraphQL** (`https://graphql.anilist.co`) — the VA data source:
  character search with per-media `voiceActors(language: JAPANESE)` edges, and
  Staff queries for notable roles (matched by AniList id, `idMal` for anime links).
- **MyAnimeList people search** (`myanimelist.net/people.php`) — resolves each
  VA to a stable `myanimelist.net/people/{id}` link (MAL 303-redirects on exact
  matches; otherwise the first search result is parsed).

- Character mode: `bin/va_lookup.py character "Gyari" [--anime "Iruma-kun"] [--lang Japanese] [--roles 8]`
  - Searches AniList characters, picks the exact name match (or uses `--anime`
    to disambiguate), reads the Japanese voiceActors edges, attaches each VA's
    notable roles + MAL people link.
- Person mode: `bin/va_lookup.py person "Misato Matsuoka" [--roles 12]`
  - Searches AniList staff, returns top roles by favourites with MAL anime links.

Output is JSON. Always run it fresh — never answer VA questions from memory
when the tool is available.

## Auth
None. Both sources are keyless read-only. The script sleeps 1s between AniList
queries (30–90 req/min limit) and backs off on 429 via `Retry-After`.
See `references/api_notes.md` for why not the official MAL API (no people
endpoints) or Jikan (unreachable from this network).

## Operating Rules
1. Default language is Japanese ("Always sub" user preference); only look up dub
   actors when the user explicitly asks for a dub.
2. If the character search returns multiple candidates and no `--anime` hint was
   given, pass the anime title from the user's question as `--anime`.
3. MAL links in the JSON are the answer's source links — verify each URL points
   at the claimed page before shipping it to the user.
4. If AniList 429s or MAL resolution fails, fall back to `browser.search` for the
   answer and say so — do not guess the VA.
