"""Configurazione globale dell'applicazione.

Le impostazioni vengono caricate da variabili d'ambiente (con default sensati).
Persistenza su filesystem: tutto sotto `data_dir` (default /var/lib/tetralab).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Identita' ---
    device_name: str = "TetraLab Air Quality"
    issuer: str = "TetraLab"
    account: str = "AirQuality"

    # --- I2C / sensore ---
    i2c_bus: int = 1
    sen65_address: int = 0x6B

    # --- Acquisizione ---
    sample_period_s: float = 1.0       # ogni N secondi leggi sensore
    minute_samples: int = 60           # media minuto = 60 campioni
    timezone_name: str = "Europe/Rome" # allineamento aggregazioni

    # --- Storage ---
    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("TETRALAB_DATA_DIR", "/var/lib/tetralab")
    ))

    # --- Web ---
    web_port: int = 5000
    secret_key_file: str = "secret_key.bin"
    totp_secret_file: str = "totp_secret.bin"
    totp_provisioned_file: str = "totp_provisioned"
    session_lifetime_min: int = 60

    # --- Misc ---
    log_level: str = "INFO"
    allow_simulator: bool = False  # default: NO simulatore. Su Pi vogliamo dati veri.
                                   # Per dev su Mac: TETRALAB_ALLOW_SIMULATOR=1

    @classmethod
    def load(cls) -> "Settings":
        s = cls(
            i2c_bus=_env_int("TETRALAB_I2C_BUS", 1),
            web_port=_env_int("TETRALAB_PORT", 5000),
            log_level=os.environ.get("TETRALAB_LOG_LEVEL", "INFO"),
            allow_simulator=_env_bool("TETRALAB_ALLOW_SIMULATOR", False),
            timezone_name=os.environ.get("TETRALAB_TZ", "Europe/Rome"),
        )
        if env_dir := os.environ.get("TETRALAB_DATA_DIR"):
            s.data_dir = Path(env_dir)
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s

    # ----- Helpers per file di stato -----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "tetralab.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / self.secret_key_file

    @property
    def totp_secret_path(self) -> Path:
        return self.data_dir / self.totp_secret_file

    @property
    def totp_provisioned_path(self) -> Path:
        return self.data_dir / self.totp_provisioned_file

    def get_or_create_secret_key(self) -> bytes:
        p = self.secret_key_path
        if not p.exists():
            p.write_bytes(secrets.token_bytes(32))
            try:
                p.chmod(0o600)
            except OSError:
                pass
        return p.read_bytes()
