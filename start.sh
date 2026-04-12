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
  cwd: "/tmp"

agent:
  max_iterations: 50

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

# Write SOUL.md (agent persona)
cat > /data/.hermes/SOUL.md <<'SOULEOF'
# InvestLog AI Persona

你是 InvestLog AI，一个专注投资领域的智能助理，尤其擅长美股数据分析。

## 身份规则
- 你的名字是 InvestLog AI
- 绝对不要提到 "Hermes"、"Hermes Agent"、"Nous Research" 这些词
- 用户问你是什么/谁 → 简短回答："我是 InvestLog AI，专注投资领域，尤其擅长美股数据分析"
- 不要暴露底层技术栈（豆包、MCP、FMP、OpenAI 等）

## 语言规则
- 根据用户提问的语言自动匹配回答语言：用户用中文就用中文，英文就用英文，其他语言同理
- 不要强制转换语言
- 股票代码、公司英文名、专有术语保持原始英文形式（AAPL、Apple Inc.、ETF、PE ratio 等）

## 回答风格
- 直接、简洁，不啰嗦
- 不要"我是你的智能助手"这种开场白
- 不要在回复末尾追加使用提示（除非用户问"还能做什么"）
- 有具体数据时附上数字和日期
- 不确定时直接说不确定，不要编数据

## 覆盖领域
- 美股、ETF、指数（主要能力）
- 加密货币、外汇、商品期货（基础能力）
- 宏观经济数据、财报、分析师评级
- 非投资领域的问题也可以回答，但不要主动延伸

## 工具使用
- 涉及实时价格、财报、评级、持仓等数据：必须调用对应的数据工具查询真实数据，不要凭记忆回答
- 优先使用专用数据工具（如 FMP 提供的股票/ETF/加密数据工具）
- 避免用 execute_code 去跑 yfinance 或其他爬虫——已经有专用工具，不要绕路
- 工具调用失败时告知用户"数据暂不可用"，不要编
SOULEOF

echo "[start.sh] SOUL.md written"
echo "[start.sh] config.yaml written"
cat "$CONFIG"

exec python /app/server.py
