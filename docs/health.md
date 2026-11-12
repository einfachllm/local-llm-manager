# Health findings

The hub checks every 15 seconds whether something needs your attention and says so where you look anyway:
a chip on the recipe card with what happened, why and what usually helps; a chip on the host in the
**Hosts** graph; a ⚠ / ✖ count on the host chip in the top bar; and an entry in **Activity** (filter
**Health**). Findings only read - they never start, stop or change anything.

## Rules and thresholds

The thresholds are fixed. They are chosen to fit a DGX Spark as well as a Mac or a small Linux box: memory
is measured as a share of the machine's total, and the KV-cache rule only fires while requests are waiting.

| Rule | Warning | Critical | Must hold for |
|---|---|---|---|
| **Host unreachable** - an agent does not answer | – | always | 1 min |
| **Memory almost full** | under 8 % of the total free | under 3 % free | 1 min |
| **GPU running hot** - the hottest GPU of the host | from 85 °C | from 90 °C | 3 min |
| **KV cache almost full** - and requests are waiting | from 85 % | – | 5 min |
| **Start failed** - with the cause from the log | – | always | at once |
| **Start takes unusually long** - compared with the recipe's own earlier starts | over 2× the median (and at least 2 min more), from 3 measured starts on | – | at once |

A condition has to hold for the time in the last column before it becomes a finding, and it has to be
gone for a minute before the finding clears. So a reading that sits on a threshold does not flap on and
off, and the activity log gets one entry when a finding appears, one when it gets worse (warning →
critical) and one when it is resolved. "Host unreachable" holds for the same minute a cleared finding
takes, so a failed start on a host that goes offline hands over without a gap.

For example, 121 GB on a DGX Spark: a warning under about 9.7 GB free, critical under about 3.6 GB.
On a 16 GB laptop: under about 1.3 GB and 0.5 GB.

## Why a start failed

When a recipe fails, the manager reads the end of its log once and names the cause it recognises:

| Shown as | Typical log line |
|---|---|
| CUDA out of memory | `torch.OutOfMemoryError: CUDA out of memory` |
| Not enough memory for the KV cache | `No available memory for the cache blocks` |
| Killed (out of memory) | `Killed`, exit code 137 |
| Port already in use | `Address already in use` |
| Model not found | `No such file or directory … model`, `failed to load model` |
| Hugging Face login needed | `gated repo`, `401 Client Error` |
| CUDA / driver error | `CUDA error`, `no CUDA-capable device` |
| Unknown argument | `unrecognized arguments` |

The matching line is shown on the card; **Logs** has the full output.

## Muting

**Mute 24 h** under a finding hides it for a day: it stays on the card dimmed (🔕), is not counted in the
top bar or in `/metrics`, and writes nothing to the activity log. Click the dimmed chip to unmute it early.
Mutes survive a restart of the manager.

## API and metrics

- `GET /api/health` - the active findings (muted ones included, marked) and the rules.
- `POST /api/health/mute` with `{"key": "<finding key>", "hours": 24}`, `POST /api/health/unmute` with `{"key": …}`.
- `/api/recipes` and `/api/hosts` carry each item's `findings`.
- With `DGXM_METRICS=1`, `/metrics` has `dgxm_health_findings{rule, severity}` (muted ones not counted) for
  alerts in Prometheus / Grafana.

`DGXM_HEALTH_TICK` (default `15`) sets the seconds between checks.
