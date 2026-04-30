"""Driver per Sensirion SEN65 (PM, RH, T, VOC, NOx) via I2C usando smbus2.

Implementazione minimale basata sul datasheet SEN6x.
Per testing su Mac/Linux senza hardware c'e' SimulatedSensor.
"""
from __future__ import annotations

import math
import random
import struct
import time
from dataclasses import dataclass
from typing import Optional


# --- Codici comando SEN65 (16-bit, big endian) ---
CMD_START_MEASUREMENT     = 0x0021
CMD_STOP_MEASUREMENT      = 0x0104
CMD_READ_DATA_READY       = 0x0202
CMD_READ_MEASURED_VALUES  = 0x0471   # 8 valori per SEN65
CMD_GET_SERIAL_NUMBER     = 0xD033
CMD_DEVICE_RESET          = 0xD304


class SensorError(Exception):
    """Errore generico nella comunicazione col sensore."""


@dataclass
class Reading:
    """Singolo campione del SEN65. Tutti i valori sono float; None se invalido."""
    pm1: Optional[float]    # ug/m3
    pm25: Optional[float]
    pm4: Optional[float]
    pm10: Optional[float]
    rh: Optional[float]     # %
    temp: Optional[float]   # °C
    voc: Optional[float]    # index 1-500
    nox: Optional[float]    # index 1-500

    def as_dict(self) -> dict:
        return {
            "pm1": self.pm1, "pm25": self.pm25, "pm4": self.pm4, "pm10": self.pm10,
            "rh": self.rh, "temp": self.temp, "voc": self.voc, "nox": self.nox,
        }

    def is_valid(self) -> bool:
        return all(v is not None and not math.isnan(v) for v in self.as_dict().values())


def crc8_sensirion(data: bytes) -> int:
    """CRC-8 con poly 0x31, init 0xFF (Sensirion standard)."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


class SEN65:
    """Driver SEN65 minimale via smbus2."""

    def __init__(self, bus: int = 1, address: int = 0x6B):
        # smbus2 e' importato lazy: non bloccare import su Mac
        try:
            import smbus2  # type: ignore
        except ImportError as e:
            raise SensorError(f"smbus2 non installato: {e}") from e
        self._smbus2 = smbus2
        self._bus_num = bus
        self._address = address
        self._bus = smbus2.SMBus(bus)

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass

    def is_simulated(self) -> bool:
        return False

    # ----- I/O low-level -----
    def _write_cmd(self, cmd: int, args: bytes = b"") -> None:
        payload = struct.pack(">H", cmd) + args
        msg = self._smbus2.i2c_msg.write(self._address, list(payload))
        self._bus.i2c_rdwr(msg)

    def _read(self, n: int) -> bytes:
        msg = self._smbus2.i2c_msg.read(self._address, n)
        self._bus.i2c_rdwr(msg)
        return bytes(msg)

    def _read_words(self, n_words: int) -> list[int]:
        """Legge n_words parole da 16 bit (3 byte ciascuna: 2 dati + CRC)."""
        raw = self._read(n_words * 3)
        out = []
        for i in range(0, len(raw), 3):
            word = raw[i:i+2]
            crc = raw[i+2]
            if crc8_sensirion(word) != crc:
                raise SensorError(f"CRC mismatch a byte {i}")
            out.append(struct.unpack(">H", word)[0])
        return out

    # ----- Comandi -----
    def reset(self) -> None:
        self._write_cmd(CMD_DEVICE_RESET)
        time.sleep(1.2)

    def stop_measurement(self) -> None:
        self._write_cmd(CMD_STOP_MEASUREMENT)
        time.sleep(1.0)

    def start_measurement(self) -> None:
        self._write_cmd(CMD_START_MEASUREMENT)
        time.sleep(0.05)
        # Primo dato disponibile dopo ~1.1 s
        time.sleep(1.1)

    def read_serial_number(self) -> str:
        self._write_cmd(CMD_GET_SERIAL_NUMBER)
        time.sleep(0.02)
        # 32 caratteri ASCII = 16 word
        words = self._read_words(16)
        chars = []
        for w in words:
            chars.append(chr((w >> 8) & 0xFF))
            chars.append(chr(w & 0xFF))
        return "".join(chars).rstrip("\x00")

    def is_data_ready(self) -> bool:
        self._write_cmd(CMD_READ_DATA_READY)
        time.sleep(0.02)
        words = self._read_words(1)
        return (words[0] & 0x01) == 1

    def read_measured_values(self) -> Reading:
        """Legge gli 8 valori del SEN65."""
        self._write_cmd(CMD_READ_MEASURED_VALUES)
        time.sleep(0.02)
        words = self._read_words(8)

        # PM (uint16, scale x10, sentinel 0xFFFF = invalido)
        pm1  = None if words[0] == 0xFFFF else words[0] / 10.0
        pm25 = None if words[1] == 0xFFFF else words[1] / 10.0
        pm4  = None if words[2] == 0xFFFF else words[2] / 10.0
        pm10 = None if words[3] == 0xFFFF else words[3] / 10.0

        # RH/T/VOC/NOx (int16 con segno, sentinel 0x7FFF)
        def as_signed(w: int) -> int:
            return w - 0x10000 if w & 0x8000 else w

        def conv(w: int, scale: float) -> Optional[float]:
            return None if w == 0x7FFF else as_signed(w) / scale

        rh   = conv(words[4], 100.0)
        temp = conv(words[5], 200.0)
        voc  = conv(words[6], 10.0)
        nox  = conv(words[7], 10.0)

        return Reading(pm1=pm1, pm25=pm25, pm4=pm4, pm10=pm10,
                       rh=rh, temp=temp, voc=voc, nox=nox)


class SimulatedSensor:
    """Simulatore per test senza hardware. Genera valori plausibili."""

    def __init__(self):
        self._t0 = time.time()

    def reset(self) -> None: pass
    def start_measurement(self) -> None: pass
    def stop_measurement(self) -> None: pass
    def close(self) -> None: pass
    def is_data_ready(self) -> bool: return True
    def is_simulated(self) -> bool: return True

    def read_serial_number(self) -> str:
        return "SIMULATED-0000000000000000"

    def read_measured_values(self) -> Reading:
        t = time.time() - self._t0
        # Variazione lenta con rumore
        base = 10 + 5 * math.sin(t / 600)
        noise = lambda: random.uniform(-1, 1)
        return Reading(
            pm1  = max(0, base * 0.5 + noise()),
            pm25 = max(0, base * 0.8 + noise()),
            pm4  = max(0, base * 0.9 + noise()),
            pm10 = max(0, base * 1.1 + noise()),
            rh   = 45 + 10 * math.sin(t / 800) + noise() * 0.3,
            temp = 22 + 3 * math.sin(t / 1200) + noise() * 0.1,
            voc  = max(1, 100 + 50 * math.sin(t / 400) + noise() * 5),
            nox  = max(1, 50 + 30 * math.sin(t / 700) + noise() * 3),
        )
