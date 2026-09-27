"""Notify-me digest worker.

Queued delivery of opt-in subscriber notifications.

Fans out a digest email via Postmark to every active subscriber in a
customer_key partition when a new object becomes available on the
public projection.

Per-subscriber 24h gate: subscribers whose last_digest_at is within 24h
are skipped this round (logged as "skipped"). The session-plan target
is true multi-object batching ("if two objects publish within the
window, combine into one email"); the v1 MVP implements the simpler
"skip if recently emailed" semantic. Future enhancement: aggregate
queued objects into a periodic batched digest. Documented as gap in
Delivery status is recorded for inspection.

Idempotency: callers can safely re-invoke with the same arguments
within the 24h window — the gate ensures duplicate sends are skipped.
This makes the trigger endpoint safe to retry on transient network
failures.

Trigger entrypoints:
- POST /api/v1/public/admin/digest/trigger (apps/api/app/api/v1/
  endpoints/public.py:admin_trigger_digest) — operator-driven after
  publish + sync, authenticated via shared bearer token
- Future: sync-completion hook in app.services.public_sync (out of
  scope for v1)

Side effects:
- One Postmark API call per eligible subscriber (or one log line in
  dry-run mode)
- last_digest_at update on each successfully-sent subscriber row
- Single transaction commit at the end of the fan-out
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Subscriber
from app.services.digest_email import DigestContext, send_digest_email

logger = logging.getLogger(__name__)


_DIGEST_BATCH_WINDOW = timedelta(hours=24)


class DigestRunCounts(TypedDict):
    """Per-run counts returned by send_object_digest.

    total      All subscribers in the partition (active + recently
               emailed). Useful as a denominator for ops dashboards.
    eligible   Subscribers passing the 24h gate (= sent + failed when
               no in-flight failures occur).
    sent       Successful Postmark sends or successful dry-run no-ops.
    skipped    Filtered by the 24h gate.
    failed     Transport-level Postmark failures (or unset-token cases
               classified as failed if dry-run is disabled). The
               subscriber row's last_digest_at is NOT advanced for
               failed sends, so they roll into the next trigger.
    """

    total: int
    eligible: int
    sent: int
    skipped: int
    failed: int


def send_object_digest(
    db: Session,
    *,
    customer_key: str,
    object_title: str,
    evidence_excerpt: str,
    deep_link: str,
    base_url: str,
) -> DigestRunCounts:
    """Fan out the digest for one object to all eligible subscribers.

    Args:
        db                Active SQLAlchemy session. Caller manages the
                          session lifecycle; this function does not
                          open or close the session itself, only commits
                          last_digest_at updates at the end.
        customer_key      Partition. Subscribers in this customer_key
                          are candidates; other partitions are not
                          touched.
        object_title      Used in the email subject + body.
        evidence_excerpt  1–2 line transcript moment for the body
                          blockquote.
        deep_link         Absolute URL to /evidence/objects/{slug} on
                          the customer's public host.
        base_url          Same public host. Used to build per-recipient
                          unsubscribe links.

    Returns counts; see DigestRunCounts.
    """
    counts: DigestRunCounts = {
        "total": 0,
        "eligible": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    active_subs = (
        db.execute(
            select(Subscriber)
            .where(
                Subscriber.customer_key == customer_key,
                Subscriber.unsubscribed_at.is_(None),
            )
            .order_by(Subscriber.created_at.asc())
        )
        .scalars()
        .all()
    )
    counts["total"] = len(active_subs)
    if not active_subs:
        return counts

    cutoff = datetime.now(timezone.utc) - _DIGEST_BATCH_WINDOW
    base_clean = base_url.rstrip("/")

    for sub in active_subs:
        if sub.last_digest_at is not None and sub.last_digest_at >= cutoff:
            counts["skipped"] += 1
            continue
        counts["eligible"] += 1

        unsubscribe_url = (
            f"{base_clean}/api/v1/public/subscribers/unsubscribe?token={sub.unsubscribe_token}"
        )
        ctx: DigestContext = {
            "customer_key": customer_key,
            "object_title": object_title,
            "evidence_excerpt": evidence_excerpt,
            "deep_link": deep_link,
            "unsubscribe_url": unsubscribe_url,
        }

        ok = send_digest_email(sub.email, ctx)
        if ok:
            counts["sent"] += 1
            sub.last_digest_at = datetime.now(timezone.utc)
        else:
            counts["failed"] += 1
            logger.warning(
                "digest send failed for subscriber id=%s customer=%s",
                sub.id,
                customer_key,
            )

    db.commit()

    logger.info(
        "digest run complete: customer=%s total=%d eligible=%d sent=%d skipped=%d failed=%d title=%r",
        customer_key,
        counts["total"],
        counts["eligible"],
        counts["sent"],
        counts["skipped"],
        counts["failed"],
        object_title,
    )
    return counts
