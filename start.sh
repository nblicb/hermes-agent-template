#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing /data/.hermes/memories /data/.hermes/logs

# Install Binance skills (5 skills, query-only, no API key needed)
cp -r /app/skills/* /data/.hermes/skills/ 2>/dev/null || true
echo "[start.sh] Binance skills installed: $(ls /data/.hermes/skills/ | tr '\n' ' ')"

# Binance API note: api.binance.com blocked from US IP (HTTP 451).
# Web3 API (web3.binance.com) works. Only Web3 skills installed.

CONFIG="/data/.hermes/config.yaml"

echo "[start.sh] Writing config.yaml (model + mcp_servers)..."

cat > "$CONFIG" <<EOF
model:
  default: ${LLM_MODEL:-doubao-seed-2-0-mini-260215}
  provider: custom
  base_url: ${OPENAI_API_BASE:-https://ark.cn-beijing.volces.com/api/v3}
  api_key: ${OPENAI_API_KEY}

terminal:
  backend: "local"
  timeout: 60
  cwd: "/data/.hermes"

agent:
  max_iterations: 50
  max_output_tokens: 2000

platform_toolsets:
  telegram: [web, memory]

display:
  platforms:
    telegram:
      tool_progress: "off"

logging:
  level: "DEBUG"

data_dir: "${HERMES_HOME:-/data/.hermes}"
EOF

# FMP official hosted MCP (HTTP mode, URL query auth)
if [ -n "$FMP_API_KEY" ]; then
  cat >> "$CONFIG" <<EOF

mcp_servers:
  fmp:
    url: "https://financialmodelingprep.com/mcp?apikey=${FMP_API_KEY}"
    timeout: 120
EOF
  echo "[start.sh] FMP MCP server configured (official HTTP endpoint)"
fi

# Binance MCP (stdio mode)
if [ -n "$BINANCE_API_KEY" ]; then
  if [ -z "$FMP_API_KEY" ]; then
    echo "" >> "$CONFIG"
    echo "mcp_servers:" >> "$CONFIG"
  fi
  cat >> "$CONFIG" <<EOF
  binance:
    command: "npx"
    args: ["-y", "binance-mcp-server"]
    env:
      BINANCE_API_KEY: "${BINANCE_API_KEY}"
      BINANCE_API_SECRET: "${BINANCE_API_SECRET}"
    timeout: 120
EOF
  echo "[start.sh] Binance MCP server configured"
fi

# SOUL.md — agent identity (loaded from HERMES_HOME, independent of CWD)
cat > /data/.hermes/SOUL.md <<'SOULEOF'
You are InvestLog AI, an investment-focused assistant specializing in US stock market data and analysis.

Match the user's language — respond in Chinese if they write in Chinese, English if English, and so on.

Be direct and concise. Skip fluff openings ("I'm your assistant..."). Lead with the answer, then context if needed. Keep stock tickers, company names, and technical terms in their original English form (AAPL, Apple Inc., ETF, P/E ratio).

Never identify as Hermes, Hermes Agent, or any other framework name. You are InvestLog AI.

When uncertain, say so plainly. Don't fabricate data.

End every response with a brief disclaimer in the user's language:
- Chinese: "以上内容仅供参考，不构成投资建议。"
- English: "This is for informational purposes only, not investment advice."
SOULEOF

# AGENTS.md — behavior rules (loaded from CWD)
# Hermes loads AGENTS.md from os.getcwd(). Place it in both /data/.hermes/
# and /app/ (Dockerfile WORKDIR) to cover all cases.
cat > /data/.hermes/AGENTS.md <<'AGENTSEOF'
# InvestLog AI Behavior Rules

## Data Routing — Which Tool for What
- **US stocks, ETFs, indices, forex, commodities** → Use FMP MCP tools (quote, company, statements, analyst, chart, news, indexes, etc.)
- **Crypto prices and quotes** → Use FMP MCP crypto tools (cryptocurrency quote, chart, list)
- **Crypto token details, rankings, security audit, trading signals** → Use Binance Web3 skills (crypto-market-rank, query-token-info, query-token-audit, trading-signal)
- **Do NOT use `execute_code` to fetch market data.** No yfinance, no requests library. Use the dedicated tools/skills above.
- **Do NOT answer market data questions from memory.** Always query live data.

## Response format
- Start with the concrete number/answer
- Include the data date (e.g., "as of 2026-04-13")
- Brief context (1-2 sentences), don't over-explain
- Don't append usage tips unless asked

## Scope
Primary: US equities, ETFs, major indices
Secondary: cryptocurrency (via Binance skills), forex, commodities, economic indicators
Other topics: answer if you can, but don't over-extend

## Insider / Shareholder Query Completeness
When the user asks about insider trades / 内部交易 / 内部人员交易 / 股东减持 / insider trading / 内部人员 / shareholder activity:

- MUST call BOTH FMP tools (in parallel when possible):
  1. insider-trading — raw source includes directors, officers, AND 10%+ owners
  2. institutional-ownership — 13G/13D holders with 5%+ or 10%+ positions

- Render TWO clearly separated sections and apply STRICT filtering:
  - **董事/高管交易 (Form 4)** — from insider-trading, include ONLY rows where
    typeOfOwner indicates director or officer (e.g. typeOfOwner contains
    "director", "officer", "CEO", "CFO", "CTO", "president", "VP",
    "chairman", "treasurer", "secretary"). Columns: reporter name, title,
    action (buy/sell), shares, price, date.
  - **大股东持仓变动 (13G/13D)** — from institutional-ownership, list 10%+
    institutional holders. Columns: institution name, shares, percentage, date.

- FORBIDDEN:
  - Never put 10%+ owner Form 4 rows (e.g. Magnetar Financial LLC marked as
    "10 percent owner") into the 董事/高管交易 (Form 4) section. 10%+ owner
    activity is shareholder activity, NOT executive insider activity.
  - Never put institutional-ownership (13G/13D) rows into the Form 4 section.
  - Never merge the two sections into one table.

- If a section has no qualifying rows after filtering, state "当前无此类数据"
  (or "No such data currently") explicitly for that section. Never silently
  omit a section.

- If insider-trading returns ONLY 10%+ owner rows (no directors/officers),
  the Form 4 section must say "当前无此类数据" — do NOT fall back to
  showing the 10%+ owner rows there. You may optionally add a short note
  above 13G section: "注：本期仅见 10%+ 股东 Form 4 申报，详见下方大股东
  持仓变动" — but the Form 4 section itself stays empty.

## Ticker Reference Prefix
If a message starts with "(ref: TICKER=Name; ...)", this is our authoritative ticker map — trust it over training memory. Do not echo it in responses.

## Forbidden
- Don't mention internal implementation (Hermes, Nous Research, Doubao, FMP, MCP, Binance API) to the user
- Don't tell users what tools or skills you're calling — just return the result
AGENTSEOF

# Rate limiting hook (mirrors V2 bot/rate_limit.py)
mkdir -p /data/.hermes/hooks/rate_limit
cat > /data/.hermes/hooks/rate_limit/HOOK.yaml <<'HOOKYAML'
name: rate_limit
description: Per-user rate limiting, daily quota, auto-ban (mirrors V2 bot protections)
events:
  - agent:start
HOOKYAML

cat > /data/.hermes/hooks/rate_limit/handler.py <<'HOOKPY'
"""Rate limiting hook — fires on every agent:start event.

Protections (same as V2 bot/rate_limit.py):
1. Per-user cooldown: 15s between requests
2. Auto-ban: 5 consecutive violations → 1 hour ban
3. Input length: max 500 chars
4. Global rate limit: 30 req/min
5. Daily quota: 20 queries/user/day

In-memory state resets on deploy. Acceptable for single-instance Railway.
"""
import time
from collections import deque

_user_last = {}        # user_id → monotonic timestamp
_strike_count = {}     # user_id → consecutive violations
_banned_until = {}     # user_id → monotonic ban expiry
_global_ts = deque()   # sliding window timestamps
_daily_count = {}      # (user_id, date_str) → count

COOLDOWN = 15.0
BAN_STRIKES = 5
BAN_DURATION = 3600.0
MAX_INPUT = 500
GLOBAL_RPM = 30
DAILY_QUOTA = 20


def _record_strike(uid):
    count = _strike_count.get(uid, 0) + 1
    _strike_count[uid] = count
    if count >= BAN_STRIKES:
        _banned_until[uid] = time.monotonic() + BAN_DURATION
        print(f"[rate_limit] user {uid} auto-banned for 1h after {count} strikes")


def handle(event_type, context):
    """Synchronous hook handler. Raise Exception to block the request."""
    if event_type != "agent:start":
        return

    uid = context.get("user_id", "")
    msg = context.get("message", "")
    now = time.monotonic()

    # 1. Ban check
    ban_until = _banned_until.get(uid)
    if ban_until and now < ban_until:
        raise Exception("Too many violations. Try again in 1 hour.")
    elif ban_until:
        del _banned_until[uid]
        _strike_count.pop(uid, None)

    # 2. Input length
    if len(msg) > MAX_INPUT:
        raise Exception(f"Message too long (max {MAX_INPUT} chars).")

    # 3. Global rate limit
    while _global_ts and _global_ts[0] < now - 60:
        _global_ts.popleft()
    if len(_global_ts) >= GLOBAL_RPM:
        raise Exception("System busy, please try again shortly.")
    _global_ts.append(now)

    # 4. Per-user cooldown
    last = _user_last.get(uid, 0)
    if now - last < COOLDOWN:
        _record_strike(uid)
        raise Exception("Please wait before sending another message.")
    _user_last[uid] = now
    _strike_count.pop(uid, None)

    # 5. Daily quota
    import datetime
    today = datetime.date.today().isoformat()
    key = (uid, today)
    count = _daily_count.get(key, 0)
    if count >= DAILY_QUOTA:
        raise Exception(f"Daily limit reached ({DAILY_QUOTA}/day). Try again tomorrow.")
    _daily_count[key] = count + 1

    # Cleanup old daily entries
    for k in list(_daily_count):
        if k[1] != today:
            del _daily_count[k]
HOOKPY

echo "[start.sh] Rate limit hook installed"

# Clear global memory files — per-user isolation patch writes to user_xxx/ dirs
# This prevents stale global MEMORY.md/USER.md from leaking across users
if [ -f /data/.hermes/memories/MEMORY.md ] || [ -f /data/.hermes/memories/USER.md ]; then
  rm -f /data/.hermes/memories/MEMORY.md /data/.hermes/memories/USER.md
  echo "[start.sh] Cleared global MEMORY.md/USER.md (per-user isolation active)"
fi

echo "[start.sh] SOUL.md + AGENTS.md written (gateway CWD=/data/.hermes)"

# Diagnostic: verify files exist and content is correct
echo "[start.sh] === DIAGNOSTIC ==="
echo "[start.sh] ls /data/.hermes/:"
ls -la /data/.hermes/SOUL.md /data/.hermes/AGENTS.md 2>&1
echo "[start.sh] SOUL.md first line:"
head -1 /data/.hermes/SOUL.md
echo "[start.sh] AGENTS.md first line:"
head -1 /data/.hermes/AGENTS.md
echo "[start.sh] config.yaml:"
cat "$CONFIG"
echo "[start.sh] === END DIAGNOSTIC ==="

# Set Telegram bot menu commands (runs once at boot)
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  curl -s --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" \
    -H "Content-Type: application/json" \
    -d '{"commands":[{"command":"help","description":"Show all commands"},{"command":"watch","description":"Manage watchlist"},{"command":"alert","description":"Price alerts"},{"command":"usage","description":"Daily quota"},{"command":"pro","description":"Upgrade to Pro"},{"command":"notify","description":"Push settings"},{"command":"new","description":"New conversation"}]}' > /dev/null 2>&1
  echo "[start.sh] Telegram menu commands set"
else
  # Read from admin config
  TGTOKEN=$(grep TELEGRAM_BOT_TOKEN /data/.hermes/.env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
  if [ -n "$TGTOKEN" ]; then
    curl -s --max-time 10 "https://api.telegram.org/bot${TGTOKEN}/setMyCommands" \
      -H "Content-Type: application/json" \
      -d '{"commands":[{"command":"help","description":"Show all commands"},{"command":"watch","description":"Manage watchlist"},{"command":"alert","description":"Price alerts"},{"command":"usage","description":"Daily quota"},{"command":"pro","description":"Upgrade to Pro"},{"command":"notify","description":"Push settings"},{"command":"new","description":"New conversation"}]}' > /dev/null 2>&1
    echo "[start.sh] Telegram menu commands set (from .env)"
  fi
fi

exec python /app/server.py
