"""THE SPEAKER'S OWN MUSIC SERVICES, FROM CORE ON THE BOX — PROOF STEP (register 741).

Register 740 proved from the Mac, on Dave's network: a Sonos reads the household's own music accounts (no
new sign-in), searches Spotify in about a second, serves the service's whole home page (recently played,
top mixes, recommendations, with artwork) and the person's library, and plays a whole playlist as its own
queue in 2.6 s — no ProOS Music in between. Dave, 10 Oct 2026 7:38 am: "yes go ahead with step 2" — prove
the same thing from Core on the box before the design relies on it.

What Core needs that the Mac had for free, and what this module does about it:
  - the SoCo update that restores the services (pull request #1010, pinned commit) — installed in Core's
    image by the Dockerfile; without it this module says so and does nothing;
  - the speaker's ADDRESS: Core is not on the home network directly, so it cannot discover speakers. The
    platform knows each speaker's network identity (its device's MAC) and the network controller knows
    which address that MAC holds (the platform's own device tracker). Both are read, never typed;
  - a way IN: to hand over the household's accounts, the speaker sends Core a message. Core opens one
    published port for it (config.yaml, 1410/tcp) and tells the speaker to send it to the BOX's own
    address (the Supervisor's network report), where the platform forwards it into Core.

READS by default. play=True plays the top result on that speaker (the installer asked for it).
"""
from __future__ import annotations

import time

SOCO_COMMIT = "97dba030b89715d7d97ad8f31039ce6aa3ecfb13"
EVENT_PORT = 1410


def soco_ready():
    """(True, None) when Core's image carries the SoCo with household music services, else (False, why)."""
    try:
        import soco  # noqa: F401
        from soco.music_services.browser import MusicServiceBrowser  # noqa: F401
        return True, None
    except Exception as e:                                       # noqa: BLE001
        return False, "the speaker music library isn't installed in this Core (%s)" % type(e).__name__


def soco_events():
    from soco import events
    return events


def listener_port(events, port=None):
    """Point SoCo's one event listener at Core's published port. A listener already running on another
    port is stopped first, so it restarts on ours. Returns the port it will use."""
    port = port or EVENT_PORT
    lst = events.event_listener
    if getattr(lst, "is_running", False) and tuple(getattr(lst, "address", ()) or ())[1:2] != (port,):
        lst.stop()
    lst.requested_port_number = port
    return port


def choose_account(accounts, service_id, name=None):
    """The account to use for one service, from the accounts the speaker already handed over: the named
    one within that service (a name that isn't there is None, never someone else's); with no name,
    that service's first. None if it has none."""
    mine = [a for a in accounts or [] if getattr(a, "service_id", None) == service_id]
    if name:
        hit = next((a for a in mine if (getattr(a, "nickname", "") or "").lower() == name.lower()), None)
        return hit
    return mine[0] if mine else None


_ACCOUNTS = {}          # household id -> (accounts, when read). Memory only — never written to disk.


def household_accounts(household_id, read, now=None):
    """The home's music accounts as the speaker handed them over — kept for Core's lifetime.
    REGISTER 745 (seen on the box): reads from the speaker came back, then didn't, then did — every other
    one, six in a row (cause not known). The accounts only change when someone adds a service in the
    speaker's own app, so they are read ONCE and kept; a read that doesn't arrive is asked once more.
    Returns (accounts, how) — how says whether they were read now or kept from earlier."""
    now = now or time.time()
    kept = _ACCOUNTS.get(household_id) if household_id else None
    if kept:
        return kept[0], "kept from %s" % time.strftime("%H:%M", time.localtime(kept[1]))
    try:
        accounts, how = read(), "read from the speaker via port %d" % EVENT_PORT
    except Exception:                                            # noqa: BLE001
        accounts, how = read(), "read from the speaker via port %d (second ask)" % EVENT_PORT
    if household_id:
        _ACCOUNTS[household_id] = (accounts, now)
    return accounts, how


def box_address(network_info):
    """The box's own LAN address, from the Supervisor's /network/info payload."""
    for iface in (network_info or {}).get("interfaces") or []:
        if not iface.get("primary"):
            continue
        for a in ((iface.get("ipv4") or {}).get("address") or []):
            ip = str(a).split("/")[0]
            if ip and not ip.startswith("127."):
                return ip
    for iface in (network_info or {}).get("interfaces") or []:
        for a in ((iface.get("ipv4") or {}).get("address") or []):
            ip = str(a).split("/")[0]
            if ip and not ip.startswith(("127.", "172.30.", "172.17.")):
                return ip
    return None


SPEAKER_IP_TEMPLATE = (
    "{%- set macs = (device_attr(device_id(eid), 'connections') or []) | map('last') | map('lower') | list -%}"
    "{%- set ns = namespace(ip='') -%}"
    "{%- for t in states.device_tracker -%}"
    "{%- if (t.attributes.mac | default('') | lower) in macs and t.attributes.ip is defined -%}"
    "{%- set ns.ip = t.attributes.ip -%}{%- endif -%}{%- endfor -%}{{ ns.ip }}"
)


def speaker_address(client, entity_id):
    """The speaker's address: its device's MAC (the platform) → the address the network controller
    reports for that MAC (the platform's own device tracker). None when either is unknown."""
    try:
        tpl = "{% set eid = '" + str(entity_id).replace("'", "") + "' %}" + SPEAKER_IP_TEMPLATE
        ip = str(client.render_template(tpl) or "").strip()
        return ip or None
    except Exception:                                            # noqa: BLE001
        return None


def proof(client, network_info, entity_id, service="Spotify", account=None, term="relaxing",
          play=False, say=print):
    """Run the 740 proof from Core. Returns {"ok", "lines": [...]} — every line also goes to Core's log."""
    lines = []

    def out(s):
        lines.append(s)
        try:
            say("  [speakermusic] " + s)
        except Exception:                                        # noqa: BLE001
            pass

    ok, why = soco_ready()
    if not ok:
        out("STOP: " + why)
        return {"ok": False, "lines": lines}
    import soco
    from soco import config as soco_config
    from soco.music_services.browser import MusicServiceBrowser

    ip = speaker_address(client, entity_id)
    box = box_address(network_info)
    out("1. %s is at %s (from the platform); this box is %s" % (entity_id, ip or "UNKNOWN", box or "UNKNOWN"))
    if not ip or not box:
        out("STOP: need both addresses")
        return {"ok": False, "lines": lines}
    soco_config.EVENT_ADVERTISE_IP = box
    soco_config.EVENT_LISTENER_PORT = EVENT_PORT
    # REGISTER 741 (seen on the box): SoCo builds its listener when it is first imported and fixes its
    # port THEN (1400). Setting the config afterwards changed nothing, and the speaker was told to reply
    # on a port the box does not publish. The listener itself is told, before it starts.
    listener_port(soco_events())
    sp = soco.SoCo(ip)
    try:
        sp = sp.group.coordinator if sp.group else sp
        out("   speaker answers: %s" % sp.player_name)
    except Exception as e:                                       # noqa: BLE001
        out("STOP: the speaker didn't answer Core: %s" % e)
        return {"ok": False, "lines": lines}
    t0 = time.time()
    lst = soco_events().event_listener
    out("   Core asks the speaker to reply to http://%s:%s (listening on %s)" % (
        soco_config.EVENT_ADVERTISE_IP, lst.requested_port_number,
        ("%s:%s" % lst.address) if getattr(lst, "address", None) else "not yet started"))
    try:
        accounts, how = household_accounts(getattr(sp, "household_id", None),
                                           lambda: MusicServiceBrowser.get_accounts(device=sp, timeout=10))
    except Exception as e:                                       # noqa: BLE001
        out("STOP at 2 (the speaker handing Core the household's accounts, port %d, asked twice): %s: %s"
            % (EVENT_PORT, type(e).__name__, e))
        return {"ok": False, "lines": lines}
    out("2. household accounts %s (%.1fs): %s" % (how, time.time() - t0, ", ".join(
        "service %s%s" % (a.service_id, (" (%s)" % a.nickname) if getattr(a, "nickname", "") else "")
        for a in accounts) or "none"))
    try:
        out("   listener actually on %s:%s" % tuple(soco_events().event_listener.address))
    except Exception:                                            # noqa: BLE001
        pass
    # REGISTER 744 (seen on the box): opening a service WITHOUT an account makes the library read the
    # household's accounts a SECOND time, and that second read never arrived. It is never needed: the
    # accounts were just read. The account is chosen from them — the person's, by name, within THAT
    # service (the old match took the first "Dave" of any service) — and always handed over.
    try:
        from soco.music_services import MusicService
        acct = choose_account(accounts, int(MusicService(service, device=sp).service_id), account)
    except Exception as e:                                       # noqa: BLE001
        out("STOP at 3 (finding %s in this home): %s: %s" % (service, type(e).__name__, e))
        return {"ok": False, "lines": lines}
    if acct is None:
        out("STOP at 3: no %s account%s is set up on these speakers" % (service, (" named %r" % account) if account else ""))
        return {"ok": False, "lines": lines}
    out("   using %s account %s" % (service, getattr(acct, "nickname", "") or "(no name)"))
    try:
        br = MusicServiceBrowser(service, account=acct, device=sp)
    except Exception as e:                                       # noqa: BLE001
        out("STOP at 3 (opening %s): %s: %s" % (service, type(e).__name__, e))
        return {"ok": False, "lines": lines}
    t0 = time.time()
    try:
        home = list(getattr(br.get_metadata(), "items", []) or [])
        out("3. %s home page (%.1fs): %s" % (service, time.time() - t0, "; ".join(i.title for i in home[:8])))
    except Exception as e:                                       # noqa: BLE001
        out("3. home page FAILED: %s: %s" % (type(e).__name__, e))
    t0 = time.time()
    found = []
    try:
        found = list(getattr(br.search("playlists", term, count=5), "items", []) or [])
        out("4. search %r playlists (%.1fs): %s" % (term, time.time() - t0, " | ".join(i.title for i in found)))
    except Exception as e:                                       # noqa: BLE001
        out("4. search FAILED: %s: %s" % (type(e).__name__, e))
    if play and found:
        from soco.music_services.browser.playback import build_metadata, build_uri, resolve_item
        t0 = time.time()
        try:
            tracks = list(getattr(br.get_metadata(found[0], count=50), "items", []) or [])
            sp.clear_queue()

            def add(t):
                iid, typ, mime, title = resolve_item(br, t)
                uri = build_uri(br, iid, typ, mime)
                sp.add_uri_to_queue(uri, build_metadata(br, iid, title, typ, mime=mime, uri=uri))

            add(tracks[0])
            sp.play_from_queue(0)
            state = ""
            for _ in range(40):
                state = sp.get_current_transport_info().get("current_transport_state")
                if state == "PLAYING":
                    break
                time.sleep(0.25)
            first = time.time() - t0
            for t in tracks[1:]:
                try:
                    add(t)
                except Exception:                                # noqa: BLE001
                    pass
            out("5. play %r: %s %.1fs after asking; %d queued" % (found[0].title, state, first, len(tracks)))
        except Exception as e:                                   # noqa: BLE001
            out("5. play FAILED: %s: %s" % (type(e).__name__, e))
    out("DONE")
    return {"ok": True, "lines": lines}
