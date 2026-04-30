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

# Access Point WiFi (sempre attivo + STA contemporaneo se disponibile)
AP_SSID="${TETRALAB_AP_SSID:-TetraLab-AQ}"
AP_PASS="${TETRALAB_AP_PASS:-tetralab2026}"
AP_IP_CIDR="${TETRALAB_AP_IP:-192.168.50.1/24}"
WIFI_IFACE="${TETRALAB_WIFI_IFACE:-wlan0}"

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
info "Access Point SSID:                  ${AP_SSID}"
info "Access Point IP:                    ${AP_IP_CIDR}"

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

  # Rimuovi versione legacy hardware (se presente) — siamo passati a i2c-gpio
  if grep -qE "^[[:space:]]*dtoverlay=i2c4,pins_8_9" "${CONFIG_TXT}"; then
    sed -i '/^[[:space:]]*dtoverlay=i2c4,pins_8_9/d' "${CONFIG_TXT}"
    info "  rimosso vecchio overlay 'i2c4,pins_8_9'"
    REBOOT_NEEDED=1
  fi

  # I2C software (i2c-gpio) su GPIO 8/9, esposto come /dev/i2c-4
  # Universale (funziona su tutti i Pi), ~100 kHz default — ok per SEN65.
  I2C_LINE='dtoverlay=i2c-gpio,bus=4,i2c_gpio_sda=8,i2c_gpio_scl=9'
  if ! grep -qFx "${I2C_LINE}" "${CONFIG_TXT}"; then
    echo "${I2C_LINE}" >> "${CONFIG_TXT}"
    info "  aggiunto: ${I2C_LINE}"
    REBOOT_NEEDED=1
  else
    info "  dtoverlay i2c-gpio (bus 4 su GPIO 8/9) gia' presente"
  fi

  # GPIO 27 = output LOW al boot (SEL del SEN65 -> modalita' I2C)
  if ! grep -qE "^[[:space:]]*gpio=27=op,dl" "${CONFIG_TXT}"; then
    echo 'gpio=27=op,dl' >> "${CONFIG_TXT}"
    info "  aggiunto: gpio=27=op,dl  (SEL=LOW per modalita' I2C del SEN65)"
    REBOOT_NEEDED=1
  else
    info "  gpio=27=op,dl gia' presente"
  fi

  # Pull-up interni su GPIO 8/9 (~50kOhm). Il default su Pi e':
  #   GPIO 8 = pull-up debole (ok)
  #   GPIO 9 = pull-DOWN (opposto di quello che serve a I2C!)
  # Lo forziamo esplicitamente. NB: idealmente metti anche 10k esterni a 3V3
  # come raccomanda il datasheet SEN65.
  if ! grep -qE "^[[:space:]]*gpio=8,9=ip,pu" "${CONFIG_TXT}"; then
    echo 'gpio=8,9=ip,pu' >> "${CONFIG_TXT}"
    info "  aggiunto: gpio=8,9=ip,pu  (pull-up interni I2C, ~50kOhm)"
    REBOOT_NEEDED=1
  else
    info "  gpio=8,9=ip,pu gia' presente"
  fi
else
  warn "config.txt non trovato in /boot o /boot/firmware — applica manualmente:"
  warn "  dtoverlay=i2c-gpio,bus=4,i2c_gpio_sda=8,i2c_gpio_scl=9"
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

# ---- 6) Access Point WiFi (creato ma SPENTO di default) -------------------
# Il profilo NM viene creato ma con autoconnect=no e NON viene avviato.
# L'utente lo attiva/disattiva dal pulsante in dashboard (o via nmcli).
# Lo stato (autoconnect yes/no) persiste al reboot grazie a NetworkManager.
#
# Nota hardware: su Pi onboard CYW43xxx, AP+STA simultaneo va sullo stesso
# canale -> attivare AP mentre la STA e' connessa puo' interrompere la STA.
if command -v nmcli >/dev/null 2>&1; then
  info "Configuro profilo Access Point WiFi (creato spento)..."
  if nmcli -t -f NAME connection show | grep -qx "TetraLab-AP"; then
    info "  profilo 'TetraLab-AP' gia' presente, aggiorno parametri"
  else
    nmcli con add type wifi ifname "${WIFI_IFACE}" mode ap \
      con-name TetraLab-AP ssid "${AP_SSID}" \
      || warn "creazione profilo AP fallita"
  fi
  # autoconnect=no di default -> AP NON parte al boot finche' l'utente non
  # lo abilita dal pulsante in dashboard (che setta autoconnect=yes).
  nmcli con modify TetraLab-AP \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "${AP_PASS}" \
    ipv4.method shared \
    ipv4.addresses "${AP_IP_CIDR}" \
    connection.autoconnect no \
    connection.autoconnect-priority 1 \
    || warn "modifica profilo AP fallita"

  AP_IP_BARE="${AP_IP_CIDR%/*}"
  info "  AP profilo creato: SSID='${AP_SSID}'  pass='${AP_PASS}'  IP=${AP_IP_BARE}"
  info "  AP NON attivo. Attivalo dal pulsante in dashboard, oppure:"
  info "    sudo nmcli con up TetraLab-AP"
else
  warn "nmcli non disponibile — Access Point NON configurato."
  warn "Installa NetworkManager o configura hostapd manualmente."
fi

# Aggiungi utente al gruppo netdev per controllare nmcli senza sudo
# (necessario perche' la webapp deve poter chiamare 'nmcli con up/down/modify')
if getent group netdev >/dev/null && ! id -nG "${APP_USER}" | grep -qw netdev; then
  info "Aggiungo ${APP_USER} al gruppo netdev (per nmcli senza sudo)..."
  usermod -aG netdev "${APP_USER}" || warn "aggiunta a netdev fallita"
fi

# ---- 7) systemd unit ------------------------------------------------------
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

# hardening soft (ProtectHome=read-only perche' il progetto sta in /home/<user>)
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
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

IP_LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
AP_IP_BARE="${AP_IP_CIDR%/*}"
echo
info "Setup completato."
echo
echo "  ── Connessione via rete WiFi locale (STA): ──────────────────────"
echo "  Webapp:    http://${IP_LAN:-<ip-raspberry>}:${PORT}/"
echo "  Hostname:  http://$(hostname).local:${PORT}/"
echo
echo "  ── Access Point (creato spento, attivalo da dashboard): ────────"
echo "  WiFi SSID: ${AP_SSID}"
echo "  Password:  ${AP_PASS}"
echo "  IP:        ${AP_IP_BARE}:${PORT}  (quando attivo)"
echo "  On/Off:    pulsante in dashboard, oppure:"
echo "             sudo nmcli con up TetraLab-AP   |   sudo nmcli con down TetraLab-AP"
echo
echo "  ── Manutenzione: ────────────────────────────────────────────────"
echo "  Logs:      sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Service:   sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
echo "  AP:        sudo nmcli con {up|down} TetraLab-AP"
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
