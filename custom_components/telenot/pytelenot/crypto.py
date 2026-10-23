"""Checksum, key derivation and AES envelope of the Telenot S2 transport.

Reimplemented from the BuildSec app (Telenot.Communication S2TelegramCipher,
Telenot.Infrastructure InternetChecksum/AesCipher, VdsAesCipherFactory).
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_NUMBER_PLAIN = 0
KEY_NUMBER_UNKNOWN = 0xFFFF
KEY_NUMBER_BETREIBER = 9997  # "ComvisMci": key derived from the Freischaltcode
RESERVED_KEY_NUMBERS = frozenset({0, 0xFFFF, *range(9990, 10000)})

MIN_ENCRYPTED_LENGTH = 256
_ZERO_IV = bytes(16)


def internet_checksum(buf: bytes) -> int:
    """RFC 1071 checksum over big-endian 16-bit words."""
    if len(buf) % 2:
        buf += b"\x00"
    total = sum(struct.unpack(f">{len(buf) // 2}H", buf))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def s2_checksum(telegram: bytes) -> int:
    """Checksum of an S2 telegram, computed with the CRC field (bytes 4..5) zeroed."""
    return internet_checksum(telegram[:4] + b"\x00\x00" + telegram[6:])


def derive_betreiber_key(freischaltcode: bytes, tc: int) -> bytes:
    """Per-session AES key for key number 9997 (operator access).

    ``tc`` is the transmission counter of the panel's SYNC_REQUEST.
    """
    t = tc.to_bytes(4, "big")
    k = bytearray(16)
    k[0:8] = freischaltcode[0:8]
    k[8] = 0x74 ^ t[3]
    k[9] = 0x19 ^ t[2]
    k[10] = 0x10 ^ t[1]
    k[11] = 0x18 ^ t[0]
    k[12] = 0x10 ^ t[3]
    k[13] = 0x07 ^ t[2]
    k[14] = 0x19 ^ t[1]
    k[15] = 0x56 ^ t[0]
    for i in range(4):
        k[i] ^= t[i]
    for base in (4, 8, 12):
        for j in range(4):
            k[base + j] ^= k[base - 4 + j]
    return bytes(k)


def _aes(key: bytes, data: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(_ZERO_IV))
    ctx = cipher.encryptor() if encrypt else cipher.decryptor()
    return ctx.update(data) + ctx.finalize()


def telegram_length(plain: bytes) -> int:
    """Length of an S2 telegram without padding, derived from its header."""
    if plain[11] == 0x82:
        return 17 + struct.unpack_from(">H", plain, 13)[0]
    return 13 + plain[12]


def seal(telegram: bytes, key_number: int, key: bytes | None) -> bytes:
    """Pad, checksum and encrypt a plaintext S2 telegram; return the TCP envelope.

    The app pads with random bytes and checksums the padded buffer; we pad with
    zeros so the padded and unpadded checksums are identical (the app's receiver
    checks the unpadded telegram).
    """
    if key_number != KEY_NUMBER_PLAIN:
        n = len(telegram)
        telegram += bytes(max(MIN_ENCRYPTED_LENGTH - n, -n % 16))
    buf = bytearray(telegram)
    struct.pack_into(">H", buf, 4, s2_checksum(bytes(buf)))
    payload = (
        bytes(buf) if key_number == KEY_NUMBER_PLAIN else _aes(key or b"", bytes(buf), encrypt=True)
    )
    return struct.pack(">HH", key_number, len(payload)) + payload


def unseal(key_number: int, payload: bytes, key: bytes | None) -> bytes | None:
    """Decrypt an envelope payload; return the unpadded telegram or None on bad checksum."""
    plain = payload if key_number == KEY_NUMBER_PLAIN else _aes(key or b"", payload, False)
    if len(plain) < 13:
        return None
    try:
        length = telegram_length(plain)
    except struct.error:
        return None
    if length > len(plain):
        return None
    telegram = plain[:length]
    stored = struct.unpack_from(">H", plain, 4)[0]
    if s2_checksum(telegram) == stored or s2_checksum(plain) == stored:
        return telegram
    return None
