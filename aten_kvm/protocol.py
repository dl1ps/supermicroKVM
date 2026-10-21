"""Wire constants and helpers for classic ATEN iKVM (TCP 5900).

Aligned with:
- public chicken-aten-ikvm lens (BSD-2)
- client pseudocode in ../pseudocode/libiKVM32_lin.c (+ iKVM32_dll.c)
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import BinaryIO

# Expected RFB version (RFBProtocol constructor in libiKVM)
RFB_MAJOR = 3
RFB_MINOR = 8
RFB_VERSION_RE = re.compile(rb"^RFB (\d{3})\.(\d{3})\n$")

# --- Client → server message IDs ---
MSG_SET_PIXEL_FORMAT = 0x00
MSG_SET_ENCODINGS = 0x02
MSG_FB_UPDATE_REQUEST = 0x03
MSG_KEY_EVENT = 0x04
MSG_POINTER_EVENT = 0x05
MSG_RESYNC_MOUSE = 0x07
MSG_MOUSE_RESET = 0x08
MSG_KEEP_ALIVE = 0x16  # 22
MSG_SCREEN_SET_INFO = 0x32  # 50 — image quality / mode (changeScreenInfo)
MSG_KICK = 0x38  # 56 — RFBPrivilege::SendKickRequest
MSG_QOS = 0x3B  # 59 — ProcQos(outgo, income, sensitiveness)

# --- Server → client message IDs (ProtocolHandler / lens) ---
SMSG_FRAMEBUFFER_UPDATE = 0x00
SMSG_FRONT_GROUND = 0x04
SMSG_KEEP_ALIVE = 0x16
SMSG_VIDEO_GET_INFO = 0x33
SMSG_KEYBOARD_INFO = 0x35
SMSG_MOUSE_GET_INFO = 0x37
SMSG_SESSION_MSG = 0x39
SMSG_VIEWER_LANG = 0x3C

# Frame payload types (lens frame_format)
FRAME_TYPE_SUBRECTS = 0
FRAME_TYPE_RAW = 1

# Encodings from RMDecoder::GetDecoder (libiKVM)
ENC_RAW = 0
ENC_AST2100 = 87  # ASTVideoDecoder → ast2100
ENC_AST_JPEG = 88  # ASTVideoDecoder → ast_jpeg
ENC_HERMON = 89  # HermonVideoDecoder
ENC_YARKON = 96
ENC_PILOT3 = 97

ENCODING_NAMES = {
    ENC_RAW: "raw/aten-0",
    ENC_AST2100: "ast2100 (87)",
    ENC_AST_JPEG: "ast_jpeg (88)",
    ENC_HERMON: "hermon (89)",
    ENC_YARKON: "yarkon (96)",
    ENC_PILOT3: "pilot3 (97)",
}


def pad24(text: str) -> bytes:
    raw = text.encode("utf-8", errors="replace")[:23]
    return raw + b"\x00" * (24 - len(raw))


def read_exact(sock: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.read(n - len(buf))
        if not chunk:
            raise ConnectionError(f"short read: need {n}, got {len(buf)}")
        buf.extend(chunk)
    return bytes(buf)


def as_signed16(value: int) -> int:
    """Interpret a 16-bit wire value as signed (screen-off / negative dims)."""
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def parse_rfb_version(buf: bytes) -> tuple[int, int]:
    m = RFB_VERSION_RE.match(buf)
    if not m:
        raise ConnectionError(f"bad RFB version string: {buf!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass
class PixelFormat:
    bits_per_pixel: int
    depth: int
    big_endian: int
    true_color: int
    red_max: int
    green_max: int
    blue_max: int
    red_shift: int
    green_shift: int
    blue_shift: int

    @classmethod
    def from_bytes(cls, data: bytes) -> PixelFormat:
        # Client reads max/shift as R, B, G
        (
            bpp,
            depth,
            be,
            tc,
            rmax,
            bmax,
            gmax,
            rshift,
            bshift,
            gshift,
        ) = struct.unpack(">BBBBHHHBBB", data[:13])
        return cls(bpp, depth, be, tc, rmax, gmax, bmax, rshift, gshift, bshift)


@dataclass
class ServerInit:
    width: int
    height: int
    pixel_format: PixelFormat
    name: str
    privilege_word: int
    video_enable: int
    km_enable: int
    kick_enable: int
    vusb_enable: int


def build_key_event(down_flag: int, hid_usage: int) -> bytes:
    """ATEN KeyEvent id=4, 18 bytes; u32 is USB HID usage (Sendkey).

    down_flag (RFBKeyboard::Sendkey):
      0 = release
      1 = press / hold
      2 = STABLE tap (Java); on some X10SLM FW this acted like Enter — avoid
    """
    return struct.pack(
        ">BxBxxI9x",
        MSG_KEY_EVENT,
        down_flag & 0xFF,
        hid_usage & 0xFFFFFFFF,
    )


def build_pointer_event(
    buttons: int, x: int, y: int, *, encrypted: bool = False
) -> bytes:
    """ATEN PointerEvent id=5, 18 bytes.

    Cleartext: encFlag=0, buttons, x, y, pad.
    Encrypted path (AES-128-CBC) is not implemented in this scaffold.
    """
    if encrypted:
        raise NotImplementedError(
            "encrypted pointer events need RFBKMCryto; use cleartext (encFlag=0)"
        )
    return struct.pack(
        ">BBBHH11x",
        MSG_POINTER_EVENT,
        0,  # encFlag
        buttons & 0xFF,
        x & 0xFFFF,
        y & 0xFFFF,
    )


def build_fb_update_request(
    incremental: bool, x: int, y: int, w: int, h: int
) -> bytes:
    return struct.pack(
        ">BBHHHH",
        MSG_FB_UPDATE_REQUEST,
        1 if incremental else 0,
        x,
        y,
        w,
        h,
    )


def build_keep_alive(payload: int = 0) -> bytes:
    return struct.pack(">BB", MSG_KEEP_ALIVE, payload & 0xFF)


def build_kick_request(session_id: int, name_payload: bytes = b"") -> bytes:
    """Msg 56 / SendKickRequest — drop another iKVM session (frees KM).

    Wire: u8 id, u32 session_id, u32 type=0, 64-byte name/pad.
    """
    name = (name_payload + bytes(64))[:64]
    return struct.pack(">BII", MSG_KICK, session_id & 0xFFFFFFFF, 0) + name


def build_mouse_sync(value: int = 1920) -> bytes:
    """MouseSync id=7 — Linux client often sends u16 1920."""
    return struct.pack(">BH", MSG_RESYNC_MOUSE, value & 0xFFFF)


def build_screen_set_info(image_quality: int, mode: int = 0) -> bytes:
    """Msg 50 / ScreenSetInfo — Hermon: quality 0=high color, 1=low color."""
    return struct.pack(
        ">BHH",
        MSG_SCREEN_SET_INFO,
        image_quality & 0xFFFF,
        mode & 0xFFFF,
    )


def build_qos(outgo: int, income: int = 0, sensitiveness: int = 10) -> bytes:
    """Msg 59 / ProcQos — bandwidth hints used by Java OptionFrame."""
    return struct.pack(">BIII", MSG_QOS, outgo & 0xFFFFFFFF, income & 0xFFFFFFFF, sensitiveness & 0xFFFFFFFF)


def rgb555_to_rgb888(pixel: int) -> tuple[int, int, int]:
    r = ((pixel >> 10) & 0x1F) * 255 // 31
    g = ((pixel >> 5) & 0x1F) * 255 // 31
    b = (pixel & 0x1F) * 255 // 31
    return r, g, b
