# Tastatur über ATEN iKVM (OSS-Viewer)

Wie Tippen und Paste vom Viewer zum Gast kommen — und was **straight** vs.
**remap → de** bedeutet.

Implementierung: `aten_kvm/keys.py`, Wire-Format: `aten_kvm/protocol.py`
(`build_key_event`). Toolbar-Hinweis: `aten_kvm/display.py` (`_layout_hint`).

---

## 1. Keine Scancodes — USB-HID Usages

Auf dem TCP-Socket (Message-ID `4`, 18 Bytes) steht **kein** PS/2-Scancode und
kein X11-Keysym. Im `u32`-Feld liegt eine **USB-HID Keyboard Usage**
(Usage Page 0x07), wie beim Java-`Sendkey`:

| HID (dez) | Bedeutung (USB) |
|-----------|-----------------|
| 4 | Taste A |
| 28 | Position „Y“ auf US-QWERTY |
| 29 | Position „Z“ auf US-QWERTY |
| 40 | Enter |
| 224 / 225 / 226 | Ctrl_L / Shift_L / Alt_L |
| 230 | Alt_R (AltGr) |

Der BMC erscheint am Host als **USB-Tastatur**. Welches **Zeichen** erscheint,
entscheidet allein das **Tastaturlayout des Gastes**.

```text
  lokale Taste / Zeichen (Tk)
           │
           ▼
  Viewer: optionaler Filter → HID (+ Shift/AltGr)
           │
           ▼
  ATEN KeyEvent (down/up + HID)
           │
           ▼
  BMC → USB-HID → Gast-OS-Layout → Glyph
```

Der KVM „sendet Buchstaben“ also nie — er **drückt Tasten**.

---

## 2. Zwei Anzeigen — Filter an oder aus

Gesteuert durch `--remote-layout` / `remote_layout` (`us` oder `de`).
Toolbar unter Name/IP:

| Anzeige | Config | Bedeutung |
|---------|--------|-----------|
| **straight** | `us` | **Kein Filter** — Keysym/Taste → HID |
| **remap → de** | `de` | **Filter an** — getipptes **Zeichen** → HID(+Mods) für DE-Gast |

Die Labels beschreiben den **Viewer-Filter**, nicht ausgelesene OS-Layouts.
Es gibt keine Erkennung von „lokal US“ oder „Gast DE“ — nur ob der
Zeichen-Remap für DE aktiv ist. Deshalb nicht `us → de` (klingt nach
gemessenem Lokal-Layout).

Typischer Nutzen des Filters: ASCII auf der Konsole (`/`, `-`, `[` …), ohne
auf dem DE-Gast erst die physische Taste zu suchen.

---

## 3. Modus *straight* (kein Filter)

Pfad: `tk_event_to_stroke` → `_us_physical_hid` (bzw. US-`char_to_stroke`).

- Tk-**Keysym** (z. B. `y`, `bracketleft`, `Return`) → **US-übliche HID-Nummern**
  (`KEYSYM_TO_HID`).
- Frage: „Welche Taste lag an?“ — nicht: „Welches Zeichen war gemeint?“
- Gehaltene Modifier gehen als eigene HID-Events mit.

**Wann gut:** lokale und Gast-Positionen passen zusammen (beide QWERTZ oder
beide US).

**Klassiker ohne Filter:** lokal US, Gast DE — Taste „Y“ → HID 28 → Gast oft
**Z**. Das ist straight, kein Bug.

---

## 4. Modus *remap → de* (Filter)

Pfad: `_de_stroke_for_us_input` / `_DE_CHAR_TO_STROKE`.

Filter: nimm das **Zeichen**, das Tk geliefert hat, und erzeuge HID+Mods, die
auf einem **deutschen QWERTZ-Gast** dieses Glyph tippen.

| Gemeintes Zeichen | Typischer Stroke (DE-Gast) |
|-------------------|----------------------------|
| `y` / `Y` | HID 29 (US-Z-Position) ± Shift |
| `z` / `Z` | HID 28 (US-Y-Position) ± Shift |
| `[` `]` `{` `}` | oft AltGr + Zifferntaste |
| `@` | AltGr+Q |
| Buchstaben a–x (ohne Sonderfall) | gleiche HID wie US |

Enter, Pfeile, F-Tasten: unveränderte HIDs (layoutunabhängig).

**Paste** nutzt dieselbe Zeichen→Stroke-Logik. Live-Tippen und Paste bleiben
konsistent.

Die Tabelle ist für lateinisches ASCII (+ typische US-Sonderzeichen) gebaut —
nicht für beliebige lokale Layouts (Akzente, Griechisch, …). Lokal muss Tk
brauchbare ASCII-`char`s liefern; das Profil heißt im Code historisch oft
„US-input“, die UI sagt absichtlich nur **remap → de**.

---

## 5. Caps / Modifier

- Caps Lock = HID 57; Sync über F9 / LED-Pfad.
- Remap-Tabellen: ohne Caps gedacht.
- AltGr = HID Alt_R (230).

---

## 6. Praktische Wahl

| Situation | Empfehlung |
|-----------|------------|
| Gast English (US) | **straight** (`remote_layout=us`) |
| Gast Deutsch, ASCII soll „wie getippt“ ankommen | **remap → de** |
| Gast Deutsch, reine physische Positionen | **straight** + lokal QWERTZ |

---

## 7. Abgrenzung

| Thema | Hier? |
|-------|--------|
| RFB/ATEN-Login, Video, KM | siehe [PROTOCOL.md](PROTOCOL.md) |
| Makros (CAD, VT, Scr, Paste-UI) | siehe [README.md](README.md) |
| AST-Boards / andere Encodings | nicht dieser Viewer |

Code-Einstieg: `KEYSYM_TO_HID`, `_us_physical_hid`, `_DE_CHAR_TO_STROKE`,
`tk_event_to_stroke`, `char_to_stroke`.
