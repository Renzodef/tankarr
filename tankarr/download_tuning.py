from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

_MIB = 1024 * 1024
_CGROUP = Path("/sys/fs/cgroup")
_MIN_PIPELINE_JOB_BYTES = 16 * _MIB
_MAX_AUTOMATIC_PIPELINE_SLOTS = 64


def _integer(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _keyed_integers(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    result: dict[str, int] = {}
    for line in lines:
        key, _, raw = line.partition(" ")
        try:
            result[key] = int(raw)
        except ValueError:
            continue
    return result


def _pressure_average(path: Path, kind: str = "some") -> float | None:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != kind:
            continue
        for field in fields[1:]:
            key, _, raw = field.partition("=")
            if key == "avg10":
                try:
                    return float(raw)
                except ValueError:
                    return None
    return None


def _cpuset_count(raw: str) -> int | None:
    count = 0
    try:
        for group in raw.strip().split(","):
            if not group:
                continue
            start, separator, end = group.partition("-")
            count += int(end) - int(start) + 1 if separator else 1
    except ValueError:
        return None
    return count or None


def _cpu_limit_cores() -> float:
    host_cores = float(os.cpu_count() or 1)
    try:
        cpuset = (_CGROUP / "cpuset.cpus.effective").read_text(encoding="ascii")
        cpuset_cores = _cpuset_count(cpuset)
        if cpuset_cores is not None:
            host_cores = min(host_cores, float(cpuset_cores))
    except OSError:
        pass
    try:
        quota_raw, period_raw = (
            (_CGROUP / "cpu.max").read_text(encoding="ascii").split()
        )
        if quota_raw != "max":
            host_cores = min(host_cores, int(quota_raw) / max(1, int(period_raw)))
    except (OSError, ValueError):
        pass
    return max(0.1, host_cores)


def _host_memory() -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None, None
    for line in lines:
        key, _, raw = line.partition(":")
        try:
            values[key] = int(raw.strip().split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    return values.get("MemTotal"), values.get("MemAvailable")


def _network_bytes() -> int | None:
    try:
        lines = Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]
    except OSError:
        return None
    total = 0
    found = False
    for line in lines:
        interface, separator, counters = line.partition(":")
        if not separator or interface.strip() == "lo":
            continue
        fields = counters.split()
        try:
            total += int(fields[0]) + int(fields[8])
            found = True
        except (IndexError, ValueError):
            continue
    return total if found else None


def _thermal_limits() -> tuple[float | None, float | None, float | None]:
    selected: tuple[float | None, float | None, float | None] = (None, None, None)
    selected_ratio = -1.0
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw_temperature = _integer(zone / "temp")
        if raw_temperature is None:
            continue
        temperature = raw_temperature / 1000
        soft: list[float] = []
        critical: list[float] = []
        for type_path in zone.glob("trip_point_*_type"):
            try:
                trip_type = type_path.read_text(encoding="ascii").strip().casefold()
            except OSError:
                continue
            temperature_path = type_path.with_name(
                type_path.name.replace("_type", "_temp")
            )
            raw_limit = _integer(temperature_path)
            if raw_limit is None:
                continue
            limit = raw_limit / 1000
            if trip_type == "critical":
                critical.append(limit)
            elif trip_type in {"hot", "passive"}:
                soft.append(limit)
        soft_limit = max(soft, default=None)
        critical_limit = min(critical, default=None)
        reference = soft_limit or critical_limit
        ratio = temperature / reference if reference and reference > 0 else temperature
        if ratio > selected_ratio:
            selected = (temperature, soft_limit, critical_limit)
            selected_ratio = ratio
    return selected


def local_download_pressure() -> dict[str, float | int | None]:
    """Best-effort, platform-neutral cgroup and host pressure inputs."""

    memory_current = _integer(_CGROUP / "memory.current")
    memory_maximum = _integer(_CGROUP / "memory.max")
    memory_stat = _keyed_integers(_CGROUP / "memory.stat")
    host_total, host_available = _host_memory()
    if memory_maximum is None:
        memory_maximum = host_total
    inactive_file = memory_stat.get("inactive_file", 0)
    memory_working_set = (
        max(0, memory_current - inactive_file)
        if memory_current is not None
        else (host_total - host_available if host_total and host_available else None)
    )
    memory_events = _keyed_integers(_CGROUP / "memory.events")
    cpu_stat = _keyed_integers(_CGROUP / "cpu.stat")
    temperature, soft_limit, critical_limit = _thermal_limits()
    return {
        "memory_current_bytes": memory_current,
        "memory_working_set_bytes": memory_working_set,
        "memory_max_bytes": memory_maximum,
        "memory_available_bytes": host_available,
        "memory_pressure_avg10": _pressure_average(_CGROUP / "memory.pressure"),
        "memory_events": sum(
            memory_events.get(key, 0) for key in ("high", "max", "oom", "oom_kill")
        ),
        "cpu_usage_usec": cpu_stat.get("usage_usec"),
        "cpu_limit_cores": _cpu_limit_cores(),
        "cpu_pressure_avg10": _pressure_average(_CGROUP / "cpu.pressure"),
        "io_pressure_avg10": _pressure_average(_CGROUP / "io.pressure", "full"),
        "host_io_pressure_avg10": _pressure_average(Path("/proc/pressure/io"), "full"),
        "network_total_bytes": _network_bytes(),
        "temperature_c": temperature,
        "temperature_soft_limit_c": soft_limit,
        "temperature_critical_c": critical_limit,
    }


class LocalResourceSampler:
    """Convert monotonic cgroup counters into rates for one worker."""

    def __init__(self, reader: Callable[[], dict[str, Any]] = local_download_pressure):
        self.reader = reader
        self._previous_at: float | None = None
        self._previous_cpu_usec: int | None = None
        self._previous_network_bytes: int | None = None
        self._previous_memory_events: int | None = None

    def sample(self, *, now: float | None = None) -> dict[str, Any]:
        observed_at = monotonic() if now is None else now
        result = dict(self.reader())
        cpu_usage = result.get("cpu_usage_usec")
        network_bytes = result.get("network_total_bytes")
        memory_events = result.get("memory_events")
        result["cpu_usage_ratio"] = None
        result["network_bytes_per_second"] = None
        result["memory_events_delta"] = 0
        if self._previous_at is not None and observed_at > self._previous_at:
            elapsed = observed_at - self._previous_at
            cores = float(result.get("cpu_limit_cores") or 1.0)
            if isinstance(cpu_usage, int) and self._previous_cpu_usec is not None:
                result["cpu_usage_ratio"] = max(
                    0.0,
                    (cpu_usage - self._previous_cpu_usec)
                    / (elapsed * 1_000_000 * cores),
                )
            if (
                isinstance(network_bytes, int)
                and self._previous_network_bytes is not None
            ):
                result["network_bytes_per_second"] = max(
                    0.0, (network_bytes - self._previous_network_bytes) / elapsed
                )
            if (
                isinstance(memory_events, int)
                and self._previous_memory_events is not None
            ):
                result["memory_events_delta"] = max(
                    0, memory_events - self._previous_memory_events
                )
        self._previous_at = observed_at
        self._previous_cpu_usec = cpu_usage if isinstance(cpu_usage, int) else None
        self._previous_network_bytes = (
            network_bytes if isinstance(network_bytes, int) else None
        )
        self._previous_memory_events = (
            memory_events if isinstance(memory_events, int) else None
        )
        return result


class AdaptivePageConcurrency:
    """Latency-driven page concurrency with bounded failure/resource pressure."""

    def __init__(
        self,
        target_requests_per_second: float,
        *,
        initial_concurrency: int = 8,
        latency_alpha: float = 0.2,
        size_alpha: float = 0.15,
        safety_factor: float = 1.15,
    ):
        self.target_requests_per_second = max(0.1, target_requests_per_second)
        self.initial_concurrency = max(1, initial_concurrency)
        self.latency_alpha = min(1.0, max(0.01, latency_alpha))
        self.size_alpha = min(1.0, max(0.01, size_alpha))
        self.safety_factor = max(1.0, safety_factor)
        self.latency_ewma_seconds: float | None = None
        self.rate_wait_ewma_seconds: float | None = None
        self.page_bytes_ewma: float | None = None
        self.failure_pressure = 0.0
        self.current_concurrency: int | None = None
        self.last_ceiling = self.initial_concurrency

    @staticmethod
    def _ewma(current: float | None, observed: float, alpha: float) -> float:
        return observed if current is None else current + alpha * (observed - current)

    def observe_success(
        self,
        latency_seconds: float,
        size_bytes: int,
        rate_wait_seconds: float = 0.0,
    ) -> None:
        if latency_seconds > 0:
            self.latency_ewma_seconds = self._ewma(
                self.latency_ewma_seconds,
                min(latency_seconds, 120.0),
                self.latency_alpha,
            )
        if rate_wait_seconds >= 0:
            self.rate_wait_ewma_seconds = self._ewma(
                self.rate_wait_ewma_seconds,
                min(rate_wait_seconds, 120.0),
                self.latency_alpha,
            )
        if size_bytes > 0:
            self.page_bytes_ewma = self._ewma(
                self.page_bytes_ewma,
                float(size_bytes),
                self.size_alpha,
            )
        self.failure_pressure = max(0.0, self.failure_pressure - (1.0 / 64.0))

    def observe_failure(self, status_code: int | None) -> None:
        if status_code == 429:
            self.failure_pressure = max(0.5, self.failure_pressure)
        elif status_code is None or status_code >= 500:
            self.failure_pressure = min(0.75, self.failure_pressure + 0.25)

    def recommend(
        self,
        ceiling: int,
        *,
        memory_current_bytes: int | None = None,
        memory_working_set_bytes: int | None = None,
        memory_max_bytes: int | None = None,
        temperature_c: float | None = None,
        temperature_soft_limit_c: float | None = None,
        temperature_critical_c: float | None = None,
        **_unused: Any,
    ) -> int:
        ceiling = max(1, int(ceiling))
        self.last_ceiling = ceiling
        if self.latency_ewma_seconds is None:
            desired = min(self.initial_concurrency, ceiling)
        else:
            desired = math.ceil(
                self.target_requests_per_second
                * self.latency_ewma_seconds
                * self.safety_factor
            )
            desired = min(ceiling, max(1, desired))
        # Back off even when the very first requests failed: there may not yet
        # be a successful latency sample from this provider.
        desired = max(1, math.ceil(desired * (1.0 - self.failure_pressure)))

        memory_used = memory_working_set_bytes or memory_current_bytes
        if (
            memory_used is not None
            and memory_max_bytes is not None
            and memory_max_bytes > 0
        ):
            reserve = max(64 * _MIB, int(memory_max_bytes * 0.15))
            usable = max(0, memory_max_bytes - memory_used - reserve)
            page_bytes = self.page_bytes_ewma or 4 * _MIB
            bytes_per_slot = max(1 * _MIB, int(page_bytes * 2.5))
            desired = min(desired, max(1, usable // bytes_per_slot))

        if temperature_c is not None:
            if (
                temperature_critical_c is not None
                and temperature_c >= temperature_critical_c - 2.0
            ):
                desired = 1
            elif (
                temperature_soft_limit_c is not None
                and temperature_c >= temperature_soft_limit_c
            ):
                desired = min(desired, max(1, ceiling // 2))

        if self.current_concurrency is None:
            self.current_concurrency = min(
                desired, min(self.initial_concurrency, ceiling)
            )
        elif desired > self.current_concurrency:
            self.current_concurrency = min(
                desired,
                max(
                    self.current_concurrency + 1,
                    math.ceil(self.current_concurrency * 1.5),
                ),
            )
        else:
            self.current_concurrency = desired
        return max(1, min(ceiling, self.current_concurrency))

    def status(self) -> dict[str, Any]:
        return {
            "effective": self.current_concurrency,
            "maximum": self.last_ceiling,
            "target_requests_per_second": self.target_requests_per_second,
            "latency_ewma_ms": (
                round(self.latency_ewma_seconds * 1000, 1)
                if self.latency_ewma_seconds is not None
                else None
            ),
            "rate_wait_ewma_ms": (
                round(self.rate_wait_ewma_seconds * 1000, 1)
                if self.rate_wait_ewma_seconds is not None
                else None
            ),
            "page_kib_ewma": (
                round(self.page_bytes_ewma / 1024, 1)
                if self.page_bytes_ewma is not None
                else None
            ),
            "failure_pressure": round(self.failure_pressure, 3),
        }


class AdaptivePipelineConcurrency:
    """AIMD-like chapter concurrency bounded by live host and source pressure."""

    def __init__(
        self,
        *,
        configured_ceiling: int = 0,
        sampler: LocalResourceSampler | None = None,
        adjustment_interval_seconds: float = 5.0,
    ):
        self.configured_ceiling = max(0, int(configured_ceiling))
        self.sampler = sampler or LocalResourceSampler()
        self.adjustment_interval_seconds = max(0.0, adjustment_interval_seconds)
        self.current_concurrency: int | None = None
        self.maximum_concurrency = 1
        self.reason = "calibrating"
        self._last_adjustment_at: float | None = None
        self._last_sample_at: float | None = None
        self._last_pressure: dict[str, Any] = {}
        self._growth_blocked_until = 0.0
        self._last_status: dict[str, Any] = {
            "effective": 1,
            "maximum": 1,
            "configured_maximum": self.configured_ceiling,
            "reason": self.reason,
            "cpu_limit_cores": 1.0,
            "cpu_usage_percent": None,
            "cpu_pressure_avg10": 0.0,
            "io_pressure_avg10": 0.0,
            "memory_working_set_mib": None,
            "memory_limit_mib": None,
            "memory_headroom_mib": None,
            "network_mib_per_second": None,
            "temperature_c": None,
            "source_latency_ms": None,
            "source_rate_wait_ms": None,
            "source_failure_pressure": 0.0,
        }

    @staticmethod
    def _job_memory_bytes(page_status: dict[str, Any]) -> int:
        page_bytes = float(page_status.get("page_kib_ewma") or 0) * 1024
        page_slots = int(page_status.get("effective") or 1)
        in_flight = int(page_bytes * page_slots * 4)
        return max(_MIN_PIPELINE_JOB_BYTES, in_flight)

    def _sample(self, now: float) -> dict[str, Any]:
        if self._last_sample_at is None or now - self._last_sample_at >= 1.0:
            self._last_pressure = self.sampler.sample(now=now)
            self._last_sample_at = now
        return self._last_pressure

    def slot_ceiling(self) -> int:
        observed_at = monotonic()
        pressure = self.sampler.sample(now=observed_at)
        self._last_pressure = pressure
        self._last_sample_at = observed_at
        cores = float(pressure.get("cpu_limit_cores") or os.cpu_count() or 1)
        cpu_slots = max(1, math.ceil(cores * 4))
        memory_maximum = pressure.get("memory_max_bytes")
        memory_slots = (
            max(1, int(memory_maximum) // _MIN_PIPELINE_JOB_BYTES)
            if isinstance(memory_maximum, int) and memory_maximum > 0
            else _MAX_AUTOMATIC_PIPELINE_SLOTS
        )
        return min(_MAX_AUTOMATIC_PIPELINE_SLOTS, cpu_slots, memory_slots)

    def recommend(
        self,
        active_jobs: int,
        page_status: dict[str, Any] | None = None,
        *,
        configured_ceiling: int | None = None,
        now: float | None = None,
    ) -> int:
        observed_at = monotonic() if now is None else now
        pressure = self._sample(observed_at)
        page = page_status or {}
        if configured_ceiling is not None:
            self.configured_ceiling = max(0, int(configured_ceiling))

        cores = max(0.1, float(pressure.get("cpu_limit_cores") or 1.0))
        automatic_maximum = min(
            _MAX_AUTOMATIC_PIPELINE_SLOTS,
            max(1, math.ceil(cores * 4)),
        )
        self.maximum_concurrency = min(
            automatic_maximum,
            self.configured_ceiling or automatic_maximum,
        )

        latency_ms = page.get("latency_ewma_ms")
        rate_wait_ms = page.get("rate_wait_ewma_ms")
        failures = float(page.get("failure_pressure") or 0.0)
        network_waiting = isinstance(latency_ms, (int, float)) and latency_ms >= 500
        limiter_waiting = isinstance(rate_wait_ms, (int, float)) and rate_wait_ms >= 250
        cpu_target = math.ceil(
            cores * (2 if network_waiting and not limiter_waiting else 1)
        )
        desired = min(self.maximum_concurrency, max(1, cpu_target))
        reason = (
            "network latency leaves room for parallel series"
            if network_waiting
            else "balanced for available CPU"
        )

        memory_maximum = pressure.get("memory_max_bytes")
        memory_used = pressure.get("memory_working_set_bytes")
        memory_headroom: int | None = None
        if (
            isinstance(memory_maximum, int)
            and memory_maximum > 0
            and isinstance(memory_used, int)
        ):
            reserve = max(64 * _MIB, int(memory_maximum * 0.10))
            memory_headroom = memory_maximum - memory_used - reserve
            additional = max(0, memory_headroom) // self._job_memory_bytes(page)
            memory_target = max(1, active_jobs + int(additional))
            desired = min(desired, memory_target)
            if memory_headroom <= 0:
                reason = "memory reserve reached"

        memory_pressure = float(pressure.get("memory_pressure_avg10") or 0.0)
        cpu_pressure = float(pressure.get("cpu_pressure_avg10") or 0.0)
        io_pressure = max(
            float(pressure.get("io_pressure_avg10") or 0.0),
            float(pressure.get("host_io_pressure_avg10") or 0.0),
        )
        cpu_usage = pressure.get("cpu_usage_ratio")
        current_target = self.current_concurrency or max(1, active_jobs or desired)
        severe_resource_pressure = False
        if int(pressure.get("memory_events_delta") or 0) > 0 or memory_pressure >= 1.0:
            desired = min(desired, max(1, current_target // 2))
            reason = "kernel memory pressure"
            severe_resource_pressure = True
        elif io_pressure >= 20.0:
            io_target = max(
                1,
                math.floor(cores * (1.0 - min(io_pressure, 90.0) / 100.0)),
            )
            desired = min(desired, io_target)
            reason = "host I/O stall pressure"
            severe_resource_pressure = True
        elif failures > 0:
            desired = min(
                desired,
                max(
                    1,
                    math.floor(current_target * (1.0 - failures)),
                ),
            )
            reason = "source errors or rate limiting"
        elif isinstance(cpu_usage, (int, float)) and cpu_usage >= 0.90:
            desired = min(desired, max(1, current_target - 1))
            reason = "CPU saturation"
        elif cpu_pressure >= 10.0:
            desired = min(desired, current_target)
            reason = "CPU scheduling pressure"
        elif io_pressure >= 5.0:
            desired = min(desired, current_target)
            reason = "I/O headroom held in reserve"
        elif limiter_waiting:
            desired = min(desired, current_target)
            reason = "provider request limit reached"
        elif isinstance(cpu_usage, (int, float)) and cpu_usage >= 0.75:
            desired = min(desired, current_target)
            reason = "CPU headroom held in reserve"

        temperature = pressure.get("temperature_c")
        soft_limit = pressure.get("temperature_soft_limit_c")
        critical_limit = pressure.get("temperature_critical_c")
        if (
            isinstance(temperature, (int, float))
            and isinstance(critical_limit, (int, float))
            and temperature >= critical_limit - 2.0
        ):
            desired = 1
            reason = "critical thermal trip is near"
        elif (
            isinstance(temperature, (int, float))
            and isinstance(soft_limit, (int, float))
            and temperature >= soft_limit
        ):
            desired = min(desired, max(1, current_target // 2))
            reason = "platform cooling threshold reached"

        if severe_resource_pressure:
            recovery_seconds = max(30.0, self.adjustment_interval_seconds * 6)
            self._growth_blocked_until = max(
                self._growth_blocked_until,
                observed_at + recovery_seconds,
            )

        desired = max(1, min(self.maximum_concurrency, desired))
        if self.current_concurrency is None:
            self.current_concurrency = min(desired, max(1, math.ceil(cores)))
            self._last_adjustment_at = observed_at
        elif desired < self.current_concurrency:
            self.current_concurrency = desired
            self._last_adjustment_at = observed_at
        elif desired > self.current_concurrency and (
            observed_at >= self._growth_blocked_until
            and (
                self._last_adjustment_at is None
                or observed_at - self._last_adjustment_at
                >= self.adjustment_interval_seconds
            )
        ):
            self.current_concurrency += 1
            self._last_adjustment_at = observed_at
        if (
            desired > self.current_concurrency
            and observed_at < self._growth_blocked_until
            and reason
            in {
                "balanced for available CPU",
                "network latency leaves room for parallel series",
            }
        ):
            reason = "recovering after resource pressure"
        self.reason = reason
        self._last_status = {
            "effective": self.current_concurrency,
            "maximum": self.maximum_concurrency,
            "configured_maximum": self.configured_ceiling,
            "reason": self.reason,
            "cpu_limit_cores": round(cores, 2),
            "cpu_usage_percent": (
                round(float(cpu_usage) * 100, 1)
                if isinstance(cpu_usage, (int, float))
                else None
            ),
            "cpu_pressure_avg10": round(cpu_pressure, 2),
            "io_pressure_avg10": round(io_pressure, 2),
            "memory_working_set_mib": (
                round(int(memory_used) / _MIB, 1)
                if isinstance(memory_used, int)
                else None
            ),
            "memory_limit_mib": (
                round(int(memory_maximum) / _MIB, 1)
                if isinstance(memory_maximum, int)
                else None
            ),
            "memory_headroom_mib": (
                round(memory_headroom / _MIB, 1)
                if memory_headroom is not None
                else None
            ),
            "network_mib_per_second": (
                round(float(pressure["network_bytes_per_second"]) / _MIB, 2)
                if isinstance(pressure.get("network_bytes_per_second"), (int, float))
                else None
            ),
            "temperature_c": (
                round(float(temperature), 1)
                if isinstance(temperature, (int, float))
                else None
            ),
            "source_latency_ms": latency_ms,
            "source_rate_wait_ms": rate_wait_ms,
            "source_failure_pressure": round(failures, 3),
        }
        return self.current_concurrency

    def status(self) -> dict[str, Any]:
        return dict(self._last_status)


__all__ = [
    "AdaptivePageConcurrency",
    "AdaptivePipelineConcurrency",
    "LocalResourceSampler",
    "local_download_pressure",
]
