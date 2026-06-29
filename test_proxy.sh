#!/bin/bash
# Proxy Model Diagnostic — 测中转站支持的模型
# Usage: bash test_proxy.sh

PROXY_URL="${OPENAI_BASE_URL:-https://api.777358.xyz}"
PROXY_KEY="${OPENAI_API_KEY:-}"

if [ -z "$PROXY_KEY" ]; then
  echo "OPENAI_API_KEY is required."
  exit 1
fi

echo "=== 测试中转站: $PROXY_URL ==="
echo ""

# 1. List models
echo "--- /v1/models ---"
curl -s --connect-timeout 10 --max-time 15 \
  "$PROXY_URL/v1/models" \
  -H "Authorization: Bearer $PROXY_KEY" 2>&1 | head -30
echo ""

# 2. Test chat with common models
for MODEL in gpt-4o-mini gpt-4o gpt-3.5-turbo deepseek-chat deepseek-reasoner gpt-5.2; do
  echo "--- /v1/chat/completions ($MODEL) ---"
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 20 \
    "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" 2>&1)
  echo "  HTTP: $CODE"
  [ "$CODE" = "200" ] && echo "  ✅ $MODEL works" || echo "  ❌ $MODEL failed"
  echo ""
done

# 3. Test embedding with common models
for MODEL in text-embedding-3-small text-embedding-ada-002 text-embedding-3-large; do
  echo "--- /v1/embeddings ($MODEL) ---"
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 20 \
    "$PROXY_URL/v1/embeddings" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"input\":\"test\"}" 2>&1)
  echo "  HTTP: $CODE"
  [ "$CODE" = "200" ] && echo "  ✅ $MODEL works" || echo "  ❌ $MODEL failed"
  echo ""
done

echo "=== 诊断完成 ==="
