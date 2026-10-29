# HAMA – AniDB Metadata Provider for Plex

*Formerly HAMA, the HTTP AniDB Metadata Agent.*

Plex is retiring legacy `.bundle` agents such as `Hama.bundle` during 2026. They are being replaced by
**Custom Metadata Providers** (Plex Media Server 1.43+). This folder is HAMA rebuilt as one of those providers:
a small HTTP service that Plex calls for matching and metadata. **Coming from legacy HAMA? Read [MIGRATING.md](MIGRATING.md).**

Requirements: Python 3.8 or newer, with no other dependencies.

## What it does

- **HAMA's matching and titles.** It uses the AniDB title database, the folder tags `[anidb-12345]` / `[tvdb-12345]` / `[tmdb-…]` / `[imdb-tt…]`,
  and the Anime-Lists (ScudLee) AniDB↔TVDB/TMDB mapping. Title language priorities and specials numbering work as in HAMA
  (S=1+, OP/ED=101+/151+, trailers=201+, parodies=301+, others=401+).
- **Title logos (clearLogo).** These are the stylised titles the new Plex apps show instead of plain text, from fanart.tv and TheMovieDb.
- **Artwork.**
  - Show and movie posters: AniList (high resolution), AniDB, TheTVDB, fanart.tv and TheMovieDb.
  - Backgrounds and season posters.
  - Episode thumbnails from TheTVDB, or from TheMovieDb.
  - You can pick any of them in Plex's *Edit > Poster/Background/Logo*.
- **Theme songs** from Plex's TV theme library, and the **TMDB audience rating** with Plex's TMDB badge.
- **Episode orderings.** For the TVDB-seasons provider, set them per show in *Edit > Advanced > Episode ordering*:

  | Ordering | Files look like | Legacy HAMA mode |
  |---|---|---|
  | TheTVDB (Aired Order) | `Season 02/Show - S02E05.mkv` (default) | tvdb |
  | TheTVDB (Absolute Order) | `Season 01/Show - S01E250.mkv` | tvdb5 |
  | TheTVDB (Seasons, Absolute Episode Numbers) | `Season 02/Show - S02E033.mkv` | tvdb3 |
  | Story Arcs (Absolute Series Scanner list) | `Season 05/Show - S05E100.mkv`, seasons = story arcs; only for the ~50 shows in that list | tvdb4 |
  | TheMovieDb (Aired Order) | TMDB's season split | – |

  Story-arc names show as season titles once the library setting *Use season titles* is on.
- **Localised libraries.** For a non-English library you get:
  - show and episode summaries in the library's language (from TheMovieDb);
  - the content rating for the library's *Certification Country*, for example `de/16`;
  - production Country tags.
- **Resilience.**
  - If TheTVDB's legacy API stops answering, episode lists come from TheMovieDb (through Anime-Lists' TMDB mapping).
  - An unreachable source costs a single timeout. After that it is skipped for 10 minutes and the cache is used.
- **Background AniDB pre-fetch.** Long franchises are filled in slowly in the background. When the collection sync is set up,
  Plex is asked to refresh the show once everything is there.
- **Collections** (opt-in, see below): HAMA franchise collections and TheMovieDb movie collections.
- **Movies beyond anime.** Movies AniDB doesn't know (or only fuzzily) are matched through TheMovieDb, with cast, crew, logos and
  collections, so one movie library can hold anime and other films.
- **`prepare` tool for Absolute Series Scanner layouts** (see below): renames specials, splits grouping folders, turns `anidb.id`
  files into hints and renumbers `[tvdb2-…]` folders, with a preview first and an undo afterwards.
- **Profiles:** extra provider addresses with their own settings, e.g. `/tv-en` with English titles, for per-library settings.
- **Status page and missing-data report.** Open `http://127.0.0.1:32550/` in a browser to see the service status and settings.
  `/report` lists shows with missing mappings, summaries, posters or logos, with links to fix them on AniDB, Anime-Lists or fanart.tv.
  It also has **Recent events**: items Plex left unmatched, adult titles skipped, requests HAMA doesn't serve, sources down and AniDB bans.
- **Self-healing:** with `plex_url` set, items that Plex left unmatched are re-matched automatically. That happens, for example, when the
  provider was down or updating during a scan (Plex never retries those by itself).
- **Adult titles:** AniDB 18+ entries are only matched when the library allows it (Plex: *Advanced > Allow matching to explicit content*);
  folder tags always win.

## Two TV providers: pick the one that matches your folders

| Provider (URL) | Use it when your folders look like | Each show is |
|---|---|---|
| **HAMA – Anime (AniDB numbering)** (`/tv`) | one folder per AniDB entry, episodes numbered from 1 (`Show - 01.mkv` or `S01E01`) | one AniDB entry: season 1 = episodes, season 0 = specials |
| **HAMA – Anime (TVDB seasons)** (`/tvdb`) | one folder per series with `Season 01`, `Season 02`… (Plex's normal TV layout) | one TVDB series. Every AniDB entry (sequels, cours, OVAs, movies) is mapped into its TVDB season and episode. Titles come from AniDB, summaries from TVDB (HAMA's defaults) |

These match HAMA's "anidb" and "tvdb" modes. A folder tag always overrides the provider: `[anidb-12345]` means AniDB numbering,
`[tvdb-12345]` means TVDB seasons. The TVDB-seasons provider also matches shows AniDB doesn't know (non-anime) through TheMovieDb and
TheTVDB, so a mixed library works too. New-style agents must use Plex's own **Plex TV Series / Plex Movie** scanners. Plex refuses
legacy scanners such as the Absolute Series Scanner with them.

What Plex's scanner does with typical anime files (tested on PMS 1.43.5):
- **Handled:** fansub names (`[Group] Show - 05 (1080p) [ABCD1234].mkv`), `v2` versions, multi-episode files (`07-08`), 3-digit absolute
  numbers (`Show - 101.mkv` stays episode 101), `SP01` (→ special 1), and `Season NN` / `Specials` folders.
- **Not episodes:** files named `NCOP1`, `ED1`, `S1` or `OVA` are either skipped or imported as unnamed show *extras*, not as specials.
  Rename them, e.g. `Show - S00E101.mkv` for the first creditless opening (HAMA numbers: OP 101+, ED 151+, trailers 201+, others 401+).
- **Grouping folders** (`Show/[2] Sequel/…`) are treated as one show; the sequel's episodes collapse onto the first one. Give each AniDB
  entry its own top-level folder (AniDB numbering) or use `Season NN` folders (TVDB seasons).
- **`.plexmatch`:** `title: Show [anidb-123]` works (it reaches the provider). `ep: s0e201: file.mkv` lines work for files Plex sees as
  episodes. `guid:`, `tvdbid:` and similar hints are **not** passed to custom providers.

The `prepare` tool below fixes the first two points (and more) for you.

## Anime movies in a TV library

Plex only combines providers that supply the same kind of items, so the *HAMA – Anime Movies* provider can't be added to a TV agent.
You don't need it there. On AniDB a movie is an entry with one episode, so the TV provider (AniDB numbering) shows it as a
one-episode show with the movie's title, summary, poster, **logo** and background (from its TMDB movie entry). Tested on PMS 1.43.5.

The catch is Plex's TV scanner: it **skips** a movie file without an episode number (`Movie (2016).mkv`, `Movie - Complete Movie.mkv`).
Name it `Movie - 01.mkv`, or let `prepare` do it (a folder's only video becomes `<folder> - 01.mkv`).
A separate Movie library with the *HAMA – Anime Movies* agent gives the full movie experience: movie-style pages, TMDB collections
and non-anime films.

## Preparing Absolute Series Scanner libraries (`prepare`)

`prepare` looks at a library folder and lists what to change so that Plex's own scanner reads it the way ASS did:

| Found | Change |
|---|---|
| `anidb.id` / `tvdb.id` / `tmdb.id` (also in `Extras/`) | `.plexmatch` gets `title: Show [anidb-123]` (your own lines are kept) |
| `NCOP1`, `NCED2`, `OP`, `ED1`, `PV`, `CM`, `Trailer`, `SP03`, `S1`, `OVA`… | renamed `Show - S00E101.mkv`, `S00E152`, `S00E201`, `S00E003`… (HAMA's special numbers); subtitles with the same name follow |
| Grouping folders `Show/[2] Sequel [anidb-4747]/` | moved to the library root as their own show |
| ASS offset folders `Arc 2 -s2e12` | files renamed with the offset applied (`- 01` → `S02E13`) |
| `[tvdb2-79824]` folders with absolute files | renamed to TVDB season/episode (`- 250` → `S12E08`), from TheTVDB |
| `[anidb2-123]` folders | the AniDB entry's absolute numbers mapped into TVDB seasons (Anime-Lists) |
| A folder's only video without an episode number (a movie: `Movie (2016).mkv`, `… - Complete Movie.mkv`) | renamed `<folder> - 01.mkv`, so the TV scanner picks it up |
| Other video files without any episode number | listed as warnings, so you can rename them yourself |

```
python3 run.py --prepare /path/to/anime            # preview only
python3 run.py --prepare /path/to/anime --apply    # do it (never overwrites; logged)
python3 run.py --undo /path/to/anime               # put everything back
```
In Docker: `docker compose exec hama-provider python -m hama_provider --prepare /media`. Set `MEDIA_MODE=rw` in `.env` before `--apply`.
Changes are logged in `<library>/.hama-prepare-undo.json`. Run it again after `--apply`: moved grouping folders are then checked as shows
of their own. Afterwards let Plex scan the library (or *Refresh All Metadata*).

## 1. Run the provider

| Platform | How |
|---|---|
| Linux (systemd) | `sudo sh install/linux/install.sh`. Runs as user `plex` when that user exists. Remove with `sudo sh install/linux/install.sh uninstall`. |
| Docker / NAS | See *Docker* below. |
| Windows | Install Python from python.org and tick *Add to PATH*. Then in an **admin** PowerShell: `powershell -ExecutionPolicy Bypass -File install\windows\install.ps1`. This adds a startup task; remove it with `-Uninstall`. To run it once by hand: `install\windows\start-hama-provider.cmd` |
| Anything else | `python3 run.py` |

Open `http://127.0.0.1:32550/` in a browser to check that it's running. For monitoring tools: `GET /health` returns
`200 {"status": "ok"}` when ready and `503` while it's still loading (the Docker image's healthcheck uses it). That page also shows the effective settings, with keys and the token masked.

The provider must be reachable **from the Plex server**. By default it listens on `127.0.0.1`, which only works when Plex runs on the
same machine. If Plex is elsewhere, set `"host": "0.0.0.0"`. The Docker image already does this.

### Docker

**From the image (recommended).** The image is published at
[ghcr.io/xesurient/hama.provider](https://github.com/xesurient/hama.provider/pkgs/container/hama.provider) for amd64, arm64 and arm/v7.
Copy `provider/install/docker/compose.yml` and `.env.example` anywhere (they don't need the rest of the repository), then:
```
cp .env.example .env               # git-ignored: your paths, PUID/PGID, Plex token, API keys, profiles
mkdir -p data && sudo chown 1000:1000 data      # use your PUID:PGID
docker compose pull && docker compose up -d
```
`HAMA_IMAGE` in `.env` defaults to `ghcr.io/xesurient/hama.provider:latest`; pin a version such as `:1.2.8` if you prefer.
A plain `docker pull ghcr.io/xesurient/hama.provider:latest` works too. Published GitHub releases also carry
`hama-provider-docker-<version>.zip` with these files.

**From source** (inside the repository):
```
cd provider/install/docker
cp .env.example .env               # set HAMA_IMAGE=hama-provider:local
docker compose -f compose.yml -f compose.build.yml up -d --build
```
`sh build.sh` builds the same image on its own (`--platform linux/amd64` for another CPU, `--save` writes a `docker load`-able tar to `dist/`).

Needs Docker Compose 2.24 or newer for the optional `.env` (`docker compose version`). On older versions, create `.env` first
(it can be empty).
- `.env` holds everything personal. It is git-ignored (as are `data/` and `config.json`), so the repository stays free of secrets.
- `PUID`/`PGID` should own your media files (for `prepare --apply`) and the data folder.
- `MEDIA_DIR` is mounted at `/media`, read-only unless `MEDIA_MODE=rw`.
- Plex URLs: from a Plex container on the same Docker network, `http://hama-provider:32550/tv`. Plex on the host: `http://<host-ip>:32550/tv`.
- Collection sync from a separate container needs `HAMA_PLEX_TOKEN`. To avoid the token, use *Option B* in `compose.yml`
  (`network_mode: "container:plex"`): the provider then shares Plex's network, Plex reaches it at `http://127.0.0.1:32550/tv`, and
  `HAMA_PLEX_URL=http://127.0.0.1:32400` works without a token.
- Updating: `docker compose pull && docker compose up -d` (from source: add `-f compose.build.yml` and `--build`). Items Plex scans while the container is down stay unmatched until the provider is back;
  with `HAMA_PLEX_URL` set they are re-matched automatically within a few minutes, and are listed on `/report` either way.

**Security.** Plex can't send credentials to custom providers yet, so the provider answers anyone who can reach port 32550. The status page
shows no keys or tokens, but don't expose the port to the internet. Keep it on your LAN or Docker network, or use *Option B*, which publishes no port.

## 2. Add it to Plex

1. Plex Web: *Settings > Metadata Agents > Add Provider*. Add `http://127.0.0.1:32550/tv`, `…/tvdb` and/or `…/movies`, whichever you need.
   If the provider runs in Docker next to a Plex container, use the container name or the host's IP instead of 127.0.0.1.
2. *Add Agent*. Choose a HAMA provider as the primary one and add **Plex Local Media** to it. Make one agent per provider you use.
3. Create a library (or edit one) and, under *Advanced*, pick the HAMA agent and the *Plex TV Series* or *Plex Movie* scanner.

Tips:
- Folder tags such as `Show Name [anidb-4880]` always win and skip the title search.
- If you can't rename a folder, put `title: Show Name [anidb-4880]` in its `.plexmatch` file. PMS passes that title to the provider,
  but it does **not** pass `tvdbid:`/`tmdbid:` hints from `.plexmatch` to custom providers.
- Want Plex's own metadata as well? Add *Plex Series* or *Plex Movie* to the same agent **below** HAMA. Plex then uses it only to fill gaps.
- Your own artwork wins: a `poster.jpg/png`, `clearlogo.png` (or `logo.png`), `background.jpg` etc. in the show folder is picked over HAMA's
  choices, with Plex Local Media in the agent as above (tested). HAMA's images stay selectable under *Edit*.

## Profiles: different settings per library

Plex doesn't pass settings to providers, so each settings variant gets its own provider address. In `config.json`, or
`HAMA_PROFILES` in Docker's `.env`:

```json
{ "profiles": { "tv-en": { "base": "tv", "title": "HAMA – Anime (English titles)",
                           "series_language_priority": ["en", "main"], "episode_language_priority": ["en", "main"] } } }
```
Then add `http://<host>:32550/tv-en` in Plex as another provider and agent, and use that agent for the library. `base` is `tv`, `tvdb` or
`movies`. A profile can change the settings used while answering Plex: title languages, image sources and languages, localisation,
theme songs, ratings, collections, genres and `match_min_score`. API keys, the AniDB client, cache, pre-fetch and the Plex sync apply to the
whole provider. Names like `report` are reserved. Each profile has its own identifier (`tv.plex.agents.custom.hama.tv.en`),
so switching a library between profiles re-matches it. The status page lists profiles and their settings.

## Collections (optional)

PMS deliberately drops collections sent by custom providers: it asks for metadata with `excludeElements=Collection`.
HAMA can add them through Plex's own API instead, as ShokoRelay does. Give it your Plex address and, unless the provider runs on the
Plex machine itself, a token:

```json
{ "plex_url": "http://127.0.0.1:32400", "plex_token": "your-token" }
```

To find the token, open Plex Web, pick any item, choose *Get Info > View XML*, and copy `X-Plex-Token=...` from the address bar.
Plex allows token-less API access only from its own machine. Keep `config.json` private, because the token gives full access to your server.

The sync runs every `collection_sync_hours` (default 12), or once by hand with `python3 run.py --sync-collections`. It:
- only touches libraries that use a HAMA agent, and only the items HAMA matched;
- adds collection tags while keeping the collections you made yourself;
- creates a collection only when at least `collection_min_items` (default 2) of its items are in that library;
- sets the poster, background and summary when a collection gains members, leaving fields you've locked in Plex alone;
- never deletes collections. Remove old ones in Plex yourself.

Movies use their TheMovieDb collection when they have one, and the franchise collection otherwise. The franchise collection is every
AniDB entry of one TVDB series, for example "Shingeki no Kyojin Collection". TVDB-seasons shows don't get franchise collections, because each show already is the franchise.
The same `plex_url` lets the background pre-fetch ask Plex to refresh a show when its AniDB data is complete.

## 3. Settings

Copy `config.example.json` to `config.json`, either next to `run.py` or in the data folder, and change what you need.
Every setting can also be set as an environment variable `HAMA_<NAME>`, for example `HAMA_FANART_API_KEY=...`.
The settings live here, not in Plex: PMS 1.43 asks providers for preferences but ignores the answer (tested with several formats).

| Setting | Default | Meaning |
|---|---|---|
| `series_language_priority` | `main, en, ja` | Show title language order. `main` is AniDB's main (romaji) title. Same as HAMA's *Serie Language Priority*. |
| `episode_language_priority` | `main, en, ja` | Episode title language order. |
| `use_request_language` | `false` | Put the library's language first in both lists. |
| `original_title_language` | `ja` | Language used for *Original Title*. |
| `localize_summaries` | `true` | Non-English libraries: summaries from TheMovieDb in the library language. |
| `logo_language_priority` | `en, ja, ""` | Preferred logo language. `""` means textless. The library language is always tried first. |
| `poster_sources` | `AniList, AniDB, TheTVDB, FanartTV, TheMovieDb` | Poster source order. Remove a source to disable it. |
| `background_sources` / `logo_sources` | `FanartTV, TheTVDB, TheMovieDb` / `FanartTV, TheMovieDb` | Same, for backgrounds and logos. |
| `season_poster_sources` | `AniList, AniDB, TheTVDB, FanartTV` | Season posters (TVDB seasons). AniList/AniDB = the cover of the AniDB entry that starts that season. |
| `fanart_api_key`, `tmdb_api_key`, `tvdb_api_key` | HAMA's shared keys | Your own keys avoid shared rate limits. fanart.tv: a free *personal* key is enough. |
| `anidb_client`, `anidb_clientver` | `hama`, `1` | AniDB HTTP client name/version; HAMA's by default (see *AniDB rules* below). |
| `anidb_titles_source` | `mirror` | AniDB title list: `mirror` = the Anime-Lists GitHub copy, falling back to AniDB itself when the mirror is down or a search finds nothing (max once a day, never during a ban); `anidb` = AniDB only (max once a day). |
| `rescue_unmatched` | `true` | With `plex_url`: re-match items Plex left unmatched (checked 2 min after start, then every 6 h). |
| `experimental_markers` | `false` | Research only: advertise a "markers" feature and log what Plex asks (see *Current limits*). |
| `genres_add_weights`, `genre_minimum_weight` | `false`, `400` | Same as HAMA's AniDB genre options. |
| `theme_songs`, `tmdb_rating` | `true`, `true` | Plex theme songs and the TMDB rating. AniDB's own score can't be shown, because Plex only has badges for IMDb, TMDB and Rotten Tomatoes. |
| `collections` | `true` | Work out franchise and TMDB collections. They only reach Plex through the collection sync. |
| `plex_url`, `plex_token` | empty | Turns on the collection sync and the Plex refresh after a pre-fetch. |
| `collection_sync_hours`, `collection_min_items` | `12`, `2` | Sync interval (0 = only by hand) and minimum collection size. |
| `anidb_prefetch_seconds` | `300` | Seconds between background AniDB fetches (0 = off). |
| `anidb_daily_limit` | `150` | Most AniDB API requests in any 24 h (0 = no limit). The pre-fetch uses up to 80 % of it; the rest is kept for Plex requests. |
| `cache_max_age_days` | `90` | Delete cached API answers that haven't been refreshed for this long. The AniDB XML is always kept. |
| `match_min_score` | `50` | Lowest title-search score accepted for automatic matches. |
| `host`, `port`, `data_dir`, `log_level`, `log_file` | `127.0.0.1`, `32550`, `./data`, `INFO`, none | Server settings. |

Put an `anime-list-custom.xml` in the data folder to override Anime-Lists entries. It uses the same format as `anime-list-master.xml`.

**AniDB rules.**
- **Client name.** The provider identifies itself to AniDB with HAMA's client name (`hama`). AniDB's rules ask applications to use
  their own registered client name. If you want to, register an HTTP client on anidb.net (*Settings > Client*) and set
  `anidb_client` / `anidb_clientver`.
- **Request rate.** AniDB bans IPs that request more than one page every 2 seconds, or the same anime repeatedly on one day.
  Requests made during a ban extend it. The provider waits 4 s between AniDB requests and caches each anime for 6 days, or up to a year
  for shows that ended long ago. The title list comes from a GitHub mirror, so restarts and new data folders don't re-download it from AniDB.
  The mirror can lag behind brand-new anime: when a title isn't found, the list is fetched once from AniDB itself (at most once a day,
  never during a ban; logged on `/report`). A folder tag `[anidb-12345]` always works, whatever the list contains.
- **If you get banned** (status page: *AniDB API: banned*):
  - The provider uses only its cache until the shown time. That's 12 h for the first ban, doubling on each further ban up to 48 h, and it
    is remembered across restarts.
  - Don't restart or re-create the container to "fix" it.
  - Check `/report` for the ban event and for *legacy HAMA* events: libraries still on the old agent query AniDB from the same IP.
  - Also check other AniDB tools on your network, and that the data folder is writable, so the cache persists.
- **Scan speed.** One Plex request fetches at most 3 uncached AniDB entries; the background pre-fetch does the rest at one entry per
  `anidb_prefetch_seconds`, within `anidb_daily_limit` (150 requests per 24 h by default). AniDB also bans for sustained fetching:
  about 250 requests in 2 hours did it. The first scan of a large library is therefore slow (about a week per 1,000 AniDB entries);
  after that only new and still-airing shows need requests. Let it finish, and raise the limit only if you know your IP tolerates it.
  The status page shows the requests made in the last 24 h.

## Current limits

- HAMA's `tvdb2`, `tvdb6`, `anidb3` and `anidb4` modes aren't available. They relied on the Absolute Series Scanner renumbering files, or on AniDB-relation reordering, and Plex no longer allows that scanner with new agents.
  `[tvdb2-…]`-style tags are treated as `[tvdb-…]`.
- Non-anime TV shows need the TVDB-seasons provider (the AniDB-numbering provider only knows AniDB entries).
- Plex keeps a provider's name and types from when it was added. After an update that changes them, remove and re-add the provider.
- No trailers or other extras: Plex staff confirmed custom providers can't supply them. No reviews, and no settings inside Plex
  (not implemented by Plex yet).
- Intro/credits markers: PMS has code to fetch markers from a provider, but it never asked our provider on a test server without
  Plex Pass. If you have Plex Pass, set `experimental_markers` / `HAMA_EXPERIMENTAL_MARKERS=1`, scan a show, and check `/report` for
  "unhandled request … markers". If it shows up, anime intro/credit skipping (via AniSkip) is possible. Please report it.
- Theme songs are Plex's TV themes. AnimeThemes only offers Ogg audio, and Plex needs MP3.
- TheTVDB's legacy API (used with HAMA's key) is no longer officially supported. If it goes away, episodes come from TheMovieDb automatically.

## Releases (maintainers)

Releases are automatic: bump `__version__` in `hama_provider/__init__.py`, commit and push to `main`. When no tag `v<version>` exists
yet, `.github/workflows/release.yml`:
- runs the tests on Python 3.8 and 3.12;
- builds the image for linux/amd64, arm64 and arm/v7 and pushes it to `ghcr.io/xesurient/hama.provider:<version>`, `:<major.minor>` and `:latest`;
- creates the GitHub release `v<version>` (notes generated from the commits) with `hama-provider-docker-<version>.zip`,
  `hama-provider-<version>.zip` and `SHA256SUMS`.

A push without a version bump only runs the tests (the Actions log says the version is already released), so a published image is
never replaced. Pushes that only change `.md` files don't run the workflow. A release published by hand with tag `v<version>` still works.
After the first release, make the package public once on GitHub (*your profile > Packages > the package > Package settings >
Change visibility*). Otherwise `docker pull` needs a login.

The assets can be built locally with `python3 install/release/make_assets.py --image ghcr.io/xesurient/hama.provider`, and the image with
`install/docker/build.sh`. Running the workflow by hand (*Actions > Release > Run workflow*) can push an image without making a release.

## Development

```
python3 -m unittest discover -s tests     # offline tests
python3 run.py --port 32550               # run in the foreground; logs go to stdout
```
The endpoints follow Plex's spec (https://developer.plex.tv/pms/#section/API-Info/Metadata-Providers):
`GET /tv`, `POST /tv/library/metadata/matches`, `GET /tv/library/metadata/{ratingKey}[/children|/grandchildren|/images|/extras]`,
`GET /tv/library/collections/{key}`, and the same under `/tvdb` and `/movies`. Rating keys look like `anidb-23-s1-e5` or `tvdb-76885-s1-e5`.
Episode orderings add a marker to the key, for example `tvdb-79824-a-s1-e250` (`a` absolute, `t3`, `t4` arcs, `m` TMDB).
PMS sends the ordering as `episodeOrder` (the SeasonType id, sometimes prefixed with its source, e.g. `tvdbAbsolute`).
