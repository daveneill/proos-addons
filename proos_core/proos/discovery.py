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
from dataclasses import dataclass, field

# Which integrations play which role in a home-theatre stack.
ROLE_BY_INTEGRATION = {
    # the screen
    "samsungtv": "display", "samsungtv_smart": "display", "webostv": "display", "bravia_tv": "display",
    "lg_netcast": "display", "philips_js": "display",
    # HDMI source devices
    "apple_tv": "source", "androidtv_remote": "source", "firetv": "source",
    "roku": "source", "kodi": "source", "cast": "source",
    # audio endpoints
    "sonos": "audio", "heos": "audio",
}

# Integrations we bother classifying. (cast is listed but usually a dead/duplicate
# entity in this house, and gets filtered as disabled before it reaches us.)
_KNOWN = list(dict.fromkeys(list(ROLE_BY_INTEGRATION) + ["cast"]))

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
CERTIFIED_INTEGRATIONS = {"samsungtv_smart", "apple_tv", "sonos"}

CERTIFIED_CAPABILITIES = {
    "samsungtv_smart": {"discrete_power", "discrete_input", "reliable_state", "awareness"},
    "apple_tv":        {"discrete_power", "reliable_state", "awareness"},  # sleep via paired remote
    "sonos":           {"tv_audio", "reliable_state", "awareness"},
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


# ProOS certified-driver versions — the version of ProOS's validated driver for each
# certified integration (shown in the room's device details). Bump when a driver is
# re-validated. Compatible/native integrations have no ProOS driver version → None.
CERTIFIED_VERSIONS = {
    "samsungtv_smart": "1.0",
    "apple_tv": "1.0",
    "sonos": "1.0",
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
    area: str
    display: Device | None
    sources: list[Device] = field(default_factory=list)
    audio: list[Device] = field(default_factory=list)

    def label_for(self, dev: Device) -> str:
        """A clean activity label, e.g. 'Family Room Shield TV' -> 'Shield TV'."""
        name = dev.name or dev.entity
        if name.lower().startswith(self.area.lower()):
            name = name[len(self.area):].strip(" -")
        return name or dev.entity


def discover_av(client, area: str) -> AVCluster:
    # The area name is injected as a JSON-quoted literal (not a bare '...' Jinja
    # string): a straight apostrophe in a name like "Ryan's Room" would otherwise
    # close the Jinja string early and raise TemplateSyntaxError. ensure_ascii=False
    # keeps curly apostrophes (e.g. "Bec's Office") literal rather than \uXXXX,
    # which Jinja wouldn't decode back to the real area name.
    raw = client.render_template(_TEMPLATE % (json.dumps(area, ensure_ascii=False), json.dumps(_KNOWN)))
    rows = json.loads(raw)
    cluster = AVCluster(area=area, display=None)
    for r in rows:
        integ = r.get("integration", "unknown")
        dev = Device(r["entity"], r.get("name") or r["entity"], integ,
                     r.get("device_class"), tier(integ))
        role = ROLE_BY_INTEGRATION.get(dev.integration)
        # Capability fallback: any TV-class media_player counts as a display, even
        # from an integration we don't explicitly map — so new TV brands/drivers
        # work with no code change. Source/audio still rely on the explicit map.
        if role is None and dev.device_class == "tv":
            role = "display"
        # HomePod / AirPlay speaker exposed by the apple_tv integration is an AUDIO
        # endpoint, not a video source. Detect by name or a speaker device_class so it's
        # roled (and later monitored) as a speaker, not mistaken for an Apple TV streamer.
        # A real Apple TV keeps device_class None and no 'homepod' in its name, so it
        # stays a source. This is the auto-role that saves the installer a manual fix.
        if role == "source" and dev.integration == "apple_tv":
            nm = (dev.name or "").lower()
            if "homepod" in nm or dev.device_class == "speaker":
                role = "audio"
        if role == "display" and cluster.display is None:
            cluster.display = dev
        elif role == "source":
            cluster.sources.append(dev)
        elif role == "audio":
            cluster.audio.append(dev)
    return cluster
