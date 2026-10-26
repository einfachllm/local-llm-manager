"""Live metrics from the model server's Prometheus endpoint (`/metrics` on its host port).

The engine stats line in the logs (see logstats.py) is printed only every ~10 s and averages over that
window, so it lags behind. Here the token counters are read on every status poll and turned into
rates over the time since the previous poll, which follows the load within a poll interval.

vLLM serves /metrics by default; SGLang needs `--enable-metrics`, llama.cpp server `--metrics`.
"""

import re
import time
from typing import Optional

# Metric names per field; the first name a server exposes is used. Labelled series are summed.
_COUNTERS = {
    "prompt": ("vllm:prompt_tokens_total", "sglang:prompt_tokens_total", "llamacpp:prompt_tokens_total"),
    "gen": ("vllm:generation_tokens_total", "sglang:generation_tokens_total", "llamacpp:tokens_predicted_total"),
    "prefix_queries": ("vllm:prefix_cache_queries_total",),
    "prefix_hits": ("vllm:prefix_cache_hits_total",),
}
_GAUGES = {
    "running": ("vllm:num_requests_running", "sglang:num_running_reqs", "llamacpp:requests_processing"),
    "waiting": ("vllm:num_requests_waiting", "sglang:num_queue_reqs", "llamacpp:requests_deferred"),
    # Fractions 0..1, shown as percent.
    "kv_cache": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc", "sglang:token_usage",
                 "llamacpp:kv_cache_usage_ratio"),
    "prefix_hit": ("sglang:cache_hit_rate",),
}
_PERCENT = ("kv_cache", "prefix_hit")
_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")

# Counters older than this give an average, not a current rate; start over instead.
MAX_RATE_WINDOW = 60.0
# Polls closer together than this (e.g. two open dashboards) are measured against the older sample.
MIN_RATE_WINDOW = 3.0


def parse(text: str) -> dict[str, float]:
    """Prometheus text format -> {metric name: value summed over its label sets}."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            v = float(m.group(2))
        except ValueError:
            continue
        values[m.group(1)] = values.get(m.group(1), 0.0) + v
    return values


def _pick(values: dict[str, float], names: tuple[str, ...]) -> Optional[float]:
    return next((values[n] for n in names if n in values), None)


class RateTracker:
    """Turns counter samples into per-second rates, per running instance."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._last: dict[str, tuple[float, dict[str, float]]] = {}

    def update(self, key: str, text: str) -> dict:
        """Metrics for the card from one /metrics response; {} if it has none we know."""
        values = parse(text)
        gauges = {k: _pick(values, names) for k, names in _GAUGES.items()}
        counters = {k: _pick(values, names) for k, names in _COUNTERS.items()}
        if all(v is None for v in (*gauges.values(), counters["prompt"], counters["gen"])):
            return {}
        out: dict = {}
        for k, v in gauges.items():
            if v is not None:
                out[k] = round(v * 100, 1) if k in _PERCENT else int(v)
        now = self.clock()
        current = {k: v for k, v in counters.items() if v is not None}
        prev = self._last.get(key)
        if prev is None or now - prev[0] >= MIN_RATE_WINDOW:
            self._last[key] = (now, current)
        if prev is None or not 0 < now - prev[0] <= MAX_RATE_WINDOW:
            return out
        dt = now - prev[0]
        delta = {k: v - prev[1][k] for k, v in current.items() if k in prev[1]}
        if any(d < 0 for d in delta.values()):  # server restarted, counters reset
            return out
        if "prompt" in delta:
            out["prompt_tps"] = round(delta["prompt"] / dt, 1)
        if "gen" in delta:
            out["gen_tps"] = round(delta["gen"] / dt, 1)
        if delta.get("prefix_queries"):
            out["prefix_hit"] = round(100 * delta.get("prefix_hits", 0) / delta["prefix_queries"], 1)
        return out

    def forget_except(self, keys: set[str]) -> None:
        for k in set(self._last) - keys:
            del self._last[k]
