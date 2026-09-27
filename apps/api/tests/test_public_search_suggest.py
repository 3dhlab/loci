from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import public
from app.db.session import get_db


class FakeResult:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    def __init__(self, responses: list[FakeResult]):
        self._responses = responses

    def execute(self, statement):
        if not self._responses:
            raise AssertionError("Unexpected query execution")
        return self._responses.pop(0)


def _build_app(fake_db: FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(public.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: fake_db
    return app


def test_public_suggest_returns_ranked_results() -> None:
    fake_db = FakeSession(
        [
            FakeResult(
                rows=[
                    ("Spoon handle", "spoon handle", "transcript", 10, 0.7, 0.4),
                    ("Spoon", "spoon", "entity", 2, 0.4, 0.35),
                    ("Spoon handle", "spoon handle", "entity", 1, 0.1, 0.2),
                ]
            )
        ]
    )

    with TestClient(_build_app(fake_db)) as client:
        response = client.get("/api/v1/public/search/suggest?q=spo&limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "spo"
    assert [item["phrase_text"] for item in body["results"]] == ["Spoon handle", "Spoon"]
    assert body["results"][0]["score"] > body["results"][1]["score"]


def test_public_suggest_rejects_unknown_published_project() -> None:
    fake_db = FakeSession([FakeResult(scalar=None)])
    unknown_project_id = uuid4()

    with TestClient(_build_app(fake_db)) as client:
        response = client.get(f"/api/v1/public/search/suggest?q=spo&project_id={unknown_project_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Published project not found"


def test_public_suggest_short_prefix_returns_empty_without_querying_db() -> None:
    fake_db = FakeSession([])

    with TestClient(_build_app(fake_db)) as client:
        response = client.get("/api/v1/public/search/suggest?q=s")

    assert response.status_code == 200
    assert response.json() == {"query": "s", "results": []}