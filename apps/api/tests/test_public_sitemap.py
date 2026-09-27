from datetime import datetime, timezone
import sys
from types import SimpleNamespace
from uuid import uuid4
import xml.etree.ElementTree as ET
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.services.public_sitemap import PublicSitemapEntry, build_public_sitemap_entries, render_public_sitemap_xml


def test_build_public_sitemap_entries_includes_public_root_and_published_evidence_urls(monkeypatch) -> None:
    older = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 19, 3, 30, tzinfo=timezone.utc)
    first_id = uuid4()
    second_id = uuid4()
    objects = [
        SimpleNamespace(id=first_id, created_at=older, updated_at=older),
        SimpleNamespace(id=second_id, created_at=older, updated_at=newer),
    ]

    monkeypatch.setattr("app.services.public_sitemap._public_objects", lambda db: objects)
    monkeypatch.setattr(
        "app.services.public_sitemap._public_object_evidence_urls",
        lambda db, rows: {
            str(first_id): "/evidence/objects/demo-vessel",
            str(second_id): "/evidence/objects/demo-panel",
        },
    )

    entries = build_public_sitemap_entries(db=None)
    entries_by_path = {entry.path: entry for entry in entries}

    assert list(entries_by_path) == [
        "/public",
        "/evidence/objects/demo-panel",
        "/evidence/objects/demo-vessel",
    ]
    assert entries_by_path["/public"].lastmod == newer
    assert entries_by_path["/evidence/objects/demo-vessel"].lastmod == older
    assert entries_by_path["/evidence/objects/demo-panel"].lastmod == newer


def test_render_public_sitemap_xml_uses_absolute_urls_and_lastmod() -> None:
    xml_text = render_public_sitemap_xml(
        [
            PublicSitemapEntry(path="/public", lastmod=datetime(2026, 4, 19, 3, 30, tzinfo=timezone.utc)),
            PublicSitemapEntry(path="/evidence/objects/demo-vessel", lastmod=datetime(2026, 4, 18, 21, 45, tzinfo=timezone.utc)),
        ],
        site_base_url="https://viewer.example.test/",
    )

    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [
        (element.text or "").strip()
        for element in root.findall("sm:url/sm:loc", namespace)
        if (element.text or "").strip()
    ]
    lastmods = [
        (element.text or "").strip()
        for element in root.findall("sm:url/sm:lastmod", namespace)
        if (element.text or "").strip()
    ]

    assert locs == [
        "https://viewer.example.test/public",
        "https://viewer.example.test/evidence/objects/demo-vessel",
    ]
    assert lastmods == ["2026-04-19T03:30:00Z", "2026-04-18T21:45:00Z"]