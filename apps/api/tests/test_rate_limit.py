import pytest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings
from app.core.rate_limit import (
    LOGIN_RATE_LIMIT_POLICY,
    PRIVATE_V1_RATE_LIMIT_POLICY,
    PUBLIC_MEDIA_RATE_LIMIT_POLICY,
    PUBLIC_RATE_LIMIT_POLICY,
    RateLimitMiddleware,
    client_ip_from_request,
    resolve_rate_limit_policy,
)


@pytest.fixture(autouse=True)
def enable_rate_limits(monkeypatch):
    monkeypatch.setattr("app.core.rate_limit.settings.rate_limit_enabled", True)


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


def _build_rate_limited_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def login() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/public/objects")
    def public_objects() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/public/objects/{object_id}/model/file")
    def public_object_model(object_id: str) -> dict[str, str]:
        return {"object_id": object_id}

    @app.get("/api/public/objects/demo")
    def public_embed_manifest() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/projects")
    def projects() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _build_cors_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.resolved_cors_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "Range", "X-Requested-With"],
    )

    @app.get("/api/v1/public/objects")
    def public_objects() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _request_with_forwarded_for(value: str, *, direct_client: str = "172.24.0.5"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", value.encode("ascii"))],
            "client": (direct_client, 43210),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_resolve_rate_limit_policy_matches_launch_requirements() -> None:
    assert resolve_rate_limit_policy("/health", "GET") is None
    assert resolve_rate_limit_policy("/metrics", "GET") is None
    assert resolve_rate_limit_policy("/api/v1/ping", "GET") is None
    assert resolve_rate_limit_policy("/api/v1/auth/login", "POST") == LOGIN_RATE_LIMIT_POLICY
    assert resolve_rate_limit_policy("/api/v1/public/objects", "GET") == PUBLIC_RATE_LIMIT_POLICY
    assert resolve_rate_limit_policy("/api/public/objects/demo", "GET") == PUBLIC_RATE_LIMIT_POLICY
    assert (
        resolve_rate_limit_policy("/api/v1/public/objects/00000000-0000-0000-0000-000000000001/model/file", "GET")
        == PUBLIC_MEDIA_RATE_LIMIT_POLICY
    )
    assert resolve_rate_limit_policy("/api/v1/public/videos/demo/stream", "GET") == PUBLIC_MEDIA_RATE_LIMIT_POLICY
    assert resolve_rate_limit_policy("/api/public/clips/demo/stream", "GET") == PUBLIC_MEDIA_RATE_LIMIT_POLICY
    assert resolve_rate_limit_policy("/api/v1/projects", "GET") == PRIVATE_V1_RATE_LIMIT_POLICY
    assert resolve_rate_limit_policy("/api/v1/public/objects", "OPTIONS") is None


def test_client_ip_uses_rightmost_public_forwarded_ip() -> None:
    request = _request_with_forwarded_for("198.51.100.10, 203.0.113.9, 8.8.8.8")

    assert client_ip_from_request(request) == "8.8.8.8"


def test_client_ip_ignores_leftmost_spoofed_forwarded_ip_when_proxy_appends_public_ip() -> None:
    request = _request_with_forwarded_for("8.8.4.4, 9.9.9.9")

    assert client_ip_from_request(request) == "9.9.9.9"


def test_client_ip_falls_back_to_rightmost_valid_forwarded_hop() -> None:
    request = _request_with_forwarded_for("garbage, 10.0.0.4, 172.24.0.5")

    assert client_ip_from_request(request) == "172.24.0.5"


def test_production_cors_origin_list_filters_local_preview_origins() -> None:
    settings = Settings(
        semantic_env="production",
        cors_origins="https://viewer.example.test,http://localhost:4321,http://127.0.0.1:4321",
        semantic_embed_allowed_origins="http://localhost:4321,https://embed.example.test",
    )

    assert settings.cors_origin_list == ["https://viewer.example.test", "https://embed.example.test"]


def test_production_cors_preflight_allows_live_origin_and_rejects_untrusted_origins() -> None:
    settings = Settings(
        semantic_env="production",
        cors_origins="https://viewer.example.test,http://localhost:4321",
        semantic_embed_allowed_origins="http://127.0.0.1:4321,https://embed.example.test",
    )

    with TestClient(_build_cors_app(settings)) as client:
        allowed_response = client.options(
            "/api/v1/public/objects",
            headers={
                "Origin": "https://viewer.example.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        blocked_response = client.options(
            "/api/v1/public/objects",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        local_preview_response = client.options(
            "/api/v1/public/objects",
            headers={
                "Origin": "http://localhost:4321",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed_response.status_code == 200
    assert allowed_response.headers["access-control-allow-origin"] == "https://viewer.example.test"
    assert blocked_response.status_code == 400
    assert "access-control-allow-origin" not in blocked_response.headers
    assert local_preview_response.status_code == 400
    assert "access-control-allow-origin" not in local_preview_response.headers


def test_rate_limit_middleware_enforces_login_limit(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: fake_redis)

    with TestClient(_build_rate_limited_app()) as client:
        for _ in range(LOGIN_RATE_LIMIT_POLICY.limit):
            response = client.post("/api/v1/auth/login")
            assert response.status_code == 200

        response = client.post("/api/v1/auth/login")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(LOGIN_RATE_LIMIT_POLICY.window_seconds)


def test_rate_limit_middleware_enforces_public_limit_across_public_prefixes(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: fake_redis)

    with TestClient(_build_rate_limited_app()) as client:
        for _ in range(PUBLIC_RATE_LIMIT_POLICY.limit):
            response = client.get("/api/v1/public/objects")
            assert response.status_code == 200

        response = client.get("/api/public/objects/demo")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(PUBLIC_RATE_LIMIT_POLICY.window_seconds)


def test_rate_limit_middleware_keeps_public_media_in_separate_bucket(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: fake_redis)

    with TestClient(_build_rate_limited_app()) as client:
        for _ in range(PUBLIC_RATE_LIMIT_POLICY.limit):
            response = client.get("/api/v1/public/objects")
            assert response.status_code == 200

        response = client.get("/api/v1/public/objects/demo/model/file")
        assert response.status_code == 200

        for _ in range(PUBLIC_MEDIA_RATE_LIMIT_POLICY.limit - 1):
            response = client.get("/api/v1/public/objects/demo/model/file")
            assert response.status_code == 200

        response = client.get("/api/v1/public/objects/demo/model/file")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(PUBLIC_MEDIA_RATE_LIMIT_POLICY.window_seconds)


def test_rate_limit_middleware_enforces_private_v1_limit(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.core.rate_limit._rate_limit_redis", lambda: fake_redis)

    with TestClient(_build_rate_limited_app()) as client:
        for _ in range(PRIVATE_V1_RATE_LIMIT_POLICY.limit):
            response = client.get("/api/v1/projects")
            assert response.status_code == 200

        response = client.get("/api/v1/projects")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(PRIVATE_V1_RATE_LIMIT_POLICY.window_seconds)
