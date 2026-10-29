# Several machines

One installation is the **hub**: it serves the dashboard and stores recipes, schedules and the activity
log. Every other machine runs the same app as an **agent**, which only carries out what the hub tells
it to. Agents never connect to the hub.

```text
 your browser ──:8090──▶  HUB (dashboard, recipes, schedules)
                           │
                           ├──:8090──▶  AGENT  e.g. Mac Studio
                           └──:8090──▶  AGENT  e.g. second Spark

 apps using a model ──recipe port, e.g. :8100──▶  the machine running it
```

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

The new machine appears in the host strip at the top. Recipes now have a **Host** field that decides where
they run.

!!! info "Keep versions in sync"
    Upgrade the hub and all agents to the same version.

## The same recipe on several machines

E.g. a company where every employee has their own DGX Spark: set the recipe up once, then roll it out.

1. On the recipe's card click **⇉** (*Copy to other hosts*) and tick the machines. For each one the dialog
   checks first whether the weights (Weights tab) and the Docker image are there, and whether another
   recipe uses the same port.
2. **Copy**: every machine gets its own copy, named `<recipe> · <host>`, with the same settings and port -
   so every employee has the same endpoint `http://<their-spark>:<port>/v1` and the same model name.
3. Later, edit the **original** (new image, other flags) and save: the manager offers to update all copies.
   Running copies are listed and can be restarted right away: the hub restarts them on all machines at
   the same time, also if you close the browser, and a panel shows the progress per machine. Cards show **✓ in sync** or **⟳ differs**,
   with **sync** to catch up.

**Images and weights on every machine:** in the **Images** tab, **⤓ Pull…** on an image runs `docker pull`
on the machines you tick (all at once). In the **Weights** tab, **⬇ Download on hosts…** downloads a
Hugging Face model on each machine into its model folder and registers it there, so the copies find their
weights. Each machine downloads by itself and keeps going if the hub's connection drops. It needs the
`hf` command there (`pip install -U huggingface_hub`); gated models need `HF_TOKEN` in that machine's
`.env`, private registries a `docker login` there.

A copy keeps only its name, container name, host and pin; everything else follows the original. To change
one copy for good, **unlink** it: it becomes a recipe of its own. Deleting the original unlinks its copies.

## Network access

| From | To | Why |
|---|---|---|
| Your browser | hub, port `8090` | The dashboard |
| Hub | each agent, port `8090` | Start and stop models, status, logs (plain HTTP plus the token) |
| Apps using a model | the machine running it, the recipe's port (e.g. `8100`) | The model's API at `http://<machine>:<port>/v1` |

!!! warning
    Keep all of this inside one private network – ideally Tailscale, see below – and never expose these
    ports to the internet. Traffic between hub and agents is plain HTTP.

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
