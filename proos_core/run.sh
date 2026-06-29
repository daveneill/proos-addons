#!/usr/bin/with-contenv bashio

bashio::log.info "Starting ProOS Core..."
bashio::log.info "Area: $(bashio::config 'area')   auto_heal: $(bashio::config 'auto_heal')"

cd /app
exec python3 server.py
