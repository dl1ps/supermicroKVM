"""Framebuffer + Hermon (encoding 89) / ATEN frame decode.

Hermon layout matches chicken-aten-ikvm AtenEncodingReader (BSD) and
HermonVideoDecoder::Decode in libiKVM pseudocode:
  header 10 bytes, length fields big-endian; pixels RGB555.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .protocol import (
    ENC_AST2100,
    ENC_AST_JPEG,
    ENC_HERMON,
    ENC_PILOT3,
    ENC_RAW,
    ENC_YARKON,
    ENCODING_NAMES,
    FRAME_TYPE_RAW,
    FRAME_TYPE_SUBRECTS,
    rgb555_to_rgb888,
)

# AtenEncodingHeader size (chicken-aten-ikvm)
ATEN_HEADER_SIZE = 10


@dataclass
class Framebuffer:
    width: int
    height: int
    pixels: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        need = self.width * self.height * 3
        if len(self.pixels) != need:
            self.pixels = bytearray(need)

    def resize(self, width: int, height: int, *, preserve: bool = True) -> None:
        """Change framebuffer size. preserve=False clears (mode-change / Java changeResolution)."""
        width, height = max(width, 1), max(height, 1)
        if width == self.width and height == self.height:
            return
        old = self.pixels
        ow, oh = self.width, self.height
        self.width, self.height = width, height
        self.pixels = bytearray(width * height * 3)
        if not preserve:
            return
        # Grow path: keep overlap so incremental tiles stay valid
        cw, ch = min(ow, width), min(oh, height)
        for y in range(ch):
            src = y * ow * 3
            dst = y * width * 3
            self.pixels[dst : dst + cw * 3] = old[src : src + cw * 3]

    def put_rgb(self, x: int, y: int, r: int, g: int, b: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        i = (y * self.width + x) * 3
        self.pixels[i : i + 3] = bytes((r & 0xFF, g & 0xFF, b & 0xFF))

    def put_rgb555(self, x: int, y: int, value: int) -> None:
        r, g, b = rgb555_to_rgb888(value)
        self.put_rgb(x, y, r, g, b)

    def put_rgb555_rect(self, x0: int, y0: int, w: int, h: int, data: bytes) -> int:
        """Row-major LE RGB555 into rectangle. Returns pixels written."""
        need = w * h * 2
        if len(data) < need:
            return 0
        off = 0
        for row in range(h):
            for col in range(w):
                pix = struct.unpack_from("<H", data, off)[0]
                off += 2
                self.put_rgb555(x0 + col, y0 + row, pix)
        return w * h

    def put_rgb2_rect(self, x0: int, y0: int, w: int, h: int, data: bytes) -> int:
        """Hermon low-color: 1 byte/pixel, 2 bits per channel (ConvertVierwerPixelFormat)."""
        need = w * h
        if len(data) < need:
            return 0
        off = 0
        for row in range(h):
            for col in range(w):
                v = data[off]
                off += 1
                # Match HermonVideoDecoder::ConvertVierwerPixelFormat (a2=1)
                b = (v & 0x03) << 6
                g = (v & 0x0C) << 4
                r = (v & 0x30) << 2
                self.put_rgb(x0 + col, y0 + row, r, g, b)
        return w * h

    def fill_block_rgb555(self, x0: int, y0: int, block: bytes) -> None:
        self.put_rgb555_rect(x0, y0, 16, 16, block)

    def fill_block_rgb2(self, x0: int, y0: int, block: bytes) -> None:
        self.put_rgb2_rect(x0, y0, 16, 16, block)

    def to_ppm(self) -> bytes:
        header = f"P6\n{self.width} {self.height}\n255\n".encode("ascii")
        return header + bytes(self.pixels)


@dataclass
class RectUpdate:
    x: int
    y: int
    width: int
    height: int
    encoding: int
    aten_flag: int
    data: bytes


def _parse_aten_header(data: bytes) -> tuple[int, int, int, int, bytes]:
    """Return (type, mode, field_a, total_length, pixel_blob).

    mode ≠ 0 → Hermon low-color (1 B/px, 256 B/tile); else RGB555 (2 B/px).
    Lengths are big-endian (AtenEncodingReader / Hermon Decode).
    """
    if len(data) < ATEN_HEADER_SIZE:
        raise ValueError("ATEN frame header short")
    ftype = data[0]
    mode = data[1]
    field_a = struct.unpack_from(">I", data, 2)[0]
    total_length = struct.unpack_from(">I", data, 6)[0]
    if total_length >= ATEN_HEADER_SIZE and total_length <= len(data):
        blob = data[ATEN_HEADER_SIZE:total_length]
    else:
        blob = data[ATEN_HEADER_SIZE:]
    return ftype, mode, field_a, total_length, blob


def decode_aten_hermon(
    data: bytes, fb: Framebuffer, x: int, y: int, w: int, h: int
) -> str:
    """Decode encoding 0/89-style ATEN Hermon payload into fb."""
    try:
        ftype, mode, field_a, total_length, blob = _parse_aten_header(data)
    except ValueError as exc:
        return f"hermon: {exc}"

    low = mode != 0
    bpp = 1 if low else 2
    color = "low8" if low else "rgb555"

    if ftype == FRAME_TYPE_RAW:
        expect = w * h * bpp
        if len(blob) < expect:
            return (
                f"raw/{color}: have {len(blob)} B, need {expect} for {w}x{h} "
                f"(hdr_total={total_length})"
            )
        if low:
            n = fb.put_rgb2_rect(x, y, w, h, blob)
        else:
            n = fb.put_rgb555_rect(x, y, w, h, blob)
        return f"raw/{color}: painted {n} px {w}x{h} (hdr_total={total_length})"

    if ftype == FRAME_TYPE_SUBRECTS:
        segments = field_a
        tile_bytes = 16 * 16 * bpp
        stride = 6 + tile_bytes
        painted = 0
        pos = 0
        for _ in range(segments):
            if pos + stride > len(blob):
                break
            ty = blob[pos + 4]
            tx = blob[pos + 5]
            tile = blob[pos + 6 : pos + stride]
            pos += stride
            if low:
                fb.fill_block_rgb2(x + tx * 16, y + ty * 16, tile)
            else:
                fb.fill_block_rgb555(x + tx * 16, y + ty * 16, tile)
            painted += 1
        return (
            f"subrects/{color}: {painted}/{segments} tiles "
            f"(hdr_total={total_length}, blob={len(blob)})"
        )

    return f"hermon: unknown type {ftype} (hdr_total={total_length})"


def describe_encoding(encoding: int) -> str:
    return ENCODING_NAMES.get(encoding, f"enc-{encoding}")


def encoding_is_ast_family(encoding: int) -> bool:
    return encoding in (ENC_AST2100, ENC_AST_JPEG, ENC_YARKON, ENC_PILOT3)


def apply_rect_to_framebuffer(rect: RectUpdate, fb: Framebuffer) -> str:
    if rect.width <= 0 or rect.height <= 0:
        return "rect: non-positive size (screen-off?)"

    # libiKVM ScreenDecode stores rect w/h as current screen size; Java then
    # changeResolution() when they differ. Hermon sends the full desktop as the
    # outer rect (x=y=0) — must shrink on BIOS↔OS mode changes, not only grow.
    size_note = ""
    if rect.x == 0 and rect.y == 0:
        if rect.width != fb.width or rect.height != fb.height:
            size_note = f"resize {fb.width}x{fb.height}→{rect.width}x{rect.height} "
            fb.resize(rect.width, rect.height, preserve=False)
    else:
        need_w = max(fb.width, rect.x + rect.width)
        need_h = max(fb.height, rect.y + rect.height)
        if need_w != fb.width or need_h != fb.height:
            size_note = f"grow {fb.width}x{fb.height}→{need_w}x{need_h} "
            fb.resize(need_w, need_h, preserve=True)

    if rect.aten_flag != 0:
        return (
            f"{size_note}aten_flag={rect.aten_flag} enc={describe_encoding(rect.encoding)} "
            f"len={len(rect.data)} (special path, no scaffold decode)"
        )

    # Hermon (89) and raw/aten-0 share the same ATEN frame header
    if rect.encoding in (ENC_RAW, ENC_HERMON):
        return size_note + decode_aten_hermon(
            rect.data, fb, rect.x, rect.y, rect.width, rect.height
        )

    if encoding_is_ast_family(rect.encoding):
        # Might still be ATEN-framed on some FW; try, else report TODO
        if len(rect.data) >= ATEN_HEADER_SIZE and rect.data[0] in (0, 1):
            status = decode_aten_hermon(
                rect.data, fb, rect.x, rect.y, rect.width, rect.height
            )
            if not status.startswith("hermon: unknown"):
                return f"{size_note}{describe_encoding(rect.encoding)}: {status}"
        return (
            f"{size_note}{describe_encoding(rect.encoding)}: len={len(rect.data)} "
            "(AST/JPEG decode not implemented — use aten-proxy / noVNC)"
        )

    return size_note + decode_aten_hermon(
        rect.data, fb, rect.x, rect.y, rect.width, rect.height
    )
