"""Orchestration: run rule or agent mode across one or more platforms."""
from __future__ import annotations

from .agent import run_agent
from .agent_mcp import run_agent_mcp
from .browser import BrowserManager
from .config import Config
from .models import StationResult
from .platforms import PLATFORMS, get_platform
from .platforms.radionet import RadioNet
from .platforms.streema import Streema
from .platforms.tunein import TuneIn
from .relevance import is_relevant, search_terms
from .storage import Storage


def _apply_config_to_platform(platform, config: Config) -> None:
    """Inject runtime config into platforms that need it (e.g. stream wait)."""
    if isinstance(platform, (RadioNet, TuneIn, Streema)):
        platform.stream_wait_seconds = config.stream_wait_seconds


async def run(
    query: str,
    platform_names: list[str],
    mode: str,
    limit: int,
    config: Config,
    storage: Storage,
    relevance_filter: bool = True,
) -> list[StationResult]:
    """Run extraction and persist results. Returns the relevant results found.

    A relevance filter (on by default) drops results whose station name does not
    match the query, e.g. other stations that merely share the frequency. Pass
    relevance_filter=False to keep every candidate.
    """
    if mode in ("agent", "mcp"):
        config.require_openai()

    # What goes into each platform's search box: the station name only. The
    # country and frequency are dropped here (they bias platforms that rank by
    # frequency or match the whole query against names) and re-applied below via
    # the relevance filter on each result's own metadata.
    search_q = search_terms(query)

    browser = BrowserManager(
        headless=config.headless, nav_timeout_ms=config.nav_timeout_ms
    )
    await browser.start()
    all_results: list[StationResult] = []
    try:
        for name in platform_names:
            platform = get_platform(name)
            _apply_config_to_platform(platform, config)
            # mcp mode drives its own browser via the Playwright MCP server, so
            # it does not use this shared Playwright page.
            page = None if mode == "mcp" else await browser.new_page()
            try:
                if mode == "rule":
                    results = await platform.extract(
                        page, search_q, limit, relevance_query=query
                    )
                elif mode == "agent":
                    results = await run_agent(page, platform, search_q, limit, config)
                elif mode == "mcp":
                    results = await run_agent_mcp(platform, search_q, limit, config)
                else:
                    raise ValueError(
                        f"unknown mode '{mode}' (use 'rule', 'agent' or 'mcp')"
                    )
            except Exception as exc:
                print(f"[{name}] extraction failed: {exc}")
                results = []
            finally:
                if page is not None:
                    await page.close()

            saved = 0
            skipped_irrelevant = 0
            skipped_dup = 0
            kept: list[StationResult] = []
            for r in results:
                if relevance_filter and not is_relevant(query, r.name, r.country):
                    skipped_irrelevant += 1
                    continue
                kept.append(r)
                # storage.save returns False when the (platform, stream_url) row
                # is already present, so reruns skip duplicates automatically.
                if storage.save(r):
                    saved += 1
                else:
                    skipped_dup += 1
            all_results.extend(kept)
            print(
                f"[{name}] mode={mode} found={len(results)} saved={saved} "
                f"(skipped: {skipped_irrelevant} off-query, {skipped_dup} duplicate)"
            )
    finally:
        await browser.close()
        storage.close()

    return all_results


def all_platform_names() -> list[str]:
    return sorted(PLATFORMS.keys())
