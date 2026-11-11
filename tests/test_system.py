from types import SimpleNamespace

import pytest

from local_llm_manager import system


def sensor(label, current):
    return SimpleNamespace(label=label, current=current, high=None, critical=None)


@pytest.fixture
def no_zones(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "THERMAL_DIR", tmp_path)
    return tmp_path


def add_zone(root, n, kind, millideg):
    z = root / f"thermal_zone{n}"
    z.mkdir()
    (z / "type").write_text(f"{kind}\n")
    (z / "temp").write_text(f"{millideg}\n")


def test_cpu_sensor_wins_over_hotter_disk(monkeypatch, no_zones):
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: {
        "nvme": [sensor("Composite", 71.0)],
        "coretemp": [sensor("Package id 0", 55.0), sensor("Core 0", 58.5)],
    }, raising=False)
    assert system.read_cpu_temp() == 58.5


def test_acpi_thermal_zones_like_dgx_spark(monkeypatch, no_zones):
    # hwmon only has the disk, so psutil never looks at the thermal zones; we read them ourselves.
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: {"nvme": [sensor("Composite", 48.0)]},
                        raising=False)
    add_zone(no_zones, 0, "acpitz", 61250)
    add_zone(no_zones, 1, "acpitz", 64800)
    assert system.read_cpu_temp() == 64.8


def test_unknown_sensors_use_the_hottest_plausible_one(monkeypatch, no_zones):
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: {
        "some_board": [sensor("", 47.0), sensor("", 52.0), sensor("", -273.0)],
        "iwlwifi_1": [sensor("", 60.0)],
    }, raising=False)
    assert system.read_cpu_temp() == 52.0


def test_no_sensors_reports_none(monkeypatch, no_zones):
    monkeypatch.delattr(system.psutil, "sensors_temperatures", raising=False)  # e.g. macOS
    assert system.read_cpu_temp() is None


def test_snapshot_includes_cpu(monkeypatch):
    async def no_gpus():
        return []
    monkeypatch.setattr(system, "read_gpus", no_gpus)
    monkeypatch.setattr(system, "read_cpu_temp", lambda: 50.0)
    import asyncio
    snap = asyncio.run(system.snapshot())
    assert snap["cpu"] == {"temperature": 50.0}


def test_memory_is_decimal_gb(monkeypatch):
    """UI-04/FLEET-12: memory readings use the same decimal GB as the Weights tab (128 GB machine
    shows 128.0, not 119.2 GiB labelled "GB")."""
    class VM:
        total = 128_000_000_000
        available = 100_000_000_000
    monkeypatch.setattr(system.psutil, "virtual_memory", lambda: VM())
    m = system.read_meminfo()
    assert m == {"total_gb": 128.0, "available_gb": 100.0, "used_gb": 28.0}
