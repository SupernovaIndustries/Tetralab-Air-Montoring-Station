# TetraLab Air Quality — Raspberry Pi

Datalogger qualità aria con **Sensirion SEN65** su **Raspberry Pi**, webapp Flask con grafici real-time, autenticazione **2FA TOTP**, export Excel multi-sheet con grafici embedded. Aggregazioni allineate alla mezzanotte locale (Europe/Rome): minuto / oraria / 12h / 24h.

## Hardware previsto
- Raspberry Pi 4/5 (o CM4) con scheda custom
- Sensirion **SEN65** sui pin **I2C-1 standard** della Pi (hanno **pull-up hardware 1.8kΩ saldati sul PCB della Pi**, niente componenti esterni):
  - VDD → **Pin 1** (3V3)
  - GND → **Pin 6** (GND)
  - SDA → **Pin 3** (GPIO 2 / SDA1)
  - SCL → **Pin 5** (GPIO 3 / SCL1)
  - SEL → **Pin 9** (GND, qualsiasi GND va bene — modalità I2C del SEN65)
- Storage: **128 GB onboard** della scheda custom
- Cellular SIMCOM 7600G — *integrazione prevista in seconda fase (UART su PCIe)*

## Installazione (sulla Raspberry)

```bash
git clone <tuo-repo>.git tetralab_air_quality_pi
cd tetralab_air_quality_pi
sudo ./setup.sh
```

Lo script:
1. Aggiorna apt + installa python3, venv, build tools, `i2c-tools`, `sqlite3`
2. Pulisce eventuali overlay legacy da config.txt (i2c-gpio, gpio27, ecc) di iterazioni precedenti
3. Abilita I2C-1 standard via `raspi-config`
4. Aggiunge l'utente al gruppo `i2c`
5. Crea `/var/lib/tetralab` per i dati persistenti
6. Crea venv in `.venv/` e installa `requirements.txt`
7. Esegue `i2cdetect -y 4` (atteso: 0x6B per il SEN65)
8. Registra `tetralab.service` con `TETRALAB_I2C_BUS=4` e lo avvia al boot

> ⚠️ Al primo run **serve un reboot** per applicare le modifiche a `config.txt` (i2c4 e gpio27). Il setup.sh te lo segnala alla fine.

Output finale: URL della webapp (es. `http://192.168.1.42:5000/`).

> Dopo il primo setup, se l'utente è stato aggiunto al gruppo `i2c` per la prima volta, potrebbe servire `logout/login` o `sudo reboot` per applicare i permessi.

## Configurazione

Tutti i parametri principali via **variabili d'ambiente** (settate in `tetralab.service`):

| Variabile | Default | Descrizione |
|---|---|---|
| `TETRALAB_DATA_DIR` | `/var/lib/tetralab` | dove finiscono DB + secrets |
| `TETRALAB_PORT`     | `5000` | porta webapp |
| `TETRALAB_TZ`       | `Europe/Rome` | timezone per allineare aggregazioni |
| `TETRALAB_I2C_BUS`  | `1` (impostato dal service) | bus I2C: `/dev/i2c-1` (pin GPIO 2/3 standard) |
| `TETRALAB_LOG_LEVEL`| `INFO` | DEBUG/INFO/WARNING |
| `TETRALAB_ALLOW_SIMULATOR` | `0` (off) | se `1`, usa fake sensor se SEN65 manca (utile solo per dev su Mac) |
| `TETRALAB_AP_SSID`  | `TetraLab-AQ` | SSID dell'access point integrato |
| `TETRALAB_AP_PASS`  | `tetralab2026` | password AP (min 8 char, **da cambiare!**) |
| `TETRALAB_AP_IP`    | `192.168.50.1/24` | IP/subnet dell'AP |
| `TETRALAB_WIFI_IFACE` | `wlan0` | interfaccia WiFi |

Per modificarle: `sudo systemctl edit tetralab.service` e aggiungi un drop-in `[Service]` con `Environment=...`, poi `sudo systemctl restart tetralab`.

## ✅ Checklist verifica post-setup (da fare in quest'ordine)

Se hai appena lanciato `sudo ./setup.sh` (o vuoi capire perché qualcosa non va), esegui in sequenza:

### 1. Il servizio è attivo?
```bash
sudo systemctl status tetralab
```
Cerca **`Active: active (running)`** verde. Se è `failed` → guarda subito i log:
```bash
sudo journalctl -u tetralab -n 100 --no-pager
```
Errori tipici:
- `ModuleNotFoundError: No module named 'flask'` → venv non installato, rilancia `sudo ./setup.sh`
- `PermissionError: /var/lib/tetralab/...` → owner sbagliato: `sudo chown -R $USER:$USER /var/lib/tetralab`
- `OSError: [Errno 19] No such device` (su I2C) → `/dev/i2c-4` non esiste, manca dtoverlay → reboot dopo aver verificato config.txt

### 2. La webapp ascolta sulla porta 5000?
```bash
sudo ss -ltnp | grep :5000
```
Devi vedere qualcosa tipo `LISTEN 0 ... *:5000 ... users:(("python",pid=...))`.
Se non c'è nulla → servizio non davvero up, torna al punto 1.

### 3. Curl in locale dalla Pi
```bash
curl -v http://127.0.0.1:5000/
```
Risposta attesa: HTTP 302 redirect a `/setup_2fa` o `/login`. Se anche da localhost non risponde, il problema è 100% sul servizio (non rete/firewall).

### 4. IP della Pi
```bash
hostname -I
ip -br addr
```
Il primo IP è quello LAN. Verifica che sia raggiungibile dal tuo PC:
```bash
# dal PC/Mac:
ping <ip-pi>
```

### 5. Firewall
Su Raspberry OS di default **non c'è firewall attivo**. Ma se hai installato `ufw`:
```bash
sudo ufw status
sudo ufw allow 5000/tcp     # se serve
```

### 6. Bus I2C visto?
```bash
ls -l /dev/i2c-*            # deve esistere /dev/i2c-4
i2cdetect -y 4              # deve apparire 0x6b (= SEN65)
```
Se `/dev/i2c-4` manca:
```bash
# verifica che config.txt abbia gli overlay
grep -E "i2c4|gpio=27" /boot/firmware/config.txt /boot/config.txt 2>/dev/null
sudo reboot                  # se hai appena modificato config.txt
```

### 7. AP visibile dal telefono?
```bash
nmcli con show --active     # deve esserci 'TetraLab-AP' in lista
iw dev wlan0 info           # type AP confermato
```
Se manca, **rilancia `sudo ./setup.sh`** (è idempotente — non rovina nulla, riapplica solo le parti mancanti, inclusa la configurazione AP).

## ⚠️ Problemi tipici "non riesco ad aprire http://&lt;ip&gt;:5000"

| Sintomo | Causa probabile | Fix |
|---|---|---|
| Browser dice "Connessione rifiutata" | servizio non in ascolto | `sudo systemctl restart tetralab`, poi punto 2 e 3 sopra |
| Browser dice "Timeout" | IP sbagliato o non raggiungibile | `ping <ip>` dal PC; rifai `hostname -I` sulla Pi |
| Curl da Pi `127.0.0.1:5000` ok ma da PC no | problema rete o firewall | sei sulla stessa rete? Controlla `ufw`, controlla che il router non isoli i client |
| Pagina si apre poi 500 | crash a runtime, vedi log | `sudo journalctl -u tetralab -n 200` |
| `/dev/i2c-4` non esiste | dtoverlay non applicato | controlla config.txt, **reboot** |
| **`Unable to locate executable .venv/bin/python`** | venv non creato o corrotto | vedi sezione "Fix venv mancante" sotto |
| **AP attivo → STA si disconnette** | limite hardware Pi onboard | vedi sezione "AP+STA: limiti e workaround" sotto |

### Fix venv mancante / corrotto

Se il service va in restart loop con errore `.venv/bin/python: No such file or directory`:

```bash
cd ~/<cartella-progetto>
sudo apt install -y python3-venv python3-pip python3-dev build-essential libffi-dev libssl-dev
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt
sudo systemctl restart tetralab
sudo systemctl status tetralab
```

### AP+STA: limiti e workaround

Il chip WiFi onboard della Pi 4/5 (Cypress CYW43xxx) supporta AP+STA **simultaneo ma sullo stesso canale**. Se la STA è connessa a un router su canale 11 e l'AP è su canale 6 (o viceversa), una delle due cade.

**Comportamento attuale del setup**:
- AP **non forza canale** (delega al firmware)
- AP ha `autoconnect-priority=1` (basso) → STA preferita quando entrambe disponibili
- In casa/ufficio: STA prevale, AP può scendere
- In Bologna (no WiFi locale): AP unico modo → sempre attivo, sempre raggiungibile

**Per testare a casa con AP attivo** (sacrifichi internet sulla Pi durante il test):
```bash
sudo nmcli con down "<nome-rete-wifi-casa>"
sudo nmcli con up TetraLab-AP
# poi connettiti col telefono a 'TetraLab-AQ' / pwd 'tetralab2026'
# vai su http://192.168.50.1:5000
```

**Per riportare la Pi su WiFi di casa**:
```bash
sudo nmcli con down TetraLab-AP
sudo nmcli con up "<nome-rete-wifi-casa>"
```

**Soluzione hardware definitiva** (futura): aggiungere un dongle WiFi USB → onboard fa STA, dongle fa AP, niente conflitto canale.

## Accesso alla webapp

La centralina ha **due modi di accesso simultanei**:

### 🌐 Via WiFi locale (STA)
Se la Raspberry è connessa al WiFi della tua rete:
```
http://<ip-raspberry>:5000/
http://raspberrypi.local:5000/   (se mDNS funziona)
```

### 📡 Via Access Point integrato
La centralina può emettere una propria rete WiFi — utile in postazioni remote senza WiFi (es. Bologna).

**Il setup crea il profilo AP ma lo lascia SPENTO**, così durante i test la STA WiFi non viene disturbata. Lo accendi quando ti serve dal **pulsante in Dashboard → Azioni → Access Point**, oppure via `sudo nmcli con up TetraLab-AP`. Lo stato (acceso/spento + autoconnect al boot) **persiste tra i reboot**: se l'ultima volta era acceso, all'avvio della Pi torna acceso da solo.

| Parametro | Default | Override |
|---|---|---|
| SSID | `TetraLab-AQ` | `TETRALAB_AP_SSID` env nel setup.sh |
| Password | `tetralab2026` | `TETRALAB_AP_PASS` env nel setup.sh |
| IP webapp | `192.168.50.1:5000` | `TETRALAB_AP_IP` env (CIDR, es. `10.0.0.1/24`) |

Il telefono/PC connesso all'AP riceve un IP automatico via DHCP (192.168.50.0/24) e vede la webapp su `http://192.168.50.1:5000/`.

> **Cambia la password di default** prima di mettere in produzione! Ricrea il profilo con:
> ```bash
> sudo nmcli con modify TetraLab-AP wifi-sec.psk "nuova-password-min-8-char"
> sudo nmcli con down TetraLab-AP && sudo nmcli con up TetraLab-AP
> ```

> AP+STA simultaneo sulla Pi 4/5 onboard (CYW43xxx) funziona, ma **vincolato allo stesso canale** della STA. Se hai problemi di range/stabilità, puoi disconnettere la STA e usare solo l'AP.

## Primo accesso (provisioning 2FA)

1. Connettiti via uno dei due modi sopra (rete locale o AP)
2. Apri il browser sull'IP della webapp
3. Vieni reindirizzato a **/setup_2fa**: scansiona il QR con **Google/Microsoft Authenticator**
4. **📸 Fai screenshot del QR** se vuoi importarlo per altri dipendenti
5. Inserisci il codice a 6 cifre per confermare → entri in dashboard
6. Da qui vedi:
   - **Live**: dati in tempo reale (aggiornamento 2 s)
   - **Grafici**: PM, T/RH, VOC, NOx con livelli minuto/ora/12h/giorno e range selezionabile
   - **Export**: scarica `.xlsx` con tutte le tabelle + grafici Chart.js orari embedded

## Layout dati

`/var/lib/tetralab/`:
```
tetralab.db                # SQLite con 4 tabelle: minute, hour, half, day
secret_key.bin             # chiave Flask sessions (auto-generata)
totp_secret.bin            # base32 secret TOTP (auto-generato)
totp_provisioned           # marker post-conferma autenticatore
```

Schema SQL (uguale per tutte le 4 tabelle):
```sql
CREATE TABLE readings_<level> (
  ts INTEGER PRIMARY KEY,            -- unix UTC
  pm1 REAL, pm25 REAL, pm4 REAL, pm10 REAL,
  rh REAL, temp REAL, voc REAL, nox REAL,
  n_samples INTEGER NOT NULL DEFAULT 0
);
```

## Aggregazioni

- **minute**: media di ~60 letture (sample_period = 1 s, configurabile)
- **hour**: allineata all'ora intera locale (es. 14:00:00, 15:00:00...)
- **half**: allineata a 00:00 / 12:00 locali
- **day**: allineata a 00:00 locale

Gli aggregati superiori vengono **upsertati ad ogni minuto**, quindi il valore "in corso" è sempre visibile aggiornato (utile per la dashboard).

## Autenticazione

- TOTP standard (SHA1, 30s, 6 cifre, finestra ±30s)
- Compatibile con qualsiasi authenticator app
- Sessione Flask con cookie firmato, durata 60 min (configurabile)
- Reset autenticatore dalla dashboard (pulsante rosso "Reset autenticatore")

## Comandi utili

```bash
# stato del servizio
sudo systemctl status tetralab

# logs in tempo reale
sudo journalctl -u tetralab -f

# riavvio (es. dopo modifica codice)
sudo systemctl restart tetralab

# controlla che il SEN65 risponda sul bus
i2cdetect -y 1            # atteso: 0x6b (bus 1 = GPIO 2/3)

# stop / disable se serve
sudo systemctl stop tetralab
sudo systemctl disable tetralab

# wipe dati (DISTRUTTIVO)
sudo systemctl stop tetralab
sudo rm -rf /var/lib/tetralab
sudo systemctl start tetralab     # rigenera tutto al primo avvio

# --- Access Point ---
nmcli con show TetraLab-AP                # parametri profilo
sudo nmcli con up TetraLab-AP             # avvia AP
sudo nmcli con down TetraLab-AP           # spegne AP
sudo nmcli con modify TetraLab-AP wifi-sec.psk "nuova-pwd"  # cambia pwd
sudo nmcli con delete TetraLab-AP         # rimuovi del tutto

# --- Connessione a una rete WiFi (STA) ---
sudo nmcli device wifi list
sudo nmcli device wifi connect "<SSID>" password "<password>"
```

## Troubleshooting

| Problema | Soluzione |
|---|---|
| `i2cdetect -y 1` non vede 0x6B | verifica cablaggio VDD/GND/SDA/SCL, SEL→GND, pin 3 e 5 sul header Pi; reboot |
| `/dev/i2c-1` non esiste | I2C non abilitato → `sudo raspi-config nonint do_i2c 0` + reboot |
| `Permission denied: /dev/i2c-1` | utente non in gruppo `i2c` → `sudo usermod -aG i2c $USER` + reboot |
| Webapp non raggiungibile | `sudo systemctl status tetralab`, `sudo ss -ltn` per vedere se 5000 è in ascolto |
| Codice TOTP sbagliato | controlla orario Raspberry: `timedatectl`, dovrebbe essere sincronizzato via NTP |
| Errore TOTP dopo molti gg | drift orologio: `sudo systemctl restart systemd-timesyncd` |
| Excel export vuoto | servono almeno 1 minuto di acquisizione (~60 letture) per popolare il primo bucket |
| AP non visibile | `nmcli con show --active` per vedere se TetraLab-AP è up; se no `sudo nmcli con up TetraLab-AP`; verifica `iw dev wlan0 info` |
| AP+STA non funzionano insieme | il chip onboard forza stesso canale: la STA prevale, l'AP si sposta. Su WiFi USB esterno → niente vincolo |
| Niente DHCP sull'AP | NetworkManager con `ipv4.method shared` richiede `nftables`/`iptables` ok; controlla `sudo nft list ruleset` |
| Toggle AP dashboard: `Insufficient privileges` | manca regola polkit, rilancia `sudo ./setup.sh` (la installa) o crea manualmente `/etc/polkit-1/rules.d/50-tetralab-nmcli.rules` |

## Sviluppo locale (Mac/Linux senza hardware)

Il progetto può girare anche su Mac per sviluppare la UI: il driver `sensor.py` ha un fallback automatico a `SimulatedSensor` se `smbus2` non è disponibile o l'I2C fallisce.

```bash
cd tetralab_air_quality_pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
TETRALAB_DATA_DIR=./data \
TETRALAB_TZ=Europe/Rome \
TETRALAB_ALLOW_SIMULATOR=1 \
python run.py
# poi apri http://localhost:5000
```

> Senza `TETRALAB_ALLOW_SIMULATOR=1` e senza SEN65 collegato, il servizio entra in retry loop infinito sul sensore (atteso). Imposta a 1 solo per dev su Mac.

## Roadmap

- [ ] Integrazione SIMCOM 7600G (cellular fallback / push dati su server)
- [ ] Webhook / MQTT push opzionale
- [ ] Soglie configurabili + alert email/SMS

## Struttura del progetto

```
tetralab_air_quality_pi/
├── run.py                       # entry point
├── setup.sh                     # installer end-to-end
├── requirements.txt
├── README.md
├── .gitignore
└── tetralab/
    ├── __init__.py
    ├── config.py                # Settings + paths/env
    ├── sensor.py                # SEN65 driver + simulator
    ├── storage.py               # SQLite wrapper thread-safe
    ├── aggregator.py            # thread acquisizione + bucketing
    ├── auth.py                  # TOTP + provisioning + verify
    ├── exporter.py              # Excel multi-sheet con grafici
    ├── webapp.py                # Flask app factory + routes
    ├── templates/
    │   ├── base.html            # layout Tailwind dark
    │   ├── setup_2fa.html       # QR + form conferma
    │   ├── login.html
    │   ├── dashboard.html       # cards live + 4 grafici Chart.js
    │   ├── export.html
    │   └── error.html
    └── static/
        └── logo.svg             # PLACEHOLDER — sostituisci col tuo logo
```
