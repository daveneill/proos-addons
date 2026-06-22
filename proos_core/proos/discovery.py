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
    "samsungtv": "display", "webostv": "display", "bravia_tv": "display",
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

_TEMPLATE = """
{%% set area = '%s' %%}
{%% set known = %s %%}
{%% set ae = area_entities(area) | select('match','media_player\\.') | list %%}
{%% set ns = namespace(rows=[]) %%}
{%% for e in ae %%}
  {%% set integ = namespace(v='unknown') %%}
  {%% for k in known %%}
    {%% if e in integration_entities(k) %%}{%% set integ.v = k %%}{%% endif %%}
  {%% endfor %%}
  {%% set ns.rows = ns.rows + [{'entity': e, 'name': state_attr(e,'friendly_name'), 'integration': integ.v}] %%}
{%% endfor %%}
{{ ns.rows | to_json }}
"""


@dataclass
class Device:
    entity: str
    name: str
    integration: str


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
    raw = client.render_template(_TEMPLATE % (area, json.dumps(_KNOWN)))
    rows = json.loads(raw)
    cluster = AVCluster(area=area, display=None)
    for r in rows:
        dev = Device(r["entity"], r.get("name") or r["entity"], r.get("integration", "unknown"))
        role = ROLE_BY_INTEGRATION.get(dev.integration)
        if role == "display" and cluster.display is None:
            cluster.display = dev
        elif role == "source":
            cluster.sources.append(dev)
        elif role == "audio":
            cluster.audio.append(dev)
    return cluster
