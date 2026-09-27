from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import sentry_tunnel
from app.core.config import settings
from app.core.rate_limit import RateLimitPolicy


class FakeRedisPipeline:
    def __init__(self, redis_conn):
        self.redis_conn = redis_conn
        self.commands: list[tuple[str, str]] = []

    def incr(self, key: str):
        self.commands.append(("incr", key))
        return self

    def ttl(self, key: str):
        self.commands.append(("ttl", key))
        return self

    def execute(self) -> list[int]:
        results: list[int] = []
        for command, key in self.commands:
            if command == "incr":
                results.append(self.redis_conn.incr(key))
                continue
            if command == "ttl":
                results.append(self.redis_conn.ttl(key))
        self.commands.clear()
        return results


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sentry_tunnel.router, prefix="/api/v1/public")
    return app


def _valid_dsn() -> str:
    return "https://public@example.ingest.sentry.io/123456789"


def _valid_envelope(dsn: str | None = None) -> bytes:
    resolved_dsn = dsn or _valid_dsn()
    return (
        f'{{"event_id":"0123456789abcdef0123456789abcdef","dsn":"{resolved_dsn}"}}\n'
        '{"type":"event"}\n'
        '{}'
    ).encode("utf-8")


def test_sentry_tunnel_forwards_valid_envelope(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.content = b'{"id":"ok"}'
            self.headers = {"content-type": "application/json"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, *, content: bytes, headers: dict[str, str]):
            calls.append({"url": url, "content": content, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(settings, "vite_sentry_dsn", _valid_dsn())
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr("app.api.v1.endpoints.sentry_tunnel.httpx.Client", FakeClient)
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: FakeRedis())

    with TestClient(_build_app()) as client:
        response = client.post(
            "/api/v1/public/sentry-tunnel",
            content=_valid_envelope(),
            headers={
                "Origin": "http://testserver",
                "Content-Type": "application/x-sentry-envelope",
                "User-Agent": "test-client/1.0",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"id": "ok"}
    assert calls[0]["url"] == "https://example.ingest.sentry.io/api/123456789/envelope/"
    assert calls[0]["content"] == _valid_envelope()
    assert "sentry_key=public" in calls[0]["headers"]["X-Sentry-Auth"]


def test_sentry_tunnel_rejects_cross_origin_requests(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vite_sentry_dsn", _valid_dsn())
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: FakeRedis())

    with TestClient(_build_app()) as client:
        response = client.post(
            "/api/v1/public/sentry-tunnel",
            content=_valid_envelope(),
            headers={
                "Origin": "https://evil.example.com",
                "Content-Type": "application/x-sentry-envelope",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-origin Sentry tunnel requests are not allowed."


def test_sentry_tunnel_rejects_mismatched_project(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vite_sentry_dsn", _valid_dsn())
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: FakeRedis())

    with TestClient(_build_app()) as client:
        response = client.post(
            "/api/v1/public/sentry-tunnel",
            content=_valid_envelope("https://public@example.ingest.sentry.io/999999"),
            headers={"Content-Type": "application/x-sentry-envelope"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Sentry project is not allowed."


def test_sentry_tunnel_rejects_oversized_envelope(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vite_sentry_dsn", _valid_dsn())
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: FakeRedis())

    oversized_body = b"x" * (sentry_tunnel.MAX_ENVELOPE_BYTES + 1)

    with TestClient(_build_app()) as client:
        response = client.post(
            "/api/v1/public/sentry-tunnel",
            content=oversized_body,
            headers={"Content-Type": "application/x-sentry-envelope"},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Sentry envelope exceeds the 200KB limit."


def test_sentry_tunnel_enforces_additional_rate_limit(monkeypatch) -> None:
    fake_redis = FakeRedis()

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.content = b""
            self.headers = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, *, content: bytes, headers: dict[str, str]):
            return FakeResponse()

    monkeypatch.setattr(settings, "vite_sentry_dsn", _valid_dsn())
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: fake_redis)
    monkeypatch.setattr(
        sentry_tunnel,
        "SENTRY_TUNNEL_RATE_LIMIT_POLICY",
        RateLimitPolicy(
            key_prefix="test_sentry_tunnel_ratelimit",
            limit=2,
            window_seconds=60,
            detail="Too many monitoring events. Try again later.",
        ),
    )
    monkeypatch.setattr("app.api.v1.endpoints.sentry_tunnel.httpx.Client", FakeClient)

    with TestClient(_build_app()) as client:
        for _ in range(2):
            response = client.post(
                "/api/v1/public/sentry-tunnel",
                content=_valid_envelope(),
                headers={"Content-Type": "application/x-sentry-envelope"},
            )
            assert response.status_code == 200

        response = client.post(
            "/api/v1/public/sentry-tunnel",
            content=_valid_envelope(),
            headers={"Content-Type": "application/x-sentry-envelope"},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"