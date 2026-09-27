from fastapi import APIRouter

from app.api.public.endpoints import clips
from app.api.public.endpoints import objects

router = APIRouter(prefix="/public", tags=["public-embed"])
router.include_router(clips.router)
router.include_router(objects.router)