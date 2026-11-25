"""Regel-Logik: Aktivität aufbereiten, Regeln prüfen, Namen aus Vorlage bauen.

Bewusst ohne Garmin- oder Datenbank-Abhängigkeit, damit es sich einfach testen lässt.
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Gruppen, damit man nicht jede Garmin-Sportart einzeln auswählen muss.
TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "@rad": ("cycling", "biking", "bmx", "cyclocross"),
    "@rad_outdoor": ("road_biking", "gravel_cycling", "mountain_biking", "cycling", "e_bike", "cyclocross",
                     "bmx", "recumbent", "commuting"),
    "@laufen": ("running",),
    "@gehen": ("walking", "hiking"),
    "@schwimmen": ("swimming",),
    "@winter": ("skiing", "snowboard", "skate_skiing", "backcountry", "snowshoe"),
}
INDOOR_HINTS = ("indoor", "virtual", "treadmill")

TYPE_LABELS_DE = {
    "cycling": "Radfahren", "road_biking": "Rennrad", "gravel_cycling": "Gravel", "mountain_biking": "Mountainbike",
    "e_bike_fitness": "E-Bike", "e_bike_mountain": "E-MTB", "indoor_cycling": "Indoor-Radfahren",
    "virtual_ride": "Virtuelle Radfahrt", "recumbent_cycling": "Liegerad", "hand_cycling": "Handbike",
    "bmx": "BMX", "cyclocross": "Cyclocross", "track_cycling": "Bahnrad", "running": "Laufen", "trail_running": "Trailrun",
    "treadmill_running": "Laufband", "walking": "Gehen", "hiking": "Wandern", "lap_swimming": "Schwimmen",
    "open_water_swimming": "Freiwasserschwimmen", "strength_training": "Krafttraining",
    "resort_skiing_snowboarding_ws": "Skifahren", "backcountry_skiing": "Skitour",
}

# Wörter, an denen man Garmins automatische Namen erkennt ("Linz Radfahren", "Wien Running", ...)
DEFAULT_NAME_WORDS = (
    "radfahren", "cycling", "rennrad", "road cycling", "gravel", "mountainbike", "mountain biking", "e-bike",
    "laufen", "running", "trailrun", "trail running", "laufband", "treadmill", "gehen", "walking", "wandern",
    "hiking", "schwimmen", "swimming", "krafttraining", "strength", "ride", "run", "walk", "indoor", "virtuell",
    "virtual", "skifahren", "skiing", "skitour", "langlauf", "yoga", "cardio", "rudern", "rowing", "fahrrad",
)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Activity:
    id: int
    name: str
    type_key: str
    start: datetime
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    distance_km: float = 0.0
    duration_s: float = 0.0
    elevation_gain: float = 0.0
    avg_speed_kmh: float = 0.0
    location: str = ""
    gear: list[str] = field(default_factory=list)

    @classmethod
    def from_garmin(cls, a: dict[str, Any], gear: list[str] | None = None) -> "Activity":
        def f(key: str) -> float | None:
            v = a.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        start_raw = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        try:
            start = datetime.fromisoformat(str(start_raw).replace("T", " ")[:19])
        except ValueError:
            start = datetime.now()
        speed = f("averageSpeed") or 0.0  # m/s
        return cls(
            id=int(a["activityId"]),
            name=a.get("activityName") or "",
            type_key=(a.get("activityType") or {}).get("typeKey", "") or "",
            start=start,
            start_lat=f("startLatitude"),
            start_lon=f("startLongitude"),
            end_lat=f("endLatitude"),
            end_lon=f("endLongitude"),
            distance_km=(f("distance") or 0.0) / 1000.0,
            duration_s=f("duration") or f("movingDuration") or 0.0,
            elevation_gain=f("elevationGain") or 0.0,
            avg_speed_kmh=speed * 3.6,
            location=a.get("locationName") or "",
            gear=gear or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "type_key": self.type_key,
            "start": self.start.isoformat(timespec="minutes"),
            "start_lat": self.start_lat, "start_lon": self.start_lon,
            "end_lat": self.end_lat, "end_lon": self.end_lon,
            "distance_km": round(self.distance_km, 2), "duration_s": self.duration_s,
            "elevation_gain": self.elevation_gain, "gear": self.gear, "location": self.location,
        }


def zone_of(lat: float | None, lon: float | None, zones: list[dict]) -> dict | None:
    """Nächstgelegene Zone, in deren Radius der Punkt liegt."""
    if lat is None or lon is None:
        return None
    best, best_d = None, float("inf")
    for z in zones:
        d = haversine_m(lat, lon, z["lat"], z["lon"])
        if d <= z["radius_m"] and d < best_d:
            best, best_d = z, d
    return best


def type_matches(type_key: str, wanted: list[str]) -> bool:
    if not wanted:
        return True
    key = type_key.lower()
    for w in wanted:
        if w == "@indoor":
            if any(h in key for h in INDOOR_HINTS):
                return True
        elif w in TYPE_GROUPS:
            if any(part in key for part in TYPE_GROUPS[w]) and not (
                w == "@rad_outdoor" and any(h in key for h in INDOOR_HINTS)
            ):
                return True
        elif key == w.lower():
            return True
    return False


def gear_matches(pattern: str, gear: list[str]) -> bool:
    """Leer = egal. Mit * oder ? als Platzhalter (z. B. *M9*), sonst "enthält".
    Mehrere Alternativen mit | trennen (z. B. *M9*|Stadtrad). Groß/klein egal."""
    alts = [p.strip().lower() for p in (pattern or "").split("|") if p.strip()]
    if not alts:
        return True
    names = [g.lower().strip() for g in gear]
    for p in alts:
        for n in names:
            if any(ch in p for ch in "*?[") and fnmatch.fnmatchcase(n, p):
                return True
            if not any(ch in p for ch in "*?[") and p in n:
                return True
    return False


def counter_from_names(pattern: str, names: list[str]) -> int:
    """Höchste Zahl, die in den Namen an der Stelle von {n} steht. "Arbeit #{n}" findet 79 in
    "Arbeit #79 M9" und in "Von der Arbeit #79 M9"."""
    if not pattern or "{n}" not in pattern:
        return 0
    rx = re.compile(re.escape(pattern.strip()).replace(r"\{n\}", r"(\d+)"), re.IGNORECASE)
    best = 0
    for name in names:
        for m in rx.finditer(name or ""):
            best = max(best, int(m.group(1)))
    return best


def matching_rules(rules: list[dict], act: Activity, zones: list[dict]) -> list[dict]:
    """Alle Regeln, die greifen: die erste passende, plus weitere, solange 'danach weiter prüfen' an ist."""
    out = []
    for r in rules:
        if not (r.get("template") or "").strip():
            continue  # reine Sportart-Regeln: siehe unten
        if r.get("enabled", True) and rule_matches(r, act, zones)[0]:
            out.append(r)
            if not r.get("cont"):
                break
    # Regeln ohne neuen Namen (z. B. nur "Sportart -> Liegerad") gelten immer, egal wo sie stehen.
    for r in rules:
        if (r not in out and r.get("enabled", True) and not (r.get("template") or "").strip()
                and rule_matches(r, act, zones)[0]):
            out.append(r)
    return out


def _hhmm(s: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _zone_cond(cond_value: Any, zone: dict | None) -> bool:
    """'' = egal, 'none' = in keiner Zone, sonst Zonen-ID."""
    if cond_value in (None, "", "any"):
        return True
    if cond_value == "none":
        return zone is None
    return zone is not None and str(zone["id"]) == str(cond_value)


def rule_matches(rule: dict, act: Activity, zones: list[dict]) -> tuple[bool, list[str]]:
    """Gibt (passt?, Begründungen warum nicht) zurück."""
    c = rule.get("conditions") or {}
    why: list[str] = []

    if not type_matches(act.type_key, c.get("types") or []):
        why.append(f"Sportart {act.type_key}")

    zs, ze = zone_of(act.start_lat, act.start_lon, zones), zone_of(act.end_lat, act.end_lon, zones)
    start_ok = _zone_cond(c.get("start_zone"), zs)
    end_ok = _zone_cond(c.get("end_zone"), ze)
    if not (start_ok and end_ok) and c.get("either_direction"):
        # Hin oder zurück: Start und Ziel vertauscht prüfen
        if _zone_cond(c.get("start_zone"), ze) and _zone_cond(c.get("end_zone"), zs):
            start_ok = end_ok = True
    if not start_ok:
        why.append("Startzone")
    if not end_ok:
        why.append("Zielzone")

    if not gear_matches(c.get("gear") or "", act.gear):
        why.append("Ausrüstung")

    days = c.get("weekdays") or []
    if days and act.start.weekday() not in [int(d) for d in days]:
        why.append("Wochentag")

    t_from, t_to = _hhmm(c.get("time_from", "")), _hhmm(c.get("time_to", ""))
    mins = act.start.hour * 60 + act.start.minute
    if t_from is not None and t_to is not None:
        ok = t_from <= mins <= t_to if t_from <= t_to else (mins >= t_from or mins <= t_to)
        if not ok:
            why.append("Uhrzeit")
    elif t_from is not None and mins < t_from:
        why.append("Uhrzeit")
    elif t_to is not None and mins > t_to:
        why.append("Uhrzeit")

    if c.get("min_km") not in (None, "") and act.distance_km < float(c["min_km"]):
        why.append("Distanz zu kurz")
    if c.get("max_km") not in (None, "") and act.distance_km > float(c["max_km"]):
        why.append("Distanz zu lang")

    pattern = (c.get("name_regex") or "").strip()
    if pattern:
        try:
            if not re.search(pattern, act.name, re.IGNORECASE):
                why.append("Originalname")
        except re.error:
            why.append("ungültiger Regex")

    return (not why, why)


def first_matching_rule(rules: list[dict], act: Activity, zones: list[dict]) -> dict | None:
    for r in rules:
        if r.get("enabled", True) and rule_matches(r, act, zones)[0]:
            return r
    return None


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}:{m:02d} h" if h else f"{m} min"


def template_values(act: Activity, zones: list[dict]) -> dict[str, str]:
    zs, ze = zone_of(act.start_lat, act.start_lon, zones), zone_of(act.end_lat, act.end_lon, zones)
    return {
        "original": act.name,
        "type": TYPE_LABELS_DE.get(act.type_key, act.type_key.replace("_", " ").title()),
        "distance_km": f"{act.distance_km:.1f}".replace(".", ","),
        "distance": f"{act.distance_km:.0f}",
        "duration": _fmt_duration(act.duration_s),
        "elevation": f"{act.elevation_gain:.0f}",
        "speed": f"{act.avg_speed_kmh:.1f}".replace(".", ","),
        "start_zone": zs["name"] if zs else "",
        "end_zone": ze["name"] if ze else "",
        "gear": ", ".join(act.gear),
        "location": act.location,
        "date": act.start.strftime("%d.%m.%Y"),
        "time": act.start.strftime("%H:%M"),
        "weekday": WEEKDAYS_DE[act.start.weekday()],
        "wd": WEEKDAYS_SHORT[act.start.weekday()],
        "year": str(act.start.year),
        "month": f"{act.start.month:02d}",
        "daytime": _daytime(act.start.hour),
    }


def _daytime(h: int) -> str:
    if 5 <= h < 11:
        return "Morgen"
    if 11 <= h < 14:
        return "Mittag"
    if 14 <= h < 18:
        return "Nachmittag"
    if 18 <= h < 22:
        return "Abend"
    return "Nacht"


_PLACEHOLDER = re.compile(r"\{(counter(?::([\w\-]+))?|[a-z_]+)\}")


def render(template: str, values: dict[str, str], counter_value: Callable[[str | None], int]) -> str:
    """{counter} = Zähler der Regel, {counter:name} = Stand eines anderen Zählers."""

    def sub(m: re.Match) -> str:
        if m.group(1).startswith("counter"):
            return str(counter_value(m.group(2)))
        return values.get(m.group(1), m.group(0))

    out = _PLACEHOLDER.sub(sub, template)
    return re.sub(r"\s{2,}", " ", out).strip()


def looks_like_default_name(name: str) -> bool:
    n = name.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", n) for w in DEFAULT_NAME_WORDS)
