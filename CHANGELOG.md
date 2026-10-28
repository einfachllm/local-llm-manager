# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Agents apart from models:** a recipe is either a *model* (an LLM server) or an *agent* (a program that
  uses one, e.g. a Hermes gateway; new `kind` field, default `model`). The Recipes tab shows two sections,
  🤖 Agents and 🧠 Models, with their own counts and "+ New" buttons, and an All / Models / Agents filter.
  Agents are wide rows instead of tiles: running or not and since when, the model they use, no endpoint or
  engine metrics. **Switch to** keeps agents running and is not offered for them (the API answers 409);
  "stop all" and "free memory" still stop them. A new agent defaults to no GPUs and no health check.
  An agent card shows each passed-through secret name with ✓/✗ (whether it is set in the manager's
  environment on that host; values are never sent) and, for native agents, warns when another copy of
  the same command runs outside the manager (e.g. a detached second gateway on the same bot token).
  Grouped by host or tag, each group stacks its agents above its models, and its "+ New" buttons create
  the recipe on that host.
- **Model rows:** models are shown as wide rows like agents (🧠 icon, the card's facts side by side) by
  default; "Models as: Rows / Tiles" in the filter rail switches back to the card grid (remembered per
  browser).
- **Self-monitoring:** `GET /healthz` (hub and agent, no login) answers `ok` while the process is up and
  the database reachable (503 otherwise), and with `DGXM_METRICS=1` `GET /metrics` serves the manager's
  own metrics in Prometheus format (uptime, request/action counts, stored recipes/hosts) - no recipe or
  host data.
- **Activity-log retention:** events and start-time measurements older than `DGXM_RETENTION_DAYS`
  (default 90 days, `0` = keep all) are pruned on start-up, and the database records its schema version
  (`PRAGMA user_version`).
- `examples/hermes-agent`: guide to run Hermes Agent (Telegram, local faster-whisper STT and Piper TTS) on an
  LLM recipe, with the Hermes gateway as a native-process recipe, tool-calling flags and security notes.
- `examples/telegram-voice-bot`: a Telegram bot (voice and text) with a local Whisper server via Docker
  Compose, using an LLM recipe of this manager. User allow-list, conversation memory, outbound-only.
- **Images** tab (shortcut `5`): a matrix of the Docker images used by recipes (rows, tags of one image grouped
  together) and the hosts (columns): `✓ 23d` when the host has the image, `✗ missing`, and `⬆ tag` when a newer
  tag of the same image is already on that host - one click switches that host's recipes to it. Search, host
  filter, "Newer available" / "Missing on a host" filters and a summary; the recipes per image are collapsed
  into a count that expands per host. Scales to ~100 recipes on 5 hosts on one screen. Agents get
  `GET /agent/v1/images` (older agents are reported as too old to list images).

### Changed
- **Recipe editor:** the command preview sits in its own column beside the form and stays in view while
  you scroll (stacked below it on narrow screens), shows docker's and the framework's flags in two colours,
  and has a Copy button. Importing is easier to find: an "📋 Import docker run / script…" button on the
  Recipes tab (shortcut `i`), a highlighted button in the editor head, and a prompt at the top of a new recipe.
- **Model cards:** "Model" shows the mounted folder's name instead of a container path like `/model`
  (full path in the tooltip), plus the `--served-model-name` clients use, the image (shortened) and a
  copy button for the endpoint. Start/Stop stay on the left; Duplicate and Delete are icon buttons.
- A stopped Docker container that exits with 137/143 (what `docker stop` usually produces) is now
  recognised as a clean stop only when the manager itself stopped it (recorded in a small state
  file next to the process logs). A container the kernel OOM-killed exits the same way but without
  that record, so it now shows **failed** instead of a misleading "stopped".

### Fixed
- The command preview of an existing recipe showed an empty `local-llm-manager.recipe=` label; it now
  shows the recipe's id, as `start` sets it.
- The "KV" figure in a card's stats line no longer drops onto its own line (it picked up the layout of
  the card's fact list).
- On phones, the tab bar wraps instead of making the page wider than the screen.

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

[Unreleased]: https://github.com/einfachllm/local-llm-manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/einfachllm/local-llm-manager/releases/tag/v0.1.0
