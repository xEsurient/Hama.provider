"""Missing-data report (HAMA's old HTML error reports): problems found while building shows, per rating-key root."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time

log = logging.getLogger("hama.report")

MAX_SHOWS = 3000
ANIME_LISTS_ISSUE = ("https://github.com/Anime-Lists/anime-lists/issues/new?template=new_mapping.yml"
                     "&title=%5BHAMA%5D%20AniDB%20ID%3A{aid}&anidb_id={aid}&anime_title={title}")


class Issues:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "issues.json")
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, ValueError):
            pass

    def set(self, root: str, title: str, links: dict, issues: dict[str, dict], kind: str = "tv"):
        """issues: {code: {"text": str, "link": str|None}}; empty = show is clean (removed from the report).
        Stores a copy: callers keep mutating their own dict from other request threads."""
        issues, links = copy.deepcopy(issues), dict(links)
        with self._lock:
            if issues:
                old = self._data.get(root)
                if old and (old["title"], old["links"], old["issues"], old.get("kind", "tv")) == (title, links, issues, kind):
                    return  # unchanged: don't rewrite the file
                self._data[root] = {"title": title, "links": links, "updated": int(time.time()), "issues": issues, "kind": kind}
                if len(self._data) > MAX_SHOWS:
                    for old in sorted(self._data, key=lambda r: self._data[r]["updated"])[:len(self._data) - MAX_SHOWS]:
                        del self._data[old]
            elif self._data.pop(root, None) is None:
                return
            self._save()

    def prune(self, keep: set[str], grace: float = 6 * 3600) -> int:
        """Remove entries whose root isn't in keep (items gone from Plex). Entries written within grace seconds stay:
        a show being added may not be listed by Plex yet. Returns the number removed."""
        now = time.time()
        with self._lock:
            gone = [r for r, e in self._data.items() if r not in keep and now - e.get("updated", 0) > grace]
            for root in gone:
                del self._data[root]
            if gone:
                self._save()
        return len(gone)

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save %s: %s", self.path, exc)

    def all(self) -> list[tuple[str, dict]]:
        with self._lock:
            return sorted(copy.deepcopy(self._data).items(), key=lambda kv: kv[1]["title"].lower())

    def count(self) -> int:
        with self._lock:
            return len(self._data)


MAX_EVENTS = 200


class Events:
    """Recent notable events for the /report page (newest first, kept across restarts): things that otherwise only
    show in the log - adult titles skipped, unmatched items re-matched, unhandled Plex requests, sources down, …"""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "events.json")
        self._lock = threading.Lock()
        self._items: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._items = json.load(fh)[:MAX_EVENTS]
        except (OSError, ValueError):
            pass

    def add(self, kind: str, text: str, link: str | None = None, merge_within: float = 0):
        """Repeats of the newest event are collapsed (count). merge_within (seconds): also fold a repeat into an older
        identical event that recent, moved to the top (for things every refresh logs again, e.g. TVDB fallbacks)."""
        with self._lock:
            now = int(time.time())
            same = next((i for i, e in enumerate(self._items) if e["kind"] == kind and e["text"] == text
                         and (i == 0 or (merge_within > 0 and now - e["time"] <= merge_within))), None)
            if same is not None:
                item = self._items.pop(same)
                item["count"] = item.get("count", 1) + 1
                item["time"] = now
                self._items.insert(0, item)
            else:
                self._items.insert(0, {"time": now, "kind": kind, "text": text, "link": link, "count": 1})
                del self._items[MAX_EVENTS:]
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._items, fh, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("Could not save %s: %s", self.path, exc)

    def all(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._items)
