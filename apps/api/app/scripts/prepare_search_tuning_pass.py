from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.api.v1.endpoints.search import search_segments
from app.db.session import SessionLocal
from app.models.entities import SearchQueryLog, User
from app.schemas.search import SegmentSearchRequest, SegmentSearchResponse, SegmentSearchResult
from app.services.search_tuning import QUERY_ALIASES, normalize_query


@dataclass
class QueryGroup:
    canonical_query: str
    rows: list[SearchQueryLog]
    variants: set[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a semantic-search tuning pass from logged analysis queries")
    parser.add_argument(
        "--output-dir",
        default="docs/tuning",
        help="Directory where the normalized query bank and worksheet should be written",
    )
    parser.add_argument(
        "--prefix",
        default=f"semantic-search-first-tuning-pass-{datetime.now().date().isoformat()}",
        help="Filename prefix for generated artifacts",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=5,
        help="How many top results to capture per query in the worksheet",
    )
    parser.add_argument(
        "--query-source",
        default="analysis",
        help="Query source to prepare from search_query_logs",
    )
    return parser.parse_args()
def format_clock(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def semantic_tier(score: float | None) -> str:
    if score is None:
        return "keyword-only"
    if score >= 0.78:
        return "strong"
    if score >= 0.72:
        return "medium"
    return "loose"


def scope_label(project_id: UUID | None, video_id: UUID | None) -> str:
    if project_id and video_id:
        return f"project={project_id} video={video_id}"
    if project_id:
        return f"project={project_id}"
    if video_id:
        return f"video={video_id}"
    return "all projects / all videos"


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|")


def load_groups(query_source: str) -> tuple[list[SearchQueryLog], list[QueryGroup]]:
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(SearchQueryLog)
                .where(SearchQueryLog.query_source == query_source)
                .order_by(SearchQueryLog.created_at.desc())
            )
            .scalars()
            .all()
        )

    grouped: dict[str, QueryGroup] = {}
    for row in rows:
        canonical_query = normalize_query(row.query_text)
        group = grouped.get(canonical_query)
        if group is None:
            group = QueryGroup(canonical_query=canonical_query, rows=[], variants=set())
            grouped[canonical_query] = group
        group.rows.append(row)
        group.variants.add(row.query_text)

    ordered_groups = sorted(grouped.values(), key=lambda group: group.canonical_query)
    return rows, ordered_groups


def rerun_query_group(
    *,
    group: QueryGroup,
    page_size: int,
) -> tuple[SegmentSearchResponse, SearchQueryLog]:
    latest_row = group.rows[0]

    with SessionLocal() as db:
        user = db.get(User, latest_row.user_id)
        if user is None:
            raise RuntimeError(f"User {latest_row.user_id} not found for query rerun")

        response = search_segments(
            SegmentSearchRequest(
                query=group.canonical_query,
                project_id=latest_row.project_id,
                video_id=latest_row.video_id,
                published_only=False,
                page=1,
                page_size=page_size,
            ),
            db=db,
            current_user=user,
        )
        latest_rerun = (
            db.execute(
                select(SearchQueryLog)
                .where(
                    SearchQueryLog.user_id == latest_row.user_id,
                    SearchQueryLog.query_source == "analysis",
                    SearchQueryLog.query_text == group.canonical_query,
                )
                .order_by(SearchQueryLog.created_at.desc())
            )
            .scalars()
            .first()
        )
        if latest_rerun is None:
            raise RuntimeError(f"No rerun log row found for query {group.canonical_query!r}")

    return response, latest_rerun


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return

    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def render_result_line(result: SegmentSearchResult) -> str:
    lexical_label = "keyword" if result.lexical_match else "semantic"
    semantic_label = ""
    if result.semantic_score is not None:
        semantic_label = f" | {semantic_tier(result.semantic_score)} {round(result.semantic_score * 100):d}/100"

    return (
        f"- {result.video_title} @ {format_clock(result.start_ms)}"
        f" | {lexical_label}{semantic_label}"
        f" | {result.text.strip()}"
    )


def write_markdown(
    *,
    path: Path,
    generated_at: datetime,
    raw_rows: list[SearchQueryLog],
    groups: list[QueryGroup],
    csv_path: Path,
    rerun_records: list[dict[str, object]],
) -> None:
    analysis_raw_count = len(raw_rows)
    analysis_distinct_count = len({row.query_text for row in raw_rows})
    canonical_count = len(groups)
    alias_lines = [f"- `{source}` -> `{target}`" for source, target in sorted(QUERY_ALIASES.items())]

    lines = [
        "# Semantic Search First Tuning Pass",
        "",
        f"Generated: {generated_at.isoformat()}",
        "",
        "## Query Bank",
        "",
        f"- Analysis search events: `{analysis_raw_count}`",
        f"- Distinct raw Analysis queries: `{analysis_distinct_count}`",
        f"- Distinct canonical Analysis queries after normalization: `{canonical_count}`",
        f"- CSV artifact: `{csv_path}`",
        "",
        "Normalization rules:",
        "",
        "- lowercase",
        "- trim leading/trailing whitespace",
        "- collapse repeated spaces",
        "- strip wrapping single/double quotes",
        *alias_lines,
        "",
        "## Scoring Table",
        "",
        "| Query | Variants | Uses | Scope | Retrieval | Top Semantic | Top Window | Top Surfaced Segment | Top 5 Useful | Notes |",
        "| ----- | -------- | ---- | ----- | --------- | ------------ | ---------- | ------------------- | ------------ | ----- |",
    ]

    for record in rerun_records:
        lines.append(
            "| {query} | {variants} | {uses} | {scope} | {retrieval} | {top_semantic} | {top_window} | {top_segment} |  |  |".format(
                query=markdown_escape(record["canonical_query"]),
                variants=markdown_escape(record["variants"]),
                uses=record["use_count"],
                scope=markdown_escape(record["scope"]),
                retrieval=record["retrieval_mode"],
                top_semantic=record["top_semantic_score"],
                top_window=record["top_window_score"],
                top_segment=record["top_surfaced_segment_score"],
            )
        )

    lines.extend(
        [
            "",
            "## Top 5 Results By Query",
            "",
        ]
    )

    for record in rerun_records:
        lines.extend(
            [
                f"### {record['canonical_query']}",
                "",
                f"- Variants: {record['variants']}",
                f"- Uses in log: {record['use_count']}",
                f"- Scope used for rerun: {record['scope']}",
                f"- Retrieval mode: {record['retrieval_mode']}",
                f"- Top semantic score: {record['top_semantic_score']}",
                f"- Top window score: {record['top_window_score']}",
                f"- Top surfaced segment score: {record['top_surfaced_segment_score']}",
                "",
                "Scoring:",
                "",
                "- Query type:",
                "- Expected target:",
                "- Top result useful:",
                "- Top 3 useful:",
                "- Fit tier felt right:",
                "- Failure mode:",
                "- Notes:",
                "",
                "Results:",
                "",
            ]
        )
        lines.extend(record["result_lines"])
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_rows, groups = load_groups(args.query_source)
    rerun_records: list[dict[str, object]] = []

    for group in groups:
        response, rerun_log = rerun_query_group(group=group, page_size=args.page_size)
        latest_row = group.rows[0]
        rerun_records.append(
            {
                "canonical_query": group.canonical_query,
                "variants": ", ".join(sorted(group.variants)),
                "use_count": len(group.rows),
                "scope": scope_label(latest_row.project_id, latest_row.video_id),
                "retrieval_mode": rerun_log.retrieval_mode or "",
                "top_semantic_score": str(rerun_log.top_semantic_score or ""),
                "top_window_score": str(rerun_log.top_window_score or ""),
                "top_surfaced_segment_score": str(rerun_log.top_surfaced_segment_score or ""),
                "result_count": response.total_results,
                "result_lines": [render_result_line(result) for result in response.results],
            }
        )

    csv_rows = [
        {
            "canonical_query": record["canonical_query"],
            "variants": record["variants"],
            "use_count": str(record["use_count"]),
            "scope": record["scope"],
            "retrieval_mode": record["retrieval_mode"],
            "top_semantic_score": record["top_semantic_score"],
            "top_window_score": record["top_window_score"],
            "top_surfaced_segment_score": record["top_surfaced_segment_score"],
            "result_count": str(record["result_count"]),
        }
        for record in rerun_records
    ]

    csv_path = output_dir / f"{args.prefix}-queries.csv"
    markdown_path = output_dir / f"{args.prefix}-worksheet.md"

    write_csv(csv_path, csv_rows)
    write_markdown(
        path=markdown_path,
        generated_at=datetime.now(),
        raw_rows=raw_rows,
        groups=groups,
        csv_path=csv_path,
        rerun_records=rerun_records,
    )

    print(f"Prepared {len(groups)} canonical {args.query_source} queries")
    print(f"Wrote {csv_path}")
    print(f"Wrote {markdown_path}")


if __name__ == "__main__":
    main()
