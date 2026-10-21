# ATEN iKVM-Protokoll (klassisches TCP 5900)

Dieses Dokument erklärt, wie der **klassische** SuperMicro-/ATEN-iKVM-Pfad
funktioniert — so wie ihn der OSS-Viewer in `aten_kvm/` spricht. Ziel: ein
Mensch soll den Ablauf verstehen, nicht nur Byte-Tabellen lesen.

**Was das ist:** Eine eigene Variante von VNC/RFB. Video + Tastatur + Maus
über **einen TCP-Socket auf Port 5900**. Kein Browser, kein WebSocket, kein
TLS in diesem Pfad.

**Was das nicht ist:** Virtual Media, HTML5-iKVM, neuere Firmware mit
WebSocket/`RFB 055.008`.

**Quellen:** Client-Pseudocode (`../pseudocode/libiKVM*`),
[chicken-aten-ikvm lens](https://github.com/thefloweringash/chicken-aten-ikvm/tree/master/lens)
(BSD-2), Implementierung in `aten_kvm/`.

---

## 1. Big Picture

```
  Viewer  ----TCP :5900---->  BMC (ATEN)
          Version, Login,
          Bild-Anfragen,
          Tasten, Maus

  Viewer  <----------------  BMC
          Bilddaten,
          Session-Infos
```

Der BMC stellt dem Host eine USB-Tastatur/-Maus bereit und liefert den
Bildschirminhalt als Video über dieselbe TCP-Verbindung.

Drei Dinge laufen parallel, sobald die Session steht:

1. **Video** — Client fragt regelmäßig nach Bildupdates; Server schickt
   Encoding-89-Frames. Nicht jedes Mal das ganze Bild: Hermon kann
   **Subframes** (nur geänderte 16×16-Kacheln) senden und spart so
   Bandbreite, solange sich wenig ändert.
2. **Tastatur** — einzelne HID-Tastencodes als kleine Pakete (18 Bytes).
3. **Maus** — optional; gleiche Paketgröße, oft problematisch für den BMC.

Der BMC meldet beim Login, ob **Video**, **Keyboard/Mouse (KM)** und **Kick**
erlaubt sind. Ohne KM tippst du ins Leere — typisch nach abgerissenem VPN,
wenn eine alte Session KM noch „besitzt“.

---

## 2. Verbindungsaufbau (von vorne nach hinten)

Alles ist **Big-Endian**, außer den Pixeln in den Bild-Tiles (dort oft
Little-Endian RGB555). Erwartete RFB-Version: **3.8**.

### Schritt A — TCP

Client verbindet zu `BMC:5900`.

### Schritt B — Versionsgruß

Server schickt 12 ASCII-Bytes, z. B.:

```text
RFB 003.008\n
```

Client prüft `3.8` und schickt **dieselbe** Zeichenkette zurück (Echo).

### Schritt C — Security-Typ

```text
Server:  u8 Anzahl N
         N × u8  (angebotene Typen)
Client:  u8      (gewählter Typ)
```

Der Original-Client nimmt **immer den letzten** angebotenen Typ
(`types[-1]`). Typische Werte sind herstellerspezifisch (oft nur einer).

### Schritt D — Login (ATEN, nicht Standard-VNC)

```text
Server → Client:  24 Byte Challenge   (wird gelesen, im Reply nicht genutzt)
Client → Server:  24 Byte Username    (UTF-8, null-padded)
                  24 Byte Password    (ebenso)
Server → Client:  u32 Status          (0 = ok)
```

Bei Fehler: zusätzlich `u32 reasonLen` + Reason-Text, dann Abbruch.

Hilfsfunktion `pad24`: String als UTF-8, max. 23 Nutzbytes, Rest `0x00`
auf genau 24 Bytes.

### Schritt E — ClientInit / ServerInit

Nach erfolgreichem Login:

```text
Client → Server:  u8 shared = 0     // 0 = exclusive Session
```

Dann liefert der Server die Desktop-Beschreibung (ähnlich RFB), **plus**
einen ATEN-Anhang:

| Feld | Bedeutung |
|------|-----------|
| Breite / Höhe | Framebuffer-Größe (kann bei Screen-off „seltsam“ sein) |
| PixelFormat | 16 Bytes (wie RFB) |
| Desktop-Name | z. B. `ATEN iKVM Server` |
| 4 Byte Padding | unbekannt |
| `privilegeWord` | Rechte-Word |
| `videoEnable` | darf Video sehen? |
| `kmEnable` | darf Tastatur/Maus? **wichtig** |
| `kickEnable` | darf andere Sessions kicken? |
| `vusbEnable` | Virtual USB (hier ungenutzt) |

Ab hier ist die Session „offen“. Der Viewer fragt das erste Vollbild an.

---

## 3. Laufzeit: wer schickt wann was?

```text
Client                              Server
  |                                    |
  |--- FramebufferUpdateRequest ------>|   „Schick Bild“
  |                                    |
  |<------ Message 0 + Bilddaten ------|   ScreenDecode
  |                                    |
  |--- KeyEvent / PointerEvent ------->|   Eingabe
  |                                    |
  |--- KeepAlive (alle ~1–5 s) ------->|   Verbindung wach halten
  |                                    |
  |<------ Session/Privilege 0x39 -----|   User-Liste, LEDs, …
```

**Wichtig:** Der Server schickt **kein** kontinuierliches Video von allein in
dem Sinne eines Streams — der Client muss Updates **anfordern**. Deshalb
throttelt der Viewer die Request-Rate (`--fps`).

---

## 4. Video (Framebuffer)

### Anfrage (Client → Server), Message-ID `3`

```text
u8  id = 3
u8  incremental     // 0 = Vollbild, 1 = nur Änderungen
u16 x, y, width, height
```

### Antwort (Server → Client), Message-ID `0`

Abweichung vom klassischen Multi-Rect-VNC: Dieser Client liest nach der ID
**3 Bytes weg** (Padding + „Anzahl Rechtecke“ werden ignoriert) und danach
**genau ein** Rechteck:

```text
u16 x, y, w, h
u32 encoding
u32 atenFlag        // 0 = normal dekodieren; sonst Sonderpfad
u32 dataLen
u8  payload[dataLen]
```

Negative Breiten/Höhen (Screen-off) werden als Betrag interpretiert.

### Encodings (was im Payload steckt)

| Encoding | Typische Hardware | Inhalt (vereinfacht) |
|----------|-------------------|----------------------|
| **89** | Hermon (X10) | ATEN-Frame: Raw oder 16×16-Tiles; RGB555 oder Low-Color |
| **87 / 88** | AST-Chips | oft JPEG-ähnlich — Decode hier nicht implementiert |
| **96 / 97** | Yarkon / Pilot3 | andere Boards |

Details zum Hermon-Pixelformat und Bandbreite: **§10**.

---

## 5. Tastatur

### Paket (Client → Server), Message-ID `4`, **18 Bytes**, unverschlüsselt

```text
u8  id = 4
u8  0
u8  down          // 1 = Taste gedrückt, 0 = losgelassen
u8  0, 0
u32 hid           // USB-HID Usage (nicht X11-Keysym!)
u8  0 × 9
```

Der Wert im `u32` ist ein **USB-HID-Usage** (z. B. `4` = Taste A, `40` =
Enter, `225` = linke Shift). Das Gast-OS wendet sein Tastaturlayout an —
deshalb kennt der Viewer `--remote-layout de`.

Modifier werden als eigene Tasten gehalten (Shift/Ctrl/Alt runter, dann
Buchstabe, dann wieder hoch) — nicht als Bitmaske im KeyEvent.

---

## 6. Maus (optional)

### Paket, Message-ID `5`, ebenfalls 18 Bytes

**Klartext** (üblich, `encFlag = 0`):

```text
u8  id = 5
u8  encFlag = 0
u8  buttons
u16 x, y
u8  pad[11]
```

Optional AES auf dem Maus-Pfad (`encFlag ≠ 0`) — im OSS-Viewer **nicht**
implementiert; viele Sessions laufen klartext.

Mausrad: kurze PointerEvents mit Extra-Button-Bits, dann wieder ohne.

Zusätzlich gibt es Hilfsnachrichten wie MouseSync (`id = 7`). Im Viewer ist
Maus standardmäßig **aus**, weil Floods auf manchen X10-Firmwares die
KM-USB-Strecke verkeilen.

---

## 7. KeepAlive und Verbindungsabriss

### KeepAlive, Message-ID `22`

```text
Client → Server:  u8 22, u8 payload
Server → Client:  u8 22, dann 1 Byte lesen
```

Dient dazu, die TCP-Session und den BMC-Timeout wach zu halten. Wenn das VPN
stirbt, kann TCP „halb offen“ bleiben — deshalb nutzt der Viewer zusätzlich
Linux-`TCP_USER_TIMEOUT` und erkennt tote Peers über fehlgeschlagene Sends.

### Reconnect / tote Tastatur

Nach hartem Abbruch (VPN weg) behält der BMC oft die **alte Session mit KM**.
Ein neuer Connect bekommt dann Video (`videoEnable=1`), aber
`kmEnable=0`.

Abhilfe im Protokoll:

1. Server schickt Session-Liste (Privilege-Messages, Typ 1).
2. Client kann Sessions **kicken** (Message-ID `56`).
3. Danach neu verbinden — idealerweise mit `kmEnable=1`.

Kick-Paket (vereinfacht):

```text
u8  id = 56
u32 sessionId
u32 type = 0          // Kick
u8  namePad[64]
```

Wenn Kick nicht hilft: iKVM im BMC-Web-UI neu starten.

---

## 8. Session- / Privilege-Messages (Server → Client)

Message-ID `0x39` (57):

```text
u32 id
u32 type
u8  payload[256]
```

Bekannte `type`-Werte (aus dem Java-Viewer):

| type | Bedeutung |
|------|-----------|
| 1 | User in Liste (Payload: `"sessionId username ip"`) |
| 2 | User entfernt |
| 3 | Logout / Session beendet |
| 7 | LED-Status Caps/Num/Scroll (Byte im Payload) |

Der OSS-Viewer nutzt Typ 1 für Kick-Ziele und Typ 7 für Caps-Sync.

---

## 9. Weitere Message-IDs (Kurzreferenz)

### Client → Server

| ID (dez) | Zweck |
|----------|--------|
| 3 | FB-Update anfordern |
| 4 | Taste |
| 5 | Maus |
| 7 | MouseSync |
| 8 | MouseReset |
| 22 | KeepAlive |
| 50 | ScreenSetInfo (u. a. Hermon Low Color) |
| 56 | Kick andere Session |
| 58 | Mouse HotPlug |
| 59 | QoS-Hinweis |

### Server → Client

| ID | Zweck |
|----|--------|
| 0 | Bild (ScreenDecode) |
| 4 | Front-ground |
| 22 | KeepAlive-Antwort |
| 0x35 | Keyboard-Info |
| 0x37 | Mouse-Info |
| 0x39 | Privilege / Session |
| 0x3C | Viewer-Lang / Screen-UI |

Unbekannte IDs darf man nicht „überspringen“, ohne die Länge zu kennen —
sonst desynct der Bytestrom.

---

## 10. Hermon-Grafikformat und Bandbreite

Auf X10 / Hermon gibt es **kein JPEG** und keine feine Qualitätsleiter wie bei
AST-Boards. Der Server schickt unkomprimierte Pixel in einem ATEN-Rahmen
(Encoding **89**). Bandbreite spart das Protokoll selbst vor allem über
**Subframes**; Client-seitig kommen Farbe und Request-Rate dazu:

1. **Subframes** (`type = 0`) — nur geänderte 16×16-Kacheln (Server entscheidet)  
2. **Low Color** — halb so viele Bytes pro Pixel (`--low-color`)  
3. **weniger Requests** — `--fps` / `--low-bandwidth`

Die TB-Werte in §10.4/§10.5 sind der **Raw-Vollframe-Worst-Case**. Sobald der
BMC Subframes schickt (typischer Text-Desktop), liegt der Traffic oft ein bis
zwei Größenordnungen darunter — siehe §10.6.

### 10.1 ATEN-Rahmen (Encoding 89)

Jeder Bild-Payload beginnt mit **10 Bytes** Header, danach Pixeldaten:

```text
u8  type          // 1 = Raw (Vollbild/-rechteck), 0 = Subframes (16×16)
u8  mode          // 0 = High Color (RGB555), ≠0 = Low Color
u32 field_a       // BE; bei Subframes: Anzahl Segmente
u32 total_len     // BE; oft = 10 + Pixelbytes
u8  pixels[...]
```

**Raw** (`type = 1`): zeilenweise das ganze Rechteck — teuer, aber nötig bei
erstem Frame, Moduswechsel, großen Änderungen.

**Subframes** (`type = 0`, im Code auch *Subrects* / Tiles): nur die Kacheln,
die sich geändert haben. Pro Segment:

```text
u8  pad[4]
u8  tile_y        // Kachel-Index (×16 Pixel)
u8  tile_x
u8  tile[16×16×bpp]
```

Eine Kachel ist immer **16×16**. Header pro Tile: 6 Bytes + Pixelblock.
Der Client merged die Kacheln in seinen Framebuffer; unveränderte Bereiche
bleiben stehen — analog zu „incremental“ in klassischem VNC, nur als
Hermon-eigenes Kachelformat.

### 10.2 Farbformate

| Modus | `mode` | Bytes/Pixel | Farben (grob) | Wire-Format |
|-------|--------|-------------|---------------|-------------|
| High Color | `0` | **2** | 32×32×32 ≈ 32k (RGB555) | Little-Endian `u16`, Bits `RRRRRGGGGGBBBBB` |
| Low Color | `≠0` | **1** | 4×4×4 = **64** (2 Bit/Kanal) | 1 Byte: Bits `..RRGGBB` (obere Bits ungenutzt) |

Low Color kommt vom BMC nach **ScreenSetInfo** (`id = 50`):

```text
u8  id = 50
u16 image_quality   // 0 = High Color, 1 = Low Color
u16 mode            // hier meist 0
```

Der OSS-Viewer sendet das erst **nach dem ersten Frame** (`--low-color`) —
zu früh nach dem Login hat das auf manchen X10-Firmwares die KM-Session
gekippt.

Lokal (`--brighten` / `--contrast`) ändert nur die Anzeige, **nicht** den
Wire-Traffic.

### 10.3 Was ein Vollframe wiegt

Payload ≈ `10 + Breite × Höhe × bpp` (Raw). Ohne TCP/RFB-Framing-Overhead
(~20 Bytes Extra pro Message — vernachlässigbar neben dem Bild).

```text
 Auflösung     High Color (2 B/px)     Low Color (1 B/px)     Ersparnis
 -----------   ---------------------   --------------------   ---------
  800×600         ~0,92 MiB              ~0,46 MiB              ~50 %
 1024×768         ~1,50 MiB              ~0,75 MiB              ~50 %
 1280×1024        ~2,50 MiB              ~1,25 MiB              ~50 %
 1600×1200        ~3,66 MiB              ~1,83 MiB              ~50 %
 1920×1080        ~3,96 MiB              ~1,98 MiB              ~50 %
```

Genau (ohne Header): `1024×768×2 = 1 572 864` Bytes High,
`1024×768×1 = 786 432` Bytes Low. Low Color spart also **nahezu die Hälfte**
der Videobytes — nicht mehr, weil Hermon nicht weiter komprimiert.

### 10.4 Geschätzter Dauerbedarf (Worst Case: jedes Request = Raw-Vollframe)

Der Client pollt mit `--fps` (Default **10**, mit `--low-bandwidth` **2**).
Wenn der BMC jedes Mal ein volles Raw-Bild schickt (BIOS, Video, starkes
Scrollen), gilt grob:

`Mbit/s ≈ Framebytes × fps × 8 / 1 000 000`

Beispiel **1024×768** (häufig auf X10):

```text
 Profil                         ~Mbit/s   Bemerkung
 ------------------------------ --------  --------------------------------
 High Color, 10 fps               ~126    Default-Viewer, Worst Case
 Low Color,  10 fps                ~63    --low-color
 High Color,  5 fps                ~63
 Low Color,   5 fps                ~31
 High Color,  2 fps                ~25    --low-bandwidth (ohne Low Color)
 Low Color,   2 fps                ~13    --low-color --low-bandwidth
```

Andere Auflösungen skalieren linear mit der Pixelzahl (bei 1920×1080 und
10 fps High Color eher **~330 Mbit/s** Worst Case — über VPN oft zu viel).

### 10.5 Traffic über 30 Tage (Schätzung)

Umrechnung (dezimale GB, 1 GB = 10⁹ Bytes):

```text
 durchgehend:     GB ≈ Mbit/s × 324          (30 × 24 h)
 pro Stunde:      GB ≈ Mbit/s × 0,45
 8 h/Tag × 30 d:  GB ≈ Mbit/s × 108          (= 1/3 der Dauerlast)
```

**1024×768**, Worst Case = jedes Request ein Raw-Vollframe, Rate seit
§10.4. Spalten: Dauerlast 30 Tage vs. realistischerer Betrieb
**8 h/Tag × 30 Tage** (Session offen, aber dauernd „volles“ Video — immer
noch pessimistisch).

```text
 Profil (1024×768)              ~Mbit/s    30 d durchgehend    8 h/d × 30 d
 ------------------------------ --------   -----------------   -------------
 High Color, 10 fps               ~126        ~41 TB              ~14 TB
 Low Color,  10 fps                ~63        ~20 TB               ~7 TB
 High Color,  5 fps                ~63        ~20 TB               ~7 TB
 Low Color,   5 fps                ~31        ~10 TB               ~3 TB
 High Color,  2 fps                ~25         ~8 TB               ~3 TB
 Low Color,   2 fps                ~13         ~4 TB               ~1 TB
```

**Ruhiger Desktop** (≈ 50 Tiles/Update, §10.6) — gleiche Zeitbasen:

```text
 Profil                         ~Mbit/s    30 d durchgehend    8 h/d × 30 d
 ------------------------------ --------   -----------------   -------------
 High Color, ~10 Updates/s          ~2        ~650 GB             ~220 GB
 Low Color,  ~10 Updates/s          ~1        ~320 GB             ~110 GB
 Low Color,   ~2 Updates/s         ~0,2        ~65 GB              ~22 GB
```

Kurz: High Color @ 10 fps als Dauer-Vollframe ist volumenmäßig absurd
(zig Terabyte/Monat). Im Alltag zählt eher die Tile-Spalte — trotzdem
schnell **hundert GB**, wenn die Session lange offen bleibt. Low Color +
niedrige fps drücken beides linear.

### 10.6 Typischer Fall: Subframes statt Raw

Im Normalbetrieb (ruhiger Text-Desktop) wählt der BMC oft `type = 0`
(Subframes) statt Raw-Vollbild. Eine 16×16-Kachel:

```text
                  Bytes/Tile     50 Tiles/Update     @10 Updates/s
 --------------   ------------   -----------------   -------------
 High Color       6+512 ≈ 518      ~26 KiB              ~2 Mbit/s
 Low Color        6+256 ≈ 262      ~13 KiB              ~1 Mbit/s
```

Da schwankt der Bedarf stark (Cursor allein: 1 Tile; Fenster ziehen: Dutzende).
Als Daumenregel: Idle oft **niedrige einstellige Mbit/s**, Vollbild-Spam
dagegen die Werte aus §10.4 — Monatsvolumen siehe §10.5.

### 10.7 Was sich wirklich lohnt

```text
 Hebel              Wire-Ersparnis (Richtwert)     Bildqualität
 ------------------ ------------------------------ ---------------------------
 --low-color        ~50 % der Pixelbytes           posterisiert, dunkler
 --fps N /          linear mit N                   ruckeliger
   --low-bandwidth  (10→2 ≈ Faktor 5 weniger
                    Requests im Worst Case)
 Subframes (Server) oft 10–100× unter Vollframe    unverändert
 --brighten/…       0 % (nur lokal)                subjektiv besser bei Low
```

Subframes sind der **größte** Hebel, solange wenig passiert — aber kein
Client-Schalter: der BMC entscheidet Raw vs. Subframe. Clientseitig bleiben
Low Color + niedrige fps die zuverlässigen Knöpfe für den Worst Case.

**Kombination für knappe VPN-Leitung:** `--low-color --low-bandwidth`
(≈ Low Color + 2 fps) — Worst Case grob **~10× unter** Default High/10 fps;
mit Subframes im Idle oft noch einmal deutlich darunter.

Message **59 (QoS)** existiert im Java-Client als Bandbreiten-Hinweis; auf
Hermon ist der wirksame Schalter für Farben weiterhin ScreenSetInfo /
`--low-color`.

---

## 11. Crypto (nur Kontext)

| Pfad | Verschlüsselung |
|------|-----------------|
| Tastatur | in der Praxis klartext (`Sendkey`) |
| Maus | optional AES-128-CBC, oft aus |
| Login | Challenge wird gelesen, Credentials klar gepadded |

Schlüssableitung (`SetCipherKey`) ist im Linux-Dump unklar — Interop über
Klartext-Maus ist der sichere Weg.

---

## 12. Abgrenzung und Lücken

| Thema | Status |
|-------|--------|
| Virtual Media / Port 623 | anderes Protokoll / andere Libs |
| HTML5 / WebSocket | neuerer Pfad |
| AST-JPEG-Decode (87/88) | nicht in diesem Viewer |
| AES-Maus | nicht implementiert |
| Vollständige User-Liste / Privilege-UI | nur Kick + LEDs |

---

## 13. Praktisch debuggen

```bash
# Traffic mitschneiden
tcpdump -s0 -w ikvm.pcap host <bmc> and port 5900

# Erwartete Marksteine im Stream:
#   "RFB 003.008\n"
#   24+24 Credential-Bytes nach Challenge
#   Message 4 / 5 = jeweils 18 Bytes
#   Message 0 = Bildblöcke
```

Viewer starten:

```bash
python3 -m aten_kvm --host <BMC> --user … --password … --port 5900 -v
```

Oder über `servers.toml` / `./StartOssKvm.sh` (siehe README).

---

## 14. Merksätze

1. **RFB 3.8 + ATEN-Login** (24+24), nicht Standard-VNC-Auth.  
2. **Ein Socket**, Big-Endian Headers, HID-Tasten statt Keysyms.  
3. **Client pollt Video**; Server pusht nicht endlos von allein.  
4. **`kmEnable` entscheidet über Tastatur** — nach VPN-Abbruch oft das Problem.  
5. Hermon Encoding **89**: Raw oder **Subframes** (16×16); Farbe RGB555/64;
   Bandbreite siehe §10.
