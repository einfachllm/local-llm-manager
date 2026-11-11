"""Guard tests for the static frontend (vanilla JS, no build step).

The dashboard ships as plain files, so these tests read them directly and pin contracts that must
not regress (findings UI-01 / TIME-01 / UI-02 in analysis/04-ui.md). Where Node.js is available the
extracted JS helpers are executed for real - including the schedule time-zone roundtrip across
simulated browser zones; without Node those functional tests skip and the static guards still run.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from local_llm_manager.main import RecipeIn
from local_llm_manager.scheduler import parse_time

STATIC = Path(__file__).resolve().parent.parent / "local_llm_manager" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"
INDEX_HTML = STATIC / "index.html"

NODE = shutil.which("node")


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def run_node(script: str, tz: str = "UTC") -> dict:
    """Run a JS snippet against the extracted app.js helpers with a simulated browser zone."""
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "TZ": tz})
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ---- B-01 / UI-01: a recipe PUT must carry every RecipeIn field -------------------------------


def recipe_body_fields(app_js: str) -> list[str]:
    m = re.search(r"const RECIPE_BODY_FIELDS\s*=\s*\[(.*?)\];", app_js, re.S)
    assert m, "RECIPE_BODY_FIELDS array not found in app.js"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_recipe_body_fields_cover_recipe_in(app_js):
    """A PUT replaces the whole recipe: a field missing from the body is reset to its default
    (weights_id -> null silently broke {weights} starts). pinned is set separately in recipeBodyFrom."""
    fields = set(recipe_body_fields(app_js))
    expected = set(RecipeIn.model_fields) - {"pinned"}
    missing = expected - fields
    assert not missing, f"RECIPE_BODY_FIELDS is missing RecipeIn fields: {sorted(missing)}"


@pytest.mark.skipif(NODE is None, reason="Node.js not available")
def test_recipe_body_from_keeps_weights_id(app_js):
    """Execute recipeBodyFrom: pinning or switching the image tag must not drop weights_id."""
    start = app_js.index("const RECIPE_BODY_FIELDS")
    end = app_js.index('$("#recipes").addEventListener')
    helpers = app_js[start:end]
    out = run_node(helpers + """
const r = { name: "x", weights_id: 7, pinned: false, host_port: 8100, image: "old:1" };
const pin = recipeBodyFrom(r, { pinned: true });
const tag = recipeBodyFrom(r, { image: "new:2" });
console.log(JSON.stringify({ pin_weights: pin.weights_id, pin_pinned: pin.pinned,
                             tag_weights: tag.weights_id, tag_image: tag.image }));
""")
    assert out == {"pin_weights": 7, "pin_pinned": True, "tag_weights": 7, "tag_image": "new:2"}


# ---- B-09 / TIME-01: one-shot schedule times must not drift between hub and browser zones ------

HUBS = [("CET (UTC+01:00)", "+01:00"), ("IST (UTC+05:30)", "+05:30"), ("EST (UTC-05:00)", "-05:00")]
BROWSER_ZONES = ["UTC", "America/New_York", "Asia/Tokyo"]


def schedule_helpers(app_js: str) -> str:
    """The three time helpers, contiguous in app.js right before openScheduleEditor."""
    start = app_js.index("function hubOffsetMinutes(")
    end = app_js.index("function openScheduleEditor(")
    return app_js[start:end]


def one_shot_script(helpers: str, hub_label: str, stored: str) -> str:
    return f"const state = {{ meta: {{ timezone: {json.dumps(hub_label)} }} }};\n" + helpers + f"""
const stored = {json.dumps(stored)};
const rendered = toLocalInput(stored);
const resubmitted = runAtFromInput(rendered);
const naiveRendered = toLocalInput("2026-11-12T06:00:00");
const before = Date.now();
const defaultSubmitted = runAtFromInput(toLocalInput(undefined));
console.log(JSON.stringify({{ rendered, resubmitted, naiveRendered,
  sameInstant: Date.parse(resubmitted) === Date.parse(stored),
  defaultDeltaMin: (Date.parse(defaultSubmitted) - before) / 60000 }}));
"""


def test_run_at_submit_wraps_input_with_hub_offset(app_js):
    """The one-shot submit must not send the raw datetime-local value (naive = browser wall time)."""
    assert not re.search(r'body\.run_at\s*=\s*\$\("#in-once"\)\.value\s*;', app_js)
    assert re.search(r'body\.run_at\s*=\s*runAtFromInput\(\$\("#in-once"\)\.value\)\s*;', app_js)


def test_time_helpers_use_hub_offset(app_js):
    block = schedule_helpers(app_js)
    assert "function toLocalInput(iso)" in block and "function runAtFromInput(value)" in block
    assert "state.meta?.timezone" in block          # offset is parsed from the hub's zone label
    assert block.count("hubOffsetMinutes()") >= 2   # both rendering and submit consult it


def test_cron_branch_unchanged(app_js):
    assert 'body.cron = $("#in-cron").value;' in app_js
    assert 'body.cron = `${m} ${h} * * ${days.length === 7 ? "*" : days.join(",")}`;' in app_js


@pytest.mark.skipif(NODE is None, reason="Node.js not available")
@pytest.mark.parametrize("hub_label,offset", HUBS)
@pytest.mark.parametrize("browser_tz", BROWSER_ZONES)
def test_one_shot_roundtrip_across_zones(app_js, hub_label, offset, browser_tz):
    """Render a stored instant as hub wall time and send it back with the hub offset: the instant
    must survive any browser zone, and the backend (parse_time) must read the same instant."""
    stored = f"2026-11-12T06:00:00{offset}"  # 06:00 hub wall time, as the hub stores it
    out = run_node(one_shot_script(schedule_helpers(app_js), hub_label, stored), tz=browser_tz)
    assert out["rendered"] == "2026-11-12T06:00"        # hub wall time, independent of the browser
    assert out["naiveRendered"] == "2026-11-12T06:00"   # legacy naive value already is hub time
    assert out["sameInstant"] is True
    assert 59 <= out["defaultDeltaMin"] <= 62           # new-schedule default: now + 1 h, hub time
    assert parse_time(out["resubmitted"]).timestamp() == datetime.fromisoformat(stored).timestamp()


@pytest.mark.skipif(NODE is None, reason="Node.js not available")
@pytest.mark.parametrize("browser_tz", BROWSER_ZONES)
@pytest.mark.parametrize("hub_label", ["", "no offset here"])
def test_one_shot_fallback_without_hub_offset(app_js, browser_tz, hub_label):
    """Without a parseable hub offset the editor keeps its old behavior: browser-local rendering
    and a naive submit value (the hub then assumes its own zone, as before)."""
    stored = "2026-11-12T06:00:00+01:00"
    out = run_node(one_shot_script(schedule_helpers(app_js), hub_label, stored), tz=browser_tz)
    expected = datetime.fromisoformat(stored).astimezone(ZoneInfo(browser_tz)).strftime("%Y-%m-%dT%H:%M")
    assert out["rendered"] == expected
    assert out["resubmitted"] == out["rendered"]        # naive value passes through unchanged
    assert out["naiveRendered"] == "2026-11-12T06:00"
