"""SQLite-Speicher für Zonen, Regeln, Zähler, Einstellungen und Verlauf."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "/config"))
DB_PATH = DATA_DIR / "garmin-renamer.db"

DEFAULT_SETTINGS: dict[str, Any] = {
    "poll_minutes": 15,
    "lookback": 30,          # wie viele der neuesten Aktivitäten pro Lauf mit den Regeln geprüft werden
    "history_count": 100,    # wie viele insgesamt eingelesen werden (z. B. für Zählerstand aus Namen)
    "dry_run": True,         # Probemodus: nichts wirklich umbenennen
    "start_date": "",        # nur Aktivitäten ab diesem Datum (YYYY-MM-DD); leer = ab erstem Start
    "skip_custom_names": False,  # Aktivitäten mit offensichtlich eigenem Namen überspringen
}

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 200,
    color TEXT NOT NULL DEFAULT '#2563eb'
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    label TEXT NOT NULL DEFAULT '',
    template TEXT NOT NULL,
    description_template TEXT NOT NULL DEFAULT '',
    counter TEXT NOT NULL DEFAULT '',
    counter_mode TEXT NOT NULL DEFAULT 'increment',  -- increment | current
    set_type TEXT NOT NULL DEFAULT '',               -- Sportart ändern auf typeKey
    cont INTEGER NOT NULL DEFAULT 0,                 -- danach weitere Regeln prüfen
    conditions TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0,
    reset TEXT NOT NULL DEFAULT 'never',   -- never | yearly | monthly | weekly
    period TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT ''       -- z. B. "Arbeit #{n}": Stand aus Aktivitätsnamen lesen
);
CREATE TABLE IF NOT EXISTS history (
    activity_id INTEGER PRIMARY KEY,
    activity_date TEXT,
    activity_type TEXT,
    old_name TEXT,
    new_name TEXT,
    rule_id INTEGER,
    status TEXT NOT NULL,          -- renamed | dry_run | no_match | skipped | error
    message TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    with _lock:
        if _db is None:
            _db = _conn()
            _db.executescript(SCHEMA)
            _migrate(_db)
            _db.commit()
        return _db


MIGRATIONS = {
    "rules": {
        "counter_mode": "TEXT NOT NULL DEFAULT 'increment'",
        "set_type": "TEXT NOT NULL DEFAULT ''",
        "cont": "INTEGER NOT NULL DEFAULT 0",
    },
    "counters": {"pattern": "TEXT NOT NULL DEFAULT ''"},
}


def _migrate(c: sqlite3.Connection) -> None:
    """Neue Spalten in bestehenden Datenbanken ergänzen."""
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def reset_for_tests(path: Path) -> None:
    """Nur für Tests: andere Datenbankdatei verwenden."""
    global _db, DB_PATH, DATA_DIR
    with _lock:
        if _db is not None:
            _db.close()
        _db = None
        DATA_DIR = path.parent
        DB_PATH = path


# ---------------------------------------------------------------- Einstellungen
def get_settings() -> dict[str, Any]:
    with _lock:
        rows = db().execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = json.loads(r["value"])
    return out


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                db().execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
        db().commit()
    return get_settings()


# ---------------------------------------------------------------- Zonen
def list_zones() -> list[dict]:
    with _lock:
        return [dict(r) for r in db().execute("SELECT * FROM zones ORDER BY name")]


def save_zone(z: dict) -> dict:
    with _lock:
        if z.get("id"):
            db().execute(
                "UPDATE zones SET name=?, lat=?, lon=?, radius_m=?, color=? WHERE id=?",
                (z["name"], z["lat"], z["lon"], z["radius_m"], z.get("color", "#2563eb"), z["id"]),
            )
            zid = z["id"]
        else:
            cur = db().execute(
                "INSERT INTO zones(name, lat, lon, radius_m, color) VALUES(?,?,?,?,?)",
                (z["name"], z["lat"], z["lon"], z["radius_m"], z.get("color", "#2563eb")),
            )
            zid = cur.lastrowid
        db().commit()
        return dict(db().execute("SELECT * FROM zones WHERE id=?", (zid,)).fetchone())


def delete_zone(zid: int) -> None:
    with _lock:
        db().execute("DELETE FROM zones WHERE id=?", (zid,))
        db().commit()


# ---------------------------------------------------------------- Regeln
def _rule_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["cont"] = bool(d.get("cont"))
    d["conditions"] = json.loads(d["conditions"] or "{}")
    return d


def list_rules() -> list[dict]:
    with _lock:
        return [_rule_row(r) for r in db().execute("SELECT * FROM rules ORDER BY position, id")]


def save_rule(r: dict) -> dict:
    vals = (
        int(bool(r.get("enabled", True))),
        r.get("label", ""),
        r["template"],
        r.get("description_template", ""),
        (r.get("counter") or "").strip(),
        r.get("counter_mode") or "increment",
        (r.get("set_type") or "").strip(),
        int(bool(r.get("cont"))),
        json.dumps(r.get("conditions") or {}),
    )
    with _lock:
        if r.get("id"):
            db().execute(
                "UPDATE rules SET enabled=?, label=?, template=?, description_template=?, "
                "counter=?, counter_mode=?, set_type=?, cont=?, conditions=? WHERE id=?",
                (*vals, r["id"]),
            )
            rid = r["id"]
        else:
            pos = db().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rules").fetchone()[0]
            cur = db().execute(
                "INSERT INTO rules(enabled, label, template, description_template, counter, "
                "counter_mode, set_type, cont, conditions, position) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*vals, pos),
            )
            rid = cur.lastrowid
        if vals[4]:
            db().execute("INSERT OR IGNORE INTO counters(name) VALUES(?)", (vals[4],))
        db().commit()
        return _rule_row(db().execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone())


def delete_rule(rid: int) -> None:
    with _lock:
        db().execute("DELETE FROM rules WHERE id=?", (rid,))
        db().commit()


def reorder_rules(ids: list[int]) -> None:
    with _lock:
        for pos, rid in enumerate(ids):
            db().execute("UPDATE rules SET position=? WHERE id=?", (pos, rid))
        db().commit()


# ---------------------------------------------------------------- Zähler
def list_counters() -> list[dict]:
    with _lock:
        return [dict(r) for r in db().execute("SELECT * FROM counters ORDER BY name")]


def save_counter(c: dict) -> None:
    with _lock:
        db().execute(
            "INSERT INTO counters(name, value, reset, period, pattern) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, reset=excluded.reset, "
            "pattern=excluded.pattern",
            (c["name"].strip(), int(c.get("value", 0)), c.get("reset", "never"), c.get("period", ""),
             (c.get("pattern") or "").strip()),
        )
        db().commit()


def delete_counter(name: str) -> None:
    with _lock:
        db().execute("DELETE FROM counters WHERE name=?", (name,))
        db().commit()


def period_key(reset: str, when: datetime) -> str:
    if reset == "yearly":
        return f"{when.year}"
    if reset == "monthly":
        return f"{when.year}-{when.month:02d}"
    if reset == "weekly":
        y, w, _ = when.isocalendar()
        return f"{y}-W{w:02d}"
    return ""


def peek_counter(name: str, when: datetime) -> int:
    """Wert, den der Zähler beim nächsten Hochzählen hätte (ohne zu speichern)."""
    with _lock:
        row = db().execute("SELECT * FROM counters WHERE name=?", (name,)).fetchone()
    if row is None:
        return 1
    if period_key(row["reset"], when) != row["period"] and row["reset"] != "never":
        return 1
    return row["value"] + 1


def get_counter(name: str) -> dict:
    with _lock:
        row = db().execute("SELECT * FROM counters WHERE name=?", (name,)).fetchone()
    return dict(row) if row else {"name": name, "value": 0, "reset": "never", "period": "", "pattern": ""}


def set_counter_state(name: str, value: int, period: str) -> None:
    with _lock:
        db().execute("INSERT OR IGNORE INTO counters(name) VALUES(?)", (name,))
        db().execute("UPDATE counters SET value=?, period=? WHERE name=?", (value, period, name))
        db().commit()


def current_counter(name: str) -> int:
    with _lock:
        row = db().execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
    return row["value"] if row else 0


def increment_counter(name: str, when: datetime) -> int:
    with _lock:
        db().execute("INSERT OR IGNORE INTO counters(name) VALUES(?)", (name,))
        row = db().execute("SELECT * FROM counters WHERE name=?", (name,)).fetchone()
        pk = period_key(row["reset"], when)
        value = row["value"] + 1
        if row["reset"] != "never" and pk != row["period"]:
            value = 1
        db().execute("UPDATE counters SET value=?, period=? WHERE name=?", (value, pk, name))
        db().commit()
        return value


# ---------------------------------------------------------------- Verlauf
def history_status(activity_id: int) -> str | None:
    with _lock:
        row = db().execute("SELECT status FROM history WHERE activity_id=?", (activity_id,)).fetchone()
    return row["status"] if row else None


def record_history(**h: Any) -> None:
    h.setdefault("message", "")
    h["processed_at"] = datetime.now().isoformat(timespec="seconds")
    with _lock:
        db().execute(
            "INSERT INTO history(activity_id, activity_date, activity_type, old_name, new_name, rule_id, "
            "status, message, processed_at) VALUES(:activity_id, :activity_date, :activity_type, :old_name, "
            ":new_name, :rule_id, :status, :message, :processed_at) "
            "ON CONFLICT(activity_id) DO UPDATE SET activity_date=excluded.activity_date, "
            "activity_type=excluded.activity_type, old_name=excluded.old_name, new_name=excluded.new_name, "
            "rule_id=excluded.rule_id, status=excluded.status, message=excluded.message, "
            "processed_at=excluded.processed_at",
            h,
        )
        db().commit()


def list_history(limit: int = 100) -> list[dict]:
    with _lock:
        return [
            dict(r)
            for r in db().execute(
                "SELECT * FROM history ORDER BY activity_date DESC, activity_id DESC LIMIT ?", (limit,)
            )
        ]


def forget_history(activity_id: int) -> None:
    with _lock:
        db().execute("DELETE FROM history WHERE activity_id=?", (activity_id,))
        db().commit()
