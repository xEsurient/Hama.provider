"""Small HTML pages for people: status (/) and missing-data report (/report)."""
from __future__ import annotations

import time
from html import escape

STYLE = """<style>body{font:14px system-ui,sans-serif;margin:2em;max-width:70em;color:#222}h1{font-size:1.4em}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.35em .5em;text-align:left;vertical-align:top}
th{background:#f4f4f4}.ok{color:#1a7f37}.bad{color:#b42318}code{background:#f4f4f4;padding:0 .3em}a{color:#0b5cad}</style>"""


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def status_page(status: dict, base: str) -> str:
    banned = status["anidb_banned_until"] > time.time()
    sync = status["last_sync"]
    rows = [
        ("Version", escape(status["version"])),
        ("Data loaded", '<span class="%s">%s</span>' % (("ok", "yes") if status["ready"] else ("bad", "loading…"))),
        ("AniDB titles / Anime-Lists mappings", "%d / %d" % (status["anime"], status["mappings"])),
        ("AniDB API", '<span class="bad">banned, cache only until %s</span>' % _when(status["anidb_banned_until"]) if banned
         else '<span class="ok">ok</span>'),
        ("Unreachable sources", '<span class="bad">%s</span>' % escape(", ".join(status["hosts_down"])) if status["hosts_down"]
         else '<span class="ok">none</span>'),
        ("TVDB → TMDB fallbacks", str(status["tvdb_fallbacks"])),
        ("Cache", "%d files, %.1f MB" % (status["cache_files"], status["cache_bytes"] / 1048576)),
        ("AniDB client", "%s (HAMA's client name; set anidb_client / anidb_clientver to use your own)" % escape(status["settings"]["anidb_client"])
         if status["settings"].get("anidb_client") == "hama" else "%s (own client)" % escape(str(status["settings"].get("anidb_client")))),
        ("Unmatched rescue", ("last run %s: %d items re-matched" % (_when(status["last_rescue"]["time"]), status["last_rescue"]["asked"]))
         if status.get("last_rescue") else "not run (needs plex_url)"),
        ("AniDB pre-fetch", "%d queued, %d fetched since start" % (status["prefetch_queue"], status["prefetch_done"])),
        ("Collection sync", ("last run %s: %d items, %d updated" % (_when(sync["time"]), sync["items"], sync["tagged"]))
         if sync else "not run (set plex_url to enable)"),
        ("Missing-data report", '<a href="/report">%d shows with issues</a>' % status["issues"]),
    ]
    providers = "".join("<li><code>%s%s</code></li>" % (escape(base), escape(p)) for p in status["providers"].values())
    fmt = lambda v: escape(", ".join(map(str, v)) if isinstance(v, list) else str(v))
    settings = "".join("<tr><th>%s</th><td>%s</td></tr>" % (escape(k), fmt(v)) for k, v in status["settings"].items())
    profiles = "".join("<tr><th>/%s</th><td>%s<br><small>based on /%s, identifier <code>%s</code></small><br>%s</td></tr>"
                       % (escape(n), escape(p["title"]), escape(p["base"]), escape(p["identifier"]),
                          "<br>".join("%s = %s" % (escape(k), fmt(v)) for k, v in p["settings"].items()) or "no changes")
                       for n, p in status["profiles"].items())
    return ("<!doctype html><meta charset=utf-8><title>HAMA provider</title>%s<h1>HAMA – AniDB Metadata Provider for Plex</h1>"
            "<table>%s</table><h2>Provider URLs for Plex</h2><ul>%s</ul>%s"
            "<h2>Settings</h2><p>Read-only. Change them in <code>config.json</code> or <code>HAMA_*</code> environment variables "
            "(Docker: <code>.env</code>) and restart.</p><table>%s</table>"
            % (STYLE, "".join("<tr><th>%s</th><td>%s</td></tr>" % r for r in rows), providers,
               "<h2>Profiles</h2><table>%s</table>" % profiles if profiles else "", settings))


def report_page(entries: list, events: list | None = None) -> str:
    if not entries:
        body = "<p class=ok>No problems recorded. Shows are checked when Plex refreshes them.</p>"
    else:
        rows = []
        for root, entry in entries:
            links = " ".join('<a href="%s">%s</a>' % (escape(url), escape(name)) for name, url in entry["links"].items())
            problems = "<br>".join(
                ('<a href="%s">%s</a>' % (escape(i["link"]), escape(i["text"])) if i.get("link") else escape(i["text"]))
                for _code, i in sorted(entry["issues"].items()))
            rows.append("<tr><td><b>%s</b><br><code>%s</code> %s<br><small>%s</small></td><td>%s</td></tr>"
                        % (escape(entry["title"]), escape(root), links, _when(entry["updated"]), problems))
        body = "<table><tr><th>Show</th><th>Problems</th></tr>%s</table>" % "".join(rows)
    rows = "".join("<tr><td><small>%s</small></td><td><b>%s</b>%s</td><td>%s</td></tr>"
                   % (_when(e["time"]), escape(e["kind"]), " ×%d" % e["count"] if e.get("count", 1) > 1 else "",
                      '<a href="%s">%s</a>' % (escape(e["link"]), escape(e["text"])) if e.get("link") else escape(e["text"]))
                   for e in events or [])
    recent = ("<h2>Recent events</h2><p>Things Plex or the sources did that need a look: unmatched items, skipped adult titles, "
              "requests HAMA doesn't serve, sources down, AniDB bans.</p><table><tr><th>When</th><th>What</th><th>Details</th></tr>%s</table>"
              % rows) if rows else "<h2>Recent events</h2><p class=ok>None.</p>"
    return ("<!doctype html><meta charset=utf-8><title>HAMA missing data</title>%s<h1>Missing-data report</h1>"
            "<p>Problems found while building metadata. Fixing them upstream (AniDB, Anime-Lists, fanart.tv) helps every HAMA user. "
            '<a href="/">Back to status</a></p>%s%s' % (STYLE, body, recent))
