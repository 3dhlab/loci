from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.scripts import export_public_projection


def test_build_public_corpus_phrase_rows_from_projected_public_content(monkeypatch) -> None:
    project_id = uuid4()
    video_id = uuid4()
    transcript_id = uuid4()

    def fake_extract(text: str, _max_phrases: int) -> list[str]:
        lowered = text.lower()
        if "smooth" in lowered:
            return ["smooth handle"]
        if "close view" in lowered:
            return ["close view"]
        return []

    class FakeEmbeddingProvider:
        def embed_texts(self, texts):
            vectors = [[1.0, 0.0] for _ in texts]
            return SimpleNamespace(vectors=vectors)

    monkeypatch.setattr(export_public_projection, "_extract_phrases_yake", fake_extract)
    monkeypatch.setattr(export_public_projection, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    rows = export_public_projection._build_public_corpus_phrase_rows(
        project_rows=[SimpleNamespace(id=project_id, name="Demo Vessel")],
        published_objects=[SimpleNamespace(project_id=project_id, name="Demo Vessel")],
        projected_videos=[SimpleNamespace(id=video_id, project_id=project_id)],
        projected_transcripts=[SimpleNamespace(id=transcript_id, video_id=video_id)],
        segment_rows=[SimpleNamespace(transcript_id=transcript_id, text="The handle is worn smooth.")],
        transcript_window_rows=[SimpleNamespace(transcript_id=transcript_id, embedding_vector=[1.0, 0.0])],
        visual_window_rows=[SimpleNamespace(video_id=video_id, description_text="Close view of the spoon handle")],
    )

    assert {row["source"] for row in rows} == {"entity", "transcript", "visual_description"}
    assert all(row["project_id"] == str(project_id) for row in rows)
    assert all(row["embedding_vector"] == [1.0, 0.0] for row in rows)
    assert all(isinstance(row["corpus_density"], float) for row in rows)