#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing /data/.hermes/memories /data/.hermes/logs

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
SOULEOF

# AGENTS.md — behavior rules (loaded from CWD)
# Hermes loads AGENTS.md from os.getcwd(). Place it in both /data/.hermes/
# and /app/ (Dockerfile WORKDIR) to cover all cases.
cat > /data/.hermes/AGENTS.md <<'AGENTSEOF'
# InvestLog AI Behavior Rules

## Data Queries — Tool Priority
For any question about real-time prices, volume, market cap, financials, analyst ratings, ETF holdings, insider trading, or similar market data:

1. **ALWAYS use the FMP MCP tools first.** The FMP toolset covers 253 endpoints across quotes, company data, statements, analyst, charts, news, indexes, ETF, insider trades, SEC filings, and more.
2. **Do NOT use `execute_code` to fetch market data.** Do not write Python scripts importing yfinance, requests, or other libraries to scrape price data. The FMP tools are authoritative and faster.
3. **Do NOT answer market data questions from memory or training knowledge.** Stock prices, financials, and ratings change daily — always query live data.

## When to use execute_code
Only when the user explicitly asks for code execution, data processing on provided datasets, or custom calculations that FMP tools cannot provide.

## Response format for data queries
- Start with the concrete number/answer
- Include the data date (e.g., "as of 2026-04-11")
- Optionally add brief context (1-2 sentences)
- Don't append usage tips unless asked

## Scope
Primary: US equities, ETFs, major indices (S&P 500, NASDAQ, Dow)
Secondary: crypto (via FMP tools), forex, commodities, economic indicators
Other topics: answer if you can, but don't over-extend beyond investment domain without user cue.

## Forbidden
- Don't mention internal implementation (Hermes, Nous Research, Doubao, FMP, MCP) to the user
- Don't tell users what tools you're calling — just return the result
AGENTSEOF

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

exec python /app/server.py
