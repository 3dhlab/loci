"""Notify-me digest email sender.

Templated opt-in subscriber notifications.

Renders the LOCI-branded digest template (apps/api/app/email_templates/
notify_digest.{txt,html}) and ships via Postmark using the existing
Postmark configuration in app.core.config (postmark_server_token,
postmark_sender_email, postmark_message_stream).

Dry-run mode: when settings.postmark_server_token is empty, send_digest
logs what it would have sent and returns True (successful no-op). Lets
the worker ship before the operator injects the Postmark credential on
the VPS — same pattern as the existing app.core.email password-reset
helper.

LOCI branding in the templates uses these defaults:
- Inline static converge mark (axis colors navy/rust/olive + burnt-
  orange locus dot, geometry verbatim from
  branding/animations/converge.svg). Animations stripped — email clients
  vary widely in CSS-animation support and inline SVG <animate> support.
- Cream `#f4efe6` background, ink `#152231` text, ink-soft `#516070`
  muted, line `#d9d2c8` hairlines, accent `#ca5f22` for the tagline +
  link colors.
- Fraunces serif display for headlines + tagline; Inter sans body. Fonts
  declared in CSS font-family with system fallbacks for clients that
  don't render web fonts.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from string import Template
from typing import TypedDict

import httpx

from app.core.config import settings
from app.core.customer_keys import customer_display_name

logger = logging.getLogger(__name__)


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "email_templates"
_TEMPLATE_TXT = _TEMPLATE_DIR / "notify_digest.txt"
_TEMPLATE_HTML = _TEMPLATE_DIR / "notify_digest.html"


class DigestContext(TypedDict):
    """Required keys for rendering a digest email.

    customer_key       partition key (e.g., "3dz") — internal
    object_title       human-readable object title (raw, will be HTML-escaped
                       in HTML body and used verbatim in plain text)
    evidence_excerpt   1–2 line excerpt from a published transcript moment
    deep_link          absolute URL to /evidence/objects/{slug} on the
                       customer's public host
    unsubscribe_url    absolute URL with the per-recipient token query param
    """

    customer_key: str
    object_title: str
    evidence_excerpt: str
    deep_link: str
    unsubscribe_url: str


def _render(template_path: Path, ctx: dict[str, str]) -> str:
    raw = template_path.read_text(encoding="utf-8")
    return Template(raw).safe_substitute(**ctx)


def _build_html_context(ctx: DigestContext, customer_name: str) -> dict[str, str]:
    """HTML-escape every field that flows into the HTML body.

    Defends against XSS in the (unlikely but plausible) case where an
    object title or excerpt contains HTML metacharacters from human
    metadata input. URLs are not escaped — they may legitimately contain
    `&` and other characters that html.escape would mangle; consumer
    template uses them in href= where browsers tolerate raw `&`.
    """
    return {
        "customer_name": html.escape(customer_name),
        "object_title": html.escape(ctx["object_title"]),
        "evidence_excerpt": html.escape(ctx["evidence_excerpt"]),
        "deep_link": ctx["deep_link"],
        "unsubscribe_url": ctx["unsubscribe_url"],
    }


def render_digest(ctx: DigestContext) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for a digest send.

    Public for testing. send_digest_email calls this internally.
    """
    customer_name = customer_display_name(ctx["customer_key"])
    subject = f"New on {customer_name} Loci — {ctx['object_title']}"

    text_ctx = {
        "customer_name": customer_name,
        "object_title": ctx["object_title"],
        "evidence_excerpt": ctx["evidence_excerpt"],
        "deep_link": ctx["deep_link"],
        "unsubscribe_url": ctx["unsubscribe_url"],
    }
    text_body = _render(_TEMPLATE_TXT, text_ctx)
    html_body = _render(_TEMPLATE_HTML, _build_html_context(ctx, customer_name))
    return subject, text_body, html_body


def send_digest_email(to_email: str, ctx: DigestContext) -> bool:
    """Send a single digest email to one recipient.

    Returns True on successful send (or successful dry-run). False on
    transport failure. Caller is responsible for retry / dead-letter
    decisions — this helper does not retry.

    Dry-run mode (Postmark token unset): logs what would have sent and
    returns True. Lets the worker ship before VPS credential injection.
    """
    subject, text_body, html_body = render_digest(ctx)

    if not settings.postmark_server_token:
        logger.warning(
            "[dry-run] notify-digest email — to=%s subject=%r — "
            "Postmark not configured; skipping send",
            to_email,
            subject,
        )
        return True

    payload = {
        "From": settings.postmark_sender_email,
        "To": to_email,
        "Subject": subject,
        "TextBody": text_body,
        "HtmlBody": html_body,
        "MessageStream": settings.postmark_message_stream,
        # Surface the per-recipient unsubscribe URL at top-level so Postmark
        # can populate the standard List-Unsubscribe header — improves
        # deliverability and gives clients (Gmail, Apple Mail) a built-in
        # unsubscribe affordance distinct from the body link.
        "MessageStreamUnsubscribeURL": ctx["unsubscribe_url"],
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": settings.postmark_server_token,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                "https://api.postmarkapp.com/email", json=payload, headers=headers
            )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to send notify-digest email to %s", to_email)
        return False

    return True
