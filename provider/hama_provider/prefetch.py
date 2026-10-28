"""Background AniDB pre-fetch.

Plex requests only fetch a few uncached AniDB entries each (AniDB's 4 s throttle and ban risk), so long franchises in the
TVDB-seasons provider show TheTVDB titles until a later refresh. This queue fetches the remaining entries slowly in the
background; when all entries of a show are cached its model is dropped and, if the Plex sync is configured, Plex is asked
to refresh that show."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable

from .anidb import HTTP_API_URL

log = logging.getLogger("hama.prefetch")


MAX_ATTEMPTS = 3
GIVE_UP_SECONDS = 86400  # an entry that failed MAX_ATTEMPTS times isn't queued again for a day


class Prefetcher:
    def __init__(self, service, interval: float):
        self.service = service
        self.interval = interval
        self.path = os.path.join(service.cfg.data_dir, "prefetch.json")
        self._lock = threading.Lock()
        self._queue: list[str] = []                       # AniDB ids, oldest first
        self._waiting: dict[str, dict] = {}               # root -> {"kind", "aids", "failed": bool}
        self._attempts: dict[str, int] = {}
        self._given_up: dict[str, float] = {}             # aid -> time given up
        self.on_show_complete: Callable[[str, str], None] | None = None  # (kind, root), e.g. Plex refresh
        self.done = 0
        try:
            with open(self.path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self._queue, self._waiting = saved.get("queue", []), saved.get("waiting", {})
            self._attempts, self._given_up = saved.get("attempts", {}), saved.get("given_up", {})
        except (OSError, ValueError):
            pass

    def __len__(self):
        with self._lock:
            return len(self._queue)

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"queue": self._queue, "waiting": self._waiting, "attempts": self._attempts,
                           "given_up": self._given_up}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save %s: %s", self.path, exc)

    def add(self, kind: str, root: str, aids):
        """Queue the uncached AniDB entries a show needs."""
        if self.interval <= 0:
            return
        now = time.time()
        missing = [a for a in aids if a and str(a).isdigit() and now - self._given_up.get(a, 0) > GIVE_UP_SECONDS
                   and self.service.anidb.anime(a, fetch=False) is None]
        if not missing:
            return
        with self._lock:
            new = [a for a in missing if a not in self._queue]
            self._queue.extend(new)
            waiting = self._waiting.setdefault(root, {"kind": kind, "aids": [], "failed": False})
            waiting["aids"] = sorted(set(waiting["aids"]) | set(missing), key=int)
            if new:
                self._save()
                log.info("Pre-fetch: %s needs %d AniDB entries (queue %d)", root, len(missing), len(self._queue))

    def start(self):
        if self.interval > 0:
            threading.Thread(target=self._run, name="hama-prefetch", daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self.interval)
            try:
                self.step()
            except Exception:
                log.exception("Pre-fetch step failed")

    def step(self) -> str | None:
        """Fetch one queued entry, then complete shows whose entries are all cached (or given up).
        Paused while AniDB bans us or is unreachable; an entry only leaves the queue once cached or after
        MAX_ATTEMPTS failures (then the show completes without a Plex refresh, so it can't loop)."""
        anidb = self.service.anidb
        if time.time() < anidb.banned_until or self.service.fetcher.host_down(HTTP_API_URL):
            return None
        with self._lock:
            aid = self._queue[0] if self._queue else None
        if aid is None:
            return None
        ok = anidb.anime(aid, fetch=False) is not None or anidb.anime(aid) is not None
        with self._lock:
            if self._queue and self._queue[0] == aid:
                self._queue.pop(0)
            if ok:
                self._attempts.pop(aid, None)
                self.done += 1
            else:
                self._attempts[aid] = self._attempts.get(aid, 0) + 1
                if self._attempts[aid] < MAX_ATTEMPTS:
                    self._queue.append(aid)  # retry later, behind the others
                else:
                    log.warning("Pre-fetch: giving up on AniDB %s after %d attempts", aid, MAX_ATTEMPTS)
                    events = getattr(self.service, "events", None)
                    if events:
                        events.add("pre-fetch gave up", "AniDB %s could not be fetched after %d attempts; retried in a day" % (aid, MAX_ATTEMPTS),
                                   "https://anidb.net/anime/%s" % aid)
                    self._attempts.pop(aid, None)
                    self._given_up[aid] = time.time()
                    for info in self._waiting.values():
                        if aid in info["aids"]:
                            info["failed"] = True
            complete = []
            for root, info in list(self._waiting.items()):
                info["aids"] = [a for a in info["aids"] if a in self._queue]
                if not info["aids"]:
                    complete.append((info["kind"], root, info.get("failed", False)))
                    del self._waiting[root]
            cutoff = time.time() - GIVE_UP_SECONDS
            self._given_up = {a: t for a, t in self._given_up.items() if t > cutoff}
            self._save()
        for kind, root, failed in complete:
            self.service.forget(root)
            log.info("Pre-fetch: AniDB entries of %s %s", root, "partly unavailable" if failed else "all cached")
            if self.on_show_complete and not failed:
                try:
                    self.on_show_complete(kind, root)
                except Exception as exc:  # Plex unreachable etc.; the next normal refresh picks it up
                    log.warning("Pre-fetch: Plex refresh of %s failed: %s", root, exc)
        return aid
