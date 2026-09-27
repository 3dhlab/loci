from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import settings
from app.models.entities import VideoStatus
from app.services import media_playback_promotion as promotion
from app.services import media_transcode
from app.services.media_transcode import (
    PUBLIC_PLAYBACK_RECIPE_REVISION,
    PlaybackArtifactValidation,
)


SOURCE_SHA = "a" * 64
PLAYBACK_BYTES = b"approved validated normalized playback bytes"
PLAYBACK_SHA = hashlib.sha256(PLAYBACK_BYTES).hexdigest()


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row
        self.new = set()
        self.dirty = set()
        self.deleted = set()
        self.commits = 0
        self.rollbacks = 0
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _Result(self.row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _setup(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    candidate = media_root / "approved-candidate.mp4"
    candidate.write_bytes(PLAYBACK_BYTES)
    monkeypatch.setattr(settings, "media_root", str(media_root))
    monkeypatch.setattr(promotion, "transcode_output_dir", lambda: media_root / "transcoded")
    monkeypatch.setattr(media_transcode, "transcode_output_dir", lambda: media_root / "transcoded")
    video = SimpleNamespace(
        id=uuid4(),
        status=VideoStatus.READY,
        sha256_checksum=SOURCE_SHA,
        title="Preserve source identity",
        source_path=str(media_root / "original.mov"),
        is_published=True,
        transcode_progress_pct=100,
        playback_sha256_checksum="b" * 64,
        playback_file_size_bytes=123,
        playback_recipe_version="prior-recipe",
        playback_color_policy="prior-policy",
        playback_storage_key=None,
    )
    artifact = PlaybackArtifactValidation(
        recipe_revision=PUBLIC_PLAYBACK_RECIPE_REVISION,
        sha256=PLAYBACK_SHA,
        size_bytes=len(PLAYBACK_BYTES),
        width=1280,
        height=720,
        frame_rate="30/1",
        duration_seconds=12,
        keyframe_count=6,
        maximum_keyframe_gap_seconds=2,
        color_policy="sdr-bt709-preserve",
    )
    before = promotion._selector_state(video)
    return media_root, candidate, video, artifact, before


def _promote(session, candidate, artifact, before):
    return promotion.promote_validated_playback(
        session,
        video_id=str(session.row.id),
        expected_source_sha256=SOURCE_SHA,
        expected_before=before,
        candidate_path=candidate,
        artifact=artifact,
        publication_token="c" * 32,
    )


def test_promotion_preserves_original_and_only_selects_validated_fields(tmp_path, monkeypatch):
    media_root, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    original_state = {
        key: getattr(video, key)
        for key in ("status", "sha256_checksum", "title", "source_path", "is_published", "transcode_progress_pct")
    }
    source_file = Path(video.source_path)
    source_file.write_bytes(b"original source bytes")
    session = _Session(video)

    receipt = _promote(session, candidate, artifact, before)

    assert session.statement._execution_options["populate_existing"] is True
    assert session.commits == 1
    assert session.rollbacks == 0
    assert receipt["before"] == before
    assert receipt["after"] == promotion._selector_state(video)
    assert video.playback_sha256_checksum == PLAYBACK_SHA
    assert video.playback_file_size_bytes == len(PLAYBACK_BYTES)
    assert video.playback_recipe_version == PUBLIC_PLAYBACK_RECIPE_REVISION
    assert video.playback_color_policy == "sdr-bt709-preserve"
    published = media_root / "transcoded" / video.playback_storage_key
    assert published.read_bytes() == PLAYBACK_BYTES
    assert published.stat().st_mode & 0o222 == 0
    assert source_file.read_bytes() == b"original source bytes"
    assert candidate.exists()
    for field, value in original_state.items():
        assert getattr(video, field) == value

    restored = promotion.rollback_validated_playback(session, receipt)
    assert session.commits == 2
    assert restored["before"] == receipt["after"]
    assert restored["after"] == before
    assert promotion._selector_state(video) == before
    assert published.exists()  # immutable bytes remain available to existing readers


def test_promotion_rejects_stale_selector_snapshot(tmp_path, monkeypatch):
    _, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    video.playback_color_policy = "changed-after-review"
    session = _Session(video)

    with pytest.raises(promotion.PlaybackPromotionRejected, match="playback_selector_changed"):
        _promote(session, candidate, artifact, before)

    assert session.commits == 0
    assert list((tmp_path / "media" / "transcoded").glob("*.mp4")) == []


def test_promotion_rejects_source_checksum_change(tmp_path, monkeypatch):
    _, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    video.sha256_checksum = "d" * 64
    session = _Session(video)

    with pytest.raises(promotion.PlaybackPromotionRejected, match="source_checksum_changed"):
        _promote(session, candidate, artifact, before)

    assert session.commits == 0
    assert list((tmp_path / "media" / "transcoded").glob("*.mp4")) == []


def test_promotion_rejects_candidate_hash_mismatch(tmp_path, monkeypatch):
    _, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    bad_artifact = PlaybackArtifactValidation(**{**artifact.__dict__, "sha256": "e" * 64})
    session = _Session(video)

    with pytest.raises(promotion.PlaybackPromotionRejected, match="candidate_identity_mismatch"):
        _promote(session, candidate, bad_artifact, before)

    assert session.commits == 0
    assert session.row.playback_storage_key is None


def test_rollback_rejects_conflicting_later_selection(tmp_path, monkeypatch):
    _, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    session = _Session(video)
    receipt = _promote(session, candidate, artifact, before)
    video.playback_storage_key = "later-selection.mp4"

    with pytest.raises(promotion.PlaybackPromotionRejected, match="playback_selector_changed_since_promotion"):
        promotion.rollback_validated_playback(session, receipt)

    assert session.commits == 1
    assert video.playback_storage_key == "later-selection.mp4"


def test_promotion_rejects_unrelated_pending_session_changes(tmp_path, monkeypatch):
    _, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    session = _Session(video)
    session.dirty.add(object())

    with pytest.raises(promotion.PlaybackPromotionRejected, match="session_has_pending_changes"):
        _promote(session, candidate, artifact, before)

    assert session.statement is None
    assert session.commits == 0


def test_corrupted_copy_is_rejected_and_only_new_output_is_removed(tmp_path, monkeypatch):
    media_root, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    session = _Session(video)
    monkeypatch.setattr(promotion.shutil, "copyfileobj", lambda source, destination, length: destination.write(b"corrupted copy"))

    with pytest.raises(promotion.PlaybackPromotionRejected, match="copied_candidate_identity_mismatch"):
        _promote(session, candidate, artifact, before)

    assert session.commits == 0
    assert session.rollbacks == 1
    assert promotion._selector_state(video) == before
    assert list((media_root / "transcoded").glob("*.mp4")) == []
    assert candidate.read_bytes() == PLAYBACK_BYTES


def test_publication_collision_preserves_existing_immutable_file(tmp_path, monkeypatch):
    media_root, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    output = media_transcode.versioned_transcode_output_path(str(video.id), artifact.sha256, "c" * 32)
    output.parent.mkdir()
    output.write_bytes(b"existing immutable bytes")
    session = _Session(video)

    with pytest.raises(FileExistsError):
        _promote(session, candidate, artifact, before)

    assert session.commits == 0
    assert output.read_bytes() == b"existing immutable bytes"
    assert promotion._selector_state(video) == before


def test_ambiguous_commit_keeps_published_bytes_for_possible_committed_reader(tmp_path, monkeypatch):
    media_root, candidate, video, artifact, before = _setup(tmp_path, monkeypatch)
    session = _Session(video)

    def fail_commit():
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit acknowledgement lost"):
        _promote(session, candidate, artifact, before)

    assert session.rollbacks == 1
    outputs = list((media_root / "transcoded").glob("*.mp4"))
    assert len(outputs) == 1
    assert outputs[0].read_bytes() == PLAYBACK_BYTES
