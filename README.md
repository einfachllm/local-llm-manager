# DGX Spark Manager

[![CI](https://github.com/olekslev69/dgxspark-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/olekslev69/dgxspark-manager/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

A small web dashboard that runs on your DGX Spark (or a Mac) and lets you, from any browser on your network:

- **Keep model recipes**: a recipe is everything needed to `docker run` an inference server
  (vLLM, SGLang, TensorRT-LLM, llama.cpp or any custom image): image, model, ports, framework arguments,
  env, volumes and extra docker flags - or to start one directly on the host as a native process
  (MLX, llama.cpp, … - e.g. on a Mac Studio).
- **Start / stop / restart** a model. Restarting re-creates the container, so edits to a recipe take effect.
- **"Switch to"** a model: stop every other model, drop the page cache, then start the chosen one.
- **Watch logs** live (streamed from `docker logs -f`), filter and download them.
- **Schedule** actions with cron or one-shot times: start, stop, restart, switch, stop all, free memory.
- See an **activity log** of everything that was done, by you or by the scheduler.

Recipes, schedules and the activity log live in a SQLite file on the Spark (`data/manager.db`).
Model weights and framework images are assumed to already be on the machine.

It is inspired by the dashboard in [dataforgex/dgx_spark](https://github.com/dataforgex/dgx_spark),
but only covers the recipe/model-control part, and recipes are edited in the browser instead of in YAML.

## Install on the Spark

Requirements: Python 3.10+, Docker with the NVIDIA runtime, and your user in the `docker` group.

```bash
git clone https://github.com/olekslev69/dgxspark-manager.git
cd dgxspark-manager
./install.sh                                 # venv + systemd service, runs as your user
nano ~/.config/dgxspark-manager.env          # set DGXM_PASSWORD (and HF_TOKEN if needed)
sudo systemctl restart dgxspark-manager
```

Then open `http://<spark-ip>:8090` and log in with `admin` / your password.

Other ways to run it:

- **Foreground:** `cp .env.example .env && ./run.sh`
- **Docker Compose:** `cp .env.example .env && docker compose up -d` uses the prebuilt image
  `ghcr.io/olekslev69/dgxspark-manager:latest` (amd64 + arm64, built by CI); add `--build` to build it
  locally instead. It mounts the docker socket and uses host networking; `HOME` is passed through so `~`
  in recipe volumes points at your home on the host. In a container, use Docker recipes: native-process
  recipes would run inside the manager's container, not on the host.

> **Security:** anyone who can open the dashboard can run arbitrary containers on the Spark, which is
> effectively root. Always set `DGXM_PASSWORD`, and only expose the port on a trusted network
> (or behind a VPN / Tailscale / an HTTPS reverse proxy).

## One dashboard for several machines

One installation is the **hub**: it has the dashboard, the database (recipes, schedules, activity) and
the scheduler. Every other machine runs the same app in **agent mode**: no dashboard, just a small API
that runs what the hub sends. A typical setup is the Spark as hub and a Mac Studio as agent.

1. On the agent machine, in its `.env` (`~/.config/dgxspark-manager.env` with `install.sh`):
   ```bash
   DGXM_MODE=agent
   DGXM_AGENT_TOKEN=<output of: openssl rand -hex 32>
   ```
   then install/restart it (`./install-macos.sh` on a Mac, `./install.sh` on Linux).
2. In the hub's dashboard: **Hosts → Add host**, with the agent's URL (e.g. `http://mac-studio.local:8090`)
   and the same token. The host list shows whether it is online, its platform, memory and GPU.
3. Recipes get a **Host** field. Status, logs, start/stop, schedules and the timeline then work across
   all hosts. **Switch to** only stops other models on the same host; "Stop all" / "Free memory" (on the
   Hosts tab and as schedules) work on one host or on all of them. The recipe list can be filtered and
   grouped by host, and the header shows each host's memory.

Ports only conflict within one host. If a host is unreachable, its recipes show **Host offline** and
actions on them fail with the reason (e.g. a wrong token).

> **Security:** the agent token gives full control over models on that machine (effectively the
> agent user's rights). The hub talks to agents over plain HTTP, so keep them on a trusted network or a
> VPN such as Tailscale, and use a long random token. The hub never shows stored tokens in the UI.

## Install on a Mac (Mac Studio etc.)

On macOS, Docker containers cannot use the Apple GPU (Metal), so MLX does not work in a container
and llama.cpp in a container is CPU-only. Use **native-process recipes** there: the manager starts the
model server directly on the Mac (see [Native processes](#native-processes)).

Requirements: Python 3.10+ (`brew install python@3.12`), a current bash for script import
(`brew install bash`), and your model servers, e.g. `pip install mlx-lm` or `brew install llama.cpp`.

```bash
git clone https://github.com/olekslev69/dgxspark-manager.git
cd dgxspark-manager
./install-macos.sh            # venv + launchd agent; creates .env on first run
nano .env                     # set DGXM_PASSWORD
./install-macos.sh            # run again to restart with the password
```

The agent runs while your user is logged in (enable automatic login for a headless Mac Studio).
Its PATH is `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`; for a server inside a
virtualenv, use its absolute path in the recipe command (e.g. `~/venvs/mlx/bin/mlx_lm.server`) or set
`PATH=...` in the recipe's Environment. "Free memory" only stops the models on macOS (there is no
page-cache drop).

## Recipes

| Field | Meaning |
|---|---|
| Framework | Picks defaults for image, command, container port and health path. Everything stays editable. |
| Docker image | e.g. `nvcr.io/nvidia/vllm:25.11-py3`, `lmsysorg/sglang:spark` |
| Model | HF id or a path inside the container; substituted for `{model}` |
| Host port / container port | Published as `-p host:container`. The API is at `http://<spark>:<host port>/v1`. With `--network host` there is no `-p` and both ports are the same. |
| Command | Server command; placeholders `{model}`, `{port}` (container port), `{host_port}`, `{name}` |
| Framework arguments | Extra CLI args, one or more per line, `#` comments allowed. Quote JSON like `'{"a": 1}'`. |
| Environment | `KEY=value` per line. A bare `KEY` (e.g. `HF_TOKEN`) is passed through from the manager's environment, so secrets are not stored in the DB. |
| Volumes | `host:container` per line; `~` and `$VARS` are expanded. |
| Extra docker flags | Defaults to the Spark-recommended `--ipc=host --ulimit memlock=-1 --ulimit stack=67108864`. |
| GPUs | Value for `--gpus` (empty to omit). |
| Health path | Polled on the host port to tell *loading* from *ready*. Empty disables the check. |
| Est. memory | If set, starting is refused when less memory is free (you can force it). |
| Tags / Card colour | Organisation only: tags for filtering and grouping, a colour (green, black, …) to tell cards apart at a glance. |

The editor shows a live preview of the exact `docker run` command. Every container the manager starts is
named after the recipe's container name and labelled `dgxspark-manager=1`.

Status values: **Stopped**, **Starting**, **Loading model** (container up, health check not passing yet),
**Ready**, **Failed** (container exited with an error; check its logs).

Stopping keeps the stopped container so its logs stay readable; the next start removes and re-creates it.

### Ports and endpoints

Each recipe has a port per host; a running model's API is at `http://<host>:<port>/v1`.

- **Hosts → Ports & endpoints** lists every recipe by host and port, with status, its endpoint and a
  Copy button. Recipes sharing a port on the same host are marked *shared* (only one of them can run).
- In the editor, the port field says whether another recipe on that host uses the port (and whether it is
  running), and **Next free** picks the first port from 8100 that no recipe on that host uses.
- Start is refused if a running recipe on the same host has the port, or if *any other program* is
  already listening on it on that host (e.g. a model started by hand), with a message saying so.
- The same port can be used on different hosts.

### Native processes

A recipe's **Runtime** is either *Docker container* or *Native process*. Native recipes run the
command directly on the host, without Docker:

- Presets: **MLX LM server** (`mlx_lm.server --model {model} --host 0.0.0.0 --port {port}`),
  **llama.cpp server (native)** (`llama-server -m {model} …`) and **Custom (native)**.
- Fields used: command, framework arguments, environment (`KEY=value`, `~` is expanded), model, port,
  health path. Image, volumes, docker flags, GPUs and the container port do not apply.
- Each process runs in its own process group; Stop sends SIGTERM to the group and SIGKILL after
  `DGXM_STOP_TIMEOUT` seconds. Output goes to `data/processes/<name>.log` (the previous run's log is
  kept as `.log.1`) and shows up in **Logs** like container logs.
- Processes keep running when the manager restarts (systemd `KillMode=process`, launchd
  `AbandonProcessGroup`) and are recognised again by PID + start time. If a server exits with an
  error, the card shows **Failed** with its exit code.
- Switch, Stop all, schedules and the timeline treat both runtimes the same.
- **Import script…** also recognises scripts that start `mlx_lm.server`, `mlx_lm server`, `llama-server`,
  `vllm serve` or `python -m sglang.launch_server` directly and creates a native recipe from them.

### Tags, filter, sort

Each recipe can have tags (comma-separated in the editor). Above the recipe grid you can:

- **filter** by clicking tags (several tags = recipes that have all of them; tags on cards are clickable too),
- **search** name, model, image, container and tags,
- **sort** by name, status (running first), framework, port or last edit,
- **group by first tag**: each recipe appears once, under its first tag.

A handy pattern for several variants of one model: put the model name first (`qwen3.8-flash, fp4, 128k`),
then **Duplicate** a recipe for each variant (tags are copied) and change what differs. The view settings
are remembered in the browser.

### Importing a starter script

In the recipe editor, **Import script…** takes a starter script (or a plain `docker run …` command) and
fills the form from it. The script is not parsed as text: bash runs it so that defaults like
`${PORT:-8081}`, `if`/`case` branches and computed values (e.g. a generated `--hf-overrides` JSON) come
out exactly as the script would have run them. The resulting `docker run` command is then split up:

- `-e` → Environment (an empty `KEY=` becomes a bare `KEY`, passed through from the manager's environment),
  `-v` → Volumes (your home directory becomes `~`), `--gpus`, `--name`, `-p` → ports;
  all other docker options (`--network host`, `--entrypoint`, `--shm-size`, …) → Extra docker flags.
  `-d`, `--rm`, `-it` are dropped because the manager runs containers detached itself.
- The container command is split into **Command** (`serve {model} --host … --port {port}`) and one
  **Framework arguments** line per flag. The model is the argument after `serve`, or the value of
  `--model` / `--model-path`.
- With `--network host`, no `-p` is added and host port = the server's `--port`.

**Safety.** Importing happens in two layers:

1. **Check (static, nothing runs).** *Check script* lists every risky call with its line number, in
   one list. Each entry says whether it is *skipped during import* (`rm`, `curl`, `sudo`, `ssh`,
   `pip install`, `python -c`, `mkdir`, …) or *not blocked during import* (programs called by absolute
   path like `/bin/rm`, writing to files, changing `PATH`, `/dev/tcp`, `curl | sh`, `awk system()`,
   `sed -i`). Text inside quotes (e.g. `echo "run sudo …"`) is ignored. If any risk is found, the import
   needs a tick on "I have reviewed the risks and trust this script" (logged in Activity).
2. **Restricted run.** `PATH` only contains (and cannot be changed, e.g. by `source venv/bin/activate`) read-only text tools (`awk`, `sed`, `grep`, `cut`, `tr`,
   `date`, `cat`, …) and a fake `docker` that records `docker run`. Every other command (`rm`, `curl`,
   `mkdir`, `nvidia-smi`, `docker ps`, …) is **recorded and skipped, not executed**; the editor then
   lists what was skipped. A 20 s time limit applies.

The check is a line-based heuristic, not a full shell parser, and the restricted run is not a sandbox
(once you confirm, the calls marked *not blocked* really run with your user's rights). Only import scripts you trust.

## Schedules

Times use the hub's local time zone. Each schedule is either a repeat (pick a time and weekdays,
or write a cron expression such as `0 22 * * 1-5`) or a one-shot date and time.

| Action | Does |
|---|---|
| Start / Stop / Restart model | What the buttons do |
| Switch to model | Stop all other models on the same host, drop its page cache, start this one |
| Stop all models | Stop every model with a recipe, on one host or all hosts |
| Stop all models and free memory | Stop all, then `sync; echo 3 > /proc/sys/vm/drop_caches` (one host or all) |

**Planned runs timeline.** Above the schedule list, a chart shows the next 24 hours, 3 days or 7 days:
one row per model, a block where the model will be running (1) and a gap where it is stopped (0).
It starts from the models running right now and plays the enabled schedules forward (a "switch" ends the
other models' blocks, "stop all" / "free memory" are dashed red lines). It assumes every action succeeds,
so it is a plan, not a guarantee. Hover a block for its times and the schedules that start/stop it; click
it to edit the schedule.

Dropping the page cache needs root: the manager tries directly, and otherwise runs it in a short-lived
privileged `busybox` container. If the manager was down when a repeating schedule was due, that run is
skipped; a missed one-shot runs if it is less than 10 minutes late.

## Configuration

| Variable | Default | |
|---|---|---|
| `DGXM_PORT` / `DGXM_HOST` | `8090` / `0.0.0.0` | Listen address |
| `DGXM_USER` / `DGXM_PASSWORD` | `admin` / *(empty = no auth)* | HTTP Basic auth |
| `DGXM_DB` | `./data/manager.db` | SQLite file |
| `DGXM_PROC_DIR` | `<DB folder>/processes` | State and log files of native processes |
| `DGXM_HEALTH_HOST` | `127.0.0.1` | Host used for model health checks |
| `DGXM_STOP_TIMEOUT` | `30` | Seconds `docker stop` waits before killing |
| `DGXM_DROP_CACHES_IMAGE` | `busybox:latest` | Image for the privileged cache drop |
| `DGXM_SEED` | `1` | Create two example recipes on an empty database |
| `DGXM_MODE` | `hub` | `agent`: no dashboard, only the agent API for a hub on another machine |
| `DGXM_AGENT_TOKEN` | *(empty)* | Token the hub must send to this agent (required in agent mode) |

## API

Everything the UI does goes through a JSON API; interactive docs are at `/docs`. Examples:

```bash
curl -u admin:$PW http://spark:8090/api/recipes
curl -u admin:$PW -X POST http://spark:8090/api/recipes/1/start
curl -u admin:$PW -X POST http://spark:8090/api/recipes/1/switch
curl -u admin:$PW "http://spark:8090/api/recipes/1/logs?tail=200"
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
DGXM_DB=/tmp/dev.db python -m dgxspark_manager.main
```

Layout: `dgxspark_manager/` holds the FastAPI app (`main.py`), recipe → `docker run` translation
(`recipes.py`), script import and its safety check (`importer.py`, `script_check.py`), lifecycle rules (`manager.py`), where things run (`executor.py`: this machine or a remote agent; agent API in
`agent.py`; native processes in `processes.py`), the scheduler (`scheduler.py`) and its timeline projection (`timeline.py`), SQLite storage (`db.py`)
and the static UI (`static/`, plain HTML/JS with no build step). Tests use a fake docker client.

### CI

`.github/workflows/ci.yml` runs the tests on Python 3.10, 3.12 and 3.13 for every push and pull request,
then builds the Docker image for `linux/amd64` and `linux/arm64` and smoke-tests it. On `main` it pushes
`ghcr.io/olekslev69/dgxspark-manager:latest` (plus a `sha-…` tag); a git tag `v1.2.3` also publishes
`1.2.3` and `1.2`. Pull requests only build. After the first push, check the package's visibility under
*GitHub → Packages*, if it should be pullable without `docker login ghcr.io`.

## License

Apache License 2.0 – see [LICENSE](LICENSE) and [NOTICE](NOTICE).
