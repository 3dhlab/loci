from __future__ import annotations

import mimetypes
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import Object, ObjectModel
from app.schemas.embed import PublicObjectManifestV1
from app.services.embed_manifest import (
    asset_cache_control,
    build_public_object_manifest,
    manifest_cache_control,
    manifest_etag,
    PublicObjectManifestNotFoundError,
)
from app.services.public_object_ids import resolve_website_object_id_alias
from app.services.public_media_paths import resolve_public_media_file

router = APIRouter(prefix="/objects")


@router.get("/{website_object_id}", response_model=PublicObjectManifestV1, response_model_exclude_none=True)
def get_public_object_manifest(
    website_object_id: str,
    response: Response,
    db: Session = Depends(get_db),
) -> PublicObjectManifestV1:
    try:
        manifest = build_public_object_manifest(db, website_object_id)
    except PublicObjectManifestNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embed-ready object not found") from exc

    response.headers["Cache-Control"] = manifest_cache_control()
    etag = manifest_etag(manifest)
    if etag is not None:
        response.headers["ETag"] = etag
    return manifest


@router.get("/{website_object_id}/poster")
def get_public_object_poster(website_object_id: str, db: Session = Depends(get_db)) -> FileResponse:
    normalized_object_id = resolve_website_object_id_alias(website_object_id)
    object_row = db.execute(
        select(Object).where(
            Object.website_object_id == normalized_object_id,
            Object.is_published.is_(True),
        )
    ).scalar_one_or_none()
    if object_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published object not found")

    model = db.execute(
        select(ObjectModel).where(
            ObjectModel.object_id == object_row.id,
            ObjectModel.is_published.is_(True),
        )
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster asset not found")

    poster_path = resolve_public_media_file(model.public_poster_path)
    if poster_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poster asset not found")

    media_type, _ = mimetypes.guess_type(poster_path.name)
    return FileResponse(
        path=poster_path,
        filename=poster_path.name,
        content_disposition_type="inline",
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": asset_cache_control()},
    )
