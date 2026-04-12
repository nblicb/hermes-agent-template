#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing

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
  cwd: "/tmp"

agent:
  max_iterations: 50

data_dir: "${HERMES_HOME:-/data/.hermes}"
EOF

# FMP official hosted MCP (HTTP mode, header auth)
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

echo "[start.sh] config.yaml written"
cat "$CONFIG"

exec python /app/server.py
