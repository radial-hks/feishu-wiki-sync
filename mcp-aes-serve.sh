#!/bin/bash
# Aes 知识库图谱 MCP 服务（局域网可访问）
# graphify.serve: Streamable HTTP, 端口 8791, api-key 鉴权
set -euo pipefail

GRAPH="/home/radial/wiki/raw/51Aes - 工程与交付知识库/graphify-out/graph.json"
KEY_FILE="/home/radial/.hermes/.env"
PORT=8791

# api-key 从 hermes .env 取（也可用 GRAPHIFY_API_KEY 环境变量）
API_KEY=$(grep '^GRAPHIFY_MCP_KEY=' "$KEY_FILE" | cut -d= -f2 || true)
if [ -z "$API_KEY" ]; then
    API_KEY=$(openssl rand -hex 24)
    echo "GRAPHIFY_MCP_KEY=$API_KEY" >> "$KEY_FILE"
    echo "[mcp-aes] 已生成并写入 GRAPHIFY_MCP_KEY 到 $KEY_FILE"
fi

exec ~/.local/share/uv/tools/graphifyy/bin/python -m graphify.serve "$GRAPH" \
    --transport http --host 0.0.0.0 --port "$PORT" \
    --api-key "$API_KEY" --stateless \
    --session-timeout 0