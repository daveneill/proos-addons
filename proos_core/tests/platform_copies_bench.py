"""NO COPIES OF THE PLATFORM'S FACTS — Core reads an integration's host and
login from the platform's own entry, every call, and keeps none of it.
Run: python3 tests/platform_copies_bench.py

13 Sep 2026, register 439 (Build B of the mirror programme). Dave: "all
the main settings need to be a mirror of the native platform — I'm sick of
hearing that you write a patch to make something work when the platform
already owns it." Core kept its own copy of the UniFi controller's host,
username and password (and Protect's), of ProOS Music's host and port, of
the MCP server's address, and an add-on option that typed the platform's
own address. The morning the network changed, Core went on calling the
controller's OLD copy for 86 seconds a call.

RULES DRIVEN (the real modules, the platform's store faked on disk):
 1. platform_entries reads the platform's entry for a domain: data, host,
    disabled entries skipped, an unreadable store answers {} — never a guess
 2. unifinet takes host/user/password/verify from the platform's `unifi`
    entry; with none, from the `unifiprotect` entry (inherited); a stale
    host left in Core's own credential store is IGNORED; there is no
    set_config any more
 3. unifi (Protect) takes host/port/user/password from the `unifiprotect`
    entry, the key from the entry when it carries one else ProOS's own
    store; set_key stores the key and nothing else; strip_platform_copies
    drops a previous build's host/login copy and keeps the key
 4. ha_mcp holds the endpoint that answered in this process only —
    nothing on disk; forget() clears it
 5. the credential store's strip() removes named meta keys and nothing else
RULES PINNED (source, comments stripped):
 6. server.py: /unifi/net/config is retired (410); the boot strip runs;
    /unifi/setup passes no host/login of Core's own; the Protect handler
    gets the integration layer so the front door provisions the platform's
    entry; there is no Music connection FILE — the platform's entry and the
    Supervisor's discovery are read on every use and the answer lives in
    process (and a failed Music call forgets it)
 7. ha_direct is gone from the add-on options, both run.sh, auth.py,
    proauth.py and provision.py
 8. every /data store Core writes is classified here as ProOS-OWN; a store
    holding a host, URL or login for something the platform has an entry
    for cannot be added without appearing in this list — and none may
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS = 0
FAIL = []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


def bare(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


TD = tempfile.mkdtemp()
STORE = os.path.join(TD, "core.config_entries")
os.environ["PROOS_PLATFORM_ENTRIES"] = STORE
os.environ["PROOS_DATA_DIR"] = TD


def write_store(entries):
    with open(STORE, "w", encoding="utf-8") as fh:
        json.dump({"data": {"entries": entries}}, fh)


from proos import platform_entries as pe  # noqa: E402
from proos import unifinet, unifi, ha_mcp  # noqa: E402
from proos.credentials import CredentialStore  # noqa: E402

# 1 · platform_entries
write_store([
    {"entry_id": "E1", "domain": "unifi", "data": {"host": "192.168.8.1", "username": "proos", "password": "pw-net", "verify_ssl": False, "site": "default"}},
    {"entry_id": "E0", "domain": "unifi", "disabled_by": "user", "data": {"host": "10.0.0.9"}},
    {"entry_id": "P1", "domain": "unifiprotect", "data": {"host": "192.168.8.1", "port": 443, "username": "proos", "password": "pw-prot", "verify_ssl": False}},
    {"entry_id": "M1", "domain": "music_assistant", "data": {"url": "http://b333b432-proos-music:8095", "token": "ma-tok"}},
])
check("the platform's entry for a domain is read from its own store", pe.data("unifi").get("host"), "192.168.8.1")
check("  …a disabled entry never serves", pe.host_of("unifi") == "192.168.8.1" and pe.entry("unifi")["entry_id"] == "E1")
check("  …host_of reads a url when the entry keeps one", pe.host_of("music_assistant"), "b333b432-proos-music")
# RE-PINNED 6 Oct 2026 (register 695, Dave: "yes" — read the address only): the
# platform's store still holds the integration's login, but ProOS never reads it.
check("  …the integration's password, username and token are never read (695)",
      (sorted(pe.data("unifi")), sorted(pe.data("music_assistant")), "password" in json.dumps(pe.entries("unifiprotect"))),
      (["host", "site", "verify_ssl"], ["url"], False))
check("  …scrub keeps Protect's api_key (Dave kept direct Protect, 695) and drops every login shape",
      sorted(pe.scrub({"data": {"api_key": "K", "host": "h", "client_secret": "s", "access_token": "t", "pin": "1", "Password2": "x"},
                       "options": {"apps": {}, "refresh_token": "r"}})["data"]),
      ["api_key", "host"])
check("  …no entry: {} and '', never a guess", (pe.data("nothing"), pe.host_of("nothing"), pe.entry("nothing")), ({}, "", None))
os.remove(STORE)
check("  …an unreadable store answers nothing, not something remembered", (pe.data("unifi"), pe.entries("unifi")), ({}, []))

# 2 · unifinet
creds = CredentialStore(os.path.join(TD, "credentials.json"))
creds.put("unifi_network", "", name="UniFi Network", host="192.168.1.1", extra={"username": "old", "password": "stale"})
write_store([
    {"entry_id": "E1", "domain": "unifi", "data": {"host": "192.168.8.1", "username": "proos", "password": "pw-net", "verify_ssl": False}},
    {"entry_id": "P1", "domain": "unifiprotect", "data": {"host": "192.168.8.2", "port": 443, "username": "prot", "password": "pw-prot", "verify_ssl": False}},
])
net = unifinet.UniFiNetClient(creds)
check("unifinet reads the controller's address from the platform's unifi entry — never its login (695)",
      net._conf(), ("192.168.8.1", False, None, None))
check("  …and says where it read from; it is never 'configured' — ProOS does not sign in (691)",
      (net._source, net._inherited, net.configured()), ("unifi", False, False))
write_store([{"entry_id": "P1", "domain": "unifiprotect", "data": {"host": "192.168.8.2", "username": "prot", "password": "pw-prot", "verify_ssl": True}}])
check("  …with no Network entry, the Protect entry's address serves (inherited), still no login",
      (net._conf(), net._inherited, net._source), (("192.168.8.2", True, None, None), True, "unifiprotect"))
check("  …status carries no username", ("username" in net.status(), net.status()["has_login"]), (False, False))
write_store([])
check("  …with neither, nothing is configured — the stale copy is never a fallback", (net.configured(), net._conf()[0]), (False, ""))
check("  …and there is no set_config to type a copy back in", hasattr(net, "set_config"), False)

# 3 · unifi (Protect)
creds.put("unifi_protect", "KEY-own", name="UniFi Protect", host="192.168.1.1", extra={"username": "old", "password": "stale", "verify_ssl": True})
write_store([{"entry_id": "P1", "domain": "unifiprotect", "data": {"host": "192.168.8.2", "port": 443, "username": "prot", "password": "pw-prot", "verify_ssl": False}}])
pc = unifi.ProtectClient(creds)
check("Protect reads the host from the platform's entry, never its login (695); the key from ProOS's own store",
      pc._conf(), ("KEY-own", "192.168.8.2", False, None, None))
write_store([{"entry_id": "P1", "domain": "unifiprotect", "data": {"host": "192.168.8.2", "port": 8443, "username": "prot", "password": "pw-prot", "api_key": "KEY-entry"}}])
check("  …when the entry carries an api_key and a non-standard port, both are the entry's",
      pc._conf(), ("KEY-entry", "192.168.8.2:8443", False, None, None))
check("  …strip_platform_copies drops a previous build's host/login copy and keeps the key",
      (pc.strip_platform_copies(), creds.get("unifi_protect"),
       [k for k in ("host", "username", "password", "verify_ssl", "port") if k in creds.meta("unifi_protect")]),
      (True, "KEY-own", []))
check("  …and is a no-op the second time", pc.strip_platform_copies(), False)
pc.set_key("KEY-new")
check("  …set_key stores the key and nothing else", (creds.get("unifi_protect"), "host" in creds.meta("unifi_protect")), ("KEY-new", False))
pc.set_key("")
check("  …a blank key keeps the one already held (Pro's 'leave blank to keep')", creds.get("unifi_protect"), "KEY-new")
creds.delete("unifi_protect")
try:
    pc.set_key("")
    check("  …with nothing held, a blank key is refused", "no error", "ProtectError")
except unifi.ProtectError as e:
    check("  …with nothing held, a blank key is refused", e.status, 400)
creds.put("unifi_protect", "KEY-new", name="UniFi Protect")
check("  …there is no set_config to type a host into", hasattr(pc, "set_config"), False)
write_store([])
stt = pc.status()
check("  …status says whether the platform has an entry and where the key is from",
      (stt["entry"], stt["has_key"], stt["key_from_entry"], stt["host"]), (False, True, False, None))

# 4 · ha_mcp
ha_mcp._conn_save({"url": "http://mcp:8080/s/mcp"})
check("the MCP endpoint that answered is held in process", ha_mcp._conn_load(), {"url": "http://mcp:8080/s/mcp"})
ha_mcp.forget()
check("  …forget() clears it", ha_mcp._conn_load(), {})
check("  …and nothing about it is on disk", sorted(f for f in os.listdir(TD) if "mcp" in f), [])
_MCP_CODE = bare(read("proos", "ha_mcp.py").split('"""', 2)[2])   # past the module docstring, which may name the retired file
check("  …ha_mcp names no file path for a connection", "_CONN_PATH" in _MCP_CODE or "mcp_conn.json" in _MCP_CODE, False)

# 5 · credentials.strip
creds.put("svc", "T", name="X", host="h", extra={"username": "u", "keep": 1})
check("strip() removes the named meta keys only",
      (creds.strip("svc", ("host", "username", "password")), creds.meta("svc").get("keep"), "host" in creds.meta("svc"), creds.get("svc")),
      (True, 1, False, "T"))
check("  …and answers False when there is nothing to strip", creds.strip("svc", ("host",)) or creds.strip("nope", ("host",)), False)

# 6 · server.py pins
SRV = bare(read("server.py"))
check("server: /unifi/net/config is retired with a plain sentence (410)",
      bool(re.search(r'if parts == \["unifi", "net", "config"\]:\s*\n\s*return self\._send\(410', SRV)))
check("  …the boot strip of platform copies runs", "_strip_platform_copies()\n" in SRV and "def _strip_platform_copies" in SRV)
check("  …/unifi/setup passes only ProOS's own key", bool(re.search(r'ensure_integration\(\s*api_key=\(_creds\.get\(unifi\.SERVICE\) if _creds else ""\) or ""\)', SRV)))
check("  …the Protect handler carries the integration layer (the front door provisions the platform's entry)", "layer=_unifi_layer)" in SRV)
check("  …there is no Music connection file", ("_MA_CONN_FILE" in SRV, 'mastore.path("ma_conn.json")' in SRV, "_MA_WHERE" in SRV), (False, False, True))
check("  …a failed Music call forgets the in-process answer", SRV.count("_forget_ma_conn()") >= 2)
check("  …unifinet.set_config is called nowhere", "_unifinet.set_config" in SRV, False)
MAS = bare(read("proos", "mastore.py"))
check("mastore keeps no connection store", ("ma_conn.json" in MAS.replace('_RETIRED = ("ma_conn.json",)', ""), "def load_conn" in MAS, "def save_conn" in MAS), (False, False, False))

# 7 · ha_direct
for f in ("config.yaml", "run.sh", os.path.join("proos", "run.sh"), os.path.join("proos", "auth.py"),
          os.path.join("proos", "proauth.py"), os.path.join("proos", "provision.py")):
    txt = read(f)
    code = bare(txt) if f.endswith(".py") else txt
    check("ha_direct is gone from %s" % f, ("ha_direct" in code, "HA_DIRECT" in code), (False, False))

# 8 · the registry of /data stores, classified
OWN = {
    # ProOS's own commissioning and preference records — the platform has no version of these
    "credentials.json": "ProOS's own tokens (Protect Integration API key, service tokens) — never a host or a login the platform holds",
    "proos_installer_tokens.json": "installer long-lived tokens Core minted",
    "ma_admin.json": "ProOS's Music admin identity", "music_speakers.json": "curated room speakers", "music_genres.json": "curated genres", "music_order.json": "the home's service order, chosen in Pro (register 896) — the platform has no such setting",
    "knowledge_site.json": "the installer's notes", "voice.json": "chosen speech engine", "net_witnesses.json": "traffic witnesses",
    "health_state.json": "health state", "device_ports.json": "device ports", "navconfig.json": "nav layout", "navcaps.json": "page capabilities",
    "assist_flags.json": "Assist flags", "consent.json": "consent", "shortcuts.json": "shortcuts", "watch_learned.json": "what this home learned about starting shows (register 970oo)", "account_copies.json": "one-way copies of Connect's yes for this home's ProOS accounts (register 970rr)", "garage_doors.json": "which pieces make each garage door, and its wiring (register 974) — the door itself IS the platform's template cover; only what only ProOS knows is kept", "room_order.json": "room order", "layouts.json": "dashboard layouts (register 960)",
    "channels.json": "channel presets", "channel_lists.json": "the house's channel lists, installer-confirmed on a TV — ProOS's own (register 1049)", "cast_links.json": "which Cast player is which Shield, linked by the installer — ProOS's own (register 1059)", "display_apps.json": "each device's last PUBLISHED source_list — Dave's ruled exception (30 Jul / 13 Sep): captured evidence, a fresh publish wins",
    "assist_scene_music.json": "scene→music bindings", "appart_brands.json": "app brands", "appart_hidden.json": "hidden app tiles",
    "appart_origin.json": "artwork origin", "appart_removed.json": "removed tiles", "proactive.json": "proactive state",
    "unifi_curation.json": "camera curation", "tech_users.json": "tech users", "installer_state.json": "installer progress",
    "provisioned.json": "provisioning marker", "catalog_published.json": "catalog publish", "recovery_config.json": "recovery pointer",
    "integration_catalog.json": "fleet catalog", "assist.json": "Assist config", "assist_memory.json": "Assist memory",
    "assist_sessions.json": "Assist sessions", "habits.json": "habits", "suggestions.json": "offers", "standing.json": "standing instructions", "usage.json": "usage ledger",
    "proos_project.json": "the commissioning record", "options.json": "the Supervisor's own options file",
    "push_config.json": "push key", "push_devices.json": "registered phones", "apns_key.json": "APNs key",
    "savant.json": "Savant bridge (Core's own bridge, not a platform integration)", "scene_photos.json": "scene pictures",
    "room_art.json": "room art", "watchers.json": "watchers", "activities.json": "activities", "membership.json": "room members",
    "room_devices.json": "room devices", "ai_usage.json": "the realtime voice's day-by-day token counts and estimated cost — ProOS's own ledger; the provider's billing is the truth (register 1151)", "assist_autofix.json": "when each device was last handed to the AI automatically — ProOS's own record of its own act, for the once-a-day limit (register 1164)", "unifi_alarm_triggers.json": "the installer's own list of this site's UniFi Alarm Manager webhook triggers (a name and the trigger ID he created in UniFi) — ProOS's own setting; the console keeps no list ProOS can read (register 1224)", "unifi_port_seen.json": "where ProOS last SAW each wired device on a switch port, with the time — its own observation; the controller drops an offline client's port from its live table, which is the moment Assist needs it (register 1222)", "voice_rule_state.json": "the voice-exposure rule outcomes ProOS itself last applied, per entity — a record of its own act; the exposure itself is the platform's (register 1146)", "appfavs.json": "app favourites", "envassign.json": "environment assignments", "sessmon.json": "session monitor",
    "powerlog.json": "power log", "prepare.json": "prepare state", "site.json": "site identity", "install_id": "install epoch",
    "journal/": "the journal directory", "terminal_audit.log": "terminal audit",
    "sweep_last.json": "the last Full Assist Sweep report — ProOS's own reading of the house (register 663)",
    "sweep_config.json": "the sweep's daily time (register 663)",
    "auto_full_backup.json": "when ProOS last ran the box's own full backup automatically — ProOS's own act (register 1024)",
    "platform_log.json": "ProOS's mirror of the platform's integration log (messages, counts carried across platform restarts) — the platform keeps its log only in memory (register 1027)",
    "notify_devices.json": "which devices the installer switched notifications on for (off by default) — ProOS's own setting; the platform has no version of it (register 1022)",
    "platform_faults_ack.json": "which platform fault cards the installer acknowledged — ProOS's own act; the faults themselves are read from the platform every time (register 1014)",
    "platform_faults_seen.json": "when ProOS FIRST saw each platform fault — ProOS's own observation; the platform keeps no such time (register 1255)",
    "sweep_acted.json": "when Pro's Pair last succeeded per integration — ProOS's own act (register 665)",
    "port_history.json": "what each device's own switch port was SEEN to do in each state, and the person's answers — ProOS's own measurement (plan A3)",
    "push_status.json": "the last real outcome of an alert to each ProOS phone — what Core itself saw happen (plan B1)",
    "apple_music_token.json": "the Apple Music token the installer pasted (register 759) — ProOS's own credential; the platform has no version of it",
    "speaker_links.json": "ProOS's own music-service sign-ins (register 778) — the person signed in on the service's own page; the platform has no version of it",
    "apple_music_people.json": "each person's own Apple Music sign-in (register 843) — ProOS's own credential per person; the platform has no version of it",
    "speaker_addresses.json": "the address each speaker last ANSWERED at with its own id (register 799) — ProOS's own measurement, used only when the platform's tracker has none",
    "platform_update.json": "the last platform/OS update ProOS started and what Core checked afterwards (register 813) — ProOS's own record; the platform keeps no such record",
    "panel_setup.json": "where the wall panel's own set-up got to (register 940) — ProOS's own record",
    "panel_login.json": "the screen login the panel made itself (register 940) — its password exists nowhere else; the platform keeps only a hash",
    "panel_wake.json": "the panel's wake word as a PERSON last chose it (register 980) — the platform's satellite forgets \"off\" at every restart; ProOS's own record of a person's choice",
    "kiosk_rest.json": "the key to the kiosk screen app's internal door, made by Core (register 963) — ProOS's own credential; the platform has no version of it",
    "remotes.json": "the ProOS Remotes this home accepted, each one's home room and button map (register 969j) — ProOS's own record; the platform has no idea of a remote",
    "remote_logins.json": "the logins Core made for the accepted remotes (register 969j) — their passwords exist nowhere else; the platform keeps only a hash",
    "connect.json": "the box's ProOS Connect id and secret, made on the box (register 969u) — ProOS's own credential; the platform has no version of it",
    "connect_policy.json": "what ProOS Connect last said about the fleet's AI and which of ProOS's keys it holds (register 1347) — a copy of CONNECT's answer, never the platform's; no key in it",
    "net_pending.json": "a network change held for Keep These Settings, with the port's settings from before (register 969z) — ProOS's own way back; the platform keeps no such record",
    "weather_std.json": "which weather service Core set up and at what location (register 970a) — only so a move is noticed; the service itself and the location are the platform's",
}
PLATFORM_OWNED_RETIRED = {"ma_conn.json": "retired 439 — swept only", "mcp_conn.json": "retired 439 — gone",
                          "atv_apps.json": "retired 442 — swept only; the Android TV entry's options are the list"}
found = set()
for dirpath, _dirs, files in os.walk(ROOT):
    if "tests" in dirpath or "__pycache__" in dirpath:
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        txt = bare(open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore").read())
        if "/data" in txt or "PROOS_DATA_DIR" in txt:
            found.update(re.findall(r'"([a-z_]+\.(?:json|log))"', txt))
        found.update(re.findall(r'"/data/([a-z_]+\.json)"', txt))
        found.update(re.findall(r'PROOS_DATA_DIR", "/data"\), "([a-z_\.]+\.json)"', txt))
unclassified = sorted(f for f in found if f not in OWN and f not in PLATFORM_OWNED_RETIRED and not f.endswith(".log")
                      and f not in ("aliases.json", "domains.json", "packages.json", "atv_seed.json", "channel_presets.json", "config.json",
                                    "approved_versions.json",   # register 813: shipped inside Core, never in /data
                                    "channel_starts.json"))      # register 1049: ProOS's starting channel lists, shipped
check("every /data store Core writes is classified as ProOS-own here (a new store needs a decision)", unclassified, [])
live_platform = sorted(f for f in found if f in PLATFORM_OWNED_RETIRED and f not in ("ma_conn.json", "atv_apps.json"))
check("  …and no store that copies a platform fact is written any more", live_platform, [])
CODE_ALL = "\n".join(bare(open(os.path.join(d, f), encoding="utf-8", errors="ignore").read())
                     for d, _x, fs in os.walk(os.path.join(ROOT, "proos")) for f in fs if f.endswith(".py"))
CODE_ALL += "\n" + SRV
check("  …no code writes a host or a login for a UniFi service into the credential store",
      bool(re.search(r'put\(\s*(?:SERVICE|"unifi_network"|"unifi_protect")[^\n]*host=', CODE_ALL)), False)

print("\n%d checks passed, %d failed%s" % (PASS, len(FAIL), ("  " + repr(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
