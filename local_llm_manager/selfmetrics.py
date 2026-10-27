"""The manager's own metrics, rendered as Prometheus text (see the /metrics endpoint).

Only aggregate values are collected - no recipe, host or model data - so the endpoint can be
served without login to a scraper that only checks whether the hub / agent is alive and healthy.
"""

import time

__all__ = ["Sink"]


def _label_string(labels: dict[str, str]) -> str:
    if not labels:
        return ""

    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(labels[k])}"' for k in sorted(labels)) + "}"


class Sink:
    """Collects counter increments and gauge values; render() returns the Prometheus text format.

    A name is either a counter (add/counter) or a gauge (gauge); mixing the two for one name is
    not supported.
    """

    def __init__(self) -> None:
        self.started = time.time()
        # name -> (help text, kind, {label tuple: value})
        self._metrics: dict[str, tuple[str, str, dict[tuple[tuple[str, str], ...], float]]] = {}

    def _entry(self, name: str, help_: str, kind: str) -> dict:
        return self._metrics.setdefault(name, (help_, kind, {}))[2]

    def add(self, name: str, help_: str, value: float = 1.0, **labels: str) -> None:
        """Increment a counter (for events that keep happening, e.g. one per request)."""
        vals = self._entry(name, help_, "counter")
        key = tuple(sorted(labels.items()))
        vals[key] = vals.get(key, 0.0) + value

    def gauge(self, name: str, help_: str, value: float, **labels: str) -> None:
        """Set a gauge's value (the last call wins)."""
        self._entry(name, help_, "gauge")[tuple(sorted(labels.items()))] = float(value)

    def counter(self, name: str, help_: str, value: float, **labels: str) -> None:
        """Set a counter's current value (for counters kept elsewhere, e.g. in the Manager)."""
        self._entry(name, help_, "counter")[tuple(sorted(labels.items()))] = float(value)

    def uptime(self) -> float:
        return time.time() - self.started

    def render(self) -> str:
        lines: list[str] = []
        for name, (help_, kind, values) in sorted(self._metrics.items()):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {kind}")
            for key, value in sorted(values.items()):
                lines.append(f"{name}{_label_string(dict(key))} {value:g}")
        return "\n".join(lines) + "\n"
