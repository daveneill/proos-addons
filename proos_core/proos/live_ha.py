"""
Live Home Assistant client -- the real one.

Implements the same HAClient contract as MockHA, so the reconciler runs
unchanged. Talks to HA's REST API over stdlib urllib (no pip installs), which
means it runs on a stock Mac Python 3 with nothing to set up.

  - snapshot()     -> GET  /api/states          (one round trip, filtered)
  - call_service() -> POST /api/services/<d>/<s>

Add-on vs cloud is ONLY the base_url:
  local add-on / Mac on the LAN : http://192.168.1.240:8123
  cloud over Nabu Casa          : https://<your-id>.ui.nabu.casa
Same code either way -- that's the seam doing its job.
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error
from .ha_client import Snapshot

# Gateway/proxy stalls: the supervisor->core proxy returns 502 (and sometimes 503/
# 504) when HA is briefly busy or mid-reload. These are TRANSIENT — retrying a beat
# later succeeds. Retrying here (the one HA choke point) makes every read/write that
# ProOS does — monitor, watcher, activity generation & listing — ride through a
# momentary stall instead of failing a whole cycle.
_RETRY_STATUS = frozenset({502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF = 0.4  # seconds; grows per attempt (0.4, 0.8, 1.2 …)


# Bumped by EVERY registry write this process makes, wherever it is made
# from (register 242). Module-level on purpose: the writes go through
# several adapters — server's _ws_call, assist, project, provision — and a
# counter each of them can reach is the only honest choke point. A cache
# that outlives our own rename is a cache that lies.
_WRITE_GEN = [0]


def registry_written():
    """Call after any registry create/update/delete. Never raises."""
    _WRITE_GEN[0] += 1


class RestHAClient:
    # STAGE 1 (16 Aug 2026): the live event stream. When the server attaches a
    # healthy HaStream here, snapshot() serves from its push-fed cache — state
    # reaches Core in the time HA takes to push a frame, not on a 5s poll.
    # When the stream is absent, down or stale, snapshot() falls back to the
    # REST poll below, byte-for-byte today's behaviour. Fail-open, never
    # fail-silent: healthy() is earned (recent frames), not assumed.
    stream = None

    def __init__(self, home_id: str, base_url: str, token: str, timeout: float = 10.0):
        self.home_id = home_id
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _req(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(_RETRY_ATTEMPTS):
            last = attempt == _RETRY_ATTEMPTS - 1
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read().decode()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                # Transient gateway stall -> wait a beat and retry (idempotent GETs and
                # our create-or-replace writes are safe to repeat; a 502 means the
                # request didn't reach HA anyway).
                if e.code in _RETRY_STATUS and not last:
                    time.sleep(_RETRY_BACKOFF * (attempt + 1))
                    continue
                detail = e.read().decode(errors="replace")[:200]
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                # Connection reset / timeout mid-reload — also transient, retry.
                if not last:
                    time.sleep(_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"Cannot reach the hub at {self.base_url} ({e.reason}). "
                    f"Check the IP/port and that this machine is on the same network."
                ) from None

    def snapshot(self, entity_ids: list[str]) -> Snapshot:
        # The live stream first (Stage 1): pushed state, sub-second fresh.
        s = self.stream
        if s is not None:
            try:
                if s.healthy():
                    return s.snapshot(entity_ids)
            except Exception:                                    # noqa: BLE001
                pass                       # a broken stream never blocks a read
        wanted = set(entity_ids)
        all_states = self._req("GET", "/api/states") or []
        out: Snapshot = {}
        for rec in all_states:
            eid = rec.get("entity_id")
            if eid in wanted:
                out[eid] = {
                    "state": rec.get("state", "unavailable"),
                    "attributes": rec.get("attributes", {}) or {},
                    "last_changed": rec.get("last_changed"),
                    "missing": False,
                }
        # NOT ON THIS BOX AT ALL. One word was doing two jobs (16 Aug 2026,
        # read live on Dave's box): a device whose integration reports
        # unavailable and an entity id the house has never heard of BOTH
        # answered "unavailable", so Assist reported a paused Sonos as
        # broken from a guessed id and could not have known better. The
        # state stays "unavailable" for every existing internal caller;
        # `missing` is the fact that was absent, and the model-facing
        # shaper turns it into words.
        for eid in wanted - set(out):
            out[eid] = {"state": "unavailable", "attributes": {},
                        "last_changed": None, "missing": True}
        return out

    def call_service(self, domain: str, service: str, entity_id: str,
                     data: dict | None = None) -> None:
        payload = {"entity_id": entity_id}
        if data:
            payload.update(data)
        self._req("POST", f"/api/services/{domain}/{service}", payload)

    def ping(self) -> str:
        """Verify auth + reachability before we touch any devices."""
        info = self._req("GET", "/api/") or {}
        return info.get("message", "connected")

    def get_script(self, object_id: str) -> dict | None:
        """Fetch a script's config, or None if it doesn't exist.

        Used by the generator for create-if-absent: an existing (possibly
        installer-edited) script must never be clobbered by routine discovery.
        """
        try:
            return self._req("GET", f"/api/config/script/config/{object_id}")
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def delete_script(self, object_id: str) -> bool:
        """Remove a script. Used only by the generator's dedupe, and only on
        scripts it can PROVE it generated and nobody edited."""
        try:
            self._req("DELETE", f"/api/config/script/config/{object_id}")
            return True
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return False
            raise

    def upsert_script(self, object_id: str, config: dict) -> None:
        """Create or replace a script via HA's REST config endpoint.

        POSTing here persists the script and reloads scripts automatically, so the
        new/updated script is live without a manual reload. Scripts have this clean
        REST path; template helpers do not (they're config-flow/WebSocket), which is
        why Core generates the editable command path as scripts.
        """
        self._req("POST", f"/api/config/script/config/{object_id}", config)

    def render_template(self, template: str) -> str:
        """Render a Jinja template server-side. Returns raw text (may be JSON)."""
        url = f"{self.base_url}/api/template"
        data = json.dumps({"template": template}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Template render failed: HTTP {e.code}: {detail}") from None

    def resolve_config_entry(self, entity_id: str) -> str | None:
        """Which integration config entry owns this entity (for reloads)."""
        out = (self.render_template("{{ config_entry_id('%s') }}" % entity_id) or "").strip()
        return out if out and out not in ("None", "") else None

    def reload_integration(self, entry_id: str) -> None:
        """Reload an integration's connection -- the first rung of self-healing."""
        self._req("POST", "/api/services/homeassistant/reload_config_entry",
                  {"entry_id": entry_id})

    # ── Config-entry flows ──────────────────────────────────────────────────
    # Lets Core own an integration's setup instead of the installer clicking in
    # HA. Detection is REST (config_entries lists entries); reading in-progress
    # flows is WebSocket-only (the REST flow index is POST-only); confirming a
    # flow step is REST again ({} confirms a confirm-only step).
    def config_entries(self, domain: str | None = None) -> list:
        """Configured integrations (config entries), optionally filtered by domain.

        WEBSOCKET, not REST (register 148). The REST index
        `/api/config/config_entries/entry` no longer answers a Supervisor-token
        caller, so this returned an EMPTY LIST rather than an error — and every
        caller read "no such integration is configured". That is how register
        147's self-heal reported "no certified provider" on a box whose UniFi
        entry was sitting right there, loaded. Registry reads already use the
        WebSocket for exactly this reason; this one had been left behind.
        """
        from .ha_ws import ws_command
        out = ws_command(self.base_url, self._token, "config_entries/get",
                         timeout=self._timeout) or []
        if domain:
            out = [e for e in out if e.get("domain") == domain]
        return out

    def flow_progress(self) -> list:
        """In-progress config flows (HA's 'discovered' list). WebSocket-only."""
        from .ha_ws import flow_progress
        return flow_progress(self.base_url, self._token, timeout=self._timeout)

    # ── THE REGISTRIES: HEARD, NOT POLLED (19 Aug 2026, register 242) ──────
    #
    # MEASURED ON DAVE'S BOX, from the Supervisor's own log: THREE full
    # websocket connections — connect, upgrade, auth, one command, close —
    # every two seconds, steadily. ~90 a minute. Around 130,000 a day. All
    # three are ctlbridge's 2-second sweep re-reading lists that change when
    # an installer renames a room.
    #
    # Stage 1's opening paragraph is about exactly this pattern: "the
    # websocket HA uses to push every change in under a second was opened
    # for one command at a time and closed." It fixed STATE. It left the
    # REGISTRIES behind.
    #
    # WHAT MAKES THIS A READING AND NOT A CACHE-SHAPED GUESS:
    #  · the stream now subscribes to the platform's own registry-updated
    #    events, so the cache is dropped when the PLATFORM says a registry
    #    changed — not on a timer somebody chose
    #  · a new stream session is a new epoch, so a reconnect never serves
    #    yesterday's list
    #  · if the stream is down, unseeded, stale, OR could not subscribe to
    #    those events, NOTHING is cached and every call reads live —
    #    byte-for-byte today's behaviour. A cache whose invalidation
    #    channel is dead must not serve.
    #
    # AND ONE MORE INVALIDATION, BECAUSE OUR OWN WRITES RACE THE EVENT.
    # When Core renames a room it does not have to wait to be told: the
    # platform's event will arrive, but a caller reading in the same
    # millisecond would be served the list from before its own write.
    # Every registry write bumps this counter, and it is part of the key.
    _REG_CMD = {"area": "config/area_registry/list",
                "device": "config/device_registry/list",
                "entity": "config/entity_registry/list"}

    def _registry(self, kind: str) -> list:
        from .ha_ws import ws_command
        s = self.stream
        gen = None
        try:
            if s is not None and s.registry_watched():
                gen = (s.registry_generation(), _WRITE_GEN[0])
        except Exception:                                    # noqa: BLE001
            gen = None                  # a broken stream never blocks a read
        if gen is not None:
            hit = getattr(self, "_reg_cache", {}).get(kind)
            if hit and hit[0] == gen:
                return hit[1]
        out = ws_command(self.base_url, self._token, self._REG_CMD[kind],
                         timeout=self._timeout) or []
        if gen is not None:
            # A RENAME THAT LANDED WHILE WE WERE ON THE WIRE NEEDS NO GUARD,
            # and the guard I wrote for it has been DELETED rather than left
            # sitting there. It re-read the generation after the call and
            # refused to cache if it had moved — and a mutation removing it
            # changed nothing, correctly: both counters only ever go UP, so
            # an entry stored under a superseded generation can never match
            # a later key. It is dead on arrival, not stale. A check that
            # cannot fail is not a check, so it is gone and the reason is
            # written down instead.
            if not hasattr(self, "_reg_cache"):
                self._reg_cache = {}
            self._reg_cache[kind] = (gen, out)
        return out

    def area_registry(self) -> list:
        """LIVE area registry (what HA holds in memory, not stale .storage)."""
        return self._registry("area")

    def device_registry(self) -> list:
        """LIVE device registry -- current area_id, name, connections, config_entries."""
        return self._registry("device")

    def entity_registry(self) -> list:
        """LIVE entity registry -- current area_id override, device_id, platform."""
        return self._registry("entity")

    def enable_entity(self, entity_id: str):
        """Enable a registry-DISABLED entity (disabled_by -> None). Used to bring a UniFi PoE
        port control switch online so ProOS can power-cycle it, without the installer ever
        opening Home Assistant. WebSocket registry write; returns the updated entry."""
        from .ha_ws import ws_command
        registry_written()
        return ws_command(
            self.base_url, self._token, "config/entity_registry/update",
            timeout=self._timeout, entity_id=entity_id, disabled_by=None)

    def set_entity_area(self, entity_id: str, area_id):
        """Assign (or clear) an entity's room. WebSocket-only registry write.

        area_id="" / None clears the override so the entity falls back to its
        device area. Returns the updated registry entry dict.
        """
        from .ha_ws import ws_command
        registry_written()
        return ws_command(
            self.base_url, self._token, "config/entity_registry/update",
            timeout=self._timeout,
            entity_id=entity_id,
            area_id=(area_id or None),
        )

    def set_device_area(self, device_id: str, area_id):
        """Assign (or clear) a DEVICE's room. WebSocket registry write.

        HA auto-stamps a device's area at pairing when its name matches a room
        (e.g. a HomePod called 'Office' -> Office). Clearing it (area_id=None)
        drops the device into Unassigned so ProOS never treats an auto-guess as a
        placement — the installer places it explicitly. This is the DEVICE area,
        distinct from set_entity_area's per-entity override.
        """
        from .ha_ws import ws_command
        registry_written()
        return ws_command(
            self.base_url, self._token, "config/device_registry/update",
            timeout=self._timeout,
            device_id=device_id,
            area_id=(area_id or None),
        )

    def set_entity_name(self, entity_id: str, name):
        """Set (or clear, name=None) an entity's registry name — the name the
        platform speaks and shows for it. WebSocket registry write. Used only
        through roomdevices.rename(), which keeps a device and its one entity
        on one name (register 328)."""
        from .ha_ws import ws_command
        registry_written()
        return ws_command(
            self.base_url, self._token, "config/entity_registry/update",
            timeout=self._timeout,
            entity_id=entity_id,
            name=((name or "").strip() or None),
        )

    def set_device_name(self, device_id: str, name):
        """Set (or clear, name=None) a device's user name (`name_by_user`).
        WebSocket registry write. Used only through roomdevices.rename_device()."""
        from .ha_ws import ws_command
        registry_written()
        return ws_command(
            self.base_url, self._token, "config/device_registry/update",
            timeout=self._timeout,
            device_id=device_id,
            name_by_user=((name or "").strip() or None),
        )

    def configure_flow(self, flow_id: str, data: dict | None = None) -> dict:
        """Submit one step to an in-progress flow. {} confirms a confirm-only step."""
        return self._req("POST", f"/api/config/config_entries/flow/{flow_id}",
                          data if data is not None else {}) or {}

    def get_flow(self, flow_id: str) -> dict:
        """Read an in-progress flow's current step (type, step_id, data_schema).

        REST GET on the flow resource returns the same form shape HA would render,
        so a provisioner can see exactly which fields the step declares before it
        submits credentials. Returns {} if the flow is gone.
        """
        return self._req("GET", f"/api/config/config_entries/flow/{flow_id}") or {}

    def start_flow(self, handler: str) -> dict:
        """Start a fresh USER config flow for `handler` and return its first step
        (with flow_id + data_schema). Used when HA isn't offering a discovery flow
        to fill — e.g. after the integration was removed and re-added."""
        return self._req("POST", "/api/config/config_entries/flow",
                          {"handler": handler, "show_advanced_options": False}) or {}

    def set_entry_title(self, entry_id: str, title: str) -> dict:
        """Rename a config entry's title (WebSocket registry write).

        Lets ProCore stamp an entry it owns — e.g. 'UniFi Protect — ProOS
        Certified' — so anyone who does look at HA sees it's ProCore-managed,
        the same way the HomeKit bridge is titled 'ProOS Apple Home'.
        """
        from .ha_ws import ws_command
        return ws_command(self.base_url, self._token, "config_entries/update",
                          timeout=self._timeout, entry_id=entry_id,
                          title=title) or {}

    def integration_entities(self, domain: str) -> list:
        """Entity IDs an integration currently exposes -- empty if it isn't loaded.

        Uses the template engine (integration_entities()) so we don't need the
        WebSocket config-entries API; good enough to tell 'set up & running' from
        'not set up', and to count players.
        """
        raw = (self.render_template(
            "{{ integration_entities('%s') | list | tojson }}" % domain) or "[]").strip()
        try:
            return json.loads(raw)
        except Exception:
            return []
