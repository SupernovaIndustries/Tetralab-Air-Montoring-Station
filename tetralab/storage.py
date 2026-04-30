"""Persistenza dati su SQLite.

Schema:
  - readings_minute (1440 righe/giorno)
  - readings_hour   (24 righe/giorno)
  - readings_half   (2 righe/giorno, mezzanotte e mezzogiorno locali)
  - readings_day    (1 riga/giorno, mezzanotte locale)

Tutti i timestamp sono stored come UNIX UTC (INTEGER).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable, Optional

from .config import Settings

log = logging.getLogger(__name__)


METRICS = ("pm1", "pm25", "pm4", "pm10", "rh", "temp", "voc", "nox")


def _create_table_sql(name: str) -> str:
    cols = ",\n  ".join(f"{m} REAL" for m in METRICS)
    return f"""
    CREATE TABLE IF NOT EXISTS {name} (
      ts INTEGER PRIMARY KEY,
      {cols},
      n_samples INTEGER NOT NULL DEFAULT 0
    );
    """


class Storage:
    """Wrapper SQLite thread-safe (un connection per thread via Lock)."""

    LEVELS = ("minute", "hour", "half", "day")

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- connection ----------
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,   # autocommit
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @contextmanager
    def cursor(self):
        with self._lock:
            cur = self._connect().cursor()
            try:
                yield cur
            finally:
                cur.close()

    def init_schema(self) -> None:
        with self.cursor() as c:
            for lvl in self.LEVELS:
                c.execute(_create_table_sql(f"readings_{lvl}"))
        log.info("DB inizializzato a %s", self.db_path)

    # ---------- insert ----------
    def insert(self, level: str, ts: int, values: dict, n_samples: int) -> None:
        if level not in self.LEVELS:
            raise ValueError(f"livello sconosciuto: {level}")
        cols = ", ".join(("ts", *METRICS, "n_samples"))
        placeholders = ", ".join("?" * (len(METRICS) + 2))
        params = [ts]
        for m in METRICS:
            v = values.get(m)
            params.append(float(v) if v is not None else None)
        params.append(int(n_samples))
        with self.cursor() as c:
            c.execute(
                f"INSERT OR REPLACE INTO readings_{level} ({cols}) VALUES ({placeholders})",
                params,
            )

    # ---------- query ----------
    def fetch(
        self,
        level: str,
        ts_from: Optional[int] = None,
        ts_to: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        if level not in self.LEVELS:
            raise ValueError(f"livello sconosciuto: {level}")
        sql = f"SELECT * FROM readings_{level} WHERE 1=1"
        params: list = []
        if ts_from is not None:
            sql += " AND ts >= ?"; params.append(int(ts_from))
        if ts_to is not None:
            sql += " AND ts <= ?"; params.append(int(ts_to))
        sql += " ORDER BY ts ASC"
        if limit is not None:
            sql += " LIMIT ?"; params.append(int(limit))
        with self.cursor() as c:
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]

    def latest(self, level: str) -> Optional[dict]:
        with self.cursor() as c:
            c.execute(f"SELECT * FROM readings_{level} ORDER BY ts DESC LIMIT 1")
            r = c.fetchone()
            return dict(r) if r else None

    def counts(self) -> dict[str, int]:
        out = {}
        with self.cursor() as c:
            for lvl in self.LEVELS:
                c.execute(f"SELECT COUNT(*) AS n FROM readings_{lvl}")
                out[lvl] = c.fetchone()["n"]
        return out

    def db_size_bytes(self) -> int:
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0

    def vacuum(self) -> None:
        with self.cursor() as c:
            c.execute("VACUUM")
