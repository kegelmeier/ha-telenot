# Telenot (BuildSec) for Home Assistant

A custom integration that controls Telenot intrusion alarm panels (complex400 / compact easy, via an IP transmitter such as comXline) **directly over your LAN**. It speaks the same protocol as the official **Telenot BuildSec** Android app.

The protocol was reverse-engineered from the BuildSec APK (`com.TelenotElectronic.BuildSec`, .NET MAUI). The findings are in [`docs/`](docs/).

> [!WARNING]
> **Only partly validated.** The integration itself has not yet been run against a real panel. Its transport was corrected against a decrypted capture of the BuildSec **iOS** app talking to a real panel (see [`docs/protocol-04-real-panel-capture.md`](docs/protocol-04-real-panel-capture.md)): encryption, key derivation, handshake, login, device type, display and LED stream match. **Arming and disarming have not been observed yet.** The tests run against a simulated panel.
>
> **This controls a security system.** Until you have confirmed it on your own installation:
> - do not rely on the alarm state shown in Home Assistant,
> - verify every arm/disarm at a real keypad or in the BuildSec app,
> - do not build security-relevant automations on it.
>
> Expected confidence per layer: encryption/framing and login high (confirmed by capture), keypad display/LEDs medium-high, arm/disarm dialogue and armed-state detection low until tuned to your panel (see [Unverified points](#unverified-points)). Reports with a debug log are very welcome.

## How it works

BuildSec does not have "arm area N" commands. It is a **remote keypad**: it opens a remote-service session and then sees exactly what a physical BT 800 keypad shows. This integration does the same:

| Layer | What happens |
|---|---|
| TCP | Plain TCP to the panel's IP interface (port as configured for BuildSec). |
| VdS 2465‑S2 | The panel is the protocol master and polls the app continuously. Telegrams are AES‑128‑CBC encrypted (padded to ≥128 bytes) and checksummed with the Internet checksum. Plain VdS header like the iOS app; the Android app's Telenot extension is available as an experimental option. |
| Key | Operator access uses key number 9997, with a per-session key derived from the **Freischaltcode**. Installer / remote-control access uses your **key number + AES key**. |
| Session | Satztyp 0x70 block `92/94/9E` + keypad code (packed BCD) + virtual keypad address. |
| Keypad | The panel **pushes** LCD text, LEDs, buzzer and switching-function state. Commands are one-byte key codes (ES/IS/US, digits, OK, …). |

## Installation

1. Copy `custom_components/telenot` into your Home Assistant `config/custom_components/` folder, or add `kegelmeier/ha-telenot` to HACS as a custom repository (category: Integration).
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Telenot (BuildSec)**.

### What you need (same values as in the BuildSec app)

| Field | Notes |
|---|---|
| Host / Port | IP or hostname of the panel / transmitter and the TCP port enabled for BuildSec. |
| Access level | Operator (Betreiber), Installer (Errichter) or Remote control (hipas). |
| Keypad code | Your operator code (≤ 6 digits). It must be enabled for remote/app use. |
| Virtual keypad address | The com2BUS keypad address (0–15) the panel enables for app access. Use the same one as your BuildSec app (often not the default 15; one real installation uses 5). |
| Freischaltcode | Operator only: 16 hex characters. |
| Key number + AES key | Installer / remote control only: VdS key number and 32 hex characters. |

**Only one remote session can be open at a time.** While Home Assistant holds the connection, the BuildSec app gets "remote access already in use". If you still need the app, switch the integration to **on-demand** mode in its options.

## Entities

| Entity | Source |
|---|---|
| `alarm_control_panel` | Arm away = ES key, arm home = IS key, disarm = US key. If the display asks for a code, the code is entered and confirmed with OK. The state is derived from keypad indicators (see below). The alarm LED means *triggered*. |
| Binary sensors | Alarm, Fault (Störung), Walk test, Operation, Connection, plus 32 indicator areas (Meldebereiche; red LED). |
| Switches | 16 switching functions (Schaltfunktionen) and 32 area locks (sperren/bypass; yellow LED). |
| Sensors | Display line 1 and 2, buzzer pattern, free LEDs 1–4 (colour). |
| Buttons | Every keypad key (disabled by default). Useful for building a keypad card. |
| Service `telenot.send_keys` | Press arbitrary keys, e.g. `keys: "esc down down ok"` or `"arm_away 1234 ok"`. |

Areas and switching functions are named from the label texts stored in the panel, which are downloaded automatically. Only labelled ones are enabled. The download only happens again when the panel's text CRC changes.

### How "armed" is detected

The protocol has **no generic armed bit**. What a keypad shows depends on how your installer programmed the panel. In the options you choose, for *armed away* and *armed home*:

- **Automatic** (default): uses the free LED, indicator area or switching function whose label contains "extern … scharf" or "intern … scharf". If none matches, it uses the last display state (next item).
- **Last display state**: remembers the last time the panel display said "Extern scharf", "Intern scharf" or "Unscharf". It survives restarts. It is never set from Home Assistant's own commands, so a failed arm cannot show as armed. Use this if your keypad lights no LED while armed.
- **Keypad display text**: only the current display content counts.
- A specific **free LED 1–4**, **indicator area 1–32** or **switching function 1–16**.

The alarm panel's attributes show which indicator is currently used. If the state looks wrong, watch the LED and display sensors while arming with a real keypad, then pick the matching indicator in the options.

### Options

- **Connection mode**: *persistent* (real-time, default) or *on demand* (connect every *refresh interval*, disconnect after *idle timeout*).
- **Arm/disarm key sequence**: *automatic* (enter the code only when the display asks), *always* (key → code → OK) or *key only*.
- **Require code in Home Assistant**: if enabled, the code typed in the HA UI is sent to the panel instead of the stored code.
- **Extended Telenot protocol header** (experimental, off): use the Android app's protocol extension instead of the plain VdS header confirmed on a real panel.

## Unverified points

Everything here is derived statically from the app. A packet capture or the first real connection should confirm:

Confirmed by a real-panel capture: encryption and key derivation, 128-byte padding, plain VdS header, handshake, login, device type, label request, display and LED stream, and the initial state snapshot after login.

Still open:

1. The exact arm/disarm dialogue on your panel, including how it refuses when zones are open. *Automatic* mode reacts to the display text.
2. What the display and LEDs show while armed (on one panel, free LEDs 1 and 2 are green while disarmed).
3. Whether the panel enforces its own idle timeout. The integration reconnects automatically with backoff.

Enable debug logging to see every decrypted telegram:

```yaml
logger:
  logs:
    custom_components.telenot: debug
```

### Capturing the BuildSec app yourself

`tools/decode_capture.py` decrypts a packet capture of the BuildSec app (pcap/pcapng, e.g. from an iPhone via `rvictl` + `tcpdump`, or a FRITZ!Box capture). It asks for the Freischaltcode (or reads `TELENOT_AES_KEY`) and masks the keypad code. Captures and decoded output contain your panel's data, so do not post them unredacted.

## Not implemented (yet)

- **hiXserver / hiXC cloud access.** The BuildSec app can also tunnel through Telenot's cloud. That needs a client certificate enrolled with an installer-issued code. See [`docs/protocol-03-hixserver.md`](docs/protocol-03-hixserver.md).
- **Push notifications.** These come from hiXserver via Firebase, not from the panel session.
- QR-code import of device settings, and installer parameterisation.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio cryptography pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

`tests/fake_panel.py` is a simulated panel (S2 master + keypad) used by the tests. The protocol library lives in `custom_components/telenot/pytelenot/` and has no Home Assistant dependency.

## Disclaimer

Not affiliated with Telenot Electronic GmbH. Use at your own risk: this controls a security system. Check that arming and disarming behave as expected before relying on automations.
