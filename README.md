# DGX Spark Manager

A small web dashboard that runs on your DGX Spark and lets you, from any browser on your network:

- **Keep model recipes**: a recipe is everything needed to `docker run` an inference server
  (vLLM, SGLang, TensorRT-LLM, llama.cpp or any custom image): image, model, ports, framework arguments,
  env, volumes and extra docker flags.
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
- **Docker Compose:** `cp .env.example .env && docker compose up -d --build`
  (mounts the docker socket and uses host networking; `HOME` is passed through so `~` in recipe volumes
  points at your home on the host).

> **Security:** anyone who can open the dashboard can run arbitrary containers on the Spark, which is
> effectively root. Always set `DGXM_PASSWORD`, and only expose the port on a trusted network
> (or behind a VPN / Tailscale / an HTTPS reverse proxy).

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

1. **Check (static, nothing runs).** *Check script* lists unusual calls with line numbers:
   *high risk* for things the restricted run below cannot block (programs called by absolute path like
   `/bin/rm`, writing files outside the import folder, changing `PATH`, `/dev/tcp`, `curl | sh`,
   `awk system()`, `sed -i`); *unusual* for `rm`, `curl`, `sudo`, `ssh`, `pip install`, `python -c`, …;
   *notes* for `docker ps/rm/logs`, `eval`, `source`. Text inside quotes (e.g. `echo "run sudo …"`) is
   ignored. An import with high-risk findings is refused unless you tick "I trust this script"
   (and that is logged in Activity).
2. **Restricted run.** `PATH` only contains read-only text tools (`awk`, `sed`, `grep`, `cut`, `tr`,
   `date`, `cat`, …) and a fake `docker` that records `docker run`. Every other command (`rm`, `curl`,
   `mkdir`, `nvidia-smi`, `docker ps`, …) is **recorded and skipped, not executed**; the editor then
   lists what was skipped. A 20 s time limit applies.

The check is a line-based heuristic, not a full shell parser, and the restricted run is not a sandbox
(a trusted high-risk script can still do anything your user can). Only import scripts you trust.

## Schedules

Times use the Spark's local time zone. Each schedule is either a repeat (pick a time and weekdays,
or write a cron expression such as `0 22 * * 1-5`) or a one-shot date and time.

| Action | Does |
|---|---|
| Start / Stop / Restart model | What the buttons do |
| Switch to model | Stop all other managed models, drop the page cache, start this one |
| Stop all models | Stop every model that has a recipe |
| Stop all models and free memory | Stop all, then `sync; echo 3 > /proc/sys/vm/drop_caches` |

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
| `DGXM_HEALTH_HOST` | `127.0.0.1` | Host used for model health checks |
| `DGXM_STOP_TIMEOUT` | `30` | Seconds `docker stop` waits before killing |
| `DGXM_DROP_CACHES_IMAGE` | `busybox:latest` | Image for the privileged cache drop |
| `DGXM_SEED` | `1` | Create two example recipes on an empty database |

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
(`recipes.py`), script import and its safety check (`importer.py`, `script_check.py`), lifecycle logic (`manager.py`), the scheduler (`scheduler.py`) and its timeline projection (`timeline.py`), SQLite storage (`db.py`)
and the static UI (`static/`, plain HTML/JS with no build step). Tests use a fake docker client.
