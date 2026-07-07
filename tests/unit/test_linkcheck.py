"""Tests for the image sniffers backing the catalog-icon rule (VGI015)."""

import struct

from vgi_lint_check.linkcheck import is_broken, sniff_image


def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0d" + b"IHDR" + struct.pack(">II", w, h)


def _gif(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h)


def _bmp(w, h):
    return b"BM" + b"\x00" * 16 + struct.pack("<ii", w, h)


def _jpeg(w, h):
    # SOF0 frame: ff d8 | ff c0 | len | precision | height | width
    return b"\xff\xd8" + b"\xff\xc0" + b"\x00\x11" + b"\x08" + struct.pack(">HH", h, w)


def _webp_vp8x(w, h):
    return (
        b"RIFF"
        + b"\x00\x00\x00\x00"
        + b"WEBP"
        + b"VP8X"
        + b"\x0a\x00\x00\x00"
        + b"\x00"
        + b"\x00\x00\x00"
        + (w - 1).to_bytes(3, "little")
        + (h - 1).to_bytes(3, "little")
    )


def _ico(w, h):
    return b"\x00\x00\x01\x00" + b"\x01\x00" + bytes([w % 256]) + bytes([h % 256])


def test_raster_sniffers_read_dimensions():
    assert sniff_image(_png(256, 128), None) == ("png", 256, 128)
    assert sniff_image(_gif(48, 64), None) == ("gif", 48, 64)
    assert sniff_image(_bmp(200, 100), None) == ("bmp", 200, 100)
    assert sniff_image(_jpeg(640, 480), None) == ("jpeg", 640, 480)
    assert sniff_image(_webp_vp8x(512, 512), None) == ("webp", 512, 512)
    # ICO encodes 0 as 256.
    assert sniff_image(_ico(0, 0), None) == ("ico", 256, 256)


def test_svg_detected_without_pixel_dimensions():
    assert sniff_image(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', None) == (
        "svg",
        None,
        None,
    )
    # content-type is enough even when the body opens with a comment/doctype.
    assert sniff_image(b"<!-- logo -->\n<foo/>", "image/svg+xml; charset=utf-8") == (
        "svg",
        None,
        None,
    )


def test_non_image_bytes_are_unrecognized():
    assert sniff_image(b"<!doctype html><html>nope</html>", "text/html") == (None, None, None)
    assert sniff_image(b"", None) == (None, None, None)


def test_is_broken_semantics():
    assert is_broken(404) is True
    assert is_broken(410) is True
    assert is_broken(200) is False
    assert is_broken(None) is False  # unreachable -> not "broken"
    assert is_broken(503) is False  # transient
    assert is_broken(403) is False  # access-gated
