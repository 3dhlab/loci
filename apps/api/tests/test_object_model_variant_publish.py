import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.services.object_model_variant_publish as variant_publisher
from app.models.entities import AuditEvent, ObjectModelDeliveryProfile, ObjectModelVariant
from app.services.object_model_variant_publish import (
    CanonicalModelRef,
    VariantMetrics,
    VariantPublishError,
    activate_required_object_variant_set,
    configure_object_delivery_profile,
    deactivate_required_object_variant_set,
    publish_object_model_variant,
)
from app.services.object_model_variants import (
    lookup_public_variant_candidate,
    resolve_public_model_path,
    resolve_public_model_representation,
)

FIXED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metrics() -> VariantMetrics:
    return VariantMetrics(
        max_texture_dimension_px=1024,
        decoded_texture_ram_bytes=5_570_560,
        triangle_count=89_930,
        vertex_count=269_790,
        geometry_extensions=["EXT_meshopt_compression", "EXT_texture_webp", "KHR_mesh_quantization"],
        generator_version="gltf-transform-4.4.0/webp-meshopt-v1",
        generator_recipe={"tool": "gltf-transform", "tex": 1024},
        visual_qa_report_path="qa-harness/QA-REPORT.demo-panel.md",
        visual_qa_changed_px_ratio=0.0088,
        device_qa_notes="iPhone 15 / iOS 26.4.2 2026-06-30: accepted, sharp at zoom, no crash",
    )


class _Env:
    def __init__(self, tmp_path: Path):
        self.media_root = tmp_path / "media"
        self.artifact_root = tmp_path / "artifacts" / "candidates"
        self.artifact_root.mkdir(parents=True)
        # candidate (the optimized GLB)
        self.candidate = self.artifact_root / "demo-panel.mobile-1024.glb"
        self.candidate.write_bytes(b"OPTIMIZED-MOBILE-1024-GLB-BYTES" * 100)
        # canonical model on disk (under media_root) + its sha
        self.object_model_id = uuid.uuid4()
        canonical_file = self.media_root / "object-models" / str(self.object_model_id) / "model.glb"
        canonical_file.parent.mkdir(parents=True)
        canonical_file.write_bytes(b"CANONICAL-GLB-BYTES" * 1000)
        self.canonical = CanonicalModelRef(
            object_model_id=self.object_model_id,
            storage_path=str(canonical_file),
            sha256_checksum=_sha(canonical_file),
            revision_number=2,
        )
        self.created_by = str(uuid.uuid4())
        self.approved_by = str(uuid.uuid4())
        self.engine = create_engine("sqlite://")
        ObjectModelVariant.__table__.create(self.engine)
        ObjectModelDeliveryProfile.__table__.create(self.engine)
        AuditEvent.__table__.create(self.engine)

    def session(self) -> Session:
        return Session(self.engine)

    def publish(self, db, **overrides):
        kwargs = dict(
            canonical=self.canonical,
            variant_key="mobile-1024",
            candidate_path=str(self.candidate),
            media_root=str(self.media_root),
            artifact_root=str(self.artifact_root),
            created_by=self.created_by,
            approved_by=self.approved_by,
            metrics=_metrics(),
            expected_canonical_sha256=self.canonical.sha256_checksum,
            approve=True,
            selectable=True,
            dry_run=False,
            now=FIXED_NOW,
        )
        kwargs.update(overrides)
        canonical = kwargs["canonical"]
        if canonical.website_object_id and db.query(ObjectModelDeliveryProfile).count() == 0:
            db.add(ObjectModelDeliveryProfile(
                object_model_id=self.object_model_id,
                standard_variant_key="web-8192",
                constrained_variant_key="mobile-4096",
                created_by=uuid.UUID(self.approved_by),
            ))
            db.commit()
        return publish_object_model_variant(db, **kwargs)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_happy_path_dry_run_writes_nothing(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        result = env.publish(db, dry_run=True)
        assert result.dry_run is True
        assert result.wrote_file is False
        assert result.inserted_row is False
        # destination computed under media_root, but not created
        assert not Path(result.destination_path).exists()
        assert str(env.media_root) in result.destination_path
        # preview sha matches the candidate bytes
        assert result.variant_sha256 == _sha(env.candidate)
        assert db.query(ObjectModelVariant).count() == 0


def test_happy_path_write_into_temp_media_root(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        result = env.publish(db)
        assert result.wrote_file is True and result.inserted_row is True
        dest = Path(result.destination_path)
        # file copied under media_root at a server-computed immutable key
        assert dest.is_file()
        assert dest.parent == env.media_root / "object-models" / str(env.object_model_id) / "variants"
        assert dest.name.startswith(f"mobile-1024.{result.variant_sha256}.")
        assert dest.suffix == ".glb"
        # variant_sha256 computed from the bytes ACTUALLY copied
        assert result.variant_sha256 == _sha(dest) == _sha(env.candidate)
        assert result.file_size_bytes == dest.stat().st_size

        row = db.query(ObjectModelVariant).one()
        assert row.storage_path == str(dest)
        assert row.variant_sha256 == result.variant_sha256
        assert row.file_size_bytes == result.file_size_bytes
        assert row.variant_key == "mobile-1024"
        assert row.source_canonical_sha256 == env.canonical.sha256_checksum
        assert row.source_revision_number == 2
        assert row.approval_status == "approved"
        assert row.visual_qa_status == "passed"
        assert row.device_qa_status == "passed"
        assert row.is_public_selectable is True
        assert str(row.created_by) == env.created_by
        assert str(row.approved_by) == env.approved_by
        assert row.original_filename == "demo-panel.mobile-1024.glb"
        assert row.device_qa_notes and "2026-06-30" in row.device_qa_notes


def test_draft_when_not_approved_is_not_selectable(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        result = env.publish(db, approve=False, selectable=False)
        row = db.query(ObjectModelVariant).one()
        assert row.approval_status == "draft"
        assert row.visual_qa_status == "pending"
        assert row.is_public_selectable is False
        assert result.wrote_file is True


def test_immutable_publication_retries_eexist_without_touching_existing_artifact(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    original_link = variant_publisher.os.link
    collided_path: Path | None = None

    def collide_once(source, destination):
        nonlocal collided_path
        if collided_path is None:
            collided_path = Path(destination)
            collided_path.write_bytes(b"EXISTING-CONCURRENT-ARTIFACT")
            raise FileExistsError("simulated immutable-key collision")
        return original_link(source, destination)

    monkeypatch.setattr(variant_publisher.os, "link", collide_once)
    with env.session() as db:
        result = env.publish(db)

    assert collided_path is not None
    assert collided_path.read_bytes() == b"EXISTING-CONCURRENT-ARTIFACT"
    assert Path(result.destination_path) != collided_path
    assert Path(result.destination_path).read_bytes() == env.candidate.read_bytes()
    assert not list(collided_path.parent.glob("*.publishing"))


def test_ambiguous_commit_outcome_retains_immutable_artifact(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    with env.session() as db:
        real_commit = db.commit

        def commit_then_raise():
            real_commit()
            raise RuntimeError("simulated connection loss after commit")

        monkeypatch.setattr(db, "commit", commit_then_raise)
        with pytest.raises(RuntimeError, match="connection loss after commit"):
            env.publish(db)

        row = db.query(ObjectModelVariant).one()
        artifact = Path(row.storage_path)
        assert artifact.is_file()
        assert artifact.read_bytes() == env.candidate.read_bytes()
        assert row.variant_sha256 == _sha(artifact)


def test_failure_before_commit_cleans_unique_artifact(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    with env.session() as db:
        def fail_add(_row):
            raise RuntimeError("add failed")

        monkeypatch.setattr(db, "add", fail_add)
        with pytest.raises(RuntimeError, match="add failed"):
            env.publish(db)

    variants_dir = env.media_root / "object-models" / str(env.object_model_id) / "variants"
    assert not list(variants_dir.glob("*.glb"))
    assert not list(variants_dir.glob("*.publishing"))


def test_losing_concurrent_insert_keeps_winner_and_its_unique_orphan(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    with env.session() as winner_db:
        winner = env.publish(winner_db)
    winner_path = Path(winner.destination_path)

    class _HiddenExistingResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    with env.session() as losing_db:
        monkeypatch.setattr(losing_db, "execute", lambda *_args, **_kwargs: _HiddenExistingResult())
        with pytest.raises(IntegrityError):
            env.publish(losing_db)

    artifacts = sorted(winner_path.parent.glob("*.glb"))
    assert len(artifacts) == 2
    assert winner_path in artifacts
    assert winner_path.read_bytes() == env.candidate.read_bytes()
    assert len({path.name for path in artifacts}) == 2
    assert all(path.read_bytes() == env.candidate.read_bytes() for path in artifacts)
    assert not list(winner_path.parent.glob("*.publishing"))


# --------------------------------------------------------------------------- #
# Rejections (nothing written)
# --------------------------------------------------------------------------- #
def test_reject_path_traversal_candidate(tmp_path):
    env = _Env(tmp_path)
    traversal = str(env.artifact_root / ".." / ".." / ".." / "etc" / "passwd")
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="candidate"):
            env.publish(db, candidate_path=traversal)
        assert db.query(ObjectModelVariant).count() == 0


def test_reject_candidate_outside_artifact_root(tmp_path):
    env = _Env(tmp_path)
    outside = tmp_path / "outside.mobile-1024.glb"
    outside.write_bytes(b"x" * 10)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="candidate"):
            env.publish(db, candidate_path=str(outside))


def test_reject_blocked_ios_low_memory_key(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="allowlisted"):
            env.publish(db, variant_key="ios-low-memory")


def test_reject_unknown_variant_key(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="allowlisted"):
            env.publish(db, variant_key="mobile-9999")


def test_reject_approved_by_equals_created_by(tmp_path):
    env = _Env(tmp_path)
    same = str(uuid.uuid4())
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="four-eyes"):
            env.publish(db, created_by=same, approved_by=same)


def test_reject_missing_approver(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="approved_by is required"):
            env.publish(db, approved_by="")


def test_reject_stale_canonical_sha(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="stale canonical"):
            env.publish(db, expected_canonical_sha256="d" * 64)


def test_reject_invalid_recorded_canonical_digest(tmp_path):
    env = _Env(tmp_path)
    invalid = replace(env.canonical, storage_path=str(tmp_path / "absent.glb"), sha256_checksum="invalid")
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="valid recorded"):
            env.publish(db, canonical=invalid, expected_canonical_sha256="invalid")


def test_reject_canonical_bytes_drift_on_disk(tmp_path):
    env = _Env(tmp_path)
    # Recorded sha no longer matches the on-disk canonical bytes.
    bad_canonical = CanonicalModelRef(
        object_model_id=env.object_model_id,
        storage_path=env.canonical.storage_path,
        sha256_checksum="e" * 64,
        revision_number=2,
    )
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="bytes on disk"):
            env.publish(db, canonical=bad_canonical, expected_canonical_sha256="e" * 64)


def test_reject_selectable_without_approval(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="selectable requires"):
            env.publish(db, approve=False, selectable=True)
        assert db.query(ObjectModelVariant).count() == 0


def test_generic_profile_dry_run_and_mutations_are_audited(tmp_path):
    env = _Env(tmp_path)
    website_object_id = "new-collection-object"
    canonical = replace(env.canonical, website_object_id=website_object_id)
    actor_id = str(uuid.uuid4())
    with env.session() as db:
        preview = configure_object_delivery_profile(
            db,
            canonical=canonical,
            website_object_id=website_object_id,
            standard_variant_key="web-8192",
            constrained_variant_key="mobile-4096",
            actor_id=actor_id,
            dry_run=True,
        )
        assert preview.created is True
        assert db.query(ObjectModelDeliveryProfile).count() == 0
        assert db.query(AuditEvent).count() == 0

        configured = configure_object_delivery_profile(
            db,
            canonical=canonical,
            website_object_id=website_object_id,
            standard_variant_key="web-8192",
            constrained_variant_key="mobile-4096",
            actor_id=actor_id,
        )
        assert configured.created is True
        profile = db.query(ObjectModelDeliveryProfile).one()
        configured_audit = db.query(AuditEvent).one()
        assert configured_audit.event_type == "object_model_delivery_profile_configured"
        assert configured_audit.actor_json == {"user_id": actor_id}
        assert configured_audit.payload_json["profile_id"] == str(profile.id)

        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        activate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id=website_object_id,
            media_root=str(env.media_root),
            actor_id=actor_id,
        )
        assert db.query(AuditEvent).filter_by(
            event_type="object_model_delivery_profile_activated"
        ).count() == 1
        assert db.query(ObjectModelDeliveryProfile).one().is_active is True

        deactivate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id=website_object_id,
            actor_id=actor_id,
        )
        audits = db.query(AuditEvent).all()
        assert {audit.event_type for audit in audits} == {
            "object_model_delivery_profile_configured",
            "object_model_delivery_profile_activated",
            "object_model_delivery_profile_deactivated",
        }
        assert db.query(ObjectModelDeliveryProfile).one().is_active is False


def test_demo_required_tier_cannot_be_published_selectable_by_itself(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        with pytest.raises(VariantPublishError, match="activated together"):
            env.publish(db, canonical=canonical, variant_key="web-8192", selectable=True)
        assert db.query(ObjectModelVariant).count() == 0


def test_demo_required_tier_set_activates_atomically(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        assert all(row.is_public_selectable is False for row in db.query(ObjectModelVariant).all())

        result = activate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
            media_root=str(env.media_root),
        )

        assert result.activated_variant_keys == ("mobile-4096", "web-8192")
        assert result.dry_run is False
        assert result.changed_variant_keys == ("mobile-4096", "web-8192")
        assert all(row.is_public_selectable is True for row in db.query(ObjectModelVariant).all())
        assert lookup_public_variant_candidate(
            db,
            env.object_model_id,
            "web-8192",
            website_object_id="demo-container",
        ) is not None
        assert lookup_public_variant_candidate(
            db,
            env.object_model_id,
            "mobile-4096",
            website_object_id="demo-container",
        ) is not None

        candidate = lookup_public_variant_candidate(
            db,
            env.object_model_id,
            "web-8192",
            website_object_id="demo-container",
        )
        representation = resolve_public_model_representation(
            canonical_path=canonical.storage_path,
            canonical_sha256=canonical.sha256_checksum,
            requested_variant="web-8192",
            variant_candidate=candidate,
            variants_enabled=True,
            variant_required=True,
            media_root=str(env.media_root),
        )
        assert representation.is_variant is True
        assert representation.required_variant_unavailable is False


def test_demo_required_tier_set_dry_run_validates_without_activation(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)

        result = activate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
            media_root=str(env.media_root),
            dry_run=True,
        )

        assert result.dry_run is True
        assert result.changed_variant_keys == ("mobile-4096", "web-8192")
        assert all(row.is_public_selectable is False for row in db.query(ObjectModelVariant).all())


def test_demo_required_tier_set_deactivates_atomically_and_idempotently(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        activate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
            media_root=str(env.media_root),
        )

        result = deactivate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
        )

        assert result.dry_run is False
        assert result.deactivated_variant_keys == ("mobile-4096", "web-8192")
        assert result.changed_variant_keys == ("mobile-4096", "web-8192")
        assert all(row.is_public_selectable is False for row in db.query(ObjectModelVariant).all())

        repeated = deactivate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
        )
        assert repeated.changed_variant_keys == ()


def test_demo_required_tier_set_deactivation_dry_run_preserves_active_rows(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        activate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
            media_root=str(env.media_root),
        )

        result = deactivate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
            dry_run=True,
        )

        assert result.dry_run is True
        assert result.changed_variant_keys == ("mobile-4096", "web-8192")
        assert all(row.is_public_selectable is True for row in db.query(ObjectModelVariant).all())


def test_demo_required_tier_set_deactivation_containment_survives_missing_peer(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        row = db.query(ObjectModelVariant).one()
        row.is_public_selectable = True
        db.commit()

        result = deactivate_required_object_variant_set(
            db,
            canonical=canonical,
            website_object_id="demo-container",
        )

        assert result.deactivated_variant_keys == ("web-8192",)
        assert result.changed_variant_keys == ("web-8192",)
        assert db.query(ObjectModelVariant).one().is_public_selectable is False


def test_demo_required_tier_set_rejects_same_size_content_tamper(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        row = db.query(ObjectModelVariant).filter_by(variant_key="web-8192").one()
        variant_path = Path(row.storage_path)
        original_size = variant_path.stat().st_size
        variant_path.write_bytes(b"X" * original_size)

        with pytest.raises(VariantPublishError, match="content digest mismatch: web-8192"):
            activate_required_object_variant_set(
                db,
                canonical=canonical,
                website_object_id="demo-container",
                media_root=str(env.media_root),
            )

        assert all(row.is_public_selectable is False for row in db.query(ObjectModelVariant).all())


def test_demo_required_tier_set_rejects_size_mismatch(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
        row = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        variant_path = Path(row.storage_path)
        variant_path.write_bytes(variant_path.read_bytes() + b"X")

        with pytest.raises(VariantPublishError, match="size mismatch: mobile-4096"):
            activate_required_object_variant_set(
                db,
                canonical=canonical,
                website_object_id="demo-container",
                media_root=str(env.media_root),
            )

        assert all(row.is_public_selectable is False for row in db.query(ObjectModelVariant).all())


def _activated_demo_pair(env: _Env, db: Session) -> CanonicalModelRef:
    canonical = replace(env.canonical, website_object_id="demo-container")
    env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
    env.publish(db, canonical=canonical, variant_key="mobile-4096", selectable=False)
    activate_required_object_variant_set(
        db,
        canonical=canonical,
        website_object_id="demo-container",
        media_root=str(env.media_root),
    )
    return canonical


def _resolve_required_demo(
    env: _Env,
    db: Session,
    canonical: CanonicalModelRef,
    requested_variant: str = "web-8192",
):
    candidate = lookup_public_variant_candidate(
        db,
        env.object_model_id,
        requested_variant,
        website_object_id="demo-container",
    )
    assert candidate is not None
    return resolve_public_model_representation(
        canonical_path=canonical.storage_path,
        canonical_sha256=canonical.sha256_checksum,
        requested_variant=requested_variant,
        variant_candidate=candidate,
        variants_enabled=True,
        variant_required=True,
        media_root=str(env.media_root),
    )


@pytest.mark.parametrize(
    ("requested_variant", "deleted_peer"),
    [("web-8192", "mobile-4096"), ("mobile-4096", "web-8192")],
)
def test_demo_runtime_rejects_either_tier_when_required_peer_file_is_deleted(
    tmp_path,
    requested_variant,
    deleted_peer,
):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key=deleted_peer).one()
        Path(peer.storage_path).unlink()

        result = _resolve_required_demo(env, db, canonical, requested_variant)
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_runtime_rejects_web_tier_when_mobile_peer_size_drifts(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        peer_path = Path(peer.storage_path)
        peer_path.write_bytes(peer_path.read_bytes() + b"size-drift")

        result = _resolve_required_demo(env, db, canonical)
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_runtime_rejects_web_tier_when_mobile_peer_path_is_tampered(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        original_path = Path(peer.storage_path)
        outside_path = tmp_path / "outside-media-root.glb"
        outside_path.write_bytes(original_path.read_bytes())
        peer.storage_path = str(outside_path)
        db.commit()

        result = _resolve_required_demo(env, db, canonical)
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_runtime_rejects_web_tier_when_mobile_peer_digest_state_is_tampered(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        peer.variant_sha256 = "0" * 63
        db.commit()

        result = _resolve_required_demo(env, db, canonical)
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_runtime_rejects_web_tier_when_mobile_peer_is_not_regular_file(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        peer_path = Path(peer.storage_path)
        peer_path.unlink()
        peer_path.mkdir()

        result = _resolve_required_demo(env, db, canonical)
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_runtime_rejects_web_tier_when_mobile_peer_state_is_revoked(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        canonical = _activated_demo_pair(env, db)
        peer = db.query(ObjectModelVariant).filter_by(variant_key="mobile-4096").one()
        peer.is_public_selectable = False
        peer.approval_status = "revoked"
        db.commit()

        candidate = lookup_public_variant_candidate(
            db,
            env.object_model_id,
            "web-8192",
            website_object_id="demo-container",
        )
        assert candidate is None
        result = resolve_public_model_representation(
            canonical_path=canonical.storage_path,
            canonical_sha256=canonical.sha256_checksum,
            requested_variant="web-8192",
            variant_candidate=candidate,
            variants_enabled=True,
            variant_required=True,
            media_root=str(env.media_root),
        )
        assert result.is_variant is False
        assert result.required_variant_unavailable is True


def test_demo_required_tier_set_keeps_every_row_inactive_when_peer_is_missing(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)

        with pytest.raises(VariantPublishError, match="incomplete"):
            activate_required_object_variant_set(
                db,
                canonical=canonical,
                website_object_id="demo-container",
                media_root=str(env.media_root),
            )

        rows = db.query(ObjectModelVariant).all()
        assert len(rows) == 1
        assert rows[0].is_public_selectable is False


def test_demo_runtime_lookup_rejects_a_manually_enabled_single_tier(tmp_path):
    env = _Env(tmp_path)
    canonical = replace(env.canonical, website_object_id="demo-container")
    with env.session() as db:
        env.publish(db, canonical=canonical, variant_key="web-8192", selectable=False)
        row = db.query(ObjectModelVariant).one()
        row.is_public_selectable = True
        db.commit()

        assert lookup_public_variant_candidate(
            db,
            env.object_model_id,
            "web-8192",
            website_object_id="demo-container",
        ) is None


def test_reject_duplicate_variant(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        env.publish(db)
        with pytest.raises(VariantPublishError, match="already exists"):
            env.publish(db)


# --------------------------------------------------------------------------- #
# Read path integration: row + flag gate
# --------------------------------------------------------------------------- #
def test_resolver_serves_variant_after_publish_when_flag_enabled(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        result = env.publish(db)
        candidate = lookup_public_variant_candidate(db, env.object_model_id, "mobile-1024")
        assert candidate is not None
        assert candidate.variant_sha256 == result.variant_sha256
        assert candidate.file_size_bytes == result.file_size_bytes
        served = resolve_public_model_path(
            canonical_path=env.canonical.storage_path,
            canonical_sha256=env.canonical.sha256_checksum,
            requested_variant="mobile-1024",
            variant_candidate=candidate,
            variants_enabled=True,
            media_root=str(env.media_root),
        )
        assert served == Path(result.destination_path)


def test_resolver_falls_back_to_canonical_when_flag_disabled(tmp_path):
    env = _Env(tmp_path)
    with env.session() as db:
        env.publish(db)
        candidate = lookup_public_variant_candidate(db, env.object_model_id, "mobile-1024")
        served = resolve_public_model_path(
            canonical_path=env.canonical.storage_path,
            canonical_sha256=env.canonical.sha256_checksum,
            requested_variant="mobile-1024",
            variant_candidate=candidate,
            variants_enabled=False,
            media_root=str(env.media_root),
        )
        assert served == Path(env.canonical.storage_path)
