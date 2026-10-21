"""Load multi-server TOML config (defaults + server list)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Python < 3.11 needs tomli: pip install tomli"
        ) from exc


@dataclass
class ViewerSettings:
    """Viewer / connect knobs shared by defaults and per-server overrides."""

    port: int = 5900
    user: str = "ADMIN"
    password: str = "ADMIN"
    timeout: float = 15.0
    remote_layout: str = "us"
    fps: Optional[float] = None
    low_color: bool = False
    low_bandwidth: bool = False
    qos_outgo: int = 0
    mouse: bool = False
    brighten: float = 1.0
    contrast: float = 1.0
    toolbar: str = "left"
    verbose: bool = False
    bandwidth_stats: bool = True
    cad_hotkey: bool = False  # F8 → Ctrl+Alt+Del; off by default (toolbar CAD stays)
    sysrq_toolbar: bool = False  # show Magic SysRq menu on toolbar; default off
    snap_toolbar: bool = True  # screenshot→clipboard button; default on
    view_mode: str = "fit"  # fit = scale to window; native = 1:1 + scrollbars
    view_toggle_toolbar: bool = False  # show Fit/1:1 button; F10 always toggles
    fkey_passthrough: bool = False  # start with F8–F11 sent to remote (strip under layout toggles)


@dataclass
class ServerEntry:
    name: str
    host: str
    note: str = ""
    # None = inherit from ViewerSettings defaults
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    timeout: Optional[float] = None
    remote_layout: Optional[str] = None
    fps: Optional[float] = None
    low_color: Optional[bool] = None
    low_bandwidth: Optional[bool] = None
    qos_outgo: Optional[int] = None
    mouse: Optional[bool] = None
    brighten: Optional[float] = None
    contrast: Optional[float] = None
    toolbar: Optional[str] = None
    verbose: Optional[bool] = None
    bandwidth_stats: Optional[bool] = None
    cad_hotkey: Optional[bool] = None
    sysrq_toolbar: Optional[bool] = None
    snap_toolbar: Optional[bool] = None
    view_mode: Optional[str] = None
    view_toggle_toolbar: Optional[bool] = None
    fkey_passthrough: Optional[bool] = None


@dataclass
class ResolvedSession:
    """Fully merged session ready for ConnectConfig + TkViewer."""

    name: str
    host: str
    note: str
    port: int
    user: str
    password: str
    timeout: float
    remote_layout: str
    fps: Optional[float]
    low_color: bool
    low_bandwidth: bool
    qos_outgo: int
    mouse: bool
    brighten: float
    contrast: float
    toolbar: str
    verbose: bool
    bandwidth_stats: bool
    cad_hotkey: bool
    sysrq_toolbar: bool
    snap_toolbar: bool
    view_mode: str
    view_toggle_toolbar: bool
    fkey_passthrough: bool


@dataclass
class AppConfig:
    defaults: ViewerSettings
    servers: list[ServerEntry]
    path: Optional[Path] = None


_DEFAULT_CONFIG_NAMES = ("servers.toml",)
_XDG_REL = Path("aten-kvm") / "servers.toml"


def default_config_candidates() -> list[Path]:
    """Search order for config when --config is omitted."""
    out: list[Path] = []
    cwd = Path.cwd()
    for name in _DEFAULT_CONFIG_NAMES:
        out.append(cwd / name)
    # Package-adjacent (oss-viewer/servers.toml when run from elsewhere)
    pkg_root = Path(__file__).resolve().parent.parent
    for name in _DEFAULT_CONFIG_NAMES:
        p = pkg_root / name
        if p not in out:
            out.append(p)
    xdg = Path.home() / ".config" / _XDG_REL
    if xdg not in out:
        out.append(xdg)
    return out


def find_config_file(explicit: Optional[str | Path] = None) -> Optional[Path]:
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"config not found: {path}")
        return path
    for cand in default_config_candidates():
        if cand.is_file():
            return cand
    return None


def _as_bool(val: Any, *, key: str) -> bool:
    if isinstance(val, bool):
        return val
    raise TypeError(f"{key} must be a boolean, got {type(val).__name__}")


def _viewer_from_table(table: dict[str, Any]) -> ViewerSettings:
    kwargs: dict[str, Any] = {}
    known = {f.name for f in fields(ViewerSettings)}
    for key, val in table.items():
        if key not in known:
            continue
        if key in (
            "low_color",
            "low_bandwidth",
            "mouse",
            "verbose",
            "bandwidth_stats",
            "cad_hotkey",
            "sysrq_toolbar",
            "snap_toolbar",
            "view_toggle_toolbar",
            "fkey_passthrough",
        ):
            kwargs[key] = _as_bool(val, key=key)
        else:
            kwargs[key] = val
    return ViewerSettings(**kwargs)


def _server_from_table(table: dict[str, Any], *, index: int) -> ServerEntry:
    if "host" not in table or not str(table["host"]).strip():
        raise ValueError(f"servers[{index}]: missing host")
    name = str(table.get("name") or table["host"]).strip()
    host = str(table["host"]).strip()
    note = str(table.get("note") or "").strip()
    kwargs: dict[str, Any] = {"name": name, "host": host, "note": note}
    known = {f.name for f in fields(ServerEntry)} - {"name", "host", "note"}
    for key in known:
        if key not in table:
            continue
        val = table[key]
        if key in (
            "low_color",
            "low_bandwidth",
            "mouse",
            "verbose",
            "bandwidth_stats",
            "cad_hotkey",
            "sysrq_toolbar",
            "snap_toolbar",
            "view_toggle_toolbar",
            "fkey_passthrough",
        ):
            kwargs[key] = _as_bool(val, key=key)
        else:
            kwargs[key] = val
    return ServerEntry(**kwargs)


def load_config(path: Path) -> AppConfig:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    defaults = _viewer_from_table(data.get("defaults") or {})
    servers_raw = data.get("servers") or []
    if not isinstance(servers_raw, list):
        raise ValueError("servers must be an array of tables ([[servers]])")
    servers = [_server_from_table(s, index=i) for i, s in enumerate(servers_raw)]
    return AppConfig(defaults=defaults, servers=servers, path=path)


def normalize_view_mode(value: str) -> str:
    v = (value or "fit").strip().lower()
    if v in ("native", "1:1", "1x", "scroll"):
        return "native"
    return "fit"


def resolve_server(defaults: ViewerSettings, server: ServerEntry) -> ResolvedSession:
    """Merge global defaults with one server entry."""
    d = defaults
    s = server

    def pick(field: str):
        v = getattr(s, field)
        return d.__dict__[field] if v is None else v

    return ResolvedSession(
        name=s.name,
        host=s.host,
        note=s.note,
        port=int(pick("port")),
        user=str(pick("user")),
        password=str(pick("password")),
        timeout=float(pick("timeout")),
        remote_layout=str(pick("remote_layout")),
        fps=pick("fps"),
        low_color=bool(pick("low_color")),
        low_bandwidth=bool(pick("low_bandwidth")),
        qos_outgo=int(pick("qos_outgo")),
        mouse=bool(pick("mouse")),
        brighten=float(pick("brighten")),
        contrast=float(pick("contrast")),
        toolbar=str(pick("toolbar")),
        verbose=bool(pick("verbose")),
        bandwidth_stats=bool(pick("bandwidth_stats")),
        cad_hotkey=bool(pick("cad_hotkey")),
        sysrq_toolbar=bool(pick("sysrq_toolbar")),
        snap_toolbar=bool(pick("snap_toolbar")),
        view_mode=normalize_view_mode(str(pick("view_mode"))),
        view_toggle_toolbar=bool(pick("view_toggle_toolbar")),
        fkey_passthrough=bool(pick("fkey_passthrough")),
    )


def overlay_cli(session: ResolvedSession, **overrides: Any) -> ResolvedSession:
    """Apply non-None CLI overrides onto a resolved session."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    if not clean:
        return session
    return replace(session, **clean)
