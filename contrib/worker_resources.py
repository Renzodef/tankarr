"""Bounded, local resource observations for optional translation-worker admission."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path


def _command(arguments):
    try:
        return subprocess.check_output(
            arguments, text=True, stderr=subprocess.DEVNULL, timeout=0.75
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def effective_cpu_count():
    """Count CPUs available to this process, including affinity and v2 quotas."""
    cpus = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        try:
            cpus = min(cpus, max(1, len(os.sched_getaffinity(0))))
        except OSError:
            pass
    if platform.system() == "Linux":
        try:
            quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
            cpus = min(cpus, max(0.01, int(quota) / int(period)))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    return cpus


def threads_per_worker(workers, override=None):
    if type(workers) is not int or workers < 1:
        raise ValueError("Worker capacity must be a positive integer")
    if override is not None:
        if type(override) is not int or override < 1:
            raise ValueError("Threads per worker must be a positive integer")
        return override
    return max(1, int(effective_cpu_count() / workers))


def thread_environment(environment, threads):
    """Set limits before engine imports; inherited all-core settings lose priority."""
    if type(threads) is not int or threads < 1:
        raise ValueError("Thread budget must be a positive integer")
    limits = {
        name: str(threads)
        for name in (
            "OMP_NUM_THREADS",
            "OMP_THREAD_LIMIT",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "NUMEXPR_MAX_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        )
    }
    return {**environment, **limits, "OMP_MAX_ACTIVE_LEVELS": "1"}


def snapshot(directory):
    """Unknown metrics stay null: no remote scans, privileged tools or dependencies."""
    cpus = effective_cpu_count()
    memory = battery = battery_percent = None
    if platform.system() == "Linux":
        try:
            fields = dict(
                re.findall(r"^(\w+):\s*(\d+)", Path("/proc/meminfo").read_text(), re.M)
            )
            memory = int(fields["MemAvailable"]) * 1024
        except (OSError, KeyError, ValueError):
            pass
        # Respect v2 container limits rather than advertising the entire host.
        try:
            cgroup = Path("/sys/fs/cgroup")
            limit = int((cgroup / "memory.max").read_text())
            current = int((cgroup / "memory.current").read_text())
            memory = (
                min(memory, max(0, limit - current))
                if memory is not None
                else max(0, limit - current)
            )
        except (OSError, ValueError):
            pass
        supplies = Path("/sys/class/power_supply")
        try:
            batteries = [
                p
                for p in supplies.iterdir()
                if (p / "type").read_text().strip() == "Battery"
            ]
            if batteries:
                battery = any(
                    (p / "status").read_text().strip() == "Discharging"
                    for p in batteries
                )
                battery_percent = min(
                    int((p / "capacity").read_text()) for p in batteries
                )
        except (OSError, ValueError):
            pass
    elif platform.system() == "Darwin":
        stats = _command(["vm_stat"])
        page = re.search(r"page size of (\d+) bytes", stats)
        fields = dict(re.findall(r"^([^:\n]+):\s*(\d+)\.", stats, re.M))
        if page:
            # An estimate from free pages and reclaimable inactive/file-cache pages.
            # Purgeable pages overlap these classes and must not be added twice.
            memory = int(page[1]) * sum(
                int(fields.get(k, 0))
                for k in (
                    "Pages free",
                    "Pages inactive",
                    "Pages speculative",
                )
            )
        power = _command(["pmset", "-g", "batt"])
        if "Now drawing from" in power:
            battery = "'Battery Power'" in power
            percent = re.search(r"(\d+)%;", power)
            battery_percent = int(percent[1]) if percent else None
    try:
        load = os.getloadavg()[0] / cpus
    except (OSError, AttributeError):
        load = None
    return {
        "cpu_count": cpus,
        "load_per_cpu": round(load, 3) if load is not None else None,
        "memory_available_bytes": memory,
        "disk_free_bytes": shutil.disk_usage(directory).free,
        "on_battery": battery,
        "battery_percent": battery_percent,
    }


class ResourceProbe:
    def __init__(self, directory, *, ttl=2.0):
        self.directory = directory
        self.ttl = ttl
        self.lock = threading.Lock()
        self.at = 0.0
        self.value = None

    def __call__(self):
        with self.lock:
            now = time.monotonic()
            if self.value is None or now - self.at >= self.ttl:
                self.value = snapshot(self.directory)
                self.at = time.monotonic()
            return dict(self.value)


def admission_reasons(resources, policy):
    reasons = []
    for field, setting, reason in (
        ("memory_available_bytes", "min_memory_bytes", "memory"),
        ("disk_free_bytes", "min_disk_bytes", "disk"),
    ):
        minimum = policy.get(setting, 0)
        measured = resources.get(field)
        if minimum > 0 and (measured is None or measured < minimum):
            reasons.append(reason)
    maximum = policy.get("max_load_per_cpu", 0)
    if (
        maximum > 0
        and resources.get("load_per_cpu") is not None
        and resources["load_per_cpu"] > maximum
    ):
        reasons.append("cpu_load")
    if resources.get("on_battery") is True:
        percent = resources.get("battery_percent")
        if (
            not policy.get("allow_battery", False)
            or percent is None
            or percent < policy.get("min_battery_percent", 30)
        ):
            reasons.append("battery")
    return reasons
