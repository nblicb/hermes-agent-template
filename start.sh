#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing

# Write MCP servers config to config.yaml if FMP_API_KEY is set
CONFIG="/data/.hermes/config.yaml"
if [ -n "$FMP_API_KEY" ]; then
  echo "Writing MCP config for FMP..."
  cat > "$CONFIG" <<EOF
mcp_servers:
  fmp:
    command: "npx"
    args: ["-y", "financial-modeling-prep-mcp-server"]
    env:
      FMP_API_KEY: "$FMP_API_KEY"
    timeout: 120
EOF
  # Add Binance MCP if BINANCE_API_KEY is set
  if [ -n "$BINANCE_API_KEY" ]; then
    cat >> "$CONFIG" <<EOF
  binance:
    command: "npx"
    args: ["-y", "binance-mcp-server"]
    env:
      BINANCE_API_KEY: "$BINANCE_API_KEY"
      BINANCE_API_SECRET: "$BINANCE_API_SECRET"
    timeout: 120
EOF
  fi
fi

exec python /app/server.py
