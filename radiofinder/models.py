"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


# CSV column order. Kept explicit so the file stays stable as fields evolve.
CSV_FIELDS = [
    "platform",
    "query",
    "name",
    "country",
    "stream_url",
    "stream_type",
    "page_url",
    "station_id",
    "mode",
    "extracted_at",
]


@dataclass
class StationResult:
    """A single extracted radio station + stream."""

    platform: str
    query: str
    name: str
    stream_url: str
    country: str = ""          # station country, when the platform exposes it
    stream_type: str = ""      # hls | mp3 | aac | external | unknown
    page_url: str = ""
    station_id: str = ""
    mode: str = ""             # rule | agent
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def dedup_key(self) -> tuple[str, str]:
        """Two results are 'the same' if platform + stream URL match."""
        return (self.platform, self.stream_url)

    def to_row(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}
