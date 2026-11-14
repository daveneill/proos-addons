"""Factory-reset residue bench — run: python3 tests/reset_residue_bench.py

Dave's spec (4 Aug 2026): "a factory reset is exactly that — the only things
remaining were the shipped essentials." Every file ProOS writes to /data is
either PER-SITE (must be cleared, or the old home's entity ids land in the
fresh commission) or FLEET/PRODUCT (must survive). This bench reads the
shipped server.py and asserts the wipe list covers every per-site store, so
adding a new store without adding it to the reset fails HERE, not on a
customer's rebuilt home.

Residue history this pins: MA user table (24 Jul), net_witnesses /
music_speakers / health_state (4 Aug — surviving an unticked reset).
"""
import os
import re
import sys

_HERE = os.path.dirname(__file__)
SRC = open(os.path.join(_HERE, "..", "server.py"), encoding="utf-8").read()

PASS, FAIL = 0, []


def check(name, got, want):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


# Per-site stores: keyed to THIS home's entities/users. Must be cleared.
PER_SITE = {
    "knowledge_site.json": "the installer's notes about THIS house "
                           "(register 226) — the PRODUCT's knowledge ships "
                           "in the image and is untouched by a reset; the "
                           "site's own record goes with the commission",
    # mcp_conn.json (register 218) and ma_conn.json are GONE (register 439):
    # where the MCP server and ProOS Music are is the Supervisor's / the
    # platform's own record, read on every use; nothing is written down.
    "voice.json": "the product's chosen speech engine + default voice "
                  "(register 216) — names tts entities the reset deletes, "
                  "so a fresh home chooses afresh",
    "net_witnesses.json": "installer-committed traffic-witness map",
    "music_speakers.json": "curated MA room-speaker allowlist",
    "health_state.json": "health/incident state",
    "device_ports.json": "where each device was last seen plugged in — site "
                         "data, and it names entity ids the reset deletes",
    # register 940: the wall panel's set-up record and the screen login it made — the reset deletes the platform's
    # users, so the login it names is gone; the next pass makes a new one
    "panel_setup.json": "where the wall panel's own set-up got to (register 940)",
    "panel_login.json": "the screen login the panel made (register 940) — its user goes with the reset",
    "panel_wake.json": "the panel's wake word as a person chose it (register 980) — goes with the reset; a fresh box has no choice",
    "kiosk_rest.json": "the key to the kiosk's internal door (register 963) — the next pass makes a new one and gives it to the kiosk",
    "remotes.json": "the ProOS Remotes this home accepted (register 969j) — each shows a new code after a reset",
    "remote_logins.json": "the logins Core made for the remotes (register 969j) — their users go with the reset",
    "connect.json": "the box's ProOS Connect id and secret (register 969u) — a reset box asks to join afresh and is approved again",
    "connect_policy.json": "ProOS Connect's last word on the fleet's AI and its keys (register 1347) — a reset box asks Connect again within minutes",
    "net_pending.json": "a network change waiting for Keep These Settings (register 969z) — the reset starts with none",
    "weather_std.json": "where Core put the weather service (register 970a) — a reset box sets it up afresh at its new location",
    "navconfig.json": "published nav layout",
    "navcaps.json": "per-room page capabilities",
    "install_id": "install epoch (browsers purge on change)",
    "ma_conn.json": "swept only — retired by register 439; an older build's file",
    "ma_admin.json": "Music Assistant admin identity",
    "assist_flags.json": "open Assist→Pro flags (name the old home's devices)",
    "savant.json": "the Savant bridge switch (register 630) — this site's "
                   "commissioning; the feed closes with it",
    # Register 143 — the fourteen that survived an unticked reset, found the
    # day before a rebuild once the coverage guard could actually see.
    "music_genres.json": "curated genre picks",
    "music_order.json": "the home's music service order, set in Pro (register 896)",
    "consent.json": "recorded consent",
    "shortcuts.json": "room shortcuts (stale entity ids)",
    "watch_learned.json": "what this home learned about starting shows, per device (register 970oo — stale entity ids)",
    "account_copies.json": "the box's one-way copies of this home's ProOS accounts (register 970rr) — a reset box must not let the old home's people in",
    "garage_doors.json": "which pieces make this home's garage doors and gates (register 974) — this home's entity ids; the platform's covers go with the reset",
    "room_order.json": "room ordering (stale rooms)",
    "layouts.json": "dashboard layouts (register 960 — per-site, pages name this home's rooms)",
    "channels.json": "channel presets",
    "channel_lists.json": "the house's channel lists (register 1049) — this home's TVs and confirmations",
    "cast_links.json": "which Cast player is which Shield, linked by the installer (register 1059) — this home's entity ids",
    "atv_apps.json": "swept only — retired by register 442 (the platform's entry holds a box's apps)",
    "display_apps.json": "per-display app lists",
    "assist_scene_music.json": "scene→music bindings",
    "appart_brands.json": "locally-added app brands (site work, not shipped)",
    "appart_hidden.json": "installer's hidden app tiles",
    "appart_origin.json": "per-app artwork origin",
    "appart_removed.json": "removed app tiles",
    "proactive.json": "proactive notify state",
    "unifi_curation.json": "UniFi device curation",
    "tech_users.json": "ProOS tech-user list (HA users are wiped)",
    "installer_state.json": "installer progress",
    "provisioned.json": "provisioning marker — a fresh box must re-provision",
    "terminal_audit.log": "terminal command history",
    "sweep_last.json": "the old home's last full sweep (register 663)",
    "sweep_config.json": "the sweep's daily time (register 663)",
    "auto_full_backup.json": "the old home's last automatic full backup time (register 1024)",
    "platform_log.json": "the old home's integration log mirror (register 1027)",
    "notify_devices.json": "the old home's per-device notification switches (register 1022)",
    "platform_faults_ack.json": "the old home's acknowledged platform fault cards (register 1014)",
    "platform_faults_seen.json": "when the old home's platform faults were first seen (register 1255)",
    "sweep_acted.json": "when each integration was last repaired by Pro's Pair (register 665)",
    "port_history.json": "per-device switch-port history and the household's answers (plan A3)",
    "push_status.json": "the last alert outcome per phone (plan B1)",
    "apple_music_token.json": "the installer-pasted Apple Music token (register 759) — cleared like the AI key",
    "speaker_links.json": "ProOS's own music-service sign-ins (register 778) — the old home's accounts",
    "speaker_addresses.json": "the address each speaker last answered at (register 799) — the old home's speakers",
    "platform_update.json": "the old home's last platform/OS update and its checks (register 813)",
    "apple_music_people.json": "each person's own Apple Music sign-in (register 843) — the old home's people",
}
# Cleared through their module's own clear()/clear_data(), not the name list.
VIA_MODULE = {
    "_assist.clear_data()": "Assist provider config + key",
    "_scenephotos.clear()": "scene photos",
    "_habits_mod.clear()": "the nightly habits file (register 430)",
    "_sugg_mod.clear()": "what people answered to offers (register 431)",
    "_standing_mod.clear(_client)": "what people asked twice, and the automations it wrote (register 1316)",
    "_roomdevices.clear()": "room device records",
    "_appfavs.clear()": "house-wide app favourites",
    "_aiusage_mod.clear()": "the realtime voice's cost ledger (register 1151)",
    "_portseen_mod.clear()": "where ProOS last saw this house's devices on its switches (register 1222)",
    "_alarmtrig_mod.triggers_clear()": "this site's Alarm Manager webhook triggers (register 1224)",
    "project.clear()": "the commissioning project",
    # Register 115 — Dave: "a factory reset should be clearing all this
    # detail." The journal survived every reset because it is a DIRECTORY of
    # .jsonl files, invisible to the /data *.json wipe AND to this bench's
    # own coverage guard (which only saw literal "/data/*.json" strings).
    "_journal_mod.clear()": "the event journal — the old home's diary",
}
# Fleet/product state: correct to SURVIVE an unticked reset.
SURVIVES = {
    "catalog_published.json": "Protech integration catalog publish",
    "recovery_config.json": "recovery/restore pointer",
    "integration_catalog.json": "the fleet catalog — product, not site",
}
# SHIPPED WITH THE IMAGE, not /data at all: bundled reference data the widened
# guard now also sees. Touching any of these would break the product itself.
BUNDLED = {"aliases.json", "domains.json", "packages.json",
           "atv_seed.json", "channel_presets.json", "config.json",
           # register 813: the platform/OS versions THIS Core approves — shipped in the zip, never in /data
           "approved_versions.json",
           # register 1049: ProOS's starting channel lists — shipped in the zip beside channels.py, never in /data
           "channel_starts.json"}

# The factory_reset body only (not the whole file).
_start = SRC.index("def factory_reset(")
_end = SRC.index("\ndef ", _start + 10)
BODY = SRC[_start:_end]

for fname, what in PER_SITE.items():
    check("cleared: %-22s (%s)" % (fname, what),
          ('"%s"' % fname) in BODY, True)

for call, what in VIA_MODULE.items():
    check("cleared via module: %-20s (%s)" % (call, what),
          call in BODY, True)

for fname, what in SURVIVES.items():
    check("survives: %-21s (%s)" % (fname, what),
          ('"%s"' % fname) in BODY, False)

# The nuclear option still exists and still globs everything.
check("clear_proos_data still wipes every /data json",
      bool(re.search(r"if clear_proos_data:[\s\S]{0,400}glob\(", BODY)), True)

# COVERAGE GUARD: every /data store referenced anywhere in the codebase is
# classified above. A new store added without a decision fails here.
# THE GUARD WAS BLIND (register 143, found 14 Aug while Dave was preparing a
# factory reset). root was "<here>/.." so EVERY dirpath began "tests/.." — and
# the filter `if "tests" in dirpath` therefore skipped every directory,
# including proos/. `found` was always EMPTY, so "every /data store is
# classified" passed on nothing, from the moment register 115 wrote it. An
# instrument that cannot answer the question, silently answering "all clear" —
# inside the very fix for that fault. Normalised, and filtered on the
# DIRECTORY NAME, never on the path.
root = os.path.abspath(os.path.join(_HERE, ".."))
found = set()
for dirpath, _dirs, files in os.walk(root):
    _base = os.path.basename(dirpath)
    if _base in ("tests", "__pycache__"):
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        txt = open(os.path.join(dirpath, fn), encoding="utf-8",
                   errors="ignore").read()
        # ANY quoted store name in a file that touches /data. The narrower
        # patterns below missed music_genres.json (built via mastore.path),
        # installer_state.json and provisioned.json (a different join shape)
        # and every .log. Over-collecting is the safe direction: it forces a
        # decision, which is the whole point of a coverage guard.
        if "/data" in txt or "PROOS_DATA_DIR" in txt:
            found.update(re.findall(r'"([a-z_]+\.(?:json|log))"', txt))
        found.update(re.findall(r'"/data/([a-z_]+\.json)"', txt))
        # Register 115: the guard's original regex only saw literal
        # "/data/*.json" strings — the journal (a DIRECTORY of .jsonl),
        # every store built via os.path.join(PROOS_DATA_DIR, ...) and .log
        # files were all invisible, and the journal survived factory reset
        # onto Dave's fresh build. The guard now sees every shape.
        found.update(re.findall(r'PROOS_DATA_DIR", "/data"\), "([a-z_\.]+\.json)"', txt))
        found.update(re.findall(r'"/data/([a-z_]+\.log)"', txt))
        if '"/data/journal"' in txt:
            found.add("journal/")
# Stores that are neither per-site nor fleet by nature (runtime plumbing).
IGNORE = {"options.json",                    # Supervisor writes it
          "credentials.json",                # auth wipe + re-provision owns it
          "proos_installer_tokens.json",     # ditto
          "proos_project.json"}              # cleared via project.clear()
# Cleared through a module clear() pinned in VIA_MODULE above, or by a
# literal path pinned below — classified, just not by filename in BODY.
MODULE_FILES = {"assist.json",               # _assist.clear_data()
                "assist_memory.json",        # _assist.clear_data()
                "assist_sessions.json",      # _assist.clear_data() — register 426;
                                             # this bench refused the build until
                                             # the new store was classified, which
                                             # is exactly its job
                                             # (assist_sessions_persist_bench proves
                                             # the clear)
                "journal/",                  # _journal_mod.clear()
                "assist_history/",           # _assist.clear_data() — register 427
                "habits.json",               # habits.clear() — register 430
                "suggestions.json",          # suggestions.clear() — register 431
                "standing.json",             # standing.clear(_client) — register 1316
                "room_devices.json",         # _roomdevices.clear()
                "voice_rule_state.json",     # project.clear() — register 1146 (the voice rule outcomes ProOS applied)
                "ai_usage.json",             # _aiusage_mod.clear() — register 1151
                "assist_autofix.json",       # _autofix_mod.clear() — register 1164
                "unifi_port_seen.json",      # _portseen_mod.clear() — register 1222 (devices' last-seen switch ports)
                "unifi_alarm_triggers.json", # _alarmtrig_mod.triggers_clear() — register 1224 (Alarm Manager triggers)
                "scene_photos.json",         # _scenephotos.clear()
                "appfavs.json"}              # _appfavs.clear()
check("cleared: %-22s (%s)" % ("watcher_audit.log", "recovery/awareness history"),
      '"/data/watcher_audit.log"' in BODY, True)
unclassified = sorted(found - set(PER_SITE) - set(SURVIVES) - IGNORE
                      - MODULE_FILES - BUNDLED - {"watcher_audit.log"})
check("every /data store is classified (no new residue)", unclassified, [])

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
