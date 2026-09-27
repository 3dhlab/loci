import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.monitoring import configure_sentry
from app.core.public_media_range_guard import PublicMediaRangeGuardMiddleware
from app.core.rate_limit import RateLimitMiddleware
from app.services.ai import AIProviderError, warm_embedding_provider
from app.services.runtime_health import build_runtime_health_snapshot, build_runtime_metrics_snapshot

configure_sentry("api")

logger = logging.getLogger(__name__)
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Accept", "Authorization", "Content-Type", "Range", "X-Requested-With"]
CORS_EXPOSE_HEADERS = [
    "ETag",
    "Last-Modified",
    "Accept-Ranges",
    "Content-Range",
    "Content-Disposition",
]


async def _warm_embedding_provider_background() -> None:
    try:
        await asyncio.to_thread(warm_embedding_provider)
    except AIProviderError:
        logger.warning("Embedding warmup skipped because the provider was unavailable.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_shared_secrets()
    warmup_task = None
    if settings.embedding_warmup_enabled:
        warmup_task = asyncio.create_task(_warm_embedding_provider_background())

    yield

    if warmup_task is not None:
        warmup_task.cancel()

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.resolved_cors_origin_regex,
    allow_credentials=True,
    allow_methods=CORS_ALLOWED_METHODS,
    allow_headers=CORS_ALLOWED_HEADERS,
    expose_headers=CORS_EXPOSE_HEADERS,
)
# Registered last so the guard runs before CORS, rate limiting, routing,
# database dependencies, and byte-serving response setup.
app.add_middleware(PublicMediaRangeGuardMiddleware)
app.include_router(api_router)


@app.get("/health", tags=["system"])
def health() -> JSONResponse:
    snapshot = build_runtime_health_snapshot()
    status_code = 200 if snapshot["status"] == "ok" else 503
    return JSONResponse(status_code=status_code, content=snapshot)


@app.get("/metrics", tags=["system"])
def metrics() -> dict:
    return build_runtime_metrics_snapshot()
