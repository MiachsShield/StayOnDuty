"""Image verifier — stdlib only.

Catches the failures a long automatic image run actually hits:
truncated downloads, wrong formats, blank or single-color frames.
Checks: magic bytes (PNG/JPEG), parsed dimensions, minimum size, and a
blank/single-color heuristic.

Extension hook: pass spec_check(image_bytes, spec) -> (ok, reason) for a
future model-graded "does it match the spec?" check (e.g. a vision call
through the same provider). Basic checks always run first.
"""
from __future__ import annotations
import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class UnsupportedImage(Exception):
    """Recognized format, but a variant the pixel check can't decode."""


class Verdict:
    def __init__(self, ok, reasons, width=None, height=None, format=None):
        self.ok = ok
        self.reasons = list(reasons)
        self.width = width
        self.height = height
        self.format = format  # "png" | "jpeg" | None

    @property
    def passed(self):
        return self.ok

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return (f"Verdict(ok={self.ok}, format={self.format}, "
                f"{self.width}x{self.height}, reasons={self.reasons})")


def detect_format(data):
    if data[:8] == PNG_MAGIC:
        return "png"
    if data[:3] == JPEG_MAGIC:
        return "jpeg"
    return None


def png_dimensions(data):
    """(width, height) from IHDR. Raises ValueError on truncation."""
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG")
    if len(data) < 33:
        raise ValueError("PNG truncated before IHDR")
    length = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR" or length < 13 or len(data) < 16 + length:
        raise ValueError("PNG truncated before IHDR")
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def jpeg_dimensions(data):
    """(width, height) by scanning for the SOF marker. Raises ValueError."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError("bad JPEG marker")
        while data[pos] == 0xFF:
            pos += 1
            if pos >= len(data):
                raise ValueError("JPEG truncated in marker")
        m = data[pos]
        pos += 1
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7 or m == 0x01:
            continue  # standalone markers, no length
        if pos + 2 > len(data):
            raise ValueError("JPEG truncated in segment header")
        ln = struct.unpack(">H", data[pos:pos + 2])[0]
        if ln < 2:
            raise ValueError("bad JPEG segment length")
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            if pos + 7 > len(data):
                raise ValueError("JPEG truncated in SOF")
            h = struct.unpack(">H", data[pos + 3:pos + 5])[0]
            w = struct.unpack(">H", data[pos + 5:pos + 7])[0]
            return w, h
        if m == 0xDA:
            raise ValueError("reached scan data with no SOF — truncated JPEG")
        pos += ln - 2
    raise ValueError("no SOF marker — truncated JPEG")


def _png_distinct_colors(data, max_distinct=8, samples=1500):
    """Decode a PNG (8-bit, non-interlaced) and count distinct colors in a
    sample. Raises UnsupportedImage for variants we don't decode."""
    pos = 8
    idat = bytearray()
    w = h = bit_depth = color_type = interlace = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if pos + 12 + length > len(data):
            raise ValueError("PNG truncated mid-chunk")
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            w, h, bit_depth, color_type, _c, _f, interlace = \
                struct.unpack(">IIBBBBB", chunk[:13])
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if w is None:
        raise ValueError("PNG has no IHDR")
    if interlace:
        raise UnsupportedImage("interlaced PNG")
    if bit_depth != 8 or color_type not in (0, 2, 6):
        raise UnsupportedImage(
            f"bit_depth={bit_depth} color_type={color_type}")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise ValueError(f"IDAT corrupt: {e}") from None
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = w * channels
    # sample a grid of pixels
    step_y = max(1, h // 40)
    step_x = max(1, w // 40)
    seen = set()
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        if p + 1 + stride > len(raw):
            raise ValueError("PNG truncated in scanlines")
        f = raw[p]
        p += 1
        cur = bytearray(raw[p:p + stride])
        p += stride
        _unfilter(f, cur, prev, channels)
        if y % step_y == 0:
            for x in range(0, w, step_x):
                i = x * channels
                seen.add(bytes(cur[i:i + 3]))  # RGB only; alpha ignored
                if len(seen) > max_distinct:
                    return len(seen)
        prev = cur
    return len(seen)


def _unfilter(f, cur, prev, channels):
    if f == 0:
        return
    n = len(cur)
    if f == 1:
        for i in range(channels, n):
            cur[i] = (cur[i] + cur[i - channels]) & 0xFF
    elif f == 2:
        for i in range(n):
            cur[i] = (cur[i] + prev[i]) & 0xFF
    elif f == 3:
        for i in range(n):
            a = cur[i - channels] if i >= channels else 0
            cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
    elif f == 4:
        for i in range(n):
            a = cur[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            pp = a + b - c
            pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            cur[i] = (cur[i] + pr) & 0xFF
    else:
        raise ValueError(f"unknown PNG filter {f}")


def verify_image(data, min_width=256, min_height=256,
                 spec=None, spec_check=None):
    """Return a Verdict. Reasons are human-readable for the timeline.

    spec_check(image_bytes, spec) -> (ok: bool, reason: str) is the hook
    for a future model-graded spec match; basic checks run first.
    """
    reasons = []
    if not data:
        return Verdict(False, ["empty output"], format=None)
    fmt = detect_format(data)
    if fmt is None:
        head = data[:16].hex()
        return Verdict(False, [f"unknown format (magic bytes {head})..."],
                       format=None)
    try:
        w, h = png_dimensions(data) if fmt == "png" else jpeg_dimensions(data)
    except ValueError as e:
        return Verdict(False, [f"{fmt.upper()} unparseable: {e}"], format=fmt)

    if w < min_width or h < min_height:
        reasons.append(f"too small: {w}x{h} (minimum {min_width}x{min_height})")

    # blank / single-color heuristic
    try:
        if fmt == "png":
            n = _png_distinct_colors(data)
            if n <= 4:
                reasons.append(f"blank or single-color: only {n} distinct"
                               " colors in sample")
        else:
            bpp = len(data) / max(1, w * h)
            # A blank 1024x1024 JPEG is ~0.02 bytes/px; a real photo is
            # ~0.15+. This is a heuristic, documented as one.
            if bpp < 0.05:
                reasons.append(f"suspiciously small file for {w}x{h}"
                               f" ({bpp:.3f} bytes/px — likely blank)")
    except UnsupportedImage as e:
        reasons.append(f"pixel check skipped: {e}")
    except ValueError as e:
        reasons.append(f"pixel data corrupt: {e}")

    if spec is not None and spec_check is not None and not reasons:
        ok, why = spec_check(data, spec)
        if not ok:
            reasons.append(f"spec check failed: {why}")

    return Verdict(not reasons, reasons, width=w, height=h, format=fmt)
