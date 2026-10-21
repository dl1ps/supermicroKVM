"""Local Caps/Num/Scroll Lock LED state (X11 via xset)."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_LED_LINE = re.compile(
    r"Caps Lock:\s*(on|off).*?Num Lock:\s*(on|off).*?Scroll Lock:\s*(on|off)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class LedState:
    caps: bool
    num: bool
    scroll: bool


def read_local_leds() -> LedState | None:
    """Return local lock-key LED state, or None if unreadable."""
    try:
        out = subprocess.check_output(["xset", "q"], text=True, timeout=1.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("xset q failed: %s", exc)
        return None
    m = _LED_LINE.search(out)
    if not m:
        log.debug("could not parse xset LED lines")
        return None
    return LedState(
        caps=m.group(1).lower() == "on",
        num=m.group(2).lower() == "on",
        scroll=m.group(3).lower() == "on",
    )


def remote_leds_from_session_byte(b3: int) -> LedState:
    """Parse privilegeCtrl type-7 LED byte (RemoteVideo.privilegeCtrl)."""
    return LedState(
        caps=bool((b3 & 2) >> 1),
        num=bool(b3 & 1),
        scroll=bool((b3 & 4) >> 2),
    )
