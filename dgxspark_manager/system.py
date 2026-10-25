"""Host memory and GPU readings. DGX Spark and Apple silicon use unified memory, so system memory is the pool."""

from typing import Any

import psutil

from .docker import run


def read_meminfo() -> dict[str, float]:
    """Total / available / used memory in GB (Linux: MemAvailable; macOS: free + inactive pages)."""
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return {"total_gb": 0, "available_gb": 0, "used_gb": 0}
    gb = 1024 ** 3
    return {
        "total_gb": round(vm.total / gb, 1),
        "available_gb": round(vm.available / gb, 1),
        "used_gb": round((vm.total - vm.available) / gb, 1),
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
