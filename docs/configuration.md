# Configuration reference

Everything is set in the **`.env` file in the install folder** (used by `install.sh`, `install-macos.sh`,
`run.sh` and Docker Compose). Copy [`.env.example`](https://github.com/einfachllm/local-llm-manager/blob/main/.env.example) to `.env`; the installers do this for
you and fill in secrets. Restart the service after changes (`sudo systemctl restart local-llm-manager`,
or rerun the installer).

Values marked *agent* matter only with `DGXM_MODE=agent`, *hub* only in the default hub mode.

## Dashboard and network

| Variable | Default | Description |
|---|---|---|
| `DGXM_HOST` | `0.0.0.0` | Listen address of the dashboard / agent API. `tailscale` = only this machine's Tailscale IPv4 (resolved at startup; the manager refuses to start when Tailscale is not connected). |
| `DGXM_PORT` | `8090` | Listen port. |
| `DGXM_USER` | `admin` | HTTP Basic username for the dashboard. |
| `DGXM_PASSWORD` | *(empty)* | Dashboard password, at least `12` characters (shorter values or `change-me` refuse to start). **Empty = no login**: the manager only answers to requests addressed by IP, `localhost` or a local/Tailscale name - never by an arbitrary Host header. Always set it when other machines can reach the port: the manager can start arbitrary containers. |
| `DGXM_PUBLISH_IP` | *(empty = all interfaces)* | Address model ports are published on: `docker run -p <ip>:<port>:…` and what `{bind_host}` expands to in native / host-network commands. `tailscale` = this machine's Tailscale IPv4. |
| `DGXM_HEALTH_HOST` | `DGXM_PUBLISH_IP`, else `127.0.0.1` | Host the manager probes model health endpoints on (the published port is appended). Set it when models are only reachable under a different address than the probe default. |

## Modes: hub and agent

| Variable | Default | Description |
|---|---|---|
| `DGXM_MODE` | `hub` | `hub`: dashboard, database and scheduler. `agent`: no dashboard, no database - only the token-protected agent API, so a hub on another machine runs models here. |
| `DGXM_AGENT_TOKEN` | *(empty)* | The Bearer token a hub must send to this machine's agent API; required in agent mode, at least `32` characters (`openssl rand -hex 32`). Enter the same value on the hub under **Hosts**. The token grants full control over this machine - keep it secret. |

## Models and recipes

| Variable | Default | Description |
|---|---|---|
| `DGXM_MODEL_DIR` | `~/models` | Directories scanned for local model folders and `.gguf` files (Weights tab, "Search hosts", presence checks). Comma- or whitespace-separated, several allowed, searched up to three levels deep. |
| `DGXM_HF_BIN` | `hf`, else `huggingface-cli` | Hugging Face CLI used by "Download on hosts" (Weights tab); downloads go into the first `DGXM_MODEL_DIR`. Gated models need `HF_TOKEN` in this machine's `.env`. |
| `DGXM_PUBLIC_URL` | the address the dashboard is opened with | Where the other machines reach this hub, e.g. `http://spark1:8090` - needed to copy weights *from* the hub's own machine when you open the dashboard as `localhost`. |
| `DGXM_HUB_URL` | – | Agent mode: ask this hub to add the machine; approve it under **Hosts** there when the join codes match. |
| `DGXM_AGENT_URL` | `http://<DGXM_HOST or machine name>:<DGXM_PORT>` | Agent mode: the address the hub should use for this machine when it asks to join. |
| `DGXM_AGENT_NAME` | the machine's name | Agent mode: the name it asks to be listed under. |
| `DGXM_DOCKER` | `docker` | Docker binary used to run Docker recipes - e.g. point it at `podman` where Docker is unavailable. |
| `DGXM_DROP_CACHES_IMAGE` | `busybox:latest` | Image of the throwaway container that drops the page cache (`sync; echo 3 > /proc/sys/vm/drop_caches`) when the manager itself lacks permission. |
| `DGXM_STOP_TIMEOUT` | `30` | Seconds a stop waits for a container / process to shut down cleanly before killing it. |
| `DGXM_SEED` | `1` | `1` adds two example recipes to an *empty* database on first start; `0` starts with none. |

## Storage

| Variable | Default | Description |
|---|---|---|
| `DGXM_DB` | `./data/manager.db` | SQLite database (recipes, hosts, events). Readable by its owner only. |
| `DGXM_PROC_DIR` | `<db folder>/processes` | State and log files of native-process recipes. |
| `DGXM_RETENTION_DAYS` | `90` | Days to keep activity-log entries and start-time measurements; pruned on startup. `0` keeps everything. |

## Scheduler and monitoring

| Variable | Default | Description |
|---|---|---|
| `DGXM_SCHEDULER_TICK` | `10` | Seconds between scheduler passes (hub). |
| `DGXM_ONCE_GRACE_SECONDS` | `600` | Grace period for one-shot schedules ("start once at 06:00") that were missed while the manager was down; older misses are skipped. |
| `DGXM_METRICS` | *(off)* | `1`/`true`/`yes` serves the manager's own metrics at `GET /metrics` in Prometheus format (uptime, request and action counts, stored recipes/hosts - no recipe or host data). `GET /healthz` is always available and needs no login. |

## Secrets passed to recipes (no `DGXM_` prefix)

A recipe's environment lists secret **names** only (one per line, e.g. `HF_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`EINFACHLLM_API_KEY`). At start the manager looks each name up in **its own environment** - which comes
from the `.env` file - and passes the value into the container or native process. Values therefore never
land in a recipe, the database or the dashboard; an agent card shows ✓/✗ per name for the host it runs on.

```bash
# .env on the manager's host
HF_TOKEN=hf_…                        # used by recipes that list the bare name HF_TOKEN
TELEGRAM_BOT_TOKEN=123456:ABC…       # used by the Hermes gateway recipe, for example
```

Any variable name works: put the value in `.env`, list the name in the recipe. `HF_TOKEN` is the
canonical example (Hugging Face downloads); see [examples/hermes-agent](https://github.com/einfachllm/local-llm-manager/blob/main/examples/hermes-agent/README.md)
for a real setup.

> Note for containers talking to models on the host: `127.0.0.1` inside a container is the container
> itself. Agent recipes that call a model on the manager's host need `http://host.docker.internal:<port>/v1`
> (or the bridge gateway `172.17.0.1`) - and what the *program's own config file* says wins over variables
> passed in here (for Hermes: `model.base_url` in `~/.hermes/config.yaml`).

## See also

- [`.env.example`](https://github.com/einfachllm/local-llm-manager/blob/main/.env.example) - the starter file with the few keys that matter on first install.
- [examples/hermes-agent](https://github.com/einfachllm/local-llm-manager/blob/main/examples/hermes-agent/README.md) - LLM + agent recipes with passed-through secrets.
- OpenAPI docs of the JSON API: `http://<machine>:8090/docs`.
