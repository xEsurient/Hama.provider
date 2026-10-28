"""Title helpers ported from HAMA (common.cleanse_title, AniDB.GetAniDBTitle, AniDB.summary_sanitizer)."""
from __future__ import annotations

import re
import unicodedata

TYPE_PRIORITY = {"main": 1, "official": 2, "kana": 3, "kanareading": 3, "card": 4, "titlecard": 4,
                 "syn": 5, "synonym": 5, "short": 6, None: 7}
_REPLACE = str.maketrans("`:/*?-.,;_", "          ")


def strip_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def cleanse_title(text: str) -> str:
    """Lowercase, drop (...) and [...] parts, diacritics and punctuation."""
    if not text:
        return ""
    stripped = strip_diacritics(text)
    if len(text) <= len(stripped) + 2:  # mostly latin text: drop accents, keep kana/kanji untouched otherwise
        text = stripped
    while re.search(r"\([^()]*?\)", text):
        text = re.sub(r"\([^()]*?\)", " ", text)
    while re.search(r"\[[^\[\]]*?\]", text):
        text = re.sub(r"\[[^\[\]]*?\]", " ", text)
    text = unicodedata.normalize("NFC", text.lower()).translate(_REPLACE)
    return " ".join(text.replace("'", "").split())


def pick_title(titles, languages):
    """titles: iterable of (type, lang, text). Returns (title, main_title).
    Same rules as HAMA GetAniDBTitle: first language in priority order that has a title;
    'main' is AniDB's main title; per language the best title type wins; 'short' is never used."""
    languages = list(languages)
    if "main" not in languages:
        languages.append("main")
    found = {}
    level = {}
    for type_, lang, text in titles:
        if not text:
            continue
        text = text.replace("`", "'")
        if lang in languages:
            if not type_:  # episode titles have no type
                found[lang], level[lang] = text, 6 + languages.index(lang)
            elif type_ != "short" and TYPE_PRIORITY.get(type_, 7) < level.get(lang, 20):
                found[lang], level[lang] = text, TYPE_PRIORITY.get(type_, 7)
        if type_ == "main":
            found["main"] = text
    main = found.get("main", "")
    for lang in languages:
        if found.get(lang):
            return found[lang], main
    return main, main


def official_title(titles, lang):
    """Official (or main) title in a given language, for originalTitle."""
    best, best_level = "", 20
    for type_, title_lang, text in titles:
        if title_lang == lang and type_ in ("main", "official") and TYPE_PRIORITY[type_] < best_level:
            best, best_level = text, TYPE_PRIORITY[type_]
    return best


def summary_sanitizer(summary: str) -> str:
    if not summary:
        return ""
    summary = summary.replace("`", "'")
    summary = re.sub(r"https?://anidb\.net/[a-z]{1,2}[0-9]+ \[(?P<text>.+?)\]", r"\g<text>", summary)
    summary = re.sub(r"https?://anidb\.net/[a-z]+/[0-9]+ \[(?P<text>.+?)\]", r"\g<text>", summary)
    summary = re.sub(r"^(\*|--|~) .*", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"\n(Source|Note|Summary):.*", "", summary, flags=re.DOTALL)
    summary = re.sub(r"\n\n+", "\n\n", summary, flags=re.DOTALL)
    return summary.strip(" \n")
