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

    if not isinstance(keys, dict):
        raise RuntimeError("SP_ENCRYPTION_KEYS must be a JSON object")

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


def _decode_hmac_key(key_id, value):
    try:
        key = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise RuntimeError(f"HMAC key {key_id!r} is not valid base64") from exc

    if len(key) < 32:
        raise RuntimeError(f"HMAC key {key_id!r} must be at least 32 bytes")

    return key


def _load_hmac_keys():
    raw = os.environ.get("SP_HMAC_KEYS")

    if raw:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SP_HMAC_KEYS must contain valid JSON") from exc

        if not isinstance(configured, dict) or not configured:
            raise RuntimeError("SP_HMAC_KEYS must be a non-empty JSON object")

        return {
            key_id: _decode_hmac_key(key_id, value)
            for key_id, value in configured.items()
        }

    # Backward compatibility with the existing single-key deployment.
    legacy = os.environ.get("SP_HMAC_KEY")
    if not legacy:
        raise RuntimeError("SP_HMAC_KEYS or SP_HMAC_KEY must be configured")

    return {"legacy": _decode_hmac_key("legacy", legacy)}


def _active_hmac_key():
    keys = _load_hmac_keys()

    if not os.environ.get("SP_HMAC_KEYS"):
        return keys["legacy"]

    active_id = os.environ.get("SP_ACTIVE_HMAC_KEY")
    if not active_id or active_id not in keys:
        raise RuntimeError("Active HMAC key is missing or invalid")

    return keys[active_id]


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


def _plate_digest(plate, key):
    normalized = normalize_plate(plate)
    return hmac.new(
        key,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def plate_hmac(plate):
    """Generate the digest used for new database writes."""
    return _plate_digest(plate, _active_hmac_key())


def plate_hmac_candidates(plate):
    """Generate lookup digests for all configured current and historical keys."""
    keys = _load_hmac_keys()
    candidates = []

    for key in keys.values():
        digest = _plate_digest(plate, key)
        if digest not in candidates:
            candidates.append(digest)

    return candidates


def json_hmac(data):
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hmac.new(
        _active_hmac_key(),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_json_hmac(data, expected):
    if not expected:
        return False

    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    for key in _load_hmac_keys().values():
        actual = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        if hmac.compare_digest(actual, expected):
            return True

    return False
