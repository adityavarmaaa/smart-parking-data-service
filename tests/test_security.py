
import base64
import json
import os
import unittest
from unittest.mock import patch

from app.security import (
    encrypt_text,
    decrypt_text,
    plate_hmac,
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

    def test_none_values(self):
        self.assertIsNone(encrypt_text(None))
        self.assertIsNone(decrypt_text(None))


if __name__ == "__main__":
    unittest.main()
