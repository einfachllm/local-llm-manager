# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-30

### Upgrading from 0.1.0
- Upgrade the hub and all agents together: the new features need new agents.
- The TensorRT-LLM preset is gone: re-save such recipes once with *Custom* (they keep running until then).
- The database is upgraded on the first start; back it up (`DGXM_DB`) if you may want to go back.

### Added
**Several machines**
- **Agent self-registration:** an agent with `DGXM_HUB_URL` asks the hub to be added; approve it by
  comparing the join code.
- **Copy to other hosts:** roll one recipe out to several machines; copies stay in sync with the original.
- **Encryption per host:** the Hosts tab shows 🔒 Tailscale, 🔒 HTTPS or ⚠ plain HTTP.

**Weights and images**
- **Search hosts** for model folders and `.gguf` files, and register them in one click. Which folders are
  searched is set with `DGXM_MODEL_DIR` in `.env` (several allowed).
- **Weights tab** and **weights registry:** one entry per model with its path on each host; recipes use
  `{weights}` and work on any host.
- **Download on hosts** from Hugging Face (gated models: `HF_TOKEN` in `.env`) and **pull images** on
  several hosts at once.
- **Copy weights host to host:** download once, then machines copy from each other (SHA-256 checked,
  resumable). Recommended for getting the same model onto several machines.
- **Missing weights per host:** see on which hosts a recipe's weights are missing before you start or copy it.
- **Images tab** (experimental): which host has which image, and newer tags to switch to.

**Recipes**
- **⚡ Quick recipe:** a new recipe from a working one plus other weights, in one dialog.
- **Added Agents** (e.g. a Hermes gateway) as their own kind of recipe, apart from models.
- **TensorFold** preset; models shown as rows (or tiles).
- **Simpler moves between hosts:** change a recipe's host and its model folder follows: `{weights}`
  mounts the path that host has in the Weights tab, so you don't edit the volumes by hand.
- **More filters and grouping:** show All / Models / Agents, and group agents by host or tag like models.

**Operations and docs**
- **Self-monitoring:** `/healthz` and opt-in Prometheus `/metrics` (`DGXM_METRICS=1`).
- **Log retention:** old activity is pruned after `DGXM_RETENTION_DAYS` (default 90).
- **Documentation site** with Quick start, Several machines, How it works, Architecture and a
  configuration reference; recipe examples in `examples/`.

### Changed
- **One lock per host:** a slow action on one machine no longer blocks the others.
- **Recipe editor:** command preview beside the form, easier import (`i`).
- **Model cards:** show the model folder, served model name and endpoint copy button.
- A container killed for lack of memory now shows **failed** instead of "stopped".

### Fixed
- Empty recipe label in the command preview of an existing recipe.
- "KV" stats figure wrapping onto its own line; tab bar too wide on phones.
- Jobs panel losing a running copy after a laptop wakes from sleep; it now also shows jobs from other
  browsers and recently finished ones.

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
