# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-12

### Highlights
- **An audit-driven reliability and security release.** A full review of the POC (five parallel audits plus
  verification) turned up one silent data loss, two security gaps and a set of promises the code did not
  keep. This release fixes them, plus the long tail of functional gaps the review found.
- **Copies and weights are trustworthy:** symlinked folders travel correctly, link targets are checked,
  copies to older agents keep working, moving a model into a folder a running recipe uses is refused, and
  a hub restart no longer orphans a running copy.
- **Schedules, health and jobs survive restarts:** schedule times are the hub's wall clock everywhere, DST
  transitions are handled, findings keep their history across a restart, and unfinished jobs resume.
- **Runs on any host:** Compose no longer insists on NVIDIA or Tailscale (both are override files), the
  installers are more forgiving, and the published image builds on a Python version CI actually tests.

### Upgrading from 0.2.0
- Upgrade the hub and all agents together. The copy protocol now carries symlinks; a new hub serves an older
  agent ordinary files, so mixed versions keep working - but update both soon.
- Docker Compose: the base file no longer reserves NVIDIA GPUs or mounts Tailscale. Add
  `-f docker-compose.nvidia.yml` for GPU readings in the Hosts graph and/or `-f docker-compose.tailscale.yml`
  for Tailscale mode. `:latest` is moved by hand after each release, so `docker compose pull &&
  docker compose up -d` upgrades again.
- A running recipe's runtime fields (ports, command, args, image, env, volumes, docker flags, GPUs, model,
  weights, framework, runtime) are locked now: stop it first. Name, description, tags, colour and the pin
  stay editable, so the dashboard no longer silently diverges from what runs.
- The import check asks for confirmation on more constructs (`cd`, `eval`, awk pipes, `sort -o`, GNU sed's
  `e`, …). Displayed commands mask `KEY=value` secrets; the values still reach the server.
- On macOS a configuration error (weak password, short token) now stops the service instead of restarting
  it in a loop: fix `.env` and rerun `./install-macos.sh`.
- Schedules now use the hub's IANA zone, so DST is handled; the database is upgraded on the first start -
  back it up if you may want to go back.

### Added

**Recipes, hosts and jobs**
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
- **Host meters in the Hosts graph:** memory, GPU load, GPU and CPU temperature as one 0-100 bar each
  (% or °C) with the value beside it, instead of one line of text. Temperatures turn yellow from 85 °C and
  red from 90 °C - the health-finding thresholds - with a tick at 85; memory from 75 / 90 % used. Readings
  a host does not have (e.g. temperatures on a Mac) are left out.
  Hub and agent PCs stand side by side when they fit in one line (each link on its own track above the
  cards); with many agents or a narrow window the hub stays on top as before.
- **GGUF weights as a folder:** a llama.cpp recipe whose weights are a folder (as a Hugging Face download
  is) now starts the model file in it - skipping vision projectors (`mmproj`) and using the first part of a
  split model. A folder with several models is refused with their names instead of a failing start.
- **Jobs survive a hub restart:** pull, download, copy and restart jobs are stored, and unfinished ones are
  picked up again after a restart. A transfer the agent lost in its own restart is started again there
  (copies resume from their `.part` files), and a copy across several hosts continues from where it stopped.
- **Digest-pinned images are recognised:** a recipe that runs `repo@sha256:…` shows as present in the
  Images tab and in copy checks instead of "missing" (`docker image ls --digests`).

**Web UI**
- **Install to the home screen:** a web app manifest with name, colours and icons - on a phone the
  dashboard opens standalone.
- **Hub version and connection state:** the sidebar shows the hub's version, and a badge in the top bar
  appears when the hub stops answering (it disappears when it is back).
- **Activity filter at the source:** the **Health** filter is asked of the hub
  (`GET /api/events?source=health`, a new optional parameter) instead of filtering the last N entries, and
  the tab refreshes while it is open.
- **Content-Security-Policy without inline scripts** (the theme boot moved to `theme.js`), `aria-live`
  toasts and labelled dialogs.

### Changed
- **Example recipes are one Qwen3.8 family:** `examples/recipes` keeps the recipes that actually run here.
  The same Qwen3.8-27B-NVFP4 on four engines - vLLM with MTP (`qwen38-mtp`, renamed from `qwen38-27b` and
  still the file a fresh install seeds from), vLLM with the DFlash2 drafter
  (`qwen38-dflash`), SGLang with DFlash2 (`qwen38-sglang`) and native TensorFold (`qwen38-tensorfold`) - all
  on port **8101** with the served name `qwen3.8`, so **Switch to** changes the engine under one endpoint;
  plus Qwen3.8-Flash-Next-FP4 (b12x) on a DGX Spark, Nemotron-3.5-Super-VL and the Hermes agent. The three
  with a drafter now mount **one** registered folder (`Qwen3.8-Models` = the folder holding model and
  drafter) instead of a fixed `/home/<user>/models/...`, so importing one on another machine needs no edits.
- **The two example tables stop drifting:** `docs/recipes.md` and `examples/recipes/README.md` list the same
  rows, and `tests/test_examples.py` keeps folder and both tables in step - every file listed exactly once in
  each, every example importing without a warning, no server flag or mount set twice, the family sharing one
  port and served name, and no example naming a user's home directory.
- **Example tests import for real:** every published starter script - `examples/recipes` and
  `examples/hermes-agent` - is now run through the restricted import in the tests and validated against
  the API model (`RecipeIn`), including the undefined-`$ARGS` Hermes starter; YAML drafts are validated
  too. Only benign warnings are allowed.
- **Compose runs on any host; NVIDIA and Tailscale are overrides:** the base `docker-compose.yml` no
  longer reserves NVIDIA devices or mounts Tailscale paths (both broke hosts without them). Use
  `docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d` for GPU readings in the
  Hosts graph, and `-f docker-compose.tailscale.yml` for Tailscale mode; README and docs explain both.
- **The Hermes example tracks `:latest` explicitly:** `nousresearch/hermes-agent:latest` with a comment
  to pin a tag for reproducibility (Hermes changes fast).
- **Logs are bounded by default:** Docker recipes get `--log-opt max-size=50m --log-opt max-file=3` (a
  recipe's own `--log-opt` wins). A native log is rotated once it passes 50 MB (copytruncate, so the running
  server is not restarted) and the log view tails it by seeking instead of reading the whole file.
- **One GB everywhere:** host memory readings are decimal GB (1e9), the same unit the Weights tab uses.
- **Placeholders are validated:** a typo such as `{modle}` is refused at start and in the preview - with the
  list of unknown names - instead of being passed to the engine. `{weights}` in a command or argument is
  quoted, so a weights path with a space stays one argument.
- **Schedules see DST transitions:** the hub resolves its IANA zone (`TZ`, then the `/etc/localtime`
  symlink, then `/etc/timezone`; fixed-offset fallback for UTC). A repeated autumn wall time fires once
  for daily/weekly schedules (an hourly schedule keeps its rhythm), and a non-existent spring time like
  02:30 runs at the transition (03:00). Documented in the README and the configuration reference.
- **Schedule times are the hub's everywhere:** the one-time editor renders and sends the hub's wall clock
  (with its offset), the schedule list and the timeline axis are labelled hub time - so times no longer
  drift by the browser/hub difference.
- **Documentation catches up with the UI:** the CPU temperature threshold in Several machines is 90 °C
  (not 95 °C), the Quick start no longer claims imported scripts are never executed (the check executes
  nothing; the import runs them restricted), the toolbar docs list the *Edited* sort, the group options
  (host / engine / host → engine / tag) and the full search fields, and the Blur toggle is documented in
  the left sidebar where it lives.
- **CI checks more of what ships:** ShellCheck covers the uninstallers too; pull requests build the
  Docker image without pushing it; the test run reports coverage (uploaded as an artifact, no
  threshold); and the release workflow smoke-tests the image before publishing, like the manual
  Docker-latest workflow already did.

### Fixed
- **Pinning a recipe or switching its image tag no longer drops its weights:** those two actions sent the
  recipe without `weights_id`, so the registered weights link was silently set to nothing and every later
  start of a `{weights}` recipe was refused (and a sync spread the loss to the copies). The recipe body now
  carries every field; a guard test keeps it complete.
- **Secrets in the activity log:** an environment line written as `KEY=value` appeared in plain text in the
  command preview, the start message and the activity log. Displayed commands now mask the value
  (`KEY=***`); bare `KEY` passthrough lines are unchanged, and the real value still reaches the server.
- **Script import check:** awk command pipes and `getline`, GNU sed's `e` command, `sort -o`, `eval`, `cd`,
  `source` with a path, `date -s` and `function`-style overrides of the import hook ran without the
  "I trust this script" confirmation because the static check did not flag them. They are now blocking
  findings, and backslash continuations are joined before checking (`curl … \` + `| sh` is one line).
- **Copies lost symlinked subdirectories:** a model folder with a linked subfolder (e.g. `tensors ->
  /data/shards`) copied "ok" while the shards never travelled. Linked directories now travel as links and
  are recreated on the target; a real directory with its own content is never replaced by a link. The
  weights scan also finds model folders that are themselves symlinks (once per real folder).
- **Copy links are validated:** link targets travel relative to their own folder and the copying side
  refuses absolute targets and targets outside the copied folder. Re-copies no longer resolve a link's
  destination into its target (which could replace a real shard with a self-link).
- **Copies to older agents:** the new link manifest broke 0.2.0 targets ("checksum does not match").
  Agents now advertise `copy_links` in their info; the hub serves symlinks as ordinary files to agents
  without it (and when the info cannot be read). New hubs and new agents keep the link layout.
- **A stopped Docker daemon no longer looks like "all models stopped":** `docker ps` failures are an error
  now, recipes show offline with the reason, and switch/stop-all refuse to act on an unknown state instead
  of silently doing nothing.
- **"Switch to" and "stop all" finish the job:** when one stop failed, the remaining models kept running
  and the switch never started. The other models are now stopped regardless, and the error carries the
  full balance (stopped / could not stop, with reasons).
- **Live stats out of the box for SGLang and llama.cpp:** the presets now include `--enable-metrics`
  (SGLang) and `--metrics` (llama.cpp, Docker and native); the SGLang example recipe enables metrics too.
- **Health findings survive a restart:** they were kept in memory only, so an update silently lost them (no
  "resolved" entry, and a host that had just gone offline showed nothing for a minute). Active findings are
  stored now, keep their "since", and resolve with an activity entry when the condition is gone. The
  offline-host rule also hands over from a failed start without a gap.
- **The timeline plans switches correctly:** a "switch" keeps the agents of a host running (it always did)
  but the plan showed them stopped, and a switch onto an *agent* recipe - which the manager refuses - was
  drawn as a run. The plan follows the manager now.
- **A deleted recipe leaves nothing behind:** its state, log, previous log, exit code, stop marker and
  pending start measurement were left on disk, so a later recipe with the same container name inherited an
  old crash and old logs.
- **Copy free-space check and long copies:** a file with the right size but a known-wrong checksum is
  replaced, so the check counts room for both; a full disk now reads as "disk full" instead of a bare
  errno. A copy into a folder a running recipe mounts is refused (stop it first), and a transfer that makes
  no progress for 30 minutes gives up instead of polling forever.
- **The TensorFold example keeps the family's endpoint:** it served `qwen3.8-27b` while the rest of the
  Qwen3.8 family serves `qwen3.8`, so "Switch to" changed the model name clients had to send. It now
  serves `qwen3.8`; the example test checks the served name for native recipes as well.
- **Compose upgrades again:** `docker-compose.yml` pinned `v0.2.0` while the README promised `:latest` and
  an upgrade via `docker compose pull`. Compose now follows `:latest`; the release checklist says to move
  it with the *Docker latest* workflow after tagging.
- **pip installs seed and store data properly:** the wheel ships the seed example inside the package
  (`DGXM_SEED` works without the repository), and the default database path is `./data/manager.db`
  relative to the working directory as documented - not inside site-packages.
- **The Linux installer no longer requires Docker:** `install.sh` only warns when `docker info` fails
  (native-only setups work without Docker) and checks `${DGXM_DOCKER:-docker}`, so Podman users are not
  blocked. Both `install.sh` and `run.sh` now refuse Python < 3.10 with a clear message instead of
  failing later in the code; an existing venv on an older Python is recreated.
- **macOS stops on configuration errors instead of looping:** launchd's `KeepAlive` restarted the manager
  every few seconds when it refused a weak password or short token (exit 78). A generated wrapper now
  leaves a `data/.config-fault` marker on exit 78, and `KeepAlive`'s `PathState` holds the job stopped
  while the marker exists - fix `.env` and rerun `./install-macos.sh`. The installer also stops the
  running agent before rebuilding the venv, verifies the start via `/healthz` (like the Linux installer)
  and prints the log on failure; `uninstall-macos.sh` refuses to run under sudo.
- **The Flash-Next example respects Tailscale mode:** `qwen38-flash-next-fp4.yaml` hardcoded
  `--host 0.0.0.0` with `--network host`, so `DGXM_PUBLISH_IP=tailscale` did not limit its model port.
  It now uses `--host {bind_host}` like the other host-network example.
- **The Docker image builds on a tested Python:** the base image was `python:3.15-slim`, outside the CI
  matrix (3.10-3.13); it is now `python:3.13-slim`, with a comment to bump base and matrix together.
- **The Docker image reports its health:** a `HEALTHCHECK` against `/healthz` (30 s interval, 5 s timeout,
  3 retries) lets `docker ps` and Compose show a sick manager. SECURITY.md now also spells out that the
  Compose container runs as root - with the mounted Docker socket that is root on the host anyway.
- **Pre-release tags no longer move the published docs:** `docs.yml` deployed every `v*` tag as `stable`,
  so an RC like `v0.3.0-rc.1` would have replaced the release docs. Tags with a suffix now skip the
  stable deploy, matching the release workflow's image tags.
- **TensorFold live stats:** recipe cards and the Ports & endpoints table showed "waiting…" for TensorFold
  because its `/metrics` uses `tensorfold:` names. Tokens/s in and out, running / waiting requests, KV cache
  and prefix-cache hits are now read from them; generated tokens of requests still running are counted
  live (TensorFold adds them to its total only when a request ends). The KV-cache health finding now works
  for TensorFold too.
- **Weights tab size for symlinked models:** a Hugging Face download can keep its shards in `tensors/`
  and put a symlink of the published name next to them. `os.stat` follows those links, so the tab added
  the bytes of every shard to its own: a 102 GB model was listed as 204 GB. Each file now counts once,
  the way `du` counts it (`du` reports GiB, the tab GB). The list under "Found on your hosts" and the GB
  of a finished download use the same rule.
- **Copies wrote symlinked shards twice:** the share listed the link and its target as two files, and the
  target machine wrote both as real files - about 100 GB of duplicated weights for a model like that, and
  a free-space check that asked for twice what the copy needed. A link inside a share now travels as a
  link: it is not hashed, not transferred and recreated with `symlink` on the target, so the copy has the
  same layout as the source. Shares and older managers still work together: an old source sends links as
  files (as before), an old target downloads them as files.
- **One model, one row in the Weights tab:** `qwen3.8-27b-nvfp4` could be created next to
  `Qwen3.8-27B-NVFP4` (`UNIQUE` on the column is case-sensitive, and the tab sorts case-insensitively, so
  the two sat side by side), and two rows could point at the *same folder* on a host - the tab then offered
  two downloads and two copies of one thing on disk. Names are now unique whatever the case, a folder that
  another entry already has is refused by name, and paths are stored without the trailing slash that made
  `~/models/x` and `~/models/x/` look like two folders. The parent folder that holds model and drafter (the
  example recipes register `Qwen3.8-Models` / `Nemotron-3.5-Models`) stays its own row: that is a different
  folder, not a second spelling of one. Folders are also compared the way they name the same place, so
  `~/models/x`, `/home/u/models/x` and `~/models/./x` are one row now, and doubled slashes are cleaned up
  on upgrade.

**Web UI**
- **Blur is complete:** it covers the log dialog, the activity log, health findings, the detail dialogs and
  the confirmations/toasts, and masks IPv6 as well as IPv4 and host names - so screenshots and screen
  shares stay clean wherever a name appears.
- **A double click no longer creates two schedules or two transfer jobs:** every dialog disables its submit
  button while the request runs.
- **A slow poll no longer overwrites newer data:** an older answer that arrives late is dropped, so statuses
  do not jump back after an action.
- **Live logs reconnect:** a network blip retries three times with a "reconnecting…" state instead of
  ending the stream, and the end label says whether a container or a native process stopped.
- **The recipe list keeps your focus:** the ⏳ counter during a start updates only its own nodes instead of
  re-rendering every card each second (keyboard shortcuts keep working right after a click too).
- **Smaller fixes:** error toasts show the HTTP status instead of an empty box when the response has no
  body; job progress is escaped before it is shortened; the muted bug icon no longer hides *why* a start
  failed; the logs tooltip says "no logs yet (never started / host offline)"; client-side histories are
  bounded; the job poll pauses in a hidden tab; and dialogs ask before discarding unsaved changes.

### Removed
- **Rows/tiles leftovers:** the layout toggle was removed earlier, but its dead rendering code, CSS and
  README paragraph remained. The recipe list is tiles-only; the dead code and the paragraph are gone.
- **Example recipes:** `embeddinggemma-2-llamacpp.yaml`, `sglang-flint.sh` and `sglang-flint-docker.sh`. The
  formats they showed stay documented (compact files and starter scripts); `examples/hermes-agent/` keeps the
  `.sh` samples.

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

[Unreleased]: https://github.com/einfachllm/local-llm-manager/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/einfachllm/local-llm-manager/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/einfachllm/local-llm-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/einfachllm/local-llm-manager/releases/tag/v0.1.0
