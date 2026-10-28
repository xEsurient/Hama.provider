"""fanart.tv artwork (posters, backgrounds, clear logos, season posters) and image ordering."""
from __future__ import annotations

import logging
from collections import namedtuple

from .net import DAY, Fetcher

log = logging.getLogger("hama.images")

FANART_TV_URL = "https://webservice.fanart.tv/v3/tv/{id}?api_key={key}"
FANART_MOVIE_URL = "https://webservice.fanart.tv/v3/movies/{id}?api_key={key}"

# Plex Image type -> fanart.tv keys (best first)
FANART_TV_TYPES = {"clearLogo": ["hdtvlogo", "clearlogo"], "coverPoster": ["tvposter"],
                   "background": ["showbackground", "show4kbackground"]}
FANART_MOVIE_TYPES = {"clearLogo": ["hdmovielogo", "movielogo"], "coverPoster": ["movieposter"],
                      "background": ["moviebackground", "movie4kbackground"]}


Image = namedtuple("Image", "url lang score source")  # lang "" = textless/unknown


class Artwork:
    """fanart.tv artwork."""
    def __init__(self, fetcher: Fetcher, fanart_key: str):
        self.fetcher = fetcher
        self.fanart_key = fanart_key

    # fanart.tv ---------------------------------------------------------------------------------------------------
    def fanart(self, movie: bool, ids: list[str]) -> dict[str, list[Image]]:
        """ids: TVDB id for TV; TMDB or IMDb ids for movies (first that answers wins)."""
        if not self.fanart_key:
            return {}
        for id_ in ids:
            if not id_:
                continue
            kind = "movies" if movie else "tv"
            url = (FANART_MOVIE_URL if movie else FANART_TV_URL).format(id=id_, key=self.fanart_key)
            data = self.fetcher.get_json(url, "FanartTV/%s/%s.json" % (kind, id_), ttl=7 * DAY)
            if not isinstance(data, dict) or "status" in data and data.get("status") == "error":
                continue
            out = {"season": {}}
            for item in data.get("seasonposter") or [] if not movie else []:
                if str(item.get("season", "")).isdigit():
                    lang = "" if item.get("lang") in ("00", None, "") else item["lang"]
                    out["season"].setdefault(int(item["season"]), []).append(
                        Image(item["url"], lang, int(item.get("likes") or 0), "FanartTV"))
            for plex_type, keys in (FANART_MOVIE_TYPES if movie else FANART_TV_TYPES).items():
                for rank, key in enumerate(keys):
                    for item in data.get(key) or []:
                        lang = "" if item.get("lang") in ("00", None, "") else item["lang"]
                        likes = int(item.get("likes") or 0)
                        out.setdefault(plex_type, []).append(Image(item["url"], lang, likes - rank * 1000, "FanartTV"))
            return out
        return {}


def order(images: dict[str, dict[str, list[Image]]], plex_type: str, sources: list[str],
          languages: list[str], limit: int, language_first: bool = False) -> list[Image]:
    """Flatten {source: {type: [Image]}} for one type, best first.
    Sorted by source priority then language priority (language first when language_first, used for logos),
    then score. Images in an unlisted language come after listed ones."""
    langs = [l.lower() for l in languages]
    pool = []
    for s_rank, source in enumerate(sources):
        for img in images.get(source, {}).get(plex_type, []):
            l_rank = langs.index(img.lang.lower()) if img.lang.lower() in langs else len(langs)
            pool.append(((l_rank, s_rank) if language_first else (s_rank, l_rank), -img.score, img))
    pool.sort(key=lambda r: r[:2])
    seen, out = set(), []
    for *_, img in pool:
        if img.url not in seen:
            seen.add(img.url)
            out.append(img)
    return out[:limit]
