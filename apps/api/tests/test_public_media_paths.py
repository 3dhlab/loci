from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.services.public_media_paths import resolve_public_media_file


def test_resolves_regular_file_inside_media_root(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    poster = media_root / "posters" / "public.svg"
    poster.parent.mkdir()
    poster.write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(settings, "media_root", str(media_root))

    assert resolve_public_media_file(str(poster)) == poster.resolve()


def test_rejects_outside_file_and_traversal(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    monkeypatch.setattr(settings, "media_root", str(media_root))

    assert resolve_public_media_file(str(outside)) is None
    assert resolve_public_media_file(str(media_root / ".." / "private.txt")) is None


def test_rejects_symlink_escape_and_non_regular_file(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    link = media_root / "escaped.svg"
    link.symlink_to(outside)
    directory = media_root / "directory"
    directory.mkdir()
    monkeypatch.setattr(settings, "media_root", str(media_root))

    assert resolve_public_media_file(link) is None
    assert resolve_public_media_file(directory) is None
    assert resolve_public_media_file(Path("")) is None
