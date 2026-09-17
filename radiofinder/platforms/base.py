"""Platform base class and registry.

Two-phase model (extract broadly, filter, then resolve):
  - search(page, query) -> list[Candidate]
        Return ALL result rows the platform's search surfaces: name + country +
        an opaque `ref` used to fetch the stream later. Cheap: no stream
        resolution, no cap.
  - resolve(page, candidate) -> StationResult | None
        Fetch the actual stream for ONE candidate. Called only for candidates
        that already passed the name+country relevance filter, so we never pay
        to resolve streams for irrelevant results.

extract() is a convenience default that composes search()+resolve() (used by
the CSV query mode); the DB workflow drives search/filter/resolve itself.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import quote

from playwright.async_api import Page

from ..models import StationResult


@dataclass
class Candidate:
    """A single search result, before its stream is resolved."""

    platform: str
    name: str
    country: str = ""
    #: opaque pointer the platform uses in resolve() (station page URL,
    #: guide_id, slug, popup URL, etc.)
    ref: str = ""
    page_url: str = ""
    station_id: str = ""
    #: some platforms (radio.net API) already know the stream at search time;
    #: resolve() can use it directly instead of another fetch.
    stream_url: str = ""


class Platform(ABC):
    #: short identifier used in CSV + CLI (e.g. "tunein")
    name: str = ""
    #: human hint the agent uses to understand where stream URLs live
    agent_hint: str = ""

    @abstractmethod
    def search_url(self, query: str) -> str:
        """Full URL for a search results page."""

    @abstractmethod
    async def search(self, page: Page, query: str) -> list["Candidate"]:
        """Return ALL result candidates (name/country/ref), no stream fetch."""

    @abstractmethod
    async def resolve(self, page: Page, cand: "Candidate") -> StationResult | None:
        """Resolve one candidate to a StationResult with a stream URL, or None."""

    async def extract(
        self, page: Page, query: str, limit: int, relevance_query: str | None = None
    ) -> list[StationResult]:
        """Default: search all, keep relevant candidates, resolve up to `limit`.

        `query` is typed into the search box (usually the station name only).
        `relevance_query` (defaults to `query`) is what candidates are filtered
        against - pass the full "name, country" here so the country gate applies
        and wrong-country look-alikes can't fill the `limit` and bury the target.
        Filtering happens BEFORE resolving so a noisy search can't crowd out the
        real station below the cap. The DB workflow drives its own
        search -> filter -> resolve + playability check.
        """
        from ..relevance import is_relevant

        rel_q = relevance_query or query
        results: list[StationResult] = []
        seen: set[str] = set()
        for cand in await self.search(page, query):
            if not is_relevant(rel_q, cand.name, cand.country):
                continue
            res = await self.resolve(page, cand)
            if res is None or not res.stream_url or res.stream_url in seen:
                continue
            seen.add(res.stream_url)
            results.append(res)
            if len(results) >= limit:
                break
        return results

    # Shared helper: percent-encode a query consistently.
    @staticmethod
    def encode(query: str) -> str:
        return quote(query)


# Registry is populated by importing the concrete modules below.
PLATFORMS: dict[str, Platform] = {}


def register(platform: Platform) -> Platform:
    PLATFORMS[platform.name] = platform
    return platform


def get_platform(name: str) -> Platform:
    if name not in PLATFORMS:
        raise KeyError(
            f"Unknown platform '{name}'. Available: {', '.join(sorted(PLATFORMS))}"
        )
    return PLATFORMS[name]


def _load_platforms() -> None:
    # Imported for their register() side effects.
    from . import onlineradiobox, streema, radionet, tunein  # noqa: F401


_load_platforms()
