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

import threading
import time
import uuid

from .ma_ws import MaClient, ma_login, MaAuthFailed


class MaUnavailable(RuntimeError):
    """MA discovery record or connection wasn't available."""


class MaCommissioner:
    # OAuth / multi-step provider config flows (Spotify, Tidal, …) tie their
    # session to a single MA WebSocket connection. One-shot connect-per-command
    # loses the session between "open form" and "authenticate", so those calls
    # share a persistent per-session connection, reaped after this idle window.
    _SESSION_TTL = 600  # seconds

    def __init__(self, get_conn):
        # get_conn() -> (host, port, token) | None
        self.get_conn = get_conn
        self._sessions = {}                  # session_id -> {client, ts, lock}
        self._sessions_lock = threading.Lock()

    def _client(self) -> MaClient:
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host, port, token = conn
        return MaClient(host, port, token)

    # ── Persistent per-session connection (OAuth-capable) ───────────────────
    def _reap_locked(self, now):
        for sid in [s for s, e in self._sessions.items()
                    if now - e["ts"] > self._SESSION_TTL]:
            try:
                self._sessions[sid]["client"].close()
            except Exception:
                pass
            del self._sessions[sid]

    def _session_entry(self, session_id):
        now = time.time()
        with self._sessions_lock:
            self._reap_locked(now)
            ent = self._sessions.get(session_id)
            if ent is not None:
                ent["ts"] = now
                return ent
            conn = self.get_conn()
            if not conn or not conn[0]:
                raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
            host, port, token = conn
            c = MaClient(host, port, token)
            c.connect()
            ent = {"client": c, "ts": now, "lock": threading.Lock()}
            self._sessions[session_id] = ent
            return ent

    def _close_session(self, session_id):
        with self._sessions_lock:
            ent = self._sessions.pop(session_id, None)
        if ent:
            try:
                ent["client"].close()
            except Exception:
                pass

    def provider_auth_probe(self, provider_domain: str, seconds: float = 12.0) -> dict:
        """Diagnostic for the OAuth relay: open a fresh MA connection, fire the
        'auth' action with a one-off session_id, and capture the frame shapes MA
        sends (to confirm AUTH_SESSION event delivery). Fresh session each call so
        the /callback/{session_id} route never collides; no retry."""
        sid = "probe" + uuid.uuid4().hex[:10]
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host, port, token = conn
        c = MaClient(host, port, token)
        c.connect()
        try:
            return c.auth_probe(provider_domain, sid, seconds=seconds)
        finally:
            try:
                c.close()
            except Exception:
                pass

    def _session_command(self, sid, command, **args):
        """Run a command on the session's persistent connection; if the socket
        has gone stale, drop it and retry once on a fresh one. (First arg is
        `sid`, not `session_id`, so callers can also forward a `session_id`
        kwarg through to MA without a collision.)"""
        ent = self._session_entry(sid)
        try:
            with ent["lock"]:
                return ent["client"].command(command, **args)
        except (ConnectionError, OSError):
            self._close_session(sid)
            ent = self._session_entry(sid)
            with ent["lock"]:
                return ent["client"].command(command, **args)


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
        and streaming-provider (Spotify/Tidal/Qobuz) setup. MA 2.9.x ties a
        config-flow / OAuth session to ONE WebSocket connection, so when a
        session_id is supplied every step rides the same persistent connection;
        without one we fall back to a one-shot connection with a throwaway id."""
        if session_id:
            return self._session_command(
                session_id, "config/providers/get_entries",
                provider_domain=provider_domain, instance_id=instance_id,
                action=action, values=values, session_id=session_id) or []
        with self._client() as c:
            return c.command("config/providers/get_entries",
                             provider_domain=provider_domain, instance_id=instance_id,
                             action=action, values=values,
                             session_id=uuid.uuid4().hex) or []

    def save_provider(self, provider_domain: str, values: dict,
                      instance_id: str | None = None,
                      session_id: str | None = None) -> dict:
        """Create/update a provider instance (submit the setup form). Runs on the
        session's persistent connection so an OAuth provider's authenticated
        values resolve, then closes the session — the dialog is done."""
        if session_id:
            try:
                return self._session_command(
                    session_id, "config/providers/save",
                    provider_domain=provider_domain, values=values,
                    instance_id=instance_id, session_id=session_id)
            finally:
                self._close_session(session_id)
        with self._client() as c:
            return c.command("config/providers/save",
                             provider_domain=provider_domain, values=values,
                             instance_id=instance_id)
