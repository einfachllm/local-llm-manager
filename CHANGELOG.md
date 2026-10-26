# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `ghcr.io/einfachllm/local-llm-manager` (amd64 + arm64), `pip install .`.

### Security
- HTTP Basic auth with a password policy (at least 12 characters, no well-known passwords) and
  lockout after 10 failed logins in 10 minutes; the installers generate a random password and agent token.
- Agents require a token of at least 32 characters.
- Cross-site requests that change something are rejected (CSRF), host names are checked against DNS
  rebinding when no password is set, and security headers prevent framing.
- The database (it stores agent tokens) is readable by its owner only.
- See [SECURITY.md](https://github.com/einfachllm/local-llm-manager/blob/main/SECURITY.md) for the threat model and how to report vulnerabilities.

[Unreleased]: https://github.com/einfachllm/local-llm-manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/einfachllm/local-llm-manager/releases/tag/v0.1.0
