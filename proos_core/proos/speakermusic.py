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


def choose_account(accounts, service_id, name=None, serial=None):
    """The account to use for one service, from the accounts the speaker already handed over.
    DAVE'S RULING, 11 Oct 2026: "defaults to first account added then option for additional accounts".
      - serial given: exactly that account of that service (the "additional accounts" option);
      - name given: that person's account within that service;
      - neither: the FIRST ADDED — the lowest account serial the speaker holds for that service (the
        speaker numbers a service's accounts as they are added: INFERRED, checked on the box in
        register 748 against the order Dave added them).
    A serial or name that isn't there is None — never someone else's account."""
    mine = [a for a in accounts or [] if getattr(a, "service_id", None) == service_id]
    if serial is not None:
        return next((a for a in mine if str(getattr(a, "serial_number", "")) == str(serial)), None)
    if name:
        return next((a for a in mine if (getattr(a, "nickname", "") or "").lower() == name.lower()), None)
    return min(mine, key=lambda a: getattr(a, "serial_number", 0) or 0) if mine else None


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


# ════════════════════════════════════════════════════════════════════════════════════════════════
# STAGE 1 OF THE MUSIC DESIGN (docs/ProOS_Music_Design_2026-10-11.md, approved by Dave 11 Oct 05:55):
# Core's speaker-music service. The home's own speaker plays, with the home's own accounts; Core finds
# the music and asks. Every answer comes from the speaker or the service — nothing is invented here.
# ════════════════════════════════════════════════════════════════════════════════════════════════
import threading                                                     # noqa: E402
from collections import OrderedDict                                  # noqa: E402


class SpeakerMusicError(Exception):
    """A reason, in plain words, that a speaker's music can't be reached. Shown as it is."""


_LOCK = threading.RLock()
_SESSIONS = {}          # (household, service id, account serial) -> {"br", "items", "lock"}
_NAMES = {}             # household -> {service id: the speaker's own name for it}
ITEMS_KEPT = 3000       # browse items remembered per account so a tap can be played (plumbing)


def connect(client, network_info, entity_id):
    """The speaker that leads this speaker's group, ready to talk to — or SpeakerMusicError, saying why.
    Addresses are READ (register 741); the listener is pointed at Core's published port first."""
    ok, why = soco_ready()
    if not ok:
        raise SpeakerMusicError(why)
    import soco
    from soco import config as soco_config
    ip = speaker_address(client, entity_id)
    if not ip:
        raise SpeakerMusicError("the network doesn't report an address for this speaker")
    box = box_address(network_info)
    if not box:
        raise SpeakerMusicError("this box's own network address isn't known")
    soco_config.EVENT_ADVERTISE_IP = box
    soco_config.EVENT_LISTENER_PORT = EVENT_PORT
    listener_port(soco_events())
    sp = soco.SoCo(ip)
    try:
        return sp.group.coordinator if sp.group else sp
    except Exception as e:                                       # noqa: BLE001
        raise SpeakerMusicError("the speaker didn't answer (%s)" % type(e).__name__)


def accounts_of(sp):
    """(accounts, how) — the home's accounts as the speaker handed them over, kept (register 745)."""
    from soco.music_services.browser import MusicServiceBrowser
    try:
        return household_accounts(sp.household_id,
                                  lambda: MusicServiceBrowser.get_accounts(device=sp, timeout=10))
    except Exception as e:                                       # noqa: BLE001
        raise SpeakerMusicError("the speaker didn't hand over the home's music accounts (%s)" % e)


def parse_service_names(xml_text):
    """{service id: name} from the speaker's own list of the services it knows (ListAvailableServices)."""
    import xml.etree.ElementTree as ET
    out = {}
    try:
        for el in ET.fromstring(xml_text or "").iter("Service"):
            try:
                out[int(el.get("Id"))] = el.get("Name") or ""
            except (TypeError, ValueError):
                continue
    except ET.ParseError:
        pass
    return out


def service_names(sp):
    hh = sp.household_id
    if hh not in _NAMES:
        r = sp.musicServices.ListAvailableServices()
        _NAMES[hh] = parse_service_names(r.get("AvailableServiceDescriptorList"))
    return _NAMES[hh]


def list_services(accounts, names):
    """The home's services and their accounts, in the speaker's own words. Each service's default is
    its first-added account (Dave's ruling, 11 Oct). Never a token — only what a person may see."""
    out, by = [], {}
    for a in sorted(accounts or [], key=lambda a: getattr(a, "serial_number", 0) or 0):
        sid = getattr(a, "service_id", None)
        name = names.get(sid)
        if not name:
            continue
        if sid not in by:
            by[sid] = {"service_id": sid, "service": name, "accounts": []}
            out.append(by[sid])
        by[sid]["accounts"].append({"account": getattr(a, "serial_number", 0),
                                    "name": getattr(a, "nickname", "") or None})
    for s in out:
        s["default"] = s["accounts"][0]["account"]
    return out


def services(sp):
    accounts, how = accounts_of(sp)
    return {"services": list_services(accounts, service_names(sp)), "accounts": how}


def _session(sp, service_id, account=None, time_zone=None):
    from soco.music_services.browser import MusicServiceBrowser
    accounts, _how = accounts_of(sp)
    acct = choose_account(accounts, int(service_id), serial=account)
    if acct is None:
        raise SpeakerMusicError("that account isn't set up for this service on these speakers")
    key = (sp.household_id, int(service_id), acct.serial_number)
    with _LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            name = service_names(sp).get(int(service_id))
            if not name:
                raise SpeakerMusicError("these speakers don't know that service")
            br = MusicServiceBrowser(name, account=acct, device=sp, time_zone=time_zone or None)
            s = _SESSIONS[key] = {"br": br, "items": OrderedDict(), "lock": threading.Lock(),
                                  "account": acct}
    return s


def _keep(s, item):
    items = s["items"]
    items[item.item_id] = item
    items.move_to_end(item.item_id)
    while len(items) > ITEMS_KEPT:
        items.popitem(last=False)
    return item


def card(item):
    """One item as the pages and Assist see it. "play" is the service's own canPlay when it says;
    a single track or stream is playable; otherwise None — not known until tried."""
    raw = getattr(item, "raw", None) or {}
    can = raw.get("canPlay") if isinstance(raw, dict) else None
    if isinstance(can, str):
        can = can.lower() == "true"
    if can is None and not item.can_browse:
        can = True
    return {"id": item.item_id, "title": item.title, "artist": item.artist or None,
            "art": item.album_art_uri or None, "type": item.item_type or None,
            "open": bool(item.can_browse), "play": can}


def home(sp, service_id, account=None, time_zone=None, sections=12, per=12):
    """The service's own home page — its sections in its own order, each with its first items."""
    s = _session(sp, service_id, account, time_zone)
    br, out = s["br"], []
    with s["lock"]:
        for sec in list(br.get_metadata().items)[:sections]:
            _keep(s, sec)
            c = card(sec)
            if sec.can_browse:
                try:
                    c["items"] = [card(_keep(s, i)) for i in list(br.get_metadata(sec, count=per).items)]
                except Exception as e:                           # noqa: BLE001
                    c["items"], c["error"] = [], str(e)[:160]
            out.append(c)
    return {"service_id": int(service_id), "account": s["account"].serial_number, "sections": out}


def open_item(sp, service_id, item_id, account=None, time_zone=None, index=0, count=50):
    s = _session(sp, service_id, account, time_zone)
    with s["lock"]:
        it = s["items"].get(item_id, item_id)
        res = s["br"].get_metadata(it, index=int(index), count=int(count))
        return {"items": [card(_keep(s, i)) for i in res.items], "index": res.index, "total": res.total}


def search(sp, service_id, q, account=None, time_zone=None, categories=None, count=8):
    """Search the service in each of ITS OWN search categories (the service lists them)."""
    s = _session(sp, service_id, account, time_zone)
    out = []
    with s["lock"]:
        cats = categories or list(s["br"].available_search_categories or [])
        for cat in cats:
            try:
                res = s["br"].search(cat, q, count=int(count))
                out.append({"category": cat, "items": [card(_keep(s, i)) for i in res.items]})
            except Exception as e:                               # noqa: BLE001
                out.append({"category": cat, "items": [], "error": str(e)[:160]})
    return {"q": q, "results": out}


def enqueue(sp, br, item):
    """Add one playable item to the speaker's queue WITH its details. REGISTER 748 (seen on the speaker):
    the library's add_uri_to_queue(uri, x) takes x as the queue POSITION — every earlier play sent the
    speaker no title, so the dashboard showed nothing. The speaker's own AddURIToQueue is called with
    the metadata in its own field; the speaker then fills in artist, album and artwork itself."""
    from soco.music_services.browser.playback import build_metadata, build_uri, resolve_item
    iid, typ, mime, title = resolve_item(br, item)
    uri = build_uri(br, iid, typ, mime)
    r = sp.avTransport.AddURIToQueue([
        ("InstanceID", 0), ("EnqueuedURI", uri),
        ("EnqueuedURIMetaData", build_metadata(br, iid, title, typ, mime=mime, uri=uri)),
        ("DesiredFirstTrackNumberEnqueued", 0), ("EnqueueAsNext", 0)])
    return int(r.get("NumTracksAdded") or 0)


def play(sp, service_id, item_id, account=None, time_zone=None, wait_s=10.0, limit=100):
    """Play an item on the speaker: a playlist/album as the speaker's own queue (first track started,
    the rest added behind it), a track or stream on its own. The answer is the SPEAKER's state."""
    s = _session(sp, service_id, account, time_zone)
    br = s["br"]
    s["lock"].acquire()
    try:
        it = s["items"].get(item_id, item_id)
        if getattr(it, "can_browse", False):
            tracks = [t for t in br.get_metadata(it, count=int(limit)).items if not t.can_browse]
            if not tracks:
                raise SpeakerMusicError("there's nothing to play directly inside “%s” — open it to choose" % it.title)
        else:
            tracks = [it]
        t0 = time.time()
        sp.clear_queue()
        enqueue(sp, br, tracks[0])
        sp.play_from_queue(0)
    except Exception:
        s["lock"].release()
        raise

    def rest():
        try:
            for t in tracks[1:]:
                try:
                    enqueue(sp, br, t)
                except Exception:                                # noqa: BLE001
                    pass
        finally:
            s["lock"].release()

    threading.Thread(target=rest, daemon=True).start()
    state = ""
    while time.time() - t0 < wait_s:
        state = (sp.get_current_transport_info() or {}).get("current_transport_state") or ""
        if state == "PLAYING":
            break
        time.sleep(0.25)
    title = getattr(it, "title", "") or ""
    return {"ok": state == "PLAYING", "state": state, "seconds": round(time.time() - t0, 1),
            "title": title, "queued": len(tracks)}


def proof(client, network_info, entity_id, service="Spotify", account=None, term="relaxing",
          play_it=False, say=print, time_zone=None, play=None):
    """Pro's Speaker Music Test — it drives the REAL service above, step by step, and every line also goes
    to Core's log. account: a person's name for that service, or empty for the first added."""
    if play is not None:
        play_it = play
    lines = []

    def out(x):
        lines.append(x)
        try:
            say("  [speakermusic] " + x)
        except Exception:                                        # noqa: BLE001
            pass

    ok, why = soco_ready()
    if not ok:
        out("STOP: " + why)
        return {"ok": False, "lines": lines}
    try:
        sp = connect(client, network_info, entity_id)
        out("1. %s answers: %s" % (entity_id, sp.player_name))
        lst = soco_events().event_listener
        out("   Core asks the speaker to reply to http://%s:%s" % (box_address(network_info), lst.requested_port_number))
        t0 = time.time()
        svc = services(sp)
        out("2. accounts %s (%.1fs): %s" % (svc["accounts"], time.time() - t0, "; ".join(
            "%s: %s" % (x["service"], ", ".join("%s%s" % (a["name"] or "account", " #%s" % a["account"])
                                                for a in x["accounts"])) for x in svc["services"])))
        mine = next((x for x in svc["services"] if x["service"].lower() == str(service).lower()), None)
        if mine is None:
            out("STOP at 3: %s isn't set up on these speakers" % service)
            return {"ok": False, "lines": lines}
        acct = None
        if account:
            accounts, _h = accounts_of(sp)
            hit = choose_account(accounts, mine["service_id"], name=account)
            if hit is None:
                out("STOP at 3: no %s account named %r" % (service, account))
                return {"ok": False, "lines": lines}
            acct = hit.serial_number
        acct = acct if acct is not None else mine["default"]
        out("   using %s account #%s; the home's time zone: %s" % (service, acct, time_zone or "not known"))
        t0 = time.time()
        h = home(sp, mine["service_id"], acct, time_zone)
        out("3. %s home page (%.1fs): %s" % (service, time.time() - t0, "; ".join(
            "%s (%d)" % (x["title"], len(x.get("items") or [])) for x in h["sections"][:8])))
        t0 = time.time()
        r = search(sp, mine["service_id"], term, acct, time_zone, categories=["playlists"], count=5)
        found = (r["results"][0]["items"] if r["results"] else [])
        out("4. search %r playlists (%.1fs): %s" % (term, time.time() - t0, " | ".join(x["title"] for x in found)))
        if play_it and found:
            p = globals()["play"](sp, mine["service_id"], found[0]["id"], acct, time_zone)
            out("5. play %r: %s %.1fs after asking; %d queued" % (p["title"], p["state"] or "NOT PLAYING", p["seconds"], p["queued"]))
            time.sleep(1.5)
            info = sp.get_current_track_info() or {}
            out("   the speaker shows: %s — %s%s" % (info.get("title") or "(no title)", info.get("artist") or "(no artist)",
                                                    "; with artwork" if info.get("album_art") else "; NO artwork"))
    except SpeakerMusicError as e:
        out("STOP: %s" % e)
        return {"ok": False, "lines": lines}
    except Exception as e:                                       # noqa: BLE001
        out("STOP: %s: %s" % (type(e).__name__, e))
        return {"ok": False, "lines": lines}
    out("DONE")
    return {"ok": True, "lines": lines}
