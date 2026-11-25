"""Holt neue Aktivitäten, wendet die Regeln an, benennt um und ändert ggf. die Sportart."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import store
from .garmin_client import state as garmin
from .rules import (Activity, counter_from_names, looks_like_default_name, matching_rules, render,
                    template_values)

log = logging.getLogger("processor")

run_lock = threading.Lock()
last_run: dict[str, Any] = {"at": None, "renamed": 0, "checked": 0, "error": ""}
recent_cache: list[dict] = []   # zuletzt geladene Aktivitäten (für die Karte)
_gear_cache: dict[int, list[str]] = {}


class Counters:
    """Zählerstände - echt (in der DB) oder nur simuliert (Probemodus/Vorschau).

    Der Stand ist immer das Maximum aus dem gespeicherten Wert und der höchsten Zahl, die in den
    Namen früherer Aktivitäten steht (falls beim Zähler ein Muster wie "Arbeit #{n}" eingetragen ist).
    """

    def __init__(self, simulate: bool) -> None:
        self.simulate = simulate
        self.sim: dict[str, dict] = {c["name"]: dict(c) for c in store.list_counters()}

    def _get(self, name: str) -> dict:
        if self.simulate:
            return self.sim.setdefault(name, store.get_counter(name))
        return store.get_counter(name)

    def base(self, name: str, when: datetime, earlier: list[Activity]) -> tuple[int, str]:
        c = self._get(name)
        pk = store.period_key(c["reset"], when)
        stored = c["value"] if (c["reset"] == "never" or c["period"] == pk) else 0
        from_names = 0
        if c.get("pattern"):
            same_period = [a.name for a in earlier
                           if c["reset"] == "never" or store.period_key(c["reset"], a.start) == pk]
            from_names = counter_from_names(c["pattern"], same_period)
        return max(stored, from_names), pk

    def take(self, name: str, when: datetime, mode: str, earlier: list[Activity]) -> int:
        base, pk = self.base(name, when, earlier)
        value = base if mode == "current" else base + 1
        if self.simulate:
            self.sim[name].update(value=value, period=pk)
        else:
            store.set_counter_state(name, value, pk)
        return value

    def current(self, name: str, when: datetime, earlier: list[Activity]) -> int:
        return self.base(name, when, earlier)[0]


@dataclass
class Decision:
    rules: list[dict] = field(default_factory=list)
    name_rule: dict | None = None
    new_name: str | None = None
    description: str = ""
    set_type: str = ""

    @property
    def label(self) -> str:
        return " + ".join((r.get("label") or r.get("template") or r.get("set_type") or "?") for r in self.rules)


def _gear(activity_id: int) -> list[str]:
    if activity_id not in _gear_cache:
        names = garmin.activity_gear(activity_id)
        if not names:
            return []  # leer nicht merken - Ausrüstung wird oft erst nach dem Upload zugeordnet
        _gear_cache[activity_id] = names
    return _gear_cache[activity_id]


def _start_date(settings: dict) -> date:
    if not settings.get("start_date"):
        # Beim allerersten Lauf: nur ab heute umbenennen, damit die alte Historie unangetastet bleibt.
        settings = store.update_settings({"start_date": date.today().isoformat()})
    return date.fromisoformat(settings["start_date"])


def _load(settings: dict, gear_for_done: bool = False) -> tuple[list[Activity], int]:
    """Lädt die neuesten Aktivitäten, älteste zuerst sortiert.

    - check_count: so viele der neuesten werden mit den Regeln geprüft
    - history_count: so viele werden insgesamt eingelesen (z. B. um den letzten Zählerstand
      aus Namen wie "Arbeit #79" zu finden). Mindestens check_count.
    Rückgabe: (alle Aktivitäten, Index der ersten zu prüfenden).
    """
    check = max(1, int(settings.get("lookback") or 30))
    history = max(check, int(settings.get("history_count") or check))
    raw = garmin.recent_activities(history)
    acts = [Activity.from_garmin(a) for a in raw if a.get("activityId")]
    acts.sort(key=lambda a: a.start)  # älteste zuerst, damit Zähler aufsteigend vergeben werden
    first = max(0, len(acts) - check)
    # Ausrüstung kostet einen Garmin-Aufruf pro Aktivität -> nur dort holen, wo sie gebraucht wird.
    for a in acts[first:]:
        st = store.history_status(a.id)
        if gear_for_done or st is None or st == "dry_run":
            a.gear = _gear(a.id)
    recent_cache[:] = [a.to_dict() for a in reversed(acts[first:])]
    return acts, first


def _decide(act: Activity, earlier: list[Activity], rules: list[dict], zones: list[dict],
            counters: Counters) -> Decision:
    d = Decision(rules=matching_rules(rules, act, zones))
    for r in d.rules:
        if not d.set_type and r.get("set_type") and r["set_type"] != act.type_key:
            d.set_type = r["set_type"]
        if d.name_rule is None and (r.get("template") or "").strip():
            d.name_rule = r
    r = d.name_rule
    if r is None:
        return d
    own = r.get("counter") or ""
    own_value = counters.take(own, act.start, r.get("counter_mode") or "increment", earlier) if own else None

    def counter_value(name: str | None) -> int:
        if name is None or name == own:
            return own_value if own_value is not None else 0
        return counters.current(name, act.start, earlier)

    values = template_values(act, zones)
    d.new_name = render(r["template"], values, counter_value) or act.name
    d.description = render(r.get("description_template") or "", values, counter_value)
    return d


def _changes(act: Activity, d: Decision) -> str:
    parts = []
    if d.new_name and d.new_name != act.name:
        parts.append("Name")
    if d.set_type:
        parts.append(f"Sportart → {d.set_type}")
    if d.description:
        parts.append("Beschreibung")
    return ", ".join(parts)


def run_once() -> dict[str, Any]:
    if not run_lock.acquire(blocking=False):
        return {"ok": False, "message": "Läuft bereits."}
    try:
        settings = store.get_settings()
        dry = bool(settings["dry_run"])
        since = _start_date(settings)
        rules, zones = store.list_rules(), store.list_zones()
        counters = Counters(simulate=dry)
        renamed = checked = 0
        acts, first = _load(settings)
        # Älteste zuerst: jede Aktivität sieht die (ggf. schon neuen) Namen aller früheren.
        for i, act in enumerate(acts):
            earlier = acts[:i]  # Namen hier sind schon aktualisiert (auch im Probemodus simuliert)
            if i < first:
                continue  # nur eingelesen (für Zähler aus Namen), nicht geprüft
            if act.start.date() < since:
                continue
            st = store.history_status(act.id)
            if st is not None and st != "dry_run":
                continue  # schon erledigt - nie zweimal anfassen
            checked += 1
            base = dict(activity_id=act.id, activity_date=act.start.isoformat(timespec="minutes"),
                        activity_type=act.type_key, old_name=act.name)
            if settings.get("skip_custom_names") and not looks_like_default_name(act.name):
                store.record_history(**base, new_name=None, rule_id=None, status="skipped",
                                     message="Eigener Name erkannt")
                continue
            d = _decide(act, earlier, rules, zones, counters)
            if not d.rules:
                store.record_history(**base, new_name=None, rule_id=None, status="no_match")
                continue
            rule_id = d.rules[0]["id"]
            what = _changes(act, d)
            if dry:
                store.record_history(**base, new_name=d.new_name, rule_id=rule_id, status="dry_run",
                                     message=f"Probemodus - nicht geändert ({what or 'nichts zu tun'})")
                if d.new_name:
                    act.name = d.new_name
                continue
            try:
                if d.set_type:
                    garmin.set_type(act.id, d.set_type)
                if d.new_name and d.new_name != act.name:
                    garmin.rename(act.id, d.new_name)
                if d.description:
                    garmin.set_description(act.id, d.description)
                store.record_history(**base, new_name=d.new_name, rule_id=rule_id, status="renamed",
                                     message=what)
                renamed += 1
                log.info("Aktivität %s: %r -> %r (%s)", act.id, act.name, d.new_name, what)
                if d.new_name:
                    act.name = d.new_name
                if d.set_type:
                    act.type_key = d.set_type
            except Exception as e:  # noqa: BLE001
                log.exception("Ändern von %s fehlgeschlagen", act.id)
                store.record_history(**base, new_name=d.new_name, rule_id=rule_id, status="error",
                                     message=str(e))
                if "TooManyRequests" in type(e).__name__:
                    break
        last_run.update(at=datetime.now().isoformat(timespec="seconds"), renamed=renamed,
                        checked=checked, error="")
        return {"ok": True, "renamed": renamed, "checked": checked, "dry_run": dry}
    except Exception as e:  # noqa: BLE001
        log.exception("Lauf fehlgeschlagen")
        last_run.update(at=datetime.now().isoformat(timespec="seconds"), error=str(e))
        return {"ok": False, "message": str(e)}
    finally:
        run_lock.release()


def preview() -> list[dict]:
    """Was würde mit den zu prüfenden Aktivitäten passieren? Ändert nichts."""
    rules, zones = store.list_rules(), store.list_zones()
    counters = Counters(simulate=True)
    out = []
    settings = store.get_settings()
    since = date.fromisoformat(settings["start_date"]) if settings.get("start_date") else date.today()
    acts, first = _load(settings, gear_for_done=True)
    for i, act in enumerate(acts):
        if i < first:
            continue
        hist = store.history_status(act.id)
        done = hist is not None and hist != "dry_run"
        before_start = act.start.date() < since
        row = {**act.to_dict(), "history": hist, "before_start": before_start}
        if done:
            # Wurde schon verarbeitet und wird nicht mehr angefasst - zählt daher nicht hoch
            matched = matching_rules(rules, act, zones)
            d = Decision(rules=matched)
        else:
            # Vor dem Startdatum: nur zur Ansicht, mit eigenem Zähler-Durchlauf, damit die echten
            # Nummern der neuen Aktivitäten nicht verfälscht werden.
            c = Counters(simulate=True) if before_start else counters
            d = _decide(act, acts[:i], rules, zones, c)
        row.update(rule_id=d.rules[0]["id"] if d.rules else None, rule_label=d.label if d.rules else "",
                   new_name=d.new_name or "", set_type=d.set_type)
        out.append(row)
        if not done and not before_start and d.new_name:
            act.name = d.new_name  # spätere Aktivitäten sehen den neuen Namen (für Zähler aus Namen)
    out.reverse()
    return out
