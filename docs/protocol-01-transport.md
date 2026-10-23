# 01 – Direct IP/LAN transport (BuildSec app → Telenot panel)

Scope: TCP socket → S2 (VdS 2465‑S2 + Telenot extension "TNT") telegrams → AES → "Nutzdaten" records → Fernservice session setup / teardown.
All paths are relative to `/Users/jens.kegelmann/dev_projects/telenot/_re/src/`.

Legend: **[F]** = observed fact in code, **[I]** = inference, **[?]** = unknown / to verify on the wire.

---

## 0. TL;DR for the Python implementation

1. **[F]** Plain TCP client → `host:port` (no TLS, no UDP). 15 s connect timeout.
2. **[F]** On TCP, every message is `K(u16 BE) | SL(u16 BE) | payload[SL]`. K = AES key number (0 = plaintext, 0xFFFF = "unknown key" error).
3. **[F]** The **panel is the S2 master**, the app is the **slave**, even though the app opens the TCP connection. After connecting, the app **waits** for the panel's `SYNC_REQUEST`. It never sends first.
4. **[F]** The payload is a 13‑byte VdS frame (`TC u32 | CRC u16 | RC u32 | IK | PK | L`), followed by a 4‑byte Telenot extension (`NL u16 | V | NK`) when PK=0x82, then the data. All integers are big‑endian. The CRC is the RFC 1071 Internet checksum, computed with bytes 4..5 set to 0.
5. **[F]** Encryption is AES‑128‑CBC with a zero IV and no PKCS padding. The IV resets for every telegram. The plaintext is padded with random bytes to max(256, next multiple of 16). The whole S2 telegram, including the header, is encrypted. There is no MAC; the Internet checksum is the only integrity check.
6. **[F]** Key choice:
   * Installer ("Errichter") and remote-control ("Fernbedien") modes: the user enters a key number (Schlüsselnummer) and a 32‑hex AES key, used directly.
   * Operator ("Betreiber") mode, direct connection: key number **9997**. The AES key is **derived per session** from the 16‑hex "Freischaltcode" (licence key) and the **TC of the panel's SYNC_REQUEST**.
7. **[F]** Session:
   * panel `SYNC_REQUEST` (PK=VdS, plaintext, K=0, data=[window=1]) → app `SYNC_RESPONSE` (encrypted, data=[0x01])
   * → panel `POLL` → session ready
   * → app `POLLTIME_SET` = 2000 ms → panel `POLL`+`POLLTIME_ACK`
   * → strict ping‑pong. Every panel telegram gets exactly one app telegram (POLL, or DATA if anything is queued) after ≤ 50 ms.
8. **[F]** App-level login is a Satztyp 0x70 record carrying a Telenot datablock, not a 0x56/0x42 record:
   * "Ferndiagnose einschalten": `06 70 10 92|94|9E <code 3 B packed BCD> <BT-addr>`
   * reply 0x70 with `93|95|9F <BT> <TexteCrc u16>`, or error `23 <Grund>`
   * then (direct only) "GTyp anfordern": `05 70 00 18 <code 3 B>` → reply 0x70 `19 <family> <maj> <min> <patch> …`.
9. **[F]** Disconnect: app sends `POLL` with NK=0x09 SESSION_CLOSE (protocol V3), waits 50 ms, then closes the socket.

---

## 1. Connection settings (what the user enters)

### 1.1 Data model
**[F]** `080_…DataTransferObjects/DirectConnectedDeviceData.cs`: `IpAdresse` (string, may be a hostname), `Port` (int, default sentinel `16777215` = "not set"), `SchluesselNummer` (string), `AesSchluessel` (string).
**[F]** `080_…/DeviceData.cs`: `ConnectionType`, `BerechtigungsEbene`, `BedienfreigabeCode` (keypad code), `FreischalteCode` (licence key), `BedienteilAdresse` (default sentinel 16777215), `IsLegacyEmz` (default true), name/texts.
**[F]** Runtime view `079_…/Connection/IConnectionData.cs` / `ConnectionData.cs`:

| Field | Type | Used for |
|---|---|---|
| `ConnectionType` | `EConnectionType {DirectConnected=0, HixServerRestApi=1, HixCRestApi=2}` | 0 = direct LAN/WAN TCP (this document) |
| `BerechtigungsEbene` | `EBerechtigungsEbene {ErrichterMode=0, BetreiberMode=1, FernbedienMode=2}` | selects key handling + login datablock |
| `HostName` | string | TCP target (IP or DNS name, e.g. a dynamic DNS name) |
| `Port` | int | TCP port. 1..65535, **no default** (sentinel 16777215 = unset) |
| `Schluesselnummer` | string (decimal) | VdS AES key number (K field). Errichter/Fernbedien only |
| `AesSchluessel` | string, exactly 32 hex chars | AES‑128 key (16 bytes). Errichter/Fernbedien only |
| `Freischaltcode` | string, exactly 16 hex chars | 8‑byte licence key used for the key‑9997 derivation (Betreiber) |
| `BedienteilCode` | string, ≤ 6 digits | keypad user/installer code → packed BCD "Kennwort" in the login datablock |
| `BedienteilAdresse` | int 0..15 | virtual keypad (BT) address on com2BUS |

**[F]** Validation (`087_…/ViewModels/DeviceEditViewModel.cs` ~l.1540‑1590):
* `Port` must be 1..65535.
* `BedienteilAdresse` must be 0..15.
* Errichter mode: `AesSchluessel.Length == 32` and hex.
* Errichter mode: `SchluesselNummer` parses as int ≥ 0 and is **not** one of {0, 65535, 9990..9999}. This list comes from the static array `00 00 00 00 ff ff 00 00 06 27 … 0f 27`, recovered from IL.
* Betreiber mode: `Freischaltcode` is 16 hex chars.
**[F]** `ConnectionData.HasAllValuesForDirectConnection()`:
* host non‑empty, port 0..65535, BT address 0..15
* Ebene 0/2 need `Schluesselnummer` + `AesSchluessel`
* Ebene 1 needs `Freischaltcode`.

**[F]** The app contains a built-in demo device (Betreiber mode) whose port is the only concrete port value in the app (credentials omitted here). **[I]** No standard port is hardcoded; it is whatever is configured in the comXline/panel IP module.

### 1.2 Key selection
**[F]** `079_…/ConnectionWrappers/ConnectionWrapper.cs` `CreateS2ProtocolData`:
```csharp
if (ConnectionType == 0 /*Direct*/ && BerechtigungsEbene == 1 /*Betreiber*/) {
    byte[] array = ByteArrayConverter.ParseHex(connectionData.Freischaltcode);       // 8 bytes
    return new S2ProtocolData(vdsAesCipherFactoryFactory.Create((ushort)9997, array));
}
if (!(connectionData.Schluesselnummer == "0")) {
    ushort num = Convert.ToUInt16(connectionData.Schluesselnummer, CultureInfo.CurrentCulture);
    byte[] array2 = ByteArrayConverter.ParseHex(connectionData.AesSchluessel);       // 16 bytes
    return new S2ProtocolData(vdsAesCipherFactoryFactory.Create(num, array2));
}
return new S2ProtocolData(vdsAesCipherFactoryFactory.Create((ushort)0, new byte[16]));  // plaintext
```
`ParseHex` (`083_…/FrameworkExtensions/ByteArrayConverter.cs`) is ordinary "hi‑nibble first" hex → bytes.

Reserved key numbers (`047_…/Telenot.Infrastructure/EReservedAesKeyNumber.cs`):
* 0 = Plain
* 65535 = Unknown (error)
* 9990..9999 = Telenot range: 9996 ComvisApps, **9997 ComvisMci (used for Betreiber)**, 9998 Barcode, 9999 Identnummer.
**[F]** Only 0, 9997 and the user's own key number are used on this path. 9999/9998 are not referenced elsewhere.

### 1.3 Other paths (out of scope, for orientation)
* **[F]** For a hiXserver connection, `ConnectionDataHelper.InitConnectionData` overwrites `HostName/Port/AesSchluessel` with the proxy host, proxy port and `SessionKey` from the TMP REST call. It also sets `Schluesselnummer = ProxyPort.ToString()` (sic). After that, exactly the same S2 transport is used. See `03_hixserver.md`.
* **[F]** QR import of device configs (`087_…/Services/BarcodeScanProcess.cs`, `085_…/Net.Security/StringCipher.cs`) works like this:
  * base64 of `salt(32) | iv(16) | AES‑256‑CBC‑PKCS7(JSON BarcodeContainer)`
  * the key is PBKDF2‑SHA256(password, salt, 10000 iterations, 32 bytes)
  * the JSON holds `ConnectionType`, `DirectConnectedDeviceData{IpAdresse,Port,SchluesselNummer,AesSchluessel}`, …, `Version=1`.
  * This could be used to import the configuration into HA.

---

## 2. TCP layer

**[F]** `085_…Droid/…IpPortClientAsync/PortAsyncFactory.cs` → `new IpPortClientAsync(hostname, port)` (System.Net.Sockets.TcpClient):
* `ConnectAsync(host, port)`
* plain `NetworkStream` read/write
* a 0‑byte read plus `Socket.Poll(1, SelectRead)` means the remote closed the connection.
* No TLS, no keepalive options, no NoDelay setting.

**[F]** Timeouts (from code):

| What | Value | Where |
|---|---|---|
| TCP open | 15 000 ms | `ConnectionWrapper.OpenPortAndProtocol` `WithTimeout(portAsync.OpenAsync, 15000)` |
| each socket send | 2 s | `S2ProtocolTelenotAsync.CreateAndSendTelegram` `WaitAsync(TimeSpan.FromSeconds(2))` |
| **function monitoring** (max gap between two received telegrams) | **24 s** | `functionMonitoringTime = TimeSpan.FromSeconds(24)`; `S2TelegramReceiver` restarts it per telegram |
| data monitoring (reply to POLLTIME_SET / raw transfers) | 25 s | `dataMonitoringTime = functionMonitoringTime + 1s` |
| app idle watchdog (no telegram *with Nutzdaten* in either direction) | 10 min → app disconnects | `S2ProcedureSlave.WatchDog(TimeSpan.FromMinutes(10))` – app policy, not protocol |
| cancel‑session receive wait | 3.5 s | `ReceiveTelegramOrThrowForCancelSession` |
| full disconnect | 60 s | `DeviceCommunicator.StopFernservice` `WithTimeout(Disconnect(), 60000)` |
| linger before socket close | 50 ms | `HandleTelegrams` "Warte 50 ms damit ausstehende Daten noch gesendet werden können" |

### 2.1 TCP message framing (outer envelope)
**[F]** `040_…/Protocols/S2TelegramReceiver.cs`: read exactly 4 bytes into the header, build `new S2TelegramEncrypted(headerBytes)`, then read exactly `SL` bytes.
**[F]** `040_…/Protocols/S2TelegramEncrypted.cs`:
```csharp
ushort k  = Endianess.Toggle(BitConverter.ToUInt16(headerBytes, 0));   // big endian
ushort sl = Endianess.Toggle(BitConverter.ToUInt16(headerBytes, 2));
if (sl < 13) throw new ArgumentOutOfRangeException("sl");
// Serialize(): K(2,BE) | SL(2,BE) | EncryptedData[SL]
```
```
+--------+--------+---------------------------------------+
| K u16  | SL u16 | payload (SL bytes)                    |
|  BE    |  BE    | K==0: plaintext S2 telegram           |
|        |        | K!=0: AES-CBC(padded S2 telegram)     |
+--------+--------+---------------------------------------+
```
There are no start or stop bytes and no escaping. The length field is the only delimiter. **[F]** SL ≥ 13 is enforced.

---

## 3. S2 telegram (plaintext layout)

**[F]** `040_…/Protocols/S2Telegram.cs` (`VdsFrameLength = 13`, `TntFrameLength = 4`, `MaxVdsNutzdaten = 247`, `MaxTntNutzdaten = 65518`):

| Off | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | TC | Transmission counter (Sendezähler), u32 BE |
| 4 | 2 | CRC | Internet checksum (see §4), u16 BE |
| 6 | 4 | RC | Receive counter (Empfangszähler), u32 BE |
| 10 | 1 | IK | Informationskennung |
| 11 | 1 | PK | Protokollkennung: `0x01` VdS 2465, `0x82` (130) Telenot |
| 12 | 1 | L | PK=0x01: number of Nutzdaten bytes. PK=0x82: length of the TNT extension, must be **4** for V ≤ 3 |
| 13 | 2 | NL | (PK=0x82 only) Nutzdaten length, u16 BE |
| 15 | 1 | V | (PK=0x82 only) Telenot protocol extension version 1..3 (app starts with **3**) |
| 16 | 1 | NK | (PK=0x82 only) Nutzdatenkennung |
| 13 / 17 | n | Nutzdaten | n = L (VdS) or NL (TNT) |

**[F]** Enums (`EInformationsKennung.cs`, `ENutzdatenKennung.cs`, `EProtokollKennung.cs`, `EProtokollErweiterung.cs`):

| IK | Name | Nutzdaten rule |
|---|---|---|
| 1 | SYNC_REQUEST | exactly 1 byte (window size) |
| 2 | SYNC_RESPONSE | exactly 1 byte |
| 3 | POLLING | none |
| 4 | DATA | ≥ 1 byte (≤ 247 when NK=1) |
| 5 | ERROR_UNKNOWN_IK | none |
| 6 | ERROR_UNKNOWN_PK | none |
| 7 | SERVICE_REQUEST | none |

| NK | Name | Allowed with IK |
|---|---|---|
| 0x01 | USERDATA_VDS (VdS‑2465 Nutzdaten records) | SYNC_*, POLL, DATA, ERROR_* |
| 0x02 | USERDATA_RAW_AUDIO | DATA |
| 0x03 | USERDATA_RAW_FIRMWARE | DATA |
| 0x04 | USERDATA_RAW_ACK | POLL |
| 0x05 | USERDATA_POLLTIME_SET (V≥2, data = u16 BE ms) | DATA |
| 0x06 | USERDATA_POLLTIME_ACK (V≥2) | POLL |
| 0x07 | USERDATA_RAW (V≥3) | DATA |
| 0x08 | USERDATA_RAW_END | DATA |
| 0x09 | SESSION_CLOSE | POLL |
| 0xFC | ERROR_SESSION_CANCELLED | POLL |
| 0xFD | ERROR_VERSION_MISMATCH | POLL |
| 0xFE | ERROR_INVALID_ID (invalid NK) | POLL |

**[F]** Rules enforced by `S2ProcedureSlave.VerifyFrameProtocol`:
* the master's **SYNC_REQUEST must use PK=0x01 (VdS, no TNT extension)**
* every other telegram, in both directions, must use **PK=0x82**
* the app always sends PK=0x82, L=4, V=current version.

---

## 4. Checksum

**[F]** `040_…/Protocols/S2TelegramCipher.cs` + `047_…/Checksums/InternetChecksum.cs`:
```csharp
private static ushort CalculateChecksum(byte[] s2TelegramStream) {
    byte[] array = (copy of stream); array[4] = 0; array[5] = 0;
    return InternetChecksum.Calculate(array);  // 16-bit BE words, end-around carry, odd byte <<8, ~sum
}
SetChecksumInStream: stream[4..5] = checksum (big endian)
```
```python
def inet_checksum(buf: bytes) -> int:
    s, i, n = 0, 0, len(buf)
    while n > 1:
        s += (buf[i] << 8) | buf[i + 1]
        if s & 0xFFFF0000: s = (s & 0xFFFF) + 1
        i += 2; n -= 2
    if n:
        s += buf[i] << 8
        if s & 0xFFFF0000: s = (s & 0xFFFF) + 1
    return (~s) & 0xFFFF

def s2_checksum(stream: bytes) -> int:
    b = bytearray(stream); b[4] = b[5] = 0
    return inet_checksum(bytes(b))
```

**[F] Asymmetry – important:**
* **TX** (`Encrypt`): when K≠0 the stream is **padded first**, then the checksum is computed over the **padded** buffer, which includes the random pad bytes.
* **RX** (`Decrypt`): the decrypted buffer is **truncated** to `17+NL` (TNT) or `13+L` (VdS), and the checksum is verified over the **unpadded** stream.
* A checksum mismatch makes the app *ignore* the telegram (`verificationResult = Ignore`) and wait for a repetition. It does not abort.

**[I]** The panel therefore probably checks over the whole decrypted block and pads with zeros. With zero padding, padded and unpadded checksums are identical.

**Recommendation:**
* TX: pad with **zero bytes** (or random, like the app) and compute the checksum over the padded buffer. With zero padding both interpretations match.
* RX: verify over the unpadded stream first, then fall back to the padded one, and log which one matched.

---

## 5. Encryption

**[F]** `047_…/Security/AesCipher.cs`: `Mode = (CipherMode)1` = **CBC**, `Padding = (PaddingMode)1` = **None**, `BlockSize = 128`, `IV = new byte[16]` (all zero). `CreateEncryptor()` / `CreateDecryptor()` are called **per Encrypt/Decrypt call**, so the IV is zero for every telegram and there is no chaining across telegrams. The key is 16 bytes (AES‑128). The code would also accept 24/32‑byte keys, but the UI forces 32 hex characters = 16 bytes.

**[F]** Padding (`S2TelegramCipher.AddPaddingBytesToStream`, `MinimumTelegramLength = 256`):
```csharp
int num  = (stream.Length % 16 != 0) ? (16 - stream.Length % 16) : 0;
int num2 = Math.Max(256 - stream.Length, num);   // random bytes appended
```
In practice every encrypted telegram is at least 256 bytes on the wire (+4 header), because SL ≥ 256. There is no padding when K=0.

**[F]** What is encrypted: the **entire S2 telegram** (TC, CRC, RC, IK, PK, L, TNT extension, data, pad). Only the 4‑byte `K|SL` envelope is in clear. There is no MAC or HMAC. Integrity depends on the checksum, the counters and the frame validation.

**[F]** Key‑number handling (`S2ProcedureSlave.VerifyKeyNumber`):
* `sessionKeyNumber` starts at 0.
* An incoming telegram is accepted if `K == sessionKeyNumber`, **or** if `K == 0` and the telegram deserialises as SYNC_REQUEST. So the panel's SYNC_REQUEST is always plaintext with K=0.
* `K == 0xFFFF` from the panel means "device does not know this key number" → fatal error ("Error.S2ProcedureSlave.KeynumberIsUnknownForDevice").
* Any other mismatch → InvalidK. The app then resends its last telegram with `K = 0xFFFF`.
* When the first SYNC_REQUEST is processed: `s2TelegramCipher.InitCipher(telegram.TC); sessionKeyNumber = s2TelegramCipher.KeyNumber;`
* From then on all app telegrams, starting with SYNC_RESPONSE, are encrypted with K = key number, and the panel's telegrams are expected with the same K.
* **[F]** Too many repetitions during sync with K≠0 → error text "Check whether the AES key is correct". This is the typical symptom of a wrong key.

### 5.1 Key 9997 derivation (Betreiber, from Freischaltcode)
**[F]** `085_…/Net.VdsAesCipherFactoryFactory/VdsAesCipherFactory.cs` `Create(uint tc)`. `tc` is the **TC of the panel's SYNC_REQUEST**. `aesKey` is the 8‑byte Freischaltcode; only `aesKey[0..7]` is used.
```python
def derive_key_9997(freischaltcode8: bytes, tc: int) -> bytes:
    t = tc.to_bytes(4, "big")              # t[0]=MSB … t[3]=LSB
    k = bytearray(16)
    k[0:8] = freischaltcode8[0:8]
    k[8]  = 0x74 ^ t[3]; k[9]  = 0x19 ^ t[2]; k[10] = 0x10 ^ t[1]; k[11] = 0x18 ^ t[0]
    k[12] = 0x10 ^ t[3]; k[13] = 0x07 ^ t[2]; k[14] = 0x19 ^ t[1]; k[15] = 0x56 ^ t[0]
    for i in range(4): k[i]      ^= t[i]
    for j in range(4): k[j + 4]  ^= k[j]
    for j in range(4): k[j + 8]  ^= k[j + 4]
    for j in range(4): k[j + 12] ^= k[j + 8]
    return bytes(k)
# sanity: derive_key_9997(bytes.fromhex("0123456789abcdef"), 0x12345678).hex()
#      == "1317131f9abcdef096f3fafafea2d7be"   (computed with this Python port, not with the app)
```
For any other key number (≠ 0): `AES key = ParseHex(AesSchluessel)`, used as is, with the same key for the whole session.

### 5.2 Encode / decode (pseudo‑Python, `cryptography` package)
```python
import os, struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def _aes(key, data, enc):
    c = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))
    x = c.encryptor() if enc else c.decryptor()
    return x.update(data) + x.finalize()

def encode_envelope(plain: bytes, key_no: int, key: bytes | None) -> bytes:
    if key_no:                                   # pad: min 256, multiple of 16
        n = len(plain); p = max(256 - n, (-n) % 16)
        plain = plain + bytes(p)                 # app uses os.urandom(p); zeros are safer (see §4)
    b = bytearray(plain); struct.pack_into(">H", b, 4, s2_checksum(plain))
    payload = _aes(key, bytes(b), True) if key_no else bytes(b)
    return struct.pack(">HH", key_no, len(payload)) + payload

async def read_envelope(reader):
    k, sl = struct.unpack(">HH", await reader.readexactly(4))
    if sl < 13: raise ProtocolError("SL<13")
    return k, await reader.readexactly(sl)

def decode_payload(k: int, payload: bytes, key: bytes | None) -> bytes:
    if k in (0, 0xFFFF):
        return payload
    p = _aes(key, payload, False)
    n = 17 + struct.unpack(">H", p[13:15])[0] if p[11] == 0x82 else 13 + p[12]
    s = p[:n]
    ok = s2_checksum(s) == struct.unpack(">H", s[4:6])[0] \
         or s2_checksum(p) == struct.unpack(">H", p[4:6])[0]
    if not ok: raise ChecksumError   # app: ignore telegram, wait for repeat
    return s
```

---

## 6. S2 procedure (session, counters, polling, ACK, retries)

Roles: **[F]** `S2ProtocolTelenotAsync` + `S2ProcedureSlave` ("ProcedureExpectsTelegramFromMaster", "TelegramReceivedFromMaster"). **App = slave, panel = master.** One TCP connection = one session. `S2ProtocolTelenotAsync` cannot be reused ("Protocol instance cannot be reused for multiple sessions").

### 6.1 Counters
**[F]** `StartSessionAsync`: `procedure.InitTransmissionCounter(randomNumberGenerator.GetUint())`. The own TC starts at a random u32.
**[F]** `CreateTelegram` (every telegram the app *originates*, except SERVICE_REQUEST and resends):
```csharp
TransmissionCounter++; ReceiveCounter++;
TC = TransmissionCounter; RC = ReceiveCounter;
```
**[F]** On SYNC_REQUEST: `ReceiveCounter = telegram.TC`. So the SYNC_RESPONSE carries `TC = rnd+1` and `RC = masterTC+1`.
**[F]** `VerifyCounters` for every later panel telegram (`T` = app's last sent TC, `R` = app's last sent RC):
* `rx.RC == T+1 && rx.TC == R` → **Accept**: a new telegram. The panel thereby implicitly ACKs our last telegram.
* `rx.RC == T && rx.TC == R-1` → **Resend**: the panel did not get our last telegram. The app resends `sentTelegram` unchanged (same TC/RC, re‑encrypted with new random padding). `repetitionCount++`.
* anything else (including `rx.RC == T+1` with a wrong TC) → **Ignore**: the app waits for the next telegram and sends nothing.
* During sync, in state SentSyncResponse, a *repeated* SYNC_REQUEST with `TC == R-1` → Resend the SYNC_RESPONSE. Otherwise a SYNC_REQUEST is accepted as new.

In short, RC carries "the TC I expect from you next". ACK is implicit via the counters; there is no separate ACK telegram.

**[F]** Retries: `MaxNumberOfRepetitions = 3`, or 2 during synchronisation. When `repetitionCount` reaches that limit after sending, the app throws `CommunicationProtocolException("The device has exceeded the permissible number of telegram repetitions.")` and ends the session.
**[I]** Retransmission is always driven by the master (panel). The slave never retransmits on its own timer. Its only timer is the 24 s function‑monitoring timeout.

### 6.2 Session start (exact sequence)
**[F]** `ConnectionWrapper.OpenPortAndProtocol`: `OpenAsync` → `protocol.StartSessionAsync()` → `protocol.SetPolltimeAsync(2000, 50)`.
**[F]** `S2ProtocolTelenotAsync.StartSessionAsync`: `procedure.StartSession()` (state Start) → receive → while `SessionSynchronizing`: `CreateAndSendTelegram()` then receive → `SessionEstablished` → spawn `HandleTelegrams` worker.

```
App (slave, TCP client)                         Panel (master)
  TCP connect host:port (≤15 s) ------------------>
  <-- K=0000 SL=000E | TC=m CRC RC=? IK=01 PK=01 L=01 | 01        SYNC_REQUEST (VdS frame, plaintext, window=1)
      (InitCipher(tc=m) → key; R:=m; check window: 1 ok, 255 = "remote access already in use", else error)
  --> K=key SL=0100 | AES{ TC=r+1 CRC RC=m+1 IK=02 PK=82 L=04 NL=0001 V=03 NK=01 | 01 | pad }   SYNC_RESPONSE
  <-- K=key | AES{ TC=m+1 RC=r+2 IK=03 PK=82 L=04 NL=0000 V=03 NK=01 }   POLL  → state Ready (session established)
        (or POLL with NK=FD and V<3 → app downgrades V and continues; see 6.5)
  --> DATA NK=05 (POLLTIME_SET) data = 07 D0 (2000 ms, u16 BE)   [only if V ≥ 2]
  <-- POLL NK=06 (POLLTIME_ACK)  (a plain POLL NK=01 in between is allowed → app answers POLL and keeps waiting)
  ... steady state ...
```
**[F]** POLLTIME_SET (`ChangeStateOnSend`: `data = BitConverter.GetBytes(Endianess.Toggle(pollTime.Value))`) = master poll interval in ms, big‑endian. The second value (`slavePolltime = 50`) is never sent. It is the app's local delay before answering (`WaitSlavePolltimeForNewJob`).
**[I]** After POLLTIME_ACK the panel polls about every 2000 ms. The 24 s function‑monitoring timeout gives a large safety margin.

### 6.3 Steady state (ping‑pong)
**[F]** `HandleTelegramsInSessionReadyState`:
* when the app may send (`CanSendTelegram`), it waits up to `polltimeSlave` = 50 ms for a job, then sends exactly one telegram:
  * queued Nutzdaten → **DATA** (IK=4, NK=1, data = one VdS Nutzdaten buffer ≤ 247 B)
  * else → **POLL** (IK=3, NK=1, no data)
* then it waits for the master's telegram. Any received **DATA** (IK=4, NK=1) is put onto the Nutzdaten RX queue and answered by the next telegram, which implicitly ACKs it.
**[F]** SERVICE_REQUEST (`CreateServiceRequestTelegram`, IK=7, NK=1, no data):
* sent when a new send job arrives while the app is waiting for the master's telegram, "um vom Master gepollt zu werden".
* uses the **current** TC/RC without incrementing.
* this lets the app push data without waiting a full 2 s poll period.
**[F]** Send‑job semantics: `SendNutzdatenAsync` completes as soon as the bytes are taken into an outgoing DATA telegram. There is no application‑level ACK wait.
**[F]** One DATA telegram carries one Nutzdaten blob. The app always sends exactly **one record** per DATA (`SerializeNutzerdatensatz()` → `SendNutzdatenAsync`). On RX, the app's `PollingOfIncomingBedienteilData` treats the whole blob as a single record (`array[1] == 0x70`). It only splits multiple records in legacy mode (`NutzdatenAnalyzer`).
**[F]** Raw transfers (NK 2/3/7/8 with RAW_ACK, 1024‑byte chunks) exist but are not used by BuildSec over this path.

### 6.4 Session end
**[F]** `ConnectionWrapper.Disconnect` → `protocol.EndSessionAsync()` (job). When it is the app's turn: `procedure.EndSession()` → state End → `ChangeStateOnSend` → **`CreateSessionCloseTelegram()` = IK=POLL(3), NK=0x09, no data** (only if V ≥ 3; for V < 3 nothing is sent). After sending: StopProcedure, wait 50 ms, `TcpClient.Close()`. The app does not wait for any answer.
**[F]** On a protocol exception (`CancelSessionOnException`), when it is the app's turn: `procedure.CancelSession()` → **IK=POLL, NK=0xFC ERROR_SESSION_CANCELLED** (V ≥ 3), then close.
**[F]** Error telegrams sent by the app (`CreateErrorTelegram`):

| Problem | Telegram sent |
|---|---|
| bad PK | IK=6, NK=1 |
| bad IK | IK=5, NK=1 |
| bad NK | IK=3, NK=0xFE |
| bad V | IK=3, NK=0xFD |
| frame/length errors, wrong IK/NK for the current state | SESSION_CANCELLED |

**[F]** There is no VdS "0xFF Verbindung nicht mehr benötigt" record and no application‑level logout record. The session is ended only by SESSION_CLOSE.

### 6.5 Version negotiation
**[F]** The app starts with V=3. During sync, a panel POLL with `NK=0xFD` and `1 ≤ V < 3` makes the app adopt that V (`TryToAdjustProtocolVersion`) and treat the POLL as "session ready". After sync, a V change is an error. V≥2 is needed for POLLTIME_SET; V≥3 for SESSION_CLOSE/CANCELLED and RAW.

### 6.6 Python state machine (slave)
```python
class S2Slave:
    async def run(self):
        k, p = await read_envelope(r)
        t = parse(decode_payload(k, p, None))              # plaintext, PK=1, IK=1
        assert t.ik == 1 and t.pk == 1
        if t.data[0] == 0xFF: raise Busy("remote access in use")
        if t.data[0] != 1:    raise ProtocolError("window")
        self.key = derive_key_9997(fc, t.tc) if key_no == 9997 else aes_key
        self.R, self.T = t.tc, secrets.randbits(32)
        await self.send_new(ik=2, nk=1, data=b"\x01")      # SYNC_RESPONSE
        t = await self.recv_verified()                     # expect POLL NK=1 (or NK=0xFD → downgrade V)
        await self.send_new(ik=4, nk=5, data=(2000).to_bytes(2, "big"))   # POLLTIME_SET
        while True:
            t = await self.recv_verified()                 # 24 s timeout
            if t.ik == 4 and t.nk == 1: self.rx_queue.put_nowait(t.data)
            if t.ik in (5, 6) or t.nk in (0xFC, 0xFD, 0xFE): raise RemoteError(t)
            await asyncio.sleep(0.05)                      # slave polltime (optional)
            if self.closing: await self.send_new(ik=3, nk=9); break
            data = self.tx_queue.get_nowait() if not self.tx_queue.empty() else b""
            await self.send_new(ik=4 if data else 3, nk=1, data=data)

    async def send_new(self, ik, nk, data=b""):
        self.T = (self.T + 1) & 0xFFFFFFFF; self.R = (self.R + 1) & 0xFFFFFFFF
        self.last = build_plain(self.T, self.R, ik, nk, data, self.V)
        await self._tx(self.last)

    async def recv_verified(self):
        while True:
            t = parse(decode_payload(*await read_envelope(r), self.key))
            if t.rc == (self.T + 1) & M and t.tc == self.R: return t        # accept
            if t.rc == self.T and t.tc == (self.R - 1) & M:                   # resend
                self.reps += 1; assert self.reps <= 3; await self._tx(self.last); continue
            # else ignore

def build_plain(tc, rc, ik, nk, data=b"", v=3):
    return struct.pack(">IHIBBBHBB", tc, 0, rc, ik, 0x82, 4, len(data), v, nk) + data
```
(Optional: while idle, send SERVICE_REQUEST `build_plain(self.T, self.R, 7, 1)` without incrementing, so an outgoing command goes out before the next 2 s poll.)

---

## 7. Nutzdaten (VdS 2465 "Nutzerdatensätze")

### 7.1 Generic record layout
**[F]** `040_…/Vds2465Nutzdaten/NutzdatenInfoAnalyzer.cs` `FindNutzerdatensatz`: `size = 2 + nutzdaten[offset]`. Records are concatenated back to back.
```
+-----+---------+--------------------------+
| SL  | Satztyp | SL bytes of record data  |      SL = number of bytes AFTER the Satztyp byte
+-----+---------+--------------------------+
```
`Satzdaten.GetUInt16/32` read big‑endian (`Endianess.Toggle`). Most records start with a `GeraetFrei` / `GeraetBereich` byte: high nibble = Gerät (0/1), low nibble = Bereich/Frei.

### 7.2 Record types implemented in the library (`Vds2465Nutzerdatensatz.CreateNutzerdatensatz`, `nutzerdatensatz[1]` dispatch)
Bold rows are the ones actually used by BuildSec on the IP path. The rest are implemented but **not referenced** outside the library (grep across 079/082/087 is empty). They are listed for completeness and for decoding panel‑initiated records.

| Satztyp | Class | Layout (after SL,Typ) | Len total |
|---|---|---|---|
| 0x00 | TelenotDatenFuerUe | Art, data… | 3..247 |
| 0x01 | Prioritaet | Abschwächungsgrad | 3 |
| 0x02 | MeldungMitQuittungsanforderung | GB, Adr, AdrZusatz, AdrErw, Meldungsart (VdS); Telenot variant SL=4: GB, Adr, AdrZusatz, Meldungsart | 7 (or 6) |
| 0x03 | Quittungsrücksendung | GB, Adr, AdrZusatz, AdrErw(low nibble 1/2), Meldungsart | 7 |
| 0x04 | MeldungOhneQuittungsanforderung | as 0x03 | 7 |
| 0x0F | MeldepunktIndex | u16 BE | 4 |
| 0x10 | Abfrage | GeraetFrei, Abfragetyp (SL=2), or for Abfragetyp ∈ {0x20,0x24,0x41}: GeraetFrei, Adr, AdrZusatz, AdrErw, Abfragetyp (SL=5) | 4 / 7 |
| 0x11 | Fehler | GeraetFrei, Fehlercode [, UnbekannterTyp if code=0xFF] | 4 / 5 |
| 0x20 | Status | GB, Adr, AdrZusatz, AdrErw(1/2/0x10), Meldungsart. Telenot variant SL=4 without AdrErw | 7 / 6 |
| 0x24 | Blockstatus | GB, Adr, AdrZusatz, AdrErw, bitmap… | 7..247 |
| 0x26 | BlockstatusAlles | GB, Adr, AdrZusatz, AdrErw, bitmap… | 7..247 |
| 0x30 | Mess/Zähl/Stellwerte | – | |
| 0x35 | GPS | – | |
| 0x40 | Testmeldung | (empty) | 2 |
| 0x41 | Testmeldungsquittung | [AdrErw,] WertLow, WertHigh | 2..5 |
| 0x42 | Sicherheitscode | WertLow, WertHigh | 4 |
| 0x44 / 0x48 | Info Meldungsweiterleitung / Verbindungsweg | – | |
| 0x50 | DatumUhrzeit | Jahr, Jahrhundert, Monat, Tag, Std, Min[, Sek] | 8 / 9 |
| 0x51 | HerstellerIdentifikation | bytes… | 2..32 |
| 0x52/0x53 | KommAdresse S2/S3 | – | |
| 0x54 | ASCII‑Zeichenfolge | ascii… | 2..247 |
| 0x55 | UnterstützteSatztypen | GeraetFrei, list of Satztyp bytes | 3..247 |
| 0x56 | Identifikationsnummer | ≤ 6 bytes, 12 BCD nibbles | 2..8 |
| 0x59 | Gerätemerkmale | GMA‑Nr, then {len, Typ, Index, Merkmal…}* | 3..247 |
| 0x60..0x63 | Netzstatus / Transportdienstkennung / Quelladresse / Schlüsselvergabe | – | |
| **0x70** | **DatenblockTransparent** | **GeraetFrei, Datenblock[SL‑1]** | 3..247 |
| 0x71 | LetzterDatenblockTransparent | GeraetFrei, Datenblock | 3..247 |
| 0x72 | DatenblockGültig | GeraetFrei | 3 |
| 0x73 | Telegrammzähler | GeraetFrei, Zähler | 4 |
| 0x74 | Container | GeraetFrei, Kennung, data… | 4..247 |
| 0xB3/0xB4/0xBB/0xBF | 10‑Baud from/to ÜE, Slotinfo, systeminterne Info | – | |
| 0xFF | VerbindungNichtMehrBenötigt | (empty) | 2 |

**[F]** For the 0x70 record (`Vds2465Nutzerdatensatz70HDatenblockTransparent.cs`): `Satzlaenge = datenblock.Length + 1`, `[1]=0x70`, `[2]=GeraetFrei`, then the datablock. Strict validation requires `GeraetFrei>>4 ∈ {0,1}` and low nibble 0. The app disables that check on RX (`validateGeraetFrei:false`).
**[F]** A datablock (`Datenblock.cs`) is `[Kennung][data…]`, 1..244 bytes for Telenot ST70 blocks.

### 7.3 Telenot ST70 datablocks used for session setup (`079_…/Datenbloecke.Satztyp70/`)
`EDatenblockKennungTelenot`:

| Kennung | Name |
|---|---|
| 0x18 | GTypAnfordern |
| 0x19 | GTypDaten |
| 0x23 | Fehlerkennung |
| 0x4B | FernVerbAbgebOk |
| 0x8A / 0x8B | Ferndiagnose einschalten / eingeschaltet, Betreiber, legacy |
| 0x8C / 0x8D | Ferndiagnose einschalten / eingeschaltet, Errichter, legacy |
| **0x92 / 0x93** | Ferndiagnose einschalten / eingeschaltet, **Betreiber** |
| **0x94 / 0x95** | Ferndiagnose einschalten / eingeschaltet, **Errichter** |
| 0x98 / 0x99 | getunnelte com2BUS‑Telegramme Anfrage / Antwort (→ other agent) |
| **0x9E / 0x9F** | BuildSec hipas Fernbedienung einschalten / eingeschaltet |

**Kennwort encoding** (`KennwortParser.ParseKennwort`, only the first 3 bytes are copied):
* digits are packed two per byte, **low nibble = 1st digit**
* unused nibbles are 0xF
```python
def kennwort(code: str) -> bytes:              # "123456" -> 21 43 65 ; "1234" -> 21 43 FF
    b = [0xFF] * 6; i = 0
    for i in range(len(code) // 2):
        b[i] = ((ord(code[2*i+1]) & 0xF) << 4) | (ord(code[2*i]) & 0xF)
    if len(code) & 1:
        i = len(code) // 2; b[i] = 0xF0 | (ord(code[-1]) & 0xF)
    return bytes(b[:3])
```

| Datablock | Bytes | Wrapped as record |
|---|---|---|
| Ferndiagnose einschalten (`DatenblockSt70Com2BusAnfrage`, `base(kennung, 4, kennwort, btadr)`) | `K, pw0, pw1, pw2, BT` (5 B) | `06 70 10 K pw0 pw1 pw2 BT` (GeraetFrei=**0x10**) |
| … over hiXserver (0x92/0x94) | `K, pw0..2, BT, sigLen, sig[sigLen]` (≥ 69 B) | GeraetFrei 0x10 |
| Eingeschaltet reply (0x93/0x95/0x9F) | `K, BT, TexteCrc_hi, TexteCrc_lo` (exactly 4 B, CRC u16 BE) | panel → `06 70 ?? K BT crc crc` |
| Legacy reply (0x8B/0x8D) | `K, BT, …` (≥ 2 B) | |
| GTyp anfordern | `18 pw0 pw1 pw2` | `05 70 00 18 pw0 pw1 pw2` (GeraetFrei=**0x00**) |
| GTyp Daten | `19, Gerätefamilie, major, minor, patch, …` (≥ 17 B) | |
| Fehler | `23, Grund` (exactly 2 B) | |

Example (Betreiber, code 123456, BT 15): app DATA Nutzdaten = `06 70 10 92 21 43 65 0F`.

`EFehlergrund` (Grund byte of the 0x23 error block):

| Code | Meaning |
|---|---|
| 0x10 | KennwortFalsch |
| 0x11 | ChecksummeFalsch |
| 0x13 | FalscheSatzlänge |
| 0x14 | UnbekannteKennung (→ app falls back to legacy 0x8A/0x8C) |
| 0x1B | FernServiceGesperrt (→ also legacy fallback) |
| 0x1D | UnzulässigeBtAdresse |
| 0x1E | Com2BusNichtFürEmzAktiviert |
| 0x1F | Com2BusStörungZurEmz |
| 0x20 | CodeGesperrt |
| 0x21 | FehlerFunktion |
| 0x22 | BedienebeneZuKlein |
| 0x23 | BedienteilBelegt |
| 0x24 | AutorisierungFehlgeschlagen |
| 0x25 | EingabeGesperrt |
| 0x26 | LanVerbindungNichtErlaubt |
| 0x27 | WanVerbindungNichtErlaubt |
| 0x28 | HixserverVerbindungNichtErlaubt |
| 0x29 | IpAdresseNichtBerechtigt |
| 0x30 | GesperrtDurchZeitModell |
| 0x99 | Unbekannt |

`EGeraetefamilie` (GTyp byte 1, `047_…/EGeraetefamilie.cs`) – e.g. 0xB0..0xB4 hiStar/ISG, 0xB9 hiplex, 0xF5 BT800, 0xF9 BT820, 0xFE mio54 … (full list in file).

**[F] Note for the keypad agent:** after login, `DeviceOperations.SendTastencode` sends **raw 1‑byte Nutzdaten** `[tastencode]` (`protocol.SendNutzdatenAsync(new byte[1]{tastencode})`), i.e. DATA NK=1 with a single byte and no record header.

---

## 8. Full app flow: connect, authenticate, disconnect

Sources: `079_…/DeviceCommunicator.cs` `StartFernservice` (l.1646), `ConnectionWrapper.cs`, `Communication/DeviceCommunication.cs`, `Communication/DeviceOperations.cs`.

1. `InitConnectionData` checks `HasAllValuesForDirectConnection()`.
2. `CreateS2ProtocolData` selects the key (§1.2).
3. `ConnectionWrapper.Connect(host, port)`:
   1. TCP connect (15 s)
   2. S2 sync (§6.2)
   3. `SetPolltimeAsync(2000, 50)` → POLLTIME_SET 0x07D0.
4. Start a background "Polling" task. It loops on `ReceiveNutzdatenAsync()` and dispatches incoming 0x70 records by datablock Kennung:
   * 0x93/0x95/0x9F/0x8B/0x8D → login result
   * 0x19 → GTyp
   * 0x23 → error (throws)
   * 0x99 → tunnelled com2BUS keypad data
   * 0x4B → log only.
5. `StartCommunication` by Ebene:
   * Betreiber → send `06 70 10 92 pw pw pw BT`
   * Errichter → `… 94 …`
   * Fernbedien → `… 9E …`
   * wait (no explicit timeout; bounded by the S2 timeouts) for the reply → `TexteCrc`.
   * If the panel answers with Fehler 0x14 or 0x1B (Ebene ≠ Fernbedien) → retry with legacy 0x8A/0x8C and expect 0x8B/0x8D.
6. Direct connection only: `InitConnection` → send GTyp request `05 70 00 18 pw pw pw` → wait for 0x19 (Gerätefamilie and firmware version are only logged).
7. State = Connected. The keypad emulation follows (texts via 0x98/0x99, key codes): see the keypad notes.
8. Disconnect (`StopFernservice` → `ConnectionWrapper.Disconnect`): `EndSessionAsync` → POLL/NK=0x09 SESSION_CLOSE → wait 50 ms → TCP close. There is no logout datablock.

**[F]** Satztyp **0x56 (Identnummer)** and **0x42 (Sicherheitscode)** are **not sent** by the app on this path. The 0x10/0x20/0x24/0x26/0x02/0x04/0x55/0x59 classes are also never used by BuildSec. **[I]** The panel may still push VdS Meldungen (0x02/0x04/0x20…) inside DATA telegrams. The app would ignore them: it only handles `array[1]==0x70`, or everything in legacy mode.

---

## 9. Unknowns / things to verify with a capture

1. **[?]** Which checksum interpretation the panel uses for encrypted RX (padded vs unpadded) – see §4. Zero padding sidesteps it on TX. On RX, accept either.
2. **[?]** Default TCP port of comXline / complex400 IP for "BuildSec/Fernservice". The app has none hardcoded (demo uses 8000).
3. **[?]** Exact VdS‑frame SYNC_REQUEST from the panel: RC value; does the panel ever send PK=0x82 SYNC_REQUEST (the app would reject it).
4. **[?]** Whether the panel tolerates a client that never sends POLLTIME_SET. The app always sends it when V ≥ 2.
5. **[?]** Meaning of `TexteCrc` (CRC over the keypad label texts. Presumably used to cache texts; other agent).
6. **[?]** Whether the panel sends unsolicited VdS 2465 alarm records (0x02/0x04/0x20) over this session. Not handled by the app.
7. **[?]** Behaviour with window size ≠ 1 (the app rejects it). 255 = "remote access already in use" (only one remote session at a time).
8. **[I]** The 10‑minute "no Nutzdaten" watchdog is app policy only. An HA integration that wants a persistent connection must keep exchanging data (e.g. periodic text/status requests) or reconnect. **[?]** whether the panel enforces its own idle limit.
9. **[?]** Whether multiple records in one DATA telegram are ever sent by the panel outside legacy mode (the app assumes one).

---

## 10. File index
- Framing / telegram: `040_Telenot.Communication/Telenot.Communication.Protocols/S2Telegram.cs`, `S2TelegramEncrypted.cs`, `S2TelegramReceiver.cs`, `EInformationsKennung.cs`, `ENutzdatenKennung.cs`, `EProtokollKennung.cs`, `EProtokollErweiterung.cs`
- Crypto / checksum: `…/Protocols/S2TelegramCipher.cs`, `047_Telenot.Infrastructure/Telenot.Infrastructure.Security/AesCipher.cs`, `…/VdsAesCipher.cs`, `047_…/Checksums/InternetChecksum.cs`, `047_…/EReservedAesKeyNumber.cs`, `085_…Droid/…Net.VdsAesCipherFactoryFactory/VdsAesCipherFactory.cs`
- Procedure: `…/Protocols/S2ProcedureSlave.cs`, `S2ProtocolTelenotAsync.cs`, `S2ProtocolJob*.cs`
- TCP: `040_…/Ports/IpPortClientAsync.cs`, `085_…/IpPortClientAsync/PortAsyncFactory.cs`, `085_…/Net.S2Protocol/S2ProtocolFactory.cs`
- Records: `040_…/Telenot.Communication.Vds2465Nutzdaten/*.cs`
- App session: `079_…/ConnectionWrappers/ConnectionWrapper.cs`, `079_…/Connection/ConnectionData*.cs`, `079_…/DeviceCommunicator.cs`, `079_…/Communication/DeviceCommunication.cs`, `DeviceOperations.cs`, `DeviceCheck.cs`, `079_…/Datenbloecke.Satztyp70/*.cs`
- Settings UI / model: `080_…/DirectConnectedDeviceData.cs`, `DeviceData.cs`, `EConnectionType.cs`, `EBerechtigungsEbene.cs`, `087_…/ViewModels/DeviceEditViewModel.cs`
- Unused here: `Com2BusProtocol/Com2BusTelegram` (serial/USB com2BUS framing with CRC16‑CCITT XMODEM), `S4Protocol`, `IpPort.cs` (sync).
