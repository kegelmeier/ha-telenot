# 4. Findings from a real panel (BuildSec iOS app)

A packet capture of the **BuildSec iOS app** talking to a real panel was decrypted with
`tools/decode_capture.py` (operator access, key 9997). Panel: GTYP family `0xA4`, firmware
date "May 5 2017", reached over the internet via port forwarding. Three sessions: connect,
browse the keypad with the arrow keys, disconnect. **No arm/disarm was captured yet.**

Where this differs from the Android source, the integration follows the capture.

| Topic | Android source (static) | Real panel + iOS app (observed) |
|---|---|---|
| Header after sync | Telenot extension, PK=0x82, V=3 | **Plain VdS 2465, PK=0x01** for every telegram, both directions |
| Minimum encrypted size | 256 bytes | **128 bytes** (both directions; 272 for long GTYP answers) |
| Poll time | POLLTIME_SET (NK=5), 2000 ms | Not sent. Panel polls/pushes every ~50–650 ms |
| SERVICE_REQUEST | current TC/RC, PK=0x82 | **TC=0, RC=0**, PK=0x01, L=0 |
| Session end | POLL with NK=0x09 | St70 block **0x8E + keypad code (3 bytes)**, then TCP FIN. Not in the Android enum |
| Login | `92 code addr` (GeraetFrei 0x10) | identical; reply `93 BT 00 00` (text CRC 0 on this panel) |
| Device type | `18 code` (GeraetFrei 0x00) | identical; the `19` answer is long (family, version, "Standard", serial-like digits, build date/time) |
| Labels | `98 code BT 00 EA` → 0xCD stream | identical; this panel has no labels (`CD 01`, `CD 02 00 00`) |
| Unsolicited | – | `St70 4B` (FernVerbAbgebOk) may arrive before the login reply |

## Keypad stream (com2BUS inside `99 BT …`)

* LCD (0x54): `1B 0C` (clear) precedes each `1B 80` / `1B C0` line. Idle shows date and time.
  Arrow down shows the area name with its state (`unscharf`), then `keine Meldungen!`.
* LEDs (0xC0) are short on this panel: `C0 00 51 10` = buzzer off, operation LED on, free LED 1
  and 2 green (disarmed). Byte 3 bit 4/7 change with key presses (not mapped by the Android parser).
  `C0 00` (buzzer only) and `C1 00 00 00` (blink) also occur.
* Further blocks not handled by the Android parser we analysed: `C4`, `C6 01`, `C8 xx xx`,
  `C9 00`, `CC 00 xx xx`, `D7 00 01`. Logged at debug level.
* Key presses are single bytes (`2B` = down) and are acknowledged with `CE 2B`.

## Still open

* The arm/disarm dialogue and the display/LED state while armed.
* Whether this panel accepts the extended header at all (option "extended header").
