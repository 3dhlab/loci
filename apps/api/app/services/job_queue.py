from __future__ import annotations

import logging
from uuid import UUID

from redis import Redis
from rq import Queue

from app.core.config import settings

logger = logging.getLogger(__name__)


def _redis_conn() -> Redis:
    return Redis.from_url(settings.redis_url)


def _queue(name: str) -> Queue:
    return Queue(name, connection=_redis_conn())


def enqueue_transcode(video_id: UUID) -> str | None:
    try:
        job = _queue("transcode").enqueue(
            "app.worker.jobs.transcode_video_job",
            str(video_id),
            job_timeout=60 * 60,
        )
        return job.id
    except Exception:
        logger.exception("Failed to enqueue transcode job for video_id=%s", video_id)
        return None


def enqueue_transcription(video_id: UUID, language: str = "en") -> str | None:
    try:
        job = _queue("transcription").enqueue("app.worker.jobs.transcribe_video_job", str(video_id), language)
        return job.id
    except Exception:
        logger.exception("Failed to enqueue transcription job for video_id=%s", video_id)
        return None


def enqueue_indexing(transcript_id: UUID) -> str | None:
    try:
        job = _queue("index").enqueue(
            "app.worker.jobs.index_transcript_job",
            str(transcript_id),
            job_timeout=45 * 60,
        )
        return job.id
    except Exception:
        logger.exception("Failed to enqueue index job for transcript_id=%s", transcript_id)
        return None


def enqueue_clip_export(clip_id: UUID) -> str | None:
    try:
        job = _queue("clip").enqueue(
            "app.worker.jobs.export_clip_job",
            str(clip_id),
            job_timeout=30 * 60,
        )
        return job.id
    except Exception:
        logger.exception("Failed to enqueue clip export job for clip_id=%s", clip_id)
        return None


def enqueue_corpus_phrase_build(project_id: UUID) -> str | None:
    try:
        job = _queue("corpus_phrases").enqueue(
            "app.worker.jobs.build_corpus_phrases_job",
            str(project_id),
            job_timeout=30 * 60,
        )
        return job.id
    except Exception:
        logger.exception("Failed to enqueue corpus_phrases job for project_id=%s", project_id)
        return None


def enqueue_title_card_ocr(
    video_id: UUID,
    fixture_path: str | None = None,
    replace_existing: bool = False,
    run_live: bool = False,
) -> str | None:
    try:
        job = _queue("ocr").enqueue(
            "app.worker.jobs.ingest_title_card_ocr_job",
            str(video_id),
            fixture_path,
            replace_existing,
            run_live,
            job_timeout=60 * 60,
        )
        return job.id
    except Exception:
        logger.exception("Failed to enqueue title-card OCR job for video_id=%s", video_id)
        return None
