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
ACCOUNTS_FRESH_S = 600  # plumbing: how long the home's accounts are used before the speaker is asked again
ACCOUNTS_RETRY_S = 60   # plumbing: after a re-read the speaker didn't answer, how soon to ask again (the kept list serves)


def household_accounts(household_id, read, now=None):
    """The home's music accounts as the speaker handed them over.
    REGISTER 745 (seen on the box): reads from the speaker came back, then didn't, then did — every other
    one, six in a row (cause not known). So they are kept, and a read that doesn't arrive never empties them.
    REGISTER 828 (Dave 13 Oct 15:08: "I also added another service to Sonos and it doesnt show up"): kept for
    Core's LIFETIME meant a service added in the speaker's own app never showed until Core restarted. Now the
    speaker is asked again once the kept list is ACCOUNTS_FRESH_S old; if it doesn't answer, the kept list is used
    and it is asked again ACCOUNTS_RETRY_S later. Returns (accounts, how) — how says read now or kept."""
    now = now or time.time()
    kept = _ACCOUNTS.get(household_id) if household_id else None
    if kept and now - kept[1] < ACCOUNTS_FRESH_S:
        return kept[0], "kept from %s" % time.strftime("%H:%M", time.localtime(kept[1]))
    if kept:
        try:
            accounts = read()
        except Exception:                                        # noqa: BLE001
            _ACCOUNTS[household_id] = (kept[0], now - ACCOUNTS_FRESH_S + ACCOUNTS_RETRY_S)
            return kept[0], "kept from earlier (the speaker didn't answer a fresh read)"
        _ACCOUNTS[household_id] = (accounts, now)
        return accounts, "read again from the speaker"
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
_ITEMS_LOCK = threading.Lock()
_SESSIONS = {}          # (household, service id, account serial) -> {"br", "items", "lock"}
_NAMES = {}             # household -> {service id: the speaker's own name for it}
ITEMS_KEPT = 3000       # browse items remembered per account so a tap can be played (plumbing)

# APPLE MUSIC (music design stage 4, register 759). 204 is the speaker's OWN id for Apple Music (read from
# its list on Dave's box). The household's stored Apple sign-in is refused to outside apps (register 740),
# so for Apple the search, charts and track lists come from Apple's own catalogue (applemusic.py, with the
# installer's token) — and the SPEAKER plays, song by song, with its own Apple sign-in (proven, 742).
APPLE_MUSIC_SID = 204
_CLIENT = {"c": None}


def _apple(service_id):
    return int(service_id) == APPLE_MUSIC_SID


# register 843: whose Apple Music this request is for — the signed-in ProOS person, set by Core around one request
# (as_person) so the speaker's other services and HEOS are untouched. None = nobody's own (charts and search only).
_PERSON = threading.local()


class as_person:
    def __init__(self, uid):
        self.uid = uid

    def __enter__(self):
        _PERSON.uid = self.uid
        return self

    def __exit__(self, *a):
        _PERSON.uid = None
        return False


def _apple_user():
    uid = getattr(_PERSON, "uid", None)
    if not uid:
        return None
    from proos import applemusic as _am
    return _am.person_token(uid)


def _apple_sf():
    from proos import applemusic as _am
    return _am.storefront(_CLIENT["c"]) if _CLIENT["c"] is not None else "us"


def connect(client, network_info, entity_id):
    """The speaker that leads this speaker's group, ready to talk to — or SpeakerMusicError, saying why.
    Addresses are READ (register 741); the listener is pointed at Core's published port first."""
    ok, why = soco_ready()
    if not ok:
        raise SpeakerMusicError(why)
    _CLIENT["c"] = client
    import soco
    from soco import config as soco_config
    ip = speaker_address(client, entity_id)
    from_memory = False
    if not ip:
        # REGISTER 799: the network forgot the speaker (its controller down, 796) — try the last address the
        # SPEAKER ITSELF confirmed, and use it only if the same speaker answers there again.
        ip = remembered_address(entity_id)
        from_memory = bool(ip)
    if not ip:
        raise SpeakerMusicError("the network doesn't report an address for this speaker")
    box = box_address(network_info)
    if not box:
        raise SpeakerMusicError("this box's own network address isn't known")
    soco_config.EVENT_ADVERTISE_IP = box
    soco_config.EVENT_LISTENER_PORT = EVENT_PORT
    listener_port(soco_events())
    sp = soco.SoCo(ip)
    sp = _confirmed(client, entity_id, sp, ip, from_memory)
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


# ════════════════════════════════════════════════════════════════════════════════════════════════
# A SERVICE SHOWS ONLY WHEN IT ANSWERS; SIGN-INS OF PROOS'S OWN (registers 768, 775–778).
# DAVE'S RULING (768): "all subscriptions need to be based on if they are signed in and show if they are
# if not then they dont show". Each service's own first page is ASKED (kept a while); one that answers
# nothing is not offered. READ ON THE BOX (775): Pandora refuses the household's stored sign-in when asked
# from outside the speaker ("AuthTokenExpired: Failed to reauth device id"), though the speaker plays it.
# PROVEN (776/777): the person signs in on the service's OWN page (the library's link flow — no password
# passes through ProOS); ProOS keeps that sign-in for browsing and searching; the speaker plays what it
# finds with the household's own account; nothing is written to the speaker. DAVE (12 Oct 09:31): "I want
# any signins to be on both" — the homeowner's Music page and Pro.
# ════════════════════════════════════════════════════════════════════════════════════════════════
import copy as _copy                                                 # noqa: E402
import json as _json                                                 # noqa: E402
import os as _os                                                     # noqa: E402

LINKS_PATH = _os.path.join(_os.environ.get("PROOS_DATA_DIR", "/data"), "speaker_links.json")
ANSWERED_KEPT_S = 600   # plumbing: how long "this service answered" is kept before it is asked again
REFUSED_KEPT_S = 60     # plumbing: how long "this service didn't answer" is kept (shorter: a blip shouldn't hide it long)
LINK_WAIT_S = 900       # plumbing: how long a started sign-in waits for the person to finish on the service's page
LINKABLE = ("AppLink", "DeviceLink")   # the sign-in kinds the service's own web page can complete (the library's words)
_ANSWERED = {}          # (household, sid, serial) -> (answered, when, why)
_PENDING = {}           # (household, sid) -> (manager, link, started)


# ── THE SPEAKER'S LAST CONFIRMED ADDRESS (register 799; Dave 12 Oct 18:06 "go on both") ──────────────────────
# Read on the box (796): when the network controller's integration was stuck after a power cut, the platform's
# device tracker held no address for any Sonos and every room's music stopped — though every speaker was on the
# network and answering. The platform's tracker stays THE answer. ProOS keeps only what it measured itself: the
# address at which the speaker answered with its OWN id (its RINCON id, matched to the platform's device record).
# Used only when the platform has no address, and only if the same speaker answers there again; if not, said.
ADDRESSES_PATH = _os.path.join(_os.environ.get("PROOS_DATA_DIR", "/data"), "speaker_addresses.json")
SPEAKER_ID_TEMPLATE = (
    "{%- set ns = namespace(id='') -%}"
    "{%- for i in (device_attr(device_id(eid), 'identifiers') or []) -%}"
    "{%- if i[0] == 'sonos' -%}{%- set ns.id = i[1] -%}{%- endif -%}{%- endfor -%}{{ ns.id }}"
)


def speaker_identity(client, entity_id):
    """The speaker's own id as the platform's device record holds it (Sonos: RINCON_…), or None."""
    try:
        tpl = "{% set eid = '" + str(entity_id).replace("'", "") + "' %}" + SPEAKER_ID_TEMPLATE
        out = str(client.render_template(tpl) or "").strip()
        return out if out.upper().startswith("RINCON_") else None
    except Exception:                                            # noqa: BLE001
        return None


def _addresses():
    try:
        with open(ADDRESSES_PATH, encoding="utf-8") as fh:
            return _json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def remembered_address(entity_id):
    return (_addresses().get(str(entity_id)) or {}).get("ip")


def remember_address(entity_id, uid, ip):
    d = _addresses()
    if (d.get(str(entity_id)) or {}).get("ip") == ip and (d.get(str(entity_id)) or {}).get("uid") == uid:
        return
    d[str(entity_id)] = {"ip": ip, "uid": uid, "seen": int(time.time())}
    try:
        _os.makedirs(_os.path.dirname(ADDRESSES_PATH), exist_ok=True)
        tmp = ADDRESSES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(d, fh)
        _os.replace(tmp, ADDRESSES_PATH)
    except Exception:                                            # noqa: BLE001
        pass


def _confirmed(client, entity_id, sp, ip, from_memory):
    """The speaker at ip, checked against the platform's record of it. Confirmed → the address is kept.
    From memory and NOT confirmed → refused, saying which (silent, or a different speaker now answers there)."""
    uid = speaker_identity(client, entity_id)
    try:
        seen = str(sp.uid or "")
    except Exception:                                            # noqa: BLE001
        seen = ""
    same = bool(uid and seen and seen.upper() == uid.upper())
    if same:
        remember_address(entity_id, uid, ip)
    elif from_memory:
        if not seen:
            raise SpeakerMusicError("the network doesn't report an address for this speaker, and it no longer "
                                    "answers at %s, the last address it answered at" % ip)
        raise SpeakerMusicError("the network doesn't report an address for this speaker, and a different speaker "
                                "now answers at %s, the last address it was seen at" % ip)
    return sp


def _links():
    try:
        with open(LINKS_PATH, encoding="utf-8") as fh:
            return _json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _links_write(d):
    _os.makedirs(_os.path.dirname(LINKS_PATH), exist_ok=True)
    tmp = LINKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(d, fh)
    _os.replace(tmp, LINKS_PATH)


def own_sign_in(household_id, service_id, serial):
    return ((_links().get(str(household_id)) or {}).get(str(int(service_id))) or {}).get(str(serial))


def save_sign_in(household_id, service_id, serial, token, key, name=""):
    d = _links()
    d.setdefault(str(household_id), {}).setdefault(str(int(service_id)), {})[str(serial)] = {
        "token": token, "key": key, "name": name or "", "linked_at": int(time.time())}
    _links_write(d)


def with_own_sign_in(household_id, acct):
    """The account with ProOS's own sign-in in place of the household's, when ProOS has one for it."""
    own = own_sign_in(household_id, getattr(acct, "service_id", 0), getattr(acct, "serial_number", ""))
    if not own:
        return acct
    mine = _copy.copy(acct)
    mine.token, mine.key = own.get("token") or "", own.get("key") or ""
    mine._proos_own = True
    return mine


def keep_fresh(sp, s):
    """A service may renew ProOS's own sign-in while answering (the library replaces it in memory): keep
    the renewed one, so the next start of Core doesn't hold a spent one."""
    a = s.get("account")
    if not getattr(a, "_proos_own", False):
        return
    own = own_sign_in(sp.household_id, a.service_id, a.serial_number) or {}
    if a.token and (a.token != own.get("token") or a.key != own.get("key")):
        save_sign_in(sp.household_id, a.service_id, a.serial_number, a.token, a.key, own.get("name") or "")


def _forget_service(household_id, service_id):
    with _LOCK:
        for k in [k for k in _SESSIONS if k[0] == household_id and k[1] == int(service_id)]:
            _SESSIONS.pop(k, None)
        for k in [k for k in _ANSWERED if k[0] == household_id and k[1] == int(service_id)]:
            _ANSWERED.pop(k, None)


def answers(sp, svc, now=None):
    """(answered, why) — the service's own first page asked with its default account: something there, or
    not. Kept ANSWERED_KEPT_S when it answered, REFUSED_KEPT_S when not."""
    now = now or time.time()
    key = (sp.household_id, svc["service_id"], svc["default"])
    kept = _ANSWERED.get(key)
    if kept and now - kept[1] < (ANSWERED_KEPT_S if kept[0] else REFUSED_KEPT_S):
        return kept[0], kept[2]
    try:
        if _apple(svc["service_id"]):
            from proos import applemusic as _am
            ok = bool(_am.charts(_apple_sf(), 1)) if _am.ready() else False
            why = None if ok else "Apple Music needs the installer's Apple token (Pro › Tech Tools › Assist)"
        else:
            s = _session(sp, svc["service_id"], svc["default"])
            with s["lock"]:
                ok = bool(list(s["br"].get_metadata(count=5).items))
            keep_fresh(sp, s)
            why = None if ok else "%s gave nothing to play" % svc["service"]
    except Exception as e:                                       # noqa: BLE001
        ok, why = False, "%s didn't answer (%s)" % (svc["service"], str(e)[:120])
    with _LOCK:
        _ANSWERED[key] = (ok, now, why)
    return ok, why


def can_link(sp, service_id):
    """True when the person can sign in to this service on its own web page (the library's link flow)."""
    if _apple(service_id):
        return False                 # Apple allows its sign-in only from the Sonos phone app (library's own words)
    try:
        from soco.music_services.music_service import MusicService
        return MusicService(service_names(sp).get(int(service_id)) or "", device=sp).auth_type in LINKABLE
    except Exception:                                            # noqa: BLE001
        return False


def services(sp):
    """The services to offer: those that ANSWER (Dave, 768). Those that don't are named with why; any of
    them the person can sign in to on the service's own page is listed under "connect"."""
    accounts, how = accounts_of(sp)
    every = list_services(accounts, service_names(sp))
    res = {}

    def one(x):
        res[x["service_id"]] = answers(sp, x)
    ts = [threading.Thread(target=one, args=(x,), daemon=True) for x in every]
    for t in ts:
        t.start()
    for t in ts:
        t.join(25)
    shown, connect, hidden = [], [], []
    for x in every:
        ok, why = res.get(x["service_id"], (False, "%s didn't answer in time" % x["service"]))
        if ok:
            x = dict(x, own_sign_in=bool(own_sign_in(sp.household_id, x["service_id"], x["default"])))
            shown.append(x)
        else:
            hidden.append(why)
            if can_link(sp, x["service_id"]):
                connect.append({"service_id": x["service_id"], "service": x["service"], "why": why})
    return {"services": shown, "connect": connect, "not_shown": hidden, "accounts": how}


def connect_begin(sp, service_id):
    """Start a sign-in of ProOS's own: the service's own sign-in page, for the person to open. Nothing is
    written to the speaker."""
    sid = int(service_id)
    name = service_names(sp).get(sid)
    if not name:
        raise SpeakerMusicError("these speakers don't know that service")
    if not can_link(sp, sid):
        raise SpeakerMusicError("%s can't be signed in from here — sign in from the speaker's own app" % name)
    from soco.music_services.onboarding import MusicServiceAccountManager
    try:
        mgr = MusicServiceAccountManager(name, device=sp)
        link = mgr.begin_link()
    except Exception as e:                                       # noqa: BLE001
        raise SpeakerMusicError("%s didn't give a sign-in page (%s)" % (name, str(e)[:160]))
    if not link.standalone_supported:
        raise SpeakerMusicError("%s only signs in from its own app" % name)
    with _LOCK:
        _PENDING[(sp.household_id, sid)] = (mgr, link, time.time())
    return {"service_id": sid, "service": name, "url": link.registration_url,
            "code": link.link_code if link.show_link_code else None}


def connect_finish(sp, service_id):
    """Finish it once the person has signed in: the service hands ProOS its own sign-in, kept on this box
    for the household's first-added account of that service (Dave's ruling 2). Not signed in yet →
    {"ok": False, "waiting": True}."""
    sid = int(service_id)
    p = _PENDING.get((sp.household_id, sid))
    if not p or time.time() - p[2] > LINK_WAIT_S:
        raise SpeakerMusicError("that sign-in has expired — start it again")
    mgr, link, _at = p
    name = service_names(sp).get(sid) or "the service"
    try:
        cred = mgr.get_device_auth_token(link)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "waiting": True, "why": str(e)[:160]}
    accounts, _how = accounts_of(sp)
    acct = choose_account(accounts, sid)
    if acct is None:
        raise SpeakerMusicError("these speakers have no %s account to play it with — add %s in the speaker's own app first" % (name, name))
    save_sign_in(sp.household_id, sid, acct.serial_number, cred.auth_token, cred.private_key, cred.nickname)
    with _LOCK:
        _PENDING.pop((sp.household_id, sid), None)
    _forget_service(sp.household_id, sid)
    return {"ok": True, "service_id": sid, "service": name}


def services_list_only(sp):
    """Every service the speaker holds, as before, unmeasured (Pro's test reads it)."""
    accounts, how = accounts_of(sp)
    return {"services": list_services(accounts, service_names(sp)), "accounts": how}


def _session(sp, service_id, account=None, time_zone=None):
    from soco.music_services.browser import MusicServiceBrowser
    accounts, _how = accounts_of(sp)
    acct = choose_account(accounts, int(service_id), serial=account)
    if acct is None:
        raise SpeakerMusicError("that account isn't set up for this service on these speakers")
    # A sign-in of ProOS's own (registers 775–778), when the household's is refused to ProOS — the
    # speaker still PLAYS with its own account (the queue's address carries that account, proven 777).
    acct = with_own_sign_in(sp.household_id, acct)
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
    """Remember an item so a later tap can play it (searches run side by side, so this is locked)."""
    with _ITEMS_LOCK:
        items = s["items"]
        items[item.item_id] = item
        items.move_to_end(item.item_id)
        while len(items) > ITEMS_KEPT:
            items.popitem(last=False)
    return item


def opens(item):
    """REGISTER 833 (seen on the box, Dave 13 Oct 16:40): Spotify's Your Music › Songs and Albums come as a trackList and an
    albumList that the service marks NOT browsable (canEnumerate false) but playable — yet the library can't play a list
    directly ("items of type 'tracklist' cannot be played directly; browse into the item"), and asked from Core they DO
    open: Songs 377 tracks, Albums 23 albums. So a list (a type ending in "list", the library's own type word) opens
    like any other container, and playing it plays what is in it."""
    if getattr(item, "can_browse", False):
        return True
    t = str(getattr(item, "item_type", "") or "").rsplit(".", 1)[-1].lower()
    return t.endswith("list")


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
            "open": opens(item), "play": can}


def home(sp, service_id, account=None, time_zone=None, sections=12, per=12):
    """The service's own home page — its sections in its own order, each with its first items."""
    s = _session(sp, service_id, account, time_zone)
    if _apple(service_id):
        from proos import applemusic as _am
        mine = _am.my_home(_apple_user(), _apple_sf(), per) if _apple_user() else []
        return {"service_id": int(service_id), "account": s["account"].serial_number,
                "sections": mine + _am.charts(_apple_sf(), per), "signed_in": bool(_apple_user())}
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
        # REGISTER 794 (Dave 11:35: "cant see and start a playlist"): a service whose home is its new content page
        # (Spotify) keeps the person's own library in its classic tree — read on the Office: Popular Playlists,
        # Charts, New Releases, Genres and Moods, Your Music (› Playlists, Songs, Albums). That tree's first level
        # goes on top as one shelf, in the service's own order and words, so the library is one tap away.
        if getattr(br, "_content_endpoint", ""):
            try:
                from soco.music_services.browser.models import _legacy_item
                recs = br._scoped_client().get_metadata("root", 0, 30, False, None, None)["items"]
                tiles = [card(_keep(s, _legacy_item(r, "smapi"))) for r in recs]
                if tiles:
                    out.insert(0, {"id": "browse", "title": "Browse", "artist": None, "art": None, "type": "container",
                                   "open": True, "play": False, "items": tiles})
            except Exception as e:                               # noqa: BLE001
                print("  [speakermusic] the service's classic tree didn't answer (%s)" % str(e)[:120], flush=True)
    keep_fresh(sp, s)
    return {"service_id": int(service_id), "account": s["account"].serial_number, "sections": out}


def open_item(sp, service_id, item_id, account=None, time_zone=None, index=0, count=50):
    s = _session(sp, service_id, account, time_zone)
    if _apple(service_id):
        from proos import applemusic as _am
        items = _am.tracks(item_id, _apple_sf(), count, user=_apple_user())
        return {"items": items, "index": 0, "total": len(items)}
    with s["lock"]:
        it = s["items"].get(item_id, item_id)
        res = s["br"].get_metadata(it, index=int(index), count=int(count))
        out = {"items": [card(_keep(s, i)) for i in res.items], "index": res.index, "total": res.total}
    keep_fresh(sp, s)
    return out


def search(sp, service_id, q, account=None, time_zone=None, categories=None, count=8):
    """Search the service in each of ITS OWN search categories (the service lists them).
    REGISTER 751 (measured from the dashboard): one after another, Spotify's five categories took 4.2 s.
    A signed-in account's searches each open their own connection to the service (the library does
    that), so they are asked SIDE BY SIDE; an account with no sign-in shares one connection, so its
    categories are still asked in turn."""
    s = _session(sp, service_id, account, time_zone)
    if _apple(service_id):
        from proos import applemusic as _am
        return {"q": q, "results": _am.search(q, _apple_sf(), count)}
    cats = list(categories or s["br"].available_search_categories or [])
    results = {}

    def one(cat):
        try:
            res = s["br"].search(cat, q, count=int(count))
            results[cat] = {"category": cat, "items": [card(_keep(s, i)) for i in res.items]}
        except Exception as e:                                   # noqa: BLE001
            results[cat] = {"category": cat, "items": [], "error": str(e)[:160]}

    if getattr(s["account"], "token", "") and len(cats) > 1:
        ts = [threading.Thread(target=one, args=(c,), daemon=True) for c in cats]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
    else:
        for c in cats:
            one(c)
    keep_fresh(sp, s)
    return {"q": q, "results": [results[c] for c in cats if c in results]}


def prepare(br, item):
    """(uri, metadata) for one playable item — the library builds both from the service's own item."""
    from soco.music_services.browser.playback import build_metadata, build_uri, resolve_item
    iid, typ, mime, title = resolve_item(br, item)
    uri = build_uri(br, iid, typ, mime)
    return uri, build_metadata(br, iid, title, typ, mime=mime, uri=uri)


def add_to_queue(sp, uri, meta, position=0, as_next=0):
    """Add one item to the speaker's queue WITH its details. REGISTER 748 (seen on the speaker): the
    library's add_uri_to_queue(uri, x) takes x as the queue POSITION — every earlier play sent the
    speaker no title, so the dashboard showed nothing. The speaker's own AddURIToQueue is called with
    the metadata in its own field; the speaker then fills in artist, album and artwork itself."""
    return add_to_queue_at(sp, uri, meta, position, as_next)[0]


def add_to_queue_at(sp, uri, meta, position=0, as_next=0):
    """(tracks added, the queue number the first one landed at) — both the speaker's own answer."""
    r = sp.avTransport.AddURIToQueue([
        ("InstanceID", 0), ("EnqueuedURI", uri), ("EnqueuedURIMetaData", meta),
        ("DesiredFirstTrackNumberEnqueued", int(position or 0)), ("EnqueueAsNext", 1 if as_next else 0)])
    try:
        first_no = int(r.get("FirstTrackNumberEnqueued") or 0)
    except (TypeError, ValueError):
        first_no = 0
    return int(r.get("NumTracksAdded") or 0), first_no



def enqueue(sp, br, item):
    uri, meta = prepare(br, item)
    return add_to_queue(sp, uri, meta)


_FILLS = {}             # speaker -> the newest play's number; an older play stops adding to the queue


def apple_prepare(br, c):
    """(uri, metadata) for one Apple song by its catalogue id — the speaker's own Apple track address
    (proven on Dave's Sonos, register 742: PLAYING 4.8 s)."""
    from soco.music_services.browser.playback import build_metadata, build_uri
    from proos import applemusic as _am
    iid = "song:%s" % _am.split(c["id"])[1]
    uri = build_uri(br, iid, "track", "audio/mp4")
    return uri, build_metadata(br, iid, c.get("title") or "", "track", mime="audio/mp4", uri=uri)


def play(sp, service_id, item_id, account=None, time_zone=None, wait_s=10.0, limit=100, mode="now"):
    """Play an item on the speaker: a playlist/album as the speaker's own queue (first track started,
    the rest added behind it), a track or stream on its own. The answer is the SPEAKER's state.
    REGISTER 751 (seen from the dashboard): the queue fill held the service for its whole length, so a
    search typed straight after waited ~10 s behind it. Now the service is held only to prepare each
    track; the speaker is told outside it, and a newer play on the same speaker stops an older fill."""
    s = _session(sp, service_id, account, time_zone)
    br = s["br"]
    if _apple(service_id):
        from proos import applemusic as _am
        songs = _am.tracks(item_id, _apple_sf(), limit, user=_apple_user())
        if not songs:
            raise SpeakerMusicError("Apple Music has no songs to play for that")
        title = (next((c.get("title") for c in songs if c["id"] == item_id), None)
                 or _am.name(item_id, _apple_sf(), _apple_user()) or str(item_id))
        if mode in ("next", "add"):
            return _enqueue(sp, s, songs, lambda c: apple_prepare(br, c), mode, title)
        return _queue_play(sp, s, songs, lambda c: apple_prepare(br, c), title, wait_s)
    with s["lock"]:
        it = s["items"].get(item_id, item_id)
        if opens(it):
            tracks = [t for t in br.get_metadata(it, count=int(limit)).items if not opens(t)]
            if not tracks:
                raise SpeakerMusicError("there's nothing to play directly inside “%s” — open it to choose" % it.title)
        else:
            tracks = [it]
            first = prepare(br, it)
    if not opens(it) and is_stream(first[0]):
        if mode in ("next", "add"):
            raise SpeakerMusicError("a station can't go in the queue — play it instead")
        return _stream_play(sp, first, getattr(it, "title", "") or "", wait_s)
    one = locals().get("first")
    if mode in ("next", "add"):
        return _enqueue(sp, s, tracks, lambda t: one if (one is not None and t is it) else prepare(br, t),
                        mode, getattr(it, "title", "") or "")
    return _queue_play(sp, s, tracks, lambda t: one if (one is not None and t is it) else prepare(br, t),
                       getattr(it, "title", "") or "", wait_s)


# PLAY NEXT / ADD TO QUEUE (register 785; Dave 10:57 "go" on 781). Into the SPEAKER's own queue — the speaker's own
# AddURIToQueue with its own position fields: "next" goes straight after the track playing now (its own queue position
# + 1), "add" goes at the end. What is said is the speaker's own count of tracks added. A speaker playing something
# that isn't its queue (a station) keeps playing it; the tracks wait in the queue, and the answer says so.
def _enqueue(sp, s, tracks, prep, mode, title):
    try:
        on_queue = str((sp.get_current_media_info() or {}).get("uri") or "").startswith("x-rincon-queue:")
    except Exception:                                            # noqa: BLE001
        on_queue = False
    pos = 0
    if mode == "next" and on_queue:
        try:
            pos = int((sp.get_current_track_info() or {}).get("playlist_position") or 0) + 1
        except (TypeError, ValueError):
            pos = 0
    with s["lock"]:
        first = prep(tracks[0])
    added, first_no = add_to_queue_at(sp, first[0], first[1], pos, mode == "next" and pos > 0)
    # DAVE'S RULING B (12 Oct 11:41): "Play next" while a STATION plays (it never ends, so "next" never comes)
    # stops the station and plays it now from the queue, then carries on through the queue.
    started, state = False, ""
    if mode == "next" and not on_queue and added and first_no:
        sp.play_from_queue(first_no - 1)
        started = True
        t0 = time.time()
        while time.time() - t0 < 10:
            state = (sp.get_current_transport_info() or {}).get("current_transport_state") or ""
            if state == "PLAYING":
                break
            time.sleep(0.25)

    # REGISTER 792: a newer play or a Clear on this speaker stops this fill too (it reads the speaker's fill number,
    # without taking it, so adding a song never stops a playlist that is still filling).
    key = getattr(sp, "ip_address", None) or id(sp)
    gen = _FILLS.get(key, 0)

    def rest():
        for k, t in enumerate(tracks[1:], start=1):
            if _FILLS.get(key, 0) != gen:
                return
            try:
                with s["lock"]:
                    uri, meta = prep(t)
                if _FILLS.get(key, 0) != gen:
                    return
                add_to_queue(sp, uri, meta, (pos + k) if pos else 0, mode == "next" and pos > 0)
            except Exception:                                    # noqa: BLE001
                pass
    if len(tracks) > 1:
        threading.Thread(target=rest, daemon=True).start()
    out = {"ok": added > 0, "mode": mode, "title": title, "queued": len(tracks) if added else 0,
           "on_queue": on_queue, "position": pos or None, "started": started}
    if started:
        out.update(ok=state == "PLAYING", state=state)
    return out


# REGISTER 782 (seen on the box, Dave 10:38): a Pandora station from "My Stations" answered "UPnP Error 800" —
# Core put it in the speaker's QUEUE, and a station can't be queued. The speaker's own address says what a thing
# is: a station or stream starts directly (the speaker's own SetAVTransportURI — how the same Pandora program
# played on the Office in 777, PLAYING 5.8 s); a track goes in the queue as before. These are the speaker's own
# address kinds for a station or stream (the library's and the speaker's words, read from the box).
STREAM_SCHEMES = ("x-sonosapi-radio:", "x-sonosapi-stream:", "x-sonosapi-hls:", "x-rincon-mp3radio:", "aac:", "hls-radio:")


def is_stream(uri):
    return str(uri or "").startswith(STREAM_SCHEMES)


def _stream_play(sp, first, title, wait_s):
    """Start a station or stream directly on the speaker (no queue); a newer play stops any older fill."""
    key = getattr(sp, "ip_address", None) or id(sp)
    with _LOCK:
        _FILLS[key] = _FILLS.get(key, 0) + 1
    uri, meta = first
    t0 = time.time()
    sp.play_uri(uri, meta=meta, title=title)
    state = ""
    while time.time() - t0 < wait_s:
        state = (sp.get_current_transport_info() or {}).get("current_transport_state") or ""
        if state == "PLAYING":
            break
        time.sleep(0.25)
    return {"ok": state == "PLAYING", "state": state, "seconds": round(time.time() - t0, 1),
            "title": title, "queued": 0}


def _queue_play(sp, s, tracks, prep, title, wait_s):
    """The speaker's own queue: cleared, the first track started, the rest added behind it; the
    service held only to prepare each track; a newer play on this speaker stops this fill."""
    key = getattr(sp, "ip_address", None) or id(sp)
    with _LOCK:
        gen = _FILLS[key] = _FILLS.get(key, 0) + 1
    with s["lock"]:
        first = prep(tracks[0])
    t0 = time.time()
    sp.clear_queue()
    add_to_queue(sp, *first)
    sp.play_from_queue(0)

    def rest():
        for t in tracks[1:]:
            if _FILLS.get(key) != gen:
                return                                           # a newer play took this speaker
            try:
                with s["lock"]:
                    uri, meta = prep(t)
                if _FILLS.get(key) != gen:
                    return
                add_to_queue(sp, uri, meta)
            except Exception:                                    # noqa: BLE001
                pass

    threading.Thread(target=rest, daemon=True).start()
    state = ""
    while time.time() - t0 < wait_s:
        state = (sp.get_current_transport_info() or {}).get("current_transport_state") or ""
        if state == "PLAYING":
            break
        time.sleep(0.25)
    return {"ok": state == "PLAYING", "state": state, "seconds": round(time.time() - t0, 1),
            "title": title, "queued": len(tracks)}


# WHICH SPEAKERS THIS SERVICE CAN TALK TO. A fact about the libraries, so it lives here — Assist and the
# pages ask, and never name a brand themselves. Sonos through SoCo (this module); HEOS through the
# platform's own HEOS integration plus the player's search (heosmusic.py, stage 5, registers 766–768).
SPEAKS_PLATFORMS = ("sonos", "heos")


def can_speak(entity_entry):
    """True when this registry entry is a speaker this service can talk to."""
    return (str((entity_entry or {}).get("entity_id") or "").startswith("media_player.")
            and (entity_entry or {}).get("platform") in SPEAKS_PLATFORMS)


def platform_of(client, entity_id):
    """The integration that owns this speaker, as the platform's own registry says."""
    try:
        return next((e.get("platform") for e in client.entity_registry() or []
                     if e.get("entity_id") == entity_id), None)
    except Exception:                                            # noqa: BLE001
        return None


def for_speaker(client, network_info, entity_id):
    """(the brand's module, the speaker ready to talk to). Both modules answer the same calls in the same
    shapes — services, home, open_item, search, play, assist_service — so nothing above names a brand."""
    if platform_of(client, entity_id) == "heos":
        from proos import heosmusic as _hm
        return _hm, _hm.connect(client, entity_id)
    import sys as _sys
    return _sys.modules[__name__], connect(client, network_info, entity_id)


def assist_service(sp, attrs):
    """(service id, account, name) Assist searches on a Sonos when nobody named a service (pick_service).
    Apple Music is searched through Apple's catalogue with the installer's token (stage 4); without one
    it can't be searched, so it is not chosen for a spoken request."""
    from proos import applemusic as _am
    accounts, _how = accounts_of(sp)
    names = service_names(sp)
    # Only a service that ANSWERS is searched (Dave, 768) — one refused to ProOS (Pandora before it is
    # signed in, 775) or Apple without the token is skipped, never tried and failed on a spoken request.
    shown = set(x["service_id"] for x in services(sp)["services"])
    skip = set(x["service_id"] for x in list_services(accounts, names)) - shown
    if not _am.ready():
        skip.add(APPLE_MUSIC_SID)
    svc = pick_service(accounts, names, (attrs or {}).get("media_content_id") or "", tuple(skip))
    if not svc:
        return None
    return svc[0], svc[1], names.get(svc[0]) or "the music service"


# ── For Assist (music design stage 3, register 753) ────────────────────────────────────────
def network_info():
    """The Supervisor's network report (where the box's own address is read from)."""
    try:
        from proos import sysadmin as _sa
        return _sa._info("/network/info") or {}
    except Exception:                                            # noqa: BLE001
        return {}


_TZ = {}


def home_time_zone(client):
    """The home's time zone as the platform holds it."""
    if "tz" not in _TZ:
        try:
            _TZ["tz"] = (client._req("GET", "/api/config") or {}).get("time_zone") or None
        except Exception:                                        # noqa: BLE001
            return None
    return _TZ["tz"]


def pick_service(accounts, names, playing_uri="", skip=()):
    """(service id, account) Assist searches when nobody named a service. Read, in this order, from the
    speaker itself: the service it is PLAYING now (its own address says sid=…); else the first service
    whose first-added account is signed in (a service that needs no sign-in has little to search);
    else the first the speaker lists. None when the home has no service at all. skip: services that
    can't be searched right now (Apple Music without the installer's token)."""
    import re as _re
    svcs = [x for x in list_services(accounts, names) if x["service_id"] not in set(skip or ())]
    if not svcs:
        return None
    m = _re.search(r"[?&]sid=(\d+)", playing_uri or "")
    now = m and next((x for x in svcs if x["service_id"] == int(m.group(1))), None)
    if now:
        return now["service_id"], now["default"]
    by_serial = {(getattr(a, "service_id", None), getattr(a, "serial_number", None)): a for a in accounts or []}
    for x in svcs:
        a = by_serial.get((x["service_id"], x["default"]))
        if a is not None and getattr(a, "token", ""):
            return x["service_id"], x["default"]
    return svcs[0]["service_id"], svcs[0]["default"]


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
        svc = services_list_only(sp)
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


# ════════════════════════════════════════════════════════════════════════════════════════════════
# THE SPEAKER'S OWN QUEUE — see it, play from it, remove, move (register 790; queue build 2, Dave 12:28 "Go").
# The platform's sonos.get_queue lists titles but no artwork, and the platform has no move for Sonos; the speaker's
# own interface (SoCo, which Core already speaks to it with) has both — so the queue is read and changed there.
# Each item's id is its place in the queue ("pos:<n>", counted from 0) as read; the page reads the queue again
# after every change, so an id is never used against a queue that has moved under it.
# ════════════════════════════════════════════════════════════════════════════════════════════════
QUEUE_READ_MAX = 500    # plumbing: the most queue items read for the page at once


def _pos(item):
    s = str(item or "")
    if not s.startswith("pos:"):
        raise SpeakerMusicError("that isn't an item in this queue")
    return int(s[4:])


def queue(sp):
    try:
        on_queue = str((sp.get_current_media_info() or {}).get("uri") or "").startswith("x-rincon-queue:")
    except Exception:                                            # noqa: BLE001
        on_queue = False
    cur = None
    if on_queue:
        try:
            cur = int((sp.get_current_track_info() or {}).get("playlist_position") or 0) - 1
        except (TypeError, ValueError):
            cur = None
    ip = getattr(sp, "ip_address", "")
    out = []
    for n, it in enumerate(list(sp.get_queue(max_items=QUEUE_READ_MAX) or [])):
        art = getattr(it, "album_art_uri", "") or ""
        if art.startswith("/") and ip:
            art = "http://%s:1400%s" % (ip, art)
        out.append({"queue_item_id": "pos:%d" % n, "name": getattr(it, "title", "") or "",
                    "artist": getattr(it, "creator", "") or "", "image": art or None})
    return {"items": out, "current_index": cur if (cur is not None and cur >= 0) else None, "on_queue": on_queue,
            "can_move": True, "can_remove": True, "can_save_queue": True}


def queue_remove(sp, item):
    sp.remove_from_queue(_pos(item))
    return {"ok": True}


def queue_move(sp, item, shift):
    """Move one item by `shift` places — the speaker's own ReorderTracksInQueue (1-based; InsertBefore is a place
    in the queue as it stands before the move)."""
    i = _pos(item)
    j = max(0, i + int(shift or 0))
    if j == i:
        return {"ok": True}
    sp.avTransport.ReorderTracksInQueue([("InstanceID", 0), ("StartingIndex", i + 1), ("NumberOfTracks", 1),
                                         ("InsertBefore", (j + 2) if j > i else (j + 1)), ("UpdateID", 0)])
    return {"ok": True}


def queue_play(sp, item):
    sp.play_from_queue(_pos(item))
    return {"ok": True}


# THE SPEAKER'S OWN PLAYLISTS (register 828; Dave 13 Oct 15:08 "go 3"; scoped in 781). A Sonos playlist is the
# speaker's own (the library's get_sonos_playlists / create_sonos_playlist; the speaker's AddURIToSavedQueue). A
# streaming service's own playlist (Spotify, Pandora) can't be added to — neither the speaker nor the platform offers
# it. What is said is the speaker's own count of the playlist, read before and after.
def playlists(sp):
    out = [{"id": p.item_id, "title": p.title} for p in (sp.get_sonos_playlists(complete_result=True) or [])]
    return {"playlists": out, "can_add": True, "can_create": True, "can_save_queue": True}


def _playlist_state(sp, playlist_id):
    """(the speaker's own update number for the playlist, how many tracks it holds)."""
    r, _ = sp.music_library._music_lib_search(playlist_id, 0, 1)
    return r.get("UpdateID"), int(r.get("TotalMatches") or 0)


def _add_saved(sp, playlist_id, update_id, uri, meta):
    r = sp.avTransport.AddURIToSavedQueue([
        ("InstanceID", 0), ("UpdateID", update_id), ("ObjectID", playlist_id), ("EnqueuedURI", uri),
        ("EnqueuedURIMetaData", meta), ("AddAtIndex", 4294967295)])
    return r.get("NewUpdateID", update_id)


def add_to_playlist(sp, service_id, item_id, playlist_id=None, title=None, account=None, time_zone=None, limit=100):
    """Add a song, album or playlist from a service to one of the speaker's own playlists (or a new one)."""
    s = _session(sp, service_id, account, time_zone)
    br = s["br"]
    if _apple(service_id):
        from proos import applemusic as _am
        tracks = _am.tracks(item_id, _apple_sf(), limit, user=_apple_user())
        prep = (lambda c: apple_prepare(br, c))
        name = (next((c.get("title") for c in tracks if c["id"] == item_id), None)
                or _am.name(item_id, _apple_sf(), _apple_user()) or str(item_id))
    else:
        with s["lock"]:
            it = s["items"].get(item_id, item_id)
            tracks = ([t for t in br.get_metadata(it, count=int(limit)).items if not opens(t)]
                      if opens(it) else [it])
        prep = (lambda t: prepare(br, t))
        name = getattr(it, "title", "") or ""
    if not tracks:
        raise SpeakerMusicError("there's nothing in that to add")
    with s["lock"]:
        first = prep(tracks[0])
    if is_stream(first[0]):
        raise SpeakerMusicError("a station can't go in a playlist")
    if not playlist_id:
        t = (title or "").strip()
        if not t:
            raise SpeakerMusicError("a new playlist needs a name")
        playlist_id = sp.create_sonos_playlist(t).item_id
    uid, before = _playlist_state(sp, playlist_id)
    uid = _add_saved(sp, playlist_id, uid, *first)
    _uid, after = _playlist_state(sp, playlist_id)
    if after <= before:
        raise SpeakerMusicError("the speaker didn't add it (the playlist still holds %d)" % after)

    def rest():
        u = _uid
        for t in tracks[1:]:
            try:
                with s["lock"]:
                    uri, meta = prep(t)
                u = _add_saved(sp, playlist_id, u, uri, meta)
            except Exception:                                    # noqa: BLE001
                pass
    if len(tracks) > 1:
        threading.Thread(target=rest, daemon=True).start()
    return {"ok": True, "title": name, "playlist_id": playlist_id, "added": after - before, "holds": after,
            "more": len(tracks) - 1}


# ADD TO SONOS FAVOURITES (register 841; Dave 13 Oct 18:12 "yes okay" to a proof on the Office). READ FIRST: the library
# (soco 97dba03) only READS favourites (FV:2). The Sonos app ADDS one with the speaker's own ContentDirectory CreateObject on
# FV:2 — a capture of the app doing it (github gist ghcs27/4abc9101…): one <item> with dc:title, r:type instantPlay, a <res>
# carrying the address, r:description (the service's name) and r:resMD (the item's own metadata, escaped); the speaker
# answers with the new favourite's ObjectID (FV:2/<n>). A song or station uses the library's own address and metadata
# (prepare). A playlist or album uses the container address the library's share-link plugin uses (album 1004206c,
# playlist 1006206c, x-rincon-cpcontainer:) with this account's descriptor. NOT YET SEEN WORKING: said added only when the
# speaker returns an FV:2 object AND the favourite is then in its own list.
FAV_CONTAINER = {"album": ("1004206c", "object.container.album.musicAlbum"),
                 "playlist": ("1006206c", "object.container.playlistContainer")}


def _x(v):
    from xml.sax.saxutils import escape
    return escape(str(v or ""), {'"': "&quot;"})


def _didl(inner):
    return ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            + inner + '</DIDL-Lite>')


def favourite_elements(title, uri, meta, service, art=None):
    """The <item> the Sonos app sends to make a favourite (the capture's shape). REGISTER 847 (Dave 14 Oct 06:30: "adds it
    without the artwork"): a favourite's picture is its own upnp:albumArtURI (what the speaker, the Sonos app and the
    platform show); the item's art from the service goes on the favourite and inside its metadata."""
    scheme = str(uri).split(":", 1)[0]
    art_el = ('<upnp:albumArtURI>%s</upnp:albumArtURI>' % _x(art)) if art else ''
    if art and 'albumArtURI' not in str(meta):
        meta = str(meta).replace('</item>', art_el + '</item>', 1)
    return _didl('<item id="" parentID="FV:2" restricted="false"><dc:title>%s</dc:title>'
                 '<upnp:class>object.itemobject.item.sonos-favorite</upnp:class><r:ordinal>-1</r:ordinal>%s'
                 '<res protocolInfo="%s:*:*:*">%s</res><r:type>instantPlay</r:type><r:description>%s</r:description>'
                 '<r:resMD>%s</r:resMD></item>' % (_x(title), art_el, _x(scheme), _x(uri), _x(service), _x(meta)))


def _escape_id(item_id):
    """The library's own id escaping for an address (soco's playback.escape_id)."""
    from soco.music_services.browser.playback import escape_id
    return escape_id(item_id)


def _container_address(br, item_id, title, kind):
    key, klass = FAV_CONTAINER[kind]
    enc = _escape_id(item_id)
    desc = br.account.udn or br.music_service.desc
    meta = _didl('<item id="%s%s" parentID="-1" restricted="true"><dc:title>%s</dc:title><upnp:class>%s</upnp:class>'
                 '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">%s</desc></item>'
                 % (key, enc, _x(title), klass, _x(desc)))
    return "x-rincon-cpcontainer:%s%s" % (key, enc), meta


def _fav_kind(it):
    t = str(getattr(it, "item_type", "") or "").rsplit(".", 1)[-1].lower()
    if t in FAV_CONTAINER:
        return t
    if "album" in t:
        return "album"
    if "playlist" in t or (opens(it) and t in ("container", "")):
        return "playlist"
    return None


def add_favourite(sp, service_id, item_id, account=None, time_zone=None):
    if _apple(service_id):
        raise SpeakerMusicError("an Apple Music item can't be made a Sonos favourite from here yet")
    s = _session(sp, service_id, account, time_zone)
    br = s["br"]
    with s["lock"]:
        it = s["items"].get(item_id, item_id)
        title = getattr(it, "title", "") or str(item_id)
        art = getattr(it, "album_art_uri", "") or None
        if opens(it):
            kind = _fav_kind(it)
            if not kind:
                raise SpeakerMusicError("“%s” can't be made a favourite — open it and choose" % title)
            uri, meta = _container_address(br, getattr(it, "item_id", item_id), title, kind)
        else:
            uri, meta = prepare(br, it)
    service = service_names(sp).get(int(service_id)) or ""
    r = sp.contentDirectory.CreateObject([("ContainerID", "FV:2"), ("Elements", favourite_elements(title, uri, meta, service, art))])
    oid = str((r or {}).get("ObjectID") or "")
    if not oid.startswith("FV:2/"):
        raise SpeakerMusicError("the speaker didn't make the favourite (it answered %s)" % (oid or "nothing"))
    ids = {getattr(f, "item_id", None) for f in (sp.music_library.get_sonos_favorites(complete_result=True) or [])}
    if oid not in ids:
        raise SpeakerMusicError("the speaker answered %s, but it isn't in its favourites" % oid)
    return {"ok": True, "title": title, "favourite_id": oid}


def save_queue(sp, title):
    """The whole queue saved as a new playlist — the speaker's own SaveQueue (the library's
    create_sonos_playlist_from_queue); said from the new playlist's own count, read back."""
    t = (title or "").strip()
    if not t:
        raise SpeakerMusicError("the playlist needs a name")
    if not list(sp.get_queue(max_items=1) or []):
        raise SpeakerMusicError("the queue is empty — there's nothing to save")
    pl = sp.create_sonos_playlist_from_queue(t)
    _u, holds = _playlist_state(sp, pl.item_id)
    if not holds:
        raise SpeakerMusicError("the speaker made %s but it holds nothing" % t)
    return {"ok": True, "title": t, "playlist_id": pl.item_id, "holds": holds}


def queue_clear(sp):
    """REGISTER 792 (Dave 13:33: "clear queue i had to hit 4-5 times to clear it it seemed to do it in stages"):
    Core was still ADDING the rest of a playlist behind the first track when Clear emptied the queue, so the queue
    filled again behind each Clear. Clear now stops any fill on this speaker first, then empties the speaker's own
    queue — and says how many it holds afterwards (the speaker's own count)."""
    key = getattr(sp, "ip_address", None) or id(sp)
    with _LOCK:
        _FILLS[key] = _FILLS.get(key, 0) + 1
    sp.clear_queue()
    try:
        left = len(list(sp.get_queue(max_items=1) or []))
    except Exception:                                            # noqa: BLE001
        left = None
    return {"ok": left == 0, "left": left}
