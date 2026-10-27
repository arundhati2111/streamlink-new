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

# How old (in days) an already-processed row may get before we re-scan it, so a
# link verified a while ago can't silently rot into a dead/stale one.
STALE_AFTER_DAYS = 2


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
    st_freq: str = ""
    st_city: str = ""
    st_state: str = ""


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

    def _ensure_conn(self) -> pymysql.connections.Connection:
        """Return a live connection, reconnecting if the server dropped it.

        The DB job runs for many minutes with long scraping/ffprobe gaps between
        queries, so MySQL can close the idle connection (wait_timeout), giving
        'Lost connection to MySQL server' (error 2013) on the next query. ping()
        with reconnect=True transparently re-opens it; if even that fails we do a
        fresh connect() so the next statement runs on a healthy socket.
        """
        if self._conn is None:
            self.connect()
            return self._require_conn()
        try:
            # reconnect=True makes PyMySQL re-open the socket if it has gone away.
            self._conn.ping(reconnect=True)
        except Exception:
            # ping() itself can raise if the socket is unusable; rebuild it.
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self.connect()
        return self._require_conn()

    def fetch_pending_stations(
        self, limit: int = 50, status: int = STATUS_PENDING
    ) -> list[Station]:
        """Return distinct stations (grouped by st_id) with the given status.

        `limit` caps the number of distinct st_ids returned. Each Station carries
        the list of row ids sharing its st_id so updates hit all duplicates.
        """
        conn = self._ensure_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, st_id, st_name, st_freq, st_city, st_state,
                       st_country, stream_link
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
                    st_freq=(r.get("st_freq") or ""),
                    st_city=(r.get("st_city") or ""),
                    st_state=(r.get("st_state") or ""),
                )
                by_stid[sid] = st
            st.row_ids.append(r["id"])

        stations = list(by_stid.values())
        return stations[: int(limit)] if limit else stations

    def fetch_stations_to_process(
        self, limit: int = 50, stale_after_days: int = STALE_AFTER_DAYS
    ) -> list[Station]:
        """Return distinct stations (grouped by st_id) that need (re)checking.

        This covers two cases in one pass:
          1) PENDING rows (status=0) that have never been resolved.
          2) STALE rows: already resolved (status=1 or 2) but whose updated_dt
             is older than `stale_after_days`. Re-scanning these catches links
             that have gone dead since the last check, preventing stale links.

        Pending rows are returned first (ORDER BY status), then the oldest stale
        rows, so a backlog of new work is never starved by re-checks.
        """
        conn = self._ensure_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, st_id, st_name, st_freq, st_city, st_state,
                       st_country, stream_link
                FROM `{TABLE}`
                WHERE status = %s
                   OR (status IN (%s, %s)
                       AND updated_dt < (NOW() - INTERVAL %s DAY))
                ORDER BY status, updated_dt, st_id, id
                """,
                (
                    STATUS_PENDING,
                    STATUS_FOUND,
                    STATUS_NOT_FOUND,
                    int(stale_after_days),
                ),
            )
            rows = cur.fetchall()

        # Group rows by st_id, preserving first-seen (status/updated_dt) order.
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
                    st_freq=(r.get("st_freq") or ""),
                    st_city=(r.get("st_city") or ""),
                    st_state=(r.get("st_state") or ""),
                )
                by_stid[sid] = st
            st.row_ids.append(r["id"])

        stations = list(by_stid.values())
        return stations[: int(limit)] if limit else stations

    def _execute_write(self, sql: str, params: tuple) -> int:
        """Run a single committed write, reconnecting+retrying once if the
        connection was dropped mid-job (MySQL error 2006/2013).

        Each attempt pings/reconnects first via _ensure_conn(), so a connection
        the server closed during a long scraping gap is transparently rebuilt
        instead of raising 'Lost connection to MySQL server'. We retry once
        because the very first statement after a silent drop can still fail
        before ping() notices the socket is dead.
        """
        last_exc: Exception | None = None
        for attempt in range(2):
            conn = self._ensure_conn()
            try:
                with conn.cursor() as cur:
                    affected = cur.execute(sql, params)
                conn.commit()
                return affected
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
                # 2006 = server gone away, 2013 = lost connection during query.
                last_exc = exc
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None  # force a fresh connect on the retry
        assert last_exc is not None
        raise last_exc

    def update_new_stream_link_by_stid(
        self,
        st_id: int,
        new_stream_link: str,
        source: str,
        status: int = STATUS_FOUND,
    ) -> int:
        """Write the discovered stream to ALL rows with this st_id."""
        return self._execute_write(
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

    def mark_existing_working_by_stid(self, st_id: int) -> int:
        """Existing stream_link works: status=1 on all rows.

        The existing link is the good one, so new_stream_link/source must be
        empty - clear any value a previous run may have written, otherwise a row
        could show status=1 with a misleading replacement link.
        """
        return self._execute_write(
            f"""UPDATE `{TABLE}`
                SET status = %s,
                    new_stream_link = '',
                    source = NULL,
                    updated_dt = %s
                WHERE st_id = %s""",
            (STATUS_FOUND, _now(), int(st_id)),
        )

    def mark_not_found_by_stid(self, st_id: int) -> int:
        """Existing dead and no replacement found: status=2 on all rows.

        Also clears any new_stream_link/source left over from a previous run -
        status=2 means "no working stream", so a stale link there would be
        contradictory. (new_stream_link is NOT NULL, so it is reset to '').
        """
        return self._execute_write(
            f"""UPDATE `{TABLE}`
                SET status = %s,
                    new_stream_link = '',
                    source = NULL,
                    updated_dt = %s
                WHERE st_id = %s""",
            (STATUS_NOT_FOUND, _now(), int(st_id)),
        )
