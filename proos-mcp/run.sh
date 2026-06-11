#!/usr/bin/with-contenv bashio

export ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')
export ALLOWED_ORIGINS=$(bashio::config 'allowed_origins')
export LOG_LEVEL=$(bashio::config 'log_level')
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"

bashio::log.info "Starting ProOS MCP Server..."
node /app/server.js
