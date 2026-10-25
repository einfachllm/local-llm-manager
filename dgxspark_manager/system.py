"""Host memory and GPU readings. DGX Spark uses unified memory, so /proc/meminfo is the pool."""

from typing import Any

from .docker import run


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, float]:
    values: dict[str, int] = {}
    try:
        with open(path) as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0])
    except OSError:
        return {"total_gb": 0, "available_gb": 0, "used_gb": 0}
    total = values.get("MemTotal", 0) / 1024 / 1024
    available = values.get("MemAvailable", 0) / 1024 / 1024
    return {
        "total_gb": round(total, 1),
        "available_gb": round(available, 1),
        "used_gb": round(total - available, 1),
    }


def _num(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


async def read_gpus() -> list[dict[str, Any]]:
    res = await run(
        ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,temperature.gpu,power.draw",
         "--format=csv,noheader,nounits"],
        timeout=5,
    )
    gpus = []
    if not res.ok:
        return gpus
    for line in res.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append({
            "index": parts[0],
            "name": parts[1],
            "utilization": _num(parts[2]),
            "temperature": _num(parts[3]),
            "power_w": _num(parts[4]),
        })
    return gpus


async def snapshot() -> dict[str, Any]:
    return {"memory": read_meminfo(), "gpus": await read_gpus()}
