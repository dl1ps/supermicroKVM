"""Tk keysym → USB HID keyboard usage IDs (ATEN iKVM / Java KeyMap).

The wire KeyEvent carries a HID usage in the u32 field (Sendkey), not an
X11 keysym. HID codes are *physical* keys; the guest OS applies its layout.

If the local keyboard is US but the guest is German (QWERTZ), sending raw US
HIDs yields umlauts on keys like [ ; ' — use remote_layout='de' to type the
characters the user intended.
"""

from __future__ import annotations

from dataclasses import dataclass

# USB HID Keyboard/Keypad page (0x07) — matches tw.com.aten.ikvm.util.KeyMap
_HID_LETTER = {chr(ord("a") + i): 4 + i for i in range(26)}
_HID_DIGIT = {
    "1": 30,
    "2": 31,
    "3": 32,
    "4": 33,
    "5": 34,
    "6": 35,
    "7": 36,
    "8": 37,
    "9": 38,
    "0": 39,
}

HID_SHIFT = 225  # Shift_L
HID_ALTGR = 230  # Alt_R / ISO_Level3_Shift

# Tk keysym name → HID (US physical positions)
KEYSYM_TO_HID: dict[str, int] = {
    **_HID_LETTER,
    **{k.upper(): v for k, v in _HID_LETTER.items()},
    **_HID_DIGIT,
    "Return": 40,
    "KP_Enter": 88,
    "Escape": 41,
    "BackSpace": 42,
    "Tab": 43,
    "space": 44,
    "minus": 45,
    "equal": 46,
    "bracketleft": 47,
    "bracketright": 48,
    "backslash": 49,
    "semicolon": 51,
    "apostrophe": 52,
    "grave": 53,
    "comma": 54,
    "period": 55,
    "slash": 56,
    "Caps_Lock": 57,
    "F1": 58,
    "F2": 59,
    "F3": 60,
    "F4": 61,
    "F5": 62,
    "F6": 63,
    "F7": 64,
    "F8": 65,
    "F9": 66,
    "F10": 67,
    "F11": 68,
    "F12": 69,
    "Print": 70,
    "Scroll_Lock": 71,
    "Pause": 72,
    "Insert": 73,
    "Home": 74,
    "Prior": 75,  # Page Up
    "Page_Up": 75,
    "Delete": 76,
    "End": 77,
    "Next": 78,  # Page Down
    "Page_Down": 78,
    "Right": 79,
    "Left": 80,
    "Down": 81,
    "Up": 82,
    "Num_Lock": 83,
    "KP_Divide": 84,
    "KP_Multiply": 85,
    "KP_Subtract": 86,
    "KP_Add": 87,
    "KP_Decimal": 99,
    "KP_0": 98,
    "KP_1": 89,
    "KP_2": 90,
    "KP_3": 91,
    "KP_4": 92,
    "KP_5": 93,
    "KP_6": 94,
    "KP_7": 95,
    "KP_8": 96,
    "KP_9": 97,
    "Control_L": 224,
    "Shift_L": 225,
    "Alt_L": 226,
    "Meta_L": 227,
    "Super_L": 227,
    "Control_R": 228,
    "Shift_R": 229,
    "Alt_R": 230,
    "ISO_Level3_Shift": 230,  # AltGr often
    "Meta_R": 231,
    "Super_R": 231,
    "Menu": 101,
    "less": 100,  # ISO 102nd key (DE < > |)
    "greater": 100,
}

# Shifted US punctuation → same physical HID as unshifted key (US layout)
_CHAR_TO_HID_US: dict[str, int] = {
    **_HID_LETTER,
    **_HID_DIGIT,
    "\r": 40,
    "\n": 40,
    "\x1b": 41,
    "\b": 42,
    "\t": 43,
    " ": 44,
    "-": 45,
    "_": 45,
    "=": 46,
    "+": 46,
    "[": 47,
    "{": 47,
    "]": 48,
    "}": 48,
    "\\": 49,
    "|": 49,
    ";": 51,
    ":": 51,
    "'": 52,
    '"': 52,
    "`": 53,
    "~": 53,
    ",": 54,
    "<": 54,
    ".": 55,
    ">": 55,
    "/": 56,
    "?": 56,
    "!": 30,
    "@": 31,
    "#": 32,
    "$": 33,
    "%": 34,
    "^": 35,
    "&": 36,
    "*": 37,
    "(": 38,
    ")": 39,
}


@dataclass(frozen=True)
class KeyStroke:
    """One logical key: optional modifier HIDs + main HID usage."""

    hid: int
    mods: tuple[int, ...] = ()


def _us_physical_hid(keysym: str, char: str) -> int | None:
    if keysym:
        hid = KEYSYM_TO_HID.get(keysym)
        if hid is not None:
            return hid
        if len(keysym) == 1:
            hid = _CHAR_TO_HID_US.get(keysym.lower()) or _CHAR_TO_HID_US.get(keysym)
            if hid is not None:
                return hid
    if char:
        hid = _CHAR_TO_HID_US.get(char)
        if hid is not None:
            return hid
        if len(char) == 1 and char.isalpha():
            return _HID_LETTER.get(char.lower())
    return None


# Intended character → (hid, mods) on a German QWERTZ guest (no Caps).
# Used when the operator types on a US keyboard but the guest layout is DE.
_DE_CHAR_TO_STROKE: dict[str, KeyStroke] = {
    # Y/Z swapped vs US
    "y": KeyStroke(29),  # physical Z
    "Y": KeyStroke(29, (HID_SHIFT,)),
    "z": KeyStroke(28),  # physical Y
    "Z": KeyStroke(28, (HID_SHIFT,)),
    # Punctuation that would become umlauts if US physical HIDs were sent
    "-": KeyStroke(56),  # DE -/
    "_": KeyStroke(56, (HID_SHIFT,)),
    "=": KeyStroke(39, (HID_SHIFT,)),  # Shift+0
    "+": KeyStroke(48),  # DE + key (US ])
    "[": KeyStroke(37, (HID_ALTGR,)),  # AltGr+8
    "]": KeyStroke(38, (HID_ALTGR,)),  # AltGr+9
    "{": KeyStroke(36, (HID_ALTGR,)),  # AltGr+7
    "}": KeyStroke(39, (HID_ALTGR,)),  # AltGr+0
    "\\": KeyStroke(45, (HID_ALTGR,)),  # AltGr+ß
    "|": KeyStroke(100, (HID_ALTGR,)),  # AltGr+<
    ",": KeyStroke(54),  # DE ,;
    ".": KeyStroke(55),  # DE .:
    ";": KeyStroke(54, (HID_SHIFT,)),  # Shift+,
    ":": KeyStroke(55, (HID_SHIFT,)),  # Shift+.
    "'": KeyStroke(50, (HID_SHIFT,)),  # Shift+#
    '"': KeyStroke(31, (HID_SHIFT,)),  # Shift+2
    "/": KeyStroke(36, (HID_SHIFT,)),  # Shift+7
    "?": KeyStroke(45, (HID_SHIFT,)),  # Shift+ß
    "`": KeyStroke(53, (HID_SHIFT,)),  # Shift+^ (approx)
    "~": KeyStroke(48, (HID_ALTGR,)),  # AltGr++ ≈ ~
    "<": KeyStroke(100),
    ">": KeyStroke(100, (HID_SHIFT,)),
    "#": KeyStroke(50),
    "*": KeyStroke(48, (HID_SHIFT,)),
    "(": KeyStroke(37, (HID_SHIFT,)),
    ")": KeyStroke(38, (HID_SHIFT,)),
    "!": KeyStroke(30, (HID_SHIFT,)),
    "$": KeyStroke(33, (HID_SHIFT,)),
    "%": KeyStroke(34, (HID_SHIFT,)),
    "&": KeyStroke(35, (HID_SHIFT,)),
    "@": KeyStroke(20, (HID_ALTGR,)),  # AltGr+q
    "^": KeyStroke(53),  # dead ^ — best effort
}


def _de_stroke_for_us_input(keysym: str, char: str) -> KeyStroke | None:
    """Map US-typed input to HID(+mods) that produce the same glyph on DE guest."""
    # Prefer the character X11 produced (what the user meant to type)
    for token in (char, keysym if len(keysym) == 1 else ""):
        if not token:
            continue
        stroke = _DE_CHAR_TO_STROKE.get(token)
        if stroke is not None:
            return stroke
        # Unshifted letter a–x (same HID as US); y/z handled above
        if len(token) == 1 and token.isalpha():
            low = token.lower()
            if low in _DE_CHAR_TO_STROKE:
                return _DE_CHAR_TO_STROKE[token if token.isupper() else low]
            hid = _HID_LETTER.get(low)
            if hid is not None:
                return KeyStroke(hid, (HID_SHIFT,) if token.isupper() else ())
        if token in _HID_DIGIT:
            return KeyStroke(_HID_DIGIT[token])

    # Non-character keys (Enter, arrows, …): same HID everywhere
    if keysym in KEYSYM_TO_HID and keysym not in (
        "minus",
        "equal",
        "bracketleft",
        "bracketright",
        "backslash",
        "semicolon",
        "apostrophe",
        "grave",
        "comma",
        "period",
        "slash",
        "less",
        "greater",
        "y",
        "Y",
        "z",
        "Z",
    ):
        return KeyStroke(KEYSYM_TO_HID[keysym])

    # US punctuation keysyms without usable char (e.g. KeyRelease)
    us_sym = {
        "minus": "-",
        "equal": "=",
        "bracketleft": "[",
        "bracketright": "]",
        "braceleft": "{",
        "braceright": "}",
        "backslash": "\\",
        "bar": "|",
        "semicolon": ";",
        "colon": ":",
        "apostrophe": "'",
        "quotedbl": '"',
        "grave": "`",
        "asciitilde": "~",
        "comma": ",",
        "period": ".",
        "slash": "/",
        "question": "?",
        "less": "<",
        "greater": ">",
        "at": "@",
        "numbersign": "#",
        "asterisk": "*",
        "parenleft": "(",
        "parenright": ")",
        "exclam": "!",
        "dollar": "$",
        "percent": "%",
        "ampersand": "&",
        "underscore": "_",
        "plus": "+",
        "y": "y",
        "Y": "Y",
        "z": "z",
        "Z": "Z",
    }
    if keysym in us_sym:
        return _DE_CHAR_TO_STROKE.get(us_sym[keysym])
    return None


def tk_event_to_hid(keysym: str, char: str = "", *, remote_layout: str = "us") -> int | None:
    """Map to a single HID usage (no extra mods). Prefer tk_event_to_stroke."""
    stroke = tk_event_to_stroke(keysym, char, remote_layout=remote_layout)
    return None if stroke is None else stroke.hid


def tk_event_to_stroke(
    keysym: str, char: str = "", *, remote_layout: str = "us"
) -> KeyStroke | None:
    """Map a Tk key event to HID usage (+ optional mods for guest layout)."""
    layout = (remote_layout or "us").lower()
    if layout in ("de", "de-de", "german", "qwertz"):
        stroke = _de_stroke_for_us_input(keysym, char)
        if stroke is not None:
            return stroke
        # Fall through for unmapped keys
    hid = _us_physical_hid(keysym, char)
    return None if hid is None else KeyStroke(hid)


# US glyphs that need Shift with the HID in _CHAR_TO_HID_US
_US_SHIFT_CHARS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:"<>?')


def char_to_stroke(ch: str, *, remote_layout: str = "us") -> KeyStroke | None:
    """Map one Unicode character to a HID stroke for paste/type-through.

    Live typing often has Shift already held; paste must inject mods itself.
    """
    if not ch:
        return None
    if ch in ("\r", "\n"):
        return KeyStroke(40)  # Return
    if ch == "\t":
        return KeyStroke(43)
    if ch == "\b":
        return KeyStroke(42)
    if ch == "\x1b":
        return KeyStroke(41)  # Escape
    if ch == " ":
        return KeyStroke(44)

    layout = (remote_layout or "us").lower()
    if layout in ("de", "de-de", "german", "qwertz"):
        return _de_stroke_for_us_input(ch, ch)

    # US guest: physical HID + Shift when needed
    if len(ch) == 1 and ch.isalpha():
        hid = _HID_LETTER.get(ch.lower())
        if hid is None:
            return None
        return KeyStroke(hid, (HID_SHIFT,) if ch.isupper() else ())
    hid = _CHAR_TO_HID_US.get(ch)
    if hid is None:
        return None
    mods = (HID_SHIFT,) if ch in _US_SHIFT_CHARS else ()
    return KeyStroke(hid, mods)


# Soft cap for paste dialog (BMC KM path is fragile if flooded)
PASTE_MAX_CHARS = 2048
# Pace: ~25–30 strokes/s keeps Hermon happier than burst typing
PASTE_INTER_MS = 40
PASTE_HOLD_MS = 30
