"""Gestione Access Point WiFi via NetworkManager (nmcli).

L'utente che gira il service deve essere nel gruppo 'netdev' (default
su Raspberry Pi OS Bookworm) per poter chiamare nmcli senza sudo.

Lo stato 'persistente al reboot' e' garantito da `connection.autoconnect`
del profilo NetworkManager: se yes, NM lo riattiva al boot.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, asdict
from typing import Optional

log = logging.getLogger(__name__)

AP_CONNECTION_NAME = "TetraLab-AP"


class NetworkError(Exception):
    pass


@dataclass
class APState:
    profile_exists: bool
    active: bool
    autoconnect: bool
    ssid: Optional[str] = None
    ip: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _nmcli(*args: str, timeout: float = 10) -> str:
    """Esegue nmcli, ritorna stdout. Solleva NetworkError se fallisce."""
    try:
        r = subprocess.run(
            ["nmcli", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as e:
        raise NetworkError(f"nmcli non installato: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise NetworkError(f"nmcli timeout su {' '.join(args)}: {e}") from e
    if r.returncode != 0:
        raise NetworkError(
            f"nmcli {' '.join(args)} (rc={r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout


def _profile_exists(name: str) -> bool:
    try:
        _nmcli("connection", "show", name)
        return True
    except NetworkError:
        return False


def _get_field(name: str, field: str) -> Optional[str]:
    """Legge un singolo campo dal profilo via -t -f (formato terse)."""
    try:
        out = _nmcli("-t", "-f", field, "connection", "show", name).strip()
    except NetworkError:
        return None
    # output: "field:value"  -> prendi tutto dopo il primo ':'
    if ":" not in out:
        return None
    return out.split(":", 1)[1] or None


def _is_active(name: str) -> bool:
    try:
        out = _nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    except NetworkError:
        return False
    for line in out.splitlines():
        if line.strip() == name:
            return True
    return False


def get_ap_state() -> APState:
    """Stato corrente del profilo AP (esiste? attivo? autoconnect?)."""
    if not _profile_exists(AP_CONNECTION_NAME):
        return APState(profile_exists=False, active=False, autoconnect=False)
    autoconnect_raw = (_get_field(AP_CONNECTION_NAME, "connection.autoconnect") or "").lower()
    ssid             = _get_field(AP_CONNECTION_NAME, "802-11-wireless.ssid")
    ip_cidr          = _get_field(AP_CONNECTION_NAME, "ipv4.addresses") or ""
    return APState(
        profile_exists=True,
        active=_is_active(AP_CONNECTION_NAME),
        autoconnect=(autoconnect_raw == "yes"),
        ssid=ssid,
        ip=ip_cidr.split("/")[0] if ip_cidr else None,
    )


def enable_ap() -> APState:
    """Attiva AP + autoconnect=yes (sopravvive al reboot)."""
    if not _profile_exists(AP_CONNECTION_NAME):
        raise NetworkError(
            f"Profilo '{AP_CONNECTION_NAME}' non esiste. Rilancia setup.sh."
        )
    _nmcli("connection", "modify", AP_CONNECTION_NAME, "connection.autoconnect", "yes")
    try:
        _nmcli("connection", "up", AP_CONNECTION_NAME)
    except NetworkError as e:
        # autoconnect e' settato a yes ma 'up' puo' fallire (es. radio busy).
        # Lo log ma non rilancio: al prossimo boot/cambio rete partira'.
        log.warning("nmcli con up fallito (autoconnect comunque settato): %s", e)
    log.info("AP abilitato (autoconnect=yes)")
    return get_ap_state()


def disable_ap() -> APState:
    """Disattiva AP + autoconnect=no (resta spento ai reboot)."""
    if not _profile_exists(AP_CONNECTION_NAME):
        raise NetworkError(f"Profilo '{AP_CONNECTION_NAME}' non esiste.")
    _nmcli("connection", "modify", AP_CONNECTION_NAME, "connection.autoconnect", "no")
    if _is_active(AP_CONNECTION_NAME):
        try:
            _nmcli("connection", "down", AP_CONNECTION_NAME)
        except NetworkError as e:
            log.warning("nmcli con down fallito: %s", e)
    log.info("AP disabilitato (autoconnect=no)")
    return get_ap_state()
