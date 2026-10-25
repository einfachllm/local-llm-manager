"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { meta: null, recipes: [], schedules: [], busy: new Set(), tab: "recipes" };
const STATUS_LABEL = { ready: "Ready", loading: "Loading model", starting: "Starting", stopped: "Stopped", failed: "Failed" };
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
}));

$$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

/* ---------------- system ---------------- */

async function loadSystem() {
  try {
    const s = await api("/api/system");
    const m = s.memory;
    const pct = m.total_gb ? (m.used_gb / m.total_gb) * 100 : 0;
    $("#mem-text").textContent = `${m.used_gb} / ${m.total_gb} GB (${m.available_gb} free)`;
    const bar = $("#mem-bar");
    bar.style.width = `${pct.toFixed(1)}%`;
    bar.className = "bar-fill" + (pct > 90 ? " crit" : pct > 75 ? " warn" : "");
    $("#gpu-text").textContent = s.gpus.length
      ? s.gpus.map((g) => `GPU ${g.utilization ?? "?"}% · ${g.temperature ?? "?"}°C · ${g.power_w ?? "?"} W`).join("  ")
      : "GPU n/a";
  } catch { /* keep last values */ }
}

$("#btn-free").addEventListener("click", async () => {
  if (!confirm("Stop ALL running models and drop the page cache?")) return;
  await runAction("free", "/api/system/free-memory");
});

/* ---------------- recipes ---------------- */

async function loadRecipes() {
  try {
    state.recipes = await api("/api/recipes");
    renderRecipes();
  } catch (e) {
    $("#recipes").innerHTML = `<div class="empty">Failed to load recipes: ${esc(e.message)}</div>`;
  }
}

/* ---------------- recipe list: filter, sort, group ---------------- */

const VIEW_KEY = "dgxm.recipeView";
const view = { search: "", tags: [], sort: "name", group: false };
try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); } catch { /* storage unavailable */ }
const saveView = () => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const RUNNING = ["ready", "loading", "starting"];
const STATUS_ORDER = { ready: 0, loading: 1, starting: 2, failed: 3, stopped: 4 };
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
    const tags = (r.tags || []).map((t) => t.toLowerCase());
    if (!want.every((t) => tags.includes(t))) return false;
    if (!q) return true;
    return [r.name, r.model, r.description, r.image, r.container_name, r.framework, ...tags]
      .some((v) => String(v || "").toLowerCase().includes(q));
  }).sort(SORTS[view.sort] || byName);
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

function cardHtml(r) {
  const busy = state.busy.has(r.id);
  const running = RUNNING.includes(r.status);
  const fw = state.meta?.frameworks[r.framework]?.label ?? r.framework;
  const url = `http://${location.hostname}:${r.host_port}`;
  const active = new Set(view.tags.map((t) => t.toLowerCase()));
  return `
      <article class="card is-${r.status}" data-id="${r.id}" data-color="${esc(r.color || "")}">
        <div class="card-head">
          <div class="card-title">${esc(r.name)}</div>
          <span class="badge ${esc(r.framework)}">${esc(fw)}</span>
        </div>
        <div><span class="pill ${busy ? "busy" : r.status}">${busy ? "Working…" : STATUS_LABEL[r.status] ?? r.status}</span></div>
        ${r.tags?.length ? `<div class="card-tags">${r.tags.map((t) =>
          `<button class="chip small ${active.has(t.toLowerCase()) ? "on" : ""}" data-tag="${esc(t)}" title="Filter by this tag">${esc(t)}</button>`).join("")}</div>` : ""}
        ${r.description ? `<div class="card-desc">${esc(r.description)}</div>` : ""}
        <dl class="kv">
          <dt>Model</dt><dd>${esc(r.model || "–")}</dd>
          <dt>Endpoint</dt><dd>${running ? `<a href="${esc(url)}/v1/models" target="_blank" rel="noopener">${esc(url)}/v1</a>` : `:${r.host_port}`}</dd>
          <dt>Container</dt><dd>${esc(r.container_name)}</dd>
          ${r.est_memory_gb ? `<dt>Est. memory</dt><dd>${r.est_memory_gb} GB</dd>` : ""}
          ${r.container ? `<dt>Docker</dt><dd>${esc(r.container.status)}</dd>` : ""}
        </dl>
        <div class="actions">
          ${running
            ? `<button class="btn small stop" data-act="stop" ${busy ? "disabled" : ""}>Stop</button>
               <button class="btn small" data-act="restart" ${busy ? "disabled" : ""}>Restart</button>`
            : `<button class="btn small go" data-act="start" ${busy ? "disabled" : ""}>Start</button>
               <button class="btn small" data-act="switch" ${busy ? "disabled" : ""} title="Stop all other models, free memory, then start this one">Switch to</button>`}
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
  // Group by each recipe's first tag (e.g. the model name), so every recipe appears once.
  const groups = new Map();
  for (const r of list) {
    const t = r.tags?.[0] ?? "";
    const key = t.toLowerCase();
    if (!groups.has(key)) groups.set(key, { label: t, items: [] });
    groups.get(key).items.push(r);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b, undefined, { numeric: true }));
  $("#recipes").innerHTML = keys.map((k) => {
    const g = groups.get(k);
    const running = g.items.filter((r) => RUNNING.includes(r.status)).length;
    return `<section class="group">
        <h3 class="group-title">${k ? esc(g.label) : "Untagged"} <span>${g.items.length} recipe${g.items.length === 1 ? "" : "s"}${running ? ` · ${running} running` : ""}</span></h3>
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
$("#recipe-group").addEventListener("change", (ev) => { view.group = ev.target.checked; saveView(); renderRecipes(); });
$("#recipe-search").value = view.search;
$("#recipe-sort").value = SORTS[view.sort] ? view.sort : "name";
$("#recipe-group").checked = !!view.group;

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
    loadSystem();
  }
}

$("#recipes").addEventListener("click", async (ev) => {
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
  data.tags = (data.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  return data;
}

function fillRecipeForm(r) {
  for (const el of recipeForm.elements) {
    if (el.name && el.name in r) el.value = Array.isArray(r[el.name]) ? r[el.name].join(", ") : r[el.name] ?? "";
  }
  markSwatch();
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
  const cur = recipeFromForm();
  const set = (k, v) => { if (overwriteAll || !cur[k]) recipeForm.elements[k].value = v ?? ""; };
  ["image", "command", "container_port", "health_path"].forEach((k) => { recipeForm.elements[k].value = p[k] ?? ""; });
  ["args", "docker_args", "volumes", "env"].forEach((k) => set(k, p[k]));
}

function nextFreePort() {
  const used = new Set(state.recipes.map((r) => r.host_port));
  let p = 8100;
  while (used.has(p)) p++;
  return p;
}

function openRecipeEditor(r) {
  editingRecipe = r;
  recipeForm.reset();
  $("#recipe-error").textContent = "";
  showRecipeNote([]);
  recipeForm.elements.color.value = "";
  markSwatch();
  $("#recipe-title").textContent = r ? `Edit recipe – ${r.name}` : "New recipe";
  if (r) {
    fillRecipeForm(r);
  } else {
    recipeForm.elements.framework.value = "vllm";
    recipeForm.elements.gpus.value = "all";
    recipeForm.elements.host_port.value = nextFreePort();
    applyFramework("vllm", true);
  }
  syncHostNetworkPorts();
  updatePreview();
  $("#dlg-recipe").showModal();
}

$("#btn-new-recipe").addEventListener("click", () => openRecipeEditor(null));

$("#sel-framework").addEventListener("change", (e) => {
  const overwrite = !editingRecipe || confirm("Replace arguments, flags, volumes and env with this framework's defaults too?");
  applyFramework(e.target.value, overwrite);
  updatePreview();
});

// With --network host the server listens on the host port directly; keep the two ports equal.
function syncHostNetworkPorts() {
  const f = recipeForm.elements;
  const hostNet = /(^|\s)--(network|net)(=|\s+)host(\s|$)/.test(f.docker_args.value);
  f.container_port.readOnly = hostNet;
  f.container_port.title = hostNet ? "Same as the host port because of --network host" : "";
  if (hostNet) f.container_port.value = f.host_port.value;
}

recipeForm.addEventListener("input", () => {
  syncHostNetworkPorts();
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

$("#btn-import").addEventListener("click", () => {
  $("#import-error").textContent = "";
  $("#dlg-import").showModal();
  $("#import-text").focus();
});

$("#form-import").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#btn-import-go");
  btn.disabled = true;
  btn.textContent = "Parsing…";
  try {
    const res = await api("/api/recipes/import", { method: "POST", body: { text: $("#import-text").value } });
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
    showRecipeNote(["Imported – review the fields and the command preview, then save.", ...res.warnings.map((w) => `• ${w}`)]);
    $("#dlg-import").close();
    updatePreview();
  } catch (e) {
    $("#import-error").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Parse into form";
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
  if (!s.recipe_id) return a;
  const r = state.recipes.find((x) => x.id === s.recipe_id);
  return `${a.split(" (")[0]}: ${r ? r.name : `#${s.recipe_id}`}`;
}

async function loadSchedules() {
  try {
    state.schedules = await api("/api/schedules");
  } catch (e) { toast(e.message, "bad"); return; }
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
        <button class="btn small ghost" data-act="edit">Edit</button>
        <button class="btn small ghost danger" data-act="delete">Delete</button>
      </td>
    </tr>`).join("");
}

function scheduleBody(s, overrides = {}) {
  return { name: s.name, action: s.action, recipe_id: s.recipe_id, cron: s.cron, run_at: s.run_at, enabled: !!s.enabled, ...overrides };
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

function syncScheduleForm() {
  const needs = state.meta.actions[$("#sel-action").value]?.needs_recipe;
  $("#lbl-recipe").classList.toggle("hidden", !needs);
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
  $("#sel-recipe").innerHTML = state.recipes.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
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

/* ---------------- activity ---------------- */

async function loadEvents() {
  try {
    const events = await api("/api/events?limit=300");
    $("#events").innerHTML = events.length ? events.map((e) => `
      <tr>
        <td style="white-space:nowrap">${esc(fmtTime(e.ts))}</td>
        <td>${esc(e.source)}</td>
        <td>${esc(e.action)}</td>
        <td>${esc(e.target || "")}</td>
        <td class="msg"><span class="${e.ok ? "ok" : "bad"}">${e.ok ? "✓" : "✗"}</span> ${esc(e.message)}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="empty">Nothing yet.</td></tr>`;
  } catch (e) { toast(e.message, "bad"); }
}
$("#btn-refresh-events").addEventListener("click", loadEvents);

/* ---------------- boot ---------------- */

async function boot() {
  state.meta = await api("/api/meta");
  $("#sel-framework").innerHTML = Object.entries(state.meta.frameworks)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  $("#sel-action").innerHTML = Object.entries(state.meta.actions)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  $("#swatches").innerHTML = state.meta.colors.map((c) =>
    `<button type="button" class="swatch" data-color="${c}" title="${c || "default"}" aria-label="${c || "default"} colour"></button>`).join("");
  await Promise.all([loadRecipes(), loadSystem()]);
  setInterval(() => { if (!document.hidden) { loadRecipes(); loadSystem(); } }, 5000);
  setInterval(() => { if (!document.hidden && state.tab === "schedules") loadSchedules(); }, 15000);
}

boot().catch((e) => toast(`Failed to start: ${e.message}`, "bad"));
