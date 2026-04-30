# TetraLab Air Quality — Raspberry Pi

Datalogger qualità aria con **Sensirion SEN65** su **Raspberry Pi**, webapp Flask con grafici real-time, autenticazione **2FA TOTP**, export Excel multi-sheet con grafici embedded. Aggregazioni allineate alla mezzanotte locale (Europe/Rome): minuto / oraria / 12h / 24h.

## Hardware previsto
- Raspberry Pi 4/5 (o CM4) con scheda custom
- Sensirion **SEN65** via connettore custom su:
  - SDA → **GPIO 8**  (bus I2C-4 hardware via `dtoverlay=i2c4,pins_8_9`)
  - SCL → **GPIO 9**
  - SEL → **GPIO 27** (output LOW al boot, modalità I2C del SEN65)
  - VDD → 3V3, GND → GND
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
2. Configura `config.txt` con `dtoverlay=i2c4,pins_8_9` + `gpio=27=op,dl` (SEL→LOW)
3. Abilita anche I2C-1 standard (per debug/futuro)
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
| `TETRALAB_I2C_BUS`  | `4` (impostato dal service) | bus I2C: `4` con i2c4 hardware su GPIO 8/9 |
| `TETRALAB_LOG_LEVEL`| `INFO` | DEBUG/INFO/WARNING |
| `TETRALAB_ALLOW_SIMULATOR` | `1` | se 1, usa fake sensor se SEN65 manca |

Per modificarle: `sudo systemctl edit tetralab.service` e aggiungi un drop-in `[Service]` con `Environment=...`, poi `sudo systemctl restart tetralab`.

## Primo accesso

1. Da un PC sulla stessa rete WiFi della Raspberry, apri il browser su `http://<ip-raspberry>:5000/`
2. Vieni reindirizzato a **/setup_2fa**: scansiona il QR con **Google/Microsoft Authenticator**
3. **📸 Fai screenshot del QR** se vuoi importarlo per altri dipendenti
4. Inserisci il codice a 6 cifre per confermare → entri in dashboard
5. Da qui vedi:
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
i2cdetect -y 4            # atteso: 0x6b (bus 4 = GPIO 8/9)

# stop / disable se serve
sudo systemctl stop tetralab
sudo systemctl disable tetralab

# wipe dati (DISTRUTTIVO)
sudo systemctl stop tetralab
sudo rm -rf /var/lib/tetralab
sudo systemctl start tetralab     # rigenera tutto al primo avvio
```

## Troubleshooting

| Problema | Soluzione |
|---|---|
| `i2cdetect -y 4` non vede 0x6B | verifica `/boot/firmware/config.txt` contiene `dtoverlay=i2c4,pins_8_9` e `gpio=27=op,dl`; reboot; misura GPIO 27 con multimetro (deve essere 0V); cablaggio VDD/GND/SDA/SCL |
| `/dev/i2c-4` non esiste | dtoverlay non applicato → reboot dopo aver controllato config.txt |
| `Permission denied: /dev/i2c-4` | utente non in gruppo `i2c` → `sudo usermod -aG i2c $USER` + reboot |
| Webapp non raggiungibile | `sudo systemctl status tetralab`, `sudo ss -ltn` per vedere se 5000 è in ascolto |
| Codice TOTP sbagliato | controlla orario Raspberry: `timedatectl`, dovrebbe essere sincronizzato via NTP |
| Errore TOTP dopo molti gg | drift orologio: `sudo systemctl restart systemd-timesyncd` |
| Excel export vuoto | servono almeno 1 minuto di acquisizione (~60 letture) per popolare il primo bucket |

## Sviluppo locale (Mac/Linux senza hardware)

Il progetto può girare anche su Mac per sviluppare la UI: il driver `sensor.py` ha un fallback automatico a `SimulatedSensor` se `smbus2` non è disponibile o l'I2C fallisce.

```bash
cd tetralab_air_quality_pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
TETRALAB_DATA_DIR=./data TETRALAB_TZ=Europe/Rome python run.py
# poi apri http://localhost:5000
```

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
