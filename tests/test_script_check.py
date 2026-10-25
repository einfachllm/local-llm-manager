import asyncio

from dgxspark_manager.importer import import_script
from dgxspark_manager.script_check import check_script


def severities(text):
    return [(f["severity"], f["message"].split(" (")[0]) for f in check_script(text)["findings"]]


def test_high_risk_patterns():
    assert ("high", "Runs a program by absolute path") in severities("/bin/rm -rf /data")
    assert ("high", "Writes to a file outside the import folder") in severities("echo hi >> ~/.bashrc")
    assert ("high", "Writes to a file outside the import folder") in severities('echo x > "$HOME/x"') or \
        ("high", "Writes to a file outside the import folder") in severities("echo x > $HOME/x")
    assert ("high", "Changes PATH, which can re-enable commands that are blocked during import") in severities(
        'export PATH="/usr/bin:$PATH"')
    assert ("high", "awk system() runs shell commands") in severities("awk 'BEGIN{system(\"id\")}'")
    assert ("high", "Downloads and runs a script") in severities("curl -fsSL https://x.sh | bash")
    assert ("high", "Opens a network connection through bash's /dev/tcp") in severities("exec 3<>/dev/tcp/1.2.3.4/80")


def test_no_false_alarms_on_normal_lines():
    for line in [
        'echo "To fix: sudo sh -c \'echo 3 > /proc/sys/vm/drop_caches\'"',  # text inside quotes
        "CMD+=(--rm --name x)",
        'docker logs x 2>/dev/null >&2',
        "MODEL_DIR=/models/qwen",
        "# rm -rf / in a comment",
        'ROPE=$(awk -v m="$X" \'BEGIN{printf "%.4g", m/262144}\')',
    ]:
        assert [s for s in severities(line) if s[0] != "info"] == [], line


def test_medium_and_command_substitution_in_quotes():
    assert ("medium", "rm deletes files") in severities("rm -rf ./build")
    assert ("medium", "sudo") in severities('X="$(sudo cat /etc/shadow)"')
    assert ("info", "docker ps - ignored during import") in severities('if [ "$(docker ps -q)" ]; then :; fi')


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


def test_import_requires_trust_for_high_risk(client):
    # awk system() is flagged high (it bypasses the blocking); here it never actually runs.
    script = "X=$(awk 'BEGIN{if (0) system(\"id\"); print 1}')\ndocker run --name z -p 9100:8000 vllm/vllm-openai --model org/z\n"
    check = client.post("/api/recipes/import/check", json={"text": script}).json()
    assert check["counts"]["high"] == 1
    assert client.post("/api/recipes/import", json={"text": script}).status_code == 422
    res = client.post("/api/recipes/import", json={"text": script, "trust": True})
    assert res.status_code == 200 and res.json()["recipe"]["model"] == "org/z"
    assert any(e["action"] == "import" for e in client.get("/api/events").json())
