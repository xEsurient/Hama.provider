"""Collection sync through the Plex HTTP API.

PMS asks custom providers for metadata with excludeElements=Collection, so collections returned by a provider are dropped.
This pushes HAMA's collections into Plex directly: for every item matched by a HAMA provider, add the collection tags
(keeping the user's own), then give each collection its poster, background and summary.
Opt-in: needs plex_url (and plex_token unless Plex allows token-less local access)."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from .service import KINDS, Service


def schemes(service: Service) -> dict[str, tuple[str, str]]:
    """GUID scheme / agent identifier -> (base kind, profile)."""
    out = {info["identifier"]: (kind, "") for kind, info in KINDS.items()}
    out.update({p["identifier"]: (p["base"], name) for name, p in service.profiles.items()})
    return out

log = logging.getLogger("hama.plexsync")
PLEX_TYPES = {"movie": 1, "show": 2, "collection": 18}


class PlexSync:
    def __init__(self, service: Service):
        self.service = service
        self.cfg = service.cfg
        self.base = self.cfg.plex_url.rstrip("/")

    def _request(self, method: str, path: str, params: dict | None = None):
        url = self.base + path + ("?" + urlencode(params, quote_via=quote) if params else "")
        headers = {"Accept": "application/json"}
        if self.cfg.plex_token:
            headers["X-Plex-Token"] = self.cfg.plex_token
        req = urllib.request.Request(url, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8")) if body.strip().startswith(b"{") else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params).get("MediaContainer", {})

    def run_once(self) -> dict:
        """Sync every HAMA library. Returns counts for logging/tests."""
        stats = {"items": 0, "tagged": 0, "collections": 0}
        if not self.service.wait_ready(300):
            log.warning("Collection sync skipped: data not loaded")
            return stats
        try:
            sections = self._get("/library/sections").get("Directory", [])
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                log.error("Collection sync: Plex refused access (401). Set plex_token; Plex only allows token-less access from its own machine.")
                return stats
            raise
        known = schemes(self.service)
        self.service.begin_request()  # one AniDB budget for the whole sync run (ban risk on large libraries)
        for section in sections:
            found = known.get(section.get("agent", ""))
            if found:
                self._sync_section(section, found, stats)
        log.info("Collection sync done: %(items)d items checked, %(tagged)d updated, %(collections)d collections styled", stats)
        self.service.last_sync = dict(stats, time=int(time.time()))
        return stats

    def _sync_section(self, section: dict, found: tuple[str, str], stats: dict):
        kind, profile = found
        sec_id, plex_type = section["key"], PLEX_TYPES["movie" if kind == "movies" else "show"]
        known = schemes(self.service)
        wanted_by_collection: dict[str, dict] = {}
        touched: set[str] = set()
        plan = []  # (item, [collection dicts])
        for item in self._get("/library/sections/%s/all" % sec_id, {"type": plex_type}).get("Metadata", []):
            guid = item.get("guid", "")
            scheme, _, rest = guid.partition("://")
            if known.get(scheme) != found:
                continue
            stats["items"] += 1
            self.service.select_profile(profile)
            rating_key = rest.rsplit("/", 1)[-1]
            source, _, ident = rating_key.partition("-")
            model = self.service._model(kind, "%s-%s" % (source, ident), "")
            if not model:
                continue
            collections = [c for c in (self.service.collection(kind, key, "") for key in self.service._collection_keys(kind, model)) if c]
            if collections:
                plan.append((item, collections))
        sizes: dict[str, int] = {}
        for _item, collections in plan:
            for c in collections:
                sizes[c["title"]] = sizes.get(c["title"], 0) + 1
                wanted_by_collection[c["title"]] = c
        for item, collections in plan:
            collections = [c for c in collections if sizes[c["title"]] >= self.cfg.collection_min_items]
            if not collections:
                continue
            detail = self._get("/library/metadata/%s" % item["ratingKey"]).get("Metadata", [{}])[0]
            existing = [c["tag"] for c in detail.get("Collection", [])]
            missing = [c["title"] for c in collections if c["title"] not in existing]
            if not missing:
                continue
            params = {"type": plex_type, "id": item["ratingKey"], "collection.locked": 1}
            for i, tag in enumerate(existing + missing):
                params["collection[%d].tag.tag" % i] = tag
            self._request("PUT", "/library/sections/%s/all" % sec_id, params)
            touched.update(missing)
            stats["tagged"] += 1
            log.info("Collections %s: %s += %s", section.get("title"), item.get("title"), missing)
        if touched:
            self._style_collections(sec_id, {t: wanted_by_collection[t] for t in touched}, stats)

    def _style_collections(self, sec_id: str, wanted: dict[str, dict], stats: dict):
        """Poster, background and summary for collections that just got members; fields the user locked are left alone."""
        for coll in self._get("/library/sections/%s/collections" % sec_id).get("Metadata", []):
            meta = wanted.get(coll.get("title"))
            if not meta:
                continue
            locked = {f.get("name") for f in coll.get("Field", []) if f.get("locked")}
            if meta.get("summary") and "summary" not in locked and not coll.get("summary"):
                self._request("PUT", "/library/sections/%s/all" % sec_id,
                              {"type": PLEX_TYPES["collection"], "id": coll["ratingKey"], "summary.value": meta["summary"]})
            if meta.get("thumb") and "thumb" not in locked:
                self._request("POST", "/library/metadata/%s/posters" % coll["ratingKey"], {"url": meta["thumb"]})
            if meta.get("art") and "art" not in locked:
                self._request("POST", "/library/metadata/%s/arts" % coll["ratingKey"], {"url": meta["art"]})
            stats["collections"] += 1

    def refresh_show(self, kind: str, root: str):
        """Ask Plex to refresh every item matched to this rating-key root (used when background pre-fetch completes)."""
        known = schemes(self.service)
        type_ = "movie" if kind == "movies" else "show"
        guids = {"%s://%s/%s" % (scheme, type_, root) for scheme, (base, _p) in known.items() if base == kind}
        for section in self._get("/library/sections").get("Directory", []):
            if known.get(section.get("agent", ""), ("",))[0] != kind:
                continue
            for item in self._get("/library/sections/%s/all" % section["key"], {"type": PLEX_TYPES[type_]}).get("Metadata", []):
                if item.get("guid") in guids:
                    self._request("PUT", "/library/metadata/%s/refresh" % item["ratingKey"])
                    log.info("Asked Plex to refresh %s (%s)", item.get("title"), root)

    def rescue_unmatched(self, limit: int = 50) -> int:
        """Ask Plex to refresh items in HAMA libraries that it left unmatched (e.g. scanned while the provider was down:
        Plex keeps them as local://… and never retries on its own). Returns the number of items asked for."""
        known = schemes(self.service)
        asked = 0
        for section in self._get("/library/sections").get("Directory", []):
            if section.get("agent") == "com.plexapp.agents.hama":
                self.service.events.add("legacy HAMA", "Library '%s' still uses the legacy HAMA agent. It queries AniDB from this IP "
                                        "too, which adds to the AniDB rate limit; switch it to a HAMA provider agent (see MIGRATING.md)"
                                        % section.get("title"))
            found = known.get(section.get("agent", ""))
            if not found:
                continue
            plex_type = PLEX_TYPES["movie" if found[0] == "movies" else "show"]
            for item in self._get("/library/sections/%s/all" % section["key"], {"type": plex_type}).get("Metadata", []):
                guid = item.get("guid", "")
                if guid.startswith(("local://", "com.plexapp.agents.none://")) or not guid:
                    self._request("PUT", "/library/metadata/%s/refresh" % item["ratingKey"])
                    log.info("Asked Plex to re-match unmatched item '%s' in %s", item.get("title"), section.get("title"))
                    self.service.events.add("unmatched", "'%s' in library '%s' was unmatched in Plex; asked Plex to match it again. If it stays "
                                            "unmatched, use Fix Match or a folder tag [anidb-…]" % (item.get("title"), section.get("title")))
                    asked += 1
                    if asked >= limit:
                        break
            if asked >= limit:
                break
        self.service.last_rescue = {"time": int(time.time()), "asked": asked}
        return asked

    def start_schedule(self):
        hours = self.cfg.collection_sync_hours
        if self.base:
            self.service.prefetch.on_show_complete = self.refresh_show
            if self.cfg.rescue_unmatched:
                threading.Thread(target=self._rescue_loop, name="hama-rescue", daemon=True).start()
        if not self.base or hours <= 0:
            return

        def loop():
            time.sleep(120)  # let PMS finish its own startup scan first
            while True:
                try:
                    self.run_once()
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    log.warning("Collection sync failed: %s", exc)
                except Exception:  # keep the schedule alive whatever happens
                    log.exception("Collection sync crashed")
                time.sleep(hours * 3600)

        threading.Thread(target=loop, name="hama-collection-sync", daemon=True).start()
        log.info("Collection sync every %s h against %s", hours, self.base)

    def _rescue_loop(self):
        time.sleep(120)  # after the provider (and Plex) are up
        while True:
            try:
                if self.service.wait_ready(300):
                    self.rescue_unmatched()
            except Exception:
                log.exception("Unmatched rescue failed")
            time.sleep(6 * 3600)
