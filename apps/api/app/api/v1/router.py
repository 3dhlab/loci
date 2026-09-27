from fastapi import APIRouter

from app.api.v1.endpoints import auth, clips, object_models, objects, ops, projects, public, search, sentry_tunnel, transcripts, videos
from app.api.v1.endpoints import authoring

router = APIRouter()
router.include_router(auth.router)
router.include_router(public.router)
router.include_router(authoring.router)
router.include_router(ops.router)
router.include_router(projects.router)
router.include_router(objects.router)
router.include_router(object_models.router)
router.include_router(videos.router)
router.include_router(transcripts.router)
router.include_router(search.router)
router.include_router(clips.router)
router.include_router(sentry_tunnel.router, prefix="/public")
