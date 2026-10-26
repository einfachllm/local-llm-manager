"""Live metrics read back out of the model server's own log lines.

Rather than scraping framework-specific endpoints (Prometheus /metrics etc.), the hub asks each
executor for the tail of a running recipe's logs and keeps the last stats line the server printed.
That works identically for local and remote hosts and for docker and process runtimes, since both
go through the log path the UI already uses.

Only vLLM is recognised so far; add a parser and register it in PARSERS for more frameworks.
"""

import re
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


def from_logs(text: str) -> dict:
    """The newest stats line in a log tail, or {} when the server printed none."""
    for line in reversed(text.splitlines()):
        for parse in PARSERS:
            metrics = parse(line)
            if metrics:
                return metrics
    return {}
