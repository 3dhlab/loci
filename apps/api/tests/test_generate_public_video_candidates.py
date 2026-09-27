from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.scripts import generate_public_video_candidates as candidates


def _row(video_id: str = "11111111-1111-1111-1111-111111111111") -> dict[str, str]:
    return {
        "public_object_id": "published-object",
        "video_id": video_id,
        "source_filename": f"{video_id}.mp4",
        "source_sha256": "a" * 64,
    }


def _manifest(path: Path, rows: list[dict[str, str]]) -> Path:
    path.write_text(json.dumps({"version": 1, "videos": rows}), encoding="utf-8")
    return path


def _args(tmp_path: Path, manifest: Path, *, execute: bool = False, retry: list[str] | None = None):
    return argparse.Namespace(
        manifest=manifest,
        source_root=tmp_path / "sources",
        output_root=tmp_path / "output",
        execute=execute,
        retry=retry,
        minimum_free_bytes=0,
    )


def test_manifest_rejects_nested_source_path(tmp_path: Path) -> None:
    row = _row()
    row["source_filename"] = "private/source.mp4"

    with pytest.raises(candidates.CandidateBatchError, match="input_manifest_source_name_invalid"):
        candidates._read_input_manifest(_manifest(tmp_path / "manifest.json", [row]))


def test_confined_source_rejects_symlink(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    (source_root / "linked.mp4").symlink_to(outside)

    with pytest.raises(candidates.CandidateBatchError, match="source_file_unavailable"):
        candidates._confined_source(source_root, "linked.mp4")


def test_dry_run_is_default_and_writes_resumable_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [_row()])
    monkeypatch.setattr(candidates, "require_public_playback_ffmpeg_runtime", lambda: {"validated": True})
    monkeypatch.setattr(
        candidates,
        "_generate_one",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not generate media"),
    )

    assert candidates.run_batch(_args(tmp_path, manifest)) == 0
    result = json.loads((tmp_path / "output" / "VIDEO-CANDIDATE-RESULTS.json").read_text())
    assert result["mode"] == "dry-run"
    assert result["workers"] == 1
    assert result["videos"][0]["status"] == "planned"


def test_execute_isolates_failure_and_persists_each_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _row()
    second = _row("22222222-2222-2222-2222-222222222222")
    manifest = _manifest(tmp_path / "manifest.json", [first, second])
    monkeypatch.setattr(candidates, "require_public_playback_ffmpeg_runtime", lambda: {"validated": True})

    def generate(row, *_args):
        if row["video_id"] == first["video_id"]:
            raise candidates.CandidateBatchError("encode_failed")
        return {**row, "status": "completed"}

    monkeypatch.setattr(candidates, "_generate_one", generate)

    assert candidates.run_batch(_args(tmp_path, manifest, execute=True)) == 1
    result = json.loads((tmp_path / "output" / "VIDEO-CANDIDATE-RESULTS.json").read_text())
    statuses = {row["video_id"]: row["status"] for row in result["videos"]}
    assert statuses == {first["video_id"]: "failed", second["video_id"]: "completed"}


def test_retry_rejects_unknown_video_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [_row()])
    monkeypatch.setattr(candidates, "require_public_playback_ffmpeg_runtime", lambda: {"validated": True})

    with pytest.raises(candidates.CandidateBatchError, match="retry_video_unknown"):
        candidates.run_batch(
            _args(
                tmp_path,
                manifest,
                retry=["33333333-3333-3333-3333-333333333333"],
            )
        )
