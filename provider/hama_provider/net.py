"""HTTP fetching with an on-disk cache and per-host throttling."""
from __future__ import annotations

import gzip
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlsplit

log = logging.getLogger("hama.net")

USER_AGENT = "HAMA-Provider/1.0 (Plex custom metadata provider; +https://github.com/xesurient/hama.provider)"
DAY = 86400
HOST_DOWN_SECONDS = 600  # after a connection failure/timeout, skip that host (use cache) for this long
LOCK_STRIPES = 256
# A body that arrived broken (truncated gzip, short read): treated like a rejected body, the host itself is fine
BODY_ERRORS = (ValueError, EOFError, zlib.error, http.client.HTTPException)


class Fetcher:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self._locks = [threading.RLock() for _ in range(LOCK_STRIPES)]  # striped: bounded, same name -> same lock
        self._throttle_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._throttle_guard = threading.Lock()
        self.down_until: dict[str, float] = {}
        self.on_host_down = None  # callback(host, exc), e.g. the /report events

    def _lock(self, name: str) -> threading.Lock:
        return self._locks[hash(name) % LOCK_STRIPES]

    def host_down(self, url: str) -> bool:
        return self.down_until.get(urlsplit(url).netloc, 0) > time.time()

    def hosts_down(self) -> list[str]:
        now = time.time()
        return sorted(h for h, t in list(self.down_until.items()) if t > now)

    def _mark_failure(self, url: str, exc: Exception):
        """Connection errors/timeouts take the host out of rotation for a while, so an unreachable API costs one timeout
        instead of one per request. HTTP 429 (too many requests) pauses the host for its Retry-After (default 60 s)."""
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            try:
                pause = max(5, min(int(exc.headers.get("Retry-After") or 60), 3600))
            except (TypeError, ValueError):
                pause = 60
            host = urlsplit(url).netloc
            if self.down_until.get(host, 0) <= time.time():
                log.warning("%s: too many requests, pausing it for %d s (cache used meanwhile)", host, pause)
            self.down_until[host] = time.time() + pause
            return
        if isinstance(exc, (urllib.error.HTTPError,) + BODY_ERRORS):  # the host answered (HTTP error / bad body)
            return
        host = urlsplit(url).netloc
        if self.down_until.get(host, 0) <= time.time():
            log.warning("%s unreachable (%s); using cache only for %d min", host, exc, HOST_DOWN_SECONDS // 60)
            if self.on_host_down:
                self.on_host_down(host, exc)
        self.down_until[host] = time.time() + HOST_DOWN_SECONDS

    def _wait(self, key: str, interval: float):
        """Per-source pacing. Throttle keys are few and fixed (anidb, tvdb, …), so they get their own locks:
        sharing the cache-name stripes could deadlock (a fetch holds its cache stripe while waiting here)."""
        with self._throttle_guard:
            lock = self._throttle_locks.setdefault(key, threading.Lock())
        with lock:
            wait = self._last.get(key, 0) + interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last[key] = time.time()

    def cache_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, *name.split("/"))

    def read_cache(self, name: str, ttl: float | None = None) -> bytes | None:
        """Cached bytes if present (and younger than ttl when given)."""
        path = self.cache_path(name)
        try:
            if ttl is not None and time.time() - os.path.getmtime(path) > ttl:
                return None
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def write_cache(self, name: str, data: bytes):
        path = self.cache_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, threading.get_ident())
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def prune(self, max_age_days: float, keep=("AniDB/xml", "AniDB/anime-titles", "AnimeLists", "TheTVDB")) -> int:
        """Delete cache files not refreshed for max_age_days. Never pruned: AniDB XML (strict rate limits) and TheTVDB
        (if its legacy API disappears, the cache is the only source of TVDB numbering)."""
        if max_age_days <= 0 or not os.path.isdir(self.cache_dir):
            return 0
        cutoff, removed = time.time() - max_age_days * DAY, 0
        keep_paths = tuple(os.path.join(self.cache_dir, *k.split("/")) for k in keep)
        for folder, _dirs, files in os.walk(self.cache_dir):
            for name in files:
                path = os.path.join(folder, name)
                if path.startswith(keep_paths):
                    continue
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        return removed

    def size(self) -> tuple[int, int]:
        """(files, bytes) in the cache."""
        files = total = 0
        for folder, _dirs, names in os.walk(self.cache_dir):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(folder, name))
                    files += 1
                except OSError:
                    pass
        return files, total

    def get(self, url: str, cache_name: str | None, ttl: float, throttle: tuple[str, float] | None = None,
            headers: dict | None = None, validate=None, timeout: float = 30) -> bytes | None:
        """Return the body of url, from cache when fresh. Falls back to a stale cache on errors.
        validate(data) -> bool rejects bad bodies (e.g. AniDB error XML) so they are never cached."""
        with self._lock(cache_name or url):
            if cache_name:
                data = self.read_cache(cache_name, ttl)
                if data is not None:
                    return data
            if self.host_down(url):
                return self.read_cache(cache_name) if cache_name else None
            try:
                if throttle:
                    self._wait(*throttle)
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                                                           **(headers or {})})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                if validate and not validate(data):
                    raise ValueError("rejected response: %r" % data[:200])
                if cache_name:
                    try:
                        self.write_cache(cache_name, data)
                    except OSError as exc:
                        log.warning("Cache write failed for %s: %s", cache_name, exc)
                log.debug("GET %s -> %d bytes", _redact(url), len(data))
                return data
            except (urllib.error.URLError, OSError) + BODY_ERRORS as exc:
                self._mark_failure(url, exc)
                code = getattr(exc, "code", None)
                (log.debug if code == 404 else log.warning)("GET %s failed: %s", _redact(url), exc)
                return self.read_cache(cache_name) if cache_name else None

    def exists(self, url: str, cache_name: str, ttl: float = 30 * DAY) -> bool:
        """HEAD check with a cached yes/no (theme songs etc.)."""
        cached = self.read_cache(cache_name, ttl)
        if cached is not None:
            return cached == b"1"
        if self.host_down(url):
            return False
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            ok = False
            if exc.code >= 500:
                return False  # don't cache server errors
        except (urllib.error.URLError, OSError) + BODY_ERRORS as exc:
            self._mark_failure(url, exc)
            return False
        try:
            self.write_cache(cache_name, b"1" if ok else b"0")
        except OSError:
            pass
        return ok

    def get_json(self, url: str, cache_name: str | None, ttl: float, headers: dict | None = None, **kw):
        data = self.get(url, cache_name, ttl, headers={"Accept": "application/json", **(headers or {})}, **kw)
        return _json(data)

    def post_json(self, url: str, payload: dict, timeout: float = 30):
        """Uncached JSON POST (logins)."""
        if self.host_down(url):
            return None
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json(resp.read())
        except (urllib.error.URLError, OSError) + BODY_ERRORS as exc:
            self._mark_failure(url, exc)
            (log.debug if getattr(exc, "code", None) == 429 else log.warning)("POST %s failed: %s", url, exc)
            return None


def _json(data: bytes | None):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return None


def _redact(url: str) -> str:
    """Hide api keys in logs."""
    return re.sub(r"(api_key=)[^&]+", r"\1***", url)
