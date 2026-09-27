from __future__ import annotations

import io
import struct
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.object_models import _commit_model_upload
from app.services.upload_validation import (
    UploadTooLargeError,
    UploadValidationError,
    persist_bounded_upload,
    validate_glb_v2,
)


def glb_bytes(*, body: bytes = b"") -> bytes:
    total = 12 + len(body)
    return struct.pack("<4sII", b"glTF", 2, total) + body


def test_bounded_upload_accepts_exact_limit_and_creates_exclusively(tmp_path) -> None:
    destination = tmp_path / "accepted.bin"

    written = persist_bounded_upload(io.BytesIO(b"abcd"), destination, max_bytes=4)

    assert written == 4
    assert destination.read_bytes() == b"abcd"
    with pytest.raises(FileExistsError):
        persist_bounded_upload(io.BytesIO(b"replacement"), destination, max_bytes=20)
    assert destination.read_bytes() == b"abcd"


def test_bounded_upload_removes_partial_file_after_limit_failure(tmp_path) -> None:
    destination = tmp_path / "partial.bin"

    with pytest.raises(UploadTooLargeError):
        persist_bounded_upload(io.BytesIO(b"abcde"), destination, max_bytes=4)

    assert not destination.exists()


def test_bounded_upload_rejects_empty_files_and_cleans_up(tmp_path) -> None:
    destination = tmp_path / "empty.bin"

    with pytest.raises(UploadValidationError):
        persist_bounded_upload(io.BytesIO(b""), destination, max_bytes=4)

    assert not destination.exists()


def test_glb_validation_accepts_binary_gltf_v2(tmp_path) -> None:
    payload = glb_bytes(body=b"payload")
    path = tmp_path / "valid.glb"
    path.write_bytes(payload)

    validate_glb_v2(path, expected_size_bytes=len(payload))


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-glb",
        struct.pack("<4sII", b"glTF", 1, 12),
        struct.pack("<4sII", b"BAD!", 2, 12),
        struct.pack("<4sII", b"glTF", 2, 999),
    ],
)
def test_glb_validation_rejects_invalid_container_identity(tmp_path, payload: bytes) -> None:
    path = tmp_path / "invalid.glb"
    path.write_bytes(payload)

    with pytest.raises(UploadValidationError):
        validate_glb_v2(path, expected_size_bytes=len(payload))


def test_model_upload_commit_failure_rolls_back_and_removes_new_file(tmp_path) -> None:
    destination = tmp_path / "rev-2-new.glb"
    destination.write_bytes(glb_bytes())
    rollbacks: list[bool] = []

    def _commit() -> None:
        raise RuntimeError("database commit failed")

    db = SimpleNamespace(
        commit=_commit,
        refresh=lambda _model: pytest.fail("refresh must follow a successful commit"),
        rollback=lambda: rollbacks.append(True),
    )

    with pytest.raises(RuntimeError, match="database commit failed"):
        _commit_model_upload(db, object(), destination)

    assert rollbacks == [True]
    assert destination.exists() is False


def test_model_upload_commit_success_retains_new_file(tmp_path) -> None:
    destination = tmp_path / "rev-2-new.glb"
    destination.write_bytes(glb_bytes())
    calls: list[str] = []
    model = object()
    db = SimpleNamespace(
        commit=lambda: calls.append("commit"),
        refresh=lambda value: calls.append("refresh") if value is model else None,
        rollback=lambda: pytest.fail("rollback must not follow a successful commit"),
    )

    _commit_model_upload(db, model, destination)

    assert calls == ["commit", "refresh"]
    assert destination.exists() is True


def test_model_upload_refresh_failure_retains_committed_file(tmp_path) -> None:
    destination = tmp_path / "rev-2-committed.glb"
    destination.write_bytes(glb_bytes())
    rollbacks: list[bool] = []
    db = SimpleNamespace(
        commit=lambda: None,
        refresh=lambda _model: (_ for _ in ()).throw(RuntimeError("refresh failed")),
        rollback=lambda: rollbacks.append(True),
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        _commit_model_upload(db, object(), destination)

    assert rollbacks == []
    assert destination.exists() is True
