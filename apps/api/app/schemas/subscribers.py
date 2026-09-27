"""Schemas for the public notify-me subscriber endpoints.

Public subscriber registration.

Data-minimization posture (subscriber privacy contract):
- email is stored in plaintext, not hashed. Industry norm for opt-in
  subscriber lists; required for email-link unsubscribe to work and for
  future migration to other email systems. Trade-off accepted because
  the alternative (hashed) would break the unsubscribe UX without
  meaningfully reducing risk — the public unsubscribe flow already
  exposes the email at click time.
- No other PII stored. The optional context fields (source_url,
  user_agent_class, language) are intentionally coarse so that even
  if the table is exfiltrated, no individual visitor can be
  fingerprinted from these rows alone.
- customer_key is derived from the Host header server-side, NOT from a
  client-supplied field. See app.core.customer_keys.resolve_customer_key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SubscribeRequest(BaseModel):
    """Body for POST /api/v1/public/subscribers.

    customer_key is intentionally NOT a request field. The endpoint always
    derives customer_key from the Host header server-side. Clients cannot
    influence which partner partition they land in.
    """

    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    source_url: str | None = Field(default=None, max_length=2048)
    user_agent_class: (
        Literal["desktop", "mobile", "tablet", "other"] | None
    ) = None
    language: str | None = Field(default=None, max_length=16)


class SubscribeResponse(BaseModel):
    """Response for POST /api/v1/public/subscribers.

    status semantics:
      created             new row inserted
      already_subscribed  email + customer_key already present and active
      resubscribed        email + customer_key was previously unsubscribed
                          and is now re-activated (unsubscribed_at cleared)
    """

    status: Literal["created", "already_subscribed", "resubscribed"]


class UnsubscribeRequest(BaseModel):
    """Body for POST /api/v1/public/subscribers/unsubscribe.

    The token is the only credential. No email field — token-based
    indexing means an attacker who guesses random tokens still cannot
    target a specific email address.
    """

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=20, max_length=128)


class UnsubscribeResponse(BaseModel):
    """Response for POST /api/v1/public/subscribers/unsubscribe.

    not_found is returned when no row matches the token. The endpoint
    deliberately returns 200 + status: "not_found" rather than 404 so a
    bad token cannot be distinguished from a never-subscribed email — a
    simple defense against token-enumeration timing attacks.
    """

    status: Literal["unsubscribed", "already_unsubscribed", "not_found"]


class SubscriberDeleteRequest(BaseModel):
    """Body for POST /api/v1/public/subscribers/delete.

    Hard-delete (vs soft-delete via unsubscribe). For GDPR right-to-erasure.
    Requires the same per-row token as unsubscribe.
    """

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=20, max_length=128)


class AdminDigestTriggerRequest(BaseModel):
    """Body for POST /api/v1/public/admin/digest/trigger .

    Operator-driven endpoint. The local authoring console hits this on
    the public stack after publish + sync to fan out a digest email to
    subscribers in the same customer_key partition.

    Auth: shared bearer token via Authorization header (config:
    loci_digest_trigger_token). The endpoint refuses unless that token
    is configured AND matches.

    customer_key is NOT a request field — derived server-side from the
    Host header same as the subscribe endpoint, so a misconfigured
    caller cannot fan a digest into the wrong partner partition.
    """

    model_config = ConfigDict(extra="ignore")

    object_slug: str = Field(min_length=1, max_length=255)
    object_title: str = Field(min_length=1, max_length=512)
    evidence_excerpt: str = Field(min_length=1, max_length=2048)


class AdminDigestTriggerResponse(BaseModel):
    """Counts returned by the digest trigger endpoint."""

    total: int = Field(ge=0)
    eligible: int = Field(ge=0)
    sent: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
