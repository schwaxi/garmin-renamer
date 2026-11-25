"use strict";

// ------------------------------------------------------------ Hilfsfunktionen
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body !== undefined ? "POST" : "GET"),
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : {},
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

const fmtDate = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("de-AT", { weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
};

const STATUS_LABEL = { renamed: "umbenannt", dry_run: "Probemodus", no_match: "keine Regel", skipped: "übersprungen", error: "Fehler" };
const WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const TYPE_CHOICES = [
  ["@rad", "Alle Radtypen"], ["@rad_outdoor", "Rad draußen"], ["@indoor", "Indoor"],
  ["cycling", "Radfahren"], ["road_biking", "Rennrad"], ["gravel_cycling", "Gravel"], ["mountain_biking", "MTB"],
  ["e_bike_fitness", "E-Bike"], ["recumbent_cycling", "Liegerad"], ["@laufen", "Laufen (alle)"], ["running", "Laufen"], ["trail_running", "Trail"],
  ["@gehen", "Gehen/Wandern"], ["@schwimmen", "Schwimmen"], ["@winter", "Wintersport"],
];
const PLACEHOLDERS = [
  ["{counter}", "Zähler"], ["{start_zone}", "Startzone"], ["{end_zone}", "Zielzone"], ["{gear}", "Fahrrad"],
  ["{distance_km}", "km"], ["{duration}", "Dauer"], ["{elevation}", "Hm"], ["{speed}", "km/h"],
  ["{type}", "Sportart"], ["{date}", "Datum"], ["{time}", "Uhrzeit"], ["{weekday}", "Wochentag"],
  ["{daytime}", "Morgen/Abend…"], ["{year}", "Jahr"], ["{location}", "Ort"], ["{original}", "Garmin-Name"],
];

const S = { zones: [], rules: [], counters: [], status: null };

// ------------------------------------------------------------ Tabs
$$("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    location.hash = b.dataset.tab;
    if (b.dataset.tab === "zones") initMap();
  })
);

// ------------------------------------------------------------ Status / Login
async function loadStatus() {
  const st = await api("/api/status");
  S.status = st;
  $("#dry-banner").classList.toggle("hidden", !st.settings.dry_run);
  $("#st-last").textContent = fmtDate(st.last_run.at);
  $("#st-next").textContent = st.garmin.status === "ok" ? fmtDate(st.next_run) : "– (nicht angemeldet)";
  $("#st-result").innerHTML = st.last_run.error
    ? `<span class="badge error">${esc(st.last_run.error)}</span>`
    : st.last_run.at ? `${st.last_run.checked} geprüft, ${st.last_run.renamed} umbenannt` : "–";
  renderGarmin(st.garmin);
  return st;
}

function renderGarmin(g) {
  const body = $("#garmin-body");
  if (g.status === "ok") {
    body.innerHTML = `<p><span class="badge ok">verbunden</span> ${esc(g.user)}</p>
      <div class="row"><button id="btn-logout">Abmelden</button></div>`;
    $("#btn-logout").onclick = async () => { await api("/api/logout", { body: {} }); loadStatus(); };
  } else if (g.status === "mfa_required") {
    body.innerHTML = `<p class="small">${esc(g.message)}</p>
      <form id="mfa-form" class="row"><input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6-stelliger Code" required style="max-width:200px">
      <button class="primary">Bestätigen</button></form>`;
    $("#mfa-form").onsubmit = async (e) => {
      e.preventDefault();
      const r = await api("/api/mfa", { body: { code: e.target.code.value } });
      toast(r.status === "ok" ? "Angemeldet" : r.message);
      loadStatus();
    };
  } else {
    body.innerHTML = `${g.message ? `<p class="small muted">${esc(g.message)}</p>` : ""}
      <form id="login-form">
        <label>E-Mail <input name="email" type="email" autocomplete="username" required></label>
        <label>Passwort <input name="password" type="password" autocomplete="current-password" required></label>
        <button class="primary">Anmelden</button>
        <p class="small muted">Das Passwort wird nicht gespeichert, nur die Anmelde-Tokens von Garmin.</p>
      </form>`;
    $("#login-form").onsubmit = async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button");
      btn.disabled = true; btn.textContent = "Melde an …";
      try {
        const r = await api("/api/login", { body: { email: e.target.email.value, password: e.target.password.value } });
        if (r.status === "error") toast(r.message);
      } catch (err) { toast(err.message); }
      loadStatus();
    };
  }
}

$("#btn-run").onclick = async () => {
  const btn = $("#btn-run");
  btn.disabled = true;
  try {
    const r = await api("/api/run", { body: {} });
    toast(r.ok ? `${r.checked} geprüft, ${r.renamed} umbenannt${r.dry_run ? " (Probemodus)" : ""}` : r.message);
  } catch (e) { toast(e.message); }
  btn.disabled = false;
  loadStatus(); loadHistory(); loadCounters();
};
$("#btn-preview").onclick = loadPreview;
$("#btn-arm").onclick = async () => {
  if (!confirm("Probemodus beenden? Ab dann werden passende Aktivitäten bei Garmin wirklich umbenannt.")) return;
  await api("/api/settings", { method: "PUT", body: { dry_run: false } });
  toast("Scharf geschaltet"); loadStatus(); loadSettings();
};

// ------------------------------------------------------------ Vorschau & Verlauf
async function loadPreview() {
  const box = $("#preview");
  box.innerHTML = `<p class="muted">Lade von Garmin …</p>`;
  try {
    const rows = await api("/api/preview");
    if (!rows.length) { box.innerHTML = `<p class="muted">Keine Aktivitäten gefunden.</p>`; return; }
    box.innerHTML = `<table><thead><tr><th>Datum</th><th>Sportart</th><th>Jetziger Name</th><th></th><th>Neuer Name</th><th>Regel</th><th>Status</th></tr></thead><tbody>
      ${rows.map((r) => `<tr>
        <td>${fmtDate(r.start)}</td><td>${esc(r.type_key)}<div class="small muted">${r.distance_km} km${r.gear.length ? " · " + esc(r.gear.join(", ")) : ""}</div></td>
        <td>${esc(r.name)}</td><td class="arrow">→</td>
        <td class="new-name">${r.new_name ? esc(r.new_name) : '<span class="muted">–</span>'}${r.set_type ? `<div class="small">Sportart → ${esc(typeLabel(r.set_type))}</div>` : ""}</td>
        <td class="small">${esc(r.rule_label) || '<span class="muted">keine passt</span>'}</td>
        <td>${r.history ? `<span class="badge ${r.history}">${STATUS_LABEL[r.history] || r.history}</span>` : r.before_start ? '<span class="badge" title="Vor dem Startdatum - wird nicht angefasst">zu alt</span>' : '<span class="badge">neu</span>'}</td>
      </tr>`).join("")}</tbody></table>`;
    if (mapReady) drawActivities();
  } catch (e) { box.innerHTML = `<p class="badge error">${esc(e.message)}</p>`; }
}

async function loadHistory() {
  const rows = await api("/api/history");
  const box = $("#history");
  if (!rows.length) { box.innerHTML = `<p class="muted">Noch nichts verarbeitet.</p>`; return; }
  box.innerHTML = `<table><thead><tr><th>Datum</th><th>Vorher</th><th></th><th>Nachher</th><th>Status</th><th></th></tr></thead><tbody>
    ${rows.map((h) => `<tr>
      <td>${fmtDate(h.activity_date)}</td><td>${esc(h.old_name)}</td><td class="arrow">→</td>
      <td class="new-name">${esc(h.new_name || "–")}${h.message && h.status !== "error" ? `<div class="small muted">${esc(h.message)}</div>` : ""}</td>
      <td><span class="badge ${h.status}" title="${esc(h.message)}">${STATUS_LABEL[h.status] || h.status}</span></td>
      <td><button class="icon small" data-reapply="${h.activity_id}" title="Vergessen und Regeln erneut anwenden">↻</button></td>
    </tr>`).join("")}</tbody></table>`;
  $$("[data-reapply]", box).forEach((b) => (b.onclick = async () => {
    if (!confirm("Regeln für diese Aktivität erneut anwenden? (Ein verwendeter Zähler steigt dabei wieder um 1.)")) return;
    const r = await api(`/api/history/${b.dataset.reapply}/reapply`, { body: {} });
    toast(r.ok ? "Erneut angewendet" : r.message); loadHistory();
  }));
}

// ------------------------------------------------------------ Karte & Zonen
let map, mapReady = false, zoneLayer, actLayer, draft = null;

function initMap() {
  if (mapReady) { setTimeout(() => map.invalidateSize(), 50); return; }
  map = L.map("map").setView([48.2, 14.3], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap-Mitwirkende",
  }).addTo(map);
  zoneLayer = L.layerGroup().addTo(map);
  actLayer = L.layerGroup().addTo(map);
  map.on("click", (e) => openZoneForm({ name: "", lat: e.latlng.lat, lon: e.latlng.lng, radius_m: 200, color: "#2563eb" }));
  mapReady = true;
  drawZones();
  drawActivities(true);
  setTimeout(() => map.invalidateSize(), 50);
}

async function loadZones() {
  S.zones = await api("/api/zones");
  $("#zone-list").innerHTML = S.zones.map((z) => `<li data-zone="${z.id}" style="cursor:pointer">
    <span class="dot" style="background:${esc(z.color)}"></span>
    <div class="grow"><div>${esc(z.name)}</div><div class="sub">${Math.round(z.radius_m)} m Radius</div></div></li>`).join("")
    || `<li class="muted small">Noch keine Zonen.</li>`;
  $$("[data-zone]").forEach((li) => (li.onclick = () => {
    const z = S.zones.find((x) => x.id == li.dataset.zone);
    openZoneForm(z); map.setView([z.lat, z.lon], 15);
  }));
  if (mapReady) drawZones();
}

function drawZones() {
  zoneLayer.clearLayers();
  for (const z of S.zones) {
    if (draft && draft.id === z.id) continue;
    L.circle([z.lat, z.lon], { radius: z.radius_m, color: z.color, weight: 2, fillOpacity: 0.15 })
      .bindTooltip(z.name, { permanent: true, direction: "center", className: "" })
      .on("click", (e) => { L.DomEvent.stopPropagation(e); openZoneForm(z); })
      .addTo(zoneLayer);
  }
  if (draft) {
    const c = L.circle([draft.lat, draft.lon], { radius: draft.radius_m, color: draft.color, weight: 3, dashArray: "6 4", fillOpacity: 0.2 }).addTo(zoneLayer);
    const m = L.marker([draft.lat, draft.lon], { draggable: true }).addTo(zoneLayer);
    m.on("drag", (e) => { const p = e.target.getLatLng(); draft.lat = p.lat; draft.lon = p.lng; c.setLatLng(p); showCoords(); });
  }
  if (S.zones.length && !draft && !drawZones.fitted) {
    map.fitBounds(L.featureGroup(zoneLayer.getLayers()).getBounds().pad(0.4));
    drawZones.fitted = true;
  }
}

async function drawActivities(fit) {
  let acts = [];
  try { acts = await api("/api/activities"); } catch { return; }
  actLayer.clearLayers();
  const pts = [];
  for (const a of acts) {
    if (a.start_lat != null) {
      L.circleMarker([a.start_lat, a.start_lon], { radius: 4, color: "#16a34a", weight: 1, fillOpacity: 0.8 }).bindTooltip(`Start: ${a.name}<br>${fmtDate(a.start)}`).addTo(actLayer);
      pts.push([a.start_lat, a.start_lon]);
    }
    if (a.end_lat != null) {
      L.circleMarker([a.end_lat, a.end_lon], { radius: 4, color: "#dc2626", weight: 1, fillOpacity: 0.8 }).bindTooltip(`Ziel: ${a.name}`).addTo(actLayer);
      pts.push([a.end_lat, a.end_lon]);
    }
  }
  if (fit && pts.length && !S.zones.length) map.fitBounds(L.latLngBounds(pts).pad(0.2));
}

function showCoords() {
  $("#zone-coords").textContent = draft ? `${draft.lat.toFixed(5)}, ${draft.lon.toFixed(5)} – Marker verschiebbar` : "";
}

function openZoneForm(z) {
  draft = { ...z };
  const f = $("#zone-form");
  f.classList.remove("hidden");
  f.elements.id.value = z.id || "";
  f.elements.name.value = z.name || "";
  f.radius_m.value = z.radius_m;
  f.color.value = z.color || "#2563eb";
  $("#radius-label").textContent = Math.round(z.radius_m);
  $("#zone-delete").classList.toggle("hidden", !z.id);
  showCoords();
  drawZones();
  f.elements.name.focus();
}

function closeZoneForm() { draft = null; $("#zone-form").classList.add("hidden"); drawZones(); }

$("#zone-form").radius_m.oninput = (e) => { draft.radius_m = +e.target.value; $("#radius-label").textContent = e.target.value; drawZones(); };
$("#zone-form").color.oninput = (e) => { draft.color = e.target.value; drawZones(); };
$("#zone-cancel").onclick = closeZoneForm;
$("#zone-delete").onclick = async () => {
  const used = S.rules.filter((r) => [r.conditions.start_zone, r.conditions.end_zone].map(String).includes(String(draft.id)));
  if (!confirm(used.length ? `Zone wird in ${used.length} Regel(n) verwendet. Trotzdem löschen?` : "Zone löschen?")) return;
  await api(`/api/zones/${draft.id}`, { method: "DELETE" });
  closeZoneForm(); loadZones();
};
$("#zone-form").onsubmit = async (e) => {
  e.preventDefault();
  draft.name = e.target.elements.name.value.trim();
  await api("/api/zones", { body: { ...draft, id: draft.id || null } });
  toast("Zone gespeichert");
  closeZoneForm(); loadZones();
};
$("#map-search-btn").onclick = async () => {
  const q = $("#map-search").value.trim();
  if (!q) return;
  const r = await fetch("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + encodeURIComponent(q)).then((x) => x.json()).catch(() => []);
  if (r[0]) map.setView([+r[0].lat, +r[0].lon], 15); else toast("Nichts gefunden");
};
$("#map-search").onkeydown = (e) => { if (e.key === "Enter") $("#map-search-btn").click(); };

// ------------------------------------------------------------ Regeln
let ACTIVITY_TYPES = [];
const FALLBACK_TYPES = [
  ["recumbent_cycling", "Liegerad"], ["cycling", "Radfahren"], ["road_biking", "Rennrad"], ["gravel_cycling", "Gravel"],
  ["mountain_biking", "Mountainbike"], ["e_bike_fitness", "E-Bike"], ["e_bike_mountain", "E-MTB"],
  ["hand_cycling", "Handbike"], ["indoor_cycling", "Indoor-Radfahren"], ["running", "Laufen"], ["walking", "Gehen"], ["hiking", "Wandern"],
].map(([key, label]) => ({ key, label }));
const typeLabel = (k) => (ACTIVITY_TYPES.find((t) => t.key === k) || FALLBACK_TYPES.find((t) => t.key === k))?.label || k;
async function loadActivityTypes() {
  try { ACTIVITY_TYPES = await api("/api/activity-types"); } catch { ACTIVITY_TYPES = []; }
  if (!ACTIVITY_TYPES.length) ACTIVITY_TYPES = FALLBACK_TYPES;
}

function zoneName(id) {
  if (!id) return null;
  if (id === "none") return "keiner Zone";
  return S.zones.find((z) => String(z.id) === String(id))?.name || "?";
}

function describe(r) {
  const c = r.conditions || {};
  const parts = [];
  if (c.types?.length) parts.push(c.types.map((t) => TYPE_CHOICES.find((x) => x[0] === t)?.[1] || t).join("/"));
  if (c.start_zone || c.end_zone) parts.push(`${zoneName(c.start_zone) || "überall"} → ${zoneName(c.end_zone) || "überall"}${c.either_direction ? " (beide Richtungen)" : ""}`);
  if (c.gear) parts.push(`Rad „${c.gear}“`);
  if (c.weekdays?.length) parts.push(c.weekdays.map((d) => WEEKDAYS[d]).join(","));
  if (c.time_from || c.time_to) parts.push(`${c.time_from || "…"}–${c.time_to || "…"} Uhr`);
  if (c.min_km || c.max_km) parts.push(`${c.min_km || 0}–${c.max_km || "∞"} km`);
  if (c.name_regex) parts.push(`Name ~ /${c.name_regex}/`);
  if (r.counter) parts.push(`Zähler „${r.counter}“ ${r.counter_mode === "current" ? "(gleiche Nr.)" : "(+1)"}`);
  if (r.set_type) parts.push(`Sportart → ${typeLabel(r.set_type)}`);
  if (r.cont) parts.push("danach weiter prüfen");
  return parts.join(" · ") || "passt auf alles";
}

async function loadRules() {
  S.rules = await api("/api/rules");
  const ul = $("#rule-list");
  ul.innerHTML = S.rules.map((r, i) => `<li class="${r.enabled ? "" : "off"}">
    <span class="num">${i + 1}</span>
    <div class="grow"><div><b>${esc(r.label || r.template || "Sportart → " + typeLabel(r.set_type))}</b>${r.label ? ` <span class="muted">→ ${esc(r.template)}</span>` : ""}</div>
    <div class="sub">${esc(describe(r))}</div></div>
    <button class="icon" data-up="${i}" ${i === 0 ? "disabled" : ""} title="Nach oben">↑</button>
    <button class="icon" data-down="${i}" ${i === S.rules.length - 1 ? "disabled" : ""} title="Nach unten">↓</button>
    <button class="icon" data-edit="${r.id}">Bearbeiten</button>
    <button class="icon danger" data-del="${r.id}">✕</button></li>`).join("")
    || `<li class="muted">Noch keine Regeln. Lege zuerst Zonen an, dann eine Regel.</li>`;
  const move = async (i, d) => {
    const ids = S.rules.map((r) => r.id);
    [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
    await api("/api/rules/order", { body: ids }); loadRules();
  };
  $$("[data-up]", ul).forEach((b) => (b.onclick = () => move(+b.dataset.up, -1)));
  $$("[data-down]", ul).forEach((b) => (b.onclick = () => move(+b.dataset.down, 1)));
  $$("[data-edit]", ul).forEach((b) => (b.onclick = () => openRule(S.rules.find((r) => r.id == b.dataset.edit))));
  $$("[data-del]", ul).forEach((b) => (b.onclick = async () => {
    if (!confirm("Regel löschen?")) return;
    await api(`/api/rules/${b.dataset.del}`, { method: "DELETE" }); loadRules();
  }));
}

function chipGroup(el, choices, selected) {
  el.innerHTML = choices.map(([v, l]) => `<span class="chip ${selected.includes(v) ? "on" : ""}" data-v="${esc(v)}">${esc(l)}</span>`).join("");
  $$(".chip", el).forEach((c) => (c.onclick = () => c.classList.toggle("on")));
}
const chipValues = (el) => $$(".chip.on", el).map((c) => c.dataset.v);

async function openRule(r) {
  r = r || { enabled: true, template: "", conditions: {} };
  const c = r.conditions || {};
  const f = $("#rule-form");
  $("#rule-dialog-title").textContent = r.id ? "Regel bearbeiten" : "Neue Regel";
  f.elements.id.value = r.id || "";
  f.label.value = r.label || "";
  f.template.value = r.template || "";
  f.description_template.value = r.description_template || "";
  f.counter.value = r.counter || "";
  f.counter_mode.value = r.counter_mode || "increment";
  f.cont.checked = !!r.cont;
  if (ACTIVITY_TYPES === FALLBACK_TYPES || !ACTIVITY_TYPES.length) await loadActivityTypes();
  f.set_type.innerHTML = `<option value="">– nicht ändern –</option>` +
    ACTIVITY_TYPES.map((t) => `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join("");
  if (r.set_type && !ACTIVITY_TYPES.some((t) => t.key === r.set_type)) f.set_type.insertAdjacentHTML("beforeend", `<option value="${esc(r.set_type)}">${esc(r.set_type)}</option>`);
  f.set_type.value = r.set_type || "";
  f.enabled.checked = r.enabled !== false;
  const zoneOpts = `<option value="">egal</option><option value="none">in keiner Zone</option>` + S.zones.map((z) => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  f.start_zone.innerHTML = zoneOpts; f.end_zone.innerHTML = zoneOpts;
  f.start_zone.value = c.start_zone ?? ""; f.end_zone.value = c.end_zone ?? "";
  f.either_direction.checked = !!c.either_direction;
  f.gear.value = c.gear || "";
  f.time_from.value = c.time_from || ""; f.time_to.value = c.time_to || "";
  f.min_km.value = c.min_km ?? ""; f.max_km.value = c.max_km ?? "";
  f.name_regex.value = c.name_regex || "";
  chipGroup($("#type-chips"), TYPE_CHOICES, c.types || []);
  chipGroup($("#weekday-chips"), WEEKDAYS.map((d, i) => [String(i), d]), (c.weekdays || []).map(String));
  $("#counter-options").innerHTML = S.counters.map((x) => `<option value="${esc(x.name)}">`).join("");
  api("/api/gear").then((g) => ($("#gear-options").innerHTML = g.map((x) => `<option value="${esc(x)}">`).join(""))).catch(() => {});
  $("#rule-dialog").showModal();
}

$("#placeholder-chips").innerHTML = PLACEHOLDERS.map(([p, l]) => `<span class="chip ph" data-p="${p}" title="${esc(l)}">${p}</span>`).join("");
$$("#placeholder-chips .chip").forEach((c) => (c.onclick = () => {
  const inp = $("#rule-form").template;
  const pos = inp.selectionStart ?? inp.value.length;
  inp.value = inp.value.slice(0, pos) + c.dataset.p + inp.value.slice(inp.selectionEnd ?? pos);
  inp.focus(); inp.selectionStart = inp.selectionEnd = pos + c.dataset.p.length;
}));

$("#btn-new-rule").onclick = () => openRule(null);
$("#rule-cancel").onclick = () => $("#rule-dialog").close();
$("#rule-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const num = (v) => (v === "" ? "" : +v);
  const rule = {
    id: f.elements.id.value ? +f.elements.id.value : null,
    label: f.label.value.trim(),
    template: f.template.value.trim(),
    description_template: f.description_template.value.trim(),
    counter: f.counter.value.trim(),
    counter_mode: f.counter_mode.value,
    set_type: f.set_type.value,
    cont: f.cont.checked,
    enabled: f.enabled.checked,
    conditions: {
      types: chipValues($("#type-chips")),
      start_zone: f.start_zone.value, end_zone: f.end_zone.value,
      either_direction: f.either_direction.checked,
      gear: f.gear.value.trim(),
      weekdays: chipValues($("#weekday-chips")).map(Number),
      time_from: f.time_from.value, time_to: f.time_to.value,
      min_km: num(f.min_km.value), max_km: num(f.max_km.value),
      name_regex: f.name_regex.value.trim(),
    },
  };
  if (!rule.template && !rule.set_type) { toast("Bitte einen neuen Namen oder eine neue Sportart angeben."); return; }
  if (rule.template.includes("{counter}") && !rule.counter) {
    if (!confirm("Die Vorlage enthält {counter}, aber kein Zähler ist eingetragen. Trotzdem speichern?")) return;
  }
  try {
    await api("/api/rules", { body: rule });
    $("#rule-dialog").close();
    toast("Regel gespeichert");
    loadRules(); loadCounters();
  } catch (err) { toast(err.message); }
};

// ------------------------------------------------------------ Zähler
async function loadCounters() {
  S.counters = await api("/api/counters");
  const box = $("#counter-table");
  if (!S.counters.length) { box.innerHTML = `<p class="muted">Noch keine Zähler. Sie entstehen auch automatisch, wenn du in einer Regel einen Namen einträgst.</p>`; return; }
  box.innerHTML = `<table><thead><tr><th>Name</th><th>Stand</th><th>Zurücksetzen</th><th>Stand aus Namen lesen</th><th>Zeitraum</th><th></th></tr></thead><tbody>
    ${S.counters.map((c) => `<tr data-name="${esc(c.name)}">
      <td><code>${esc(c.name)}</code></td>
      <td><input type="number" value="${c.value}" style="max-width:100px" data-f="value"></td>
      <td><select data-f="reset">${["never", "yearly", "monthly", "weekly"].map((v) => `<option value="${v}" ${c.reset === v ? "selected" : ""}>${{ never: "nie", yearly: "jährlich", monthly: "monatlich", weekly: "wöchentlich" }[v]}</option>`).join("")}</select></td>
      <td><input data-f="pattern" value="${esc(c.pattern || "")}" placeholder="z. B. Arbeit #{n}" style="min-width:180px"></td>
      <td class="small muted">${esc(c.period || "–")}</td>
      <td class="row" style="margin:0"><button class="icon" data-save>Speichern</button><button class="icon danger" data-del>✕</button></td>
    </tr>`).join("")}</tbody></table>`;
  $$("tr[data-name]", box).forEach((tr) => {
    $("[data-save]", tr).onclick = async () => {
      await api("/api/counters", { body: { name: tr.dataset.name, value: +$("[data-f=value]", tr).value, reset: $("[data-f=reset]", tr).value, pattern: $("[data-f=pattern]", tr).value } });
      toast("Zähler gespeichert"); loadCounters();
    };
    $("[data-del]", tr).onclick = async () => {
      if (!confirm(`Zähler „${tr.dataset.name}“ löschen?`)) return;
      await api(`/api/counters/${encodeURIComponent(tr.dataset.name)}`, { method: "DELETE" }); loadCounters();
    };
  });
}
$("#counter-form").onsubmit = async (e) => {
  e.preventDefault();
  await api("/api/counters", { body: { name: e.target.elements.name.value, reset: e.target.elements.reset.value, pattern: e.target.elements.pattern.value, value: 0 } });
  HTMLFormElement.prototype.reset.call(e.target); loadCounters();
};

// ------------------------------------------------------------ Einstellungen
async function loadSettings() {
  const s = await api("/api/settings");
  const f = $("#settings-form");
  f.dry_run.checked = s.dry_run; f.skip_custom_names.checked = s.skip_custom_names;
  f.poll_minutes.value = s.poll_minutes; f.lookback.value = s.lookback; f.history_count.value = s.history_count; f.start_date.value = s.start_date || "";
}
$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  await api("/api/settings", { method: "PUT", body: {
    dry_run: f.dry_run.checked, skip_custom_names: f.skip_custom_names.checked,
    poll_minutes: +f.poll_minutes.value, lookback: +f.lookback.value, history_count: Math.max(+f.history_count.value, +f.lookback.value), start_date: f.start_date.value,
  } });
  toast("Gespeichert"); loadStatus();
};

// ------------------------------------------------------------ Start
(async () => {
  await Promise.all([loadStatus(), loadZones(), loadRules(), loadCounters(), loadSettings(), loadHistory()]);
  const openHashTab = () => {
    const tab = location.hash.slice(1);
    const btn = tab && $(`#tabs button[data-tab="${tab}"]`);
    if (btn && !btn.classList.contains("active")) btn.click();
  };
  openHashTab();
  window.addEventListener("hashchange", openHashTab);
  setInterval(() => { loadStatus(); if ($("#tab-overview").classList.contains("active")) loadHistory(); }, 30000);
})();
