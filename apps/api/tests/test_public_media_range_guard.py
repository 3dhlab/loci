"""Early Range security boundary and the July multipart incident regression."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import starlette
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.public_media_range_guard import (
    MAX_RANGE_HEADER_BYTES,
    MEDIA_RANGE_GUARD_ROUTE_TEMPLATES,
    PublicMediaRangeGuardMiddleware,
    media_range_guard_applies_to_path,
    range_headers_are_supported,
    single_byte_range_is_supported,
)

FILE_BODY = b"0123456789"
TEST_MEDIA_PATH = "/api/v1/public/objects/00000000-0000-0000-0000-000000000001/model/file"
MEDIA_ROUTE_SAMPLES = (
    "/api/public/objects/public-id/poster",
    "/api/public/clips/public-clip/stream",
    "/api/public/clips/public-clip/poster",
    TEST_MEDIA_PATH,
    "/api/v1/public/videos/00000000-0000-0000-0000-000000000002/stream",
    "/api/v1/public/visual-windows/00000000-0000-0000-0000-000000000003/thumbnail",
    "/api/v1/objects/00000000-0000-0000-0000-000000000004/model/file",
    "/api/v1/videos/00000000-0000-0000-0000-000000000005/stream",
    "/api/v1/clips/00000000-0000-0000-0000-000000000006/download",
    "/api/v1/clips/00000000-0000-0000-0000-000000000007/transcript",
    "/api/v1/search/visual-windows/00000000-0000-0000-0000-000000000008/thumbnail",
    "/api/v1/search/visual-windows/00000000-0000-0000-0000-000000000009/frames/0",
)


def _file_app(path, *, guarded: bool) -> tuple[FastAPI, Callable[[], int]]:
    app = FastAPI()
    call_count = 0

    @app.api_route(TEST_MEDIA_PATH, methods=["GET", "HEAD"])
    def asset() -> FileResponse:
        nonlocal call_count
        call_count += 1
        return FileResponse(path, media_type="application/octet-stream")

    if guarded:
        app.add_middleware(PublicMediaRangeGuardMiddleware)
    return app, lambda: call_count


@pytest.mark.parametrize(
    "raw_value",
    [
        b"bytes=0-0",
        b"bytes=0-999",
        b"bytes=2-",
        b"bytes=-2",
        b"bytes=-0",
        b"BYTES=0-0",
        b"\tbytes=0-0 ",
        b"bytes=9999999999999999999-",
    ],
)
def test_valid_single_byte_ranges_pass_the_constant_cost_parser(raw_value: bytes) -> None:
    assert single_byte_range_is_supported(raw_value)


@pytest.mark.parametrize(
    "raw_value",
    [
        b"",
        b"items=0-1",
        b"octets=0-1",
        b"bytes=",
        b"bytes=-",
        b"bytes=,",
        b"bytes=abc-def",
        b"bytes =0-1",
        b"bytes= 0-1",
        b"bytes=0 -1",
        b"bytes=0- 1",
        b"bytes=0-1,2-3",
        b"bytes=0-1,",
        b"bytes=,0-1",
        b"bytes=10-5",
        b"bytes=0-1-2",
        b"bytes=10000000000000000000-",
        b"bytes=0-10000000000000000000",
        b" " * (MAX_RANGE_HEADER_BYTES + 1),
    ],
)
def test_unsafe_range_shapes_fail_the_constant_cost_parser(raw_value: bytes) -> None:
    assert not single_byte_range_is_supported(raw_value)


def test_duplicate_range_fields_are_rejected_before_header_combining() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [
            (b"range", b"bytes=0-0"),
            (b"range", b"bytes=2-2"),
        ],
    }

    assert not range_headers_are_supported(scope)


@pytest.mark.parametrize("path", MEDIA_ROUTE_SAMPLES)
def test_explicit_route_inventory_covers_each_byte_serving_path(path: str) -> None:
    assert media_range_guard_applies_to_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/v1/ping",
        "/api/v1/public/objects",
        "/api/v1/public/evidence/objects/public-id",
        "/api/v1/auth/me",
        "/api/v1/authoring/batches",
        f"{TEST_MEDIA_PATH}/",
        f"{TEST_MEDIA_PATH}/extra",
        "/api//v1/public/objects/id/model/file",
    ],
)
def test_route_scope_excludes_json_auth_health_and_noncanonical_paths(path: str) -> None:
    assert not media_range_guard_applies_to_path(path)


def test_route_inventory_templates_are_real_application_get_routes() -> None:
    from app.main import app

    application_paths = set(app.openapi()["paths"])
    assert set(MEDIA_RANGE_GUARD_ROUTE_TEMPLATES).issubset(application_paths)


@pytest.mark.parametrize("path", MEDIA_ROUTE_SAMPLES)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_actual_application_routes_reject_incident_shape_before_dependencies(
    path: str, method: str
) -> None:
    from app.main import app

    client = TestClient(app)
    try:
        response = client.request(method, path, headers={"Range": "bytes=0-0,2-2"})
    finally:
        client.close()

    assert response.status_code == 400
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["cache-control"] == "no-store"


def test_actual_application_json_route_preserves_existing_range_behavior() -> None:
    from app.main import app

    client = TestClient(app)
    try:
        response = client.get("/api/v1/ping", headers={"Range": "bytes=0-0,2-2"})
    finally:
        client.close()

    assert response.status_code == 200
    assert response.json() == {"message": "pong"}


def test_integrated_application_registers_explicit_media_head_policy() -> None:
    from app.main import app

    routes = list(app.routes)
    visited: set[int] = set()
    discovered = []
    while routes:
        route = routes.pop()
        if id(route) in visited:
            continue
        visited.add(id(route))
        discovered.append(route)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.extend(original_router.routes)
        routes.extend(getattr(route, "routes", []))

    media_routes = [
        route
        for route in discovered
        if getattr(route, "path", None) == "/public/objects/{object_id}/model/file"
    ]
    assert any("GET" in route.methods for route in media_routes)
    assert any("HEAD" in route.methods for route in media_routes)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "range_value",
    [
        "items=0-1",
        "bytes=abc-def",
        "bytes=",
        "bytes=-",
        "bytes=10-5",
        "bytes=0-0,2-2",
        f"bytes={'9' * 20}-",
        f"bytes={'9' * 5000}-",
        "bytes=" + ",".join(f"{index}-{index}" for index in range(1000)),
    ],
)
def test_guard_rejects_unsafe_get_and_head_before_file_response(
    tmp_path, method: str, range_value: str
) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, endpoint_calls = _file_app(path, guarded=True)

    with TestClient(app) as client:
        response = client.request(method, TEST_MEDIA_PATH, headers={"Range": range_value})

    assert response.status_code == 400
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert endpoint_calls() == 0


def test_guard_rejects_duplicate_range_fields_before_file_response(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, endpoint_calls = _file_app(path, guarded=True)

    with TestClient(app) as client:
        response = client.get(
            TEST_MEDIA_PATH,
            headers=[("Range", "bytes=0-0"), ("Range", "bytes=2-2")],
        )

    assert response.status_code == 400
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert endpoint_calls() == 0


@pytest.mark.parametrize(
    ("range_value", "expected_status", "expected_body"),
    [
        ("bytes=0-0", 206, b"0"),
        ("bytes=-2", 206, b"89"),
        ("bytes=2-", 206, b"23456789"),
        ("bytes=0-999", 206, FILE_BODY),
        ("bytes=99-100", 416, b""),
    ],
)
def test_guard_preserves_valid_and_unsatisfiable_single_range_behavior(
    tmp_path, range_value: str, expected_status: int, expected_body: bytes
) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, endpoint_calls = _file_app(path, guarded=True)

    with TestClient(app) as client:
        response = client.get(TEST_MEDIA_PATH, headers={"Range": range_value})

    assert response.status_code == expected_status
    assert response.content == expected_body
    assert endpoint_calls() == 1


def test_guard_preserves_head_range_and_if_range(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, endpoint_calls = _file_app(path, guarded=True)

    with TestClient(app) as client:
        etag = client.get(TEST_MEDIA_PATH).headers["etag"]
        matching = client.get(
            TEST_MEDIA_PATH,
            headers={"Range": "bytes=0-0", "If-Range": etag},
        )
        stale = client.get(
            TEST_MEDIA_PATH,
            headers={"Range": "bytes=0-0", "If-Range": '"stale"'},
        )
        head = client.head(TEST_MEDIA_PATH, headers={"Range": "bytes=0-0"})

    assert matching.status_code == 206
    assert matching.content == b"0"
    assert stale.status_code == 200
    assert stale.content == FILE_BODY
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-length"] == "1"
    assert endpoint_calls() == 4


def test_incident_shape_is_bodyless_and_length_consistent_at_the_guard(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, endpoint_calls = _file_app(path, guarded=True)

    with TestClient(app) as client:
        response = client.get(TEST_MEDIA_PATH, headers={"Range": "bytes=0-0,2-2"})

    assert response.status_code == 400
    assert len(response.content) == int(response.headers["content-length"]) == 0
    assert endpoint_calls() == 0


def test_bare_pinned_file_response_multipart_body_matches_content_length(tmp_path) -> None:
    """The durable dependency fix also resolves the upstream incident shape."""
    assert starlette.__version__ == "1.6.0"
    path = tmp_path / "asset.bin"
    path.write_bytes(FILE_BODY)
    app, _endpoint_calls = _file_app(path, guarded=False)

    with TestClient(app) as client:
        response = client.get(TEST_MEDIA_PATH, headers={"Range": "bytes=0-0,2-2"})

    assert response.status_code == 206
    assert response.headers["content-type"].startswith("multipart/byteranges;")
    assert len(response.content) == int(response.headers["content-length"])


def test_pinned_starlette_rejects_host_path_poisoning() -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/public/objects",
            "raw_path": b"/api/v1/public/objects",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"trusted.example/hidden?path=")],
            "server": ("fallback.example", 443),
            "client": ("127.0.0.1", 12345),
        }
    )

    assert request.url.path == "/api/v1/public/objects"
    assert request.url.hostname == "fallback.example"


def test_pinned_starlette_keeps_unrooted_path_out_of_url_authority() -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "@attacker.example",
            "raw_path": b"@attacker.example",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"trusted.example")],
            "server": ("trusted.example", 443),
            "client": ("127.0.0.1", 12345),
        }
    )

    assert request.url.hostname == "trusted.example"
    assert request.url.path == "/@attacker.example"
