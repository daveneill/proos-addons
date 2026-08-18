#!/usr/bin/with-contenv bashio

bashio::log.info "Starting ProOS Core..."
bashio::log.info "Area: $(bashio::config 'area')   auto_heal: $(bashio::config 'auto_heal')"

# ProOS security / hardening flags -> environment.
# server.py reads its config from /data/options.json, but the security modules
# (proos/auth.py, consent.py, provision.py) read these from the environment,
# so translate the add-on options into PROOS_* env vars before launch.
if bashio::config.true 'require_auth'; then export PROOS_REQUIRE_AUTH=1; else export PROOS_REQUIRE_AUTH=0; fi
if bashio::config.true 'consent_enforce'; then export PROOS_CONSENT_ENFORCE=1; else export PROOS_CONSENT_ENFORCE=0; fi
if bashio::config.true 'set_hostname'; then export PROOS_SET_HOSTNAME=1; else export PROOS_SET_HOSTNAME=0; fi
if bashio::config.has_value 'ha_direct'; then export PROOS_HA_DIRECT="$(bashio::config 'ha_direct')"; fi
if bashio::config.has_value 'owner_secret'; then export PROOS_OWNER_SECRET="$(bashio::config 'owner_secret')"; fi
if bashio::config.has_value 'installer_password'; then export PROOS_INSTALLER_PW="$(bashio::config 'installer_password')"; fi

bashio::log.info "flags: require_auth=${PROOS_REQUIRE_AUTH} consent_enforce=${PROOS_CONSENT_ENFORCE} set_hostname=${PROOS_SET_HOSTNAME}"

cd /app
exec python3 server.py
