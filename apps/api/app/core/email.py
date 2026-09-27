from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_password_reset_email(to_email: str, reset_token: str) -> bool:
    if not settings.postmark_server_token:
        logger.warning("Postmark server token is not configured; password reset email skipped")
        return settings.semantic_env != "production"

    reset_link = f"{settings.password_reset_base_url}?token={reset_token}"
    subject = "Semantic password reset"
    text_body = (
        "A password reset was requested for your Semantic account. "
        f"Use this link within {settings.reset_token_expire_minutes} minutes: {reset_link}"
    )

    payload = {
        "From": settings.postmark_sender_email,
        "To": to_email,
        "Subject": subject,
        "TextBody": text_body,
        "HtmlBody": (
            "<p>A password reset was requested for your Semantic account.</p>"
            f"<p>Use this link within {settings.reset_token_expire_minutes} minutes:</p>"
            f"<p><a href=\"{reset_link}\">{reset_link}</a></p>"
        ),
        "MessageStream": settings.postmark_message_stream,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": settings.postmark_server_token,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.postmarkapp.com/email", json=payload, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to send Postmark password reset email")
        return False

    return True
