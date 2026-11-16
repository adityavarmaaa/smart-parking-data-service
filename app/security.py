
import base64
import hashlib
import hmac
import json
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _load_keys():
    raw = os.environ.get("SP_ENCRYPTION_KEYS")
    if not raw:
        raise RuntimeError("SP_ENCRYPTION_KEYS is not configured")

    try:
        keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SP_ENCRYPTION_KEYS must contain valid JSON") from exc

    decoded = {}
    for key_id, value in keys.items():
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise RuntimeError(f"Encryption key {key_id!r} must be 32 bytes")
        decoded[key_id] = key

    return decoded


def _active_key():
    key_id = os.environ.get("SP_ACTIVE_ENCRYPTION_KEY")
    keys = _load_keys()

    if not key_id or key_id not in keys:
        raise RuntimeError("Active encryption key is missing or invalid")

    return key_id, keys[key_id]


def _hmac_key():
    raw = os.environ.get("SP_HMAC_KEY")
    if not raw:
        raise RuntimeError("SP_HMAC_KEY is not configured")

    key = base64.b64decode(raw, validate=True)
    if len(key) < 32:
        raise RuntimeError("SP_HMAC_KEY must be at least 32 bytes")

    return key


def normalize_plate(plate):
    if not isinstance(plate, str):
        raise ValueError("Plate must be a string")

    return re.sub(r"\s+", "", plate).upper()


def encrypt_text(value):
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("Value must be a string")

    key_id, key = _active_key()
    nonce = os.urandom(12)
    aad = f"smart-parking:v1:{key_id}".encode("utf-8")

    encrypted = AESGCM(key).encrypt(
        nonce,
        value.encode("utf-8"),
        aad,
    )

    payload = base64.b64encode(nonce + encrypted).decode("ascii")
    return f"enc:v1:{key_id}:{payload}"


def decrypt_text(value):
    if value is None:
        return None

    if not isinstance(value, str) or not value.startswith("enc:v1:"):
        raise ValueError("Value is not a supported encrypted value")

    parts = value.split(":", 3)
    if len(parts) != 4:
        raise ValueError("Invalid encrypted value format")

    _, version, key_id, payload = parts

    if version != "v1":
        raise ValueError("Unsupported encryption version")

    keys = _load_keys()
    if key_id not in keys:
        raise RuntimeError(f"Encryption key {key_id!r} is unavailable")

    raw = base64.b64decode(payload, validate=True)
    if len(raw) < 29:
        raise ValueError("Encrypted payload is too short")

    nonce = raw[:12]
    ciphertext = raw[12:]
    aad = f"smart-parking:v1:{key_id}".encode("utf-8")

    plaintext = AESGCM(keys[key_id]).decrypt(
        nonce,
        ciphertext,
        aad,
    )

    return plaintext.decode("utf-8")


def plate_hmac(plate):
    normalized = normalize_plate(plate)
    digest = hmac.new(
        _hmac_key(),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return digest


def json_hmac(data):
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hmac.new(
        _hmac_key(),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_json_hmac(data, expected):
    if not expected:
        return False

    actual = json_hmac(data)
    return hmac.compare_digest(actual, expected)
