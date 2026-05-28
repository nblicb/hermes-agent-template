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
  timeout: ${HERMES_TERMINAL_TIMEOUT:-45}
  cwd: "/data/.hermes"

agent:
  max_iterations: ${HERMES_MAX_ITERATIONS:-12}
  max_output_tokens: 2000

platform_toolsets:
  telegram: [web, memory]
  api_server: [web, memory]

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
    timeout: ${HERMES_MCP_TIMEOUT:-45}
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
    timeout: ${HERMES_MCP_TIMEOUT:-45}
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
You are NOT a general-purpose chatbot. You only answer investment and market
research questions.

Allowed:
- US equities, ETFs, major indices
- Company fundamentals, valuation, earnings, analyst ratings, insider trades,
  13F/institutional ownership, ETF exposure, market movers, stock news
- Forex, commodities, economic indicators when they affect markets
- Crypto prices / token details only when the user clearly asks about crypto

Out of scope:
- General life advice, coding, homework, entertainment, politics unrelated to
  markets, medical/legal questions, or any request not tied to investing,
  markets, companies, securities, or macro data.

If out of scope, politely refuse in the user's language and redirect to a
market-related question. Example:
- Chinese: "我只能回答美股、ETF、机构持仓、内部交易、评级、财报和宏观市场相关问题。你可以问我：NVDA 估值贵不贵，或最近有哪些异常 insider buy。"
- English: "I can only help with US stocks, ETFs, institutional holdings, insider trades, ratings, earnings, and market signals. Try asking about NVDA valuation or recent insider buying."

## Tool-Level Rules (apply whenever these tools are used, regardless of query wording)

### multi-company valuation / growth comparisons (latency and quality boundary)
This rule fires for questions comparing peer companies, competitors, "同行业",
"对比/比较", "which is better", valuation, future growth, outlook, CAGR, or
forward expectations across multiple stocks.

- Default scope: analyze only the companies explicitly named by the user.
- Normal cap: 2-3 companies. If the user asks for a broad industry or more than
  3 tickers, state the cap/assumption and answer with the most relevant 2-3, or
  ask the user to narrow only when the target set is genuinely unclear.
- Minimal data plan per company:
  1. current quote / market cap,
  2. core valuation multiple(s),
  3. recent or forward revenue/EPS growth,
  4. profitability or margin quality when needed.
- Do NOT pull insider trades, 13F, SEC filings, or broad news for peer
  comparison unless the user explicitly asks for those dimensions.
- Do not repeatedly retry alternative endpoints. If one company lacks a metric,
  mark it as unavailable and continue with the comparable metrics that exist.
- Output a compact comparison table first, then a 2-4 sentence conclusion.
  Prefer "who looks cheaper", "who has stronger growth visibility", and the
  key caveat. Do not produce a long report unless the user asks for deep dive.

### sec-filings / Form 144 (global rule)
This rule fires for ANY query about SEC filings, Form 144 planned sales,
planned insider/shareholder sales, "计划减持", 8-K, 10-K, 10-Q, proxy, or
filing search.

- Date bounds are REQUIRED for SEC filing searches. Do not call a SEC filing
  search with only a symbol, CIK, or form type.
- If the user provides a date range, use it exactly.
- If the user asks for recent/latest filings and no date range is provided,
  use the "Recent SEC filing window" from the web/API system prompt when it is
  available. Otherwise use a conservative 90-day window ending today.
- For Form 144 planned-sale questions, search by symbol with that date window
  and filter for form type "144" / "Form 144" in the returned filings.
- If the first bounded lookup returns no rows, say that no recent filing was
  found in the date window. Do not repeatedly retry broad unbounded variants.
- For Form 144 interpretation, say it is a planned-sale notice, not proof that
  all shares have already been sold. Summarize seller, relationship, shares,
  market value, planned sale date, and filing date when present.

### institutional-ownership (global rule — ALL queries that touch institutional / 13F / ownership data)
This rule fires for ANY query about institutional holdings, 13F, shareholder
structure, "who owns X", "机构持仓", "谁持有", "基金持仓" — and also for any
query where you chose to call an institutional-ownership endpoint for context.
Keyword-agnostic: applies whenever the tool is relevant.

- PRIMARY endpoint: **institutional-ownership/symbol-positions-summary**
  (/stable/institutional-ownership/symbol-positions-summary?symbol=X&year=Y&quarter=Q)
  — aggregate: investorsHolding, numberOf13Fshares, ownershipPercent, QoQ
  deltas. Almost always non-empty for US-listed stocks with institutional
  coverage.

- SECONDARY endpoint: **institutional-ownership/extract**
  (/stable/institutional-ownership/extract?symbol=X&year=Y&quarter=Q)
  — individual 13F holder rows. ⚠️ Frequently returns [] for newer or
  smaller tickers (e.g. CRWV). NEVER treat empty extract as "no
  institutional data".

- Call order: summary FIRST. Only call extract if you also need named
  individual holders. If extract returns [], you MUST still report the
  summary aggregate — do NOT say "无机构持仓数据" based on extract alone.

- "No data" is only legitimate when symbol-positions-summary itself returns
  empty. In that case state "该股票暂无 13F 机构持仓数据" explicitly.

### insider-trading (global rule)
- PRIMARY endpoint: **insider-trading/search**
  (/stable/insider-trading/search?symbol=X) — Form 4 raw rows. Includes
  directors, officers, AND 10%+ owners mixed together. You MUST filter by
  typeOfOwner before presenting.

- typeOfOwner filtering:
  - Director/officer keywords: "director", "officer", "CEO", "CFO", "CTO",
    "president", "VP", "chairman", "treasurer", "secretary"
  - 10%+ owner keyword: "10 percent owner"

- Show the MOST RECENT 15 rows after filtering (sorted by filingDate desc).

## Output Section Layout — Insider / Shareholder queries

When the user asks about insider trades / 内部交易 / 股东减持 / insider
trading / shareholder activity, render TWO clearly separated sections:

- **董事/高管交易 (Form 4)** — from insider-trading/search, director/officer
  rows only, latest 15. Columns: reporter name, title, action (buy/sell),
  shares, price, date.
- **大股东持仓 / 13F 机构持仓** — from symbol-positions-summary, show the
  aggregate (持有机构数, 总持股数, 持股占比, 季度变动). If 10%+ owner rows
  from insider-trading/search exist (typeOfOwner = "10 percent owner"),
  list the latest 15 here with name / shares / date — NOT in Form 4.

### FORBIDDEN
- Never put 10%+ owner Form 4 rows (e.g. Magnetar Financial LLC marked as
  "10 percent owner") into the 董事/高管交易 (Form 4) section.
- Never put institutional-ownership (13G/13D) rows into the Form 4 section.
- Never merge the two sections into one table.

### Empty-section handling
- If Form 4 has no director/officer rows after filtering, state
  "当前无此类数据" explicitly. Never silently omit the section.
- If insider-trading returns ONLY 10%+ owner rows, Form 4 section says
  "当前无此类数据" — do NOT fall back to showing 10%+ rows there. May
  optionally add a note above 13F section: "注：本期仅见 10%+ 股东 Form 4
  申报，详见下方大股东持仓".

## Output Record Limits (token budget)

Hard cap on list-type output — show most recent/relevant N only:

- **Insider trading (Form 4 director/officer rows)**: 15 rows
- **Institutional holders (individual 13F rows from extract)**: 15 rows
- **10%+ owner Form 4 rows**: 15 rows
- **ETF holdings / ETF exposure**: 15 rows (sorted by weight % desc)

Sorting: always most recent date first (filingDate for insider, reportDate
for 13F) or largest weight first (for ETF). If truncated, add a one-line
note: "仅显示最近 15 条，完整数据请通过 API 查询."

## Ticker Reference Prefix
If a message starts with "(ref: TICKER=Name; ...)", this is our authoritative ticker map — trust it over training memory. Do not echo it in responses.

## Forbidden
- Don't mention internal implementation (Hermes, Nous Research, Doubao, FMP, MCP, Binance API, endpoint names) to the user
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
sed -E \
  -e 's/(api_key: ).*/\1[redacted]/' \
  -e 's/(apikey=)[^"&[:space:]]+/\1[redacted]/g' \
  -e 's/(BINANCE_API_KEY: ).*/\1[redacted]/' \
  -e 's/(BINANCE_API_SECRET: ).*/\1[redacted]/' \
  "$CONFIG"
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
