"""Small HTML pages for people: status (/) and missing-data report (/report)."""
from __future__ import annotations

import time
from html import escape

LIGHT = ("--bg:#ffffff;--fg:#1f2328;--muted:#656d76;--line:#d8dee4;--head:#f6f8fa;--code:#eff1f3;"
         "--link:#0969da;--ok:#1a7f37;--bad:#cf222e;--accent:#0969da")
DARK = ("--bg:#0f1115;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--head:#161b22;--code:#21262d;"
        "--link:#58a6ff;--ok:#3fb950;--bad:#f85149;--accent:#58a6ff")

# Light/dark: follows the system setting (prefers-color-scheme) unless the toggle picked one; the choice is kept in
# localStorage and applied before the page draws (no white flash).
STYLE = """<style>
:root{%s;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){%s;color-scheme:dark}}
:root[data-theme=dark]{%s;color-scheme:dark}
body{font:14px system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:var(--bg);color:var(--fg)}
main{margin:2em auto;max-width:72em;padding:0 1.2em}
h1{font-size:1.4em;margin:.2em 0 .8em}h2{font-size:1.1em;margin-top:2em}
table{border-collapse:collapse;width:100%%}td,th{border-bottom:1px solid var(--line);padding:.4em .55em;text-align:left;vertical-align:top}
th{background:var(--head);font-weight:600}tr:hover td{background:var(--head)}
.ok{color:var(--ok)}.bad{color:var(--bad)}small{color:var(--muted)}
code{background:var(--code);padding:.05em .35em;border-radius:4px}a{color:var(--link)}
header{display:flex;justify-content:space-between;align-items:center;gap:1em;flex-wrap:wrap}
nav a{margin-right:1em}
.theme{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.theme button{font:inherit;font-size:12px;background:transparent;color:var(--muted);border:0;padding:.3em .7em;cursor:pointer}
.theme button[aria-pressed=true]{background:var(--accent);color:var(--bg)}
</style>""" % (LIGHT, DARK, DARK)

HEAD_SCRIPT = """<script>try{var t=localStorage.getItem('hama-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>"""

TOGGLE = """<div class=theme role=group aria-label="Colour theme"><button data-t=auto>Auto</button><button data-t=light>Light</button><button data-t=dark>Dark</button></div>
<script>(function(){var r=document.documentElement,b=document.querySelectorAll('.theme button');
function mark(){var t=r.dataset.theme||'auto';b.forEach(function(x){x.setAttribute('aria-pressed',x.dataset.t===t)})}
b.forEach(function(x){x.onclick=function(){var t=x.dataset.t;if(t==='auto'){delete r.dataset.theme}else{r.dataset.theme=t}
try{localStorage.setItem('hama-theme',t)}catch(e){}mark()}});mark()})()</script>"""


def _page(title: str, heading: str, body: str) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
            "<meta name=color-scheme content=\"light dark\"><title>%s</title>%s%s</head><body><main>"
            "<header><nav><a href=\"/\">Status</a><a href=\"/report\">Report</a></nav>%s</header><h1>%s</h1>%s</main></body></html>"
            % (escape(title), HEAD_SCRIPT, STYLE, TOGGLE, escape(heading), body))


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
    return _page("HAMA provider", "HAMA – AniDB Metadata Provider for Plex",
                 "<table>%s</table><h2>Provider URLs for Plex</h2><ul>%s</ul>%s"
                 "<h2>Settings</h2><p>Read-only. Change them in <code>config.json</code> or <code>HAMA_*</code> environment variables "
                 "(Docker: <code>.env</code>) and restart.</p><table>%s</table>"
                 % ("".join("<tr><th>%s</th><td>%s</td></tr>" % r for r in rows), providers,
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
    return _page("HAMA missing data", "Missing-data report",
                 "<p>Problems found while building metadata. Fixing them upstream (AniDB, Anime-Lists, fanart.tv) helps every HAMA user.</p>"
                 "%s%s" % (body, recent))
