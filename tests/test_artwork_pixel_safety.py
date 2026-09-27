from __future__ import annotations

import struct
import zlib

import pytest
from PIL import Image

from tankarr.metadata.service import ArtworkStore


def oversized_png(width: int, height: int) -> bytes:
    """A tiny hostile header, with no large raster allocation in the test."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("dimensions", [(10_000, 10_000), (30_000, 30_000)])
def test_artwork_rejects_pixel_bomb_header_without_disabling_other_decoders(
    monkeypatch, dimensions
):
    pixel_limit = Image.MAX_IMAGE_PIXELS
    open_image = Image.open

    def guarded_open(*args, **kwargs):
        assert Image.MAX_IMAGE_PIXELS == pixel_limit
        return open_image(*args, **kwargs)

    monkeypatch.setattr(Image, "open", guarded_open)
    with pytest.raises(ValueError, match="decoded safely"):
        ArtworkStore._prepare_image(oversized_png(*dimensions))
    assert Image.MAX_IMAGE_PIXELS == pixel_limit
