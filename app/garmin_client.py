"""Verbindung zu Garmin Connect (python-garminconnect >= 0.3) inkl. Login mit MFA über die Weboberfläche."""
from __future__ import annotations

import logging
import os
import random
import threading
from datetime import datetime, timedelta
from typing import Any

from . import store

log = logging.getLogger("garmin")

DEMO = os.getenv("DEMO", "").lower() in ("1", "true", "yes")


def token_dir() -> str:
    return os.getenv("GARMINTOKENS", str(store.DATA_DIR / "garmin-tokens"))


class GarminState:
    def __init__(self) -> None:
        self.client: Any = None
        self.status = "logged_out"   # logged_out | mfa_required | ok | error
        self.message = ""
        self.user = ""
        self._pending: tuple[Any, Any] | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ Login
    def try_tokens(self) -> bool:
        if DEMO:
            self.client, self.status, self.user, self.message = FakeGarmin(), "ok", "Demo-Modus", ""
            return True
        from garminconnect import Garmin  # erst hier importieren, damit Tests ohne Paket laufen

        if not os.path.exists(os.path.join(token_dir(), "garmin_tokens.json")):
            self.status, self.message = "logged_out", "Noch nicht angemeldet."
            return False
        try:
            g = Garmin()
            g.login(token_dir())
            self._set_ok(g)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Login mit gespeicherten Tokens fehlgeschlagen: %s", e)
            self.status, self.message = "logged_out", f"Gespeicherte Anmeldung ungültig: {e}"
            return False

    def start_login(self, email: str, password: str) -> str:
        from garminconnect import Garmin

        with self._lock:
            os.makedirs(token_dir(), exist_ok=True)
            g = Garmin(email=email, password=password, return_on_mfa=True)
            try:
                result = g.login(token_dir())
            except Exception as e:  # noqa: BLE001
                self.status, self.message = "error", f"Anmeldung fehlgeschlagen: {e}"
                return self.status
            if isinstance(result, tuple) and result and result[0] == "needs_mfa":
                self._pending = (g, result[1])
                self.status, self.message = "mfa_required", "Bitte den Code aus der Garmin-Mail/App eingeben."
                return self.status
            self._persist(g)
            self._set_ok(g)
            return self.status

    def finish_mfa(self, code: str) -> str:
        with self._lock:
            if not self._pending:
                self.status, self.message = "error", "Kein Login wartet auf einen Code."
                return self.status
            g, client_state = self._pending
            try:
                g.resume_login(client_state, code.strip())
            except Exception as e:  # noqa: BLE001
                self.message = f"Code abgelehnt: {e}"
                return self.status  # bleibt mfa_required, neuer Versuch möglich
            self._pending = None
            self._persist(g)
            self._set_ok(g)
            return self.status

    def logout(self) -> None:
        with self._lock:
            if self.client is not None and not DEMO:
                try:
                    self.client.logout(token_dir())
                except Exception:  # noqa: BLE001
                    pass
            self.client, self.status, self.message, self.user = None, "logged_out", "Abgemeldet.", ""

    def _set_ok(self, g: Any) -> None:
        self.client, self.status, self.message = g, "ok", ""
        for attr in ("full_name", "display_name"):
            v = getattr(g, attr, None)
            if v:
                self.user = str(v)
                break

    def _persist(self, g: Any) -> None:
        """Tokens sichern, falls die Bibliothek das nach dem MFA-Login nicht selbst getan hat."""
        if os.path.exists(os.path.join(token_dir(), "garmin_tokens.json")):
            return
        for obj in (g, getattr(g, "client", None), getattr(g, "garth", None)):
            for meth in ("dump", "save_tokens", "dump_tokens"):
                fn = getattr(obj, meth, None) if obj is not None else None
                if callable(fn):
                    try:
                        fn(token_dir())
                        log.info("Tokens gespeichert über %s.%s", type(obj).__name__, meth)
                        return
                    except Exception:  # noqa: BLE001
                        continue
        log.warning("Tokens konnten nicht gespeichert werden - nach einem Neustart ist ein neuer Login nötig.")

    # ------------------------------------------------------------ API
    def require(self) -> Any:
        if self.client is None:
            raise RuntimeError("Nicht bei Garmin angemeldet.")
        return self.client

    def recent_activities(self, limit: int) -> list[dict]:
        """Neueste Aktivitäten, seitenweise geholt (Garmin begrenzt die Anzahl pro Abfrage)."""
        c, out, page = self.require(), [], 50
        while len(out) < limit:
            want = min(page, limit - len(out))
            batch = c.get_activities(len(out), want) or []
            out.extend(batch)
            if len(batch) < want:
                break  # keine weiteren Aktivitäten
        return out[:limit]

    def activity_gear(self, activity_id: int) -> list[str]:
        c = self.require()
        for meth in ("get_activity_gear", "get_activity_gear_v2"):
            fn = getattr(c, meth, None)
            if not callable(fn):
                continue
            try:
                raw = fn(activity_id)
            except Exception as e:  # noqa: BLE001
                log.info("%s(%s) fehlgeschlagen: %s", meth, activity_id, e)
                continue
            log.debug("%s(%s) -> %r", meth, activity_id, raw)
            names = gear_names(raw)
            if names:
                return names
        return []

    def activity_types(self) -> list[dict]:
        """Alle Garmin-Sportarten (typeId, typeKey, parentTypeId). Wird zwischengespeichert."""
        if getattr(self, "_types", None):
            return self._types
        raw = self.require().get_activity_types()
        if isinstance(raw, dict):
            raw = raw.get("activityTypes") or raw.get("items") or list(raw.values())
        self._types = [t for t in raw or [] if isinstance(t, dict) and t.get("typeKey")]
        return self._types

    def set_type(self, activity_id: int, type_key: str) -> None:
        t = next((t for t in self.activity_types() if t["typeKey"] == type_key), None)
        if t is None:
            raise RuntimeError(f"Sportart '{type_key}' gibt es bei Garmin nicht.")
        self.require().set_activity_type(str(activity_id), t["typeId"], t["typeKey"], t.get("parentTypeId"))

    def gear_list(self) -> list[str]:
        c = self.require()
        for meth in ("get_gear_list",):
            fn = getattr(c, meth, None)
            if callable(fn):
                try:
                    return gear_names(fn(0, 100))
                except Exception as e:  # noqa: BLE001
                    log.info("%s fehlgeschlagen: %s", meth, e)
        return []

    def rename(self, activity_id: int, name: str) -> None:
        self.require().set_activity_name(activity_id, name)

    def set_description(self, activity_id: int, text: str) -> None:
        c = self.require()
        fn = getattr(c, "set_activity_description", None)
        if callable(fn):
            fn(activity_id, text)
        else:
            raise RuntimeError("Diese garminconnect-Version kann keine Beschreibung setzen.")


def gear_names(raw: Any) -> list[str]:
    """Namen aus den verschiedenen Garmin-Antwortformen (alt: filterGear, neu: v2) herausholen."""
    if isinstance(raw, dict):
        raw = raw.get("gearList") or raw.get("gear") or raw.get("items") or raw.get("gearDTOs") or [raw]
    names: list[str] = []

    def add(v: Any) -> None:
        v = str(v).strip() if v else ""
        if v and v.lower() not in ("other", "none", "null") and v not in names:
            names.append(v)

    for g in raw or []:
        if not isinstance(g, dict):
            continue
        add(g.get("displayName"))
        add(g.get("customMakeModel"))
        add(g.get("name"))
        add(" ".join(str(x) for x in (g.get("gearMakeName"), g.get("gearModelName")) if x and x != "Other"))
        add(" ".join(str(x) for x in (g.get("brand"), g.get("model")) if x))
    return names


state = GarminState()


# ---------------------------------------------------------------- Demo
class FakeGarmin:
    """Liefert Beispiel-Aktivitäten, damit man die Oberfläche ohne Garmin-Konto ausprobieren kann."""

    HOME = (48.3064, 14.2861)
    WORK = (48.2685, 14.3297)

    def __init__(self) -> None:
        rnd = random.Random(7)
        self.names: dict[int, str] = {}
        self.acts: list[dict] = []
        self.gear: dict[int, list[dict]] = {}
        now = datetime.now().replace(second=0, microsecond=0)
        aid = 20000000000
        for day in range(21, 0, -1):
            d = now - timedelta(days=day)
            if d.weekday() < 5:
                for start, end, hour in ((self.HOME, self.WORK, 7), (self.WORK, self.HOME, 17)):
                    aid += 1
                    self._add(aid, d.replace(hour=hour, minute=rnd.randint(0, 40)), "cycling", start, end,
                              rnd.uniform(6.8, 7.6), "Liegerad M9", rnd)
            elif d.weekday() == 5:
                aid += 1
                self._add(aid, d.replace(hour=9, minute=30), "gravel_cycling", self.HOME, self.HOME,
                          rnd.uniform(45, 80), "Canyon Grizl", rnd)
            else:
                aid += 1
                self._add(aid, d.replace(hour=10, minute=0), "running", self.HOME, self.HOME,
                          rnd.uniform(8, 14), None, rnd)
        self.acts.reverse()  # neueste zuerst, wie Garmin

    def _add(self, aid, start, typ, a, b, km, bike, rnd):
        jitter = lambda p: (p[0] + rnd.uniform(-0.0006, 0.0006), p[1] + rnd.uniform(-0.0006, 0.0006))  # noqa: E731
        s, e = jitter(a), jitter(b)
        name = {"cycling": "Linz Radfahren", "gravel_cycling": "Linz Gravel", "running": "Linz Laufen"}[typ]
        self.names[aid] = name
        self.acts.append({
            "activityId": aid, "activityName": name, "activityType": {"typeKey": typ},
            "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
            "startLatitude": s[0], "startLongitude": s[1], "endLatitude": e[0], "endLongitude": e[1],
            "distance": km * 1000, "duration": km / 22 * 3600 if typ != "running" else km * 330,
            "elevationGain": km * 6, "averageSpeed": 22 / 3.6, "locationName": "Linz",
        })
        self.gear[aid] = [{"displayName": bike}] if bike else []

    def get_activities(self, start, limit):
        for a in self.acts:
            a["activityName"] = self.names[a["activityId"]]
        return self.acts[start:start + limit]

    def get_activity_gear(self, aid):
        return self.gear.get(aid, [])

    def get_gear_list(self, *_):
        return [{"displayName": "Stadtrad"}, {"displayName": "Canyon Grizl"}, {"displayName": "Liegerad M9"}]

    def get_activity_types(self):
        return [
            {"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
            {"typeId": 10, "typeKey": "road_biking", "parentTypeId": 2},
            {"typeId": 143, "typeKey": "gravel_cycling", "parentTypeId": 2},
            {"typeId": 21, "typeKey": "recumbent_cycling", "parentTypeId": 2},
            {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
        ]

    def set_activity_type(self, aid, type_id, type_key, parent_type_id):
        for a in self.acts:
            if str(a["activityId"]) == str(aid):
                a["activityType"] = {"typeKey": type_key}

    def set_activity_name(self, aid, name):
        self.names[aid] = name

    def set_activity_description(self, aid, text):
        pass

    def logout(self, *_):
        pass
