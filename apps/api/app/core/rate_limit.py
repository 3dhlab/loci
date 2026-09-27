from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitPolicy:
    key_prefix: str
    limit: int
    window_seconds: int
    detail: str


PUBLIC_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="public_ratelimit",
    limit=60,
    window_seconds=60,
    detail="Too many public API requests. Try again later.",
)
PUBLIC_MEDIA_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="public_media_ratelimit",
    limit=300,
    window_seconds=60,
    detail="Too many public media requests. Try again later.",
)
LOGIN_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="login_ratelimit",
    limit=5,
    window_seconds=60,
    detail="Too many login attempts. Try again later.",
)
PRIVATE_V1_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="private_v1_ratelimit",
    limit=120,
    window_seconds=60,
    detail="Too many API requests. Try again later.",
)
# Stricter cap on the unauthenticated subscribe endpoint. The base
# PUBLIC_RATE_LIMIT_POLICY (60/min) applies to all /api/v1/public/*
# routes via the middleware; this policy is enforced ADDITIONALLY inside
# the subscribe handler to deter list-flood / spam-signup abuse.
SUBSCRIBE_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="subscribe_ratelimit",
    limit=5,
    window_seconds=60,
    detail="Too many subscribe attempts. Try again in a minute.",
)
SENTRY_TUNNEL_RATE_LIMIT_POLICY = RateLimitPolicy(
    key_prefix="sentry_tunnel_ratelimit",
    limit=10,
    window_seconds=60,
    detail="Too many monitoring events. Try again later.",
)

EXEMPT_PATHS = {"/health", "/metrics", "/api/v1/ping"}


@lru_cache(maxsize=1)
def _rate_limit_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def client_ip_from_request(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        forwarded_ips = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        for raw_ip in reversed(forwarded_ips):
            try:
                parsed_ip = ip_address(raw_ip)
            except ValueError:
                continue
            if parsed_ip.is_global:
                return raw_ip
        for raw_ip in reversed(forwarded_ips):
            try:
                ip_address(raw_ip)
            except ValueError:
                continue
            return raw_ip

    if request.client is not None and request.client.host:
        return request.client.host

    return "unknown"


def resolve_rate_limit_policy(path: str, method: str) -> RateLimitPolicy | None:
    normalized_method = method.upper()
    normalized_path = path.rstrip("/") or "/"

    if normalized_method == "OPTIONS":
        return None

    if normalized_path in EXEMPT_PATHS:
        return None

    if normalized_path == "/api/v1/auth/login":
        return LOGIN_RATE_LIMIT_POLICY

    if (
        normalized_path.startswith("/api/v1/public/objects/")
        and normalized_path.endswith("/model/file")
    ) or (
        normalized_path.startswith("/api/v1/public/videos/")
        and normalized_path.endswith("/stream")
    ) or (
        normalized_path.startswith("/api/public/clips/")
        and (normalized_path.endswith("/stream") or normalized_path.endswith("/poster"))
    ) or (
        normalized_path.startswith("/api/public/objects/")
        and normalized_path.endswith("/poster")
    ):
        return PUBLIC_MEDIA_RATE_LIMIT_POLICY

    if normalized_path.startswith("/api/public/") or normalized_path.startswith("/api/v1/public/"):
        return PUBLIC_RATE_LIMIT_POLICY

    if normalized_path.startswith("/api/v1/"):
        return PRIVATE_V1_RATE_LIMIT_POLICY

    return None


def enforce_rate_limit(request: Request, policy: RateLimitPolicy) -> None:
    client_ip = client_ip_from_request(request)
    key = f"{policy.key_prefix}:{client_ip}"

    try:
        redis_conn = _rate_limit_redis()
        pipeline = redis_conn.pipeline()
        pipeline.incr(key)
        pipeline.ttl(key)
        attempt_count, ttl = pipeline.execute()

        if attempt_count == 1 or ttl < 0:
            redis_conn.expire(key, policy.window_seconds)
            ttl = policy.window_seconds

        if attempt_count > policy.limit:
            retry_after = str(max(int(ttl), 1))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=policy.detail,
                headers={"Retry-After": retry_after},
            )
    except RedisError as exc:
        logger.warning("Skipping %s for %s because Redis is unavailable: %s", policy.key_prefix, client_ip, exc)


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not getattr(settings, "rate_limit_enabled", True):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        policy = resolve_rate_limit_policy(scope.get("path", "/"), request.method)
        if policy is None:
            await self.app(scope, receive, send)
            return

        try:
            enforce_rate_limit(request, policy)
        except HTTPException as exc:
            response = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
