# 🚲 Garmin Renamer

Benennt Garmin-Connect-Aktivitäten automatisch nach eigenen Regeln um – ähnlich wie StravAutomator, nur für Garmin und selbst gehostet.

- **Zonen auf der Karte**: Start in Zone A, Ziel in Zone B (optional beide Richtungen)
- **Fahrrad / Ausrüstung**, Sportart, Wochentag, Uhrzeit, Distanz, Originalname (Regex)
- **Zähler** (`{counter}`), z. B. „Arbeit #57 – Hinweg“, auf Wunsch jährlich/monatlich/wöchentlich zurückgesetzt
  - pro Regel **+1** (Hinweg) oder **gleiche Nummer** (Heimweg)
  - Stand kann aus den letzten Aktivitätsnamen gelesen werden (Muster `Arbeit #{n}`)
- **Sportart ändern**, z. B. auf *Liegerad*, wenn eine bestimmte Ausrüstung verwendet wurde
- Ausrüstung mit Platzhalter: `*M9*`, mehrere mit `|`
- **Probemodus** und **Vorschau**: erst sehen, was passieren würde, dann scharf schalten
- Jede Aktivität wird **nur einmal** angefasst. Benennst du sie danach selbst um, bleibt dein Name.

## Installation auf Unraid

Das Image wird bei jeder Änderung automatisch von GitHub gebaut: `ghcr.io/schwaxi/garmin-renamer:latest`.

1. Vorlage auf Unraid speichern (einmalig, im Unraid-Terminal):
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-garmin-renamer.xml      https://raw.githubusercontent.com/schwaxi/garmin-renamer/main/unraid-template.xml
   ```
2. **Docker → Add Container → Template: garmin-renamer → Apply**
3. Oberfläche öffnen: `http://<unraid-ip>:8085`

**Updates:** Auf der Docker-Seite erscheint „update ready“ → anklicken. Deine Regeln, Zonen und Zähler bleiben erhalten (sie liegen in `/mnt/user/appdata/garmin-renamer`).

Alternativ mit Compose: `docker compose up -d` (nutzt dasselbe Image).

## Erste Schritte

1. **Übersicht → Garmin Connect**: mit E-Mail und Passwort anmelden, ggf. den MFA-Code eingeben.
   Das Passwort wird nicht gespeichert, nur die Tokens in `/config/garmin-tokens`.
2. **Zonen**: in die Karte klicken, Namen vergeben, Radius einstellen. Grüne/rote Punkte zeigen Start/Ziel deiner letzten Aktivitäten (nach „Vorschau laden“).
3. **Regeln**: z. B.
   - *Arbeitsweg hin*: Sportart „Alle Radtypen“, Start *Zuhause*, Ziel *Arbeit*, Zähler `arbeit`, Name `🚲 Arbeit #{counter} – Hinweg`
   - *Arbeitsweg heim*: umgekehrt, gleicher Zähler `arbeit`
   - *Gravel*: Ausrüstung enthält „Canyon“, Name `Gravel-{daytime} mit dem {gear} ({distance_km} km)`
   Die **erste passende Regel** gewinnt – Reihenfolge mit ↑/↓ ändern.
   Regeln **ohne neuen Namen** (nur „Sportart ändern auf“) gelten immer zusätzlich, egal wo sie stehen.

### Beispiel: Arbeitsweg mit fortlaufender Nummer

| Regel | Bedingung | Neuer Name | Zähler | Verhalten |
|---|---|---|---|---|
| Hinweg | Start *Zuhause*, Ziel *Arbeit* | `Arbeit #{counter} M9` | `arbeit` | +1 |
| Heimweg | Start *Arbeit*, Ziel *Zuhause* | `Von der Arbeit #{counter} M9` | `arbeit` | gleiche Nummer |
| Liegerad | Ausrüstung `*M9*` | *(leer)* – Sportart → Liegerad | – | – |

Beim Zähler `arbeit` unter *Zähler* das Muster `Arbeit #{n}` eintragen: Steht heute früh schon „Arbeit #79 M9“ in Garmin, wird der Heimweg „Von der Arbeit #79 M9“ und die nächste Fahrt „Arbeit #80 M9“.
4. **Vorschau laden** und prüfen. Wenn alles passt: **Scharf schalten**.

### Platzhalter

`{counter}` `{counter:name}` `{start_zone}` `{end_zone}` `{gear}` `{distance_km}` `{distance}` `{duration}` `{elevation}` `{speed}` `{type}` `{date}` `{time}` `{weekday}` `{wd}` `{daytime}` `{year}` `{month}` `{location}` `{original}`

### Wichtig zu wissen

- **Startdatum**: Beim ersten Lauf wird es auf heute gesetzt, damit alte Aktivitäten nicht umbenannt werden. In den Einstellungen änderbar.
- **Zähler startwert**: Unter *Zähler* kannst du den Stand setzen (z. B. auf 56, wenn du dieses Jahr schon 56-mal gefahren bist).
- Im Probemodus wird der Zähler nur simuliert. Erst nach dem Scharfschalten zählt er wirklich.
- „↻“ im Verlauf wendet die Regeln für eine Aktivität erneut an (der Zähler steigt dabei wieder um 1).

## Wenn der Login über die Oberfläche nicht klappt

```bash
docker exec -it garmin-renamer python -m app.cli_login
docker restart garmin-renamer
```

## Optionen (Umgebungsvariablen)

| Variable | Bedeutung |
|---|---|
| `UI_PASSWORD` | Passwortschutz für die Oberfläche (Benutzer `admin` bzw. `UI_USER`) |
| `TZ` | Zeitzone, Standard `Europe/Vienna` |
| `DEMO=1` | Beispieldaten statt Garmin-Konto – zum Ausprobieren |
| `LOG_LEVEL` | z. B. `DEBUG` |

## Lokal ausprobieren (ohne Garmin)

```bash
pip install fastapi "uvicorn[standard]"
DEMO=1 DATA_DIR=./data uvicorn app.main:app --port 8085
```

Tests: `pip install pytest && pytest tests`

## Technik

Python 3.12, FastAPI, SQLite, [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) ≥ 0.3.20 (neue Garmin-Anmeldung seit 2026), Leaflet + OpenStreetMap.
Die Oberfläche hat ohne `UI_PASSWORD` keinen Schutz – nur im Heimnetz betreiben.
