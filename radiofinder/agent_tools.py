"""Playwright actions exposed to the LLM as callable tools (agent mode).

Each tool takes JSON-serialisable args and returns a JSON-serialisable result,
so it can be wired straight into OpenAI tool-calling. A single AgentTools
instance wraps one Playwright page plus a stream sniffer.
"""
from __future__ import annotations

import json
import re

from playwright.async_api import Page

from .browser import StreamSniffer, dismiss_consent, goto

# The tool schema advertised to the model.
TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Load a URL in the browser and dismiss any cookie/consent dialog.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_links",
            "description": (
                "Return anchor hrefs on the current page, optionally filtered by a "
                "substring (e.g. '/s/' or '/radio/'). Use to find station page links."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contains": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_attributes",
            "description": (
                "Return attributes of elements matching a CSS selector. Useful to read "
                "'stream'/'data-url'/'radioname' attributes. Each returned object also "
                "includes '_href' (the nearest link URL, usable as page_url) and "
                "'_text'. Returns a list of objects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "attributes": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                "required": ["selector", "attributes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the first element matching a CSS selector or visible text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_streams",
            "description": (
                "Return audio stream URLs observed on the network since the last "
                "navigate (e.g. after clicking play). Prefer calling after a play click."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_json",
            "description": (
                "Fetch a URL from within the page context and return parsed JSON. Use "
                "for resolver APIs like TuneIn's Tune.ashx?id=...&render=json."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_text",
            "description": "Return trimmed text of the first element matching a selector (e.g. 'h1').",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_station",
            "description": (
                "Record a found station and its stream URL. Call once per station you "
                "successfully extract. This is how results are saved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stream_url": {"type": "string"},
                    "page_url": {
                        "type": "string",
                        "description": "The station's own page URL, NOT the search results page.",
                    },
                    "country": {
                        "type": "string",
                        "description": "The station's country, if shown on the page.",
                    },
                    "station_id": {"type": "string"},
                },
                "required": ["name", "stream_url"],
            },
        },
    },
]


class AgentTools:
    """Executes the tool calls the model requests against a single page."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.sniffer = StreamSniffer()
        self.sniffer.attach(page)
        self.reported: list[dict] = []

    async def dispatch(self, name: str, args: dict) -> str:
        """Run a tool by name; always returns a JSON string for the model."""
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            result = await handler(args or {})
        except Exception as exc:  # never crash the loop on a tool error
            return json.dumps({"error": str(exc)})
        return json.dumps(result)

    async def _tool_navigate(self, args: dict) -> dict:
        self.sniffer.hits.clear()
        # Some sites (streema) intermittently reset the connection on the first
        # hit; retry a couple of times before giving up, mirroring the rule
        # extractors' behaviour so the agent doesn't abandon a good result.
        last_exc = None
        for attempt in range(3):
            try:
                await goto(self.page, args["url"])
                await dismiss_consent(self.page)
                return {"ok": True, "url": self.page.url}
            except Exception as exc:
                last_exc = exc
                await self.page.wait_for_timeout(1000 * (attempt + 1))
        return {"ok": False, "error": str(last_exc), "url": args.get("url", "")}

    async def _tool_get_links(self, args: dict) -> dict:
        contains = args.get("contains") or ""
        limit = int(args.get("limit") or 30)
        hrefs = await self.page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )
        out, seen = [], set()
        for h in hrefs:
            if contains and contains not in h:
                continue
            if h in seen:
                continue
            seen.add(h)
            out.append(h)
            if len(out) >= limit:
                break
        return {"links": out}

    async def _tool_query_attributes(self, args: dict) -> dict:
        selector = args["selector"]
        attributes = args["attributes"]
        limit = int(args.get("limit") or 20)
        rows = await self.page.evaluate(
            """({selector, attributes, limit}) => {
                const els = Array.from(document.querySelectorAll(selector)).slice(0, limit);
                return els.map(el => {
                    const o = {};
                    for (const a of attributes) o[a] = el.getAttribute(a);
                    o._text = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
                    // Nearest station link, useful as page_url AND for reading the
                    // country from the URL path (e.g. onlineradiobox /za/heartfm/).
                    // The play element often isn't the link itself, so also look
                    // at ancestor <a> and the enclosing result row (li/tr/div).
                    let a = el.closest('a[href]') || el.querySelector('a[href]');
                    if (!a) {
                        const row = el.closest('li, tr, .stations__station, .search-station, .station');
                        if (row) a = row.querySelector('a[href]');
                    }
                    o._href = a ? a.href : '';
                    return o;
                });
            }""",
            {"selector": selector, "attributes": attributes, "limit": limit},
        )
        return {"elements": rows}

    async def _tool_click(self, args: dict) -> dict:
        selector = args.get("selector")
        text = args.get("text")
        if selector:
            await self.page.locator(selector).first.click(timeout=5000)
        elif text:
            await self.page.get_by_text(text, exact=False).first.click(timeout=5000)
        else:
            return {"ok": False, "error": "need selector or text"}
        await self.page.wait_for_timeout(500)
        return {"ok": True}

    async def _tool_get_streams(self, args: dict) -> dict:
        # Give the network a beat to emit stream requests.
        await self.page.wait_for_timeout(1500)
        return {"streams": list(dict.fromkeys(self.sniffer.hits))[:20]}

    #: max list items returned from a JSON API to the model (a search can return
    #: hundreds of stations; the model only needs the top matches, and the full
    #: blob easily overflows the context window).
    _JSON_LIST_CAP = 20

    def _cap_json(self, data):
        """Trim large result lists so a big API response can't blow the context.

        Truncates the first long list found under a common results key (or a
        top-level list) to _JSON_LIST_CAP items and notes how many were dropped.
        """
        cap = self._JSON_LIST_CAP
        if isinstance(data, list):
            if len(data) > cap:
                return data[:cap] + [{"__truncated": len(data) - cap}]
            return data
        if isinstance(data, dict):
            for key in ("playables", "body", "results", "stations", "data", "items"):
                val = data.get(key)
                if isinstance(val, list) and len(val) > cap:
                    data = dict(data)
                    data[key] = val[:cap]
                    data["__truncated"] = {key: len(val) - cap}
                    break
        return data

    async def _tool_fetch_json(self, args: dict) -> dict:
        # Use Playwright's request API to avoid CORS restrictions on cross-origin
        # resolver endpoints (e.g. opml.radiotime.com).
        try:
            resp = await self.page.request.get(args["url"])
            data = await resp.json()
        except Exception as exc:
            return {"data": {"__error": str(exc)}}
        return {"data": self._cap_json(data)}

    async def _tool_get_text(self, args: dict) -> dict:
        txt = await self.page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return el ? el.textContent.replace(/\\s+/g,' ').trim() : ''; }",
            args["selector"],
        )
        return {"text": txt}

    async def _tool_report_station(self, args: dict) -> dict:
        stream = (args.get("stream_url") or "").strip()
        if not stream or not re.match(r"^https?://", stream):
            return {"ok": False, "error": "stream_url must be an absolute http(s) URL"}
        page_url = (args.get("page_url") or "").strip()
        # A search results page is not a station page; drop it so we don't store
        # a misleading page_url.
        if re.search(r"/search|[?&]q=|[?&]query=", page_url):
            page_url = ""
        self.reported.append(
            {
                "name": (args.get("name") or "").strip(),
                "stream_url": stream,
                "page_url": page_url,
                "country": (args.get("country") or "").strip(),
                "station_id": (args.get("station_id") or "").strip(),
            }
        )
        return {"ok": True, "count": len(self.reported)}
