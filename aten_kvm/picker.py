"""Tk dialog: pick a BMC from the loaded server list."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Optional

from .config import AppConfig, ResolvedSession, resolve_server


def pick_server(app: AppConfig, *, title: str = "aten-kvm — select server") -> Optional[ResolvedSession]:
    """Show a modal list of servers. Returns merged session, or None if cancelled."""
    if not app.servers:
        messagebox.showerror(
            "aten-kvm",
            "No servers in config"
            + (f":\n{app.path}" if app.path else "")
            + "\n\nAdd [[servers]] entries to servers.toml",
        )
        return None

    root = tk.Tk()
    root.title(title)
    root.resizable(True, True)
    try:
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    chosen: list[Optional[ResolvedSession]] = [None]

    frame = tk.Frame(root, padx=12, pady=12)
    frame.pack(fill=tk.BOTH, expand=True)

    hdr = tk.Label(frame, text="Select BMC / iKVM server", font=("DejaVu Sans", 12, "bold"))
    hdr.pack(anchor=tk.W, pady=(0, 8))

    if app.path is not None:
        tk.Label(
            frame,
            text=str(app.path),
            font=("DejaVu Sans", 9),
            fg="#555555",
        ).pack(anchor=tk.W, pady=(0, 6))

    list_frame = tk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)

    scroll = tk.Scrollbar(list_frame)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    lb = tk.Listbox(
        list_frame,
        height=min(12, max(4, len(app.servers))),
        font=("DejaVu Sans Mono", 11),
        activestyle="dotbox",
        exportselection=False,
        yscrollcommand=scroll.set,
    )
    lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.config(command=lb.yview)

    labels: list[str] = []
    for s in app.servers:
        port = s.port if s.port is not None else app.defaults.port
        line = f"{s.name}  —  {s.host}:{port}"
        if s.note:
            line += f"  ({s.note})"
        labels.append(line)
        lb.insert(tk.END, line)

    lb.selection_set(0)
    lb.activate(0)

    detail = tk.Label(frame, text="", font=("DejaVu Sans", 10), justify=tk.LEFT, anchor=tk.W)
    detail.pack(fill=tk.X, pady=(8, 4))

    def _update_detail(_event: object = None) -> None:
        sel = lb.curselection()
        if not sel:
            detail.config(text="")
            return
        s = app.servers[int(sel[0])]
        port = s.port if s.port is not None else app.defaults.port
        user = s.user if s.user is not None else app.defaults.user
        bits = [f"host {s.host}:{port}", f"user {user}"]
        if s.note:
            bits.append(s.note)
        detail.config(text=" · ".join(bits))

    def _connect(_event: object = None) -> None:
        sel = lb.curselection()
        if not sel:
            return
        chosen[0] = resolve_server(app.defaults, app.servers[int(sel[0])])
        root.destroy()

    def _cancel(_event: object = None) -> None:
        chosen[0] = None
        root.destroy()

    btn_row = tk.Frame(frame)
    btn_row.pack(fill=tk.X, pady=(10, 0))
    tk.Button(btn_row, text="Connect", command=_connect, width=12, default=tk.ACTIVE).pack(
        side=tk.RIGHT, padx=(6, 0)
    )
    tk.Button(btn_row, text="Cancel", command=_cancel, width=12).pack(side=tk.RIGHT)

    lb.bind("<<ListboxSelect>>", _update_detail)
    lb.bind("<Double-Button-1>", _connect)
    lb.bind("<Return>", _connect)
    root.bind("<Return>", _connect)
    root.bind("<Escape>", _cancel)
    root.protocol("WM_DELETE_WINDOW", _cancel)

    _update_detail()
    lb.focus_set()

    # Center-ish on screen
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return chosen[0]
