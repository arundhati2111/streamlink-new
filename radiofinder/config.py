"""Runtime configuration loaded from environment (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime
    pass


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str | None
    headless: bool
    nav_timeout_ms: int
    stream_wait_seconds: int
    output_csv: str
    # Database (local MySQL) settings
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            headless=_get_bool("HEADLESS", True),
            nav_timeout_ms=_get_int("NAV_TIMEOUT_MS", 30000),
            stream_wait_seconds=_get_int("STREAM_WAIT_SECONDS", 6),
            output_csv=os.getenv("OUTPUT_CSV", "output/streams.csv"),
            db_host=os.getenv("DB_HOST", "127.0.0.1"),
            db_port=_get_int("DB_PORT", 3306),
            db_user=os.getenv("DB_USER", "root"),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_name=os.getenv("DB_NAME", "new_schema"),
        )

    def require_openai(self) -> None:
        """Raise a clear error if agent mode is requested without credentials.

        A local OpenAI-compatible server (llama.cpp, Ollama, etc., set via
        OPENAI_BASE_URL) needs no real key, so a base_url is accepted on its own.
        """
        if not self.openai_api_key and not self.openai_base_url:
            raise RuntimeError(
                "Agent mode needs OPENAI_API_KEY (for OpenAI) or OPENAI_BASE_URL "
                "(for a local server). Set one in your environment or .env file."
            )

    @property
    def effective_openai_key(self) -> str:
        """Key to hand the OpenAI SDK, which rejects an empty string.

        Local servers ignore the key, so fall back to a harmless placeholder
        when only a base_url is configured.
        """
        return self.openai_api_key or "local"
