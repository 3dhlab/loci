from __future__ import annotations

from collections import namedtuple

from app.core.config import settings
from app.services import runtime_health


DiskUsage = namedtuple("DiskUsage", "total used free")


def test_runtime_root_full_degrades_health_while_media_metrics_remain_healthy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_root_disk_alert_threshold_percent", 85)
    monkeypatch.setattr(settings, "runtime_root_disk_min_free_bytes", 5 * 1024**3)
    monkeypatch.setattr(runtime_health.shutil, "disk_usage", lambda path: DiskUsage(1000, 900, 100))
    monkeypatch.setattr(runtime_health, "_probe_database", lambda: runtime_health._service_status("database", True, "ok"))
    monkeypatch.setattr(
        runtime_health,
        "_probe_redis_and_queues",
        lambda: (runtime_health._service_status("redis", True, "ok"), {"queue_depth": 0}),
    )
    monkeypatch.setattr(
        runtime_health,
        "_probe_media_root",
        lambda: (
            runtime_health._service_status("media_root", True, "Media is healthy."),
            {"disk_usage_percent": 14, "disk_alert_triggered": False, "media_root_exists": True},
        ),
    )

    snapshot = runtime_health.build_runtime_health_snapshot()

    assert snapshot["status"] == "degraded"
    assert snapshot["services"]["media_root"]["status"] == "ok"
    assert snapshot["services"]["runtime_root_filesystem"]["status"] == "degraded"
    assert snapshot["services"]["runtime_root_filesystem"]["disk_alert_triggered"] is True
    assert snapshot["metrics"]["disk_usage_percent"] == 14
    assert snapshot["metrics"]["disk_alert_triggered"] is False
    assert snapshot["metrics"]["runtime_root_disk_usage_percent"] == 90
    assert snapshot["metrics"]["runtime_root_disk_free_bytes"] == 100


def test_runtime_root_alerts_when_free_space_threshold_is_crossed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_root_disk_alert_threshold_percent", 95)
    monkeypatch.setattr(settings, "runtime_root_disk_min_free_bytes", 500)
    monkeypatch.setattr(runtime_health.shutil, "disk_usage", lambda path: DiskUsage(10_000, 9_000, 400))

    service, metrics = runtime_health._probe_runtime_root_filesystem()

    assert service["status"] == "degraded"
    assert service["disk_usage_percent"] == 90
    assert service["free_bytes"] == 400
    assert metrics["runtime_root_disk_alert_triggered"] is True


def test_runtime_root_probe_failure_degrades_health_without_raising(monkeypatch) -> None:
    monkeypatch.setattr(runtime_health.shutil, "disk_usage", lambda _path: (_ for _ in ()).throw(OSError("unavailable")))

    service, metrics = runtime_health._probe_runtime_root_filesystem()

    assert service["status"] == "degraded"
    assert service["probe_failed"] is True
    assert "unavailable" not in service["detail"]
    assert metrics["runtime_root_disk_probe_failed"] is True
    assert metrics["runtime_root_disk_usage_percent"] is None
    assert metrics["runtime_root_disk_free_bytes"] is None


def test_runtime_root_healthy_when_both_thresholds_have_headroom(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_root_disk_alert_threshold_percent", 85)
    monkeypatch.setattr(settings, "runtime_root_disk_min_free_bytes", 500)
    monkeypatch.setattr(runtime_health.shutil, "disk_usage", lambda _path: DiskUsage(10_000, 8_000, 2_000))

    service, metrics = runtime_health._probe_runtime_root_filesystem()

    assert service["status"] == "ok"
    assert service["disk_alert_triggered"] is False
    assert metrics["runtime_root_disk_usage_percent"] == 80
    assert metrics["runtime_root_disk_probe_failed"] is False
