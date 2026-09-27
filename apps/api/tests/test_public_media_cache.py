"""Cache identity for public model and normalized-video representations."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.entities import ObjectModelVariant
from app.services import public_media_cache
from app.services.public_media_cache import (
    canonical_model_revision,
    model_cache_token,
    model_variant_state_digest,
    normalized_sha256,
    video_cache_token,
)

MODEL_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
AUTHOR = uuid.UUID("22222222-2222-4222-8222-222222222222")
APPROVER = uuid.UUID("33333333-3333-4333-8333-333333333333")


class _FakeModel:
    def __init__(self, revision=1, updated_at=None, sha256_checksum="c" * 64, file_size_bytes=1024):
        self.id = MODEL_ID
        self.revision_number = revision
        self.updated_at = updated_at or datetime(2026, 5, 6, 14, 3, 29, 281218, tzinfo=timezone.utc)
        self.sha256_checksum = sha256_checksum
        self.file_size_bytes = file_size_bytes


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ObjectModelVariant.__table__.create(engine)
    with Session(engine) as value:
        yield value


def _variant(**overrides):
    row = {
        "object_model_id": MODEL_ID,
        "variant_key": "web-8192",
        "storage_path": "/srv/media/model-variants/demo.web-8192.glb",
        "original_filename": "demo.web-8192.glb",
        "source_canonical_sha256": "c" * 64,
        "source_revision_number": 1,
        "variant_sha256": "1eeb608b12233edc18181d78cdae2548920276e3c7c722ea012193c48329206f",
        "file_size_bytes": 7947608,
        "max_texture_dimension_px": 8192,
        "decoded_texture_ram_bytes": 357913941,
        "triangle_count": 200000,
        "vertex_count": 600000,
        "generator_version": "test",
        "generator_recipe": {},
        "visual_qa_status": "passed",
        "device_qa_status": "passed",
        "approval_status": "approved",
        "is_public_selectable": True,
        "created_by": AUTHOR,
        "approved_by": APPROVER,
    }
    row.update(overrides)
    return ObjectModelVariant(**row)


@pytest.fixture(autouse=True)
def variants_enabled(monkeypatch):
    monkeypatch.setattr(public_media_cache.settings, "public_model_variants_enabled", True)


def test_model_token_is_opaque_and_stable(session):
    model = _FakeModel()
    token = model_cache_token(session, model)
    assert len(token) == 64
    assert all(character in "0123456789abcdef" for character in token)
    assert canonical_model_revision(model) not in token
    assert token == model_cache_token(session, model)


def test_publication_replacement_revocation_and_kill_switch_change_model_token(session, monkeypatch):
    model = _FakeModel()
    initial = model_cache_token(session, model)
    row = _variant()
    session.add(row)
    session.flush()
    published = model_cache_token(session, model)
    assert published != initial

    row.variant_sha256 = "d" * 64
    session.flush()
    replaced = model_cache_token(session, model)
    assert replaced != published

    row.is_public_selectable = False
    session.flush()
    revoked = model_cache_token(session, model)
    assert revoked != replaced

    monkeypatch.setattr(public_media_cache.settings, "public_model_variants_enabled", False)
    assert model_cache_token(session, model) != revoked


@pytest.mark.parametrize(
    "replacement",
    [
        _FakeModel(sha256_checksum="d" * 64),
        _FakeModel(file_size_bytes=1025),
    ],
)
def test_canonical_digest_and_size_independently_change_model_token(session, replacement):
    assert model_cache_token(session, replacement) != model_cache_token(session, _FakeModel())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approval_status", "revoked"),
        ("visual_qa_status", "failed"),
        ("device_qa_status", "failed"),
        ("source_canonical_sha256", "e" * 64),
    ],
)
def test_every_selection_gate_changes_model_token(session, field, value):
    model = _FakeModel()
    row = _variant()
    session.add(row)
    session.flush()
    before = model_cache_token(session, model)
    row.is_public_selectable = False
    setattr(row, field, value)
    session.flush()
    assert model_cache_token(session, model) != before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_by", uuid.UUID("44444444-4444-4444-8444-444444444444")),
        ("approved_by", uuid.UUID("55555555-5555-4555-8555-555555555555")),
        ("storage_path", "/srv/media/model-variants/replacement.web-8192.glb"),
        ("file_size_bytes", 7_947_609),
        ("source_revision_number", 2),
    ],
)
def test_every_runtime_selection_or_body_field_independently_changes_model_token(
    session,
    field,
    value,
):
    model = _FakeModel()
    row = _variant()
    session.add(row)
    session.flush()
    before = model_cache_token(session, model)
    setattr(row, field, value)
    session.flush()
    assert model_cache_token(session, model) != before


def test_model_token_is_opaque_url_safe_and_bounded(session):
    session.add(_variant(variant_key="mobile-4096"))
    session.add(_variant())
    session.flush()
    digest = model_variant_state_digest(session, MODEL_ID)
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    for secret in ("web-8192", "mobile-4096", "media", "glb", "/"):
        assert secret not in digest
    token = model_cache_token(session, _FakeModel())
    assert len(token) == 64
    assert all(character in "0123456789abcdef" for character in token)


def test_model_token_ignores_another_models_rows(session):
    baseline = model_cache_token(session, _FakeModel())
    session.add(_variant(object_model_id=uuid.UUID("99999999-9999-4999-8999-999999999999")))
    session.flush()
    assert model_cache_token(session, _FakeModel()) == baseline


def test_video_token_uses_persisted_digest_and_changes_on_same_size_replacement():
    first = SimpleNamespace(playback_sha256_checksum="a" * 64, playback_file_size_bytes=64)
    replacement = SimpleNamespace(playback_sha256_checksum="b" * 64, playback_file_size_bytes=64)
    assert video_cache_token(first) != video_cache_token(replacement)
    assert video_cache_token(first) == video_cache_token(first)
    assert len(video_cache_token(first)) == 64


@pytest.mark.parametrize("value", [None, "", "g" * 64, "a" * 63, object()])
def test_legacy_or_invalid_video_digest_uses_explicit_legacy_identity(value):
    assert video_cache_token(SimpleNamespace(playback_sha256_checksum=value)) == "legacy"


def test_normalized_sha256_accepts_only_exact_hex():
    assert normalized_sha256(" A" + "B" * 63 + " ") == "a" + "b" * 63
    assert normalized_sha256("x" * 64) is None
