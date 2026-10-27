"""radio.net extractor.

radio.net exposes a public JSON API that returns stream URLs directly, so
search() reads all matching stations (each with name/country/stream) from
prod.radio-api.net/stations/search and resolve() just packages the known
stream. No browser player needed.
"""
from __future__ import annotations

from playwright.async_api import Page

from ..browser import classify_stream, goto
from ..models import StationResult
from .base import Candidate, Platform, register

BASE = "https://www.radio.net"
SEARCH_API = "https://prod.radio-api.net/stations/search"
# "All" results: the API caps out well below this for a normal query.
SEARCH_COUNT = 60


class RadioNet(Platform):
    name = "radionet"
    agent_hint = (
        "radio.net has a JSON API - use it directly, do NOT click play buttons on "
        "the website. Call "
        "https://prod.radio-api.net/stations/search?query=<terms>&count=60 and read "
        "the 'playables' array; each item has 'name', 'country' (already the full "
        "country name, e.g. 'South Africa'), 'id', and a 'streams' array whose first "
        "entry's 'url' is the direct stream. SCAN THE WHOLE array for the item whose "
        "'country' matches the query - the correct one is often NOT first (e.g. "
        "'Heart FM 104.9 Cape Town' with country 'South Africa' among several UK/US "
        "'Heart' stations). Match on country here; no station page or play click "
        "needed."
    )

    def __init__(self, stream_wait_seconds: int = 6) -> None:
        # kept for interface symmetry; the API path does not need it
        self.stream_wait_seconds = stream_wait_seconds

    def search_url(self, query: str) -> str:
        return f"{BASE}/search?q={self.encode(query)}"

    def _api_url(self, query: str) -> str:
        return f"{SEARCH_API}?query={self.encode(query)}&count={SEARCH_COUNT}"

    async def search(self, page: Page, query: str) -> list[Candidate]:
        # Establish a normal browsing context, then hit the JSON API.
        try:
            await goto(page, BASE + "/")
        except Exception:
            pass

        data = None
        for _ in range(2):  # brief retry for transient network errors
            try:
                resp = await page.request.get(self._api_url(query))
                data = await resp.json()
                break
            except Exception:
                await page.wait_for_timeout(1000)
        if not isinstance(data, dict):
            return []

        cands: list[Candidate] = []
        for item in data.get("playables", []):
            streams = item.get("streams") or []
            url = (streams[0] or {}).get("url") if streams else ""
            if not url:
                continue
            slug = item.get("id") or ""
            # The API often carries a city and a short slogan/genres; fold them
            # into a description so the DB workflow can soft-match city/freq.
            desc_bits = [
                str(item.get("city") or ""),
                str(item.get("genres") or ""),
                str(item.get("shortDescription") or item.get("description") or ""),
            ]
            description = " ".join(b for b in desc_bits if b).strip()
            cands.append(
                Candidate(
                    platform=self.name,
                    name=item.get("name") or "",
                    country=item.get("country") or "",
                    page_url=f"{BASE}/s/{slug}" if slug else "",
                    station_id=slug,
                    stream_url=url,
                    description=description,
                )
            )
        return cands

    async def resolve(self, page: Page, cand: Candidate) -> StationResult | None:
        if not cand.stream_url:
            return None
        return StationResult(
            platform=self.name,
            query="",
            name=cand.name,
            country=cand.country,
            stream_url=cand.stream_url,
            stream_type=classify_stream(cand.stream_url),
            page_url=cand.page_url,
            station_id=cand.station_id,
            mode="rule",
            description=cand.description,
        )


register(RadioNet())
