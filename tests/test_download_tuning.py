from __future__ import annotations

from tankarr.download_tuning import (
    AdaptivePageConcurrency,
    AdaptivePipelineConcurrency,
    LocalResourceSampler,
)

MIB = 1024 * 1024


class StaticSampler:
    def __init__(self, pressure: dict):
        self.pressure = pressure

    def sample(self, *, now=None):
        del now
        return dict(self.pressure)


def healthy_pressure(**overrides):
    pressure = {
        "cpu_limit_cores": 4.0,
        "cpu_usage_ratio": 0.20,
        "cpu_pressure_avg10": 0.0,
        "io_pressure_avg10": 0.0,
        "host_io_pressure_avg10": 0.0,
        "memory_current_bytes": 900 * MIB,
        "memory_working_set_bytes": 700 * MIB,
        "memory_max_bytes": 1024 * MIB,
        "memory_pressure_avg10": 0.0,
        "memory_events_delta": 0,
        "network_bytes_per_second": 8 * MIB,
        "temperature_c": 60.0,
        "temperature_soft_limit_c": 80.0,
        "temperature_critical_c": 100.0,
    }
    pressure.update(overrides)
    return pressure


def page_status(**overrides):
    status = {
        "effective": 5,
        "page_kib_ewma": 250.0,
        "latency_ewma_ms": 2_000.0,
        "rate_wait_ewma_ms": 0.0,
        "failure_pressure": 0.0,
    }
    status.update(overrides)
    return status


def test_concurrency_follows_littles_law_and_configured_ceiling():
    tuning = AdaptivePageConcurrency(24)

    assert tuning.recommend(12) == 8
    for _ in range(16):
        tuning.observe_success(0.02, 200_000)
    assert tuning.recommend(12) == 1

    for _ in range(24):
        tuning.observe_success(0.6, 2_000_000)
    assert tuning.recommend(12) == 2
    assert tuning.recommend(12) == 3
    assert tuning.recommend(12) == 5
    assert tuning.recommend(12) == 8
    assert tuning.recommend(12) == 12


def test_failures_and_local_pressure_reduce_concurrency():
    tuning = AdaptivePageConcurrency(24)
    for _ in range(16):
        tuning.observe_success(1.0, 8 * 1024 * 1024)
    assert tuning.recommend(12) == 8
    assert tuning.recommend(12) == 12

    tuning.observe_failure(429)
    assert tuning.recommend(12) == 6
    assert tuning.failure_pressure == 0.5

    constrained = tuning.recommend(
        12,
        memory_current_bytes=900 * 1024 * 1024,
        memory_max_bytes=1024 * 1024 * 1024,
    )
    assert constrained == 1
    assert (
        tuning.recommend(
            12,
            temperature_c=85.0,
            temperature_soft_limit_c=80.0,
            temperature_critical_c=86.0,
        )
        == 1
    )


def test_server_errors_add_less_pressure_than_rate_limiting():
    tuning = AdaptivePageConcurrency(24)

    tuning.observe_failure(503)
    assert tuning.failure_pressure == 0.25
    tuning.observe_success(0.1, 100_000)
    assert tuning.failure_pressure < 0.25
    tuning.observe_failure(404)
    assert tuning.failure_pressure < 0.25


def test_cold_provider_backs_off_without_a_successful_latency_sample():
    tuning = AdaptivePageConcurrency(24)
    assert tuning.recommend(12) == 8

    tuning.observe_failure(429)
    assert tuning.latency_ewma_seconds is None
    assert tuning.recommend(12) == 4
    tuning.observe_failure(503)
    assert tuning.recommend(12) == 2


def test_resource_sampler_turns_cgroup_counters_into_rates():
    snapshots = iter(
        [
            {
                "cpu_usage_usec": 1_000_000,
                "cpu_limit_cores": 2.0,
                "network_total_bytes": 10 * MIB,
                "memory_events": 3,
            },
            {
                "cpu_usage_usec": 2_000_000,
                "cpu_limit_cores": 2.0,
                "network_total_bytes": 14 * MIB,
                "memory_events": 4,
            },
        ]
    )
    sampler = LocalResourceSampler(lambda: next(snapshots))

    assert sampler.sample(now=10)["cpu_usage_ratio"] is None
    measured = sampler.sample(now=11)
    assert measured["cpu_usage_ratio"] == 0.5
    assert measured["network_bytes_per_second"] == 4 * MIB
    assert measured["memory_events_delta"] == 1


def test_pipeline_starts_from_cpu_capacity_and_grows_for_slow_healthy_source():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure()),  # type: ignore[arg-type]
        adjustment_interval_seconds=5,
    )

    assert tuning.recommend(0, page_status(), now=0) == 4
    assert tuning.recommend(4, page_status(), now=5) == 5
    assert tuning.recommend(5, page_status(), now=10) == 6
    assert tuning.recommend(6, page_status(), now=15) == 7
    assert tuning.recommend(7, page_status(), now=20) == 8
    assert (
        tuning.status()["reason"] == "network latency leaves room for parallel series"
    )


def test_pipeline_uses_working_set_instead_of_reclaimable_file_cache():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure()),  # type: ignore[arg-type]
    )

    assert tuning.recommend(2, page_status(latency_ewma_ms=100.0), now=0) == 4
    assert tuning.status()["memory_working_set_mib"] == 700.0
    assert tuning.status()["memory_headroom_mib"] > 200


def test_pipeline_reacts_to_source_and_kernel_pressure_immediately():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure()),  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )
    assert tuning.recommend(4, page_status(), now=0) == 4

    assert tuning.recommend(4, page_status(failure_pressure=0.5), now=2) == 2
    assert tuning.status()["reason"] == "source errors or rate limiting"

    pressured = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure(memory_events_delta=1)),  # type: ignore[arg-type]
    )
    assert pressured.recommend(6, page_status(), now=0) == 3
    assert pressured.status()["reason"] == "kernel memory pressure"


def test_pipeline_reduces_slots_for_host_io_stalls_without_cascading():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure(host_io_pressure_avg10=47.0)),  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )

    assert tuning.recommend(4, page_status(), now=0) == 2
    assert tuning.recommend(2, page_status(), now=2) == 2
    assert tuning.status()["reason"] == "host I/O stall pressure"
    assert tuning.status()["io_pressure_avg10"] == 47.0


def test_pipeline_holds_growth_for_moderate_io_pressure():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure(host_io_pressure_avg10=8.0)),  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )

    assert tuning.recommend(4, page_status(), now=0) == 4
    assert tuning.recommend(4, page_status(), now=2) == 4
    assert tuning.status()["reason"] == "I/O headroom held in reserve"


def test_pipeline_waits_for_stable_recovery_before_growing_again():
    sampler = StaticSampler(healthy_pressure(host_io_pressure_avg10=60.0))
    tuning = AdaptivePipelineConcurrency(
        sampler=sampler,  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )

    assert tuning.recommend(4, page_status(), now=0) == 1
    sampler.pressure = healthy_pressure(host_io_pressure_avg10=30.0)
    assert tuning.recommend(1, page_status(), now=2) == 1

    sampler.pressure = healthy_pressure()
    assert tuning.recommend(1, page_status(), now=20) == 1
    assert tuning.status()["reason"] == "recovering after resource pressure"
    assert tuning.recommend(1, page_status(), now=33) == 2


def test_pipeline_does_not_add_jobs_when_provider_limiter_is_backed_up():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(healthy_pressure()),  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )

    limited = page_status(rate_wait_ewma_ms=750.0)
    assert tuning.recommend(4, limited, now=0) == 4
    assert tuning.recommend(4, limited, now=2) == 4
    assert tuning.status()["reason"] == "provider request limit reached"


def test_pipeline_operator_value_is_only_a_ceiling():
    tuning = AdaptivePipelineConcurrency(
        configured_ceiling=3,
        sampler=StaticSampler(healthy_pressure()),  # type: ignore[arg-type]
        adjustment_interval_seconds=0,
    )

    assert tuning.recommend(0, page_status(), now=0) == 3
    assert tuning.status()["maximum"] == 3

    constrained = AdaptivePipelineConcurrency(
        configured_ceiling=12,
        sampler=StaticSampler(healthy_pressure(memory_working_set_bytes=950 * MIB)),  # type: ignore[arg-type]
    )
    assert constrained.recommend(0, page_status(), now=0) == 1


def test_active_cooling_trip_is_not_a_cpu_throttling_limit():
    tuning = AdaptivePipelineConcurrency(
        sampler=StaticSampler(
            healthy_pressure(
                temperature_c=78.0,
                temperature_soft_limit_c=None,
                temperature_critical_c=110.0,
            )
        ),  # type: ignore[arg-type]
    )

    assert tuning.recommend(0, page_status(latency_ewma_ms=None), now=0) == 4
    assert tuning.status()["reason"] == "balanced for available CPU"
