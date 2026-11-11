"""A pip install must seed like a checkout: the wheel ships no examples/ folder, so the seed example
travels inside the package (local_llm_manager/seed/, package-data in pyproject.toml). These tests keep
the packaged copy identical to the published example and prove the fallback works when the repo path
is absent - the situation after `pip install .`.
"""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from local_llm_manager import main
from local_llm_manager.db import Database

ROOT = Path(__file__).resolve().parent.parent
PUBLISHED = ROOT / "examples" / "recipes" / "qwen38-mtp.yaml"
PACKAGED = ROOT / "local_llm_manager" / "seed" / "qwen38-mtp.yaml"


def test_the_packaged_seed_is_the_published_example():
    assert PACKAGED.read_bytes() == PUBLISHED.read_bytes(), (
        "local_llm_manager/seed/qwen38-mtp.yaml drifted from examples/recipes/qwen38-mtp.yaml - copy it again")


def test_the_wheel_configuration_ships_the_seed():
    # Cheap guard for the package-data wiring; the real build is the opt-in test below.
    text = (ROOT / "pyproject.toml").read_text()
    assert '"seed/*"' in text, "pyproject.toml package-data no longer ships local_llm_manager/seed/"


def test_seed_falls_back_to_the_packaged_copy(tmp_path, monkeypatch):
    """No examples/ folder (pip install): the packaged copy seeds recipe and weights entry."""
    monkeypatch.setattr(main, "SEED_EXAMPLE", tmp_path / "no-such-folder" / "qwen38-mtp.yaml")
    db = Database(tmp_path / "m.db")
    main.seed_examples(db)
    recipes = db.list_recipes()
    assert [r["name"] for r in recipes] == ["qwen3.8-mtp"]
    assert recipes[0]["framework"] == "vllm"
    weights = db.list_weights()
    assert [w["name"] for w in weights] == ["Qwen3.8-27B-NVFP4"]
    assert recipes[0]["weights_id"] == weights[0]["id"]


def test_no_seed_file_anywhere_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SEED_EXAMPLE", tmp_path / "missing.yaml")
    monkeypatch.setattr(main, "SEED_FALLBACK", tmp_path / "also-missing.yaml")
    db = Database(tmp_path / "m.db")
    main.seed_examples(db)
    assert db.list_recipes() == []


def test_a_fresh_app_seeds_without_the_repo_folder(tmp_path, monkeypatch, fake_docker):
    """The whole path a pip install takes: create_app with DGXM_SEED=1 and no examples/ folder."""
    monkeypatch.setenv("DGXM_SEED", "1")
    monkeypatch.setattr(main, "SEED_EXAMPLE", tmp_path / "missing.yaml")
    app = main.create_app(Database(tmp_path / "m.db"), fake_docker, run_scheduler=False)
    assert [r["name"] for r in app.state.db.list_recipes()] == ["qwen3.8-mtp"]
    assert [w["name"] for w in app.state.db.list_weights()] == ["Qwen3.8-27B-NVFP4"]


@pytest.mark.skipif(not os.environ.get("LLM_MANAGER_WHEEL_TEST"),
                    reason="opt-in wheel build: set LLM_MANAGER_WHEEL_TEST=1 (needs build tooling/network)")
def test_the_built_wheel_contains_the_seed(tmp_path):
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", ".", "-w", str(tmp_path)],
                   cwd=ROOT, check=True, capture_output=True, timeout=300)
    wheel = next(tmp_path.glob("local_llm_manager-*.whl"))
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    assert "local_llm_manager/seed/qwen38-mtp.yaml" in names
    assert "local_llm_manager/static/index.html" in names
