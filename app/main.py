"""Webserver + Zeitplan."""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import processor, store
from .garmin_client import DEMO, state as garmin
from .rules import TYPE_GROUPS, TYPE_LABELS_DE

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

STATIC = Path(__file__).parent / "static"
UI_USER = os.getenv("UI_USER", "admin")
UI_PASSWORD = os.getenv("UI_PASSWORD", "")

_wake = threading.Event()
_next_run: dict[str, Any] = {"at": None}


def _scheduler() -> None:
    garmin.try_tokens()
    time.sleep(5)
    while True:
        if garmin.status == "ok":
            processor.run_once()
        minutes = max(1, int(store.get_settings()["poll_minutes"]))
        _next_run["at"] = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        _wake.wait(minutes * 60)
        _wake.clear()


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.db()
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
    yield


app = FastAPI(title="Garmin Renamer", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if UI_PASSWORD:
        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, UI_USER) and secrets.compare_digest(pw, UI_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Garmin Renamer"'})
    return await call_next(request)


# ---------------------------------------------------------------- Status & Login
@app.get("/api/status")
def status():
    return {
        "garmin": {"status": garmin.status, "message": garmin.message, "user": garmin.user, "demo": DEMO},
        "last_run": processor.last_run,
        "next_run": _next_run["at"],
        "running": processor.run_lock.locked(),
        "settings": store.get_settings(),
    }


@app.post("/api/login")
def login(data: dict = Body(...)):
    if DEMO:
        garmin.try_tokens()
    else:
        garmin.start_login(data.get("email", ""), data.get("password", ""))
    if garmin.status == "ok":
        _wake.set()
    return status()["garmin"]


@app.post("/api/mfa")
def mfa(data: dict = Body(...)):
    garmin.finish_mfa(data.get("code", ""))
    if garmin.status == "ok":
        _wake.set()
    return status()["garmin"]


@app.post("/api/logout")
def logout():
    garmin.logout()
    return status()["garmin"]


@app.post("/api/run")
def run_now():
    if garmin.status != "ok":
        raise HTTPException(400, "Nicht bei Garmin angemeldet.")
    return processor.run_once()


@app.get("/api/preview")
def preview():
    if garmin.status != "ok":
        raise HTTPException(400, "Nicht bei Garmin angemeldet.")
    try:
        return processor.preview()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Garmin-Abfrage fehlgeschlagen: {e}") from e


@app.get("/api/activities")
def activities():
    return processor.recent_cache


@app.get("/api/gear")
def gear():
    names: list[str] = []
    if garmin.status == "ok":
        try:
            names = garmin.gear_list()
        except Exception:  # noqa: BLE001
            names = []
    for a in processor.recent_cache:
        for g in a.get("gear", []):
            if g not in names:
                names.append(g)
    return names


@app.get("/api/activity-types")
def activity_types():
    if garmin.status != "ok":
        return []
    try:
        types = garmin.activity_types()
    except Exception as e:  # noqa: BLE001
        log.warning("Sportarten nicht abrufbar: %s", e)
        return []
    return sorted(
        ({"key": t["typeKey"], "label": TYPE_LABELS_DE.get(t["typeKey"], t["typeKey"].replace("_", " "))}
         for t in types),
        key=lambda t: t["label"].lower(),
    )


@app.get("/api/meta")
def meta():
    return {"type_groups": list(TYPE_GROUPS) + ["@indoor"], "type_labels": TYPE_LABELS_DE}


# ---------------------------------------------------------------- Einstellungen
@app.get("/api/settings")
def get_settings():
    return store.get_settings()


@app.put("/api/settings")
def put_settings(data: dict = Body(...)):
    s = store.update_settings(data)
    _wake.set()  # neues Intervall sofort übernehmen
    return s


# ---------------------------------------------------------------- Zonen
@app.get("/api/zones")
def zones():
    return store.list_zones()


@app.post("/api/zones")
def save_zone(z: dict = Body(...)):
    if not z.get("name"):
        raise HTTPException(400, "Name fehlt.")
    return store.save_zone(z)


@app.delete("/api/zones/{zid}")
def delete_zone(zid: int):
    store.delete_zone(zid)
    return {"ok": True}


# ---------------------------------------------------------------- Regeln
@app.get("/api/rules")
def rules():
    return store.list_rules()


@app.post("/api/rules")
def save_rule(r: dict = Body(...)):
    if not (r.get("template") or "").strip() and not (r.get("set_type") or "").strip():
        raise HTTPException(400, "Bitte einen neuen Namen oder eine neue Sportart angeben.")
    r["template"] = r.get("template") or ""
    return store.save_rule(r)


@app.post("/api/rules/order")
def order_rules(ids: list[int] = Body(...)):
    store.reorder_rules(ids)
    return store.list_rules()


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int):
    store.delete_rule(rid)
    return {"ok": True}


# ---------------------------------------------------------------- Zähler
@app.get("/api/counters")
def counters():
    return store.list_counters()


@app.post("/api/counters")
def save_counter(c: dict = Body(...)):
    if not (c.get("name") or "").strip():
        raise HTTPException(400, "Name fehlt.")
    store.save_counter(c)
    return store.list_counters()


@app.delete("/api/counters/{name}")
def delete_counter(name: str):
    store.delete_counter(name)
    return {"ok": True}


# ---------------------------------------------------------------- Verlauf
@app.get("/api/history")
def history(limit: int = 100):
    return store.list_history(limit)


@app.post("/api/history/{activity_id}/reapply")
def reapply(activity_id: int):
    store.forget_history(activity_id)
    return processor.run_once()


# ---------------------------------------------------------------- Oberfläche
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
