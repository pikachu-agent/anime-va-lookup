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
query ($q: String) {
  Page(perPage: 10) {
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
                "anime_mal_url": (f"https://myanimelist.net/anime/{node['idMal']}"
                                  if node.get("idMal") else None),
            })
            break  # one anime per character
        if len(roles) >= limit:
            break
    return roles, staff


def cmd_character(args):
    lang = LANG_CODES.get(args.lang.lower(), "JAPANESE")
    data = graphql(CHAR_SEARCH, {"q": args.name})
    char = pick_character(args.name, data["Page"]["characters"], args.anime)
    if not char:
        print(json.dumps({"error": f"no character found for {args.name!r}"}))
        return 1
    result = {
        "character": char["name"]["full"],
        "character_kanji": char["name"].get("native"),
        "voice_actors": [],
    }
    for va in vas_for_character(char, lang):
        mal_url = mal_people_url(va["name"])
        roles, _ = notable_roles(va["anilist_id"], va["anilist_id"], args.roles)
        result["voice_actors"].append({
            "name": va["name"],
            "name_kanji": va["name_kanji"],
            "language": args.lang,
            "mal_url": mal_url,
            "notable_roles": roles,
        })
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

    args = ap.parse_args(argv)
    if args.mode == "character":
        return cmd_character(args)
    return cmd_person(args)


if __name__ == "__main__":
    sys.exit(main())
