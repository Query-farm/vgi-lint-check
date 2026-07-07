"""HTTP link resolution for the content rules (opt-out via --no-check-links).

A resolver maps a URL to an HTTP status code (or ``None`` when it cannot be
reached at all). It is wired only for real runs (in ``core``); rules treat a
missing resolver as "skip", so offline/unit tests never make network calls.
"""

from __future__ import annotations

import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

LinkResolver = Callable[[str], int | None]

_HEADERS = {"User-Agent": "vgi-lint-check link checker"}


def _fetch(url: str, timeout: float, method: str) -> int | None:
    req = urllib.request.Request(url, method=method, headers=_HEADERS)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as e:
        # Some servers reject HEAD; retry once with GET.
        if method == "HEAD" and e.code in (403, 405, 501):
            return _fetch(url, timeout, "GET")
        return int(e.code)
    except Exception:  # noqa: BLE001 - DNS/timeout/SSL -> unverifiable
        if method == "HEAD":
            return _fetch(url, timeout, "GET")
        return None


def make_link_resolver(timeout: float = 10.0) -> LinkResolver:
    """Return a caching resolver: ``url -> HTTP status`` (or None if unreachable)."""
    cache: dict[str, int | None] = {}

    def resolve(url: str) -> int | None:
        if url not in cache:
            cache[url] = _fetch(url, timeout, "HEAD")
        return cache[url]

    return resolve


# --------------------------------------------------------------------------
# Image probing (VGI015 — catalog icon)
# --------------------------------------------------------------------------
# Raster/vector formats an <img> renders in every mainstream browser. We sniff
# by magic bytes rather than trusting Content-Type (misconfigured servers send
# application/octet-stream for perfectly good PNGs).
DISPLAYABLE_IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "webp", "bmp", "ico", "svg", "avif"})
# How many bytes to pull to sniff format + dimensions. A JPEG's SOF marker can
# sit arbitrarily deep, but real icons are small; this cap covers the common case
# while bounding the download.
_IMAGE_SNIFF_BYTES = 262144


@dataclass(frozen=True)
class ImageInfo:
    """What an image probe learned about a URL (best-effort; fields may be None)."""

    status: int | None = None  # HTTP status, or None if the request never completed
    content_type: str | None = None  # server-declared MIME type (informational only)
    fmt: str | None = None  # sniffed format (png/jpeg/…); None if unrecognized
    width: int | None = None  # pixels; None if not a raster or undecodable within the cap
    height: int | None = None
    size_bytes: int | None = None  # Content-Length when present, else bytes read
    error: str | None = None  # network-layer failure (DNS/timeout/TLS)


def _sniff_png(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def _sniff_gif(data: bytes) -> tuple[int, int] | None:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    return None


def _sniff_bmp(data: bytes) -> tuple[int, int] | None:
    if data[:2] == b"BM" and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return abs(w), abs(h)
    return None


def _sniff_webp(data: bytes) -> tuple[int, int] | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8 ":  # lossy: 14-bit dims in the frame header
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return w, h
    if chunk == b"VP8L":  # lossless: dims packed in the bits after the 0x2f signature
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
        return w, h
    return None


def _sniff_ico(data: bytes) -> tuple[int, int] | None:
    if data[:4] == b"\x00\x00\x01\x00" and len(data) >= 8:
        w = data[6] or 256  # 0 encodes 256
        h = data[7] or 256
        return w, h
    return None


def _sniff_jpeg(data: bytes) -> tuple[int, int] | None:
    if data[:3] != b"\xff\xd8\xff":
        return None
    i, n = 2, len(data)
    while i + 9 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF15 carry the frame dimensions; skip the two non-SOF codes.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers, no length field
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _looks_like_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<?xml") and b"<svg" in data[:2048].lower() or head.startswith(b"<svg")


def sniff_image(data: bytes, content_type: str | None) -> tuple[str | None, int | None, int | None]:
    """Return (format, width, height) sniffed from image bytes; Nones when unknown.

    SVG is vector (no pixel resolution), so its dimensions are reported as None —
    callers treat an unknown resolution as "cannot judge", never as a failure.
    """
    for fmt, sniff in (
        ("png", _sniff_png),
        ("gif", _sniff_gif),
        ("jpeg", _sniff_jpeg),
        ("webp", _sniff_webp),
        ("bmp", _sniff_bmp),
        ("ico", _sniff_ico),
    ):
        dims = sniff(data)
        if dims is not None:
            return fmt, dims[0], dims[1]
    # AVIF is ISO-BMFF: an `ftyp` box with an `avif`/`avis` brand. Dimensions live
    # in an `ispe` box we don't decode, so report the format only.
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "avif", None, None
    if _looks_like_svg(data) or (content_type or "").lower().startswith("image/svg"):
        return "svg", None, None
    return None, None, None


def probe_image(url: str, timeout: float) -> ImageInfo:
    """Fetch ``url`` far enough to sniff its image format and dimensions."""
    req = urllib.request.Request(url, method="GET", headers=_HEADERS)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = int(resp.status)
            content_type = resp.headers.get("Content-Type")
            declared = resp.headers.get("Content-Length")
            data = resp.read(_IMAGE_SNIFF_BYTES)
    except urllib.error.HTTPError as e:
        return ImageInfo(status=int(e.code))
    except Exception as e:  # noqa: BLE001 - DNS/timeout/TLS -> unverifiable
        return ImageInfo(error=str(e) or e.__class__.__name__)
    fmt, w, h = sniff_image(data, content_type)
    size = int(declared) if declared and declared.isdigit() else len(data)
    return ImageInfo(
        status=status,
        content_type=content_type,
        fmt=fmt,
        width=w,
        height=h,
        size_bytes=size,
    )


ImageProbe = Callable[[str], ImageInfo]


def make_image_probe(timeout: float = 10.0) -> ImageProbe:
    """Return a caching image probe: ``url -> ImageInfo``."""
    cache: dict[str, ImageInfo] = {}

    def probe(url: str) -> ImageInfo:
        if url not in cache:
            cache[url] = probe_image(url, timeout)
        return cache[url]

    return probe


def is_broken(status: int | None) -> bool:
    """True when a status is a definitive client-side "broken link".

    None (unreachable: DNS/timeout/TLS), 5xx (transient server), and access-gated
    codes (401/403/429) are NOT treated as broken to avoid flaky failures.
    """
    if status is None:
        return False
    if status in (401, 403, 429):
        return False
    return 400 <= status < 500
