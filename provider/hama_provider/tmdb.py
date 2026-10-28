"""TheMovieDb API: ids, search, details, artwork, collections and TV episode lists (fallback/alternative to TheTVDB)."""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import quote

from .images import Image
from .net import DAY, Fetcher

log = logging.getLogger("hama.tmdb")

URL = "https://api.themoviedb.org/3/{path}?api_key={key}{extra}"
IMG = "https://image.tmdb.org/t/p/original{}"
IMAGE_TYPES = {"clearLogo": "logos", "coverPoster": "posters", "background": "backdrops"}


class TheMovieDb:
    def __init__(self, fetcher: Fetcher, api_key: str):
        self.fetcher = fetcher
        self.api_key = api_key

    def _get(self, path: str, cache: str, extra: str = "", ttl: float = 7 * DAY):
        if not self.api_key:
            return None
        return self.fetcher.get_json(URL.format(path=path, key=self.api_key, extra=extra), "TheMovieDb/%s.json" % cache, ttl=ttl)

    def find(self, external_id: str, source: str) -> tuple[str, str]:
        """(tv_id, movie_id) for a tvdb_id / imdb_id."""
        data = self._get("find/%s" % external_id, "find/%s_%s" % (source, external_id), "&external_source=%s" % source) or {}
        tv = (data.get("tv_results") or [{}])[0].get("id", "")
        mv = (data.get("movie_results") or [{}])[0].get("id", "")
        return str(tv), str(mv)

    def search_tv(self, query: str, year: int | None = None) -> list[tuple[str, str, str]]:
        """[(tmdb id, name, first air date)] for a TV title search."""
        extra = "&query=%s%s" % (quote(query), "&first_air_date_year=%d" % year if year else "")
        data = self._get("search/tv", "search/tv_%s_%s" % (_slug(query), year or ""), extra) or {}
        return [(str(r["id"]), r.get("name") or "", r.get("first_air_date") or "") for r in data.get("results") or []]

    def search_movie(self, query: str, year: int | None = None) -> list[tuple[str, str, str]]:
        """[(tmdb id, title, release date)] for a movie title search."""
        extra = "&query=%s%s" % (quote(query), "&year=%d" % year if year else "")
        data = self._get("search/movie", "search/movie_%s_%s" % (_slug(query), year or ""), extra) or {}
        return [(str(r["id"]), r.get("title") or "", r.get("release_date") or "") for r in data.get("results") or []]

    def credits(self, movie: bool, tmdb_id: str) -> dict:
        kind = "movie" if movie else "tv"
        return (self._get("%s/%s/credits" % (kind, tmdb_id), "%s/%s_credits" % (kind, tmdb_id)) or {}) if tmdb_id else {}

    def tvdb_id(self, tv_id: str) -> str:
        data = self._get("tv/%s/external_ids" % tv_id, "tv/%s_external_ids" % tv_id) or {}
        return str(data.get("tvdb_id") or "")

    def collection(self, collection_id: str) -> dict:
        return (self._get("collection/%s" % collection_id, "collection/%s" % collection_id) or {}) if collection_id else {}

    def details(self, movie: bool, tmdb_id: str, lang: str = "") -> dict:
        if not tmdb_id:
            return {}
        kind = "movie" if movie else "tv"
        extra = "&language=%s" % lang if lang else ""
        return self._get("%s/%s" % (kind, tmdb_id), "%s/%s_details%s" % (kind, tmdb_id, "_" + lang if lang else ""), extra) or {}

    def content_rating(self, movie: bool, tmdb_id: str, country: str) -> str:
        """Certification for a country in Plex's format: US ratings as-is, others 'xx/rating' (e.g. 'de/16')."""
        if not tmdb_id or not country:
            return ""
        country = country.upper()
        if movie:
            data = self._get("movie/%s/release_dates" % tmdb_id, "movie/%s_release_dates" % tmdb_id) or {}
            certs = [d.get("certification") for r in data.get("results") or [] if r.get("iso_3166_1") == country
                     for d in r.get("release_dates") or []]
        else:
            data = self._get("tv/%s/content_ratings" % tmdb_id, "tv/%s_content_ratings" % tmdb_id) or {}
            certs = [r.get("rating") for r in data.get("results") or [] if r.get("iso_3166_1") == country]
        cert = next((c for c in certs if c), "")
        return "" if not cert else cert if country == "US" else "%s/%s" % (country.lower(), cert)

    def images(self, movie: bool, tmdb_id: str) -> dict[str, list[Image]]:
        if not tmdb_id:
            return {}
        kind = "movie" if movie else "tv"
        data = self._get("%s/%s/images" % (kind, tmdb_id), "%s/%s_images" % (kind, tmdb_id))
        if not isinstance(data, dict):
            return {}
        out = {}
        for plex_type, key in IMAGE_TYPES.items():
            for item in data.get(key) or []:
                if item.get("file_path"):
                    score = int(float(item.get("vote_average") or 0) * 10 + int(item.get("vote_count") or 0))
                    out.setdefault(plex_type, []).append(Image(IMG.format(item["file_path"]), item.get("iso_639_1") or "", score, "TheMovieDb"))
        return out

    def episodes(self, tv_id: str, lang: str = "") -> dict[tuple[int, int], dict]:
        """{(season, episode): episode} in TMDB order, normalised to the TheTVDB field names the service uses
        (episodeName, overview, firstAired, absoluteNumber, thumb_url). absoluteNumber counts regular seasons in order."""
        if not tv_id:
            return {}
        show = self.details(False, tv_id)
        out, absolute = {}, 0
        for season in sorted(show.get("seasons") or [], key=lambda s: s.get("season_number", 0)):
            number = season.get("season_number")
            if number is None:
                continue
            extra = "&language=%s" % lang if lang else ""
            data = self._get("tv/%s/season/%s" % (tv_id, number), "tv/%s_season_%s%s" % (tv_id, number, "_" + lang if lang else ""),
                             extra, ttl=2 * DAY) or {}
            for ep in sorted(data.get("episodes") or [], key=lambda e: e.get("episode_number", 0)):
                if number > 0:
                    absolute += 1
                out[(int(number), int(ep["episode_number"]))] = {
                    "episodeName": ep.get("name") or "", "overview": ep.get("overview") or "", "firstAired": ep.get("air_date") or "",
                    "absoluteNumber": absolute if number > 0 else None, "runtime": ep.get("runtime"),
                    "thumb_url": IMG.format(ep["still_path"]) if ep.get("still_path") else ""}
        return out


def _slug(query: str) -> str:
    """Cache-file-safe, collision-free name for a search query (readable prefix + hash of the exact text)."""
    return "%s_%s" % (re.sub(r"[^\w]+", "_", query.lower())[:60], hashlib.sha1(query.encode("utf-8")).hexdigest()[:10])
