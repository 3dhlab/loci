from __future__ import annotations

from datetime import datetime, timezone
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from urllib.parse import urlsplit, urlunsplit

from app.api.v1.endpoints import public
from app.db.base import Base
from app.db.session import get_db
from app.models.entities import Object, Project, User


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result(_Rows):
    def scalar_one(self):
        return self._rows

    def scalars(self):
        return self


class _PagedDb:
    def __init__(self, objects):
        self.objects = objects
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result(len(self.objects))
        offset = statement._offset_clause.value if statement._offset_clause is not None else 0
        limit = statement._limit_clause.value if statement._limit_clause is not None else None
        return _Result(self.objects[offset:offset + limit])


def _object(name: str, *, published: bool = True):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(), project_id=uuid4(), name=name, description=None,
        metadata_json={}, website_object_id=name.lower().replace(" ", "-"),
        external_url=None, is_published=published, created_at=now, updated_at=now,
    )


def _client(db):
    app = FastAPI()
    app.include_router(public.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.mark.parametrize("total", [0, 1, 3, 4, 6, 7])
def test_public_object_pages_are_bounded_and_clamp_past_last(monkeypatch, total):
    objects = [_object(f"Object {i}") for i in range(total)]
    db = _PagedDb(objects)
    materialized_sizes = []

    def evidence(_db, page_objects):
        materialized_sizes.append(len(page_objects))
        assert len(page_objects) <= 3
        return {obj.id: None for obj in page_objects}

    monkeypatch.setattr(public, "_public_object_evidence_urls", evidence)
    monkeypatch.setattr(public, "_public_object_poster_urls", lambda _db, rows: {obj.id: None for obj in rows})

    with _client(db) as client:
        response = client.get("/api/v1/public/objects/page?page=3")

    assert response.status_code == 200, response.text
    body = response.json()
    pages = (total + 2) // 3
    assert body["total"] == total
    assert body["total_pages"] == pages
    assert body["page"] == (min(3, pages) if pages else 1)
    assert body["page_size"] == 3
    assert len(body["items"]) <= 3
    assert len(db.statements) == 2
    if db.statements[1]._limit_clause is not None:
        assert db.statements[1]._limit_clause.value == 3
    assert materialized_sizes == [len(body["items"])]


def test_public_object_page_filters_project_and_preserves_pending_published_rows(monkeypatch):
    project_id = uuid4()
    pending = _object("Pending published object")
    pending.project_id = project_id
    db = _PagedDb([pending])
    monkeypatch.setattr(public, "_public_object_evidence_urls", lambda _db, rows: {obj.id: None for obj in rows})
    monkeypatch.setattr(public, "_public_object_poster_urls", lambda _db, rows: {obj.id: None for obj in rows})

    with _client(db) as client:
        response = client.get(f"/api/v1/public/objects/page?project_id={project_id}")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(pending.id)]
    assert response.json()["items"][0]["evidence_url"] is None
    assert "project_id" in str(db.statements[1])


@pytest.mark.parametrize("query", ["page=0", "page=100001", "page_size=2", "page_size=4", "project_id=invalid"])
def test_public_object_page_rejects_invalid_query_values(query):
    with _client(_PagedDb([])) as client:
        response = client.get(f"/api/v1/public/objects/page?{query}")
    assert response.status_code == 422


def _database_url_with_name(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def postgres_engine():
    """A small disposable PostgreSQL database, following test_smoke.py's pattern."""
    base_url = os.environ.get(
        "SEMANTIC_TEST_BASE_DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql+psycopg://semantic:semantic@postgres:5432/semantic"),
    )
    test_name = f"semantic_browse_page_{os.getpid()}"
    admin_name = os.environ.get("SEMANTIC_TEST_ADMIN_DB", "postgres")
    admin_url = _database_url_with_name(base_url, admin_name)
    test_url = _database_url_with_name(base_url, test_name)

    with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (test_name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{test_name}"')
        conn.execute(f'CREATE DATABASE "{test_name}"')

    engine = create_engine(test_url)
    try:
        Base.metadata.create_all(bind=engine, tables=[User.__table__, Project.__table__, Object.__table__])
        yield engine
    finally:
        engine.dispose()
        with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (test_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{test_name}"')


def test_public_object_page_orders_curated_slugs_filters_unpublished_and_is_deterministic(
    postgres_engine, monkeypatch
):
    owner_id = uuid4()
    owner = User(id=owner_id, email="pagination@example.test", password_hash="not-used")
    first_project_id = uuid4()
    other_project_id = uuid4()
    other_object_id = uuid4()
    first_project = Project(id=first_project_id, owner_id=owner_id, name="Primary", description=None)
    other_project = Project(id=other_project_id, owner_id=owner_id, name="Other", description=None)

    curated = [
        ("demo-vessel", "Demo Vessel"),
        ("demo-container", "Demo Container"),
        ("demo-bowl", "Demo Bowl"),
        ("demo-panel", "Demo Panel"),
        ("demo-wand", "Demo Wand"),
        ("demo-sculpture", "Demo Sculpture"),
    ]
    monkeypatch.setattr(public.settings, "public_object_priority_ids", ",".join(slug for slug, _ in curated))
    # Equal-name extra objects exercise the final UUID tie-breaker.
    tied_ids = [
        UUID("00000000-0000-4000-8000-000000000002"),
        UUID("00000000-0000-4000-8000-000000000001"),
    ]
    rows = [
        Object(project_id=first_project_id, name=name, website_object_id=slug,
               description=None, external_url=None, metadata_json={}, is_published=True)
        for slug, name in curated
    ]
    rows.extend(
        Object(
            id=tied_id, project_id=first_project_id, name="Unlisted tie",
            website_object_id=f"extra-{index}", description=None, external_url=None,
            metadata_json={}, is_published=True,
        )
        for index, tied_id in enumerate(tied_ids)
    )
    rows.append(
        Object(project_id=first_project_id, name="Hidden", website_object_id="hidden",
               description=None, external_url=None, metadata_json={}, is_published=False)
    )
    rows.append(
        Object(id=other_object_id, project_id=other_project_id, name="Other project", website_object_id="other-project",
               description=None, external_url=None, metadata_json={}, is_published=True)
    )
    with Session(postgres_engine) as database:
        database.add_all([owner, first_project, other_project, *rows])
        database.commit()
    serialized_sizes = []

    def capture_evidence(_db, page_objects):
        serialized_sizes.append(len(page_objects))
        assert len(page_objects) <= 3
        return {obj.id: None for obj in page_objects}

    monkeypatch.setattr(public, "_public_object_evidence_urls", capture_evidence)
    monkeypatch.setattr(
        public, "_public_object_poster_urls",
        lambda _db, page_objects: {obj.id: None for obj in page_objects},
    )
    app = FastAPI()
    app.include_router(public.router, prefix="/api/v1")

    def override_db():
        with Session(postgres_engine) as database:
            yield database

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        pages = [client.get(f"/api/v1/public/objects/page?page={page}") for page in (1, 2, 3)]
        filtered = client.get(
            f"/api/v1/public/objects/page?project_id={first_project_id}&page=1"
        )

    assert all(response.status_code == 200 for response in pages + [filtered])
    page_bodies = [response.json() for response in pages]
    assert [item["name"] for item in page_bodies[0]["items"]] == [name for _, name in curated[:3]]
    assert [item["name"] for item in page_bodies[1]["items"]] == [name for _, name in curated[3:]]
    assert [item["name"] for item in page_bodies[2]["items"]] == [
        "Other project", "Unlisted tie", "Unlisted tie"
    ]
    assert [item["id"] for item in page_bodies[2]["items"]] == [
        str(other_object_id), *[str(value) for value in sorted(tied_ids)]
    ]
    assert [body["total"] for body in page_bodies] == [9, 9, 9]
    assert [body["total_pages"] for body in page_bodies] == [3, 3, 3]
    assert all(len(body["items"]) <= 3 for body in page_bodies)
    assert filtered.json()["total"] == 8
    assert all(item["project_id"] == str(first_project_id) for item in filtered.json()["items"])
    assert "Hidden" not in [item["name"] for body in page_bodies for item in body["items"]]
    assert serialized_sizes == [3, 3, 3, 3]
