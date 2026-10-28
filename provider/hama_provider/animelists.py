"""Anime-Lists (ScudLee) AniDB <-> TVDB/TMDB/IMDb mapping."""
from __future__ import annotations

import logging
import os
import threading
import xml.etree.ElementTree as ET

from .net import DAY, Fetcher

log = logging.getLogger("hama.animelists")

MASTER_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
CUSTOM_FILE = "anime-list-custom.xml"  # optional local overrides in the data dir, same format as the master list


class AnimeLists:
    def __init__(self, fetcher: Fetcher, data_dir: str):
        self.fetcher = fetcher
        self.data_dir = data_dir
        self.by_aid: dict[str, dict] = {}
        self.by_tvdb: dict[str, list[str]] = {}
        self.by_tmdb: dict[str, str] = {}
        self.by_tmdbtv: dict[str, list[str]] = {}
        self.by_imdb: dict[str, str] = {}
        self.ready = threading.Event()

    def load(self):
        data = self.fetcher.get(MASTER_URL, "AnimeLists/anime-list-master.xml", ttl=DAY,
                                validate=lambda d: b"<anime-list" in d[:500])
        if not data:
            raise RuntimeError("anime-list-master.xml unavailable (no download and no cache)")
        entries = {}
        for node in ET.fromstring(data).iter("anime"):
            if (node.get("anidbid") or "").isdigit():
                entries[node.get("anidbid")] = _entry(node)
        custom = os.path.join(self.data_dir, CUSTOM_FILE)
        if os.path.isfile(custom):
            try:
                nodes = list(ET.parse(custom).getroot().iter("anime"))
                for node in nodes:
                    if (node.get("anidbid") or "").isdigit():  # skip broken custom entries
                        entries[node.get("anidbid")] = _entry(node)
                log.info("Custom mapping loaded: %d entries from %s", len(nodes), custom)
            except ET.ParseError as exc:
                log.error("Bad %s: %s", custom, exc)
        by_tvdb, by_tmdb, by_tmdbtv, by_imdb = {}, {}, {}, {}
        for aid, e in entries.items():
            if e["tvdbid"].isdigit():
                by_tvdb.setdefault(e["tvdbid"], []).append(aid)
            if e["tmdbtv"].isdigit():
                by_tmdbtv.setdefault(e["tmdbtv"], []).append(aid)
            for tmdb in e["tmdbid"].split(","):
                if tmdb.strip().isdigit():
                    by_tmdb.setdefault(tmdb.strip(), aid)
            for imdb in e["imdbid"].split(","):
                if imdb.strip().startswith("tt"):
                    by_imdb.setdefault(imdb.strip(), aid)
        self.by_aid, self.by_tvdb, self.by_tmdb, self.by_tmdbtv, self.by_imdb = entries, by_tvdb, by_tmdb, by_tmdbtv, by_imdb
        self.ready.set()
        log.info("Anime-Lists loaded: %d AniDB entries", len(entries))

    def get(self, aid: str) -> dict:
        return self.by_aid.get(aid, {})

    def entries_for_tvdb(self, tvdbid: str) -> dict[str, dict]:
        by_aid = self.by_aid  # a daily reload may swap the dicts mid-request
        return {aid: by_aid[aid] for aid in self.by_tvdb.get(tvdbid, []) if aid in by_aid}

    def entries_for_tmdbtv(self, tmdb_id: str) -> dict[str, dict]:
        by_aid = self.by_aid
        return {aid: by_aid[aid] for aid in self.by_tmdbtv.get(tmdb_id, []) if aid in by_aid}

    def primary_aid_for_tvdb(self, tvdbid: str) -> str:
        """The AniDB entry that starts a TVDB series (season 1 or absolute, no offset), else the lowest regular season."""
        by_aid = self.by_aid  # a daily reload may swap the dicts mid-request
        aids = [a for a in self.by_tvdb.get(tvdbid, []) if a in by_aid]

        def key(aid):
            e = by_aid[aid]
            season = "1" if e["defaulttvdbseason"] == "a" else e["defaulttvdbseason"]  # 'a' = absolute, starts the series
            return (0 if season == "1" and e["episodeoffset"] in ("", "0") else 1,
                    int(season) if season.isdigit() and season != "0" else 99, int(aid))
        return min(aids, key=key) if aids else ""


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Anime-Lists attribute names per numbering: (entry default season, entry offset, <mapping> line season)
MAP_FIELDS = {"tvdb": ("defaulttvdbseason", "episodeoffset", "tvdbseason"), "tmdb": ("tmdbseason", "tmdboffset", "tmdbseason")}


def episode_map(entries: dict[str, dict], episodes: dict[tuple[int, int], dict],
                source: str = "tvdb") -> dict[tuple[int, int], tuple[str, int, int]]:
    """{(season, episode): (aid, anidb season, anidb episode)} for all AniDB entries of one TVDB (or TMDB) series.
    Port of HAMA AnimeLists rules: explicit <mapping> lines (start/end/offset ranges and ;anidb-target; pairs) win,
    then each entry's default season + offset (the entry with the largest offset below the episode wins,
    'a' = absolute numbering), then specials 1:1 when only one AniDB entry maps to the series.
    Lines are used only if they carry this source's season attribute (Anime-Lists mixes TVDB and TMDB lines)."""
    season_field, offset_field, line_field = MAP_FIELDS[source]
    lines = {aid: [m for m in e.get("mappings", []) if m.get(line_field) is not None] for aid, e in entries.items()}
    result: dict[tuple[int, int], tuple[str, int, int]] = {}
    explicit_anidb = set()
    has_special_mapping = False
    for aid in sorted(entries, key=int):
        for m in lines[aid]:
            a_season, t_season, offset = _int(m["anidbseason"], 1), _int(m[line_field], 1), _int(m["offset"])
            has_special_mapping |= a_season == 0 or t_season == 0
            if m["start"]:
                for ep in range(_int(m["start"]), _int(m["end"], _int(m["start"])) + 1):
                    result.setdefault((t_season, ep + offset), (aid, a_season, ep))
                    explicit_anidb.add((aid, a_season, ep))
            for pair in filter(None, (m["text"] or "").split(";")):
                a_ep, _, t_eps = pair.partition("-")
                if not a_ep.strip().isdigit():
                    continue
                explicit_anidb.add((aid, a_season, int(a_ep)))
                for t_ep in t_eps.split("+"):
                    if _int(t_ep) > 0:
                        result.setdefault((t_season, _int(t_ep)), (aid, a_season, int(a_ep)))

    by_season: dict[str, list[tuple[int, str]]] = {}
    for aid, e in entries.items():
        default = e.get(season_field) or ""
        if default.isdigit() or default == "a":
            by_season.setdefault(default, []).append((_int(e.get(offset_field)), aid))
    for candidates in by_season.values():
        candidates.sort(reverse=True)
    for (t_season, t_ep), ep in episodes.items():
        if (t_season, t_ep) in result:
            continue
        for season_key, number in ((str(t_season), t_ep), ("a", _int(ep.get("absoluteNumber")))):
            if not number or season_key not in by_season or (season_key == "a" and t_season == 0):
                continue
            for offset, aid in by_season[season_key]:
                if season_key == "0" and (lines[aid] or number - offset != 1):
                    continue  # specials-season entries (movies/OVAs): only their first episode, unless explicitly mapped
                if number > offset and (aid, 1, number - offset) not in explicit_anidb:
                    result[(t_season, t_ep)] = (aid, 1, number - offset)
                    break
            if (t_season, t_ep) in result:
                break

    if len(entries) == 1 and not has_special_mapping:
        aid = next(iter(entries))
        for t_season, t_ep in episodes:
            if t_season == 0:
                result.setdefault((0, t_ep), (aid, 0, t_ep))
    return result


def _entry(node: ET.Element) -> dict:
    return {
        "tvdbid": node.get("tvdbid") or "",
        "defaulttvdbseason": node.get("defaulttvdbseason") or "",
        "episodeoffset": node.get("episodeoffset") or "",
        "tmdbtv": node.get("tmdbtv") or "",
        "tmdbseason": node.get("tmdbseason") or "",
        "tmdboffset": node.get("tmdboffset") or "",
        "tmdbid": node.get("tmdbid") or "",
        "imdbid": node.get("imdbid") or "",
        "name": (node.findtext("name") or "").strip(),
        "studio": (node.findtext("supplemental-info/studio") or "").strip(),
        "mappings": [{"anidbseason": m.get("anidbseason"), "tvdbseason": m.get("tvdbseason"), "tmdbseason": m.get("tmdbseason"), "start": m.get("start"),
                      "end": m.get("end"), "offset": m.get("offset"), "text": m.text}
                     for m in node.findall("mapping-list/mapping")],
    }
