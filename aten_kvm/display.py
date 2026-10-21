"""Tk viewer: scale-to-fit local display, map mouse back to KVM coords."""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageTk

from .client import AtenKvmClient
from .config import normalize_view_mode
from .framebuffer import Framebuffer
from .keys import (
    PASTE_HOLD_MS,
    PASTE_INTER_MS,
    PASTE_MAX_CHARS,
    KeyStroke,
    char_to_stroke,
    tk_event_to_stroke,
)

log = logging.getLogger(__name__)

# Leave room for window decorations / panel
_SCREEN_MARGIN = 80

# Macro toolbar placement (CLI --toolbar)
_TOOLBAR_SIDES = frozenset({"left", "right", "top", "bottom"})

# USB HID modifier / latch keys — held until KeyRelease
_HID_MODIFIERS = frozenset(
    {
        224,  # Control_L
        225,  # Shift_L
        226,  # Alt_L
        227,  # GUI_L
        228,  # Control_R
        229,  # Shift_R
        230,  # Alt_R
        231,  # GUI_R
    }
)


class TkViewer:
    def __init__(
        self,
        client: AtenKvmClient,
        title: str = "aten-kvm scaffold",
        *,
        enable_mouse: bool = False,
        remote_layout: str = "us",
        brighten: float = 1.0,
        contrast: float = 1.0,
        toolbar: str = "left",
        server_name: str = "",
        cad_hotkey: bool = False,
        sysrq_toolbar: bool = False,
        snap_toolbar: bool = True,
        view_mode: str = "fit",
        view_toggle_toolbar: bool = False,
        fkey_passthrough: bool = False,
    ) -> None:
        self.client = client
        self.root = tk.Tk()
        self.root.title(title)
        side = (toolbar or "off").lower().strip()
        if side in ("none", "no", "false", "0"):
            side = "off"
        self._toolbar_side = side if side in _TOOLBAR_SIDES else "off"
        self._cad_hotkey = bool(cad_hotkey)
        self._sysrq_toolbar = bool(sysrq_toolbar)
        self._snap_toolbar = bool(snap_toolbar)
        self._view_toggle_toolbar = bool(view_toggle_toolbar)
        self._fkey_passthrough = bool(fkey_passthrough)
        self._view_mode = normalize_view_mode(view_mode)
        self._canvas_image_id: Optional[int] = None
        self._img_ox = 0
        self._img_oy = 0
        self._scrollregion: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._configure_after: Optional[str] = None
        self._view_mode_btn: Optional[tk.Button] = None
        self._fkey_pass_strip: Optional[tk.Frame] = None
        self._last_title = ""
        host = (getattr(client.config, "host", "") or "").strip()
        name = (server_name or "").strip()
        # Avoid duplicating IP when CLI --host was used as the only label
        self._server_name = name if name and name != host else ""
        self._server_host = host or "?"
        if self._server_name:
            self._server_label = f"{self._server_name} · {self._server_host}"
        else:
            self._server_label = self._server_host
        self._toolbar_ip_photo: Optional[ImageTk.PhotoImage] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._pending: Optional[Framebuffer] = None
        self._last_fb: Optional[Framebuffer] = None
        self._last_paint_digest: Optional[bytes] = None
        self._last_paint_view: tuple[int, int] = (0, 0)
        self._fb_w = 0
        self._fb_h = 0
        self._view_w = 0
        self._view_h = 0
        self._scale = 1.0
        self._buttons = 0
        self._closed = False
        self._keys_down: set[int] = set()  # main HID usages currently down
        self._down_at: dict[int, float] = {}
        self._up_after: dict[int, str] = {}
        self._stroke_by_hid: dict[int, KeyStroke] = {}
        self._hid_by_keycode: dict[int, int] = {}  # physical keycode → active HID
        self._layout_mods: set[int] = set()  # mods we injected for layout remap
        self._fullscreen = False
        self._enable_mouse = enable_mouse
        self._remote_layout = remote_layout or "us"
        # Local-only picture controls (1.0 = unchanged). Useful with Hermon low-color.
        self._brighten = max(0.1, float(brighten))
        self._contrast = max(0.1, float(contrast))
        self._paste_cancel = False
        self._paste_busy = False
        self._paste_dialog: Optional[tk.Toplevel] = None
        # Java KeyboardMode.NORMAL: down=1 / up=0. (STABLE down=2 produced
        # delayed newlines on this X10SLM firmware for every keystroke.)
        self._min_hold_ms = 30
        self._link_lost_notified = False
        self._status_visible = False
        self._reconnect_busy = False
        self._recover_probe_at = 0.0
        self._km_recover_at = 0.0

        # Shown only on link/KM problems (click to force reconnect)
        self._status = tk.Label(
            self.root,
            text="",
            anchor=tk.W,
            font=("DejaVu Sans", 10, "bold"),
            fg="#ffd0d0",
            bg="#5a1515",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self._status.bind("<Button-1>", self._on_status_click)

        # Pack order: toolbar edge first, then video fills remaining space
        if self._toolbar_side in ("right", "bottom"):
            self._build_video_pane()
            self._build_toolbar()
        else:
            self._build_toolbar()
            self._build_video_pane()

        # Mouse off by default — pointer floods have wedged ATEN KM on X10SLM
        if enable_mouse:
            self.label.bind("<Motion>", self._on_motion)
            self.label.bind("<ButtonPress>", self._on_button)
            self.label.bind("<ButtonRelease>", self._on_button)
            self.label.bind("<MouseWheel>", self._on_wheel)
            self.label.bind("<Button-4>", lambda e: self._on_wheel_btn(e, 1))
            self.label.bind("<Button-5>", lambda e: self._on_wheel_btn(e, -1))
        else:
            # Focus on click; in 1:1 mode wheel pans the canvas
            self.label.bind("<ButtonPress>", lambda _e: self.label.focus_set())
            self.label.bind("<MouseWheel>", self._on_canvas_wheel)
            self.label.bind("<Button-4>", lambda e: self._on_canvas_wheel_btn(e, -1))
            self.label.bind("<Button-5>", lambda e: self._on_canvas_wheel_btn(e, 1))
        self.label.bind("<Configure>", self._on_configure)
        self.root.bind_all("<KeyPress>", self._on_key)
        self.root.bind_all("<KeyRelease>", self._on_key)
        self.root.bind("<FocusOut>", self._on_focus_out)
        self._apply_local_fkey_bindings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start maximized-ish: size window to fit screen
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{max(sw - _SCREEN_MARGIN, 640)}x{max(sh - _SCREEN_MARGIN, 480)}+0+0")

        self.root.focus_force()
        self.label.focus_set()
        # Frames arrive via client.take_frame() — never touch Tk from reader thread
        client.on_frame = None

    def _build_video_pane(self) -> None:
        """Canvas + scrollbars (Java JScrollPane equivalent). ``self.label`` is the Canvas."""
        frame = tk.Frame(self.root, bg="black")
        frame.pack(fill=tk.BOTH, expand=True)
        vbar = tk.Scrollbar(frame, orient=tk.VERTICAL)
        hbar = tk.Scrollbar(frame, orient=tk.HORIZONTAL)
        canvas = tk.Canvas(
            frame,
            bg="black",
            highlightthickness=0,
            xscrollcommand=hbar.set,
            yscrollcommand=vbar.set,
        )
        vbar.configure(command=canvas.yview)
        hbar.configure(command=canvas.xview)
        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self._video_frame = frame
        self._vbar = vbar
        self._hbar = hbar
        self.label = canvas  # keep attribute name used across focus/handlers
        self._sync_scrollbar_visibility()

    def _sync_scrollbar_visibility(self) -> None:
        """Show scrollbars only in native (1:1) mode."""
        if self._view_mode == "native":
            self._vbar.grid()
            self._hbar.grid()
        else:
            self._vbar.grid_remove()
            self._hbar.grid_remove()
            try:
                self.label.xview_moveto(0)
                self.label.yview_moveto(0)
            except tk.TclError:
                pass

    @staticmethod
    def _render_host_badge(
        host: str,
        *,
        side: str,
        name: str = "",
    ) -> ImageTk.PhotoImage:
        """BMC label: name above IP (name larger); 90° on left/right rails."""
        host = host.strip() or "?"
        name = name.strip()
        try:
            font_name = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14
            )
            font_host = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11
            )
        except OSError:
            font_name = ImageFont.load_default()
            font_host = font_name

        tmp = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(tmp)
        lines: list[tuple[str, object, str]] = []
        if name:
            lines.append((name, font_name, "#f0f0f0"))
        lines.append((host, font_host, "#9a9a9a"))

        sizes: list[tuple[int, int, tuple[int, int, int, int]]] = []
        for text, font, _color in lines:
            bbox = draw.textbbox((0, 0), text, font=font)
            sizes.append((bbox[2] - bbox[0], bbox[3] - bbox[1], bbox))

        # Generous gap — after 90° rotate this becomes spacing along the rail
        gap = 10 if len(lines) > 1 else 0
        tw = max(w for w, _h, _b in sizes)
        th = sum(h for _w, h, _b in sizes) + gap * (len(lines) - 1)
        pad_x, pad_y = 10, 8
        img = Image.new("RGB", (tw + pad_x * 2, th + pad_y * 2), "#1a1a1a")
        draw = ImageDraw.Draw(img)
        y = pad_y
        for (text, font, color), (w, h, bbox) in zip(lines, sizes):
            x = pad_x + (tw - w) // 2 - bbox[0]
            draw.text((x, y - bbox[1]), text, fill=color, font=font)
            y += h + gap

        if side == "left":
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif side == "right":
            img = img.transpose(Image.Transpose.ROTATE_270)
        return ImageTk.PhotoImage(img)

    @staticmethod
    def _layout_hint(remote_layout: str) -> tuple[str, str]:
        """Return (short label, hover detail) for toolbar keyboard mapping.

        Labels describe the filter, not detected OS layouts:
        straight = no filter; remap → de = char→HID for DE guest.
        """
        remote = (remote_layout or "us").lower()
        if remote in ("de", "de-de", "german", "qwertz"):
            return "remap → de", "filter: typed characters → HID for DE guest"
        return "straight", "no filter (keysym/key → HID passthrough)"

    def _build_toolbar(self) -> None:
        side = self._toolbar_side
        if side == "off":
            return

        vertical = side in ("left", "right")
        pack_side = {"left": tk.LEFT, "right": tk.RIGHT, "top": tk.TOP, "bottom": tk.BOTTOM}[
            side
        ]
        bar = tk.Frame(
            self.root,
            bg="#1a1a1a",
            padx=4 if vertical else 6,
            pady=6 if vertical else 4,
        )
        bar.pack(side=pack_side, fill=tk.Y if vertical else tk.X)

        # SuperMicro X10 POST: F11 = Boot Menu, F12 = Network/PXE
        # Native Tk text (not PIL bitmaps) — sharp and readable on small screens
        btn_pack = tk.TOP if vertical else tk.LEFT
        btn_pad = {"pady": 3, "fill": tk.X} if vertical else {"padx": 3}
        macros_before_vt = (
            ("CAD", "Ctrl+Alt+Del", self._on_cad),
            ("Del\nSetup", "BIOS Setup (POST)", self._on_del_setup),
            ("F11\nBoot", "BIOS Boot Menu", self._on_f11_boot),
            ("F12\nPXE", "Network / PXE boot", self._on_f12_pxe),
        )
        macros_after_vt: list[tuple[str, str, object]] = [
            ("Scr\nUp", "screen: CSI Ctrl+PageUp (\\e[5;5~)", self._on_ctrl_page_up),
            ("Scr\nDn", "screen: CSI Ctrl+PageDown (\\e[6;5~)", self._on_ctrl_page_down),
        ]
        if self._snap_toolbar:
            macros_after_vt.append(
                ("Snap", "Copy remote screen to clipboard (PNG)", self._on_screenshot_clipboard)
            )
        macros_after_vt.append(("Paste", "Paste text as keystrokes", self._on_paste))
        for label, tip, handler in macros_before_vt:
            self._pack_toolbar_button(bar, label, tip, handler, btn_pack, btn_pad)
        if self._view_toggle_toolbar:
            self._view_mode_btn = self._pack_toolbar_button(
                bar,
                self._view_mode_button_label(),
                self._view_mode_button_tip(),
                self._on_toggle_view_mode,
                btn_pack,
                btn_pad,
            )
        self._pack_vt_menu_button(bar, btn_pack, btn_pad)
        if self._sysrq_toolbar:
            self._pack_sysrq_menu_button(bar, btn_pack, btn_pad)
        for label, tip, handler in macros_after_vt:
            self._pack_toolbar_button(bar, label, tip, handler, btn_pack, btn_pad)

        layout_text, layout_detail = self._layout_hint(self._remote_layout)
        self._toolbar_ip_photo = self._render_host_badge(
            self._server_host,
            side=side,
            name=self._server_name,
        )
        # Host badge may be rotated; layout hint stays upright under/beside it
        host_box = tk.Frame(bar, bg="#1a1a1a")
        ip_lbl = tk.Label(
            host_box,
            image=self._toolbar_ip_photo,
            bg="#1a1a1a",
            bd=0,
            highlightthickness=0,
        )
        layout_lbl = tk.Label(
            host_box,
            text=layout_text,
            bg="#1a1a1a",
            fg="#7a9aaa",
            bd=0,
            highlightthickness=0,
            font=("DejaVu Sans", 9),
        )
        # Slim F-key passthrough bar under layout (color = state; click toggles)
        fkey_strip = tk.Frame(
            host_box,
            height=5,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        fkey_strip.pack_propagate(False)
        self._fkey_pass_strip = fkey_strip
        self._style_fkey_pass_strip()
        fkey_strip.bind("<Button-1>", self._on_toggle_fkey_passthrough)
        tip = f"BMC {self._server_label}  ·  {layout_text} ({layout_detail})"
        for w in (host_box, ip_lbl, layout_lbl):
            w.bind("<Enter>", lambda _e, t=tip: self.root.title(f"aten-kvm — {t}"))
            w.bind("<Leave>", lambda _e: self._restore_title())
        fkey_strip.bind(
            "<Enter>",
            lambda _e: self.root.title(f"aten-kvm — {self._fkey_pass_button_tip()}"),
        )
        fkey_strip.bind("<Leave>", lambda _e: self._restore_title())
        if vertical:
            ip_lbl.pack(side=tk.TOP)
            layout_lbl.pack(side=tk.TOP, pady=(4, 0))
            fkey_strip.pack(side=tk.TOP, fill=tk.X, pady=(3, 0))
            host_box.pack(side=tk.BOTTOM, pady=(8, 4))
        else:
            ip_lbl.pack(side=tk.TOP)
            layout_lbl.pack(side=tk.TOP, pady=(2, 0))
            fkey_strip.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))
            host_box.pack(side=tk.RIGHT, padx=(8, 4))

    def _restore_title(self) -> None:
        if self._last_fb is not None:
            self._update_title(self._last_fb.width, self._last_fb.height, self._scale)
        else:
            self.root.title("aten-kvm")

    def _toolbar_button_kwargs(self) -> dict:
        return dict(
            takefocus=0,
            justify=tk.CENTER,
            font=("DejaVu Sans", 11, "bold"),
            fg="#ffffff",
            bg="#3d3d3d",
            activeforeground="#ffffff",
            activebackground="#5a5a5a",
            disabledforeground="#888888",
            relief=tk.RAISED,
            bd=1,
            highlightthickness=0,
            padx=8,
            pady=6,
        )

    def _pack_toolbar_button(
        self,
        bar: tk.Frame,
        label: str,
        tip: str,
        handler: object,
        btn_pack: str,
        btn_pad: dict,
    ) -> tk.Button:
        btn = tk.Button(bar, text=label, command=handler, **self._toolbar_button_kwargs())
        btn.pack(side=btn_pack, **btn_pad)
        btn.bind(
            "<Enter>",
            lambda _e, t=f"{label.replace(chr(10), ' ')} — {tip}": self.root.title(
                f"aten-kvm — {t}"
            ),
        )
        btn.bind("<Leave>", lambda _e: self._restore_title())
        return btn

    def _new_toolbar_menu(self) -> tk.Menu:
        return tk.Menu(
            self.root,
            tearoff=0,
            font=("DejaVu Sans", 11),
            bg="#2a2a2a",
            fg="#ffffff",
            activebackground="#5a5a5a",
            activeforeground="#ffffff",
            bd=1,
        )

    def _pack_popup_menu_button(
        self,
        bar: tk.Frame,
        label: str,
        tip: str,
        btn_pack: str,
        btn_pad: dict,
        menu: tk.Menu,
    ) -> tk.Button:
        def _open_menu(_event: object = None) -> None:
            btn.update_idletasks()
            if btn_pack == tk.TOP:
                x = btn.winfo_rootx() + btn.winfo_width()
                y = btn.winfo_rooty()
            else:
                x = btn.winfo_rootx()
                y = btn.winfo_rooty() + btn.winfo_height()
            try:
                menu.tk_popup(x, y)
            finally:
                menu.grab_release()

        btn = self._pack_toolbar_button(bar, label, tip, _open_menu, btn_pack, btn_pad)
        return btn

    def _pack_vt_menu_button(
        self, bar: tk.Frame, btn_pack: str, btn_pad: dict
    ) -> None:
        """One toolbar control: popup menu Ctrl+Alt+F1…F12 (Linux VTs)."""
        menu = self._new_toolbar_menu()
        for n in range(1, 13):
            menu.add_command(
                label=f"Ctrl+Alt+F{n}  →  tty{n}",
                command=lambda nn=n: self._on_vt(nn),
            )
        self._vt_menu = menu
        self._vt_menu_btn = self._pack_popup_menu_button(
            bar, "VT", "Linux VT: Ctrl+Alt+Fn → ttyN", btn_pack, btn_pad, menu
        )

    def _pack_sysrq_menu_button(
        self, bar: tk.Frame, btn_pack: str, btn_pad: dict
    ) -> None:
        """Magic SysRq (Alt+Print+key) — confirm before destructive commands."""
        # (command, menu label, confirm level: None | "soft" | "hard")
        entries: list[tuple[str | None, str, str | None]] = [
            ("h", "h  help (console)", None),
            ("m", "m  show memory", None),
            ("t", "t  show tasks", None),
            ("p", "p  show registers", None),
            ("r", "r  unraw (keyboard)", None),
            (None, "", None),  # separator
            ("s", "s  sync filesystems", "soft"),
            ("u", "u  remount read-only", "soft"),
            ("e", "e  SIGTERM all processes", "hard"),
            ("i", "i  SIGKILL all processes", "hard"),
            ("b", "b  immediate reboot", "hard"),
            ("o", "o  power off", "hard"),
            ("c", "c  crash / kexec", "hard"),
            (None, "", None),
            ("reisub", "REISUB  emergency reboot sequence", "hard"),
        ]
        menu = self._new_toolbar_menu()
        for cmd, label, level in entries:
            if cmd is None:
                menu.add_separator()
                continue
            menu.add_command(
                label=label,
                command=lambda c=cmd, lv=level, lb=label: self._on_sysrq(c, lv, lb),
            )
        self._sysrq_menu = menu
        self._sysrq_menu_btn = self._pack_popup_menu_button(
            bar,
            "SysRq",
            "Linux Magic SysRq (Alt+Print+key) — needs kernel.sysrq",
            btn_pack,
            btn_pad,
            menu,
        )

    def _on_cad(self, _event: object = None) -> str:
        # Confirm: CAD is disruptive; toolbar/F8 must not fire on mis-clicks
        ok = messagebox.askokcancel(
            "Ctrl+Alt+Del",
            "Send Ctrl+Alt+Del to the remote system?",
            parent=self.root,
            default=messagebox.CANCEL,
            icon=messagebox.WARNING,
        )
        if ok:
            self._release_all_keys()
            self.client.send_ctrl_alt_del()
        self.label.focus_set()
        return "break"

    def _on_del_setup(self, _event: object = None) -> None:
        self._release_all_keys()
        self.client.send_del()
        self.label.focus_set()

    def _on_f11_boot(self, _event: object = None) -> None:
        self._release_all_keys()
        self.client.send_f11()
        self.label.focus_set()

    def _on_f12_pxe(self, _event: object = None) -> None:
        self._release_all_keys()
        self.client.send_f12()
        self.label.focus_set()

    def _on_vt(self, n: int) -> None:
        self._release_all_keys()
        self.client.send_ctrl_alt_fn(n)
        self.label.focus_set()

    def _on_sysrq(self, command: str, confirm: str | None, label: str) -> None:
        if confirm == "soft":
            ok = messagebox.askokcancel(
                "Magic SysRq",
                f"Send SysRq ({label}) to the remote Linux kernel?\n\n"
                "Only works if SysRq is enabled (kernel.sysrq).",
                parent=self.root,
                default=messagebox.CANCEL,
                icon=messagebox.WARNING,
            )
            if not ok:
                self.label.focus_set()
                return
        elif confirm == "hard":
            ok = messagebox.askokcancel(
                "Magic SysRq — dangerous",
                f"Send SysRq ({label}) to the remote system?\n\n"
                "This can kill processes, reboot, or crash the machine.\n"
                "Requires kernel.sysrq enabled. Default: Cancel.",
                parent=self.root,
                default=messagebox.CANCEL,
                icon=messagebox.WARNING,
            )
            if not ok:
                self.label.focus_set()
                return
        self._release_all_keys()

        def _run() -> None:
            try:
                if command == "reisub":
                    self.client.send_sysrq_reisub()
                else:
                    self.client.send_sysrq(command)
            except (OSError, ConnectionError, ValueError) as exc:
                log.warning("SysRq failed: %s", exc)
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Magic SysRq", f"Send failed:\n{exc}", parent=self.root
                    ),
                )
            finally:
                self.root.after(0, self.label.focus_set)

        # REISUB sleeps between steps — keep Tk responsive
        if command == "reisub":
            threading.Thread(target=_run, name="sysrq-reisub", daemon=True).start()
        else:
            _run()

    def _on_ctrl_page_up(self, _event: object = None) -> None:
        self._release_all_keys()
        self.client.send_ctrl_page_up(remote_layout=self._remote_layout)
        self.label.focus_set()

    def _on_ctrl_page_down(self, _event: object = None) -> None:
        self._release_all_keys()
        self.client.send_ctrl_page_down(remote_layout=self._remote_layout)
        self.label.focus_set()

    def _framebuffer_to_png(self, fb: Framebuffer) -> bytes:
        """Native-resolution PNG (same brighten/contrast as the view, not scaled)."""
        img = Image.frombytes("RGB", (fb.width, fb.height), bytes(fb.pixels))
        if self._brighten != 1.0:
            img = ImageEnhance.Brightness(img).enhance(self._brighten)
        if self._contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(self._contrast)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _copy_png_to_clipboard(png: bytes) -> str:
        """Put PNG on the desktop clipboard. Returns tool name used."""
        if shutil.which("wl-copy"):
            subprocess.run(
                ["wl-copy", "--type", "image/png"],
                input=png,
                check=True,
            )
            return "wl-copy"
        if shutil.which("xclip"):
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"],
                input=png,
                check=True,
            )
            return "xclip"
        raise RuntimeError(
            "No image-clipboard tool found. Install wl-clipboard (wl-copy) "
            "or xclip."
        )

    def _on_screenshot_clipboard(self, _event: object = None) -> None:
        fb = self._last_fb
        if fb is None or fb.width <= 0 or fb.height <= 0:
            messagebox.showinfo(
                "Snap",
                "No framebuffer yet — wait for the first video frame.",
                parent=self.root,
            )
            self.label.focus_set()
            return
        try:
            png = self._framebuffer_to_png(fb)
            tool = self._copy_png_to_clipboard(png)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            log.warning("clipboard screenshot failed: %s", exc)
            messagebox.showwarning("Snap", f"Clipboard failed:\n{exc}", parent=self.root)
            self.label.focus_set()
            return
        log.info(
            "screenshot → clipboard via %s (%sx%s, %s KiB PNG)",
            tool,
            fb.width,
            fb.height,
            len(png) // 1024,
        )
        self.root.title(
            f"aten-kvm — Snap copied ({fb.width}x{fb.height} PNG via {tool})"
        )
        self.root.after(2000, self._restore_title)
        self.label.focus_set()

    class _DlgActionButton:
        """Bitmap button — avoids Tk text-metric clipping under broken HiDPI scaling."""

        _W, _H = 96, 36

        def __init__(
            self,
            parent: tk.Misc,
            text: str,
            command: Callable[[], None],
            keep: list[ImageTk.PhotoImage],
        ) -> None:
            self._disabled = False
            self._command = command
            self._text = text
            self._keep = keep
            self._photo = self._render(text, disabled=False, hover=False)
            keep.append(self._photo)
            self.label = tk.Label(
                parent,
                image=self._photo,
                bd=0,
                highlightthickness=0,
                bg="#2b2b2b",
            )
            self.label.bind("<Button-1>", self._on_click)
            self.label.bind("<Enter>", self._on_enter)
            self.label.bind("<Leave>", self._on_leave)

        @classmethod
        def _render(
            cls, text: str, *, disabled: bool, hover: bool
        ) -> ImageTk.PhotoImage:
            if disabled:
                bg, fg, edge = (0x33, 0x33, 0x33), (0x88, 0x88, 0x88), (0x55, 0x55, 0x55)
            elif hover:
                bg, fg, edge = (0x5e, 0x5e, 0x5e), (0xff, 0xff, 0xff), (0xc0, 0xc0, 0xc0)
            else:
                bg, fg, edge = (0x4a, 0x4a, 0x4a), (0xf0, 0xf0, 0xf0), (0x90, 0x90, 0x90)
            img = Image.new("RGB", (cls._W, cls._H), bg)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, cls._W - 1, cls._H - 1), outline=edge, width=2)
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14
                )
            except OSError:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(
                ((cls._W - tw) // 2 - bbox[0], (cls._H - th) // 2 - bbox[1]),
                text,
                fill=fg,
                font=font,
            )
            return ImageTk.PhotoImage(img)

        def _set_image(self, *, hover: bool = False) -> None:
            self._photo = self._render(self._text, disabled=self._disabled, hover=hover)
            self._keep.append(self._photo)
            self.label.configure(image=self._photo)

        def pack(self, **kwargs: object) -> None:
            self.label.pack(**kwargs)  # type: ignore[arg-type]

        def configure(self, **kwargs: object) -> None:
            if "text" in kwargs:
                self._text = str(kwargs["text"])
            state = kwargs.get("state")
            if state == tk.DISABLED:
                self._disabled = True
            elif state == tk.NORMAL:
                self._disabled = False
            self._set_image(hover=False)

        def _on_click(self, _event: object = None) -> None:
            if not self._disabled:
                self._command()

        def _on_enter(self, _event: object = None) -> None:
            if not self._disabled:
                self._set_image(hover=True)

        def _on_leave(self, _event: object = None) -> None:
            self._set_image(hover=False)

    def _dialog_action_button(
        self, parent: tk.Misc, text: str, command: Callable[[], None]
    ) -> "TkViewer._DlgActionButton":
        if not hasattr(self, "_paste_btn_photos"):
            self._paste_btn_photos: list[ImageTk.PhotoImage] = []
        return TkViewer._DlgActionButton(parent, text, command, self._paste_btn_photos)

    def _on_paste(self, _event: object = None) -> None:
        if self._paste_busy:
            messagebox.showinfo(
                "Paste",
                "Paste already in progress — wait or Cancel in the dialog.",
                parent=self.root,
            )
            return
        if self._paste_dialog is not None and self._paste_dialog.winfo_exists():
            self._paste_dialog.lift()
            return

        dlg = tk.Toplevel(self.root)
        self._paste_dialog = dlg
        self._paste_btn_photos = []
        dlg.title("Paste to remote")
        dlg.transient(self.root)
        dlg.geometry("560x400")
        dlg.minsize(480, 360)

        # Pack BOTTOM chrome first so the expanding Text cannot crush the buttons
        btns = tk.Frame(dlg, height=44)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 10))
        btns.pack_propagate(False)  # keep reserved height for bitmap buttons
        status = tk.Label(dlg, text="0 chars", anchor=tk.W)
        status.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))

        tk.Label(
            dlg,
            text=(
                f"Text is typed as remote keystrokes (layout={self._remote_layout}). "
                f"Max {PASTE_MAX_CHARS} chars; ~{1000 // max(PASTE_INTER_MS, 1)} chars/s. "
                "Avoid flooding — long pastes take a while."
            ),
            wraplength=520,
            justify=tk.LEFT,
        ).pack(side=tk.TOP, anchor=tk.W, padx=8, pady=(8, 4))
        text = tk.Text(
            dlg,
            wrap=tk.WORD,
            height=10,
            undo=True,
            insertbackground="#33ff66",
            insertwidth=3,
            insertontime=600,
            insertofftime=300,
        )
        text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

        def _update_count(_event: object = None) -> None:
            n = len(text.get("1.0", "end-1c"))
            status.configure(
                text=f"{n} chars"
                + (f" (limit {PASTE_MAX_CHARS})" if n > PASTE_MAX_CHARS else "")
            )

        text.bind("<<Modified>>", lambda e: (text.edit_modified(False), _update_count()))
        text.bind("<KeyRelease>", _update_count)

        def _close() -> None:
            self._paste_cancel = True
            self._paste_dialog = None
            try:
                dlg.destroy()
            except tk.TclError:
                pass
            self.label.focus_set()

        def _send() -> None:
            raw = text.get("1.0", "end-1c")
            # Normalize newlines; drop lone CR
            raw = raw.replace("\r\n", "\n").replace("\r", "\n")
            if not raw:
                return
            if len(raw) > PASTE_MAX_CHARS:
                messagebox.showwarning(
                    "Paste",
                    f"Text longer than {PASTE_MAX_CHARS} characters.\n"
                    "Shorten it or paste in chunks.",
                    parent=dlg,
                )
                return
            strokes: list[KeyStroke] = []
            skipped = 0
            for ch in raw:
                st = char_to_stroke(ch, remote_layout=self._remote_layout)
                if st is None:
                    skipped += 1
                    continue
                strokes.append(st)
            if not strokes:
                messagebox.showwarning(
                    "Paste",
                    "No sendable characters for the current remote layout.",
                    parent=dlg,
                )
                return
            if skipped:
                if not messagebox.askokcancel(
                    "Paste",
                    f"{skipped} character(s) cannot be mapped and will be skipped.\n"
                    f"Send {len(strokes)} keystroke(s)?",
                    parent=dlg,
                    default=messagebox.CANCEL,
                ):
                    return
            self._release_all_keys()
            self._paste_cancel = False
            self._paste_busy = True
            send_btn.configure(state=tk.DISABLED)
            cancel_btn.configure(text="Stop")
            status.configure(text=f"Sending 0/{len(strokes)} …")

            hold_s = PASTE_HOLD_MS / 1000.0

            def _ui(msg: str, *, reset: bool = False) -> None:
                try:
                    status.configure(text=msg)
                    if reset:
                        send_btn.configure(state=tk.NORMAL)
                        cancel_btn.configure(text="Close")
                except tk.TclError:
                    pass

            def _step(i: int) -> None:
                if self._paste_cancel or self._closed:
                    self._paste_busy = False
                    _ui(f"Stopped at {i}/{len(strokes)}", reset=True)
                    return
                if i >= len(strokes):
                    self._paste_busy = False
                    try:
                        text.delete("1.0", tk.END)
                        _update_count()
                        text.focus_set()
                    except tk.TclError:
                        pass
                    _ui(f"Done — {len(strokes)} keystrokes", reset=True)
                    log.info("paste finished %s strokes (%s skipped)", len(strokes), skipped)
                    return
                try:
                    self.client.send_stroke(strokes[i], hold_s=hold_s)
                except (OSError, ConnectionError) as exc:
                    self._paste_busy = False
                    _ui(f"Error: {exc}", reset=True)
                    return
                _ui(f"Sending {i + 1}/{len(strokes)} …")
                self.root.after(PASTE_INTER_MS, lambda: _step(i + 1))

            log.info(
                "paste start %s strokes layout=%s (~%ss)",
                len(strokes),
                self._remote_layout,
                len(strokes) * PASTE_INTER_MS / 1000.0,
            )
            self.root.after(10, lambda: _step(0))

        def _clear_text() -> None:
            if self._paste_busy:
                return
            try:
                text.delete("1.0", tk.END)
                _update_count()
                text.focus_set()
            except tk.TclError:
                pass

        # Bitmap buttons — avoid Tk text-metric clipping under HiDPI
        send_btn = self._dialog_action_button(btns, "Send", _send)
        send_btn.pack(side=tk.LEFT, padx=(0, 8))
        clear_btn = self._dialog_action_button(btns, "Clear", _clear_text)
        clear_btn.pack(side=tk.LEFT, padx=(0, 8))
        cancel_btn = self._dialog_action_button(btns, "Close", _close)
        cancel_btn.pack(side=tk.LEFT)

        dlg.protocol("WM_DELETE_WINDOW", _close)
        text.focus_set()
        # Prefill from clipboard if available
        try:
            clip = self.root.clipboard_get()
            if clip and isinstance(clip, str):
                text.insert("1.0", clip)
                _update_count()
        except tk.TclError:
            pass

    def _max_view_size(self) -> tuple[int, int]:
        self.root.update_idletasks()
        # Prefer current canvas viewport size once laid out; else screen minus margin
        lw = self.label.winfo_width()
        lh = self.label.winfo_height()
        if lw >= 64 and lh >= 64:
            return lw, lh
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return max(sw - _SCREEN_MARGIN, 320), max(sh - _SCREEN_MARGIN, 240)

    def _fit_size(self, fb_w: int, fb_h: int) -> tuple[int, int, float]:
        max_w, max_h = self._max_view_size()
        if fb_w <= 0 or fb_h <= 0:
            return max_w, max_h, 1.0
        scale = min(max_w / fb_w, max_h / fb_h)
        # Downscale only — never upscale above 1:1 (keeps CLI text sharp).
        if scale > 1.0:
            scale = 1.0
        vw = max(1, int(fb_w * scale))
        vh = max(1, int(fb_h * scale))
        return vw, vh, scale

    def _local_fkey_set(self) -> set[str]:
        """F-keys handled locally (not forwarded). Empty in passthrough mode."""
        if self._fkey_passthrough:
            return set()
        keys = {"F9", "F10", "F11"}
        if self._cad_hotkey:
            keys.add("F8")
        return keys

    def _apply_local_fkey_bindings(self) -> None:
        """Bind or release F8–F11 depending on passthrough mode."""
        for key in ("F8", "F9", "F10", "F11"):
            try:
                self.root.unbind(f"<{key}>")
            except tk.TclError:
                pass
        if self._fkey_passthrough:
            return
        if self._cad_hotkey:
            self.root.bind("<F8>", self._on_cad)
        self.root.bind("<F9>", self._on_sync_leds)
        self.root.bind("<F10>", self._on_toggle_view_mode)
        self.root.bind("<F11>", self._toggle_fullscreen)

    def _fkey_pass_button_tip(self) -> str:
        if self._fkey_passthrough:
            return "F-keys → remote (amber) — click strip for local F9/F10/F11"
        return "F-keys local (teal) — click strip to send F8–F11 to remote"

    def _style_fkey_pass_strip(self) -> None:
        """5px color bar: teal=local shortcuts, amber=passthrough to remote."""
        strip = self._fkey_pass_strip
        if strip is None:
            return
        bg = "#a06818" if self._fkey_passthrough else "#2a5a52"
        try:
            strip.configure(bg=bg)
        except tk.TclError:
            pass

    def _on_toggle_fkey_passthrough(self, _event: object = None) -> None:
        self._fkey_passthrough = not self._fkey_passthrough
        self._apply_local_fkey_bindings()
        self._style_fkey_pass_strip()
        log.info(
            "F-key passthrough %s",
            "ON (F8–F11 → remote)" if self._fkey_passthrough else "OFF (local shortcuts)",
        )
        if self._last_fb is not None:
            self._update_title(self._last_fb.width, self._last_fb.height, self._scale)
        else:
            self._last_title = ""
            self.root.title(
                "aten-kvm — F→R"
                if self._fkey_passthrough
                else "aten-kvm — local F-keys"
            )
            self.root.after(1500, self._restore_title)
        self.label.focus_set()

    def _view_mode_button_label(self) -> str:
        # Button shows the mode you switch *to*
        return "Fit" if self._view_mode == "native" else "1:1"

    def _view_mode_button_tip(self) -> str:
        if self._view_mode == "native":
            return "Switch to fit (scale remote into window) — also F10"
        return "Switch to 1:1 (native pixels + scrollbars) — also F10"

    def _on_toggle_view_mode(self, _event: object = None) -> str:
        self._view_mode = "fit" if self._view_mode == "native" else "native"
        self._sync_scrollbar_visibility()
        if self._view_mode_btn is not None:
            try:
                tip = self._view_mode_button_tip()
                self._view_mode_btn.configure(text=self._view_mode_button_label())
                self._view_mode_btn.bind(
                    "<Enter>",
                    lambda _e, t=tip: self.root.title(f"aten-kvm — {t}"),
                )
            except tk.TclError:
                pass
        log.info("view_mode → %s", self._view_mode)
        if self._last_fb is not None:
            self._render(self._last_fb, force=True)
        self.label.focus_set()
        return "break"

    def _flush_frame(self) -> None:
        fb = self._pending
        if fb is None:
            return
        self._pending = None
        self._last_fb = fb
        self.client.busy_display = True
        try:
            self._render(fb)
        finally:
            self.client.busy_display = False

    @staticmethod
    def _fb_digest(fb: Framebuffer) -> bytes:
        # Fast enough for console FPS; avoids full Pillow work on static screens
        return hashlib.blake2b(fb.pixels, digest_size=16).digest()

    def _set_scrollregion(self, region: tuple[int, int, int, int]) -> None:
        if region == self._scrollregion:
            return
        self._scrollregion = region
        self.label.configure(scrollregion=region)

    def _place_canvas_image(self, photo: ImageTk.PhotoImage) -> None:
        """Swap PhotoImage without tearing down the canvas item."""
        if self._canvas_image_id is None:
            self._canvas_image_id = self.label.create_image(
                self._img_ox, self._img_oy, anchor=tk.NW, image=photo
            )
            self._photo = photo
            return
        # Keep previous PhotoImage alive until after itemconfigure (anti-flicker)
        prev = self._photo
        self.label.itemconfigure(self._canvas_image_id, image=photo)
        if (self._img_ox, self._img_oy) != getattr(self, "_placed_xy", None):
            self.label.coords(self._canvas_image_id, self._img_ox, self._img_oy)
            self._placed_xy = (self._img_ox, self._img_oy)
        self._photo = photo
        _ = prev

    def _layout_canvas(self, vw: int, vh: int) -> None:
        """Update image origin + scrollregion; avoid Configure feedback loops."""
        if self._view_mode == "native":
            ox = oy = 0
            region = (0, 0, vw, vh)
        else:
            cw = max(self.label.winfo_width(), 1)
            ch = max(self.label.winfo_height(), 1)
            ox = max(0, (cw - vw) // 2)
            oy = max(0, (ch - vh) // 2)
            region = (0, 0, cw, ch)
        moved = (ox, oy) != (self._img_ox, self._img_oy)
        self._img_ox, self._img_oy = ox, oy
        self._set_scrollregion(region)
        if moved and self._canvas_image_id is not None:
            self.label.coords(self._canvas_image_id, self._img_ox, self._img_oy)

    def _render(self, fb: Framebuffer, *, force: bool = False) -> None:
        try:
            if self._view_mode == "native":
                vw, vh, scale = fb.width, fb.height, 1.0
            else:
                vw, vh, scale = self._fit_size(fb.width, fb.height)
            digest = self._fb_digest(fb)
            paint_key = (vw, vh, self._view_mode)
            if (
                not force
                and digest == self._last_paint_digest
                and paint_key == getattr(self, "_last_paint_key", None)
            ):
                log.debug("skip paint (identical frame %sx%s → %sx%s)", fb.width, fb.height, vw, vh)
                self._update_title(fb.width, fb.height, scale)
                return

            img = Image.frombytes("RGB", (fb.width, fb.height), bytes(fb.pixels))
            self._fb_w, self._fb_h = fb.width, fb.height
            self._view_w, self._view_h, self._scale = vw, vh, scale
            if self._brighten != 1.0:
                img = ImageEnhance.Brightness(img).enhance(self._brighten)
            if self._contrast != 1.0:
                img = ImageEnhance.Contrast(img).enhance(self._contrast)
            if (vw, vh) != (fb.width, fb.height):
                # BILINEAR: less "frayed" than NEAREST at non-integer scales
                img = img.resize((vw, vh), Image.Resampling.BILINEAR)
            photo = ImageTk.PhotoImage(img)
            self._layout_canvas(vw, vh)
            self._place_canvas_image(photo)

            self._last_paint_digest = digest
            self._last_paint_view = (vw, vh)
            self._last_paint_key = paint_key
            self._update_title(fb.width, fb.height, scale)
        except Exception as exc:  # noqa: BLE001
            log.warning("display update failed: %s", exc)

    def _update_title(self, width: int, height: int, scale: float) -> None:
        pct = int(scale * 100)
        led = self.client.remote_leds
        led_s = ""
        if led is not None:
            parts = []
            if led.caps:
                parts.append("CAPS")
            if led.num:
                parts.append("NUM")
            if led.scroll:
                parts.append("SCR")
            led_s = f"  [{' '.join(parts)}]" if parts else "  [locks off]"
        where = f"{self._server_label}  " if self._server_label else ""
        lost = "" if self.client.kvm_link_ok else "  [KVM LOST]"
        hints = []
        if self._fkey_passthrough:
            hints.append("F→R")
        else:
            hints.append("F10 " + ("1:1" if self._view_mode == "native" else "fit"))
            if self._cad_hotkey:
                hints.append("F8 CAD")
            hints.extend(["F9 locks", "F11 fullscreen"])
        title = (
            f"aten-kvm — {where}{width}x{height} @ {pct}%{led_s}{lost}  "
            f"({', '.join(hints)})"
        )
        if title == self._last_title:
            return
        self._last_title = title
        self.root.title(title)

    def _show_link_lost(self, reason: str = "") -> None:
        """Overlay + title when BMC link is down (idempotent)."""
        detail = f" — {reason}" if reason else ""
        text = f"KVM connection lost{detail} — waiting to reconnect…"
        self._status.config(text=text, bg="#5a1515", fg="#ffd0d0")
        if not self._status_visible:
            self._status.place(relx=0, rely=0, relwidth=1.0)
            self._status.lift()
            self._status_visible = True
        if self._last_fb is not None:
            self._update_title(
                self._last_fb.width, self._last_fb.height, self._scale
            )
        else:
            self.root.title(f"aten-kvm — {self._server_label}  [KVM LOST]")

    def _show_km_dead(self) -> None:
        self._status.config(
            text="KVM: keyboard/mouse disabled (stale session) — "
            "retrying… (click to force)",
            bg="#5a4010",
            fg="#ffe8c0",
        )
        if not self._status_visible:
            self._status.place(relx=0, rely=0, relwidth=1.0)
            self._status.lift()
            self._status_visible = True

    def _refresh_link_status(self) -> None:
        """Banner for link loss or KM-disabled; hide only when fully healthy."""
        if self._reconnect_busy:
            return
        if self.client.kvm_link_ok and getattr(self.client, "km_ok", True):
            if self._status_visible:
                self._status.place_forget()
                self._status_visible = False
            return
        if self.client.kvm_link_ok and not getattr(self.client, "km_ok", True):
            self._show_km_dead()
            return
        err = self.client.reader_error
        self._show_link_lost(str(err) if err else "disconnected")

    def _host_reachable(self) -> bool:
        host = self.client.config.host
        port = self.client.config.port
        try:
            s = socket.create_connection((host, port), timeout=1.5)
        except OSError:
            return False
        try:
            s.close()
        except OSError:
            pass
        return True

    def _finish_reconnect_ui(self, err: Optional[BaseException]) -> None:
        self._reconnect_busy = False
        if self._closed:
            return
        if err is not None:
            log.warning("reconnect failed: %s", err)
            self._show_link_lost(f"reconnect failed: {err}")
            return
        self._link_lost_notified = False
        if not getattr(self.client, "km_ok", True):
            self._show_km_dead()
            log.error("reconnect: KM still disabled — will keep retrying")
            return
        if self._status_visible:
            self._status.place_forget()
            self._status_visible = False
        log.info("KVM session restored (KM ok)")
        if self._last_fb is not None:
            self._update_title(
                self._last_fb.width, self._last_fb.height, self._scale
            )

    def _start_reconnect_thread(self, *, reason: str) -> None:
        if self._closed or self._reconnect_busy:
            return
        self._reconnect_busy = True
        self._release_all_keys()
        self._status.config(
            text=f"KVM: {reason}",
            bg="#5a4010",
            fg="#ffe8c0",
        )
        if not self._status_visible:
            self._status.place(relx=0, rely=0, relwidth=1.0)
            self._status.lift()
            self._status_visible = True

        def do_reconnect() -> None:
            err: Optional[BaseException] = None
            try:
                self.client.reconnect()
            except BaseException as exc:  # noqa: BLE001
                err = exc
            try:
                self.root.after(0, lambda: self._finish_reconnect_ui(err))
            except tk.TclError:
                self._reconnect_busy = False

        threading.Thread(
            target=do_reconnect, name="aten-kvm-reconnect", daemon=True
        ).start()

    def _schedule_recover(self) -> None:
        """If the BMC answers TCP again, reconnect the KVM session."""
        if self._closed or self._reconnect_busy or self.client.kvm_link_ok:
            return
        now = time.monotonic()
        if now - self._recover_probe_at < 3.0:
            return
        self._recover_probe_at = now

        def worker() -> None:
            if not self._host_reachable():
                return

            def start_reconnect() -> None:
                if self._closed or self._reconnect_busy or self.client.kvm_link_ok:
                    return
                self._start_reconnect_thread(reason="host reachable — reconnecting…")

            try:
                self.root.after(0, start_reconnect)
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="aten-kvm-recover", daemon=True).start()

    def _schedule_km_recover(self) -> None:
        """Video up but KM dead — keep trying kick/reconnect (don't give up once)."""
        if (
            self._closed
            or self._reconnect_busy
            or not self.client.kvm_link_ok
            or getattr(self.client, "km_ok", True)
        ):
            return
        now = time.monotonic()
        if now - self._km_recover_at < 8.0:
            return
        self._km_recover_at = now
        self._show_km_dead()

        def worker() -> None:
            err: Optional[BaseException] = None
            try:
                self.client.recover_km(attempts=3)
            except BaseException as exc:  # noqa: BLE001
                err = exc

            def done() -> None:
                self._reconnect_busy = False
                if self._closed:
                    return
                if err is not None:
                    log.warning("KM recover failed: %s", err)
                    self._show_km_dead()
                    return
                if getattr(self.client, "km_ok", True):
                    if self._status_visible:
                        self._status.place_forget()
                        self._status_visible = False
                    log.info("KM recovered")
                else:
                    self._show_km_dead()

            try:
                self.root.after(0, done)
            except tk.TclError:
                self._reconnect_busy = False

        self._reconnect_busy = True
        self._status.config(
            text="KVM: reclaiming keyboard/mouse…",
            bg="#5a4010",
            fg="#ffe8c0",
        )
        threading.Thread(
            target=worker, name="aten-kvm-km-recover", daemon=True
        ).start()

    def _on_status_click(self, _event: object = None) -> None:
        """Manual force-reconnect from the status banner."""
        if self._closed or self._reconnect_busy:
            return
        self._start_reconnect_thread(reason="manual reconnect…")

    def _on_configure(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        # Debounce: scrollregion/scrollbar changes also fire Configure — must not
        # re-enter _render every time or the picture flickers.
        if self._last_fb is None or self._pending is not None:
            return
        if self._view_mode == "native":
            # Image is 1:1; viewport size changes do not require a re-encode.
            return
        if self._configure_after is not None:
            try:
                self.root.after_cancel(self._configure_after)
            except tk.TclError:
                pass
        self._configure_after = self.root.after(80, self._apply_fit_configure)

    def _apply_fit_configure(self) -> None:
        self._configure_after = None
        if self._closed or self._last_fb is None or self._view_mode != "fit":
            return
        vw, vh, _scale = self._fit_size(self._last_fb.width, self._last_fb.height)
        if vw == self._view_w and vh == self._view_h:
            # Same scale — only recenter / scrollregion if the canvas grew
            self._layout_canvas(vw, vh)
            return
        self._render(self._last_fb, force=True)

    def _map_pointer(self, event: tk.Event) -> tuple[int, int]:  # type: ignore[type-arg]
        """Canvas coords → remote framebuffer coords (includes scroll offset)."""
        if self._view_w <= 0 or self._view_h <= 0 or self._scale <= 0:
            return int(event.x), int(event.y)
        cx = float(self.label.canvasx(event.x)) - self._img_ox
        cy = float(self.label.canvasy(event.y)) - self._img_oy
        fx = int(cx / self._scale)
        fy = int(cy / self._scale)
        fx = max(0, min(fx, max(self._fb_w - 1, 0)))
        fy = max(0, min(fy, max(self._fb_h - 1, 0)))
        return fx, fy

    def _on_canvas_wheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Pan canvas in 1:1 mode when mouse-to-BMC is off."""
        if self._view_mode != "native":
            return
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return
        steps = -1 if delta > 0 else 1
        if event.state & 0x0001:  # Shift → horizontal
            self.label.xview_scroll(steps, "units")
        else:
            self.label.yview_scroll(steps, "units")

    def _on_canvas_wheel_btn(self, event: tk.Event, direction: int) -> None:  # type: ignore[type-arg]
        if self._view_mode != "native":
            return
        if event.state & 0x0001:
            self.label.xview_scroll(direction, "units")
        else:
            self.label.yview_scroll(direction, "units")

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._enable_mouse:
            return
        x, y = self._map_pointer(event)
        self.client.send_pointer(x, y, self._buttons)

    def _on_button(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._enable_mouse:
            return
        bit = {1: 1, 2: 2, 3: 4}.get(event.num, 0)
        if event.type == tk.EventType.ButtonPress:
            self._buttons |= bit
        else:
            self._buttons &= ~bit
        x, y = self._map_pointer(event)
        self.client.send_pointer(x, y, self._buttons)

    def _on_wheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._enable_mouse:
            return
        delta = 1 if getattr(event, "delta", 0) > 0 else -1
        x, y = self._map_pointer(event)
        self.client.send_pointer(x, y, self._buttons, wheel=delta)

    def _on_wheel_btn(self, event: tk.Event, delta: int) -> None:  # type: ignore[type-arg]
        if not self._enable_mouse:
            return
        x, y = self._map_pointer(event)
        self.client.send_pointer(x, y, self._buttons, wheel=delta)

    def _release_all_keys(self) -> None:
        for aid in list(self._up_after.values()):
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._up_after.clear()
        self._down_at.clear()
        for hid in list(self._keys_down):
            try:
                self.client.send_key(hid, 0)
            except OSError:
                pass
        self._keys_down.clear()
        for mid in list(self._layout_mods):
            try:
                self.client.send_key(mid, 0)
            except OSError:
                pass
        self._layout_mods.clear()
        self._stroke_by_hid.clear()
        self._hid_by_keycode.clear()

    def _on_focus_out(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._keys_down or self._up_after or self._layout_mods:
            log.debug("focus out — releasing %s held key(s)", len(self._keys_down))
            self._release_all_keys()

    @staticmethod
    def _is_key_press(event: tk.Event) -> bool:  # type: ignore[type-arg]
        t = event.type
        return t == tk.EventType.KeyPress or t == "2" or t == 2

    def _send_up(self, hid: int) -> None:
        self._up_after.pop(hid, None)
        self._down_at.pop(hid, None)
        if hid not in self._keys_down:
            return
        self._keys_down.discard(hid)
        stroke = self._stroke_by_hid.pop(hid, KeyStroke(hid))
        for kc, h in list(self._hid_by_keycode.items()):
            if h == hid:
                self._hid_by_keycode.pop(kc, None)
        self.client.send_key(hid, 0)
        for mid in reversed(stroke.mods):
            if mid in self._layout_mods:
                self.client.send_key(mid, 0)
                self._layout_mods.discard(mid)

    def _on_sync_leds(self, _event: object = None) -> None:
        if self.client.remote_leds is not None:
            self.client.sync_lock_keys_to_local()
        else:
            self.client.toggle_remote_caps()

    def _event_is_local_ui(self, event: tk.Event) -> bool:  # type: ignore[type-arg]
        """True if the key belongs to a local dialog/entry — not the remote console."""
        w = getattr(event, "widget", None)
        if w is None:
            return False
        dlg = self._paste_dialog
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    cur: object = w
                    while cur is not None:
                        if cur == dlg:
                            return True
                        cur = getattr(cur, "master", None)
            except tk.TclError:
                pass
        try:
            cls = str(w.winfo_class())
        except tk.TclError:
            return False
        return cls in ("Text", "Entry", "TEntry", "TCombobox", "Listbox", "Spinbox")

    def _on_key(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        # Typing in Paste dialog / other local fields must not hit the BMC
        if self._event_is_local_ui(event):
            return
        # Local window shortcuts — not sent to BMC
        if event.keysym in self._local_fkey_set():
            return

        down = self._is_key_press(event)
        keycode = int(getattr(event, "keycode", 0) or 0)

        # Release: reuse the stroke from press (Shift changes keysym/char)
        if not down and keycode and keycode in self._hid_by_keycode:
            hid = self._hid_by_keycode[keycode]
            stroke = self._stroke_by_hid.get(hid, KeyStroke(hid))
        else:
            stroke = tk_event_to_stroke(
                event.keysym or "",
                event.char or "",
                remote_layout=self._remote_layout,
            )
            if stroke is None:
                log.info("unmapped key keysym=%r char=%r", event.keysym, event.char)
                return
            hid = stroke.hid

        log.debug(
            "key %s hid=%s mods=%s keysym=%r char=%r layout=%s",
            "down" if down else "up",
            hid,
            stroke.mods,
            event.keysym,
            event.char,
            self._remote_layout,
        )

        # Java KeyboardMode.NORMAL: down=1 on press, down=0 on release
        if down:
            if hid in self._keys_down:
                return  # X11 auto-repeat
            aid = self._up_after.pop(hid, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass
            for mid in stroke.mods:
                if mid not in self._keys_down and mid not in self._layout_mods:
                    self.client.send_key(mid, 1)
                    self._layout_mods.add(mid)
            self._keys_down.add(hid)
            self._down_at[hid] = time.monotonic()
            self._stroke_by_hid[hid] = stroke
            if keycode:
                self._hid_by_keycode[keycode] = hid
            self.client.send_key(hid, 1)
            return

        if hid not in self._keys_down or hid in self._up_after:
            return
        elapsed_ms = (time.monotonic() - self._down_at.get(hid, time.monotonic())) * 1000.0
        delay = max(0, int(self._min_hold_ms - elapsed_ms))
        if delay == 0:
            self._send_up(hid)
        else:
            self._up_after[hid] = self.root.after(delay, lambda h=hid: self._send_up(h))

    def _toggle_fullscreen(self, _event: object = None) -> None:
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        if self._last_fb is not None:
            self.root.after(50, lambda: self._render(self._last_fb, force=True))  # type: ignore[arg-type]

    def _on_close(self) -> None:
        self._release_all_keys()
        self._closed = True
        self.root.quit()

    def run(self) -> None:
        self.client.start_reader()
        interrupted = False
        status_every = 0

        def tick() -> None:
            nonlocal status_every
            if self._closed:
                return
            err = self.client.reader_error
            link_ok = self.client.kvm_link_ok
            try:
                fb = self.client.take_frame()
                if fb is not None:
                    self._pending = fb
                    self._flush_frame()
            except Exception as exc:  # noqa: BLE001
                log.error("viewer loop error: %s", exc)
                print(f"error: {exc}", file=sys.stderr)

            # Poll link ~2 Hz (and immediately on error / reader death)
            status_every += 1
            km_ok = getattr(self.client, "km_ok", True)
            if status_every >= 20 or err is not None or not link_ok or not km_ok:
                status_every = 0
                self._refresh_link_status()
                if not link_ok:
                    self._schedule_recover()
                elif not km_ok:
                    self._schedule_km_recover()

            if not link_ok and not self._link_lost_notified:
                self._link_lost_notified = True
                why = str(err) if err else "disconnected"
                log.error("KVM link lost: %s", why)
                print(f"error: KVM link lost: {why}", file=sys.stderr)
                self._show_link_lost(why)
                # Non-blocking: a modal dialog would freeze reconnect probes

            self.root.after(25, tick)

        def on_sigint(_signum: int, _frame: object) -> None:
            nonlocal interrupted
            interrupted = True
            self._closed = True
            self.client.request_shutdown()
            try:
                self.root.after(0, self.root.quit)
            except tk.TclError:
                pass

        prev_handler = signal.getsignal(signal.SIGINT)
        sig_installed = False
        try:
            signal.signal(signal.SIGINT, on_sigint)
            sig_installed = True
        except (ValueError, OSError):
            pass  # not on main thread — KeyboardInterrupt path remains

        self.root.after(8, tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            interrupted = True
            self._closed = True
            self.client.request_shutdown()
            try:
                self.root.quit()
            except tk.TclError:
                pass
        finally:
            if sig_installed:
                try:
                    signal.signal(signal.SIGINT, prev_handler)
                except (ValueError, OSError):
                    pass
            self._release_all_keys()
            self.client.close()
        if interrupted:
            raise KeyboardInterrupt
