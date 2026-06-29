"""
ProOS Core -- Music Assistant commissioning layer
=================================================

Everything ProHost needs to commission MA *from ProOS only* -- read the real
providers and players, disable the noise MA over-discovered, and run a provider's
own setup form (e.g. adopt HEOS). Sits on the stdlib MaClient (ma_ws).

Connection details (host/port/token) come from the Supervisor discovery record,
supplied by a `get_conn` callable so we always use a fresh token.

Key MA API commands used (all carry args under "args"):
  config/providers            -> list provider configs
  config/providers/get_entries-> a provider's setup form (for HEOS etc.)
  config/providers/save       -> create/update a provider instance (incl. enabled)
  config/players              -> list player configs
  config/players/save         -> update a player (incl. enabled)
"""
from __future__ import annotations

import uuid

from .ma_ws import MaClient, ma_login, MaAuthFailed


class MaUnavailable(RuntimeError):
    """MA discovery record or connection wasn't available."""


class MaCommissioner:
    def __init__(self, get_conn):
        # get_conn() -> (host, port, token) | None
        self.get_conn = get_conn

    def _client(self) -> MaClient:
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host, port, token = conn
        return MaClient(host, port, token)

    # ── One-time connect: mint + return a long-lived ProOS token ────────────
    def mint_token(self, username: str, password: str, hosts: list,
                   port: int = 8095, token_name: str = "ProOS Core") -> dict:
        """Log in to MA with HA credentials on the first reachable host, then
        create a long-lived token. Returns {host, port, token}. Raises MaAuthFailed
        (bad creds) or MaUnavailable (no host reachable)."""
        last_err = None
        for host in [h for h in hosts if h]:
            try:
                access = ma_login(host, port, username, password)
            except MaAuthFailed:
                raise  # creds are wrong regardless of host — stop now
            except Exception as e:
                last_err = e
                continue
            with MaClient(host, port, access) as c:
                token = c.command("auth/token/create", name=token_name)
            if not token:
                raise MaAuthFailed("MA did not return a long-lived token")
            return {"host": host, "port": port, "token": token}
        raise MaUnavailable(f"Could not reach the Music server (tried {hosts}): {last_err}")

    # ── Read ────────────────────────────────────────────────────────────────
    def providers(self) -> list:
        with self._client() as c:
            return c.command("config/providers", include_values=False) or []

    def players(self) -> list:
        with self._client() as c:
            return c.command("config/players") or []

    def inventory(self) -> dict:
        """One round trip: server version + trimmed providers + players for ProHost."""
        with self._client() as c:
            info = c.server_info or {}
            provs = c.command("config/providers", include_values=False) or []
            plyrs = c.command("config/players") or []
        return {
            "server": {
                "version": info.get("server_version"),
                "schema": info.get("schema_version"),
            },
            "providers": [
                {
                    "instance_id": p.get("instance_id"),
                    "domain": p.get("domain"),
                    "name": p.get("name") or p.get("default_name"),
                    "type": p.get("type"),
                    "enabled": p.get("enabled", True),
                }
                for p in provs
            ],
            "players": [
                {
                    "player_id": p.get("player_id"),
                    "name": p.get("name") or p.get("default_name"),
                    "provider": p.get("provider"),
                    "enabled": p.get("enabled", True),
                }
                for p in plyrs
            ],
        }

    # ── Write ───────────────────────────────────────────────────────────────
    def set_player_enabled(self, player_id: str, enabled: bool) -> dict:
        """Enable/disable a player (this is how we drop the noise MA discovered)."""
        with self._client() as c:
            return c.command("config/players/save",
                             player_id=player_id, values={"enabled": bool(enabled)})

    def set_provider_enabled(self, instance_id: str, enabled: bool) -> dict:
        """Enable/disable a whole provider instance (e.g. turn off Chromecast/DLNA)."""
        with self._client() as c:
            domain = next((p.get("domain") for p in
                           (c.command("config/providers", include_values=False) or [])
                           if p.get("instance_id") == instance_id), None)
            if not domain:
                raise MaUnavailable(f"No provider instance {instance_id}")
            return c.command("config/providers/save",
                             provider_domain=domain, instance_id=instance_id,
                             values={"enabled": bool(enabled)})

    def provider_entries(self, provider_domain: str, instance_id: str | None = None,
                         action: str | None = None, values: dict | None = None,
                         session_id: str | None = None) -> list:
        """A provider's setup form (config entries) -- drives the HEOS adopt flow
        and the streaming-provider (Spotify/Tidal/Qobuz) setup. MA 2.9.x requires
        a client-supplied session_id to anchor multi-step / OAuth login sessions;
        ProHost passes a stable one across a dialog, and we fall back to a fresh
        one so credential providers still work."""
        with self._client() as c:
            return c.command("config/providers/get_entries",
                             provider_domain=provider_domain, instance_id=instance_id,
                             action=action, values=values,
                             session_id=session_id or uuid.uuid4().hex) or []

    def save_provider(self, provider_domain: str, values: dict,
                      instance_id: str | None = None,
                      session_id: str | None = None) -> dict:
        """Create/update a provider instance (submit the setup form). Carries the
        same session_id so an OAuth provider's authenticated values resolve."""
        with self._client() as c:
            kw = dict(provider_domain=provider_domain, values=values,
                      instance_id=instance_id)
            if session_id:
                kw["session_id"] = session_id
            return c.command("config/providers/save", **kw)
