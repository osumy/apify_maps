import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure root directory is on sys.path for direct script execution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from key_manager import (
    ApifyKeyManager,
    KeyStatus,
    mask_token,
    load_tokens_from_env,
    AllKeysExhaustedError,
)
from apify_client.errors import ApifyApiError


def make_mock_apify_error(status_code: int, err_type: str, message: str) -> ApifyApiError:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"error": {"type": err_type, "message": message}}
    resp.text = message
    return ApifyApiError(resp, 1)


class TestApifyKeyManager(unittest.TestCase):

    def test_mask_token(self):
        token = "apify_api_1234567890abcdefghijklmnopqrstuvwxyz"
        masked = mask_token(token)
        self.assertTrue(masked.startswith("apify_api_12345"))
        self.assertTrue(masked.endswith("uvwxyz"))
        self.assertIn("...", masked)
        self.assertNotIn("abcdefgh", masked)

        # Short tokens
        self.assertEqual(mask_token("short"), "short...")
        self.assertEqual(mask_token(""), "")

    def test_load_tokens_from_env(self):
        with patch.dict("os.environ", {
            "APIFY_TOKENS": "token_a, token_b; token_c\ntoken_d",
            "APIFY_TOKEN": "token_e, token_a"
        }):
            tokens = load_tokens_from_env()
            self.assertIn("token_a", tokens)
            self.assertIn("token_b", tokens)
            self.assertIn("token_c", tokens)
            self.assertIn("token_d", tokens)
            self.assertIn("token_e", tokens)
            # Check deduplication
            self.assertEqual(len(tokens), 5)

    def test_rotation_error_detection(self):
        manager = ApifyKeyManager(tokens=["token_dummy_1"])

        # Unauthorized
        auth_err = make_mock_apify_error(401, "user-or-token-not-found", "Token not valid")
        is_rot, reason = manager.is_rotation_error(auth_err)
        self.assertTrue(is_rot)

        # Quota limit
        api_err = make_mock_apify_error(403, "monthly-usage-limit-exceeded", "Monthly usage limit exceeded")
        is_rot, reason = manager.is_rotation_error(api_err)
        self.assertTrue(is_rot)

        # Rate limit
        rate_err = make_mock_apify_error(429, "rate-limit-exceeded", "Rate limit exceeded")
        is_rot, reason = manager.is_rotation_error(rate_err)
        self.assertTrue(is_rot)

        # Keyword in string
        generic_err = Exception("Payment required for compass/crawler-google-places")
        is_rot, reason = manager.is_rotation_error(generic_err)
        self.assertTrue(is_rot)

        # Random application error (should NOT rotate)
        rand_err = ValueError("Invalid json payload")
        is_rot, reason = manager.is_rotation_error(rand_err)
        self.assertFalse(is_rot)

    def test_sequential_rotation_and_failover(self):
        tokens = ["token_1", "token_2", "token_3"]
        manager = ApifyKeyManager(tokens=tokens, strategy="sequential")

        manager._key_cache["token_1"] = KeyStatus(
            token="token_1",
            masked_token=mask_token("token_1"),
            is_valid=True,
            is_exhausted=True,
            remaining_usd=0.0,
            status_label="EXHAUSTED",
            last_checked_at=9999999999,
        )
        manager._key_cache["token_2"] = KeyStatus(
            token="token_2",
            masked_token=mask_token("token_2"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=5.0,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )
        manager._key_cache["token_3"] = KeyStatus(
            token="token_3",
            masked_token=mask_token("token_3"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=4.0,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )

        active = manager.get_active_token()
        self.assertEqual(active, "token_2")

    def test_highest_balance_strategy(self):
        tokens = ["token_1", "token_2", "token_3"]
        manager = ApifyKeyManager(tokens=tokens, strategy="highest_balance")

        manager._key_cache["token_1"] = KeyStatus(
            token="token_1",
            masked_token=mask_token("token_1"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=2.5,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )
        manager._key_cache["token_2"] = KeyStatus(
            token="token_2",
            masked_token=mask_token("token_2"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=9.8,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )
        manager._key_cache["token_3"] = KeyStatus(
            token="token_3",
            masked_token=mask_token("token_3"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=5.0,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )

        active = manager.get_active_token()
        self.assertEqual(active, "token_2")

    def test_execute_with_retry_failover(self):
        tokens = ["token_bad", "token_good"]
        manager = ApifyKeyManager(tokens=tokens)

        manager._key_cache["token_bad"] = KeyStatus(
            token="token_bad",
            masked_token=mask_token("token_bad"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=1.0,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )
        manager._key_cache["token_good"] = KeyStatus(
            token="token_good",
            masked_token=mask_token("token_good"),
            is_valid=True,
            is_exhausted=False,
            remaining_usd=5.0,
            status_label="ACTIVE",
            last_checked_at=9999999999,
        )

        call_records = []

        def mock_operation(client):
            call_records.append(client.token)
            if client.token == "token_bad":
                raise make_mock_apify_error(403, "monthly-usage-limit-exceeded", "Usage limit exceeded")
            return "SUCCESS_RESULT"

        result = manager.execute_with_retry(mock_operation)
        self.assertEqual(result, "SUCCESS_RESULT")
        self.assertEqual(call_records, ["token_bad", "token_good"])
        self.assertTrue(manager._key_cache["token_bad"].is_exhausted)
        self.assertEqual(manager.get_active_token(), "token_good")

    def test_all_keys_exhausted_raises(self):
        tokens = ["token_1"]
        manager = ApifyKeyManager(tokens=tokens)
        manager._key_cache["token_1"] = KeyStatus(
            token="token_1",
            masked_token=mask_token("token_1"),
            is_valid=True,
            is_exhausted=True,
            remaining_usd=0.0,
            status_label="EXHAUSTED",
            last_checked_at=9999999999,
        )

        with self.assertRaises(AllKeysExhaustedError):
            manager.execute_with_retry(lambda client: client.actor("foo").call())


if __name__ == "__main__":
    unittest.main()
