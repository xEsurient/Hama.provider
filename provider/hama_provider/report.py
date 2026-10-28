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

    def set(self, root: str, title: str, links: dict, issues: dict[str, dict]):
        """issues: {code: {"text": str, "link": str|None}}; empty = show is clean (removed from the report).
        Stores a copy: callers keep mutating their own dict from other request threads."""
        issues, links = copy.deepcopy(issues), dict(links)
        with self._lock:
            if issues:
                old = self._data.get(root)
                if old and (old["title"], old["links"], old["issues"]) == (title, links, issues):
                    return  # unchanged: don't rewrite the file
                self._data[root] = {"title": title, "links": links, "updated": int(time.time()), "issues": issues}
                if len(self._data) > MAX_SHOWS:
                    for old in sorted(self._data, key=lambda r: self._data[r]["updated"])[:len(self._data) - MAX_SHOWS]:
                        del self._data[old]
            elif self._data.pop(root, None) is None:
                return
            self._save()

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

    def add(self, kind: str, text: str, link: str | None = None):
        with self._lock:
            if self._items and self._items[0]["kind"] == kind and self._items[0]["text"] == text:
                self._items[0]["count"] = self._items[0].get("count", 1) + 1  # collapse repeats
                self._items[0]["time"] = int(time.time())
            else:
                self._items.insert(0, {"time": int(time.time()), "kind": kind, "text": text, "link": link, "count": 1})
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
