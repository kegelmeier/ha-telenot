# 03 – hiXserver / hiXC cloud path, TMP protocol, push notifications

Scope: how BuildSec reaches a panel through the hiXserver (classic) or hiXC (new cloud) instead of directly over IP.
Sources: `_re/src/041..046` (HiXServer libraries), `082/.../Domain.Logic.Hixserver`, `079` (DataTransfer),
`085`/`086` (platform providers), `088` (Android FCM service), APK `assets/`.

Legend: **[F]** = observed fact in code, **[I]** = inference, **[?]** = unknown.

---

## 0. TL;DR

- The cloud path does not use a JSON command API for the panel. **[F]** The hiXserver is used for three things:
  1. **Enrollment and identity:** a TLS line protocol called **TMP** ("TMP/1.0", SIP-like text messages) on `tmp.hixserver.de:9901/9909`. A one-time password is turned into an **ECDSA P-384 client certificate** issued by the TELENOT smart services PKI.
  2. **Discovery and brokering:** a TMP `Find` call for a device URI returns a **VdS proxy host:port**. The same call does an **ephemeral ECDH P-384 exchange** that yields a 16-byte session AES key.
  3. **Object list:** a small REST API (`/admin.php/api/v1/objects` or hiXC `/portal/api/user-device-properties`), authorised with a token fetched over TMP.
- **[F]** After `Find`, the app **closes TMP** and opens a **plain TCP** connection to `ProxyHost:ProxyPort`. It then speaks the **same VdS 2465 / S2 stream as in direct-IP mode**, with these settings:
  - AES key = Find session key.
  - Schlüsselnummer (key number) = the proxy port.

  The only extra step is a Satztyp-70 "Ferndiagnose einschalten … OverHixserver" block (Kennung `0x92`/`0x94`). It carries an **ECDSA-SHA384 signature made with the user's TMP client key**.
- **[F]** Push notifications reach the phone through **Firebase Cloud Messaging** (Firebase project `hixserver-push-service`). They are registered over TMP (`Update RegisterPush`). The message text is encrypted end-to-end with AES-256-CBC plus HMAC-SHA256, keyed by an ECDH secret.
- **[F]** Hosts are hard-coded: production, a Bamberg test IP, and four hiXC environments. There is **no setting for a custom or on-prem hiXserver host** in the app.

---

## 1. Hosts, ports, environments

`EHixserverEnvironment` (`083/.../EHixserverEnvironment.cs`) **[F]**:

| Enum | Value | TMP host (`TmpClientAccessory.Use*Environment`) | REST portal (`HixRestApiClient.CreateSingletonInstance`) |
|---|---|---|---|
| ProductiveEnv | 0 | `tmp.hixserver.de` | `https://hixserver.de/admin.php/api/v1/` |
| TestEnvBamberg | 1 | `<test host>` | `<test REST URL>` (uses `IgnoreSSLClientHandler`, so TLS is not verified) |
| HixcProductiveEnv | 2 | `ep.app.hixc.cloud` | `https://app.hixc.cloud/portal/api/` |
| HixcVdsEnv | 3 | `ep.vds.hixc.cloud` | `https://portal.vds.hixc.cloud/portal/api/` |
| HixcTestEnv | 4 | `ep.test.hixc.cloud` | `https://portal.test.hixc.cloud/portal/api/` |
| HixcDevEnv | 5 | `ep.dev.hixc.cloud` | `https://portal.dev.hixc.cloud/portal/api/` |

TMP ports (`046/.../Connection.cs`) **[F]**:
```csharp
public int Port { get; set; } = 9901;        // used when no valid client cert -> only REGISTER allowed
public int SecurePort { get; set; } = 9909;  // used when a valid client cert exists (mutual TLS)
```
**[F]** On Android, **both** sockets are `BouncyTlsSocket` (`085/.../TmpClient.cs` `Connect()`):
- 9901: TLS 1.2 with server authentication only.
- 9909: TLS 1.2 with mutual TLS.

Choosing the environment **[F]**: the app selects it from the stored user certificate (`TmpClientAccessory.SetHixEnvironment(uri)`, `HixServerHelper`):
- hiXserver test: the issuer DN contains `TEST-IssuingCA-V2`.
- hiXC: the issuer matches `hiXC( .+)? ECC CA 1`, plus the certificate extension OID `1.3.6.1.4.1.52898.2.1.2.2`. Its UTF-8 value is the "deployment environment" (`hiXC dev Environment`, `hiXC test Environment`, `hiXC vds Environment`, otherwise production).

---

## 2. Identities and URIs

`083/.../HixServerHelper.cs` **[F]**:
```csharp
HixserverUriRegex       = "\A.+@(?<errichterid>\d{6})(\.(?<kennung>[\x20-\x7E]+))?\.hixserver\Z";   // user URI
HixcUriRegex            = e-mail-like address that is not a hixserver URI                          // hiXC user URI
HixserverDeviceUriRegex = "\A[0-9]+@((?<errichterid>\d{6})|(?<kennung>[\x20-\x7E]+))\.hixserver\Z"; // panel URI
HixcDeviceUriRegex      = "\A[0-9]+\Z";
```
- User URI examples:
  - `name@123456.hixserver`, where `123456` is the installer (Errichter) ID.
  - `name@123456.<GlobalHubKennung>.hixserver`.
  - The UI-test user is `<ui-test user>`.
- Device URI: `<number>@<errichterid>.hixserver`. It comes from the object list (`Anlage.uri`) and is stored in `ConnectionData.DeviceUri`. It can also be typed in manually (`HixRestDeviceEditViewModel`).
- The client certificate CN is the user URI (lower-case).
- Storage on Android **[F]** (`085/.../BasicCertificateManager.cs`):
  - The certificate PEM is a file named `<uri>` in the app library folder.
  - The P-384 private key PEM is in `Plugin.SecureStorage` under service `com.TelenotElectronic.BuildSec.EccPrivateKey`, account `<uri>`.
- TMP `Device` header **[F]**:
  - Device ID = Android `Settings.Secure.ANDROID_ID`.
  - Device name = the phone name with whitespace replaced by `-`.
  - The server binds the certificate to the device ID. `Remove_Device` exists, and the lock state `ErrorUserDeviceLocked` exists.
- `UserAgent` **[F]** (`TmpClient.Connect`): `hixsec`. In "hiPas" application mode it is `hipas` or `hixsec-hipasmode`. The version is `AppConstants.AppVersion`.
  - Known agents (`UserAgents.cs`): `compasx`, `compasx-user`, `hipas`, `hipas-user`, `hixsec`.

---

## 3. TMP protocol ("TMP/1.0")

The library is `044_Telenot.HiXServer.TMP` (codec) plus `046_Telenot.HiXServer.TMP.Client` (session logic). The name "TMP" is never expanded in code **[?]**; it is plausibly "Telenot Message Protocol" **[I]**.

### 3.1 Framing **[F]** (`TmpCodec.cs`)
UTF-8 text, CRLF line endings, SIP/HTTP-like:
```
<Start-line>\r\n
<Header>:<value>\r\n        (no space after the colon when encoding; parser trims)
...
\r\n
[<payload>\r\n\r\n]         (only if a Payload header is present)
```
- Request start line: `<Keyword> <Feld> TMP/1.0`.
- Response start line: `<Code> <Feld> TMP/1.0`, for example `200 OK TMP/1.0`.
- Message end detection (`IsMessageComplete`): the buffer ends with `\r\n\r\n`, all headers parse, and the payload length matches `Payload: <type> <len>`.
  - For `unencrypted-information` payloads, `len` or `len+2` bytes are accepted.
- Headers are sent in insertion order. For a Request this is: `From`, `To`, `CSeq`, `Device`, `UserAgent`, then `Authorisation`/`Payload` if present.
- `Feld` is *not* set by `CreateRequest<T>()` for `Register`, `Find`, `Token`, `Renewal` or `Bye`. **[I]** That gives a start line with an empty middle token, e.g. `Find  TMP/1.0` (two spaces). The decoder accepts this because it splits on spaces and needs at least 3 parts. Verify on the wire.

Message registry (`ProtocolVersion.EnableVersion10`) **[F]**:
- Requests: `Ack`, `Bye`, `Cancel`, `Find`, `Register`, `Renewal`, `Update` (also KeepAlive = `Update PING`), `Token`, `SystemStructure`, `Notify`.
- Responses:
  - `200`, `202` → Ok (202 = "OK_Warning").
  - `301`, `302` → Redirect.
  - `400`, `401`, `402`, `423` → Error.
  - `500`, `501` → InternalError.
  - `503` → Busy.
- Headers: `From`, `To`, `CSeq`, `Authenticate`, `Authorisation`, `Payload`, `Relay`, `Renew`, `Device`, `UserAgent`.
- Payload types: `BLOB` (raw string) and `unencrypted-information` (`key:value\r\n` lines).

Header value formats **[F]**:
| Header | Format |
|---|---|
| From / To | `<uri>[ tag:<6 hex>]`. The `From` tag is random per request; it is removed for `Token`. |
| CSeq | `<seq> <Keyword>`. Always `1`, except Register/Renewal with Authorisation, which use `3`. |
| Device | `<deviceId>[ name:<deviceName>]` |
| UserAgent | `<agent>[ version:<ver>]` |
| Payload | `<type> <byteLength>` |
| Authenticate (server→client) | `type:certificate nonce:<hex>` |
| Authorisation | `type:certificate response:<Response-string> signature:<hex>` |
| Relay (server→client) | `ip:<host> port:<port> mode:Proxy priority:1 nonce:<...>` |

### 3.2 Session and connection flow **[F]**
`Connection.ConnectAsync()`:
1. If a valid stored certificate exists for the URI, connect over TLS to `host:9909` with that certificate. Otherwise connect to `host:9901`, where only `Register` is allowed.
2. On the secure socket, send `Update INIT TMP/1.0`:
   - Response `200` → OK.
   - Response `202` → OK_Warning. This sets `IsErrichterFirmaRestricted`.
   - Any other response: the `Feld` is a lock state (`ErrorAccountLocked`, `ErrorCustomerLocked`, `ErrorUserDeviceLocked`, …).
3. The management session targets `To: tmp.hixserver.de`, or the host name for hiXC. Other sessions use `To: <device-uri>`.
4. The server can send `Bye`, and the client then disconnects. Other unsolicited requests are ignored on Android.
5. Keep-alive is `Update PING`. The app runs in `ConnectionMode.Manual` and connects per operation. There is no persistent connection.

### 3.3 TLS details **[F]**
- `BouncyTlsSocket` uses TLS 1.2 only. Client auth uses the ECDSA certificate; for P-384 keys the hash is SHA-384.
- Server certificate validation (`CertificateValidator`, `TrustAnchorPinning`, `CrlRetriever`):
  - The trust anchor is `assets/Root_CA_Certificate.pem`: `CN=TELENOT smart services Root-CA V1, O=TELENOT smart services GmbH, C=DE`, P-384, SHA-1 fingerprint `10d71483309618e65d48c9d9eb1d3515b1e9ce2d`.
  - The SubjectPublicKeyInfo is **pinned** to a hard-coded base64 string.
  - A PKIX path is built, and **CRLs are fetched over HTTP from each certificate's CRL Distribution Point**.
- `BasicCertificateManager.IsCertificateValid()` checks the client's own certificate the same way. It downloads the issuing CA certificates via the AIA URL in the certificate.

### 3.4 Register (enrollment with OTP) **[F]** (`Session.RegisterAsync`, `ChallengeResponse`)
1. TLS to `:9901`. Send `Register` with no Feld, `From: <user-uri>`, `To: tmp.hixserver.de`.
2. The server answers `401` with `Authenticate: type:certificate nonce:<serverNonce>`.
3. The client generates an EC key pair on **P-384** (`GenerateKeys(384)`).
   - `pubB64` = base64 of the uncompressed point `04||X||Y`.
   - `clientNonce` = 20 random hex chars.
4. Response hash:
   ```
   pwHash   = SHA256_HEX_UPPER(otp + uri.lower())
   response = SHA256_HEX(pubB64 + serverNonce.lower() + clientNonce.lower() + pwHash.lower()).lower()
   Response-string = "Response:<response> User:<uri> Nonce:<clientNonce>"
   ```
5. Send a second `Register` with `CSeq: 3 Register`, `Payload: BLOB <len>`, and body = `pubB64`.
   - Header: `Authorisation: type:certificate response:Response:<h> User:<uri> Nonce:<cn> signature:<sig>`.
   - `sig` = lower-case hex of the DER **SHA384withECDSA** signature, made with the new key, over the UTF-8 string `"Authorisation:type:certificate response:Response:<h> User:<uri> Nonce:<cn>"`. This is everything before ` signature:`.
6. A `200`/`202` response carries a payload of **base64 DER X.509**, which is the issued client certificate. The client stores the certificate and private key.
   - Optional check: certificate extension `1.3.6.1.4.1.52898.2.1.2.1` must equal the device ID. This only applies where the device info implements `ITmpServerTypeSpecificDeviceInformation`. The Android `DeviceInfo` does not, so the check is skipped.
   - `.2.2` = the hiXC deployment environment.

The OTP and user URI are issued by the installer or the hiXserver portal **[I]** (the UI asks for both).

### 3.5 Renewal **[F]**
- Runs on the secure connection. The request is `Renewal`, with body `BLOB` = new P-384 public key (base64). No challenge.
- The response is a base64 DER certificate. The old certificate is replaced.
- The client renews when `now > NotAfter − 42 days` and `now < NotAfter` (`CertificateRenewalCheck`). An expired certificate makes `Connect` throw.

### 3.6 Token (for the REST API) **[F]**
- `Token` on the secure connection (From without tag).
- Response payload `unencrypted-information` containing `token:<value>`.

### 3.7 Find (tunnel brokering) **[F]** (`Session.FindAsync`, `HixCommunication.HixConnect`)
1. On the secure connection, create a session with `To: <device-uri>`. Send `Find` with an `unencrypted-information` payload:
   ```
   pubkey:<urlencode(base64(04||X||Y of ephemeral P-384 key))>
   nonce:<20 hex>
   ```
2. A successful response (`200`/`202`) carries:
   - `Relay: ip:<proxyHost> port:<proxyPort> mode:Proxy ...`.
   - A payload that is either:
     - `BLOB`: base64 of `"nonce:<same nonce>\r\npubkey:<b64 raw point>"` (seen on hiXC), or
     - `unencrypted-information` with `nonce` and `pubkey` (url-encoded).
3. The nonce must match. Then:
   ```
   Z          = ECDH(ephemeralPriv, peerPub)  (x-coordinate, left-padded to 48 bytes)
   sessionKey = HEX_UPPER( SHA256(Z)[0:16] )      -> 128-bit AES key
   ```
4. On failure, `Feld` = `ERROR_<n>`. The codes are in `ETmpErrorCode`:

   | Code | Name |
   |---|---|
   | 0 | Ok |
   | 1 | NotFound |
   | 2 | Offline |
   | 3 | NotAllowed |
   | 4 | ProxyError |
   | 5 | NoNonce |
   | 6 | NoObject |
   | 7 | NoPerson |
   | 8 | AccountLocked |
   | 9 | Generic |
   | 10 | Occupied |
   | 11 | DeviceLocked |
   | 12 | EMALocked |
   | 13 | ServiceNotAllowed |
   | 14 | ServiceNotFound |
5. The app then **disconnects TMP** (`HixCommunication.HixConnect`, finally block).

**[I]** The ECDH peer is the **panel side** (comXline / transmission device or the panel itself), not the server, so the session key is end-to-end. The server only relays public keys and allocates a VdS-proxy tunnel. The contract models support this reading: `VdsProxy {host,port,maxTunnels,activeTunnels}`, `Tunnel {host,port,basePort,partner1,partner2}`, `TmpEndpoint`.

### 3.8 What runs inside the tunnel **[F]**: the same VdS 2465 / S2 stream as direct IP
`079/.../ConnectionDataHelper.InitConnectionData` (state machine):
```csharp
connectionData.HostName        = result.ProxyHost;
connectionData.Port            = result.ProxyPort;
connectionData.AesSchluessel   = result.SessionKey;                 // 16-byte hex
connectionData.Schluesselnummer = result.ProxyPort.ToString(...);    // key number = proxy port
connectionData.ConnectionSignatureData = hixCommunication.GenerateConnectionSignature(connectionData);
```
- `ConnectionWrapper.CreateS2ProtocolData` builds `vdsAesCipherFactoryFactory.Create(ushort.Parse(Schluesselnummer), ParseHex(AesSchluessel))`.
- `ConnectionWrapper.Connect` uses the **same `IPortAsyncFactory`** (plain TCP) as direct mode. It then runs `StartSessionAsync()` and `SetPolltimeAsync(2000, 50)`.
- After the S2 session is up, `DeviceCommunication.StartCommunication` differs from direct mode only in this step:
  - Direct: `StartFernserviceBetreiber` / `StartFernserviceErrichter`.
  - hiX: `StartFernservice{Betreiber|Errichter}OverHixserver(bedienteilCode, btAdr, signature)`.

  This sends a Satztyp 70 transparent block (`BuildSecVds2465NutzerdatensatzSt70DatenblockTransparent(16, …)`):
  ```
  DatenblockSt70FerndiagnoseEinschaltenBetreiberOverHixserver : base(146 /*0x92*/, 5+sigLen, kennwort, btadr)
  DatenblockSt70FerndiagnoseEinschaltenErrichterOverHixserver : base(148 /*0x94*/, 5+sigLen, kennwort, btadr)
  bytes: [0]=Kennung, [1..3]=Kennwort (KennwortParser, ≤6 digits), [4]=Bedienteiladresse, [5]=sigLen, [6..]=signature
  ```
- The signature comes from `HixCommunication.GenerateConnectionSignature`. It is SHA384withECDSA (DER), made with the **user's TMP client private key**:
  - Betreiber (operator): signed data = `UTF8(userUri) || hex2bin(AesSchluessel) || hex2bin(Freischaltcode)`.
  - Errichter (installer): signed data = `UTF8(userUri) || hex2bin(AesSchluessel)`.
- The reply is `DatenblockSt70FerndiagnoseEingeschaltet{Betreiber|Errichter}`, which carries `TexteCrc`. From here the session behaves like a direct session.
- **[I]** The panel or transmission device checks this signature against the user certificate it gets from the hiXserver PKI. That is why the "remote service" mode is bound to a registered user.

---

## 4. REST API

### 4.1 hiXserver (classic) **[F]** (`042/043`, `HixRestApiClient`)
- Base URL: `https://hixserver.de/admin.php/api/v1/`.
- Default headers:
  - `Accept: application/json`
  - `X-Application: <hard-coded key, omitted>` (hard-coded app API key)
  - `X-Token: <token from TMP Token request>`
- The app makes **one call only**: `GET objects?userAgent=hixsec`. It returns `List<Objekt>` (Newtonsoft, field names as below).
  ```jsonc
  [ { "id": 1, "description": "...",
      "alarmSystems": [ { "id": 1, "barcode": "...", "description": "...", "uri": "1234@123456.hixserver",
                          "state": "0|1|2", "type": 3, "lastSeen": "...", "property": "...", "propertyId": 1,
                          "isRemoteAccessLocked": false,
                          "moduls": [ { "id": "...", "description": "...", "state": 2, "type": "...", "uri": "...", "tmpendpoint": true } ] } ] } ]
  ```
  `state` values: 0 = registrationPending, 1 = offline, 2 = online.
- The generic client (`NostromoService`) also offers the following. The app never calls these on this path; they look like admin/back-end use **[I]**:
  - `POST login` (form `user`, `pass`), which returns `{Id, Result, Token}`.
  - `GET logout`, `GET healthcheck`.
  - `Resource<T>`: `GET name`, `GET name?id=`, `GET name?dummy=1&k=v`, `POST name` (JSON), `DELETE name?id=`.
  - Resource `certificates`, which returns `Zertifikat {certificate, commonName, deviceId, deviceName, fingerprint, id, serialnumber, validUntil}`.

### 4.2 hiXC **[F]**
- Base URL: `https://<portalHost>/portal/api/`.
- Header: `Authorization: Bearer <TMP token>`.
- `GET user-device-properties` returns `{ properties: [ { id, name, systemDevices: [ { id, serialNumber, tmpUri, deviceType("transmissionDevice"|"intruderAlarmControlPanel"), name, connectionState("registrationPending"|"offline"|"online"), connectionStateChangedAt, isRemoteAccessLocked, proxiedSystemDevices:[{id, serialNumber, tmpUri, canUseTmp, deviceType, name, connectionState, connectionStateChangedAt}] } ] } ] }`.
- The JSON is camelCase **[I]**: Newtonsoft binds case-insensitively, so the exact casing is not visible in the code.
- The client maps this to the legacy `Objekt` / `Anlage` / `Geraet` models.

### 4.3 There is no username/password login for end users
**[F]** The app never calls `LoginAsync` (`InitAsync(false)`). All user authentication works like this:
- Client certificate (mutual TLS on 9909), then a TMP `Token`, then the REST call.
- The only "password" is the one-time registration code.

---

## 5. Push notifications

### 5.1 Registration (TMP, on the secure connection) **[F]** (`Session.CreateRegisterPushRequest`, `TmpClient.RegisterPushAsync`, `PushRegistrations`)
```
Update RegisterPush TMP/1.0
... Payload: unencrypted-information <len>

PublicKey:<urlencode(base64(04||X||Y of a fresh P-384 key))>
nonce:<20 hex>
Device-Token:<FCM registration token>
Plattform:Android|iOS
```
- On OK, the response payload contains `pubKey:<b64>`. On error it contains `message:<text>`.
- Push key:
  ```
  peer = b64decode(pubKey); if hiXserver: peer = peer[23:23+97]  (strip P-384 SPKI header); hiXC: raw point
  pushKey = HEX(SHA256(ECDH_x_padded48)).lower()     -> 64-char lowercase hex string
  ```
  The app stores it in SecureStorage (`com.TelenotElectronic.BuildSec.PushDecryptionKey`, account = user URI).
- The ephemeral key pair lives only in memory during the call.
- Unregister: `Update UnregisterPush` with `nonce` and `Device-Token`.
  - **[F] quirk:** the domain layer passes the *platform* string as the device token (`UnregisterPushAsync(GetCurrentPushPlatform())`).
- Remove device: `Update Remove_Device` with payload `Device-ID:<androidId>`.

### 5.2 Delivery and payload **[F]** (`088/.../MyFirebaseMessagingService.cs`, `085/.../PushCryptoProvider.cs`)
- Transport: FCM data message. Firebase project `hixserver-push-service` (bucket string `hixserver-push-service.appspot.com` in `resources.arsc`).
- Data keys:

  | Key | Meaning |
  |---|---|
  | `ema` | panel name |
  | `message` | encrypted text |
  | `useruri` | recipient user URI |
  | `emauri` | panel URI |
  | `timestamp` | |
  | `meldungsart` | hex string, parsed as `Convert.ToInt32(x,16)` |
- Decrypting `message`:
  ```
  raw = b64decode(message); iv = raw[0:16]; mac = raw[16:48]; ct = raw[48:]
  check HMAC-SHA256(key = UTF8(pushKey 64-char hex), data = iv||ct) == mac
  AES-256-CBC/PKCS7, key = UTF8(pushKey)[0:32]  (i.e. the first 32 hex characters as ASCII bytes), iv
  ```
- `meldungsart` (`EVdsMeldungsart`):

  | Value | Meaning |
  |---|---|
  | 33 (0x21) | Überfall (hold-up), shown as a silent alarm notification |
  | 97 (0x61) | ExternScharf (externally armed) |
  | 98 (0x62) | InternScharf (internally armed) |
  | 225 (0xE1) | ExternUnscharf (externally disarmed) |
  | 226 (0xE2) | InternUnscharf (internally disarmed) |
  | anything else | "AlarmeStoerungen" (alarms/faults) |

  These map well to HA events and alarm states.
- The message carries no command channel. It is notification-only.

---

## 6. Connection modes and the "local hiXserver" question

`EConnectionType` (`080`) **[F]**:

| Value | Name | How it connects |
|---|---|---|
| 0 | DirectConnected | TCP to the panel or comXline IP:port, VdS 2465 S2. AES from `Schluesselnummer` + `AesSchluessel` (Errichter/Fernbedien). Betreiber legacy mode: key number **9997** + `Freischaltcode` as key (`ConnectionWrapper.CreateS2ProtocolData`). |
| 1 | HixServerRestApi | The path described above (tmp.hixserver.de). |
| 2 | HixCRestApi | Same as 1, with hiXC hosts and Bearer REST. |

`EBerechtigungsEbene` (permission level): `ErrichterMode=0`, `BetreiberMode=1`, `FernbedienMode=2`. Fernbedien (remote control) mode exists **only for direct connections**. It uses `StartFernbedienMode` and has no "OverHixserver" variant.

- **[F]** The app **does talk to the panel directly over LAN/IP** (DirectConnected). The hiX path is optional.
- **[F]** There is **no UI or setting for a custom hiXserver or TMP host**. All hosts are constants.
  - `TmpEnvironment.ManagementTmpUri` is settable in the library, but BuildSec never sets it.
- **[I]** A self-hosted or on-prem hiXserver is therefore not supported by this app. It would also fail certificate pinning to the TELENOT smart services Root-CA, and it would need the Telenot PKI to issue certificates. Whether Telenot sells an on-prem hiXserver is outside the app code **[?]**.

---

## 7. Assets (describe only; no key material copied)

| File | Role |
|---|---|
| `Root_CA_Certificate.pem` | TLS trust anchor for TMP. TELENOT smart services Root-CA V1 (P-384, valid 2019–2038). Its fingerprint equals `BasicCertificateManager.DefaultRootCaThumbprint`. |
| `ManagementCA.cacert.Attack.pem` | RSA-2048 "CN=ManagementCA, O=TELENOT Sample". Not referenced by code; probably a negative test fixture for pinning **[I]**. |
| `uitestuser.pfx` | PKCS#12 **with a private key** for `<ui-test user>` (hiXserver). Loaded only by the UI-test backdoor `MainActivity.PrepareBuildSecForUITestWithHixserver` with a hard-coded password. **Do not use**: it is Telenot's test credential. Its key and password were not copied here. |
| `BasicPkiProvider` (045) | Server/test code, unused in the app. Refers to `rootCA.pfx` with a password and to `management@telenot.de.cert.pem` / `telenot.new-frontiers.de.cert.pem` ("Fraunhofer Dummy"). This suggests the TMP library was co-developed with Fraunhofer and the "new frontiers" vendor **[I]**. |

---

## 8. Feasibility for a Python HA integration via hiXserver

What you would need to implement:
1. **TMP client** (~300 LOC): asyncio + `ssl` (TLS 1.2), the text codec above, and the Register / INIT / Token / Find / Renewal / Bye / Update flows.
   - Crypto with `cryptography`: P-384 ECDH, SHA384-ECDSA (DER), SHA-256.
   - Server validation: pin the Root CA (load `Root_CA_Certificate.pem` as the only CA) and set the hostname. CRL checking is optional.
2. **Enrollment:** the user needs a hiXserver **user URI + one-time registration code** from the installer or portal.
   - The integration generates its own P-384 key and does `Register` as a new "device". It needs a stable fake `Device` ID / name and `UserAgent: hixsec version:<x>`.
   - It stores the returned certificate and key, renews 42 days before expiry, and handles `Remove_Device` on uninstall.
   - **Blocker risk:** one URI may be bound to one device. Registering HA could use up or replace the phone's registration, or need a second user URI **[?]**.
3. **Per connection:**
   - INIT on 9909.
   - `Find <device-uri>`, giving proxy host:port + session key.
   - Bye / close TMP.
   - TCP to the proxy, then the **existing VdS 2465 S2 implementation** (from the direct-IP work) with key number = proxy port and AES key = session key.
   - Send St70 `0x92`/`0x94` with the Bedienteil code, Bedienteil address and the ECDSA signature.
   - Continue as in direct mode.
   - **Not available over hiX: FernbedienMode.** It is unclear whether arm/disarm and state polling work in Betreiber-Fernservice mode **[?]**. Check against the direct-mode notes.
4. **Object list (optional):** Token, then `GET https://hixserver.de/admin.php/api/v1/objects?userAgent=hixsec` with `X-Application` + `X-Token`. This gives device URIs and online state; a user could also enter the device URI by hand.
5. **Push (optional, hard):**
   - Receiving FCM for Telenot's Firebase project needs an unofficial FCM/GCM client (as `push_receiver` / `firebase-messaging` do), using the sender ID / app ID / API key from `resources.arsc`.
   - Then `Update RegisterPush` with that token and AES/HMAC decryption.
   - Fragile, and it likely conflicts with Google/Telenot terms. Polling over a VdS session, or the direct-IP path, is simpler.

Blockers and risks:
- Each user/device enrollment needs a Telenot-issued registration code. Telenot can revoke or lock it (`ErrorUserDeviceLocked`, `ErrorCustomerLocked`).
- Every session goes through the cloud and opens a "Fernservice" session on the panel. That may show on the panel, occupy it (`ErrorOccupied`), or need the Bedienteil (keypad) code and Freischaltcode. Not well suited to frequent polling **[I]**.
- `Find` latency is several seconds (the app's UI timeouts allow up to ~75 progress steps).
- Wire details are not yet confirmed. Capture a real session to verify:
  - empty-Feld start lines,
  - header order,
  - whether the `Plattform` / `Device-Token` keys are case-sensitive.
- If the panel is reachable on LAN, **DirectConnected is far simpler and does not depend on the cloud**. Recommend hiX only as a fallback.

---

## 9. Unknowns / to verify
- Exact on-wire bytes (start-line spacing, CRLF after payload). Get a TLS-decrypted capture via Frida on `BouncyTlsSocket.SendAsync`, or via app logs: `Sending:` / `Received:` are logged at LogLevel 5.
- Whether the ECDH peer in `Find` is the panel/transmission device (end-to-end) or the server.
- How the panel validates the St70 signature (does it fetch the user certificate from hiXserver?).
- Whether several devices (phone + HA) can hold certificates for the same user URI at once.
- Whether arm/disarm commands are allowed in Betreiber-Fernservice over hiX (see the VdS notes).
- The exact hiXC JSON casing, and the `Relay` nonce semantics.
- Server-side messages `Notify`, `SystemStructure`, `Ack`, `Cancel` exist in the codec (`IHiXManagement` is the server interface). Their use by clients is unknown.
