# Architecture

How the hub and the agents work together: who talks to whom, what each side stores and decides,
and what happens on the network when you start a model, add a machine or copy weights. This page is for
administrators; [How it works](how-it-works.md) explains the same in pictures, without the technical details.

!!! info "One app, two modes"
    The hub and the agents are the **same program**. `DGXM_MODE` picks the role: the default `hub` serves
    the dashboard and stores everything; `agent` only runs what a hub asks for and has no database, no
    dashboard and no scheduler. A hub controls its own machine directly, so a single machine needs no agent.

## The big picture

```mermaid
flowchart TB
    browser["🌐 Browser / phone<br/>(dashboard)"]
    apps["🧩 Apps using a model<br/>(OpenAI-compatible clients)"]

    subgraph hubhost["Hub machine"]
        hub["<b>Hub</b> :8090<br/>dashboard · REST API · scheduler<br/>recipes · jobs · SQLite"]
        m0[("models")]
        hub -. "runs directly" .-> m0
    end

    subgraph a1host["Machine A (Spark)"]
        a1["<b>Agent</b> :8090<br/>/agent/v1/…"]
        m1[("models<br/>Docker / native")]
        a1 -. "runs" .-> m1
    end

    subgraph a2host["Machine B (Mac)"]
        a2["<b>Agent</b> :8090<br/>/agent/v1/…"]
        m2[("models<br/>Docker / native")]
        a2 -. "runs" .-> m2
    end

    browser == "HTTP Basic auth" ==> hub
    hub == "Bearer agent token" ==> a1
    hub == "Bearer agent token" ==> a2
    a1 <-. "weight copies<br/>(share ticket)" .-> a2
    a2 -. "join request (optional)" .-> hub
    apps -- "model API on the recipe's port,<br/>e.g. machine-a:8100/v1" --> m1
```

- **Solid lines** are the control path. Only the browser talks to the hub, and only the hub talks to the
  agents' API.
- **Dotted lines** are the exceptions: an agent with `DGXM_HUB_URL` *asks* the hub to add it, and machines
  copy weights *straight from each other* after the hub has arranged it.
- **Model traffic never passes through the manager.** Apps call the model server on its own port on the
  machine that runs it.

## Inside one process

```mermaid
flowchart TB
    subgraph hubp["Hub process (DGXM_MODE=hub)"]
        direction TB
        ui["Dashboard<br/>(static app.js)"]
        api["REST API /api/…<br/>auth · CSRF · lockout"]
        sched["Scheduler<br/>cron / weekdays / one-shot"]
        mgr["Manager<br/>rules · per-host locks · jobs"]
        db[("SQLite<br/>recipes · hosts · weights<br/>schedules · events · join requests")]
        lex["LocalExecutor"]
        rex["RemoteExecutor<br/>(one per agent host)"]
        ui --> api --> mgr
        sched --> mgr
        mgr <--> db
        mgr --> lex
        mgr --> rex
    end

    subgraph agentp["Agent process (DGXM_MODE=agent)"]
        direction TB
        aapi["Agent API /agent/v1/…<br/>Bearer token"]
        alex["LocalExecutor"]
        tr["Transfers<br/>pull · download · copy"]
        sh["Shares<br/>read-only, ticket"]
        jn["Joiner → hub /api/join<br/>(if DGXM_HUB_URL)"]
        aapi --> alex
        alex --> tr
        alex --> sh
    end

    rex -- "HTTP(S) + token" --> aapi
    lex --> dk["Docker / native processes<br/>on the hub machine"]
    alex --> dk2["Docker / native processes<br/>on the agent machine"]
```

| Part | Lives on | Does |
|---|---|---|
| **Manager** | hub | Owns the rules: port conflicts, memory check, weights check, what *Switch to* stops, the activity log, background jobs. Picks an executor per host. |
| **LocalExecutor** | hub and every agent | Does the actual work on *its own* machine: `docker run/stop`, native processes, logs, health checks, `/metrics`, transfers and shares. |
| **RemoteExecutor** | hub | The same interface as LocalExecutor, but every call becomes a request to an agent's `/agent/v1/…`. |
| **Scheduler** | hub | Fires schedules through the Manager, exactly like a click in the dashboard. Agents have no clock of their own. |
| **Database** | hub | Everything persistent. Agents keep only process state files, logs and a checksum cache. |

Because the hub's own machine and an agent go through the same executor interface, every feature works
the same on a single machine and on many.

## Who holds which key

```mermaid
flowchart LR
    you(["You"]) -- "DGXM_USER / DGXM_PASSWORD<br/>(HTTP Basic)" --> hub["Hub"]
    hub -- "DGXM_AGENT_TOKEN of that agent<br/>(stored in the hub's DB)" --> agent["Agent"]
    agent -- "share ticket<br/>(one folder, read-only, 6 h)" --> other["Other agent<br/>copying weights"]
    newagent["New agent"] -- "POST /api/join<br/>(no login, only lands in a list)" --> hub
```

- The **agent token** gives full control over models on that machine. Only the hub has it; it is never sent
  to the browser.
- A **share ticket** only allows listing and reading the files below one path, for 6 hours. The copying
  machine never sees the source's agent token, and no SSH is involved.
- A **join request** proves nothing on its own: it only appears under *Hosts → Asking to be added* until
  someone compares the join code and approves it.

## Status: what the 5-second refresh does

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant H as Hub
    participant A as Agent (machine A)
    participant M as Model server on A

    loop every 5 s while the tab is visible
        B->>H: GET /api/recipes
        par one request per host, all hosts at once
            H->>A: POST /agent/v1/statuses (all recipes of A)
            A->>A: docker ps / process state
            A->>M: GET health path (e.g. /health)
            A->>M: GET /metrics (tokens/s, requests, KV cache)
            A-->>H: status + stats per recipe
        end
        H-->>B: recipes with status (offline if a host did not answer)
    end
```

The hub asks each host **once** per refresh for all of its recipes, so many recipes on one machine
cost one request. A host that does not answer shows its recipes as *offline* without slowing the others.

## Starting or switching a model on an agent

```mermaid
sequenceDiagram
    autonumber
    participant U as You / Scheduler
    participant H as Hub (Manager)
    participant A as Agent
    participant D as Docker / process on A

    U->>H: Switch to "Qwen 32B" (host A)
    H->>H: take host A's lock
    H->>A: statuses of every recipe on A
    A-->>H: running / stopped
    H->>H: checks: port free? enough memory?<br/>weights path known and present?
    loop every other running model on A
        H->>A: POST /agent/v1/stop
        A->>D: docker stop / end process
    end
    H->>A: POST /agent/v1/drop-caches
    H->>H: fill {weights} with A's path
    H->>A: POST /agent/v1/start (complete recipe)
    A->>D: docker run … / start process
    A-->>H: started
    H->>H: log event, release lock
    H-->>U: "started"
    Note over H,A: The status refresh then shows<br/>loading → ready once the health check passes.
```

- **The hub sends the complete recipe** with every start, so an agent needs no copy of recipes and a
  recipe moved to another host just works. `{weights}` becomes the path *on that host*.
- **One lock per host:** actions on the same machine (dashboard, scheduler, jobs) run one after another;
  a slow start on machine A (e.g. pulling a large image) does not hold up machine B.

## Adding a machine that asks to join

```mermaid
sequenceDiagram
    autonumber
    participant A as New agent<br/>(DGXM_HUB_URL set)
    participant H as Hub
    participant U as You (dashboard)

    A->>A: join code = from its token, e.g. K7Q2-X9PM<br/>(shown by installer, log, start page)
    loop every 30 s until decided
        A->>H: POST /api/join {name, url, token, version}
        H-->>A: pending
    end
    H->>U: Hosts → "Asking to be added" (with code)
    U->>U: compare the codes on both screens
    U->>H: Codes match – add
    H->>H: create host with the agent's URL + token
    A->>H: POST /api/join
    H-->>A: approved
    loop every 10 min
        A->>H: POST /api/join (current address)
        Note right of H: only the holder of the approved<br/>token can change the address
    end
```

Rejected machines stay blocked for a week; requests nobody answers expire after an hour.

## Getting images and weights onto the machines

Pulls and downloads are **jobs**: the hub starts a task on each machine and then only asks how it is going.
The machines do the work themselves, so a hub restart or a closed browser does not interrupt them.

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (jobs panel)
    participant H as Hub
    participant A as Agent A
    participant C as Agent B
    participant HF as Hugging Face / registry

    B->>H: POST /api/jobs/pull (image, hosts A + B)
    par
        H->>A: POST /agent/v1/transfers {kind: pull}
        A->>HF: docker pull
    and
        H->>C: POST /agent/v1/transfers {kind: pull}
        C->>HF: docker pull
    end
    loop every 3 s until both are done
        H->>A: GET /agent/v1/transfers/{id}
        A-->>H: output, GB so far
        H->>C: GET /agent/v1/transfers/{id}
        C-->>H: output, GB so far
    end
    B->>H: GET /api/jobs
    H-->>B: progress per host (also after a reload)
```

### Copying weights: host to host, like a snowball

```mermaid
sequenceDiagram
    autonumber
    participant H as Hub
    participant S as Source (has weights)
    participant T as Target

    H->>S: POST /agent/v1/shares {path}
    S-->>H: share id + ticket (read-only, 6 h)
    H->>T: POST /agent/v1/transfers {kind: copy, source URL, share}
    T->>S: list files (x-share-ticket)
    S-->>T: names, sizes, SHA-256
    par 4 files at a time, 4 MB pieces
        T->>S: GET file (resume from where it stopped)
    end
    T->>T: check SHA-256 of every file
    H->>T: GET /agent/v1/transfers/{id} (GB, MB/s, time left)
    H->>H: register T's path in the Weights tab
```

Every machine that finishes becomes a source for the next one. With one machine that has the weights and
four that need them, the copy takes three rounds instead of four:

```mermaid
flowchart LR
    subgraph r1["Round 1"]
        s1["Spark 1 ✓"] --> t2["Spark 2"]
    end
    subgraph r2["Round 2"]
        s1b["Spark 1 ✓"] --> t3["Spark 3"]
        s2b["Spark 2 ✓"] --> t4["Spark 4"]
    end
    subgraph r3["Round 3"]
        s1c["Spark 1 ✓"] --> t5["Spark 5"]
    end
    r1 --> r2 --> r3
```

*Download on hosts* uses the same mechanism: the first ticked machine downloads from Hugging Face, and the
others copy from it, so the model comes over the internet only once.

## Copies of one recipe on several machines

```mermaid
flowchart TB
    save(["You save the original"]) --> orig
    orig["<b>Original</b><br/>Qwen 32B · Spark 1 · port 8100"]
    orig -- "copy_of" --- c2["Copy · Spark 2<br/>✓ in sync"]
    orig -- "copy_of" --- c3["Copy · Spark 3<br/>⟳ differs"]
    orig -- "copy_of" --- c4["Copy · Spark 4<br/>✓ in sync"]
    c2 & c3 & c4 --> job["Restart job on the hub<br/>hosts in parallel"]
```

When you save the original, the hub offers to update every copy and to restart the running ones. Copies are rows in the hub's database that point at their original (`copy_of`). They keep their own name,
container name, host and pin; everything else follows the original. Restarts after a sync run as a
background job: all hosts at the same time, one after another on the same host.

## Network ports

| From | To | Port | Encrypted? |
|---|---|---|---|
| Browser | hub | `8090` (`DGXM_PORT`) | with Tailscale or `tailscale serve` / a reverse proxy |
| Hub | each agent | `8090` | with Tailscale or an `https://` agent URL |
| Agent | hub (join only) | `8090` | as above |
| Agent | agent (weight copies) | `8090` | as above; the copy dialog warns about plain HTTP |
| Apps | model server | recipe port, e.g. `8100` | as the model server provides it |

The Hosts tab marks each host 🔒 Tailscale, 🔒 HTTPS or ⚠ plain HTTP. See
[Several machines → Tailscale](several-machines.md#recommended-tailscale) for the recommended setup.
