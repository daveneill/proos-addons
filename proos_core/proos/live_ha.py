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
import urllib.request
import urllib.error
from .ha_client import Snapshot


class RestHAClient:
    def __init__(self, home_id: str, base_url: str, token: str, timeout: float = 10.0):
        self.home_id = home_id
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _req(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"HA {method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach HA at {self.base_url} ({e.reason}). "
                f"Check the IP/port and that this machine is on the same network."
            ) from None

    def snapshot(self, entity_ids: list[str]) -> Snapshot:
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
                }
        # Anything HA didn't return is genuinely missing -- surface it, don't hide it.
        for eid in wanted - set(out):
            out[eid] = {"state": "unavailable", "attributes": {}, "last_changed": None}
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
            raise RuntimeError(f"HA template render failed: HTTP {e.code}: {detail}") from None

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
        """Configured integrations (config entries), optionally filtered by domain."""
        out = self._req("GET", "/api/config/config_entries/entry") or []
        if domain:
            out = [e for e in out if e.get("domain") == domain]
        return out

    def flow_progress(self) -> list:
        """In-progress config flows (HA's 'discovered' list). WebSocket-only."""
        from .ha_ws import flow_progress
        return flow_progress(self.base_url, self._token, timeout=self._timeout)

    def area_registry(self) -> list:
        """LIVE area registry (what HA holds in memory, not stale .storage)."""
        from .ha_ws import ws_command
        return ws_command(self.base_url, self._token, "config/area_registry/list",
                          timeout=self._timeout) or []

    def device_registry(self) -> list:
        """LIVE device registry -- current area_id, name, connections, config_entries."""
        from .ha_ws import ws_command
        return ws_command(self.base_url, self._token, "config/device_registry/list",
                          timeout=self._timeout) or []

    def entity_registry(self) -> list:
        """LIVE entity registry -- current area_id override, device_id, platform."""
        from .ha_ws import ws_command
        return ws_command(self.base_url, self._token, "config/entity_registry/list",
                          timeout=self._timeout) or []

    def set_entity_area(self, entity_id: str, area_id):
        """Assign (or clear) an entity's room. WebSocket-only registry write.

        area_id="" / None clears the override so the entity falls back to its
        device area. Returns the updated registry entry dict.
        """
        from .ha_ws import ws_command
        return ws_command(
            self.base_url, self._token, "config/entity_registry/update",
            timeout=self._timeout,
            entity_id=entity_id,
            area_id=(area_id or None),
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
