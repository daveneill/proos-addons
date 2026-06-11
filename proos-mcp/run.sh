#!/bin/bash

export ANTHROPIC_API_KEY=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin).get('anthropic_api_key',''))")
export ALLOWED_ORIGINS=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin).get('allowed_origins','*'))")
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"

echo "Starting ProOS MCP Server..."
node /app/server.js
