
import base64
import json
import os
import unittest
from unittest.mock import patch

from app.security import (
    encrypt_text,
    decrypt_text,
    plate_hmac,
    plate_hmac_candidates,
    normalize_plate,
)


class PlateSecurityTests(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "SP_ENCRYPTION_KEYS": json.dumps({
                "test-key": base64.b64encode(os.urandom(32)).decode()
            }),
            "SP_ACTIVE_ENCRYPTION_KEY": "test-key",
            "SP_HMAC_KEY": base64.b64encode(os.urandom(32)).decode(),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_encrypt_decrypt(self):
        plate = "TS09AB1234"
        encrypted = encrypt_text(plate)
        self.assertNotEqual(encrypted, plate)
        self.assertEqual(decrypt_text(encrypted), plate)

    def test_random_nonce(self):
        plate = "TS09AB1234"
        self.assertNotEqual(encrypt_text(plate), encrypt_text(plate))

    def test_tampered_ciphertext_rejected(self):
        encrypted = encrypt_text("TS09AB1234")
        parts = encrypted.split(":")
        parts[3] = parts[3][:-2] + "AA"
        with self.assertRaises(Exception):
            decrypt_text(":".join(parts))

    def test_normalization(self):
        self.assertEqual(
            normalize_plate(" ts 09 ab 1234 "),
            "TS09AB1234"
        )

    def test_hmac_consistency(self):
        self.assertEqual(
            plate_hmac("TS09AB1234"),
            plate_hmac(" ts 09 ab 1234 ")
        )

    def test_hmac_keyring_supports_historical_and_active_keys(self):
        plate = "TS09AB1234"
        old_key = base64.b64encode(os.urandom(32)).decode()
        current_key = base64.b64encode(os.urandom(32)).decode()

        with patch.dict(os.environ, {"SP_HMAC_KEY": old_key}, clear=True):
            old_digest = plate_hmac(plate)

        with patch.dict(os.environ, {"SP_HMAC_KEY": current_key}, clear=True):
            expected_current_digest = plate_hmac(plate)

        with patch.dict(os.environ, {
            "SP_HMAC_KEYS": json.dumps({
                "historical": old_key,
                "current": current_key,
            }),
            "SP_ACTIVE_HMAC_KEY": "current",
        }, clear=True):
            candidates = plate_hmac_candidates(plate)

            self.assertEqual(plate_hmac(plate), expected_current_digest)
            self.assertIn(old_digest, candidates)
            self.assertIn(expected_current_digest, candidates)
            self.assertEqual(len(candidates), 2)

    def test_empty_hmac_keyring_uses_legacy_key(self):
        legacy_key = base64.b64encode(os.urandom(32)).decode()

        with patch.dict(os.environ, {
            "SP_HMAC_KEYS": "",
            "SP_HMAC_KEY": legacy_key,
        }, clear=True):
            self.assertEqual(
                len(plate_hmac_candidates("TEST-PLATE")),
                1,
            )
            self.assertTrue(plate_hmac("TEST-PLATE"))

    def test_none_values(self):
        self.assertIsNone(encrypt_text(None))
        self.assertIsNone(decrypt_text(None))


if __name__ == "__main__":
    unittest.main()
