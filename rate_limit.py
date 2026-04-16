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


def _apply_memory_isolation_patch():
    """Patch MemoryStore + AIAgent to use per-user memory directories.

    Each user gets /data/.hermes/memories/user_{user_id}/ instead of the
    shared global /data/.hermes/memories/. Fail-safe: if anything goes wrong,
    memory is disabled for that user (never falls back to global).
    """
    try:
        from tools.memory_tool import MemoryStore, get_memory_dir
        from run_agent import AIAgent

        # -- Patch MemoryStore methods to respect instance _mem_dir --

        _orig_load = MemoryStore.load_from_disk
        _orig_save = MemoryStore.save_to_disk
        # _path_for is @staticmethod, replace with instance-aware version

        def _patched_path_for(self, target: str):
            from pathlib import Path
            mem_dir = getattr(self, '_mem_dir', None) or get_memory_dir()
            if target == "user":
                return mem_dir / "USER.md"
            return mem_dir / "MEMORY.md"

        def _patched_load(self):
            from pathlib import Path
            mem_dir = getattr(self, '_mem_dir', None) or get_memory_dir()
            mem_dir.mkdir(parents=True, exist_ok=True)
            self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
            self.user_entries = self._read_file(mem_dir / "USER.md")
            self.memory_entries = list(dict.fromkeys(self.memory_entries))
            self.user_entries = list(dict.fromkeys(self.user_entries))
            self._system_prompt_snapshot = {
                "memory": self._render_block("memory", self.memory_entries),
                "user": self._render_block("user", self.user_entries),
            }

        def _patched_save(self, target: str):
            mem_dir = getattr(self, '_mem_dir', None) or get_memory_dir()
            mem_dir.mkdir(parents=True, exist_ok=True)
            self._write_file(self._path_for(target), self._entries_for(target))

        MemoryStore.load_from_disk = _patched_load
        MemoryStore.save_to_disk = _patched_save
        MemoryStore._path_for = _patched_path_for

        # -- Patch AIAgent.__init__ to set per-user _mem_dir --

        _orig_agent_init = AIAgent.__init__

        def _patched_agent_init(self, *args, **kwargs):
            _orig_agent_init(self, *args, **kwargs)
            try:
                if not getattr(self, '_user_id', None):
                    # No user_id (e.g. Web API without user context) → disable memory
                    # Never allow fallback to global directory
                    if getattr(self, '_memory_store', None):
                        self._memory_store = None
                        self._memory_enabled = False
                        self._user_profile_enabled = False
                        logger.warning("[MEMORY PATCH] No user_id, memory disabled")
                    return

                if getattr(self, '_memory_store', None):
                    from pathlib import Path
                    from hermes_constants import get_hermes_home
                    user_mem_dir = get_hermes_home() / "memories" / f"user_{self._user_id}"
                    user_mem_dir.mkdir(parents=True, exist_ok=True)
                    self._memory_store._mem_dir = user_mem_dir
                    self._memory_store.load_from_disk()  # reload from per-user dir
                    print(f"[MEMORY PATCH] user {self._user_id} -> {user_mem_dir}", flush=True)
            except Exception as e:
                # Fail-safe: disable memory, never fall back to global
                logger.error("[MEMORY PATCH] Failed for user %s: %s",
                             getattr(self, '_user_id', '?'), e)
                self._memory_store = None
                self._memory_enabled = False
                self._user_profile_enabled = False

        AIAgent.__init__ = _patched_agent_init

        print("[MEMORY PATCH] Per-user memory isolation applied", flush=True)
        return True

    except Exception as e:
        logger.error("[MEMORY PATCH] Failed to apply: %s", e)
        print(f"[MEMORY PATCH] FAILED: {e}", flush=True)
        return False


def _apply_mcp_resilience_patch():
    """Increase MCP reconnect retries + add session lifecycle logging.

    1. _MAX_RECONNECT_RETRIES 5 → 50 (~25 min recovery window)
    2. Patch MCP server's run() to log session connect/disconnect times
       for collecting FMP TTL data.
    """
    try:
        import tools.mcp_tool as mcp_module

        # Increase retry limit
        old_val = getattr(mcp_module, '_MAX_RECONNECT_RETRIES', 5)
        mcp_module._MAX_RECONNECT_RETRIES = 50
        print(f"[MCP PATCH] _MAX_RECONNECT_RETRIES: {old_val} → 50", flush=True)

        # Patch _run_http to log session lifecycle
        _McpServer = getattr(mcp_module, 'McpServer', None) or getattr(mcp_module, '_McpServer', None)
        if _McpServer and hasattr(_McpServer, '_run_http'):
            _orig_run_http = _McpServer._run_http

            async def _patched_run_http(self, config):
                import time as _time
                _start = _time.time()
                _name = getattr(self, 'name', 'unknown')
                print(f"[MCP SESSION] {_name}: connecting...", flush=True)
                try:
                    await _orig_run_http(self, config)
                finally:
                    _elapsed = _time.time() - _start
                    _mins = int(_elapsed // 60)
                    print(f"[MCP SESSION] {_name}: disconnected after {_mins}m {int(_elapsed % 60)}s", flush=True)

            _McpServer._run_http = _patched_run_http
            print("[MCP PATCH] Session lifecycle logging enabled", flush=True)
        else:
            # Try alternative class name
            for attr_name in dir(mcp_module):
                cls = getattr(mcp_module, attr_name, None)
                if isinstance(cls, type) and hasattr(cls, '_run_http') and attr_name != 'McpServer':
                    _orig = cls._run_http
                    async def _wrap(self, config, _orig=_orig):
                        import time as _t
                        _s = _t.time()
                        print(f"[MCP SESSION] {getattr(self,'name','?')}: connecting...", flush=True)
                        try:
                            await _orig(self, config)
                        finally:
                            print(f"[MCP SESSION] {getattr(self,'name','?')}: disconnected after {int((_t.time()-_s)//60)}m", flush=True)
                    cls._run_http = _wrap
                    print(f"[MCP PATCH] Session logging on {attr_name}", flush=True)
                    break

    except Exception as e:
        print(f"[MCP PATCH] Failed: {e}", flush=True)


def apply_patch():
    """Monkey-patch GatewayRunner._handle_message with rate limiting + logging."""
    try:
        # Apply patches
        _apply_memory_isolation_patch()
        _apply_mcp_resilience_patch()

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

                # Slash command intercept (zero tokens, direct DB)
                try:
                    from commands import dispatch_command
                    cmd_response = dispatch_command(uid_str, msg)
                    if cmd_response is not None:
                        _log_usage(uid_str, platform_name, msg, cmd_response[:200], 0, None)
                        try:
                            chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
                            adapter = self.adapters.get(platform)
                            if adapter and chat_id:
                                await adapter.send(chat_id, cmd_response)
                        except Exception as e:
                            logger.debug("Failed to send command response: %s", e)
                        return None
                except Exception as e:
                    logger.debug("Command dispatch error: %s", e)

                # Rate limit check (only for non-command messages)
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

        # Patch STT: replace Hermes built-in with Volcengine BigModel ASR
        _volc_app_id = os.environ.get("VOLCENGINE_ASR_APP_ID", "")
        _volc_token = os.environ.get("VOLCENGINE_ASR_TOKEN", "")
        if _volc_app_id and _volc_token:
            _original_enrich = GatewayRunner._enrich_message_with_transcription

            async def _patched_enrich(self, event, source):
                """Use Volcengine BigModel ASR instead of built-in STT."""
                import asyncio
                attachments = getattr(event, 'attachments', []) or []
                voice_att = None
                for att in attachments:
                    mime = getattr(att, 'mime_type', '') or ''
                    if mime.startswith('audio/') or mime.startswith('voice/'):
                        voice_att = att
                        break
                if not voice_att:
                    return await _original_enrich(self, event, source)

                # Download audio
                try:
                    audio_path = getattr(voice_att, 'local_path', None) or getattr(voice_att, 'path', None)
                    if audio_path:
                        import pathlib
                        audio_bytes = pathlib.Path(audio_path).read_bytes()
                    elif hasattr(voice_att, 'data') and voice_att.data:
                        audio_bytes = voice_att.data
                    else:
                        return await _original_enrich(self, event, source)

                    from asr import transcribe_voice
                    text = await transcribe_voice(audio_bytes)
                    if text:
                        event.text = text
                        logger.info("Volcengine ASR: %s", text[:50])
                        return text
                    else:
                        return await _original_enrich(self, event, source)
                except Exception as e:
                    logger.warning("Volcengine ASR failed, falling back: %s", e)
                    return await _original_enrich(self, event, source)

            GatewayRunner._enrich_message_with_transcription = _patched_enrich
            print("[rate_limit] Volcengine ASR patch applied", flush=True)
        else:
            print("[rate_limit] Volcengine ASR not configured (using built-in STT)", flush=True)

        logger.info("Rate limiting + logging patch applied (admin: %s)", ADMIN_IDS)
        print("[rate_limit] Monkey-patch applied (rate limiting + DB logging)", flush=True)
        return True

    except Exception as e:
        logger.error("Failed to apply rate limit patch: %s", e)
        print(f"[rate_limit] Patch FAILED: {e}", flush=True)
        return False
