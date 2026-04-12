#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing

# Append mcp_servers to config.yaml without overwriting existing content.
# If config.yaml exists (written by admin API), preserve it; only replace mcp_servers section.
CONFIG="/data/.hermes/config.yaml"
if [ -n "$FMP_API_KEY" ]; then
  echo "[start.sh] Configuring MCP servers (FMP)..."
  python3 -c "
import yaml, os

config_path = '$CONFIG'
config = {}
if os.path.exists(config_path):
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

# Build mcp_servers section
mcp = {}
mcp['fmp'] = {
    'command': 'npx',
    'args': ['-y', 'financial-modeling-prep-mcp-server'],
    'env': {'FMP_API_KEY': os.environ['FMP_API_KEY']},
    'timeout': 120,
}

binance_key = os.environ.get('BINANCE_API_KEY', '')
if binance_key:
    mcp['binance'] = {
        'command': 'npx',
        'args': ['-y', 'binance-mcp-server'],
        'env': {
            'BINANCE_API_KEY': binance_key,
            'BINANCE_API_SECRET': os.environ.get('BINANCE_API_SECRET', ''),
        },
        'timeout': 120,
    }

config['mcp_servers'] = mcp

with open(config_path, 'w') as f:
    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

print(f'[start.sh] config.yaml: {len(config)} top-level keys, {len(mcp)} MCP servers')
"
fi

exec python /app/server.py
