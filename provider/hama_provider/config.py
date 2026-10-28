"""Settings: built-in defaults < config.json < HAMA_* environment variables."""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("hama.config")

PROVIDER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The API keys/client below are the public shared ones shipped with upstream HAMA (Hama.bundle).
# They are rate-limited and shared by every HAMA user; set your own in config.json or env to avoid that.
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 32550,
    "data_dir": os.path.join(PROVIDER_DIR, "data"),
    "log_level": "INFO",
    "log_file": "",

    # API keys / clients (upstream HAMA shared defaults)
    "anidb_client": "hama",
    "anidb_clientver": "1",
    "fanart_api_key": "cfa9dc054d221b8d107f8411cd20b13f",
    "tmdb_api_key": "7f4a0bd0bd3315bb832e17feda70b5cd",
    "tvdb_api_key": "A27AD9BE0DA63333",

    # Titles (same meaning as HAMA's SerieLanguagePriority / EpisodeLanguagePriority)
    "series_language_priority": ["main", "en", "ja"],
    "episode_language_priority": ["main", "en", "ja"],
    "use_request_language": False,          # put the Plex library language first in both lists above
    "original_title_language": "ja",
    "localize_summaries": True,             # non-English libraries: show/episode summaries from TMDB in the library language        # originalTitle = official title in this language, else AniDB main title

    # Images; source order = preference order. Sources: AniList, AniDB, TheTVDB, FanartTV, TheMovieDb
    # (AniList/AniDB covers are per AniDB entry; AniList is the high-resolution one)
    "poster_sources": ["AniList", "AniDB", "TheTVDB", "FanartTV", "TheMovieDb"],
    "background_sources": ["FanartTV", "TheTVDB", "TheMovieDb"],
    "season_poster_sources": ["AniList", "AniDB", "TheTVDB", "FanartTV"],  # TVDB-seasons provider
    "logo_sources": ["FanartTV", "TheMovieDb"],
    "logo_language_priority": ["en", "ja", ""],  # "" = textless/unknown language
    "max_images_per_type": 10,
    "cache_max_age_days": 90,               # prune cached API answers not refreshed for this long (AniDB XML is kept)

    # Extras
    "theme_songs": True,                    # Plex's TV theme library (tvthemes.plexapp.com, by TVDB id)
    "tmdb_rating": True,
    "collections": True,                    # franchise collections (TVDB series groups) and TMDB movie collections

    # Collection sync: PMS ignores collections sent by custom providers, so HAMA can add them through the Plex API.
    # Off until plex_url is set. Token: Plex Web > any item > Get Info > View XML, the X-Plex-Token in the URL.
    "plex_url": "",                         # e.g. http://127.0.0.1:32400
    "plex_token": "",
    "collection_sync_hours": 12,
    "collection_min_items": 2,
    "rescue_unmatched": True,
    "experimental_markers": False,          # advertise a 'markers' feature and log what PMS asks (research only)               # with plex_url: re-match items Plex left unmatched (e.g. provider was down)              # only create collections with at least this many items in the library            # 0 = only when run by hand (python3 run.py --sync-collections)                    # TMDB audience rating with Plex's TMDB badge (AniDB has no Plex badge)

    # Background AniDB pre-fetch for TVDB-seasons shows (seconds between AniDB requests; 0 = off)
    "anidb_prefetch_seconds": 30,

    # Extra providers with their own settings, e.g. {"tv-en": {"base": "tv", "title": "HAMA – Anime (English titles)",
    # "series_language_priority": ["en", "main"]}} -> add http://host:32550/tv-en in Plex (Plex passes no provider settings)
    "profiles": {},

    # Matching / content
    "match_min_score": 50,
    "genres_add_weights": False,            # HAMA AnidbGenresAddWeights
    "genre_minimum_weight": 400,            # HAMA MinimumWeight

    # Paging: PMS 1.43 sends X-Plex-Container-Start=0 for the first item (Plex's example code assumes 1)
    "container_start_base": 0,
}

def coerce(key, value):
    default = DEFAULTS[key]
    if isinstance(default, dict):
        return value if isinstance(value, dict) else json.loads(value or "{}")
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, list):
        return value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
    return str(value)


class Config(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def load(path: str | None = None) -> Config:
    cfg = Config(DEFAULTS)
    candidates = [path, os.environ.get("HAMA_CONFIG"),
                  os.path.join(os.environ.get("HAMA_DATA_DIR", DEFAULTS["data_dir"]), "config.json"),
                  os.path.join(PROVIDER_DIR, "config.json")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                if key not in DEFAULTS:
                    log.warning("config.json: unknown setting '%s' ignored", key)
                    continue
                cfg[key] = coerce(key, value)
            cfg["_config_file"] = candidate
            break
    for key in DEFAULTS:
        env = os.environ.get("HAMA_" + key.upper())
        if env is not None and env != "":
            cfg[key] = coerce(key, env)
    return cfg
