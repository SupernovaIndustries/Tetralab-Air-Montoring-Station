#!/usr/bin/env bash
# ===========================================================================
# TetraLab Air Quality — setup.sh
# Installa dipendenze, abilita I2C, crea venv, installa requirements,
# registra il servizio systemd e lo avvia.
#
# Uso (dalla cartella del progetto, su Raspberry Pi):
#   chmod +x setup.sh
#   sudo ./setup.sh
# ===========================================================================
set -euo pipefail

# ---- Config ---------------------------------------------------------------
APP_NAME="tetralab"
APP_USER="${SUDO_USER:-pi}"                       # utente che ha invocato sudo
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_NAME="${APP_NAME}.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
PORT="${TETRALAB_PORT:-5000}"
TZ_NAME="${TETRALAB_TZ:-Europe/Rome}"

# ---- Helpers --------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[x]${NC} $*"; }

if [[ $EUID -ne 0 ]]; then
  err "Esegui come root: sudo $0"
  exit 1
fi

# Verifica di essere su Raspberry / Linux ARM
if ! grep -qi "raspberry\|debian\|ubuntu" /etc/os-release; then
  warn "Sistema operativo non riconosciuto come Raspberry/Debian/Ubuntu — proseguo comunque"
fi

info "Utente di esecuzione del servizio: ${APP_USER}"
info "Cartella progetto:                 ${PROJECT_DIR}"
info "Cartella dati persistenti:         ${DATA_DIR}"
info "Porta web:                          ${PORT}"
info "Timezone aggregazioni:              ${TZ_NAME}"

# ---- 1) Pacchetti di sistema ---------------------------------------------
info "Aggiorno apt e installo dipendenze di sistema..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  python3 python3-venv python3-dev python3-pip \
  build-essential libffi-dev libssl-dev \
  i2c-tools sqlite3 git tzdata

# ---- 2) Abilita I2C su Raspberry ------------------------------------------
# Cablaggio SEN65 sul connettore custom:
#   SDA  -> GPIO 8   (i2c4)
#   SCL  -> GPIO 9   (i2c4)
#   SEL  -> GPIO 27  (output LOW al boot, modalita' I2C del SEN65)
#   VDD  -> 3V3, GND -> GND
# Bus risultante: /dev/i2c-4

# Trova config.txt giusto (Bookworm = /boot/firmware/config.txt, Bullseye = /boot/config.txt)
CONFIG_TXT=""
if [[ -f /boot/firmware/config.txt ]]; then
  CONFIG_TXT=/boot/firmware/config.txt
elif [[ -f /boot/config.txt ]]; then
  CONFIG_TXT=/boot/config.txt
fi

REBOOT_NEEDED=0
if [[ -n "${CONFIG_TXT}" ]]; then
  info "Configuro ${CONFIG_TXT}..."
  # I2C4 hardware su GPIO 8/9
  if ! grep -qE "^[[:space:]]*dtoverlay=i2c4,pins_8_9" "${CONFIG_TXT}"; then
    echo 'dtoverlay=i2c4,pins_8_9' >> "${CONFIG_TXT}"
    info "  aggiunto: dtoverlay=i2c4,pins_8_9"
    REBOOT_NEEDED=1
  else
    info "  dtoverlay i2c4 gia' presente"
  fi
  # GPIO 27 = output LOW al boot (SEL del SEN65 -> modalita' I2C)
  if ! grep -qE "^[[:space:]]*gpio=27=op,dl" "${CONFIG_TXT}"; then
    echo 'gpio=27=op,dl' >> "${CONFIG_TXT}"
    info "  aggiunto: gpio=27=op,dl  (SEL=LOW per modalita' I2C del SEN65)"
    REBOOT_NEEDED=1
  else
    info "  gpio=27=op,dl gia' presente"
  fi
else
  warn "config.txt non trovato in /boot o /boot/firmware — applica manualmente:"
  warn "  dtoverlay=i2c4,pins_8_9"
  warn "  gpio=27=op,dl"
fi

# Abilita anche I2C-1 standard (per debug / future espansioni)
if command -v raspi-config >/dev/null 2>&1; then
  info "Abilito interfaccia I2C-1 standard (debug/future use)..."
  raspi-config nonint do_i2c 0 || warn "raspi-config do_i2c non disponibile"
fi

# Aggiungi utente ai gruppi i2c/gpio
if id -nG "${APP_USER}" | grep -qw i2c; then
  info "${APP_USER} è già nel gruppo i2c"
else
  usermod -aG i2c "${APP_USER}" || warn "non riesco ad aggiungere ${APP_USER} al gruppo i2c"
fi
if getent group gpio >/dev/null && ! id -nG "${APP_USER}" | grep -qw gpio; then
  usermod -aG gpio "${APP_USER}" || true
fi

# ---- 3) Cartella dati -----------------------------------------------------
info "Creo cartella dati ${DATA_DIR}..."
mkdir -p "${DATA_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"
chmod 750 "${DATA_DIR}"

# ---- 4) Virtualenv + requirements -----------------------------------------
info "Creo virtualenv in ${VENV_DIR}..."
sudo -u "${APP_USER}" python3 -m venv "${VENV_DIR}"
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip wheel
info "Installo requirements Python..."
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

# ---- 5) Test rapido sensore (non blocca se fallisce) ----------------------
info "Provo a leggere i bus I2C..."
if command -v i2cdetect >/dev/null 2>&1; then
  if [[ -e /dev/i2c-4 ]]; then
    info "scan /dev/i2c-4 (atteso: 0x6b per SEN65):"
    i2cdetect -y 4 || warn "i2cdetect su bus 4 ha riportato un errore"
  else
    warn "/dev/i2c-4 non esiste ancora — sara' disponibile dopo il reboot"
  fi
  if [[ -e /dev/i2c-1 ]]; then
    info "scan /dev/i2c-1:"
    i2cdetect -y 1 || true
  fi
else
  warn "i2cdetect non disponibile — salto"
fi

# ---- 6) systemd unit ------------------------------------------------------
info "Scrivo unit systemd in ${SERVICE_PATH}..."
cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=TetraLab Air Quality datalogger + webapp
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=TETRALAB_DATA_DIR=${DATA_DIR}
Environment=TETRALAB_PORT=${PORT}
Environment=TETRALAB_TZ=${TZ_NAME}
Environment=TETRALAB_I2C_BUS=4
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/run.py
Restart=on-failure
RestartSec=5

# hardening soft
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF

info "Reload systemd + enable + start..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --lines=20 status "${SERVICE_NAME}" || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
info "Setup completato."
echo
echo "  Webapp:    http://${IP:-<ip-raspberry>}:${PORT}/"
echo "  Hostname:  http://$(hostname).local:${PORT}/"
echo "  Logs:      sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Service:   sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
echo "  Data dir:  ${DATA_DIR}"
echo
if [[ "${REBOOT_NEEDED}" == "1" ]]; then
  warn "================================================================"
  warn " REBOOT NECESSARIO: ho modificato config.txt (i2c4 + gpio27)."
  warn " Il SEN65 NON sara' raggiungibile finche' non riavvii."
  warn " Esegui:  sudo reboot"
  warn "================================================================"
fi
warn "Se hai aggiunto l'utente ${APP_USER} al gruppo i2c per la prima volta,"
warn "potrebbe servire logout/login (o reboot) per applicare i permessi."
