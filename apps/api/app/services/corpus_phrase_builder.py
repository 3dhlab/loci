from __future__ import annotations

import logging
import math
import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.entities import (
    CorpusPhrase,
    Object,
    Project,
    SearchQueryLog,
    Segment,
    Transcript,
    TranscriptWindow,
    Video,
    VisualWindowDescription,
)
from app.services.ai import AIProviderError, get_embedding_provider

logger = logging.getLogger(__name__)

MIN_PHRASE_CHARS = 3
MAX_PHRASE_CHARS = 60
PHRASES_PER_RECORD = 5
DENSITY_REFERENCE_WINDOW_COUNT = 10
QUERY_LOG_SCORE_FLOOR = Decimal("0.68")


def normalize_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _extract_phrases_yake(text: str, max_phrases: int) -> list[str]:
    if not text or not text.strip():
        return []
    try:
        import yake  # type: ignore
    except ImportError:
        return _fallback_bigram_phrases(text, max_phrases)
    try:
        extractor = yake.KeywordExtractor(lan="en", n=3, top=max_phrases)
        keywords = extractor.extract_keywords(text)
    except Exception:
        logger.exception("yake extraction failed; falling back to bigrams")
        return _fallback_bigram_phrases(text, max_phrases)
    return [phrase for phrase, _score in keywords]


def _fallback_bigram_phrases(text: str, max_phrases: int) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", text)
    seen: set[str] = set()
    phrases: list[str] = []
    for i in range(len(words) - 1):
        bigram = " ".join([words[i], words[i + 1]]).lower()
        if bigram not in seen:
            seen.add(bigram)
            phrases.append(bigram)
            if len(phrases) >= max_phrases:
                break
    return phrases


def _phrase_is_useful(phrase: str | None) -> bool:
    if not phrase:
        return False
    if len(phrase) < MIN_PHRASE_CHARS or len(phrase) > MAX_PHRASE_CHARS:
        return False
    if not re.search(r"[A-Za-z]", phrase):
        return False
    return True


def build_corpus_phrases_for_project(project_id: UUID) -> dict[str, int]:
    db: Session = SessionLocal()
    try:
        return _build(db, project_id)
    finally:
        db.close()


def _build(db: Session, project_id: UUID) -> dict[str, int]:
    project = db.get(Project, project_id)
    if project is None:
        return {"transcript": 0, "visual_description": 0, "past_query": 0, "entity": 0}

    records: list[dict] = []

    # transcript segments
    segments = (
        db.execute(
            select(Segment.text)
            .join(Transcript, Segment.transcript_id == Transcript.id)
            .join(Video, Transcript.video_id == Video.id)
            .where(Video.project_id == project_id)
        )
        .scalars()
        .all()
    )
    for text in segments:
        for phrase in _extract_phrases_yake(text or "", PHRASES_PER_RECORD):
            if _phrase_is_useful(phrase):
                records.append({"phrase": phrase, "source": "transcript"})

    # visual descriptions
    descriptions = (
        db.execute(
            select(VisualWindowDescription.description_text)
            .join(Video, VisualWindowDescription.video_id == Video.id)
            .where(Video.project_id == project_id)
        )
        .scalars()
        .all()
    )
    for text in descriptions:
        for phrase in _extract_phrases_yake(text or "", PHRASES_PER_RECORD):
            if _phrase_is_useful(phrase):
                records.append({"phrase": phrase, "source": "visual_description"})

    # past successful queries
    past_queries = (
        db.execute(
            select(SearchQueryLog.query_text)
            .where(SearchQueryLog.project_id == project_id)
            .where(SearchQueryLog.top_semantic_score >= QUERY_LOG_SCORE_FLOOR)
        )
        .scalars()
        .all()
    )
    for query_text in past_queries:
        phrase = (query_text or "").strip()
        if _phrase_is_useful(phrase):
            records.append({"phrase": phrase, "source": "past_query"})

    # entity: object names + project name
    object_names = (
        db.execute(select(Object.name).where(Object.project_id == project_id))
        .scalars()
        .all()
    )
    for name in object_names:
        candidate = (name or "").strip()
        if _phrase_is_useful(candidate):
            records.append({"phrase": candidate, "source": "entity"})
    if project.name:
        candidate = project.name.strip()
        if _phrase_is_useful(candidate):
            records.append({"phrase": candidate, "source": "entity"})

    # aggregate
    aggregated: dict[tuple[str, str], dict] = {}
    for rec in records:
        normalized = normalize_phrase(rec["phrase"])
        if not normalized:
            continue
        key = (normalized, rec["source"])
        if key in aggregated:
            aggregated[key]["source_count"] += 1
        else:
            aggregated[key] = {
                "project_id": project_id,
                "phrase_text": rec["phrase"].strip(),
                "normalized_phrase": normalized,
                "source": rec["source"],
                "source_count": 1,
            }

    if not aggregated:
        return {"transcript": 0, "visual_description": 0, "past_query": 0, "entity": 0}

    phrases_to_write = list(aggregated.values())

    # embeddings
    provider = get_embedding_provider()
    texts = [entry["phrase_text"] for entry in phrases_to_write]
    try:
        result = provider.embed_texts(texts)
        embeddings = list(result.vectors)
    except AIProviderError:
        logger.exception("Embedding failed; proceeding with null embeddings")
        embeddings = [None] * len(texts)

    # reference windows for density
    reference_vecs = (
        db.execute(
            select(TranscriptWindow.embedding_vector)
            .join(Transcript, TranscriptWindow.transcript_id == Transcript.id)
            .join(Video, Transcript.video_id == Video.id)
            .where(Video.project_id == project_id)
            .where(TranscriptWindow.embedding_vector.is_not(None))
            .limit(DENSITY_REFERENCE_WINDOW_COUNT)
        )
        .scalars()
        .all()
    )

    for entry, embedding in zip(phrases_to_write, embeddings):
        entry["embedding_vector"] = list(embedding) if embedding is not None else None
        entry["corpus_density"] = _compute_density(embedding, reference_vecs)

    stmt = pg_insert(CorpusPhrase).values(phrases_to_write)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_corpus_phrases_project_phrase_source",
        set_={
            "phrase_text": stmt.excluded.phrase_text,
            "embedding_vector": stmt.excluded.embedding_vector,
            "source_count": stmt.excluded.source_count,
            "corpus_density": stmt.excluded.corpus_density,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    db.commit()

    counts = {"transcript": 0, "visual_description": 0, "past_query": 0, "entity": 0}
    for (_normalized, source) in aggregated.keys():
        counts[source] = counts.get(source, 0) + 1
    return counts


def _compute_density(embedding, reference_vecs) -> Decimal:
    if embedding is None or not reference_vecs:
        return Decimal("0")
    sims: list[float] = []
    for ref in reference_vecs:
        if ref is None:
            continue
        sim = _cosine(embedding, ref)
        if sim is not None:
            sims.append(sim)
    if not sims:
        return Decimal("0")
    return Decimal(str(round(sum(sims) / len(sims), 4)))


def _cosine(a, b) -> float | None:
    dot = 0.0
    mag_a = 0.0
    mag_b = 0.0
    count = 0
    for x, y in zip(a, b):
        xf = float(x)
        yf = float(y)
        dot += xf * yf
        mag_a += xf * xf
        mag_b += yf * yf
        count += 1
    if count == 0 or mag_a == 0 or mag_b == 0:
        return None
    return dot / (math.sqrt(mag_a) * math.sqrt(mag_b))
