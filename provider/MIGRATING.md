# Moving from legacy HAMA (Hama.bundle) to the HAMA provider

Tested with Plex Media Server 1.43.5. A library that used legacy HAMA with the Absolute Series Scanner was switched in place, and it
**kept watched states and playback positions** (Plex keeps the same items and only re-matches them).

## Step by step

1. Install and start the provider, then add it in Plex (see [README.md](README.md), steps 1–2). Run `prepare` on the library (preview,
   then `--apply`; see the table below). Then pick the provider that fits the library:
   - folders per AniDB entry, episodes numbered from 1 (the usual HAMA/ASS layout) → **HAMA – Anime (AniDB numbering)**;
   - `Season 01`, `Season 02`… folders per series → **HAMA – Anime (TVDB seasons)**.
2. *Manage Library > Edit > Advanced*. Set **Agent** to the HAMA agent you created and **Scanner** to **Plex TV Series**
   (or *Plex Movie* for movie libraries). Plex refuses the Absolute Series Scanner with new agents ("new scanner needs to be paired with new agent").
3. Save, then *Refresh All Metadata*. Every item is re-matched. Folder tags like `[anidb-23]` still work, and so does the ASS
   file naming (`Show - 01.mkv`) in Plex's own scanner.
4. Optional:
   - add `plex_url` (and `plex_token` if needed) to get collections back;
   - turn on *Use season titles* in the library to see story-arc names;
   - set *Episode ordering* on long shows (see below).
5. Once nothing uses it any more, you can remove `Plug-ins/Hama.bundle` and `Scanners/Series/Absolute Series Scanner.py`.

What changes for a migrated item: the GUID becomes, for example, `tv.plex.agents.custom.hama.tv://show/anidb-23`
(was `com.plexapp.agents.hama://anidb-23`). Plex keeps the same library items, so watched states and playback positions stay (tested).
Item-level extras such as your own ratings, labels and playlists should stay for the same reason, but weren't tested.

## What changes without the Absolute Series Scanner, and what `prepare` does about it

Plex's own scanner now decides the season and episode numbers. Run `prepare` (see README) on the library before switching. It previews
first and can undo everything:

| ASS feature | With the Plex TV Series scanner | `prepare` |
|---|---|---|
| `NCOP1`, `ED1`, `OP`, `PV`, `S1`, `OVA` files become specials (101+, 151+, …) | Skipped or imported as unnamed extras | Renames them to `Show - S00E101.mkv` etc. (tested: imported as specials with AniDB titles) |
| Grouping folders (`Show/[1] Show/`, `Show/[2] Sequel/`) | Collapse into one show | Moves each to the library root as its own show |
| `anidb.id` / `tvdb.id` files | Not read | Writes `.plexmatch` `title: Show [anidb-123]` |
| Folder offsets `-s2`, `-e12`, `-s2e12` | Not supported | Renames the files with the offset applied |
| tvdb2 / anidb2 (absolute files shown in TVDB seasons) | Not possible | Renames absolute files to TVDB `SxxEyy` (TheTVDB / Anime-Lists) |
| tvdb3 / tvdb4 / tvdb5 | — | Not needed: per-show *Episode ordering* in the TVDB-seasons provider |
| tvdb6, anidb3/4 (relation-based reordering) | Not possible | — |
| Unrecognised files as specials 501+ | Not imported | Listed as warnings to rename |
| ASS logs (`filelist.log` …) | — | Plex's own logs + the provider's `/report` page |

## Legacy HAMA vs the provider

| Legacy HAMA | HAMA provider |
|---|---|
| Runs inside PMS (Python 2 plug-in) and is configured in Plex's agent settings | A separate small service. Settings live in `config.json` or `HAMA_*` env vars, because Plex ignores provider preferences for now |
| Per-field source priorities (title, summary, posters… per source) | Sensible fixed rules (AniDB titles, TVDB/AniDB summaries, …) plus source-order lists for images |
| Modes chosen by folder tag + ASS: anidb, anidb2–4, tvdb, tvdb2–6 | AniDB numbering (`/tv`) and TVDB seasons (`/tvdb`), plus per-show *Episode ordering* for tvdb3, tvdb4 and tvdb5. tvdb2/tvdb6/anidb3/anidb4 needed ASS renumbering and aren't possible |
| Absolute Series Scanner | Plex TV Series scanner (ASS can't be paired with new agents) |
| Posters: AniDB, TVDB, TMDB, fanart.tv, AniList, tvdb4 | The same, plus **title logos** (clearLogo) and episode thumbnails, with the high-resolution AniList covers first |
| Themes from Plex + TVTunes | Plex theme library |
| Ratings from AniDB/TVDB/MAL/OMDb | TMDB audience rating (the only anime-relevant badge Plex accepts) |
| Collections from the movie-set list (now empty upstream) | Franchise + TMDB collections through the Plex API (opt-in) |
| HTML error reports per library | `/report` page (missing data + recent events such as unmatched items) |
| Agent setting "Include adult content" | Plex library setting *Allow matching to explicit content* (AniDB 18+ entries are skipped without it) |
| TVDB legacy API only | TVDB, with automatic TheMovieDb fallback |
| English summaries | Library-language summaries, country ratings and Country tags for non-English libraries |

## Old HAMA settings → new settings

| Legacy agent setting | New setting |
|---|---|
| Serie Language Priority | `series_language_priority` |
| Episode Language Priority | `episode_language_priority` |
| TheTVDB Poster Language Priority | `logo_language_priority` (logos); posters follow the library language |
| AniDB include weighted genres / minimum weight | `genres_add_weights` / `genre_minimum_weight` |
| OMDb / MyAnimeList keys | not used |
| Per-field source lists (`title`, `summary`, `posters`, …) | `poster_sources`, `background_sources`, `logo_sources`, `season_poster_sources`; texts follow HAMA's defaults |
| `anime-list-custom.xml` in the media folder | `anime-list-custom.xml` in the provider's data folder (the provider can't see your media folders) |
