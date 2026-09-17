"""Decide whether an extracted station actually matches the search query.

Search engines return fuzzy matches: a query like "Radio FM Mas - FM 92.3 - Talca"
also returns other 92.3 FM stations, or a Mexican "Mas FM". We keep a result only
if its name is a strong match on the query's *distinctive* tokens (ignoring
generic radio words and frequency numbers).
"""
from __future__ import annotations

import re
import unicodedata

from .countries import canonical_country, is_known_country

# Words that carry no identifying signal for a station.
_STOPWORDS = {
    "radio", "fm", "am", "online", "live", "stream", "streaming",
    "the", "de", "la", "el", "los", "las", "y", "en", "a",
    "hd", "digital", "official", "oficial",
}


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _strip_accents(text.lower()))


def _distinctive(tokens: list[str]) -> set[str]:
    """Identifying tokens: drop stopwords and bare frequency-like numbers."""
    return {t for t in tokens if t not in _STOPWORDS and not t.isdigit()}


# Trailing frequency/band clutter, e.g. " - 104.9 FM", "97.1FM", "88 MHz".
# Stripped from the search string because platforms that rank by frequency (or
# match the whole query against names) otherwise bury the real station under
# same-frequency results worldwide.
_FREQ_RE = re.compile(r"\s*[-,]?\s*\d{2,3}([.,]\d)?\s*(fm|am|mhz)\b", re.IGNORECASE)


def search_terms(query: str) -> str:
    """The station NAME to type into a platform's search box.

    Drops a trailing ', Country' segment and any frequency/band clutter, so the
    search matches the station name rather than its country or dial number.
    Country and frequency are still enforced afterwards via is_relevant().
    """
    text = query
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) >= 2 and is_known_country(parts[-1]):
        text = ", ".join(parts[:-1])
    cleaned = _FREQ_RE.sub("", text).strip(" -,")
    return cleaned or query.strip()


def country_hint(query: str) -> str:
    """Extract the country the user asked for, if any (canonical name or '').

    Supports the recommended "Name, Country" format (country is the last
    comma-separated segment) and also a country word appearing anywhere.
    """
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) >= 2 and is_known_country(parts[-1]):
        return canonical_country(parts[-1])
    for tok in _tokens(query):
        if is_known_country(tok):
            return canonical_country(tok)
    return ""


def country_matches(query_country: str, result_country: str) -> bool:
    """True if the result's country is compatible with the query's country.

    Country is an *additional* filter, not a gate that discards unknown data:
    if either side is unknown we don't block (many streema results lack country).
    """
    if not query_country or not result_country:
        return True
    return canonical_country(query_country) == canonical_country(result_country)


def is_relevant(query: str, name: str, result_country: str = "") -> bool:
    """True if `name` plausibly refers to the same station as `query`.

    Strong match = the result shares a long/specific distinctive token with the
    query (e.g. a place name like "talca"), or shares >=2 distinctive tokens.
    A single short token overlap (e.g. "mas") is treated as too weak on its own.
    When the query names a country and the result's country is known, they must
    agree.
    """
    # Country gate first: a known wrong country is an immediate reject.
    q_country = country_hint(query)
    if not country_matches(q_country, result_country):
        return False

    q_dist = _distinctive(_tokens(query))
    n_dist = _distinctive(_tokens(name))
    # Don't let the country word itself count as a required station-name token.
    if q_country:
        for w in q_country.split():
            q_dist.discard(w)

    # Exact / near-exact NAME match wins outright. After dropping generic words
    # (radio/fm/...) and the country, if the query's station-name tokens are the
    # same set as the result's (or a subset of them), it is the same station -
    # even when that set is a single short token like {"m"} ("Radio M FM"). The
    # length-based heuristic below is only a fallback for partial overlaps.
    if q_dist and q_dist <= n_dist:
        return True

    # If the query has distinctive words (a real station name/place), the result
    # must share one of them. A name that is only a frequency (e.g. "92.3 FM")
    # has no distinctive tokens and is therefore NOT a match.
    if q_dist:
        if not n_dist:
            return False
    else:
        # Query is only generic/frequency/country: if the country already
        # matched, accept; otherwise fall back to matching the dial number.
        if q_country:
            return True
        q_nums = {t for t in _tokens(query) if t.isdigit()}
        n_nums = {t for t in _tokens(name) if t.isdigit()}
        return bool(q_nums & n_nums)

    shared = q_dist & n_dist
    if not shared:
        return False
    # Any shared token of length >= 3 (brand/place names like "mas","talca") is
    # a strong signal. Pure frequency numbers were already dropped above, so a
    # match here means a real word overlapped, not just "92.3".
    if any(len(t) >= 3 for t in shared):
        return True
    # Otherwise require at least two shared distinctive tokens.
    return len(shared) >= 2
