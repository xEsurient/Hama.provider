"""TheTVDB (legacy v3 API, HAMA's key): series, episodes and artwork."""
from __future__ import annotations

import json
import logging
import threading
import time

from .images import Image
from .net import DAY, Fetcher

log = logging.getLogger("hama.tvdb")

API = "https://api.thetvdb.com"
ART = "https://artworks.thetvdb.com/banners/"
TOKEN_TTL = 20 * 3600
LOGIN_RETRY = 600  # after a failed login, wait before trying again


class TheTVDB:
    def __init__(self, fetcher: Fetcher, api_key: str):
        self.fetcher = fetcher
        self.api_key = api_key
        self._token, self._token_time = "", 0.0
        self._lock = threading.Lock()

    def _auth(self) -> dict | None:
        with self._lock:
            age = time.time() - self._token_time
            if (self._token and age > TOKEN_TTL) or (not self._token and age > LOGIN_RETRY):
                data = self.fetcher.post_json(API + "/login", {"apikey": self.api_key})
                self._token = (data or {}).get("token", "")
                self._token_time = time.time()
                if not self._token:
                    log.warning("TVDB login failed")
            return {"Authorization": "Bearer " + self._token} if self._token else None

    def _get(self, path: str, cache: str, lang: str = "en", ttl: float = 2 * DAY):
        if not self.api_key:
            return None
        cached = self.fetcher.read_cache(cache, ttl)
        if cached is not None:
            return json.loads(cached.decode("utf-8"))
        headers = self._auth()
        if headers is None:
            return self.fetcher.get_json(API + path, cache, ttl=10 * 365 * DAY)  # stale cache only
        headers["Accept-Language"] = lang
        return self.fetcher.get_json(API + path, cache, ttl=ttl, headers=headers, throttle=("tvdb", 0.2))

    def series(self, tvdbid: str, lang: str = "en") -> dict:
        data = self._get("/series/%s" % tvdbid, "TheTVDB/%s/series_%s.json" % (tvdbid, lang), lang)
        series = (data or {}).get("data") or {}
        if lang != "en" and (not series.get("seriesName") or not series.get("overview")):
            en = self.series(tvdbid, "en")
            series = dict(en, **{k: v for k, v in series.items() if v})
        return series

    def episodes(self, tvdbid: str, lang: str = "en") -> dict[tuple[int, int], dict]:
        """{(season, episode): episode json} in aired order."""
        out, page = {}, 1
        while page:
            data = self._get("/series/%s/episodes?page=%d" % (tvdbid, page),
                             "TheTVDB/%s/episodes_%s_p%d.json" % (tvdbid, lang, page), lang)
            if not data or not isinstance(data.get("data"), list):
                break
            for ep in data["data"]:
                if ep.get("airedSeason") is not None and ep.get("airedEpisodeNumber") is not None:
                    out[(int(ep["airedSeason"]), int(ep["airedEpisodeNumber"]))] = ep
            page = (data.get("links") or {}).get("next")
        if lang != "en" and out and sum(1 for e in out.values() if e.get("episodeName")) < len(out) / 2:
            for key, ep in self.episodes(tvdbid, "en").items():
                if key in out:
                    out[key] = dict(ep, **{k: v for k, v in out[key].items() if v})
        return out

    def images(self, tvdbid: str) -> dict[str, list[Image]]:
        """{'coverPoster': [...], 'background': [...], 'season': {n: [...]}} from TVDB's artwork."""
        out = {"coverPoster": [], "background": [], "season": {}}
        for key_type, plex_type in (("poster", "coverPoster"), ("fanart", "background"), ("season", "season")):
            data = self._get("/series/%s/images/query?keyType=%s" % (tvdbid, key_type),
                             "TheTVDB/%s/images_%s.json" % (tvdbid, key_type), ttl=7 * DAY)
            for img in (data or {}).get("data") or []:
                rating = img.get("ratingsInfo") or {}
                score = int(float(rating.get("average") or 0) * 10 + int(rating.get("count") or 0))
                item = Image(ART + img["fileName"], img.get("language") or "", score, "TheTVDB")
                if plex_type == "season":
                    if str(img.get("subKey", "")).isdigit():
                        out["season"].setdefault(int(img["subKey"]), []).append(item)
                else:
                    out[plex_type].append(item)
        return out


def thumb(ep: dict) -> str:
    """Episode still: TVDB filename, or a full URL from the TMDB fallback."""
    if ep.get("thumb_url"):
        return ep["thumb_url"]
    return ART + ep["filename"] if ep.get("filename") else ""
