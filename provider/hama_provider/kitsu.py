"""Kitsu (kitsu.app, public API, no key): show and episode synopses, the last fallback when AniDB, TheTVDB and TheMovieDb
have none. Found through the Kitsu id (Fribb's anime-lists) or the MyAnimeList id."""
from __future__ import annotations

import logging

from .net import DAY, Fetcher

log = logging.getLogger("hama.kitsu")

API = "https://kitsu.app/api/edge"
PAGE = 20          # Kitsu's maximum page size
MAX_PAGES = 25     # 500 episodes


class Kitsu:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def _get(self, path: str, cache: str, ttl: float = 7 * DAY) -> dict:
        data = self.fetcher.get_json(API + path, "Kitsu/%s.json" % cache, ttl=ttl,
                                     headers={"Accept": "application/vnd.api+json"}, throttle=("kitsu", 0.5))
        return data if isinstance(data, dict) else {}

    def id_for_mal(self, mal: str) -> str:
        if not mal:
            return ""
        data = self._get("/mappings?filter%%5BexternalSite%%5D=myanimelist/anime&filter%%5BexternalId%%5D=%s&include=item" % mal,
                         "mal/%s" % mal, ttl=30 * DAY)
        return next((str(i.get("id")) for i in data.get("included") or [] if i.get("type") == "anime" and i.get("id")), "")

    def synopsis(self, kitsu_id: str) -> str:
        if not kitsu_id:
            return ""
        attrs = (self._get("/anime/%s" % kitsu_id, "anime/%s" % kitsu_id).get("data") or {}).get("attributes") or {}
        return (attrs.get("synopsis") or attrs.get("description") or "").strip()

    def episodes(self, kitsu_id: str) -> dict[int, dict]:
        """{number: {'title', 'summary', 'airdate'}}"""
        out = {}
        if not kitsu_id:
            return out
        for page in range(MAX_PAGES):
            data = self._get("/anime/%s/episodes?page%%5Blimit%%5D=%d&page%%5Boffset%%5D=%d&sort=number" % (kitsu_id, PAGE, page * PAGE),
                             "anime/%s/episodes_%d" % (kitsu_id, page))
            for item in data.get("data") or []:
                a = item.get("attributes") or {}
                if isinstance(a.get("number"), int):
                    out[a["number"]] = {"title": a.get("canonicalTitle") or "", "airdate": a.get("airdate") or "",
                                        "summary": (a.get("description") or a.get("synopsis") or "").strip()}
            if not (data.get("links") or {}).get("next"):
                break
            if page == 0 and not any(e["summary"] for e in out.values()):
                break  # no text on the first page: obscure entry, don't page through the rest
        return out
