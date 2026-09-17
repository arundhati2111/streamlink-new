"""onlineradiobox.com extractor.

Stream URLs live directly in the DOM on the `.b-play.station_play` element's
`stream` attribute, so search() already carries the stream and resolve() just
packages it - no extra fetch needed.
"""
from __future__ import annotations

from playwright.async_api import Page

from ..browser import classify_stream, dismiss_consent, goto
from ..countries import country_from_code
from ..models import StationResult
from .base import Candidate, Platform, register

BASE = "https://onlineradiobox.com"


class OnlineRadioBox(Platform):
    name = "onlineradiobox"
    agent_hint = (
        "Search results and station pages carry an element with class "
        "'b-play station_play'. Its 'stream' attribute is the direct audio URL "
        "and 'radioname' is the station name. Use the 'All world' filter link if "
        "the default results are region-filtered."
    )

    def search_url(self, query: str) -> str:
        return f"{BASE}/search?q={self.encode(query)}"

    async def search(self, page: Page, query: str) -> list[Candidate]:
        await goto(page, self.search_url(query))
        await dismiss_consent(page)

        # Broaden to worldwide results if the region filter link exists.
        try:
            all_world = page.get_by_role("link", name="All world", exact=False)
            if await all_world.count() > 0:
                await all_world.first.click(timeout=3000)
                await page.wait_for_timeout(800)
        except Exception:
            pass

        rows = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('.b-play.station_play, .station_play').forEach(play => {
                    const stream = play.getAttribute('stream');
                    if (!stream) return;
                    const row = play.closest('li, .stations__station, .search-station');
                    let href = '';
                    if (row) {
                        const a = row.querySelector('a[href]');
                        if (a) href = a.href;
                    }
                    out.push({
                        name: play.getAttribute('radioname') || '',
                        stream,
                        streamtype: play.getAttribute('streamtype') || '',
                        page_url: href,
                        station_id: play.getAttribute('radioid') || '',
                    });
                });
                return out;
            }"""
        )

        cands: list[Candidate] = []
        for r in rows:
            stream = r.get("stream") or ""
            if not stream:
                continue
            # radioid looks like "cl.talcaonline" / "us.masfm"; prefix = ISO code.
            rid = r.get("station_id") or ""
            country = country_from_code(rid.split(".", 1)[0]) if "." in rid else ""
            cands.append(
                Candidate(
                    platform=self.name,
                    name=r.get("name") or "",
                    country=country,
                    ref=r.get("streamtype") or "",  # carry streamtype hint
                    page_url=r.get("page_url") or "",
                    station_id=rid,
                    stream_url=stream,  # stream already known from the DOM
                )
            )
        return cands

    async def resolve(self, page: Page, cand: Candidate) -> StationResult | None:
        # Stream is already known from search(); just package it.
        if not cand.stream_url:
            return None
        stype = cand.ref or classify_stream(cand.stream_url)
        return StationResult(
            platform=self.name,
            query="",
            name=cand.name,
            country=cand.country,
            stream_url=cand.stream_url,
            stream_type=stype,
            page_url=cand.page_url,
            station_id=cand.station_id,
            mode="rule",
        )


register(OnlineRadioBox())
