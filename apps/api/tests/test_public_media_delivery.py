"""HTTP contract for public model and normalized-video delivery.

These tests use real ASGI round trips so Starlette's GET, HEAD, Range, and
conditional response paths remain covered. Hostile Range syntax has a separate
global-middleware regression suite in ``test_public_media_range_guard.py``.
"""

from __future__ import annotations

import hashlib
import os
from email.utils import formatdate, parsedate_to_datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import public as public_endpoints
from app.core.rate_limit import PUBLIC_MEDIA_RATE_LIMIT_POLICY, resolve_rate_limit_policy
from app.services.object_model_variants import PublicVariantCandidate
from app.services.public_media_delivery import (
    LEGACY_MEDIA_CACHE_CONTROL,
    MediaIdentityUnavailableError,
    MediaValidators,
)

MODEL_BODY = b"glTF-binary-body-" + bytes(range(256)) * 8
VIDEO_BODY = b"mp4-body-" + bytes(range(256)) * 16
MODEL_SHA256 = hashlib.sha256(MODEL_BODY).hexdigest()
VIDEO_SHA256 = hashlib.sha256(VIDEO_BODY).hexdigest()


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _VideoSession:
    def __init__(self, video):
        self.video = video

    def execute(self, _statement):
        return _ScalarResult(self.video)


class _Endpoint(SimpleNamespace):
    @property
    def url(self) -> str:
        return f"{self.bare_url}?v={self.current_version()}"


def _registered_methods(path: str) -> set[str]:
    return {
        method
        for route in public_endpoints.router.routes
        if getattr(route, "path", "") == path
        for method in getattr(route, "methods", set())
    }


def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(public_endpoints.router)
    app.dependency_overrides[public_endpoints.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def model_endpoint(tmp_path, monkeypatch):
    """A published canonical-model route wired to a real file."""
    path = tmp_path / "demo.web-8192.glb"
    path.write_bytes(MODEL_BODY)
    model = SimpleNamespace(
        id=uuid4(),
        storage_path=str(path),
        sha256_checksum=MODEL_SHA256,
        file_size_bytes=len(MODEL_BODY),
        mime_type="model/gltf-binary",
        original_filename="DEMO-002UARK.glb",
    )
    monkeypatch.setattr(
        public_endpoints,
        "_public_object_or_404",
        lambda *_a, **_k: SimpleNamespace(website_object_id="test-object"),
    )
    monkeypatch.setattr(public_endpoints, "_public_model", lambda *_a, **_k: model)
    monkeypatch.setattr(
        public_endpoints,
        "active_delivery_profile_tokens",
        lambda *_a, **_k: frozenset(),
    )
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", False)
    monkeypatch.setattr(public_endpoints.settings, "semantic_env", "production")
    version_state = SimpleNamespace(value="model-state-v1")
    monkeypatch.setattr(
        public_endpoints,
        "model_cache_token",
        lambda *_a, **_k: version_state.value,
    )
    return _Endpoint(
        kind="model",
        bare_url=f"/public/objects/{uuid4()}/model/file",
        path=path,
        body=MODEL_BODY,
        digest=MODEL_SHA256,
        identity_row=model,
        version_state=version_state,
        current_version=lambda: version_state.value,
        client=_client(object()),
        media_type="model/gltf-binary",
    )


@pytest.fixture
def video_endpoint(tmp_path, monkeypatch):
    """A published video route wired to a normalized playback file."""
    video_id = uuid4()
    path = tmp_path / f"{video_id}.mp4"
    path.write_bytes(VIDEO_BODY)
    video = SimpleNamespace(
        id=video_id,
        original_filename="demo-vessel.mp4",
        playback_sha256_checksum=VIDEO_SHA256,
        playback_file_size_bytes=len(VIDEO_BODY),
    )
    monkeypatch.setattr(public_endpoints, "video_playback_path", lambda _video: path)
    monkeypatch.setattr(public_endpoints.settings, "semantic_env", "production")
    return _Endpoint(
        kind="video",
        bare_url=f"/public/videos/{video_id}/stream",
        path=path,
        body=VIDEO_BODY,
        digest=VIDEO_SHA256,
        identity_row=video,
        current_version=lambda: public_endpoints.video_cache_token(video),
        client=_client(_VideoSession(video)),
        media_type="video/mp4",
    )


@pytest.fixture(params=["model", "video"])
def media(request, model_endpoint, video_endpoint):
    """Both strong-identity endpoints satisfy the same HTTP contract."""
    return model_endpoint if request.param == "model" else video_endpoint


def _set_persisted_digest(media, digest: str) -> None:
    if media.kind == "model":
        media.identity_row.sha256_checksum = digest
        media.version_state.value = f"model-state-{digest[:12]}"
    else:
        media.identity_row.playback_sha256_checksum = digest


# --------------------------------------------------------------------------- #
# Methods, identity, and full representations
# --------------------------------------------------------------------------- #


def test_public_model_and_video_delivery_support_get_and_head():
    assert {"GET", "HEAD"}.issubset(_registered_methods("/public/objects/{object_id}/model/file"))
    assert {"GET", "HEAD"}.issubset(_registered_methods("/public/videos/{video_id}/stream"))


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/public/objects/demo/model/file",
        "/api/v1/public/videos/demo/stream",
    ],
)
def test_public_media_requests_stay_in_the_media_rate_limit_bucket(path, method):
    assert resolve_rate_limit_policy(path, method) == PUBLIC_MEDIA_RATE_LIMIT_POLICY


def test_full_get_returns_strong_persisted_identity_and_complete_headers(media):
    response = media.client.get(media.url)

    assert response.status_code == 200
    assert response.content == media.body
    assert response.headers["content-length"] == str(len(media.body))
    assert response.headers["content-type"] == media.media_type
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["etag"] == f'"sha256:{media.digest}"'
    assert parsedate_to_datetime(response.headers["last-modified"]) is not None


def test_head_returns_the_same_representation_headers_and_no_body(media):
    get_response = media.client.get(media.url)
    head_response = media.client.head(media.url)

    assert head_response.status_code == 200
    assert head_response.content == b""
    for header in (
        "content-length",
        "content-type",
        "cache-control",
        "etag",
        "last-modified",
        "accept-ranges",
        "content-disposition",
    ):
        assert head_response.headers[header] == get_response.headers[header], header


def test_media_validators_use_the_supplied_digest_and_one_stat_result(media):
    validators = MediaValidators(
        media.path,
        content_sha256=media.digest,
        expected_size_bytes=len(media.body),
    )

    assert validators.stat_result.st_size == len(media.body)
    assert validators.etag == f'"sha256:{media.digest}"'
    assert validators.is_strong is True
    assert parsedate_to_datetime(validators.last_modified) is not None


@pytest.mark.parametrize("digest", [None, "", "f" * 63, "g" * 64])
def test_strong_delivery_rejects_missing_or_invalid_persisted_digests(tmp_path, digest):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"asset")

    with pytest.raises(MediaIdentityUnavailableError):
        MediaValidators(path, content_sha256=digest)


# --------------------------------------------------------------------------- #
# Byte ranges and If-Range
# --------------------------------------------------------------------------- #


def test_first_byte_range_returns_206(media):
    response = media.client.get(media.url, headers={"Range": "bytes=0-0"})

    assert response.status_code == 206
    assert response.content == media.body[:1]
    assert response.headers["content-range"] == f"bytes 0-0/{len(media.body)}"
    assert response.headers["content-length"] == "1"


def test_middle_suffix_and_open_ended_ranges_return_exact_bytes(media):
    size = len(media.body)
    cases = (
        ("bytes=100-199", media.body[100:200], f"bytes 100-199/{size}"),
        ("bytes=-64", media.body[-64:], f"bytes {size - 64}-{size - 1}/{size}"),
        (f"bytes={size - 10}-", media.body[-10:], f"bytes {size - 10}-{size - 1}/{size}"),
    )

    for range_header, expected_body, expected_content_range in cases:
        response = media.client.get(media.url, headers={"Range": range_header})
        assert response.status_code == 206
        assert response.content == expected_body
        assert response.headers["content-range"] == expected_content_range


def test_range_past_end_is_clamped_and_unsatisfiable_range_returns_416(media):
    size = len(media.body)
    clamped = media.client.get(media.url, headers={"Range": f"bytes=0-{size + 1000}"})
    unsatisfiable = media.client.get(
        media.url, headers={"Range": f"bytes={size + 10}-{size + 20}"}
    )

    assert clamped.status_code == 206
    assert clamped.content == media.body
    assert clamped.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{size}"
    assert unsatisfiable.headers["cache-control"] == "no-store"


def test_head_with_range_reports_partial_headers_and_no_body(media):
    response = media.client.head(media.url, headers={"Range": "bytes=0-99"})

    assert response.status_code == 206
    assert response.content == b""
    assert response.headers["content-range"] == f"bytes 0-99/{len(media.body)}"
    assert response.headers["content-length"] == "100"


def test_matching_strong_if_range_serves_the_requested_partial_body(media):
    etag = media.client.get(media.url).headers["etag"]
    response = media.client.get(
        media.url, headers={"Range": "bytes=0-9", "If-Range": etag}
    )

    assert response.status_code == 206
    assert response.content == media.body[:10]


def test_if_range_date_safely_returns_the_complete_200(media):
    last_modified = media.client.get(media.url).headers["last-modified"]
    response = media.client.get(
        media.url, headers={"Range": "bytes=0-9", "If-Range": last_modified}
    )

    assert response.status_code == 200
    assert response.content == media.body
    assert "content-range" not in response.headers


@pytest.mark.parametrize("if_range", ['"stale-etag"', 'W/"sha256:placeholder"'])
def test_stale_or_weak_if_range_returns_the_complete_200(media, if_range):
    if if_range.startswith("W/"):
        if_range = f'W/{media.client.get(media.url).headers["etag"]}'
    response = media.client.get(
        media.url, headers={"Range": "bytes=0-9", "If-Range": if_range}
    )

    assert response.status_code == 200
    assert response.content == media.body
    assert "content-range" not in response.headers


# --------------------------------------------------------------------------- #
# Conditional requests
# --------------------------------------------------------------------------- #


def test_matching_if_none_match_returns_304_with_representation_headers(media):
    etag = media.client.get(media.url).headers["etag"]
    response = media.client.get(media.url, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["accept-ranges"] == "bytes"
    assert "last-modified" in response.headers


@pytest.mark.parametrize("template", ["{etag}", "W/{etag}", '"other", {etag}', "*"])
def test_if_none_match_uses_weak_comparison_and_accepts_lists(media, template):
    etag = media.client.get(media.url).headers["etag"]
    header = template.format(etag=etag)

    assert media.client.get(media.url, headers={"If-None-Match": header}).status_code == 304


def test_nonmatching_if_none_match_returns_the_complete_body(media):
    response = media.client.get(media.url, headers={"If-None-Match": '"stale-etag"'})

    assert response.status_code == 200
    assert response.content == media.body


def test_if_modified_since_is_informational_and_never_authorizes_304(media):
    last_modified = media.client.get(media.url).headers["last-modified"]
    matching = media.client.get(media.url, headers={"If-Modified-Since": last_modified})
    older = media.client.get(
        media.url, headers={"If-Modified-Since": formatdate(0, usegmt=True)}
    )

    assert matching.status_code == 200
    assert matching.content == media.body
    assert older.status_code == 200
    assert older.content == media.body


@pytest.mark.parametrize(
    "header_value",
    ["not-a-date", "", "Thu, 02 Apr 2099 17:09:40", "9999999999999"],
)
def test_malformed_if_modified_since_is_ignored(media, header_value):
    response = media.client.get(media.url, headers={"If-Modified-Since": header_value})

    assert response.status_code == 200
    assert response.content == media.body


def test_if_none_match_takes_precedence_over_if_modified_since(media):
    last_modified = media.client.get(media.url).headers["last-modified"]
    response = media.client.get(
        media.url,
        headers={"If-None-Match": '"stale-etag"', "If-Modified-Since": last_modified},
    )

    assert response.status_code == 200
    assert response.content == media.body


# --------------------------------------------------------------------------- #
# Persisted identity, replacement, and legacy video behavior
# --------------------------------------------------------------------------- #


def test_same_size_replacement_changes_etag_when_persisted_digest_changes(media):
    before = media.client.get(media.url).headers["etag"]
    replacement = bytes(len(media.body))
    replacement_digest = hashlib.sha256(replacement).hexdigest()

    media.path.write_bytes(replacement)
    _set_persisted_digest(media, replacement_digest)

    after = media.client.get(media.url).headers["etag"]
    assert after == f'"sha256:{replacement_digest}"'
    assert after != before


def test_same_second_replacement_cannot_return_false_304_from_http_date(media):
    fixed_second = 1_900_000_000
    initial_stat = media.path.stat()
    os.utime(
        media.path,
        ns=(initial_stat.st_atime_ns, fixed_second * 1_000_000_000 + 100),
    )
    first = media.client.get(media.url)
    old_last_modified = first.headers["last-modified"]

    replacement = bytes(len(media.body))
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    media.path.write_bytes(replacement)
    replacement_stat = media.path.stat()
    os.utime(
        media.path,
        ns=(replacement_stat.st_atime_ns, fixed_second * 1_000_000_000 + 900),
    )
    _set_persisted_digest(media, replacement_digest)

    response = media.client.get(
        media.url,
        headers={"If-Modified-Since": old_last_modified},
    )

    assert response.status_code == 200
    assert response.content == replacement
    assert response.headers["etag"] == f'"sha256:{replacement_digest}"'
    assert response.headers["last-modified"] == old_last_modified


def test_model_size_mismatch_fails_closed_with_generic_503(model_endpoint):
    model_endpoint.identity_row.file_size_bytes += 1
    response = model_endpoint.client.get(model_endpoint.url)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Public media representation is temporarily unavailable."}
    assert str(model_endpoint.path) not in response.text


@pytest.mark.parametrize(
    ("digest", "size"),
    [
        (VIDEO_SHA256, len(VIDEO_BODY) + 1),
        (VIDEO_SHA256, None),
        (None, len(VIDEO_BODY)),
        ("invalid", len(VIDEO_BODY)),
    ],
)
def test_partial_invalid_or_mismatched_video_identity_fails_closed(
    video_endpoint, digest, size
):
    video_endpoint.identity_row.playback_sha256_checksum = digest
    video_endpoint.identity_row.playback_file_size_bytes = size

    response = video_endpoint.client.get(video_endpoint.url)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Public media representation is temporarily unavailable."}
    assert str(video_endpoint.path) not in response.text


def test_legacy_video_uses_labeled_weak_validator_and_no_store(video_endpoint):
    video_endpoint.identity_row.playback_sha256_checksum = None
    video_endpoint.identity_row.playback_file_size_bytes = None

    response = video_endpoint.client.get(video_endpoint.url)

    assert response.status_code == 200
    assert response.content == video_endpoint.body
    assert response.headers["etag"].startswith('W/"legacy-')
    assert response.headers["cache-control"] == LEGACY_MEDIA_CACHE_CONTROL


def test_legacy_video_keeps_plain_range_playback(video_endpoint):
    video_endpoint.identity_row.playback_sha256_checksum = None
    video_endpoint.identity_row.playback_file_size_bytes = None

    response = video_endpoint.client.get(
        video_endpoint.url, headers={"Range": "bytes=0-9"}
    )

    assert response.status_code == 206
    assert response.content == video_endpoint.body[:10]


def test_legacy_video_if_range_always_returns_the_complete_200(video_endpoint):
    video_endpoint.identity_row.playback_sha256_checksum = None
    video_endpoint.identity_row.playback_file_size_bytes = None
    first = video_endpoint.client.get(video_endpoint.url)

    for if_range in (first.headers["etag"], first.headers["last-modified"]):
        response = video_endpoint.client.get(
            video_endpoint.url,
            headers={"Range": "bytes=0-9", "If-Range": if_range},
        )
        assert response.status_code == 200
        assert response.content == video_endpoint.body
        assert "content-range" not in response.headers


def test_legacy_video_can_answer_explicit_revalidation_with_304(video_endpoint):
    video_endpoint.identity_row.playback_sha256_checksum = None
    video_endpoint.identity_row.playback_file_size_bytes = None
    etag = video_endpoint.client.get(video_endpoint.url).headers["etag"]

    response = video_endpoint.client.get(
        video_endpoint.url, headers={"If-None-Match": etag}
    )

    assert response.status_code == 304
    assert response.headers["cache-control"] == LEGACY_MEDIA_CACHE_CONTROL


# --------------------------------------------------------------------------- #
# Variant selection and generic required-tier recovery
# --------------------------------------------------------------------------- #


def _approved_variant(path, digest: str) -> PublicVariantCandidate:
    return PublicVariantCandidate(
        variant_key="web-8192",
        storage_path=str(path),
        approval_status="approved",
        visual_qa_status="passed",
        device_qa_status="passed",
        is_public_selectable=True,
        source_canonical_sha256=MODEL_SHA256,
        variant_sha256=digest,
        file_size_bytes=path.stat().st_size,
        created_by="author",
        approved_by="independent-approver",
    )


def test_selected_variant_uses_its_exact_persisted_sha_etag(
    model_endpoint, tmp_path, monkeypatch
):
    variant_body = b"approved-web-8192" * 32
    variant_digest = hashlib.sha256(variant_body).hexdigest()
    variant_path = tmp_path / "demo.web-8192.approved.glb"
    variant_path.write_bytes(variant_body)
    candidate = _approved_variant(variant_path, variant_digest)
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", True)
    monkeypatch.setattr(public_endpoints.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(
        public_endpoints,
        "lookup_public_variant_candidate",
        lambda *_a, **_k: candidate,
    )
    monkeypatch.setattr(
        public_endpoints,
        "active_delivery_profile_tokens",
        lambda *_a, **_k: frozenset({"web-8192", "mobile-4096"}),
    )

    response = model_endpoint.client.get(
        f"{model_endpoint.url}&variant=web-8192&variant_required=true"
    )

    assert response.status_code == 200
    assert response.content == variant_body
    assert response.headers["etag"] == f'"sha256:{variant_digest}"'
    assert model_endpoint.identity_row.sha256_checksum not in response.headers["etag"]


@pytest.mark.parametrize("requested", ["web-8192", "mobile-4096"])
def test_required_variant_failure_is_generic_and_recoverable(
    model_endpoint, monkeypatch, requested
):
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", True)
    monkeypatch.setattr(
        public_endpoints,
        "lookup_public_variant_candidate",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        public_endpoints,
        "active_delivery_profile_tokens",
        lambda *_a, **_k: frozenset({"web-8192", "mobile-4096"}),
    )

    response = model_endpoint.client.get(
        f"{model_endpoint.url}&variant={requested}&variant_required=true"
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "detail": "Requested model representation is temporarily unavailable."
    }
    assert str(model_endpoint.path) not in response.text
    if requested:
        assert requested not in response.text
    assert response.content != model_endpoint.body


@pytest.mark.parametrize("requested", ["", "../../../etc/passwd", "mobile-2048"])
def test_exact_flag_for_unadvertised_token_fails_closed(
    model_endpoint, monkeypatch, requested
):
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", True)
    monkeypatch.setattr(
        public_endpoints,
        "lookup_public_variant_candidate",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        public_endpoints,
        "active_delivery_profile_tokens",
        lambda *_a, **_k: frozenset({"web-8192", "mobile-4096"}),
    )

    response = model_endpoint.client.get(
        f"{model_endpoint.url}&variant={requested}&variant_required=true"
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "detail": "Requested model representation is temporarily unavailable."
    }
    assert str(model_endpoint.path) not in response.text
    if requested:
        assert requested not in response.text
    assert response.content != model_endpoint.body


def test_exact_flag_fails_closed_while_variant_switch_is_disabled(
    model_endpoint, monkeypatch
):
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", False)
    lookup_called = False

    def _lookup(*_args, **_kwargs):
        nonlocal lookup_called
        lookup_called = True
        return None

    monkeypatch.setattr(public_endpoints, "lookup_public_variant_candidate", _lookup)

    response = model_endpoint.client.get(
        f"{model_endpoint.url}&variant=web-8192&variant_required=true"
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "detail": "Requested model representation is temporarily unavailable."
    }
    assert response.content != model_endpoint.body
    assert lookup_called is False


def test_optional_unavailable_variant_preserves_canonical_fallback(
    model_endpoint, monkeypatch
):
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", True)
    monkeypatch.setattr(
        public_endpoints,
        "lookup_public_variant_candidate",
        lambda *_a, **_k: None,
    )

    response = model_endpoint.client.get(f"{model_endpoint.url}&variant=web-8192")

    assert response.status_code == 200
    assert response.content == model_endpoint.body
    assert response.headers["etag"] == f'"sha256:{MODEL_SHA256}"'


# --------------------------------------------------------------------------- #
# URL identity, cache policy, CORS, disposition, and query confidentiality
# --------------------------------------------------------------------------- #


def test_public_video_url_contains_the_playback_identity_token(video_endpoint):
    first = public_endpoints._public_video_url(video_endpoint.identity_row)
    video_endpoint.identity_row.playback_sha256_checksum = "b" * 64
    replacement = public_endpoints._public_video_url(video_endpoint.identity_row)

    assert first.startswith(f"/api/v1/public/videos/{video_endpoint.identity_row.id}/stream?v=")
    assert replacement != first
    assert "legacy" not in first
    assert str(video_endpoint.path) not in first


def test_public_model_url_encodes_timezone_aware_state_tokens(monkeypatch):
    token = "3-2026-08-11T12:30:00+00:00-deadbeef"
    object_id = uuid4()
    monkeypatch.setattr(
        public_endpoints,
        "model_cache_token",
        lambda *_a, **_k: token,
    )

    url = public_endpoints._public_model_url(object(), object_id, SimpleNamespace())

    assert url == (
        f"/api/v1/public/objects/{object_id}/model/file?"
        "v=3-2026-08-11T12%3A30%3A00%2B00%3A00-deadbeef"
    )


def test_legacy_public_video_url_is_explicitly_versioned(video_endpoint):
    video_endpoint.identity_row.playback_sha256_checksum = None

    assert public_endpoints._public_video_url(video_endpoint.identity_row).endswith("?v=legacy")


def test_stale_model_url_is_rejected_after_kill_switch_or_revocation_state_change(
    model_endpoint,
):
    previously_current_url = model_endpoint.url
    model_endpoint.version_state.value = "model-state-after-revocation"

    stale = model_endpoint.client.get(previously_current_url)
    current = model_endpoint.client.get(model_endpoint.url)

    assert stale.status_code == 409
    assert stale.headers["cache-control"] == "no-store"
    assert stale.json() == {"detail": "Public media URL is no longer current."}
    assert stale.content != model_endpoint.body
    assert current.status_code == 200
    assert current.content == model_endpoint.body


def test_model_state_change_during_resolution_rejects_the_previously_current_url(
    model_endpoint, monkeypatch
):
    previously_current_url = model_endpoint.url
    tokens = iter(("model-state-v1", "model-state-after-revocation"))
    monkeypatch.setattr(
        public_endpoints,
        "model_cache_token",
        lambda *_a, **_k: next(tokens),
    )

    response = model_endpoint.client.get(previously_current_url)

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Public media URL is no longer current."}
    assert response.content != model_endpoint.body


def test_stale_video_url_is_rejected_after_same_size_replacement(video_endpoint):
    previously_current_url = video_endpoint.url
    replacement = bytes(len(video_endpoint.body))
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    video_endpoint.path.write_bytes(replacement)
    video_endpoint.identity_row.playback_sha256_checksum = replacement_digest

    stale = video_endpoint.client.get(previously_current_url)
    current = video_endpoint.client.get(video_endpoint.url)

    assert stale.status_code == 409
    assert stale.headers["cache-control"] == "no-store"
    assert stale.json() == {"detail": "Public media URL is no longer current."}
    assert stale.content != replacement
    assert current.status_code == 200
    assert current.content == replacement
    assert current.headers["etag"] == f'"sha256:{replacement_digest}"'


@pytest.mark.parametrize("kind", ["model", "video"])
def test_arbitrary_or_duplicate_version_tokens_fail_closed(
    kind, model_endpoint, video_endpoint
):
    endpoint = model_endpoint if kind == "model" else video_endpoint
    current = endpoint.current_version()

    for query in (
        "?v=arbitrary",
        f"?v={current}&v={current}",
        f"?v={current}&v=stale",
        "?v=",
    ):
        response = endpoint.client.get(f"{endpoint.bare_url}{query}")
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"detail": "Public media URL is no longer current."}
        assert str(endpoint.path) not in response.text


@pytest.mark.parametrize("kind", ["model", "video"])
def test_stale_versioned_head_fails_closed_without_a_body(
    kind, model_endpoint, video_endpoint
):
    endpoint = model_endpoint if kind == "model" else video_endpoint

    response = endpoint.client.head(f"{endpoint.bare_url}?v=stale")

    assert response.status_code == 409
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_unversioned_legacy_direct_links_remain_readable_with_no_store(media):
    response = media.client.get(media.bare_url)

    assert response.status_code == 200
    assert response.content == media.body
    assert response.headers["cache-control"] == LEGACY_MEDIA_CACHE_CONTROL
    assert response.headers["etag"] == f'"sha256:{media.digest}"'


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("production", "public, max-age=3600"),
        ("staging", "public, max-age=60"),
        ("development", "no-store"),
        ("test", "no-store"),
    ],
)
def test_strong_cache_control_follows_environment(media, monkeypatch, environment, expected):
    monkeypatch.setattr(public_endpoints.settings, "semantic_env", environment)

    assert media.client.get(media.url).headers["cache-control"] == expected
    assert media.client.head(media.url).headers["cache-control"] == expected


def test_cors_exposes_public_media_response_headers():
    from app.main import CORS_EXPOSE_HEADERS, app
    from starlette.middleware.cors import CORSMiddleware

    cors = next(middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware)
    expected = {
        "ETag",
        "Last-Modified",
        "Accept-Ranges",
        "Content-Range",
        "Content-Disposition",
    }
    assert expected.issubset(set(CORS_EXPOSE_HEADERS))
    assert expected.issubset(set(cors.kwargs["expose_headers"]))

    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    exposed = {
        value.strip()
        for value in response.headers["access-control-expose-headers"].split(",")
    }
    assert expected.issubset(exposed)


def test_ascii_filename_uses_a_quoted_inline_disposition(model_endpoint):
    disposition = model_endpoint.client.get(model_endpoint.url).headers["content-disposition"]

    assert disposition == 'inline; filename="DEMO-002UARK.glb"'


def test_non_ascii_filename_is_encoded_and_header_safe(tmp_path, monkeypatch):
    path = tmp_path / "model.glb"
    path.write_bytes(MODEL_BODY)
    model = SimpleNamespace(
        id=uuid4(),
        storage_path=str(path),
        sha256_checksum=MODEL_SHA256,
        file_size_bytes=len(MODEL_BODY),
        mime_type="model/gltf-binary",
        original_filename="Demo Vessel — médecine.glb",
    )
    monkeypatch.setattr(
        public_endpoints,
        "_public_object_or_404",
        lambda *_a, **_k: SimpleNamespace(website_object_id="test-object"),
    )
    monkeypatch.setattr(public_endpoints, "_public_model", lambda *_a, **_k: model)
    monkeypatch.setattr(public_endpoints.settings, "public_model_variants_enabled", False)
    monkeypatch.setattr(public_endpoints.settings, "semantic_env", "production")

    response = _client(object()).get(f"/public/objects/{uuid4()}/model/file")
    disposition = response.headers["content-disposition"]

    assert disposition.startswith("inline;")
    assert "filename*=utf-8''" in disposition
    assert "—" not in disposition
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?variant=web-8192",
        "?variant=WEB-8192",
        "?variant=",
        "?variant=ios-low-memory",
        "?variant=mobile-9999",
        "?variant=../../../etc/passwd",
        "?variant=%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
        "?variant=web-8192%0d%0aX-Injected%3A%20yes",
        "?variant=web-8192&variant=mobile-2048",
        "?unknown=param",
    ],
)
def test_query_values_cannot_select_a_path_or_enter_headers(model_endpoint, query):
    response = model_endpoint.client.get(f"{model_endpoint.bare_url}{query}")

    assert response.status_code == 200
    assert response.content == model_endpoint.body
    assert "x-injected" not in {key.lower() for key in response.headers}
    for value in response.headers.values():
        assert "\r" not in value and "\n" not in value


def test_response_headers_never_expose_storage_paths(media):
    response = media.client.get(media.url)

    joined = " ".join(f"{key}: {value}" for key, value in response.headers.items())
    assert str(media.path.parent) not in joined
    assert "/var/lib/semantic" not in joined
