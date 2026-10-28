"""HTTP server exposing the HAMA TV and Movie providers at /tv and /movies."""
from __future__ import annotations

import json
import logging
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .pages import report_page, status_page
from .service import METADATA_PATH, Service

log = logging.getLogger("hama.server")

TOKEN_RE = re.compile(r"(X-Plex-Token=)[^&\s]+", re.IGNORECASE)
# /<provider or profile>/...: the first segment is resolved by Service.resolve() (built-in kinds + configured profiles)
COLLECTION_ROUTE_RE = re.compile(r"^/([a-z0-9-]+)/library/collections/([A-Za-z0-9_-]+)(?:/(children))?/?$")
ROUTE_RE = re.compile(r"^/([a-z0-9-]+)(?:%s(?:/([A-Za-z0-9_-]+)(?:/(children|grandchildren|images|extras))?)?)?/?$" % METADATA_PATH)


class Handler(BaseHTTPRequestHandler):
    server_version = "HAMA-Provider"
    service: Service = None  # set by serve()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), TOKEN_RE.sub(r"\1***", fmt % args))

    # Helpers ---------------------------------------------------------------------------------------------------------
    def _param(self, name: str, default: str = "") -> str:
        """Plex sends these either as headers or query parameters."""
        value = self.headers.get(name)
        if value is None:
            value = self._query.get(name, [default])[0]
        return value

    def _lang(self) -> str:
        return (self._param("X-Plex-Language") or "").split("-")[0].lower()

    def _country(self) -> str:
        """X-Plex-Country, else the region of X-Plex-Language (en-US -> US)."""
        country = self._param("X-Plex-Country") or (self._param("X-Plex-Language") or "").partition("-")[2]
        return country[:2].upper()

    def _send(self, code: int, payload: dict | None):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _container(self, kind: str, key: str, items: list, offset: int = 0, total: int | None = None) -> dict:
        return {"MediaContainer": {"offset": offset, "totalSize": len(items) if total is None else total,
                                   "identifier": self.service.identifier(kind), "size": len(items), key: items}}

    def _paged(self, kind: str, items: list) -> dict:
        base = self.service.cfg.container_start_base
        try:
            size = int(self._param("X-Plex-Container-Size", "20"))
            start = int(self._param("X-Plex-Container-Start", str(base)))
        except ValueError:
            size, start = 20, base
        offset = max(0, start - base)
        page = items[offset:offset + max(0, size)]
        return self._container(kind, "Metadata", page, offset, len(items))

    def _unhandled(self, method: str):
        """Requests under a provider path (/tv/…, /tvdb/…, /movies/…, profiles) come from Plex: worth an event on /report
        (e.g. a feature Plex tries that HAMA doesn't serve). Anything else (browsers, scanners) only goes to the debug log."""
        path = urlsplit(self.path).path
        first = path.strip("/").split("/", 1)[0]
        if self.service.resolve(first):
            log.info("Unhandled %s %s", method, TOKEN_RE.sub(r"\1***", self.path))
            self.service.events.add("unhandled request", "Plex %s %s, which HAMA doesn't serve"
                                    % ("asked for" if method == "GET" else "sent POST", path))
        else:
            log.debug("Unknown %s %s", method, TOKEN_RE.sub(r"\1***", self.path))

    def _route(self, regex=ROUTE_RE):
        """Match the path; returns (base kind, *groups) with the profile selected for this request, or None."""
        parts = urlsplit(self.path)
        self._query = parse_qs(parts.query)
        m = regex.match(parts.path)
        resolved = self.service.resolve(m.group(1)) if m else None
        if not resolved:
            return None
        self.service.begin_request(resolved[1])
        return (resolved[0],) + m.groups()[1:]

    # Verbs -----------------------------------------------------------------------------------------------------------
    def do_GET(self):
        self.service.begin_request()
        try:
            path = urlsplit(self.path).path
            if path in ("", "/"):
                status = self.service.status()
                if "text/html" in self.headers.get("Accept", ""):
                    return self._send_html(status_page(status, "http://%s" % (self.headers.get("Host") or "127.0.0.1:32550")))
                return self._send(200, status)
            if path.rstrip("/") == "/report":
                return self._send_html(report_page(self.service.issues.all(), self.service.events.all()))
            if path.rstrip("/") == "/health":  # for Docker / uptime monitors: 200 when ready, 503 while loading
                ready = self.service.titledb.ready.is_set() and self.service.lists.ready.is_set()
                return self._send(200 if ready else 503, {"status": "ok" if ready else "starting",
                                                          "version": self.service.status_version()})
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            m = self._route()
            if not m:
                c = self._route(COLLECTION_ROUTE_RE)
                if c:
                    kind, key, children = c
                    log.info("GET %s collection %s%s", kind, key, "/children" if children else "")
                    if children:  # members aren't enumerable from the provider side; Plex groups matched items itself
                        return self._send(200, self._paged(kind, []))
                    item = self.service.collection(kind, key, self._lang())
                    return self._send(404, None) if item is None else self._send(200, self._container(kind, "Metadata", [item]))
                self._unhandled("GET")
                return self._send(404, {"error": "not found"})
            kind, rating_key, sub = m
            if not rating_key:
                return self._send(200, self.service.provider(kind))
            if self.service.base_cfg.experimental_markers and any("marker" in k.lower() for k in self._query):
                self.service.events.add("markers request", "Plex asked for %s with %s"
                                        % (urlsplit(self.path).path, ", ".join(sorted(self._query))))
            lang = self._lang()
            order = self._query.get("episodeOrder", [""])[0]
            log.info("GET %s %s%s%s", kind, rating_key, "/" + sub if sub else "", " order=" + order if order else "")
            if sub in ("children", "grandchildren"):
                items = self.service.children(kind, rating_key, lang, grand=sub == "grandchildren", episode_order=order,
                                              country=self._country())
                return self._send(404, None) if items is None else self._send(200, self._paged(kind, items))
            if sub == "extras":  # trailers etc.: none from AniDB
                return self._send(200, self._paged(kind, []))
            if sub == "images":
                images = self.service.images(kind, rating_key, lang, order, self._country())
                return self._send(404, None) if images is None else self._send(200, self._container(kind, "Image", images))
            include_children = self._query.get("includeChildren", ["0"])[0] == "1"
            item = self.service.metadata(kind, rating_key, lang, include_children, order, self._country())
            return self._send(404, None) if item is None else self._send(200, self._container(kind, "Metadata", [item]))
        except Exception as exc:
            log.error("GET %s failed: %s\n%s", TOKEN_RE.sub(r"\1***", self.path), exc, traceback.format_exc())
            return self._send(500, {"error": str(exc)})

    def do_POST(self):
        self.service.begin_request()
        try:
            m = self._route()
            if not m or m[1] != "matches":
                self._unhandled("POST")
                return self._send(404, {"error": "not found"})
            kind = m[0]
            try:  # only a malformed request body is the client's fault (400); anything later is ours (500)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype or raw.strip().startswith(b"{"):
                    body = json.loads(raw.decode("utf-8") or "{}")
                else:
                    body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            for k, v in self._query.items():  # some clients pass match hints as query params
                body.setdefault(k, v[0])
            log.info("MATCH %s %s", kind, {k: body.get(k) for k in ("type", "title", "parentTitle", "grandparentTitle",
                                                                   "year", "guid", "index", "parentIndex", "filename", "manual", "episodeOrder", "includeAdult") if body.get(k) not in (None, "")})
            items = self.service.match(kind, body, self._lang(), self._country())
            return self._send(200, self._container(kind, "Metadata", items))
        except Exception as exc:
            log.error("POST %s failed: %s\n%s", TOKEN_RE.sub(r"\1***", self.path), exc, traceback.format_exc())
            return self._send(500, {"error": str(exc)})


def serve(service: Service):
    Handler.service = service
    httpd = ThreadingHTTPServer((service.cfg.host, service.cfg.port), Handler)
    httpd.daemon_threads = True
    log.info("HAMA provider listening on http://%s:%d  (/tv AniDB numbering, /tvdb TVDB seasons, /movies)", service.cfg.host, service.cfg.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
