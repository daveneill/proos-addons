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
        Used for create-if-absent so installer-edited scripts are never clobbered."""
        try:
            return self._req("GET", f"/api/config/script/config/{object_id}")
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def upsert_script(self, object_id: str, config: dict) -> None:
        """Create or replace a script via HA's REST config endpoint (auto-reloads)."""
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
