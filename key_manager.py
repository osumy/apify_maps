"""
Apify Key Manager & Auto-Rotation Pool
=======================================
A modular, production-grade key management system for Apify API tokens.
Features:
- Live credit & monthly limit inspection via Apify account/usage endpoints.
- Auto-detection of healthy, exhausted, rate-limited, and invalid keys.
- Automatic key rotation & failover during long-running scraping jobs.
- Standalone CLI interface for pool inspection and remaining balance checks.
- Thread-safe and zero-dependency beyond standard library + requests + apify-client.
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
from dataclasses import dataclass, asdict
from typing import Callable, Any, Optional, List, Dict
import requests
from apify_client import ApifyClient
from apify_client.errors import (
    ApifyApiError,
    ApifyClientError,
    UnauthorizedError,
    ForbiddenError,
    RateLimitError,
)

logger = logging.getLogger("ApifyKeyManager")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [KeyManager] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class KeyManagerError(Exception):
    """Base exception for KeyManager errors."""
    pass


class AllKeysExhaustedError(KeyManagerError):
    """Raised when every key in the pool is exhausted, invalid, or unavailable."""
    pass


@dataclass
class KeyStatus:
    token: str
    masked_token: str
    username: str = "Unknown"
    email: str = "Unknown"
    tier: str = "UNKNOWN"
    max_monthly_usd: float = 0.0
    monthly_usage_usd: float = 0.0
    remaining_usd: float = 0.0
    cycle_end: str = ""
    is_valid: bool = False
    is_exhausted: bool = False
    is_rate_limited: bool = False
    status_label: str = "UNKNOWN"  # ACTIVE, EXHAUSTED, INVALID, RATE_LIMITED, ERROR
    error_message: Optional[str] = None
    last_checked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Avoid leaking full token in serialized representations
        d["token"] = self.masked_token
        return d


def mask_token(token: str) -> str:
    """Masks an API token for safe printing and logging."""
    if not token:
        return ""
    if len(token) <= 18:
        return token[:6] + "..."
    return f"{token[:15]}...{token[-6:]}"


def load_tokens_from_env() -> List[str]:
    """
    Extracts tokens from environment variables:
    1. APIFY_TOKENS: Comma, semicolon, or newline separated list.
    2. APIFY_TOKEN: Single token or comma-separated tokens fallback.
    3. Optional 'apify_tokens.txt' file in current or project directory.
    """
    tokens: List[str] = []

    def _add_raw(raw: str):
        if not raw:
            return
        # Replace common separators with commas
        normalized = raw.replace(";", ",").replace("\n", ",").replace("\r", ",")
        for part in normalized.split(","):
            cleaned = part.strip().strip("'\"")
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)

    # 1. From APIFY_TOKENS variable
    _add_raw(os.getenv("APIFY_TOKENS", ""))

    # 2. From APIFY_TOKEN variable
    _add_raw(os.getenv("APIFY_TOKEN", ""))

    # 3. Check for tokens file (e.g. apify_tokens.txt)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_files = [
        os.path.join(base_dir, "apify_tokens.txt"),
        os.path.join(os.getcwd(), "apify_tokens.txt"),
    ]
    for filepath in candidate_files:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip().strip("'\"")
                        if line and not line.startswith("#") and line not in tokens:
                            tokens.append(line)
            except Exception as e:
                logger.warning(f"Could not read tokens file '{filepath}': {e}")

    return tokens


class ApifyKeyManager:
    """
    Manages a pool of Apify API keys with live quota checking,
    transparent failover, and automatic rotation.
    """

    def __init__(
        self,
        tokens: Optional[List[str]] = None,
        strategy: str = "sequential",  # "sequential" or "highest_balance"
        min_credit_threshold_usd: float = 0.05,
        cache_ttl_secs: float = 180.0,
        request_timeout: float = 12.0,
    ):
        """
        :param tokens: List of Apify API tokens. If None, auto-loaded from environment / files.
        :param strategy: 'sequential' (use current key until exhausted, then rotate)
                         or 'highest_balance' (prioritize key with most remaining credit).
        :param min_credit_threshold_usd: Threshold below which a key is considered exhausted ($0.05).
        :param cache_ttl_secs: Cache duration for user info / limits before re-querying Apify API.
        :param request_timeout: Timeout in seconds for Apify API requests.
        """
        raw_tokens = tokens if tokens is not None else load_tokens_from_env()
        # Clean and deduplicate while preserving original order
        self._tokens: List[str] = []
        for t in raw_tokens:
            cleaned = t.strip().strip("'\"")
            if cleaned and cleaned not in self._tokens:
                self._tokens.append(cleaned)

        self.strategy = strategy.lower()
        self.min_credit_threshold = min_credit_threshold_usd
        self.cache_ttl = cache_ttl_secs
        self.timeout = request_timeout

        self._key_cache: Dict[str, KeyStatus] = {}
        self._client_cache: Dict[str, ApifyClient] = {}
        self._current_index: int = 0

        if not self._tokens:
            logger.warning("No Apify tokens found! Provide them via APIFY_TOKENS, APIFY_TOKEN, or apify_tokens.txt.")

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def add_token(self, token: str) -> bool:
        """Adds a new token to the pool if not already present."""
        cleaned = token.strip().strip("'\"")
        if cleaned and cleaned not in self._tokens:
            self._tokens.append(cleaned)
            return True
        return False

    def remove_token(self, token: str) -> bool:
        """Removes a token from the pool."""
        cleaned = token.strip().strip("'\"")
        if cleaned in self._tokens:
            self._tokens.remove(cleaned)
            self._key_cache.pop(cleaned, None)
            self._client_cache.pop(cleaned, None)
            return True
        return False

    def fetch_key_info(self, token: str, force_refresh: bool = False) -> KeyStatus:
        """
        Queries Apify's API to inspect account identity, plan tier,
        usage limit, current monthly usage, and remaining credits.
        """
        now = time.time()
        cached = self._key_cache.get(token)
        if not force_refresh and cached and (now - cached.last_checked_at < self.cache_ttl):
            return cached

        status = KeyStatus(
            token=token,
            masked_token=mask_token(token),
            last_checked_at=now,
        )

        headers = {"Authorization": f"Bearer {token}"}

        try:
            # 1. Fetch user profile & plan info
            user_res = requests.get("https://api.apify.com/v2/users/me", headers=headers, timeout=self.timeout)
            if user_res.status_code == 401:
                status.is_valid = False
                status.status_label = "INVALID"
                status.error_message = "Authentication failed (401 Unauthorized / Token not found)"
                self._key_cache[token] = status
                return status
            elif user_res.status_code == 429:
                status.is_valid = True
                status.is_rate_limited = True
                status.status_label = "RATE_LIMITED"
                status.error_message = "Rate limit exceeded (429 Too Many Requests)"
                self._key_cache[token] = status
                return status
            elif user_res.status_code != 200:
                status.is_valid = False
                status.status_label = "ERROR"
                status.error_message = f"HTTP {user_res.status_code}: {user_res.text[:100]}"
                self._key_cache[token] = status
                return status

            user_data = user_res.json().get("data", {})
            status.username = user_data.get("username", "Unknown")
            status.email = user_data.get("email", "Unknown")
            plan = user_data.get("plan", {})
            status.tier = plan.get("tier", "FREE")
            monthly_credits = float(plan.get("monthlyUsageCreditsUsd", 0.0) or 0.0)

            # 2. Fetch monthly usage and limits
            limits_res = requests.get("https://api.apify.com/v2/users/me/limits", headers=headers, timeout=self.timeout)
            if limits_res.status_code == 200:
                lim_data = limits_res.json().get("data", {})
                limits = lim_data.get("limits", {})
                current = lim_data.get("current", {})
                cycle = lim_data.get("monthlyUsageCycle", {})

                # Max allowed usage in USD (falls back to monthly credits if unset)
                status.max_monthly_usd = float(limits.get("maxMonthlyUsageUsd", monthly_credits) or monthly_credits)
                status.monthly_usage_usd = float(current.get("monthlyUsageUsd", 0.0) or 0.0)
                status.cycle_end = cycle.get("endAt", "")
            else:
                status.max_monthly_usd = monthly_credits
                status.monthly_usage_usd = 0.0

            # Calculate remaining credit
            status.remaining_usd = max(0.0, status.max_monthly_usd - status.monthly_usage_usd)
            status.is_valid = True

            if status.remaining_usd <= self.min_credit_threshold:
                status.is_exhausted = True
                status.status_label = "EXHAUSTED"
                status.error_message = f"Monthly credit exhausted (${status.monthly_usage_usd:.2f} / ${status.max_monthly_usd:.2f})"
            else:
                status.is_exhausted = False
                status.status_label = "ACTIVE"
                status.error_message = None

        except requests.RequestException as e:
            status.is_valid = False
            status.status_label = "NETWORK_ERROR"
            status.error_message = f"Network timeout/error checking key: {e}"

        self._key_cache[token] = status
        return status

    def check_all(self, force_refresh: bool = False) -> List[KeyStatus]:
        """Checks status for all tokens in the pool."""
        return [self.fetch_key_info(t, force_refresh=force_refresh) for t in self._tokens]

    def get_healthy_keys(self, force_refresh: bool = False) -> List[KeyStatus]:
        """Returns all keys that are valid and have remaining credits above threshold."""
        all_status = self.check_all(force_refresh=force_refresh)
        return [
            s for s in all_status
            if s.is_valid and not s.is_exhausted and not s.is_rate_limited and s.remaining_usd > self.min_credit_threshold
        ]

    def get_active_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        Determines the current active token according to the configured rotation strategy.
        """
        if not self._tokens:
            return None

        # Check current key first if in sequential mode
        if self.strategy == "sequential":
            # Attempt to use key at current index
            if 0 <= self._current_index < len(self._tokens):
                candidate = self._tokens[self._current_index]
                status = self.fetch_key_info(candidate, force_refresh=force_refresh)
                if status.is_valid and not status.is_exhausted and not status.is_rate_limited:
                    return candidate

            # If current key is not healthy, find the next healthy key sequentially
            for idx, token in enumerate(self._tokens):
                status = self.fetch_key_info(token, force_refresh=force_refresh)
                if status.is_valid and not status.is_exhausted and not status.is_rate_limited:
                    self._current_index = idx
                    return token

            return None

        elif self.strategy == "highest_balance":
            healthy = self.get_healthy_keys(force_refresh=force_refresh)
            if not healthy:
                return None
            best = max(healthy, key=lambda s: s.remaining_usd)
            self._current_index = self._tokens.index(best.token)
            return best.token

        else:
            # Fallback sequential
            healthy = self.get_healthy_keys(force_refresh=force_refresh)
            if healthy:
                best = healthy[0]
                self._current_index = self._tokens.index(best.token)
                return best.token
            return None

    def get_client(self, token: Optional[str] = None) -> ApifyClient:
        """
        Returns a cached or new ApifyClient instance for the active (or specified) token.
        """
        active_token = token or self.get_active_token()
        if not active_token:
            raise AllKeysExhaustedError(
                "Cannot create ApifyClient: No healthy Apify keys available with remaining credit!"
            )

        if active_token not in self._client_cache:
            self._client_cache[active_token] = ApifyClient(active_token)
        return self._client_cache[active_token]

    def mark_exhausted(self, token: str, reason: str = "Quota limit reached") -> None:
        """Explicitly marks a key as exhausted (e.g. after a runtime API quota error)."""
        cached = self._key_cache.get(token)
        if cached:
            cached.is_exhausted = True
            cached.remaining_usd = 0.0
            cached.status_label = "EXHAUSTED"
            cached.error_message = reason
        else:
            self._key_cache[token] = KeyStatus(
                token=token,
                masked_token=mask_token(token),
                is_valid=True,
                is_exhausted=True,
                remaining_usd=0.0,
                status_label="EXHAUSTED",
                error_message=reason,
                last_checked_at=time.time(),
            )
        logger.warning(f"Key {mask_token(token)} marked as EXHAUSTED: {reason}")

    def mark_invalid(self, token: str, reason: str = "Invalid token") -> None:
        """Marks a key as invalid (e.g. on 401 Unauthorized)."""
        cached = self._key_cache.get(token)
        if cached:
            cached.is_valid = False
            cached.status_label = "INVALID"
            cached.error_message = reason
        else:
            self._key_cache[token] = KeyStatus(
                token=token,
                masked_token=mask_token(token),
                is_valid=False,
                status_label="INVALID",
                error_message=reason,
                last_checked_at=time.time(),
            )
        logger.error(f"Key {mask_token(token)} marked as INVALID: {reason}")

    def rotate(self, reason: Optional[str] = None) -> str:
        """
        Advances the pool to the next available healthy key.
        """
        old_token = self._tokens[self._current_index] if (0 <= self._current_index < len(self._tokens)) else None
        if reason:
            logger.info(f"Rotating key from {mask_token(old_token or '')}. Reason: {reason}")

        # Search forward from current index + 1
        num_keys = len(self._tokens)
        for offset in range(1, num_keys + 1):
            next_idx = (self._current_index + offset) % num_keys
            candidate = self._tokens[next_idx]
            status = self.fetch_key_info(candidate, force_refresh=False)
            if status.is_valid and not status.is_exhausted and not status.is_rate_limited:
                self._current_index = next_idx
                logger.info(
                    f"Switched to healthy key #{next_idx + 1}: {status.masked_token} "
                    f"(User: {status.username}, Balance: ${status.remaining_usd:.2f})"
                )
                return candidate

        raise AllKeysExhaustedError(
            "All keys in the Apify pool have been exhausted or are invalid. Please add more active keys."
        )

    def is_rotation_error(self, exc: Exception) -> tuple[bool, str]:
        """
        Determines if an exception is caused by credit exhaustion, quota limit,
        or token revocation that warrants switching to another key.
        """
        msg = str(exc).lower()

        # Apify Api Error checks
        if isinstance(exc, (UnauthorizedError, ForbiddenError, RateLimitError, ApifyApiError)):
            status_code = getattr(exc, "status_code", None)
            err_type = getattr(exc, "type", "") or ""

            if status_code == 401 or "user-or-token-not-found" in err_type:
                return True, f"Authentication failed (401): {exc}"
            if status_code == 429 or "rate-limit" in err_type:
                return True, f"Rate limit reached (429): {exc}"
            if status_code in (402, 403) or "usage-limit" in err_type or "monthly-usage" in err_type:
                return True, f"Account quota / usage limit exceeded: {exc}"

        # Text-based keyword heuristic for nested or raw error responses
        quota_keywords = [
            "monthly usage limit exceeded",
            "usage limit reached",
            "monthly-usage-limit-exceeded",
            "usage-limit-exceeded",
            "user was not found or authentication token is not valid",
            "credit limit",
            "payment required",
            "quota exceeded",
            "insufficient credit",
            "not enough compute units",
            "max monthly usage",
        ]
        for kw in quota_keywords:
            if kw in msg:
                return True, f"Matched quota exhaustion indicator '{kw}': {exc}"

        return False, ""

    def execute_with_retry(
        self,
        func: Callable[[ApifyClient], Any],
        max_rotations: Optional[int] = None,
    ) -> Any:
        """
        Executes an Apify operation with automatic failover and key rotation.
        If the active key runs out of credits or encounters an auth error,
        it automatically rotates to the next available healthy key in the pool and retries!
        """
        if not self._tokens:
            raise AllKeysExhaustedError("No Apify keys registered in the pool.")

        allowed_rotations = max_rotations if max_rotations is not None else len(self._tokens)
        rotations_done = 0

        while True:
            active_token = self.get_active_token()
            if not active_token:
                raise AllKeysExhaustedError(
                    "No healthy Apify keys available. All accounts exhausted or invalid."
                )

            client = self.get_client(active_token)
            try:
                return func(client)
            except Exception as e:
                is_rotatable, reason = self.is_rotation_error(e)
                if not is_rotatable:
                    # Regular application or parameter error — re-raise as-is
                    raise

                rotations_done += 1
                logger.warning(
                    f"[FAILOVER TRIGGERED] Key {mask_token(active_token)} encountered error: {e}"
                )

                if "401" in reason or "not valid" in reason:
                    self.mark_invalid(active_token, reason)
                else:
                    self.mark_exhausted(active_token, reason)

                if rotations_done > allowed_rotations:
                    raise AllKeysExhaustedError(
                        f"Exceeded maximum rotations ({allowed_rotations}). No remaining usable keys."
                    ) from e

                # Try to rotate to the next key
                try:
                    self.rotate(reason=reason)
                except AllKeysExhaustedError:
                    raise AllKeysExhaustedError(
                        "All keys in the Apify pool are exhausted after encountering runtime quota errors."
                    ) from e

    def print_summary(self, force_refresh: bool = False) -> None:
        """
        Prints a clean, formatted ASCII table of all keys, their limits,
        usage, remaining dollars, and health status.
        """
        statuses = self.check_all(force_refresh=force_refresh)

        print("\n" + "=" * 96)
        print("                        APIFY MULTI-KEY POOL STATUS & BALANCES")
        print("=" * 96)
        header = f"{'#':<3} | {'Token':<22} | {'User / Email':<28} | {'Tier':<6} | {'Limit':<7} | {'Used':<7} | {'Rem.':<7} | {'Status'}"
        print(header)
        print("-" * 96)

        total_rem = 0.0
        active_count = 0
        exhausted_count = 0
        invalid_count = 0

        for idx, s in enumerate(statuses, 1):
            user_str = f"{s.username}"
            if s.email and s.email != "Unknown" and s.email != s.username:
                user_str = f"{s.username} ({s.email[:15]})"
            user_str = (user_str[:26] + "..") if len(user_str) > 28 else user_str

            lim_str = f"${s.max_monthly_usd:.2f}" if s.is_valid else "-"
            used_str = f"${s.monthly_usage_usd:.2f}" if s.is_valid else "-"
            rem_str = f"${s.remaining_usd:.2f}" if s.is_valid else "-"

            status_icon = "⚪"
            if s.status_label == "ACTIVE":
                status_icon = "🟢 ACTIVE"
                active_count += 1
                total_rem += s.remaining_usd
            elif s.status_label == "EXHAUSTED":
                status_icon = "🟡 EXHAUSTED"
                exhausted_count += 1
            elif s.status_label == "INVALID":
                status_icon = "🔴 INVALID"
                invalid_count += 1
            elif s.status_label == "RATE_LIMITED":
                status_icon = "⏳ RATE_LIMIT"
            else:
                status_icon = f"⚠️ {s.status_label}"

            row = f"{idx:<3} | {s.masked_token:<22} | {user_str:<28} | {s.tier:<6} | {lim_str:<7} | {used_str:<7} | {rem_str:<7} | {status_icon}"
            print(row)

        print("-" * 96)
        print(f"Summary: Total Keys: {len(statuses)} | Active: {active_count} | Exhausted: {exhausted_count} | Invalid: {invalid_count}")
        print(f"Total Usable Balance: ${total_rem:.2f} USD")
        active_key = self.get_active_token(force_refresh=False)
        if active_key:
            active_info = self.fetch_key_info(active_key)
            print(f"Current Working Key: {active_info.masked_token} (User: {active_info.username}, Rem: ${active_info.remaining_usd:.2f})")
        else:
            print("Current Working Key: NONE AVAILABLE")
        print("=" * 96 + "\n")


# ------------------------------------------------------------------------------
# CLI Helper
# ------------------------------------------------------------------------------
def main():
    """CLI entrypoint for checking balances and inspecting the Apify key pool."""
    # Load .env if present
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    manager = ApifyKeyManager()

    if "--json" in sys.argv:
        statuses = manager.check_all(force_refresh=True)
        print(json.dumps([s.to_dict() for s in statuses], indent=2))
        return

    manager.print_summary(force_refresh=True)


if __name__ == "__main__":
    main()
