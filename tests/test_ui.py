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


# ---- UI-03: blur must cover host names, IPv4 and IPv6 everywhere ------------------------------


def blur_helpers(app_js: str) -> str:
    """aliasHash .. blurText, contiguous in app.js; state is stubbed by the caller."""
    start = app_js.index("function aliasHash(")
    end = app_js.index("function memPct(")
    return app_js[start:end]


BLUR_STATE = "const state = { blur: true, hosts: [{ name: 'spark-01', info: { hostname: 'spark-01.local' } }] };\n"


@pytest.mark.skipif(NODE is None, reason="Node.js not available")
def test_mask_addr_covers_ipv6(app_js):
    out = run_node(BLUR_STATE + blur_helpers(app_js) + """
console.log(JSON.stringify({
  v6: maskAddr("fe80::1"), v6full: maskAddr("2001:db8:0:1:1:1:1:1"),
  mapped: maskAddr("::ffff:192.168.1.1"), v4: maskAddr("192.168.1.5"),
  host: maskAddr("spark-01"), loopback: maskAddr("::1"), localhost: maskAddr("localhost"),
}));
""")
    for key in ("v6", "v6full", "mapped", "v4"):
        assert out[key].startswith("ip-"), f"{key}: {out[key]}"
    assert out["host"].startswith("host-")
    assert out["loopback"] == "::1" and out["localhost"] == "localhost"


@pytest.mark.skipif(NODE is None, reason="Node.js not available")
def test_blur_text_masks_ips_but_not_clock_times(app_js):
    out = run_node(BLUR_STATE + blur_helpers(app_js) + """
console.log(JSON.stringify({
  mixed: blurText("connect to 2001:db8::1 and 10.0.0.1 now"),
  clock: blurText("at 12:34:56 done"),
  iso: blurText("since 2026-11-12T06:00:00+01:00"),
  mac: blurText("peer aa:bb:cc:dd:ee:ff"),
  named: blurHostText("Host spark-01 is not reachable", "http://spark-01:8090"),
  off: (state.blur = false, blurText("10.0.0.1 and spark-01")),
}));
""")
    assert "2001:db8::1" not in out["mixed"] and "10.0.0.1" not in out["mixed"]
    assert out["mixed"].count("ip-") == 2
    assert out["clock"] == "at 12:34:56 done"
    assert out["iso"] == "since 2026-11-12T06:00:00+01:00"
    assert "aa:bb:cc:dd:ee:ff" not in out["mac"]
    assert "spark-01" not in out["named"] and "host-" in out["named"]
    assert out["off"] == "10.0.0.1 and spark-01"


def test_blur_covers_logs_activity_findings_and_dialogs(app_js):
    """UI-03: the screen-share blur must reach every place that shows host names or IPs."""
    assert "esc(blurHostText(l))" in app_js                      # log dialog lines
    assert "esc(blurHostText(e.message))" in app_js              # activity messages
    assert "esc(blurHostText(e.target" in app_js                 # activity targets
    assert "blurHostText(findingTitle(f))" in app_js             # finding chips
    assert "blurHostText(f.message)" in app_js                   # finding details
    assert "renderJoinRequests();" in app_js.split('$("#btn-blur").addEventListener')[1][:800]  # toggle re-renders joins
    # confirms about hosts mask the name (functional copy-prompts stay real by design)
    assert "Stop all models on ${maskAddr(h.name)}?" in app_js
    assert "Delete recipe “${recipeTitle(r)}”?" in app_js


# ---- Welle 2: UI-05/06/07/09/10/11/12/13/14/15/17/18/20/21 guards ------------------------------


def test_wave2_ui_guards(app_js):
    # UI-06: every dialog submit goes through the double-click guard
    assert app_js.count("oneSubmit(") >= 7  # helper + 6 forms
    # UI-05: schedule times and the timeline axis render in the hub's zone
    assert "function fmtHub(" in app_js and "hubHour" in app_js and "Axis in hub time" in app_js
    # UI-07: stale poll answers are discarded
    assert "recipesSeq" in app_js and "hostsSeq" in app_js
    # UI-09: the log stream reconnects instead of dying on the first error
    assert "reconnecting…" in app_js and "logRetries" in app_js
    # UI-10: the 1 s tick updates only the elapsed nodes; buttons don't swallow shortcuts
    assert "tickStartElapsed()" in app_js and "/^(input|textarea|select)$/i" in app_js
    # UI-11: the hub version is shown
    assert '$("#hub-version").textContent' in app_js
    # UI-12: an empty error body still yields a message
    assert "|| res.statusText || `HTTP ${res.status}`" in app_js
    # UI-14: connection-lost badge
    assert "pollFailed()" in app_js and 'id="conn-state"' in INDEX_HTML.read_text()
    # UI-15: client maps stay bounded
    assert "sparkHist.delete(id)" in app_js and "closedJobs.size > 20" in app_js
    # UI-17: activity asks the server for the source filter and refreshes while visible
    assert "&source=health" in app_js and 'state.tab === "activity") loadEvents()' in app_js
    # UI-18: one job poll loop, paused in hidden tabs
    assert "jobsPolling" in app_js
    # UI-20: dirty dialogs ask before discarding; docker container port is required
    assert "Discard unsaved changes?" in app_js and "container_port.required = !native" in app_js
    # UI-21: muted findings keep their cause visible
    assert "hf-box ${f.severity}${f.muted" in app_js


def test_wave2_static_assets():
    # UI-21 CSP: no inline script left, theme boot moved to a file; UI-13: manifest + icons exist
    html = INDEX_HTML.read_text()
    assert 'http-equiv="Content-Security-Policy"' in html
    assert '<script src="/static/theme.js"></script>' in html
    assert "<script>" not in html and "onclick=" not in html
    assert 'rel="manifest"' in html and 'aria-live="polite"' in html
    assert (STATIC / "theme.js").exists() and (STATIC / "manifest.webmanifest").exists()
    assert (STATIC / "icon-192.png").exists() and (STATIC / "icon-512.png").exists()
    import json
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/" and len(manifest["icons"]) == 2
