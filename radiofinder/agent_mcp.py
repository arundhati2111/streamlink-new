"""Vision MCP agent: let a VL model drive the Playwright MCP server.

Unlike the plain agent (agent.py), which is "blind" and reads pre-shaped text
from a fixed toolset, this backend gives the model real perception:

  - It drives the official @playwright/mcp server (spawned over stdio).
  - After every action the loop attaches a fresh SCREENSHOT + accessibility
    SNAPSHOT, so a vision model (Qwen2.5-VL) literally sees the current page -
    including popups/consent dialogs it can then dismiss via browser_handle_dialog.
  - The real stream URL is harvested from browser_network_requests (the audio the
    player actually fetched), not guessed.

Requires:
  - a VISION model behind OPENAI_BASE_URL (e.g. Qwen2.5-VL via llama.cpp) that
    accepts image_url content;
  - node + @playwright/mcp available on PATH (npx).

Only used when --mode mcp. Rule/agent modes are untouched.
"""
from __future__ import annotations

import json
import os
import re

from openai import AsyncOpenAI

from .config import Config
from .browser import classify_stream
from .models import StationResult
from .platforms.base import Platform

try:  # mcp is an optional dependency; only needed for --mode mcp
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - handled at call time
    _MCP_AVAILABLE = False


# The MCP browser tools we expose to the model. Deliberately a small, focused set
# (a 7B model gets confused by 24 tools); the rest of @playwright/mcp is hidden.
# Note browser_run_code_unsafe is intentionally NOT exposed (arbitrary JS = RCE).
_EXPOSED_TOOLS = {
    "browser_navigate",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_handle_dialog",
    "browser_network_requests",
    "browser_find",
    "browser_wait_for",
}


SYSTEM_PROMPT = """You are a browser agent that finds internet radio STREAM URLs.
You SEE the page: after every action you receive a screenshot plus an
accessibility snapshot of the current page. Use them to decide what to do next.

TASK: find the station "{query}" on {platform} ({site}) and report its playable
stream URL.

HOW TO WORK:
1. browser_navigate to the search page for the station NAME.
2. LOOK at the screenshot/snapshot. If a cookie/consent/login POPUP or dialog is
   covering the page, dismiss it: use browser_handle_dialog, or click its
   accept/close button (you can see it in the screenshot).
3. Find the result that matches the station. Many stations share a name, so pick
   the one whose COUNTRY/city matches "{query}". The country may be shown on the
   result, in the station page, or in the station URL (e.g. '/za/' = South
   Africa, '/uk/' = UK). If unsure, open the station page and read it.
4. To capture the STREAM: click the station's play button, wait ~3s
   (browser_wait_for time=3), then call browser_network_requests to list network
   activity. The stream is the audio URL (ends in .m3u8/.mp3/.aac, or contains
   /stream, icecast, shoutcast). Some sites also expose it as a link/attribute -
   use browser_find or the snapshot.
5. When you have the correct station's real audio URL, call report_station with
   name, stream_url (absolute http(s)), country, and page_url. Report ONLY the
   station matching the requested country. Skip placeholder/"notcompatible" URLs.
6. If an action fails or a tool returns nothing, LOOK at the screenshot and try a
   different approach (scroll, another result, open the station page) before
   giving up. Stop after reporting the match, or when no true match exists.
"""


def _mcp_result_to_blocks(result) -> list[dict]:
    """Convert an MCP call result into OpenAI chat 'content' blocks.

    Text stays text; images become image_url data-URLs the VL model can see.
    """
    blocks: list[dict] = []
    for item in getattr(result, "content", None) or []:
        itype = getattr(item, "type", None)
        if itype == "text":
            text = getattr(item, "text", "") or ""
            if text:
                blocks.append({"type": "text", "text": text[:6000]})
        elif itype == "image":
            data = getattr(item, "data", "") or ""
            mime = getattr(item, "mime_type", None) or "image/png"
            if data:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    }
                )
    if not blocks:
        blocks.append({"type": "text", "text": "(no content returned)"})
    return blocks


def _report_station_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "report_station",
            "description": (
                "Record the matching station and its playable stream URL. Call "
                "once you have confirmed the correct-country station's real audio "
                "URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "stream_url": {
                        "type": "string",
                        "description": "Absolute http(s) audio stream URL.",
                    },
                    "country": {"type": "string"},
                    "page_url": {"type": "string"},
                },
                "required": ["name", "stream_url"],
            },
        },
    }


def _mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """Translate the exposed MCP tools into OpenAI tool-call schemas."""
    out: list[dict] = []
    for t in mcp_tools:
        if t.name not in _EXPOSED_TOOLS:
            continue
        schema = (
            getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        )
        # OpenAI requires an object schema with 'properties'.
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or t.name)[:400],
                    "parameters": schema,
                },
            }
        )
    out.append(_report_station_schema())
    return out


def _prune_old_images(messages: list[dict], keep_last: int = 2) -> None:
    """Drop image blocks from all but the most recent `keep_last` messages.

    Screenshots are token-heavy; keeping every one would blow the context
    window within a few steps. The model only needs to see the CURRENT page, so
    older screenshots are replaced with a short placeholder while their text is
    preserved.
    """
    seen = 0
    for m in reversed(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        )
        if not has_image:
            continue
        seen += 1
        if seen <= keep_last:
            continue
        m["content"] = [
            b
            if not (isinstance(b, dict) and b.get("type") == "image_url")
            else {"type": "text", "text": "[older screenshot omitted]"}
            for b in content
        ]


def _dedup(
    reported: list[dict], limit: int, platform: str, query: str
) -> list[StationResult]:
    results: list[StationResult] = []
    seen: set[str] = set()
    for r in reported[:limit]:
        url = (r.get("stream_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        page_url = (r.get("page_url") or "").strip()
        if re.search(r"/search|[?&]q=|[?&]query=", page_url):
            page_url = ""
        results.append(
            StationResult(
                platform=platform,
                query=query,
                name=(r.get("name") or "").strip(),
                country=(r.get("country") or "").strip(),
                stream_url=url,
                stream_type=classify_stream(url),
                page_url=page_url,
                mode="mcp",
            )
        )
    return results


async def run_agent_mcp(
    platform: Platform,
    query: str,
    limit: int,
    config: Config,
    max_steps: int = 24,
) -> list[StationResult]:
    """Drive @playwright/mcp with a vision model to extract streams for `query`."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "MCP mode needs the 'mcp' package. Install it: pip install mcp"
        )
    config.require_openai()

    debug = os.getenv("AGENT_DEBUG", "").lower() in {"1", "true", "yes"}
    client = AsyncOpenAI(
        api_key=config.effective_openai_key, base_url=config.openai_base_url
    )
    reported: list[dict] = []

    server = StdioServerParameters(
        command="npx",
        args=["-y", "@playwright/mcp@latest", "--headless", "--isolated"],
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = _mcp_tools_to_openai(mcp_tools)

            messages: list[dict] = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        query=query,
                        platform=platform.name,
                        site=platform.search_url(query),
                    ),
                },
                {
                    "role": "user",
                    "content": f"Find '{query}' on {platform.name}. Start now.",
                },
            ]
            # Qwen thinking mode wastes the turn on reasoning; disable it.
            extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

            for step in range(max_steps):
                _prune_old_images(messages)
                resp = await client.chat.completions.create(
                    model=config.openai_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0,
                    extra_body=extra_body,
                )
                msg = resp.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                if not msg.tool_calls:
                    if debug and msg.content:
                        print(f"[mcp:{platform.name}] final: {msg.content[:300]}")
                    break

                for call in msg.tool_calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    if name == "report_station":
                        reported.append(args)
                        content = [{"type": "text", "text": json.dumps({"ok": True})}]
                        if debug:
                            print(
                                f"[mcp:{platform.name}] step={step} "
                                f"report_station({args})"
                            )
                    else:
                        try:
                            result = await session.call_tool(name, args)
                            content = _mcp_result_to_blocks(result)
                        except Exception as exc:
                            content = [
                                {
                                    "type": "text",
                                    "text": json.dumps({"error": str(exc)}),
                                }
                            ]
                        if debug:
                            preview = next(
                                (b.get("text", "[image]") for b in content), ""
                            )
                            print(
                                f"[mcp:{platform.name}] step={step} "
                                f"{name}({args}) -> {str(preview)[:160]}"
                            )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": content,
                        }
                    )

                if len(reported) >= limit:
                    break

                # Keep the model "seeing" the page: unless it just looked at the
                # page itself, attach a fresh screenshot so its next decision is
                # grounded in what is currently on screen (popups included).
                called = {c.function.name for c in msg.tool_calls}
                looked = called & {"browser_take_screenshot", "browser_snapshot"}
                if not looked and "report_station" not in called:
                    try:
                        shot = await session.call_tool(
                            "browser_take_screenshot", {"type": "jpeg"}
                        )
                        blocks = _mcp_result_to_blocks(shot)
                        if any(b.get("type") == "image_url" for b in blocks):
                            messages.append(
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Current page screenshot:",
                                        },
                                        *[
                                            b
                                            for b in blocks
                                            if b.get("type") == "image_url"
                                        ],
                                    ],
                                }
                            )
                    except Exception:
                        pass  # screenshot is best-effort context, never fatal

    return _dedup(reported, limit, platform.name, query)
