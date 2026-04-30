"""Flask webapp.

Route principali:
  /                  -> redirect a dashboard (o setup_2fa / login)
  /setup_2fa         -> primo run, mostra QR
  /login             -> form codice TOTP
  /logout            -> distrugge sessione
  /dashboard         -> grafici + dati live
  /export            -> pagina export
  /api/live          -> JSON ultimi valori
  /api/data?...      -> JSON serie storica per grafici
  /api/info          -> JSON stato sistema
  /api/export.xlsx   -> download Excel
  /api/reset_2fa     -> POST resetta provisioning (richiede sessione)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from flask import (Flask, Response, abort, current_app, flash, g, jsonify,
                   redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from .aggregator import Aggregator
from .auth import AuthManager
from .config import Settings
from .exporter import build_xlsx
from .network import NetworkError, disable_ap, enable_ap, get_ap_state
from .storage import METRICS, Storage

log = logging.getLogger(__name__)


# -------------------------------------------------------------------- helpers
def _login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        # check expiry
        exp = session.get("exp", 0)
        if exp and exp < time.time():
            session.clear()
            flash("Sessione scaduta, rifare login.", "warn")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def _provisioning_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth: AuthManager = current_app.config["AUTH"]
        if not auth.is_provisioned():
            return redirect(url_for("setup_2fa"))
        return view(*args, **kwargs)
    return wrapper


# -------------------------------------------------------------------- factory
def create_app(*, settings: Settings, storage: Storage, aggregator: Aggregator) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    app.config["SETTINGS"]   = settings
    app.config["STORAGE"]    = storage
    app.config["AGGREGATOR"] = aggregator
    auth = AuthManager(settings)
    app.config["AUTH"] = auth

    app.config["SECRET_KEY"] = settings.get_or_create_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=settings.session_lifetime_min)

    # ---------- routes ----------

    @app.route("/")
    def root():
        if not auth.is_provisioned():
            return redirect(url_for("setup_2fa"))
        if not session.get("authed"):
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

    # ----- 2FA setup -----
    @app.route("/setup_2fa", methods=["GET", "POST"])
    def setup_2fa():
        if auth.is_provisioned() and session.get("authed"):
            return redirect(url_for("dashboard"))
        if auth.is_provisioned() and not session.get("authed"):
            return redirect(url_for("login"))

        if request.method == "POST":
            code = request.form.get("code", "").strip()
            if auth.verify(code):
                auth.mark_provisioned()
                session["authed"] = True
                session["exp"] = int(time.time()) + settings.session_lifetime_min * 60
                flash("Autenticatore configurato.", "ok")
                return redirect(url_for("dashboard"))
            flash("Codice errato. Riprova con il prossimo.", "err")

        return render_template(
            "setup_2fa.html",
            qr_data_uri=auth.qr_data_uri(),
            secret=auth.get_secret(),
            uri=auth.provisioning_uri(),
            settings=settings,
        )

    # ----- login -----
    @app.route("/login", methods=["GET", "POST"])
    @_provisioning_required
    def login():
        if session.get("authed"):
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            code = request.form.get("code", "").strip()
            if auth.verify(code):
                session["authed"] = True
                session["exp"] = int(time.time()) + settings.session_lifetime_min * 60
                next_url = request.args.get("next") or url_for("dashboard")
                return redirect(next_url)
            flash("Codice errato.", "err")

        return render_template("login.html", settings=settings)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ----- dashboard -----
    @app.route("/dashboard")
    @_provisioning_required
    @_login_required
    def dashboard():
        return render_template("dashboard.html", settings=settings)

    @app.route("/export")
    @_provisioning_required
    @_login_required
    def export():
        counts = storage.counts()
        return render_template("export.html", settings=settings, counts=counts)

    # ----- API -----
    @app.route("/api/live")
    @_provisioning_required
    @_login_required
    def api_live():
        return jsonify(aggregator.get_live())

    @app.route("/api/data")
    @_provisioning_required
    @_login_required
    def api_data():
        level = request.args.get("level", "hour")
        if level not in Storage.LEVELS:
            return jsonify({"error": "invalid_level"}), 400
        ts_from = request.args.get("from", type=int)
        ts_to   = request.args.get("to", type=int)
        rows = storage.fetch(level, ts_from=ts_from, ts_to=ts_to, limit=20000)
        return jsonify({"level": level, "rows": rows})

    @app.route("/api/info")
    @_provisioning_required
    @_login_required
    def api_info():
        return jsonify({
            "device":   settings.device_name,
            "tz":       settings.timezone_name,
            "now_utc":  int(time.time()),
            "counts":   storage.counts(),
            "db_bytes": storage.db_size_bytes(),
            "session_exp": session.get("exp"),
        })

    @app.route("/api/export.xlsx")
    @_provisioning_required
    @_login_required
    def api_export_xlsx():
        ts_from = request.args.get("from", type=int)
        ts_to   = request.args.get("to", type=int)
        data = build_xlsx(storage, settings, ts_from=ts_from, ts_to=ts_to)
        ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"tetralab_export_{ts_label}.xlsx"
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )

    @app.route("/api/reset_2fa", methods=["POST"])
    @_provisioning_required
    @_login_required
    def api_reset_2fa():
        auth.reset_provisioning()
        session.clear()
        return jsonify({"ok": True})

    # ----- AP toggle -----
    @app.route("/api/ap", methods=["GET"])
    @_provisioning_required
    @_login_required
    def api_ap_get():
        try:
            return jsonify({"ok": True, **get_ap_state().to_dict()})
        except NetworkError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/ap", methods=["POST"])
    @_provisioning_required
    @_login_required
    def api_ap_set():
        payload = request.get_json(silent=True) or {}
        enable = bool(payload.get("enable", True))
        try:
            new_state = enable_ap() if enable else disable_ap()
            return jsonify({"ok": True, **new_state.to_dict()})
        except NetworkError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------- error handlers ----------
    @app.errorhandler(404)
    def _404(e):
        return render_template("error.html", code=404, msg="Pagina non trovata"), 404

    @app.errorhandler(500)
    def _500(e):
        log.exception("500: %s", e)
        return render_template("error.html", code=500, msg="Errore interno"), 500

    return app
