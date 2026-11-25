from datetime import datetime
from pathlib import Path

from app import store
from app.rules import Activity, first_matching_rule, looks_like_default_name, render, rule_matches, template_values

HOME = {"id": 1, "name": "Zuhause", "lat": 48.3064, "lon": 14.2861, "radius_m": 200}
WORK = {"id": 2, "name": "Arbeit", "lat": 48.2685, "lon": 14.3297, "radius_m": 200}
ZONES = [HOME, WORK]


def act(**kw):
    base = dict(id=1, name="Linz Radfahren", type_key="cycling", start=datetime(2026, 11, 23, 7, 15),
                start_lat=48.3065, start_lon=14.2862, end_lat=48.2686, end_lon=14.3296,
                distance_km=7.2, duration_s=1300, gear=["Stadtrad"])
    base.update(kw)
    return Activity(**base)


def test_commute_zones():
    rule = {"conditions": {"types": ["@rad"], "start_zone": "1", "end_zone": "2"}}
    assert rule_matches(rule, act(), ZONES)[0]
    back = act(start_lat=48.2686, start_lon=14.3296, end_lat=48.3065, end_lon=14.2862)
    assert not rule_matches(rule, back, ZONES)[0]
    rule["conditions"]["either_direction"] = True
    assert rule_matches(rule, back, ZONES)[0]


def test_conditions():
    assert not rule_matches({"conditions": {"types": ["@laufen"]}}, act(), ZONES)[0]
    assert rule_matches({"conditions": {"gear": "stadt"}}, act(), ZONES)[0]
    assert not rule_matches({"conditions": {"gear": "Canyon"}}, act(), ZONES)[0]
    assert rule_matches({"conditions": {"weekdays": [0]}}, act(), ZONES)[0]  # 23.11.2026 = Montag
    assert not rule_matches({"conditions": {"time_from": "08:00"}}, act(), ZONES)[0]
    assert rule_matches({"conditions": {"time_from": "22:00", "time_to": "08:00"}}, act(), ZONES)[0]
    assert not rule_matches({"conditions": {"min_km": 10}}, act(), ZONES)[0]
    assert rule_matches({"conditions": {"end_zone": "none"}}, act(end_lat=47.0, end_lon=13.0), ZONES)[0]
    assert not rule_matches({"conditions": {"types": ["@rad_outdoor"]}}, act(type_key="indoor_cycling"), ZONES)[0]
    assert rule_matches({"conditions": {"types": ["@rad"]}}, act(type_key="road_biking"), ZONES)[0]


def test_first_rule_wins_and_disabled_skipped():
    rules = [
        {"id": 1, "enabled": False, "conditions": {}},
        {"id": 2, "enabled": True, "conditions": {"gear": "Canyon"}},
        {"id": 3, "enabled": True, "conditions": {"types": ["@rad"]}},
    ]
    assert first_matching_rule(rules, act(), ZONES)["id"] == 3


def test_render():
    vals = template_values(act(), ZONES)
    out = render("🚲 Arbeit #{counter} {start_zone}→{end_zone} {distance_km} km {unknown}", vals, lambda n: 42)
    assert out == "🚲 Arbeit #42 Zuhause→Arbeit 7,2 km {unknown}"
    assert render("{counter:x}", vals, lambda n: 7 if n == "x" else 0) == "7"


def test_default_name():
    assert looks_like_default_name("Linz Radfahren")
    assert looks_like_default_name("Wien - Running")
    assert not looks_like_default_name("Tour mit Papa zum See")


def test_counters(tmp_path: Path):
    store.reset_for_tests(tmp_path / "t.db")
    store.save_counter({"name": "arbeit", "value": 0, "reset": "yearly"})
    assert store.increment_counter("arbeit", datetime(2026, 12, 30)) == 1
    assert store.increment_counter("arbeit", datetime(2026, 12, 31)) == 2
    assert store.peek_counter("arbeit", datetime(2027, 1, 2)) == 1
    assert store.increment_counter("arbeit", datetime(2027, 1, 2)) == 1


def test_gear_wildcards():
    from app.rules import gear_matches
    assert gear_matches("*M9*", ["Liegerad M9"])
    assert gear_matches("*m9*", ["Liegerad M9"])
    assert gear_matches("M9", ["Liegerad M9"])
    assert gear_matches("Canyon|*M9*", ["Liegerad M9"])
    assert not gear_matches("*X5*", ["Liegerad M9"])
    assert not gear_matches("*M9*", [])
    assert gear_matches("", [])


def test_counter_from_names():
    from app.rules import counter_from_names
    names = ["Arbeit #78 M9", "Von der Arbeit #78 M9", "Arbeit #79 M9", "Linz Radfahren"]
    assert counter_from_names("Arbeit #{n}", names) == 79
    assert counter_from_names("Arbeit #{n}", ["Linz Radfahren"]) == 0


def test_commute_flow_with_name_counter(tmp_path: Path, monkeypatch):
    """Morgens 'Arbeit #79 M9' (von Hand benannt) -> nachmittags 'Von der Arbeit #79 M9',
    nächster Morgen 'Arbeit #80 M9'. Dazu Sportart -> Liegerad über eine eigene Regel."""
    from app import garmin_client, processor
    store.reset_for_tests(tmp_path / "flow.db")
    monkeypatch.setattr(garmin_client, "DEMO", True)
    garmin_client.state.try_tokens()
    fake = garmin_client.state.client
    processor._gear_cache.clear()

    acts = sorted(fake.acts, key=lambda a: a["startTimeLocal"])
    commutes = [a for a in acts if a["activityType"]["typeKey"] == "cycling"]
    first_morning = commutes[0]
    fake.names[first_morning["activityId"]] = "Arbeit #79 M9"   # wie heute früh von Hand
    start = commutes[1]["startTimeLocal"][:10]
    store.record_history(activity_id=first_morning["activityId"], activity_date=start, activity_type="cycling",
                         old_name="Linz Radfahren", new_name="Arbeit #79 M9", rule_id=None, status="renamed")

    h = store.save_zone({"name": "Zuhause", "lat": fake.HOME[0], "lon": fake.HOME[1], "radius_m": 250})
    w = store.save_zone({"name": "Arbeit", "lat": fake.WORK[0], "lon": fake.WORK[1], "radius_m": 250})
    store.save_counter({"name": "arbeit", "value": 0, "reset": "never", "pattern": "Arbeit #{n}"})
    store.save_rule({"label": "Liegerad", "template": "", "set_type": "recumbent_cycling", "cont": False,
                     "conditions": {"gear": "*M9*"}})
    store.save_rule({"template": "Arbeit #{counter} M9", "counter": "arbeit", "counter_mode": "increment",
                     "conditions": {"types": ["@rad"], "start_zone": str(h["id"]), "end_zone": str(w["id"])}})
    store.save_rule({"template": "Von der Arbeit #{counter} M9", "counter": "arbeit", "counter_mode": "current",
                     "conditions": {"types": ["@rad"], "start_zone": str(w["id"]), "end_zone": str(h["id"])}})
    store.update_settings({"dry_run": False, "lookback": 200, "history_count": 200, "start_date": start})

    res = processor.run_once()
    assert res["ok"], res
    names = [fake.names[a["activityId"]] for a in commutes[:5]]
    assert names == ["Arbeit #79 M9", "Von der Arbeit #79 M9", "Arbeit #80 M9", "Von der Arbeit #80 M9",
                     "Arbeit #81 M9"]
    assert all(a["activityType"]["typeKey"] == "recumbent_cycling" for a in commutes[1:5])
    # zweiter Lauf ändert nichts mehr
    assert processor.run_once()["renamed"] == 0


def test_history_is_read_but_not_changed(tmp_path: Path, monkeypatch):
    """Nur die neuesten N werden geprüft (älteste davon zuerst); ältere werden nur gelesen,
    um den Zählerstand aus den Namen zu finden."""
    from app import garmin_client, processor
    store.reset_for_tests(tmp_path / "hist.db")
    monkeypatch.setattr(garmin_client, "DEMO", True)
    garmin_client.state.try_tokens()
    fake = garmin_client.state.client
    processor._gear_cache.clear()

    acts = sorted(fake.acts, key=lambda a: a["startTimeLocal"])     # älteste zuerst
    checked = acts[-6:]
    old = acts[:-6]
    old_morning = [a for a in old if a["activityType"]["typeKey"] == "cycling"][2]
    fake.names[old_morning["activityId"]] = "Arbeit #79 M9"
    before = {a["activityId"]: fake.names[a["activityId"]] for a in old}

    h = store.save_zone({"name": "Zuhause", "lat": fake.HOME[0], "lon": fake.HOME[1], "radius_m": 250})
    w = store.save_zone({"name": "Arbeit", "lat": fake.WORK[0], "lon": fake.WORK[1], "radius_m": 250})
    store.save_counter({"name": "arbeit", "value": 0, "reset": "never", "pattern": "Arbeit #{n}"})
    store.save_rule({"template": "Arbeit #{counter} M9", "counter": "arbeit",
                     "conditions": {"start_zone": str(h["id"]), "end_zone": str(w["id"])}})
    store.save_rule({"template": "Von der Arbeit #{counter} M9", "counter": "arbeit", "counter_mode": "current",
                     "conditions": {"start_zone": str(w["id"]), "end_zone": str(h["id"])}})
    store.update_settings({"dry_run": False, "lookback": 6, "history_count": 200, "start_date": "2000-01-01"})

    assert processor.run_once()["ok"]
    # ältere Aktivitäten unverändert
    assert all(fake.names[k] == v for k, v in before.items())
    # geprüfte: fortlaufend ab 80, in zeitlicher Reihenfolge
    n, expected = 79, []
    for a in checked:
        s = (round(a["startLatitude"], 2), round(a["endLatitude"], 2))
        if a["activityType"]["typeKey"] != "cycling":
            expected.append(fake.names[a["activityId"]])
        elif s[0] == round(fake.HOME[0], 2):
            n += 1
            expected.append(f"Arbeit #{n} M9")
        else:
            expected.append(f"Von der Arbeit #{n} M9")
    assert [fake.names[a["activityId"]] for a in checked] == expected
    assert any("#80" in e for e in expected)
