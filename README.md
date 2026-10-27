<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/kegelmeier/ha-telenot/main/custom_components/telenot_buildsec/brand/dark_logo.png">
  <img src="https://raw.githubusercontent.com/kegelmeier/ha-telenot/main/custom_components/telenot_buildsec/brand/logo.png" alt="Telenot" width="240">
</picture>

# Telenot (BuildSec)

Local, **cloud-free** Home Assistant integration for **Telenot** intrusion alarm
panels (complex400 / compact easy), speaking the protocol of the official
**BuildSec** app.

[![HACS Custom][hacs-shield]][hacs]
[![GitHub Release][release-shield]][releases]
[![License][license-shield]][license]
![HA Min Version][ha-shield]
[![Validate][validate-shield]][validate]

[![Open in HACS][hacs-repo-badge]][hacs-repo]

</div>

Talks directly to the panel's IP interface (e.g. comXline) over local TCP. It needs no
hiXserver account, no cloud and no internet connection. The protocol is not publicly
documented. It was reverse-engineered from the BuildSec Android app and then corrected
against a decrypted capture of the BuildSec iOS app talking to a real panel (see
[`docs/`](docs/)).

> [!WARNING]
> **This controls a security system and is only partly validated.**
> Encryption, key derivation, handshake, login, device type, display and LED stream were
> confirmed against a real panel capture. **Arming and disarming have not been observed on
> real hardware yet.** Until you have confirmed them on your installation:
> - verify every arm/disarm at a real keypad or in the BuildSec app,
> - do the first test while someone is at home, next to the keypad,
> - do not build security-relevant automations on it.
>
> Reports with a debug log are very welcome.

> [!CAUTION]
> **Security notice.** This is an unofficial community project. It is **not certified by VdS,
> not approved by Telenot** and not a replacement for a certified alarm transmission or
> monitoring service. Connecting third-party software to an intrusion alarm panel changes its
> security properties: anyone who controls your Home Assistant, or the network path to the
> panel's IP interface, may be able to read the system state or **arm and disarm the system**,
> and automations can do so without a code. This may affect a VdS classification, the
> conditions of your insurance or your installer's warranty, so check with your installer and
> insurer first. Secure Home Assistant and the panel interface accordingly: strong
> credentials, no exposure of the panel port to the internet, a separate network where possible.

## ✨ Features

**Remote keypad**
- Live 2×16 keypad display (two sensors), pushed by the panel in real time
- Every keypad key as a button (disabled by default) and a `telenot_buildsec.send_keys` service

**Alarm**
- `alarm_control_panel` with arm away (ES), arm home (IS) and disarm (US); the code is
  entered automatically when the display asks for it
- Configurable armed-state detection (LED, indicator area, switching function or display text)
- Optional Home Assistant alarm code, separately for arm away, arm home and disarm; a wrong
  code never reaches the panel
- Event log entity: armed, disarmed, alarm and fault as reported by the panel, plus commands
  from Home Assistant that failed and why
- Alarm, fault (Störung), walk test and operation indicators

**Open windows, doors and motion**
- Reads the keypad's message list (the DOWN key) whenever the *Bereit* LEDs go out, and again
  every 2 minutes while something stays open
- One binary sensor per detector, created automatically the first time it shows up
  (e.g. *Kippfenster re. OG*), with a device class guessed from its name
- *Open messages* sensor (count and list), *Read message list* button and event-log entries
  `detector_opened` / `detector_closed`

**Areas & switching functions**
- 32 indicator areas (Meldebereiche) and 32 area locks (sperren/bypass)
- 16 switching functions (Schaltfunktionen) as switches
- Named from the label texts stored in the panel; only labelled ones are enabled
- Each labelled indicator area becomes its own device (a child of the panel) with its state
  and lock switch, and a device class guessed from the label (window, door, motion, smoke, …)

**Connection**
- Persistent session with automatic reconnect, or on-demand mode so the BuildSec app stays usable
- *Remote access* switch to release the session for the BuildSec app and take it back later
- Never retries wrong credentials (avoids keypad lockouts and manipulation reports)
- After a (re)connect, entities stay unavailable until the panel has sent its full status, so a
  stale state is never shown as current. Exception: when the panel ends the session on its own
  (some panels close every remote session after about 35½ minutes), the integration retries after
  1, 2, 4 … s and keeps the last state for up to 60 s instead of flagging everything unavailable
- The *Connection* sensor shows when the session started, how often the panel dropped it and why
  (attributes `connected_since`, `disconnects`, `last_disconnect`, `last_disconnect_reason`); each
  drop is also logged at INFO level
- German and English translations, diagnostics with secrets redacted

## 📦 Installation

### HACS (recommended)

1. Make sure [HACS](https://hacs.xyz) is installed.
2. Add this repository as a **custom repository** (category **Integration**):

   [![Open in HACS][hacs-repo-badge]][hacs-repo]

   …or in HACS go to **⋮ → Custom repositories**, paste
   `https://github.com/kegelmeier/ha-telenot`, choose **Integration**, and add it.
3. Search for **Telenot (BuildSec)**, install, and **restart Home Assistant**.

### Manual

1. Copy `custom_components/telenot_buildsec/` into your `config/custom_components/` directory.
2. Restart Home Assistant.

## ⚙️ Configuration

Close the BuildSec app first, then add the integration:

[![Add Integration][config-flow-badge]][config-flow]

…or go to **Settings → Devices & Services → Add Integration → “Telenot (BuildSec)”**, then
enter the same values as in the BuildSec app:

| Field | Example | Notes |
|-------|---------|-------|
| **Host** | `192.168.1.50` | Local IP of the panel / IP transmitter |
| **Port** | `7657` | TCP port enabled for BuildSec on your transmitter |
| **Access level** | Operator | Operator (Betreiber), Installer (Errichter) or Remote control (hipas) |
| **Keypad code** | — | Your operator code (≤ 6 digits), enabled for remote/app use |
| **Virtual keypad address** | `5` | Same address as in your BuildSec app (often not the default 15) |
| **Freischaltcode** | — | Operator only: 16 hex characters |
| **Key number + AES key** | — | Installer / remote control only |

Each value is tried exactly once during setup. If the panel rejects the code or key, the
integration stops and asks you to re-authenticate instead of retrying.

> [!IMPORTANT]
> **Only one remote session can be open at a time.** While Home Assistant holds the
> connection, the BuildSec app reports “remote access already in use”. Turn off the
> **Remote access** switch while you use the app (and back on afterwards), or switch to
> **on-demand** mode in the options.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| **Connection mode** | Persistent | *Persistent* = real-time; *On demand* = connect every refresh interval, disconnect after the idle timeout |
| **Indicator for armed away / home** | Automatic | Which LED, area, switching function or display text means “armed” (see below) |
| **Arm/disarm key sequence** | Automatic | *Automatic* enters the code only when the display asks; *Always* = key → code → OK; *Key only* |
| **Alarm code (Home Assistant)** | empty | Checked by Home Assistant, never sent to the panel. Empty = never ask for a code |
| **Require the alarm code for** | all | Any of arm away, arm home, disarm. Home Assistant has one “code to arm” switch: if one arm mode needs the code, its keypad also opens for the other, but only the selected mode checks it |
| **Keypad LED labels** | empty | Text next to the four free LEDs, as on your keypad (e.g. *Intern Bereit, Extern Bereit, Intern Scharf, Extern Scharf*). Names the LED sensors, labels the keypad card and feeds the automatic armed detection. Empty = text from the panel, if any |
| **Extended Telenot protocol header** | Off | Experimental: the Android app's protocol extension instead of the plain VdS header confirmed on a real panel |

### How “armed” is detected

The protocol has **no generic armed bit**. A remote keypad sees what a physical keypad
shows, and that depends on how your installer programmed the panel. For *armed away* and
*armed home* you choose:

- **Automatic**: the free LED, area or switching function whose label contains
  “extern … scharf” / “intern … scharf”; otherwise the last display state. Your own
  **keypad LED labels** count as labels, so entering *Intern Scharf* / *Extern Scharf*
  there is usually all it takes.
- **Last display state**: remembers when the display last said “Extern scharf”,
  “Intern scharf” or “Unscharf”. It is never set from Home Assistant's own commands, so a
  failed arm cannot show as armed. Use this if your keypad lights no LED while armed.
- **Keypad display text**: only the current display content counts.
- A specific **free LED 1–4**, **indicator area 1–32** or **switching function 1–16**.

The alarm panel's attributes show which indicator is in use. If the state looks wrong,
watch the LED and display sensors while arming at a real keypad and pick the matching one.

## 🎛️ Keypad card

The integration ships a Lovelace card that looks and works like the BuildSec keypad:
LCD, the four LEDs with your labels, alarm/fault/walk-test indicators and every key.
It is registered automatically as a dashboard resource (`/telenot_buildsec/telenot-keypad-card.js`), no manual resource or HACS frontend install needed.

```yaml
type: custom:telenot-keypad-card
entity: alarm_control_panel.alarmanlage
confirm_arming: true  # arm keys need a second tap within 3 s
show_logo: true
```

Tap an LED, a status icon or the display to open its history. When detectors are open, a
chip under the keypad shows how many; tap it for the list.

When the remote access is paused, the card says so and offers a *Connect* button; while
connected, *Release for app* pauses it so you can use the BuildSec app.

## 🔌 Entities

| Entity | Type | Description |
|---|---|---|
| Alarm | Alarm control panel | Arm away / arm home / disarm; alarm LED ⇒ *triggered* |
| Alarm, Fault, Walk test, Operation, Connection | Binary sensor | Keypad indicators and session state |
| Area 1–32 | Binary sensor | Indicator areas (red LED), enabled when labelled |
| Switching function 1–16 | Switch | Toggle a switching function |
| Area lock 1–32 | Switch | Lock/bypass an area (yellow LED) |
| Remote access | Switch | Off closes the session and keeps Home Assistant away, e.g. for the BuildSec app; kept across restarts |
| Display line 1 / 2 | Sensor | Keypad LCD text |
| Buzzer, Free LED 1–4 | Sensor | Buzzer pattern, LED colour |
| Event log | Event | `armed_away`, `armed_home`, `disarmed`, `triggered`, `fault`, `fault_cleared`, `command_failed`, `detector_opened`, `detector_closed` with source, action, reason, detector and display text |
| Detectors | Binary sensor | One per detector found in the message list, on while listed |
| Open messages | Sensor | Number of listed detectors, names in `detectors` |
| Read message list | Button | Scroll through the message list now (disarmed only) |
| Keypad keys | Button | Every key (disabled by default), e.g. for a keypad card |

Labelled indicator areas appear as their own devices under the panel, each with a *State*
binary sensor and a *Locked* switch. If the guessed device class is wrong, change it with
*Show as* in the entity settings.

Service `telenot_buildsec.send_keys` presses arbitrary keys, e.g. `keys: "esc down down ok"`.
Menu keys can change the panel's state, so use it with care.

## 🔍 How it works

BuildSec has no “arm area N” command. It is a **remote keypad**: it opens a remote-service
session and then sees exactly what a physical keypad shows.

| Layer | What happens |
|---|---|
| TCP | Plain TCP to the panel's IP interface |
| VdS 2465‑S2 | The panel is the protocol master and polls continuously. Telegrams are AES‑128‑CBC encrypted (padded to ≥ 128 bytes) with an Internet checksum |
| Key | Operator: key number 9997, per-session key derived from the **Freischaltcode**. Installer / remote control: your key number + AES key |
| Session | Satztyp 0x70 login block `92/94/9E` + keypad code + virtual keypad address |
| Keypad | The panel **pushes** LCD text, LEDs, buzzer and switching-function state; commands are one-byte key codes |

Protocol notes: [transport](docs/protocol-01-transport.md) ·
[application layer](docs/protocol-02-application-layer.md) ·
[hiXserver](docs/protocol-03-hixserver.md) ·
[real-panel capture](docs/protocol-04-real-panel-capture.md)

## 📝 Notes & limitations

- **Validated by capture:** encryption, 128-byte padding, plain VdS header, handshake,
  login, device type, label request, display/LED stream and the state snapshot after login.
- **Still open:** the exact arm/disarm dialogue on your panel (including refusals when zones
  are open), what display/LEDs show while armed, and whether the panel enforces an idle timeout.
- **Not implemented:** hiXserver / hiXC cloud access, push notifications (Firebase via
  hiXserver), QR-code import and installer parameterisation.
- Brand icon/logo are bundled and served via the Home Assistant brands proxy API
  (requires **Home Assistant 2026.3+**).
- The panel always gets the stored keypad code. The optional Home Assistant alarm code is a
  separate gate in Home Assistant only.
- The BuildSec keypad channel sends no zone states: the indicator areas stay off on panels
  like a complex400. Open detectors come from the message list instead, which is read by
  pressing DOWN on the virtual keypad. That needs an LED labelled *Bereit* (option *LED
  labels*), runs only while disarmed and waits until nobody has pressed a key for 30 s.
  When everything is closed again, the *Bereit* LEDs alone clear the detectors.

Enable debug logging to see every decrypted telegram:

```yaml
logger:
  logs:
    custom_components.telenot_buildsec: debug
```

### Capturing the BuildSec app yourself

[`tools/decode_capture.py`](tools/decode_capture.py) decrypts a packet capture of the
BuildSec app (pcap/pcapng, e.g. from an iPhone via `rvictl` + `tcpdump`, or a router
capture). It asks for the Freischaltcode (or reads `TELENOT_AES_KEY`) and masks the keypad
code. Captures contain your panel's data, so do not post them unredacted.

## 🛠️ Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio cryptography pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

`tests/fake_panel.py` simulates a panel (S2 master + keypad). The protocol library lives in
`custom_components/telenot_buildsec/pytelenot/` and has no Home Assistant dependency.

## 🙏 Credits

Several ideas (alarm code per action, event log, child devices per area, availability only
after a full status, CI validation, the security notice) were inspired by
[benji2k2/ha_telenot](https://github.com/benji2k2/ha_telenot), which connects to the
complex 400 over its serial GMS interface instead. No code was copied.

## ⚠️ Disclaimer

This is an unofficial, community-built integration. It is **not affiliated with,
endorsed by, or supported by Telenot Electronic GmbH**. “Telenot”, “BuildSec”, the
Telenot logo and the BuildSec app icon are trademarks of Telenot Electronic GmbH and are
used here only to identify the compatible product. Use at your own risk: this controls a
security system, and the author accepts no liability for false alarms, failed arming or
any other damage.

## 📄 License

Released under the [MIT License](LICENSE). The MIT license does not cover the Telenot
trademarks and brand images.

<!-- badges -->
[hacs]: https://hacs.xyz
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge
[releases]: https://github.com/kegelmeier/ha-telenot/releases
[release-shield]: https://img.shields.io/github/v/release/kegelmeier/ha-telenot?style=for-the-badge
[license]: https://github.com/kegelmeier/ha-telenot/blob/main/LICENSE
[license-shield]: https://img.shields.io/github/license/kegelmeier/ha-telenot?style=for-the-badge
[ha-shield]: https://img.shields.io/badge/Home%20Assistant-2026.9%2B-41BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white
[hacs-repo]: https://my.home-assistant.io/redirect/hacs_repository/?owner=kegelmeier&repository=ha-telenot&category=integration
[hacs-repo-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[config-flow]: https://my.home-assistant.io/redirect/config_flow_start/?domain=telenot_buildsec
[config-flow-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[validate]: https://github.com/kegelmeier/ha-telenot/actions/workflows/validate.yml
[validate-shield]: https://img.shields.io/github/actions/workflow/status/kegelmeier/ha-telenot/validate.yml?branch=main&style=for-the-badge&label=Validate
