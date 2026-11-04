# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Health findings:** the hub checks every 15 s for problems and names them where you look anyway - a
  chip on the recipe card (with what happened, the log line that shows why, and what usually helps), on the
  host in the Hosts graph, a ⚠/✖ count on the host chip in the top bar, and an entry in the activity log
  (new **Health** filter). Rules: host unreachable, memory almost full (under 8 % / 3 % free), GPU hot
  (85 / 90 °C), KV cache almost full with requests waiting, a failed start with its cause read from the log
  (CUDA out of memory, KV cache too small, killed, port in use, model not found, gated repo, CUDA error,
  unknown argument) and a start that takes far longer than usual. A condition must hold for a while before
  it becomes a finding and must be gone for a minute before it clears, so readings on a threshold do not
  flap. **Mute 24 h** hides one; `GET /api/health` lists them and `/metrics` has `dgxm_health_findings`.
  Findings only read - nothing is started or stopped. `DGXM_HEALTH_TICK` sets the interval.
- **Recipes by engine:** **Group → Engine** (vLLM, SGLang, TensorFold, llama.cpp, MLX, Custom; Docker and
  native of one engine together, with a 🐳 / ⚙ count) and **Host → Engine**; engine and host filter chips
  with counts next to the tags; one badge colour per engine with a 🐳 / ⚙ mark. Removed: the *Framework*
  sort (Group → Engine replaces it); Host, Group and Sort are chips instead of dropdowns (also the Images
  tab's host filter); the host on each card and the
  engine name on its badge are left out under a heading that already says them.
- **GGUF weights as a folder:** a llama.cpp recipe whose weights are a folder (as a Hugging Face download
  is) now starts the model file in it - skipping vision projectors (`mmproj`) and using the first part of a
  split model. A folder with several models is refused with their names instead of a failing start.

## [0.2.0] - 2026-11-02

### Highlights
- **Recipes across the broad inference engines in one place:** vLLM, SGLang, llama.cpp (Docker or native),
  MLX and TensorFold (experimental) presets, plus any image or binary as *Custom*.
- **Recipes from YAML or a shell script, and between managers:** **＋ Add recipe** is a paste box for a
  compact YAML/JSON recipe or a start script / `docker run` (with an example of each). **Recipe file…**
  exports a recipe - editable, so it can be tweaked as text - to import on another host or another manager
  installation; **⇉ Copy to hosts** rolls it out on one dashboard and keeps the copies in sync.
- **Weights management:** the Weights tab is one table (model × host) that searches every host by itself
  and offers what it finds under **Found on your hosts**. **⬇ Download** from Hugging Face once, then
  **⇉ Copy to other hosts** machine to machine (SHA-256 checked, resumable). Recipes use `{weights}` and
  run on any host without edits.
- **Hosts graph with the important metrics:** the hub and every agent PC as a map with memory, GPU load,
  GPU and CPU temperature, running models and agents and connection encryption; **Ports & endpoints**
  shows live speed, requests and KV-cache use per model.
- **Hub and agents:** start the hub, then connect each agent with its token - added on the hub, or by the
  agent asking to join (`DGXM_HUB_URL`) and you approving matching join codes.

### Upgrading from 0.1.0
- Upgrade the hub and all agents together: the new features need new agents.
- The TensorRT-LLM preset is gone: re-save such recipes once with *Custom* (they keep running until then).
- The database is upgraded on the first start; back it up (`DGXM_DB`) if you may want to go back.

### Added
**Recipes**
- **＋ Add recipe by paste:** a YAML/JSON recipe file (the editor's fields; missing settings come from the
  framework preset) or a starter script / `docker run`, checked for risky lines and run in a restricted
  mode that only records the server command. One-click examples of each under the box; importable
  examples in `examples/recipes` (Qwen3.8, Nemotron-3.5-Super-VL, EmbeddingGemma 2, SGLang Flint, Hermes).
- **Recipe file…** in the editor: copy or download a recipe as compact YAML/JSON, edit it there and
  **Apply to editor** without saving.
- **Agents** (e.g. a Hermes gateway) as their own kind of recipe, apart from models, with their own section
  per host, secret checks and a warning about stray second copies.
- **TensorFold** preset (experimental); models shown as rows or tiles.
- **More filters and grouping:** show All / Models / Agents, and group agents by host or tag like models.

**Several machines**
- **Agent self-registration:** an agent with `DGXM_HUB_URL` asks the hub to be added; approve it by
  comparing the join code.
- **Copy to other hosts:** roll one recipe out to several machines; copies stay in sync with the original
  and can be restarted on all machines at once, in the background.
- **Hosts graph:** the Hosts tab opens with a map of your setup - the hub on top, a line to every agent PC,
  and on each machine its memory, GPU and what runs there: models (🧠, with their port) and agents (🤖),
  with a live status dot; stopped recipes are counted. Line colours show the connection: green for
  Tailscale / HTTPS, yellow for plain HTTP, red dotted for an offline machine. Click a machine to edit or
  remove it, or a recipe to open it. The host table moved behind **Host details & actions**.
- **CPU temperature per host** next to the GPU's (graph, hosts table, top-bar tooltip), yellow from 85 °C
  and red from 95 °C. Reads `coretemp`, `k10temp`, … and the ACPI thermal zones the DGX Spark uses; macOS
  shows none.
- **Ports & endpoints metrics:** each running model's output/input speed, requests and KV-cache use.

**Weights and images**
- **Weights tab** and **weights registry:** one entry per model with its path on each host; recipes use
  `{weights}` and work on any host. Changing a recipe's host makes its model folder follow.
- **Found on your hosts:** the tab searches each host's `DGXM_MODEL_DIR` (several allowed) for model
  folders and `.gguf` files by itself and offers paths, new folders and recipes that mount a folder
  directly, one click each.
- **Download from Hugging Face** on hosts (gated models: `HF_TOKEN` in `.env`) and **pull images** on
  several hosts at once.
- **Copy weights host to host:** download once, then machines copy from each other (snowball-style,
  SHA-256 checked, resumable, free disk space checked first).
- **Missing weights per host:** the editor, the copy dialog and the start check say where a model is missing.
- **Images tab** (experimental): which host has which image, and newer tags to switch to.

**Operations and docs**
- **Activity log controls:** show the last 10, 50, 100 or 300 entries and clear the log.
- **Self-monitoring:** `/healthz` and opt-in Prometheus `/metrics` (`DGXM_METRICS=1`).
- **Log retention:** old activity is pruned after `DGXM_RETENTION_DAYS` (default 90).
- **Documentation site** with Quick start, Recipes (supported engines, adding and moving recipes),
  Weights, Several machines (hub and agents, Hosts tab), How it works and a configuration
  reference. The README leads with the main use cases.

### Changed
- **Left sidebar navigation:** Recipes, Schedules, Activity, Hosts, Images and Weights moved into a left
  menu (a drawer on phones); the top bar shows the section, host status, Blur and Theme.
- **One lock per host:** a slow action on one machine no longer blocks the others.
- **Recipe editor:** command preview beside the form.
- **Model cards:** show the model folder, served model name and endpoint copy button.
- **Clearer status:** **Pulling image…** while a missing image is fetched, own labels for stop / restart /
  switch, and a container killed for lack of memory shows **failed** instead of "stopped".
- **First start:** an empty database gets the **qwen3.8** example (`examples/recipes/qwen38-27b.yaml`) and
  its weights entry. Register or download the weights in the Weights tab before starting it.

### Fixed
- Empty recipe label in the command preview of an existing recipe.
- "KV" stats figure wrapping onto its own line; tab bar too wide on phones.
- Jobs panel losing a running copy after a laptop wakes from sleep; it now also shows jobs from other
  browsers and recently finished ones.
- A copied recipe could be hidden by the host filter, search or a collapsed group; the saved host filter
  now shows correctly in the selector.

## [0.1.0] - 2026-10-26

First public release.

### Features
- **Recipes** for model servers - vLLM, SGLang, TensorRT-LLM, llama.cpp, MLX or any image - as Docker
  containers or native processes (e.g. MLX on a Mac), with presets, a live `docker run` preview,
  placeholders (`{model}`, `{port}`, `{host_port}`, `{name}`, `{bind_host}`), tags, colours and pinning.
- **Import** a starter script or `docker run` command into a recipe: a static safety check first, then a
  restricted run that only records the server command.
- **Start, stop, restart, "switch to"** (stop the others on that machine, free memory, start) and duplicate.
- **Live status, logs and stats:** loading/ready/failed, tokens/s with sparkline, running requests,
  KV-cache usage from the server's `/metrics`, typical start time.
- **Schedules** (weekdays/time, cron, one-shot) with a planned-runs timeline, pause all and run now; the
  hub's time zone is shown.
- **Several machines, one dashboard:** a hub plus agents (Linux, macOS, DGX Spark), per-host ports and
  endpoints, host filter and grouping.
- **Tailscale-only mode:** `DGXM_HOST=tailscale` and `DGXM_PUBLISH_IP=tailscale` keep the dashboard,
  the agent API and the model ports inside your tailnet - also for use from a phone.
- Blur toggle for screenshots and screen shares, dark/light/auto themes, keyboard shortcuts.
- Installers for Linux (systemd) and macOS (launchd), Docker Compose, multi-arch image
  `ghcr.io/einfachllm/local-llm-manager` (amd64 + arm64), `pip install .`. All of them read the settings
  from `.env` in the install folder; the installers create it with a random password, check that the
  service started and print its address (also over Tailscale).

### Security
- HTTP Basic auth with a password policy (at least 12 characters, no well-known passwords, not
  containing the user name) and
  lockout after 10 failed logins in 10 minutes; the installers generate a random password and agent token.
- Agents require a token of at least 32 characters.
- Cross-site requests that change something are rejected (CSRF), host names are checked against DNS
  rebinding when no password is set, and security headers prevent framing.
- The database (it stores agent tokens) is readable by its owner only.
- See [SECURITY.md](https://github.com/einfachllm/local-llm-manager/blob/main/SECURITY.md) for the threat model and how to report vulnerabilities.

[Unreleased]: https://github.com/einfachllm/local-llm-manager/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/einfachllm/local-llm-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/einfachllm/local-llm-manager/releases/tag/v0.1.0
