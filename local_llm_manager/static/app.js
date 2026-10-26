"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { meta: null, recipes: [], schedules: [], hosts: [], busy: new Set(), tab: "recipes", blur: true, schedPaused: false };
try { state.blur = localStorage.getItem("llm.blur") !== "0"; } catch { /* storage unavailable */ }
const STATUS_LABEL = { ready: "Ready", loading: "Loading model", starting: "Starting", stopped: "Stopped", failed: "Failed", offline: "Host offline" };
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = text;
  try { data = JSON.parse(text); } catch { /* plain text */ }
  if (!res.ok) {
    let msg = data?.detail ?? data ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("\n");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 4500);
}

function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/* ---------------- tabs ---------------- */

$$(".tab").forEach((btn) => btn.addEventListener("click", () => {
  state.tab = btn.dataset.tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${state.tab}`));
  if (state.tab === "schedules") loadSchedules();
  if (state.tab === "activity") loadEvents();
  if (state.tab === "hosts") renderHosts();
  if (state.tab === "images") loadImages();
}));

$$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

/* ---------------- hosts ---------------- */

const multiHost = () => state.hosts.length > 1;
const hostById = (id) => state.hosts.find((h) => h.id === Number(id));
const hostName = (id) => hostById(id)?.name ?? `#${id}`;
const hostPlatform = (id) => hostById(id)?.info?.platform ?? "";
const PLATFORM = { darwin: "macOS", linux: "Linux" };

function memText(m) { return `${m.used_gb} / ${m.total_gb} GB`; }

/* Blur a host name / IP when the "Blur" toggle in the top bar is on, for screenshots and screen
 * shares. Nothing of the real name is shown: each name becomes a stable alias such as "host-3f2a"
 * (an IPv4 address "ip-…"), so different machines still look different. Only display strings are
 * masked; the underlying values stay untouched. */
function aliasHash(s) {
  let h = 0x811c9dc5;  // FNV-1a
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return (h >>> 0).toString(16).padStart(8, "0").slice(0, 4);
}
function maskAddr(name) {
  const s = String(name ?? "").trim();
  if (!s) return "–";
  if (!state.blur) return s;
  if (s === "localhost" || s === "127.0.0.1" || s === "::1") return s;
  return `${/^(?:\d{1,3}\.){3}\d{1,3}$/.test(s) ? "ip" : "host"}-${aliasHash(s.toLowerCase())}`;
}
function maskUrl(url) {
  if (!url) return "–";
  if (!state.blur) return url;
  try {
    const u = new URL(url);
    const host = maskAddr(u.hostname);
    return u.port ? `${host}:${u.port}` : host;
  } catch {
    return maskAddr(url);
  }
}
// Full endpoint URL (http://host:port/v1) with only the host part masked, so it stays recognisable.
function maskEndpoint(urlStr) {
  if (!state.blur) return urlStr;
  try {
    const u = new URL(urlStr);
    return urlStr.replace(`//${u.hostname}`, `//${maskAddr(u.hostname)}`);
  } catch {
    return maskAddr(urlStr);
  }
}
// Mask a host's own name (from its URL) and any IPv4 address inside free text such as an error message.
function blurHostText(text, url) {
  let str = String(text ?? "");
  if (!state.blur) return str;
  try { const h = new URL(url).hostname; if (h) str = str.split(h).join(maskAddr(h)); } catch { /* no URL */ }
  return blurText(str);
}
// Mask bare IPv4 addresses inside free text (e.g. activity log messages).
function blurText(s) {
  const str = String(s ?? "");
  if (!state.blur) return str;
  return str.replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, (m) => maskAddr(m));
}
function memPct(m) { return m.total_gb ? (m.used_gb / m.total_gb) * 100 : 0; }
function barClass(pct) { return "bar-fill" + (pct > 90 ? " crit" : pct > 75 ? " warn" : ""); }

// Address of a model endpoint: the host's own name for remote hosts, else the one in the address bar.
function endpointHost(hostId) {
  const h = hostById(hostId);
  if (h?.url) { try { return new URL(h.url).hostname; } catch { /* fall through */ } }
  return location.hostname;
}

async function loadHosts() {
  try {
    state.hosts = await api("/api/hosts");
  } catch { return; }
  renderHostsStrip();
  renderHostOptions();
  if (state.tab === "hosts") renderHosts();
}

function renderHostsStrip() {
  $("#hosts-strip").innerHTML = state.hosts.map((h) => {
    if (!h.online) {
      return `<button class="host-chip offline" data-host="${h.id}" title="${esc(blurHostText(h.error || "offline", h.url))}">
        <span class="host-name">${esc(maskAddr(h.name))}</span><span class="host-mem">offline</span></button>`;
    }
    const m = h.system.memory;
    const pct = memPct(m);
    const gpu = h.system.gpus[0];
    const title = `${maskAddr(h.name)}: ${memText(m)} used, ${m.available_gb} GB free` + (gpu ? `\nGPU ${gpu.utilization ?? "?"}% · ${gpu.temperature ?? "?"}°C · ${gpu.power_w ?? "?"} W` : "");
    return `<button class="host-chip" data-host="${h.id}" title="${esc(title)}">
        <span class="host-name">${esc(maskAddr(h.name))} ${h.is_local ? '<span class="host-note">this machine</span>' : '<span class="host-note">remote</span>'}</span>
        <span class="bar"><span class="${barClass(pct)}" style="width:${pct.toFixed(1)}%"></span></span>
        <span class="host-mem">${esc(memText(m))}${gpu ? ` · GPU ${gpu.utilization ?? "?"}%` : ""}</span>
      </button>`;
  }).join("");
}

$("#hosts-strip").addEventListener("click", () => $('.tab[data-tab="hosts"]').click());

/* ---------------- blur toggle (top bar) ---------------- */

function renderBlurToggle() {
  const b = $("#btn-blur");
  b.classList.toggle("on", state.blur);
  b.setAttribute("aria-pressed", String(state.blur));
  b.textContent = state.blur ? "🙈 Blur" : "👁 Show";
  b.title = state.blur ? "IP addresses and host names are blurred – click to show them"
    : "IP addresses and host names are visible – click to blur them";
}

$("#btn-blur").addEventListener("click", () => {
  state.blur = !state.blur;
  try { localStorage.setItem("llm.blur", state.blur ? "1" : "0"); } catch { /* ignore */ }
  renderBlurToggle();
  renderHostsStrip();
  renderHostOptions();
  renderRecipes();
  if (state.tab === "hosts") renderHosts();
  if (state.tab === "schedules") loadSchedules();
  if (state.tab === "activity") loadEvents();
  if (state.tab === "images") renderImages();
});
renderBlurToggle();

/* ---------------- theme toggle (top bar) ---------------- */

try { state.theme = ["light", "auto"].includes(localStorage.getItem("llm.theme")) ? localStorage.getItem("llm.theme") : "dark"; } catch { state.theme = "dark"; }

function effectiveTheme() {
  return state.theme === "auto" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : state.theme;
}
function applyTheme() {
  document.documentElement.dataset.theme = effectiveTheme();
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (state.theme === "auto") applyTheme(); });

function renderThemeToggle() {
  const b = $("#btn-theme");
  b.textContent = { dark: "🌙 Dark", light: "☀️ Light", auto: "🌗 Auto" }[state.theme];
  b.title = { dark: "Dark theme – click for light", light: "Light theme – click to follow the system", auto: `Following the system (currently ${effectiveTheme()}) – click for dark` }[state.theme];
}

$("#btn-theme").addEventListener("click", () => {
  state.theme = { dark: "light", light: "auto", auto: "dark" }[state.theme];
  try { localStorage.setItem("llm.theme", state.theme); } catch { /* ignore */ }
  applyTheme();
  renderThemeToggle();
});
renderThemeToggle();

function renderHostOptions() {
  const opts = state.hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}${h.is_local ? " (this machine)" : ""}</option>`).join("");
  const filter = $("#recipe-host");
  const cur = filter.value;
  filter.innerHTML = `<option value="">All hosts</option>` + opts;
  filter.value = state.hosts.some((h) => String(h.id) === cur) ? cur : "";
  filter.closest("label").classList.toggle("hidden", !multiHost());
  // Editor selects are refreshed when their dialog opens, so an open form keeps its choice.
}

function renderHosts() {
  renderPorts();
  const running = (id) => state.recipes.filter((r) => r.host_id === id && RUNNING.includes(r.status)).length;
  const total = (id) => state.recipes.filter((r) => r.host_id === id).length;
  $("#hosts").innerHTML = state.hosts.map((h) => {
    const m = h.system?.memory;
    const gpu = h.system?.gpus?.[0];
    return `<tr data-id="${h.id}">
      <td><b>${esc(maskAddr(h.name))}</b>${h.is_local ? ` <span class="chip small on">this machine</span>` : `<br><small class="host-url">${esc(maskUrl(h.url))}</small>`}</td>
      <td>${h.online ? `<span class="ok">● online</span>` : `<span class="bad">● offline</span><br><small>${esc(blurHostText(h.error || "", h.url))}</small>`}</td>
      <td>${h.info ? `${esc(PLATFORM[h.info.platform] ?? h.info.platform)} · ${esc(h.info.machine)}<br><small>${esc(maskAddr(h.info.hostname))} · v${esc(h.info.version)}</small>` : "–"}</td>
      <td style="min-width:160px">${m ? `<div class="bar"><div class="${barClass(memPct(m))}" style="width:${memPct(m).toFixed(1)}%"></div></div>
        <small>${esc(memText(m))} · ${m.available_gb} GB free</small>` : "–"}</td>
      <td>${gpu ? `${gpu.utilization ?? "?"}% · ${gpu.temperature ?? "?"}°C` : "–"}</td>
      <td>${running(h.id)} running / ${total(h.id)}</td>
      <td style="white-space:nowrap">
        <button class="btn small ghost" data-act="stop-all" ${h.online ? "" : "disabled"}>Stop all</button>
        <button class="btn small ghost" data-act="free" ${h.online ? "" : "disabled"} title="Stop all models on this host and drop the page cache">Free memory</button>
        <button class="btn small ghost" data-act="edit">Edit</button>
        ${h.is_local ? "" : `<button class="btn small ghost danger" data-act="delete">Remove</button>`}
      </td></tr>`;
  }).join("");
}

$("#hosts").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const h = hostById(btn.closest("tr").dataset.id);
  const act = btn.dataset.act;
  if (act === "stop-all") {
    if (confirm(`Stop all models on ${h.name}?`)) await runAction(`host-${h.id}`, `/api/system/stop-all?host_id=${h.id}`);
  } else if (act === "free") {
    if (confirm(`Stop all models on ${h.name} and drop its page cache?`)) await runAction(`host-${h.id}`, `/api/system/free-memory?host_id=${h.id}`);
  } else if (act === "edit") {
    openHostEditor(h);
    return;
  } else if (act === "delete") {
    if (!confirm(`Remove host ${h.name} from this dashboard? (Nothing is changed on the machine.)`)) return;
    try { await api(`/api/hosts/${h.id}`, { method: "DELETE" }); toast("Host removed", "ok"); } catch (e) { toast(e.message, "bad"); }
  }
  loadHosts();
});

const hostForm = $("#form-host");
let editingHost = null;

function openHostEditor(h) {
  editingHost = h;
  hostForm.reset();
  $("#host-error").textContent = "";
  $("#host-title").textContent = h ? `Edit host – ${h.name}` : "Add host";
  hostForm.elements.name.value = h?.name ?? "";
  hostForm.elements.url.value = h?.url ?? "";
  const local = h?.is_local;
  $("#lbl-host-url").classList.toggle("hidden", !!local);
  $("#lbl-host-token").classList.toggle("hidden", !!local);
  $("#host-token-hint").textContent = h ? "leave empty to keep the current token" : "the agent's DGXM_AGENT_TOKEN";
  $("#dlg-host").showModal();
}

$("#btn-new-host").addEventListener("click", () => openHostEditor(null));

hostForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { name: hostForm.elements.name.value, url: hostForm.elements.url.value, token: hostForm.elements.token.value };
  try {
    const h = editingHost
      ? await api(`/api/hosts/${editingHost.id}`, { method: "PUT", body })
      : await api("/api/hosts", { method: "POST", body });
    $("#dlg-host").close();
    toast(h.online ? `${h.name} is online` : `Saved, but ${h.name} is not reachable: ${h.error}`, h.online ? "ok" : "bad");
    await loadHosts();
    loadRecipes();
  } catch (e) {
    $("#host-error").textContent = e.message;
  }
});

/* ---------------- recipes ---------------- */

async function loadRecipes() {
  try {
    state.recipes = await api("/api/recipes");
    recordMetrics(state.recipes);
    if (watchStarts(state.recipes)) await loadStartTimes();  // a boot just finished
    renderRecipes();
  } catch (e) {
    $("#recipes").innerHTML = `<div class="empty">Failed to load recipes: ${esc(e.message)}</div>`;
  }
}

/* ---------------- recipe list: filter, sort, group ---------------- */

const VIEW_KEY = "dgxm.recipeView";
const view = { search: "", tags: [], sort: "name", group: "", host: "", collapsed: {} };
try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); } catch { /* storage unavailable */ }
if (typeof view.group === "boolean") view.group = view.group ? "tag" : "";  // older saved views
const saveView = () => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const RUNNING = ["ready", "loading", "starting"];
const STATUS_ORDER = { ready: 0, loading: 1, starting: 2, failed: 3, stopped: 4, offline: 5 };

/* Start-duration history (server-measured trigger→ready) and a live timer while a model boots. */
state.startTimes = {};                       // recipe id -> [seconds, newest first]
const startWatch = new Map();                // recipe id -> ts of first "starting" observation
async function loadStartTimes() { try { state.startTimes = await api("/api/start-times"); } catch { /* keep last */ } }
function watchStarts(recipes) {
  const now = Date.now();
  let finished = false;
  for (const r of recipes) {
    const booting = r.status === "starting" || r.status === "loading";
    if (booting && !startWatch.has(r.id)) startWatch.set(r.id, now);
    if (!booting && startWatch.has(r.id)) { startWatch.delete(r.id); finished = true; }
  }
  return finished;
}
const median = (a) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
function fmtDur(s) {
  s = Math.round(s);
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h ${Math.round(s % 3600 / 60)}m`;
}
const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
const SORTS = {
  name: byName,
  status: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || byName(a, b),
  framework: (a, b) => a.framework.localeCompare(b.framework) || byName(a, b),
  port: (a, b) => a.host_port - b.host_port || byName(a, b),
  updated: (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "") || byName(a, b),
};

function allTags() {
  const counts = new Map();
  for (const r of state.recipes) for (const t of r.tags || []) counts.set(t, (counts.get(t) || 0) + 1);
  return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { sensitivity: "base", numeric: true }));
}

function visibleRecipes() {
  const q = view.search.trim().toLowerCase();
  const want = view.tags.map((t) => t.toLowerCase());
  return state.recipes.filter((r) => {
    if (view.host && String(r.host_id) !== view.host) return false;
    const tags = (r.tags || []).map((t) => t.toLowerCase());
    if (!want.every((t) => tags.includes(t))) return false;
    if (!q) return true;
    return [r.name, r.model, r.description, r.image, r.container_name, r.framework, hostName(r.host_id), ...tags]
      .some((v) => String(v || "").toLowerCase().includes(q));
  }).sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (SORTS[view.sort] || byName)(a, b));
}

function renderToolbar() {
  const tags = allTags();
  // Drop filters for tags that no longer exist.
  const known = new Set(tags.map(([t]) => t.toLowerCase()));
  view.tags = view.tags.filter((t) => known.has(t.toLowerCase()));
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  $("#tag-filter").innerHTML = tags.length
    ? `<button class="chip ${active.size ? "" : "on"}" data-tag="">All <span>${state.recipes.length}</span></button>` +
      tags.map(([t, n]) => `<button class="chip ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}">${esc(t)} <span>${n}</span></button>`).join("")
    : `<span class="hint">Add tags to recipes (e.g. the model name) to filter and group them here.</span>`;
  $("#tag-list").innerHTML = tags.map(([t]) => `<option value="${esc(t)}">`).join("");
}

function fmtTps(v) { return v >= 100 ? Math.round(v).toLocaleString() : +v.toFixed(1); }

// Live numbers from the server's /metrics (rates since the previous poll), or else from its last
// stats line in the logs (see servermetrics.py / logstats.py); refreshed with the status poll.
const STALE_LOG_S = 30;  // vLLM prints a stats line every ~10 s while busy and none while idle

/* Per-recipe generation-throughput history (client side, sampled at the status poll). */
const sparkHist = new Map();
const SPARK_MAX = 40;   // ~3–4 min at the 5 s poll
function recordMetrics(recipes) {
  const now = Date.now();
  for (const r of recipes) {
    const v = r.metrics?.gen_tps;
    if (!RUNNING.includes(r.status) || v == null) continue;
    let a = sparkHist.get(r.id);
    if (!a) sparkHist.set(r.id, a = []);
    const last = a[a.length - 1];
    if (last && now - last.t < 3000) { last.v = v; last.t = now; }  // coalesce re-renders
    else a.push({ t: now, v });
    if (a.length > SPARK_MAX) a.shift();
  }
}
function sparkline(id, idle) {
  const a = sparkHist.get(id);
  if (!a || a.length < 3) return "";
  const w = 72, h = 16;
  const vs = a.map((p) => p.v);
  const max = Math.max(...vs), min = Math.min(...vs), range = max - min || 1;
  const pts = a.map((p, i) =>
    `${((i / (a.length - 1)) * (w - 2) + 1).toFixed(1)},${(h - 1.5 - ((p.v - min) / range) * (h - 3)).toFixed(1)}`).join(" ");
  const secs = Math.round((a[a.length - 1].t - a[0].t) / 1000);
  const tip = `Output tok/s over the last ~${fmtAge(secs)}\nnow ${fmtTps(a[a.length - 1].v)} · peak ${fmtTps(max)}`;
  return `<svg class="spark${idle ? " flat" : ""}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><title>${esc(tip)}</title><polyline points="${pts}"/></svg>`;
}

function metricsHtml(r) {
  const m = r.metrics;
  if (!m || !RUNNING.includes(r.status)) return "";
  const parts = [];
  if (m.prompt_tps != null) parts.push(`<span title="Prompt (input) throughput">${fmtTps(m.prompt_tps)} tok/s in</span>`);
  if (m.gen_tps != null) {
    const per = m.running > 1 && m.gen_tps ? ` (≈${fmtTps(m.gen_tps / m.running)}/req)` : "";
    parts.push(`<span title="Generation (output) throughput">${fmtTps(m.gen_tps)} tok/s out${per}</span>`);
  }
  if (m.running != null) parts.push(`<span title="Requests running / waiting">${m.running} req${m.waiting ? ` · +${m.waiting} queued` : ""}</span>`);
  const kvCls = m.kv_cache > 85 ? " hot" : m.kv_cache > 60 ? " warm" : "";
  if (m.kv_cache != null) parts.push(`<span class="kv${kvCls}" title="KV cache usage">${m.kv_cache}% KV</span>`);
  if (m.prefix_hit != null) parts.push(`<span title="Prefix cache hit rate">${m.prefix_hit}% hit</span>`);
  if (!parts.length) return "";
  const stale = m.source === "log" && m.age_s != null && m.age_s > STALE_LOG_S;
  if (stale) parts.push(`<span>${fmtAge(m.age_s)} ago</span>`);
  const title = m.source === "metrics" ? "Live from the server's /metrics, over the last poll interval"
    : `From the server's last stats line in the log${m.age_s != null ? ` (${fmtAge(m.age_s)} ago)` : ""}`;
  const idle = !(m.running > 0) && !(m.waiting > 0) && !(m.prompt_tps > 0) && !(m.gen_tps > 0) && !(m.kv_cache > 0);
  // While idle the zeros are noise: show one calm line; the sparkline still reveals the last activity.
  const body = idle
    ? `<span class="idle-word">idle</span>${m.kv_cache != null ? ` · <span class="kv${kvCls}">${m.kv_cache}% KV</span>` : ""}`
    : parts.join(" · ");
  return `<div class="card-metrics mono${stale ? " stale" : ""}${idle ? " idle" : ""}" title="${title}">${idle ? "💤" : "📈"} ${body}${sparkline(r.id, idle)}</div>`;
}

function fmtAge(s) { return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`; }

function cardHtml(r) {
  const busy = state.busy.has(r.id);
  const running = RUNNING.includes(r.status);
  const fw = state.meta?.frameworks[r.framework]?.label ?? r.framework;
  const url = `http://${endpointHost(r.host_id)}:${r.host_port}`;
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  // Optional startup estimate: median of the recent measured trigger→ready times.
  const hist = state.startTimes[r.id] || [];
  const med = hist.length ? median(hist.slice(0, 8)) : null;
  const booting = !busy && (r.status === "starting" || r.status === "loading");
  let startInfo = "";
  if (booting) {
    const elapsed = (Date.now() - (startWatch.get(r.id) ?? Date.now())) / 1000;
    startInfo = `<span class="start-live mono" title="Time since the start was triggered">⏳ ${fmtDur(elapsed)}</span>`
      + (med ? `<span class="start-hint mono" title="Median time to ready over the last ${Math.min(hist.length, 8)} start${hist.length > 1 ? "s" : ""}">/ ~${fmtDur(med)}</span>`
        + `<span class="bar eta-bar" title="Elapsed vs. typical startup time"><span class="${elapsed > med * 1.5 ? "bar-fill warn" : "bar-fill"}" style="width:${Math.min(100, Math.round(elapsed / med * 100))}%"></span></span>` : "");
  } else if (med && !running) {
    startInfo = `<span class="start-hint mono" title="Median time from Start to ready (last ${Math.min(hist.length, 8)} start${hist.length > 1 ? "s" : ""})">⏱ ~${fmtDur(med)}</span>`;
  }
  return `
      <article class="card is-${r.status}" data-id="${r.id}" data-color="${esc(r.color || "")}" tabindex="0">
        <div class="card-head">
          <div class="card-title">${esc(r.name)}</div>
          <div class="card-head-right">
            <button class="btn small ghost pin${r.pinned ? " on" : ""}" data-act="pin" title="${r.pinned ? "Unpin: back to normal sort" : "Pin: always keep this recipe on top"}">${r.pinned ? "★" : "☆"}</button>
            <span class="badge ${esc(r.framework)}">${esc(fw)}</span>
          </div>
        </div>
        ${multiHost() ? `<div class="card-host" title="Runs on this host">⌂ ${esc(maskAddr(hostName(r.host_id)))}</div>` : ""}
        <div class="status-row"><span class="pill ${busy ? "busy" : r.status}">${busy ? "Working…" : STATUS_LABEL[r.status] ?? r.status}</span>${startInfo}</div>
        ${r.tags?.length ? `<div class="card-tags">${r.tags.map((t) =>
          `<button class="chip small ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}" title="Filter by this tag">${esc(t)}</button>`).join("")}</div>` : ""}
        ${r.description ? `<div class="card-desc">${esc(r.description)}</div>` : ""}
        <dl class="kv">
          <dt>Model</dt><dd>${esc(r.model || "–")}</dd>
          <dt>Endpoint</dt><dd>${running ? `<a href="${esc(url)}/v1/models" target="_blank" rel="noopener">${esc(maskEndpoint(url))}/v1</a>` : `<span class="muted">${esc(maskEndpoint(url))}/v1</span>`}</dd>
          <dt>${r.runtime === "process" ? "Name" : "Container"}</dt><dd>${esc(r.container_name)}</dd>
          ${r.est_memory_gb ? `<dt>Est. memory</dt><dd>${r.est_memory_gb} GB</dd>` : ""}
          ${r.container ? `<dt>${r.runtime === "process" ? "Process" : "Docker"}</dt><dd>${esc(r.container.status)}</dd>` : ""}
        </dl>
        ${metricsHtml(r)}
        <div class="actions">
          ${running
            ? `<button class="btn small stop" data-act="stop" ${busy ? "disabled" : ""}>Stop</button>
               <button class="btn small" data-act="restart" ${busy ? "disabled" : ""}>Restart</button>`
            : `<button class="btn small go" data-act="start" ${busy ? "disabled" : ""}>Start</button>
               <button class="btn small" data-act="switch" ${busy ? "disabled" : ""} title="Stop all other models on this host, free its memory, then start this one">Switch to</button>`}
          <button class="btn small ghost" data-act="logs" ${r.container ? "" : "disabled"}>Logs</button>
          <button class="btn small ghost" data-act="edit">Edit</button>
          <button class="btn small ghost" data-act="duplicate">Duplicate</button>
          <button class="btn small ghost danger" data-act="delete" ${running ? "disabled" : ""}>Delete</button>
        </div>
      </article>`;
}

function renderRecipes() {
  renderToolbar();
  if (!state.recipes.length) {
    $("#recipes").innerHTML = `<div class="empty">No recipes yet. Click “New recipe”.</div>`;
    return;
  }
  const list = visibleRecipes();
  if (!list.length) {
    $("#recipes").innerHTML = `<div class="empty">No recipe matches the current filter.</div>`;
    return;
  }
  if (!view.group) {
    $("#recipes").innerHTML = `<div class="grid">${list.map(cardHtml).join("")}</div>`;
    return;
  }
  // Group by host, or by each recipe's first tag (e.g. the model name); every recipe appears once.
  const groups = new Map();
  for (const r of list) {
    const label = view.group === "host" ? maskAddr(hostName(r.host_id)) : r.tags?.[0] ?? "";
    const key = view.group === "host" ? String(state.hosts.findIndex((h) => h.id === r.host_id)).padStart(3, "0") : label.toLowerCase();
    if (!groups.has(key)) groups.set(key, { label, items: [] });
    groups.get(key).items.push(r);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b, undefined, { numeric: true }));
  $("#recipes").innerHTML = keys.map((k) => {
    const g = groups.get(k);
    const running = g.items.filter((r) => RUNNING.includes(r.status)).length;
    const collapsed = !!view.collapsed[`${view.group}:${k}`];
    return `<section class="group${collapsed ? " is-collapsed" : ""}" data-gkey="${esc(k)}">
        <h3 class="group-title" title="Click to ${collapsed ? "expand" : "collapse"}"><span class="group-caret">${collapsed ? "▸" : "▾"}</span>${g.label ? esc(g.label) : "Untagged"} <span>${g.items.length} recipe${g.items.length === 1 ? "" : "s"}${running ? ` · ${running} running` : ""}</span></h3>
        <div class="grid">${g.items.map(cardHtml).join("")}</div>
      </section>`;
  }).join("");
}

function toggleTagFilter(tag) {
  if (!tag) view.tags = [];
  else if (view.tags.some((t) => t.toLowerCase() === tag.toLowerCase())) view.tags = view.tags.filter((t) => t.toLowerCase() !== tag.toLowerCase());
  else view.tags = [...view.tags, tag];
  saveView();
  renderRecipes();
}

$("#tag-filter").addEventListener("click", (ev) => {
  const chip = ev.target.closest("[data-tag]");
  if (chip) toggleTagFilter(chip.dataset.tag);
});
$("#recipe-search").addEventListener("input", (ev) => { view.search = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-sort").addEventListener("change", (ev) => { view.sort = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-group").addEventListener("change", (ev) => { view.group = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-host").addEventListener("change", (ev) => { view.host = ev.target.value; saveView(); renderRecipes(); });
$("#recipe-search").value = view.search;
$("#recipe-sort").value = SORTS[view.sort] ? view.sort : "name";
$("#recipe-group").value = ["host", "tag"].includes(view.group) ? view.group : "";

async function runAction(key, path, okMsg) {
  state.busy.add(key);
  renderRecipes();
  try {
    const res = await api(path, { method: "POST" });
    toast(okMsg ?? res?.message ?? "Done", "ok");
    return true;
  } catch (e) {
    toast(e.message, "bad");
    return e;
  } finally {
    state.busy.delete(key);
    await loadRecipes();
    loadHosts();
  }
}

const RECIPE_BODY_FIELDS = ["name", "description", "host_id", "runtime", "framework", "image", "model",
  "container_name", "host_port", "container_port", "command", "args", "env", "volumes", "docker_args",
  "gpus", "health_path", "est_memory_gb", "tags", "color"];
function recipeBodyFrom(r, overrides = {}) {
  const body = { pinned: !!r.pinned };
  for (const f of RECIPE_BODY_FIELDS) body[f] = r[f];
  return { ...body, ...overrides };
}

$("#recipes").addEventListener("click", async (ev) => {
  const head = ev.target.closest(".group-title");
  if (head) {
    const key = head.closest(".group").dataset.gkey;
    if (ev.shiftKey) {
      // Shift+click: collapse every group, or expand them all when this one is collapsed.
      const anyOpen = $$(".group", $("#recipes")).some((s) => !s.classList.contains("is-collapsed"));
      for (const s of $$(".group", $("#recipes"))) {
        view.collapsed[`${view.group}:${s.dataset.gkey}`] = anyOpen;
      }
    } else {
      const id = `${view.group}:${key}`;
      view.collapsed[id] = !view.collapsed[id];
    }
    saveView();
    renderRecipes();
    return;
  }
  const chip = ev.target.closest(".card [data-tag]");
  if (chip) { toggleTagFilter(chip.dataset.tag); return; }
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.closest(".card").dataset.id);
  const r = state.recipes.find((x) => x.id === id);
  const act = btn.dataset.act;
  if (act === "start") {
    const res = await runAction(id, `/api/recipes/${id}/start`);
    if (res instanceof Error && /memory/i.test(res.message) && confirm(`${res.message}\n\nStart anyway?`)) {
      await runAction(id, `/api/recipes/${id}/start?force=true`);
    }
  } else if (act === "stop") {
    await runAction(id, `/api/recipes/${id}/stop`);
  } else if (act === "restart") {
    await runAction(id, `/api/recipes/${id}/restart`);
  } else if (act === "switch") {
    if (confirm(`Stop all other models, free memory and start “${r.name}”?`)) await runAction(id, `/api/recipes/${id}/switch`);
  } else if (act === "logs") {
    openLogs(r);
  } else if (act === "edit") {
    openRecipeEditor(r);
  } else if (act === "duplicate") {
    try {
      const copy = await api(`/api/recipes/${id}/duplicate`, { method: "POST" });
      await loadRecipes();
      openRecipeEditor(copy);
    } catch (e) { toast(e.message, "bad"); }
  } else if (act === "pin") {
    try {
      await api(`/api/recipes/${id}`, { method: "PUT", body: recipeBodyFrom(r, { pinned: !r.pinned }) });
      await loadRecipes();
    } catch (e) { toast(e.message, "bad"); }
  } else if (act === "delete") {
    if (!confirm(`Delete recipe “${r.name}”? Schedules using it are deleted too.`)) return;
    try {
      await api(`/api/recipes/${id}`, { method: "DELETE" });
      toast("Recipe deleted", "ok");
      loadRecipes();
    } catch (e) { toast(e.message, "bad"); }
  }
});

/* ---------------- recipe editor ---------------- */

const recipeForm = $("#form-recipe");
let editingRecipe = null;
let previewTimer = null;

function recipeFromForm() {
  const f = new FormData(recipeForm);
  const data = Object.fromEntries(f.entries());
  data.host_port = Number(data.host_port) || 0;
  data.container_port = Number(data.container_port) || 0;
  data.est_memory_gb = data.est_memory_gb === "" ? null : Number(data.est_memory_gb);
  data.host_id = data.host_id ? Number(data.host_id) : null;
  data.tags = (data.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  return data;
}

function fillRecipeForm(r) {
  // Runtime first: it decides which frameworks are selectable.
  if (r.runtime) {
    recipeForm.elements.runtime.value = r.runtime;
    renderFrameworkOptions(r.runtime);
  }
  for (const el of recipeForm.elements) {
    if (el.name && el.name in r) el.value = Array.isArray(r[el.name]) ? r[el.name].join(", ") : r[el.name] ?? "";
  }
  syncRuntime();
  markSwatch();
}

function renderFrameworkOptions(runtime) {
  const sel = $("#sel-framework");
  const current = sel.value;
  sel.innerHTML = Object.entries(state.meta.frameworks)
    .filter(([, v]) => v.runtime === runtime)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

// Native processes run directly on the host: no image, volumes, docker flags or second port.
function syncRuntime() {
  const native = recipeForm.elements.runtime.value === "process";
  $$(".docker-only", recipeForm).forEach((el) => el.classList.toggle("hidden", native));
  $("#name-label").textContent = native ? "Process name" : "Container name";
  $("#port-label").textContent = native ? "Port" : "Host port";
  $("#model-hint").textContent = native ? "HF id or local path (~ allowed)" : "HF id or path inside the container";
  if (native) recipeForm.elements.container_port.value = recipeForm.elements.host_port.value;
}

function markSwatch() {
  const current = recipeForm.elements.color.value;
  $$("#swatches .swatch").forEach((b) => b.classList.toggle("on", b.dataset.color === current));
}

$("#swatches").addEventListener("click", (ev) => {
  const b = ev.target.closest(".swatch");
  if (!b) return;
  recipeForm.elements.color.value = b.dataset.color;
  markSwatch();
});

function applyFramework(name, overwriteAll) {
  const p = state.meta.frameworks[name];
  recipeForm.elements.runtime.value = p.runtime;
  renderFrameworkOptions(p.runtime);
  recipeForm.elements.framework.value = name;
  const cur = recipeFromForm();
  const set = (k, v) => { if (overwriteAll || !cur[k]) recipeForm.elements[k].value = v ?? ""; };
  ["image", "command", "container_port", "health_path"].forEach((k) => { recipeForm.elements[k].value = p[k] ?? ""; });
  ["args", "docker_args", "volumes", "env"].forEach((k) => set(k, p[k]));
  syncRuntime();
}

// Ports are per host: the same port may be used on two different machines.
function recipesOnPort(hostId, port, exceptId) {
  return state.recipes.filter((r) => r.host_id === Number(hostId) && r.host_port === Number(port) && r.id !== exceptId);
}

function nextFreePort(hostId) {
  const used = new Set(state.recipes.filter((r) => r.host_id === Number(hostId)).map((r) => r.host_port));
  let p = 8100;
  while (used.has(p)) p++;
  return p;
}

function updatePortHint() {
  const f = recipeForm.elements;
  const hint = $("#port-hint");
  const port = Number(f.host_port.value);
  if (!port) { hint.textContent = ""; hint.className = "port-hint"; return; }
  const others = recipesOnPort(f.host_id.value, port, editingRecipe?.id);
  const where = multiHost() ? ` on ${maskAddr(hostName(f.host_id.value))}` : "";
  if (!others.length) {
    hint.textContent = `No other recipe uses port ${port}${where}.`;
    hint.className = "port-hint ok";
    return;
  }
  const running = others.filter((r) => RUNNING.includes(r.status));
  hint.textContent = running.length
    ? `Port ${port}${where} is in use by running “${running.map((r) => r.name).join("”, “")}” – this recipe can't start while it runs.`
    : `Also used by “${others.map((r) => r.name).join("”, “")}”${where} – only one of them can run at a time.`;
  hint.className = `port-hint ${running.length ? "bad" : "warn"}`;
}

$("#btn-next-port").addEventListener("click", () => {
  recipeForm.elements.host_port.value = nextFreePort(recipeForm.elements.host_id.value);
  recipeForm.dispatchEvent(new Event("input"));
});

function endpointUrl(r) { return `http://${endpointHost(r.host_id)}:${r.host_port}/v1`; }

function renderPorts() {
  const order = (r) => state.hosts.findIndex((h) => h.id === r.host_id);
  const rows = [...state.recipes].sort((a, b) => order(a) - order(b) || a.host_port - b.host_port || byName(a, b));
  $("#ports").innerHTML = rows.length ? rows.map((r) => {
    const shared = recipesOnPort(r.host_id, r.host_port, r.id);
    const running = RUNNING.includes(r.status);
    return `<tr>
      <td>${esc(maskAddr(hostName(r.host_id)))}</td>
      <td class="mono">${r.host_port}${shared.length ? ` <span class="chip small warn-chip" title="Also used by: ${esc(shared.map((x) => x.name).join(", "))}">shared</span>` : ""}</td>
      <td>${esc(r.name)}</td>
      <td><span class="pill ${r.status}">${STATUS_LABEL[r.status] ?? r.status}</span></td>
      <td class="mono">${running ? `<a href="${esc(endpointUrl(r))}/models" target="_blank" rel="noopener">${esc(maskEndpoint(endpointUrl(r)))}</a>` : `<span class="muted">${esc(maskEndpoint(endpointUrl(r)))}</span>`}
        <button class="btn small ghost copy" data-copy="${esc(endpointUrl(r))}" title="Copy endpoint">Copy</button></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">No recipes yet.</td></tr>`;
}

$("#ports").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-copy]");
  if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); toast("Endpoint copied", "ok"); }
  catch { prompt("Copy the endpoint:", b.dataset.copy); }
});

function openRecipeEditor(r) {
  editingRecipe = r;
  recipeForm.reset();
  $("#sel-host").innerHTML = state.hosts.map((h) =>
    `<option value="${h.id}">${esc(maskAddr(h.name))}${h.info ? ` (${esc(PLATFORM[h.info.platform] ?? h.info.platform)})` : h.online ? "" : " (offline)"}</option>`).join("");
  $("#sel-host").closest("label").classList.toggle("hidden", !multiHost());
  $("#recipe-error").textContent = "";
  showRecipeNote([]);
  recipeForm.elements.color.value = "";
  markSwatch();
  $("#recipe-title").textContent = r ? `Edit recipe – ${r.name}` : "New recipe";
  if (r) {
    fillRecipeForm(r);
  } else {
    const hostId = view.host || state.hosts.find((h) => h.is_local)?.id;
    recipeForm.elements.host_id.value = hostId ?? "";
    // On a Mac, containers have no GPU access, so native MLX is the sensible default.
    const mac = hostPlatform(hostId) === "darwin";
    recipeForm.elements.gpus.value = mac ? "" : "all";
    recipeForm.elements.host_port.value = nextFreePort(recipeForm.elements.host_id.value);
    applyFramework(mac ? "mlx" : "vllm", true);
  }
  syncHostNetworkPorts();
  updatePortHint();
  updatePreview();
  $("#dlg-recipe").showModal();
}

$("#btn-new-recipe").addEventListener("click", () => openRecipeEditor(null));

$("#sel-host").addEventListener("change", (e) => {
  updatePortHint();
  // New recipe moved to a Mac: switch to native MLX (Docker on macOS has no GPU access).
  if (!editingRecipe && hostPlatform(e.target.value) === "darwin" && recipeForm.elements.runtime.value === "docker") {
    recipeForm.elements.gpus.value = "";
    applyFramework("mlx", true);
    updatePreview();
  }
});

$("#sel-runtime").addEventListener("change", (e) => {
  const first = Object.entries(state.meta.frameworks).find(([, v]) => v.runtime === e.target.value)[0];
  const overwrite = !editingRecipe || confirm("Replace arguments and env with this framework's defaults too?");
  applyFramework(first, overwrite);
  updatePreview();
});

$("#sel-framework").addEventListener("change", (e) => {
  const overwrite = !editingRecipe || confirm("Replace arguments, flags, volumes and env with this framework's defaults too?");
  applyFramework(e.target.value, overwrite);
  updatePreview();
});

// With --network host the server listens on the host port directly; keep the two ports equal.
function syncHostNetworkPorts() {
  const f = recipeForm.elements;
  const hostNet = f.runtime.value === "process" || /(^|\s)--(network|net)(=|\s+)host(\s|$)/.test(f.docker_args.value);
  f.container_port.readOnly = hostNet;
  f.container_port.title = hostNet ? "Same as the host port because of --network host" : "";
  if (hostNet) f.container_port.value = f.host_port.value;
}

recipeForm.addEventListener("input", () => {
  syncHostNetworkPorts();
  updatePortHint();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 250);
});

async function updatePreview() {
  try {
    const res = await api("/api/recipes/preview", { method: "POST", body: recipeFromForm() });
    $("#recipe-preview").textContent = res.command.replace(/ (--?[a-zA-Z])/g, " \\\n  $1");
  } catch (e) {
    $("#recipe-preview").textContent = `(incomplete) ${e.message}`;
  }
}

recipeForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = recipeFromForm();
  try {
    let saved;
    if (editingRecipe) {
      saved = await api(`/api/recipes/${editingRecipe.id}`, { method: "PUT", body });
    } else {
      saved = await api("/api/recipes", { method: "POST", body });
    }
    $("#dlg-recipe").close();
    toast("Recipe saved", "ok");
    const wasRunning = editingRecipe && ["ready", "loading", "starting"].includes(editingRecipe.status);
    if (wasRunning && confirm("This model is running. Restart it now so the changes take effect?")) {
      await runAction(saved.id, `/api/recipes/${saved.id}/restart`);
    } else {
      loadRecipes();
    }
  } catch (e) {
    $("#recipe-error").textContent = e.message;
  }
});

/* ---------------- import ---------------- */

function showRecipeNote(lines) {
  const note = $("#recipe-note");
  note.textContent = lines.join("\n");
  note.classList.toggle("hidden", !lines.length);
}

function resetImportReport() {
  $("#import-report").classList.add("hidden");
  $("#import-trust-row").classList.add("hidden");
  $("#import-trust").checked = false;
  $("#import-error").textContent = "";
}

function renderImportReport(check) {
  const box = $("#import-report");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="report-head ${check.count ? "bad" : "ok"}">${check.count
      ? `${check.count} risk${check.count === 1 ? "" : "s"} found`
      : "No risky calls found."}</div>` +
    check.findings.map((f) => `<div class="finding">
        <span class="ln">line ${f.line}</span>
        <span class="msg">${esc(f.message)}</span>
        <code>${esc(f.code)}</code>
      </div>`).join("");
  $("#import-trust-row").classList.toggle("hidden", !check.count);
}

async function runImportCheck() {
  const check = await api("/api/recipes/import/check", { method: "POST", body: { text: $("#import-text").value } });
  renderImportReport(check);
  return check;
}

$("#btn-import").addEventListener("click", () => {
  resetImportReport();
  $("#dlg-import").showModal();
  $("#import-text").focus();
});

$("#import-text").addEventListener("input", resetImportReport);

$("#btn-import-check").addEventListener("click", async () => {
  try { await runImportCheck(); } catch (e) { $("#import-error").textContent = e.message; }
});

$("#form-import").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#btn-import-go");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const check = await runImportCheck();
    const trust = $("#import-trust").checked;
    if (check.count && !trust) {
      $("#import-error").textContent = "Review the risks above and tick the box to import anyway.";
      return;
    }
    btn.textContent = "Importing…";
    const res = await api("/api/recipes/import", { method: "POST", body: { text: $("#import-text").value, trust } });
    const r = res.recipe;
    if (editingRecipe) {
      // Re-importing into an existing recipe keeps its identity.
      r.name = editingRecipe.name;
      r.container_name = editingRecipe.container_name;
      r.description = r.description || editingRecipe.description;
      r.est_memory_gb = editingRecipe.est_memory_gb;
      r.color = editingRecipe.color;
    }
    // Scripts carry no tags; keep whatever the form already has.
    if (!r.tags?.length) r.tags = recipeFromForm().tags;
    fillRecipeForm(r);
    syncHostNetworkPorts();
    const skipped = res.skipped_calls || [];
    const riskCount = res.check?.count ?? 0;
    showRecipeNote([
      "Imported – review the fields and the command preview, then save.",
      ...res.warnings.map((w) => `• ${w}`),
      ...(riskCount ? [`• Script check: ${riskCount} risk${riskCount === 1 ? "" : "s"} (confirmed)`] : []),
      ...(skipped.length ? [`• Not executed during import (${skipped.length}):`,
        ...skipped.slice(0, 12).map((s) => `    ${s}`), ...(skipped.length > 12 ? [`    … ${skipped.length - 12} more`] : [])] : []),
    ]);
    $("#dlg-import").close();
    updatePreview();
  } catch (e) {
    $("#import-error").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Import";
  }
});

/* ---------------- logs ---------------- */

let logSource = null;
let logLines = [];
const MAX_LOG_LINES = 20000;

function logClass(line) {
  if (/\b(ERROR|CRITICAL|Traceback|Exception)\b/.test(line)) return "err";
  if (/\bWARN(ING)?\b/.test(line)) return "warn";
  return "";
}

function renderLogs() {
  const filter = $("#logs-filter").value.toLowerCase();
  const lines = filter ? logLines.filter((l) => l.toLowerCase().includes(filter)) : logLines;
  $("#logs").innerHTML = lines.map((l) => {
    const c = logClass(l);
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  if ($("#logs-follow").checked) $("#logs").scrollTop = $("#logs").scrollHeight;
}

let renderPending = false;
function scheduleLogRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderLogs(); });
}

function openLogs(r) {
  closeLogStream();
  logLines = [];
  $("#logs-title").textContent = `Logs – ${r.name}`;
  $("#logs").textContent = "";
  $("#logs-state").textContent = "connecting";
  $("#logs-state").className = "pill loading";
  $("#dlg-logs").showModal();
  $("#dlg-logs").dataset.name = r.container_name;
  logSource = new EventSource(`/api/recipes/${r.id}/logs/stream?tail=1000`);
  logSource.onopen = () => { $("#logs-state").textContent = "live"; $("#logs-state").className = "pill ready"; };
  logSource.onmessage = (e) => {
    logLines.push(e.data);
    if (logLines.length > MAX_LOG_LINES) logLines.splice(0, logLines.length - MAX_LOG_LINES);
    scheduleLogRender();
  };
  logSource.addEventListener("end", () => {
    $("#logs-state").textContent = "container stopped";
    $("#logs-state").className = "pill";
    closeLogStream();
  });
  logSource.onerror = () => {
    if (!logSource) return;
    $("#logs-state").textContent = "disconnected";
    $("#logs-state").className = "pill failed";
    closeLogStream();
  };
}

function closeLogStream() {
  if (logSource) { logSource.close(); logSource = null; }
}

$("#dlg-logs").addEventListener("close", closeLogStream);
$("#logs-filter").addEventListener("input", renderLogs);
$("#logs-clear").addEventListener("click", () => { logLines = []; renderLogs(); });
$("#logs-download").addEventListener("click", () => {
  const blob = new Blob([logLines.join("\n")], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${$("#dlg-logs").dataset.name || "model"}.log`;
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ---------------- schedules ---------------- */

const scheduleForm = $("#form-schedule");
let editingSchedule = null;

$("#days").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" value="${i}" checked> ${d}</label>`).join("");

function describeCron(cron) {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* ([\d,*-]+)$/.exec(cron || "");
  if (!m) return null;
  const time = `${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")}`;
  let days;
  if (m[3] === "*") days = [0, 1, 2, 3, 4, 5, 6];
  else {
    days = [];
    for (const part of m[3].split(",")) {
      const [a, b] = part.split("-").map(Number);
      if (b === undefined) days.push(a % 7);
      else for (let i = a; i <= b; i++) days.push(i % 7);
    }
  }
  return { time, days: [...new Set(days)].sort() };
}

function cronLabel(cron) {
  const d = describeCron(cron);
  if (!d) return `cron ${cron}`;
  const days = d.days.length === 7 ? "every day" : d.days.join() === "1,2,3,4,5" ? "weekdays" : d.days.map((i) => DAYS[i]).join(", ");
  return `${days} at ${d.time}`;
}

function actionLabel(s) {
  const a = state.meta.actions[s.action]?.label ?? s.action;
  if (!s.recipe_id) return multiHost() ? `${a} – ${s.host_id ? maskAddr(hostName(s.host_id)) : "all hosts"}` : a;
  const r = state.recipes.find((x) => x.id === s.recipe_id);
  const where = multiHost() && r ? ` (${maskAddr(hostName(r.host_id))})` : "";
  return `${a.split(" (")[0]}: ${r ? r.name : `#${s.recipe_id}`}${where}`;
}

async function loadSchedules() {
  try {
    state.schedules = await api("/api/schedules");
  } catch (e) { toast(e.message, "bad"); return; }
  try { state.schedPaused = (await api("/api/settings")).schedules_paused; } catch { /* keep last */ }
  renderSchedPause();
  loadTimeline();
  const tbody = $("#schedules");
  if (!state.schedules.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No schedules yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.schedules.map((s) => `
    <tr data-id="${s.id}">
      <td><input type="checkbox" data-act="toggle" ${s.enabled ? "checked" : ""}></td>
      <td>${esc(s.name)}</td>
      <td>${esc(actionLabel(s))}</td>
      <td>${esc(s.cron ? cronLabel(s.cron) : `once ${fmtTime(s.run_at)}`)}</td>
      <td>${s.enabled ? esc(fmtTime(s.next_run)) : "–"}</td>
      <td class="msg">${s.last_run ? `<span class="${s.last_ok ? "ok" : "bad"}">${s.last_ok ? "✓" : "✗"}</span> ${esc(fmtTime(s.last_run))}<br><small>${esc(s.last_message || "")}</small>` : "–"}</td>
      <td style="white-space:nowrap">
        <button class="btn small ghost" data-act="run">Run now</button>
        <button class="btn small ghost" data-act="duplicate">Duplicate</button>
        <button class="btn small ghost" data-act="edit">Edit</button>
        <button class="btn small ghost danger" data-act="delete">Delete</button>
      </td>
    </tr>`).join("");
}

function scheduleBody(s, overrides = {}) {
  return { name: s.name, action: s.action, recipe_id: s.recipe_id, cron: s.cron, run_at: s.run_at, enabled: !!s.enabled,
           host_id: s.host_id, ...overrides };
}

$("#schedules").addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const id = Number(el.closest("tr").dataset.id);
  const s = state.schedules.find((x) => x.id === id);
  try {
    if (el.dataset.act === "toggle") {
      await api(`/api/schedules/${id}`, { method: "PUT", body: scheduleBody(s, { enabled: el.checked }) });
    } else if (el.dataset.act === "run") {
      if (!confirm(`Run “${s.name}” now?`)) return;
      el.disabled = true;
      const res = await api(`/api/schedules/${id}/run`, { method: "POST" });
      toast(res.message, "ok");
      loadRecipes();
    } else if (el.dataset.act === "duplicate") {
      let i = 1;
      const names = new Set(state.schedules.map((x) => x.name));
      while (names.has(`${s.name} (copy ${i})`)) i += 1;
      await api("/api/schedules", { method: "POST", body: scheduleBody(s, { name: `${s.name} (copy ${i})` }) });
      toast("Schedule duplicated — adjust and save, or just use it as is", "ok");
    } else if (el.dataset.act === "edit") {
      openScheduleEditor(s);
      return;
    } else if (el.dataset.act === "delete") {
      if (!confirm(`Delete schedule “${s.name}”?`)) return;
      await api(`/api/schedules/${id}`, { method: "DELETE" });
    }
  } catch (e) {
    toast(e.message, "bad");
  }
  loadSchedules();
});

/* Master pause for all schedules. */
function renderSchedPause() {
  const b = $("#btn-sched-pause");
  b.textContent = state.schedPaused ? "▶ Resume all" : "⏸ Pause all";
  b.classList.toggle("paused-on", !!state.schedPaused);
  b.title = state.schedPaused ? "Schedules are paused – click to let them fire again" : "Stop all schedules from firing (manual actions keep working)";
  $("#sched-paused-hint").classList.toggle("hidden", !state.schedPaused);
}
$("#btn-sched-pause").addEventListener("click", async () => {
  try {
    const res = await api("/api/settings", { method: "PUT", body: { schedules_paused: !state.schedPaused } });
    state.schedPaused = res.schedules_paused;
    toast(state.schedPaused ? "All schedules paused" : "All schedules resumed", "ok");
    renderSchedPause();
    loadSchedules();
  } catch (e) { toast(e.message, "bad"); }
});

function syncScheduleForm() {
  const needs = state.meta.actions[$("#sel-action").value]?.needs_recipe;
  $("#lbl-recipe").classList.toggle("hidden", !needs);
  $("#lbl-host").classList.toggle("hidden", needs || !multiHost());
  const when = $("#sel-when").value;
  $$(".when", scheduleForm).forEach((el) => el.classList.toggle("hidden", el.dataset.when !== when));
}

function toLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date(Date.now() + 3600e3);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openScheduleEditor(s) {
  editingSchedule = s;
  scheduleForm.reset();
  $("#schedule-error").textContent = "";
  $("#schedule-title").textContent = s ? "Edit schedule" : "New schedule";
  $("#sel-recipe").innerHTML = state.recipes.map((r) =>
    `<option value="${r.id}">${esc(r.name)}${multiHost() ? ` – ${esc(maskAddr(hostName(r.host_id)))}` : ""}</option>`).join("");
  $("#sel-sched-host").innerHTML = `<option value="">All hosts</option>` +
    state.hosts.map((h) => `<option value="${h.id}">${esc(maskAddr(h.name))}</option>`).join("");
  $("#sel-sched-host").value = s?.host_id ?? "";
  $("#in-once").value = toLocalInput(s?.run_at);
  $$("#days input").forEach((c) => { c.checked = true; });
  $("#in-time").value = "22:00";
  $("#in-cron").value = "";
  if (s) {
    scheduleForm.elements.name.value = s.name;
    scheduleForm.elements.action.value = s.action;
    if (s.recipe_id) scheduleForm.elements.recipe_id.value = s.recipe_id;
    scheduleForm.elements.enabled.checked = !!s.enabled;
    if (s.cron) {
      const d = describeCron(s.cron);
      $("#in-cron").value = s.cron;
      if (d) {
        $("#sel-when").value = "weekly";
        $("#in-time").value = d.time;
        $$("#days input").forEach((c) => { c.checked = d.days.includes(Number(c.value)); });
      } else {
        $("#sel-when").value = "cron";
      }
    } else {
      $("#sel-when").value = "once";
    }
  } else {
    $("#sel-when").value = "weekly";
  }
  syncScheduleForm();
  $("#dlg-schedule").showModal();
}

$("#btn-new-schedule").addEventListener("click", () => {
  if (!state.recipes.length) toast("Create a recipe first for start/stop/switch schedules");
  openScheduleEditor(null);
});
$("#sel-action").addEventListener("change", syncScheduleForm);
$("#sel-when").addEventListener("change", syncScheduleForm);

scheduleForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = scheduleForm.elements;
  const body = {
    name: f.name.value,
    action: f.action.value,
    recipe_id: state.meta.actions[f.action.value].needs_recipe ? Number(f.recipe_id.value) : null,
    host_id: !state.meta.actions[f.action.value].needs_recipe && f.host_id.value ? Number(f.host_id.value) : null,
    enabled: f.enabled.checked,
    cron: null,
    run_at: null,
  };
  const when = $("#sel-when").value;
  if (when === "weekly") {
    const [h, m] = $("#in-time").value.split(":").map(Number);
    const days = $$("#days input").filter((c) => c.checked).map((c) => c.value);
    if (!days.length) { $("#schedule-error").textContent = "Pick at least one day"; return; }
    body.cron = `${m} ${h} * * ${days.length === 7 ? "*" : days.join(",")}`;
  } else if (when === "cron") {
    body.cron = $("#in-cron").value;
  } else {
    body.run_at = $("#in-once").value;
  }
  try {
    if (editingSchedule) await api(`/api/schedules/${editingSchedule.id}`, { method: "PUT", body });
    else await api("/api/schedules", { method: "POST", body });
    $("#dlg-schedule").close();
    toast("Schedule saved", "ok");
    loadSchedules();
  } catch (e) {
    $("#schedule-error").textContent = e.message;
  }
});


/* ---------------- schedule timeline ---------------- */

const TL_KEY = "dgxm.timelineHours";
let tlHours = 24;
try { tlHours = Number(localStorage.getItem(TL_KEY)) || 24; } catch { /* storage unavailable */ }
let tlData = null;

const CARD_HEX = {
  "": "#4f8cff", green: "#22c58b", teal: "#2cc4c9", blue: "#4f8cff", purple: "#a77bf3", pink: "#ec6fb4",
  orange: "#f2884b", yellow: "#f0c93a", red: "#ef5b5b", black: "#000000",
};
const ACTION_SHORT = { start: "start", stop: "stop", restart: "restart", switch: "switch to", stop_all: "stop all", free_memory: "free memory" };
const HOUR = 3600e3;

async function loadTimeline() {
  $$("#tl-range button").forEach((b) => b.classList.toggle("on", Number(b.dataset.h) === tlHours));
  try {
    tlData = await api(`/api/schedules/timeline?hours=${tlHours}`);
    renderTimeline();
  } catch (e) {
    $("#timeline").innerHTML = `<div class="empty">Could not load timeline: ${esc(e.message)}</div>`;
  }
}

$("#tl-range").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-h]");
  if (!b) return;
  tlHours = Number(b.dataset.h);
  try { localStorage.setItem(TL_KEY, String(tlHours)); } catch { /* ignore */ }
  loadTimeline();
});

function fmtClock(d) { return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
function fmtDayClock(d) { return d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }); }

function renderTimeline() {
  const el = $("#timeline");
  const tl = tlData;
  if (!tl) return;
  if (!tl.lanes.length) {
    el.innerHTML = `<div class="empty">Nothing planned – no model is running and no enabled schedule targets a model in this period.</div>`;
    return;
  }
  const t0 = new Date(tl.start).getTime();
  const t1 = new Date(tl.end).getTime();
  const width = Math.max(el.clientWidth, 280);
  const labelW = Math.min(200, Math.max(90, Math.round(width * 0.22)));
  const maxChars = Math.max(6, Math.floor((labelW - 14) / 7));
  const padR = 12, axisH = 34, laneH = 34, top = axisH + 4;
  const plotW = width - labelW - padR;
  const height = top + tl.lanes.length * laneH + 6;
  const x = (t) => labelW + ((t - t0) / (t1 - t0)) * plotW;

  // Ticks: smallest step leaving >= 64px between labels.
  const step = [1, 2, 3, 6, 12, 24].find((h) => (h * HOUR / (t1 - t0)) * plotW >= 64) ?? 24;
  const first = new Date(t0);
  first.setMinutes(0, 0, 0);
  const ticks = [];
  for (let t = first.getTime(); t <= t1; t += HOUR) {
    const d = new Date(t);
    if (t >= t0 && d.getHours() % step === 0) ticks.push(d);
  }

  const parts = [];
  // lane backgrounds + labels
  tl.lanes.forEach((lane, i) => {
    const y = top + i * laneH;
    if (i % 2 === 0) parts.push(`<rect class="tl-lane" x="${labelW}" y="${y}" width="${plotW}" height="${laneH}"/>`);
    const full = multiHost() ? `${maskAddr(lane.host)} · ${lane.name}` : lane.name;
    const name = full.length > maxChars ? full.slice(0, maxChars - 1) + "…" : full;
    parts.push(`<text class="tl-label" x="${labelW - 10}" y="${y + laneH / 2}" text-anchor="end" dominant-baseline="central"><title>${esc(full)}</title>${esc(name)}</text>`);
  });
  // grid + axis labels
  for (const d of ticks) {
    const tx = x(d.getTime());
    const midnight = d.getHours() === 0;
    parts.push(`<line class="tl-grid ${midnight ? "day" : ""}" x1="${tx}" x2="${tx}" y1="${axisH - 6}" y2="${height - 6}"/>`);
    const label = midnight || step >= 24 ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "numeric" }) : fmtClock(d);
    if (tx + label.length * 6.5 < width) parts.push(`<text class="tl-axis ${midnight ? "day" : ""}" x="${tx + 4}" y="${axisH - 12}">${esc(label)}</text>`);
  }
  // global events (stop all / free memory)
  for (const e of tl.events.filter((ev) => !ev.recipe_id)) {
    const tx = x(new Date(e.time).getTime());
    const where = multiHost() ? ` (${e.host_id ? maskAddr(hostName(e.host_id)) : "all hosts"})` : "";
    const tip = `${fmtDayClock(new Date(e.time))} · ${e.schedule}: ${ACTION_SHORT[e.action]}${where}`;
    // A host-scoped stop-all only crosses that host's rows.
    tl.lanes.forEach((lane, i) => {
      if (e.host_id && lane.host_id !== e.host_id) return;
      const y = top + i * laneH;
      parts.push(`<g class="tl-hit" data-sid="${e.schedule_id}" data-tip="${esc(tip)}">
        <line class="tl-global" x1="${tx}" x2="${tx}" y1="${y}" y2="${y + laneH}"/>
        <rect x="${tx - 5}" y="${y}" width="10" height="${laneH}" fill="transparent"/></g>`);
    });
  }
  // blocks
  tl.lanes.forEach((lane, i) => {
    const y = top + i * laneH + 6;
    const h = laneH - 12;
    const color = CARD_HEX[lane.color] ?? CARD_HEX[""];
    for (const b of lane.blocks) {
      const s = new Date(b.start), en = new Date(b.end);
      const bx = x(s.getTime()), bw = Math.max(2, x(en.getTime()) - bx - 2);
      const from = b.running_at_start ? "running now" : `${fmtDayClock(s)} (${b.started_by})`;
      const to = b.open_end ? "beyond this view" : `${fmtDayClock(en)} (${b.stopped_by})`;
      const hours = (en - s) / HOUR;
      const tip = `${lane.name}\n${from} → ${to}\n${hours >= 1 ? hours.toFixed(hours < 10 ? 1 : 0) + " h" : Math.round(hours * 60) + " min"}`;
      const text = bw > 96 ? `${b.running_at_start ? "now" : fmtClock(s)} – ${b.open_end ? "…" : fmtClock(en)}` : "";
      const sid = b.started_by_id ?? b.stopped_by_id ?? "";
      parts.push(`<g class="tl-hit" data-sid="${sid}" data-tip="${esc(tip)}">
        <rect class="tl-block ${lane.color === "black" ? "black" : ""}" x="${bx}" y="${y}" width="${bw}" height="${h}" rx="4" fill="${color}"/>
        ${text ? `<text class="tl-block-text" x="${bx + 8}" y="${y + h / 2}" dominant-baseline="central">${esc(text)}</text>` : ""}
        ${b.open_end ? `<path class="tl-open" d="M${bx + bw - 7} ${y + 4} l5 ${h / 2 - 4} l-5 ${h / 2 - 4}"/>` : ""}
      </g>`);
    }
  });
  // per-model event markers
  for (const e of tl.events.filter((ev) => ev.recipe_id)) {
    const i = tl.lanes.findIndex((l) => l.recipe_id === e.recipe_id);
    if (i < 0) continue;
    const tx = x(new Date(e.time).getTime());
    const y = top + i * laneH + 1;
    const tip = `${fmtDayClock(new Date(e.time))} · ${e.schedule}: ${ACTION_SHORT[e.action]} ${tl.lanes[i].name}`;
    parts.push(`<g class="tl-hit" data-sid="${e.schedule_id}" data-tip="${esc(tip)}">
      <path class="tl-mark" d="M${tx - 5} ${y} h10 l-5 6 z"/><rect x="${tx - 7}" y="${y - 2}" width="14" height="12" fill="transparent"/></g>`);
  }
  // now
  parts.push(`<text class="tl-now" x="${labelW + 2}" y="${height - 1}">now</text>`);

  el.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="Planned model runs over the next ${tlHours} hours">${parts.join("")}</svg>`;
}

const tip = $("#tl-tip");
$("#timeline").addEventListener("mousemove", (ev) => {
  const g = ev.target.closest("[data-tip]");
  if (!g) { tip.classList.add("hidden"); return; }
  tip.textContent = g.dataset.tip;
  tip.classList.remove("hidden");
  const pad = 14;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = ev.clientX + pad, topPos = ev.clientY + pad;
  if (left + w > window.innerWidth - 8) left = ev.clientX - w - pad;
  if (topPos + h > window.innerHeight - 8) topPos = ev.clientY - h - pad;
  tip.style.left = `${left}px`;
  tip.style.top = `${topPos}px`;
});
$("#timeline").addEventListener("mouseleave", () => tip.classList.add("hidden"));
$("#timeline").addEventListener("click", (ev) => {
  const g = ev.target.closest("[data-sid]");
  const s = g && state.schedules.find((x) => x.id === Number(g.dataset.sid));
  if (s) { tip.classList.add("hidden"); openScheduleEditor(s); }
});
let tlResize = null;
window.addEventListener("resize", () => { clearTimeout(tlResize); tlResize = setTimeout(renderTimeline, 150); });

/* ---------------- activity ---------------- */

async function loadEvents() {
  try {
    const events = await api("/api/events?limit=300");
    $("#events").innerHTML = events.length ? events.map((e) => `
      <tr>
        <td style="white-space:nowrap">${esc(fmtTime(e.ts))}</td>
        <td>${esc(e.source)}</td>
        <td>${esc(e.action)}</td>
        <td>${esc(blurText(e.target || ""))}</td>
        <td class="msg"><span class="${e.ok ? "ok" : "bad"}">${e.ok ? "✓" : "✗"}</span> ${esc(blurText(e.message))}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="empty">Nothing yet.</td></tr>`;
  } catch (e) { toast(e.message, "bad"); }
}
$("#btn-refresh-events").addEventListener("click", loadEvents);

/* ---------------- images tab ---------------- */

let imagesData = null;

function imageAge(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "age unknown";
  const days = Math.floor((Date.now() - d) / 86400e3);
  return days < 1 ? "built today" : days === 1 ? "1 day old" : `${days} days old`;
}

async function loadImages() {
  const tbody = $("#images");
  if (!imagesData) tbody.innerHTML = `<tr><td colspan="3" class="empty">Asking the hosts…</td></tr>`;
  try {
    imagesData = await api("/api/images");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  renderImages();
}

function imageHostHtml(img, h) {
  const name = multiHost() ? `<b>${esc(maskAddr(hostName(h.host_id)))}</b> ` : "";
  if (h.state === "unknown") return `<div class="img-host">${name}<span class="muted">${esc(blurHostText(h.error || "not reachable", hostById(h.host_id)?.url))}</span></div>`;
  const current = h.state === "present"
    ? `<span class="ok">✓ available</span> <span class="muted">· ${esc(imageAge(h.local.created))} · ${esc(h.local.size)} · ${esc(h.local.id)}</span>`
    : `<span class="bad">✗ not on this host</span> <span class="muted">· pulled on the first start</span>`;
  const tags = h.other_tags.map((t, i) => {
    const title = `${t.repository}:${t.tag} · ${t.size} · created ${fmtTime(t.created)}`;
    return t.newer
      ? `<button class="chip small newer-chip" data-img="${esc(img.repository)}:${esc(img.tag)}" data-host="${h.host_id}" data-tag="${i}" title="${esc(title)} – click to switch the recipes to it">⬆ ${esc(t.tag)} · ${esc(imageAge(t.created))}</button>`
      : `<span class="chip small" title="${esc(title)}">${esc(t.tag)} · ${esc(imageAge(t.created))}</span>`;
  }).join(" ");
  return `<div class="img-host">${name}${current}${tags ? `<div class="img-tags"><span class="muted">Other tags here:</span> ${tags}</div>` : ""}</div>`;
}

function renderImages() {
  if (!imagesData) return;
  const n = imagesData.native_recipes;
  $("#images-native").textContent = n ? `${n} native-process recipe${n === 1 ? "" : "s"} run without an image and ${n === 1 ? "is" : "are"} not listed.` : "";
  const tbody = $("#images");
  if (!imagesData.images.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">No Docker recipes yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = imagesData.images.map((img) => {
    const recipes = img.recipes.map((r) => `<button class="chip small" data-recipe="${r.id}" title="Edit this recipe">${esc(r.name)}${multiHost() ? ` <span>⌂ ${esc(maskAddr(hostName(r.host_id)))}</span>` : ""}</button>`).join(" ");
    return `<tr>
      <td class="mono img-ref">${esc(img.repository)}<br><b>:${esc(img.tag)}</b></td>
      <td>${recipes}</td>
      <td>${img.hosts.map((h) => imageHostHtml(img, h)).join("")}</td>
    </tr>`;
  }).join("");
}

$("#btn-refresh-images").addEventListener("click", loadImages);

$("#images").addEventListener("click", async (ev) => {
  const recipeBtn = ev.target.closest("[data-recipe]");
  if (recipeBtn) {
    const r = state.recipes.find((x) => x.id === Number(recipeBtn.dataset.recipe));
    if (r) openRecipeEditor(r);
    return;
  }
  const tagBtn = ev.target.closest(".newer-chip");
  if (!tagBtn) return;
  const img = imagesData.images.find((i) => `${i.repository}:${i.tag}` === tagBtn.dataset.img);
  const hostId = Number(tagBtn.dataset.host);
  const tag = img?.hosts.find((h) => h.host_id === hostId)?.other_tags[Number(tagBtn.dataset.tag)];
  if (!img || !tag) return;
  const newRef = `${tag.repository}:${tag.tag}`;
  const targets = state.recipes.filter((r) => r.host_id === hostId && img.recipes.some((x) => x.id === r.id));
  if (!targets.length) return;
  if (!confirm(`Switch ${targets.map((r) => `"${r.name}"`).join(", ")} to\n${newRef}?\n\nA running model keeps the old image until you restart it.`)) return;
  try {
    for (const r of targets) await api(`/api/recipes/${r.id}`, { method: "PUT", body: recipeBodyFrom(r, { image: newRef }) });
    toast(targets.length === 1 ? `"${targets[0].name}" now uses ${newRef}` : `${targets.length} recipes now use ${newRef}`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
  await loadRecipes();
  loadImages();
});

/* ---------------- keyboard shortcuts ---------------- */

const TAB_KEYS = { "1": "recipes", "2": "schedules", "3": "activity", "4": "hosts", "5": "images" };
const CARD_KEYS = { s: "start", x: "stop", r: "restart", l: "logs", e: "edit" };

document.addEventListener("keydown", (ev) => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const t = ev.target;
  if (t instanceof HTMLElement && (t.isContentEditable || /^(input|textarea|select|button)$/i.test(t.tagName))) return;
  if (document.querySelector("dialog[open]")) return;
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  const card = document.activeElement instanceof Element ? document.activeElement.closest(".card") : null;
  if (card && k.startsWith("Arrow")) {  // move focus between cards
    const cards = $$("#recipes .card");
    const i = cards.indexOf(card);
    const next = (k === "ArrowRight" || k === "ArrowDown") ? cards[i + 1] : cards[i - 1];
    if (next) { next.focus(); ev.preventDefault(); }
    return;
  }
  if (card && CARD_KEYS[k]) {
    const btn = card.querySelector(`button[data-act="${CARD_KEYS[k]}"]:not([disabled])`);
    if (btn) { btn.click(); ev.preventDefault(); }
    return;
  }
  if (TAB_KEYS[k]) { $(`.tab[data-tab="${TAB_KEYS[k]}"]`).click(); ev.preventDefault(); return; }
  if (k === "/") { $('.tab[data-tab="recipes"]').click(); $("#recipe-search").focus(); ev.preventDefault(); return; }
  if (k === "n" && state.tab === "recipes") { $("#btn-new-recipe").click(); ev.preventDefault(); return; }
  if (k === "t") { $("#btn-theme").click(); return; }
  if (k === "b") { $("#btn-blur").click(); return; }
  if (k === "?") { toast("Shortcuts: 1–5 tabs · / search · n new recipe · arrows move between cards · s start, x stop, r restart, l logs, e edit on the focused card · t theme · b blur"); return; }
});

/* ---------------- boot ---------------- */

async function boot() {
  state.meta = await api("/api/meta");
  $("#hub-tz").textContent = state.meta.timezone ? `: ${state.meta.timezone}` : "";
  $("#sel-runtime").innerHTML = Object.entries(state.meta.runtimes)
    .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  renderFrameworkOptions("docker");
  $("#sel-action").innerHTML = Object.entries(state.meta.actions)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  $("#swatches").innerHTML = state.meta.colors.map((c) =>
    `<button type="button" class="swatch" data-color="${c}" title="${c || "default"}" aria-label="${c || "default"} colour"></button>`).join("");
  await loadHosts();
  await loadStartTimes();
  await loadRecipes();
  setInterval(() => { if (!document.hidden) { loadRecipes(); loadHosts(); } }, 5000);
  // Keep the ⏳ elapsed counter ticking between the 5 s polls.
  setInterval(() => { if (!document.hidden && startWatch.size) renderRecipes(); }, 1000);
  setInterval(() => { if (!document.hidden && state.tab === "schedules") loadSchedules(); }, 15000);
}

boot().catch((e) => toast(`Failed to start: ${e.message}`, "bad"));
