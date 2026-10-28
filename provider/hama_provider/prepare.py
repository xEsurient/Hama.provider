"""'prepare': make Absolute-Series-Scanner-style folders work with Plex's own scanner.

New-style agents must use the Plex TV Series scanner, which knows nothing about ASS conventions. This walks a library folder and
plans the changes that keep HAMA numbering working:
  - anidb.id / tvdb.id / tmdb.id files     -> .plexmatch "title: Name [anidb-123]" (the only hint that reaches the provider)
  - NCOP/NCED/OP/ED/PV/CM/Trailer/SP/OVA   -> renamed "Show - S00E101.mkv" (Plex imports those names as extras, not episodes)
  - grouping folders (Show/[2] Sequel/)     -> moved to the library root as their own show
  - ASS folder offsets ("Folder -s2e12")   -> files renamed to SxxEyy
  - [tvdb2-…] / [anidb2-…] folders          -> absolute files renamed to TVDB season/episode numbers
Nothing changes unless apply=True. Every rename/move and .plexmatch write is logged to <library>/.hama-prepare-undo.json for undo()."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("hama.prepare")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".ogm", ".flv", ".mpg", ".mpeg"}
EXTRAS_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts", "trailers", "other"}
SEASON_DIR_RE = re.compile(r"^(season|series|saison|staffel|temporada|book|s)[ ._-]*\d+\b|^specials?$|^season 0+$", re.IGNORECASE)
TAG_RE = re.compile(r"[\[{](anidb|tvdb|tmdb|imdb)([2-9]?)-(tt\d+|\d+)[\]}]", re.IGNORECASE)
ID_FILE_RE = re.compile(r"^(anidb|tvdb|tmdb|imdb)([2-9]?)\.id$", re.IGNORECASE)
OFFSET_RE = re.compile(r"\s+-s(\d+)(?:e(-?\d+))?\s*$|\s+-e(-?\d+)\s*$", re.IGNORECASE)
GROUP_PREFIX_RE = re.compile(r"^\s*[\[(]\d+[\])]\s*")
UNDO_FILE = ".hama-prepare-undo.json"
BLOCK_BEGIN, BLOCK_END = "# --- HAMA prepare (generated) ---", "# --- end HAMA prepare ---"

# HAMA/ASS special numbering (season 0): S 1-100, OP 101-150, ED 151-200, trailers/PV/CM 201-300, others 401-500.
# A pattern must match the WHOLE end of the name (after the last ' - ', or the last word), so episode titles such as
# 'Show - 05 - The Ending' or 'Ed's Mission' never turn an episode into a special.
SPECIAL_PATTERNS = [
    (re.compile(r"^(?:NC)?OP(?:ening)?\s*0*(\d{1,2})?(?:v\d+)?$", re.IGNORECASE), 100),
    (re.compile(r"^(?:NC)?(?:ED|Ending)\s*0*(\d{1,2})?(?:v\d+)?$", re.IGNORECASE), 150),
    (re.compile(r"^(?:PV|CM|Trailer|Teaser|Promo|Preview)\s*0*(\d{1,2})?(?:v\d+)?$", re.IGNORECASE), 200),
    (re.compile(r"^(?:SP|Special|OVA|OAD|S)\s*0*(\d{1,3})(?:v\d+)?$", re.IGNORECASE), 0),
    (re.compile(r"^(?:OVA|OAD|Special)$", re.IGNORECASE), 0),
]
SPECIAL_DIR_RE = re.compile(r"^(sps?|specials?|ovas?|oads?)$", re.IGNORECASE)  # BD-rip style specials folders
GROUP_DIR_RE = re.compile(r"^\s*[\[(]\d+[\])]")                              # ASS grouping folder: "[2] Sequel"


@dataclass
class Action:
    kind: str            # rename | move | plexmatch | warn
    path: str
    target: str = ""
    reason: str = ""
    content: str = ""


@dataclass
class Plan:
    root: str
    actions: list = field(default_factory=list)

    def add(self, kind, path, target="", reason="", content=""):
        self.actions.append(Action(kind, path, target, reason, content))

    def text(self) -> str:
        if not self.actions:
            return "Nothing to change in %s" % self.root
        rel = lambda p: os.path.relpath(p, self.root) if p else ""
        lines = []
        for a in self.actions:
            if a.kind in ("rename", "move"):
                lines.append("%-9s %s\n          -> %s   (%s)" % (a.kind.upper(), rel(a.path), rel(a.target), a.reason))
            elif a.kind == "plexmatch":
                lines.append("PLEXMATCH %s   (%s)\n          %s" % (rel(a.path), a.reason, a.content.strip().replace("\n", "\n          ")))
            else:
                lines.append("WARN      %s: %s" % (rel(a.path), a.reason))
        return "\n".join(lines)


def clean_title(name: str) -> str:
    """Folder name without id tags, ASS offsets and grouping prefixes."""
    name = TAG_RE.sub("", name)
    name = OFFSET_RE.sub("", name)
    name = GROUP_PREFIX_RE.sub("", name)
    return " ".join(name.split())


def _core(stem: str) -> str:
    """Filename stem without [group]/[hash]/(1080p) chunks and separators."""
    stem = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", stem)
    return " ".join(stem.replace("_", " ").replace(".", " ").split())


def episode_number(stem: str):
    """(season, episode) when explicit SxxEyy, (None, n) for absolute numbers, else None.
    Fractional numbers ('Show - 05.5', recaps) return None: they are no regular episode."""
    if re.search(r"(?:^| - |\b[Ee][Pp]?\s*)\d{1,4}\.\d(?!\d)", re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)):
        return None
    core = _core(stem)
    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,4})\b", core, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    for pattern in (r" - (\d{1,4})(?:v\d+)?\b", r"\b(?:E|EP|Episode)\s*(\d{1,4})(?:v\d+)?\b", r"\b(\d{1,4})(?:v\d+)?\s*$"):
        m = re.search(pattern, core, re.IGNORECASE)
        if m:
            return None, int(m.group(1))
    return None


def special_number(stem: str):
    """HAMA season-0 number for ASS special names (NCOP1 -> 101, ED2 -> 152, PV -> 201, SP03 -> 3), else None.
    Only a name that ENDS in a special token counts: the text after the last ' - ', else the last word.
    A ' - NN' episode number anywhere wins ('Show - 05 - The Ending' stays episode 5)."""
    core = _core(stem)
    if re.search(r"\bS\d{1,2}\s*E\d{1,4}\b", core, re.IGNORECASE) or re.search(r" - \d{1,4}(?:v\d+)?\b", core):
        return None
    candidates = [core.rsplit(" - ", 1)[-1]] if " - " in core else []
    words = core.split()
    candidates += [" ".join(words[-2:]), words[-1]] if words else []
    for tail in candidates:
        for pattern, base in SPECIAL_PATTERNS:
            m = pattern.match(tail.strip())
            if m:
                n = next((int(g) for g in m.groups() if g), 1)
                return base + n if base or n else None  # SP00 / S0: no valid special number
    return None


def _videos(folder: str):
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXTRAS_DIRS and not d.startswith("."))
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in VIDEO_EXT:
                yield os.path.join(dirpath, name)


def _sidecars(video: str):
    """Subtitles/nfo next to a video that share its name (Show - 01.en.srt)."""
    folder, name = os.path.split(video)
    stem = os.path.splitext(name)[0]
    for other in sorted(os.listdir(folder)):
        if other != name and other.startswith(stem + ".") and os.path.splitext(other)[1].lower() not in VIDEO_EXT:
            yield os.path.join(folder, other), other[len(stem):]


def _read_id_file(folder: str):
    for where in (folder, os.path.join(folder, "Extras")):
        if not os.path.isdir(where):
            continue
        for name in os.listdir(where):
            m = ID_FILE_RE.match(name)
            if m:
                try:
                    with open(os.path.join(where, name), encoding="utf-8", errors="replace") as fh:
                        value = (fh.read().split() or [""])[0]
                except OSError:
                    continue
                if value:
                    return m.group(1).lower(), m.group(2), value
    return None


def _read_text(path: str) -> str | None:
    """File text with undecodable bytes preserved (surrogateescape), so rewriting never mangles the user's content."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError:
        return None


def _existing_plexmatch(folder: str) -> tuple[list[str], list[str]]:
    """(user lines, our generated lines) of a folder's .plexmatch. If our end marker went missing, only hint lines
    (title:/ep:) after the begin marker count as ours, so the user's lines are never dropped."""
    text = _read_text(os.path.join(folder, ".plexmatch"))
    if text is None:
        return [], []
    lines = text.splitlines()
    terminated = any(l.strip() == BLOCK_END for l in lines)
    user, ours, inside = [], [], False
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
        elif line.strip() == BLOCK_END:
            inside = False
        elif inside and (terminated or line.strip().lower().startswith(("title:", "ep:"))):
            ours.append(line)
        else:
            user.append(line)
    return user, ours


class Preparer:
    def __init__(self, service=None):
        self.service = service  # for tvdb2/anidb2 renumbering (TVDB episode lists, Anime-Lists mapping)

    # Planning ------------------------------------------------------------------------------------------------------
    def plan(self, root: str) -> Plan:
        root = os.path.abspath(root)
        plan = Plan(root)
        for name in sorted(os.listdir(root)):
            show = os.path.join(root, name)
            if os.path.isdir(show) and not name.startswith(".") and name.lower() not in EXTRAS_DIRS:
                self._plan_show(plan, root, show)
        return plan

    def _plan_show(self, plan: Plan, root: str, show: str):
        name = os.path.basename(show)
        title = clean_title(name)
        tag = TAG_RE.search(name)
        source, variant, ident = (tag.group(1).lower(), tag.group(2), tag.group(3)) if tag else (None, "", None)
        targets: set[str] = set()  # planned target paths (normcase) of this show: collision check

        # 1. id files -> .plexmatch title hint
        if not tag:
            found = _read_id_file(show)
            if found:
                source, variant, ident = found
                user, ours = _existing_plexmatch(show)
                wanted = "title: %s [%s%s-%s]" % (title, source, variant, ident)
                if wanted in (l.strip() for l in ours):
                    pass  # already written by an earlier --apply
                elif any(l.strip().lower().startswith(("title:", "show:")) for l in user):
                    plan.add("warn", show, reason="has %s%s.id but .plexmatch already sets a title; add [%s%s-%s] to it yourself"
                                                   % (source, variant, source, variant, ident))
                else:
                    plan.add("plexmatch", os.path.join(show, ".plexmatch"), reason="from %s%s.id" % (source, variant),
                             content=wanted + "\n")

        # 2. sub-folders: ASS grouping folders ("[2] Sequel" or id-tagged) become shows; others are only reported
        handled: set[str] = set()  # sub-folders whose files are dealt with by a move (skip them below)
        for sub in sorted(os.listdir(show)):
            path = os.path.join(show, sub)
            if (not os.path.isdir(path) or sub.startswith(".") or sub.lower() in EXTRAS_DIRS or SEASON_DIR_RE.match(sub)
                    or SPECIAL_DIR_RE.match(sub) or OFFSET_RE.search(sub) or not any(True for _ in _videos(path))):
                continue
            handled.add(path)
            if not (GROUP_DIR_RE.match(sub) or TAG_RE.search(sub)):
                plan.add("warn", path, reason="sub-folder that isn't a season, specials or ASS grouping folder; Plex reads its "
                                              "files as episodes of '%s' - check the numbering or move it yourself" % title)
                continue
            sub_tag = TAG_RE.search(sub)
            new_name = clean_title(sub) + (" " + sub_tag.group(0) if sub_tag else "")
            target = os.path.join(root, new_name)
            if os.path.normcase(target) == os.path.normcase(show):
                # "Show/[1] Show/": this entry IS the parent show -> move its files up instead of moving the folder
                for video in _videos(path):
                    rel = os.path.relpath(video, path)
                    dest = os.path.join(show, rel)
                    if os.path.exists(dest) or os.path.normcase(dest) in targets:
                        plan.add("warn", video, reason="can't move up to %s: taken" % os.path.relpath(dest, root))
                        continue
                    targets.add(os.path.normcase(dest))
                    plan.add("move", video, dest, "grouping folder of the parent show itself -> files move up")
                    for side, suffix in _sidecars(video):
                        plan.add("move", side, os.path.splitext(dest)[0] + suffix, "follows its video")
            elif os.path.exists(target):
                plan.add("warn", path, reason="grouping folder; %s already exists, move it by hand" % new_name)
            else:
                plan.add("move", path, target, "grouping folder -> its own show (Plex merges sub-folders into one show)")

        # 3. files: specials, specials folders, offsets, tvdb2/anidb2
        renumber = self._renumber_map(source, variant, ident)
        for video in _videos(show):
            if any(video.startswith(h + os.sep) for h in handled):
                continue  # inside a grouping/unknown folder: prepare again after applying (it is its own show then)
            folder = os.path.dirname(video)
            stem, ext = os.path.splitext(os.path.basename(video))
            new_se, reason = None, ""
            special = special_number(stem)
            number = episode_number(stem)
            offset = OFFSET_RE.search(os.path.basename(folder)) if folder != show else None
            if special is not None:
                new_se, reason = (0, special), "ASS special name -> HAMA season-0 number"
            elif SPECIAL_DIR_RE.match(os.path.basename(folder)) and number and number[0] is None:
                new_se, reason = (0, number[1]), "file in a specials folder -> season 0"
            elif offset and number and number[0] is None:  # absolute numbers only; SxxEyy names are already done
                season = int(offset.group(1) or 1)
                shift = int(offset.group(2) or offset.group(3) or 0)
                new_se, reason = (season, number[1] + shift), "ASS folder offset"
            elif renumber is not None and number and number[0] is None:
                new_se = renumber.get(number[1])
                reason = "%s%s: absolute -> TVDB season/episode" % (source, variant)
                if new_se is None:
                    plan.add("warn", video, reason="absolute episode %d not found in the TVDB mapping" % number[1])
                    continue
            elif number is None:
                plan.add("warn", video, reason="no episode number; Plex will skip it (rename it, or use SxxEyy)")
                continue
            if new_se is None:
                continue
            target = os.path.join(folder, "%s - S%02dE%02d%s" % (title, new_se[0], new_se[1], ext))
            if os.path.normcase(target) == os.path.normcase(video):
                continue
            if os.path.normcase(target) in targets or os.path.exists(target):
                plan.add("warn", video, reason="would become %s, which is taken" % os.path.basename(target))
                continue
            targets.add(os.path.normcase(target))
            plan.add("rename", video, target, reason)
            for side, suffix in _sidecars(video):
                side_target = os.path.splitext(target)[0] + suffix
                if os.path.normcase(side_target) in targets or os.path.exists(side_target):
                    plan.add("warn", side, reason="would become %s, which is taken" % os.path.basename(side_target))
                    continue
                targets.add(os.path.normcase(side_target))
                plan.add("rename", side, side_target, "follows its video")

    def _renumber_map(self, source, variant, ident):
        """absolute number -> (season, episode) for [tvdb2-x] (TVDB absolute numbers) and [anidb2-x] (AniDB entry mapped
        into TVDB seasons). None when the folder doesn't ask for it."""
        if variant != "2" or source not in ("tvdb", "anidb") or not self.service:
            return None
        svc = self.service
        if not svc.lists.ready.is_set():
            svc.lists.load()
        if source == "tvdb":
            eps = svc.tvdb.episodes(ident)
            return {int(ep["absoluteNumber"]): (s, e) for (s, e), ep in eps.items() if s > 0 and ep.get("absoluteNumber")}
        from .animelists import episode_map
        tvdbid = svc.lists.get(ident).get("tvdbid", "")
        if not tvdbid.isdigit():
            return {}
        eps = svc.tvdb.episodes(tvdbid)
        mapping = episode_map(svc.lists.entries_for_tvdb(tvdbid), eps, "tvdb")
        return {a_ep: key for key, (aid, a_season, a_ep) in mapping.items() if aid == ident and a_season == 1}

    # Applying / undo -------------------------------------------------------------------------------------------------
    def apply(self, plan: Plan) -> int:
        """Carry out a plan. Never overwrites. Each step is written to the undo log BEFORE it happens (and marked done
        after), so nothing is ever changed without a record; if the log can't be written, nothing is changed."""
        log_path = os.path.join(plan.root, UNDO_FILE)
        history = _load_undo(log_path)
        _save_undo(log_path, history)  # raises (and changes nothing) if the library root isn't writable
        done = 0
        order = {"rename": 0, "move": 1, "plexmatch": 2}  # files first (folder moves change their paths), then hints
        for a in sorted((a for a in plan.actions if a.kind in order), key=lambda a: order[a.kind]):
            if a.kind in ("rename", "move"):
                if os.path.exists(a.target) or not os.path.exists(a.path):
                    log.warning("Skipped %s: target exists or source is gone", a.path)
                    continue
                step = {"op": a.kind, "from": a.path, "to": a.target, "time": int(time.time()), "done": False}
            else:
                step = {"op": "plexmatch", "path": a.path, "before": _read_text(a.path), "time": int(time.time()), "done": False}
            history.append(step)
            _save_undo(log_path, history)
            try:
                if a.kind in ("rename", "move"):
                    os.makedirs(os.path.dirname(a.target), exist_ok=True)
                    os.rename(a.path, a.target)
                else:
                    step["after"] = _write_plexmatch(a.path, a.content)
                step["done"] = True
                done += 1
            except (OSError, ValueError) as exc:
                log.error("%s %s failed: %s", a.kind, a.path, exc)
            _save_undo(log_path, history)
        return done

    def undo(self, root: str) -> int:
        """Reverse what the library's undo log records (newest first). A .plexmatch you edited since is left alone."""
        log_path = os.path.join(os.path.abspath(root), UNDO_FILE)
        history, undone, kept = _load_undo(log_path), 0, []
        for step in reversed(history):
            if not step.get("done", True):
                continue  # never happened
            try:
                if step["op"] in ("rename", "move"):
                    if os.path.exists(step["to"]) and not os.path.exists(step["from"]):
                        os.makedirs(os.path.dirname(step["from"]), exist_ok=True)
                        os.rename(step["to"], step["from"])
                        undone += 1
                        continue
                elif step["op"] == "plexmatch":
                    if "after" in step and _read_text(step["path"]) != step["after"]:
                        log.warning("Not undoing %s: it was edited after prepare", step["path"])
                    elif step["before"] is None:
                        os.remove(step["path"])
                        undone += 1
                        continue
                    else:
                        with open(step["path"], "w", encoding="utf-8", errors="surrogateescape") as fh:
                            fh.write(step["before"])
                        undone += 1
                        continue
            except OSError as exc:
                log.error("Undo of %s failed: %s", step, exc)
            kept.insert(0, step)
        _save_undo(log_path, kept)
        return undone


def _write_plexmatch(path: str, content: str) -> str:
    """Put our lines into the generated block of a .plexmatch, keeping the user's lines (bytes preserved). Returns the new text."""
    new = _read_text(path) is None
    user, _ours = _existing_plexmatch(os.path.dirname(path))
    kept = [l for l in user if l.strip()]
    text = "\n".join(kept) + ("\n" if kept else "")
    text += "%s\n%s%s\n" % (BLOCK_BEGIN, content if content.endswith("\n") else content + "\n", BLOCK_END)
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)
    if new:
        os.chmod(path, 0o644)  # containers often run with umask 000; don't touch modes of the user's own files
    return text


def _load_undo(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _save_undo(path: str, history: list):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
