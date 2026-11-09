"""Bounded Pillow dependency checks; Loci uses FFmpeg for its image assets.

The shared-tag fixture retains at most 512 bytes on an older Pillow release.
All images are generated in memory and every operation runs locally.
"""
from io import BytesIO
import struct
from types import SimpleNamespace
import warnings

from PIL import Image, ImageOps, TiffImagePlugin, UnidentifiedImageError, features
import pytest


def test_shared_ifd_values_have_a_file_sized_budget() -> None:
    tag_count = 8
    value_size = 64
    header = b"II" + struct.pack("<HL", 42, 8)
    data_offset = len(header) + 2 + 12 * tag_count + 4
    entries = b"".join(
        struct.pack("<HHLL", 65000 + index, 7, value_size, data_offset)
        for index in range(tag_count)
    )
    payload = (
        header + struct.pack("<H", tag_count) + entries
        + struct.pack("<L", 0) + b"x" * value_size
    )
    directory = TiffImagePlugin.ImageFileDirectory_v2(header)
    stream = BytesIO(payload)
    stream.seek(8)

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        directory.load(stream)

    assert directory[65000] == b"x" * value_size
    assert sum(len(directory[tag]) for tag in directory) <= len(payload)


def test_jpeg_exif_orientation_survives_decode() -> None:
    source = Image.new("RGB", (12, 8), (180, 40, 20))
    exif = Image.Exif()
    exif[274] = 6
    stream = BytesIO()
    source.save(stream, format="JPEG", exif=exif)
    stream.seek(0)

    with Image.open(stream) as decoded:
        oriented = ImageOps.exif_transpose(decoded)
        assert oriented.size == (8, 12)
        assert oriented.getexif().get(274) is None
        assert oriented.mode == "RGB"


def test_png_thumbnail_preserves_transparency() -> None:
    stream = BytesIO()
    Image.new("RGBA", (16, 8), (20, 80, 140, 0)).save(stream, format="PNG")
    stream.seek(0)

    with Image.open(stream) as decoded:
        decoded.thumbnail((4, 4))
        assert decoded.size == (4, 2)
        assert decoded.getchannel("A").getextrema() == (0, 0)
        output = BytesIO()
        decoded.save(output, format="PNG")
    output.seek(0)
    with Image.open(output) as restored:
        assert restored.mode == "RGBA"
        assert restored.getpixel((0, 0))[3] == 0


def test_benign_jpeg2000_roundtrip_uses_the_installed_codec() -> None:
    assert features.check_codec("jpg_2000")
    source = Image.new("RGB", (4, 4), (12, 34, 56))
    stream = BytesIO()
    source.save(stream, format="JPEG2000", irreversible=False)
    stream.seek(0)
    with Image.open(stream) as decoded:
        decoded.load()
        assert decoded.format == "JPEG2000"
        assert decoded.size == source.size
        assert decoded.tobytes() == source.tobytes()


@pytest.mark.parametrize(
    "payload", [b"ordinary invalid image", b"\x89PNG\r\n\x1a\n" + b"\0" * 12],
)
def test_invalid_image_has_a_bounded_error(payload: bytes) -> None:
    with pytest.raises((UnidentifiedImageError, OSError)):
        with Image.open(BytesIO(payload)) as image:
            image.load()


@pytest.mark.parametrize("extension", ["png", "jpg", "tiff", "jp2"])
def test_media_upload_rejects_still_images_before_decode(
    extension: str, monkeypatch,
) -> None:
    from fastapi import HTTPException, UploadFile
    from app.api.v1.endpoints.videos import upload_media_file

    def unexpected_decode(*args, **kwargs):
        pytest.fail("Rejected still-image upload reached Pillow")

    monkeypatch.setattr(Image, "open", unexpected_decode)
    upload = UploadFile(filename=f"generated.{extension}", file=BytesIO(b"x"))
    with pytest.raises(HTTPException) as error:
        upload_media_file(media_file=upload, current_user=SimpleNamespace())
    assert error.value.status_code == 400
    assert error.value.detail == "Unsupported media file type"
