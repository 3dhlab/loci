"""Compute review state + publish preview using the SAME functions the public
citation projection uses, so the authoring preview can never drift from what
production will actually publish.

We build a transient (unpersisted) ``TitleCardObservation`` from a lane
``ObservationReview`` row and call the real ``public_evidence`` gate/projection
functions. No projection logic is reimplemented here.
"""
from __future__ import annotations

from app.models.authoring_lane import ObservationReview
from app.models.entities import TitleCardObservation
from app.services.public_evidence import (
    _citation_attribution_payload,
    _public_title_card_speaker_label,
    _title_card_observation_has_reliable_date,
)

# review_state values (mirrors the workflow spec + API plan).
REVIEW_STATES = ("ready", "caption_only", "no_date", "failed", "restored", "suppressed", "approved")
# will_publish_as values.
PUBLISH_STATES = ("named", "dated_speakerless", "unavailable")


def observation_to_public_shape(obs: ObservationReview) -> TitleCardObservation:
    """Build a transient TitleCardObservation mirroring the public-import shape.

    Only the fields the public gates read are populated. ``source_kind`` is set
    to the canonical pilot value so the row is shape-equivalent to an imported
    observation; the gate functions used here do not depend on source priority.
    """
    return TitleCardObservation(
        video_id=obs.batch_id,  # transient; never persisted
        timestamp_ms=obs.timestamp_ms,
        title_card_visible=bool(obs.title_card_visible),
        public_speaker_label=obs.public_speaker_label,
        session_date=None,  # the lane stores only the verbatim text form
        session_date_text=obs.session_date_text,
        confidence_object=obs.confidence_object or "none",
        confidence_presenter=obs.confidence_presenter or "none",
        confidence_session_date=obs.confidence_session_date or "none",
        raw_text_observed=obs.raw_text_observed or "",
        prompt_version="authoring-preview",
        source_kind="pilot_artifact",
        source_version="authoring-preview",
        status=obs.status or "ready",
    )


def compute_gates(obs: ObservationReview) -> dict:
    """Return date_gate, speaker_gate, will_publish_as, and the public preview.

    The preview dict is exactly what ``_citation_attribution_payload`` would
    emit for this row if imported as-is — the single source of truth.
    """
    shaped = observation_to_public_shape(obs)
    date_gate = _title_card_observation_has_reliable_date(shaped)
    speaker_gate = _public_title_card_speaker_label(shaped) is not None
    payload = _citation_attribution_payload(shaped)

    if speaker_gate:
        will_publish_as = "named"
    elif date_gate:
        will_publish_as = "dated_speakerless"
    else:
        will_publish_as = "unavailable"

    return {
        "date_gate": date_gate,
        "speaker_gate": speaker_gate,
        "will_publish_as": will_publish_as,
        "public_preview": {
            "speaker_label": payload["speaker_label"],
            "session_date_text": payload["session_date_text"],
            "session_date_precision": payload["session_date_precision"],
            "attribution_mode": payload["attribution_mode"],
        },
    }


_NO_DATE_REASON_TOKENS = ("no_date", "no date", "date_not", "missing_date", "no_session_date")


def compute_review_state(obs: ObservationReview, restored_timestamps: set[int]) -> str:
    """Classify an observation's current review state from lane fields.

    ``approved`` reflects the review decision (``included_in_speaker_approved``),
    not whether the speaker actually publishes — a row can be approved and still
    resolve dated-speakerless; ``will_publish_as`` carries that honest outcome.
    """
    if (obs.status or "ready").lower() == "failed":
        return "failed"
    if obs.timestamp_ms in restored_timestamps:
        return "restored"
    if obs.is_suppressed:
        reason = (obs.suppression_reason or "").lower()
        if "caption" in reason:
            return "caption_only"
        if any(token in reason for token in _NO_DATE_REASON_TOKENS):
            return "no_date"
        return "suppressed"
    if obs.included_in_speaker_approved:
        return "approved"
    return "ready"
