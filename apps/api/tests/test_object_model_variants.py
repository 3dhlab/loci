import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import ObjectModelDeliveryProfile, ObjectModelVariant
from app.schemas.embed import PublicObjectManifestV1
from app.schemas.public import PublicEvidenceModelResponse, PublicObjectModelResponse
from app.services.object_model_variants import (
    ALLOWED_VARIANT_KEYS,
    IOS_LOW_MEMORY_MODEL_VARIANT,
    PublicVariantCandidate,
    is_allowed_variant_key,
    is_blocked_variant,
    lookup_public_delivery_capability,
    lookup_public_variant_candidate,
    normalize_variant_key,
    resolve_object_model_variant_path,
    resolve_public_model_path,
    resolve_public_model_representation,
)

MEDIA_ROOT = "/var/lib/semantic/media"
CANONICAL_PATH = "/var/lib/semantic/media/object-models/obj/model.glb"
VARIANT_PATH = "/var/lib/semantic/media/object-models/obj/model.mobile-2048.glb"
CANONICAL_SHA = "a" * 64
VARIANT_SIZE = 4_530_000
AUTHOR_ID = "11111111-1111-1111-1111-111111111111"
APPROVER_ID = "22222222-2222-2222-2222-222222222222"


def _candidate(**overrides) -> PublicVariantCandidate:
    base = dict(
        variant_key="mobile-2048",
        storage_path=VARIANT_PATH,
        approval_status="approved",
        visual_qa_status="passed",
        device_qa_status="passed",
        is_public_selectable=True,
        source_canonical_sha256=CANONICAL_SHA,
        variant_sha256="c" * 64,
        file_size_bytes=VARIANT_SIZE,
        created_by=AUTHOR_ID,
        approved_by=APPROVER_ID,
    )
    base.update(overrides)
    return PublicVariantCandidate(**base)


def _resolve(
    requested="mobile-2048",
    candidate=None,
    variants_enabled=True,
    file_exists=lambda _p: True,
    file_size=lambda _p: VARIANT_SIZE,
    sha=CANONICAL_SHA,
    media_root=MEDIA_ROOT,
):
    return resolve_public_model_path(
        canonical_path=CANONICAL_PATH,
        canonical_sha256=sha,
        requested_variant=requested,
        variant_candidate=_candidate() if candidate is None else candidate,
        variants_enabled=variants_enabled,
        media_root=media_root,
        file_exists=file_exists,
        file_size=file_size,
    )


# --------------------------------------------------------------------------- #
# Existing guards (must stay green).
# --------------------------------------------------------------------------- #
def test_low_memory_variant_request_resolves_to_canonical_model(tmp_path):
    model_path = tmp_path / "model.glb"
    model_path.write_bytes(b"canonical")
    sidecar_path = tmp_path / "model.ios-low-memory.glb"
    sidecar_path.write_bytes(b"degraded")

    assert resolve_object_model_variant_path(str(model_path), IOS_LOW_MEMORY_MODEL_VARIANT) == model_path


def test_public_contracts_do_not_advertise_low_memory_model_urls():
    assert "low_memory_model_url" not in PublicEvidenceModelResponse.model_fields
    assert "lowMemoryModelSrc" not in PublicObjectManifestV1.model_fields


def test_public_schemas_expose_only_bounded_delivery_capabilities():
    for schema in (PublicEvidenceModelResponse, PublicObjectModelResponse, PublicObjectManifestV1):
        for field_name in schema.model_fields:
            lowered = field_name.lower()
            assert "low_memory" not in lowered
            assert "lowmemory" not in lowered
            assert "storage" not in lowered
            assert "path" not in lowered


# --------------------------------------------------------------------------- #
# Normalization / allowlist / permanent block helpers.
# --------------------------------------------------------------------------- #
def test_normalize_variant_key_is_case_and_whitespace_insensitive():
    assert normalize_variant_key("  IOS-Low-Memory ") == "ios-low-memory"
    assert normalize_variant_key("MOBILE-2048") == "mobile-2048"
    assert normalize_variant_key(None) == ""


def test_blocklist_and_allowlist_membership():
    for spelling in ("ios-low-memory", "  IOS-LOW-MEMORY ", "Ios-Low-Memory"):
        assert is_blocked_variant(spelling)
        assert not is_allowed_variant_key(spelling)
    assert ALLOWED_VARIANT_KEYS == {"mobile-1024", "mobile-2048", "mobile-4096", "web-8192"}
    assert is_allowed_variant_key("mobile-1024")
    assert is_allowed_variant_key("web-8192")
    assert not is_allowed_variant_key("mobile-9999")


# --------------------------------------------------------------------------- #
# Pure resolver — default-deny matrix (all fall back to canonical).
# --------------------------------------------------------------------------- #
def test_no_variant_requested_serves_canonical():
    assert _resolve(requested=None) == Path(CANONICAL_PATH)
    assert _resolve(requested="") == Path(CANONICAL_PATH)


def test_ios_low_memory_serves_canonical_even_when_enabled_and_candidate_present():
    # Even if a (hypothetical) candidate is supplied, the poisoned token is blocked.
    poisoned = _candidate(variant_key="ios-low-memory")
    for spelling in ("ios-low-memory", "  IOS-Low-Memory ", "IOS-LOW-MEMORY"):
        assert _resolve(requested=spelling, candidate=poisoned, variants_enabled=True) == Path(CANONICAL_PATH)


def test_flag_off_serves_canonical():
    assert _resolve(variants_enabled=False) == Path(CANONICAL_PATH)


def test_unknown_variant_key_serves_canonical():
    assert _resolve(requested="mobile-9999") == Path(CANONICAL_PATH)
    assert _resolve(requested="webp") == Path(CANONICAL_PATH)


def test_missing_candidate_row_serves_canonical():
    assert resolve_public_model_path(
        canonical_path=CANONICAL_PATH,
        canonical_sha256=CANONICAL_SHA,
        requested_variant="mobile-2048",
        variant_candidate=None,
        variants_enabled=True,
    ) == Path(CANONICAL_PATH)


def test_unapproved_serves_canonical():
    assert _resolve(candidate=_candidate(approval_status="submitted")) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(approval_status="revoked")) == Path(CANONICAL_PATH)


def test_non_selectable_serves_canonical():
    assert _resolve(candidate=_candidate(is_public_selectable=False)) == Path(CANONICAL_PATH)


def test_failed_qa_serves_canonical():
    assert _resolve(candidate=_candidate(visual_qa_status="pending")) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(device_qa_status="failed")) == Path(CANONICAL_PATH)


def test_stale_source_sha_serves_canonical():
    assert _resolve(candidate=_candidate(source_canonical_sha256="b" * 64)) == Path(CANONICAL_PATH)
    # also stale if canonical has no recorded sha
    assert _resolve(sha="") == Path(CANONICAL_PATH)
    assert _resolve(
        sha="not-a-content-digest",
        candidate=_candidate(source_canonical_sha256="not-a-content-digest"),
    ) == Path(CANONICAL_PATH)


def test_missing_variant_file_serves_canonical():
    assert _resolve(file_exists=lambda _p: False) == Path(CANONICAL_PATH)


def test_missing_or_invalid_variant_digest_serves_canonical():
    assert _resolve(candidate=_candidate(variant_sha256="")) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(variant_sha256="z" * 64)) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(variant_sha256="c" * 63)) == Path(CANONICAL_PATH)


def test_missing_invalid_or_mismatched_persisted_size_serves_canonical():
    assert _resolve(candidate=_candidate(file_size_bytes=0)) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(file_size_bytes=-1)) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(file_size_bytes=None)) == Path(CANONICAL_PATH)
    assert _resolve(file_size=lambda _path: VARIANT_SIZE + 1) == Path(CANONICAL_PATH)
    assert _resolve(file_size=lambda _path: (_ for _ in ()).throw(OSError("stat failed"))) == Path(CANONICAL_PATH)


def test_candidate_key_mismatch_serves_canonical():
    assert _resolve(requested="mobile-2048", candidate=_candidate(variant_key="mobile-1024")) == Path(CANONICAL_PATH)


def test_fully_qualified_variant_is_served():
    assert _resolve() == Path(VARIANT_PATH)


def test_representation_identifies_variant_and_exact_persisted_digest():
    result = resolve_public_model_representation(
        canonical_path=CANONICAL_PATH,
        canonical_sha256=CANONICAL_SHA,
        requested_variant="mobile-2048",
        variant_candidate=_candidate(variant_sha256="d" * 64),
        variants_enabled=True,
        media_root=MEDIA_ROOT,
        file_exists=lambda _path: True,
        file_size=lambda _path: VARIANT_SIZE,
    )
    assert result.path == Path(VARIANT_PATH)
    assert result.content_sha256 == "d" * 64
    assert result.expected_size_bytes == VARIANT_SIZE
    assert result.is_variant is True
    assert result.required_variant_unavailable is False


@pytest.mark.parametrize(
    "candidate,variants_enabled,file_exists",
    [
        (None, True, lambda _path: True),
        (_candidate(approval_status="revoked"), True, lambda _path: True),
        (_candidate(visual_qa_status="failed"), True, lambda _path: True),
        (_candidate(source_canonical_sha256="b" * 64), True, lambda _path: True),
        (_candidate(storage_path="/etc/passwd"), True, lambda _path: True),
        (_candidate(variant_sha256=""), True, lambda _path: True),
        (_candidate(), True, lambda _path: False),
        (_candidate(), False, lambda _path: True),
    ],
)
def test_required_representation_fails_closed_without_inventory_detail(candidate, variants_enabled, file_exists):
    result = resolve_public_model_representation(
        canonical_path=CANONICAL_PATH,
        canonical_sha256=CANONICAL_SHA,
        requested_variant="mobile-2048",
        variant_candidate=candidate,
        variants_enabled=variants_enabled,
        variant_required=True,
        media_root=MEDIA_ROOT,
        file_exists=file_exists,
        file_size=lambda _path: VARIANT_SIZE,
    )
    assert result.path == Path(CANONICAL_PATH)
    assert result.is_variant is False
    assert result.required_variant_unavailable is True


def test_ordinary_variant_failure_preserves_canonical_fallback_identity():
    result = resolve_public_model_representation(
        canonical_path=CANONICAL_PATH,
        canonical_sha256=CANONICAL_SHA,
        requested_variant="mobile-2048",
        variant_candidate=None,
        variants_enabled=True,
        variant_required=False,
        media_root=MEDIA_ROOT,
        canonical_size_bytes=37_052_012,
    )
    assert result.path == Path(CANONICAL_PATH)
    assert result.content_sha256 == CANONICAL_SHA
    assert result.expected_size_bytes == 37_052_012
    assert result.is_variant is False
    assert result.required_variant_unavailable is False


def test_variant_path_comes_from_candidate_not_request_no_traversal():
    # A traversal token is not allowlisted -> canonical, and never used to build a path.
    traversal = "../../../../etc/passwd"
    assert _resolve(requested=traversal, candidate=None, variants_enabled=True) == Path(CANONICAL_PATH)
    # Even a valid request only ever returns the server-controlled candidate path.
    assert _resolve() == Path(VARIANT_PATH)
    assert ".." not in str(_resolve())


# --------------------------------------------------------------------------- #
# Four-eyes fail-closed in the resolver (defense in depth beside the DB CHECK).
# --------------------------------------------------------------------------- #
def test_null_approver_serves_canonical():
    assert _resolve(candidate=_candidate(approved_by=None)) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(approved_by="")) == Path(CANONICAL_PATH)
    assert _resolve(candidate=_candidate(approved_by="   ")) == Path(CANONICAL_PATH)


def test_self_approval_serves_canonical():
    assert _resolve(candidate=_candidate(created_by=AUTHOR_ID, approved_by=AUTHOR_ID)) == Path(CANONICAL_PATH)


def test_distinct_present_approver_is_served():
    assert _resolve(candidate=_candidate(created_by=AUTHOR_ID, approved_by=APPROVER_ID)) == Path(VARIANT_PATH)


# --------------------------------------------------------------------------- #
# Media-root confinement: storage_path must resolve under media_root.
# --------------------------------------------------------------------------- #
def test_absolute_path_outside_media_root_serves_canonical():
    assert _resolve(candidate=_candidate(storage_path="/etc/passwd")) == Path(CANONICAL_PATH)


def test_traversal_storage_path_escaping_media_root_serves_canonical():
    escape = f"{MEDIA_ROOT}/object-models/obj/../../../../../../etc/passwd"
    assert _resolve(candidate=_candidate(storage_path=escape)) == Path(CANONICAL_PATH)


def test_sibling_prefix_path_is_not_treated_as_confined():
    # `/var/lib/semantic/media-evil/...` shares a string prefix but is NOT under
    # media_root; relative_to-based confinement must reject it.
    assert _resolve(candidate=_candidate(storage_path="/var/lib/semantic/media-evil/x.glb")) == Path(CANONICAL_PATH)


def test_missing_media_root_fails_closed_to_canonical():
    assert _resolve(media_root=None) == Path(CANONICAL_PATH)
    assert _resolve(media_root="") == Path(CANONICAL_PATH)


def test_confined_path_under_media_root_is_served():
    assert _resolve(candidate=_candidate(storage_path=VARIANT_PATH)) == Path(VARIANT_PATH)


# --------------------------------------------------------------------------- #
# DB-level CHECK constraints (run on in-memory SQLite; portable column types).
# --------------------------------------------------------------------------- #
def _variant_table_session():
    engine = create_engine("sqlite://")
    ObjectModelVariant.__table__.create(engine)
    ObjectModelDeliveryProfile.__table__.create(engine)
    return Session(engine)


def _row_kwargs(**overrides):
    now = datetime.now(timezone.utc)
    author = uuid.uuid4()
    base = dict(
        id=uuid.uuid4(),
        object_model_id=uuid.uuid4(),
        variant_key="mobile-2048",
        storage_path=VARIANT_PATH,
        original_filename="model.mobile-2048.glb",
        source_canonical_sha256=CANONICAL_SHA,
        source_revision_number=1,
        variant_sha256="c" * 64,
        file_size_bytes=4_530_000,
        max_texture_dimension_px=2048,
        decoded_texture_ram_bytes=16_777_216,
        triangle_count=200_000,
        vertex_count=600_000,
        generator_version="gltf-transform-4.4.0/webp-meshopt-v1",
        generator_recipe={"tool": "gltf-transform", "pipeline": ["dedup", "textureCompress", "meshopt"]},
        approval_status="draft",
        visual_qa_status="pending",
        device_qa_status="pending",
        is_public_selectable=False,
        created_by=author,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return base


def test_db_accepts_valid_allowlisted_variant_key():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(variant_key="mobile-1024")))
        s.commit()  # should not raise


def test_db_accepts_fidelity_first_web_variant_key():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(variant_key="web-8192")))
        s.commit()  # should not raise


def test_db_rejects_ios_low_memory_variant_key():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(variant_key="ios-low-memory")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_rejects_unknown_variant_key():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(variant_key="mobile-9999")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_rejects_selectable_without_approval():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(
            is_public_selectable=True, approval_status="submitted",
            visual_qa_status="passed", device_qa_status="passed",
        )))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_rejects_selectable_without_passed_qa():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(
            is_public_selectable=True, approval_status="approved",
            visual_qa_status="passed", device_qa_status="pending",
        )))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_accepts_approved_selectable_row():
    author = uuid.uuid4()
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(
            created_by=author, approved_by=uuid.uuid4(),
            approval_status="approved", visual_qa_status="passed", device_qa_status="passed",
            is_public_selectable=True,
        )))
        s.commit()  # should not raise


def test_db_rejects_four_eyes_self_approval():
    author = uuid.uuid4()
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(created_by=author, approved_by=author, approval_status="approved")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_rejects_approved_without_approver():
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(approval_status="approved", approved_by=None)))
        with pytest.raises(IntegrityError):
            s.commit()


def test_db_rejects_selectable_approved_with_null_approver():
    # The headline gap: approved + selectable + both QA passed, but no approver.
    with _variant_table_session() as s:
        s.add(ObjectModelVariant(**_row_kwargs(
            approval_status="approved", visual_qa_status="passed", device_qa_status="passed",
            is_public_selectable=True, approved_by=None,
        )))
        with pytest.raises(IntegrityError):
            s.commit()


# --------------------------------------------------------------------------- #
# DB lookup helper short-circuits blocked / non-allowlisted keys before query.
# --------------------------------------------------------------------------- #
def test_lookup_returns_none_for_blocked_or_unknown_without_db():
    # db is never touched for blocked / unknown keys, so a sentinel that explodes
    # on use proves the short-circuit.
    class Boom:
        def execute(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("DB should not be queried for blocked/unknown variant keys")

    assert lookup_public_variant_candidate(Boom(), uuid.uuid4(), "ios-low-memory") is None
    assert lookup_public_variant_candidate(Boom(), uuid.uuid4(), "mobile-9999") is None
    assert lookup_public_variant_candidate(Boom(), uuid.uuid4(), None) is None


def test_public_capability_is_data_driven_and_contains_only_fixed_tokens():
    model_id = uuid.uuid4()
    author = uuid.uuid4()
    approver = uuid.uuid4()
    with _variant_table_session() as db:
        db.add(ObjectModelDeliveryProfile(
            object_model_id=model_id,
            standard_variant_key="web-8192",
            constrained_variant_key="mobile-4096",
            is_active=True,
            created_by=author,
            activated_by=approver,
            activated_at=datetime.now(timezone.utc),
        ))
        for key in ("web-8192", "mobile-4096"):
            db.add(ObjectModelVariant(**_row_kwargs(
                object_model_id=model_id,
                variant_key=key,
                created_by=author,
                approved_by=approver,
                approval_status="approved",
                visual_qa_status="passed",
                device_qa_status="passed",
                is_public_selectable=True,
            )))
        db.commit()

        capability = lookup_public_delivery_capability(db, model_id, variants_enabled=True)
        assert capability is not None
        assert capability.public_payload() == {
            "exact_required": True,
            "tiers": [
                {"client_category": "standard", "variant": "web-8192"},
                {"client_category": "constrained", "variant": "mobile-4096"},
            ],
        }
        assert lookup_public_delivery_capability(db, model_id, variants_enabled=False) is None


def test_public_capability_disappears_when_profile_or_peer_is_inactive():
    model_id = uuid.uuid4()
    author = uuid.uuid4()
    with _variant_table_session() as db:
        profile = ObjectModelDeliveryProfile(
            object_model_id=model_id,
            standard_variant_key="web-8192",
            constrained_variant_key="mobile-4096",
            created_by=author,
        )
        db.add(profile)
        db.commit()
        assert lookup_public_delivery_capability(db, model_id, variants_enabled=True) is None
