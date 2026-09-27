from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.api.v1.endpoints.search import search_segments
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import User
from app.schemas.search import SegmentSearchRequest


RETRIEVAL_MODES = ("transcript_only", "visual_only", "combined")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Step 13 multimodal retrieval evaluation battery")
    parser.add_argument(
        "--query-file",
        default="docs/tuning/step13-retrieval-evaluation-query-battery-2026-04-09.csv",
        help="CSV file with query battery rows",
    )
    parser.add_argument(
        "--output",
        default="docs/tuning/step13-retrieval-evaluation-results-2026-04-09.json",
        help="Path to write the detailed JSON results",
    )
    parser.add_argument(
        "--user-email",
        default="u@example.com",
        help="User email whose private corpus should be queried",
    )
    parser.add_argument(
        "--user-id",
        default="",
        help="Optional explicit user UUID; overrides --user-email when provided",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=5,
        help="How many top results to capture for each query/mode pair",
    )
    return parser.parse_args()


def load_query_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))
    if not rows:
        raise RuntimeError(f"No query rows found in {path}")
    required_columns = {"query_id", "category", "query", "expected_visual_role", "notes"}
    missing = required_columns.difference(rows[0].keys())
    if missing:
        raise RuntimeError(f"Missing required columns in {path}: {sorted(missing)}")
    return rows


def load_user(*, user_id: str, user_email: str) -> User:
    with SessionLocal() as db:
        user: User | None = None
        if user_id:
            user = db.get(User, UUID(user_id))
        else:
            user = db.execute(select(User).where(User.email == user_email)).scalar_one_or_none()
        if user is None:
            raise RuntimeError(f"User not found for Step 13 evaluation: {user_id or user_email}")
        db.expunge(user)
    return user


def run_query_modes(
    *,
    user: User,
    query_row: dict[str, str],
    page_size: int,
) -> dict[str, dict]:
    mode_payloads: dict[str, dict] = {}

    for retrieval_mode in RETRIEVAL_MODES:
        with SessionLocal() as db:
            current_user = db.get(User, user.id)
            if current_user is None:
                raise RuntimeError(f"User disappeared during evaluation: {user.id}")
            response = search_segments(
                SegmentSearchRequest(
                    query=query_row["query"],
                    published_only=False,
                    retrieval_mode=retrieval_mode,
                    page=1,
                    page_size=page_size,
                ),
                db=db,
                current_user=current_user,
            )
            mode_payloads[retrieval_mode] = response.model_dump(mode="json")

    return mode_payloads


def main() -> None:
    args = parse_args()
    query_file = Path(args.query_file)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    query_rows = load_query_rows(query_file)
    user = load_user(user_id=args.user_id.strip(), user_email=args.user_email.strip())

    evaluation_rows: list[dict[str, object]] = []
    for row in query_rows:
        evaluation_rows.append(
            {
                "query_id": row["query_id"],
                "category": row["category"],
                "query": row["query"],
                "expected_visual_role": row["expected_visual_role"],
                "notes": row["notes"],
                "modes": run_query_modes(user=user, query_row=row, page_size=args.page_size),
            }
        )
        print(f"Completed {row['query_id']} {row['query']}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": str(user.id),
        "user_email": user.email,
        "page_size": args.page_size,
        "query_file": str(query_file),
        "settings": {
            "visual_description_prompt_version": settings.visual_description_prompt_version,
            "visual_description_max_tokens": settings.visual_description_max_tokens,
            "visual_description_transcript_char_limit": settings.visual_description_transcript_char_limit,
            "openai_vision_temperature": settings.openai_vision_temperature,
            "openai_vision_model": settings.openai_vision_model,
            "visual_rerank_alpha": settings.visual_rerank_alpha,
            "visual_only_min_score": settings.visual_only_min_score,
        },
        "queries": evaluation_rows,
    }

    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2)
        output_file.write("\n")

    print(f"Wrote Step 13 evaluation results to {output_path}")


if __name__ == "__main__":
    main()