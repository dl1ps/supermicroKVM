# aten-kvm — OSS ATEN iKVM viewer

Python-Viewer für klassisches SuperMicro / ATEN iKVM über **TCP 5900**
(X10SLM / Hermon, ohne HTML5). KVM only: Video, Tastatur, Maus — **kein**
Virtual Media.

Laufzeit-Abhängigkeiten: **Python 3**, **Tk**, **Pillow**. Kein Java, keine
ATEN-`.so`/`.dll`.

Protokollbasis: öffentliche
[chicken-aten-ikvm lens](https://github.com/thefloweringash/chicken-aten-ikvm/tree/master/lens)
(BSD-2) und KVM-Pfade aus `../pseudocode/libiKVM*`. Details: [PROTOCOL.md](PROTOCOL.md).

---

## Voraussetzungen

```bash
cd oss-viewer
python3 -m pip install -r requirements.txt   # Pillow
```

---

## VPN (openconnect)

Separates Helper-Skript (normaler User → `sudo` für Root/TUN):

```bash
./vpn.sh start     # IPv6 prüfen/aktivieren, openconnect im Hintergrund
./vpn.sh status
./vpn.sh stop      # sauber beenden (SIGINT an openconnect)
```

Host/User/Servercert/Passwort stehen in `vpn.sh` (oder per Env). Skript
`chmod 700`. Binary: `/usr/sbin/openconnect` (nicht im normalen User-PATH).
PID: `/run/aten-kvm-vpn.pid`.

Typischer Ablauf: `./vpn.sh start` → `./StartOssKvm.sh` → später `./vpn.sh stop`.

Verbindungsabbruch zum BMC: kurze rote Hinweiszeile, letztes Bild eingefroren.
Sobald der Host wieder TCP annimmt (z. B. nach VPN-Reconnect), verbindet der
Viewer neu. Wenn die alte Session am BMC noch KM hält, werden fremde Sessions
gekickt und erneut verbunden. Bleibt die Tastatur tot: BMC-Web → iKVM neu starten.

---

## Schnellstart

**Mehrere Server (empfohlen):** `servers.toml` pflegen, dann starten — es erscheint
eine Auswahl:

```bash
cp servers.example.toml servers.toml   # falls noch keine Config
chmod 600 servers.toml                 # Passwörter im Klartext
./StartOssKvm.sh
# gleichwertig: python3 -m aten_kvm
```

Doppelklick / Enter verbindet; Esc oder Cancel beendet ohne Connect.

**Ein Server per CLI** (Picker überspringen, Skripte):

```bash
python3 -m aten_kvm \
  --host <BMC-IP> \
  --user <USER> \
  --password <PASS> \
  --port 5900
```

CLI-Flags überschreiben Werte aus der Config (z. B.
`./StartOssKvm.sh --toolbar off`).

Headless (nur Protokoll / Dump; braucht `--host`):

```bash
python3 -m aten_kvm --host … --user … --password … \
  --no-display --dump-ppm last.ppm -v
```

---

## Server-Config (`servers.toml`)

Suchreihenfolge ohne `--config`:

1. `./servers.toml` (Arbeitsverzeichnis)
2. `oss-viewer/servers.toml` (neben dem Paket)
3. `~/.config/aten-kvm/servers.toml`

Vorlage: [servers.example.toml](servers.example.toml).

```toml
[defaults]
port = 5900
user = "ADMIN"
remote_layout = "de"
fps = 2.0
low_color = true
low_bandwidth = true
toolbar = "left"
brighten = 1.35
contrast = 1.2
verbose = true

[[servers]]
name = "X10SLM lab"
host = "172.16.0.2"
user = "…"
password = "…"
note = "optional"

[[servers]]
name = "Host B"
host = "192.168.1.10"
password = "…"
# übrige Keys erben von [defaults]
```

Passwörter liegen **plaintext** in der Datei (wie früher in `StartOssKvm.sh`) —
Datei nicht committen, wenn das Repo öffentlich ist; `chmod 600` setzen.
Python **3.11+** (stdlib `tomllib`); darunter: `pip install tomli`.

---

## Konfigurationsparameter (CLI)

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `--config FILE` | siehe Suchpfade | TOML mit `[defaults]` + `[[servers]]` |
| `--host` | *(Picker, wenn weg)* | BMC / iKVM-IP — setzt Picker aus |
| `--port` | Config / `5900` | KVM-TCP-Port |
| `--user` | Config / `ADMIN` | iKVM-Benutzer |
| `--password` | Config / `ADMIN` | iKVM-Passwort |
| `--timeout` | Config / `15` | Connect-Timeout (Sekunden) |
| `--fps N` | Config / `10` (bzw. `2` mit low-bandwidth) | Max. Framebuffer-Request-Rate |
| `--low-bandwidth` | Config / aus | ~2 FPS + späte UI-Frames verwerfen (Client) |
| `--low-color` | Config / aus | Hermon Low Color nach erstem Frame |
| `--qos-outgo N` | Config / `0` | ProcQos-Hinweis; auf X10 bei `0` belassen |
| `--mouse` | Config / aus | Maus/Wheel an den BMC senden |
| `--remote-layout {us,de}` | Config / `us` | Tastatur-Layout des **Gastes** |
| `--brighten F` | Config / `1.0` | Lokale Helligkeit nach Decode |
| `--contrast F` | Config / `1.0` | Lokaler Kontrast nach Decode |
| `--toolbar {off,left,right,top,bottom}` | Config / `left` | Makro-Leiste |
| `--view-mode {fit,native}` | Config / `fit` | Startmodus: `fit` oder `native` (Umschalten: **F10**) |
| `--view-toggle-toolbar` | Config / aus | Fit/1:1 zusätzlich als Toolbar-Button |
| `--fkey-passthrough` | Config / aus | Start: F8–F11 zum Remote (Badge-Farbstreifen toggelt) |
| `--no-display` | aus | Ohne Fenster; braucht `--host` |
| `--dump-ppm FILE` | — | Letztes Framebuffer als PPM (mit `--no-display`) |
| `-v` / `--verbose` | Config / aus | Debug-Logging |
| `--no-bandwidth-stats` | Config / an | Periodische RX/TX-Bandbreite in der Konsole aus |
| `--cad-hotkey` | Config / aus | **F8** → Ctrl+Alt+Del (sonst nur Toolbar-CAD) |
| `--sysrq` | Config / aus | Toolbar-Menü **SysRq** (Magic SysRq; default aus) |
| `--no-snap` | Config / an | Toolbar-**Snap** (Screenshot→Clipboard) ausblenden |

Boolean-CLI-Flags (`--low-color`, `-v`, `--cad-hotkey`, `--sysrq`, …) schalten nur
**an**; Aus in der Config bleibt, wenn das Flag fehlt. Ausnahmen die etwas
**abschalten**: `--no-bandwidth-stats`, `--no-snap` (sowie `… = false` in TOML).
---

## Hotkeys (lokal — gehen **nicht** zum BMC)

| Taste | Aktion |
|-------|--------|
| **F8** | nur mit `--cad-hotkey`: Ctrl+Alt+Del — entfällt bei F-key-Passthrough |
| **F9** | Lock-Sync / Caps — entfällt bei Passthrough |
| **F10** | fit ↔ 1:1 — entfällt bei Passthrough |
| **F11** | lokales Vollbild — entfällt bei Passthrough |

Unter dem Layout-Hinweis (Badge unten): schmaler Farbstreifen — **teal** = F-Tasten
lokal, **amber** = F8–F11 zum Remote. Klick toggelt. Zurück aus Passthrough nur
über den Streifen (nicht über eine F-Taste).

Alle anderen Tasten (inkl. **Esc**, auch im Vollbild) gehen als HID-KeyEvents
an den BMC. Tippen im **Paste-Dialog** bleibt lokal.

**Wichtig:** Lokales **F11** = Vollbild. Remote-F11 (BIOS Boot-Menü) nur über
die Toolbar-Schaltfläche **F11 Boot**.

---

## Toolbar-Makros

Leiste per `--toolbar` platzieren. Unten (bei `left`/`right`): Servername
und BMC-IP um 90° gedreht; darunter ungedreht klein der Layout-Hinweis:
`remap → de` wenn der Zeichen-Filter für DE-Gast aktiv ist, sonst `straight`
(kein Filter — keine Aussage über lokale/Remote-OS-Layouts).

| Button | Remote-Aktion | Hinweise |
|--------|----------------|----------|
| **CAD** | Ctrl+Alt+Del | Bestätigung; unabhängig von F8 |
| **1:1** / **Fit** | — | nur mit `--view-toggle-toolbar`; sonst **F10** |
| **VT** | Menü Ctrl+Alt+F1…F12 | Linux virtuelle Konsolen (tty1–tty12) |
| **SysRq** | Menü Alt+Print+Taste | nur mit `--sysrq` / `sysrq_toolbar = true`; Bestätigung bei destruktiven Einträgen; braucht `kernel.sysrq` |
| **Del Setup** | Delete | SuperMicro POST → BIOS Setup |
| **F11 Boot** | F11 | BIOS Boot-Device-Menü |
| **F12 PXE** | F12 | Network / PXE-Boot |
| **Scr Up** | `\e[5;5~` | xterm-CSI Ctrl+PageUp für GNU `screen` `bindkey` |
| **Scr Dn** | `\e[6;5~` | xterm-CSI Ctrl+PageDown |
| **Snap** | — | PNG in die Zwischenablage (`wl-copy` / `xclip`); aus mit `--no-snap` / `snap_toolbar = false` |
| **Paste** | — | Öffnet Paste-Dialog (siehe unten) |

**Scr Up/Dn:** tippen die CSI-Bytes, nicht HID Ctrl+PageUp. Auf einer Linux-VT
erzeugt die echte Tastenkombination oft keine xterm-Sequenz — Terminal-Emulatoren
schon. Passende `~/.screenrc`:

```
bindkey -t "^[[5;5~" eval "copy" "stuff ^u"
bindkey -t "^[[6;5~" eval "copy" "stuff ^d"
bindkey -m -t "^[[5;5~" stuff ^u
bindkey -m -t "^[[6;5~" stuff ^d
```

---

## Paste-Dialog

Toolbar **Paste** öffnet ein Fenster (Clipboard vorausgefüllt, wenn möglich).

| Button | Aktion |
|--------|--------|
| **Send** | Text als Keystrokes tippen (`--remote-layout`) |
| **Clear** | Inhalt des Textfelds löschen (nicht während Send) |
| **Close** / **Stop** | Schließen; während Send → Abbruch |

Nach erfolgreichem Send wird das Feld geleert. Der Einfügecursor blinkt
sichtbar (grün).

### Limits

| Limit | Wert | Grund |
|-------|------|--------|
| Max. Zeichen | **2048** | Soft-Cap im Dialog |
| Tempo | ~**25** Zeichen/s (`40 ms` Pause) | BMC/KM nicht überfluten |
| Zeichensatz | Druckbares ASCII + Tab/Enter/Backspace | Layout-Mapping US/DE |
| Nicht mappbar | werden übersprungen (Nachfrage) | z. B. Steuerzeichen |

---

## Tastatur-Layout

Kurz: Wire = **USB-HID Usages** (keine Scancodes). Toolbar zeigt **straight**
(kein Filter) oder **remap → de**. Details: **[KEYBOARD.md](KEYBOARD.md)**.

---

## Video / Hermon (X10)

Encoding **89 (Hermon)** — kein AST-JPEG auf diesem Board.

### Anzeige Fit vs. 1:1

| Modus | Bedeutung |
|-------|-----------|
| **fit** (Default) | Remote in den Viewport einpassen (max. 1:1, kein Upscale) |
| **native** | Originalauflösung; Scrollbars wenn größer als das Fenster |

Umschalten: **F10** (immer). Optional Toolbar-Button mit `--view-toggle-toolbar` /
`view_toggle_toolbar = true`. Startmodus: `--view-mode` / `view_mode`.
Unter i3 bleibt die Fenstergröße gleich — nur der Inhalt scrollt.

| Hebel | Wirkung |
|-------|---------|
| `--low-color` | Server-seitig weniger Traffic; Bild dunkler/posterisiert |
| `--brighten` / `--contrast` | Nur Anzeige, kein Extra-Traffic |
| `--fps` / `--low-bandwidth` | Weniger Requests / weniger UI-Last |
| Auflösungswechsel | Volldatei `0,0` setzt Framebuffer-Größe neu (BIOS↔OS) |

Fenster skaliert proportional (bilinear). Identische Frames werden nicht neu
gezeichnet.

---

## Maus

Standard **aus** (`--mouse` zum Einschalten). Pointer-Floods haben auf X10SLM
die KM-Session verkeilt.

---

## Session tot / Tastatur tot nach Disconnect

BMC hält oft exklusiv KM. Lokal prüfen: `ss -tpn 'dport = :5900'`.

1. BMC-Web → Remote Control → andere Sessions kicken  
2. Java-iKVM: Keyboard/Mouse HotPlug (falls noch verfügbar)  
3. 1–3 Min warten  
4. `ipmitool -I lanplus -H <BMC> -U … -P … mc reset cold`

---

## Verwandte OSS-Projekte

| Projekt | Rolle |
|---------|--------|
| [chicken-aten-ikvm](https://github.com/thefloweringash/chicken-aten-ikvm) | Protokoll-PoC + Lens |
| [aten-proxy](https://github.com/thefloweringash/aten-proxy) | ATEN → Standard-VNC |
| [kelleyk/noVNC `bmc-support`](https://github.com/kelleyk/noVNC) | Browser + AST2100 |

Dieser Viewer zielt auf **Hermon/TCP 5900** mit Layout/Paste/Makros; er ersetzt
nicht Virtual Media und nicht AST-JPEG-Boards.

---

## Implementierungsstand

| Teil | Status |
|------|--------|
| RFB 3.8 + ATEN 24+24 Auth | ja |
| ServerInit (Privilege + Caps) | ja |
| ScreenDecode Hermon 89 | ja (raw + 16×16, Low-Color) |
| Auflösungswechsel | ja |
| Tastatur HID + US→DE | ja |
| Paste-Dialog | ja |
| Maus/Wheel | ja (opt-in) |
| LED-Sync (Session type 7) | ja |
| QoS | optional, X10 vorsichtig |
| AST 87/88/96/97 Decode | nein |
| AES-Pointer | nein |
| Virtual Media | nein (bewusst) |

---

## Lizenz / Herkunft

Wire-Format an öffentliche Lens und eigene Pseudocode-Analyse angelehnt.
Kein Laufzeit-Link zu ATEN-Java oder Native-Libs.
