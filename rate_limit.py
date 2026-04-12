"""
Rate limiting monkey-patch for Hermes gateway.

Patches GatewayRunner._handle_message to add rate limiting before
message processing. Mirrors V2 bot/rate_limit.py protections.

Import this module before starting the gateway to activate the patch.
If patching fails (e.g. Hermes API changed), falls back silently.
"""

import time
import logging
import datetime
from collections import deque

logger = logging.getLogger("rate_limit")

# ── Config ──────────────────────────────────────────────────────
COOLDOWN = 15.0          # seconds between requests per user
BAN_STRIKES = 5          # consecutive violations before auto-ban
BAN_DURATION = 3600.0    # 1 hour ban
MAX_INPUT = 500          # max message length
GLOBAL_RPM = 30          # global requests per minute
DAILY_QUOTA = 20         # per-user daily limit
ADMIN_IDS = {"353559286"}  # admin users skip all limits

# ── State (in-memory, resets on deploy) ─────────────────────────
_user_last: dict[str, float] = {}
_strike_count: dict[str, int] = {}
_banned_until: dict[str, float] = {}
_global_ts: deque[float] = deque()
_daily_count: dict[tuple[str, str], int] = {}


def _record_strike(uid: str):
    count = _strike_count.get(uid, 0) + 1
    _strike_count[uid] = count
    if count >= BAN_STRIKES:
        _banned_until[uid] = time.monotonic() + BAN_DURATION
        logger.warning("User %s auto-banned for 1h after %d strikes", uid, count)


def check_rate_limit(uid: str, message: str) -> str | None:
    """Check all rate limits. Returns error message string, or None if allowed."""
    if uid in ADMIN_IDS:
        return None

    now = time.monotonic()

    # 1. Ban check
    ban_until = _banned_until.get(uid)
    if ban_until:
        if now < ban_until:
            return "⏱ Too many violations. Try again in 1 hour."
        del _banned_until[uid]
        _strike_count.pop(uid, None)

    # 2. Input length
    if len(message) > MAX_INPUT:
        return f"⚠️ Message too long (max {MAX_INPUT} chars)."

    # 3. Global rate limit
    while _global_ts and _global_ts[0] < now - 60:
        _global_ts.popleft()
    if len(_global_ts) >= GLOBAL_RPM:
        return "⏱ System busy, please try again shortly."
    _global_ts.append(now)

    # 4. Per-user cooldown
    last = _user_last.get(uid, 0)
    if now - last < COOLDOWN:
        _record_strike(uid)
        return "⏱ Please wait before sending another message."
    _user_last[uid] = now
    _strike_count.pop(uid, None)

    # 5. Daily quota
    today = datetime.date.today().isoformat()
    key = (uid, today)
    count = _daily_count.get(key, 0)
    if count >= DAILY_QUOTA:
        return f"⚠️ Daily limit reached ({DAILY_QUOTA}/day). Try again tomorrow."
    _daily_count[key] = count + 1

    # Cleanup old daily entries
    for k in list(_daily_count):
        if k[1] != today:
            del _daily_count[k]

    return None


def apply_patch():
    """Monkey-patch GatewayRunner._handle_message with rate limiting."""
    try:
        from gateway.run import GatewayRunner
        _original = GatewayRunner._handle_message

        async def _patched_handle_message(self, event):
            uid = getattr(getattr(event, 'source', None), 'user_id', None)
            msg = getattr(event, 'text', '') or ''

            if uid:
                err = check_rate_limit(str(uid), msg)
                if err:
                    logger.info("Rate limited user %s: %s", uid, err)
                    try:
                        platform = getattr(getattr(event, 'source', None), 'platform', None)
                        chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
                        adapter = self.adapters.get(platform)
                        if adapter and chat_id:
                            await adapter.send(chat_id, err)
                    except Exception as e:
                        logger.debug("Failed to send rate limit message: %s", e)
                    return None

            return await _original(self, event)

        GatewayRunner._handle_message = _patched_handle_message
        logger.info("Rate limiting patch applied (admin: %s)", ADMIN_IDS)
        print("[rate_limit] Monkey-patch applied successfully", flush=True)
        return True

    except Exception as e:
        logger.error("Failed to apply rate limit patch: %s", e)
        print(f"[rate_limit] Patch FAILED (falling back to no rate limiting): {e}", flush=True)
        return False
