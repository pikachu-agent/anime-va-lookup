#!/usr/bin/env python3
"""Deterministic anime voice-actor lookups: AniList GraphQL + MyAnimeList link resolution.

Two modes:

  Character lookup (default):
      va_lookup.py character "Gyari" [--anime "Iruma-kun"] [--lang Japanese]
      -> the character's voice actor(s) in the requested language,
         each with a MyAnimeList people link, plus the VA's notable roles.

  Person lookup:
      va_lookup.py person "Misato Matsuoka" [--roles 10]
      -> the voice actor's notable roles with MyAnimeList links.

Design notes (see references/api_notes.md):
- AniList GraphQL (https://graphql.anilist.co, no key) is the VA data source:
  per-media `voiceActors(language: JAPANESE)` edges give deterministic
  character -> VA mapping with id/name in both scripts.
- AniList's Staff/Character types carry NO MAL id, so MAL links are resolved
  separately: MAL's people search (`people.php?q=...&cat=person`) 303-redirects
  to the exact-match person page; otherwise the first search result is parsed
  from the HTML. This yields stable `myanimelist.net/people/{id}` links.
- The official MAL API v2 has no character/people endpoints at all, and Jikan
  (which would be ideal) is unreachable from this network, hence this combo.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

ANILIST = "https://graphql.anilist.co"
MAL_SEARCH = "https://myanimelist.net/people.php"
USER_AGENT = "anime-va-lookup/1.0 (Pika/Muse skill; contact: none)"
LANG_CODES = {"japanese": "JAPANESE", "english": "ENGLISH"}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")


def load_known_anime():
    """Local-only user config (never committed). Returns ordered title list."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return [t for t in data.get("known_anime", []) if t]
    except (OSError, ValueError):
        return []


def known_rank(anime_titles, known):
    """Priority rank of an anime in the user's list, or None.

    Matches on either the English or romaji title, exact or substring, so
    "Cherry Magic" matches "Cherry Magic! Thirty Years of Virginity...".
    Lower rank = higher priority (list order).
    """
    titles = [t.lower() for t in anime_titles if t]
    for i, k in enumerate(known):
        k = k.lower()
        for t in titles:
            if k == t or k in t or t in k:
                return i
    return None


def _http(req, data=None, _retries=2):
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return resp, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retries > 0:
            wait = int(e.headers.get("Retry-After", "10") or 10)
            time.sleep(wait)
            return _http(req, data, _retries - 1)
        if e.code in (500, 502, 503) and _retries > 0:
            # AniList occasionally 500s on complex queries; one retry is safe
            # since all our requests are idempotent reads.
            time.sleep(2)
            return _http(req, data, _retries - 1)
        raise


def graphql(query, variables):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        ANILIST, data=payload, headers={"Content-Type": "application/json"})
    _, body = _http(req)
    result = json.loads(body)
    if result.get("errors"):
        raise RuntimeError(f"AniList error: {result['errors'][0].get('message')}")
    time.sleep(1.0)  # stay well under the 30-90 req/min limit
    return result["data"]


CHAR_SEARCH = """
query ($q: String, $page: Int) {
  Page(page: $page, perPage: 10) {
    pageInfo { hasNextPage }
    characters(search: $q) {
      id
      name { full native }
      favourites
      media(sort: TRENDING_DESC, type: ANIME, perPage: 5) {
        edges {
          voiceActors(language: JAPANESE) { id name { full native } }
          node { id idMal title { romaji english native } }
        }
      }
    }
  }
}
"""

# NOTE: AniList 500s on the deeply nested Staff -> characters -> media path,
# so roles are resolved in two steps: staff -> character ids, then one aliased
# batch query over Character(id:) -> media edges.
STAFF_BASIC = """
query ($id: Int) {
  Staff(id: $id) {
    id
    name { full native }
    languageV2
    siteUrl
    characters(perPage: 24, sort: FAVOURITES_DESC) {
      nodes { id name { full } }
    }
  }
}
"""

CHAR_MEDIA_TMPL = """{alias}: Character(id: {cid}) {{
  id
  name {{ full }}
  media(sort: TRENDING_DESC, type: ANIME, perPage: 5) {{
    edges {{
      voiceActors(language: JAPANESE) {{ id }}
      node {{ idMal title {{ romaji english }} }}
    }}
  }}
}}"""


def mal_people_url(name):
    """Resolve a VA name to its MyAnimeList people URL.

    MAL's people search 303-redirects to the person page on an exact match;
    urllib follows the redirect, so resp.geturl() is the answer. Otherwise
    parse the first search-result link out of the HTML.
    """
    params = urllib.parse.urlencode({"q": name, "cat": "person"})
    req = urllib.request.Request(f"{MAL_SEARCH}?{params}")
    resp, body = _http(req)
    url = resp.geturl()
    m = re.search(r"https://myanimelist\.net/people/(\d+)/([A-Za-z0-9_%-]+)", url)
    if m:
        return f"https://myanimelist.net/people/{m.group(1)}/{m.group(2)}"
    # Fallback: first result link in the search-results HTML.
    m = re.search(r'href="(https://myanimelist\.net/people/\d+/[^"]+)"', body)
    if m:
        return m.group(1)
    return None


def pick_character(name, candidates, anime_hint=None):
    """Deterministic pick: exact name match first, then anime hint.

    If an anime hint is given but no candidate appears in that anime, return
    None instead of falling back to favourites — a wrong-anime match is worse
    than no match (it once returned Akame ga Kill!'s Susanoo for a Kusunoki
    question).
    """
    if not candidates:
        return None
    lowered = name.strip().lower()
    exact = [c for c in candidates if (c["name"]["full"] or "").lower() == lowered]
    pool = exact or candidates
    if anime_hint:
        hint = anime_hint.lower()
        for c in pool:
            for edge in (c.get("media") or {}).get("edges", []):
                node = edge.get("node") or {}
                titles = node.get("title") or {}
                blob = " ".join(str(titles.get(k) or "") for k in ("romaji", "english", "native"))
                if hint in blob.lower():
                    return c
        return None
    return max(pool, key=lambda c: c.get("favourites") or 0)


def vas_for_character(char, lang="JAPANESE"):
    seen, vas = set(), []
    for edge in (char.get("media") or {}).get("edges", []):
        actors = edge.get("voiceActors") or []
        if lang != "JAPANESE":
            continue  # only Japanese edges were fetched; other langs need another query
        for va in actors:
            if va["id"] not in seen:
                seen.add(va["id"])
                vas.append({"anilist_id": va["id"],
                            "name": va["name"]["full"],
                            "name_kanji": va["name"].get("native")})
    return vas


def notable_roles(staff_id, va_anilist_id, limit=8):
    staff = graphql(STAFF_BASIC, {"id": staff_id})["Staff"]
    char_nodes = (staff.get("characters") or {}).get("nodes", [])
    # Batch-resolve media edges for the top characters in one aliased query.
    wanted = char_nodes[: max(limit * 2, 12)]
    parts, variables = [], {}
    for i, c in enumerate(wanted):
        alias = f"c{i}"
        variables[alias] = c["id"]
        parts.append(CHAR_MEDIA_TMPL.format(alias=alias, cid="$" + alias))
    sig = ", ".join(f"${a}: Int" for a in variables)
    batch = graphql("query (%s) { %s }" % (sig, " ".join(parts)), variables)
    roles = []
    # Collect more than `limit` so the known-anime sort below has candidates;
    # truncation to `limit` happens after sorting.
    collect_cap = max(limit * 2, 12)
    for c in wanted:
        alias = next(a for a, cid in variables.items() if cid == c["id"])
        char = batch.get(alias)
        if not char:
            continue
        for edge in (char.get("media") or {}).get("edges", []):
            actor_ids = {a["id"] for a in (edge.get("voiceActors") or [])}
            if va_anilist_id not in actor_ids:
                continue
            node = edge.get("node") or {}
            titles = node.get("title") or {}
            roles.append({
                "character": char["name"]["full"],
                "anime": titles.get("english") or titles.get("romaji"),
                "_romaji": titles.get("romaji"),
                "anime_mal_url": (f"https://myanimelist.net/anime/{node['idMal']}"
                                  if node.get("idMal") else None),
            })
            break  # one anime per character
        if len(roles) >= collect_cap:
            break
    # Flag roles from the user's known anime (config.json, local-only) and
    # surface them first, ordered by the list's priority (top of the list =
    # highest). The rest of the list still covers other titles.
    # Sort known-first BEFORE truncating so a known-anime role never gets cut.
    known = load_known_anime()
    for r in roles:
        rank = known_rank([r.get("anime"), r.get("_romaji")], known)
        r["known_anime"] = rank is not None
        r["_rank"] = rank if rank is not None else 0
        del r["_romaji"]
    roles.sort(key=lambda r: (not r["known_anime"], r["_rank"]))
    for r in roles:
        del r["_rank"]
    return roles[:limit], staff


def search_characters_paged(name, max_pages=3):
    """Character search with pagination (AniList search returns 10/page)."""
    candidates = []
    for page in range(1, max_pages + 1):
        data = graphql(CHAR_SEARCH, {"q": name, "page": page})
        page_data = data["Page"]
        candidates.extend(page_data["characters"])
        if not page_data["pageInfo"]["hasNextPage"]:
            break
    return candidates


ANIME_SEARCH = """
query ($q: String) {
  Page(perPage: 10) {
    media(search: $q, type: ANIME) {
      id idMal title { romaji english native }
    }
  }
}
"""

ANIME_CAST = """
query ($id: Int) {
  Media(id: $id) {
    id idMal title { romaji english native }
    characters(sort: ROLE, perPage: 50) {
      edges {
        role
        voiceActors(language: JAPANESE) { id name { full native } }
        node { id name { full native } }
      }
    }
  }
}
"""


def find_anime(title):
    """Search AniList for an anime; prefer an exact title match."""
    results = graphql(ANIME_SEARCH, {"q": title})["Page"]["media"] or []
    if not results:
        return None
    lowered = title.strip().lower()
    for m in results:
        titles = m.get("title") or {}
        for key in ("romaji", "english", "native"):
            if (titles.get(key) or "").lower() == lowered:
                return m
    return results[0]


def anime_cast(media_id):
    return graphql(ANIME_CAST, {"id": media_id})["Media"]


def va_result(va, lang, roles_limit):
    """Shared VA result shape: MAL link + notable roles."""
    mal_url = mal_people_url(va["name"])
    roles, _ = notable_roles(va["anilist_id"], va["anilist_id"], roles_limit)
    return {
        "name": va["name"],
        "name_kanji": va["name_kanji"],
        "language": lang,
        "mal_url": mal_url,
        "notable_roles": roles,
    }


def character_via_anime(anime_title, character_name, lang, roles_limit):
    """Anime-first lookup: resolve the anime, scan its full cast for the
    character. Catches characters the global character search misses
    (e.g. Susanoo in Kusunoki's Garden of Gods, absent from AniList's
    character index but present in the media's cast)."""
    anime = find_anime(anime_title)
    if not anime:
        return None, None
    cast = anime_cast(anime["id"])
    lowered = character_name.strip().lower()
    for edge in (cast.get("characters") or {}).get("edges", []):
        node = edge.get("node") or {}
        if (node.get("name", {}).get("full") or "").lower() != lowered:
            continue
        vas = [{
            "anilist_id": v["id"],
            "name": v["name"]["full"],
            "name_kanji": v["name"].get("native"),
        } for v in (edge.get("voiceActors") or [])]
        titles = anime.get("title") or {}
        return {
            "character": node["name"]["full"],
            "character_kanji": node["name"].get("native"),
            "anime": titles.get("english") or titles.get("romaji"),
            "anime_mal_url": (f"https://myanimelist.net/anime/{anime['idMal']}"
                              if anime.get("idMal") else None),
            "voice_actors": [va_result(va, lang, roles_limit) for va in vas],
        }, anime
    return None, anime


def cmd_character(args):
    lang = LANG_CODES.get(args.lang.lower(), "JAPANESE")
    candidates = search_characters_paged(args.name)
    char = pick_character(args.name, candidates, args.anime)
    if not char:
        # Anime-first fallback: scan the anime's full cast. Catches characters
        # missing from AniList's character search index.
        if args.anime:
            result, _ = character_via_anime(args.anime, args.name, args.lang, args.roles)
            if result:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
        hint = f" in {args.anime!r}" if args.anime else ""
        print(json.dumps({"error": f"no character {args.name!r}{hint} found"}))
        return 1
    result = {
        "character": char["name"]["full"],
        "character_kanji": char["name"].get("native"),
        "voice_actors": [],
    }
    for va in vas_for_character(char, lang):
        result["voice_actors"].append(va_result(va, args.lang, args.roles))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_anime(args):
    lang = LANG_CODES.get(args.lang.lower(), "JAPANESE")
    anime = find_anime(args.name)
    if not anime:
        print(json.dumps({"error": f"no anime found for {args.name!r}"}))
        return 1
    cast = anime_cast(anime["id"])
    titles = (cast.get("title") or {})
    result = {
        "anime": titles.get("english") or titles.get("romaji"),
        "anime_mal_url": (f"https://myanimelist.net/anime/{cast['idMal']}"
                          if cast.get("idMal") else None),
        "characters": [],
    }
    for edge in (cast.get("characters") or {}).get("edges", []):
        node = edge.get("node") or {}
        if args.character and (node.get("name", {}).get("full") or "").lower() != args.character.strip().lower():
            continue
        if args.role and (edge.get("role") or "").upper() != args.role.upper():
            continue
        entry = {
            "character": node["name"]["full"],
            "character_kanji": node["name"].get("native"),
            "role": edge.get("role"),
            "voice_actors": [],
        }
        for v in (edge.get("voiceActors") or []):
            va = {"anilist_id": v["id"], "name": v["name"]["full"],
                  "name_kanji": v["name"].get("native")}
            entry["voice_actors"].append(va_result(va, args.lang, args.roles))
        result["characters"].append(entry)
        if len(result["characters"]) >= args.limit:
            break
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_person(args):
    # Reuse the character search path is wrong here; do a staff search instead.
    data = graphql("""
query ($q: String) {
  Page(perPage: 10) {
    staff(search: $q) {
      id name { full native } languageV2 favourites
    }
  }
}""", {"q": args.name})
    people = data["Page"]["staff"] or []
    if not people:
        print(json.dumps({"error": f"no person found for {args.name!r}"}))
        return 1
    lowered = args.name.strip().lower()
    person = next((p for p in people if (p["name"]["full"] or "").lower() == lowered),
                  max(people, key=lambda p: p.get("favourites") or 0))
    roles, staff = notable_roles(person["id"], person["id"], args.roles)
    result = {
        "name": staff["name"]["full"],
        "name_kanji": staff["name"].get("native"),
        "languages": staff.get("languageV2"),
        "anilist_url": staff.get("siteUrl"),
        "mal_url": mal_people_url(staff["name"]["full"]),
        "notable_roles": roles,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Anime voice-actor lookups (AniList + MAL links).")
    sub = ap.add_subparsers(dest="mode", required=True)

    c = sub.add_parser("character", help="find a character's voice actor(s)")
    c.add_argument("name")
    c.add_argument("--anime", help="anime title hint for disambiguation")
    c.add_argument("--lang", default="Japanese")
    c.add_argument("--roles", type=int, default=8, help="notable roles to include per VA")

    p = sub.add_parser("person", help="look up a voice actor and their roles")
    p.add_argument("name")
    p.add_argument("--roles", type=int, default=12)

    a = sub.add_parser("anime", help="list an anime's cast and their voice actors")
    a.add_argument("name")
    a.add_argument("--character", help="only this character from the cast")
    a.add_argument("--role", choices=["MAIN", "SUPPORTING", "BACKGROUND"],
                   help="only characters with this role type")
    a.add_argument("--lang", default="Japanese")
    a.add_argument("--roles", type=int, default=5, help="notable roles to include per VA")
    a.add_argument("--limit", type=int, default=20, help="max characters to list")

    args = ap.parse_args(argv)
    if args.mode == "character":
        return cmd_character(args)
    if args.mode == "anime":
        return cmd_anime(args)
    return cmd_person(args)


if __name__ == "__main__":
    sys.exit(main())
