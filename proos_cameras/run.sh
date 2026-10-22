#!/usr/bin/with-contenv bashio
bashio::log.info "Starting ProOS Cameras..."
cd /app
exec python3 -u cameras.py
