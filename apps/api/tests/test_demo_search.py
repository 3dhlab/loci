from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints import public
from app.schemas.search import SegmentSearchRequest
from app.services import ai


def test_disabled_embedding_factory_never_constructs_provider(monkeypatch):
    monkeypatch.setattr(ai.settings, 'embedding_enabled', False)
    def unexpected():
        raise AssertionError('Disabled embedding requested a model provider')
    monkeypatch.setattr(ai, 'get_local_embedding_provider', unexpected)
    monkeypatch.setattr(ai, 'get_openai_provider', unexpected)
    with pytest.raises(ai.AIProviderError, match='disabled'):
        ai.get_embedding_provider()
    assert ai.warm_embedding_provider() is None


def test_public_search_uses_lexical_results_when_embeddings_disabled(monkeypatch):
    monkeypatch.setattr(public.settings, 'embedding_enabled', False)
    def unexpected():
        raise AssertionError('Public search requested a disabled embedding provider')
    monkeypatch.setattr(public, 'get_embedding_provider', unexpected)
    monkeypatch.setattr(public, '_attach_visual_result_details', lambda db, rows: None)
    monkeypatch.setattr(public, '_enrich_public_search_results', lambda db, rows: rows)
    segment=SimpleNamespace(id=uuid4(),start_ms=0,end_ms=4000,text='The blue cube.')
    transcript=SimpleNamespace(id=uuid4())
    video=SimpleNamespace(id=uuid4(),title='Demo',project_id=uuid4())
    db=SimpleNamespace(execute=lambda query: SimpleNamespace(all=lambda: [(segment,transcript,video)]))
    result=public.search_public_segments(SegmentSearchRequest(query='cube',retrieval_mode='combined'),db)
    assert result.total_results==1
    assert result.results[0].lexical_match is True
    assert result.results[0].semantic_score is None
