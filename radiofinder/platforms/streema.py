"""streema.com extractor.

search(): return ALL /radios/<slug> result rows (name + station-page URL).
resolve(): open the station page and get the stream. The play button
(#spot-play-button) carries a data-url; it may be a direct stream, a
'/go/?url=' external wrapper, or a '/radios/play/<slug>' player-popup whose
real stream only appears on the popup (we navigate there and sniff it).

Streema doesn't expose a reliable country, so candidates carry no country and
the relevance filter treats it as "don't block".
"""
from __future__ import annotations

from urllib.parse import unquote

from playwright.async_api import Page

from ..browser import StreamSniffer, classify_stream, dismiss_consent, goto
from ..models import StationResult
from .base import Candidate, Platform, register

BASE = "https://streema.com"


def _unwrap(data_url: str) -> str:
    """Turn '/go/?url=https://...' into the real URL; leave others absolute."""
    if not data_url:
        return ""
    marker = "url="
    if "/go/" in data_url and marker in data_url:
        return unquote(data_url.split(marker, 1)[1])
    if data_url.startswith("/"):
        return BASE + data_url
    return data_url


class Streema(Platform):
    name = "streema"
    agent_hint = (
        "Search page: https://streema.com/radios/search/?q=<terms> . The station "
        "links are <a href> starting with '/radios/' (e.g. '/radios/Heart_104.9'). "
        "To read them, call query_attributes with selector 'a[href^=\"/radios/\"]' "
        "and attributes ['href'] - do NOT rely on get_links, which also returns "
        "'#loginModal'/social-login anchors that are NOT stations; ignore any href "
        "containing '/search', '#', 'login', or 'facebook'. Do not guess slugs; use "
        "the real href from the results. Open the chosen station page (navigate to "
        "the '/radios/<slug>' URL). There the play button has id 'spot-play-button' "
        "with a 'data-url': if it contains '/go/?url=', the real stream is the "
        "URL-decoded value after 'url='; if it contains '/radios/play/', open that "
        "popup and read the audio request via get_streams(). data-role "
        "'external-stream-tab' means it only links out to another site. Streema has "
        "no country field - confirm the country from the station page text."
    )

    def __init__(self, stream_wait_seconds: int = 6) -> None:
        self.stream_wait_seconds = stream_wait_seconds

    def search_url(self, query: str) -> str:
        return f"{BASE}/radios/search/?q={self.encode(query)}"

    async def _goto_retry(self, page: Page, url: str, attempts: int = 3) -> bool:
        """Navigate with retries; streema intermittently resets connections."""
        for i in range(attempts):
            try:
                await goto(page, url)
                return True
            except Exception:
                await page.wait_for_timeout(1000 * (i + 1))
        return False

    async def search(self, page: Page, query: str) -> list[Candidate]:
        if not await self._goto_retry(page, self.search_url(query)):
            return []
        await dismiss_consent(page)
        try:
            await page.wait_for_selector('a[href^="/radios/"]', timeout=8000)
        except Exception:
            pass
        rows = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a[href^="/radios/"]').forEach(a => {
                    const h = a.getAttribute('href');
                    if (!h || h.startsWith('/radios/search') || seen.has(h)) return;
                    seen.add(h);
                    out.push({ href: h, name: (a.textContent || '').replace(/\\s+/g,' ').trim() });
                });
                return out;
            }"""
        )
        cands: list[Candidate] = []
        for r in rows:
            h = r.get("href") or ""
            full = BASE + h if h.startswith("/") else h
            cands.append(
                Candidate(
                    platform=self.name,
                    name=(r.get("name") or "").split(" - ")[0].strip(),
                    ref=full,
                    page_url=full,
                )
            )
        return cands

    async def resolve(self, page: Page, cand: Candidate) -> StationResult | None:
        sniffer = StreamSniffer()
        sniffer.attach(page)
        try:
            if not await self._goto_retry(page, cand.ref):
                return None
            await dismiss_consent(page)
            info = await page.evaluate(
                """() => {
                    const btn = document.querySelector('#spot-play-button');
                    const h1 = document.querySelector('h1');
                    return {
                        name: h1 ? h1.textContent.replace(/\\s+/g, ' ').trim() : document.title,
                        data_url: btn ? btn.getAttribute('data-url') : '',
                        role: btn ? btn.getAttribute('data-role') : '',
                    };
                }"""
            )
        except Exception:
            return None

        role = info.get("role") or ""
        data_url = info.get("data_url") or ""
        stream = _unwrap(data_url)

        popup = ""
        if "/radios/play/" in data_url:
            popup = data_url if data_url.startswith("http") else BASE + data_url
            stream = ""  # the popup URL itself is not a stream

        if not stream:
            try:
                sniffer.hits.clear()
                if popup:
                    if await self._goto_retry(page, popup):
                        await dismiss_consent(page)
                        await page.wait_for_timeout(self.stream_wait_seconds * 1000)
                else:
                    for target in [
                        page.locator("#spot-play-button"),
                        page.get_by_role("button", name="Play", exact=False),
                    ]:
                        if await target.count() > 0:
                            await target.first.click(timeout=4000)
                            break
                    await page.wait_for_timeout(self.stream_wait_seconds * 1000)
                stream = sniffer.best() or ""
            except Exception:
                stream = ""

        # Reject streema's own player/redirect pages - they are HTML, not audio.
        if "streema.com/radios/play" in stream or "streema.com/go" in stream:
            stream = ""
        if not stream:
            return None

        stype = "external" if role == "external-stream-tab" else classify_stream(stream)
        name = cand.name or (info.get("name") or "").split(" - ")[0].strip()
        return StationResult(
            platform=self.name,
            query="",
            name=name,
            stream_url=stream,
            stream_type=stype,
            page_url=cand.page_url,
            mode="rule",
            description=cand.description,
        )


register(Streema())
