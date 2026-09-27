from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import SearchQueryLog


def main() -> None:
    parser = argparse.ArgumentParser(description="Export logged search queries for tuning review")
    parser.add_argument(
        "--output",
        default="/tmp/semantic-search-query-log.csv",
        help="Path to write the CSV export",
    )
    parser.add_argument("--limit", type=int, default=500, help="Maximum number of query rows to export")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    query = select(SearchQueryLog).order_by(SearchQueryLog.created_at.desc()).limit(args.limit)

    with SessionLocal() as db:
        rows = db.execute(query).scalars().all()

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "created_at",
                "query_text",
                "query_source",
                "project_id",
                "video_id",
                "result_count",
                "lexical_result_count",
                "semantic_result_count",
                "semantic_attempted",
                "semantic_succeeded",
                "semantic_provider",
                "semantic_model",
                "retrieval_mode",
                "top_semantic_score",
                "top_window_score",
                "top_surfaced_segment_score",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.created_at.isoformat() if row.created_at else "",
                    row.query_text,
                    row.query_source,
                    row.project_id or "",
                    row.video_id or "",
                    row.result_count,
                    row.lexical_result_count,
                    row.semantic_result_count,
                    row.semantic_attempted,
                    row.semantic_succeeded,
                    row.semantic_provider or "",
                    row.semantic_model or "",
                    row.retrieval_mode or "",
                    row.top_semantic_score if row.top_semantic_score is not None else "",
                    row.top_window_score if row.top_window_score is not None else "",
                    row.top_surfaced_segment_score if row.top_surfaced_segment_score is not None else "",
                ]
            )

    print(f"Exported {len(rows)} search queries to {output_path}")


if __name__ == "__main__":
    main()
