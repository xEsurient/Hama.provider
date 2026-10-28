"""Offline tests: python -m unittest discover -s tests (from the provider/ folder)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hama_provider import config, server  # noqa: E402
from hama_provider.anidb import parse_anime  # noqa: E402
from hama_provider.animelists import episode_map  # noqa: E402
from hama_provider.images import Image, order  # noqa: E402
from hama_provider.plexsync import PlexSync  # noqa: E402
from hama_provider.service import Service  # noqa: E402
from hama_provider.titles import cleanse_title, pick_title, summary_sanitizer  # noqa: E402

ANIME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<anime id="23" restricted="false">
  <type>TV Series</type><episodecount>26</episodecount>
  <startdate>1998-04-03</startdate><enddate>1999-04-24</enddate>
  <titles>
    <title xml:lang="x-jat" type="main">Cowboy Bebop</title>
    <title xml:lang="ja" type="official">カウボーイビバップ</title>
    <title xml:lang="en" type="official">Cowboy Bebop EN</title>
    <title xml:lang="en" type="short">CB</title>
  </titles>
  <description>http://anidb.net/ch123 [Spike] hunts bounties.
Source: somewhere</description>
  <picture>123.jpg</picture>
  <tags><tag weight="600" infobox="true"><name>space western</name></tag>
        <tag weight="200"><name>jazz</name></tag></tags>
  <creators><name type="Animation Work">Sunrise</name><name type="Direction">Watanabe Shinichirou</name></creators>
  <characters><character type="main character in"><name>Spike Spiegel</name><seiyuu picture="9.jpg">Yamadera Kouichi</seiyuu></character></characters>
  <episodes>
    <episode><epno type="1">2</epno><length>25</length><airdate>1998-04-10</airdate><title xml:lang="en">Stray Dog Strut</title></episode>
    <episode><epno type="1">1</epno><length>25</length><airdate>1998-04-03</airdate><title xml:lang="en">Asteroid Blues</title><title xml:lang="x-jat">Asteroid Blues JP</title></episode>
    <episode><epno type="2">S1</epno><length>25</length><title xml:lang="en">Mish-Mash Blues</title></episode>
    <episode><epno type="3">C1</epno><title xml:lang="en">Opening</title></episode>
    <episode><epno type="3">C2</epno><title xml:lang="en">Ending</title></episode>
    <episode><epno type="4">T1</epno><title xml:lang="en">Trailer</title></episode>
  </episodes>
</anime>"""

TITLES = {"23": [("main", "x-jat", "Cowboy Bebop"), ("official", "ja", "カウボーイビバップ")],
          "219": [("main", "x-jat", "Cowboy Bebop: Tengoku no Tobira")]}


class TitleTests(unittest.TestCase):
    def test_cleanse(self):
        self.assertEqual(cleanse_title("Shingeki no Kyojin (2013) [BD]: Season-2"), "shingeki no kyojin season 2")
        self.assertEqual(cleanse_title("Pokémon"), "pokemon")

    def test_pick_title_priority(self):
        titles = parse_anime(ET.fromstring(ANIME_XML))["titles"]
        self.assertEqual(pick_title(titles, ["main", "en"]), ("Cowboy Bebop", "Cowboy Bebop"))
        self.assertEqual(pick_title(titles, ["en", "main"])[0], "Cowboy Bebop EN")   # official beats short
        self.assertEqual(pick_title(titles, ["ja"])[0], "カウボーイビバップ")
        self.assertEqual(pick_title(titles, ["de"])[0], "Cowboy Bebop")              # falls back to main

    def test_summary(self):
        self.assertEqual(summary_sanitizer("http://anidb.net/ch123 [Spike] hunts.\nSource: x"), "Spike hunts.")


class AnimeParseTests(unittest.TestCase):
    def test_episode_numbering(self):
        anime = parse_anime(ET.fromstring(ANIME_XML))
        got = [(e["season"], e["index"]) for e in anime["episodes"]]
        # regular eps sorted, S1 -> 0/1, C1 opening -> 0/101, C2 'Ending' -> 0/151, T1 -> 0/201
        self.assertEqual(got, [(1, 1), (1, 2), (0, 1), (0, 101), (0, 151), (0, 201)])
        self.assertEqual(anime["episodes"][0]["duration"], 25 * 60000)
        self.assertEqual(anime["picture"], "https://cdn.anidb.net/images/main/123.jpg")
        self.assertEqual(anime["characters"][0]["name"], "Yamadera Kouichi")


class ImageOrderTests(unittest.TestCase):
    def test_logo_language_first(self):
        found = {"FanartTV": {"clearLogo": [Image("f-ja", "ja", 5, "FanartTV"), Image("f-en", "en", 1, "FanartTV")]},
                 "TheMovieDb": {"clearLogo": [Image("t-en", "en", 99, "TheMovieDb")]}}
        urls = [i.url for i in order(found, "clearLogo", ["FanartTV", "TheMovieDb"], ["en", "ja", ""], 10, True)]
        self.assertEqual(urls, ["f-en", "t-en", "f-ja"])

    def test_posters_source_first(self):
        found = {"AniDB": {"coverPoster": [Image("a", "", 0, "AniDB")]},
                 "FanartTV": {"coverPoster": [Image("f", "en", 9, "FanartTV")]}}
        urls = [i.url for i in order(found, "coverPoster", ["AniDB", "FanartTV"], ["en", ""], 10)]
        self.assertEqual(urls, ["a", "f"])


def _entry(season, offset="", mappings=()):
    return {"defaulttvdbseason": season, "episodeoffset": offset, "mappings": [
        dict({"anidbseason": "1", "tvdbseason": None, "start": None, "end": None, "offset": None, "text": None}, **m)
        for m in mappings]}


class HelperTests(unittest.TestCase):
    def test_env_example_lists_every_setting(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install", "docker", ".env.example")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        missing = [k for k in config.DEFAULTS if "HAMA_%s=" % k.upper() not in text]
        self.assertEqual(missing, [])

    def test_ranges(self):
        from hama_provider.service import _ranges
        self.assertEqual(_ranges([5, 1, 2, 3, 9, 10]), "1-3, 5, 9-10")

    def test_prune_keeps_anidb(self):
        from hama_provider.net import Fetcher
        with tempfile.TemporaryDirectory() as tmp:
            f = Fetcher(tmp)
            names = ("TheMovieDb/tv/1_details.json", "TheTVDB/1/series_en.json", "AniDB/xml/1.xml")
            for name in names:
                f.write_cache(name, b"{}")
            old = time.time() - 200 * 86400
            for name in names:
                os.utime(f.cache_path(name), (old, old))
            self.assertEqual(f.prune(90), 1)
            self.assertIsNone(f.read_cache("TheMovieDb/tv/1_details.json"))
            self.assertIsNotNone(f.read_cache("TheTVDB/1/series_en.json"))   # kept: fallback if TheTVDB's API is gone
            self.assertIsNotNone(f.read_cache("AniDB/xml/1.xml"))


class HostDownTests(unittest.TestCase):
    def test_unreachable_host_is_skipped(self):
        from hama_provider.net import Fetcher
        with tempfile.TemporaryDirectory() as tmp:
            f = Fetcher(tmp)
            f.write_cache("x/a.json", b'{"cached": 1}')
            started = time.time()
            self.assertEqual(f.get("http://127.0.0.1:9/a", "x/a.json", ttl=0, timeout=2), b'{"cached": 1}')  # refused -> stale cache
            self.assertTrue(f.host_down("http://127.0.0.1:9/b"))
            self.assertIsNone(f.get("http://127.0.0.1:9/b", None, ttl=0))      # skipped instantly while down
            self.assertLess(time.time() - started, 5)
            f._mark_failure("http://other/x", ValueError("rejected body"))
            self.assertFalse(f.host_down("http://other/y"))


class PrefetchTests(unittest.TestCase):
    def test_queue_fetch_and_complete(self):
        from hama_provider.prefetch import Prefetcher

        class Anidb:
            banned_until, cached, fetched = 0, {"1"}, []

            def anime(self, aid, fetch=True):
                if aid in self.cached:
                    return {"aid": aid}
                if fetch:
                    self.fetched.append(aid)
                    self.cached.add(aid)
                    return {"aid": aid}
                return None

        with tempfile.TemporaryDirectory() as tmp:
            svc = type("Svc", (), {})()
            svc.cfg, svc.anidb, forgotten, refreshed = config.Config(data_dir=tmp), Anidb(), [], []
            svc.fetcher = type("F", (), {"host_down": lambda self, url: False})()
            svc.forget = forgotten.append
            pre = Prefetcher(svc, 30)
            pre.on_show_complete = lambda kind, root: refreshed.append((kind, root))
            pre.add("tvdb", "tvdb-5", ["1", "2", "3"])          # "1" is cached already
            self.assertEqual(len(pre), 2)
            self.assertEqual(len(Prefetcher(svc, 30)), 2)        # persisted
            pre.step()
            self.assertEqual(refreshed, [])
            pre.step()
            self.assertEqual((svc.anidb.fetched, forgotten, refreshed), (["2", "3"], ["tvdb-5"], [("tvdb", "tvdb-5")]))
            svc.anidb.banned_until = time.time() + 60
            pre.add("tvdb", "tvdb-6", ["4"])
            self.assertIsNone(pre.step())                        # paused during an AniDB ban


class ReviewRegressionTests(unittest.TestCase):
    """Bugs found by the independent review (2026-10-27)."""

    def test_issues_store_keeps_a_copy(self):
        from hama_provider.report import Issues
        with tempfile.TemporaryDirectory() as tmp:
            store, live = Issues(tmp), {"no_logo": {"text": "No logo", "link": None}}
            store.set("anidb-1", "Show", {}, live)
            live["no_poster"] = {"text": "changed later by another thread", "link": None}
            self.assertEqual(set(dict(store.all())["anidb-1"]["issues"]), {"no_logo"})
            dict(store.all())["anidb-1"]["issues"].clear()                      # callers get copies too
            self.assertEqual(store.count(), 1)
            self.assertEqual(set(dict(store.all())["anidb-1"]["issues"]), {"no_logo"})

    def test_anidb_budget_is_per_request(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = Service(config.Config(dict(config.DEFAULTS, data_dir=tmp.name)))
        fetched = []

        class Anidb:
            banned_until = 0
            def anime(self, aid, fetch=True):
                if fetch:
                    fetched.append(aid)
                return None
        svc.anidb = Anidb()
        svc.begin_request()
        for aid in ("1", "2", "3", "4", "5"):
            svc._anidb_budgeted(aid)
        self.assertEqual(fetched, ["1", "2", "3"])
        svc.begin_request()
        svc._anidb_budgeted("6")
        self.assertEqual(fetched[-1], "6")

    def test_prefetch_gives_up_without_refresh_loop(self):
        from hama_provider.prefetch import MAX_ATTEMPTS, Prefetcher

        class Anidb:
            banned_until, calls = 0, 0
            def anime(self, aid, fetch=True):
                if fetch:
                    Anidb.calls += 1
                return None                                   # AniDB never answers for this id
        with tempfile.TemporaryDirectory() as tmp:
            svc = type("Svc", (), {})()
            svc.cfg, svc.anidb, forgotten, refreshed = config.Config(data_dir=tmp), Anidb(), [], []
            svc.fetcher = type("F", (), {"host_down": lambda self, url: False})()
            svc.forget = forgotten.append
            pre = Prefetcher(svc, 30)
            pre.on_show_complete = lambda kind, root: refreshed.append(root)
            pre.add("tvdb", "tvdb-9", ["7"])
            for _ in range(MAX_ATTEMPTS + 2):
                pre.step()
            self.assertEqual((Anidb.calls, len(pre), refreshed, forgotten), (MAX_ATTEMPTS, 0, [], ["tvdb-9"]))
            pre.add("tvdb", "tvdb-9", ["7"])                  # given up: not queued again for a day
            self.assertEqual(len(pre), 0)
            svc.fetcher = type("F", (), {"host_down": lambda self, url: True})()
            pre.add("tvdb", "tvdb-10", ["8"])
            self.assertIsNone(pre.step())                     # AniDB unreachable: wait, don't drain the queue

    def test_broken_gzip_falls_back_to_cache(self):
        from http.server import BaseHTTPRequestHandler
        from hama_provider.net import Fetcher

        class Broken(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_GET(self):
                body = b"\x1f\x8b\x08\x00broken"                      # gzip magic, then garbage
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Broken)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            f = Fetcher(tmp)
            f.write_cache("x/a.xml", b"<old/>")
            url = "http://127.0.0.1:%d/a" % httpd.server_address[1]
            self.assertEqual(f.get(url, "x/a.xml", ttl=0), b"<old/>")
            self.assertFalse(f.host_down(url))                # the host answered: not marked down

    def test_http_429_pauses_host(self):
        import urllib.error
        from hama_provider.net import Fetcher
        f = Fetcher(tempfile.mkdtemp())
        err = urllib.error.HTTPError("https://graphql.anilist.co", 429, "Too Many Requests", {"Retry-After": "30"}, None)
        f._mark_failure("https://graphql.anilist.co", err)
        self.assertTrue(f.host_down("https://graphql.anilist.co"))
        self.assertAlmostEqual(f.down_until["graphql.anilist.co"] - time.time(), 30, delta=3)
        f._mark_failure("https://other.example", urllib.error.HTTPError("https://other.example", 404, "x", {}, None))
        self.assertFalse(f.host_down("https://other.example"))

    def test_logo_language_library_first(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = Service(config.Config(dict(config.DEFAULTS, data_dir=tmp.name)))
        self.assertEqual(svc._logo_languages("ja"), ["ja", "en", ""])
        self.assertEqual(svc._logo_languages(""), ["en", "ja", ""])

    def test_corrupt_story_arc_list(self):
        from hama_provider.tvdb4 import StoryArcs
        fetcher = type("F", (), {"get": lambda self, url, name, ttl, validate=None: b"<tvdb4entries><anime tvdbid='1'>01|001"})()
        arcs = StoryArcs(fetcher)
        self.assertEqual(arcs.arcs("1"), [])                  # no exception, provider keeps working


class PrepareTests(unittest.TestCase):
    def _tree(self, root, files):
        for rel, content in files.items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(content)

    def test_names(self):
        from hama_provider.prepare import episode_number, special_number
        cases = {"[Grp] Show - NCOP1 [ABCD]": 101, "Show - ED2": 152, "Show - NCED": 151, "Show - PV": 201,
                 "Show - SP03": 3, "Show - S1": 1, "Show - OVA": 1, "Show S2 - 05": None, "Show - 05": None,
                 "Show - S01E05": None, "Show - Opening 2": 102, "Show NCOP1": 101, "Show - SP00": None,
                 # episode titles / show names that merely contain special words stay episodes (review 2026-10-28)
                 "Show - 05 - The Ending": None, "Fullmetal Alchemist - 03 - Ed's Mission": None,
                 "Show - 07 - Opening Night": None, "Show - 12 - Preview of Tomorrow": None, "Special A 05": None,
                 "Show S2 05": None, "86 - 01": None}
        self.assertEqual({k: special_number(k) for k in cases}, cases)
        self.assertEqual(episode_number("[SubsPlease] Show - 05v2 (1080p) [ABCD1234]"), (None, 5))
        self.assertEqual(episode_number("Show S02E07"), (2, 7))
        self.assertEqual(episode_number("Show - Extra Clip"), None)
        self.assertEqual(episode_number("Show - 05.5"), None)                       # recap: not a regular episode

    def test_plan_apply_undo(self):
        from hama_provider.prepare import Preparer, UNDO_FILE
        with tempfile.TemporaryDirectory() as root:
            self._tree(root, {
                "Cowboy Bebop/Cowboy Bebop - 01.mkv": "",
                "Cowboy Bebop/Cowboy Bebop - NCOP1.mkv": "op",
                "Cowboy Bebop/Cowboy Bebop - NCOP1.en.ass": "subs",
                "Cowboy Bebop/anidb.id": "23\n",
                "Cowboy Bebop/Bonus Clip.mkv": "",
                "Cowboy Bebop/SPs/Cowboy Bebop - 02.mkv": "",
                "Cowboy Bebop/Bonus/Making of - 01.mkv": "",
                "Zero no Tsukaima/[1] Zero no Tsukaima/Zero - 01.mkv": "",
                "Zero no Tsukaima/[2] Futatsuki no Kishi [anidb-4747]/Futatsuki - 01.mkv": "",
                "Show [anidb-9]/Arc 2 -s2e12/Show - 01.mkv": "",
                "Plain [anidb-1]/.plexmatch": "year: 1998\n",
                "Plain [anidb-1]/Plain - 01.mkv": "",
            })
            prep = Preparer()
            plan = prep.plan(root)
            got = {(a.kind, os.path.relpath(a.path, root), os.path.relpath(a.target, root) if a.target else a.content.strip())
                   for a in plan.actions if a.kind != "warn"}
            self.assertEqual(got, {
                ("plexmatch", "Cowboy Bebop/.plexmatch", "title: Cowboy Bebop [anidb-23]"),
                ("rename", "Cowboy Bebop/Cowboy Bebop - NCOP1.mkv", "Cowboy Bebop/Cowboy Bebop - S00E101.mkv"),
                ("rename", "Cowboy Bebop/Cowboy Bebop - NCOP1.en.ass", "Cowboy Bebop/Cowboy Bebop - S00E101.en.ass"),
                ("rename", "Cowboy Bebop/SPs/Cowboy Bebop - 02.mkv", "Cowboy Bebop/SPs/Cowboy Bebop - S00E02.mkv"),
                # "[1] Same name" is the parent show itself: its files move up
                ("move", "Zero no Tsukaima/[1] Zero no Tsukaima/Zero - 01.mkv", "Zero no Tsukaima/Zero - 01.mkv"),
                ("move", "Zero no Tsukaima/[2] Futatsuki no Kishi [anidb-4747]", "Futatsuki no Kishi [anidb-4747]"),
                ("rename", "Show [anidb-9]/Arc 2 -s2e12/Show - 01.mkv", "Show [anidb-9]/Arc 2 -s2e12/Show - S02E13.mkv"),
            })
            warns = [os.path.relpath(a.path, root) for a in plan.actions if a.kind == "warn"]
            self.assertIn("Cowboy Bebop/Bonus Clip.mkv", warns)
            self.assertIn("Cowboy Bebop/Bonus", warns)                              # unknown sub-folder: reported, never moved
            self.assertNotIn("Cowboy Bebop/Bonus/Making of - 01.mkv", [os.path.relpath(a.path, root) for a in plan.actions])
            before = sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)
            self.assertEqual(prep.apply(plan), 7)
            self.assertTrue(os.path.exists(os.path.join(root, "Cowboy Bebop", "Cowboy Bebop - S00E101.en.ass")))
            self.assertTrue(os.path.isdir(os.path.join(root, "Futatsuki no Kishi [anidb-4747]")))
            with open(os.path.join(root, "Cowboy Bebop", ".plexmatch")) as fh:
                self.assertIn("title: Cowboy Bebop [anidb-23]", fh.read())
            self.assertEqual(prep.apply(prep.plan(root)), 0)                    # idempotent
            self.assertEqual(prep.undo(root), 7)
            after = sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs if f != UNDO_FILE)
            self.assertEqual(after, before)

    def test_lone_movie_files(self):
        from hama_provider.prepare import Preparer
        with tempfile.TemporaryDirectory() as root:
            self._tree(root, {"Kimi no Na wa (2016) [anidb-11829]/Kimi no Na wa (2016).mkv": "",
                              "Tenki no Ko/Tenki no Ko - Complete Movie.mkv": "",
                              "Koe no Katachi/Koe no Katachi.mkv": "",
                              "Spirited Away/Spirited Away - 01.mkv": "",            # already fine
                              "Some Show/Some Show - 05.mkv": ""})                    # numbered: an episode, untouched
            got = {(os.path.relpath(a.path, root), os.path.basename(a.target)) for a in Preparer().plan(root).actions if a.kind == "rename"}
            self.assertEqual(got, {("Kimi no Na wa (2016) [anidb-11829]/Kimi no Na wa (2016).mkv", "Kimi no Na wa (2016) - 01.mkv"),
                                   ("Tenki no Ko/Tenki no Ko - Complete Movie.mkv", "Tenki no Ko - 01.mkv"),
                                   ("Koe no Katachi/Koe no Katachi.mkv", "Koe no Katachi - 01.mkv")})

    def test_plexmatch_keeps_user_lines(self):
        from hama_provider.prepare import _write_plexmatch, BLOCK_BEGIN
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, ".plexmatch")
            with open(path, "w") as fh:
                fh.write("year: 1998\n")
            _write_plexmatch(path, "title: X [anidb-1]\n")
            _write_plexmatch(path, "title: Y [anidb-2]\n")                   # rewrite: block replaced, not duplicated
            with open(path) as fh:
                text = fh.read()
            self.assertEqual((text.count(BLOCK_BEGIN), "year: 1998" in text, "anidb-1" in text, "anidb-2" in text), (1, True, False, True))
            with open(path, "wb") as fh:                                       # Latin-1 byte + our block without end marker
                fh.write(b"title: Caf\xe9\n" + BLOCK_BEGIN.encode() + b"\nyear: 2001\ntitle: Z [anidb-3]\n")
            _write_plexmatch(path, "title: W [anidb-4]\n")
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertIn(b"Caf\xe9", raw)                                     # user bytes preserved
            self.assertIn(b"year: 2001", raw)                                   # unterminated block: user line kept

    def test_apply_needs_writable_root(self):
        from hama_provider.prepare import Preparer
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        with tempfile.TemporaryDirectory() as root:
            self._tree(root, {"Show/Show - NCOP1.mkv": ""})
            prep = Preparer()
            plan = prep.plan(root)
            os.chmod(root, 0o555)
            try:
                with self.assertRaises(OSError):
                    prep.apply(plan)
            finally:
                os.chmod(root, 0o755)
            self.assertTrue(os.path.exists(os.path.join(root, "Show", "Show - NCOP1.mkv")))    # nothing changed


class AniDBBanTests(unittest.TestCase):
    def test_ban_persists_and_escalates(self):
        from hama_provider.anidb import AniDB, BAN_BACKOFF
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "anidb_state.json")
            a = AniDB(None, "hama", "1", state)
            self.assertFalse(a._validate(b'<error code="500">banned</error>'))
            first = a.banned_until - time.time()
            self.assertAlmostEqual(first, BAN_BACKOFF, delta=5)
            b = AniDB(None, "hama", "1", state)                              # restart: still banned
            self.assertGreater(b.banned_until, time.time() + BAN_BACKOFF - 10)
            b._validate(b'<error code="500">banned</error>')                  # second ban: twice as long
            self.assertAlmostEqual(b.banned_until - time.time(), 2 * BAN_BACKOFF, delta=5)
            self.assertTrue(b._validate(b'<anime id="1"><type>TV</type></anime>'))
            self.assertEqual(AniDB(None, "hama", "1", state).ban_strikes, 0)    # good answer resets the escalation

    def test_title_source_and_fallback(self):
        from hama_provider.anidb import TITLES_MIRROR_URL, TITLES_URL, TitleDB
        from hama_provider.net import Fetcher
        names = {"1": "Kumo no Mukou", "2": "Hoshi no Koe"}
        dump = lambda *aids: ("<animetitles>" + "".join('<anime aid="%s"><title type="main" xml:lang="x-jat">%s</title></anime>'
                                                         % (a, names[a]) for a in aids) + "</animetitles>").encode()
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            responses = {TITLES_MIRROR_URL: dump("1"), TITLES_URL: dump("1", "2")}

            class F(Fetcher):
                def get(self, url, cache_name, ttl, **kw):
                    calls.append(url)
                    data = responses.get(url)
                    if data:
                        self.write_cache(cache_name, data)
                    return data

            db = TitleDB(F(tmp))
            db.load()
            self.assertEqual((calls, len(db.titles)), ([TITLES_MIRROR_URL], 1))           # mirror only, no AniDB load
            self.assertEqual(db.search("Hoshi no Koe"), [])
            self.assertTrue(db.refresh_from_anidb("test"))                                # missing anime -> AniDB once
            self.assertEqual(db.search("Hoshi no Koe")[0][1], "2")
            self.assertFalse(db.refresh_from_anidb("again"))                              # max once a day
            self.assertEqual(calls.count(TITLES_URL), 1)
            db2 = TitleDB(F(tmp))
            db2.load()                                                                    # restart: keeps the larger list
            self.assertEqual(len(db2.titles), 2)
            self.assertEqual(calls.count(TITLES_URL), 1)
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            responses = {TITLES_URL: dump("1")}                                           # mirror down -> AniDB fallback
            db = TitleDB(F(tmp))
            db.load()
            self.assertEqual(calls, [TITLES_MIRROR_URL, TITLES_URL])
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            responses = {}                                                                # banned: no direct download
            db = TitleDB(F(tmp))
            db.direct_allowed = lambda: False
            with self.assertRaises(RuntimeError):
                db.load()
            self.assertEqual(calls, [TITLES_MIRROR_URL])


class TvdbMapTests(unittest.TestCase):
    def test_defaults_offsets_and_explicit(self):
        eps = {(1, e): {} for e in range(1, 4)}
        eps.update({(2, e): {} for e in range(1, 6)})
        eps.update({(0, 1): {}, (0, 2): {}})
        entries = {
            "10": _entry("1", mappings=[{"anidbseason": "0", "tvdbseason": "0", "text": ";1-2;"}]),
            "20": _entry("2"),
            "30": _entry("2", "3"),                              # second cour continues TVDB season 2 at ep 4
            "40": _entry("0"),                                   # movie placed in specials: only its ep 1
            "50": _entry("3", mappings=[{"tmdbseason": "9", "start": "1", "end": "3", "offset": "5"}]),  # TMDB-only line
        }
        m = episode_map(entries, eps)
        self.assertEqual(m[(1, 1)], ("10", 1, 1))
        self.assertEqual(m[(2, 3)], ("20", 1, 3))
        self.assertEqual(m[(2, 4)], ("30", 1, 1))
        self.assertEqual(m[(0, 2)], ("10", 0, 1))
        self.assertEqual(m[(0, 1)], ("40", 1, 1))
        self.assertNotIn("50", {v[0] for v in m.values()})     # TMDB-only mapping lines are ignored

    def test_absolute_and_ranges(self):
        eps = {(1, e): {"absoluteNumber": e} for e in range(1, 4)}
        eps.update({(2, e): {"absoluteNumber": 3 + e} for e in range(1, 4)})
        entries = {"7": _entry("a", mappings=[{"tvdbseason": "2", "start": "5", "end": "6", "offset": "-3"}])}
        m = episode_map(entries, eps)
        self.assertEqual([m[(2, e)] for e in (1, 2, 3)], [("7", 1, 4), ("7", 1, 5), ("7", 1, 6)])
        self.assertEqual(m[(1, 3)], ("7", 1, 3))


class FakeTVDB:
    EPS = {(1, 1): {"absoluteNumber": 1, "episodeName": "TVDB One", "overview": "tvdb summary", "firstAired": "1998-10-24", "filename": "episodes/76885/1.jpg"},
           (1, 2): {"absoluteNumber": 2, "episodeName": "TVDB Two", "overview": "", "firstAired": "1998-11-01", "filename": ""},
           (2, 1): {"absoluteNumber": 3, "episodeName": "Movie Ep", "overview": "", "firstAired": "2001-09-01", "filename": ""}}

    def series(self, tvdbid, lang="en"):
        return {"seriesName": "Cowboy Bebop TVDB", "overview": "Series overview", "firstAired": "1998-10-24",
                "network": "TV Tokyo", "imdbId": "tt0213338", "runtime": "25"} if tvdbid == "76885" else {}

    def episodes(self, tvdbid, lang="en"):
        return dict(self.EPS) if tvdbid == "76885" else {}

    def images(self, tvdbid):
        return {"coverPoster": [], "background": [], "season": {}}


class FakeTMDB:
    """TMDB numbering for Cowboy Bebop: one season of 3 episodes."""
    def episodes(self, tv_id, lang=""):
        return {(1, n): {"episodeName": "TMDB %d" % n, "overview": "Deutsch %d" % n if lang == "de" else "", "firstAired": "1998-10-%02d" % (20 + n),
                         "absoluteNumber": n, "thumb_url": "https://image.tmdb.org/t/p/original/s%d.jpg" % n} for n in (1, 2, 3)} if tv_id == "30991" else {}

    def find(self, external_id, source):
        return {"76885": ("30991", ""), "tt0133093": ("", "603")}.get(external_id, ("", ""))

    def search_movie(self, query, year=None):
        return [("603", "The Matrix", "1999-03-30"), ("604", "The Matrix Reloaded", "2003-05-15")] if "matrix" in query.lower() else []

    def credits(self, movie, tmdb_id):
        return {"cast": [{"name": "Keanu Reeves", "character": "Neo", "profile_path": "/k.jpg"}],
                "crew": [{"name": "Lana Wachowski", "job": "Director"}]} if tmdb_id == "603" else {}

    def details(self, movie, tmdb_id, lang=""):
        if movie and tmdb_id == "603":
            return {"title": "The Matrix", "overview": "Neo wakes up.", "release_date": "1999-03-30", "runtime": 136,
                    "genres": [{"name": "Action"}], "production_companies": [{"name": "Warner Bros."}], "imdb_id": "tt0133093"}
        if tmdb_id != "30991":
            return {}
        return {"name": "Cowboy Bebop TMDB", "overview": "Deutsche Beschreibung" if lang == "de" else "TMDB overview",
                "production_countries": [{"iso_3166_1": "JP", "name": "Japan"}]}

    def content_rating(self, movie, tmdb_id, country):
        return {"DE": "de/12", "US": "TV-14"}.get(country, "") if tmdb_id == "30991" else ""

    def images(self, movie, tmdb_id):
        return {}

    def collection(self, collection_id):
        return {}


class FakeArcs:
    def arcs(self, tvdbid):
        return [(1, 1, 2, "Arc One"), (2, 3, 10 ** 6, "Arc Two")] if tvdbid == "76885" else []

    def posters(self, tvdbid):
        return {2: [Image("https://example.org/arc2.jpg", "", 0, "tvdb4")]} if tvdbid == "76885" else {}


class FakeAniList:
    def cover(self, aid):
        return [Image("https://s4.anilist.co/cover/%s.jpg" % aid, "", 0, "AniList")] if aid == "23" else []


class FakeAniDB:
    banned_until = 0
    def anime(self, aid, fetch=True):
        return parse_anime(ET.fromstring(ANIME_XML)) if aid == "23" else None


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = config.load(os.path.join(cls.tmp.name, "none.json"))
        cfg.update(data_dir=cls.tmp.name, fanart_api_key="", tmdb_api_key="", tvdb_api_key="", theme_songs=False, tmdb_rating=False, collections=True,
                   profiles={"tv-en": {"base": "tv", "title": "HAMA EN", "series_language_priority": ["en", "main"],
                                       "tmdb_api_key": "ignored-per-profile"},
                             "Bad Name": {"base": "tv"}, "x": {"base": "nope"}, "report": {"base": "tv"}}, host="127.0.0.1", port=0)
        svc = Service(cfg)
        svc.anidb = FakeAniDB()
        svc.tvdb = FakeTVDB()
        svc.tmdb = FakeTMDB()
        svc.arcs = FakeArcs()
        svc.anilist = FakeAniList()
        svc.titledb.titles = TITLES
        svc.titledb.index = [(cleanse_title(t), aid, t) for aid, ts in TITLES.items() for _, _, t in ts]
        svc.titledb.ready.set()
        svc.lists.by_aid = {"23": {"tvdbid": "76885", "tmdbtv": "30991", "tmdbid": "", "imdbid": "", "studio": "Sunrise",
                                   "defaulttvdbseason": "1", "episodeoffset": "", "tmdbseason": "1",
                                   "tmdboffset": "", "mappings": []}}
        svc.lists.by_tvdb = {"76885": ["23"]}
        svc.lists.by_tmdbtv = {"30991": ["23"]}
        svc.lists.ready.set()
        cls.svc = svc
        server.Handler.service = svc
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, None

    def match(self, kind, body):
        req = urllib.request.Request(self.base + "/%s/library/metadata/matches" % kind, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["MediaContainer"]["Metadata"]

    def test_provider_root(self):
        code, body = self.get("/tv")
        self.assertEqual(code, 200)
        self.assertEqual(body["MediaProvider"]["identifier"], "tv.plex.agents.custom.hama.tv")
        self.assertEqual([t["type"] for t in body["MediaProvider"]["Types"]], [2, 3, 4])

    def test_show_metadata(self):
        code, body = self.get("/tv/library/metadata/anidb-23?includeChildren=1")
        show = body["MediaContainer"]["Metadata"][0]
        self.assertEqual(show["title"], "Cowboy Bebop")
        self.assertEqual(show["originalTitle"], "カウボーイビバップ")
        self.assertEqual(show["guid"], "tv.plex.agents.custom.hama.tv://show/anidb-23")
        self.assertEqual(show["originallyAvailableAt"], "1998-04-03")
        self.assertEqual(show["studio"], "Sunrise")
        self.assertIn({"id": "tvdb://76885"}, show["Guid"])
        self.assertEqual([g["tag"] for g in show["Genre"]], ["Space western"])  # HAMA capwords(name, "-")
        self.assertEqual([c["index"] for c in show["Children"]["Metadata"]], [0, 1])
        posters = [i["url"] for i in show["Image"] if i["type"] == "coverPoster"]
        self.assertEqual(posters, ["https://s4.anilist.co/cover/23.jpg", "https://cdn.anidb.net/images/main/123.jpg"])  # AniList first

    def test_paging_zero_based(self):
        code, body = self.get("/tv/library/metadata/anidb-23-s1/children",
                              {"X-Plex-Container-Start": "1", "X-Plex-Container-Size": "5"})
        c = body["MediaContainer"]
        self.assertEqual((c["offset"], c["size"], c["totalSize"]), (1, 1, 2))
        self.assertEqual(c["Metadata"][0]["title"], "Stray Dog Strut")
        code, body = self.get("/tv/library/metadata/anidb-23/grandchildren")
        self.assertEqual(body["MediaContainer"]["totalSize"], 6)

    def test_episode(self):
        code, body = self.get("/tv/library/metadata/anidb-23-s0-e151")
        ep = body["MediaContainer"]["Metadata"][0]
        self.assertEqual((ep["title"], ep["parentIndex"], ep["index"], ep["parentTitle"]), ("Ending", 0, 151, "Specials"))
        self.assertEqual(ep["grandparentGuid"], "tv.plex.agents.custom.hama.tv://show/anidb-23")

    def test_not_found(self):
        self.assertEqual(self.get("/tv/library/metadata/anidb-99")[0], 404)
        self.assertEqual(self.get("/tv/library/metadata/anidb-23-s5")[0], 404)
        self.assertEqual(self.get("/tv/library/metadata/bad%20key")[0], 404)
        self.assertEqual(self.get("/tv/library/metadata/anidb-23/extras")[1]["MediaContainer"]["size"], 0)

    def test_tvdb_mode(self):
        show = self.get("/tvdb/library/metadata/tvdb-76885?includeChildren=1")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual(show["guid"], "tv.plex.agents.custom.hama.tvdb://show/tvdb-76885")
        self.assertEqual(show["title"], "Cowboy Bebop")                       # AniDB title of the primary entry
        self.assertEqual(show["summary"], "Series overview")                  # TVDB summary first in TVDB mode
        self.assertEqual(show["Network"], [{"tag": "TV Tokyo"}])
        self.assertEqual([c["index"] for c in show["Children"]["Metadata"]], [1, 2])
        eps = self.get("/tvdb/library/metadata/tvdb-76885-s1/children")[1]["MediaContainer"]["Metadata"]
        self.assertEqual([e["title"] for e in eps], ["Asteroid Blues", "Stray Dog Strut"])  # AniDB titles over TVDB
        self.assertEqual(eps[0]["summary"], "tvdb summary")
        self.assertEqual(eps[0]["Image"], [{"type": "snapshot", "url": "https://artworks.thetvdb.com/banners/episodes/76885/1.jpg",
                                            "alt": "Asteroid Blues"}])
        self.assertEqual(eps[1]["grandparentRatingKey"], "tvdb-76885")
        unmapped = self.get("/tvdb/library/metadata/tvdb-76885-s2-e1")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual(unmapped["title"], "Movie Ep")

    def test_absolute_order(self):
        show = self.get("/tvdb/library/metadata/tvdb-76885?includeChildren=1&episodeOrder=absolute")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual([t["source"] + t["id"] for t in show["SeasonType"]],
                         ["tvdbaired", "tvdbabsolute", "tvdbseasonsAbsolute", "tvdbarcs", "tmdbtmdbAired"])
        self.assertEqual([c["ratingKey"] for c in show["Children"]["Metadata"]], ["tvdb-76885-a-s1"])
        eps = self.get("/tvdb/library/metadata/tvdb-76885-a-s1/children")[1]["MediaContainer"]["Metadata"]
        self.assertEqual([(e["index"], e["title"]) for e in eps], [(1, "Asteroid Blues"), (2, "Stray Dog Strut"), (3, "Movie Ep")])
        self.assertEqual(eps[2]["parentRatingKey"], "tvdb-76885-a-s1")
        ep = self.match("tvdb", {"type": 4, "grandparentTitle": "Cowboy Bebop", "parentIndex": 1, "index": 3, "episodeOrder": "tvdbAbsolute"})[0]
        self.assertEqual((ep["ratingKey"], ep["title"]), ("tvdb-76885-a-s1-e3", "Movie Ep"))
        self.assertEqual(self.get("/tvdb/library/metadata/tvdb-76885-s1-e3")[0], 404)   # aired order has no S1E3

    def test_tvdb3_and_story_arcs(self):
        eps = self.get("/tvdb/library/metadata/tvdb-76885-s2/children?episodeOrder=tvdbSeasonsAbsolute")[1]["MediaContainer"]["Metadata"]
        self.assertEqual([(e["ratingKey"], e["index"]) for e in eps], [("tvdb-76885-t3-s2-e3", 3)])
        show = self.get("/tvdb/library/metadata/tvdb-76885?includeChildren=1&episodeOrder=tvdbArcs")[1]["MediaContainer"]["Metadata"][0]
        seasons = show["Children"]["Metadata"]
        self.assertEqual([(c["ratingKey"], c["title"]) for c in seasons], [("tvdb-76885-t4-s1", "Arc One"), ("tvdb-76885-t4-s2", "Arc Two")])
        self.assertEqual(seasons[1]["thumb"], "https://example.org/arc2.jpg")
        eps = self.get("/tvdb/library/metadata/tvdb-76885-t4-s1/children")[1]["MediaContainer"]["Metadata"]
        self.assertEqual([(e["index"], e["parentTitle"]) for e in eps], [(1, "Arc One"), (2, "Arc One")])

    def test_tmdb_order_and_fallback(self):
        show = self.get("/tvdb/library/metadata/tvdb-76885")[1]["MediaContainer"]["Metadata"][0]
        self.assertIn({"id": "tmdbAired", "source": "tmdb", "tag": "Aired", "title": "TheMovieDb (Aired Order)"}, show["SeasonType"])
        eps = self.get("/tvdb/library/metadata/tvdb-76885-s1/children?episodeOrder=tmdbTmdbAired")[1]["MediaContainer"]["Metadata"]
        self.assertEqual([(e["ratingKey"], e["title"]) for e in eps],
                         [("tvdb-76885-m-s1-e1", "Asteroid Blues"), ("tvdb-76885-m-s1-e2", "Stray Dog Strut"), ("tvdb-76885-m-s1-e3", "TMDB 3")])
        self.assertEqual(eps[0]["thumb"], "https://image.tmdb.org/t/p/original/s1.jpg")
        # TheTVDB returning nothing -> TMDB fallback in the default order
        svc, orig = self.svc, FakeTVDB.episodes
        FakeTVDB.episodes = lambda self, tvdbid, lang="en": {}
        svc._models.clear()
        try:
            eps = self.get("/tvdb/library/metadata/tvdb-76885-s1/children")[1]["MediaContainer"]["Metadata"]
        finally:
            FakeTVDB.episodes = orig
            svc._models.clear()
        self.assertEqual([e["title"] for e in eps], ["Asteroid Blues", "Stray Dog Strut", "TMDB 3"])
        self.assertGreater(svc.tvdb_fallbacks, 0)

    def test_localized_library(self):
        headers = {"X-Plex-Language": "de-DE", "X-Plex-Country": "DE"}
        show = self.get("/tv/library/metadata/anidb-23", headers)[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual((show["summary"], show["contentRating"], show["Country"]), ("Deutsche Beschreibung", "de/12", [{"tag": "Japan"}]))
        eps = self.get("/tv/library/metadata/anidb-23-s1/children", headers)[1]["MediaContainer"]["Metadata"]
        self.assertEqual([e["summary"] for e in eps], ["Deutsch 1", "Deutsch 2"])      # matched through the AniDB entry
        self.assertEqual(eps[0]["title"], "Asteroid Blues")                            # titles follow the language settings
        en = self.get("/tv/library/metadata/anidb-23", {"X-Plex-Language": "en-US"})[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual((en["summary"], en["contentRating"]), ("Spike hunts bounties.", "TV-14"))

    def test_anidb_mode_tvdb_thumbs(self):
        ep = self.get("/tv/library/metadata/anidb-23-s1-e1")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual(ep["thumb"], "https://artworks.thetvdb.com/banners/episodes/76885/1.jpg")

    def test_theme_and_rating(self):
        svc = self.svc
        svc.cfg.update(theme_songs=True, tmdb_rating=True)
        orig_exists, orig_details = svc.fetcher.exists, svc.tmdb.details
        svc.fetcher.exists = lambda url, cache, ttl=0: url.endswith("/76885.mp3")
        svc.tmdb.details = lambda movie, tmdb: {"vote_average": 8.46, "vote_count": 100} if tmdb == "30991" else {}
        try:
            extras = svc._show_extras("tv", {"tvdbid": "76885", "mapping": svc.lists.get("23"), "lang": "", "fields": {}})
        finally:
            svc.cfg.update(theme_songs=False, tmdb_rating=False)
            svc.fetcher.exists, svc.tmdb.details = orig_exists, orig_details
        self.assertEqual(extras["theme"], "https://tvthemes.plexapp.com/76885.mp3")
        self.assertEqual(extras["Rating"], [{"image": "themoviedb://image.rating", "type": "audience", "value": 8.5}])

    def test_franchise_collection(self):
        svc = self.svc
        svc.lists.by_aid["219"] = dict(svc.lists.by_aid["23"], defaulttvdbseason="0")
        svc.lists.by_tvdb["76885"] = ["23", "219"]
        svc._models.clear()
        try:
            show = self.get("/tv/library/metadata/anidb-23")[1]["MediaContainer"]["Metadata"][0]
            self.assertEqual(show["Collection"], [{"guid": "tv.plex.agents.custom.hama.tv://collection/tvdbset-76885",
                                                   "key": "/library/collections/tvdbset-76885", "tag": "Cowboy Bebop Collection",
                                                   "summary": "Spike hunts bounties.", "thumb": "https://s4.anilist.co/cover/23.jpg"}])
            coll = self.get("/tv/library/collections/tvdbset-76885")[1]["MediaContainer"]["Metadata"][0]
            self.assertEqual((coll["type"], coll["title"]), ("collection", "Cowboy Bebop Collection"))
            self.assertEqual(self.get("/tv/library/collections/tvdbset-76885/children")[1]["MediaContainer"]["size"], 0)
            self.assertEqual(self.get("/tv/library/collections/bogus-1")[0], 404)
        finally:
            del svc.lists.by_aid["219"]
            svc.lists.by_tvdb["76885"] = ["23"]
            svc._models.clear()
        tvdb_show = self.get("/tvdb/library/metadata/tvdb-76885")[1]["MediaContainer"]["Metadata"][0]
        self.assertNotIn("Collection", tvdb_show)   # TVDB mode: the show already is the whole franchise

    def test_collection_sync(self):
        svc = self.svc
        svc.lists.by_aid["219"] = dict(svc.lists.by_aid["23"], defaulttvdbseason="0")
        svc.lists.by_tvdb["76885"] = ["23", "219"]
        saved_219 = svc.titledb.titles["219"]
        svc.titledb.titles["219"] = [("main", "x-jat", "Bebop Movie")]
        svc._models.clear()
        calls = []
        items = [{"ratingKey": "1", "title": "Bebop", "guid": "tv.plex.agents.custom.hama.tv://show/anidb-23"},
                 {"ratingKey": "2", "title": "Bebop Movie", "guid": "tv.plex.agents.custom.hama.tv://show/anidb-219"},
                 {"ratingKey": "3", "title": "Other", "guid": "plex://show/abc"}]

        def fake(method, path, params=None):
            calls.append((method, path, dict(params or {})))
            if path == "/library/sections":
                return {"MediaContainer": {"Directory": [{"key": "7", "title": "TV", "agent": "tv.plex.agents.custom.hama.tv"},
                                                         {"key": "8", "title": "Other", "agent": "tv.plex.agents.series"}]}}
            if path == "/library/sections/7/all" and method == "GET":
                return {"MediaContainer": {"Metadata": items}}
            if path == "/library/metadata/1":
                return {"MediaContainer": {"Metadata": [{"Collection": [{"tag": "My Favourites"}]}]}}
            if path.startswith("/library/metadata/"):
                return {"MediaContainer": {"Metadata": [{}]}}
            if path == "/library/sections/7/collections":
                return {"MediaContainer": {"Metadata": [{"ratingKey": "99", "title": "Cowboy Bebop Collection", "Field": []}]}}
            return {}

        svc.cfg.update(plex_url="http://plex:32400", collection_min_items=2)
        sync = PlexSync(svc)
        sync._request = fake
        try:
            stats = sync.run_once()
        finally:
            svc.cfg.update(plex_url="")
            del svc.lists.by_aid["219"]
            svc.titledb.titles["219"] = saved_219
            svc.lists.by_tvdb["76885"] = ["23"]
            svc._models.clear()
        self.assertEqual(stats, {"items": 2, "tagged": 2, "collections": 1})
        puts = [c for c in calls if c[0] == "PUT" and c[2].get("id") == "1"]
        self.assertEqual(puts[0][2]["collection[0].tag.tag"], "My Favourites")          # user's own collection kept
        self.assertEqual(puts[0][2]["collection[1].tag.tag"], "Cowboy Bebop Collection")
        self.assertFalse(any(c[1].startswith("/library/sections/8") for c in calls))      # non-HAMA libraries untouched
        self.assertIn(("POST", "/library/metadata/99/posters", {"url": "https://s4.anilist.co/cover/23.jpg"}), calls)

    def test_status_and_report(self):
        code, status = self.get("/")
        self.assertEqual((code, status["ready"], status["anime"]), (200, True, 2))
        self.get("/tv/library/metadata/anidb-219")                       # titles only: no AniDB XML, no mapping
        entry = dict(self.svc.issues.all())["anidb-219"]
        self.assertEqual(set(entry["issues"]), {"no_mapping", "anidb_unavailable", "no_summary", "no_poster", "no_logo"})
        self.assertIn("anime-lists/issues/new", entry["issues"]["no_mapping"]["link"])
        req = urllib.request.Request(self.base + "/report")
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        self.assertIn("Cowboy Bebop: Tengoku no Tobira", html)
        req = urllib.request.Request(self.base + "/", headers={"Accept": "text/html"})
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        self.assertIn("Provider URLs for Plex", html)
        self.assertIn("/tv-en", html)
        self.assertEqual(status["settings"]["plex_token"], "not set")
        self.assertEqual(status["profiles"]["tv-en"]["settings"], {"series_language_priority": ["en", "main"]})
        self.assertNotIn(config.DEFAULTS["tmdb_api_key"], json.dumps(status) + html)     # secrets never shown

    def test_profiles(self):
        code, body = self.get("/tv-en")
        self.assertEqual((code, body["MediaProvider"]["identifier"], body["MediaProvider"]["title"]),
                         (200, "tv.plex.agents.custom.hama.tv.en", "HAMA EN"))
        en = self.get("/tv-en/library/metadata/anidb-23")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual((en["title"], en["guid"]), ("Cowboy Bebop EN", "tv.plex.agents.custom.hama.tv.en://show/anidb-23"))
        base = self.get("/tv/library/metadata/anidb-23")[1]["MediaContainer"]["Metadata"][0]
        self.assertEqual((base["title"], base["guid"]), ("Cowboy Bebop", "tv.plex.agents.custom.hama.tv://show/anidb-23"))
        self.assertEqual(set(self.svc.profiles), {"tv-en"})                      # invalid profiles are skipped
        self.assertEqual(self.get("/x")[0], 404)

    def test_tmdb_movie_fallback(self):
        hit = self.match("movies", {"type": 1, "title": "The Matrix", "year": 1999})[0]
        self.assertEqual((hit["ratingKey"], hit["title"], hit["guid"]), ("tmdb-603", "The Matrix", "tv.plex.agents.custom.hama.movies://movie/tmdb-603"))
        self.assertEqual((hit["duration"], hit["studio"], hit["Role"][0]["thumb"], hit["Director"]),
                         (136 * 60000, "Warner Bros.", "https://image.tmdb.org/t/p/original/k.jpg", [{"tag": "Lana Wachowski"}]))
        self.assertIn({"id": "imdb://tt0133093"}, hit["Guid"])
        self.assertEqual(self.match("movies", {"type": 1, "title": "x", "filename": "Matrix [imdb-tt0133093]/m.mkv"})[0]["ratingKey"], "tmdb-603")
        self.assertEqual(self.get("/tv/library/metadata/tmdb-603")[0], 404)          # TMDB keys are movies-only
        saved = dict(self.svc.titledb.titles)
        self.svc.titledb.titles["777"] = [("main", "x-jat", "The Animatrix")]
        self.svc.titledb.index.append((cleanse_title("The Animatrix"), "777", "The Animatrix"))
        try:  # a fuzzy anime match must not beat an exact TMDB title
            self.assertEqual(self.match("movies", {"type": 1, "title": "The Matrix", "year": 1999})[0]["ratingKey"], "tmdb-603")
        finally:
            self.svc.titledb.titles = saved
            self.svc.titledb.index.pop()

    def test_review2_fixes(self):
        svc = self.svc
        # forget() really drops cached models (key layout includes the profile)
        self.get("/tv/library/metadata/anidb-23")
        self.assertTrue(any(k[2] == "anidb-23" for k in svc._models))
        svc.forget("anidb-23")
        self.assertFalse(any(k[2] == "anidb-23" for k in svc._models))
        # profiles: only per-request settings are taken
        self.assertNotIn("tmdb_api_key", svc.profiles["tv-en"]["cfg"] and
                         {k for k in ("tmdb_api_key",) if svc.profiles["tv-en"]["cfg"][k] == "ignored-per-profile"})
        # a TMDB movie hit that Anime-Lists maps to AniDB keeps AniDB metadata
        svc.lists.by_tmdb["603"] = "23"
        try:
            self.assertIn((99, "anidb-23"), svc._tmdb_movie_search("The Matrix", 1999))
        finally:
            del svc.lists.by_tmdb["603"]

    def test_include_adult(self):
        svc = self.svc
        orig = FakeAniDB.anime
        FakeAniDB.anime = lambda self, aid, fetch=True: dict(orig(self, aid, fetch) or {}, restricted=True) if aid == "23" else orig(self, aid, fetch)
        svc._models.clear()
        try:
            hits = self.match("tv", {"type": 2, "title": "Cowboy Bebop", "includeAdult": 0})
            self.assertEqual([h["ratingKey"] for h in hits], ["anidb-219"])             # adult entry skipped -> next candidate
            self.assertEqual(self.match("tv", {"type": 2, "title": "Cowboy Bebop", "includeAdult": 1})[0]["ratingKey"], "anidb-23")
            self.assertEqual(self.match("tv", {"type": 2, "title": "Cowboy Bebop"})[0]["ratingKey"], "anidb-219")  # not sent = not allowed
            self.assertEqual(self.match("tv", {"type": 2, "title": "x [anidb-23]", "includeAdult": 0})[0]["ratingKey"], "anidb-23")  # forced
        finally:
            FakeAniDB.anime = orig
            svc._models.clear()
        self.get("/tv/markers/123")                                                     # something Plex might ask for
        with urllib.request.urlopen(self.base + "/favicon.ico") as resp:
            self.assertEqual(resp.status, 204)
        svc.base_cfg["experimental_markers"] = True
        try:
            self.get("/tv/library/metadata/anidb-23?includeMarkers=1")                  # markers as a query option
        finally:
            svc.base_cfg["experimental_markers"] = False
        self.get("/wp-login.php")                                                       # not Plex: no event
        self.assertEqual(self.get("/health"), (200, {"status": "ok", "version": svc.status_version()}))
        kinds = [e["kind"] for e in svc.events.all()]
        self.assertIn("adult skipped", kinds)
        self.assertIn("unhandled request", kinds)
        self.assertIn("markers request", kinds)
        with urllib.request.urlopen(self.base + "/report") as resp:
            html = resp.read().decode()
        self.assertIn("Recent events", html)
        self.assertIn("/tv/markers/123", html)
        self.assertNotIn("wp-login", html)
        self.assertNotIn("favicon", html)

    def test_rescue_unmatched(self):
        calls = []

        def fake(method, path, params=None):
            calls.append((method, path))
            if path == "/library/sections":
                return {"MediaContainer": {"Directory": [{"key": "7", "title": "TV", "agent": "tv.plex.agents.custom.hama.tv"},
                                                         {"key": "8", "title": "Other", "agent": "tv.plex.agents.series"}]}}
            if path == "/library/sections/7/all":
                return {"MediaContainer": {"Metadata": [{"ratingKey": "1", "guid": "tv.plex.agents.custom.hama.tv://show/anidb-23"},
                                                        {"ratingKey": "2", "guid": "local://2", "title": "Juuden"}]}}
            return {}
        sync = PlexSync(self.svc)
        sync._request = fake
        self.assertEqual(sync.rescue_unmatched(), 1)
        self.assertIn(("PUT", "/library/metadata/2/refresh"), calls)
        self.assertNotIn(("PUT", "/library/metadata/1/refresh"), calls)
        self.assertFalse(any(p.startswith("/library/sections/8") for _, p in calls))

    def test_movie_in_tv_library(self):
        svc = self.svc
        movie_model = {"mode": "anidb", "tvdbid": "", "mapping": {"tvdbid": "movie", "tmdbid": "129", "tmdbtv": ""},
                       "anime": {"type": "Movie", "episodes": [{"season": 1, "index": 1}]}, "fields": {"title": "Sen to Chihiro"}}
        self.assertTrue(svc._uses_movie_art("tv", movie_model))                           # TMDB movie artwork/logo in TV
        self.assertFalse(svc._uses_movie_art("tv", {"mode": "anidb", "tvdbid": "76885", "mapping": {"tmdbtv": "30991"}}))
        self.assertEqual(svc._movie_episode_title(movie_model, {"season": 1, "index": 1}), "Sen to Chihiro")
        series = dict(movie_model, anime={"type": "TV Series", "episodes": [{"season": 1, "index": 1}, {"season": 1, "index": 2}]})
        self.assertEqual(svc._movie_episode_title(series, {"season": 1, "index": 1}), "")

    def test_match_paths(self):
        self.assertEqual(self.match("tv", {"type": 2, "title": "Cowboy Bebop"})[0]["ratingKey"], "anidb-23")
        self.assertEqual(self.match("tv", {"type": 2, "title": "x", "guid": "tvdb://76885"})[0]["ratingKey"], "anidb-23")
        self.assertEqual(self.match("tv", {"type": 3, "parentTitle": "Whatever", "index": 0,
                                           "filename": "Bebop [anidb-23]/Specials/x.mkv"})[0]["ratingKey"], "anidb-23-s0")
        ep = self.match("tv", {"type": 4, "grandparentTitle": "cowboy bebop", "parentIndex": 1, "index": 2})[0]
        self.assertEqual(ep["ratingKey"], "anidb-23-s1-e2")
        manual = self.match("tv", {"type": 2, "title": "Cowboy Bebop", "manual": 1})
        self.assertEqual([m["ratingKey"] for m in manual][:2], ["anidb-23", "anidb-219"])
        self.assertEqual(self.match("tv", {"type": 2, "title": "zzzz qqqq"}), [])
        # mode follows the provider; folder tags override it
        self.assertEqual(self.match("tvdb", {"type": 2, "title": "Cowboy Bebop"})[0]["ratingKey"], "tvdb-76885")
        self.assertEqual(self.match("tv", {"type": 3, "parentTitle": "x", "index": 1,
                                           "filename": "B [tvdb-76885]/Season 01/x.mkv"})[0]["ratingKey"], "tvdb-76885-s1")
        self.assertEqual(self.match("tvdb", {"type": 2, "title": "x [anidb-23]"})[0]["ratingKey"], "anidb-23")


if __name__ == "__main__":
    unittest.main()
