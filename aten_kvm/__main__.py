"""CLI entry: python -m aten_kvm [--host …] or config + picker."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .client import AtenKvmClient, ConnectConfig
from .config import (
    AppConfig,
    ResolvedSession,
    ViewerSettings,
    find_config_file,
    load_config,
    overlay_cli,
    resolve_server,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OSS ATEN iKVM viewer (TCP 5900). "
        "Omit --host to pick a server from servers.toml."
    )
    p.add_argument(
        "--config",
        metavar="FILE",
        help="TOML server list (default: ./servers.toml, "
        "oss-viewer/servers.toml, or ~/.config/aten-kvm/servers.toml)",
    )
    p.add_argument("--host", default=None, help="BMC / iKVM IP (skips picker)")
    p.add_argument("--port", type=int, default=None, help="KVM port (default 5900)")
    p.add_argument("--user", default=None, help="iKVM username")
    p.add_argument("--password", default=None, help="iKVM password")
    p.add_argument("--timeout", type=float, default=None, help="connect timeout (s)")
    p.add_argument("--no-display", action="store_true", help="headless message loop")
    p.add_argument(
        "--dump-ppm",
        metavar="FILE",
        help="write last framebuffer to PPM on exit (with --no-display)",
    )
    p.add_argument(
        "--low-bandwidth",
        action="store_true",
        help="console-friendly: ~2 FPS + drop late UI frames (client-side only)",
    )
    p.add_argument(
        "--low-color",
        action="store_true",
        help="Hermon Low Color (lighter traffic); sent after first frame, not at connect",
    )
    p.add_argument(
        "--fps",
        type=float,
        default=None,
        help="max framebuffer request rate (default 10, or 2 with --low-bandwidth)",
    )
    p.add_argument(
        "--qos-outgo",
        type=int,
        default=None,
        help="ProcQos outgo hint (0=off; firmware-sensitive, leave at 0 on X10)",
    )
    p.add_argument(
        "--mouse",
        action="store_true",
        help="enable pointer/wheel to BMC (off by default)",
    )
    p.add_argument(
        "--remote-layout",
        default=None,
        choices=("us", "de"),
        help="guest OS keyboard layout (us|de)",
    )
    p.add_argument(
        "--brighten",
        type=float,
        default=None,
        metavar="F",
        help="local brightness gain after decode (1.0=off)",
    )
    p.add_argument(
        "--contrast",
        type=float,
        default=None,
        metavar="F",
        help="local contrast gain after decode (1.0=off)",
    )
    p.add_argument(
        "--toolbar",
        default=None,
        choices=("off", "left", "right", "top", "bottom"),
        help="macro toolbar placement",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--no-bandwidth-stats",
        action="store_true",
        help="disable periodic RX/TX bandwidth lines on the console (default: on)",
    )
    p.add_argument(
        "--cad-hotkey",
        action="store_true",
        help="bind F8 to Ctrl+Alt+Del (default: off; use toolbar CAD instead)",
    )
    p.add_argument(
        "--sysrq",
        action="store_true",
        help="show Magic SysRq menu on the toolbar (default: off)",
    )
    p.add_argument(
        "--no-snap",
        action="store_true",
        help="hide Snap (clipboard screenshot) on the toolbar (default: shown)",
    )
    p.add_argument(
        "--view-mode",
        default=None,
        choices=("fit", "native"),
        help="video view: fit=scale to window (default), native=1:1 with scrollbars",
    )
    p.add_argument(
        "--view-toggle-toolbar",
        action="store_true",
        help="show Fit/1:1 toggle on the toolbar (default: off; use F10)",
    )
    p.add_argument(
        "--fkey-passthrough",
        action="store_true",
        help="start with F8–F11 sent to remote (toggle via slim strip under layout badge)",
    )
    return p


def _load_app_config(path_arg: Optional[str]) -> Optional[AppConfig]:
    try:
        path = find_config_file(path_arg)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if path is None:
        return None
    try:
        return load_config(path)
    except (OSError, ValueError, TypeError) as exc:
        print(f"config error ({path}): {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _session_from_cli_host(
    args: argparse.Namespace, app: Optional[AppConfig]
) -> ResolvedSession:
    base = app.defaults if app is not None else ViewerSettings()
    # Synthetic server so resolve_server applies the same merge rules
    from .config import ServerEntry

    entry = ServerEntry(
        name=args.host,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        timeout=args.timeout,
        remote_layout=args.remote_layout,
        fps=args.fps,
        low_color=True if args.low_color else None,
        low_bandwidth=True if args.low_bandwidth else None,
        qos_outgo=args.qos_outgo,
        mouse=True if args.mouse else None,
        brighten=args.brighten,
        contrast=args.contrast,
        toolbar=args.toolbar,
        verbose=True if args.verbose else None,
        bandwidth_stats=False if args.no_bandwidth_stats else None,
        cad_hotkey=True if args.cad_hotkey else None,
        sysrq_toolbar=True if args.sysrq else None,
        snap_toolbar=False if args.no_snap else None,
        view_mode=args.view_mode,
        view_toggle_toolbar=True if args.view_toggle_toolbar else None,
        fkey_passthrough=True if args.fkey_passthrough else None,
    )
    return resolve_server(base, entry)


def _apply_cli_flags(session: ResolvedSession, args: argparse.Namespace) -> ResolvedSession:
    """Overlay CLI on a picker-chosen session (None / False flags = keep)."""
    return overlay_cli(
        session,
        port=args.port,
        user=args.user,
        password=args.password,
        timeout=args.timeout,
        remote_layout=args.remote_layout,
        fps=args.fps,
        low_color=True if args.low_color else None,
        low_bandwidth=True if args.low_bandwidth else None,
        qos_outgo=args.qos_outgo,
        mouse=True if args.mouse else None,
        brighten=args.brighten,
        contrast=args.contrast,
        toolbar=args.toolbar,
        verbose=True if args.verbose else None,
        bandwidth_stats=False if args.no_bandwidth_stats else None,
        cad_hotkey=True if args.cad_hotkey else None,
        sysrq_toolbar=True if args.sysrq else None,
        snap_toolbar=False if args.no_snap else None,
        view_mode=args.view_mode,
        view_toggle_toolbar=True if args.view_toggle_toolbar else None,
        fkey_passthrough=True if args.fkey_passthrough else None,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    app = _load_app_config(args.config)

    if args.host:
        session = _session_from_cli_host(args, app)
    else:
        if app is None:
            print(
                "no --host and no servers.toml found.\n"
                "Create oss-viewer/servers.toml (see README) or pass --host / --config.",
                file=sys.stderr,
            )
            return 2
        if args.no_display:
            print(
                "--no-display requires --host (picker needs a display)",
                file=sys.stderr,
            )
            return 2
        from .picker import pick_server

        picked = pick_server(app)
        if picked is None:
            print("cancelled", file=sys.stderr)
            return 0
        session = _apply_cli_flags(picked, args)

    logging.basicConfig(
        level=logging.DEBUG if session.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)
    if app is not None and app.path is not None:
        log.info("config %s (%s server(s))", app.path, len(app.servers))

    low_bw = session.low_bandwidth
    max_fps = session.fps if session.fps is not None else (2.0 if low_bw else 10.0)
    cfg = ConnectConfig(
        host=session.host,
        port=session.port,
        username=session.user,
        password=session.password,
        timeout=session.timeout,
        low_color=session.low_color,
        max_fps=max_fps,
        qos_outgo=session.qos_outgo,
        drop_frames=low_bw,
        bandwidth_stats=session.bandwidth_stats,
    )
    if low_bw or cfg.low_color:
        log.info(
            "video profile: low_color=%s (deferred) fps=%s qos_outgo=%s drop_frames=%s",
            cfg.low_color,
            cfg.max_fps,
            cfg.qos_outgo,
            cfg.drop_frames,
        )

    client = AtenKvmClient(cfg)

    try:
        client.connect()
    except OSError as exc:
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1
    except ConnectionError as exc:
        print(f"handshake/auth failed: {exc}", file=sys.stderr)
        client.close()
        return 2

    if args.no_display:
        code = 0
        try:
            client.run_forever()
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            code = 130
        finally:
            if args.dump_ppm and client.fb:
                open(args.dump_ppm, "wb").write(client.fb.to_ppm())
                print(f"wrote {args.dump_ppm}")
            client.close()
        return code

    from .display import TkViewer

    label = session.name or session.host
    title = f"aten-kvm — {label} ({session.host}:{session.port})"
    if session.remote_layout != "us":
        log.info("remote keyboard layout=%s (local typing remapped)", session.remote_layout)
    if session.brighten != 1.0 or session.contrast != 1.0:
        log.info(
            "local picture: brighten=%.2f contrast=%.2f",
            session.brighten,
            session.contrast,
        )
    if session.toolbar != "left":
        log.info("toolbar=%s", session.toolbar)
    if session.cad_hotkey:
        log.info("CAD hotkey: F8 (with confirm dialog)")
    if session.sysrq_toolbar:
        log.info("toolbar SysRq menu enabled")
    if session.view_mode != "fit":
        log.info("view_mode=%s", session.view_mode)
    if session.fkey_passthrough:
        log.info("F-key passthrough on at start (F8–F11 → remote; toggle via color strip under layout)")
    try:
        TkViewer(
            client,
            title=title,
            enable_mouse=session.mouse,
            remote_layout=session.remote_layout,
            brighten=session.brighten,
            contrast=session.contrast,
            toolbar=session.toolbar,
            server_name=session.name if session.name != session.host else "",
            cad_hotkey=session.cad_hotkey,
            sysrq_toolbar=session.sysrq_toolbar,
            snap_toolbar=session.snap_toolbar,
            view_mode=session.view_mode,
            view_toggle_toolbar=session.view_toggle_toolbar,
            fkey_passthrough=session.fkey_passthrough,
        ).run()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
