from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.entities import TitleCardObservation, Video
from app.scripts import import_title_card_ocr as import_title_card_ocr_script
from app.services import job_queue
from app.services.title_card_ocr import import_title_card_observation_batch, load_title_card_import_batch, title_card_sample_timestamps
from app.worker import jobs


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "title_card_ocr_fixture.json"


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDBSession:
    def __init__(self, existing_rows=None):
        self.rows = list(existing_rows or [])

    def execute(self, statement):
        if getattr(statement, "is_delete", False):
            delete_ids = set()
            for value in statement.compile().params.values():
                if isinstance(value, (list, tuple, set)):
                    delete_ids.update(value)
                elif value is not None:
                    delete_ids.add(value)
            self.rows = [row for row in self.rows if row.id not in delete_ids]
            return _ScalarResult([])
        return _ScalarResult(self.rows)

    def add(self, row):
        if row not in self.rows:
            self.rows.append(row)


class FakeWorkerSession:
    def __init__(self, video: Video | None):
        self.video = video
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, model, key):
        if model is Video:
            return self.video
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def add(self, _row):
        return None


def _build_video() -> Video:
    return Video(
        id=uuid4(),
        project_id=uuid4(),
        object_id=None,
        stable_video_id="video-title-card-fixture",
        title="Demo Vessel",
        original_filename="spoon.mp4",
        source_path="/tmp/spoon.mp4",
        sha256_checksum="0" * 64,
        duration_ms=12000,
    )


def _build_existing_observation(
    video: Video,
    *,
    timestamp_ms: int,
    prompt_version: str = "2026-04-24-v1",
    source_kind: str = "fixture_import",
    source_version: str = "structured-json-v1",
    raw_text_observed: str = "existing row",
) -> TitleCardObservation:
    row = TitleCardObservation(
        video_id=video.id,
        timestamp_ms=timestamp_ms,
        title_card_visible=True,
        raw_text_observed=raw_text_observed,
        confidence_object="none",
        confidence_presenter="none",
        confidence_session_date="none",
        prompt_version=prompt_version,
        model_provider="fixture",
        model_name="fixture-title-card-ocr",
        detail="high",
        source_kind=source_kind,
        source_version=source_version,
        status="ready",
    )
    row.id = uuid4()
    return row


def test_title_card_sampling_supports_five_second_cadence() -> None:
    assert title_card_sample_timestamps(16000, cadence_seconds=5) == [0, 5000, 10000, 15000]


def test_import_title_card_fixture_writes_observations() -> None:
    batch = load_title_card_import_batch(FIXTURE_PATH)
    video = _build_video()
    db = FakeDBSession()

    summary = import_title_card_observation_batch(db, video=video, batch=batch, replace_existing=True)
    rows = sorted(db.rows, key=lambda row: row.timestamp_ms)

    assert summary["observations_written"] == 2
    assert summary["inserted"] == 2
    assert summary["updated"] == 0
    assert summary["prompt_versions"] == ["2026-04-24-v1"]
    assert len(rows) == 2
    assert rows[0].source_kind == "fixture_import"
    assert rows[0].source_version == "structured-json-v1"
    assert rows[0].session_date_text == "February 14, 2025"
    assert rows[0].session_date.isoformat() == "2025-02-14"
    assert rows[0].presenter_name == "Synthetic Presenter Alpha"
    assert rows[0].detail == "high"
    assert rows[0].public_speaker_label is None
    assert rows[0].model_provider == "fixture"
    assert rows[0].raw_text_observed == "Synthetic Presenter Alpha\n42\nExample Village\nFebruary 14, 2025"
    assert rows[0].raw_payload_json["object"]["accession_number"] == "DEMO-001"
    assert rows[1].session_date_text == "early February 2025"
    assert rows[1].session_date is None
    assert rows[1].presenter_name == "Synthetic Presenter Beta"
    assert rows[1].presenter_age is None
    assert rows[1].confidence_presenter == "medium"


def test_import_title_card_fixture_updates_existing_observation() -> None:
    batch = load_title_card_import_batch(FIXTURE_PATH)
    video = _build_video()
    existing = _build_existing_observation(video, timestamp_ms=0, raw_text_observed="outdated")
    db = FakeDBSession(existing_rows=[existing])

    summary = import_title_card_observation_batch(db, video=video, batch=batch, replace_existing=False)

    assert summary["inserted"] == 1
    assert summary["updated"] == 1
    refreshed = next(row for row in db.rows if row.timestamp_ms == 0)
    assert refreshed.raw_text_observed == "Synthetic Presenter Alpha\n42\nExample Village\nFebruary 14, 2025"


def test_import_title_card_fixture_replace_existing_only_removes_matching_source_scope() -> None:
    batch = load_title_card_import_batch(FIXTURE_PATH)
    video = _build_video()
    stale_matching_scope = _build_existing_observation(video, timestamp_ms=2500)
    preserved_other_source = _build_existing_observation(
        video,
        timestamp_ms=2500,
        source_kind="pilot_artifact",
        source_version="speaker-approved-v1",
    )
    preserved_other_prompt = _build_existing_observation(
        video,
        timestamp_ms=2500,
        prompt_version="2026-05-01-v1",
    )
    db = FakeDBSession(existing_rows=[stale_matching_scope, preserved_other_source, preserved_other_prompt])

    summary = import_title_card_observation_batch(db, video=video, batch=batch, replace_existing=True)
    remaining_ids = {row.id for row in db.rows}

    assert summary["removed"] == 1
    assert stale_matching_scope.id not in remaining_ids
    assert preserved_other_source.id in remaining_ids
    assert preserved_other_prompt.id in remaining_ids


def test_ingest_title_card_ocr_job_requires_explicit_live_flag_without_fixture(monkeypatch) -> None:
    video = _build_video()
    session = FakeWorkerSession(video)

    monkeypatch.setattr(jobs, "SessionLocal", lambda: session)
    monkeypatch.setattr(jobs, "_owner_id_for_video", lambda db, project_id: uuid4())
    monkeypatch.setattr(
        jobs,
        "get_vision_provider",
        lambda: (_ for _ in ()).throw(AssertionError("live OCR provider should not be requested")),
    )

    result = jobs.ingest_title_card_ocr_job(str(video.id))

    assert result == {"status": "live_ocr_not_requested", "video_id": str(video.id)}
    assert session.committed is False
    assert session.rolled_back is False


def test_ingest_title_card_ocr_job_fixture_import_still_completes_without_live(monkeypatch) -> None:
    video = _build_video()
    session = FakeWorkerSession(video)
    model_run_id = uuid4()
    batch = SimpleNamespace(
        model_provider="fixture",
        model_name="fixture-import",
        detail="high",
        prompt_version="2026-04-24-v1",
        source_kind="fixture_import",
        source_version="structured-json-v1",
        observations=[SimpleNamespace(timestamp_ms=0)],
    )

    monkeypatch.setattr(jobs, "SessionLocal", lambda: session)
    monkeypatch.setattr(jobs, "_owner_id_for_video", lambda db, project_id: uuid4())
    monkeypatch.setattr(jobs, "load_title_card_import_batch", lambda path: batch)
    monkeypatch.setattr(jobs, "_log_model_run", lambda *args, **kwargs: SimpleNamespace(id=model_run_id))
    monkeypatch.setattr(
        jobs,
        "import_title_card_observation_batch",
        lambda *args, **kwargs: {
            "observations_written": 1,
            "inserted": 1,
            "updated": 0,
            "removed": 0,
            "prompt_versions": ["2026-04-24-v1"],
        },
    )
    monkeypatch.setattr(
        jobs,
        "get_vision_provider",
        lambda: (_ for _ in ()).throw(AssertionError("live OCR provider should not be requested")),
    )

    result = jobs.ingest_title_card_ocr_job(str(video.id), fixture_path=str(FIXTURE_PATH), run_live=False)

    assert result == {
        "status": "completed",
        "mode": "fixture_import",
        "video_id": str(video.id),
        "model_run_id": str(model_run_id),
        "observations_written": 1,
        "inserted": 1,
        "updated": 0,
        "removed": 0,
        "prompt_versions": ["2026-04-24-v1"],
    }
    assert session.committed is True


def test_enqueue_title_card_ocr_defaults_to_non_live(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(id="job-ocr-default")

    monkeypatch.setattr(job_queue, "_queue", lambda name: FakeQueue())

    job_id = job_queue.enqueue_title_card_ocr(uuid4())

    assert job_id == "job-ocr-default"
    assert calls == [
        (
            ("app.worker.jobs.ingest_title_card_ocr_job", calls[0][0][1], None, False, False),
            {"job_timeout": 60 * 60},
        )
    ]


def test_enqueue_title_card_ocr_can_request_live(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(id="job-ocr-live")

    monkeypatch.setattr(job_queue, "_queue", lambda name: FakeQueue())

    job_id = job_queue.enqueue_title_card_ocr(uuid4(), run_live=True)

    assert job_id == "job-ocr-live"
    assert calls == [
        (
            ("app.worker.jobs.ingest_title_card_ocr_job", calls[0][0][1], None, False, True),
            {"job_timeout": 60 * 60},
        )
    ]


def test_import_title_card_ocr_cli_passes_run_live_only_for_live(monkeypatch, capsys) -> None:
    seen_calls: list[dict] = []

    def fake_ingest(video_id, *, fixture_path=None, replace_existing=False, run_live=False):
        seen_calls.append(
            {
                "video_id": video_id,
                "fixture_path": fixture_path,
                "replace_existing": replace_existing,
                "run_live": run_live,
            }
        )
        return {"status": "completed", "video_id": video_id}

    monkeypatch.setattr(import_title_card_ocr_script, "ingest_title_card_ocr_job", fake_ingest)

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_title_card_ocr", "--video-id", "video-fixture", "--fixture", str(FIXTURE_PATH)],
    )
    import_title_card_ocr_script.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_title_card_ocr",
            "--video-id",
            "video-fixture-replace",
            "--fixture",
            str(FIXTURE_PATH),
            "--replace-matching-source",
        ],
    )
    import_title_card_ocr_script.main()

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_title_card_ocr", "--video-id", "video-live", "--live"],
    )
    import_title_card_ocr_script.main()

    assert seen_calls == [
        {
            "video_id": "video-fixture",
            "fixture_path": str(FIXTURE_PATH),
            "replace_existing": False,
            "run_live": False,
        },
        {
            "video_id": "video-fixture-replace",
            "fixture_path": str(FIXTURE_PATH),
            "replace_existing": True,
            "run_live": False,
        },
        {
            "video_id": "video-live",
            "fixture_path": None,
            "replace_existing": False,
            "run_live": True,
        },
    ]
    captured = capsys.readouterr()
    assert '"status": "completed"' in captured.out


def test_import_title_card_ocr_cli_help_describes_scoped_replacement(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["import_title_card_ocr", "--help"])

    with pytest.raises(SystemExit):
        import_title_card_ocr_script.main()

    captured = capsys.readouterr()
    normalized_help = " ".join(captured.out.split())

    assert "--replace-matching-source" in normalized_help
    assert "prompt version, source kind, and source version match the imported batch" in normalized_help
    assert "clear all title-card observations for the video" in normalized_help
    assert "--replace-existing" not in normalized_help