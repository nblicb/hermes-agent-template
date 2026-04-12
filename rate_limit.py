"""
Rate limiting + usage logging monkey-patch for Hermes gateway.

Patches GatewayRunner._handle_message to:
1. Rate limit (cooldown, ban, quota, input length)
2. Log every query to PostgreSQL il_chat_usage table (same as web-app)

Import this module before starting the gateway to activate the patch.
If patching fails, falls back silently (no rate limiting, no logging).
"""

import os
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

# ── DB connection for usage logging ────────────────────────────
_db_conn = None

def _get_db():
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.cursor().execute("SELECT 1")
            return _db_conn
        except Exception:
            _db_conn = None
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    try:
        import psycopg2
        _db_conn = psycopg2.connect(db_url)
        _db_conn.autocommit = True
        return _db_conn
    except Exception as e:
        logger.debug("DB connection failed: %s", e)
        return None


def _log_usage(user_id: str, platform: str, query_text: str,
               response_preview: str = None, elapsed_ms: int = None, error: str = None):
    """Log usage to il_chat_usage table (same table as web-app)."""
    conn = _get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        today = datetime.date.today().isoformat()
        cur.execute(
            """INSERT INTO il_chat_usage
               (user_id, used_date, query, ip, response_preview, elapsed_ms, error, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
            (f"tg:{user_id}", today, (query_text or "")[:200], platform,
             (response_preview or "")[:500] if response_preview else None,
             elapsed_ms, (error or "")[:200] if error else None)
        )
    except Exception as e:
        logger.debug("Usage log failed: %s", e)


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
    """Monkey-patch GatewayRunner._handle_message with rate limiting + logging."""
    try:
        from gateway.run import GatewayRunner
        _original = GatewayRunner._handle_message

        async def _patched_handle_message(self, event):
            uid = getattr(getattr(event, 'source', None), 'user_id', None)
            msg = getattr(event, 'text', '') or ''
            platform = getattr(getattr(event, 'source', None), 'platform', None)
            platform_name = platform.value if platform else "unknown"
            start_ms = time.monotonic()

            if uid:
                uid_str = str(uid)
                # Rate limit check
                err = check_rate_limit(uid_str, msg)
                if err:
                    logger.info("Rate limited user %s: %s", uid, err)
                    _log_usage(uid_str, platform_name, msg, error="rate_limited")
                    try:
                        chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
                        adapter = self.adapters.get(platform)
                        if adapter and chat_id:
                            await adapter.send(chat_id, err)
                    except Exception as e:
                        logger.debug("Failed to send rate limit message: %s", e)
                    return None

            # Send "querying" status message before agent runs (match user language)
            status_msg_id = None
            try:
                chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
                adapter = self.adapters.get(platform)
                is_chinese = any('\u4e00' <= c <= '\u9fff' for c in msg)
                status_text = "🔍 正在查询数据..." if is_chinese else "🔍 Fetching data..."
                if adapter and chat_id:
                    sent = await adapter.send(chat_id, status_text)
                    # Try to get message ID for later deletion
                    if sent and hasattr(sent, 'message_id'):
                        status_msg_id = sent.message_id
                    elif sent and isinstance(sent, dict):
                        status_msg_id = sent.get('message_id')
            except Exception:
                pass

            # Call original handler
            result = await _original(self, event)

            # Delete status message after response
            if status_msg_id:
                try:
                    chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
                    adapter = self.adapters.get(platform)
                    if adapter and hasattr(adapter, '_bot') and chat_id:
                        await adapter._bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
                except Exception:
                    pass  # Message may already be gone

            # Log usage after response
            if uid:
                elapsed = int((time.monotonic() - start_ms) * 1000)
                response_preview = result[:500] if isinstance(result, str) else None
                _log_usage(str(uid), platform_name, msg, response_preview, elapsed)

            return result

        GatewayRunner._handle_message = _patched_handle_message
        logger.info("Rate limiting + logging patch applied (admin: %s)", ADMIN_IDS)
        print("[rate_limit] Monkey-patch applied (rate limiting + DB logging)", flush=True)
        return True

    except Exception as e:
        logger.error("Failed to apply rate limit patch: %s", e)
        print(f"[rate_limit] Patch FAILED: {e}", flush=True)
        return False
