# anime-va-lookup

Deterministic anime voice-actor lookups from the command line. Given a
character name, it returns the Japanese voice actor(s), their MyAnimeList
people link, and their notable roles — all from live APIs, never from model
memory.

## Usage

```bash
# Who voices this character? (Japanese by default)
python3 bin/va_lookup.py character "Gyari" --anime "Iruma-kun"

# Look up a voice actor directly
python3 bin/va_lookup.py person "Misato Matsuoka" --roles 12
```

Output is JSON with stable `myanimelist.net` links:

```json
{
  "character": "Gyari",
  "character_kanji": "ギャリー",
  "voice_actors": [
    {
      "name": "Misato Matsuoka",
      "name_kanji": "松岡美里",
      "mal_url": "https://myanimelist.net/people/50192/Misato_Matsuoka",
      "notable_roles": [
        { "character": "Tsubame Mizusaki",
          "anime": "Keep Your Hands Off Eizouken!",
          "anime_mal_url": "https://myanimelist.net/anime/39792" }
      ]
    }
  ]
}
```

## How it works

Two keyless, read-only data sources (Python 3 stdlib only, no dependencies):

- **AniList GraphQL** (`graphql.anilist.co`) — character search with per-media
  `voiceActors(language: JAPANESE)` edges, plus Staff queries for notable roles
  (roles are matched by AniList id; `idMal` gives MAL anime links).
- **MyAnimeList people search** (`myanimelist.net/people.php`) — resolves each
  VA name to a stable `myanimelist.net/people/{id}` link. MAL 303-redirects on
  an exact name match; otherwise the first search result is parsed.

See `references/api_notes.md` for the research behind this design: the official
MAL API v2 has no character/people endpoints at all, and Jikan (the ideal
MAL-backed REST API) is unreachable from some networks, hence this combo.

Built as a [Muse](https://muse.ai) skill (`SKILL.md`); also usable standalone.
