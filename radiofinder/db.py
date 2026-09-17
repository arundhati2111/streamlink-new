"""MySQL access for the down_station_automation table.

The table can hold duplicate rows per station (same st_id). The worker treats a
station as a unit: it processes each distinct st_id once and writes the result
back to EVERY row sharing that st_id.

All SQL uses parameterised queries (never string interpolation of values) to
avoid injection. Credentials come from Config (env / .env), never hardcoded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pymysql
from pymysql.cursors import DictCursor

from .config import Config

TABLE = "down_station_automation"

# status values used by the worker
STATUS_PENDING = 0     # needs verifying / a new stream link
STATUS_FOUND = 1       # existing works OR new_stream_link filled in
STATUS_NOT_FOUND = 2   # existing dead and no replacement found


@dataclass
class Station:
    """One distinct station (by st_id) to re-check.

    row_ids lists every table row that shares this st_id, so updates can touch
    all of them.
    """

    st_id: int
    st_name: str
    st_country: str
    stream_link: str
    row_ids: list[int]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Thin PyMySQL wrapper scoped to the station-automation table."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._conn: pymysql.connections.Connection | None = None

    def connect(self) -> None:
        self._conn = pymysql.connect(
            host=self._config.db_host,
            port=self._config.db_port,
            user=self._config.db_user,
            password=self._config.db_password,
            database=self._config.db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require_conn(self) -> pymysql.connections.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    def fetch_pending_stations(
        self, limit: int = 50, status: int = STATUS_PENDING
    ) -> list[Station]:
        """Return distinct stations (grouped by st_id) with the given status.

        `limit` caps the number of distinct st_ids returned. Each Station carries
        the list of row ids sharing its st_id so updates hit all duplicates.
        """
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, st_id, st_name, st_country, stream_link
                FROM `{TABLE}`
                WHERE status = %s
                ORDER BY st_id, id
                """,
                (status,),
            )
            rows = cur.fetchall()

        # Group rows by st_id, preserving first-seen order.
        by_stid: dict[int, Station] = {}
        for r in rows:
            sid = r["st_id"]
            st = by_stid.get(sid)
            if st is None:
                st = Station(
                    st_id=sid,
                    st_name=r["st_name"],
                    st_country=r["st_country"],
                    stream_link=r["stream_link"],
                    row_ids=[],
                )
                by_stid[sid] = st
            st.row_ids.append(r["id"])

        stations = list(by_stid.values())
        return stations[: int(limit)] if limit else stations

    def update_new_stream_link_by_stid(
        self,
        st_id: int,
        new_stream_link: str,
        source: str,
        status: int = STATUS_FOUND,
    ) -> int:
        """Write the discovered stream to ALL rows with this st_id."""
        conn = self._require_conn()
        with conn.cursor() as cur:
            affected = cur.execute(
                f"""
                UPDATE `{TABLE}`
                SET new_stream_link = %s,
                    source = %s,
                    status = %s,
                    updated_dt = %s
                WHERE st_id = %s
                """,
                (new_stream_link, source, status, _now(), int(st_id)),
            )
        conn.commit()
        return affected

    def mark_existing_working_by_stid(self, st_id: int) -> int:
        """Existing stream_link works: status=1 on all rows.

        The existing link is the good one, so new_stream_link/source must be
        empty - clear any value a previous run may have written, otherwise a row
        could show status=1 with a misleading replacement link.
        """
        conn = self._require_conn()
        with conn.cursor() as cur:
            affected = cur.execute(
                f"""UPDATE `{TABLE}`
                    SET status = %s,
                        new_stream_link = '',
                        source = NULL,
                        updated_dt = %s
                    WHERE st_id = %s""",
                (STATUS_FOUND, _now(), int(st_id)),
            )
        conn.commit()
        return affected

    def mark_not_found_by_stid(self, st_id: int) -> int:
        """Existing dead and no replacement found: status=2 on all rows.

        Also clears any new_stream_link/source left over from a previous run -
        status=2 means "no working stream", so a stale link there would be
        contradictory. (new_stream_link is NOT NULL, so it is reset to '').
        """
        conn = self._require_conn()
        with conn.cursor() as cur:
            affected = cur.execute(
                f"""UPDATE `{TABLE}`
                    SET status = %s,
                        new_stream_link = '',
                        source = NULL,
                        updated_dt = %s
                    WHERE st_id = %s""",
                (STATUS_NOT_FOUND, _now(), int(st_id)),
            )
        conn.commit()
        return affected
