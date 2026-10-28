# Quick start

This page sets up the manager on one machine and starts a first model. For more machines, continue with
[Several machines](several-machines.md) afterwards.

## Requirements

- Python 3.10 or newer.
- For Docker recipes: Docker, usable by your user without `sudo`, with the NVIDIA container runtime for
  GPUs.
- The model weights and Docker images you want to run, already on the machine.

## 1. Install

=== "Linux"

    Installs a systemd service that runs as your user.

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    ./install.sh
    ```

    The installer prints a generated password and stores it in `.env` in this folder.

=== "macOS"

    Installs a launchd agent. Docker on a Mac cannot use the GPU, so use native recipes (framework
    *Custom (native)*) with e.g. MLX (`pip install mlx-lm`) or llama.cpp (`brew install llama.cpp`). For importing start scripts, also
    run `brew install bash`.

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    ./install-macos.sh
    ```

    The installer prints a generated password and stores it in `.env`.

=== "Docker Compose"

    Linux only, and only for Docker recipes. Uses the image `ghcr.io/einfachllm/local-llm-manager:latest`
    (amd64 and arm64).

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    cp .env.example .env
    ```

    Set `DGXM_PASSWORD` in `.env` (at least 12 characters, e.g. from `openssl rand -base64 18`), then:

    ```bash
    docker compose up -d
    ```

    Add `--build` to build the image yourself.

## 2. Log in

Open `http://<machine>:8090` in a browser and log in as **admin** with the password from the installer
(or the one you set in `.env`).

To change the password, edit `DGXM_PASSWORD` in `.env` (at least 12 characters, no well-known passwords)
and restart the service.

## 3. Start your first model

1. Click **+ New recipe**.
2. Pick a **framework**, e.g. vLLM. The preset fills in the image, command, port and health check.
3. Set the **Model**: a Hugging Face id such as `Qwen/Qwen3-8B`, or a path inside the container.
4. Check the **Command preview** next to the form, then **Save**.
5. Click **Start** on the new card. The status goes from *starting* to *loading* to *ready*.

!!! tip "Already have a start script?"
    Use **📋 Import docker run / script…** instead of steps 1–3. Paste the script or `docker run`
    command; it is checked for risky calls first and nothing in it is executed.

When the model is ready, its OpenAI-compatible API is at the endpoint shown on the card:

```bash
curl http://<machine>:<port>/v1/models
```

## Next steps

- Run models on more than one machine: [Several machines](several-machines.md).
- Upgrading, uninstalling, schedules and all settings are described in the
  [README](https://github.com/einfachllm/local-llm-manager#readme).
