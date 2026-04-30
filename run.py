"""Entry point for tetralab_air_quality on Raspberry Pi.

Avvia (in ordine):
  1. Storage SQLite
  2. Sensor SEN65
  3. Aggregator (thread di acquisizione 1Hz + flush minuto/ora/12h/24h)
  4. Webapp Flask (porta configurabile)

Per uso production: usa gunicorn (vedi tetralab.service).
Per dev: `python run.py` -> Flask dev server.
"""
from __future__ import annotations

import logging
import signal
import sys

from tetralab.aggregator import Aggregator
from tetralab.config import Settings
from tetralab.sensor import SEN65, SensorError, SimulatedSensor
from tetralab.storage import Storage
from tetralab.webapp import create_app


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def make_sensor(settings: Settings):
    """Init SEN65 con retry. Se allow_simulator e' True (default: False) e
    l'hardware non c'e', cade sul simulatore. Altrimenti riprova con backoff."""
    log = logging.getLogger("sensor.init")
    attempt = 0
    while True:
        attempt += 1
        try:
            s = SEN65(bus=settings.i2c_bus, address=settings.sen65_address)
            s.reset()
            s.start_measurement()
            log.info("SEN65 inizializzato al tentativo %d (serial=%s)",
                     attempt, s.read_serial_number())
            return s
        except (SensorError, OSError, FileNotFoundError) as e:
            if settings.allow_simulator:
                log.warning("SEN65 non disponibile (%s) — uso SimulatedSensor "
                            "(TETRALAB_ALLOW_SIMULATOR=1)", e)
                return SimulatedSensor()
            # backoff: 2s, 4s, 8s, ..., max 30s
            delay = min(2 ** attempt, 30)
            log.error("SEN65 init fallito al tentativo %d: %s "
                      "— ritento tra %ds", attempt, e, delay)
            import time
            time.sleep(delay)


def main() -> int:
    settings = Settings.load()
    setup_logging(settings.log_level)
    log = logging.getLogger("run")
    log.info("TetraLab Air Quality avvio. data_dir=%s", settings.data_dir)

    storage = Storage(settings)
    storage.init_schema()

    sensor = make_sensor(settings)

    aggregator = Aggregator(sensor=sensor, storage=storage, settings=settings)
    aggregator.start()

    app = create_app(settings=settings, storage=storage, aggregator=aggregator)

    def _shutdown(signum, _frame):
        log.info("ricevuto segnale %s, chiusura...", signum)
        aggregator.stop()
        try:
            sensor.stop_measurement()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("avvio webapp su 0.0.0.0:%d", settings.web_port)
    app.run(host="0.0.0.0", port=settings.web_port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
