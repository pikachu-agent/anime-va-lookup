# API research notes (2026-10-08)

Decision record for why this skill uses AniList GraphQL + MAL people.php
instead of the alternatives.

## Official MAL API v2 — rejected
- Base `https://api.myanimelist.net/v2`. Read-only works with just the
  `X-MAL-CLIENT-ID` header (client ID obtainable instantly at
  myanimelist.net/apiconfig; no OAuth needed for public data).
- **Has NO character/people/seiyuu endpoints** — anime/manga search, details,
  ranking, seasonal, forum, user lists only. Useless for VA lookups.
- Rate limits officially undocumented; community practice ≤ ~1 req/s; abuse
  surfaces as 403 "DoS detected".

## Jikan v4 — ideal but unreachable from here
- `https://api.jikan.moe/v4`: MAL-backed REST, no key. Would give the perfect
  pipeline: `/characters?q=` → `/characters/{id}/voices` (filter
  `language == "Japanese"`) → `/people/{id}/voices` for roles, with native
  `mal_id` + canonical MAL URLs on every object. Limits 3/s, 60/min.
- **api.jikan.moe consistently times out from this sandbox's egress**
  (docs.api.jikan.moe works fine), so the skill cannot depend on it.
  If Jikan ever becomes reachable, prefer it: one REST chain replaces the
  two-source approach below.

## AniList GraphQL — chosen as VA source
- `https://graphql.anilist.co` (POST), no key for public reads. Works from here.
- Character search: `Page { characters(search:) { ... media { edges {
  voiceActors(language: JAPANESE) { id name { full native } } node {
  id idMal title { romaji english native } } } } } }`.
- Staff roles: `Staff(id:) { characters(sort: FAVOURITES_DESC) { nodes {
  name { full } media { edges { voiceActors(language: JAPANESE) { id }
  node { id idMal title { romaji english } } } } } } }` — match the VA's
  AniList id on each edge to attribute the role, use `idMal` for MAL anime links.
- Limits: normally 90 req/min, degraded-state banner currently says 30/min.
  Send a real User-Agent; honor 429 `Retry-After`. Script sleeps 1s per query.
- **Limitation: Staff/Character types have no `idMal`** — AniList cannot
  produce MAL people/character links, only MAL anime links.

## MAL people.php — chosen as MAL-link resolver
- `GET https://myanimelist.net/people.php?q=<name>&cat=person` with a browser
  User-Agent: on an exact name match MAL 303-redirects straight to
  `/people/{id}/{slug}`; urllib follows it and `resp.geturl()` is the link.
  Otherwise the first `href="https://myanimelist.net/people/…"` in the
  search-results HTML is used.
- Reachable with plain curl from this network (fast, no Cloudflare block).
