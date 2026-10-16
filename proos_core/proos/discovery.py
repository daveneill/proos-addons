"""
Device discovery.

Core finds a room's AV devices itself, the same way the dashboard does -- by
asking HA which media_players live in an area and which integration each belongs
to, then mapping integration -> role. No hardcoded entity IDs.

Done server-side via one template render (REST), so it stays stdlib-only and
needs no WebSocket. Disabled entities (e.g. the dead Cast device) are excluded
automatically because area_entities() doesn't return them.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field

# Which integrations play which role in a home-theatre stack.
ROLE_BY_INTEGRATION = {
    # the screen
    "samsungtv": "display", "samsungtv_smart": "display", "webostv": "display", "bravia_tv": "display",
    "lg_netcast": "display", "philips_js": "display",
    # HDMI source devices
    "apple_tv": "source", "androidtv_remote": "source", "firetv": "source",
    "roku": "source", "kodi": "source", "cast": "source",
    # audio endpoints (incl. AV receivers — a Denon/Marantz AVR IS the room's audio path)
    "sonos": "audio", "heos": "audio", "denonavr": "audio",
}

# Integrations we bother classifying. (cast is listed but usually a dead/duplicate
# entity in this house, and gets filtered as disabled before it reaches us.)
# NB: this is the AV media_player discovery filter — ONLY media_player integrations
# belong here. The non‑AV certified classes (lighting/climate/security) are classified
# via CLASS_BY_INTEGRATION below and must NEVER be added here, or discover_av would try
# to treat a light/thermostat/alarm as a media player.
_KNOWN = list(dict.fromkeys(list(ROLE_BY_INTEGRATION) + ["cast"]))

# ── NON‑AV CERTIFIED CLASSES ─────────────────────────────────────────────────
# ProOS certifies beyond AV: lighting, climate and security devices get the same
# awareness (independent reachability) + recovery (safe self‑heal) as AV — the machinery
# is generic. These are classified here (NOT in ROLE_BY_INTEGRATION) so they stay out of
# media‑player discovery while still earning a certified tier, capabilities and monitoring.
CLASS_BY_INTEGRATION = {
    "lifx": "lighting", "shelly": "lighting", "wiz": "lighting",
    "coolmaster": "climate",
    "elkm1": "security",
}


def device_class(integration: str):
    """The ProOS class for an integration — AV role (display/source/audio) OR a non‑AV
    class (lighting/climate/security). Single brand‑agnostic lookup for the whole home."""
    return ROLE_BY_INTEGRATION.get(integration) or CLASS_BY_INTEGRATION.get(integration)

# ── CERTIFICATION TIER ───────────────────────────────────────────────────────
# Every integration in ROLE_BY_INTEGRATION is ALLOWLISTED: it can be commissioned and
# used. A CERTIFIED integration is one ProOS ships/validates -- it adds the discrete
# commands native HA integrations routinely omit (discrete input select, discrete power),
# and it is covered by awareness monitoring + self-heal. Native integrations aren't built
# for bulletproof one-touch because that isn't HA's audience; it is ProOS's. Certified is
# a STRICT SUBSET of the allowlist. Everything else on the allowlist is "compatible"
# (usable, best-effort control via whatever the native driver exposes -- often HDMI-CEC --
# and NOT guaranteed-monitored/supported). Anything off the allowlist is "unsupported"
# and never enters commissioning.
# The certified badge is EARNED, not assigned: to be certified an integration must meet
# the ProOS AV Certification Standard for its role (see ProOS_Certification_Standard.md).
# CERTIFIED_CAPABILITIES records what each currently-certified integration is validated to
# provide; the commissioning UI shows them, and a commission-time capability check can
# confirm they're actually present on THIS box (e.g. a certified display must really expose
# discrete HDMI inputs) before the badge is granted for a given device.
#
# Capability vocabulary (the standard's checklist):
#   discrete_power  - explicit power ON and OFF (not a toggle)
#   discrete_input  - named input/source selection (displays & AVRs)
#   reliable_state  - trustworthy power/playback/source reporting (feeds awareness verdicts)
#   awareness       - exposes a reachability/second signal + safe reload for self-heal
#   tv_audio        - can carry TV audio into the room (audio endpoints)
# STAGE 5 BUILD 4 (16 Aug 2026): CERTIFICATION TRUTH. Four integrations
# were certified BEYOND THEIR EVIDENCE — each claimed `reliable_state`
# while the product's own record documents the opposite:
#   apple_tv         — the traffic-witness option, the two-signal system
#                      and the display-input map all exist BECAUSE its
#                      state lies ("Companion flakiness"; "an Apple TV
#                      that falsely reports 'off'").
#   samsungtv_smart  — the watcher's doctrine: "integration state lags,
#                      goes stale, or settles to 'off' within seconds of
#                      a real outage"; Frame art-mode makes even on/off
#                      ambiguous (A-8, D-2).
#   androidtv_remote — the input map exists because a Shield "mis-reports
#                      its own power"; the paired-remote resolution exists
#                      because its ids lie.
#   heos             — media_pause/media_play_pause both 500, observed
#                      live; stops to 'idle' instead of pausing.
# All four DEMOTED to compatible: still usable, still commissioned, still
# monitored — just no longer wearing a claim the record contradicts. The
# generator's Display On for these panels uses the honest warm-up delay
# instead of waiting on a state the record distrusts. Re-certifying any
# of them is an ON-BOX capability validation plus Dave's approval — never
# an edit to this set alone (certification_truth_bench.py machine-checks
# that every member meets its role's bar).
CERTIFIED_INTEGRATIONS = {
    # AV
    "sonos", "denonavr",
    # lighting
    "lifx", "shelly", "wiz",
    # climate
    "coolmaster",
    # security
    "elkm1",
}

# Capability vocabulary (extended for the non‑AV classes):
#   AV:        discrete_power · discrete_input · reliable_state · awareness · tv_audio
#   lighting:  discrete_onoff · level · color        (level/color optional per device)
#   climate:   mode_setpoint  (hvac mode + target temp)
#   security:  arm_state · zone_state
# reliable_state + awareness are UNIVERSAL — every certified class must have them.
CERTIFIED_CAPABILITIES = {
    # AV — samsungtv_smart / apple_tv / androidtv_remote / heos removed with
    # their badges (build 4 above): their capability entries were part of the
    # unearned claim. Their real, observed behaviours still work exactly as
    # before — discrete inputs are read live from the panel's own source_list
    # (_discrete_inputs), never from this table.
    "sonos":           {"tv_audio", "reliable_state", "awareness"},
    "denonavr":        {"discrete_power", "discrete_input", "tv_audio", "reliable_state", "awareness"},
    # lighting (local, push/near‑real‑time state)
    "lifx":            {"discrete_onoff", "level", "color", "reliable_state", "awareness"},
    "wiz":             {"discrete_onoff", "level", "color", "reliable_state", "awareness"},
    "shelly":          {"discrete_onoff", "level", "reliable_state", "awareness"},  # dimmer where present
    # climate
    "coolmaster":      {"mode_setpoint", "reliable_state", "awareness"},
    # security
    "elkm1":           {"arm_state", "zone_state", "reliable_state", "awareness"},
}


# ── THE MINIMUM BAR ──────────────────────────────────────────────────────────
# The capabilities an integration MUST provide (AND verify on-box) to be CERTIFIED
# for a role -- the "complete package". This is the machine-readable form of
# ProOS_Certification_Standard.md, and it is BRAND-AGNOSTIC: certifying a new TV
# brand later means declaring it meets these caps, with ZERO new logic in the
# generator/watcher/UI (they all key off capabilities, never a brand name).
#
# Universal caps (reliable_state + awareness) are folded into every role below.
# 'stable identity' and 'safe self-heal' from the written standard are behavioural
# process checks, not runtime attributes, so they gate entry to CERTIFIED_INTEGRATIONS
# rather than appearing here.
REQUIRED_CAPABILITIES = {
    # AV
    "display": {"discrete_power", "discrete_input", "reliable_state", "awareness"},
    "source":  {"discrete_power", "reliable_state", "awareness"},
    "audio":   {"tv_audio", "reliable_state", "awareness"},
    # non‑AV classes — minimum bar per class (level/color/zone_state are per‑device
    # extras, not required, so a plain relay or a zone‑less panel still certifies)
    "lighting": {"discrete_onoff", "reliable_state", "awareness"},
    "climate":  {"mode_setpoint", "reliable_state", "awareness"},
    "security": {"arm_state", "reliable_state", "awareness"},
}


def tier(integration: str) -> str:
    if integration in CERTIFIED_INTEGRATIONS:
        return "certified"
    if integration in ROLE_BY_INTEGRATION:
        return "compatible"
    return "unsupported"


def capabilities(integration: str) -> list:
    """What a certified integration is validated to provide (empty for compatible)."""
    return sorted(CERTIFIED_CAPABILITIES.get(integration, []))


def has_capability(integration: str, cap: str) -> bool:
    """Does this integration provide a specific certified capability? The single
    brand-agnostic question the generator/watcher/UI ask instead of 'is it a Samsung'."""
    return cap in CERTIFIED_CAPABILITIES.get(integration, set())


def missing_capabilities(integration: str, role: str) -> list:
    """Which required capabilities this integration lacks for a role (empty = meets
    the bar). Drives the honest 'certified driver, capability X not verified' UI."""
    req = REQUIRED_CAPABILITIES.get(role, set())
    have = set(CERTIFIED_CAPABILITIES.get(integration, set()))
    return sorted(req - have)


CERTIFIED_VERSIONS = {
    "samsungtv_smart": "1.0",
    "apple_tv": "1.0",
    "sonos": "1.0",
    "androidtv_remote": "1.0",
    "denonavr": "1.0",
    "heos": "1.0",
    "lifx": "1.0",
    "wiz": "1.0",
    "shelly": "1.0",
    "coolmaster": "1.0",
    "elkm1": "1.0",
}


def driver_version(integration: str):
    return CERTIFIED_VERSIONS.get(integration)

_TEMPLATE = """
{%% set area = %s %%}
{%% set known = %s %%}
{%% set ae = area_entities(area) | select('match','media_player\\.') | list %%}
{%% set ns = namespace(rows=[]) %%}
{%% for e in ae %%}
  {%% set integ = namespace(v='unknown') %%}
  {%% for k in known %%}
    {%% if e in integration_entities(k) %%}{%% set integ.v = k %%}{%% endif %%}
  {%% endfor %%}
  {%% set ns.rows = ns.rows + [{'entity': e, 'name': state_attr(e,'friendly_name'), 'integration': integ.v, 'device_class': state_attr(e,'device_class'), 'model': device_attr(device_id(e),'model')}] %%}
{%% endfor %%}
{{ ns.rows | to_json }}
"""


# ── ONE BOX SEEN TWICE (5 Sep 2026, register 315) ────────────────────────────
# A streamer or panel that speaks two protocols is registered by the platform
# as TWO devices with TWO media players — a Shield by its remote driver and by
# its built-in Chromecast; a Google TV likewise; a TV by its own driver and by
# DLNA; an AVR by its driver and by HEOS; an Apple TV by its driver and by
# HomeKit. There is no shared identifier for the platform to join them on
# (the Chromecast half of a Shield carries no MAC at all), so the box offers
# both, and both were added to Dave's rooms as Sources — the second one with
# no input, because it is not a second box. Pro's row collapse (4 Aug) hid
# the row and nothing else: the candidate tray re-offered it, the record kept
# it, and Health raised "commit didn't fully apply" that no recommit could
# clear — the invisible member.
#
# THE RULE, brand-agnostic, keyed only on what the DEVICE ITSELF reports:
# two media players in ONE room, reached through DIFFERENT integrations,
# whose manufacturer, model and advertised device name all agree, with no
# CONTRADICTING hardware identity (two different MACs = two real boxes),
# are one box. The richer control path is the room's device — certified
# driver, a paired remote, more entities, a hardware address — never a
# brand or an integration name. The other half is FOLDED: never a
# candidate, never a member, still a device the awareness can read.
# No facts, no fold: an entity the registries cannot describe is left alone.
def _norm(s) -> str:
    return " ".join(str(s or "").lower().replace("_", " ").split())


# ONE BOX, BY ITS HARDWARE (register 912, 16 Oct 2026). The name rule above misses a receiver whose two halves
# describe their model differently — read on Dave's box: the Denon driver says "Marantz SR5014", HEOS says "SR5014",
# and both carry serial BHL36191003541. Register 315 said to measure such a box before loosening; this is that box.
# The hardware rule is the one awareness already uses for the same pair (watcher, 4 Aug): a MAC in common, or one
# device's SERIAL (8+ characters) inside the other's hardware ids. Ids only — never a name or a model.
def hw_keys(d) -> tuple:
    """(macs, ids, serial) for a device-registry entry. One definition — the watcher's twin collapse reads it too."""
    macs, ids = set(), set()
    for pair in (d.get("connections") or []):
        if (isinstance(pair, (list, tuple)) and len(pair) > 1
                and str(pair[0]).lower() in ("mac", "mac_address")):
            macs.add(str(pair[1]).lower().replace("-", ":"))
    for pair in (d.get("identifiers") or []):
        if isinstance(pair, (list, tuple)) and len(pair) > 1:
            ids.add(str(pair[1]).lower())
    sn = str(d.get("serial_number") or "").strip().lower()
    if len(sn) >= 8:
        ids.add(sn)
    return macs, ids, (sn if len(sn) >= 8 else None)


def same_hardware(fa: dict, fb: dict) -> bool:
    """Two devices' facts name one piece of hardware: a shared MAC, or one's serial inside the other's ids."""
    ma, mb = set(fa.get("macs") or ()), set(fb.get("macs") or ())
    if ma and mb:
        return bool(ma & mb)
    for x, y in ((fa, fb), (fb, fa)):
        sn = x.get("serial")
        if sn and any(sn in i for i in (y.get("ids") or ())):
            return True
    return False


def device_facts(client, entities) -> dict:
    """entity_id -> the device-reported facts the fold keys on, from the LIVE
    registries: manufacturer, model, dev_name (the device's own name — the
    installer's rename is ignored on purpose), integration (the entity's
    platform), hw_ids (connections, e.g. ['mac:aa:bb…']), n_entities on the
    device, has_remote. {} on any failure — fail-open means NO fold."""
    try:
        ents = client.entity_registry() or []
        devs = {d.get("id"): d for d in (client.device_registry() or [])}
    except Exception:                                        # noqa: BLE001
        return {}
    want = set(entities or [])
    by_dev: dict = {}
    for e in ents:
        did = e.get("device_id")
        if did:
            by_dev.setdefault(did, []).append(e.get("entity_id") or "")
    out = {}
    for e in ents:
        eid = e.get("entity_id")
        if eid not in want:
            continue
        d = devs.get(e.get("device_id")) or {}
        mine = by_dev.get(e.get("device_id"), [])
        out[eid] = {
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "dev_name": d.get("name"),
            "integration": e.get("platform"),
            "hw_ids": sorted("%s:%s" % (str(c[0]).lower(), str(c[1]).lower())
                             for c in (d.get("connections") or [])
                             if isinstance(c, (list, tuple)) and len(c) == 2),
            "n_entities": len(mine),
            "has_remote": any(str(x).startswith("remote.") for x in mine),
        }
        _m, _i, _s = hw_keys(d)
        out[eid].update({"macs": sorted(_m), "ids": sorted(_i), "serial": _s})
    # 912: what each player SAYS it is (device_class — "receiver", "tv", "speaker"), read once for the lot. Unread
    # = None: a pairing never happens on a missing class (no facts, no pair).
    _read = False
    try:
        _dc = json.loads(client.render_template(
            "{% set ns = namespace(o={}) %}{% for e in " + json.dumps(sorted(out)) + " %}"
            "{% set ns.o = dict(ns.o, **{e: state_attr(e, 'device_class')}) %}{% endfor %}{{ ns.o | to_json }}") or "{}")
        _read = isinstance(_dc, dict)
    except Exception:                                        # noqa: BLE001
        _dc = {}
    for eid in out:
        out[eid]["dc"] = _dc.get(eid) if _read else None
        out[eid]["dc_read"] = bool(_read and eid in _dc)
    return out


def _twin_score(f: dict) -> int:
    """The richer control path wins. Capability, never a brand."""
    n = 0
    if (f.get("integration") or "") in CERTIFIED_INTEGRATIONS:
        n += 100
    if f.get("has_remote"):
        n += 5
    if f.get("hw_ids"):
        n += 3
    n += min(int(f.get("n_entities") or 0), 20)
    return n


def fold_twins(facts: dict, entities: list) -> tuple[list, dict]:
    """Pure. (kept entities in their original order, {folded: kept_for}). A receiver's MUSIC half is never folded
    (register 912 — it is the room's music speaker); match_twins reports it as paired instead."""
    kept, folded, _paired = match_twins(facts, entities)
    return kept, folded


def _music_half(f: dict) -> bool:
    """A player that streams music and is not itself a receiver or a screen (a receiver's streaming half).
    Only when the class was actually READ — a half whose class could not be read is never paired (no facts, no pair);
    a class read and genuinely empty (a streaming half often reports none) is a music half."""
    return bool(f.get("dc_read")) and (f.get("dc") or "") in ("", "speaker")


def match_twins(facts: dict, entities: list) -> tuple[list, dict, dict]:
    """Pure. (kept, {folded: kept_for}, {paired music half: its receiver}).

    One box is two media players reached through DIFFERENT integrations that either agree on manufacturer, model
    and device name with no contradicting MAC (register 315), or name the same HARDWARE (register 912: a shared MAC,
    or one's serial inside the other's ids). The same driver twice is two boxes (an AVR's zones) — never matched.

    Inside one box: when one half SAYS it is a receiver (device_class "receiver") and another is a music player
    (device_class speaker or none — never a receiver or a TV), the music half is PAIRED to the receiver, not folded:
    it is the room's music speaker, the receiver keeps the video jobs (Dave 16 Oct: "one is audio and one is video",
    any brand). Every other twin folds into the richer control path exactly as before."""
    ents = [e for e in (entities or []) if e]
    parent = {e: e for e in ents}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def same_box(a, b):
        fa, fb = facts.get(a) or {}, facts.get(b) or {}
        if not fa or not fb:
            return False                     # no facts, no fold
        if (fa.get("integration") or "") == (fb.get("integration") or ""):
            return False                     # same driver twice = two boxes (zones)
        key_a = (_norm(fa.get("manufacturer")), _norm(fa.get("model")), _norm(fa.get("dev_name")))
        key_b = (_norm(fb.get("manufacturer")), _norm(fb.get("model")), _norm(fb.get("dev_name")))
        if all(key_a) and key_a == key_b:
            if fa.get("hw_ids") and fb.get("hw_ids") and not (set(fa["hw_ids"]) & set(fb["hw_ids"])):
                return False                 # two hardware addresses = two boxes
            return True
        return same_hardware(fa, fb)

    for i, a in enumerate(ents):
        for b in ents[i + 1:]:
            if same_box(a, b):
                parent[find(b)] = find(a)
    groups: dict = {}
    for e in ents:
        groups.setdefault(find(e), []).append(e)
    folded: dict = {}
    paired: dict = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        receivers = [e for e in members if (facts[e].get("dc") or "") == "receiver"]
        rest = members
        if len(receivers) == 1:
            rx = receivers[0]
            music = [e for e in members if e != rx and _music_half(facts[e])]
            if music:
                mwin = sorted(music, key=lambda e: (-_twin_score(facts[e]), e))[0]
                paired[mwin] = rx
                rest = [e for e in members if e != mwin]
        if len(rest) < 2:
            continue
        ranked = sorted(rest, key=lambda e: (-_twin_score(facts[e]), e))
        win = ranked[0]
        for e in ranked[1:]:
            folded[e] = win
    return [e for e in ents if e not in folded], folded, paired


@dataclass
class Device:
    entity: str
    name: str
    integration: str
    device_class: str | None = None
    tier: str = "compatible"   # certified | compatible (set from integration in discover_av)
    model: str | None = None   # HA device model, e.g. "HomePod Mini" / "Apple TV 4K" — the reliable audio/video discriminator for apple_tv


@dataclass
class AVCluster:
    area: str                 # DISPLAY name only (captions, label stripping). Mutable — never keyed on.
    display: Device | None
    area_id: str = ""         # IMMUTABLE HA area_id. This is what generated script ids are built from.
    sources: list[Device] = field(default_factory=list)
    audio: list[Device] = field(default_factory=list)
    # Whether the installer committed the DISPLAY itself as a source ("Also a source --
    # live TV / built-in apps"). Only then does the room get a 'Watch TV' activity; a
    # display-only TV gets none (the room is driven by its external sources). display_input
    # is the TV's own committed input for that activity (e.g. 'TV' for the broadcast tuner).
    display_is_source: bool = False
    display_input: str | None = None
    # WHERE THE TV ACTIVITY SITS (Dave, 9 Aug 2026): when the display is
    # committed "Also a source", its slot among the record's sources — set by
    # dragging Devices & AV in Pro — is where build_watch_activities places
    # the 'watch_tv' activity in the served list (dashboards order the nav/
    # tile activity buttons by that list). None = no slot: appended last,
    # the pre-9-Aug behaviour, so legacy records are byte-identical.
    display_pos: int | None = None
    # The committed AV SWITCH entity (an AVR/matrix, ANY brand). It routes and
    # amplifies sources but is NOT itself a watchable source, so
    # build_watch_activities skips it — no phantom 'watch_<avr>' activity that can
    # never commission (the "commit didn't fully apply" cause). It still powers
    # off with the room and remains a valid audio endpoint.
    avswitch: str | None = None
    # One box seen twice (register 315): {folded entity: the entity kept}.
    # Folded entities never reach sources/audio/display.
    folded: dict = field(default_factory=dict)
    # A receiver's music half (register 912): {music half: its receiver}. NOT folded — it is the room's music
    # speaker; Pro draws it inside the receiver's row and binds it as the room's audio endpoint.
    paired: dict = field(default_factory=dict)

    def label_for(self, dev: Device) -> str:
        """A clean activity label, e.g. 'Family Room Shield TV' -> 'Shield TV'.

        NEVER a raw entity_id: a just-re-paired device can be nameless for a
        moment, and rendering its entity_id produced an activity called
        'Watch media_player.bedroom_bedroom_apple_tv'. Labels are display —
        derive something human from the object_id instead."""
        name = dev.name or ""
        if name.lower().startswith(self.area.lower()):
            name = name[len(self.area):].strip(" -")
        if name:
            return name
        # Fallback: object_id -> words, area tokens stripped, known brands fixed.
        oid = dev.entity.split(".", 1)[-1]
        area_toks = set(re.sub(r"[^a-z0-9]+", " ", self.area.lower()).split())
        words = [w for w in oid.split("_") if w and w not in area_toks and not w.isdigit()]
        label = " ".join(words).title() or dev.entity
        for a, b in (("Tv", "TV"), ("Avr", "AVR"), ("Hdmi", "HDMI")):
            label = label.replace(a, b)
        return label


def role_for(integration: str, dev_class, has_remote=None, model=None):
    """Natural role for a discovered media_player. Pure; benched
    (tests/discovery_role_bench.py). Matrix #2's discovery half.

    Order of knowledge:
      1. ROLE_BY_INTEGRATION — the certified register's mapping.
      2. device_class 'tv' fallback for unmapped integrations, so a new TV
         brand works with no code change.
      3. The apple_tv discriminator: a device qualifies as a SOURCE only when
         its paired remote.<oid> is present — the register's own on-box check
         (it is also how sleep/wake works). A HomePod has none; its dc is
         'speaker' on some firmware and NULL on others (the live Office one,
         31 Jul), which is exactly how it beat the old dc-only rule, got
         bucketed a source, and froze the Office's status.

    has_remote is tri-state: True / False / None(unknown). Unknown NEVER
    re-roles — never re-role on missing evidence (confirm, don't assume).
    """
    role = ROLE_BY_INTEGRATION.get(integration)
    if role is None and dev_class == "tv":
        role = "display"
    if role == "source" and integration == "apple_tv":
        # MODEL is the reliable discriminator. Proven on the live box (Bec's
        # Office HomePod Mini, 6 Aug 2026): a HomePod registers on apple_tv AND
        # exposes its own remote.<oid> — exactly like an Apple TV — so the
        # remote-presence check alone CANNOT tell them apart. Apple hands us the
        # model string ("HomePod ..." vs "Apple TV ..."); that is what an
        # AV professional reads off the box. Model first, then the older
        # device_class / remote fallbacks for firmwares that omit the model.
        if (model and "homepod" in model.lower()) or dev_class == "speaker" or has_remote is False:
            role = "audio"
    return role


def _apple_tv_has_remote(client, dev) -> bool | None:
    """Does this media_player's DEVICE expose a remote entity? Tri-state:
    None on any failure so the caller fails open (generator._paired_remote
    is unsuitable here — its fallback GUESSES a remote name, and a guess is
    exactly what a presence check must not do)."""
    tmpl = ("{% set ns = namespace(r=[]) %}"
            "{% for e in device_entities(device_id(" + json.dumps(dev.entity) + ")) %}"
            "{% if e.startswith('remote.') %}{% set ns.r = ns.r + [e] %}{% endif %}"
            "{% endfor %}{{ ns.r | to_json }}")
    try:
        rl = json.loads(client.render_template(tmpl) or "null")
        if isinstance(rl, list):
            return bool(rl)
    except Exception:
        pass
    return None


def discover_av(client, area_id: str) -> AVCluster:
    """Discover a room's AV by its platform AREA ID (register 441). The id
    is injected as a JSON-quoted literal (a straight apostrophe would
    otherwise close a bare Jinja string). The name is the platform's live
    name for the id, for display only."""
    area_id = str(area_id or "").strip()
    try:
        name = (client.render_template("{{ area_name(%s) or '' }}" % json.dumps(area_id, ensure_ascii=False)) or "").strip()
    except Exception:
        name = ""
    raw = client.render_template(_TEMPLATE % (json.dumps(area_id, ensure_ascii=False), json.dumps(_KNOWN)))
    rows = json.loads(raw)
    cluster = AVCluster(area=(name or area_id), area_id=area_id, display=None)
    # One box seen twice is offered once (register 315): fold BEFORE roles, so
    # the folded half is never a candidate for any slot.
    _kept, cluster.folded, cluster.paired = match_twins(
        device_facts(client, [r.get("entity") for r in rows]),
        [r.get("entity") for r in rows])
    if cluster.paired:
        print("  [discovery] %s a receiver's music half, paired: %s"
              % (cluster.area, ", ".join("%s → %s" % kv for kv in sorted(cluster.paired.items()))), flush=True)
    if cluster.folded:
        print("  [discovery] %s one device seen twice, folded: %s"
              % (cluster.area, ", ".join("%s → %s" % kv
                                 for kv in sorted(cluster.folded.items()))),
              flush=True)
    for r in rows:
        if r.get("entity") in cluster.folded:
            continue
        integ = r.get("integration", "unknown")
        dev = Device(r["entity"], r.get("name") or r["entity"], integ,
                     r.get("device_class"), tier(integ), model=r.get("model"))
        role = role_for(dev.integration, dev.device_class,
                        _apple_tv_has_remote(client, dev)
                        if dev.integration == "apple_tv" else None,
                        model=dev.model)
        if role == "display" and cluster.display is None:
            cluster.display = dev
        elif role == "source":
            cluster.sources.append(dev)
        elif role == "audio":
            cluster.audio.append(dev)
    return cluster
