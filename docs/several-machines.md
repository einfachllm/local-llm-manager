# Several machines

One installation is the **hub**: it serves the dashboard and stores recipes, schedules and the activity
log. Every other machine runs the same app as an **agent**, which only carries out what the hub tells
it to. Only two things go the other way: an agent can *ask* the hub to add it, and machines copy weights
straight from each other once the hub has arranged it. The [Architecture](architecture.md) page shows
all of this as diagrams.

```text
 your browser ──:8090──▶  HUB (dashboard, recipes, schedules)
                           │
                           ├──:8090──▶  AGENT  e.g. Mac Studio
                           └──:8090──▶  AGENT  e.g. second Spark

 apps using a model ──recipe port, e.g. :8100──▶  the machine running it
```

## Hub and agents in short

| | Hub | Agent |
|---|---|---|
| Runs | the dashboard, the database (recipes, weights, schedules, activity) | only what the hub asks: start/stop, status, logs, pulls and copies |
| `.env` | `DGXM_PASSWORD` (the installer sets it) | `DGXM_MODE=agent`, `DGXM_AGENT_TOKEN` (≥ 32 characters, the installer generates it), optionally `DGXM_HUB_URL` |
| You open it | in the browser, `http://<hub>:8090` | never - its start page only shows its join code |

There are two ways to connect an agent, both protected by the agent's **token**:

- **You add it on the hub:** **Hosts → + Add host** with the agent's URL and its token.
- **It asks to join:** the agent gets `DGXM_HUB_URL` and announces itself; the hub lists it under
  **Asking to be added** with a **join code** derived from its token, and you approve it when the code is
  the same as on the agent. The token itself never has to be typed or copied.

## Add a machine

1. Install the manager on the new machine as in the [Quick start](quick-start.md).
2. In that machine's `.env`, uncomment these two lines and run the installer again. The installer has
   already filled in a random token of at least 32 characters.

    ```bash
    DGXM_MODE=agent
    DGXM_AGENT_TOKEN=3f9c…   # generated
    ```

3. In the hub's dashboard, open **Hosts → + Add host**. Enter the agent's URL (e.g.
   `http://mac-studio:8090`) and the same token.

!!! tip "Or let the machine ask to be added"
    Instead of step 3, also set `DGXM_HUB_URL=http://<hub>:8090` in the new machine's `.env` and run the
    installer again. It prints a **join code** (e.g. `K7Q2-X9PM`), and the machine appears on the hub under
    **Hosts → Asking to be added** with a code. Click **Codes match – add** only if both codes are the
    same: another machine announcing the same name has a different code. Rejected machines are blocked for a
    week; requests nobody answers expire after an hour without news from the machine. The machine shows its
    code also on its start page (`http://<machine>:8090`) and in its log. Optional: `DGXM_AGENT_URL` (how
    the hub reaches it, default `http://<its name>:8090`) and `DGXM_AGENT_NAME`. If its address changes
    later, the machine tells the hub itself.

The new machine appears in the host strip at the top and in the Hosts graph. Recipes now have a **Host**
field that decides where they run.

## The Hosts tab

The tab opens with a **graph of your setup**: the hub on top and a line to every agent PC. Each machine
shows what matters at a glance:

- **online / offline**, operating system and CPU architecture (e.g. `Linux · aarch64`);
- a **memory bar** (used / total GB), **GPU load** and **GPU and CPU temperature** (the CPU value turns yellow from 85 °C, red
  from 95 °C);
- what **runs there**: models (🧠, with their port) and agents (🤖) with a live status dot, plus the number
  of stopped recipes (hover for their names).

The line colour shows the connection: **green** for Tailscale / HTTPS, **yellow** for plain HTTP,
**red dotted** for a machine that does not answer. Click a machine to edit or remove it, or a recipe to
open it. On a phone the machines stack along one line.

Below the graph:

- **Host details & actions** (collapsed): the host table with status, platform, memory, GPU / CPU and
  models, plus **Stop all** and **Free memory** (stop all models and drop the page cache) per machine.
- **Ports & endpoints:** every recipe's port per host, its status, live **metrics** of running models
  (output/input tokens per second, running requests, KV-cache use) and its endpoint
  `http://<host>:<port>/v1` with a **Copy** button. Recipes that share a port on one host are marked
  *shared* - only one of them can run at a time, which is exactly what **Switch to** uses.

!!! info "Keep versions in sync"
    Upgrade the hub and all agents to the same version.

## The same recipe on several machines

E.g. a company where every employee has their own DGX Spark: set the recipe up once, then roll it out.

1. Make sure the model is on those machines: [Weights](weights.md) downloads it once and copies it to
   the others.
2. On the recipe's card click **⇉** (*Copy to other hosts*) and tick the machines. For each one the dialog
   checks first whether the weights (Weights tab) and the Docker image are there, and whether another
   recipe uses the same port.
3. **Copy**: every machine gets its own copy, named `<recipe> · <host>`, with the same settings and port -
   so every employee has the same endpoint `http://<their-spark>:<port>/v1` and the same model name.
4. Later, edit the **original** (new image, other flags) and save: the manager offers to update all copies.
   Running copies are listed and can be restarted right away: the hub restarts them on all machines at
   the same time, also if you close the browser, and a panel shows the progress per machine. Cards show **✓ in sync** or **⟳ differs**,
   with **sync** to catch up.

**Images and weights on every machine:**

- **Images:** in the **Images** tab, **⤓ Pull…** on an image runs `docker pull` on the machines you tick,
  all at once (private registries: `docker login` on each machine once).
Details on the Weights tab: [Weights](weights.md).

- **Weights from Hugging Face:** in the **Weights** tab, **⬇ Download from Hugging Face…**. By default only the first
  ticked machine downloads; the others then copy from it over your network, so the model comes over the
  internet once. Needs the `hf` command there (`pip install -U huggingface_hub`); gated models need
  `HF_TOKEN` in that machine's `.env`.
- **Weights already on a machine** (e.g. from your SSD): **⇉ Copy to other hosts…** on the model's row. Machines copy
  straight from each other - the hub only arranges it - like a snowball: each machine that has the weights
  serves one copy at a time and every finished machine joins in, so 5 machines are done in 3 rounds.
  Every file is checked with SHA-256, files a machine already has are skipped, an interrupted copy
  continues where it stopped, and free disk space is checked first. The jobs panel shows GB, MB/s and
  the time left per machine.

The machines must reach each other at the addresses in the Hosts tab. To copy *from* the hub's own
machine, open the dashboard by its network name (not `localhost`) or set `DGXM_PUBLIC_URL`. Use Tailscale
names as addresses so copies are encrypted - the copy dialog warns about hosts reached over plain HTTP.

A copy keeps only its name, container name, host and pin; everything else follows the original. To change
one copy for good, **unlink** it: it becomes a recipe of its own. Deleting the original unlinks its copies.

## Network access

| From | To | Why |
|---|---|---|
| Your browser | hub, port `8090` | The dashboard |
| Hub | each agent, port `8090` | Start and stop models, status, logs (plain HTTP plus the token) |
| Apps using a model | the machine running it, the recipe's port (e.g. `8100`) | The model's API at `http://<machine>:<port>/v1` |
| Agent | hub, port `8090` | Only with `DGXM_HUB_URL`: asking to be added, reporting a changed address |
| Agent | other agents, port `8090` | Copying weights (read-only share with a ticket) |

!!! warning
    Keep all of this inside one private network – ideally Tailscale, see below – and never expose these
    ports to the internet. Traffic between hub and agents, and between agents copying weights, is plain HTTP unless you use Tailscale or HTTPS addresses.

## Recommended: Tailscale

[Tailscale](https://tailscale.com) puts all your machines and your phone into one private, encrypted
network. The dashboard, the agents and the model APIs are then reachable from anywhere, but only by your
own devices, and the hub-to-agent traffic is encrypted.

1. Install Tailscale on every machine (hub and agents) and on your phone, logged in to the same tailnet.
2. On every machine, add to its `.env` and restart the manager:

    ```bash
    DGXM_HOST=tailscale         # the manager listens only on this machine's Tailscale IP
    DGXM_PUBLISH_IP=tailscale   # model ports too
    ```

3. Add agents under **Hosts → + Add host** by their Tailscale name, e.g. `http://mac-studio:8090`.
4. On your phone, open `http://<hub-name>:8090` and use *Add to Home Screen*. For HTTPS, run
   `tailscale serve --bg 8090` on the hub and open `https://<hub-name>.<tailnet>.ts.net`.

`tailscale` is resolved to the machine's Tailscale IPv4 address at start-up. If Tailscale is not up yet,
the service fails and systemd or launchd retries it.

!!! tip
    Keep `DGXM_PASSWORD` set anyway: your phone's browser visits other sites too.

## Troubleshooting

- **Host shows "not reachable":** check that the agent runs – on Linux `systemctl status local-llm-manager`,
  its log with `journalctl -u local-llm-manager` – and that the hub can reach the agent's port `8090`.
- **"rejected the agent token":** the token under **Hosts** must equal `DGXM_AGENT_TOKEN` in the agent's
  `.env`.
- **The service does not start after setting the token:** agent tokens must be at least 32 characters.
