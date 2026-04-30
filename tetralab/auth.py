"""Autenticazione 2FA TOTP (compatibile Google/Microsoft Authenticator).

Flow:
  - primo run: nessun secret -> /setup_2fa mostra QR + form per confermare
  - secret confermato: /login chiede codice -> imposta sessione
  - tutte le altre route richiedono sessione attiva
"""
from __future__ import annotations

import io
import logging
import secrets
from base64 import b64encode
from typing import Optional

import pyotp
import qrcode

from .config import Settings

log = logging.getLogger(__name__)


class AuthManager:
    """Gestisce secret TOTP, provisioning, validazione codici."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._secret: Optional[str] = None
        self._load_or_create_secret()

    # ---------- secret ----------
    def _load_or_create_secret(self) -> None:
        p = self.settings.totp_secret_path
        if p.exists():
            self._secret = p.read_text().strip()
            log.info("TOTP secret caricato da %s", p)
        else:
            # 20 byte base32 (= secret a 160 bit, standard)
            self._secret = pyotp.random_base32()
            p.write_text(self._secret)
            try:
                p.chmod(0o600)
            except OSError:
                pass
            log.info("Nuovo TOTP secret generato e salvato in %s", p)

    def get_secret(self) -> str:
        assert self._secret is not None
        return self._secret

    # ---------- provisioning ----------
    def is_provisioned(self) -> bool:
        return self.settings.totp_provisioned_path.exists()

    def mark_provisioned(self) -> None:
        self.settings.totp_provisioned_path.write_text("ok")
        log.info("Provisioning TOTP confermato")

    def reset_provisioning(self) -> None:
        # Genera un NUOVO secret e azzera flag (l'utente dovra' riscansionare)
        try:
            self.settings.totp_secret_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.settings.totp_provisioned_path.unlink()
        except FileNotFoundError:
            pass
        self._secret = None
        self._load_or_create_secret()
        log.warning("Provisioning TOTP resettato (nuovo secret generato)")

    # ---------- URI + QR ----------
    def provisioning_uri(self) -> str:
        totp = pyotp.TOTP(self.get_secret())
        return totp.provisioning_uri(
            name=self.settings.account, issuer_name=self.settings.issuer
        )

    def qr_data_uri(self) -> str:
        """Restituisce il QR del provisioning come data:image/png;base64,..."""
        img = qrcode.make(self.provisioning_uri(), box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + b64encode(buf.getvalue()).decode()

    # ---------- verifica ----------
    def verify(self, code: str) -> bool:
        if not code or not code.strip().isdigit():
            return False
        totp = pyotp.TOTP(self.get_secret())
        # valid_window=1 -> tolleranza +/- 30s sul drift dell'orologio client
        return totp.verify(code.strip(), valid_window=1)

    # ---------- session token (CSRF helper) ----------
    @staticmethod
    def new_csrf_token() -> str:
        return secrets.token_urlsafe(24)
