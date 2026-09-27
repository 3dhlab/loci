"""Internal operator command: publish ONE approved optimized object-model variant.

NOT a web endpoint. Run from a trusted shell with explicit arguments. The core
validation/copy/insert lives in
``app.services.object_model_variant_publish``; this wrapper only resolves the
canonical model from the DB and forwards explicit operator arguments.

Example (Demo Panel mobile-1024, dry run first):

    python -m app.scripts.publish_object_model_variant \
      --website-object-id demo-panel \
      --variant-key mobile-1024 \
      --candidate examples/model-optimization/candidates/demo-panel.mobile-1024.glb \
      --created-by <uploader-user-uuid> --approved-by <approver-user-uuid> \
      --expected-canonical-sha256 <sha-the-candidate-was-built-from> \
      --max-texture-dimension-px 1024 --decoded-texture-ram-bytes 5570560 \
      --triangle-count 89930 --vertex-count 269790 \
      --geometry-extensions EXT_meshopt_compression,EXT_texture_webp,KHR_mesh_quantization \
      --generator-version gltf-transform-4.4.0/webp-meshopt-v1 \
      --visual-qa-report-path examples/model-optimization/qa-harness/QA-REPORT.demo-panel.md \
      --visual-qa-changed-px-ratio 0.0088 \
      --approve --dry-run

Drop --dry-run to write into media_root + insert the row. Add --selectable (only
with --approve) to make it public-selectable. This command does NOT enable
public_model_variants_enabled and does NOT deploy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import Object, ObjectModel
from app.services.object_model_variant_publish import (
    CanonicalModelRef,
    VariantMetrics,
    VariantPublishError,
    publish_object_model_variant,
)


def _default_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "apps").is_dir() and (parent / "debug").is_dir():
            return parent
        if (parent / "app").is_dir() and (parent / "alembic").is_dir():
            return parent
    return Path.cwd()


def _default_artifact_root() -> Path:
    return _default_repo_root() / "debug" / "repro_artifacts" / "2026-06-27-model-optimization" / "candidates"


def _resolve_canonical(db, *, object_model_id: str | None, website_object_id: str | None) -> CanonicalModelRef:
    model: ObjectModel | None = None
    if object_model_id:
        model = db.get(ObjectModel, object_model_id)
    elif website_object_id:
        obj = db.execute(
            select(Object).where(Object.website_object_id == website_object_id)
        ).scalar_one_or_none()
        if obj is not None:
            model = db.execute(
                select(ObjectModel).where(ObjectModel.object_id == obj.id)
            ).scalar_one_or_none()
    if model is None:
        raise VariantPublishError("canonical object model not found for the given identifier")
    return CanonicalModelRef(
        object_model_id=model.id,
        storage_path=model.storage_path,
        sha256_checksum=model.sha256_checksum,
        revision_number=model.revision_number,
        website_object_id=(
            db.execute(select(Object.website_object_id).where(Object.id == model.object_id)).scalar_one_or_none()
        ),
    )


def _build_metrics(args: argparse.Namespace) -> VariantMetrics:
    extensions = [e.strip() for e in (args.geometry_extensions or "").split(",") if e.strip()]
    recipe = json.loads(args.generator_recipe) if args.generator_recipe else {}
    return VariantMetrics(
        max_texture_dimension_px=args.max_texture_dimension_px,
        decoded_texture_ram_bytes=args.decoded_texture_ram_bytes,
        triangle_count=args.triangle_count,
        vertex_count=args.vertex_count,
        draw_call_count=args.draw_call_count,
        geometry_extensions=extensions,
        generator_version=args.generator_version,
        generator_recipe=recipe,
        visual_qa_report_path=args.visual_qa_report_path,
        visual_qa_changed_px_ratio=args.visual_qa_changed_px_ratio,
        device_qa_notes=args.device_qa_notes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one approved optimized object-model variant (internal).")
    ident = parser.add_mutually_exclusive_group(required=True)
    ident.add_argument("--object-model-id", help="Canonical object_models.id (UUID).")
    ident.add_argument("--website-object-id", help="Public object slug, e.g. demo-panel.")
    parser.add_argument(
        "--variant-key",
        required=True,
        help="Allowlisted key: mobile-1024 | mobile-2048 | mobile-4096 | web-8192.",
    )
    parser.add_argument("--candidate", required=True, help="Path to the approved candidate .glb (under the artifact root).")
    parser.add_argument("--created-by", required=True, help="Uploader/author user UUID.")
    parser.add_argument("--approved-by", required=True, help="Approver user UUID (must differ from --created-by).")
    parser.add_argument("--media-root", default=settings.media_root, help="Destination media root (default: settings.media_root).")
    parser.add_argument("--artifact-root", default=None, help="Approved candidate source root.")
    parser.add_argument("--expected-canonical-sha256", default=None, help="SHA the candidate was built from; verified against current canonical.")
    parser.add_argument("--max-texture-dimension-px", type=int, required=True)
    parser.add_argument("--decoded-texture-ram-bytes", type=int, required=True)
    parser.add_argument("--triangle-count", type=int, required=True)
    parser.add_argument("--vertex-count", type=int, required=True)
    parser.add_argument("--draw-call-count", type=int, default=None)
    parser.add_argument("--geometry-extensions", default="", help="Comma-separated glTF extensions.")
    parser.add_argument("--generator-version", required=True)
    parser.add_argument("--generator-recipe", default="", help="JSON object describing the optimization recipe.")
    parser.add_argument("--visual-qa-report-path", default=None)
    parser.add_argument("--visual-qa-changed-px-ratio", type=float, default=None)
    parser.add_argument("--device-qa-notes", default=None, help="Truthful physical-device QA evidence (device, OS, date, verdict).")
    parser.add_argument("--approve", action="store_true", help="Set approval/QA statuses to passed (else draft).")
    parser.add_argument("--selectable", action="store_true", help="Mark public-selectable (requires --approve).")
    parser.add_argument("--dry-run", action="store_true", help="Validate + plan without copying or inserting.")
    args = parser.parse_args()
    artifact_root = args.artifact_root or str(_default_artifact_root())

    try:
        with SessionLocal() as db:
            canonical = _resolve_canonical(
                db, object_model_id=args.object_model_id, website_object_id=args.website_object_id
            )
            result = publish_object_model_variant(
                db,
                canonical=canonical,
                variant_key=args.variant_key,
                candidate_path=args.candidate,
                media_root=args.media_root,
                artifact_root=artifact_root,
                created_by=args.created_by,
                approved_by=args.approved_by,
                metrics=_build_metrics(args),
                expected_canonical_sha256=args.expected_canonical_sha256,
                approve=args.approve,
                selectable=args.selectable,
                dry_run=args.dry_run,
            )
    except VariantPublishError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2

    mode = "DRY-RUN (no write)" if result.dry_run else "PUBLISHED"
    print(f"{mode}")
    print(f"  destination: {result.destination_path}")
    print(f"  variant_sha256: {result.variant_sha256}")
    print(f"  file_size_bytes: {result.file_size_bytes}")
    print(f"  approval_status: {result.row_fields['approval_status']}")
    print(f"  visual_qa_status: {result.row_fields['visual_qa_status']}")
    print(f"  device_qa_status: {result.row_fields['device_qa_status']}")
    print(f"  is_public_selectable: {result.row_fields['is_public_selectable']}")
    print("NOTE: public_model_variants_enabled is unchanged; enable per environment to serve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
