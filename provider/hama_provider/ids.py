"""Extra AniDB -> TVDB/TMDB/IMDb/MAL/Kitsu ids for entries Anime-Lists leaves unmapped: Fribb's anime-lists (one JSON file,
github.com/Fribb/anime-lists) and the links on the AniDB page itself (the AniDB XML's <resources>)."""
from __future__ import annotations

import json
import logging
import threading

from .net import DAY, Fetcher

log = logging.getLogger("hama.ids")

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
# AniDB resource types: 2 = MyAnimeList, 43 = IMDb, 44 = TheMovieDb
ANIDB_MAL, ANIDB_IMDB, ANIDB_TMDB = "2", "43", "44"


def _id(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    return value.strip() if isinstance(value, str) and value.strip().isdigit() else ""


def _first(value) -> str:
    """Fribb ids may be a single value or a list."""
    return _id(value[0]) if isinstance(value, list) and value else _id(value)


def _imdb(value) -> str:
    value = value[0] if isinstance(value, list) and value else value
    return value if isinstance(value, str) and value.startswith("tt") else ""


class ExtraIds:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self.by_aid: dict[str, dict] = {}
        self.ready = threading.Event()

    def load(self):
        data = self.fetcher.get(FRIBB_URL, "AnimeLists/fribb-anime-list-full.json", ttl=7 * DAY,
                                validate=lambda d: d.lstrip()[:1] == b"[", timeout=120)
        try:
            entries = json.loads(data.decode("utf-8")) if data else []
        except ValueError as exc:
            log.warning("Fribb anime-lists unreadable: %s", exc)
            entries = []
        out = {}
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            aid = _id(e.get("anidb_id"))
            if not aid:
                continue
            tmdb = e.get("themoviedb_id") if isinstance(e.get("themoviedb_id"), dict) else {}
            item = {k: v for k, v in (("tvdb", _id(e.get("tvdb_id") or e.get("thetvdb_id"))), ("tmdb_tv", _first(tmdb.get("tv"))),
                                      ("tmdb_movie", _first(tmdb.get("movie"))), ("imdb", _imdb(e.get("imdb_id"))),
                                      ("mal", _id(e.get("mal_id"))), ("kitsu", _id(e.get("kitsu_id")))) if v}
            if item:
                item["movie"] = str(e.get("type") or "").upper() == "MOVIE"
                out[aid] = item
        if out:
            self.by_aid = out
        self.ready.set()
        log.info("Fribb anime-lists loaded: %d AniDB entries with ids", len(self.by_aid))

    def get(self, aid: str) -> dict:
        return self.by_aid.get(aid, {})


def from_anidb(anime: dict | None) -> dict:
    """{'mal', 'imdb', 'tmdb', 'tmdb_kind'} from the AniDB page's links (empty values left out). tmdb_kind is 'movie'/'tv'
    when the link says so, else '' (unknown: the caller must confirm it, e.g. with Fribb; AniDB OVAs are often TMDB movies)."""
    res = (anime or {}).get("resources") or {}
    out = {}
    mal = next((i for i in res.get(ANIDB_MAL, []) if i.isdigit()), "")
    imdb = next((i for i in res.get(ANIDB_IMDB, []) if i.startswith("tt")), "")
    tmdb_parts = res.get(ANIDB_TMDB, [])
    tmdb = next((i for i in tmdb_parts if i.isdigit()), "")
    kind = next((i for i in tmdb_parts if i in ("movie", "tv")), "")
    if mal:
        out["mal"] = mal
    if imdb:
        out["imdb"] = imdb
    if tmdb:
        out["tmdb"] = tmdb
        out["tmdb_kind"] = kind
    return out
