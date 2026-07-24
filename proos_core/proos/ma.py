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

    def __init__(self, get_conn, get_ingress_user=None):
        # get_conn() -> (host, port, token) | None
        # get_ingress_user() -> (user_id, username, display_name) | None — a
        # CURRENT, valid HA admin identity for MA's ingress listener.
        self.get_conn = get_conn
        self.get_ingress_user = get_ingress_user
        self._sessions = {}                  # session_id -> {client, ts, lock}
        self._sessions_lock = threading.Lock()

    def _client(self) -> MaClient:
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host, port, token = conn
        return self._mk(host, port, token)

    def _mk(self, host, port, token, timeout: float = 10.0) -> MaClient:
        """Every MA connection goes through here — ANONYMOUS on purpose,
        including on the ingress listener (:8094), which MA treats as
        pre-authed by design (admin included). Presenting X-Remote-User-*
        headers is what fires MA's ingress create_user, which (a) crashes the
        socket on a username collision with a stale row and (b) RACES against
        itself when concurrent connections carry the same new identity — both
        observed live 2026-07-24, both fatal ("WS closed mid-frame"). Identity
        headers belong ONLY to the explicit save_provider_admin path, used
        once and serially."""
        return MaClient(host, port, token, timeout=timeout)

    def _admin_command(self, ingress_user: tuple, command: str,
                       timeout: float = 40.0, **args):
        """Run an ADMIN-required MA command over the ingress channel (:8094)
        carrying the installer/owner identity headers — the same path
        save_provider_admin uses. MA rejects admin commands (e.g.
        config/providers/remove, required_role='admin') on the anonymous API
        connection, so those MUST come through here."""
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        last = None
        for h in dict.fromkeys([conn[0], "172.30.32.1"]):
            if not h:
                continue
            c = MaClient(h, self.INGRESS_PORT, token=None, timeout=timeout,
                         ingress_user=ingress_user)
            try:
                c.connect()
            except Exception as e:  # noqa: BLE001
                last = e
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                return c.command(command, **args)
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
        raise MaUnavailable(f"Could not reach the Music ingress channel: {last}")

    def remove_provider(self, instance_id: str, ingress_user: tuple) -> dict:
        """Remove ONE provider instance entirely — credentials and library sync
        go with it. config/providers/remove is admin-required, so it rides the
        ingress-admin channel (not the anonymous API, which silently refused it
        — the 'couldn't remove from Pro/dashboard' bug)."""
        self._admin_command(ingress_user, "config/providers/remove",
                            instance_id=instance_id)
        return {"ok": True, "removed": instance_id}

    def wipe_providers(self) -> dict:
        """FACTORY-RESET helper: remove every provider instance from the Music
        server. Streaming logins (a Spotify refresh token, an Apple Music
        authorisation) are the PREVIOUS home's credentials living in the MA
        add-on's own data, which no HA-side wipe can reach — observed live:
        after a full factory reset the services came back enabled with the old
        accounts. Player providers are re-adopted at the next commission; the
        builtin provider belongs to MA itself and stays. Best-effort: reports
        per-instance results, never raises into the reset."""
        out = {"removed": [], "errors": []}
        try:
            with self._client() as c:
                for p in (c.command("config/providers", include_values=False) or []):
                    iid, dom = p.get("instance_id"), (p.get("domain") or "")
                    if not iid or dom == "builtin":
                        continue
                    try:
                        c.command("config/providers/remove", instance_id=iid)
                        out["removed"].append("%s:%s" % (dom, iid))
                    except Exception as e:  # noqa: BLE001
                        out["errors"].append("%s: %s" % (dom, e))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(str(e))
        return out

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
            c = self._mk(host, port, token)
            c.connect()
            ent = {"client": c, "ts": now, "lock": threading.Lock()}
            self._sessions[session_id] = ent
            return ent

    def has_session(self, session_id) -> bool:
        with self._sessions_lock:
            return session_id in self._sessions

    def _close_session(self, session_id):
        with self._sessions_lock:
            ent = self._sessions.pop(session_id, None)
        if ent:
            try:
                ent["client"].close()
            except Exception:
                pass

    def run_provider_auth(self, provider_domain: str, session_id: str, on_auth_url,
                          values: dict | None = None, timeout: float = 80.0,
                          action: str = "auth"):
        """Open a dedicated MA connection and drive a provider's auth flow to
        completion on it (MA ties the flow to one socket). `on_auth_url(url)` is
        called as soon as MA emits the auth URL; returns the filled entries once
        the user finishes login. The connection is held open for the whole flow
        and closed afterwards. `action` defaults to the generic "auth"; providers
        with their own auth action (e.g. Apple Music: "CONF_ACTION_AUTH") pass it."""
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host, port, token = conn
        c = self._mk(host, port, token, timeout=timeout)
        c.connect()
        try:
            entries = c.provider_auth(provider_domain, session_id, on_auth_url,
                                      values=values, timeout=timeout, action=action)
        except Exception:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        # KEEP this connection alive as the session's connection. MA stores the
        # OAuth result (refresh_token, …) server-side against THIS socket and
        # MASKS secure values in every entries payload it returns — so a save
        # arriving on any other socket has no token anywhere and MA KeyErrors
        # ("config/providers/save: 'refresh_token'", observed live). The save
        # must ride the auth socket, exactly like MA's own frontend. The
        # session TTL reaps it if the dialog is abandoned.
        now = time.time()
        with self._sessions_lock:
            old = self._sessions.pop(session_id, None)
            if old:
                try:
                    old["client"].close()
                except Exception:  # noqa: BLE001
                    pass
            self._sessions[session_id] = {"client": c, "ts": now,
                                          "lock": threading.Lock()}
        return entries

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
        c = self._mk(host, port, token)
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

    def browse(self, path: str | None = None) -> list:
        """Browse the MA tree by service/provider. path=None → the root folders
        (Library + each provider: Spotify, Apple Music, RadioBrowser …); a folder's
        own path drills in. Mirrors MA's own Browse."""
        with self._client() as c:
            return c.command("music/browse", path=path) or []

    # ── Play queue (the editable "up next" list) ──────────────────────────────
    # MA's active queue for a player shares the player's id (queue_id == player_id),
    # so ProOS passes the MA player_id straight through as the queue_id. These are the
    # standard player_queues API commands; args ride under "args" like every other call.
    def queue_items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list:
        """The full ordered list of items in a player's queue."""
        with self._client() as c:
            return c.command("player_queues/items",
                             queue_id=queue_id, limit=int(limit), offset=int(offset)) or []

    def queue_get(self, queue_id: str) -> dict:
        """Queue meta (current_index, count, shuffle/repeat) for the same player."""
        with self._client() as c:
            return c.command("player_queues/get", queue_id=queue_id) or {}

    def queue_move(self, queue_id: str, queue_item_id: str, pos_shift: int) -> dict:
        """Reorder: shift an item by pos_shift places (negative = earlier)."""
        with self._client() as c:
            return c.command("player_queues/move_item",
                             queue_id=queue_id, queue_item_id=queue_item_id,
                             pos_shift=int(pos_shift))

    def queue_delete(self, queue_id: str, queue_item_id: str) -> dict:
        """Remove one item from the queue (accepts the queue_item_id)."""
        with self._client() as c:
            return c.command("player_queues/delete_item",
                             queue_id=queue_id, item_id_or_index=queue_item_id)

    def queue_play_index(self, queue_id: str, index) -> dict:
        """Jump to and play a queue entry (accepts the queue_item_id or a position)."""
        with self._client() as c:
            return c.command("player_queues/play_index", queue_id=queue_id, index=index)

    # ── Library / favourites ─────────────────────────────────────────────────
    # "Add to favourites" in MA == add the item to your library (by its uri). One
    # command; box-test the exact name against your MA version if it no-ops.
    def favorite_add(self, uri: str) -> dict:
        with self._client() as c:
            return c.command("music/library/add_item", item=uri)

    # ── Playlists ─────────────────────────────────────────────────────────────
    def playlists(self, limit: int = 200) -> list:
        """The user's library playlists — for the 'Add to playlist' picker."""
        with self._client() as c:
            return c.command("music/playlists/library_items", limit=int(limit)) or []

    def playlist_add(self, playlist_id: str, uris) -> dict:
        """Append one or more track uris to a playlist."""
        if not isinstance(uris, (list, tuple)):
            uris = [uris]
        with self._client() as c:
            return c.command("music/playlists/add_playlist_tracks",
                             db_playlist_id=playlist_id, uris=list(uris))

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

    # ── Admin WRITE via MA's HA-Ingress channel ─────────────────────────────
    # MA gates config/providers/save behind an admin user. Core's discovery
    # token authenticates as the non-admin system user on the public :8095 API,
    # so saves are refused there. MA *does* grant admin to any connection that
    # lands on its internal ingress listener (172.30.32.x:8094) carrying
    # X-Remote-User-* headers — it trusts those headers because, by its own
    # design, only HA's ingress proxy can reach that port. Core is an add-on on
    # the same internal network, so it reaches :8094 too and presents the
    # installer's HA admin user via those headers. No MA token, no MA UI; reads
    # and the OAuth relay stay on :8095 unchanged.
    INGRESS_PORT = 8094

    def save_provider_admin(self, provider_domain: str, values: dict,
                            ingress_user: tuple, instance_id: str | None = None,
                            timeout: float = 80.0) -> dict:
        conn = self.get_conn()
        if not conn or not conn[0]:
            raise MaUnavailable("Music not connected yet — run ProHost 'Connect Music'")
        host = conn[0]
        last = None
        for h in dict.fromkeys([host, "172.30.32.1"]):
            if not h:
                continue
            c = MaClient(h, self.INGRESS_PORT, token=None, timeout=timeout,
                         ingress_user=ingress_user)
            try:
                c.connect()
            except Exception as e:
                last = e
                try:
                    c.close()
                except Exception:
                    pass
                continue  # couldn't reach this host's ingress port — try the next
            # Connected on the ingress channel: MA processes this as admin. Whatever
            # the save returns — success, or a provider-level error like bad creds —
            # is the real result, so propagate it; don't fall through to another host.
            try:
                return c.command("config/providers/save",
                                 provider_domain=provider_domain, values=values,
                                 instance_id=instance_id)
            finally:
                try:
                    c.close()
                except Exception:
                    pass
        raise MaUnavailable(f"Could not reach the Music ingress channel: {last}")
