#!/bin/bash

export ANTHROPIC_API_KEY=$(node -e "const o=require('/data/options.json');process.stdout.write(o.anthropic_api_key||'')")
export ALLOWED_ORIGINS=$(node -e "const o=require('/data/options.json');process.stdout.write(o.allowed_origins||'*')")
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"

echo "Starting ProOS MCP Server..."
node /app/server.js
