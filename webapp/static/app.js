"use strict";
// Worker Safety Monitor: browser app. Plain JavaScript, no build step, no external files,
// so it also works on a phone hotspot with no internet connection.
// Data: GET /api/state once, then live updates from the Server-Sent Events stream /api/stream.

const EVENT_TEXT = {
  ALERT_FALL: "Fall", ALERT_MAN_DOWN: "Man down", ALERT_VITALS: "Abnormal vitals",
  DEVICE_FAULT: "Motion sensor fault", ALERT_TEST: "Test alert",
  ALERT_CLEARED: "Alert cleared on the device", DEVICE_OK: "Motion sensor recovered",
};
// Device defaults (firmware Kconfig / safety_core): shown as reference lines on the charts.
const LIMITS = { hrLow: 45, hrHigh: 140, spo2Low: 90 };
const SPARK_SECONDS = 600;

const S = {
  workers: {}, alerts: new Map(), offset: 0, offlineAfter: 90, live: false,
  sound: false, audio: null, me: store("wsm.name") || "", view: null, notified: new Set(),
  loaded: false, dirty: new Set(), online: new Map(),
};

function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem(k);
    localStorage.setItem(k, v);
  } catch (e) { /* private mode: settings just are not remembered */ }
  return null;
}

// ------------------------------------------------------------------ DOM helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false || kid === "") continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
// Update `a` in place to look like `b`, keeping the nodes that stay (so a click that is in
// progress, focus and hover survive the frequent live updates). Returns the node in the DOM.
function morph(a, b) {
  if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) { a.replaceWith(b); return b; }
  if (a.nodeType === Node.TEXT_NODE) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return a; }
  if (a.nodeType !== Node.ELEMENT_NODE) { a.replaceWith(b); return b; }
  for (const at of [...a.attributes]) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
  for (const at of b.attributes) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
  const ak = [...a.childNodes], bk = [...b.childNodes];
  bk.forEach((n, i) => { if (i < ak.length) morph(ak[i], n); else a.appendChild(n); });
  for (let i = bk.length; i < ak.length; i++) ak[i].remove();
  return a;
}
// Keyed list: one child per item, matched by key, updated with morph and kept in order.
function keyed(container, items, keyOf, render) {
  const old = container._keys || new Map(), next = new Map();
  let cursor = container.firstChild;
  for (const it of items) {
    const k = keyOf(it), prev = old.get(k);
    const el = prev ? morph(prev, render(it)) : render(it);
    next.set(k, el);
    if (el === cursor) cursor = cursor.nextSibling;
    else container.insertBefore(el, cursor);
  }
  for (const [k, el] of old) if (!next.has(k) && el.parentNode === container) el.remove();
  container._keys = next;
}
const SVG = "http://www.w3.org/2000/svg";
function sv(tag, attrs, text) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

// ------------------------------------------------------------------ formatting
const now = () => Date.now() / 1000 + S.offset;            // server clock
const clock = (t) => t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
const dateTime = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
function ago(sec) {
  sec = Math.max(0, sec);
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
  return `${Math.floor(sec / 86400)} d`;
}
function mmss(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const fmt = (v, d = 0) => (v == null || Number.isNaN(v)) ? "–" : Number(v).toFixed(d);
const eventText = (e) => EVENT_TEXT[e] || e || "Alert";
const workerName = (id) => (S.workers[id] && S.workers[id].name) || id;

// ------------------------------------------------------------------ state helpers
const online = (w) => now() - (w.last_seen || 0) < S.offlineAfter;
const isOpen = (a) => a.needs_ack && !a.acked_at && !a.cleared_at;
function openAlerts() {
  return [...S.alerts.values()].filter(isOpen).sort((a, b) => a.received - b.received);
}
function status(w) {
  if (!online(w)) return { cls: "offline", icon: "○", text: "Offline" };
  const open = openAlerts().filter((a) => a.worker === w.id);
  if (open.length) return { cls: "alert", icon: "▲", text: eventText(open[open.length - 1].event) };
  if (w.state === "ALERT") return { cls: "helping", icon: "✚", text: w.acked ? "Help on the way" : "Alert active" };
  if (w.state === "FALL_PENDING") return { cls: "pre", icon: "!", text: "Fall pre-alarm" };
  if (w.state === "MAN_DOWN_PENDING") return { cls: "pre", icon: "!", text: "Man-down pre-alarm" };
  if (w.sensor_fault) return { cls: "fault", icon: "!", text: "Sensor fault" };
  return { cls: "ok", icon: "✓", text: "OK" };
}
const RANK = { alert: 0, helping: 1, pre: 2, fault: 3, ok: 4, offline: 5 };
function sortedWorkers() {
  return Object.values(S.workers).sort((a, b) =>
    (RANK[status(a).cls] - RANK[status(b).cls]) || String(a.name).localeCompare(String(b.name)));
}
const pill = (st) => h("span", { class: `pill ${st.cls}` }, h("span", { "aria-hidden": "true" }, st.icon), st.text);

// ------------------------------------------------------------------ server
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (!r.ok && r.status !== 404) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function loadState() {
  const st = await api("/api/state");
  S.offset = st.server_time - Date.now() / 1000;
  S.offlineAfter = st.offline_after;
  S.workers = st.workers;
  for (const a of st.alerts) S.alerts.set(a.id, a);
  if (!S.loaded) for (const a of S.alerts.values()) S.notified.add(a.id);   // no notification storm on load
  S.loaded = true;
  changed("all");
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { setLive(true); loadState().catch((e) => console.error(e)); };
  es.onmessage = (ev) => apply(JSON.parse(ev.data));
  es.onerror = () => setLive(false);                     // EventSource reconnects by itself
}

function setLive(on) {
  S.live = on;
  const el = document.getElementById("live");
  el.classList.toggle("on", on);
  el.textContent = on ? "Live" : "Reconnecting…";
}

function apply(m) {
  if (m.type === "worker") {
    const w = m.worker, point = w.point;
    delete w.point;
    const spark = (S.workers[w.id] && S.workers[w.id].spark) || [];
    if (point) {
      spark.push(point);
      if (spark.length > 180) spark.splice(0, spark.length - 180);
    }
    S.workers[w.id] = Object.assign(w, { spark });
    changed("worker", w.id, point);
  } else if (m.type === "alert") {
    const a = m.alert, fresh = !S.alerts.has(a.id);
    S.alerts.set(a.id, a);
    if (fresh) onNewAlert(a);
    changed("alert", a.worker);
  }
}

let frame = 0;
function changed(kind, id, point) {
  if (point && S.view && S.view.onPoint) S.view.onPoint(id, point);
  S.dirty.add(kind);
  if (frame) return;
  const run = () => {
    frame = 0;
    const d = S.dirty;
    S.dirty = new Set();
    renderBanner();
    if (S.view) S.view.update(d);
  };
  // requestAnimationFrame pauses in background tabs; the banner and title must still update
  frame = document.hidden ? setTimeout(run, 200) : requestAnimationFrame(run);
}

// ------------------------------------------------------------------ alarm: sound, title, notifications
function beep() {
  const ctx = S.audio;
  if (!ctx) return;
  const t = ctx.currentTime;
  [[0, 880], [0.32, 660]].forEach(([d, f]) => {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "square";
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t + d);
    g.gain.exponentialRampToValueAtTime(0.12, t + d + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.26);
    o.connect(g).connect(ctx.destination);
    o.start(t + d);
    o.stop(t + d + 0.3);
  });
}
setInterval(() => { if (S.sound && openAlerts().length) beep(); }, 2500);

function toggleSound() {
  if (!S.audio) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) S.audio = new AC();
  }
  if (S.audio) S.audio.resume();
  S.sound = !S.sound;
  const b = document.getElementById("sound");
  b.setAttribute("aria-pressed", String(S.sound));
  b.textContent = S.sound ? "🔔 Sound on" : "🔇 Sound off";
  if (S.sound) beep();
}

function onNewAlert(a) {
  if (S.notified.has(a.id)) return;
  S.notified.add(a.id);
  if (!a.needs_ack) return;
  if (S.sound) beep();
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(`${eventText(a.event)}: ${workerName(a.worker)}`, { body: a.detail || "", tag: a.id, requireInteraction: true });
    } catch (e) { /* some browsers only allow notifications from a service worker */ }
  }
}

async function acknowledge(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  try {
    const r = await api("/api/ack", { id, by: S.me || "supervisor" });
    if (!r.ok) throw new Error("already acknowledged or unknown alert");
  } catch (e) {
    window.alert(`Could not acknowledge: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = "Acknowledge"; }
  }
}
// Acknowledge buttons carry the alert ID; one delegated listener handles them all, so a
// button that a live update has re-used always acknowledges the alert it now shows.
const ackButton = (a, cls = "btn primary") =>
  h("button", { class: cls, type: "button", "data-ack": a.id }, "Acknowledge");
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-ack]");
  if (b && !b.disabled) acknowledge(b.dataset.ack, b);
});

function alertFacts(a) {
  const f = [];
  if (a.posture) f.push(a.posture);
  if (a.impact_g != null) f.push(`impact ${a.impact_g} g`);
  if (a.hr != null) f.push(`HR ${a.hr} bpm`);
  if (a.spo2 != null) f.push(`SpO2 ${a.spo2} %`);
  return f;
}

function renderBanner() {
  const el = document.getElementById("banner");
  const open = openAlerts();
  const pre = Object.values(S.workers).filter((w) => online(w) && (w.state === "FALL_PENDING" || w.state === "MAN_DOWN_PENDING"));
  const items = [...open.map((a) => ({ k: `a:${a.id}`, a })), ...pre.map((w) => ({ k: `p:${w.id}`, w }))];
  keyed(el, items, (it) => it.k, ({ a, w }) => a ?
    h("div", { class: "alarm", role: "alert" },
      h("span", { class: "icon", "aria-hidden": "true" }, "▲"),
      h("div", { class: "grow" },
        h("div", { class: "what" }, `${eventText(a.event)}: ${workerName(a.worker)}`),
        h("div", { class: "facts" }, [a.detail, ...alertFacts(a), `received ${clock(a.received)}`].filter(Boolean).join(" · "))),
      h("span", { class: "since", "data-since": a.received, title: "time since the alert arrived" }, mmss(now() - a.received)),
      h("a", { class: "btn small", href: `#/worker/${encodeURIComponent(a.worker)}` }, "Details"),
      ackButton(a)) :
    h("div", { class: "alarm pre" },
      h("span", { class: "icon", "aria-hidden": "true" }, "!"),
      h("div", { class: "grow" },
        h("div", { class: "what" }, `${w.state === "FALL_PENDING" ? "Possible fall" : "Not moving"}: ${w.name}`),
        h("div", { class: "facts" }, "Pre-alarm on the device. The worker can still cancel it; an alert follows if they do not."))));
  el.hidden = !items.length;
  document.title = open.length ? `(${open.length}) ALERT · Worker Safety Monitor` : "Worker Safety Monitor";
  const badge = document.getElementById("nav-open");
  badge.textContent = open.length;
  badge.hidden = !open.length;
}

// one-second ticker: running clocks, "seen ... ago", and online/offline changes
setInterval(() => {
  for (const el of document.querySelectorAll("[data-since]")) el.textContent = mmss(now() - Number(el.dataset.since));
  for (const el of document.querySelectorAll("[data-ago]")) el.textContent = `seen ${ago(now() - Number(el.dataset.ago))} ago`;
  let flip = false;
  for (const w of Object.values(S.workers)) {
    const on = online(w);
    if (S.online.get(w.id) !== on) { S.online.set(w.id, on); flip = true; }
  }
  if (flip && S.loaded) changed("all");
}, 1000);

// ------------------------------------------------------------------ small charts
function sparkline(points, idx, label) {
  const t1 = now(), t0 = t1 - SPARK_SECONDS;
  const pts = points.filter((p) => p[0] >= t0 && p[idx] != null);
  const svg = sv("svg", { viewBox: "0 0 100 34", preserveAspectRatio: "none", role: "img",
    "aria-label": `${label}, last ${SPARK_SECONDS / 60} minutes` });
  if (pts.length < 2) return svg;
  let lo = Math.min(...pts.map((p) => p[idx])), hi = Math.max(...pts.map((p) => p[idx]));
  if (hi - lo < 4) { const c = (hi + lo) / 2; lo = c - 2; hi = c + 2; }
  const x = (t) => ((t - t0) / (t1 - t0)) * 100, y = (v) => 31 - ((v - lo) / (hi - lo)) * 28;
  svg.append(sv("polyline", { points: pts.map((p) => `${x(p[0]).toFixed(2)},${y(p[idx]).toFixed(2)}`).join(" "),
    fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "vector-effect": "non-scaling-stroke",
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
  return svg;
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

// A line chart with a crosshair tooltip. points: [[t, value], ...] (value may be null = gap)
function makeChart(title, unit, opt = {}) {
  const now_ = h("span", { class: "now" });
  const svg = sv("svg", { role: "img", "aria-label": title });
  const tip = h("div", { class: "tip", hidden: true });
  const el = h("div", { class: "chart" }, h("div", {}, h("span", { class: "title" }, title), now_), svg, tip);
  let pts = [], x0 = 0, x1 = 1, g = null, hoverX = null;

  function draw() {
    const W = Math.max(svg.clientWidth || 600, 200), H = 170, m = { l: 42, r: 12, t: 12, b: 22 };
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.replaceChildren();
    const vals = pts.filter((p) => p[1] != null).map((p) => p[1]);
    let lo = opt.min != null ? opt.min : Math.min(...vals), hi = opt.max != null ? opt.max : Math.max(...vals);
    for (const r of opt.refs || []) { lo = Math.min(lo, r.v); hi = Math.max(hi, r.v); }
    if (!vals.length && !(opt.refs || []).length) { lo = 0; hi = 1; }
    if (opt.min == null || opt.max == null) {
      const span = Math.max(hi - lo, opt.minSpan || 1), c = (hi + lo) / 2;
      if (opt.min == null) lo = c - span * 0.6;
      if (opt.max == null) hi = c + span * 0.6;
      if (opt.floor != null) lo = Math.max(lo, opt.floor);
    }
    const X = (t) => m.l + ((t - x0) / (x1 - x0)) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
    for (const v of niceTicks(lo, hi)) {
      svg.append(sv("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
      svg.append(sv("text", { class: "axis-text", x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, opt.tick ? opt.tick(v) : v));
    }
    svg.append(sv("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
    const nx = Math.max(2, Math.min(6, Math.floor((W - m.l - m.r) / 110)));
    for (let i = 0; i <= nx; i++) {
      const t = x0 + ((x1 - x0) * i) / nx;
      const label = new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      svg.append(sv("text", { class: "axis-text", x: X(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle" }, label));
    }
    for (const r of opt.refs || []) {
      svg.append(sv("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(r.v), y2: Y(r.v) }));
      svg.append(sv("text", { class: "ref-text", x: W - m.r - 4, y: Y(r.v) - 4, "text-anchor": "end" }, r.label));
    }
    // the line, broken where readings are missing
    const gap = Math.max(30, (x1 - x0) / 60);
    let d = "", prev = null;
    for (const p of pts) {
      if (p[1] == null || p[0] < x0) { prev = null; continue; }
      d += `${prev && p[0] - prev[0] <= gap ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`;
      prev = p;
    }
    if (d) svg.append(sv("path", { class: "line", d }));
    else svg.append(sv("text", { class: "nodata", x: W / 2, y: H / 2, "text-anchor": "middle" }, "No readings in this period"));
    const last = [...pts].reverse().find((p) => p[1] != null);
    now_.textContent = last ? `now ${opt.fmt ? opt.fmt(last[1]) : last[1]} ${unit}` : "";
    g = { X, Y, W, H, m };
    if (hoverX != null) hover(hoverX);
  }

  function hover(px) {
    hoverX = px;
    const valid = pts.filter((p) => p[1] != null && p[0] >= x0);
    svg.querySelectorAll(".cross,.dot").forEach((n) => n.remove());
    if (!g || !valid.length) { tip.hidden = true; return; }
    const t = x0 + ((px - g.m.l) / (g.W - g.m.l - g.m.r)) * (x1 - x0);
    let best = valid[0];
    for (const p of valid) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    const cx = g.X(best[0]), cy = g.Y(best[1]);
    svg.append(sv("line", { class: "cross", x1: cx, x2: cx, y1: g.m.t, y2: g.H - g.m.b }));
    svg.append(sv("circle", { class: "dot", cx, cy, r: 4.5 }));
    tip.replaceChildren(h("span", { class: "key" }), h("strong", {}, `${opt.fmt ? opt.fmt(best[1]) : best[1]} ${unit}`),
      h("span", { class: "muted" }, `  ${clock(best[0])}`));
    tip.hidden = false;
    const sr = svg.getBoundingClientRect(), er = el.getBoundingClientRect();
    const left = sr.left - er.left + cx * (sr.width / g.W) + 12;
    tip.style.left = `${Math.max(4, Math.min(left, el.clientWidth - tip.offsetWidth - 4))}px`;
    tip.style.top = `${sr.top - er.top + 4}px`;
  }

  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    hover(((e.clientX - r.left) / r.width) * (g ? g.W : 600));
  });
  svg.addEventListener("pointerleave", () => {
    hoverX = null;
    tip.hidden = true;
    svg.querySelectorAll(".cross,.dot").forEach((n) => n.remove());
  });
  return { el, draw, set(p, from, to) { pts = p; x0 = from; x1 = to; draw(); } };
}

// ------------------------------------------------------------------ views
function fact(label, value) {
  return value == null || value === "" ? null : h("span", {}, h("span", { class: "muted" }, `${label} `), String(value));
}
function workerNotes(w) {
  const n = [];
  if (w.sensor_fault) n.push(h("span", { class: "note bad" }, "⚠ motion sensor fault: fall detection is off"));
  if (w.worn === false) n.push(h("span", { class: "note warn" }, "pulse sensor not on skin"));
  if (w.vitals_reliable === false && w.worn !== false) n.push(h("span", { class: "note" }, "vitals paused while moving"));
  if (w.queued > 0) n.push(h("span", { class: "note warn" }, `${w.queued} message(s) waiting to send`));
  return n.length ? h("div", { class: "notes" }, n) : null;
}
function vital(label, value, unit, spark, idx, paused) {
  return h("div", { class: "vital" },
    h("div", { class: "label" }, label, paused ? h("span", { class: "muted" }, " (paused)") : null),
    h("div", { class: "num" }, fmt(value), h("span", { class: "unit" }, unit)),
    sparkline(spark || [], idx, label));
}
function workerCard(w) {
  const st = status(w);
  return h("a", { class: `card${st.cls === "alert" ? " alarmed" : ""}${st.cls === "offline" ? " offline" : ""}`,
    href: `#/worker/${encodeURIComponent(w.id)}` },
    h("div", { class: "head" },
      h("div", {},
        h("div", { class: "name" }, w.name),
        h("div", { class: "sub" }, w.name !== w.id ? `${w.id} · ` : "",
          h("span", { "data-ago": w.last_seen }, `seen ${ago(now() - w.last_seen)} ago`),
          w.rssi != null ? ` · Wi-Fi ${w.rssi} dBm` : "")),
      pill(st)),
    h("div", { class: "vitals" },
      vital("Heart rate", w.hr, "bpm", w.spark, 1, w.vitals_reliable === false),
      vital("SpO2", w.spo2, "%", w.spark, 2, w.vitals_reliable === false)),
    h("div", { class: "facts" },
      fact("Posture", w.posture), fact("Motion", w.motion_g != null ? `${fmt(w.motion_g, 2)} g` : null),
      fact("Fall prob.", w.fall_prob != null ? fmt(w.fall_prob, 2) : null)),
    workerNotes(w));
}

function startOfToday() { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }

const Overview = {
  mount(root) {
    this.tiles = h("div", { class: "tiles" });
    this.grid = h("div", { class: "grid" });
    this.empty = h("div", { class: "empty", hidden: true },
      h("p", {}, h("strong", {}, "No devices yet.")),
      h("p", {}, "Set up a device on the ", h("a", { href: "#/setup" }, "Setup page"),
        ", or try the app with simulated workers: ", h("code", {}, "python webapp/server.py --demo 3")));
    root.replaceChildren(this.tiles, this.grid, this.empty);
    this.update();
  },
  update() {
    const ws = sortedWorkers();
    const today = [...S.alerts.values()].filter((a) => a.needs_ack && a.received >= startOfToday());
    const ackTimes = today.filter((a) => a.acked_at).map((a) => a.acked_at - a.received).sort((a, b) => a - b);
    const median = ackTimes.length ? ackTimes[Math.floor(ackTimes.length / 2)] : null;
    const tile = (label, value) => h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
    morph(this.tiles, h("div", { class: "tiles" },
      tile("Workers online", `${ws.filter(online).length} / ${ws.length}`),
      tile("Open alerts", openAlerts().length),
      tile("Alerts today", today.length),
      tile("Median time to acknowledge", median == null ? "–" : mmss(median))));
    keyed(this.grid, ws, (w) => w.id, workerCard);
    this.empty.hidden = ws.length > 0 || !S.loaded;
  },
};

function eventCell(a) {
  const icon = a.needs_ack ? "▲" : "·";
  return h("td", { class: "event" }, h("span", { "aria-hidden": "true" }, `${icon} `), eventText(a.event));
}
function ackCell(a) {
  if (a.acked_at) return h("td", {}, `✓ ${a.acked_by || "acknowledged"} after ${mmss(a.acked_at - a.received)}`);
  if (a.cleared_at) return h("td", { class: "muted" }, "cleared on the device");
  if (a.needs_ack) return h("td", {}, ackButton(a, "btn primary small"));
  return h("td", { class: "muted" }, "information");
}

const Alerts = {
  mount(root) {
    this.root = root;
    this.f = { worker: "", kind: "", open: false };
    this.workerSel = h("select", { "aria-label": "Worker", onchange: (e) => { this.f.worker = e.target.value; this.update(); } });
    this.kindSel = h("select", { "aria-label": "Event", onchange: (e) => { this.f.kind = e.target.value; this.update(); } },
      h("option", { value: "" }, "All events"),
      Object.entries(EVENT_TEXT).map(([k, v]) => h("option", { value: k }, v)));
    this.openBox = h("input", { type: "checkbox", onchange: (e) => { this.f.open = e.target.checked; this.update(); } });
    this.tbody = h("tbody");
    this.table = h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Received", "Worker", "Event", "Details", "Posture", "Impact", "HR", "SpO2", "Status"].map((c) => h("th", {}, c)))),
      this.tbody));
    this.empty = h("div", { class: "empty" }, "No alerts match.");
    this.names = "";
    root.replaceChildren(
      h("h1", {}, "Alerts"),
      h("div", { class: "filters" }, this.workerSel, this.kindSel, h("label", {}, this.openBox, "Open only"),
        h("span", { class: "spacer" }), h("a", { class: "btn", href: "/api/alerts.csv" }, "Export CSV")),
      this.table, this.empty);
    api("/api/alerts?limit=5000").then((r) => { for (const a of r.alerts) S.alerts.set(a.id, a); this.update(); });
    this.update();
  },
  update() {
    const ws = Object.values(S.workers).sort((a, b) => String(a.name).localeCompare(b.name));
    const names = ws.map((w) => `${w.id}=${w.name}`).join("|");
    if (names !== this.names) {             // rebuild the list only when workers change, not on every reading
      this.names = names;
      this.workerSel.replaceChildren(h("option", { value: "" }, "All workers"),
        ws.map((w) => h("option", { value: w.id, selected: w.id === this.f.worker }, w.name)));
    }
    let rows = [...S.alerts.values()].sort((a, b) => b.received - a.received);
    if (this.f.worker) rows = rows.filter((a) => a.worker === this.f.worker);
    if (this.f.kind) rows = rows.filter((a) => a.event === this.f.kind);
    if (this.f.open) rows = rows.filter(isOpen);
    keyed(this.tbody, rows.slice(0, 1000), (a) => a.id, (a) => h("tr", {},
      h("td", {}, dateTime(a.received)),
      h("td", {}, h("a", { href: `#/worker/${encodeURIComponent(a.worker)}` }, workerName(a.worker))),
      eventCell(a), h("td", { class: "wrap" }, a.detail || ""), h("td", {}, a.posture || ""),
      h("td", {}, a.impact_g != null ? `${a.impact_g} g` : ""), h("td", {}, fmt(a.hr)), h("td", {}, fmt(a.spo2)),
      ackCell(a)));
    this.table.hidden = !rows.length;
    this.empty.hidden = rows.length > 0;
  },
};

function WorkerView(id) {
  const RANGES = [[10, "10 min"], [60, "1 h"], [480, "8 h"], [1440, "24 h"]];
  return {
    range: 60, series: [],
    async mount(root) {
      this.root = root;
      this.head = h("div");
      this.rangeBtns = RANGES.map(([m, label]) => h("button", { type: "button", "aria-pressed": String(m === this.range),
        onclick: () => { this.range = m; this.rangeBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(RANGES[i][0] === m))); this.load(); } }, label));
      this.charts = {
        hr: makeChart("Heart rate", "bpm", { minSpan: 20, floor: 0, refs: [{ v: LIMITS.hrHigh, label: `alarm above ${LIMITS.hrHigh}` }, { v: LIMITS.hrLow, label: `alarm below ${LIMITS.hrLow}` }] }),
        spo2: makeChart("SpO2", "%", { max: 100, minSpan: 8, refs: [{ v: LIMITS.spo2Low, label: `alarm below ${LIMITS.spo2Low}` }] }),
        motion: makeChart("Motion (variation of acceleration)", "g", { min: 0, minSpan: 0.3, fmt: (v) => v.toFixed(2), tick: (v) => v.toFixed(1) }),
        prob: makeChart("Fall probability (latest window)", "", { min: 0, max: 1, fmt: (v) => v.toFixed(2), tick: (v) => String(v) }),
      };
      this.info = h("dl", { class: "kv" });
      this.atbody = h("tbody");
      this.atable = h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Received", "Event", "Details", "Status"].map((c) => h("th", {}, c)))), this.atbody));
      this.aempty = h("div", { class: "empty" }, "No alerts from this worker.");
      this.alertList = h("div", {}, this.atable, this.aempty);
      root.replaceChildren(
        h("a", { class: "back", href: "#/" }, "← All workers"), this.head,
        h("div", { class: "filters" }, h("span", { class: "muted" }, "Show the last"), h("div", { class: "range" }, this.rangeBtns)),
        h("div", { class: "charts" }, Object.values(this.charts).map((c) => c.el)),
        h("h2", {}, "Device and alerts"),
        h("div", { class: "two" }, this.info, this.alertList));
      this.update();
      await this.load();
    },
    async load() {
      const r = await api(`/api/history?worker=${encodeURIComponent(id)}&minutes=${this.range}`);
      this.series = r.points || [];
      this.drawCharts();
    },
    onPoint(wid, p) {
      if (wid === id) this.series.push([p[0], null, p[1], p[2], p[3], null, p[4], null]);
    },
    drawCharts() {
      const t1 = now(), t0 = t1 - this.range * 60;
      const col = (i) => this.series.filter((p) => p[0] >= t0).map((p) => [p[0], p[i]]);
      this.charts.hr.set(col(2), t0, t1);
      this.charts.spo2.set(col(3), t0, t1);
      this.charts.motion.set(col(4), t0, t1);
      this.charts.prob.set(col(6), t0, t1);
    },
    update() {
      const w = S.workers[id];
      if (!w) { morph(this.head, h("div", {}, h("h1", {}, id), h("p", { class: "muted" }, "This worker has not reported yet."))); return; }
      const st = status(w);
      morph(this.head, h("div", {},
        h("div", { class: "detail-head" },
          h("h1", {}, w.name), pill(st),
          h("button", { class: "btn small", type: "button", onclick: () => this.rename(S.workers[id]) }, "Rename"),
          w.demo && online(w) && w.state === "NORMAL" ? h("button", { class: "btn small", type: "button",
            title: "The device plays a recorded synthetic fall into its own sensor stream; the real model and alarm then run",
            onclick: (e) => this.demoFall(e.currentTarget) }, "Simulate a fall (demo)") : null,
          ...openAlerts().filter((a) => a.worker === id).map((a) => ackButton(a))),
        h("p", { class: "muted" }, w.note || "", w.note ? " · " : "", h("span", { "data-ago": w.last_seen }, `seen ${ago(now() - w.last_seen)} ago`)),
        workerNotes(w)));
      const row = (k, v) => v == null || v === "" ? [] : [h("dt", {}, k), h("dd", {}, String(v))];
      morph(this.info, h("dl", { class: "kv" },
        ...row("Device ID", w.id), ...row("Firmware", w.fw), ...row("IP address", w.ip),
        ...row("Wi-Fi signal", w.rssi != null ? `${w.rssi} dBm` : null),
        ...row("Uptime", w.uptime_s != null ? ago(w.uptime_s) : null),
        ...row("State", w.state), ...row("Posture", w.posture != null ? `${w.posture} (${fmt(w.tilt_deg)}° from upright)` : null),
        ...row("Heart rate", w.hr != null ? `${fmt(w.hr)} bpm` : null), ...row("SpO2", w.spo2 != null ? `${fmt(w.spo2)} %` : null),
        ...row("Model windows", w.windows), ...row("Inference time", w.infer_us_avg ? `${fmt(w.infer_us_avg / 1000, 1)} ms` : null),
        ...row("Orientation", w.calibrated == null ? null : w.calibrated ? "calibrated on the setup page" : "default mounting (not calibrated)"),
        ...row("Worst inference", w.infer_us_max ? `${fmt(w.infer_us_max / 1000, 1)} ms` : null),
        ...row("Free memory", w.heap_kb != null ? `${w.heap_kb} KB` : null), ...row("Queued messages", w.queued),
        ...row("First seen", dateTime(w.first_seen))));
      const mine = [...S.alerts.values()].filter((a) => a.worker === id).sort((a, b) => b.received - a.received).slice(0, 50);
      keyed(this.atbody, mine, (a) => a.id, (a) => h("tr", {}, h("td", {}, dateTime(a.received)), eventCell(a),
        h("td", { class: "wrap" }, [a.detail, ...alertFacts(a)].filter(Boolean).join(" · ")), ackCell(a)));
      this.atable.hidden = !mine.length;
      this.aempty.hidden = mine.length > 0;
      this.drawCharts();
    },
    async demoFall(btn) {
      btn.disabled = true;
      btn.textContent = "Requested: plays at the next report…";
      try { await api(`/api/workers/${encodeURIComponent(id)}/demo-fall`, {}); }
      catch (e) { window.alert(`Could not request the demo: ${e.message}`); }
      setTimeout(() => { btn.disabled = false; btn.textContent = "Simulate a fall (demo)"; }, 8000);
    },
    async rename(w) {
      const name = window.prompt("Name for this worker (shown on the dashboard):", w.name);
      if (name == null) return;
      const note = window.prompt("Note, e.g. team or location (optional):", w.note || "");
      await api(`/api/workers/${encodeURIComponent(id)}`, { name, note: note == null ? w.note : note });
    },
  };
}

const Setup = {
  async mount(root) {
    this.root = root;
    root.replaceChildren(h("h1", {}, "Setup"), h("p", { class: "muted" }, "Loading…"));
    let info = {};
    try { info = await api("/api/info"); } catch (e) { /* shown below as unknown */ }
    if (S.view !== Setup) return;                          // the user moved on meanwhile
    const urls = info.device_urls || [];
    const copy = (text) => h("button", { class: "btn small", type: "button",
      onclick: (e) => { navigator.clipboard && navigator.clipboard.writeText(text); e.currentTarget.textContent = "Copied"; } }, "Copy");
    const secure = window.isSecureContext;
    const nameInput = h("input", { type: "text", value: S.me, placeholder: "e.g. Priya (site supervisor)",
      oninput: (e) => { S.me = e.target.value.trim(); store("wsm.name", S.me); } });
    const flashStep = info.firmware && info.firmware.length ? [
      h("p", {}, `Firmware ready for: ${info.firmware.join(", ")}. Plug the board into this computer with a USB data cable, then:`),
      secure ? h("esp-web-install-button", { manifest: "/firmware/manifest.json" }) :
        h("p", { class: "note warn" }, "Flashing from the browser needs this page opened as http://localhost:" + (info.port || 8080) + " on the computer the board is plugged into, in Chrome or Edge."),
    ] : [
      h("p", {}, "No firmware file is in ", h("code", {}, "webapp/firmware/"), " yet. Either:"),
      h("ul", {},
        h("li", {}, "run ", h("code", {}, "python webapp/get_firmware.py"), " to download the latest release, then reload this page; or"),
        h("li", {}, "follow ", h("strong", {}, "HARDWARE.md"), " to flash with esptool or build with ESP-IDF.")),
    ];
    root.replaceChildren(h("h1", {}, "Setup"), h("div", { class: "steps" },
      h("section", { class: "step" },
        h("h2", {}, "1. Put everything on one Wi-Fi network"),
        h("p", {}, "The devices and this computer must be on the same 2.4 GHz Wi-Fi network. A phone hotspot works well; university networks that need a login (such as eduroam) usually do not."),
        h("p", {}, "Server address to type into each device:"),
        urls.length ? urls.map((u) => h("div", { class: "addr" }, h("code", {}, u), copy(u))) :
          h("p", { class: "note warn" }, "Could not find this computer's network address. Look it up in the network settings (it looks like 192.168.x.y) and use http://<address>:" + (info.port || 8080)),
        info.token_required ? h("p", {}, "This server requires a device token. Enter the same token in each device's setup page.") : null),
      h("section", { class: "step" },
        h("h2", {}, "2. Flash a new device (once)"), ...flashStep),
      h("section", { class: "step" },
        h("h2", {}, "3. Connect the device to Wi-Fi"),
        h("ol", {},
          h("li", {}, "Power the device. On its first start it opens its own Wi-Fi network called ", h("strong", {}, "WorkerSafety-XXXX"), " and its LED blinks fast. To return to this mode later, hold the button for 10 s (not while plugging it in: that starts the chip's flashing mode)."),
          h("li", {}, "Join that network with your phone. The setup page opens by itself; if not, go to ", h("code", {}, "http://192.168.4.1"), "."),
          h("li", {}, "Choose your Wi-Fi network, enter its password, the server address from step 1 and the worker's name, then Save."),
          h("li", {}, "The device restarts, joins your Wi-Fi and appears on the Overview page within a few seconds. Its LED blinks slowly when it is connected."))),
      h("section", { class: "step" },
        h("h2", {}, "4. Check the alarm on this computer"),
        h("p", {}, "Turn on the sound with the button at the top of the page (browsers only allow sound after a click), then send a test alert."),
        h("div", { class: "addr" },
          h("button", { class: "btn blue", type: "button", onclick: () => api("/api/test-alert", {}) }, "Send a test alert"),
          "Notification" in window && secure ? h("button", { class: "btn", type: "button",
            onclick: async (e) => { const p = await Notification.requestPermission(); e.currentTarget.textContent = p === "granted" ? "Notifications on" : "Notifications blocked"; } },
          Notification.permission === "granted" ? "Notifications on" : "Enable desktop notifications") : null),
        h("p", {}, h("label", {}, "Your name, shown with your acknowledgments: ", nameInput))),
      h("section", { class: "step" },
        h("h2", {}, "No hardware yet?"),
        h("p", {}, "Run the app with simulated workers: ", h("code", {}, "python webapp/server.py --demo 3"),
          ". Or run the real firmware in the ESP32 emulator, as described in DEMO.md."),
        h("p", { class: "muted" }, `Server version ${info.version || "?"} · devices count as offline after ${info.offline_after || S.offlineAfter} s without a report · `,
          h("a", { href: "/simple" }, "simple view without JavaScript")))));
    if (secure && info.firmware && info.firmware.length && !customElements.get("esp-web-install-button")) {
      const s = document.createElement("script");
      s.type = "module";
      s.src = "https://unpkg.com/esp-web-tools@10.4.0/dist/web/install-button.js?module";
      document.head.append(s);
    }
  },
  update() { /* static page */ },
};

// ------------------------------------------------------------------ router
function route() {
  const hash = location.hash.replace(/^#/, "") || "/";
  const root = document.getElementById("view");
  let key = "overview", view = Overview;
  if (hash.startsWith("/worker/")) { key = "worker"; view = WorkerView(decodeURIComponent(hash.slice(8))); }
  else if (hash === "/alerts") { key = "alerts"; view = Alerts; }
  else if (hash === "/setup") { key = "setup"; view = Setup; }
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === key));
  S.view = view;
  view.mount(root);
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
window.addEventListener("resize", () => { if (S.view && S.view.drawCharts) S.view.drawCharts(); });
document.getElementById("sound").addEventListener("click", toggleSound);
route();
connect();
