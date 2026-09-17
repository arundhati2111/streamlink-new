"""AI agent mode: let gpt-4o-mini drive Playwright to find and extract streams.

The model is given the platform's search URL and an agent_hint describing where
stream URLs live, plus a fixed toolset (navigate/get_links/click/get_streams/
fetch_json/report_station). It loops until it reports enough stations or hits a
step budget.
"""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI
from playwright.async_api import Page

from .agent_tools import AgentTools, TOOL_SCHEMA
from .browser import classify_stream
from .config import Config
from .models import StationResult
from .platforms.base import Platform

SYSTEM_PROMPT = """You are a web-scraping agent that extracts internet radio stream URLs.
You control a real browser through tools.

THE MOST IMPORTANT INSTRUCTION - the platform hint tells you exactly where the
stream URL lives. Follow it literally:

>>> PLATFORM HINT: {hint}

Recommended procedure:
1. navigate() to the search URL: {search_url}
2. Locate the matching station(s) for the query.
   - If the hint mentions a DOM attribute (e.g. a 'stream' attribute on elements
     matching a CSS class), use query_attributes(selector, attributes) to read it
     directly. For example query_attributes with selector ".b-play.station_play"
     and attributes ["stream","radioname"].
   - If the hint says the stream only appears after clicking play, use click()
     then get_streams() to read the audio URL from the network.
   - If the hint mentions a JSON resolver API, use fetch_json(url) on that API.
   - Use get_links(contains) only to discover station page URLs (e.g. "/s/",
     "/radio/", "/radios/"); do NOT expect stream URLs from get_links.
3. RELEVANCE - report ONLY the station the user actually asked for, but do NOT
   reject a name match without checking its country first.
   - Match on the station's distinctive NAME (and city, if given), not on
     generic words like "radio"/"fm" or the dial number alone.
   - A result that ONLY shares the frequency but has a clearly different name is
     not a match. But a result whose NAME matches (e.g. "Heart FM") must be
     COUNTRY-CHECKED before you accept or reject it - never decide by guessing.
   COUNTRY - when the query names a country you MUST confirm each name-matching
   candidate's real country. It is almost never a guess from the list text.
   Resolve it, using whichever of these the platform offers (see the hint):
   - A country code in the station URL/_href path, e.g. onlineradiobox
     '/za/heartfm/' = South Africa, '/uk/...' = UK, '/us/...' = USA.
   - A country/location field in a JSON API result (e.g. radio.net 'country',
     TuneIn Describe.ashx 'location' = "City, Country").
   - Otherwise OPEN the station page: navigate() to its _href / page URL and
     get_text() on the page to read the city/country before deciding.
   Several results often share the SAME name (many "Heart FM"s worldwide). Work
   through them and keep the one whose resolved country matches the query. Do
   NOT stop at the first few - the correct-country one may be further down the
   list, so check candidates until you find it or the list is exhausted.
4. For each true match call report_station(name, stream_url, page_url,
   country, station_id) with an absolute http(s) stream URL. ALWAYS include
   page_url (the station's own page, e.g. '/za/heartfm/'), and set country to
   the RESOLVED country (from URL code, JSON field, or the station page) - not a
   guess. A placeholder/"not compatible" URL is NOT a stream - skip it.
5. PERSIST - do not give up early. If a tool returns empty or errors:
   - Retry the same step once (transient resets/timeouts happen), then try the
     ALTERNATIVE the hint suggests (e.g. DOM attribute -> JSON API -> open the
     station page and click play + get_streams).
   - If the search list looks irrelevant, open the most likely name-matching
     station page and verify there before concluding "no match".
   Only report "no match" after you have country-checked every name-matching
   candidate. Stop once you have reported {limit} matching station(s) or truly
   exhausted the options, then give a one-line summary.

Platform: {platform}
Target query: "{query}"
Report at most {limit} station(s) that truly match the query.
"""


async def run_agent(
    page: Page,
    platform: Platform,
    query: str,
    limit: int,
    config: Config,
    max_steps: int = 20,
) -> list[StationResult]:
    config.require_openai()
    client = AsyncOpenAI(
        api_key=config.effective_openai_key, base_url=config.openai_base_url
    )
    tools = AgentTools(page)

    messages: list[dict] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                platform=platform.name,
                search_url=platform.search_url(query),
                hint=platform.agent_hint,
                query=query,
                limit=limit,
            ),
        },
        {
            "role": "user",
            "content": f"Find '{query}' on {platform.name} and extract streams.",
        },
    ]

    debug = os.getenv("AGENT_DEBUG", "").lower() in {"1", "true", "yes"}

    # Qwen (and similar) models default to a "thinking" mode that emits long
    # reasoning and can end a turn with no tool call, which would stall the loop.
    # Disable it for tool use. extra_body is ignored by servers that don't
    # support it (e.g. the real OpenAI API), so this is safe for both backends.
    extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

    nudged = False
    for step in range(max_steps):
        response = await client.chat.completions.create(
            model=config.openai_model,
            messages=messages,
            tools=TOOL_SCHEMA,
            tool_choice="auto",
            temperature=0,
            extra_body=extra_body,
        )
        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            # A turn with neither a tool call nor any text is usually the model
            # stalling (e.g. spent its budget thinking). Nudge it once to act
            # before treating an empty turn as a real "I'm done".
            if not (msg.content or "").strip() and not nudged:
                nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue using the tools. If you have navigated to "
                            "the search page, call query_attributes now to read "
                            "the results, then report_station for the match."
                        ),
                    }
                )
                continue
            if debug and msg.content:
                print(f"[agent:{platform.name}] final: {msg.content[:400]}")
            break  # model produced a final answer

        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await tools.dispatch(call.function.name, args)
            if debug:
                print(
                    f"[agent:{platform.name}] step={step} "
                    f"{call.function.name}({args}) -> {result[:200]}"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )

        # The model acted this turn, so allow another nudge if it stalls later.
        nudged = False

        if len(tools.reported) >= limit:
            break

    # Convert reported stations to StationResult rows.
    results: list[StationResult] = []
    seen: set[str] = set()
    for r in tools.reported[:limit]:
        url = r["stream_url"]
        if url in seen:
            continue
        seen.add(url)
        results.append(
            StationResult(
                platform=platform.name,
                query=query,
                name=r.get("name", ""),
                country=r.get("country", ""),
                stream_url=url,
                stream_type=classify_stream(url),
                page_url=r.get("page_url", ""),
                station_id=r.get("station_id", ""),
                mode="agent",
            )
        )
    return results
