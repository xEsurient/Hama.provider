"""AniDB: title database search and HTTP API anime details."""
from __future__ import annotations

import datetime
import json
import os
import difflib
import logging
import string
import threading
import time
import xml.etree.ElementTree as ET

from .net import DAY, Fetcher
from .titles import cleanse_title, summary_sanitizer

log = logging.getLogger("hama.anidb")

TITLES_URL = "https://anidb.net/api/anime-titles.xml.gz"
# The same dump, mirrored by the Anime-Lists project on GitHub: no load on AniDB (which allows the dump once a day per IP)
TITLES_MIRROR_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/animetitles.xml"
HTTP_API_URL = "http://api.anidb.net:9001/httpapi?request=anime&client={client}&clientver={clientver}&protover=1&aid={aid}"
PIC_URL = "https://cdn.anidb.net/images/main/{}"
LANG_ATTR = "{http://www.w3.org/XML/1998/namespace}lang"

FILTER_SEARCH_WORDS = {
    "to", "wa", "ga", "no", "age", "da", "chou", "super", "yo", "de", "chan", "hime", "ni", "sekai",
    "a", "of", "an", "the", "motion", "picture", "special", "oav", "ova", "tv", "eternal", "final", "last", "one",
    "movie", "me", "princess", "theater", "and",
    "le", "la", "un", "les", "nos", "vos", "des", "ses", "world", "in", "another", "this", "story", "life", "name",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv", "xvi"}

# AniDB special episode prefixes -> HAMA season 0 numbering offset
SPECIAL_OFFSETS = {"S": 0, "C": 100, "T": 200, "P": 300, "O": 400}
RESTRICTED_GENRE = {"18 restricted": "X", "pornography": "X", "tv censoring": "TV-MA", "borderline porn": "TV-MA"}
BAN_BACKOFF = 3 * 3600        # first ban: cache only for 3 h; doubles on each further ban (max 24 h), reset by a good answer
BAN_BACKOFF_MAX = 24 * 3600


DIRECT_CACHE, MIRROR_CACHE = "AniDB/anime-titles.xml", "AniDB/anime-titles-mirror.xml"
DIRECT_ATTEMPT = "AniDB/anime-titles-direct.attempt"   # mtime = last direct download attempt (max 1 per DIRECT_MIN_AGE)
DIRECT_MIN_AGE = DAY


class TitleDB:
    """AniDB title list. Default: the Anime-Lists GitHub mirror (no AniDB load). Falls back to AniDB's own dump when the mirror
    is unreachable, or when a search finds nothing (the mirror can lag behind new anime): at most once a day, never during a ban.
    Whichever list (mirror or direct) has more anime is used."""

    def __init__(self, fetcher: Fetcher, source: str = "mirror"):
        self.fetcher = fetcher
        self.source = source  # "mirror" (default, with AniDB fallback) or "anidb" (direct only)
        self.titles: dict[str, list[tuple[str, str, str]]] = {}   # aid -> [(type, lang, text)]
        self.index: list[tuple[str, str, str]] = []              # (cleansed, aid, text)
        self.ready = threading.Event()
        self.direct_allowed = lambda: True  # set by the service: False while AniDB bans us
        self.on_direct = None               # callback(reason, count) for the /report events
        self._lock = threading.Lock()

    @staticmethod
    def _valid(data: bytes) -> bool:
        return b"<animetitles" in data[:500]

    @staticmethod
    def _parse(data: bytes):
        titles, index = {}, []
        for anime in ET.fromstring(data).iter("anime"):
            aid = anime.get("aid")
            entries = [(t.get("type"), t.get(LANG_ATTR), t.text or "") for t in anime.findall("title")]
            titles[aid] = entries
            for _type, _lang, text in entries:
                index.append((cleanse_title(text), aid, text))
        return titles, index

    def load(self):
        candidates = []
        if self.source != "anidb":
            mirror = self.fetcher.get(TITLES_MIRROR_URL, MIRROR_CACHE, ttl=DAY, validate=self._valid)
            if mirror:
                candidates.append(("mirror", mirror))
        direct = self.fetcher.read_cache(DIRECT_CACHE)  # an earlier direct download (fallback or source=anidb)
        if self.source == "anidb" or not candidates:
            fresh = self._fetch_direct("mirror unavailable" if self.source != "anidb" else "")
            direct = fresh or direct
        if direct and self._valid(direct):
            candidates.append(("AniDB", direct))
        if not candidates:
            raise RuntimeError("AniDB title database unavailable (no mirror, no AniDB download, no cache)")
        best = None
        for name, data in candidates:
            try:
                parsed = self._parse(data)
            except ET.ParseError as exc:
                log.warning("AniDB title list from %s unreadable: %s", name, exc)
                continue
            if best is None or len(parsed[0]) > len(best[1][0]):
                best = (name, parsed)
        if best is None:
            raise RuntimeError("AniDB title database unreadable")
        with self._lock:
            self.titles, self.index = best[1]
        self.ready.set()
        log.info("AniDB titles loaded from %s: %d anime, %d titles", best[0], len(self.titles), len(self.index))

    def _fetch_direct(self, reason: str) -> bytes | None:
        """AniDB's own dump, at most once per DIRECT_MIN_AGE (counted across restarts) and never during a ban."""
        attempt = self.fetcher.read_cache(DIRECT_ATTEMPT, DIRECT_MIN_AGE)
        if attempt is not None or not self.direct_allowed():
            return None
        try:
            self.fetcher.write_cache(DIRECT_ATTEMPT, str(int(time.time())).encode())
        except OSError:
            return None  # can't record the attempt: don't risk repeated downloads
        data = self.fetcher.get(TITLES_URL, DIRECT_CACHE, ttl=0, validate=self._valid)
        if data and reason:
            log.info("AniDB title list downloaded directly (%s)", reason)
        return data

    def refresh_from_anidb(self, reason: str) -> bool:
        """Search found nothing: maybe the mirror lacks a new anime. Try AniDB's own dump (guarded); True if more anime now."""
        if self.source == "anidb":
            return False
        data = self._fetch_direct(reason)
        if not data:
            return False
        try:
            titles, index = self._parse(data)
        except ET.ParseError:
            return False
        with self._lock:
            if len(titles) <= len(self.titles):
                return False
            added = len(titles) - len(self.titles)
            self.titles, self.index = titles, index
        log.info("AniDB title list refreshed from AniDB: %d more anime than the mirror", added)
        if self.on_direct:
            self.on_direct(reason, added)
        return True

    def search(self, query: str, limit: int = 10) -> list[tuple[int, str, str]]:
        """Return [(score, aid, matched title)] best first. Port of HAMA AniDB.Search scoring."""
        query = query.strip()
        if not query:
            return []
        q_lower, q_clean = query.lower(), cleanse_title(query)
        words = [w for w in q_clean.split() if w not in FILTER_SEARCH_WORDS and len(w) > 3] or q_clean.split()
        best: dict[str, tuple[int, str]] = {}

        def offer(score, aid, text):
            if score > best.get(aid, (-1, ""))[0]:
                best[aid] = (score, text)

        for clean, aid, text in self.index:
            if text == query:
                offer(100, aid, text)
            elif text.lower() == q_lower:
                offer(99, aid, text)
            elif clean == q_clean:
                offer(98, aid, text)
            elif words and any(w in clean for w in words):
                offer(int(similarity(q_clean, clean)), aid, text)
        ranked = sorted(((s, aid, t) for aid, (s, t) in best.items()), key=lambda r: (-r[0], int(r[1])))
        return ranked[:limit]


def similarity(query: str, title: str) -> float:
    """max(edit-similarity, HAMA WordsScore), 0-97 so exact matches always win."""
    if not query or not title:
        return 0
    ratio = difflib.SequenceMatcher(None, query, title).ratio() * 100
    max_len = max(len(query.replace(" ", "")), len(title))
    words = 0.0
    for word in query.split():
        m = difflib.SequenceMatcher(None, word, title).find_longest_match(0, len(word), 0, len(title))
        words += 100.0 * m.size / max_len
    return min(97, max(ratio, words))


class AniDB:
    def __init__(self, fetcher: Fetcher, client: str, clientver: str, state_path: str = ""):
        self.fetcher = fetcher
        self.client, self.clientver = client, clientver
        self.banned_until = 0.0
        self.ban_strikes = 0
        self.state_path = state_path  # ban state survives restarts: requests during a ban extend it
        self._load_state()
        self.on_ban = None  # callback(), e.g. the /report events
        self._memo: dict[str, tuple[float, dict]] = {}
        self._memo_lock = threading.Lock()

    def anime(self, aid: str, fetch: bool = True) -> dict | None:
        """Parsed AniDB anime XML (cached on disk; refresh age depends on how long ago the show ended).
        fetch=False only uses the disk cache (avoids long AniDB throttle chains inside one Plex request)."""
        with self._memo_lock:
            hit = self._memo.get(aid)
            if hit and time.time() - hit[0] < 600:
                return hit[1]
        cache_name = "AniDB/xml/%s.xml" % aid
        cached = self.fetcher.read_cache(cache_name)
        ttl = 6 * DAY
        if cached:
            try:
                ttl = _cache_ttl(ET.fromstring(cached).findtext("enddate") or "")
            except ET.ParseError:
                cached = None
        if time.time() < self.banned_until or (not fetch and cached):
            data = cached
        elif not fetch:
            return None
        else:
            data = self.fetcher.get(HTTP_API_URL.format(client=self.client, clientver=self.clientver, aid=aid),
                                    cache_name, ttl=ttl, throttle=("anidb", 4.0), validate=self._validate)
        if not data:
            return None
        try:
            parsed = parse_anime(ET.fromstring(data))
        except ET.ParseError as exc:
            log.warning("AniDB %s: bad XML: %s", aid, exc)
            return None
        with self._memo_lock:
            if len(self._memo) > 500:
                self._memo.clear()
            self._memo[aid] = (time.time(), parsed)
        return parsed

    def _validate(self, data: bytes) -> bool:
        head = data[:300].decode("utf-8", "replace")
        if "<error" in head:
            if "banned" in head.lower():
                self.ban_strikes += 1
                backoff = min(BAN_BACKOFF * 2 ** (self.ban_strikes - 1), BAN_BACKOFF_MAX)
                self.banned_until = time.time() + backoff
                self._save_state()
                log.error("AniDB API ban hit (#%d), using cache only for %d hours", self.ban_strikes, backoff // 3600)
                if self.on_ban:
                    self.on_ban()
            else:
                log.warning("AniDB API error: %s", head.strip())
            return False
        if "<anime" in head and self.ban_strikes:
            self.ban_strikes = 0  # a good answer again: next ban starts at the short back-off
            self._save_state()
        return "<anime" in head

    def _load_state(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            self.banned_until, self.ban_strikes = float(state.get("banned_until", 0)), int(state.get("strikes", 0))
            if self.banned_until > time.time():
                log.warning("AniDB ban from before the restart still active until %s: cache only",
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(self.banned_until)))
        except (OSError, ValueError):
            pass

    def _save_state(self):
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"banned_until": self.banned_until, "strikes": self.ban_strikes}, fh)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("Could not save AniDB ban state: %s", exc)


def _cache_ttl(enddate: str) -> float:
    """HAMA rule: ended >5y ago -> 1 year, ended >30 days ago -> scaled, otherwise 6 days."""
    try:
        if len(enddate) == 4:
            end = datetime.date(int(enddate), 12, 31)
        elif len(enddate) == 7:
            end = datetime.date(int(enddate[:4]), int(enddate[5:7]), 28)
        else:
            end = datetime.date.fromisoformat(enddate)
    except ValueError:
        return 6 * DAY
    days_old = (datetime.date.today() - end).days
    if days_old > 1825:
        return 365 * DAY
    if days_old > 30:
        return max(6 * DAY, days_old * 365 * DAY / 1825)
    return 6 * DAY


def parse_anime(root: ET.Element) -> dict:
    def text(node, path):
        return (node.findtext(path) or "").strip()

    anime = {
        "aid": root.get("id"),
        "restricted": root.get("restricted") == "true",
        "type": text(root, "type"),
        "episodecount": text(root, "episodecount"),
        "startdate": text(root, "startdate"),
        "enddate": text(root, "enddate"),
        "titles": [(t.get("type"), t.get(LANG_ATTR), t.text or "") for t in root.findall("titles/title")],
        "description": summary_sanitizer(text(root, "description")),
        "picture": PIC_URL.format(text(root, "picture")) if text(root, "picture") else "",
        "rating": text(root, "ratings/permanent"),
        "tags": [(text(t, "name"), int(t.get("weight") or 0), t.get("infobox") == "true") for t in root.findall("tags/tag")],
        "creators": [(c.get("type"), (c.text or "").strip()) for c in root.findall("creators/name")],
        "characters": [],
        "episodes": [],
        "resources": {},
    }
    for char in root.findall("characters/character"):
        if char.get("type") not in ("main character in", "secondary cast in"):
            continue
        seiyuu = char.find("seiyuu")
        if seiyuu is None or not (seiyuu.text or "").strip() or not text(char, "name"):
            continue
        anime["characters"].append({
            "main": char.get("type") == "main character in",
            "role": text(char, "name"),
            "name": seiyuu.text.strip(),
            "photo": PIC_URL.format(seiyuu.get("picture")) if seiyuu.get("picture") else ""})
    for res in root.findall("resources/resource"):
        ids = [text(e, "identifier") for e in res.findall("externalentity") if text(e, "identifier")]
        anime["resources"].setdefault(res.get("type"), []).extend(ids)

    # Episodes, numbered like HAMA/ASS: regular -> season 1, specials -> season 0 (S=n, C=100+n, endings 150+, T=200+n, P=300+n, O=400+n)
    raw = []
    for ep in root.findall("episodes/episode"):
        epno = ep.find("epno")
        if epno is None or not (epno.text or "").strip():
            continue
        num, ep_type = epno.text.strip(), epno.get("type")
        raw.append((int(ep_type or 0), int(num if num.isdigit() else (num[1:] or 0)), num, ep))
    raw.sort(key=lambda r: (r[0], r[1]))
    ending_offset = 99
    for ep_type, n, num, ep in raw:
        titles = [(None, t.get(LANG_ATTR), t.text or "") for t in ep.findall("title")]
        first_title = titles[0][2] if titles else ""
        if ep_type == 3 and first_title.startswith("Ending") and n - 1 < ending_offset:
            ending_offset = n - 1
        if ep_type == 1:
            season, index = 1, n
        elif ep_type == 3 and n > ending_offset:
            season, index = 0, n + 150 - ending_offset
        elif num[:1] in SPECIAL_OFFSETS:
            season, index = 0, SPECIAL_OFFSETS[num[:1]] + n
        else:
            continue
        length = text(ep, "length")
        anime["episodes"].append({
            "season": season, "index": index, "epno": num, "titles": titles,
            "airdate": text(ep, "airdate"), "summary": summary_sanitizer(text(ep, "summary")),
            "duration": int(length) * 60000 if length.isdigit() else None, "rating": text(ep, "rating")})
    return anime


def genres_and_rating(anime: dict, add_weights: bool, min_weight: int) -> tuple[list[str], str]:
    """Genres from AniDB tags (HAMA rules) and a content rating from restricted tags."""
    genres, content_rating = set(), ""
    for name, weight, infobox in anime["tags"]:
        if infobox or (add_weights and weight >= min_weight):
            genres.add(string.capwords(name, "-"))
            content_rating = RESTRICTED_GENRE.get(name.lower(), content_rating)
    return sorted(genres), content_rating
