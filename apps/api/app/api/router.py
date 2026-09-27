from fastapi import APIRouter

from app.api.public.router import router as public_router
from app.api.v1.router import router as v1_router

api_router = APIRouter(prefix="/api")
api_router.include_router(v1_router, prefix="/v1")
api_router.include_router(public_router)


@api_router.get("/v1/ping", tags=["system"])
def ping() -> dict[str, str]:
    return {"message": "pong"}
