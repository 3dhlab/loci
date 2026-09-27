from __future__ import annotations

import sys
import json
from types import ModuleType

from app.core import monitoring


class DummyFastApiIntegration:
    def __init__(self, transaction_style: str | None = None):
        self.transaction_style = transaction_style


class DummyRqIntegration:
    pass


def test_configure_sentry_noops_without_dsn(monkeypatch):
    monkeypatch.setattr(monitoring.settings, "sentry_dsn", "")

    assert monitoring.configure_sentry("api") is False


def test_configure_sentry_initializes_fastapi(monkeypatch):
    calls: dict[str, object] = {}
    sentry_sdk_module = ModuleType("sentry_sdk")
    fastapi_module = ModuleType("sentry_sdk.integrations.fastapi")
    rq_module = ModuleType("sentry_sdk.integrations.rq")

    def fake_init(**kwargs):
        calls.update(kwargs)

    sentry_sdk_module.init = fake_init
    fastapi_module.FastApiIntegration = DummyFastApiIntegration
    rq_module.RqIntegration = DummyRqIntegration

    monkeypatch.setattr(monitoring.settings, "sentry_dsn", "https://public@example.ingest.sentry.io/1")
    monkeypatch.setattr(monitoring.settings, "semantic_env", "production")
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fastapi_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.rq", rq_module)

    assert monitoring.configure_sentry("api") is True
    assert calls["dsn"] == "https://public@example.ingest.sentry.io/1"
    assert calls["environment"] == "production"
    assert calls["traces_sample_rate"] == 0.0
    assert calls["send_default_pii"] is False
    assert calls["max_breadcrumbs"] == 0
    assert calls["max_request_body_size"] == "never"
    assert calls["include_local_variables"] is False
    assert callable(calls["before_send"])
    assert len(calls["integrations"]) == 1
    assert isinstance(calls["integrations"][0], DummyFastApiIntegration)
    assert calls["integrations"][0].transaction_style == "endpoint"


def test_configure_sentry_initializes_rq(monkeypatch):
    calls: dict[str, object] = {}
    sentry_sdk_module = ModuleType("sentry_sdk")
    fastapi_module = ModuleType("sentry_sdk.integrations.fastapi")
    rq_module = ModuleType("sentry_sdk.integrations.rq")

    def fake_init(**kwargs):
        calls.update(kwargs)

    sentry_sdk_module.init = fake_init
    fastapi_module.FastApiIntegration = DummyFastApiIntegration
    rq_module.RqIntegration = DummyRqIntegration

    monkeypatch.setattr(monitoring.settings, "sentry_dsn", "https://public@example.ingest.sentry.io/1")
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fastapi_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.rq", rq_module)

    assert monitoring.configure_sentry("worker") is True
    assert len(calls["integrations"]) == 1
    assert isinstance(calls["integrations"][0], DummyRqIntegration)


def test_server_event_allowlist_removes_private_request_and_worker_data():
    secret = "participant-private-secret"
    event = {
        "event_id": "a" * 32,
        "timestamp": 1_787_000_000.25,
        "platform": "python",
        "level": "error",
        "environment": "production",
        "release": "loci-2026.08.11",
        "message": f"failed for {secret}",
        "transaction": f"/api/private/import/{secret}",
        "request": {
            "url": f"https://example.invalid/?token={secret}",
            "data": {"transcript": secret},
            "headers": {"authorization": secret},
        },
        "user": {"email": f"{secret}@example.invalid"},
        "breadcrumbs": [{"message": secret}],
        "contexts": {"runtime": {"name": secret}},
        "extra": {"storage_path": f"/private/{secret}.mp4"},
        "tags": {"credential": secret},
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": secret,
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": f"/Users/private/{secret}/jobs.py",
                                "abs_path": f"/Users/private/{secret}/jobs.py",
                                "function": secret,
                                "lineno": 42,
                                "vars": {"payload": secret},
                                "context_line": secret,
                                "in_app": True,
                            }
                        ]
                    },
                    "mechanism": {"handled": False, "data": {"detail": secret}},
                }
            ]
        },
    }

    safe = monitoring.build_privacy_safe_server_event(event, runtime="worker")
    serialized = json.dumps(safe, sort_keys=True)

    assert secret not in serialized
    assert "/Users/" not in serialized
    assert safe["transaction"] == "Worker job"
    assert safe["tags"] == {"runtime": "worker"}
    assert safe["message"] == "Server-side exception"
    assert safe["exception"]["values"][0] == {
        "type": "RuntimeError",
        "value": "Server-side exception",
        "stacktrace": {"frames": [{"filename": "jobs.py", "lineno": 42, "in_app": True}]},
        "mechanism": {"handled": False},
    }


def test_configured_before_send_uses_the_api_privacy_allowlist(monkeypatch):
    calls: dict[str, object] = {}
    sentry_sdk_module = ModuleType("sentry_sdk")
    fastapi_module = ModuleType("sentry_sdk.integrations.fastapi")

    def fake_init(**kwargs):
        calls.update(kwargs)

    sentry_sdk_module.init = fake_init
    fastapi_module.FastApiIntegration = DummyFastApiIntegration
    monkeypatch.setattr(monitoring.settings, "sentry_dsn", "https://public@example.ingest.sentry.io/1")
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fastapi_module)

    assert monitoring.configure_sentry("api") is True
    safe = calls["before_send"](
        {
            "transaction": "/api/v1/public/objects/private-object/model/file",
            "request": {"query_string": "credential=secret"},
            "exception": {"values": [{"type": "RuntimeError", "value": "secret"}]},
        },
        {"exc_info": "secret"},
    )

    assert safe["transaction"] == "API request"
    assert safe["tags"] == {"runtime": "api"}
    assert safe["exception"]["values"][0]["value"] == "Server-side exception"
    assert "secret" not in json.dumps(safe)
