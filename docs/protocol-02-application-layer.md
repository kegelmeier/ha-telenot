# 02 – BuildSec application layer (on top of S2/VdS 2465)

Scope: what the BuildSec app sends/receives inside VdS‑2465 *Nutzdaten* once the S2 session
is up (framing/crypto: see the transport notes from the other agent).
All paths are relative to `/Users/jens.kegelmann/dev_projects/telenot/_re/src/`.
Legend: **[F]** = observed fact in code, **[I]** = inference, **[?]** = unknown.

---------------------------------------------------------------------------------------------
## 0. TL;DR

* BuildSec is a **remote keypad (Bedienteil, BT‑800‑style) emulator**. There is **no** "arm area N"
  or "read area state" command in the app. **[F]** Everything is:
  1. a *Satztyp 0x70* Telenot data block to **open the remote session** ("Fernservice"/Ferndiagnose),
  2. the panel then **pushes** tunnelled **com2BUS keypad telegrams** (LCD text, LEDs, buzzer,
     switching‑function states) — the app never polls them,
  3. the app sends **1‑byte key codes** ("Tastencode") exactly like a physical keypad,
  4. once per text‑CRC change the app requests the **label texts** (Beschriftungstexte) for
     4 freely‑programmable LEDs, 32 "Meldebereiche" (display LEDs) and 16 switching functions.
* Arm/disarm = key presses `ES` (0x45, extern scharf), `IS` (0x49, intern scharf), `US` (0x41,
  unscharf) + code digits (0x30‑0x39) + OK (0x42) — the *panel* drives the dialogue on the LCD. **[F/I]**
* Switching functions ("Schaltfunktionen") 1‑16 = key codes **0xD0‑0xDF**, state = bits in
  com2BUS block **0xCA**. **[F]**
* Meldebereich 1‑32 lock/unlock (sperren) = key codes **0x80‑0x9F**, state = LED bits in **0xC0/0xC1**. **[F]**
* PIN ("Kennwort"/Bedienteilcode, ≤6 digits) is sent **once** in the session‑open block as 3 bytes
  nibble‑swapped BCD padded with 0xF. **[F]**

---------------------------------------------------------------------------------------------
## 1. Container formats

### 1.1 VdS Nutzerdatensatz (record) – generic **[F]**
`040_Telenot.Communication/Telenot.Communication.Vds2465Nutzdaten/NutzdatenInfoAnalyzer.cs`
```
record := [len:1][typ:1][data: len bytes]        total = len+2
```
Nutzdaten of one S2 frame may contain several records back‑to‑back
(`FindNutzerdatensatz`: `size = 2 + nutzdaten[offset]`).

### 1.2 Satztyp 0x70 "Datenblock transparent" **[F]**
`040_.../Vds2465Nutzerdatensatz70HDatenblockTransparent.cs`
```
[Satzlaenge = len(datenblock)+1][0x70][GeraetFrei][datenblock ...]
```
* `GeraetFrei`: high nibble "Geraet" (0/1), low nibble must be 0. BuildSec sends **0x10** for
  everything except the GTyp request which uses **0x00** (`DeviceOperations.ReceiveGTypUe`).
* On receive the app does **not** validate GeraetFrei (`validateGeraetFrei: false`).
* `datenblock[0]` = **Kennung** (`Datenblock.Kennung => DatenblockBytes[0]`,
  `040_.../Datenblock.cs`). `Datenblock(kennung, datenLength)` allocates `datenLength+1` bytes.

### 1.3 Tunnelled com2BUS answer – St70 Kennung 0x99 **[F]**
`DatenblockSt70Com2BusAntwort.cs`
```
datenblock = [0x99][BtAdresse][NutzdatenSaetze ...]
NutzdatenSaetze = sequence of VdS‑style records [len][com2bus_kennung][payload(len)]
```
Each inner record is turned into a com2BUS Datenblock by dropping the length byte
(`Com2BusDatenblock.CreateDatenblock`: `kennung = rec[1]`, `datenblock = rec[1:]`).

### 1.4 Legacy mode (old panels) **[F]**
If the panel answered the modern start with error *UnbekannteKennung (0x14)* or
*FernServiceGesperrt (0x1B)*, the app starts the legacy session (§3.3). In legacy mode
incoming Nutzdaten whose `array[1] != 0x70` are parsed **directly** as a sequence of
com2BUS records `[len][kennung][payload]` (`DeviceOperations.HandleLegacySatztypen`).

---------------------------------------------------------------------------------------------
## 2. Feature list (everything a user can do in the app)

| # | User feature | Wire action | Source |
|---|---|---|---|
| 1 | Connect (Betreiber = operator / Errichter = installer / Hipas‑Fernbedienung) | St70 0x92/0x94/0x9E (+0x8A/0x8C legacy) | `DeviceOperations.StartFernservice*` |
| 2 | Read device type/firmware (direct IP only) | St70 0x18 → 0x19 | `ReceiveGTypUe`, `DeviceCheck` |
| 3 | Download label texts | St70 0x98 with Satztyp 0xEA → com2BUS 0xCD stream | `SendTextabfrage` |
| 4 | Virtual keypad: digits, ▲/▼, ESC, OK, ES/IS/US, freely programmable key "O" | 1‑byte Tastencode | `BedienteilViewModel`, `fragment_bedienteil.xml` |
| 5 | View 2×16 LCD | com2BUS 0x54 | `DatenblockTextAufLcdDarstellen` |
| 6 | View LEDs Alarm/Störung/Gehtest/Betrieb + 4 free LEDs (+labels) + buzzer | com2BUS 0xC0/0xC1 | `DatenblockLeds*` |
| 7 | Meldebereiche page: 32 "areas"/indicator LEDs with 2‑line label, red = info/alarm, yellow = gesperrt | com2BUS 0xC0/0xC1, labels 0xCD | `MeldebereichIvm` |
| 8 | Lock/unlock a Meldebereich | Tastencode 0x80+i, waits for 0xCE ack, reads 0xCF | `MeldebereichIvm`, `SendSperrenTastencodeAsync` |
| 9 | Switching functions page: 16 toggles with label + active/passive text | Tastencode 0xD0+i, waits for 0xCE ack; state 0xCA | `SchaltfunktionIvm` |
| 10 | Disconnect | close S2 session | `StopFernservice` |
| – | App user code / biometric lock, DB encryption, hiXserver accounts, push notifications | **local / cloud only, not panel protocol** | `UserCodeViewModel` (stores code in SecureStorage and compares locally) |

Push notifications ("Internally set/unset", "Externally set/unset", "Alarms/faults") come from
**hiXserver**, not from this TCP session (`NotificationChannel_*` strings, `PushMessage*`). **[F]**

---------------------------------------------------------------------------------------------
## 3. Session start (Satztyp 70 Telenot blocks)

Enum `EDatenblockKennungTelenot` (`079_.../Datenbloecke.Satztyp70/EDatenblockKennungTelenot.cs`) **[F]**:

| Kennung | Name | Dir |
|---|---|---|
| 0x18 | GTypAnfordern | → |
| 0x19 | GTypDaten | ← |
| 0x23 | ComvisMcFehlerkennung (error) | ← |
| 0x4B | FernVerbAbgebOk (just logged) | ← |
| 0x8A / 0x8B | FerndiagnoseEinschalten/Eingeschaltet **Betreiber Legacy** | →/← |
| 0x8C / 0x8D | … **Errichter Legacy** | →/← |
| 0x92 / 0x93 | FerndiagnoseEinschalten/Eingeschaltet **Betreiber** | →/← |
| 0x94 / 0x95 | … **Errichter** | →/← |
| 0x98 / 0x99 | GetunnelteCom2BusTelegramme Anfrage / Antwort | →/← |
| 0x9E / 0x9F | BuildSecHipasFernbedienung Einschalten / Eingeschaltet | →/← |

### 3.1 Kennwort (PIN) encoding – `KennwortParser.ParseKennwort` **[F]**
```csharp
for (i = 0; i < len/2; i++) { a[i] = (k[2i+1] & 0xF) << 4 | (k[2i] & 0xF); }
if (len odd) a[i] = (k[last] & 0xF) | 0xF0;      // rest stays 0xFF
```
Only the **first 3 bytes** are copied into the block (`Array.Copy(value,0,bytes,1,3)`), max 6 chars.
```python
def encode_kennwort(code: str) -> bytes:          # "123456" -> 21 43 65 ; "1234" -> 21 43 FF ; "12345" -> 21 43 F5
    if not code or len(code) > 6: raise ValueError
    out = bytearray(b"\xff" * 6)
    n = len(code)
    for i in range(n // 2):
        out[i] = ((ord(code[2*i+1]) & 0xF) << 4) | (ord(code[2*i]) & 0xF)
    if n & 1:
        out[n // 2] = (ord(code[-1]) & 0xF) | 0xF0
    return bytes(out[:3])
```
The code is the **keypad/operator code** entered on the virtual keypad ("Enter password:") or
stored per device ("Bedienteilcode", `ValidationHint_BtCode = "You can only enter 6 characters"`).
On the keypad the app collects ASCII key codes 0x30‑0x39, auto‑submits after 6 digits or on OK
(0x42), ESC (0x53) = backspace (`DomainModel.BedienteilcodeEingabe`). It is **not sent as key
presses**; it only goes into the start block.

### 3.2 Start request – `DatenblockSt70Com2BusAnfrage` (5 bytes) **[F]**
```
datenblock = [Kennung][K0][K1][K2][BtAdresse]          BtAdresse 0..15 (ConnectionData validation)
record     = [0x06][0x70][0x10] + datenblock
```
Which Kennung (`DeviceCommunication.StartCommunication`):
* `BerechtigungsEbene` 1 = **BetreiberMode** → 0x92, 0 = ErrichterMode → 0x94, 2 = FernbedienMode → 0x9E.
* ConnectionType 0 = DirectConnected (TCP to panel/transmitter). Via hiXserver/hiXC (types 1/2)
  the 0x92/0x94 block is extended: `[K][K0 K1 K2][BtAdr][SigLen][Signature…]`, datenLength = 5+SigLen,
  min total length 69 (`...OverHixserver.cs`) — not needed for local HA. **[F]**

```python
def build_st70(datenblock: bytes, geraet_frei=0x10) -> bytes:
    return bytes([len(datenblock) + 1, 0x70, geraet_frei]) + datenblock

def start_betreiber(code, bt_adr):   # operator ("Betreiber") – what HA should use
    return build_st70(bytes([0x92]) + encode_kennwort(code) + bytes([bt_adr]))
```

### 3.3 Start response **[F]**
`DatenblockSt70FerndiagnoseEingeschaltetBetreiber` (Kennung 0x93, exactly 4 bytes):
```
[0x93][BtAdresse][CRC_hi][CRC_lo]     TexteCrc = big‑endian (BitConverter LE + byte swap "Toggle")
```
0x95 (Errichter) and 0x9F (Hipas) identical layout. Legacy answers 0x8B/0x8D: no CRC (crc := 0).

Fallback logic (`DeviceCommunicator.StartFernservice`): on error 0x14 *UnbekannteKennung* or
0x1B *FernServiceGesperrt* (and not FernbedienMode) → send legacy block 0x8A (Betreiber) /
0x8C (Errichter) with the same 5‑byte layout and wait for 0x8B/0x8D; then `isLegacyMode = true`.

After a direct‑IP start the app also does the **GTyp check** (`InitConnection` only if
ConnectionType == 0):
```
→ record [0x05][0x70][0x00][0x18][K0][K1][K2]                (GeraetFrei = 0x00 here!)
← datenblock [0x19][Geraetefamilie][major][minor][patch] … (min 17, max 244 bytes)
```
`EGeraetefamilie` (`047_Telenot.Infrastructure/Telenot.Infrastructure/EGeraetefamilie.cs`):
0xF5 Bt800, 0xF9 Bt820, 0xF6 Nt800Intern, 0xB9 Hiplex, 0xF7 HiplexSubcontroller, 0xF0 Fgw310,
0xFA Fgw230, … Version string `vMM.mm` (+ " Beta pp" if patch != 0xFF). Value is only logged.

### 3.4 Text CRC handling (`ConnectionManager.ConnectToDeviceWithIdAsync`) **[F]**
* crc == 0 → "no texts parametrised" → use locally stored free‑LED texts only.
* stored crc == 0 (first connect) **or** crc == 0xFFFF (re‑parametrised) **or** crc != stored →
  run text request (§5) and store texts + crc.
* else → load texts from local DB.

### 3.5 Error block 0x23 – `DatenblockFehler` (exactly 2 bytes) **[F]**
`[0x23][Grund]`, `EFehlergrund`:

| Grund | Name | UI text (EN) |
|---|---|---|
| 0x10 | KennwortFalsch | Code incorrect |
| 0x11 | ChecksummeFalsch | |
| 0x13 | FalscheSatzlaenge | |
| 0x14 | UnbekannteKennung | ID not recognized (→ legacy fallback) |
| 0x1B | FernServiceGesperrt | "The app enabling is disabled in the intruder alarm system!" (→ legacy fallback) |
| 0x1D | UnzulaessigeBtAdresse | invalid keypad address |
| 0x1E | Com2BusNichtFuerEmzAktiviert | activate com2BUS of transmitter for panel |
| 0x1F | Com2BusStoerungZurEmz | com2BUS fault |
| 0x20 | CodeGesperrt | code disabled |
| 0x21 | FehlerFunktion | "Access enabling code is not enabled for this keypad" |
| 0x22 | BedienebeneZuKlein | operating level too small |
| 0x23 | BedienteilBelegt | remote access occupied, use other keypad address |
| 0x24 | AutorisierungFehlgeschlagen | (polling stops, connection closed) |
| 0x25 | EingabeGesperrt | entry locked |
| 0x26 / 0x27 / 0x28 | Lan / Wan / Hixserver connection not allowed | |
| 0x29 | IpAdresseNichtBerechtigt | |
| 0x30 | GesperrtDurchZeitModell | locked by time model |
| 0x99 | Unbekannt | |

---------------------------------------------------------------------------------------------
## 4. Receive loop ("Polling") – push, not poll **[F]**

`DeviceOperations.PollingOfIncomingBedienteilData` is started right after TCP connect and runs
`protocol.ReceiveNutzdatenAsync()` in a loop until shutdown. There is **no app‑level timer or
periodic request** (no `Timer`/`PeriodicTimer` in 079/082/083/087). Liveness is handled in the S2
layer ("data monitoring time" = function monitoring time + 1 s, `S2ProtocolTelenotAsync.cs:3264`).

Dispatch (per received Nutzdaten buffer):
```python
def on_nutzdaten(buf):
    if len(buf) > 2 and buf[1] == 0x70:
        db = buf[3:3 + buf[0] - 1]                    # strip [len][0x70][GeraetFrei]
        k = db[0]
        if   k in (0x93, 0x95, 0x9F, 0x8B, 0x8D): complete_start(db)
        elif k == 0x19: complete_gtyp(db)
        elif k == 0x23: raise PanelError(db[1])
        elif k == 0x4B: pass                           # FernVerbAbgebOk, log only
        elif k == 0x99:                                # tunnelled com2BUS
            bt_adr = db[1]
            for rec in split_records(db[2:]):          # [len][kenn][payload]
                handle_com2bus(rec[1:])
        else: log("Unbekannte Kennung")
    elif legacy_mode:
        for rec in split_records(buf): handle_com2bus(rec[1:])

def split_records(b):
    i = 0
    while i < len(b):
        n = b[i]; yield b[i:i + 2 + n]; i += 2 + n
```
The app never sends an explicit "give me current state" request; the panel sends the current
display/LED/switch state after the session is opened **[I: the keypad starts empty and is filled
by pushed telegrams; BedienteilView shows "Press any button" until connected]**.

---------------------------------------------------------------------------------------------
## 5. Label texts (Beschriftungstexte)

### 5.1 Request – `DatenblockSt70Com2BusStAbfrage` (7 bytes) **[F]**
```
datenblock = [0x98][K0][K1][K2][BtAdresse][AbfrageSatzlaenge=0x00][AbfrageSatztyp=0xEA]
record     = [0x08][0x70][0x10] + datenblock
```
(0xEA = `EDatenblockKennungCom2Bus.TextAbfrage`, built from `new DatenblockSatztypAnfordern(234, 0)`.)
The app then waits (UI polls the task every 4 s, progress +1 % each, aborts at 100 %).

### 5.2 Response stream – com2BUS 0xCD (`DatenblockBeschriftungstexte`, 2…518 bytes) **[F]**
```
[0xCD][PUK][Index][len1][len2][text1: len1][text2: len2]          PUK != 2
[0xCD][0x02][ … ][CRC_hi][CRC_lo]                                  PUK == 2 (last; CRC = last 2 bytes, BE)
[0xCD][x]                                                         length 2 → also "done"
```
* PUK (`EParameterUebergabeKennung`): 0 = weitere, 1 = erste, 2 = letzte Sendung.
* Text bytes: LCD charset → `ConvertUmlauteToAscii` (0xE1→ä, 0xEF→ö, 0xF5→ü, 0xE8→'v') then
  ISO‑8859‑1, `.Trim()`, later `.Trim('\0')`. LED/switch labels are joined `text1 + "\n" + text2`.
* `EBeschriftungIndex`:
  * 0‑3 → free LED 1‑4 label (`LedDuos[i].Text`)
  * 4‑35 → Meldebereich 1‑32 (`Text1`, `Text2`)
  * 36‑83 → Schaltfunktion `n = (idx-36)//3`, kind `(idx-36)%3`: 0 = name (`Text`),
    1 = AktivText (`AktionsTextEin`), 2 = PassivText (`AktionsTextAus`)
  * anything else → error.

```python
LCD_FIX = {0xE1: 0xE4, 0xF5: 0xFC, 0xEF: 0xF6, 0xE8: 0x76}
def lcd_decode(b: bytes) -> str:
    return bytes(LCD_FIX.get(x, x) for x in b).decode("latin-1").strip().strip("\0")

def parse_beschriftung(db):
    puk = db[1]
    if len(db) == 2: return ("done", None)
    if puk == 2:     return ("done", (db[-2] << 8) | db[-1])
    idx, l1, l2 = db[2], db[3], db[4]
    t1 = lcd_decode(db[5:5+l1]) if l1 else None
    t2 = lcd_decode(db[5+l1:5+l1+l2]) if l2 else None
    return ("text", idx, t1, t2)
```

---------------------------------------------------------------------------------------------
## 6. com2BUS keypad telegrams (panel → app)

`EDatenblockKennungCom2Bus` / `Com2BusDatenblock.CreateDatenblock` **[F]**

| Kennung | Name | Length (incl. Kennung) |
|---|---|---|
| 0x54 | TextAufLcdDarstellen | 1…256 |
| 0xC0 | LedsEinAusUndSummerDarstellen (steady) | 2…12 |
| 0xC1 | LedsBlinkenUndSummerDarstellen (blink) | 2…12 |
| 0xCA | SchaltfunktionsZustandDarstellen | 3 |
| 0xCD | Beschriftungstexte | 2…518 |
| 0xCE | TastencodeAck | 2 |
| 0xCF | DisplayTastaturAnzeige ("EmaResponse") | 2 |
| 0xEA | TextAbfrage (ignored on receive) | – |
Unknown kennungen → `CreateDatenblock` returns null → ignored.

### 6.1 0x54 LCD text **[F]**
Bytes contain HD44780‑style escape sequences: `0x1B 0x80` → next **16** bytes = line 1;
`0x1B 0xC0` → next 16 bytes = line 2. Only lines present are updated. A line of 16×0xFF is the
"display test" → shown as empty (and parsing of line 2 is skipped – app bug). Block length 1 → ignored.
```python
def parse_lcd(db):
    out = {}
    for marker, key in ((0x80, 1), (0xC0, 2)):
        for i in range(len(db) - 1):
            if db[i] == 0x1B and db[i+1] == marker:
                raw = db[i+2:i+18]
                out[key] = "" if raw == b"\xff" * 16 else lcd_decode(raw)
                break
    return out
```
**[?]** Other ESC sequences (cursor, blinking cursor during code entry) are ignored by the app.

### 6.2 0xC0 / 0xC1 LEDs + buzzer **[F]** (`DatenblockLeds*.cs`, translator)
```
byte1  buzzer: b0 1 s | b1 3 s | b2 intermittent | b3 beep+3 s pause | b4 continuous   (0 = off)
byte2  b0 Betrieb | b1 Alarm | b2 Störung | b3 Gehtest | b4‑5 freeLED1 | b6‑7 freeLED2
byte3  b0‑1 freeLED3 | b2‑3 freeLED4                    free LED 2‑bit: 0 off,1 green,2 red,3 yellow
byte4,5   Meldebereich 1‑16  "info"   (b0 of byte4 = MB1 … b7 of byte5 = MB16)
byte6,7   Meldebereich 1‑16  "gesperrt"
byte8,9   Meldebereich 17‑32 "info"
byte10,11 Meldebereich 17‑32 "gesperrt"
```
Block may be truncated; fields beyond the length are not touched (translator switches on length).
* **0xC0 = steady state.** Sets colours (Betrieb→green, Alarm→red, Störung→yellow, Gehtest→green,
  MB info→red, MB gesperrt→yellow, free LEDs per 2‑bit colour) **only if that LED is not currently
  blinking**. Buzzer priority (last wins): 1 s < beep+pause < continuous < intermittent < 3 s.
* **0xC1 = blink overlay.** A set bit ⇒ LED blinks in its colour; a cleared bit on a blinking LED ⇒
  LED off and not blinking. Betrieb bit only toggles blink. Free LEDs: colour bits ≠ 0 ⇒ blinking in
  that colour. Byte1 (buzzer) of 0xC1 is **not** evaluated by the app.

```python
COL = {0: "off", 1: "green", 2: "red", 3: "yellow"}
def parse_leds(db):
    n = len(db); s = {}
    if n >= 2: s["buzzer"] = db[1]
    if n >= 3:
        b = db[2]
        s.update(betrieb=bool(b & 1), alarm=bool(b & 2), stoerung=bool(b & 4), gehtest=bool(b & 8),
                 led1=COL[(b >> 4) & 3], led2=COL[(b >> 6) & 3])
    if n >= 4:
        s.update(led3=COL[db[3] & 3], led4=COL[(db[3] >> 2) & 3])
    def bits(lo, hi):
        v = (db[lo] if n > lo else 0) | ((db[hi] if n > hi else 0) << 8); return [bool(v >> i & 1) for i in range(16)]
    if n >= 5:  s["mb_info"]     = bits(4, 5) + (bits(8, 9) if n >= 9 else [None]*16)
    if n >= 7:  s["mb_gesperrt"] = bits(6, 7) + (bits(10, 11) if n >= 11 else [None]*16)
    return s
```
(Translator nuance: for lengths 9/11 it only reads byte8 / byte10; treat missing bytes as "unchanged".)

### 6.3 0xCA switching‑function state **[F]**
`[0xCA][b1][b2]`: bit (i‑1) of b1 = SF i (1‑8), of b2 = SF 9‑16. 1 ⇒ `Aktiv = true` and the button
shows `AktionsTextAus` (passive text = what pressing will do); 0 ⇒ shows `AktionsTextEin`.

### 6.4 0xCE / 0xCF **[F]**
* 0xCE `[0xCE][code]` = acknowledge of a key code; completes the pending `ReceiveTastencodeAckAsync`.
* 0xCF `[0xCF][resp]` = "EmaResponse"; stored, read after the 0xCE ack in
  `SendSperrenTastencodeAsync`. `resp == 2` ⇒ UI "Operation not possible. Function disabled."
  **[?]** other values (0 presumably OK).

---------------------------------------------------------------------------------------------
## 7. Key codes (app → panel)

### 7.1 Transport **[F]**
`DeviceOperations.SendTastencode`: `protocol.SendNutzdatenAsync(new byte[1] { tastencode })` –
the key code is sent as a **single raw Nutzdaten byte**, not wrapped in a St70/VdS record.
(Same in legacy and modern mode.) **[?]** How the transport/panel distinguishes this from records
→ confirm in transport notes / with a capture.

Three send variants:
* `SendTastencode` – fire & forget (keypad buttons).
* `SendTastencodeAsync` – send, then await com2BUS 0xCE (switching functions; UI timeout 20 s).
* `SendSperrenTastencodeAsync` – reset EmaResponse=0, send, await 0xCE, return last 0xCF value
  (Meldebereich lock; UI timeout 20 s).

### 7.2 Keypad map (`res/layout/fragment_bedienteil.xml`, button drawables, tooltips) **[F]**
| Code | ASCII | Button (id / drawable) | Meaning |
|---|---|---|---|
| 0x30‑0x39 | '0'‑'9' | button0…button9 | digits |
| 0x2D | '-' | buttonUp (arrow up) | scroll up |
| 0x2B | '+' | buttonDown (arrow down) | scroll down |
| 0x42 | 'B' | buttonOK | OK / confirm |
| 0x53 | 'S' | buttonESC | ESC (long press = local UI event only) |
| 0x45 | 'E' | buttonES (empty house icon) | **Extern scharf** (set externally) |
| 0x49 | 'I' | buttonIS (house with person) | **Intern scharf** (set internally) |
| 0x41 | 'A' | buttonUS | **Unscharf** (unset) |
| 0x4E | 'N' | buttonX (circle "O") | freely programmable key – app only sends on **long press** |

### 7.3 Function codes **[F]**
* Switching function i (1‑16): `0xD0 + (i-1)` (`SchaltfunktionIvm`: `b2 = (byte)(b + 208)`).
* Meldebereich i (1‑32) lock/unlock toggle: `0x80 + (i-1)` (`MeldebereichIvm`: `b2 = (byte)(b + 128)`).

---------------------------------------------------------------------------------------------
## 8. Mapping to Home Assistant

What the app can *show* is only keypad state; mapping needs panel parametrisation knowledge. **[I]**

| HA entity | Source | Notes |
|---|---|---|
| `alarm_control_panel` actions | arm_away = key 0x45, arm_home = 0x49, disarm = 0x41, then code digits + OK if the LCD asks | **[I]** exact dialogue depends on panel config (some panels arm without code; disarm needs code). Implement as a small LCD‑driven state machine, verify against real panel. |
| `alarm_control_panel` state | Free LEDs 1‑4 (labels from Beschriftung idx 0‑3, e.g. installer typically sets "Extern scharf"/"Intern scharf") and/or Meldebereich LEDs with labels; Alarm LED (red, blink) ⇒ `triggered`; buzzer intermittent/beep during entry/exit delay ⇒ `arming`/`pending` | **[I]** No generic "armed" bit exists in the protocol; let user map LED/MB/SF → state in options flow. LCD text is a fallback ("… scharf"). |
| `binary_sensor` alarm / problem / walk test | byte2 b1 / b2 / b3 (+0xC1 blink) | **[F]** |
| `binary_sensor` per Meldebereich (1‑32) | MB info bit (red) | label = Text1/Text2 ; **[I]** meaning (zone open, area armed, alarm) depends on parametrisation |
| `switch` "bypass" per Meldebereich | gesperrt bit (yellow); toggle = key 0x80+i | check 0xCF==2 → refused |
| `switch` per Schaltfunktion (1‑16) | 0xCA bit; toggle = key 0xD0+i | names from idx 36+3n, on/off texts from 36+3n+1/2 |
| `sensor` display line 1/2 | 0x54 | useful for debugging / showing panel prompts |
| `sensor` buzzer | 0xC0 byte1 | |
| `button`s for raw keys | §7.2 | optional "virtual keypad" |

The app's own UI pages: `DetermineBedienteilUserInterfaceMode` shows the Meldebereich page if any
MB has a label (16 vs 32), and the switch page if any SF has a label/text. Use the same rule for
entity creation.

---------------------------------------------------------------------------------------------
## 9. Implementation sketch (Python, asyncio)

```python
class BuildSecSession:
    def __init__(self, transport, code, bt_adr=0):   # transport: S2 session with send(bytes)/recv()->bytes
        self.t, self.code, self.bt = transport, code, bt_adr
        self.state = dict(lcd={1: "", 2: ""}, leds={}, mb_info=[False]*32, mb_lock=[False]*32,
                          mb_blink=[False]*32, sf=[False]*16, labels={})
        self._ack = None; self._ema = 0; self._start = None; self._texts = None

    async def open(self):
        self._start = asyncio.get_running_loop().create_future()
        asyncio.create_task(self._rx_loop())
        await self.t.send(build_st70(bytes([0x92]) + encode_kennwort(self.code) + bytes([self.bt])))
        db = await asyncio.wait_for(self._start, 15)
        return (db[2] << 8) | db[3] if len(db) >= 4 else 0       # texte CRC

    async def request_texts(self):
        self._texts = asyncio.get_running_loop().create_future()
        await self.t.send(build_st70(bytes([0x98]) + encode_kennwort(self.code) + bytes([self.bt, 0x00, 0xEA])))
        return await asyncio.wait_for(self._texts, 120)

    async def key(self, code: int, wait_ack=False):
        if wait_ack: self._ack = asyncio.get_running_loop().create_future(); self._ema = 0
        await self.t.send(bytes([code]))
        if wait_ack:
            await asyncio.wait_for(self._ack, 20); return self._ema

    async def toggle_switch(self, i):   return await self.key(0xD0 + i - 1, True)
    async def toggle_bypass(self, i):   return await self.key(0x80 + i - 1, True)   # 2 => refused

    async def _rx_loop(self):
        while True:
            on_nutzdaten(await self.t.recv())    # §4, feeding handle_com2bus (§6) and futures
```

---------------------------------------------------------------------------------------------
## 10. Unknowns / to verify on a real panel
1. **[?]** Whether the 1‑byte Tastencode Nutzdaten is really sent bare on the wire (transport may
   need to accept non‑record Nutzdaten) – confirm with transport agent / capture.
2. **[?]** Exact arming dialogue per key (ES/IS/US → "Code eingeben" → digits → OK?), how the panel
   reports refusal (open zones), and how many seconds until LEDs update.
3. **[?]** Semantics of free LEDs 1‑4 and Meldebereich LEDs are installer‑defined (labels help).
4. **[?]** Meaning of 0xCF values other than 2; meaning of 0x4B FernVerbAbgebOk.
5. **[?]** Whether the panel sends a full state snapshot right after 0x93, or only on change
   (app gives no hint; physical keypads get full refresh – likely).
6. **[?]** Which keypad address (BtAdresse 0‑15) to use: must be a com2BUS address enabled for the
   "app"/remote access in the panel (errors 0x1D/0x23 otherwise).
7. **[?]** Required code level: errors 0x21/0x22 show the code must be enabled for remote keypad use.
8. GTyp payload bytes 5..16 are not interpreted by the app.

## Key files
* `079_Telenot.BuildSec.Data.DataTransfer/Telenot.BuildSec.Data.DataTransfer.Communication/DeviceOperations.cs`
* `079_…/Telenot.BuildSec.Data.DataTransfer/DatenblockToBedienteilTranslator.cs`, `DeviceCommunicator.cs`, `EnumConverter.cs`
* `079_…/Datenbloecke.Satztyp70/*.cs`, `079_…/Datenbloecke.Com2Bus/*.cs`
* `040_Telenot.Communication/Telenot.Communication.Vds2465Nutzdaten/{Datenblock,Vds2465Nutzerdatensatz70HDatenblockTransparent,NutzdatenInfoAnalyzer}.cs`
* `082_…/Telenot.BuildSec.Domain.Logic/{ConnectionManager,DomainModel}.cs`
* `087_…/ItemViewModels/{SchaltfunktionIvm,MeldebereichIvm}.cs`, `ViewModels/BedienteilViewModel.cs`
* `083_…/Telenot.BuildSec.Infrastructure.Helper/TextHelper.cs` (LCD umlauts)
* APK `res/layout/fragment_bedienteil.xml` (key codes; decoded with `/tmp/re_scripts/axml_dump.py`)
* IL: `<PrivateImplementationDetails>` 16×0xFF = LCD display‑test pattern
