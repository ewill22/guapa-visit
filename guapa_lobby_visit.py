#!/usr/bin/env python3
"""
guapa_lobby_visit.py -- a Guapa Lobby "skill".

Send your agent to hang out in the Guapa Lobby (the presence stage on
guapadata.com): it gets a body on the stage, puts a record on, sees what the
room is into, then leaves and prints a short report -- "here's what to grab" --
for your agent to relay back to you.

    pip install websockets
    python guapa_lobby_visit.py
    python guapa_lobby_visit.py --name "eric's agent" --dwell 30 --json

Heartbeat: have your agent run this on a schedule (every few hours) or on
command ("go grab a coffee and listen to some records"). Each run is one visit.

Taste: with --like it reps you across the WHOLE Guapa back catalog (~16k albums,
937 artists) instead of just the week's new releases -- see --scope.

Safe by design: reads ONLY public data (guapadata.com/data/*.json) and talks to
the already-public, wordless relay (see PROTOCOL.md) -- no secrets, no private
data, no account. Affiliate-safe: the buy pointer sends you back to guapadata.com,
where the record store's affiliate links live; the skill never mints raw affiliate
URLs itself.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

RELAY_URL = "wss://lobby.guapadata.com/"
NEW_RELEASES_URL = "https://guapadata.com/data/new-releases.json"
# The whole back catalog, minus per-track data: ~1.4 MB gzipped, 937 artists /
# ~16k albums. Emitted by the guapa-data pipeline (export_from_json.py) and
# already served publicly for the record store's browse view.
CATALOG_URL = "https://guapadata.com/data/music-catalog-slim.json"
COFFEE_URL = "https://guapadata.com/data/coffee-offerings.json"
RECORD_STORE_URL = "https://guapadata.com/music.html"

# A few Guapa-voice handles to join as when none is given (the relay hands out a
# fresh one anyway if it collides -- see PROTOCOL.md).
HANDLES = (
    "here for the music", "just browsing", "crate digger", "the regular",
    "off the street", "new in town", "passing through", "the guest",
)


def _cache_dir():
    """Where to keep cached feeds. None disables caching entirely.

    The back catalog is ~7 MB and changes once a day, when Guapa's pipeline
    regenerates it. An agent on a 4-hourly heartbeat would otherwise re-download
    the same 7 MB six times a day, which is rude to the server and slow for you.
    """
    try:
        base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / ".cache"
        d = root / "guapa-visit"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001 -- read-only home, odd container, whatever
        return None


def _fetch_json(url: str, what: str, timeout: float, cache: bool = True):
    """GET public JSON, revalidating a local cache with the server's ETag.

    Sends If-None-Match; a 304 means nothing changed and the body isn't
    transferred at all, so a repeat visit costs a few hundred bytes instead of
    7 MB. Every failure path degrades to a plain fetch, and a plain fetch failing
    just means this feed is skipped -- a visit still happens without it.
    """
    cdir = _cache_dir() if cache else None
    key = hashlib.sha256(url.encode()).hexdigest()[:16] if cdir else None
    body_f = cdir / f"{key}.json" if cdir else None
    etag_f = cdir / f"{key}.etag" if cdir else None

    headers = {"User-Agent": "guapa-lobby-skill"}
    if etag_f and etag_f.exists() and body_f and body_f.exists():
        try:
            headers["If-None-Match"] = etag_f.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            if body_f:
                try:
                    body_f.write_text(raw, encoding="utf-8")
                    tag = r.headers.get("ETag")
                    if tag:
                        etag_f.write_text(tag, encoding="utf-8")
                except Exception:  # noqa: BLE001 -- caching is a nicety, never fatal
                    pass
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 304 and body_f and body_f.exists():
            try:
                return json.loads(body_f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 -- corrupt cache, fall through
                pass
        print(f"(couldn't fetch {what}: {e})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- degrade gracefully
        # Offline or the server is down: a stale cached copy beats no visit.
        if body_f and body_f.exists():
            try:
                print(f"(using cached {what}: {e})", file=sys.stderr)
                return json.loads(body_f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        print(f"(couldn't fetch {what}: {e})", file=sys.stderr)
    return None


def fetch_new_releases(timeout: float = 8.0, cache: bool = True) -> list[dict]:
    """Public data -- this week's new records, grouped by genre. Returns a flat
    list of {'artist','album','genre','subgenre','year','notability'} for
    release_type 'new' (reissues skipped), most-notable first (notability here
    is the Wikidata sitelink count). Empty on any failure."""
    data = _fetch_json(NEW_RELEASES_URL, "new releases", timeout, cache)
    if not data:
        return []
    out: list[dict] = []
    for genre, releases in (data.get("by_genre") or {}).items():
        for rel in releases:
            if rel.get("release_type") == "reissue":
                continue
            if rel.get("artist") and rel.get("album"):
                out.append({
                    "artist": rel["artist"],
                    "album": rel["album"],
                    "genre": genre,
                    "subgenre": rel.get("subgenre") or "",
                    "year": None,  # feed carries no year -- these are this week's
                    "langs": None, "views": None, "trend": None,
                    "notability": rel.get("sitelinks") or 0,
                })
    out.sort(key=lambda r: r["notability"], reverse=True)
    return out


def _day_seed() -> str:
    """Today's date -- seeds the tie-break shuffle so a heartbeat running every
    few hours reports a consistent crate all day, but a different one tomorrow.
    Without it, 16k albums tie-broken deterministically means the same three
    records forever."""
    return datetime.date.today().isoformat()


# Every artist in the catalog has cleared the pipeline's 20-sitelink notability
# gate, so 20 is the floor to assume for one whose score isn't published yet.
# It also makes the fame term cancel out when NO artist has a score, leaving
# pure album canonicity -- how this ranked before sitelinks reached the feed.
FAME_FLOOR = 20


def fetch_catalog(timeout: float = 20.0, cache: bool = True) -> list[dict]:
    """Public data -- the WHOLE Guapa back catalog (~16k albums / 937 artists),
    flattened to the same record shape as fetch_new_releases, most-notable
    first. Empty on any failure.

    Records carry three real measurements, all from CC0 sources (Wikidata +
    Wikimedia pageviews), and they are deliberately NOT blended into one score:

    * **langs** -- how many language Wikipedias cover this specific album.
      Encyclopedic depth, and it moves slowly, so it reads as *enduring*
      significance. Thriller 64, Quiet Nights 8.
    * **views** -- human lookups over the last 12 months. Current attention.
    * **trend** -- percent change against the 12 months before that.

    The axes disagree, and that's the point. BULLY: 1.24M views, +73%, 15 langs
    -- popular right now. My Beautiful Dark Twisted Fantasy: 503k views, -12%,
    30 langs -- beloved. A single score erases that distinction; keeping them
    apart is what lets an agent hold an opinion rather than recite a number.

    Ranking uses langs first (enduring beats trending, for a record shop), broken
    by views, and falls back to the artist's own fame only for albums with no
    measurement at all -- an album no Wikipedia anywhere covers is usually a
    sampler, a single or a bootleg, so that absence is itself a verdict. Ties
    break on a day-seeded shuffle so picks vary day to day."""
    data = _fetch_json(CATALOG_URL, "the back catalog", timeout, cache)
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    measured = 0
    for artist in data.values():
        name = artist.get("name")
        if not name:
            continue
        fame = artist.get("sitelinks") or FAME_FLOOR
        for alb in artist.get("albums") or []:
            title = alb.get("title")
            if not title:
                continue
            langs = alb.get("sitelinks")
            views = alb.get("views_12mo")
            prior = alb.get("views_prior")
            trend = round(100 * (views - prior) / prior) if views and prior else None
            if langs is not None:
                measured += 1
            out.append({
                "artist": name,
                "album": title,
                "genre": alb.get("genre") or artist.get("genre") or "",
                "subgenre": alb.get("subgenre") or artist.get("subgenre") or "",
                "year": alb.get("release_year"),
                "langs": langs,
                "views": views,
                "trend": trend,
                # A tuple, so the axes stay separate rather than collapsing into
                # one number: album depth, then attention, then the artist's fame
                # as the last resort for albums nothing has measured.
                "notability": (langs if langs is not None else -1, views or 0, fame),
            })
    if not measured:
        print("(no album measurements in the catalog feed yet -- ranking on artist "
              "fame alone)", file=sys.stderr)
    rng = random.Random(_day_seed())
    rng.shuffle(out)  # then a stable sort keeps the shuffle as the tie-break
    out.sort(key=lambda r: r["notability"], reverse=True)
    return out


def _terms(like: str) -> list[str]:
    return [t.strip().lower() for t in (like or "").split(",") if t.strip()]


def taste_coverage(records: list[dict], like: str) -> dict[str, int]:
    """How many records each taste term actually hits. A term that hits nothing
    matters: with every record tied at zero matches, ranking falls back to plain
    notability and the report would hand you the house crate as though it repped
    you (--like "shoegaze" returning Miles Davis -- the catalog's 60 subgenres
    don't include shoegaze). The caller says so out loud instead."""
    out: dict[str, int] = {}
    for t in _terms(like):
        out[t] = sum(1 for r in records
                     if t in f"{r['genre']} {r['subgenre']} {r['artist']}".lower())
    return out


def rank_by_taste(records: list[dict], like: str) -> list[dict]:
    """Reorder records to rep YOUR taste. `like` is free text like
    "hip hop, shoegaze, the strokes" -- each comma-separated term is matched
    (case-insensitive substring) against a record's genre / subgenre / artist.
    Records matching more terms sort first; the sort is stable, so ties keep the
    incoming most-notable-first order. With no hint, order is unchanged."""
    terms = _terms(like)
    if not terms:
        return records

    def matches(r: dict) -> int:
        hay = f"{r['genre']} {r['subgenre']} {r['artist']}".lower()
        return sum(1 for t in terms if t in hay)

    return sorted(records, key=matches, reverse=True)


def fetch_coffee(timeout: float = 8.0, cache: bool = True) -> list[dict]:
    """Public data -- what's live on the bar (roaster, title, origin, process,
    tasting notes, product url). Empty on any failure."""
    data = _fetch_json(COFFEE_URL, "coffee", timeout, cache)
    return (data or {}).get("offerings") or []


def pick_coffee(offerings: list[dict], like: str) -> dict | None:
    """Grab one bag off the bar. With a `--like` hint, prefer one whose roaster /
    title / origin / process / tasting notes match; otherwise pick at random."""
    if not offerings:
        return None
    terms = [t.strip().lower() for t in (like or "").split(",") if t.strip()]
    if terms:
        def score(o: dict) -> int:
            hay = " ".join([
                str(o.get("roaster") or ""), str(o.get("title") or ""),
                str(o.get("country") or ""), str(o.get("process") or ""),
                " ".join(o.get("tasting_notes") or []),
            ]).lower()
            return sum(1 for t in terms if t in hay)
        best = max(offerings, key=score)
        if score(best) > 0:
            return best
    return random.choice(offerings)


async def visit(name: str, dwell: float, ws_url: str, records: list[dict]) -> dict:
    import websockets  # imported here so --help works without the dep installed

    my_pick = records[0] if records else None  # what I'll "put on" (most notable)
    names: dict[str, str] = {}   # figure id -> handle, for reading the room
    room_aux: list[str] = []     # "artist -- album" others had on their aux
    here_at_join = 1
    welcomed = asyncio.Event()

    async with websockets.connect(ws_url, max_size=2 ** 16) as ws:

        async def read_loop() -> None:
            nonlocal here_at_join
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                t = m.get("type")
                if t == "welcome":
                    here_at_join = (m.get("room") or {}).get("count", 1)
                    for f in m.get("roster", []):
                        names[f.get("id")] = f.get("name")
                        z = f.get("zone")
                        if z and z.get("kind") == "aux" and z.get("album"):
                            room_aux.append(_label(z.get("artist"), z.get("album")))
                    welcomed.set()
                elif t == "join":
                    f = m.get("figure", {})
                    names[f.get("id")] = f.get("name")
                elif t == "zone":
                    if m.get("kind") == "aux" and m.get("album"):
                        room_aux.append(_label(m.get("artist"), m.get("album")))
                elif t == "leave":
                    names.pop(m.get("id"), None)

        reader = asyncio.create_task(read_loop())
        await ws.send(json.dumps({"type": "join", "name": name, "color": random.randint(0, 359)}))

        # Wait for the welcome, then be present: wave in, put a record on, and
        # wander a little so the figure visibly moves on the live stage.
        try:
            await asyncio.wait_for(welcomed.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        await ws.send(json.dumps({"type": "emote", "verb": "wave"}))
        if my_pick:
            await ws.send(json.dumps({
                "type": "zone", "kind": "aux",
                "artist": my_pick["artist"], "album": my_pick["album"],
            }))

        moves = max(1, int(dwell / 1.2))
        just_danced = False
        for _ in range(moves):
            # Every so often, take a dance break instead of a step. A dance holds
            # until the figure moves again (the stage renders it that way), so we
            # emote once, hold still a few seconds, and the next step ends it.
            if not just_danced and random.random() < 0.12:
                await ws.send(json.dumps({"type": "emote", "verb": "dance"}))
                await asyncio.sleep(2.5 + random.random() * 2.5)
                just_danced = True
                continue
            await ws.send(json.dumps({
                "type": "move", "x": round(random.random(), 3), "y": round(random.random(), 3),
            }))
            just_danced = False
            await asyncio.sleep(1.2)

        await ws.send(json.dumps({"type": "leave"}))
        reader.cancel()

    return {
        "name": name,
        "here": here_at_join,
        "put_on": my_pick,
        "room_aux": _dedupe(room_aux),
        "records": spread_by_artist(records, 3),
        "like": "",
    }


def spread_by_artist(records: list[dict], n: int) -> list[dict]:
    """The best `n` records, at most one per artist.

    Notability is largely artist-level, so a straight top-n clusters: fame puts
    six Joan Baez albums at the top, and "3 records to grab" turns into three
    records by one person. Order is otherwise preserved, so the top pick stays
    the top pick."""
    out, seen = [], set()
    for r in records:
        key = r["artist"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= n:
            break
    return out


def _label(artist, album) -> str:
    a = (artist or "").strip()
    return f"{a} -- {album}" if a else str(album)


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


SCOPE_BLURB = {
    "new": "this week's new releases",
    "catalog": "the whole Guapa back catalog",
}


def report(res: dict, as_json: bool) -> None:
    pick, recs = res["put_on"], res["records"]
    c = res.get("coffee")
    scope = res.get("scope", "new")
    if as_json:
        print(json.dumps({
            "visited": "guapa lobby",
            "as": res["name"],
            "repping_taste": res["like"] or None,
            "taste_terms_with_nothing_in_the_crate": res.get("unmatched") or [],
            "here_at_join": res["here"],
            "i_put_on": (_label(pick["artist"], pick["album"]) if pick else None),
            "room_was_into": res["room_aux"],
            "picked_from": SCOPE_BLURB.get(scope, scope),
            # Each pick ships its measurements, not just a name. The axes can
            # disagree -- lots of views but few languages means popular right now;
            # many languages with falling views means a quiet classic -- and that
            # disagreement is what you form an opinion from. Nulls mean unmeasured,
            # which for an album usually means a sampler, single or bootleg.
            "grab_these": [{
                "record": _label(r["artist"], r["album"]),
                "year": r.get("year"),
                "languages_covering_it": r.get("langs"),
                "views_last_12mo": r.get("views"),
                "trend_pct": r.get("trend"),
            } for r in recs],
            "how_to_read_the_numbers":
                "languages_covering_it = how many language Wikipedias cover this "
                "album (enduring significance, moves slowly). views_last_12mo = "
                "human attention this year. trend_pct = change against the year "
                "before. High views + few languages = popular now; many languages "
                "+ falling views = a classic people stopped looking up. Say which "
                "it is rather than just naming the record.",
            "buy_at": RECORD_STORE_URL,
            "grabbed_coffee": ({
                "roaster": c.get("roaster"), "title": c.get("title"),
                "country": c.get("country"), "process": c.get("process"),
                "tasting_notes": c.get("tasting_notes") or [], "url": c.get("url"),
            } if c else None),
        }, indent=2, ensure_ascii=False))
        return
    print("-- Guapa Lobby visit --")
    print(f'Dropped in as "{res["name"]}". {res["here"]} in the room.')
    if res["like"]:
        print(f'Repping your taste: {res["like"]}.')
        if res.get("unmatched"):
            print(f'  (nothing in the crate for: {", ".join(res["unmatched"])}.)')
    if res["room_aux"]:
        print("The room was into: " + "; ".join(res["room_aux"][:5]) + ".")
    else:
        print("Quiet in there -- no one else had a record on.")
    if pick:
        print(f'I put on: {_label(pick["artist"], pick["album"])}.')
    if recs:
        print(f"\nRecords to grab, from {SCOPE_BLURB.get(scope, scope)}:")
        for r in recs:
            year = f', {r["year"]}' if r.get("year") else ""
            print(f'  - {_label(r["artist"], r["album"])}  ({r["genre"].title()}{year})')
            bits = []
            if r.get("langs") is not None:
                bits.append(f'{r["langs"]} languages')
            if r.get("views"):
                bits.append(f'{r["views"]:,} lookups/yr')
            if r.get("trend") is not None:
                bits.append(f'{r["trend"]:+d}% year on year')
            if bits:
                print(f'      {" | ".join(bits)}')
        print(f"\nBrowse + grab them on vinyl at the Guapa record store:\n  {RECORD_STORE_URL}")
    if c:
        origin = c.get("country") or "parts unknown"
        proc = f", {c['process']}" if c.get("process") else ""
        notes = f' -- {", ".join(c["tasting_notes"][:3])}' if c.get("tasting_notes") else ""
        print(f'\nGrabbed a coffee: {c["roaster"]} -- {c["title"]} ({origin}{proc}){notes}.')
        if c.get("url"):
            print(f'  {c["url"]}')
    else:
        print("\n(Swung by the coffee counter -- nothing readable on the bar.)")


def main() -> None:
    # The catalog is genuinely multilingual (Hebrew, Japanese, and typographic
    # hyphens in names like O-Zone), and a Windows console defaults to a legacy
    # codepage that can't encode any of it -- printing one such record killed the
    # whole visit with UnicodeEncodeError. Ask for UTF-8, and fall back to
    # replacement characters rather than a traceback.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a TextIOWrapper (piped/redirected)
            pass

    ap = argparse.ArgumentParser(description="Send your agent to hang out in the Guapa Lobby.")
    ap.add_argument("--name", default=random.choice(HANDLES), help="handle to join as")
    ap.add_argument("--dwell", type=float, default=25.0, help="seconds to hang out (default 25)")
    ap.add_argument("--like", default="", help='taste hint, e.g. "hip hop, shoegaze, the strokes"')
    ap.add_argument("--scope", choices=("auto", "new", "catalog"), default="auto",
                    help="which crate to pick from: 'new' = this week's releases, "
                         "'catalog' = the whole back catalog (~16k albums, a 1.4 MB "
                         "fetch), 'auto' (default) = catalog when --like is given, "
                         "new releases otherwise")
    ap.add_argument("--no-cache", action="store_true",
                    help="don't cache feeds locally (default: cache + revalidate by "
                         "ETag, so a repeat visit re-downloads nothing unchanged)")
    ap.add_argument("--ws", default=RELAY_URL, help="relay url override")
    ap.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = ap.parse_args()

    scope = args.scope
    if scope == "auto":
        # A taste hint is only worth having if it can reach past 6 new releases.
        scope = "catalog" if args.like.strip() else "new"
    use_cache = not args.no_cache
    records = (fetch_catalog(cache=use_cache) if scope == "catalog"
               else fetch_new_releases(cache=use_cache))
    if not records and scope == "catalog":
        print("(back catalog unavailable -- falling back to this week's releases)", file=sys.stderr)
        scope, records = "new", fetch_new_releases(cache=use_cache)
    coverage = taste_coverage(records, args.like)
    unmatched = [t for t, n in coverage.items() if n == 0]
    if unmatched:
        print(f'(nothing in the crate matches: {", ".join(unmatched)} -- '
              f"picks fall back to the house crate for those)", file=sys.stderr)
    records = rank_by_taste(records, args.like)
    coffee = pick_coffee(fetch_coffee(cache=use_cache), args.like)
    try:
        res = asyncio.run(visit(args.name, args.dwell, args.ws, records))
    except ModuleNotFoundError:
        print("This skill needs the 'websockets' package:  pip install websockets", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"Visit failed: {e}", file=sys.stderr)
        sys.exit(1)
    res["like"] = args.like.strip()
    res["scope"] = scope
    res["unmatched"] = unmatched
    res["coffee"] = coffee
    report(res, args.json)


if __name__ == "__main__":
    main()
