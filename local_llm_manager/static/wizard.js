"use strict";

/* Guided 3-step "New Recipe" wizard.
 *
 * Loads AFTER app.js (classic script; relies on its globals: state, api, toast, esc,
 * loadRecipes, nextFreePort, maskAddr, openImport and, when present, hostById,
 * hostName, hostPlatform, blurHostText, maskEndpoint, previewHtml).
 * Creates its own <dialog id="dlg-wizard"> and appends it to <body>, so index.html
 * only needs the <script>/<link> tags. Entry point: openRecipeWizard().
 *
 * Step 1: kind, name, host, weights (with memory-fit badges) or a typed model id/path.
 * Step 2: engine cards from state.meta.frameworks + port + Advanced preset overrides.
 * Step 3: review + live command preview (POST /api/recipes/preview) + Save.
 */

const wizStepsMeta = [
  { title: "Model & Weights", sub: "What should run" },
  { title: "Engine & Port", sub: "Auto-configured" },
  { title: "Review & Save", sub: "Ready to launch" },
];

const wizKindInfo = {
  model: { label: "Local AI Model", desc: "Serves an OpenAI-compatible API for chat, coding, or apps." },
  agent: { label: "Background Agent", desc: "Stays running even when you switch models (e.g. a gateway)." },
};

// One-line plain-English notes for the engines we know; anything else falls back to its runtime.
const wizEngineBlurb = {
  vllm: "Fast OpenAI-compatible server for NVIDIA GPUs.",
  sglang: "High-throughput serving with RadixAttention caching.",
  llamacpp: "Runs GGUF model files with llama.cpp.",
  tensorfold: "TensorFold engine, run natively on the host.",
  "llamacpp-native": "llama.cpp installed directly on the host.",
  mlx: "Apple-silicon serving with MLX.",
  custom: "Bring your own Docker image and command.",
  "process-custom": "Any command that runs on the host.",
};

let wiz = null;          // wizard model, reset on every open
let wizDlg = null;       // the <dialog>, built once
let wizEls = null;       // element references
let wizAdvEls = null;    // advanced field references
let wizWeightsSeq = 0;   // guards the async weights check
let wizPreviewSeq = 0;   // guards the async command preview
let wizAdvDirty = false; // user edited Advanced fields → engine switch must not clobber them
let wizAdvOpen = false;  // Advanced <details> stays open across re-renders
let wizEnginePicked = false; // user chose an engine explicitly → don't re-derive the default

/* ---------- small helpers (reuse app.js helpers when they exist) ---------- */

const wizHostById = (id) => (typeof hostById === "function" ? hostById(id) : state.hosts.find((h) => h.id === Number(id)));
const wizHostName = (id) => (typeof hostName === "function" ? hostName(id) : wizHostById(id)?.name ?? `#${id}`);
const wizPlatform = (id) => (typeof hostPlatform === "function" ? hostPlatform(id) : wizHostById(id)?.info?.platform ?? "");
const wizMaskHost = (id) => maskAddr(wizHostName(id));
const wizBlurText = (s, url) => (typeof blurHostText === "function" ? blurHostText(s, url) : String(s ?? ""));
const wizMaskEndpoint = (url) => (typeof maskEndpoint === "function" ? maskEndpoint(url) : url);
const wizPreset = () => state.meta?.frameworks?.[wiz.framework] || {};
const wizRuntime = () => wizPreset().runtime || "docker";
const wizUsesWeightsRe = (s) => /\{weights(?::[^{}]*)?\}/.test(s || "");
const wizUsesWeights = () => ["model", "volumes", "command", "args", "env"].some((f) => wizUsesWeightsRe(wiz.adv[f]));

// Endpoint as the app builds it: the host's own name for remote hosts, else the address-bar host.
function wizEndpointHost(id) {
  const h = wizHostById(id);
  if (h?.url) { try { return new URL(h.url).hostname; } catch { /* fall through */ } }
  return location.hostname;
}
function wizEndpointUrl() { return `http://${wizEndpointHost(wiz.hostId)}:${wiz.port}/v1`; }

// Sensible engine choices per host/kind: Macs get native processes (no GPU in Docker on macOS),
// Linux gets Docker engines plus the native ones. Mirrors app.js defaults.
function wizEngineList() {
  const fw = state.meta?.frameworks || {};
  const darwin = wizPlatform(wiz.hostId) === "darwin";
  return Object.entries(fw).filter(([, v]) => (darwin ? v.runtime === "process" : true));
}
function wizRecommendedEngine() {
  const fw = state.meta?.frameworks || {};
  const darwin = wizPlatform(wiz.hostId) === "darwin";
  const want = wiz.kind === "agent" || darwin ? "process-custom" : "vllm";
  return fw[want] ? want : (wizEngineList()[0]?.[0] ?? "");
}

// Free memory for the fit check: the host's available system memory (unified on GPU boxes).
function wizFreeGb() {
  const m = wizHostById(wiz.hostId)?.system?.memory;
  return typeof m?.available_gb === "number" ? m.available_gb : null;
}

/* ---------- model / defaults ---------- */

function wizReset() {
  const local = state.hosts.find((h) => h.is_local) || state.hosts[0];
  wiz = {
    step: 1, kind: "model", name: "", description: "",
    hostId: local?.id ?? null,
    weightsId: null, model: "",
    framework: "", port: local ? nextFreePort(local.id) : 8100,
    adv: { image: "", command: "", args: "", env: "", volumes: "", docker_args: "", container_port: "", health_path: "", gpus: "all" },
    weightsRows: null, weightsError: "", saving: false, error: "",
  };
  wizAdvDirty = false;
  wiz.framework = wizRecommendedEngine();
  wizFillPreset(true);
}

// Prefill Advanced from the engine preset. force=true after an engine switch when not dirty;
// otherwise only empty fields are filled (user values survive).
function wizFillPreset(force) {
  const p = wizPreset();
  const set = (k, v) => { if (force || !String(wiz.adv[k] ?? "").trim()) wiz.adv[k] = v == null ? "" : String(v); };
  // Like app.js applyFramework: identity fields always follow the preset; args/env/volumes/flags
  // only fill when empty (or on a forced reset), so user values survive an engine switch.
  ["image", "command", "container_port", "health_path"].forEach((k) => { wiz.adv[k] = p[k] == null ? "" : String(p[k]); });
  ["args", "docker_args", "volumes", "env"].forEach((k) => set(k, p[k]));
  // Agents: no GPU by default, no health check (running = process/container is up), like app.js.
  if (wiz.kind === "agent") {
    if (force || wiz.adv.gpus === "all") wiz.adv.gpus = "";
    if (force || wiz.adv.health_path === (p.health_path ?? "")) wiz.adv.health_path = "";
  } else {
    if (!wiz.adv.gpus && wizPlatform(wiz.hostId) !== "darwin") wiz.adv.gpus = "all";
    set("health_path", p.health_path ?? "/health");
  }
  // Native processes listen on the host directly: one port.
  if (wizRuntime() === "process") wiz.adv.container_port = String(wiz.port || "");
}

// Mount picked weights where the model is read (same idea as app.js applyWeightsSelection):
// docker gets a {weights}:/model:ro volume + model /model; a native process reads {weights} directly.
// Only auto-written while Advanced is untouched, so user edits are never clobbered.
function wizApplyWeightsMount(force) {
  if (wizAdvDirty && !force) return;
  const docker = wizRuntime() === "docker";
  const stripAuto = () => {
    wiz.adv.volumes = wiz.adv.volumes.split("\n").filter((l) => !/^\{weights\}:\/model:ro$/.test(l.trim())).join("\n");
    if (wiz.adv.model === "/model" || wiz.adv.model === "{weights}") wiz.adv.model = "";
  };
  stripAuto();
  if (!wiz.weightsId) { if (docker) wiz.adv.model = ""; return; }
  if (docker) {
    const lines = wiz.adv.volumes.split("\n");
    if (!lines.some((l) => l.trim().split(":")[1]?.replace(/\/+$/, "") === "/model")) {
      wiz.adv.volumes = ["{weights}:/model:ro", ...lines.filter((l, i) => l.trim() || i < lines.length - 1)].join("\n");
    }
    wiz.adv.model = "/model";
  } else {
    wiz.adv.model = "{weights}";
  }
}

// POST body, mirroring app.js recipeFromForm(): numbers coerced, weights_ids canonical.
function wizBody() {
  const port = Number(wiz.port) || 0;
  const runtime = wizRuntime();
  const ids = wiz.weightsId ? [wiz.weightsId] : [];
  return {
    name: wiz.name.trim(),
    description: wiz.description.trim(),
    kind: wiz.kind,
    host_id: wiz.hostId != null ? Number(wiz.hostId) : null,
    runtime,
    framework: wiz.framework,
    image: wiz.adv.image.trim(),
    // weights picked → the app substitutes the per-host path ({weights} native, /model mount in docker)
    model: (wiz.weightsId ? (wiz.adv.model || (runtime === "process" ? "{weights}" : "/model")) : wiz.model).trim(),
    container_name: "",
    gpus: runtime === "docker" ? wiz.adv.gpus.trim() : "",
    host_port: port,
    container_port: runtime === "process" ? port : (Number(wiz.adv.container_port) || 0),
    health_path: wiz.adv.health_path.trim(),
    est_memory_gb: null,
    command: wiz.adv.command.trim(),
    args: wiz.adv.args,
    env: wiz.adv.env,
    volumes: wiz.adv.volumes,
    docker_args: runtime === "docker" ? wiz.adv.docker_args : "",
    tags: [],
    color: "",
    weights_ids: ids,
    weights_id: ids[0] ?? null,
  };
}

/* ---------- validation ---------- */

function wizValidate(step) {
  if (step === 1) {
    if (!wiz.name.trim()) return "Give the recipe a display name.";
    if (wiz.kind === "model" && !wiz.weightsId && !wiz.model.trim()) return "Pick registered weights or type a model id / path.";
    return "";
  }
  if (step === 2) {
    const p = Number(wiz.port);
    if (!Number.isInteger(p) || p < 1 || p > 65535) return "Port must be a whole number between 1 and 65535.";
    if (!wiz.framework) return "Pick an engine.";
    if (wizRuntime() === "docker" && !wiz.adv.image.trim()) return "Docker recipes need an image (Advanced).";
    if (wizRuntime() === "process" && !wiz.adv.command.trim()) return "Native-process recipes need a command (Advanced).";
    if (wizWeightsIdMismatch()) return "The recipe uses {weights} but no weights are picked — go back to step 1.";
    return "";
  }
  return "";
}
function wizWeightsIdMismatch() { return wizUsesWeights() && !wiz.weightsId; }

/* ---------- dialog construction (once) ---------- */

function wizBuild() {
  wizDlg = document.createElement("dialog");
  wizDlg.id = "dlg-wizard";
  wizDlg.className = "wiz-dialog";
  wizDlg.setAttribute("aria-labelledby", "wiz-title");
  wizDlg.innerHTML = `
    <div class="wiz-shell">
      <header class="wiz-head">
        <div class="wiz-headtext">
          <h3 id="wiz-title">New Recipe</h3>
          <p>Configure a model in 3 quick steps — no scripts required.</p>
        </div>
        <button type="button" class="wiz-x" data-wiz="close" aria-label="Close">&times;</button>
      </header>
      <nav class="wiz-steps" aria-label="Setup steps"></nav>
      <div class="wiz-body" data-wiz="body"></div>
      <footer class="wiz-foot">
        <button type="button" class="wiz-link" data-wiz="import">Paste YAML / script instead</button>
        <span class="wiz-spacer"></span>
        <span class="wiz-error" data-wiz="error" role="alert"></span>
        <button type="button" class="wiz-btn" data-wiz="back">Back</button>
        <button type="button" class="wiz-btn primary" data-wiz="next">Next →</button>
      </footer>
    </div>`;
  document.body.appendChild(wizDlg);
  wizEls = {
    steps: wizDlg.querySelector(".wiz-steps"),
    body: wizDlg.querySelector('[data-wiz="body"]'),
    error: wizDlg.querySelector('[data-wiz="error"]'),
    back: wizDlg.querySelector('[data-wiz="back"]'),
    next: wizDlg.querySelector('[data-wiz="next"]'),
    import: wizDlg.querySelector('[data-wiz="import"]'),
  };

  wizDlg.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-wiz]");
    if (!b || !wizDlg.contains(b)) return;
    const act = b.dataset.wiz;
    if (act === "close") wizDlg.close();
    else if (act === "back") wizGo(wiz.step - 1);
    else if (act === "next") wizNext();
    else if (act === "import") { wizDlg.close(); openImport(); }
    else if (act === "goto") { const n = Number(b.dataset.step); if (n < wiz.step) wizGo(n); }
    else if (act === "kind") wizSetKind(b.dataset.kind);
    else if (act === "engine") wizSetEngine(b.dataset.engine);
    else if (act === "reset-adv") { wizAdvDirty = false; wizFillPreset(true); wizApplyWeightsMount(true); wizRenderStep2(); }
    else if (act === "save") wizSave();
  });

  // Model changes from inputs/selects/textareas; radio rows re-render themselves.
  wizDlg.addEventListener("input", (ev) => {
    const el = ev.target;
    const f = el.dataset.wizField;
    if (!f) return;
    if (f.startsWith("adv.")) {
      wiz.adv[f.slice(4)] = el.value;
      wizAdvDirty = true;
      const note = wizDlg.querySelector("[data-wiz-advnote]");
      if (note) note.hidden = false;
      if (f === "adv.container_port" || f === "adv.command" || f === "adv.image") wizClearError();
    } else if (f === "name") { wiz.name = el.value; wizClearError(); }
    else if (f === "description") wiz.description = el.value;
    else if (f === "model") { wiz.model = el.value; wizClearError(); }
    else if (f === "port") {
      wiz.port = el.value;
      if (wizRuntime() === "process") wiz.adv.container_port = el.value;
      wizUpdatePortHint();
      wizClearError();
    }
  });
  wizDlg.addEventListener("change", (ev) => {
    const el = ev.target;
    const f = el.dataset.wizField;
    if (f === "host") wizSetHost(Number(el.value));
    else if (f === "weights") {
      wiz.weightsId = el.value ? Number(el.value) : null;
      if (wiz.weightsId) wiz.model = "";
      wizApplyWeightsMount(false);
      wizRenderWeights();
      wizClearError();
    }
  });

  // Enter advances only when the current step is valid (never inside a textarea).
  wizDlg.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.target.tagName === "TEXTAREA" || ev.target.tagName === "BUTTON") return;
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") {
      ev.preventDefault();
      wizNext();
    }
  });
  // Discard guard for Esc, matching the other dialogs' behaviour.
  wizDlg.addEventListener("cancel", (ev) => {
    if (wizIsDirty() && !confirm("Discard unsaved changes?")) ev.preventDefault();
  });
}

function wizIsDirty() { return !!(wiz.name.trim() || wiz.description.trim() || wiz.weightsId || wiz.model.trim() || wizAdvDirty); }

/* ---------- rendering ---------- */

function wizRender() {
  wizRenderSteps();
  if (wiz.step === 1) wizRenderStep1();
  else if (wiz.step === 2) wizRenderStep2();
  else wizRenderStep3();
  wizEls.back.hidden = wiz.step === 1;
  wizEls.next.textContent = wiz.step === 3 ? "Save Recipe" : "Next →";
  wizEls.next.dataset.wiz = wiz.step === 3 ? "save" : "next";
  wizEls.next.disabled = wiz.saving;
  wizEls.error.textContent = wiz.error;
}

function wizRenderSteps() {
  wizEls.steps.innerHTML = wizStepsMeta.map((s, i) => {
    const n = i + 1;
    const cls = n === wiz.step ? "active" : n < wiz.step ? "done" : "";
    const num = n < wiz.step ? "✓" : `0${n}`;
    return `<button type="button" class="wiz-step ${cls}" data-wiz="goto" data-step="${n}" ${n > wiz.step ? "disabled" : ""}
      ${n === wiz.step ? 'aria-current="step"' : ""}>
      <span class="wiz-num">${num}</span>
      <span class="wiz-steptxt"><b>${esc(s.title)}</b><small>${esc(s.sub)}</small></span>
    </button>`;
  }).join("");
}

function wizRenderStep1() {
  const multi = state.hosts.length > 1;
  wizEls.body.innerHTML = `
    <section class="wiz-section">
      <div class="wiz-seclabel"><span>1. What are you setting up?</span></div>
      <div class="wiz-kinds">
        ${Object.entries(wizKindInfo).map(([k, v]) => `
          <button type="button" class="wiz-kind ${wiz.kind === k ? "sel" : ""}" data-wiz="kind" data-kind="${k}" aria-pressed="${wiz.kind === k}">
            <b>${esc(v.label)}</b><small>${esc(v.desc)}</small>
          </button>`).join("")}
      </div>
    </section>
    <section class="wiz-section">
      <div class="wiz-grid2">
        <label class="wiz-field"><span>Display Name</span>
          <input class="wiz-input" data-wiz-field="name" maxlength="100" value="${esc(wiz.name)}" placeholder="e.g. qwen-coding-fast" autocomplete="off">
        </label>
        <label class="wiz-field"><span>Short Note <small>(optional)</small></span>
          <input class="wiz-input" data-wiz-field="description" value="${esc(wiz.description)}" placeholder="What is it for?" autocomplete="off">
        </label>
      </div>
      ${multi ? `
      <label class="wiz-field"><span>Host</span>
        <select class="wiz-select" data-wiz-field="host">
          ${state.hosts.map((h) => `<option value="${h.id}" ${h.id === wiz.hostId ? "selected" : ""}>${esc(maskAddr(h.name))}${h.info?.platform ? ` (${esc(h.info.platform)})` : ""}${h.online ? "" : " (offline)"}</option>`).join("")}
        </select>
      </label>` : ""}
    </section>
    <section class="wiz-section" data-wiz-weights></section>`;
  wizRenderWeights();
  wizEls.body.querySelector('[data-wiz-field="name"]').focus();
}

// The weights list comes from /api/weights?check=true (adds per-host size/presence); until it
// answers we show the registry entries with paths only — never invented numbers.
function wizWeightsSource() {
  if (wiz.weightsRows) return wiz.weightsRows;
  return state.weights.map((w) => ({ id: w.id, name: w.name, path: w.locations?.[wiz.hostId] || "", size_gb: null, state: "" }));
}

function wizRenderWeights() {
  const box = wizEls.body.querySelector("[data-wiz-weights]");
  if (!box) return;
  const free = wizFreeGb();
  const rows = wizWeightsSource().filter((w) => w.path);
  const noneSel = !wiz.weightsId;
  const rowHtml = rows.map((w) => {
    const size = typeof w.size_gb === "number" ? w.size_gb : null;
    let badge = "";
    if (size != null && free != null) {
      badge = size <= free * 0.9
        ? `<span class="wiz-badge ok">Fits</span>`
        : `<span class="wiz-badge warn" title="Close to the free memory — may not load">Tight</span>`;
    }
    const sizeTxt = size != null ? `<span class="wiz-size">${size.toFixed(1)} GB</span>` : "";
    const missing = w.state && w.state !== "present" ? ` <span class="wiz-badge warn" title="The host could not confirm this path">?</span>` : "";
    const path = wizBlurText(w.path, wizHostById(wiz.hostId)?.url);
    return `<label class="wiz-wrow ${wiz.weightsId === w.id ? "sel" : ""}">
      <input type="radio" name="wiz-weights" data-wiz-field="weights" value="${w.id}" ${wiz.weightsId === w.id ? "checked" : ""}>
      <span class="wiz-wmain"><b>${esc(w.name)}</b><code>${esc(path)}</code></span>
      <span class="wiz-fit">${sizeTxt}${badge}${missing}</span>
    </label>`;
  }).join("");
  box.innerHTML = `
    <div class="wiz-seclabel">
      <span>2. Choose Model Files (Weights)</span>
      ${free != null ? `<span class="wiz-free">${free.toFixed(1)} GB available on ${esc(wizMaskHost(wiz.hostId))}</span>` : ""}
    </div>
    ${wiz.weightsError ? `<div class="wiz-note">${esc(wiz.weightsError)}</div>` : ""}
    <div class="wiz-weights">
      ${rowHtml || `<div class="wiz-empty">No registered weights on this host — type a model id or path below.</div>`}
      <label class="wiz-wrow ${noneSel ? "sel" : ""}">
        <input type="radio" name="wiz-weights" data-wiz-field="weights" value="" ${noneSel ? "checked" : ""}>
        <span class="wiz-wmain"><b>${wiz.kind === "agent" ? "No model files (optional)" : "No registered weights — type a model id / path"}</b>
        <small class="wiz-note">${wiz.kind === "agent" ? "Agents usually need none; optionally name the model it talks to." : "A Hugging Face id (org/model) or a path on the host (~ allowed)."}</small></span>
      </label>
      ${noneSel ? `<div class="wiz-modelwrap">
        <input class="wiz-input wiz-mono" data-wiz-field="model" value="${esc(wiz.model)}"
          placeholder="e.g. Qwen/Qwen3-8B or ~/models/my-model" autocomplete="off">
      </div>` : ""}
    </div>`;
}

function wizRenderStep2() {
  const engines = wizEngineList();
  const rec = wizRecommendedEngine();
  const docker = wizRuntime() === "docker";
  const adv = wiz.adv;
  const field = (label, key, opts = {}) => `
    <label class="wiz-field"><span>${esc(label)}</span>
      ${opts.textarea
        ? `<textarea class="wiz-textarea" data-wiz-field="adv.${key}" rows="${opts.rows || 2}" spellcheck="false">${esc(adv[key])}</textarea>`
        : `<input class="wiz-input wiz-mono" data-wiz-field="adv.${key}" value="${esc(adv[key])}" spellcheck="false" autocomplete="off">`}
      ${opts.hint ? `<small>${esc(opts.hint)}</small>` : ""}
    </label>`;
  wizEls.body.innerHTML = `
    <section class="wiz-section">
      <div class="wiz-seclabel"><span>1. Engine</span></div>
      <div class="wiz-engines">
        ${engines.map(([k, v]) => `
          <button type="button" class="wiz-engine ${wiz.framework === k ? "sel" : ""}" data-wiz="engine" data-engine="${esc(k)}" aria-pressed="${wiz.framework === k}">
            <span class="wiz-engine-top"><b>${esc(v.label)}</b>
              <span class="wiz-tag">${v.runtime === "docker" ? "Docker" : "native"}</span>
              ${k === rec ? `<span class="wiz-tag rec">Recommended</span>` : ""}
            </span>
            <small>${esc(wizEngineBlurb[k] || (v.runtime === "docker" ? "Runs as a Docker container." : "Runs directly on the host."))}</small>
          </button>`).join("")}
      </div>
    </section>
    <section class="wiz-section">
      <div class="wiz-grid2">
        <label class="wiz-field"><span>API Port</span>
          <span class="wiz-portrow">
            <input class="wiz-input wiz-mono" data-wiz-field="port" inputmode="numeric" value="${esc(wiz.port)}" autocomplete="off">
            <span class="wiz-endpoint" data-wiz-endpoint>→ ${esc(wizMaskEndpoint(wizEndpointUrl()))}</span>
          </span>
          <small class="wiz-porthint" data-wiz-porthint></small>
        </label>
      </div>
    </section>
    <details class="wiz-adv" ${wizAdvOpen ? "open" : ""}>
      <summary><span>Advanced: image, command &amp; flags</span><span class="wiz-opt">Optional</span></summary>
      <div class="wiz-advbody">
        <div class="wiz-advnote" data-wiz-advnote ${wizAdvDirty ? "" : "hidden"}>
          <span>Edited — switching engines will keep your values.</span>
          <button type="button" class="wiz-link" data-wiz="reset-adv">Reset to engine defaults</button>
        </div>
        ${docker ? field("Container image", "image", { hint: "Docker pulls it on first start if missing." }) : ""}
        ${field("Command", "command", { textarea: true, rows: 2, hint: "Placeholders: {model} {port} {host_port} {bind_host} {name}" })}
        ${field("Arguments (one per line)", "args", { textarea: true, rows: 3 })}
        ${field("Environment (one per line)", "env", { textarea: true, rows: 2, hint: "NAME=value, or just NAME to pass it through from the hub." })}
        ${docker ? field("Volumes (one per line)", "volumes", { textarea: true, rows: 2, hint: "host:container[:ro] — {weights} stands for the picked weights path." }) : ""}
        ${docker ? field("Extra docker flags", "docker_args", { hint: "e.g. --ipc=host --ulimit memlock=-1" }) : ""}
        <div class="wiz-grid2">
          ${docker ? field("Container port", "container_port") : ""}
          ${field("Health path", "health_path", { hint: wiz.kind === "agent" ? "Empty for agents: running = process/container is up." : "e.g. /health" })}
        </div>
        ${docker ? field("GPUs", "gpus", { hint: '"all", "0,1" or empty for none.' }) : ""}
      </div>
    </details>`;
  wizAdvEls = {};
  wizEls.body.querySelectorAll("[data-wiz-field]").forEach((el) => { wizAdvEls[el.dataset.wizField] = el; });
  const det = wizEls.body.querySelector(".wiz-adv");
  det.addEventListener("toggle", () => { wizAdvOpen = det.open; });
  wizUpdatePortHint();
  const first = wizEls.body.querySelector(".wiz-engine.sel") || wizEls.body.querySelector(".wiz-engine");
  first?.focus();
}

function wizUpdatePortHint() {
  const hint = wizEls.body.querySelector("[data-wiz-porthint]");
  const ep = wizEls.body.querySelector("[data-wiz-endpoint]");
  if (!hint) return;
  const port = Number(wiz.port);
  if (ep) ep.textContent = `→ ${wizMaskEndpoint(wizEndpointUrl())}`;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    hint.textContent = "Port must be between 1 and 65535.";
    hint.className = "wiz-porthint bad";
    return;
  }
  const others = state.recipes.filter((r) => r.host_id === Number(wiz.hostId) && r.host_port === port);
  const where = state.hosts.length > 1 ? ` on ${wizMaskHost(wiz.hostId)}` : "";
  if (!others.length) {
    hint.textContent = `No other recipe uses port ${port}${where}.`;
    hint.className = "wiz-porthint ok";
    return;
  }
  const running = others.filter((r) => ["ready", "loading", "starting"].includes(r.status));
  hint.textContent = running.length
    ? `Port ${port}${where} is in use by running “${running.map((r) => r.name).join("”, “")}” — this recipe can't start while it runs.`
    : `Also used by “${others.map((r) => r.name).join("”, “")}”${where} — only one of them can run at a time.`;
  hint.className = `wiz-porthint ${running.length ? "bad" : "warn"}`;
}

function wizRenderStep3() {
  const b = wizBody();
  const w = wiz.weightsId ? wizWeightsSource().find((x) => x.id === wiz.weightsId) : null;
  const runtimeLabel = state.meta?.runtimes?.[b.runtime] || b.runtime;
  const rows = [
    ["Name", esc(b.name)],
    ["Kind", esc(wizKindInfo[wiz.kind].label)],
    ["Host", esc(wizMaskHost(wiz.hostId))],
    ["Engine", `${esc(wizPreset().label || wiz.framework)} · ${esc(runtimeLabel)}`],
    ["Weights", w ? `${esc(w.name)} <code class="wiz-mono">${esc(wizBlurText(w.path, wizHostById(wiz.hostId)?.url))}</code>`
      : b.model ? `<code class="wiz-mono">${esc(b.model)}</code>` : `<span class="wiz-note">none</span>`],
    ["Port", `<span class="wiz-mono">${b.host_port}</span>`],
    ["Endpoint", `<code class="wiz-mono">${esc(wizMaskEndpoint(wizEndpointUrl()))}</code>`],
  ];
  wizEls.body.innerHTML = `
    <section class="wiz-section">
      <div class="wiz-seclabel"><span>Review</span></div>
      <div class="wiz-review">
        ${rows.map(([k, v]) => `<div class="wiz-rrow"><span>${esc(k)}</span><span>${v}</span></div>`).join("")}
      </div>
    </section>
    <section class="wiz-section">
      <div class="wiz-seclabel"><span>Generated command</span></div>
      <pre class="wiz-preview" data-wiz-preview>Building preview…</pre>
      <div class="wiz-note">You can edit or test this recipe anytime after saving.</div>
    </section>`;
  wizUpdatePreview();
  wizEls.next.focus();
}

async function wizUpdatePreview() {
  const seq = ++wizPreviewSeq;
  const pre = wizEls.body.querySelector("[data-wiz-preview]");
  if (!pre) return;
  try {
    const res = await api("/api/recipes/preview", { method: "POST", body: wizBody() });
    if (seq !== wizPreviewSeq || !pre.isConnected) return;
    pre.textContent = res.command;
    if (typeof previewHtml === "function") pre.innerHTML = previewHtml(res.command, wiz.adv.image.trim());
  } catch (e) {
    if (seq !== wizPreviewSeq || !pre.isConnected) return;
    pre.textContent = `(incomplete) ${e.message}`;
  }
}

/* ---------- navigation & actions ---------- */

function wizClearError() { wiz.error = ""; wizEls.error.textContent = ""; }

function wizGo(step) {
  if (step < 1 || step > 3) return;
  wiz.step = step;
  wizClearError();
  wizRender();
  wizEls.body.scrollTop = 0;
}

function wizNext() {
  if (wiz.saving) return;
  const err = wizValidate(wiz.step);
  if (err) {
    wiz.error = err;
    wizEls.error.textContent = err;
    if (wiz.step === 1 && !wiz.name.trim()) wizEls.body.querySelector('[data-wiz-field="name"]')?.focus();
    else if (wiz.step === 2) wizEls.body.querySelector('[data-wiz-field="port"]')?.focus();
    return;
  }
  if (wiz.step === 3) { wizSave(); return; }
  if (wiz.step === 1) {
    // Re-derive the recommendation when kind/host changed the sensible default, unless the user
    // already picked an engine explicitly (tracked by wizEnginePicked).
    if (!wizEnginePicked) { wiz.framework = wizRecommendedEngine(); wizFillPreset(true); wizApplyWeightsMount(true); }
    if (wizRuntime() === "process") wiz.adv.container_port = String(Number(wiz.port) || "");
  }
  wizGo(wiz.step + 1);
}

function wizSetKind(kind) {
  if (wiz.kind === kind) return;
  wiz.kind = kind;
  wizEnginePicked = false;
  wiz.framework = wizRecommendedEngine();
  wizFillPreset(true);
  wizApplyWeightsMount(true);
  wizRenderStep1();
}

function wizSetHost(id) {
  wiz.hostId = id;
  wiz.port = nextFreePort(id);
  wiz.weightsId = null;
  wizEnginePicked = false;
  wiz.framework = wizRecommendedEngine();
  wiz.adv.gpus = wizPlatform(id) === "darwin" ? "" : (wiz.kind === "agent" ? "" : "all");
  wizFillPreset(true);
  wizApplyWeightsMount(true);
  wizLoadWeights();
  wizRenderStep1();
}

function wizSetEngine(key) {
  if (wiz.framework === key) return;
  wiz.framework = key;
  wizEnginePicked = true;
  wizFillPreset(!wizAdvDirty);
  wizApplyWeightsMount(false);
  wizRenderStep2();
}

async function wizLoadWeights() {
  const seq = ++wizWeightsSeq;
  wiz.weightsRows = null;
  wiz.weightsError = "";
  try {
    const data = await api("/api/weights?check=true");
    if (seq !== wizWeightsSeq) return;
    wiz.weightsRows = (data.weights || []).map((w) => {
      const c = (w.hosts || []).find((x) => x.host_id === Number(wiz.hostId));
      return { id: w.id, name: w.name, path: c?.path || w.locations?.[wiz.hostId] || "", size_gb: c?.size_gb ?? null, state: c?.state || "" };
    });
  } catch (e) {
    if (seq !== wizWeightsSeq) return;
    wiz.weightsError = `Could not check weights sizes (${e.message}) — paths shown without sizes.`;
  }
  if (wiz.step === 1) wizRenderWeights();
}

async function wizSave() {
  if (wiz.saving) return;
  const err = wizValidate(1) || wizValidate(2);
  if (err) { wiz.error = err; wizEls.error.textContent = err; return; }
  wiz.saving = true;
  wizEls.next.disabled = true;
  wizEls.next.textContent = "Saving…";
  wizClearError();
  try {
    await api("/api/recipes", { method: "POST", body: wizBody() });
    wizDlg.close();
    toast("Recipe saved", "ok");
    loadRecipes();
  } catch (e) {
    wiz.error = e.message;   // server message inline; the form stays as it was
    wizEls.error.textContent = e.message;
  } finally {
    wiz.saving = false;
    if (wizDlg.open) { wizEls.next.disabled = false; wizEls.next.textContent = "Save Recipe"; }
  }
}

/* ---------- entry point ---------- */

function openRecipeWizard() {
  if (!wizDlg) wizBuild();
  wizReset();
  wizEnginePicked = false;
  wizAdvOpen = false;
  wizLoadWeights();
  wizRender();
  if (!wizDlg.open) wizDlg.showModal();
  // showModal() focuses the first focusable element itself — put the cursor in the first field after.
  wizEls.body.querySelector('[data-wiz-field="name"]')?.focus();
}
