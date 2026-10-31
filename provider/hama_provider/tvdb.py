"""TheTVDB: series, episodes and artwork. API v4 first when a v4 key is set (your own key: thetvdb.com/api-information),
then the legacy v3 API (HAMA's shared key), which is being phased out."""
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
API4 = "https://api4.thetvdb.com/v4"
TOKEN_TTL = 20 * 3600
TOKEN4_TTL = 25 * DAY  # v4 tokens are valid for a month
LOGIN_RETRY = 600  # after a failed login, wait before trying again
# v4 artwork type ids (series): poster, background, clear logo. Read from /artwork/types when it answers; these are the fallback.
ART4_TYPES = {2: "coverPoster", 3: "background", 23: "clearLogo"}
ART4_SLUGS = {"posters": "coverPoster", "poster": "coverPoster", "backgrounds": "background", "background": "background",
              "clearlogo": "clearLogo"}
# v4 uses ISO 639-2 language codes, Plex and the settings ISO 639-1
LANG3 = {"en": "eng", "ja": "jpn", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por", "ru": "rus", "zh": "zho",
         "ko": "kor", "nl": "nld", "pl": "pol", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "cs": "ces", "hu": "hun",
         "tr": "tur", "ar": "ara", "he": "heb", "el": "ell", "th": "tha", "vi": "vie", "id": "ind", "uk": "ukr", "ro": "ron"}
LANG2 = {v: k for k, v in LANG3.items()}


class TheTVDB:
    def __init__(self, fetcher: Fetcher, api_key: str, v4_key: str = "", v4_pin: str = ""):
        self.fetcher = fetcher
        self.api_key = api_key
        self.v4_key, self.v4_pin = v4_key, v4_pin
        self._token, self._token_time = "", 0.0
        self._token4, self._token4_time = "", 0.0
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

    # API v4 ----------------------------------------------------------------------------------------------------------
    def _auth4(self) -> str:
        with self._lock:
            age = time.time() - self._token4_time
            if (self._token4 and age > TOKEN4_TTL) or (not self._token4 and age > LOGIN_RETRY):
                payload = {"apikey": self.v4_key}
                if self.v4_pin:
                    payload["pin"] = self.v4_pin
                data = self.fetcher.post_json(API4 + "/login", payload)
                self._token4 = ((data or {}).get("data") or {}).get("token", "")
                self._token4_time = time.time()
                if not self._token4:
                    log.warning("TVDB v4 login failed: check tvdb_v4_api_key (and tvdb_v4_pin for subscriber keys)")
            return self._token4

    def _get4(self, path: str, cache: str, ttl: float = 2 * DAY):
        """v4 'data' payload (or the whole answer when it has 'links', for paging)."""
        if not self.v4_key:
            return None
        cached = self.fetcher.read_cache(cache, ttl)
        if cached is not None:
            return json.loads(cached.decode("utf-8"))
        token = self._auth4()
        if not token:
            return self.fetcher.get_json(API4 + path, cache, ttl=10 * 365 * DAY)  # stale cache only
        return self.fetcher.get_json(API4 + path, cache, ttl=ttl, headers={"Authorization": "Bearer " + token},
                                     throttle=("tvdb4", 0.2))

    def _extended(self, tvdbid: str) -> dict:
        data = self._get4("/series/%s/extended" % tvdbid, "TheTVDB/%s/v4_extended.json" % tvdbid)
        return (data or {}).get("data") or {}

    def _translation(self, tvdbid: str, lang3: str) -> dict:
        data = self._get4("/series/%s/translations/%s" % (tvdbid, lang3), "TheTVDB/%s/v4_translation_%s.json" % (tvdbid, lang3))
        return (data or {}).get("data") or {}

    def _art_types(self) -> dict[int, str]:
        data = self._get4("/artwork/types", "TheTVDB/v4_artwork_types.json", ttl=30 * DAY)
        found = {t["id"]: ART4_SLUGS[(t.get("slug") or "").lower()] for t in (data or {}).get("data") or []
                 if isinstance(t, dict) and (t.get("recordType") or "").lower() == "series" and (t.get("slug") or "").lower() in ART4_SLUGS}
        return found if "clearLogo" in found.values() else ART4_TYPES

    def series4(self, tvdbid: str, lang: str = "en") -> dict:
        """v4 series in the v3 field names the service uses ({} without a v4 key or when TheTVDB doesn't answer)."""
        ext = self._extended(tvdbid) if self.v4_key else {}
        if not ext:
            return {}
        lang3 = LANG3.get(lang, "eng")
        tr = self._translation(tvdbid, lang3)
        if lang3 != "eng" and not (tr.get("name") and tr.get("overview")):
            en = self._translation(tvdbid, "eng")
            tr = dict(en, **{k: v for k, v in tr.items() if v})
        original = ext.get("originalLanguage") == lang3
        network = (ext.get("originalNetwork") or ext.get("latestNetwork") or {}).get("name") or ""
        imdb = next((r.get("id") for r in ext.get("remoteIds") or [] if (r.get("sourceName") or "").lower() == "imdb"), "") or ""
        rating = next((r.get("name") for r in ext.get("contentRatings") or [] if r.get("country") == "usa"), "") or ""
        return {"seriesName": tr.get("name") or ext.get("name") or "",
                "overview": tr.get("overview") or (ext.get("overview") if original else "") or "",
                "firstAired": ext.get("firstAired") or "", "network": network, "imdbId": imdb, "rating": rating,
                "runtime": str(ext["averageRuntime"]) if ext.get("averageRuntime") else "",
                "genre": [g["name"] for g in ext.get("genres") or [] if g.get("name")]}

    def episodes4(self, tvdbid: str, lang: str = "en") -> dict[tuple[int, int], dict]:
        """Aired-order ('official') episodes from v4, normalised to the v3 field names."""
        if not self.v4_key:
            return {}
        lang3 = LANG3.get(lang, "eng")
        out, page = {}, 0
        while page is not None and page < 50:
            data = self._get4("/series/%s/episodes/official/%s?page=%d" % (tvdbid, lang3, page),
                              "TheTVDB/%s/v4_episodes_%s_p%d.json" % (tvdbid, lang3, page))
            body = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else {}
            # the translated list nests the episodes in data.series (untranslated: data.episodes)
            eps = body.get("episodes") if isinstance(body.get("episodes"), list) else (body.get("series") or {}).get("episodes")
            if not isinstance(eps, list):
                break
            for ep in eps:
                if ep.get("seasonNumber") is None or ep.get("number") is None:
                    continue
                out[(int(ep["seasonNumber"]), int(ep["number"]))] = {
                    "episodeName": ep.get("name") or "", "overview": ep.get("overview") or "", "firstAired": ep.get("aired") or "",
                    "absoluteNumber": ep.get("absoluteNumber") or None, "runtime": ep.get("runtime"),
                    "thumb_url": art_url(ep.get("image") or "")}
            page = page + 1 if (data.get("links") or {}).get("next") else None
        if lang3 != "eng" and out and sum(1 for e in out.values() if e["episodeName"]) < len(out) / 2:
            for key, ep in self.episodes4(tvdbid, "en").items():
                if key in out:
                    out[key] = dict(ep, **{k: v for k, v in out[key].items() if v})
        return out

    def images4(self, tvdbid: str) -> dict:
        """{'coverPoster', 'background', 'clearLogo': [...], 'season': {n: [...]}} from the v4 series record."""
        ext = self._extended(tvdbid) if self.v4_key else {}
        out = {"coverPoster": [], "background": [], "clearLogo": [], "season": {}}
        types = self._art_types() if ext.get("artworks") else ART4_TYPES
        for art in ext.get("artworks") or []:
            plex_type = types.get(art.get("type"))
            if plex_type and art.get("image"):
                lang = LANG2.get(art.get("language") or "", art.get("language") or "")
                out[plex_type].append(Image(art_url(art["image"]), lang, int(art.get("score") or 0), "TheTVDB"))
        for season in ext.get("seasons") or []:
            if (season.get("type") or {}).get("type") == "official" and season.get("image") and season.get("number") is not None:
                out["season"].setdefault(int(season["number"]), []).append(Image(art_url(season["image"]), "", 0, "TheTVDB"))
        return out

    # Public: v4 first, then v3 ---------------------------------------------------------------------------------------
    def series(self, tvdbid: str, lang: str = "en") -> dict:
        s4 = self.series4(tvdbid, lang)
        if s4.get("seriesName") and s4.get("overview"):
            return s4
        s3 = self.series3(tvdbid, lang)
        return dict(s3, **{k: v for k, v in s4.items() if v})

    def episodes(self, tvdbid: str, lang: str = "en") -> dict[tuple[int, int], dict]:
        return self.episodes4(tvdbid, lang) or self.episodes3(tvdbid, lang)

    def images(self, tvdbid: str) -> dict[str, list[Image]]:
        v3 = self.images3(tvdbid)
        v4 = self.images4(tvdbid)
        out = {t: v4.get(t, []) + v3.get(t, []) for t in ("coverPoster", "background", "clearLogo")}
        out["season"] = {n: v4["season"].get(n, []) + v3["season"].get(n, []) for n in set(v4["season"]) | set(v3["season"])}
        return out

    # Legacy API v3 ---------------------------------------------------------------------------------------------------
    def series3(self, tvdbid: str, lang: str = "en") -> dict:
        data = self._get("/series/%s" % tvdbid, "TheTVDB/%s/series_%s.json" % (tvdbid, lang), lang)
        series = (data or {}).get("data") or {}
        if lang != "en" and (not series.get("seriesName") or not series.get("overview")):
            en = self.series3(tvdbid, "en")
            series = dict(en, **{k: v for k, v in series.items() if v})
        return series

    def episodes3(self, tvdbid: str, lang: str = "en") -> dict[tuple[int, int], dict]:
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
            for key, ep in self.episodes3(tvdbid, "en").items():
                if key in out:
                    out[key] = dict(ep, **{k: v for k, v in out[key].items() if v})
        return out

    def images3(self, tvdbid: str) -> dict[str, list[Image]]:
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


def art_url(path: str) -> str:
    """v4 image paths are usually full URLs, sometimes '/banners/…'."""
    if not path or path.startswith("http"):
        return path
    return "https://artworks.thetvdb.com" + path if path.startswith("/") else ART + path


def thumb(ep: dict) -> str:
    """Episode still: TVDB filename, or a full URL from the TMDB fallback."""
    if ep.get("thumb_url"):
        return ep["thumb_url"]
    return ART + ep["filename"] if ep.get("filename") else ""
