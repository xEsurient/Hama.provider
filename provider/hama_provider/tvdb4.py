"""Story arcs as seasons (HAMA 'tvdb4' mode), from the Absolute Series Scanner project's community lists."""
from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET

from .images import Image
from .net import DAY, Fetcher

log = logging.getLogger("hama.tvdb4")

MAPPING_URL = "https://raw.githubusercontent.com/ZeroQI/Absolute-Series-Scanner/master/tvdb4.mapping.xml"
POSTERS_URL = "https://raw.githubusercontent.com/ZeroQI/Absolute-Series-Scanner/master/tvdb4.posters.xml"
OPEN_END = 10 ** 6
RELOAD_SECONDS = 7 * DAY


class StoryArcs:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self._arcs: dict[str, list[tuple[int, int, int, str]]] = {}
        self._posters: dict[str, dict[int, list[Image]]] = {}
        self._loaded = 0.0
        self._lock = threading.Lock()

    def _ensure(self):
        """(Re)load weekly. A failed or corrupt download keeps the previous lists instead of breaking every request."""
        with self._lock:
            if time.time() - self._loaded < RELOAD_SECONDS:
                return
            self._loaded = time.time()
            try:
                self._load()
            except ET.ParseError as exc:
                log.warning("tvdb4 lists unreadable, keeping the previous ones: %s", exc)
            if not self._arcs:  # nothing usable yet (offline, corrupt): try again in an hour, not a week
                self._loaded = time.time() - RELOAD_SECONDS + 3600

    def _load(self):
        arcs, posters = {}, {}
        data = self.fetcher.get(MAPPING_URL, "tvdb4/tvdb4.mapping.xml", ttl=7 * DAY, validate=lambda d: b"<tvdb4entries" in d[:300])
        if data:
            for node in ET.fromstring(data).iter("anime"):
                rows = []
                for line in (node.text or "").strip().splitlines():
                    parts = [p.strip() for p in line.split("|", 3)]
                    if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
                        start, end = int(parts[1]), int(parts[2])
                        if "(unknown length)" in parts[3] or start == end:  # ongoing last arc
                            end = OPEN_END
                        rows.append((int(parts[0]), start, end, parts[3].replace("(unknown length)", "").strip(" -—")))
                if rows:
                    arcs[node.get("tvdbid")] = rows
        data = self.fetcher.get(POSTERS_URL, "tvdb4/tvdb4.posters.xml", ttl=7 * DAY, validate=lambda d: b"<tvdb4entries" in d[:300])
        if data:
            for node in ET.fromstring(data).iter("posters"):
                for line in (node.text or "").strip().splitlines():
                    season, _, url = line.strip().partition("|")
                    if season.strip().isdigit() and url.startswith("http"):
                        posters.setdefault(node.get("tvdbid"), {}).setdefault(int(season), []).append(Image(url.strip(), "", 0, "tvdb4"))
        if arcs:
            self._arcs, self._posters = arcs, posters or self._posters
        log.info("tvdb4 story arcs loaded for %d series", len(self._arcs))

    def arcs(self, tvdbid: str) -> list[tuple[int, int, int, str]]:
        """[(season, first absolute ep, last absolute ep, arc name)]"""
        self._ensure()
        return self._arcs.get(tvdbid, [])

    def posters(self, tvdbid: str) -> dict[int, list[Image]]:
        self._ensure()
        return self._posters.get(tvdbid, {})


def arc_of(arcs, absolute: int):
    for season, start, end, _name in arcs:
        if start <= absolute <= end:
            return season
    return None
