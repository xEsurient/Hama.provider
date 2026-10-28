"""HAMA – AniDB Metadata Provider for Plex. Builds Plex provider responses (match, metadata, children, images).

Two numbering modes, chosen by the provider (agent) a library uses or by folder tags:
  anidb-<aid>      one AniDB entry = one show; season 1 = episodes, season 0 = specials (HAMA "anidb" mode)
  tvdb-<tvdbid>    one TVDB series = one show; AniDB entries mapped into TVDB seasons via Anime-Lists (HAMA "tvdb" mode)
Both are turned into the same series model, then into Plex JSON."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from urllib.parse import quote

from . import __version__
from .anidb import AniDB, TitleDB, genres_and_rating, similarity
from .anilist import AniList
from .animelists import AnimeLists, episode_map
from .config import DEFAULTS, Config, coerce
from .images import Artwork, Image, order
from .net import Fetcher
from .prefetch import Prefetcher
from .report import ANIME_LISTS_ISSUE, Events, Issues
from .titles import cleanse_title, official_title, pick_title
from .tmdb import IMG as TMDB_IMG, TheMovieDb
from .tvdb import TheTVDB, thumb
from .tvdb4 import StoryArcs, arc_of

log = logging.getLogger("hama.service")

KINDS = {
    "tv": {"identifier": "tv.plex.agents.custom.hama.tv", "title": "HAMA – Anime (AniDB numbering)", "types": [2, 3, 4], "mode": "anidb"},
    "tvdb": {"identifier": "tv.plex.agents.custom.hama.tvdb", "title": "HAMA – Anime (TVDB seasons)", "types": [2, 3, 4], "mode": "tvdb"},
    "movies": {"identifier": "tv.plex.agents.custom.hama.movies", "title": "HAMA – Anime Movies", "types": [1], "mode": "anidb"},
}
METADATA_PATH = "/library/metadata"
COLLECTIONS_PATH = "/library/collections"
COLLECTION_KEY_RE = re.compile(r"^(tvdbset|tmdbset)-(\d+)$")
RATING_KEY_RE = re.compile(r"^(anidb|tvdb|tmdb)-(\d+)(?:-(a|m|t3|t4))?(?:-s(\d+)(?:-e(\d+))?)?$")  # -code = episode order (ORDERS)
FORCED_ID_RE = re.compile(r"[\[{](anidb|tvdb|tmdb|imdb)[2-9]?-(tt\d+|\d+)[\]}]", re.IGNORECASE)
GUID_RE = re.compile(r"^(anidb|tvdb|tmdb|imdb)://(tt\d+|\d+)$", re.IGNORECASE)
GENERIC_EP_TITLE = re.compile(r"^(Episode|Special|Complete Movie|OVA|Part)\s*\d*$", re.IGNORECASE)
ANIDB_FETCH_BUDGET = 3     # uncached AniDB API calls allowed per Plex request (4 s each); the rest fill in on later refreshes
MODEL_TTL = 600
# Episode orders offered to Plex as SeasonType (Edit > Advanced > Episode ordering). Code = rating-key marker.
# PMS sends episodeOrder as source + capitalised id (e.g. tvdbAbsolute).
ORDERS = {
    "": {"id": "aired", "source": "tvdb", "tag": "Aired", "title": "TheTVDB (Aired Order)", "kind": "aired"},
    "a": {"id": "absolute", "source": "tvdb", "tag": "Absolute", "title": "TheTVDB (Absolute Order)", "kind": "absolute"},
    "t3": {"id": "seasonsAbsolute", "source": "tvdb", "tag": "Seasons, absolute episodes",
           "title": "TheTVDB (Seasons, Absolute Episode Numbers)", "kind": "seasons_absolute"},   # HAMA tvdb3
    "t4": {"id": "arcs", "source": "tvdb", "tag": "Story arcs", "title": "Story Arcs (Absolute Series Scanner list)",
           "kind": "arcs"},                                                                   # HAMA tvdb4
    "m": {"id": "tmdbAired", "source": "tmdb", "tag": "Aired", "title": "TheMovieDb (Aired Order)", "kind": "aired"},  # ids must be unique
}
THEME_URL = "https://tvthemes.plexapp.com/{}.mp3"


PROFILE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_NAMES = {"report", "status", "api", "library", "static"}
# Settings a profile can change: the ones read while answering a Plex request. Keys, AniDB client, cache, pre-fetch,
# collection sync and server settings are process-wide.
PROFILE_KEYS = {"series_language_priority", "episode_language_priority", "use_request_language", "original_title_language",
                "localize_summaries", "poster_sources", "background_sources", "season_poster_sources", "logo_sources",
                "logo_language_priority", "max_images_per_type", "theme_songs", "tmdb_rating", "collections",
                "match_min_score", "genres_add_weights", "genre_minimum_weight"}


SECRET_KEYS = ("fanart_api_key", "tmdb_api_key", "tvdb_api_key", "plex_token")


def masked_settings(cfg: Config) -> dict:
    """Effective settings for display: secrets reduced to 'HAMA default' / 'set' / 'not set'."""
    out = {}
    for key in DEFAULTS:
        if key == "profiles":
            continue
        value = cfg.get(key)
        if key in SECRET_KEYS:
            value = "not set" if not value else "HAMA default" if value == DEFAULTS[key] else "set"
        out[key] = value
    return out


def load_profiles(cfg: Config) -> dict:
    """config 'profiles': {"tv-en": {"base": "tv", "title": "...", <any setting>: value}} -> extra providers at /tv-en
    with their own identifier and settings (Plex passes no provider settings, so one URL per settings variant)."""
    out = {}
    for name, spec in (cfg.get("profiles") or {}).items():
        spec = dict(spec or {})
        base = spec.pop("base", "")
        if not PROFILE_NAME_RE.match(name) or name in KINDS or name in RESERVED_NAMES or base not in KINDS:
            log.error("Profile '%s' ignored: name must be lowercase letters/digits/dashes and base one of %s", name, list(KINDS))
            continue
        title = spec.pop("title", "%s (%s)" % (KINDS[base]["title"], name))
        merged = Config(cfg)
        for key, value in spec.items():
            if key not in PROFILE_KEYS:
                log.warning("Profile '%s': setting '%s' can't differ per profile (only %s); ignored", name, key, sorted(PROFILE_KEYS))
                continue
            merged[key] = coerce(key, value)
        out[name] = {"base": base, "title": title, "cfg": merged,
                     "identifier": "tv.plex.agents.custom.hama.%s" % name.replace("-", ".")}
    return out


def season_title(season: int) -> str:
    return "Specials" if season == 0 else "Season %d" % season


class Service:
    def __init__(self, cfg: Config):
        self.base_cfg = cfg
        self.profiles = load_profiles(cfg)
        self.fetcher = Fetcher(os.path.join(cfg.data_dir, "cache"))
        self.titledb = TitleDB(self.fetcher)
        self.anidb = AniDB(self.fetcher, cfg.anidb_client, cfg.anidb_clientver)
        self.lists = AnimeLists(self.fetcher, cfg.data_dir)
        self.art = Artwork(self.fetcher, cfg.fanart_api_key)
        self.tmdb = TheMovieDb(self.fetcher, cfg.tmdb_api_key)
        self.tvdb = TheTVDB(self.fetcher, cfg.tvdb_api_key)
        self.arcs = StoryArcs(self.fetcher)
        self.anilist = AniList(self.fetcher)
        self.issues = Issues(cfg.data_dir)
        self.events = Events(cfg.data_dir)
        self.fetcher.on_host_down = lambda host, exc: self.events.add("source down", "%s unreachable (%s); cache used for 10 min" % (host, exc))
        self.anidb.on_ban = lambda: self.events.add("AniDB ban", "AniDB refused requests (ban); cache only for 3 hours. Check the AniDB client name and request rate.")
        self._report_lock = threading.Lock()
        self._request = threading.local()
        self.prefetch = Prefetcher(self, cfg.anidb_prefetch_seconds)
        self.tvdb_fallbacks = 0
        self.last_sync: dict = {}
        self.last_rescue: dict = {}
        self._models: dict[tuple, tuple[float, dict]] = {}
        self._generation: dict[str, int] = {}  # bumped by forget(): models built before it aren't cached
        self._models_lock = threading.Lock()

    # Startup ---------------------------------------------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._load, name="hama-load", daemon=True).start()
        self.prefetch.start()

    def begin_request(self, profile: str = ""):
        """Start of a Plex request / background item: reset the AniDB budget and select the profile's settings."""
        self._request.anidb_left = ANIDB_FETCH_BUDGET
        self.select_profile(profile)

    def select_profile(self, profile: str = ""):
        """Switch settings without resetting the AniDB budget (background jobs walking many items)."""
        self._request.profile = profile if profile in self.profiles else ""

    @property
    def cfg(self) -> Config:
        """Settings of the current request's profile (thread-local), else the base settings."""
        profile = getattr(self._request, "profile", "")
        return self.profiles[profile]["cfg"] if profile else self.base_cfg

    @property
    def profile(self) -> str:
        return getattr(self._request, "profile", "")

    def identifier(self, kind: str) -> str:
        """Provider identifier / GUID scheme: the current profile's when it is built on this kind."""
        profile = self.profile
        if profile and self.profiles[profile]["base"] == kind:
            return self.profiles[profile]["identifier"]
        return KINDS[kind]["identifier"]

    def resolve(self, path_kind: str):
        """URL path segment -> (base kind, profile name) or None."""
        if path_kind in KINDS:
            return path_kind, ""
        if path_kind in self.profiles:
            return self.profiles[path_kind]["base"], path_kind
        return None

    def _anidb_budgeted(self, aid: str) -> dict | None:
        """AniDB data from cache; at most ANIDB_FETCH_BUDGET uncached API calls per request (4 s each, ban risk).
        Entries skipped here are fetched by the background pre-fetch."""
        cached = self.anidb.anime(aid, fetch=False)
        left = getattr(self._request, "anidb_left", ANIDB_FETCH_BUDGET)
        if cached is None and left > 0:
            self._request.anidb_left = left - 1
            cached = self.anidb.anime(aid)
        return cached

    def forget(self, root: str):
        """Drop cached models of a show (all kinds/languages/orders) so the next request rebuilds it."""
        with self._models_lock:
            self._generation[root] = self._generation.get(root, 0) + 1
            for key in [k for k in self._models if k[2] == root]:  # key: (kind, profile, root, lang, order, country)
                del self._models[key]

    def _load(self):
        for loader in (self.lists.load, self.titledb.load):
            try:
                loader()
            except Exception:  # keep serving; requests will report not-ready
                log.exception("Startup load failed")
        while True:  # daily maintenance; the core lists refresh through their cache TTL when reloaded
            removed = self.fetcher.prune(self.cfg.cache_max_age_days)
            if removed:
                log.info("Cache: pruned %d files older than %d days", removed, self.cfg.cache_max_age_days)
            time.sleep(86400)
            for loader in (self.lists.load, self.titledb.load):
                try:
                    loader()
                except Exception:
                    log.exception("Daily reload failed")

    def status(self) -> dict:
        files, size = self.fetcher.size()
        return {"version": __version__, "ready": self.titledb.ready.is_set(), "anime": len(self.titledb.titles),
                "mappings": len(self.lists.by_aid), "anidb_banned_until": self.anidb.banned_until,
                "cache_files": files, "cache_bytes": size, "issues": self.issues.count(), "last_sync": self.last_sync, "last_rescue": self.last_rescue,
                "tvdb_fallbacks": self.tvdb_fallbacks, "prefetch_queue": len(self.prefetch), "prefetch_done": self.prefetch.done,
                "hosts_down": self.fetcher.hosts_down(),
                "providers": {k: "/" + k for k in list(KINDS) + list(self.profiles)},
                "settings": masked_settings(self.base_cfg),
                "profiles": {name: {"base": p["base"], "identifier": p["identifier"], "title": p["title"],
                                    "settings": {k: v for k, v in masked_settings(p["cfg"]).items()
                                                 if p["cfg"].get(k) != self.base_cfg.get(k)}}
                             for name, p in self.profiles.items()}}

    def wait_ready(self, timeout: float = 120) -> bool:
        return self.titledb.ready.wait(timeout) and self.lists.ready.wait(1)

    # Provider root ---------------------------------------------------------------------------------------------------
    def provider(self, kind: str) -> dict:
        info = KINDS[kind]
        profile = self.profiles.get(self.profile) if self.profile else None
        return {"MediaProvider": {
            "identifier": self.identifier(kind),
            "title": profile["title"] if profile else info["title"],
            "version": __version__,
            "Types": [{"type": t, "Scheme": [{"scheme": self.identifier(kind)}]} for t in info["types"]],
            "Feature": [{"type": "metadata", "key": METADATA_PATH},
                        {"type": "match", "key": METADATA_PATH + "/matches"},
                        {"type": "collection", "key": COLLECTIONS_PATH}]
                       + ([{"type": "markers", "key": "/markers"}] if self.base_cfg.experimental_markers else []),
        }}

    # Language helpers ------------------------------------------------------------------------------------------------
    def _languages(self, setting: list[str], lang: str) -> list[str]:
        langs = list(setting)
        if self.cfg.use_request_language and lang and lang not in langs[:1]:
            langs = [lang] + [l for l in langs if l != lang]
        return langs

    def _logo_languages(self, lang: str) -> list[str]:
        """The library language always comes first, then the configured order."""
        if not lang:
            return list(self.cfg.logo_language_priority)
        return [lang] + [l for l in self.cfg.logo_language_priority if l != lang]

    def _image_languages(self, lang: str) -> list[str]:
        return list(dict.fromkeys([l for l in (lang, "en", "ja") if l] + [""]))

    # Match -----------------------------------------------------------------------------------------------------------
    def match(self, kind: str, body: dict, lang: str, country: str = "") -> list[dict]:
        type_ = _int(body.get("type"), 1 if kind == "movies" else 2)
        manual = str(body.get("manual", "0")) in ("1", "true", "True")
        title = body.get("title") if type_ in (1, 2) else body.get("parentTitle") if type_ == 3 else body.get("grandparentTitle")
        candidates = self.find(kind, title or "", body.get("guid") or "", body.get("filename") or "",
                               _int(body.get("year"), 0) or None, manual)
        forced = FORCED_ID_RE.search(title or "") or FORCED_ID_RE.search(body.get("filename") or "")
        # PMS sends includeAdult=1 only when the library allows explicit content ("Allow matching to explicit content"),
        # and omits it otherwise (tested, PMS 1.43.5): filter unless it says 1. Folder tags always win.
        if not forced and str(body.get("includeAdult", "")) not in ("1", "true", "True"):
            candidates = self._drop_adult(candidates, fetch=not manual)
        if not candidates:
            return []
        if manual and type_ in (1, 2):
            return [self._light(kind, root, score, lang) for score, root in candidates]
        root = candidates[0][1]
        include_children = str(body.get("includeChildren", "0")) == "1"
        order = body.get("episodeOrder") or ""
        code = _order_code(order) if root.startswith("tvdb-") else ""
        season_root = root + ("-" + code if code else "")
        if type_ in (1, 2):
            item = self.metadata(kind, root, lang, include_children, order, country)
        elif type_ == 3:
            item = self.metadata(kind, "%s-s%d" % (season_root, _int(body.get("index"), 1)), lang, include_children, "", country)
        else:
            season, episode = _int(body.get("parentIndex"), 1), _int(body.get("index"), 0)
            item = self.metadata(kind, "%s-s%d-e%d" % (season_root, season, episode), lang, False, "", country)
        return [item] if item else []

    def find(self, kind: str, title: str, guid: str, filename: str, year: int | None, manual: bool) -> list[tuple[int, str]]:
        """[(score, rating key root)] best first: forced ids in title/filename, external guid, then title search."""
        mode = KINDS[kind]["mode"]
        for text in (title, filename):
            m = FORCED_ID_RE.search(text or "")
            if m:
                root = self._root_from_external(kind, m.group(1).lower(), m.group(2), forced=True)
                if root:
                    return [(100, root)]
        m = GUID_RE.match(guid)
        if m:
            root = self._root_from_external(kind, m.group(1).lower(), m.group(2), forced=False)
            if root:
                return [(100, root)]
        if guid.startswith(self.identifier(kind) + "://"):
            rk = RATING_KEY_RE.match(guid.rsplit("/", 1)[-1])
            if rk:
                return [(100, "%s-%s" % (rk.group(1), rk.group(2)))]
        if not self.wait_ready():
            raise RuntimeError("AniDB title database not loaded yet")
        query = FORCED_ID_RE.sub("", title).strip()
        results, seen = [], set()
        for score, aid, _ in self.titledb.search(query, limit=10 if manual else 5):
            if not manual and score < self.cfg.match_min_score:
                continue
            root = self._root_for_aid(aid, mode)
            if root not in seen:
                seen.add(root)
                results.append((score, root))
        # AniDB unsure -> also ask TMDB (best score wins). TV: below 80, as HAMA did. Movies: unless AniDB has an exact title,
        # because non-anime movies often fuzzy-match an anime one ("The Matrix" ~ "The Animatrix").
        unsure = not results or results[0][0] < (98 if kind == "movies" else 80)
        if (mode == "tvdb" or kind == "movies") and (manual or unsure):
            search = self._tmdb_movie_search if kind == "movies" else self._tmdb_search
            results += [r for r in search(query, year) if r[1] not in seen and (manual or r[0] >= self.cfg.match_min_score)]
            results.sort(key=lambda r: -r[0])
        log.info("search %s '%s' -> %s", kind, query, results[:5])
        return results

    def _drop_adult(self, candidates, fetch: bool):
        """Remove AniDB 18+ (restricted) entries. Automatic matches check the top candidates (AniDB budget applies);
        manual searches only use what's cached. TVDB-mode roots are checked through their primary AniDB entry."""
        kept, checked = [], 0
        for score, root in candidates:
            source, ident = root.split("-", 1)
            aid = ident if source == "anidb" else self.lists.primary_aid_for_tvdb(ident) if source == "tvdb" else ""
            anime = None
            if aid:
                anime = self._anidb_budgeted(aid) if fetch and checked < 3 else self.anidb.anime(aid, fetch=False)
                checked += 1
            if anime and anime.get("restricted"):
                log.info("Skipping adult title %s (library doesn't allow adult content)", root)
                self.events.add("adult skipped", "%s is 18+ on AniDB and the library doesn't allow explicit content "
                                "(Plex: library > Advanced > Allow matching to explicit content)" % root,
                                "https://anidb.net/anime/%s" % aid)
                continue
            kept.append((score, root))
        return kept

    def _root_for_aid(self, aid: str, mode: str) -> str:
        tvdbid = self.lists.get(aid).get("tvdbid", "")
        return "tvdb-%s" % tvdbid if mode == "tvdb" and tvdbid.isdigit() else "anidb-%s" % aid

    def _root_from_external(self, kind: str, source: str, ident: str, forced: bool) -> str:
        """Folder tags pick the numbering ([anidb-x] = AniDB, [tvdb-x] = TVDB seasons); external guids follow the provider mode."""
        mode = KINDS[kind]["mode"]
        if source == "anidb":
            return "anidb-%s" % ident if forced or kind == "movies" else self._root_for_aid(ident, mode)
        self.lists.ready.wait(60)
        if source == "tvdb" and kind != "movies":
            if forced or mode == "tvdb":
                return "tvdb-%s" % ident
            aid = self.lists.primary_aid_for_tvdb(ident)
            return "anidb-%s" % aid if aid else ""
        if source == "tmdb":
            aid = self.lists.by_tmdb.get(ident, "") if kind == "movies" else min(
                self.lists.by_tmdbtv.get(ident, []) or [""], key=lambda a: int(a or 0))
        elif source == "imdb":
            aid = self.lists.by_imdb.get(ident, "")
        else:
            aid = ""
        if not aid and kind == "movies" and source in ("tmdb", "imdb"):  # not anime (or not in Anime-Lists): TMDB movie
            tmdb_id = ident if source == "tmdb" else self.tmdb.find(ident, "imdb_id")[1]
            return "tmdb-%s" % tmdb_id if tmdb_id else ""
        return (("anidb-%s" % aid) if kind == "movies" else self._root_for_aid(aid, mode)) if aid else ""

    def _tmdb_search(self, query: str, year: int | None) -> list[tuple[int, str]]:
        """Shows AniDB doesn't know (TVDB mode only): TMDB TV search -> TVDB id."""
        out = []
        q = cleanse_title(query)
        for tmdb_id, name, first_air in self.tmdb.search_tv(query, year)[:5]:
            score = 99 if cleanse_title(name) == q else int(similarity(q, cleanse_title(name)))
            tvdbid = self.tmdb.tvdb_id(tmdb_id) if score >= 40 else ""
            if tvdbid:
                out.append((score, "tvdb-%s" % tvdbid))
        return out

    def _tmdb_movie_search(self, query: str, year: int | None) -> list[tuple[int, str]]:
        q = cleanse_title(query)
        out = []
        for tmdb_id, name, released in self.tmdb.search_movie(query, year)[:5]:
            score = 99 if cleanse_title(name) == q else int(similarity(q, cleanse_title(name)))
            if year and released[:4].isdigit() and abs(int(released[:4]) - year) > 1:
                score -= 20
            aid = self.lists.by_tmdb.get(tmdb_id, "")  # an anime movie Anime-Lists knows: keep AniDB metadata
            out.append((score, "anidb-%s" % aid if aid else "tmdb-%s" % tmdb_id))
        return out

    def _light(self, kind: str, root: str, score: int, lang: str) -> dict:
        """Manual search result without AniDB API calls."""
        source, ident = root.split("-", 1)
        if source == "tmdb":
            details = self.tmdb.details(True, ident, lang)
            item = self._base(kind, "movie", root, details.get("title") or root)
            item.update({"score": score, "summary": "TMDB %s%s" % (ident, " (%s)" % details["release_date"][:4] if details.get("release_date") else "")})
            return item
        aid = ident if source == "anidb" else self.lists.primary_aid_for_tvdb(ident)
        titles = self.titledb.titles.get(aid, [])
        title, main = pick_title(titles, self._languages(self.cfg.series_language_priority, lang))
        if not title and source == "tvdb":
            title = self.tvdb.series(ident).get("seriesName") or root
        item = self._base(kind, "movie" if kind == "movies" else "show", root, title or main or root)
        item["score"] = score
        item["summary"] = "%s %s%s" % ("AniDB" if source == "anidb" else "TVDB", ident, " - " + main if main else "")
        return item

    # Public metadata endpoints ---------------------------------------------------------------------------------------
    def metadata(self, kind: str, rating_key: str, lang: str, include_children: bool = False,
                 episode_order: str = "", country: str = "") -> dict | None:
        parsed = self._parse(kind, rating_key, episode_order)
        if not parsed:
            return None
        root, season, episode, order = parsed
        if kind == "movies":
            return None if season is not None else self._show_item(kind, self._model(kind, root, lang, "", country), lang, False)
        model = self._model(kind, root, lang, order, country)
        if not model:
            return None
        if season is None:
            return self._show_item(kind, model, lang, include_children)
        if episode is None:
            return self._season_item(kind, model, season, include_children)
        ep = next((e for e in self._episodes(model, season) if e["index"] == episode), None)
        return self._episode_item(kind, model, ep) if ep else None

    def children(self, kind: str, rating_key: str, lang: str, grand: bool = False, episode_order: str = "",
                 country: str = "") -> list[dict] | None:
        parsed = self._parse(kind, rating_key, episode_order)
        if not parsed or kind == "movies" or parsed[2] is not None or (grand and parsed[1] is not None):
            return None
        root, season, _, order = parsed
        model = self._model(kind, root, lang, order, country)
        if not model:
            return None
        if season is None and not grand:
            return [self._season_item(kind, model, s, False) for s in model["seasons"]]
        seasons = model["seasons"] if season is None else [season]
        return [self._episode_item(kind, model, ep) for s in seasons for ep in self._episodes(model, s)]

    def images(self, kind: str, rating_key: str, lang: str, episode_order: str = "", country: str = "") -> list[dict] | None:
        item = self.metadata(kind, rating_key, lang, False, episode_order, country)
        return None if item is None else item.get("Image", [])

    def _parse(self, kind: str, rating_key: str, episode_order: str = ""):
        """-> (root, season, episode, order). Absolute order comes from the key (-a) or the episodeOrder param (TVDB only)."""
        m = RATING_KEY_RE.match(rating_key or "")
        allowed = ("anidb", "tmdb") if kind == "movies" else ("anidb", "tvdb")
        if not m or m.group(1) not in allowed:
            return None
        season = int(m.group(4)) if m.group(4) is not None else None
        episode = int(m.group(5)) if m.group(5) is not None else None
        code = (m.group(3) or _order_code(episode_order)) if m.group(1) == "tvdb" else ""
        return "%s-%s" % (m.group(1), m.group(2)), season, episode, code

    # Series model ----------------------------------------------------------------------------------------------------
    def _model(self, kind: str, root: str, lang: str, order: str = "", country: str = "") -> dict | None:
        """Show-level data for a rating key root, cached briefly (Plex fetches show, seasons and episodes in a burst)."""
        key = (kind, self.profile, root, lang, order, country)
        with self._models_lock:
            hit = self._models.get(key)
            if hit and time.time() - hit[0] < MODEL_TTL:
                return hit[1]
        source, ident = root.split("-", 1)
        with self._models_lock:
            generation = self._generation.get(root, 0)
        if source == "anidb":
            model = self._anidb_model(kind, ident, lang)
        elif source == "tmdb":
            model = self._tmdb_movie_model(kind, ident, lang)
        else:
            model = self._tvdb_model(kind, ident, lang, order)
        if model:
            model["country"] = country
            with self._models_lock:
                if self._generation.get(root, 0) != generation:  # forgotten while building: don't cache stale data
                    return model
                if len(self._models) > 200:
                    self._models.clear()
                self._models[key] = (time.time(), model)
        return model

    def _anidb_model(self, kind: str, aid: str, lang: str) -> dict | None:
        anime = self.anidb.anime(aid)
        titles = anime["titles"] if anime else self.titledb.titles.get(aid)
        if not titles:
            return None
        mapping = self.lists.get(aid)
        tvdbid = mapping.get("tvdbid", "") if mapping.get("tvdbid", "").isdigit() else ""
        model = {"root": "anidb-%s" % aid, "mode": "anidb", "lang": lang, "aid": aid, "anime": anime, "mapping": mapping,
                 "tvdbid": tvdbid, "fields": self._anidb_fields(kind, aid, anime, titles, mapping, lang),
                 "seasons": sorted({e["season"] for e in (anime or {}).get("episodes", [])}), "episodes": {},
                 "order": "", "season_root": "anidb-%s" % aid}
        model["fields"]["Guid"] = self._guids(kind, mapping)
        return model

    def _episode_source(self, tvdbid: str, tmdb_tv: str, lang: str, prefer: str = "tvdb"):
        """(episodes, AniDB entries, episode map, source). TheTVDB unless TMDB order is asked for; falls back to TMDB
        (Anime-Lists tmdbtv/tmdbseason/tmdboffset mapping) when TheTVDB returns nothing, e.g. its legacy API is gone."""
        eps = self.tvdb.episodes(tvdbid, lang or "en") if prefer == "tvdb" and tvdbid else {}
        if eps:
            entries = self.lists.entries_for_tvdb(tvdbid)
            return eps, entries, episode_map(entries, eps, "tvdb"), "tvdb"
        if not tmdb_tv and tvdbid:
            tmdb_tv = self.tmdb.find(tvdbid, "tvdb_id")[0]
        eps = self.tmdb.episodes(tmdb_tv, lang) if tmdb_tv else {}
        if not eps:
            return {}, {}, {}, prefer
        if prefer == "tvdb":
            self.tvdb_fallbacks += 1
            log.warning("TVDB %s: no episodes from TheTVDB, using TheMovieDb %s", tvdbid, tmdb_tv)
            self.events.add("TVDB fallback", "TVDB %s: TheTVDB returned no episodes, used TheMovieDb %s" % (tvdbid, tmdb_tv))
        entries = self.lists.entries_for_tmdbtv(tmdb_tv)
        return eps, entries, episode_map(entries, eps, "tmdb"), "tmdb"

    def _tmdb_movie_model(self, kind: str, tmdb_id: str, lang: str) -> dict | None:
        """Movies AniDB doesn't know: TheMovieDb only (fallback search or [tmdb-x]/[imdb-x] folder tags)."""
        details = self.tmdb.details(True, tmdb_id, lang)
        if not details.get("title"):
            return None
        fields = {"title": details["title"]}
        if details.get("original_title") and details["original_title"] != details["title"]:
            fields["originalTitle"] = details["original_title"]
        for src, dst in (("overview", "summary"), ("tagline", "tagline")):
            if details.get(src):
                fields[dst] = details[src]
        if details.get("release_date"):
            fields["originallyAvailableAt"] = details["release_date"]
            fields["year"] = int(details["release_date"][:4])
        if details.get("runtime"):
            fields["duration"] = int(details["runtime"]) * 60000
        if details.get("genres"):
            fields["Genre"] = [{"tag": g["name"]} for g in details["genres"]]
        studios = [c["name"] for c in details.get("production_companies") or [] if c.get("name")]
        if studios:
            fields["studio"], fields["Studio"] = studios[0], [{"tag": n} for n in studios]
        credits = self.tmdb.credits(True, tmdb_id)
        cast = (credits.get("cast") or [])[:20]
        if cast:
            fields["Role"] = [dict({"tag": c["name"], "role": c.get("character") or "", "order": i + 1},
                                   **({"thumb": TMDB_IMG.format(c["profile_path"])} if c.get("profile_path") else {}))
                              for i, c in enumerate(cast)]
        for field, jobs in (("Director", ("Director",)), ("Writer", ("Screenplay", "Writer", "Novel")), ("Producer", ("Producer",))):
            names = list(dict.fromkeys(c["name"] for c in credits.get("crew") or [] if c.get("job") in jobs))
            if names:
                fields[field] = [{"tag": n} for n in names]
        imdb = details.get("imdb_id") or ""
        fields["Guid"] = [{"id": "tmdb://%s" % tmdb_id}] + ([{"id": "imdb://%s" % imdb}] if imdb.startswith("tt") else [])
        return {"root": "tmdb-%s" % tmdb_id, "mode": "tmdb", "lang": lang, "aid": "", "anime": None, "tvdbid": "",
                "mapping": {"tmdbid": tmdb_id, "imdbid": imdb}, "fields": fields, "seasons": [], "episodes": {}, "order": ""}

    def _tvdb_model(self, kind: str, tvdbid: str, lang: str, order: str = "") -> dict | None:
        tvdb_lang = lang or "en"
        series = self.tvdb.series(tvdbid, tvdb_lang)
        aid = self.lists.primary_aid_for_tvdb(tvdbid)
        tmdb_tv = self.lists.get(aid).get("tmdbtv", "") if aid else ""
        tvdb_eps, entries, ep_map, ep_source = self._episode_source(tvdbid, tmdb_tv if tmdb_tv.isdigit() else "",
                                                                     lang, ORDERS[order]["source"])
        if not series:  # TheTVDB down or unknown: TMDB series details
            tmdb_tv = tmdb_tv if tmdb_tv.isdigit() else self.tmdb.find(tvdbid, "tvdb_id")[0]
            details = self.tmdb.details(False, tmdb_tv, lang) if tmdb_tv else {}
            series = {"seriesName": details.get("name"), "overview": details.get("overview"), "firstAired": details.get("first_air_date"),
                      "network": ((details.get("networks") or [{}])[0]).get("name"),
                      "genre": [g["name"] for g in details.get("genres") or []]} if details else {}
        if not series and not aid:
            return None
        arcs = self.arcs.arcs(tvdbid)
        self.prefetch.add(kind, "tvdb-%s" % tvdbid, sorted(set(entries) | set(self.lists.by_tvdb.get(tvdbid, [])), key=int))
        anime = self._anidb_budgeted(aid) if aid else None
        titles = anime["titles"] if anime else self.titledb.titles.get(aid, [])
        mapping = self.lists.get(aid) if aid else {}
        fields = self._anidb_fields(kind, aid, anime, titles, mapping, lang) if titles else {}
        if not fields.get("title"):
            fields["title"] = series.get("seriesName") or "TVDB %s" % tvdbid
        if series.get("overview"):  # HAMA default: TVDB summary first for TVDB-numbered shows
            fields["summary"] = series["overview"]
        if series.get("firstAired"):
            fields["originallyAvailableAt"] = series["firstAired"]
            fields["year"] = int(series["firstAired"][:4])
        if series.get("network"):
            fields["Network"] = [{"tag": series["network"]}]
        if not fields.get("Genre") and series.get("genre"):
            fields["Genre"] = [{"tag": g} for g in series["genre"]]
        if not fields.get("contentRating") and series.get("rating"):
            fields["contentRating"] = series["rating"]
        if str(series.get("runtime") or "").isdigit() and not fields.get("duration"):
            fields["duration"] = int(series["runtime"]) * 60000
        guids = ["tvdb://%s" % tvdbid] + [g["id"] for g in self._guids(kind, mapping) if not g["id"].startswith("tvdb")]
        if (series.get("imdbId") or "").startswith("tt") and not any(g.startswith("imdb") for g in guids):
            guids.append("imdb://" + series["imdbId"])
        fields["Guid"] = [{"id": g} for g in guids]
        return {"root": "tvdb-%s" % tvdbid, "mode": "tvdb", "lang": lang, "aid": aid, "anime": anime, "mapping": mapping,
                "tvdbid": tvdbid, "fields": fields, "series": series, "tvdb_eps": tvdb_eps, "entries": entries,
                "map": ep_map, "ep_source": ep_source, "episodes": {}, "order": order,
                "season_root": "tvdb-%s%s" % (tvdbid, "-" + order if order else ""),
                "arcs": arcs, "season_names": {s: name for s, _a, _b, name in arcs} if order == "t4" else {},
                "season_types": [code for code in ORDERS if code != "t4" or arcs],
                "seasons": sorted({_ordered(order, s, e, ep, arcs)[0] for (s, e), ep in tvdb_eps.items() if _ordered(order, s, e, ep, arcs)})}

    def _anidb_fields(self, kind: str, aid: str, anime: dict | None, titles, mapping: dict, lang: str) -> dict:
        cfg = self.cfg
        title, main = pick_title(titles, self._languages(cfg.series_language_priority, lang))
        fields = {"title": title}
        original = official_title(titles, cfg.original_title_language) or main
        if original and original != title:
            fields["originalTitle"] = original
        if not anime:
            return fields
        if anime["startdate"]:
            fields["originallyAvailableAt"] = _full_date(anime["startdate"])
            fields["year"] = int(anime["startdate"][:4])
        if anime["description"]:
            fields["summary"] = anime["description"]
        genres, content_rating = genres_and_rating(anime, cfg.genres_add_weights, cfg.genre_minimum_weight)
        if genres:
            fields["Genre"] = [{"tag": g} for g in genres]
        if content_rating:
            fields["contentRating"] = content_rating
        if anime["restricted"]:
            fields["isAdult"] = True
        studio = mapping.get("studio") or next((n for t, n in anime["creators"] if t == "Animation Work"), "") \
            or next((n for t, n in anime["creators"] if t == "Work"), "")
        if studio:
            fields["studio"] = studio
            fields["Studio"] = [{"tag": studio}]
        people = {"Director": ["Direction"], "Producer": ["Series Composition"], "Writer": ["Original Work", "Script", "Screenplay"]}
        for field, roles in people.items():
            names = list(dict.fromkeys(n for t, n in anime["creators"] if t in roles and n))
            if names:
                fields[field] = [{"tag": n} for n in names]
        roles = sorted(anime["characters"], key=lambda c: not c["main"])
        if roles:
            fields["Role"] = [dict({"tag": c["name"], "role": c["role"], "order": i + 1},
                                   **({"thumb": c["photo"]} if c["photo"] else {})) for i, c in enumerate(roles)]
        regular = [e["duration"] for e in anime["episodes"] if e["season"] == 1 and e["duration"]]
        if regular:
            fields["duration"] = int(sum(regular) / len(regular))
        return fields

    def _guids(self, kind: str, mapping: dict) -> list[dict]:
        guids = []
        if mapping.get("tvdbid", "").isdigit():
            guids.append("tvdb://" + mapping["tvdbid"])
        tmdb = mapping.get("tmdbid", "").split(",")[0].strip() if kind == "movies" else mapping.get("tmdbtv", "")
        if tmdb.isdigit():
            guids.append("tmdb://" + tmdb)
        imdb = mapping.get("imdbid", "").split(",")[0].strip()
        if imdb.startswith("tt"):
            guids.append("imdb://" + imdb)
        return [{"id": g} for g in guids]

    # Missing-data report ----------------------------------------------------------------------------------------------
    def _publish_issues(self, model: dict):
        if model.get("order"):  # one report per show: the default (aired) view; other orderings would overwrite it
            return
        links = {}
        if model.get("aid"):
            links["AniDB"] = "https://anidb.net/anime/%s" % model["aid"]
        if model.get("tvdbid"):
            links["TVDB"] = "https://thetvdb.com/?tab=series&id=%s" % model["tvdbid"]
        self.issues.set(model["root"], model["fields"]["title"], links, model.setdefault("issues", {}))

    def _report_show(self, kind: str, model: dict):
        with self._report_lock:  # models are shared by request threads
            self._report_show_locked(kind, model)

    def _report_show_locked(self, kind: str, model: dict):
        issues, fields, aid = model.setdefault("issues", {}), model["fields"], model.get("aid")
        for code in ("no_mapping", "anidb_unavailable", "no_summary", "no_poster", "no_logo"):
            issues.pop(code, None)
        link = ANIME_LISTS_ISSUE.format(aid=aid, title=quote(fields["title"])) if aid else None
        if aid and kind != "movies" and model["mode"] == "anidb" and not model["tvdbid"]:
            issues["no_mapping"] = {"text": "No TVDB id in Anime-Lists: no episode thumbnails, TVDB seasons or franchise collection", "link": link}
        if aid and kind == "movies" and not any(self._external_ids(kind, model)[1:]):
            issues["no_mapping"] = {"text": "No TMDB/IMDb id in Anime-Lists: no logo, background or TMDB collection", "link": link}
        if aid and not model["anime"]:
            issues["anidb_unavailable"] = {"text": "AniDB details not loaded (API error or ban); refresh the item later", "link": None}
        if not fields.get("summary"):
            issues["no_summary"] = {"text": "No summary", "link": "https://anidb.net/anime/%s" % aid if aid else None}
        types = {i["type"] for i in model["images"]}
        if "coverPoster" not in types:
            issues["no_poster"] = {"text": "No poster", "link": None}
        if "clearLogo" not in types:
            tvdb = model["tvdbid"]
            issues["no_logo"] = {"text": "No title logo (clearLogo) on fanart.tv or TheMovieDb",
                                 "link": "https://fanart.tv/series/%s/" % tvdb if tvdb and kind != "movies" else None}
        self._publish_issues(model)

    def _report_episodes(self, model: dict, season: int):
        with self._report_lock:
            self._report_episodes_locked(model, season)

    def _report_episodes_locked(self, model: dict, season: int):
        issues = model.setdefault("issues", {})
        eps = model["episodes"][season]
        for code in ("unmapped_s%d" % season, "no_ep_summary_s%d" % season):
            issues.pop(code, None)
        if model["mode"] == "tvdb" and not model["order"] and season > 0 and model.get("entries"):
            unmapped = [e for (s, e) in model["tvdb_eps"] if s == season and (s, e) not in model["map"]]
            if unmapped:
                issues["unmapped_s%d" % season] = {"text": "%s: TVDB episodes without an AniDB mapping: E%s"
                                                   % (season_title(season), _ranges(unmapped)), "link": None}
        missing = [e["index"] for e in eps if not e["summary"]]
        if missing and season > 0:
            issues["no_ep_summary_s%d" % season] = {"text": "%s: no summary for E%s" % (season_title(season), _ranges(missing)), "link": None}
        if "images" in model:  # show-level checks done: publish (otherwise _report_show publishes)
            self._publish_issues(model)

    # Collections ------------------------------------------------------------------------------------------------------
    def _collection_keys(self, kind: str, model: dict) -> list[str]:
        """Movies: their TMDB collection when they have one. Otherwise the franchise = all AniDB entries of one TVDB
        series (HAMA rule; AniDB-numbered items only, TVDB-mode shows already are the whole franchise)."""
        if kind == "movies":
            _tvdb, tmdb, _imdb = self._external_ids(kind, model)
            coll = (self.tmdb.details(True, tmdb).get("belongs_to_collection") or {}) if tmdb else {}
            if coll.get("id"):
                return ["tmdbset-%s" % coll["id"]]
        if model["mode"] == "anidb" and model["tvdbid"] and len(self.lists.by_tvdb.get(model["tvdbid"], [])) > 1:
            return ["tvdbset-%s" % model["tvdbid"]]
        return []

    def collection(self, kind: str, rating_key: str, lang: str) -> dict | None:
        m = COLLECTION_KEY_RE.match(rating_key or "")
        if not m:
            return None
        source, ident = m.groups()
        item = {"ratingKey": rating_key, "key": "%s/%s" % (COLLECTIONS_PATH, rating_key), "type": "collection",
                "guid": "%s://collection/%s" % (self.identifier(kind), rating_key)}
        if source == "tvdbset":
            aid = self.lists.primary_aid_for_tvdb(ident)
            title, _main = pick_title(self.titledb.titles.get(aid, []), self._languages(self.cfg.series_language_priority, lang))
            title = title or self.tvdb.series(ident).get("seriesName") or ""
            if not title:
                return None
            item["title"] = "%s Collection" % title
            anime = self.anidb.anime(aid, fetch=False) if aid else None
            if anime and anime["description"]:
                item["summary"] = anime["description"]
            art = self.tvdb.images(ident)
            fanart = self.art.fanart(False, [ident])
            poster = order({"AniDB": {"coverPoster": [Image(anime["picture"], "", 0, "AniDB")]} if anime and anime["picture"] else {},
                            "AniList": {"coverPoster": self.anilist.cover(aid)} if aid else {},
                            "TheTVDB": art, "FanartTV": fanart}, "coverPoster", self.cfg.poster_sources,
                           self._image_languages(lang), 1)
            background = order({"TheTVDB": art, "FanartTV": fanart}, "background", self.cfg.background_sources,
                               self._image_languages(lang), 1)
        else:
            data = self.tmdb.collection(ident)
            if not data.get("name"):
                return None
            item["title"] = data["name"]
            if data.get("overview"):
                item["summary"] = data["overview"]
            poster = [Image(TMDB_IMG.format(data["poster_path"]), "", 0, "TheMovieDb")] if data.get("poster_path") else []
            background = [Image(TMDB_IMG.format(data["backdrop_path"]), "", 0, "TheMovieDb")] if data.get("backdrop_path") else []
        if poster:
            item["thumb"] = poster[0].url
        if background:
            item["art"] = background[0].url
        item["Image"] = [{"type": t, "url": i.url, "alt": item["title"]} for t, imgs in (("coverPoster", poster), ("background", background)) for i in imgs]
        return item

    # Episodes --------------------------------------------------------------------------------------------------------
    def _episodes(self, model: dict, season: int) -> list[dict]:
        """Normalised episodes of one season: {season, index, title, summary, airdate, duration, thumb}."""
        if season not in model["episodes"]:
            build = self._anidb_episodes if model["mode"] == "anidb" else self._tvdb_episodes
            model["episodes"][season] = build(model, season)
            self._localize_episodes(model, model["episodes"][season])
            self._report_episodes(model, season)
        return model["episodes"][season]

    def _ep_title(self, titles, lang: str) -> str:
        title, _main = pick_title(titles, self._languages(self.cfg.episode_language_priority, lang))
        return title

    def _anidb_episodes(self, model: dict, season: int) -> list[dict]:
        anime, lang = model["anime"] or {"episodes": []}, model["lang"]
        tvdb_eps, inverse = {}, {}
        tmdb_tv = model["mapping"].get("tmdbtv", "")
        if model["tvdbid"] or tmdb_tv.isdigit():  # TVDB (or TMDB fallback) thumbnails + summaries for AniDB-numbered episodes
            tvdb_eps, _entries, ep_map, _source = self._episode_source(model["tvdbid"], tmdb_tv if tmdb_tv.isdigit() else "", lang)
            for t_key, (aid, a_season, a_ep) in ep_map.items():
                if aid == model["aid"]:
                    inverse.setdefault((a_season, a_ep), t_key)
        out = []
        for e in anime["episodes"]:
            if e["season"] != season:
                continue
            tv = tvdb_eps.get(inverse.get((e["season"], e["index"])), {})
            out.append({"anidb": (model["aid"], e["season"], e["index"]), "src_key": None,
                        "season": season, "index": e["index"], "epno": e["epno"],
                        "title": self._ep_title(e["titles"], lang) or tv.get("episodeName") or "Episode %s" % e["epno"],
                        "summary": e["summary"] or tv.get("overview") or "", "airdate": e["airdate"] or tv.get("firstAired") or "",
                        "duration": e["duration"], "thumb": thumb(tv) if tv else ""})
        return out

    def _tvdb_episodes(self, model: dict, season: int) -> list[dict]:
        lang = model["lang"]

        def anime_for(aid):
            if aid == model["aid"] and model["anime"]:
                return model["anime"]
            return self._anidb_budgeted(aid)

        out = []
        for (s, e), tv in sorted(model["tvdb_eps"].items()):
            position = _ordered(model["order"], s, e, tv, model["arcs"])
            if not position or position[0] != season:
                continue
            ep = {"anidb": model["map"].get((s, e)), "src_key": (s, e),
                  "season": position[0], "index": position[1], "epno": str(position[1]), "title": tv.get("episodeName") or "", "summary": tv.get("overview") or "",
                  "airdate": tv.get("firstAired") or "", "duration": None, "thumb": thumb(tv)}
            mapped = model["map"].get((s, e))
            if mapped:
                aid, a_season, a_ep = mapped
                anidb_ep = next((x for x in (anime_for(aid) or {}).get("episodes", [])
                                 if x["season"] == a_season and x["index"] == a_ep), None)
                if anidb_ep:
                    a_title = self._ep_title(anidb_ep["titles"], lang)
                    if a_title and not GENERIC_EP_TITLE.match(a_title):  # HAMA default: AniDB titles, TVDB summaries
                        ep["title"] = a_title
                    ep["summary"] = ep["summary"] or anidb_ep["summary"]
                    ep["airdate"] = ep["airdate"] or anidb_ep["airdate"]
                    ep["duration"] = anidb_ep["duration"]
            ep["title"] = ep["title"] or "Episode %d" % ep["index"]
            out.append(ep)
        return sorted(out, key=lambda x: x["index"])

    # Plex JSON builders ----------------------------------------------------------------------------------------------
    def _base(self, kind: str, type_: str, rating_key: str, title: str) -> dict:
        key = "%s/%s" % (METADATA_PATH, rating_key)
        return {"ratingKey": rating_key, "key": key + ("/children" if type_ in ("show", "season") else ""),
                "guid": "%s://%s/%s" % (self.identifier(kind), type_, rating_key), "type": type_, "title": title}

    def _show_item(self, kind: str, model: dict | None, lang: str, include_children: bool) -> dict | None:
        if not model:
            return None
        fields = model["fields"]
        item = self._base(kind, "movie" if kind == "movies" else "show", model["root"], fields["title"])
        item.update({k: v for k, v in fields.items() if k != "title" and v})
        if "images" not in model:
            model["images"] = self._show_images(kind, model, lang)
            self._report_show(kind, model)
        if model["images"]:
            item["Image"] = model["images"]
            for plex_type, field in (("coverPoster", "thumb"), ("background", "art")):
                first = next((i["url"] for i in model["images"] if i["type"] == plex_type), None)
                if first:
                    item[field] = first
        model["thumb"] = item.get("thumb", "")
        if model["mode"] == "tvdb":
            item["SeasonType"] = [{k: ORDERS[c][k] for k in ("id", "source", "tag", "title")} for c in model["season_types"]]
        if "extras" not in model:
            model["extras"] = self._show_extras(kind, model)
            if self.cfg.collections:
                refs = [self.collection(kind, key, lang) for key in self._collection_keys(kind, model)]
                refs = [{k: c[k] for k in ("guid", "key", "tag", "summary", "thumb", "art") if c.get(k)}
                        for c in (dict(r, tag=r["title"]) for r in refs if r)]
                if refs:
                    model["extras"]["Collection"] = refs
        item.update(model["extras"])
        if include_children and kind != "movies":
            seasons = [self._season_item(kind, model, s, False) for s in model["seasons"]]
            item["Children"] = {"size": len(seasons), "Metadata": seasons}
        return item

    def _show_images(self, kind: str, model: dict, lang: str) -> list[dict]:
        cfg, movie = self.cfg, kind == "movies"
        tvdb, tmdb, imdb = self._external_ids(kind, model)
        sources = set(cfg.poster_sources + cfg.background_sources + cfg.logo_sources + cfg.season_poster_sources)
        found = {}
        if "FanartTV" in sources:
            found["FanartTV"] = self.art.fanart(movie, [tmdb, imdb] if movie else [tvdb])
        if "TheMovieDb" in sources:
            found["TheMovieDb"] = self.tmdb.images(movie, tmdb)
        if "TheTVDB" in sources and tvdb and not movie:
            found["TheTVDB"] = self.tvdb.images(tvdb)
        anime = model["anime"]
        if anime and anime["picture"]:
            found["AniDB"] = {"coverPoster": [Image(anime["picture"], "", 0, "AniDB")]}
        if model.get("aid") and "AniList" in sources:
            found["AniList"] = {"coverPoster": self.anilist.cover(model["aid"])}
        model["found_images"] = found
        alt, out = model["fields"]["title"], []
        for plex_type, src, langs, lang_first in (
                ("coverPoster", cfg.poster_sources, self._image_languages(lang), False),
                ("background", cfg.background_sources, self._image_languages(lang), False),
                ("clearLogo", cfg.logo_sources, self._logo_languages(lang), True)):
            for img in order(found, plex_type, src, langs, cfg.max_images_per_type, lang_first):
                out.append({"type": plex_type, "url": img.url, "alt": alt})
        log.debug("images %s: tvdb=%s tmdb=%s imdb=%s -> %s", model["root"], tvdb, tmdb, imdb,
                  {t: sum(1 for i in out if i["type"] == t) for t in ("coverPoster", "background", "clearLogo")})
        return out

    def _external_ids(self, kind: str, model: dict) -> tuple[str, str, str]:
        """(tvdb, tmdb, imdb) for a show/movie, resolving TMDB through /find when Anime-Lists has none."""
        if "ids" not in model:
            movie, mapping = kind == "movies", model["mapping"]
            tvdb = model["tvdbid"]
            imdb = mapping.get("imdbid", "").split(",")[0].strip() or (model.get("series") or {}).get("imdbId") or ""
            tmdb = mapping.get("tmdbid", "").split(",")[0].strip() if movie else mapping.get("tmdbtv", "")
            if not tmdb.isdigit():
                tmdb = ""
                if movie and imdb.startswith("tt"):
                    tmdb = self.tmdb.find(imdb, "imdb_id")[1]
                elif not movie and tvdb:
                    tmdb = self.tmdb.find(tvdb, "tvdb_id")[0]
            model["ids"] = (tvdb, tmdb, imdb)
        return model["ids"]

    def _localized(self, lang: str) -> bool:
        return bool(self.cfg.localize_summaries and lang and lang != "en")

    def _localize_episodes(self, model: dict, episodes: list[dict]):
        """Non-English libraries: TMDB episode summaries in the library language, matched through the AniDB entry
        (or directly when the episodes already come from TMDB)."""
        lang = model["lang"]
        if not self._localized(lang) or not episodes:
            return
        _tvdb, tmdb_tv, _imdb = self._external_ids("tv", model)
        if not tmdb_tv:
            return
        loc = self.tmdb.episodes(tmdb_tv, lang)
        if not loc:
            return
        if model.get("ep_source") == "tmdb":
            by_anidb = {}
        else:
            entries = self.lists.entries_for_tmdbtv(tmdb_tv)
            by_anidb = {triple: key for key, triple in episode_map(entries, loc, "tmdb").items()}
        for ep in episodes:
            key = ep["src_key"] if model.get("ep_source") == "tmdb" else by_anidb.get(ep.get("anidb"))
            text = (loc.get(key) or {}).get("overview") if key else ""
            if text:
                ep["summary"] = text

    def _show_extras(self, kind: str, model: dict) -> dict:
        """Theme song (Plex TV themes, by TVDB id), TMDB audience rating, and for the library's language/country:
        localised summary, country certification and production countries."""
        extras = {}
        tvdb, tmdb, _imdb = self._external_ids(kind, model)
        movie = kind == "movies"
        if tmdb:
            details = self.tmdb.details(movie, tmdb)
            countries = [c["name"] for c in details.get("production_countries") or [] if c.get("name")]
            if countries:
                extras["Country"] = [{"tag": c} for c in countries]
            if self._localized(model["lang"]):
                overview = self.tmdb.details(movie, tmdb, model["lang"]).get("overview")
                if overview:
                    extras["summary"] = overview
            if model.get("country") and not model["fields"].get("isAdult"):
                rating = self.tmdb.content_rating(movie, tmdb, model["country"])
                if rating:
                    extras["contentRating"] = rating
        if self.cfg.theme_songs and kind != "movies" and tvdb:
            url = THEME_URL.format(tvdb)
            if self.fetcher.exists(url, "Plex/themes/%s.exists" % tvdb):
                extras["theme"] = url
        if self.cfg.tmdb_rating and tmdb:
            details = self.tmdb.details(kind == "movies", tmdb)
            if details.get("vote_count") and details.get("vote_average"):
                extras["Rating"] = [{"image": "themoviedb://image.rating", "type": "audience",
                                     "value": round(float(details["vote_average"]), 1)}]
        return extras

    def _season_images(self, model: dict, season: int) -> list[dict]:
        """TVDB mode: the AniDB entry that starts this TVDB season, then TVDB/fanart.tv season posters."""
        found = {}
        for aid, entry in model.get("entries", {}).items():
            if entry["defaulttvdbseason"] == str(season) and entry["episodeoffset"] in ("", "0"):
                anime = model["anime"] if aid == model["aid"] else self.anidb.anime(aid, fetch=False)
                if anime and anime["picture"]:
                    found["AniDB"] = {"coverPoster": [Image(anime["picture"], "", 0, "AniDB")]}
                if "AniList" in self.cfg.season_poster_sources:
                    found["AniList"] = {"coverPoster": self.anilist.cover(aid)}
                break
        sources = list(self.cfg.season_poster_sources)
        if model["order"] == "t4":  # arcs aren't TVDB seasons: only the arc posters from the tvdb4 list fit
            found, sources = {"tvdb4": {"coverPoster": self.arcs.posters(model["tvdbid"]).get(season, [])}}, ["tvdb4"]
        else:
            for source in ("TheTVDB", "FanartTV"):
                posters = model.get("found_images", {}).get(source, {}).get("season", {}).get(season)
                if posters:
                    found[source] = {"coverPoster": posters}
        return [{"type": "coverPoster", "url": img.url, "alt": _season_name(model, season)}
                for img in order(found, "coverPoster", sources, self._image_languages(model["lang"]), self.cfg.max_images_per_type)]

    def _season_summary(self, model: dict, season: int) -> str:
        for aid, entry in model.get("entries", {}).items():
            if entry["defaulttvdbseason"] == str(season) and entry["episodeoffset"] in ("", "0"):
                anime = model["anime"] if aid == model["aid"] else self.anidb.anime(aid, fetch=False)
                return (anime or {}).get("description", "")
        return ""

    def _season_item(self, kind: str, model: dict, season: int, include_children: bool) -> dict | None:
        if season not in model["seasons"]:
            return None
        if "thumb" not in model:
            self._show_item(kind, model, model["lang"], False)
        show_rk = model["root"]
        item = self._base(kind, "season", "%s-s%d" % (model["season_root"], season), _season_name(model, season))
        item.update({"index": season, "parentRatingKey": show_rk, "parentKey": "%s/%s" % (METADATA_PATH, show_rk),
                     "parentGuid": "%s://show/%s" % (self.identifier(kind), show_rk), "parentType": "show",
                     "parentTitle": model["fields"]["title"]})
        if model["mode"] == "anidb":
            dates = sorted(e["airdate"] for e in (model["anime"] or {}).get("episodes", []) if e["season"] == season and e["airdate"])
        else:
            dates = sorted(ep.get("firstAired") for (s, e), ep in model["tvdb_eps"].items()
                           if ep.get("firstAired") and (_ordered(model["order"], s, e, ep, model["arcs"]) or (None,))[0] == season)
            summary = self._season_summary(model, season)
            if summary:
                item["summary"] = summary
            images = self._season_images(model, season)
            if images:
                item["Image"] = images
                item["thumb"] = images[0]["url"]
        if dates:
            item["originallyAvailableAt"] = _full_date(dates[0])
            item["year"] = int(dates[0][:4])
        if model["thumb"]:
            item.setdefault("thumb", model["thumb"])
            item["parentThumb"] = model["thumb"]
        if include_children:
            eps = [self._episode_item(kind, model, ep) for ep in self._episodes(model, season)]
            item["Children"] = {"size": len(eps), "Metadata": eps}
        return item

    def _episode_item(self, kind: str, model: dict, ep: dict) -> dict:
        if "thumb" not in model:
            self._show_item(kind, model, model["lang"], False)
        show_rk, season = model["root"], ep["season"]
        season_rk = "%s-s%d" % (model["season_root"], season)
        ident = self.identifier(kind)
        item = self._base(kind, "episode", "%s-e%d" % (season_rk, ep["index"]), ep["title"])
        item.update({"index": ep["index"], "parentIndex": season,
                     "parentRatingKey": season_rk, "parentKey": "%s/%s" % (METADATA_PATH, season_rk),
                     "parentGuid": "%s://season/%s" % (ident, season_rk), "parentType": "season", "parentTitle": _season_name(model, season),
                     "grandparentRatingKey": show_rk, "grandparentKey": "%s/%s" % (METADATA_PATH, show_rk),
                     "grandparentGuid": "%s://show/%s" % (ident, show_rk), "grandparentType": "show",
                     "grandparentTitle": model["fields"]["title"]})
        if ep["airdate"]:
            item["originallyAvailableAt"] = _full_date(ep["airdate"])
            item["year"] = int(ep["airdate"][:4])
        if ep["summary"]:
            item["summary"] = ep["summary"]
        if ep["duration"]:
            item["duration"] = ep["duration"]
        if ep["thumb"]:
            item["thumb"] = ep["thumb"]
            item["Image"] = [{"type": "snapshot", "url": ep["thumb"], "alt": ep["title"]}]
        if model["thumb"]:
            item["grandparentThumb"] = model["thumb"]
        return item


def _ranges(numbers) -> str:
    """[1, 2, 3, 5] -> '1-3, 5'"""
    out, nums = [], sorted(set(numbers))
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in out)


def _order_code(episode_order: str) -> str:
    """episodeOrder from PMS ('tvdbAbsolute' = source + id) or a bare id -> ORDERS code; unknown = TheTVDB aired."""
    value = (episode_order or "").lower()
    for code, o in ORDERS.items():
        if value in ((o["source"] + o["id"]).lower(), o["id"].lower()):
            return code
    return ""


def _season_name(model: dict, season: int) -> str:
    return model.get("season_names", {}).get(season) or season_title(season)


def _ordered(order: str, season: int, episode: int, ep: dict, arcs=()):
    """(season, index) of an episode in the requested order; None if it has no place there. Specials never move.
    absolute: all in season 1 by absolute number (HAMA tvdb5); seasons_absolute: TVDB season, absolute number (tvdb3);
    arcs: story-arc season, absolute number (tvdb4)."""
    kind = ORDERS.get(order, ORDERS[""])["kind"]
    if kind == "aired" or season == 0:
        return season, episode
    absolute = _int(ep.get("absoluteNumber"), 0)
    if not absolute:
        return None
    if kind == "absolute":
        return 1, absolute
    if kind == "seasons_absolute":
        return season, absolute
    arc = arc_of(arcs or (), absolute)
    return (arc, absolute) if arc is not None else None


def _int(value, default: int) -> int:
    """int() that keeps 0 (season 0 = Specials) and falls back on missing values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _full_date(date: str) -> str:
    """AniDB dates may be YYYY or YYYY-MM; Plex wants YYYY-MM-DD."""
    if len(date) == 4:
        return date + "-01-01"
    if len(date) == 7:
        return date + "-01"
    return date
