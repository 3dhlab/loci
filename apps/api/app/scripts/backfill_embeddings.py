from __future__ import annotations

import argparse
import time
from uuid import UUID

from redis import Redis
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import exists, or_, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.ai import AIProviderError, get_vision_provider
from app.services.job_queue import enqueue_indexing
from app.models.entities import Segment, Transcript, TranscriptWindow, VisualWindowDescription
from app.worker.jobs import index_transcript_job


TERMINAL_JOB_STATUSES = {"finished", "failed", "stopped", "canceled", "missing"}


def _redis_conn() -> Redis:
    return Redis.from_url(settings.redis_url)


def _poll_job_states(job_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    connection = _redis_conn()
    for job_id in job_ids:
        try:
            status = Job.fetch(job_id, connection=connection).get_status(refresh=True)
        except NoSuchJobError:
            status = "missing"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _print_corpus_summary() -> None:
    with SessionLocal() as db:
        total_visual = db.execute(select(VisualWindowDescription.id)).scalars().all()
        ready_visual = db.execute(
            select(VisualWindowDescription.id).where(VisualWindowDescription.status == "ready")
        ).scalars().all()
        failed_visual = db.execute(
            select(VisualWindowDescription.id).where(VisualWindowDescription.status == "failed")
        ).scalars().all()
    print(f"Visual rows total: {len(total_visual)}")
    print(f"Visual rows ready: {len(ready_visual)}")
    print(f"Visual rows failed: {len(failed_visual)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill transcript segment embeddings, transcript-window embeddings, and visual window descriptions"
    )
    parser.add_argument("--all", action="store_true", help="Re-index all transcripts, even if they already have embeddings")
    parser.add_argument(
        "--transcript-id",
        action="append",
        dest="transcript_ids",
        help="Restrict backfill to one or more specific transcript UUIDs",
    )
    parser.add_argument("--sync", action="store_true", help="Run indexing inline instead of queuing it on the worker")
    parser.add_argument("--wait", action="store_true", help="Wait for queued jobs to finish and print status updates")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=10,
        help="Polling interval in seconds when --wait is set (default: 10)",
    )
    parser.add_argument(
        "--allow-missing-vision",
        action="store_true",
        help="Allow enqueueing even when the vision provider is unavailable",
    )
    args = parser.parse_args()

    if not args.allow_missing_vision:
        try:
            get_vision_provider()
        except AIProviderError as exc:
            raise SystemExit(
                f"Vision provider unavailable: {exc}. Set OPENAI_API_KEY before running the multimodal backfill, or rerun with --allow-missing-vision."
            ) from exc

    with SessionLocal() as db:
        query = select(Transcript.id).order_by(Transcript.updated_at.desc())
        if args.transcript_ids:
            transcript_ids = [UUID(value) for value in args.transcript_ids]
            query = query.where(Transcript.id.in_(transcript_ids))
        if not args.all:
            query = query.where(
                or_(
                    ~exists(select(Segment.id).where(Segment.transcript_id == Transcript.id)),
                    exists(
                        select(Segment.id).where(
                            Segment.transcript_id == Transcript.id,
                            Segment.embedding_vector.is_(None),
                        )
                    ),
                    ~exists(select(TranscriptWindow.id).where(TranscriptWindow.transcript_id == Transcript.id)),
                    exists(
                        select(TranscriptWindow.id).where(
                            TranscriptWindow.transcript_id == Transcript.id,
                            TranscriptWindow.embedding_vector.is_(None),
                        )
                    ),
                    ~exists(
                        select(VisualWindowDescription.id).where(VisualWindowDescription.transcript_id == Transcript.id)
                    ),
                    exists(
                        select(VisualWindowDescription.id).where(
                            VisualWindowDescription.transcript_id == Transcript.id,
                            or_(
                                VisualWindowDescription.embedding_vector.is_(None),
                                VisualWindowDescription.status != "ready",
                            ),
                        )
                    ),
                )
            )

        transcript_ids = [str(row[0]) for row in db.execute(query).all()]

    print(f"Found {len(transcript_ids)} transcripts to backfill.")
    if not transcript_ids:
        _print_corpus_summary()
        return

    if args.sync:
        completed = 0
        failed = 0
        for transcript_id in transcript_ids:
            result = index_transcript_job(transcript_id)
            status = result.get("status", "unknown")
            print(f"{transcript_id} -> {status}")
            if status == "completed":
                completed += 1
            else:
                failed += 1

        print(f"Completed: {completed}")
        print(f"Failed: {failed}")
        _print_corpus_summary()
        return

    queued = 0
    queue_failed = 0
    job_ids: list[str] = []
    for transcript_id in transcript_ids:
        job_id = enqueue_indexing(UUID(transcript_id))
        if job_id:
            queued += 1
            job_ids.append(job_id)
            print(f"{transcript_id} -> queued {job_id}")
        else:
            queue_failed += 1
            print(f"{transcript_id} -> queue_failed")

    print(f"Queued: {queued}")
    print(f"Queue failed: {queue_failed}")

    if args.wait and job_ids:
        poll_seconds = max(1, int(args.poll_seconds))
        last_snapshot: dict[str, int] | None = None
        while True:
            snapshot = _poll_job_states(job_ids)
            if snapshot != last_snapshot:
                ordered = ", ".join(f"{status}={count}" for status, count in sorted(snapshot.items())) or "no_jobs=0"
                print(f"Job status: {ordered}")
                last_snapshot = snapshot
            if all(status in TERMINAL_JOB_STATUSES for status in snapshot):
                break
            time.sleep(poll_seconds)

    _print_corpus_summary()


if __name__ == "__main__":
    main()
