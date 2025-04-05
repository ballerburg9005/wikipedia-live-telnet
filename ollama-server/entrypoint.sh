#!/bin/bash
set -e

echo "[ENTRYPOINT] Reading config..."

OLLAMA_URI=$(awk -F= '/^ollama_uri/ {print $2}' /app/server.cfg | xargs)
MODEL=$(awk -F= '/^model/ {print $2}' /app/server.cfg | xargs)

echo "[ENTRYPOINT] ollama_uri=${OLLAMA_URI}"
echo "[ENTRYPOINT] model=${MODEL}"

if echo "$OLLAMA_URI" | grep -q "localhost"; then
    echo "[ENTRYPOINT] Starting Ollama in background..."
    nohup ollama serve >/dev/null 2>&1 &

    {
    sleep 12  # give it a moment to start
    echo "[ENTRYPOINT] Detected localhost, pulling model: ${MODEL}"
    ollama pull "$MODEL"
    }&

else
    echo "[ENTRYPOINT] Remote ollama_uri, skipping model pull/start"
fi

echo "[ENTRYPOINT] Starting Python AI server..."
exec python3 /app/ollama_ai_server.py

