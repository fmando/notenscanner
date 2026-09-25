# Sheet Music Web

Webanwendung zur automatischen Notenerkennung aus Scans und PDFs.
Upload → Audiveris OMR → MuseScore → SVG-Anzeige im Browser + MIDI-Wiedergabe + Export.

---

## Features

- Upload von Notenscans als PNG, JPG, TIFF oder PDF
- Automatische Notenerkennung (OMR) via Audiveris 5.10
- Mehrseitige Anzeige als MuseScore-gerendertes SVG (pixelgleich mit PDF)
- MIDI-Wiedergabe einzelner Stimmen oder des Gesamtsatzes
- Download: PDF, MIDI (gesamt), MIDI pro Stimme, MusicXML, LilyPond, MP3
- Optionale OCR-Texterkennung fuer Liedtexte (Tesseract, per Upload-Toggle)
- Grossdatei-Unterstuetzung: mehrseitige PDFs werden seitenweise verarbeitet

---

## Stack

| Schicht | Technologie |
|---------|-------------|
| OMR | Audiveris 5.10.2 |
| Notensatz + Export | MuseScore 3 + Xvfb |
| OCR | Tesseract 5 (via Audiveris) |
| Backend | Python 3.12 + FastAPI + SQLModel + SQLite |
| Frontend | React 18 + TypeScript + Vite |
| Notenansicht | MuseScore SVG (serverseitig gerendert) |
| MIDI-Wiedergabe | html-midi-player (Tone.js) |
| Reverse Proxy | Caddy (separater Container) — nginx im App-Container nur als Alternative, siehe unten |

---

## Installation (Ubuntu 24.04 LTS)

### Voraussetzungen

- Frischer Ubuntu 24.04 Server
- Root-Zugang
- Mindestens 4 GB RAM empfohlen (2 GB + Swap funktioniert, ist aber knapp)
- Domain mit DNS-Eintrag auf den Server (fuer HTTPS, optional)

### Schnellstart

```bash
git clone https://github.com/DEIN_USER/sheet-music-web.git /opt/sheet-music-web
cd /opt/sheet-music-web
bash install.sh meine-domain.de
```

Das Script installiert automatisch:
- OpenJDK 17, MuseScore 3, Xvfb, ghostscript, nginx, certbot
- Audiveris 5.10.2 (Download von GitHub Releases)
- Tesseract-Sprachdaten Englisch + Deutsch (Legacy-Format fuer Audiveris)
- Python 3.12 Virtualenv mit allen Abhaengigkeiten
- Frontend-Build (Node.js + Vite)
- systemd-Services (`sheet-music-xvfb`, `sheet-music-backend`)
- nginx-Konfiguration + optionales Let's Encrypt SSL

---

## Manuelle Installation (Schritt fuer Schritt)

### 1. Systempakete

```bash
apt-get update
apt-get install -y \
    openjdk-17-jdk musescore3 xvfb ghostscript \
    nginx certbot python3-certbot-nginx \
    python3.12 python3.12-venv \
    nodejs npm curl unzip \
    tesseract-ocr-eng tesseract-ocr-deu
```

### 2. Audiveris 5.10.2

```bash
curl -L -o /tmp/audiveris.deb \
  https://github.com/Audiveris/audiveris/releases/download/5.10.2/Audiveris-5.10.2-ubuntu24.04-x86_64.deb
dpkg -i /tmp/audiveris.deb
# installiert nach /opt/audiveris/bin/Audiveris
```

### 3. Tesseract-Sprachdaten (Legacy-Format)

Die apt-Pakete liefern nur LSTM-Daten; Audiveris benoetigt das Legacy-Format:

```bash
TESSDATA=/root/.config/AudiverisLtd/audiveris/tessdata
mkdir -p "$TESSDATA"
curl -L -o "$TESSDATA/eng.traineddata" \
  https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata
curl -L -o "$TESSDATA/deu.traineddata" \
  https://github.com/tesseract-ocr/tessdata/raw/main/deu.traineddata
cp /usr/share/tesseract-ocr/5/tessdata/osd.traineddata "$TESSDATA/"
```

### 4. Repository und Python-Umgebung

```bash
git clone https://github.com/DEIN_USER/sheet-music-web.git /opt/sheet-music-web
cd /opt/sheet-music-web
python3.12 -m venv venv
venv/bin/pip install -r backend/requirements.txt

cp deploy/backend.env.example backend/.env
# ggf. DATA_DIR in backend/.env anpassen
```

### 5. Frontend bauen

```bash
cd /opt/sheet-music-web/frontend
npm ci
npm run build
mkdir -p /var/www/sheet-music-web
rsync -a --delete dist/ /var/www/sheet-music-web/
chown -R www-data:www-data /var/www/sheet-music-web
```

### 6. systemd-Services

```bash
cp /opt/sheet-music-web/deploy/sheet-music-xvfb.service    /etc/systemd/system/
cp /opt/sheet-music-web/deploy/sheet-music-backend.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sheet-music-xvfb sheet-music-backend
```

### 7. Reverse Proxy

Das Frontend wird von FastAPI selbst ausgeliefert (`FRONTEND_DIST` in `backend/.env`) — ein lokaler
Webserver im App-Container ist dafuer nicht mehr noetig. Zwei Optionen:

**a) Caddy in einem separaten Container/Host** (so laeuft die Produktivinstanz):

```
# deploy/Caddyfile, im Reverse-Proxy-Container:
meine-domain.de {
    reverse_proxy APP_CONTAINER_IP:8000 {
        flush_interval -1        # SSE-Stream (Status-Updates) funktioniert nur so
        transport http {
            read_timeout  310s   # Grosse Uploads / lange OMR-Verarbeitung
            write_timeout 310s
        }
    }
}
```

Caddy holt das TLS-Zertifikat automatisch via Let's Encrypt, kein `certbot` noetig. `CORS_ORIGINS`
in `backend/.env` muss auf dieselbe Domain zeigen (sonst blockt der Browser die Frontend-Requests).

**b) nginx direkt im App-Container** (Alternative, z. B. wenn kein separater Proxy-Host existiert):

```bash
cp /opt/sheet-music-web/deploy/nginx.conf /etc/nginx/sites-available/meine-domain.de
sed -i 's/DEINE_DOMAIN/meine-domain.de/' /etc/nginx/sites-available/meine-domain.de
ln -s /etc/nginx/sites-available/meine-domain.de /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
certbot --nginx -d meine-domain.de
```

`install.sh <domain>` richtet automatisch Variante (b) ein.

---

## Updates einspielen

```bash
cd /opt/sheet-music-web
git pull
bash deploy.sh
```

---

## Konfiguration

### `backend/.env`

```env
DATA_DIR=/opt/sheet-music-web/data
MUSESCORE_BIN=musescore3
XVFB_DISPLAY=:99
AUDIVERIS_BIN=/opt/audiveris/bin/Audiveris

# Pfad zum gebauten Frontend (dist/). Wenn gesetzt, serviert FastAPI das
# Frontend selbst — kein nginx im Container noetig.
FRONTEND_DIST=/opt/sheet-music-web/frontend/dist

# Kommagetrennte Liste erlaubter CORS-Origins. Ersetzt den Default
# (localhost:5173,3000) komplett, statt ihn zu ergaenzen — bei Bedarf
# die Dev-Origins hier mit auffuehren. Muss zur Domain im Reverse-Proxy
# passen, siehe "Reverse Proxy" oben.
CORS_ORIGINS=https://meine-domain.de
```

---

## Betrieb

```bash
# Status
systemctl status sheet-music-xvfb sheet-music-backend

# Logs
journalctl -u sheet-music-backend -f

# Neustart nach Aenderungen
systemctl restart sheet-music-backend
```

---

## API-Referenz

Swagger UI: `https://meine-domain.de/api/docs`

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| `POST` | `/api/scores` | Datei hochladen (`file` + optionales `ocr=true`) |
| `GET` | `/api/scores` | Alle Partituren auflisten |
| `GET` | `/api/scores/{id}` | Metadaten + Stimmenliste |
| `GET` | `/api/scores/{id}/status` | Verarbeitungsstatus (SSE-Stream) |
| `GET` | `/api/scores/{id}/svg` | Seitenanzahl als JSON `{"pages": N}` |
| `GET` | `/api/scores/{id}/svg/{page}` | SVG-Seite (1-indiziert) |
| `GET` | `/api/scores/{id}/pdf` | Neu gesetztes PDF |
| `GET` | `/api/scores/{id}/midi` | Gesamt-MIDI |
| `GET` | `/api/scores/{id}/parts/{name}/midi` | MIDI einer Stimme |
| `GET` | `/api/scores/{id}/export/{fmt}` | Export: `mxl`, `ly`, `mp3`, `mscz` |

---

## Projektstruktur

```
sheet-music-web/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── api/routes/
│   │   │   ├── scores.py        # Upload-Endpoint, Verarbeitungs-Pipeline
│   │   │   └── files.py         # SVG, PDF, MIDI, MusicXML, Export-Endpunkte
│   │   └── services/
│   │       ├── audiveris.py     # Audiveris CLI-Wrapper (OMR, OCR)
│   │       ├── musescore.py     # MuseScore CLI-Wrapper (SVG, PDF, MIDI)
│   │       ├── xml_parser.py    # MusicXML-Parser
│   │       └── storage.py
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── components/
│       │   ├── UploadZone.tsx   # Drag & Drop + OCR-Toggle
│       │   ├── ScoreViewer.tsx  # Mehrseitige SVG-Anzeige
│       │   ├── ScoreList.tsx
│       │   ├── PartPlayer.tsx   # MIDI-Player mit Stimmenwahl
│       │   └── ExportPanel.tsx
│       └── api.ts
├── deploy/
│   ├── sheet-music-xvfb.service
│   ├── sheet-music-backend.service
│   ├── nginx.conf       # Alternative: Reverse Proxy direkt im App-Container
│   ├── Caddyfile         # Produktiv-Setup: Reverse Proxy in separatem Container
│   └── backend.env.example
├── install.sh   # Vollstaendiges Installationsscript
└── deploy.sh    # Update-Script
```

---

## Hinweise

**Erkennungsqualitaet:**
- Optimal: klare Schwarz-Weiss-Scans gedruckter Noten, mind. 300 DPI
- Eingeschraenkt: handgeschriebene Noten, verzerrte Scans
- Nicht geeignet: Fotos mit starken Schatten oder Perspektivverzerrung

**OCR / Liedtexte:**
Option "Liedtexte erkennen (OCR)" beim Upload aktivieren. Verlaengert die Verarbeitungszeit um ca. 60-120 Sekunden.

**Grosse Dateien:**
PDFs werden seitenweise verarbeitet (je Seite bis zu 15 Minuten Timeout). Fehlschlagende Seiten werden uebersprungen und geloggt.

**RAM:**
Mindestens 4 GB empfohlen. Bei 2 GB kann Audiveris bei grossen Dateien durch den OOM-Killer beendet werden.
