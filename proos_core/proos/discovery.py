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
CERTIFIED_INTEGRATIONS = {
    # AV
    "samsungtv_smart", "apple_tv", "sonos", "androidtv_remote", "denonavr", "heos",
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
    # AV
    "samsungtv_smart": {"discrete_power", "discrete_input", "reliable_state", "awareness"},
    "apple_tv":        {"discrete_power", "reliable_state", "awareness"},  # sleep via paired remote
    "sonos":           {"tv_audio", "reliable_state", "awareness"},
    "androidtv_remote": {"discrete_power", "reliable_state", "awareness"},  # Shield: power+nav via ADB‑remote
    "denonavr":        {"discrete_power", "discrete_input", "tv_audio", "reliable_state", "awareness"},
    "heos":            {"tv_audio", "reliable_state", "awareness"},         # AVR/speaker streaming path
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


def meets_bar(integration: str, role: str) -> bool:
    """True when the integration declares every capability the role's minimum bar
    requires. (On-box verification -- e.g. discrete_input_verified -- is confirmed
    separately at commission time; register membership alone isn't the badge.)"""
    return not missing_capabilities(integration, role)


# ProOS certified-driver versions — the version of ProOS's validated driver for each
# certified integration (shown in the room's device details). Bump when a driver is
# re-validated. Compatible/native integrations have no ProOS driver version → None.
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
  {%% set ns.rows = ns.rows + [{'entity': e, 'name': state_attr(e,'friendly_name'), 'integration': integ.v, 'device_class': state_attr(e,'device_class')}] %%}
{%% endfor %%}
{{ ns.rows | to_json }}
"""


@dataclass
class Device:
    entity: str
    name: str
    integration: str
    device_class: str | None = None
    tier: str = "compatible"   # certified | compatible (set from integration in discover_av)


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


def role_for(integration: str, dev_class, has_remote=None):
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
        if dev_class == "speaker" or has_remote is False:
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


def discover_av(client, area: str) -> AVCluster:
    # The area name is injected as a JSON-quoted literal (not a bare '...' Jinja
    # string): a straight apostrophe in a name like "Ryan's Room" would otherwise
    # close the Jinja string early and raise TemplateSyntaxError. ensure_ascii=False
    # keeps curly apostrophes (e.g. "Bec's Office") literal rather than \uXXXX,
    # which Jinja wouldn't decode back to the real area name.
    # Resolve the IMMUTABLE area_id (what generated script ids are built from). 'area' is the
    # room name from the caller; area_id() maps it to the stable id. Keep the name for display.
    try:
        area_id = (client.render_template("{{ area_id(%s) or '' }}" % json.dumps(area, ensure_ascii=False)) or "").strip()
    except Exception:
        area_id = ""
    raw = client.render_template(_TEMPLATE % (json.dumps(area, ensure_ascii=False), json.dumps(_KNOWN)))
    rows = json.loads(raw)
    cluster = AVCluster(area=area, area_id=area_id, display=None)
    for r in rows:
        integ = r.get("integration", "unknown")
        dev = Device(r["entity"], r.get("name") or r["entity"], integ,
                     r.get("device_class"), tier(integ))
        role = role_for(dev.integration, dev.device_class,
                        _apple_tv_has_remote(client, dev)
                        if dev.integration == "apple_tv" else None)
        if role == "display" and cluster.display is None:
            cluster.display = dev
        elif role == "source":
            cluster.sources.append(dev)
        elif role == "audio":
            cluster.audio.append(dev)
    return cluster
