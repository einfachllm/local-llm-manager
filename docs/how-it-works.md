# How it works

Local LLM Manager is a remote control for the AI models on your own computers. You open one web page,
and from there you start, stop and switch models on every machine you have.

## The idea in one picture

```mermaid
flowchart LR
    you["👩‍💻 <b>You</b><br/>laptop or phone"]
    dash["🎛️ <b>Dashboard</b><br/>one web page for everything"]
    subgraph machines["Your machines"]
        direction LR
        m1["🖥️ DGX Spark<br/>runs Qwen"]
        m2["🍎 Mac Studio<br/>runs Llama"]
        m3["🖥️ Second Spark<br/>runs Mistral"]
    end
    apps["💬 <b>Your apps</b><br/>chat, coding tools, …"]

    you --> dash
    dash == "start · stop · switch" ==> machines
    apps -. "talk to the models directly" .-> machines

    classDef person fill:#f59e0b40,stroke:#d97706
    classDef hub fill:#6366f140,stroke:#6366f1
    classDef machine fill:#22c55e38,stroke:#16a34a
    classDef app fill:#ec489938,stroke:#db2777
    class you person
    class dash hub
    class m1,m2,m3 machine
    class apps app
```

- **The dashboard** is the one place where you control everything. It runs on one of your machines (the
  *hub*).
- **Every other machine** has a small helper installed (an *agent*) that does what the dashboard asks.
- **Your apps talk to the models directly.** The dashboard only switches models on and off. It never sees
  your chats.

!!! tip "Only one machine?"
    Then the dashboard and the models live on the same computer. You don't need anything else.

## Recipes: one card per model

A **recipe** is a saved start button for one model. You set it up once; after that, starting the model
is one click. Every recipe is made of three things:

```mermaid
flowchart LR
    w["📦 <b>The model</b><br/>e.g. Qwen 32B"]
    e["⚙️ <b>The engine</b><br/>e.g. vLLM"]
    h["🖥️ <b>The machine</b><br/>e.g. DGX Spark"]
    r(["🃏 <b>Recipe card</b><br/>Qwen 32B on the Spark<br/>▶️ Start"])
    w --> r
    e --> r
    h --> r

    classDef part fill:#6366f126,stroke:#6366f1
    classDef card fill:#22c55e38,stroke:#16a34a
    class w,e,h part
    class r card
```

You will soon have many recipes: one model in two sizes, the same model on several machines, a fast
model for coding and a big one for hard questions. That is fine. Each card is independent, and
**Switch to** makes sure only the right ones run.

!!! tip "Keep the list tidy"
    Pin the recipes you use daily, give cards a colour or tags (e.g. `coding`, `chat`), and use the host
    filter at the top to see one machine at a time.

### Which engine should I pick?

The *engine* (called **framework** in the recipe) is the program that runs the model. Several exist
because they suit different machines and model files. You don't need to know them all, just use the first row that fits:

| Your situation | Pick | Why |
|---|---|---|
| 🍎 A Mac with Apple silicon | **MLX** | Made for Apple chips |
| 📄 The model is one **.gguf** file | **llama.cpp** | Small and simple, made for GGUF |
| 👥 Several people or tools use the model at once | **vLLM** (or **SGLang**) | Fastest with many users at the same time |
| 👤 Just you, on a Linux / NVIDIA machine | **vLLM** or **llama.cpp** | Both are fine |

Everything else (**TensorFold**, **Custom**) is for special cases: a program not in the list, or a
start script you already have. **📋 Import** turns such a script into a recipe for you.

### The easy way: copy what already works

You rarely fill in a recipe by hand. **⚡ Quick recipe** takes a recipe that already runs and swaps in
another model. Everything else (engine, settings, port) stays the same:

```mermaid
flowchart LR
    a["✅ A recipe that works<br/>Qwen 32B · vLLM"]
    b["📦 Another model<br/>Llama 70B"]
    c(["🃏 New recipe<br/>Llama 70B · vLLM<br/>same settings"])
    a -- "settings" --> c
    b -- "model" --> c

    classDef old fill:#6366f126,stroke:#6366f1
    classDef new fill:#22c55e38,stroke:#16a34a
    class a,b old
    class c new
```

On the same machine, the new recipe even gets the same port, so your apps don't need any change: just
**Switch to** the other model.

## Switching to another model

Most machines only have memory for one big model at a time. **Switch to** takes care of everything in one
click:

```mermaid
flowchart LR
    a(["👆 <b>Switch to</b>"]) --> b["⏹️ Stop the others<br/>and free memory"]
    b --> c["▶️ Start<br/>the new model"]
    c --> f(["✅ Ready"])

    classDef step fill:#6366f126,stroke:#6366f1
    classDef go fill:#f59e0b40,stroke:#d97706
    classDef done fill:#22c55e38,stroke:#16a34a
    class b,c step
    class a go
    class f done
```

The card shows each step live. When it turns **Ready**, your apps can use the new model. You can close
the browser at any point: the machine carries on by itself.

!!! example "Let it happen on a schedule"
    *Coding model during the day, a big reasoning model at night*: under **Schedules** the dashboard
    switches for you at set times, even when nobody has it open.

## Adding a new machine

<div class="grid cards" markdown>

-   **1️⃣ Install**

    Install the manager on the new machine and tell it where the dashboard is.

-   **2️⃣ Compare the code**

    The machine shows a short code, e.g. **K7Q2-X9PM**. The dashboard shows a code too.

-   **3️⃣ Add it**

    Same code on both? Click **Codes match – add**. Done ✅

</div>

The code works like the pairing code of a Bluetooth headset. If the two codes are the same, it is really
your machine. If they differ, click **Reject**. The step by step guide is in
[Several machines](several-machines.md).

## Getting a model onto every machine

Models are big: often 20 to 200 GB. The manager downloads a model from the internet **once**, and then the
machines pass it on to each other over your own network, which is much faster.

```mermaid
flowchart LR
    net["☁️ Hugging Face<br/>(internet)"]
    s1["🖥️ Spark 1"]
    s2["🖥️ Spark 2"]
    s3["🖥️ Spark 3"]
    s4["🖥️ Spark 4"]
    s5["🖥️ Spark 5"]

    net == "download once" ==> s1
    s1 -- "copy" --> s2
    s1 -- "copy" --> s3
    s2 -- "copy" --> s4
    s1 -- "copy" --> s5

    classDef cloud fill:#f59e0b40,stroke:#d97706
    classDef machine fill:#22c55e38,stroke:#16a34a
    class net cloud
    class s1,s2,s3,s4,s5 machine
```

Like a snowball, every machine that is finished helps the next one. Five machines are done in three
rounds instead of five. Every file is checked on arrival, an interrupted copy continues where it stopped,
and a progress panel shows how long it will take.

## One setup for the whole team

Example: everyone on the team has their own DGX Spark. Set the model up **once** and roll it out:

```mermaid
flowchart TB
    o["⭐ Your setup<br/>Qwen 32B"]
    c1["👤 Anna's Spark<br/>✓ up to date"]
    c2["👤 Ben's Spark<br/>✓ up to date"]
    c3["👤 Chris's Spark<br/>⟳ needs update"]
    o --> c1 & c2 & c3

    classDef main fill:#f59e0b40,stroke:#d97706
    classDef ok fill:#22c55e38,stroke:#16a34a
    classDef todo fill:#f9731640,stroke:#ea580c
    class o main
    class c1,c2 ok
    class c3 todo
```

Everyone gets the same model at the same address. If you change the original later (a newer version,
other settings), the dashboard offers to update all copies and restart them.

## Is it safe?

<div class="grid cards" markdown>

-   🏠 **Stays at home**

    Everything runs on your own machines. Your prompts and answers never leave them.

-   🔑 **Password protected**

    The dashboard needs a password. Machines only obey the dashboard they were added to.

-   🔒 **Private network**

    With [Tailscale](several-machines.md#recommended-tailscale) all traffic is encrypted, and it also works
    from your phone when you are away.

-   ✅ **Checked copies**

    Every model file copied between machines is checked, so a broken download cannot start a model.

</div>

!!! info "Want the details?"
    The [Architecture](architecture.md) page shows the same things for administrators: which machine talks
    to which, ports, keys and every step on the network.
