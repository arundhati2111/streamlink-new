"""Lightweight country helpers.

We only need enough to (a) turn a platform's country code into a name and
(b) compare a query's country term against a result's country. Kept small and
tolerant rather than a full ISO dataset.
"""
from __future__ import annotations

import re
import unicodedata

# ISO 3166-1 alpha-2 (and a few common aliases) -> canonical country name.
_CODE_TO_NAME = {
    "us": "united states", "uk": "united kingdom", "gb": "united kingdom",
    "cl": "chile", "mx": "mexico", "ar": "argentina", "br": "brazil",
    "ca": "canada", "au": "australia", "nz": "new zealand", "ie": "ireland",
    "de": "germany", "fr": "france", "es": "spain", "it": "italy",
    "pt": "portugal", "nl": "netherlands", "be": "belgium", "ch": "switzerland",
    "at": "austria", "se": "sweden", "no": "norway", "dk": "denmark",
    "fi": "finland", "pl": "poland", "ru": "russia", "ua": "ukraine",
    "in": "india", "pk": "pakistan", "bd": "bangladesh", "lk": "sri lanka",
    "cn": "china", "jp": "japan", "kr": "south korea", "ph": "philippines",
    "id": "indonesia", "my": "malaysia", "sg": "singapore", "th": "thailand",
    "vn": "vietnam", "ae": "united arab emirates", "sa": "saudi arabia",
    "za": "south africa", "ng": "nigeria", "ke": "kenya", "eg": "egypt",
    "co": "colombia", "pe": "peru", "ve": "venezuela", "ec": "ecuador",
    "uy": "uruguay", "py": "paraguay", "bo": "bolivia", "gt": "guatemala",
    "cr": "costa rica", "do": "dominican republic", "pr": "puerto rico",
    "gr": "greece", "tr": "turkey", "il": "israel", "cz": "czechia",
    "ro": "romania", "hu": "hungary", "bg": "bulgaria", "hr": "croatia",
    "rs": "serbia", "sk": "slovakia", "si": "slovenia",
}

# Common alternative spellings a user might type -> canonical name.
_ALIASES = {
    "usa": "united states", "u.s.a.": "united states", "u.s.": "united states",
    "america": "united states", "united states of america": "united states",
    "england": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "u.k.": "united kingdom",
    "uae": "united arab emirates",
}


def _norm(text: str) -> str:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", " ", text).strip()


def canonical_country(value: str) -> str:
    """Normalise a country name/code to a canonical lowercase name, or ''."""
    if not value:
        return ""
    v = _norm(value)
    if v in _CODE_TO_NAME:      # e.g. "us"
        return _CODE_TO_NAME[v]
    if v in _ALIASES:           # e.g. "usa", "england"
        return _ALIASES[v]
    return v                    # already a full name like "chile"


def country_from_code(code: str) -> str:
    """Turn a 2-letter code (e.g. from a URL path or radioid) into a name."""
    if not code:
        return ""
    return _CODE_TO_NAME.get(code.lower(), code.lower())


# Canonical country names we recognise (for detecting a country term in a query).
KNOWN_COUNTRY_NAMES = set(_CODE_TO_NAME.values()) | set(_ALIASES.values())


def is_known_country(value: str) -> bool:
    """True if `value` resolves to a country we recognise."""
    return canonical_country(value) in KNOWN_COUNTRY_NAMES
