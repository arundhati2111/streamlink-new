"""tunein.com extractor.

TuneIn is fully API-driven:
  - search():  Search.ashx?query=<q>&render=json -> ALL station guide_ids + names
  - resolve(): Tune.ashx?id=<gid> -> stream URL, Describe.ashx?id=<gid> -> country

search() is cheap (one list call). The per-station Tune/Describe calls happen in
resolve(), which the workflow runs only for candidates that passed the name
filter - so we don't hit the API for every unrelated result.
"""
from __future__ import annotations

from playwright.async_api import Page

from ..browser import classify_stream, goto
from ..models import StationResult
from ..relevance import is_relevant
from .base import Candidate, Platform, register

BASE = "https://tunein.com"
SEARCH_API = "https://opml.radiotime.com/Search.ashx"
TUNE_API = "https://opml.radiotime.com/Tune.ashx"
DESCRIBE_API = "https://opml.radiotime.com/Describe.ashx"


class TuneIn(Platform):
    name = "tunein"
    agent_hint = (
        "TuneIn has JSON APIs; ALWAYS append &render=json to every call. "
        "1) Search: https://opml.radiotime.com/Search.ashx?query=<terms>&render=json "
        "-> read the 'body' array; station items have item=='station' and a "
        "'guide_id' like 's67995'. There can be many same-named stations. "
        "2) COUNTRY: for each name-matching guide_id call "
        "https://opml.radiotime.com/Describe.ashx?id=<guide_id>&render=json and read "
        "body[0].'location' (\"City, Country\", e.g. 'Cape Town, South Africa') to "
        "confirm the country BEFORE deciding - do not judge country from the search "
        "text. 3) Stream: https://opml.radiotime.com/Tune.ashx?id=<guide_id>&render=json "
        ", take the first body entry's 'url'. If that url contains 'notcompatible' "
        "it is a placeholder, NOT a stream - that station has no usable stream, skip "
        "it. Check multiple candidates; the right-country one may not be first."
    )

    def __init__(self, stream_wait_seconds: int = 6) -> None:
        self.stream_wait_seconds = stream_wait_seconds

    def search_url(self, query: str) -> str:
        return f"{BASE}/search/?query={self.encode(query)}"

    async def _get_json(self, page: Page, url: str) -> dict | None:
        for _ in range(2):  # brief retry for transient failures
            try:
                resp = await page.request.get(url)
                return await resp.json()
            except Exception:
                await page.wait_for_timeout(1000)
        return None

    async def search(self, page: Page, query: str) -> list[Candidate]:
        # Load the site once so the API calls carry normal context.
        try:
            await goto(page, BASE + "/")
        except Exception:
            pass

        data = await self._get_json(
            page, f"{SEARCH_API}?query={self.encode(query)}&render=json"
        )
        cands: list[Candidate] = []
        if not isinstance(data, dict):
            return cands
        seen: set[str] = set()
        for it in data.get("body", []):
            if not isinstance(it, dict) or it.get("item") != "station":
                continue
            gid = it.get("guide_id") or ""
            name = it.get("text") or ""
            if not gid or gid in seen:
                continue
            # TuneIn pads a low-match search with popular/suggested stations
            # (e.g. "W107", "Today FM" for a query it can't match). Those share
            # no name tokens with the query, so keep only results whose NAME
            # actually matches - country is unknown here, checked later in
            # resolve()/the relevance post-filter.
            if not is_relevant(query, name):
                continue
            seen.add(gid)
            cands.append(
                Candidate(
                    platform=self.name,
                    name=name,
                    ref=gid,
                    page_url=f"{BASE}/radio/{gid}/",
                    station_id=gid,
                )
            )
        return cands

    async def _stream(self, page: Page, gid: str) -> str:
        data = await self._get_json(page, f"{TUNE_API}?id={gid}&render=json")
        if not isinstance(data, dict):
            return ""
        options = [
            b
            for b in data.get("body", [])
            if isinstance(b, dict)
            and b.get("url")
            and "notcompatible" not in b.get("url", "").lower()
        ]
        if not options:
            return ""
        options.sort(
            key=lambda b: (
                b.get("url", "").startswith("https"),
                float(b.get("bitrate") or 0),
            ),
            reverse=True,
        )
        return options[0].get("url", "")

    async def _describe(self, page: Page, gid: str) -> tuple[str, str]:
        """Return (country, location) from Describe.ashx.

        location is the raw 'City, Country' string (used as a description for
        city/state soft-matching); country is its last segment. Also folds in
        any 'slogan'/'description' text the API provides.
        """
        data = await self._get_json(page, f"{DESCRIBE_API}?id={gid}&render=json")
        if not isinstance(data, dict):
            return "", ""
        body = data.get("body") or []
        if not body or not isinstance(body[0], dict):
            return "", ""
        first = body[0]
        location = first.get("location") or ""
        country = location.split(",")[-1].strip() if location else ""
        slogan = first.get("slogan") or first.get("description") or ""
        description = " ".join(b for b in (location, slogan) if b).strip()
        return country, description

    async def resolve(self, page: Page, cand: Candidate) -> StationResult | None:
        gid = cand.ref
        stream = await self._stream(page, gid)
        if not stream:
            return None
        country, description = await self._describe(page, gid)
        return StationResult(
            platform=self.name,
            query="",
            name=cand.name,
            country=country,
            stream_url=stream,
            stream_type=classify_stream(stream),
            page_url=cand.page_url,
            station_id=cand.station_id,
            mode="rule",
            description=description,
        )


register(TuneIn())
