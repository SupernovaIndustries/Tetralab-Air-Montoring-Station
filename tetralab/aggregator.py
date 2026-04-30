"""Background thread che:
  - legge il sensore a sample_period_s (default 1 Hz)
  - tiene 4 accumulator: minuto / ora / 12h / 24h (allineati alla mezzanotte locale)
  - flusha su DB quando il bucket cambia o quando minute_samples e' raggiunto
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings
from .sensor import Reading
from .storage import METRICS, Storage

log = logging.getLogger(__name__)


@dataclass
class Bucket:
    """Accumulator per un livello (somma + count)."""
    start_ts: int = 0
    n: int = 0
    sums: dict = field(default_factory=lambda: {m: 0.0 for m in METRICS})

    def reset(self, start_ts: int) -> None:
        self.start_ts = start_ts
        self.n = 0
        self.sums = {m: 0.0 for m in METRICS}

    def add(self, r: Reading) -> None:
        d = r.as_dict()
        added = False
        for m in METRICS:
            v = d.get(m)
            if v is None:
                continue
            self.sums[m] += float(v)
            added = True
        if added:
            self.n += 1

    def averages(self) -> dict:
        if self.n == 0:
            return {m: None for m in METRICS}
        return {m: self.sums[m] / self.n for m in METRICS}


def _floor_to(ts: int, period_s: int) -> int:
    return (ts // period_s) * period_s


class Aggregator:
    """Thread di acquisizione + aggregazione."""

    def __init__(self, *, sensor, storage: Storage, settings: Settings):
        self.sensor = sensor
        self.storage = storage
        self.settings = settings
        self._tz = ZoneInfo(settings.timezone_name)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Stato live (ultimo campione raw)
        self._lock = threading.Lock()
        self._last_reading: Optional[Reading] = None
        self._last_reading_ts: int = 0
        self._sample_count: int = 0

        # Accumulator
        self._minute = Bucket()
        self._hour   = Bucket()
        self._half   = Bucket()
        self._day    = Bucket()

    # ---------- API thread ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aggregator", daemon=True)
        self._thread.start()
        log.info("Aggregator avviato")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("Aggregator fermato")

    # ---------- API live ----------
    def get_live(self) -> dict:
        with self._lock:
            r = self._last_reading
            ts = self._last_reading_ts
            n = self._sample_count
        return {
            "ts": ts,
            "samples_total": n,
            "values": r.as_dict() if r else {m: None for m in METRICS},
        }

    # ---------- helpers di allineamento (in locale) ----------
    def _hour_start(self, ts: int) -> int:
        local = datetime.fromtimestamp(ts, tz=self._tz).replace(minute=0, second=0, microsecond=0)
        return int(local.timestamp())

    def _half_start(self, ts: int) -> int:
        local = datetime.fromtimestamp(ts, tz=self._tz)
        h = 0 if local.hour < 12 else 12
        local = local.replace(hour=h, minute=0, second=0, microsecond=0)
        return int(local.timestamp())

    def _day_start(self, ts: int) -> int:
        local = datetime.fromtimestamp(ts, tz=self._tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(local.timestamp())

    # ---------- main loop ----------
    def _run(self) -> None:
        period = self.settings.sample_period_s
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.exception("errore nel ciclo: %s", e)
            next_tick += period
            wait = next_tick - time.monotonic()
            if wait > 0:
                self._stop.wait(wait)
            else:
                # in ritardo, salta avanti per non accumulare
                next_tick = time.monotonic()

    def _tick(self) -> None:
        try:
            r = self.sensor.read_measured_values()
        except Exception as e:
            log.warning("read sensore fallita: %s", e)
            return

        now = int(time.time())
        with self._lock:
            self._last_reading = r
            self._last_reading_ts = now
            self._sample_count += 1

        # Inizializza il bucket minuto al primo campione
        if self._minute.n == 0 and self._minute.start_ts == 0:
            self._minute.reset(now - (now % 60))   # allinea al minuto
        self._minute.add(r)

        # Quando ho minute_samples campioni -> chiudi minuto
        if self._minute.n >= self.settings.minute_samples:
            self._flush_minute(now)

    def _flush_minute(self, now: int) -> None:
        """Chiude il bucket minuto -> insert nel DB + alimenta i livelli superiori."""
        avg = self._minute.averages()
        ts_min = self._minute.start_ts
        n_samples = self._minute.n

        try:
            self.storage.insert("minute", ts_min, avg, n_samples)
        except Exception as e:
            log.exception("insert minute fallito: %s", e)

        # Alimenta i livelli superiori usando una "Reading-like" coi valori medi.
        synthetic = Reading(**avg)

        for level, bucket, start_fn in (
            ("hour", self._hour, self._hour_start),
            ("half", self._half, self._half_start),
            ("day",  self._day,  self._day_start),
        ):
            target_start = start_fn(ts_min)
            if bucket.n > 0 and bucket.start_ts != target_start:
                # Cambio bucket: scrivi quello vecchio
                old_avg = bucket.averages()
                try:
                    self.storage.insert(level, bucket.start_ts, old_avg, bucket.n)
                except Exception as e:
                    log.exception("insert %s fallito: %s", level, e)
                bucket.reset(target_start)
            elif bucket.n == 0:
                bucket.reset(target_start)
            bucket.add(synthetic)

            # Inoltre: scrivi continuamente l'aggregato corrente (cosi' il dashboard
            # ha sempre l'ultimo valore parziale aggiornato a fine minuto)
            try:
                self.storage.insert(level, bucket.start_ts, bucket.averages(), bucket.n)
            except Exception as e:
                log.exception("upsert %s fallito: %s", level, e)

        # Reset bucket minuto, prossimo blocco
        self._minute.reset(now - (now % 60))

        log.debug("minuto %s flushato (n=%d): %s", ts_min, n_samples, avg)
