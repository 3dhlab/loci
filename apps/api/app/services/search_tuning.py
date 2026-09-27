from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import SearchQueryLog, SearchResultFeedback, SearchTuningReport


TUNING_REPORT_MIN_NEW_VOTES = 50
TUNING_REPORT_MIN_RECURRING_CANONICAL_QUERIES = 10

QUERY_ALIASES = {
    "father hood": "fatherhood",
    "herosim": "heroism",
}


@dataclass
class TuningWindowSummary:
    feedback_window_start_at: datetime | None
    feedback_window_end_at: datetime | None
    new_vote_count: int
    positive_vote_count: int
    negative_vote_count: int
    canonical_query_count: int
    recurring_canonical_query_count: int
    ready_for_report: bool
    minimum_new_votes_required: int
    minimum_recurring_canonical_queries_required: int
    canonical_queries: list[dict[str, Any]]
    score_bands: list[dict[str, Any]]
    retrieval_modes: list[dict[str, Any]]
    recent_feedback: list[dict[str, Any]]


def normalize_query(text: str) -> str:
    value = (text or "").strip().lower()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    value = re.sub(r"\s+", " ", value)
    return QUERY_ALIASES.get(value, value)


def latest_tuning_report(db: Session, user_id: UUID) -> SearchTuningReport | None:
    return (
        db.execute(
            select(SearchTuningReport)
            .where(SearchTuningReport.user_id == user_id)
            .order_by(SearchTuningReport.created_at.desc())
        )
        .scalars()
        .first()
    )


def _score_band_label(score: Decimal | None) -> str:
    if score is None:
        return "Keyword only"
    score_value = float(score)
    if score_value < 0.68:
        return "Below 68"
    if score_value < 0.72:
        return "68-71"
    if score_value < 0.78:
        return "72-77"
    return "78+"


def _serialize_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_tuning_window_summary(
    db: Session,
    *,
    user_id: UUID,
    limit_canonical_queries: int = 12,
    limit_recent_feedback: int = 12,
) -> TuningWindowSummary:
    last_report = latest_tuning_report(db, user_id)
    feedback_window_start_at = last_report.feedback_window_end_at if last_report is not None else None

    feedback_stmt = (
        select(SearchResultFeedback, SearchQueryLog)
        .join(SearchQueryLog, SearchQueryLog.id == SearchResultFeedback.search_query_log_id)
        .where(
            SearchResultFeedback.user_id == user_id,
            SearchQueryLog.query_source == "analysis",
        )
        .order_by(SearchResultFeedback.created_at.desc())
    )
    if feedback_window_start_at is not None:
        feedback_stmt = feedback_stmt.where(SearchResultFeedback.created_at > feedback_window_start_at)

    feedback_rows = db.execute(feedback_stmt).all()

    positive_vote_count = 0
    negative_vote_count = 0
    latest_feedback_at: datetime | None = None
    canonical_map: dict[str, dict[str, Any]] = {}
    score_band_map: dict[str, dict[str, Any]] = {}
    retrieval_mode_map: dict[str, dict[str, Any]] = {}
    recent_feedback: list[dict[str, Any]] = []

    for feedback, query_log in feedback_rows:
        if latest_feedback_at is None:
            latest_feedback_at = feedback.created_at

        canonical_query = normalize_query(query_log.query_text)
        canonical_entry = canonical_map.setdefault(
            canonical_query,
            {
                "canonical_query": canonical_query,
                "variants": set(),
                "vote_count": 0,
                "positive_vote_count": 0,
                "negative_vote_count": 0,
                "distinct_query_run_ids": set(),
                "retrieval_modes": defaultdict(int),
                "semantic_score_sum": 0.0,
                "semantic_score_count": 0,
                "rank_position_sum": 0,
            },
        )
        canonical_entry["variants"].add(query_log.query_text)
        canonical_entry["vote_count"] += 1
        canonical_entry["distinct_query_run_ids"].add(str(query_log.id))
        canonical_entry["retrieval_modes"][query_log.retrieval_mode or "unknown"] += 1
        canonical_entry["rank_position_sum"] += feedback.rank_position
        if feedback.semantic_score is not None:
            canonical_entry["semantic_score_sum"] += float(feedback.semantic_score)
            canonical_entry["semantic_score_count"] += 1

        if feedback.is_relevant:
            positive_vote_count += 1
            canonical_entry["positive_vote_count"] += 1
        else:
            negative_vote_count += 1
            canonical_entry["negative_vote_count"] += 1

        score_band_label = _score_band_label(feedback.semantic_score)
        score_band_entry = score_band_map.setdefault(
            score_band_label,
            {"label": score_band_label, "vote_count": 0, "positive_vote_count": 0, "negative_vote_count": 0},
        )
        score_band_entry["vote_count"] += 1
        if feedback.is_relevant:
            score_band_entry["positive_vote_count"] += 1
        else:
            score_band_entry["negative_vote_count"] += 1

        retrieval_mode_label = query_log.retrieval_mode or "unknown"
        retrieval_mode_entry = retrieval_mode_map.setdefault(
            retrieval_mode_label,
            {"label": retrieval_mode_label, "vote_count": 0, "positive_vote_count": 0, "negative_vote_count": 0},
        )
        retrieval_mode_entry["vote_count"] += 1
        if feedback.is_relevant:
            retrieval_mode_entry["positive_vote_count"] += 1
        else:
            retrieval_mode_entry["negative_vote_count"] += 1

        if len(recent_feedback) < limit_recent_feedback:
            recent_feedback.append(
                {
                    "created_at": feedback.created_at,
                    "query_text": query_log.query_text,
                    "canonical_query": canonical_query,
                    "is_relevant": feedback.is_relevant,
                    "rank_position": feedback.rank_position,
                    "semantic_score": float(feedback.semantic_score) if feedback.semantic_score is not None else None,
                    "lexical_match": feedback.lexical_match,
                    "retrieval_mode": retrieval_mode_label,
                }
            )

    canonical_queries = []
    for canonical_entry in canonical_map.values():
        vote_count = canonical_entry["vote_count"]
        positive_count = canonical_entry["positive_vote_count"]
        recurring_query_runs = len(canonical_entry["distinct_query_run_ids"])
        canonical_queries.append(
            {
                "canonical_query": canonical_entry["canonical_query"],
                "variants": sorted(canonical_entry["variants"]),
                "vote_count": vote_count,
                "positive_vote_count": positive_count,
                "negative_vote_count": canonical_entry["negative_vote_count"],
                "approval_rate": (positive_count / vote_count) if vote_count else 0.0,
                "distinct_query_run_count": recurring_query_runs,
                "is_recurring": recurring_query_runs >= 2,
                "average_semantic_score": (
                    canonical_entry["semantic_score_sum"] / canonical_entry["semantic_score_count"]
                    if canonical_entry["semantic_score_count"]
                    else None
                ),
                "average_rank_position": canonical_entry["rank_position_sum"] / vote_count if vote_count else None,
                "retrieval_modes": [
                    {"label": label, "vote_count": count}
                    for label, count in sorted(
                        canonical_entry["retrieval_modes"].items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ],
            }
        )

    canonical_queries.sort(
        key=lambda item: (
            not item["is_recurring"],
            -item["vote_count"],
            item["approval_rate"],
            item["canonical_query"],
        )
    )

    score_bands = []
    for band in sorted(
        score_band_map.values(),
        key=lambda item: ("Keyword only", "Below 68", "68-71", "72-77", "78+").index(item["label"]),
    ):
        vote_count = band["vote_count"]
        score_bands.append(
            {
                **band,
                "approval_rate": (band["positive_vote_count"] / vote_count) if vote_count else 0.0,
            }
        )

    retrieval_modes = []
    for mode in sorted(retrieval_mode_map.values(), key=lambda item: (-item["vote_count"], item["label"])):
        vote_count = mode["vote_count"]
        retrieval_modes.append(
            {
                **mode,
                "approval_rate": (mode["positive_vote_count"] / vote_count) if vote_count else 0.0,
            }
        )

    recurring_canonical_query_count = sum(1 for item in canonical_queries if item["is_recurring"])
    ready_for_report = (
        len(feedback_rows) >= TUNING_REPORT_MIN_NEW_VOTES
        and recurring_canonical_query_count >= TUNING_REPORT_MIN_RECURRING_CANONICAL_QUERIES
    )

    return TuningWindowSummary(
        feedback_window_start_at=feedback_window_start_at,
        feedback_window_end_at=latest_feedback_at,
        new_vote_count=len(feedback_rows),
        positive_vote_count=positive_vote_count,
        negative_vote_count=negative_vote_count,
        canonical_query_count=len(canonical_queries),
        recurring_canonical_query_count=recurring_canonical_query_count,
        ready_for_report=ready_for_report,
        minimum_new_votes_required=TUNING_REPORT_MIN_NEW_VOTES,
        minimum_recurring_canonical_queries_required=TUNING_REPORT_MIN_RECURRING_CANONICAL_QUERIES,
        canonical_queries=canonical_queries[:limit_canonical_queries],
        score_bands=score_bands,
        retrieval_modes=retrieval_modes,
        recent_feedback=recent_feedback,
    )


def serialize_tuning_window_summary(summary: TuningWindowSummary) -> dict[str, Any]:
    return {
        "feedback_window_start_at": _serialize_timestamp(summary.feedback_window_start_at),
        "feedback_window_end_at": _serialize_timestamp(summary.feedback_window_end_at),
        "new_vote_count": summary.new_vote_count,
        "positive_vote_count": summary.positive_vote_count,
        "negative_vote_count": summary.negative_vote_count,
        "canonical_query_count": summary.canonical_query_count,
        "recurring_canonical_query_count": summary.recurring_canonical_query_count,
        "ready_for_report": summary.ready_for_report,
        "minimum_new_votes_required": summary.minimum_new_votes_required,
        "minimum_recurring_canonical_queries_required": summary.minimum_recurring_canonical_queries_required,
        "canonical_queries": summary.canonical_queries,
        "score_bands": summary.score_bands,
        "retrieval_modes": summary.retrieval_modes,
        "recent_feedback": [
            {
                **item,
                "created_at": _serialize_timestamp(item["created_at"]),
            }
            for item in summary.recent_feedback
        ],
    }


def parse_summary_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
