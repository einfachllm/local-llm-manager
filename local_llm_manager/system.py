"""Host memory and GPU readings. DGX Spark and Apple silicon use unified memory, so system memory is the pool."""

from pathlib import Path
from typing import Any

import psutil

from .docker import run


def read_meminfo() -> dict[str, float]:
    """Total / available / used memory in decimal GB (Linux: MemAvailable; macOS: free + inactive pages).
    Decimal (1e9), not GiB: the same unit the Weights tab uses, so "GB" means one thing everywhere."""
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return {"total_gb": 0, "available_gb": 0, "used_gb": 0}
    gb = 1e9
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


# CPU temperature (Linux). Sensor names differ per machine: Intel "coretemp", AMD "k10temp", Arm boards
# "cpu_thermal"/"soc_thermal", and ACPI firmware - e.g. the DGX Spark's Grace CPU - "acpitz" thermal
# zones. Those are preferred; otherwise the hottest sensor that is clearly not a disk, GPU, battery or
# radio is used. macOS gives no CPU temperature without root, so it reports none.
THERMAL_DIR = Path("/sys/class/thermal")
_CPU_NAMES = ("coretemp", "k10temp", "zenpower", "cpu", "soc", "acpitz", "x86_pkg_temp", "tctl", "package")
_NOT_CPU = ("nvme", "drivetemp", "amdgpu", "nouveau", "radeon", "nvidia", "gpu", "iwlwifi", "ath", "mt7",
            "wifi", "bat", "spd", "jc42", "pch", "ucsi", "mlx")


def _plausible(t: Any) -> bool:
    return isinstance(t, (int, float)) and 1 < t < 150


def _thermal_zones() -> list[tuple[str, float]]:
    """(type, °C) of each /sys/class/thermal zone - psutil only reads these when hwmon has nothing."""
    zones = []
    for zone in sorted(THERMAL_DIR.glob("thermal_zone*")):
        try:
            zones.append(((zone / "type").read_text().strip(), int((zone / "temp").read_text().strip()) / 1000))
        except (OSError, ValueError):
            continue
    return zones


def read_cpu_temp() -> float | None:
    """The CPU temperature in °C (the hottest CPU sensor), or None when the machine reports none."""
    readings: list[tuple[str, float]] = []
    if hasattr(psutil, "sensors_temperatures"):
        try:
            for name, entries in psutil.sensors_temperatures().items():
                readings += [(f"{name} {e.label}".lower(), e.current) for e in entries]
        except Exception:
            pass
    readings += [(name.lower(), t) for name, t in _thermal_zones()]
    readings = [(n, t) for n, t in readings if _plausible(t) and not any(x in n for x in _NOT_CPU)]
    cpu = [t for n, t in readings if any(x in n for x in _CPU_NAMES)]
    pick = cpu or [t for _, t in readings]
    return round(max(pick), 1) if pick else None


async def snapshot() -> dict[str, Any]:
    return {"memory": read_meminfo(), "gpus": await read_gpus(), "cpu": {"temperature": read_cpu_temp()}}
