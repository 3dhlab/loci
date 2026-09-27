"""Configure, validate, activate, or contain generic object delivery profiles.

Each requested object runs in its own database transaction. This keeps batch
locks small, makes retries idempotent, and records one durable audit event per
successful object mutation. The global kill switch remains unchanged.
"""
from __future__ import annotations

import argparse
import sys

from app.core.config import settings
from app.db.session import SessionLocal
from app.scripts.publish_object_model_variant import _resolve_canonical
from app.services.object_model_variant_publish import (
    VariantPublishError,
    activate_required_object_variant_set,
    configure_object_delivery_profile,
    deactivate_required_object_variant_set,
)

MAX_BATCH_OBJECTS = 25


def main() -> int:
    parser = argparse.ArgumentParser(description="Operate one or more object-model delivery profiles.")
    parser.add_argument(
        "--website-object-id",
        action="append",
        required=True,
        help="Public object slug. Repeat for a bounded independent-transaction batch.",
    )
    parser.add_argument("--actor-id", required=True, help="Audited operator user UUID.")
    parser.add_argument("--media-root", default=settings.media_root, help="Configured model media root.")
    parser.add_argument("--dry-run", action="store_true", help="Validate each object and roll back all changes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--deactivate", action="store_true", help="Contain each configured profile.")
    mode.add_argument("--configure", action="store_true", help="Create or update an inactive profile.")
    parser.add_argument("--standard-tier", help="Fixed tier for standard clients during --configure.")
    parser.add_argument("--constrained-tier", help="Fixed tier for constrained clients during --configure.")
    args = parser.parse_args()
    object_ids = [value.strip().lower() for value in args.website_object_id if value.strip()]
    if not object_ids:
        parser.error("at least one non-empty --website-object-id is required")
    if len(object_ids) > MAX_BATCH_OBJECTS:
        parser.error(f"batch size exceeds the {MAX_BATCH_OBJECTS}-object operational limit")
    if len(set(object_ids)) != len(object_ids):
        parser.error("each --website-object-id must be unique within a batch")
    if args.configure and (not args.standard_tier or not args.constrained_tier):
        parser.error("--configure requires --standard-tier and --constrained-tier")
    if not args.configure and (args.standard_tier or args.constrained_tier):
        parser.error("tier arguments require --configure")

    failures = 0
    for website_object_id in object_ids:
        try:
            with SessionLocal() as db:
                canonical = _resolve_canonical(db, object_model_id=None, website_object_id=website_object_id)
                if args.configure:
                    result = configure_object_delivery_profile(
                        db,
                        canonical=canonical,
                        website_object_id=website_object_id,
                        standard_variant_key=args.standard_tier,
                        constrained_variant_key=args.constrained_tier,
                        actor_id=args.actor_id,
                        dry_run=args.dry_run,
                    )
                    print(
                        f"{'VALIDATED' if result.dry_run else 'CONFIGURED'} {result.website_object_id}: "
                        f"standard={result.standard_variant_key}, constrained={result.constrained_variant_key}"
                    )
                elif args.deactivate:
                    result = deactivate_required_object_variant_set(
                        db,
                        canonical=canonical,
                        website_object_id=website_object_id,
                        actor_id=args.actor_id,
                        dry_run=args.dry_run,
                    )
                    print(
                        f"{'VALIDATED' if result.dry_run else 'DEACTIVATED'} {result.website_object_id}: "
                        f"{','.join(result.deactivated_variant_keys) or 'no rows'}"
                    )
                else:
                    result = activate_required_object_variant_set(
                        db,
                        canonical=canonical,
                        website_object_id=website_object_id,
                        media_root=args.media_root,
                        actor_id=args.actor_id,
                        dry_run=args.dry_run,
                    )
                    print(
                        f"{'VALIDATED' if result.dry_run else 'ACTIVATED'} {result.website_object_id}: "
                        f"{','.join(result.activated_variant_keys)}"
                    )
        except (VariantPublishError, ValueError) as exc:
            failures += 1
            print(f"REJECTED {website_object_id}: {exc}", file=sys.stderr)

    print("NOTE: public_model_variants_enabled is unchanged; deployment and publication remain approval-gated.")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
