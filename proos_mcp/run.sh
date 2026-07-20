#!/bin/bash

export ANTHROPIC_API_KEY=$(node -e "const o=require('/data/options.json');process.stdout.write(o.anthropic_api_key||'')")
export ALLOWED_ORIGINS=$(node -e "const o=require('/data/options.json');process.stdout.write(o.allowed_origins||'*')")
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"
export PROOS_CORE_URL=$(node -e "const o=require('/data/options.json');process.stdout.write(o.proos_core_url||'http://b333b432-proos-core:8770')")
export PROOS_MCP_REQUIRE_AUTH=$(node -e "const o=require('/data/options.json');process.stdout.write(o.require_auth?'1':'0')")

echo "Starting ProOS MCP Server..."
node /app/server.js
