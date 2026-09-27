from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine

QUEUE_NAMES = ("default", "transcode", "transcription", "index", "clip")


def _service_status(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "status": "ok" if ok else "degraded",
        "detail": detail,
    }
    payload.update(extra)
    return payload


def _probe_database() -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return _service_status("database", True, "SELECT 1 succeeded.")
    except Exception as exc:
        return _service_status("database", False, f"Database probe failed: {exc}")


def _probe_redis_and_queues() -> tuple[dict[str, Any], dict[str, Any]]:
    queue_depths = {name: 0 for name in QUEUE_NAMES}
    failed_job_counts = {name: 0 for name in QUEUE_NAMES}

    try:
        redis_conn = Redis.from_url(settings.redis_url)
        redis_conn.ping()

        for name in QUEUE_NAMES:
            queue = Queue(name, connection=redis_conn)
            queue_depths[name] = len(queue)
            failed_job_counts[name] = len(queue.failed_job_registry.get_job_ids())

        workers = Worker.all(connection=redis_conn)
        return (
            _service_status("redis", True, "Redis ping succeeded.", worker_count=len(workers)),
            {
                "queue_depth": sum(queue_depths.values()),
                "failed_jobs": sum(failed_job_counts.values()),
                "worker_count": len(workers),
                "queue_depths": queue_depths,
                "failed_job_counts": failed_job_counts,
            },
        )
    except Exception as exc:
        return (
            _service_status("redis", False, f"Redis probe failed: {exc}", worker_count=0),
            {
                "queue_depth": 0,
                "failed_jobs": 0,
                "worker_count": 0,
                "queue_depths": queue_depths,
                "failed_job_counts": failed_job_counts,
            },
        )


def _probe_media_root() -> tuple[dict[str, Any], dict[str, Any]]:
    media_root = Path(settings.media_root)
    usage_target = media_root if media_root.exists() else media_root.parent if media_root.parent.exists() else Path("/")
    total_bytes, used_bytes, free_bytes = shutil.disk_usage(usage_target)
    disk_usage_percent = int(round((used_bytes / total_bytes) * 100)) if total_bytes else 0
    threshold_exceeded = disk_usage_percent >= settings.disk_alert_threshold_percent
    exists = media_root.exists()
    ok = exists and not threshold_exceeded

    if not exists:
        detail = f"Media root is missing: {media_root}"
    elif threshold_exceeded:
        detail = (
            f"Media root disk usage is {disk_usage_percent}% at {usage_target}, "
            f"above threshold {settings.disk_alert_threshold_percent}%."
        )
    else:
        detail = f"Media root is available at {media_root}; disk usage {disk_usage_percent}%."

    return (
        _service_status(
            "media_root",
            ok,
            detail,
            path=str(media_root),
            usage_target=str(usage_target),
            exists=exists,
            disk_usage_percent=disk_usage_percent,
            disk_alert_threshold_percent=settings.disk_alert_threshold_percent,
            disk_alert_triggered=threshold_exceeded,
        ),
        {
            "disk_usage_percent": disk_usage_percent,
            "disk_alert_threshold_percent": settings.disk_alert_threshold_percent,
            "disk_alert_triggered": threshold_exceeded,
            "media_root_exists": exists,
            "media_root_path": str(media_root),
            "media_root_usage_target": str(usage_target),
            "media_root_total_bytes": int(total_bytes),
            "media_root_used_bytes": int(used_bytes),
            "media_root_free_bytes": int(free_bytes),
        },
    )


def build_runtime_health_snapshot() -> dict[str, Any]:
    database_status = _probe_database()
    redis_status, queue_metrics = _probe_redis_and_queues()
    media_root_status, disk_metrics = _probe_media_root()

    services = {
        "database": database_status,
        "redis": redis_status,
        "media_root": media_root_status,
    }
    overall_ok = all(service["status"] == "ok" for service in services.values())
    metrics = {
        **queue_metrics,
        **disk_metrics,
    }

    return {
        "status": "ok" if overall_ok else "degraded",
        "app": {
            "name": settings.app_name,
            "environment": settings.semantic_env,
        },
        "services": services,
        "metrics": metrics,
    }


def build_runtime_metrics_snapshot() -> dict[str, Any]:
    snapshot = build_runtime_health_snapshot()
    return {
        "status": snapshot["status"],
        **snapshot["metrics"],
        "services": snapshot["services"],
    }