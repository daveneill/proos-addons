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

# ── `ha` CLI shim for the Tech Tools terminal ────────────────────────────────
# The container has no `ha` binary, so `ha store reload` / `ha addons update …`
# fail. This add-on already has hassio_role:manager + a SUPERVISOR_TOKEN, so we
# drop in a small `ha` that forwards to the SAME Supervisor REST endpoints the
# real CLI uses. Makes the ProOS terminal behave like native HA's for the
# operations a tech needs (store, addons, core, supervisor, host, os).
cat > /usr/bin/ha <<'HA_SHIM'
#!/bin/sh
SUP="http://supervisor"; TOK="${SUPERVISOR_TOKEN}"
[ -z "$TOK" ] && { echo "ha: no SUPERVISOR_TOKEN in this container"; exit 1; }
_call() { # METHOD PATH [JSONBODY]
  if [ -n "$3" ]; then
    curl -sS -X "$1" -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -d "$3" "$SUP$2"
  else
    curl -sS -X "$1" -H "Authorization: Bearer $TOK" "$SUP$2"
  fi; echo
}
dom="$1"; verb="$2"; slug="$3"
case "$dom" in
  ""|info) _call GET /info ;;
  store)
    case "$verb" in
      reload) _call POST /store/reload ;;
      add|add-repository) _call POST /store/repositories "{\"repository\":\"$slug\"}" ;;
      ""|list) _call GET /store ;;
      *) _call GET "/store/$verb" ;;
    esac ;;
  addons|addon)
    case "$verb" in
      ""|list) _call GET /addons ;;
      reload) _call POST /addons/reload ;;
      update|restart|rebuild|start|stop|install|uninstall)
        [ -z "$slug" ] && { echo "usage: ha addons $verb <slug>"; exit 2; }
        _call POST "/addons/$slug/$verb" ;;
      info|stats|logs|changelog|options)
        [ -z "$slug" ] && { echo "usage: ha addons $verb <slug>"; exit 2; }
        _call GET "/addons/$slug/$verb" ;;
      *) echo "ha addons: unsupported '$verb'"; exit 2 ;;
    esac ;;
  core|supervisor|host|os|network|hardware|resolution|jobs)
    case "$verb" in
      ""|info|logs|stats|options) _call GET "/$dom/${verb:-info}" ;;
      *) _call POST "/$dom/$verb" ;;
    esac ;;
  *)
    echo "ha: '$dom' not supported by the ProOS shim."
    echo "Supported: store | addons | core | supervisor | host | os | network | info"
    echo "Anything else: curl -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" http://supervisor/<path>"
    exit 2 ;;
esac
HA_SHIM
chmod +x /usr/bin/ha
bashio::log.info "ha CLI shim installed (/usr/bin/ha -> Supervisor API)"

cd /app
exec python3 server.py
