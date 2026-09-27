from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Object, ObjectModel, ObjectModelAnnotation, Video, VideoStatus
from app.services.publication_manifest import PublicationManifestBuildInput, build_publication_manifest

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
ElementTree.register_namespace("", SITEMAP_NAMESPACE)


@dataclass(frozen=True)
class PublicSitemapEntry:
    path: str
    lastmod: datetime | None


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        return f"/{normalized}"
    return normalized


def _normalize_base_url(site_base_url: str) -> str:
    normalized = site_base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("site_base_url must not be empty")
    return normalized


def _normalize_lastmod(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_lastmod(value: datetime | None) -> str | None:
    normalized = _normalize_lastmod(value)
    if normalized is None:
        return None
    return normalized.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_objects(db: Session) -> list[Object]:
    return db.execute(
        select(Object)
        .where(Object.is_published.is_(True))
        .order_by(Object.name.asc())
    ).scalars().all()


def _public_object_evidence_urls(db: Session, objects: list[Object]) -> dict[str, str | None]:
    if not objects:
        return {}

    object_ids = [obj.id for obj in objects]
    models_by_object_id = {
        model.object_id: model
        for model in db.execute(
            select(ObjectModel).where(
                ObjectModel.object_id.in_(object_ids),
                ObjectModel.is_published.is_(True),
            )
        ).scalars().all()
    }

    videos_by_object_id = {obj.id: [] for obj in objects}
    for video in db.execute(
        select(Video).where(
            Video.object_id.in_(object_ids),
            Video.object_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
        )
    ).scalars().all():
        if video.object_id is not None:
            videos_by_object_id.setdefault(video.object_id, []).append(video)

    annotations_by_object_id = {obj.id: [] for obj in objects}
    for annotation in db.execute(
        select(ObjectModelAnnotation).where(
            ObjectModelAnnotation.object_id.in_(object_ids),
            ObjectModelAnnotation.is_published.is_(True),
        )
    ).scalars().all():
        annotations_by_object_id.setdefault(annotation.object_id, []).append(annotation)

    evidence_urls: dict[str, str | None] = {}
    for obj in objects:
        manifest = build_publication_manifest(
            PublicationManifestBuildInput(
                object_row=obj,
                model=models_by_object_id.get(obj.id),
                videos=videos_by_object_id.get(obj.id, []),
                annotations=annotations_by_object_id.get(obj.id, []),
                clips=[],
            )
        )
        evidence_urls[str(obj.id)] = manifest.object_entry.evidence_url if manifest is not None else None

    return evidence_urls


def build_public_sitemap_entries(db: Session) -> list[PublicSitemapEntry]:
    objects = _public_objects(db)
    evidence_urls = _public_object_evidence_urls(db, objects)

    latest_object_update = max(
        (_normalize_lastmod(obj.updated_at or obj.created_at) for obj in objects),
        default=None,
    )

    entries_by_path: dict[str, PublicSitemapEntry] = {
        "/public": PublicSitemapEntry(path="/public", lastmod=latest_object_update)
    }

    for obj in objects:
        evidence_url = evidence_urls.get(str(obj.id))
        if not evidence_url:
            continue

        normalized_path = _normalize_path(evidence_url)
        next_entry = PublicSitemapEntry(
            path=normalized_path,
            lastmod=_normalize_lastmod(obj.updated_at or obj.created_at),
        )
        existing_entry = entries_by_path.get(normalized_path)
        if existing_entry is None:
            entries_by_path[normalized_path] = next_entry
            continue

        existing_lastmod = existing_entry.lastmod or datetime.min.replace(tzinfo=timezone.utc)
        next_lastmod = next_entry.lastmod or datetime.min.replace(tzinfo=timezone.utc)
        if next_lastmod >= existing_lastmod:
            entries_by_path[normalized_path] = next_entry

    public_entry = entries_by_path.pop("/public")
    return [public_entry, *[entries_by_path[path] for path in sorted(entries_by_path)]]


def render_public_sitemap_xml(entries: list[PublicSitemapEntry], *, site_base_url: str) -> str:
    normalized_base_url = _normalize_base_url(site_base_url)
    urlset = ElementTree.Element(f"{{{SITEMAP_NAMESPACE}}}urlset")

    for entry in entries:
        url = ElementTree.SubElement(urlset, f"{{{SITEMAP_NAMESPACE}}}url")
        loc = ElementTree.SubElement(url, f"{{{SITEMAP_NAMESPACE}}}loc")
        loc.text = f"{normalized_base_url}{_normalize_path(entry.path)}"

        formatted_lastmod = _format_lastmod(entry.lastmod)
        if formatted_lastmod:
            lastmod = ElementTree.SubElement(url, f"{{{SITEMAP_NAMESPACE}}}lastmod")
            lastmod.text = formatted_lastmod

    return ElementTree.tostring(urlset, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"