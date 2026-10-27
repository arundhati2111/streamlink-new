"""DB-driven workflow: read stations from the DB, find a fresh stream, write it back.

For each pending station we build a "<name>, <country>" query, run the finder
across the platforms, take the best matching stream, and update the row's
new_stream_link / source / status.
"""
from __future__ import annotations

import re

from tqdm import tqdm

from .browser import BrowserManager, check_stream_alive
from .config import Config
from .db import Database, Station
from .logging_setup import get_logger
from .models import StationResult
from .platforms import get_platform
from .relevance import is_relevant, location_score
from .runner import _apply_config_to_platform
from .validate import ffprobe_available, is_stream_playable

log = get_logger()

# The DB repair workflow fetches replacement links from onlineradiobox only.
# (Query/CSV mode still searches all platforms; this restriction is DB-only.)
DB_PLATFORMS = ["onlineradiobox"]


async def _is_working(page, url: str) -> bool:
    """Authoritative playability check.

    Prefer ffprobe (actually decodes audio, handles endless Icecast/SHOUTcast
    streams and HLS). Fall back to the HTTP header heuristic only if ffprobe
    isn't installed.
    """
    if ffprobe_available():
        return await is_stream_playable(url)
    return await check_stream_alive(page, url)


# Prefer a directly-playable stream over an "external" link-out.
_TYPE_RANK = {"hls": 3, "mp3": 3, "aac": 3, "audio": 2, "dash": 2, "ogg": 2, "": 0}


def _type_score(result: StationResult) -> int:
    return _TYPE_RANK.get(result.stream_type, 1)


def _best_stream(
    query: str, results: list[StationResult], station: Station | None = None
) -> StationResult | None:
    """Pick the best relevant result.

    Country is already enforced as a hard gate by is_relevant(). Among the
    survivors we rank by, in order:
      1) location/frequency soft score - how well the candidate's name/
         description matches the DB station's st_freq / st_city / st_state
         (see relevance.location_score); higher is better.
      2) stream type - prefer a directly-playable stream over an external
         link-out, as a tiebreaker when the location signal is equal.
    """
    relevant = [
        r for r in results if r.stream_url and is_relevant(query, r.name, r.country)
    ]
    if not relevant:
        return None

    def _loc(r: StationResult) -> int:
        if station is None:
            return 0
        return location_score(
            db_freq=station.st_freq,
            db_city=station.st_city,
            db_state=station.st_state,
            name=r.name,
            description=r.description,
        )

    # Sort by location/frequency match first, then stream type. Python's sort is
    # stable, so platform/discovery order breaks any remaining ties.
    relevant.sort(key=lambda r: (_loc(r), _type_score(r)), reverse=True)
    return relevant[0]


async def _resolve_relevant(
    page, check_page, platform, search_q: str, rel_q: str
) -> list[StationResult]:
    """Rule-mode pipeline for one platform: search all -> filter -> resolve -> verify.

    - search_q (name only) is typed into the platform's search box.
    - rel_q (name + country) gates candidates via the relevance filter, so we
      only pay to resolve streams for plausibly-correct stations.
    - each resolved stream is playability-checked (ffprobe) before we keep it,
      so anything _best_stream later picks is already known to work.
    """
    candidates = await platform.search(page, search_q)
    survivors = [
        c for c in candidates if is_relevant(rel_q, c.name, c.country)
    ]
    working: list[StationResult] = []
    for cand in survivors:
        try:
            res = await platform.resolve(page, cand)
        except Exception as exc:
            print(f"  [{platform.name}] resolve failed for '{cand.name}': {exc}")
            continue
        if res is None or not res.stream_url:
            continue
        if await _is_working(check_page, res.stream_url):
            working.append(res)
    return working


# Trailing frequency/band clutter in station names, e.g. " - 104.9 FM", "97.1FM".
# Removing it makes the search match the station name instead of same-frequency
# stations worldwide (which bury the real result in the rankings).
_FREQ_RE = re.compile(r"\s*[-,]?\s*\d{2,3}([.,]\d)?\s*(fm|am|mhz)\b", re.IGNORECASE)


def _clean_name(name: str) -> str:
    cleaned = _FREQ_RE.sub("", name or "").strip(" -,")
    return cleaned or (name or "").strip()


def _search_query(st: Station) -> str:
    """What we type into each platform's search box: the cleaned NAME only.

    We deliberately leave the country OUT of the search string. Some platforms
    (onlineradiobox) match the whole query against station *names*, so adding
    'South Africa' pulls in every station with those words in its name and
    buries the real one. The country is applied afterwards via the relevance
    filter using each result's own country metadata.
    """
    return _clean_name(st.st_name or "")


def _relevance_query(st: Station) -> str:
    """What relevance uses: cleaned name + country, so country is enforced.

    Only name + country go here. Country is the single HARD gate in
    is_relevant(); city/state/frequency are applied separately as SOFT ranking
    signals in _best_stream() via relevance.location_score(), so they must not
    leak into the relevance query (that would turn them into a gate).
    """
    name = _clean_name(st.st_name or "")
    country = (st.st_country or "").strip()
    return f"{name}, {country}" if country else name


async def process_pending(
    config: Config,
    platform_names: list[str],
    mode: str,
    limit_stations: int,
    per_platform: int,
    dry_run: bool = False,
) -> int:
    """Process pending DB stations. Returns the number of rows updated.

    dry_run=True finds streams and logs what it would do without writing.

    Note: replacement links are fetched from onlineradiobox only (DB_PLATFORMS),
    regardless of `platform_names`. The `platform_names` argument is retained for
    CLI compatibility but ignored by the DB workflow; the query/CSV mode still
    honours the user's platform selection.
    """
    if mode == "agent":
        config.require_openai()

    if platform_names and list(platform_names) != DB_PLATFORMS:
        log.info(
            "DB workflow fetches from %s only; ignoring --platforms %s.",
            ", ".join(DB_PLATFORMS), ", ".join(platform_names),
        )

    # Import here so rule mode doesn't require OpenAI to be importable.
    from .agent import run_agent

    # Search deep enough that the right station isn't lost below a tiny limit.
    # Noisy queries (e.g. a common name on a busy frequency) can rank the correct
    # station well down the list, so we always gather several candidates and let
    # the relevance filter + playability check pick the winner.
    candidates_per_platform = max(per_platform, 6)

    updated = 0
    # Running tally shown live on the progress bar.
    tally = {"ok": 0, "new": 0, "dead": 0, "err": 0}
    with Database(config) as db:
        stations = db.fetch_pending_stations(limit=limit_stations)
        if not stations:
            log.info("No pending stations (status=0) to process.")
            return 0
        log.info("Fetched %d pending station(s).", len(stations))

        browser = BrowserManager(
            headless=config.headless, nav_timeout_ms=config.nav_timeout_ms
        )
        await browser.start()
        bar = tqdm(
            total=len(stations),
            desc="stations",
            unit="stn",
            dynamic_ncols=True,
        )
        try:
            check_page = await browser.new_page()
            for st in stations:
              # One station's failure (DB write error, browser hiccup, oversized
              # URL, ...) must not abort the whole batch - log it and move on.
              try:
                search_q = _search_query(st)   # name only -> platform search box
                rel_q = _relevance_query(st)    # name + country -> relevance filter
                dup = f"{len(st.row_ids)} row(s)"

                # 1) Is the existing stream_link still working? If so, status=1
                #    on all rows with this st_id; do NOT fill new_stream_link.
                alive = await _is_working(check_page, st.stream_link)
                if alive:
                    log.info(
                        "st_id=%s '%s': existing link OK -> status=1 (%s)",
                        st.st_id, rel_q, dup,
                    )
                    tally["ok"] += 1
                    if not dry_run:
                        n = db.mark_existing_working_by_stid(st.st_id)
                        updated += n
                    continue

                # 2) Existing is dead: search the platforms for a replacement.
                #    Rule mode: search ALL candidates, keep only those passing the
                #    name+country relevance filter, then resolve+verify each
                #    survivor's stream. Agent mode: delegate to the LLM agent.
                results: list[StationResult] = []
                for name in DB_PLATFORMS:
                    platform = get_platform(name)
                    _apply_config_to_platform(platform, config)
                    page = await browser.new_page()
                    try:
                        if mode == "rule":
                            found = await _resolve_relevant(
                                page, check_page, platform, search_q, rel_q
                            )
                        else:
                            found = await run_agent(
                                page, platform, search_q,
                                candidates_per_platform, config,
                            )
                        results.extend(found)
                    except Exception as exc:
                        log.warning("[%s] failed for '%s': %s", name, search_q, exc)
                    finally:
                        await page.close()

                best = _best_stream(rel_q, results, st)

                # 3) In agent mode results aren't pre-verified, so confirm the
                #    chosen candidate actually plays before saving it. In rule
                #    mode _resolve_relevant already verified every stream.
                if (
                    best is not None
                    and mode != "rule"
                    and not await _is_working(check_page, best.stream_url)
                ):
                    log.info(
                        "st_id=%s '%s': candidate %s did not verify, discarding",
                        st.st_id, rel_q, best.stream_url,
                    )
                    best = None

                if best is None:
                    # 4) Existing dead and no working replacement -> status=2.
                    log.info(
                        "st_id=%s '%s': no working stream -> status=2 (%s)",
                        st.st_id, rel_q, dup,
                    )
                    tally["dead"] += 1
                    if not dry_run:
                        db.mark_not_found_by_stid(st.st_id)
                    continue

                # 5) Found a working replacement -> new_stream_link + status=1 on
                #    all rows with this st_id; source = the platform it came from.
                log.info(
                    "st_id=%s '%s': new link %s [%s/%s] -> status=1 (%s)",
                    st.st_id, rel_q, best.stream_url, best.platform,
                    best.stream_type, dup,
                )
                tally["new"] += 1
                if not dry_run:
                    n = db.update_new_stream_link_by_stid(
                        st.st_id, new_stream_link=best.stream_url, source=best.platform
                    )
                    updated += n
              except Exception as exc:
                tally["err"] += 1
                log.error("st_id=%s: skipped due to error: %s", st.st_id, exc)
              finally:
                bar.update(1)
                bar.set_postfix(tally, refresh=False)
            await check_page.close()
        finally:
            bar.close()
            await browser.close()

    log.info("Done. Updated %d row(s). Tally: %s", updated, tally)
    return updated
