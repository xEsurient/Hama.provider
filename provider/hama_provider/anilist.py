"""AniList cover art (high-resolution, one per AniDB entry). Ids via the arm.haglund.dev mapping server, as upstream HAMA."""
from __future__ import annotations

import json
import logging

from .images import Image
from .net import DAY, Fetcher

log = logging.getLogger("hama.anilist")

ARM_URL = "https://arm.haglund.dev/api/v2/ids?source=anidb&include=anilist&id={aid}"
GRAPHQL_URL = "https://graphql.anilist.co"
QUERY = "query($id:Int){Media(id:$id,type:ANIME){coverImage{extraLarge} bannerImage}}"


class AniList:
    def __init__(self, fetcher: Fetcher, enabled: bool = True):
        self.fetcher = fetcher
        self.enabled = enabled

    def anilist_id(self, aid: str) -> str:
        data = self.fetcher.get_json(ARM_URL.format(aid=aid), "AniList/arm/%s.json" % aid, ttl=30 * DAY, throttle=("arm", 0.5))
        return str((data or {}).get("anilist") or "")

    def cover(self, aid: str) -> list[Image]:
        """[coverImage.extraLarge] for an AniDB id, or []."""
        if not self.enabled or not aid:
            return []
        al_id = self.anilist_id(aid)
        if not al_id:
            return []
        cache = "AniList/media/%s.json" % al_id
        raw = self.fetcher.read_cache(cache, 14 * DAY)
        if raw is None:
            if self.fetcher.host_down(GRAPHQL_URL):
                raw = self.fetcher.read_cache(cache)
            else:
                self.fetcher._wait("anilist", 2.0)  # AniList: ~90/min nominal, often throttled to ~30/min
                data = self.fetcher.post_json(GRAPHQL_URL, {"query": QUERY, "variables": {"id": int(al_id)}})
                if data and (data.get("data") or {}).get("Media"):
                    raw = json.dumps(data).encode("utf-8")
                    self.fetcher.write_cache(cache, raw)
                else:
                    raw = self.fetcher.read_cache(cache)
        try:
            media = (json.loads(raw.decode("utf-8")).get("data") or {}).get("Media") or {} if raw else {}
        except ValueError:
            media = {}
        url = (media.get("coverImage") or {}).get("extraLarge")
        return [Image(url, "", 0, "AniList")] if url else []
