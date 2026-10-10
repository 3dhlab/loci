"""Exercise the real Sentry/FastAPI patch in a clean interpreter.

Sentry patches FastAPI routing at import time, so this regression test runs its
integration scenario in a subprocess. That keeps the patch from leaking into
other API tests in the pytest process.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


_SCENARIO = textwrap.dedent(
    r"""
    import asyncio
    import json
    from concurrent.futures import ThreadPoolExecutor

    import sentry_sdk
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from sentry_sdk.envelope import Envelope
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.transport import Transport

    from app.core.monitoring import build_privacy_safe_server_event

    SECRET = "private-search-query-7f3b"

    class MemoryTransport(Transport):
        instance = None

        def __init__(self, options):
            super().__init__(options)
            self.events = []
            MemoryTransport.instance = self

        def capture_envelope(self, envelope: Envelope):
            for item in envelope.items:
                event = item.get_event()
                if event is not None:
                    self.events.append(event)

    child = APIRouter()
    nested = APIRouter()

    @child.post("/sync")
    def sync_endpoint(payload: dict):
        return {"ok": True, "value": payload.get("value")}

    @child.post("/async")
    async def async_endpoint(payload: dict):
        return {"ok": True, "value": payload.get("value")}

    @child.post("/error")
    def error_endpoint(payload: dict):
        raise RuntimeError(f"failed query {payload.get('query')}")

    nested.include_router(child, prefix="/nested")
    app = FastAPI()
    app.include_router(nested, prefix="/api")

    sentry_sdk.init(
        dsn="https://public@example.ingest.sentry.io/1",
        integrations=[FastApiIntegration(transaction_style="endpoint")],
        traces_sample_rate=0.0,
        send_default_pii=False,
        max_breadcrumbs=0,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=lambda event, _hint: build_privacy_safe_server_event(
            event, runtime="api"
        ),
        transport=MemoryTransport,
    )

    sync_route = child.routes[0]
    def wrapper_depth(call):
        depth = 0
        seen = set()
        while call is not None and id(call) not in seen:
            seen.add(id(call))
            call = getattr(call, "__wrapped__", None)
            if call is not None:
                depth += 1
        return depth

    with TestClient(app, raise_server_exceptions=False) as client:
        sequential = []
        for index in range(2100):
            response = client.post("/api/nested/sync", json={"value": index})
            sequential.append(response.status_code)
            if response.status_code != 200:
                break

        def concurrent_request(index):
            return client.post(
                "/api/nested/sync", json={"value": f"parallel-{index}"}
            ).status_code

        with ThreadPoolExecutor(max_workers=16) as pool:
            concurrent = list(pool.map(concurrent_request, range(64)))

        async_status = client.post("/api/nested/async", json={"value": "control"}).status_code
        error_response = client.post(
            "/api/nested/error", json={"query": SECRET}
        )

    sentry_sdk.flush(timeout=5)
    events = MemoryTransport.instance.events
    safe_events = [json.dumps(event, sort_keys=True) for event in events]
    runtime_error_events = [
        event
        for event in events
        if any(
            value.get("type") == "RuntimeError"
            for value in event.get("exception", {}).get("values", [])
        )
    ]
    result = {
        "sequential_count": len(sequential),
        "sequential_failures": sum(code != 200 for code in sequential),
        "first_sequential_failure": next(
            (index for index, code in enumerate(sequential) if code != 200), None
        ),
        "concurrent_failures": sum(code != 200 for code in concurrent),
        "async_status": async_status,
        "error_status": error_response.status_code,
        "wrapper_depth": wrapper_depth(sync_route.dependant.call),
        "event_count": len(events),
        "runtime_error_event_count": len(runtime_error_events),
        "secret_leaked": any(SECRET in event for event in safe_events),
        "privacy_shape_ok": bool(runtime_error_events)
        and all(
            event.get("transaction") == "API request"
            and event.get("tags") == {"runtime": "api"}
            and event.get("exception", {}).get("values", [{}])[0].get("value")
            == "Server-side exception"
            and "request" not in event
            and "user" not in event
            for event in runtime_error_events
        ),
    }
    print("RUNTIME_RESULT=" + json.dumps(result, sort_keys=True))
    """
)


def test_real_sentry_fastapi_integration_is_stable_under_repeated_requests():
    completed = subprocess.run(
        [sys.executable, "-c", _SCENARIO],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )

    result_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith("RUNTIME_RESULT=")),
        None,
    )
    assert completed.returncode == 0 and result_line is not None, (
        "Sentry/FastAPI runtime scenario crashed.\n"
        f"stdout:\n{completed.stdout[-4000:]}\n"
        f"stderr:\n{completed.stderr[-4000:]}"
    )
    result = json.loads(result_line.removeprefix("RUNTIME_RESULT="))

    assert result["sequential_count"] == 2100, result
    assert result["sequential_failures"] == 0, result
    assert result["concurrent_failures"] == 0, result
    assert result["async_status"] == 200, result
    assert result["error_status"] == 500, result
    assert result["wrapper_depth"] <= 2, result
    assert result["runtime_error_event_count"] >= 1, result
    assert result["secret_leaked"] is False, result
    assert result["privacy_shape_ok"] is True, result
