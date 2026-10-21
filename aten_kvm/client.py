"""TCP client for classic ATEN iKVM (port 5900).

Handshake / ScreenDecode aligned with libiKVM pseudocode + OSS lens.
"""

from __future__ import annotations

import logging
import queue
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .framebuffer import Framebuffer, RectUpdate, apply_rect_to_framebuffer, describe_encoding
from .keys import KeyStroke
from .leds import LedState, read_local_leds, remote_leds_from_session_byte
from .protocol import (
    RFB_MAJOR,
    RFB_MINOR,
    SMSG_FRAMEBUFFER_UPDATE,
    SMSG_FRONT_GROUND,
    SMSG_KEEP_ALIVE,
    SMSG_KEYBOARD_INFO,
    SMSG_MOUSE_GET_INFO,
    SMSG_SESSION_MSG,
    SMSG_VIDEO_GET_INFO,
    SMSG_VIEWER_LANG,
    PixelFormat,
    ServerInit,
    as_signed16,
    build_fb_update_request,
    build_keep_alive,
    build_key_event,
    build_kick_request,
    build_mouse_sync,
    build_pointer_event,
    build_qos,
    build_screen_set_info,
    pad24,
    parse_rfb_version,
    read_exact,
)

log = logging.getLogger(__name__)

OnFrame = Callable[[Framebuffer], None]

# How often to print RX/TX totals + rates on the calling console (INFO).
_BANDWIDTH_LOG_INTERVAL_S = 5.0


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GiB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MiB"
    if n >= 1024:
        return f"{n / 1024:.0f} KiB"
    return f"{n} B"


def _fmt_bitrate(bytes_per_sec: float) -> str:
    if bytes_per_sec < 0:
        bytes_per_sec = 0.0
    bits = bytes_per_sec * 8.0
    if bits >= 1_000_000:
        return f"{bits / 1_000_000:.1f} Mbit/s"
    if bits >= 1_000:
        return f"{bits / 1_000:.0f} kbit/s"
    return f"{bits:.0f} bit/s"


@dataclass
class ConnectConfig:
    host: str
    port: int = 5900
    username: str = "ADMIN"
    password: str = "ADMIN"
    timeout: float = 15.0
    require_rfb_38: bool = True
    # Bandwidth / console-friendly tweaks
    low_color: bool = False  # Hermon "Low Color (Lighter Traffic)"
    max_fps: float = 10.0  # client-side FB request throttle
    qos_outgo: int = 0  # ProcQos outgo hint; 0 = leave default
    drop_frames: bool = False  # skip painting if UI still busy (set by viewer)
    bandwidth_stats: bool = True  # periodic RX/TX log on the calling console


class AtenKvmClient:
    def __init__(self, config: ConnectConfig) -> None:
        self.config = config
        self.sock: Optional[socket.socket] = None
        self._file = None
        self.server_init: Optional[ServerInit] = None
        self.fb: Optional[Framebuffer] = None
        self.on_frame: Optional[OnFrame] = None
        self._last_keep_alive = 0.0
        self._last_fb_request = 0.0
        self._got_first_frame = False
        self._fb_outstanding = False  # one FB request in flight at a time
        self.busy_display = False  # viewer sets True while painting
        self._send_lock = threading.Lock()
        # Latest decoded frames for the UI thread (reader never touches Tk)
        self._frame_queue: queue.Queue[Optional[Framebuffer]] = queue.Queue(maxsize=2)
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self.reader_error: Optional[BaseException] = None
        self._closing = False
        # Pointer throttle — ATEN KM USB dies under MotionEvent floods
        self._last_pointer_at = 0.0
        self._last_pointer_buttons = -1
        self._pointer_min_interval = 1.0 / 25.0
        self._max_frame_bytes = 8_000_000
        self.remote_leds: Optional[LedState] = None
        self._led_sync_lock = threading.Lock()
        self._led_syncing = False
        self._low_color_sent = False
        # Session IDs from privilege type=1 user-list messages (for kick)
        self._listed_sessions: set[int] = set()
        self.km_ok: bool = True  # last known IKVMKMEnable from ServerInit
        # Wire traffic (process lifetime; survives reconnect)
        self._bw_lock = threading.Lock()
        self._rx_total = 0
        self._tx_total = 0
        self._rx_window = 0
        self._tx_window = 0
        self._bw_window_start = time.monotonic()
        self._last_bw_log = 0.0

    @property
    def kvm_link_ok(self) -> bool:
        """True while the reader is alive and no fatal receive error."""
        if self._closing or self.reader_error is not None:
            return False
        if self.sock is None:
            return False
        return self.reader_alive

    def connect(self) -> None:
        log.info("connect %s:%s", self.config.host, self.config.port)
        self.sock = socket.create_connection(
            (self.config.host, self.config.port), timeout=self.config.timeout
        )
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Larger buffers help on lossy / high-latency links
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 18)
        except OSError:
            pass
        # Dead-peer detection: VPN drops leave TCP half-open; send() still
        # "succeeds" into the kernel until retransmit/USER_TIMEOUT fires.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Linux: fail socket if TX is unacked this long (ms). Without this,
            # a VPN drop can look fine until the path returns (retransmit lag).
            user_timeout = getattr(socket, "TCP_USER_TIMEOUT", 18)
            self.sock.setsockopt(socket.IPPROTO_TCP, user_timeout, 3_000)
            if hasattr(socket, "TCP_KEEPIDLE"):
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 3)
            if hasattr(socket, "TCP_KEEPINTVL"):
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
            if hasattr(socket, "TCP_KEEPCNT"):
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except OSError as exc:
            log.debug("TCP keepalive/USER_TIMEOUT not fully applied: %s", exc)
        self._file = self.sock.makefile("rwb", buffering=0)

        # InitHandShake + Authenticate (ClientInit/ServerInit inside auth success)
        self._handshake()
        self._send_client_init()
        self.server_init = self._read_server_init()

        w, h = self.server_init.width, self.server_init.height
        if w <= 0 or h <= 0:
            w, h = 640, 480
            log.warning("non-positive geometry from server; using %sx%s", w, h)
        self.fb = Framebuffer(w, h)
        log.info(
            "server %sx%s name=%r priv=0x%x video=%s km=%s kick=%s vusb=%s",
            w,
            h,
            self.server_init.name,
            self.server_init.privilege_word,
            self.server_init.video_enable,
            self.server_init.km_enable,
            self.server_init.kick_enable,
            self.server_init.vusb_enable,
        )
        # Do NOT send KeyboardUpdateInfo/MouseUpdateInfo / ScreenSetInfo here —
        # on some X10SLM firmwares control msgs right after ClientInit drop KM.
        # Low-color is deferred until the first decoded frame (see below).
        # QoS (if requested) is still sent after connect — opt-in only.
        if self.config.qos_outgo > 0:
            self._write(build_qos(self.config.qos_outgo, 0, 10))
            log.info("sent QoS outgo=%s", self.config.qos_outgo)
        if self.config.low_color:
            log.info("low-color requested — will send ScreenSetInfo after first frame")
        self.km_ok = bool(self.server_init and self.server_init.km_enable)
        if not self.km_ok:
            log.warning(
                "ServerInit KM disabled (video=%s) — keyboard will not work until KM is freed",
                self.server_init.video_enable if self.server_init else "?",
            )
        self.request_framebuffer(incremental=False)

    def start_reader(self) -> None:
        """Background receive/decode so Tk can send keys without waiting on frames."""
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._closing = False
        self._reader_stop.clear()
        self.reader_error = None
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="aten-kvm-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def request_shutdown(self) -> None:
        """Signal reader + socket to stop (safe from signal handlers)."""
        self._closing = True
        self._reader_stop.set()
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop_reader(self) -> None:
        self._closing = True
        self._reader_stop.set()
        t = self._reader_thread
        # Only shutdown read if the reader is still blocked — avoids extra RST
        # noise on a BMC that already dropped us.
        if t is not None and t.is_alive() and t is not threading.current_thread():
            sock = self.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RD)
                except OSError:
                    pass
            t.join(timeout=2.0)
        self._reader_thread = None

    @property
    def reader_alive(self) -> bool:
        t = self._reader_thread
        return t is not None and t.is_alive()

    def _reader_loop(self) -> None:
        try:
            while not self._reader_stop.is_set():
                if not self.poll_once(wait=0.05):
                    if not self._closing and not self._reader_stop.is_set():
                        self.reader_error = ConnectionError("connection closed by peer")
                        log.error("reader stopped: connection closed by peer")
                    break
        except (ConnectionError, OSError, EOFError) as exc:
            # Normal during Ctrl+C / window close / BMC drop — no traceback
            if self._closing or self._reader_stop.is_set():
                log.debug("reader stopped (shutdown): %s", exc)
            else:
                self.reader_error = exc
                log.error("reader stopped: %s", exc)
        except BaseException as exc:  # noqa: BLE001 — surface to UI thread
            if self._closing or self._reader_stop.is_set():
                log.debug("reader stopped (shutdown): %s", exc)
            else:
                self.reader_error = exc
                log.exception("reader stopped: %s", exc)
        finally:
            self._reader_stop.set()
            try:
                self._frame_queue.put_nowait(None)
            except queue.Full:
                pass

    def take_frame(self) -> Optional[Framebuffer]:
        """Drain queued frames; return the newest (None = reader ended)."""
        latest: Optional[Framebuffer] = None
        saw_sentinel = False
        while True:
            try:
                item = self._frame_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                saw_sentinel = True
                continue
            latest = item
        if saw_sentinel and latest is None and not self.reader_alive:
            return None
        return latest

    def _publish_frame(self, fb: Framebuffer) -> None:
        snap = Framebuffer(fb.width, fb.height, bytearray(fb.pixels))
        if self.on_frame is not None:
            # Legacy/headless callback (may run on reader thread)
            if self.config.drop_frames and self.busy_display:
                log.debug("drop frame (display busy)")
            else:
                self.on_frame(snap)
        try:
            self._frame_queue.put_nowait(snap)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait(snap)
            except queue.Full:
                pass

    def close(self, *, quiet: bool = False) -> None:
        """Drop the TCP session cleanly.

        ATEN often keeps an exclusive KM session until TCP teardown or a BMC
        timeout; abrupt process kill can leave keyboard dead for all clients.
        """
        self._closing = True
        self.stop_reader()
        sock = self.sock
        self.sock = None
        file_obj = self._file
        self._file = None
        if file_obj is not None:
            try:
                file_obj.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if not quiet:
            self.log_bandwidth_summary(prefix="session total")
            log.info("connection closed")

    def _reset_session_state(self) -> None:
        self._closing = False
        self.reader_error = None
        self.server_init = None
        self.fb = None
        self._got_first_frame = False
        self._fb_outstanding = False
        self._low_color_sent = False
        self._last_keep_alive = 0.0
        self._last_fb_request = 0.0
        self.remote_leds = None
        self._listed_sessions.clear()
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def send_kick(self, session_id: int) -> None:
        """Ask BMC to drop another session (Java sendPrivilegeCtrl type 0)."""
        log.info("kick session id=%s", session_id)
        self._write(build_kick_request(session_id))

    def _wait_for_session_list(self, seconds: float = 5.0) -> list[int]:
        """Let the reader collect privilege type=1 user-list entries."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._listed_sessions:
                # Keep listening a bit so late rows arrive
                time.sleep(0.6)
                break
            if self.reader_error is not None or not self.reader_alive:
                break
            time.sleep(0.1)
        return sorted(self._listed_sessions)

    def _kick_listed_sessions(self) -> int:
        targets = sorted(self._listed_sessions)
        for sid in targets:
            try:
                self.send_kick(sid)
            except (OSError, ConnectionError) as exc:
                log.warning("kick %s failed: %s", sid, exc)
        return len(targets)

    def _connect_fresh(self) -> None:
        self.close(quiet=True)
        self._reset_session_state()
        self.connect()
        self.start_reader()
        self.km_ok = bool(self.server_init and self.server_init.km_enable)

    def recover_km(self, *, attempts: int = 4) -> bool:
        """Free zombie KM sessions (kick + reconnect). Returns True if KM ok."""
        if self.server_init and self.server_init.km_enable:
            self.km_ok = True
            return True
        self.km_ok = False
        for round_i in range(1, max(1, attempts) + 1):
            si = self.server_init
            log.warning(
                "KM recover round %s/%s (video=%s kick=%s sessions=%s)",
                round_i,
                attempts,
                si.video_enable if si else "?",
                si.kick_enable if si else "?",
                sorted(self._listed_sessions),
            )
            listed = self._wait_for_session_list(5.0 if round_i == 1 else 3.0)
            n = self._kick_listed_sessions()
            if n == 0:
                log.warning("round %s: empty user-list — reconnect anyway", round_i)
            else:
                time.sleep(1.2)
            try:
                self._connect_fresh()
            except (OSError, ConnectionError) as exc:
                log.warning("KM recover reconnect failed: %s", exc)
                time.sleep(1.5)
                try:
                    self._connect_fresh()
                except (OSError, ConnectionError) as exc2:
                    log.warning("KM recover retry failed: %s", exc2)
                    continue
            if self.km_ok:
                log.info("KM re-enabled on recover round %s", round_i)
                return True
            time.sleep(1.0)
        log.error(
            "KM still disabled after %s rounds — restart iKVM in the BMC web UI",
            attempts,
        )
        return False

    def reconnect(self) -> None:
        """Tear down the old session and open a fresh one (same ConnectConfig)."""
        log.info("reconnecting %s:%s", self.config.host, self.config.port)
        self._connect_fresh()
        if not self.km_ok:
            self.recover_km(attempts=4)

    def _account_rx(self, n: int) -> None:
        if n <= 0:
            return
        with self._bw_lock:
            self._rx_total += n
            self._rx_window += n

    def _account_tx(self, n: int) -> None:
        if n <= 0:
            return
        with self._bw_lock:
            self._tx_total += n
            self._tx_window += n

    def bandwidth_totals(self) -> tuple[int, int]:
        """Return (rx_bytes, tx_bytes) since process start / client creation."""
        with self._bw_lock:
            return self._rx_total, self._tx_total

    def maybe_log_bandwidth(self, interval: float = _BANDWIDTH_LOG_INTERVAL_S) -> None:
        """INFO line with window rate + cumulative totals (console via logging)."""
        if not self.config.bandwidth_stats:
            return
        now = time.monotonic()
        if now - self._last_bw_log < interval:
            return
        with self._bw_lock:
            rx_w = self._rx_window
            tx_w = self._tx_window
            rx_t = self._rx_total
            tx_t = self._tx_total
            started = self._bw_window_start
            self._rx_window = 0
            self._tx_window = 0
            self._bw_window_start = now
            self._last_bw_log = now
        elapsed = max(now - started, 1e-3)
        # Window left: volume + bitrate. Far right: cumulative transferred *bytes*
        # (shown as KiB/MiB/GiB), not bits.
        log.info(
            "bandwidth: ↓ %s (%s)  ↑ %s (%s)  | total(bytes) ↓ %s  ↑ %s",
            _fmt_bytes(rx_w),
            _fmt_bitrate(rx_w / elapsed),
            _fmt_bytes(tx_w),
            _fmt_bitrate(tx_w / elapsed),
            _fmt_bytes(rx_t),
            _fmt_bytes(tx_t),
        )

    def log_bandwidth_summary(self, *, prefix: str = "bandwidth") -> None:
        if not self.config.bandwidth_stats:
            return
        rx, tx = self.bandwidth_totals()
        log.info("%s (bytes): ↓ %s  ↑ %s", prefix, _fmt_bytes(rx), _fmt_bytes(tx))

    def _read(self, n: int) -> bytes:
        assert self._file is not None
        data = read_exact(self._file, n)
        self._account_rx(len(data))
        return data

    def _write(self, data: bytes) -> None:
        """All client→server bytes go through one locked sendall path."""
        with self._send_lock:
            if self.sock is None:
                raise ConnectionError("not connected")
            self.sock.sendall(data)
            self._account_tx(len(data))

    # --- Handshake ---

    def _handshake(self) -> None:
        ver = self._read(12)
        major, minor = parse_rfb_version(ver)
        log.info("server version %r → %s.%s", ver, major, minor)
        if self.config.require_rfb_38 and (major != RFB_MAJOR or minor != RFB_MINOR):
            raise ConnectionError(
                f"expected RFB {RFB_MAJOR}.{RFB_MINOR}, got {major}.{minor}"
            )
        self._write(ver)

        # ProcSecurity: always pick the LAST advertised type (libiKVM)
        ntypes = self._read(1)[0]
        types = self._read(ntypes) if ntypes else b""
        log.info("security types: %s", list(types))
        if not types:
            raise ConnectionError("server offered no security types")
        chosen = types[-1]
        self._write(bytes([chosen]))
        log.debug("chose security type %s", chosen)

        challenge = self._read(24)
        log.debug("aten challenge (%d B): %s", len(challenge), challenge.hex())

        self._write(pad24(self.config.username) + pad24(self.config.password))

        result = struct.unpack(">I", self._read(4))[0]
        if result != 0:
            reason = b""
            try:
                reason_len = struct.unpack(">I", self._read(4))[0]
                reason = self._read(min(reason_len, 4096))
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(
                f"auth failed (result={result}): {reason!r}"
            )
        log.info("authentication ok")

    def _send_client_init(self) -> None:
        self._write(bytes([0]))  # exclusive

    def _read_server_init(self) -> ServerInit:
        """ProcServerInit: classic RFB header + skip(4)+u32+4 caps."""
        width_u, height_u = struct.unpack(">HH", self._read(4))
        width, height = as_signed16(width_u), as_signed16(height_u)
        pf = PixelFormat.from_bytes(self._read(16))
        name_len = struct.unpack(">I", self._read(4))[0]
        if name_len > 1_000_000:
            raise ConnectionError(f"implausible desktop name length {name_len}")
        name = self._read(name_len).decode("utf-8", errors="replace")

        # Pseudocode ProcServerInit trailer (not lens 8+4 undifferentiated):
        padding4 = self._read(4)
        privilege_word = struct.unpack(">I", self._read(4))[0]
        caps = self._read(4)
        log.debug(
            "server-init pad=%s priv=0x%08x caps=%s",
            padding4.hex(),
            privilege_word,
            caps.hex(),
        )
        return ServerInit(
            width=width,
            height=height,
            pixel_format=pf,
            name=name,
            privilege_word=privilege_word,
            video_enable=caps[0],
            km_enable=caps[1],
            kick_enable=caps[2],
            vusb_enable=caps[3],
        )

    # --- Client messages ---

    def _maybe_send_deferred_low_color(self) -> None:
        """Hermon Low Color after the session is alive (safer than at connect)."""
        if not self.config.low_color or self._low_color_sent:
            return
        try:
            self._write(build_screen_set_info(1, 0))
            self._low_color_sent = True
            log.info("requested low-color video (deferred, lighter traffic)")
        except (OSError, ConnectionError) as exc:
            log.warning("deferred low-color failed: %s", exc)

    def request_framebuffer(self, incremental: bool = True) -> None:
        assert self.fb is not None
        self._write(
            build_fb_update_request(
                incremental, 0, 0, self.fb.width, self.fb.height
            )
        )
        self._last_fb_request = time.monotonic()
        self._fb_outstanding = True

    def maybe_request_framebuffer(self) -> None:
        """Throttle FB requests; never stack requests on a slow BMC."""
        if self._fb_outstanding:
            return
        fps = max(self.config.max_fps, 0.2)
        interval = 1.0 / fps
        now = time.monotonic()
        if now - self._last_fb_request < interval:
            return
        self.request_framebuffer(incremental=self._got_first_frame)

    def send_key(self, hid_usage: int, down_flag: int | bool) -> None:
        """Send one ATEN KeyEvent. down_flag: 0=up, 1=down, 2=STABLE tap."""
        flag = int(down_flag)
        pkt = build_key_event(flag, hid_usage)
        log.debug("TX KeyEvent hid=%s down=%s %s", hid_usage, flag, pkt.hex())
        try:
            self._write(pkt)
        except (OSError, ConnectionError) as exc:
            log.warning("send_key failed: %s", exc)
            if not self._closing and self.reader_error is None:
                self.reader_error = ConnectionError(f"send failed: {exc}")

    def send_key_tap(self, hid_usage: int, hold_s: float = 0.04) -> None:
        """NORMAL-mode tap: down=1, brief hold, down=0."""
        self.send_key(hid_usage, 1)
        time.sleep(hold_s)
        self.send_key(hid_usage, 0)

    def send_key_chord(self, hid_usages: list[int], hold_s: float = 0.06) -> None:
        """Hold several keys together (mods first), then release in reverse."""
        if not hid_usages:
            return
        for hid in hid_usages:
            self.send_key(hid, 1)
        time.sleep(hold_s)
        for hid in reversed(hid_usages):
            self.send_key(hid, 0)

    def send_stroke(self, stroke: KeyStroke, hold_s: float = 0.03) -> None:
        """One KeyStroke (optional mods + key) as NORMAL down/up."""
        for mid in stroke.mods:
            self.send_key(mid, 1)
        self.send_key(stroke.hid, 1)
        time.sleep(hold_s)
        self.send_key(stroke.hid, 0)
        for mid in reversed(stroke.mods):
            self.send_key(mid, 0)

    def send_ctrl_alt_del(self) -> None:
        """Remote Ctrl+Alt+Del (HID Control_L + Alt_L + Delete)."""
        log.info("macro CAD: Ctrl+Alt+Del")
        self.send_key_chord([224, 226, 76])

    def send_ctrl_alt_fn(self, n: int) -> None:
        """Remote Ctrl+Alt+Fn — Linux virtual console switch (ttyN).

        HID F1=58 … F12=69. Typical: F1→tty1, F2→tty2, …
        """
        if not 1 <= n <= 12:
            raise ValueError(f"Fn out of range: {n}")
        hid_fn = 57 + n
        log.info("macro Ctrl+Alt+F%s (VT/tty)", n)
        self.send_key_chord([224, 226, hid_fn])

    def send_sysrq(self, command: str, *, hold_s: float = 0.08) -> None:
        """Linux Magic SysRq: Alt + Print/SysRq + command key.

        ``command`` is one letter or digit (e.g. ``s`` sync, ``b`` reboot).
        Requires kernel SysRq enabled on the guest (``kernel.sysrq``).
        """
        cmd = (command or "").strip().lower()
        if len(cmd) != 1 or not (cmd.isalpha() or cmd.isdigit()):
            raise ValueError(f"SysRq command must be one letter/digit, got {command!r}")
        if cmd.isalpha():
            hid_cmd = 4 + (ord(cmd) - ord("a"))
        else:
            hid_cmd = 39 if cmd == "0" else 29 + int(cmd)
        hid_alt, hid_sysrq = 226, 70  # Alt_L, Print/SysRq
        log.info("macro SysRq-%s (Alt+Print+%s)", cmd, cmd)
        self.send_key(hid_alt, 1)
        self.send_key(hid_sysrq, 1)
        time.sleep(hold_s)
        self.send_key(hid_cmd, 1)
        time.sleep(hold_s)
        self.send_key(hid_cmd, 0)
        self.send_key(hid_sysrq, 0)
        self.send_key(hid_alt, 0)

    def send_sysrq_reisub(self, *, step_s: float = 1.0) -> None:
        """Emergency sequence R-E-I-S-U-B (unraw, term, kill, sync, umount, reboot)."""
        log.info("macro SysRq REISUB")
        for ch in "reisub":
            self.send_sysrq(ch)
            time.sleep(step_s)

    def send_del(self) -> None:
        """Remote Delete — SuperMicro X10: enter BIOS Setup during POST."""
        log.info("macro Del: BIOS Setup")
        self.send_key_tap(76)

    def send_f11(self) -> None:
        """Remote F11 — SuperMicro X10: BIOS Boot Menu during POST."""
        log.info("macro F11: Boot Menu")
        self.send_key_tap(68)

    def send_f12(self) -> None:
        """Remote F12 — SuperMicro X10: Network / PXE boot during POST."""
        log.info("macro F12: Network/PXE boot")
        self.send_key_tap(69)

    def send_chars(
        self,
        text: str,
        *,
        remote_layout: str = "us",
        hold_s: float = 0.03,
        inter_s: float = 0.04,
    ) -> None:
        """Type a string as HID strokes (guest layout aware)."""
        from .keys import char_to_stroke

        for i, ch in enumerate(text):
            stroke = char_to_stroke(ch, remote_layout=remote_layout)
            if stroke is None:
                log.warning("send_chars: skip unmapped %r", ch)
                continue
            self.send_stroke(stroke, hold_s=hold_s)
            if inter_s > 0 and i + 1 < len(text):
                time.sleep(inter_s)

    def send_ctrl_page_up(self, *, remote_layout: str = "us") -> None:
        """xterm Ctrl+PageUp CSI — what GNU screen bindkey usually matches.

        HID Ctrl+Prior often never becomes ``\\e[5;5~`` on a Linux VT; screenrc
        bindings for Ctrl+PgUp expect this escape sequence instead.
        """
        log.info("macro Ctrl+PageUp as CSI \\e[5;5~ (layout=%s)", remote_layout)
        self.send_chars("\x1b[5;5~", remote_layout=remote_layout)

    def send_ctrl_page_down(self, *, remote_layout: str = "us") -> None:
        """xterm Ctrl+PageDown CSI — ``\\e[6;5~`` for screen bindkey."""
        log.info("macro Ctrl+PageDown as CSI \\e[6;5~ (layout=%s)", remote_layout)
        self.send_chars("\x1b[6;5~", remote_layout=remote_layout)

    def sync_lock_keys_to_local(self, remote: Optional[LedState] = None) -> None:
        """Toggle remote Caps/Num/Scroll so they match the local keyboard LEDs.

        Java's changeLEDstate only flips *local* LEDs (cosmetic). Typing case
        follows the *guest/BMC* Caps state, so we pulse HID lock keys instead.
        """
        if remote is None:
            remote = self.remote_leds
        local = read_local_leds()
        if local is None or remote is None:
            log.info(
                "LED sync skipped (local=%s remote=%s) — press F9 to toggle remote Caps",
                local,
                remote,
            )
            return
        with self._led_sync_lock:
            if self._led_syncing:
                return
            self._led_syncing = True
        try:
            # HID: Caps=57, Scroll=71, Num=83
            fixes: list[tuple[str, int, bool, bool]] = [
                ("Caps", 57, remote.caps, local.caps),
                ("Num", 83, remote.num, local.num),
                ("Scroll", 71, remote.scroll, local.scroll),
            ]
            for name, hid, r_on, l_on in fixes:
                if r_on == l_on:
                    continue
                log.info(
                    "LED sync %s: remote=%s local=%s → tap HID %s",
                    name,
                    r_on,
                    l_on,
                    hid,
                )
                self.send_key_tap(hid)
                # Optimistic update; next session type-7 will confirm
                if hid == 57:
                    remote = LedState(local.caps, remote.num, remote.scroll)
                elif hid == 83:
                    remote = LedState(remote.caps, local.num, remote.scroll)
                else:
                    remote = LedState(remote.caps, remote.num, local.scroll)
            self.remote_leds = remote
        finally:
            with self._led_sync_lock:
                self._led_syncing = False

    def toggle_remote_caps(self) -> None:
        """Manual escape hatch (F9): pulse remote Caps Lock."""
        log.info("toggle remote Caps Lock (HID 57)")
        self.send_key_tap(57)
        if self.remote_leds is not None:
            r = self.remote_leds
            self.remote_leds = LedState(not r.caps, r.num, r.scroll)

    def send_pointer(self, x: int, y: int, buttons: int = 0, wheel: int = 0) -> None:
        """Pointer + optional wheel bursts (MouseAction semantics).

        Motion is capped (~25 Hz). Button/wheel changes are sent immediately.
        """
        try:
            if wheel != 0:
                wheel_bits = 8 + (8 if wheel <= 0 else 0)
                for _ in range(min(abs(wheel), 4)):
                    self._write(build_pointer_event(buttons | wheel_bits, x, y))
                    self._write(build_pointer_event(buttons, x, y))
                self._last_pointer_buttons = buttons
                self._last_pointer_at = time.monotonic()
                return

            now = time.monotonic()
            button_changed = buttons != self._last_pointer_buttons
            if (
                not button_changed
                and now - self._last_pointer_at < self._pointer_min_interval
            ):
                return
            self._write(build_pointer_event(buttons, x, y))
            self._last_pointer_buttons = buttons
            self._last_pointer_at = now
        except (OSError, ConnectionError) as exc:
            log.debug("send_pointer failed: %s", exc)

    def send_mouse_sync(self, value: int = 1920) -> None:
        self._write(build_mouse_sync(value))

    def maybe_keep_alive(self, interval: float = 1.0) -> None:
        """Send keep-alive; raises on dead socket so the reader can stop.

        ~1s probes + TCP_USER_TIMEOUT so a VPN drop surfaces in a few seconds
        instead of only when the tunnel comes back.
        """
        now = time.monotonic()
        if now - self._last_keep_alive >= interval:
            self._write(build_keep_alive(0))
            self._last_keep_alive = now

    def _socket_dead(self) -> bool:
        """True if the kernel already knows the TCP session is broken."""
        sock = self.sock
        if sock is None:
            return True
        try:
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError:
            return True
        return err != 0

    # --- Server message loop ---

    def poll_once(self, wait: float = 0.05) -> bool:
        """Wait up to `wait` seconds for data, then read one full message.

        Uses select + blocking reads so large Hermon frames (~1.5 MiB) are
        never interrupted by a short socket timeout.
        Returns False when the peer is gone.
        """
        assert self._file is not None and self.fb is not None
        assert self.sock is not None

        if self._socket_dead():
            return False

        self.sock.settimeout(None)
        try:
            # Also watch "exceptional" — often set when the path dies
            readable, _, exceptional = select.select(
                [self.sock], [], [self.sock], wait
            )
        except (OSError, ValueError):
            return False
        if exceptional or self._socket_dead():
            return False
        if not readable:
            try:
                self.maybe_keep_alive(interval=1.0)
                self.maybe_request_framebuffer()
            except (OSError, ConnectionError):
                return False
            if self._socket_dead():
                return False
            self.maybe_log_bandwidth()
            return True

        try:
            msg_id = self._read(1)[0]
        except (ConnectionError, OSError) as exc:
            log.warning("connection lost while waiting for server message: %s", exc)
            return False

        if msg_id == SMSG_FRAMEBUFFER_UPDATE:
            self._handle_screen_decode()
        elif msg_id == SMSG_FRONT_GROUND:
            # lens: 20 bytes; keep consuming fixed size for scaffold stability
            self._read(20)
            log.debug("front-ground-event")
        elif msg_id == SMSG_KEEP_ALIVE:
            self._read(1)
            log.debug("keep-alive")
        elif msg_id == SMSG_VIDEO_GET_INFO:
            self._read(4)
            log.debug("video-get-info")
        elif msg_id == SMSG_KEYBOARD_INFO:
            # ProcKeyboardInfo reads two u8 in native code
            a, b = self._read(2)
            log.debug("keyboard-info %s %s", a, b)
        elif msg_id == SMSG_MOUSE_GET_INFO:
            self._read(2)
            log.debug("mouse-get-info")
        elif msg_id == SMSG_SESSION_MSG:
            self._handle_session_msg()
        elif msg_id == SMSG_VIEWER_LANG:
            self._read(8)
            log.debug("viewer-lang")
        else:
            # Never skip unknown IDs — stream desync can wedge the BMC session
            raise ConnectionError(f"unhandled server message id 0x{msg_id:02x}")
        self.maybe_log_bandwidth()
        return True

    @staticmethod
    def _session_id_from_user_payload(payload: bytes) -> Optional[int]:
        """Parse 'sessionId username ip' from privilege type=1 payload."""
        try:
            text = payload.split(b"\x00", 1)[0].decode("ascii", errors="replace")
            first = text.split(" ", 1)[0].strip()
            if first.isdigit():
                return int(first)
        except (TypeError, ValueError):
            return None
        return None

    def _handle_session_msg(self) -> None:
        """Privilege / session (0x39): u32 id, u32 type, 256-byte payload."""
        sid, typ = struct.unpack(">II", self._read(8))
        payload = self._read(0x100)
        log.debug("session-msg id=%s type=%s", sid, typ)
        # type 1 = user joined (UserList); payload starts with session id
        if typ == 1:
            listed = self._session_id_from_user_payload(payload)
            if listed is not None:
                self._listed_sessions.add(listed)
                log.info("session list: id=%s", listed)
            elif sid:
                self._listed_sessions.add(sid)
        elif typ == 2:
            listed = self._session_id_from_user_payload(payload)
            if listed is not None:
                self._listed_sessions.discard(listed)
            self._listed_sessions.discard(sid)
        # Java privilegeCtrl(i, i2=7, bArr): LED bits in bArr[3]
        elif typ == 7 and len(payload) > 3:
            remote = remote_leds_from_session_byte(payload[3])
            self.remote_leds = remote
            log.info(
                "remote LEDs caps=%s num=%s scroll=%s (local will be matched)",
                remote.caps,
                remote.num,
                remote.scroll,
            )
            # Align BMC lock state to the local keyboard (fixes stuck Caps)
            self.sync_lock_keys_to_local(remote)

    def _handle_screen_decode(self) -> None:
        """ScreenDecode after msg id 0 (libiKVM): skip 3, one rect + flag + len."""
        assert self.fb is not None
        self._read(3)  # pad(1) + nRects(u16) — ignored by native client

        x_u, y_u, w_u, h_u = struct.unpack(">HHHH", self._read(8))
        encoding = struct.unpack(">I", self._read(4))[0]
        # encoding may be written as signed in some dumps; keep unsigned wire value
        aten_flag = struct.unpack(">I", self._read(4))[0]
        data_len = struct.unpack(">I", self._read(4))[0]
        if data_len > self._max_frame_bytes:
            raise ConnectionError(
                f"implausible frame payload length {data_len} (limit {self._max_frame_bytes})"
            )

        x, y = as_signed16(x_u), as_signed16(y_u)
        w, h = as_signed16(w_u), as_signed16(h_u)
        if w < 0:
            w = -w
        if h < 0:
            h = -h

        payload = self._read(data_len) if data_len > 0 else b""
        log.debug(
            "ScreenDecode %s,%s %sx%s enc=%s (%s) aten_flag=%s len=%s",
            x,
            y,
            w,
            h,
            encoding,
            describe_encoding(encoding),
            aten_flag,
            data_len,
        )

        rect = RectUpdate(x, y, w, h, encoding, aten_flag, payload)
        prev_size = (self.fb.width, self.fb.height)
        status = apply_rect_to_framebuffer(rect, self.fb)
        resized = (self.fb.width, self.fb.height) != prev_size
        if resized:
            log.info(
                "framebuffer size %sx%s → %sx%s (mode change)",
                prev_size[0],
                prev_size[1],
                self.fb.width,
                self.fb.height,
            )
        log.debug("%s", status)
        first = not self._got_first_frame
        self._got_first_frame = True
        self._fb_outstanding = False
        self._publish_frame(self.fb)
        if first:
            self._maybe_send_deferred_low_color()
        if resized:
            # Match Java refresh() after changeResolution — full frame, new geometry
            self.request_framebuffer(incremental=False)
        else:
            # Pace the next request; avoids bursting the BMC encode path
            self.maybe_request_framebuffer()

    def run_forever(self) -> None:
        try:
            while self.poll_once():
                pass
        finally:
            self.close()
