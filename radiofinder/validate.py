"""Stream playability validation using ffprobe.

An HTTP header check is not enough: a live Icecast/SHOUTcast stream has no
Content-Length and never "finishes", so an HTTP client that reads the whole body
times out even though the stream plays fine. ffprobe instead connects, reads
just enough to identify the audio codec, and exits - a real "does it play"
signal that also works for HLS/DASH manifests.
"""
from __future__ import annotations

import asyncio
import json
import shutil

# Resolved once: path to ffprobe, or None if it isn't installed.
_FFPROBE = shutil.which("ffprobe")


def ffprobe_available() -> bool:
    return _FFPROBE is not None


async def is_stream_playable(url: str, timeout_s: int = 20) -> bool:
    """Return True if ffprobe can decode an audio stream from `url`.

    Runs ffprobe with a bounded read timeout, no shell (args are passed as a
    list, so the URL cannot inject commands). Returns False on any error,
    non-zero exit, or absence of an audio stream. Never raises.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    if _FFPROBE is None:
        # ffprobe missing: cannot verify playability here.
        return False

    args = [
        _FFPROBE,
        "-v", "error",
        "-rw_timeout", str(timeout_s * 1_000_000),  # microseconds
        "-show_streams",
        "-print_format", "json",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
    except Exception:
        return False

    if proc.returncode != 0:
        return False
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return False
    return any(
        s.get("codec_type") == "audio" for s in data.get("streams", [])
    )
