"""Live metrics read back out of the model server's own log lines.

Rather than scraping framework-specific endpoints (Prometheus /metrics etc.), the hub asks each
executor for the tail of a running recipe's logs and keeps the last stats line the server printed.
That works identically for local and remote hosts and for docker and process runtimes, since both
go through the log path the UI already uses.

Only vLLM is recognised so far; add a parser and register it in PARSERS for more frameworks.
"""

import re
from datetime import datetime, timezone
from typing import Callable, Optional

# vLLM engine stats, printed about every 10 seconds (idle included):
#   (APIServer pid=1) INFO 10-25 18:34:51 [loggers.py:315] Engine 000: Avg prompt throughput:
#   4897.8 tokens/s, Avg generation throughput: 18.2 tokens/s, Running: 1 reqs, Waiting: 0 reqs,
#   GPU KV cache usage: 11.2%, Prefix cache hit rate: 46.5%
# Releases differ in the KV wording ("GPU cache usage") and add optional fields (MM cache,
# External prefix cache hit rate) that are ignored here.
_VLLM_FIELDS = {
    "prompt_tps": r"Avg prompt throughput: ([0-9.]+) tokens/s",
    "gen_tps": r"Avg generation throughput: ([0-9.]+) tokens/s",
    "running": r"Running: (\d+) reqs",
    "waiting": r"Waiting: (\d+) reqs",
    "kv_cache": r"GPU(?: KV)? cache usage: ([0-9.]+)%",
    "prefix_hit": r"Prefix cache hit rate: ([0-9.]+)%",  # not the lowercase "External prefix ..."
}


def _vllm(line: str) -> Optional[dict]:
    if "Avg generation throughput:" not in line:
        return None
    metrics: dict[str, float] = {}
    for key, pattern in _VLLM_FIELDS.items():
        m = re.search(pattern, line)
        if m:
            metrics[key] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    return metrics


PARSERS: tuple[Callable[[str], Optional[dict]], ...] = (_vllm,)


# `docker logs --timestamps` prefix, e.g. 2026-10-25T18:34:51.123456789Z
_DOCKER_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ")


def from_logs(text: str, now: Optional[datetime] = None) -> dict:
    """The newest stats line in a log tail, or {} when the server printed none.

    With docker's timestamp prefix, `age_s` says how old the line is: vLLM stops printing while idle,
    so an old line no longer describes the current load.
    """
    for line in reversed(text.splitlines()):
        for parse in PARSERS:
            metrics = parse(line)
            if metrics:
                ts = _DOCKER_TS.match(line)
                if ts:
                    at = datetime.fromisoformat(ts.group(1)).replace(tzinfo=timezone.utc)
                    metrics["age_s"] = max(0, int(((now or datetime.now(timezone.utc)) - at).total_seconds()))
                return metrics
    return {}
