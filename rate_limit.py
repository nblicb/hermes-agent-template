"""
Hermes gateway monkey-patches (applied at startup via apply_patch()).

All patches live in this module to keep a single upgrade-review surface.

Active patches:
1. Rate limit + usage logging (GatewayRunner._handle_message wrapper)
   Cooldown / ban / daily quota / input-length + writes il_chat_usage.
2. Slash-command interceptor (inside #1)
   commands.dispatch_command short-circuits /watch /alert /usage /pro /notify.
3. Volcengine ASR (GatewayRunner._enrich_message_with_transcription wrapper)
   Replaces built-in STT when VOLCENGINE_ASR_APP_ID+TOKEN are set.
4. Memory isolation (_apply_memory_isolation_patch)
   Per-user /data/.hermes/memories/user_{uid}/; fail-safe disables memory
   when no user_id. Source: tools/memory_tool.py, run_agent.py:626.
   Based on: hermes-agent@58b6b5e (2026-04-12)
5. MCP resilience (_apply_mcp_resilience_patch)
   _MAX_RECONNECT_RETRIES 5 → 50; logs MCP session connect/disconnect.
   Source: tools/mcp_tool.py.
   Based on: hermes-agent@58b6b5e (2026-04-12)
6. Home-channel nag suppression (_apply_home_channel_suppress)
   gateway.run.os → shim intercepting *_HOME_CHANNEL getenv only.
   Module-scoped so gateway.config + cron.scheduler see unset env.
   Source: gateway/run.py:3451.
   Based on: hermes-agent@58b6b5e (2026-04-12)
7. API-server user_id propagation (_apply_api_server_user_id_patch)
   X-Hermes-User-Id header → ContextVar (bridge) → _run_agent reads on
   async side → closure → _create_agent → AIAgent(user_id=).
   Source: gateway/platforms/api_server.py:451, :529, :1410.
   Based on: hermes-agent@58b6b5e (2026-04-12)

Rejected approaches (do not re-propose):
- TELEGRAM_HOME_CHANNEL="disabled" sentinel: pollutes config.py:733
  (HomeChannel chat_id) + cron/scheduler.py:95 (fallback target).
- adapter.send text-match for nag: fragile to copy changes, wrong layer,
  didn't verify /sethome. Function-level patch instead.
- self._pending_user_id singleton attr for Fix 1: race across concurrent
  requests — ApiServerPlatform is singleton, async tasks interleave at awaits.
- Pure ContextVar + copy_context().run spanning async→executor: works but
  adds PEP-567 cognitive load. Prefer: read on async side, capture in
  closure, thread explicit kwarg through executor boundary.

Import this module before starting the gateway. Failures fall back silently.
"""

import os
import time
import logging
import datetime
import contextvars
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

# ── User-ID propagation for API server (Fix 1) ──────────────────
# Bridges X-Hermes-User-Id header → _run_agent → _create_agent → AIAgent.
# ContextVar used only on async side; executor receives user_id via closure.
_hermes_user_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "hermes_user_id", default=None
)

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


def _apply_home_channel_suppress():
    """Suppress the 'No home channel is set for X' first-message nag.

    The nag lives inline inside GatewayRunner._handle_message_with_agent
    (gateway/run.py ~line 3451): `if not os.getenv(env_key)` where env_key
    is f"{PLATFORM}_HOME_CHANNEL". There is no helper function to patch.

    Why not set the env var: TELEGRAM_HOME_CHANNEL is a functional chat_id,
    not a toggle. gateway/config.py:733 reads it to build a HomeChannel
    object; cron/scheduler.py:95 reads it as a fallback delivery target.
    Setting it to a sentinel pollutes both.

    Approach: replace `gateway.run.os` with a shim that intercepts ONLY
    `*_HOME_CHANNEL` getenv lookups and returns a non-empty string when the
    real env is unset. Module-scoped — gateway.config and cron.scheduler each
    do their own `import os` so they are unaffected and still see the unset
    env. /sethome uses os.environ.__setitem__ (not getenv) so it is also
    unaffected.
    """
    try:
        import os as _real_os
        from gateway import run as gateway_run

        class _HomeChannelOSShim:
            def __getattr__(self, name):
                return getattr(_real_os, name)

            def getenv(self, key, default=None):
                if isinstance(key, str) and key.endswith("_HOME_CHANNEL"):
                    real = _real_os.getenv(key, default)
                    return real if real else "__nag_suppressed__"
                return _real_os.getenv(key, default)

        gateway_run.os = _HomeChannelOSShim()
        print("[HOME CHANNEL PATCH] gateway.run.os shim installed (nag suppressed)", flush=True)
    except Exception as e:
        print(f"[HOME CHANNEL PATCH] FAILED: {e}", flush=True)


def _apply_api_server_user_id_patch():
    """Propagate X-Hermes-User-Id header → AIAgent.user_id.

    Chain: _handle_chat_completions (wrap) → ContextVar → _run_agent (replace,
    reads ContextVar on async side) → _create_agent (replace, accepts user_id
    kwarg) → AIAgent(user_id=...).

    ContextVar is a bridge across only the _handle_chat_completions →
    _run_agent hop (handler is 220 lines, not replaced wholesale). From
    _run_agent downward, user_id is an explicit kwarg. Each aiohttp request
    runs in its own asyncio.Task with its own Context, so concurrent requests
    cannot cross-contaminate.
    """
    try:
        from gateway.platforms.api_server import ApiServerPlatform
        import asyncio

        # Patch 1: wrap _handle_chat_completions — read header → set ContextVar
        _orig_handle = ApiServerPlatform._handle_chat_completions

        async def _patched_handle_chat_completions(self, request):
            user_id = (request.headers.get("X-Hermes-User-Id", "") or "").strip() or None
            token = _hermes_user_id_var.set(user_id)
            try:
                return await _orig_handle(self, request)
            finally:
                _hermes_user_id_var.reset(token)

        ApiServerPlatform._handle_chat_completions = _patched_handle_chat_completions

        # Patch 2: replace _run_agent — read ContextVar on async side, pass via closure
        async def _patched_run_agent(
            self,
            user_message,
            conversation_history,
            ephemeral_system_prompt=None,
            session_id=None,
            stream_delta_callback=None,
            tool_progress_callback=None,
            agent_ref=None,
        ):
            loop = asyncio.get_event_loop()
            user_id = _hermes_user_id_var.get()

            def _run():
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=stream_delta_callback,
                    tool_progress_callback=tool_progress_callback,
                    user_id=user_id,
                )
                if agent_ref is not None:
                    agent_ref[0] = agent
                result = agent.run_conversation(
                    user_message=user_message,
                    conversation_history=conversation_history,
                    task_id="default",
                )
                usage = {
                    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                    "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                }
                return result, usage

            return await loop.run_in_executor(None, _run)

        ApiServerPlatform._run_agent = _patched_run_agent

        # Patch 3: replace _create_agent — accept user_id kwarg, pass to AIAgent
        def _patched_create_agent(
            self,
            ephemeral_system_prompt=None,
            session_id=None,
            stream_delta_callback=None,
            tool_progress_callback=None,
            user_id=None,
        ):
            from run_agent import AIAgent
            from gateway.run import (
                _resolve_runtime_agent_kwargs,
                _resolve_gateway_model,
                _load_gateway_config,
                GatewayRunner,
            )
            from hermes_cli.tools_config import _get_platform_tools

            runtime_kwargs = _resolve_runtime_agent_kwargs()
            model = _resolve_gateway_model()
            user_config = _load_gateway_config()
            enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            fallback_model = GatewayRunner._load_fallback_model()

            return AIAgent(
                model=model,
                **runtime_kwargs,
                max_iterations=max_iterations,
                quiet_mode=True,
                verbose_logging=False,
                ephemeral_system_prompt=ephemeral_system_prompt or None,
                enabled_toolsets=enabled_toolsets,
                session_id=session_id,
                platform="api_server",
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
                session_db=self._ensure_session_db(),
                fallback_model=fallback_model,
                user_id=user_id,
            )

        ApiServerPlatform._create_agent = _patched_create_agent

        print("[API SERVER USER_ID PATCH] installed (X-Hermes-User-Id → AIAgent.user_id)", flush=True)
    except Exception as e:
        print(f"[API SERVER USER_ID PATCH] FAILED: {e}", flush=True)


def apply_patch():
    """Monkey-patch GatewayRunner._handle_message with rate limiting + logging."""
    try:
        # Apply patches
        _apply_memory_isolation_patch()
        _apply_mcp_resilience_patch()
        _apply_home_channel_suppress()
        _apply_api_server_user_id_patch()

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
