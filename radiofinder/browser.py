"""Playwright browser manager with shared helpers for the platform extractors."""
from __future__ import annotations

import re

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

# Patterns that identify an actual audio stream URL seen on the network.
# Kept deliberately specific: audio file extensions, HLS/DASH manifests, known
# streaming CDNs/servers, and audio-only paths. A bare "stream" substring is
# NOT matched because it appears in ad/analytics URLs (e.g. "instream").
STREAM_URL_RE = re.compile(
    r"\.(m3u8|mpd|mp3|aac|ogg|pls|m4a)(\?|$)"
    r"|/hls/|/dash/|icecast|shoutcast|/;?stream(?:ing)?[/?]"
    r"|bbcmedia|open\.live\.bbc\.co\.uk|a\.files\.bbci\.co\.uk"
    r"|as-hls-ww-live|akamaized\.net/.+\.(m3u8|ts|aac|mp3)"
    r"|streamguys|streaming\.|sharp-stream|radioboss|zeno\.fm|:8\d{3}/",
    re.IGNORECASE,
)

# Hosts/paths that look stream-ish but are ads/analytics/telemetry - never audio.
STREAM_URL_DENY_RE = re.compile(
    r"doubleclick|googlesyndication|google\.com/ccm|analytics|/collect\?"
    r"|2mdn\.net|\.js(\?|$)|adservice|adsystem|scorecardresearch|/gampad/"
    r"|googletagmanager|facebook\.com|/pixel|/beacon",
    re.IGNORECASE,
)

# Text on common cookie/consent buttons across these sites.
CONSENT_LABELS = [
    "Agree and continue",
    "Accept all",
    "I Accept",
    "Accept All Cookies",
    "Agree",
    "Accept",
    "Consent",
]


async def goto(page: Page, url: str) -> None:
    """Navigate waiting only for DOM, not full 'load'.

    Ad/tracking-heavy sites (tunein, radio.net) keep connections open so the
    'load' event may never fire within the timeout. domcontentloaded is enough
    since our selectors and resolver calls only need the DOM.
    """
    await page.goto(url, wait_until="domcontentloaded")


def classify_stream(url: str) -> str:
    u = url.lower()
    if ".m3u8" in u or "/hls/" in u:
        return "hls"
    if ".mpd" in u or "/dash/" in u:
        return "dash"
    if ".mp3" in u:
        return "mp3"
    if ".aac" in u:
        return "aac"
    if ".ogg" in u:
        return "ogg"
    if ".pls" in u:
        return "pls"
    if ".m4a" in u:
        return "m4a"
    # No known extension: if it matched the stream allow-list (e.g. an Icecast
    # mount like /capitalfm) treat it as generic audio; otherwise it's a link
    # out to an external page.
    if STREAM_URL_RE.search(u):
        return "audio"
    return "external"


# Content-types that indicate a live audio stream or a playlist pointing at one.
_AUDIO_CONTENT_TYPES = (
    "audio/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "application/ogg",
    "video/mp2t",  # HLS .ts segments
)


async def check_stream_alive(page: Page, url: str, timeout_ms: int = 12000) -> bool:
    """Best-effort liveness check for a stream URL.

    Returns True if the URL responds successfully with an audio/playlist
    content-type. Uses Playwright's request API (follows redirects, real TLS).
    Never raises.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    try:
        resp = await page.request.get(url, timeout=timeout_ms, max_redirects=5)
    except Exception:
        return False

    if not (200 <= resp.status < 400):
        return False

    ctype = (resp.headers or {}).get("content-type", "").lower()
    if any(ct in ctype for ct in _AUDIO_CONTENT_TYPES):
        return True

    # Some Icecast/SHOUTcast servers report text/html or octet-stream for the
    # mount but include ICY headers - treat those as alive too.
    headers = resp.headers or {}
    if any(k.lower().startswith("icy-") for k in headers):
        return True

    # HLS/PLS manifests may be served as text/plain; sniff a short body.
    if "text/" in ctype or "octet-stream" in ctype:
        try:
            body = (await resp.text())[:400].lower()
        except Exception:
            body = ""
        if "#extm3u" in body or "[playlist]" in body or "file1=" in body:
            return True
    return False


class BrowserManager:
    """Owns a single Playwright browser + context; hands out pages."""

    def __init__(self, headless: bool = True, nav_timeout_ms: int = 30000) -> None:
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        self._context.set_default_navigation_timeout(self.nav_timeout_ms)
        self._context.set_default_timeout(self.nav_timeout_ms)

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserManager not started")
        return await self._context.new_page()

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()


async def dismiss_consent(page: Page) -> bool:
    """Best-effort click of a cookie/consent button. Never raises.

    Returns True if something was clicked. Checks the page and any iframes
    (radio.net serves its CMP inside an iframe that loads slightly after the
    page, so we poll a few times before giving up).
    """
    for _ in range(5):  # ~5s total, CMP iframes load late
        for label in CONSENT_LABELS:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2500)
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                pass
            # Try inside frames (CMP iframes).
            for frame in page.frames:
                try:
                    fbtn = frame.get_by_role("button", name=label, exact=False)
                    if await fbtn.count() > 0:
                        await fbtn.first.click(timeout=2500)
                        await page.wait_for_timeout(500)
                        return True
                except Exception:
                    pass
        await page.wait_for_timeout(1000)
    return False


class StreamSniffer:
    """Records network request URLs that look like audio streams."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)

    def _on_request(self, request) -> None:
        url = request.url
        low = url.lower()
        # Skip known UI sample/placeholder clips that are not real streams.
        if "sample" in low or "notcompatible" in low or "/blank." in low:
            return
        # Skip ads/analytics/telemetry that superficially look stream-ish.
        if STREAM_URL_DENY_RE.search(url):
            return
        if STREAM_URL_RE.search(url):
            self.hits.append(url)

    def best(self) -> str | None:
        """Prefer a playlist/manifest over segments; else first hit."""
        if not self.hits:
            return None
        # Prefer .m3u8 master over .ts segments.
        manifests = [
            u for u in self.hits if ".m3u8" in u.lower() and ".ts" not in u.lower()
        ]
        if manifests:
            return manifests[0]
        direct = [u for u in self.hits if re.search(r"\.(mp3|aac|ogg)(\?|$)", u, re.I)]
        if direct:
            return direct[0]
        return self.hits[0]
