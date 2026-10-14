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
`bin/va_lookup.py` (Python 3 stdlib only, no deps). Three data sources, no API keys:

- **AniList GraphQL** (`https://graphql.anilist.co`) — the primary VA data source:
  character search (paginated) with per-media `voiceActors(language: JAPANESE)`
  edges, and Staff queries for notable roles (matched by AniList id, `idMal`
  for anime links).
- **MyAnimeList HTML** (`myanimelist.net`) — automatic fallback when AniList is
  unreachable (it 403s this network at times). Character search →
  character page (Animeography + Voice Actors table) → people page
  (Voice Acting Roles table). Keyless, fully parseable, and MAL is the link
  target anyway. The JSON output carries `"source": "mal-scrape"` when the
  fallback was used; roles come in MAL's (recency) order rather than by
  favourites.
- **MyAnimeList people search** (`myanimelist.net/people.php`) — resolves each
  VA to a stable `myanimelist.net/people/{id}` link (MAL 303-redirects on exact
  matches; otherwise the first search result is parsed).

- Character mode: `bin/va_lookup.py character "Gyari" [--anime "Iruma-kun"] [--lang Japanese] [--roles 8]`
  - Searches AniList characters (paginated, up to 3 pages), picks the exact name
    match (or uses `--anime` to disambiguate), reads the Japanese voiceActors
    edges, attaches each VA's notable roles + MAL people link.
  - If `--anime` is given but the character search finds no match in that
    anime, it falls back to an anime-first scan of the full cast before
    giving up — this catches characters missing from AniList's character
    search index. It never falls back to a different anime's character.
- Anime mode: `bin/va_lookup.py anime "Kusunoki's Garden of Gods" [--character "Tsumugi"] [--role MAIN|SUPPORTING|BACKGROUND] [--limit 20]`
  - Resolves the anime, then lists its cast (role-sorted) with Japanese VAs,
    MAL people links, and notable roles. Use when the user asks about a
    show's cast, or when character mode can't find someone.
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
2. The answer must always include the VA's **MyAnimeList people link**
   (user preference). Research may draw on any source — AniList, Wikipedia,
   fandom wikis, whatever resolves the answer — but MAL is the required link
   target. The script's `mal_url` field is the canonical way to get it.
3. If the character search returns multiple candidates and no `--anime` hint was
   given, pass the anime title from the user's question as `--anime`.
3. MAL links in the JSON are the answer's source links — verify each URL points
   at the claimed page before shipping it to the user.
4. Notable roles should cover anime beyond the user's known titles too, but a
   role from one of the user's known anime must always be included and called
   out. The script reads `config.json` (local-only, never committed/pushed;
   list order = priority, top first) and marks matching roles with
   `"known_anime": true`, sorted by priority first. If the file is missing,
   check memory for the list.
5. If both AniList and MAL fail, fall back to `browser.search` for the
   answer and say so — do not guess the VA.
