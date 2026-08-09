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

    def ensure_pairing_admin(self, ingress_user: tuple) -> dict:
        # THE PAIRING USER MUST BE ADMIN (Dave, 9 Aug 2026). The white-label
        # MA server (2.9.5, new auth model) mints the Home-Assistant discovery
        # token with role "user"; the HA integration play handler makes an
        # admin call (auth.list_users) on EVERY play-content request, so the
        # MA server refused every play from anyone through HA — 4 days,
        # silently (see register 23c). Dave: "we don't use HA... why is this
        # not standard, will this happen on every installation?" IT WOULD —
        # so the guarantee lives HERE, in the product: on boot and on demand,
        # Core verifies the pairing user's role over its ingress-admin
        # channel and promotes it. No UI, no installer step, self-repairs
        # after any token re-mint or Music update. Command names confirmed
        # from music_assistant_client source (auth.py): list = "auth/users",
        # update = "auth/user/update" (role: "admin"/"user", admin only).
        users = self._admin_command(ingress_user, "auth/users") or []
        row = next((u for u in users
                    if (u.get("username") or "") == "homeassistant_system"), None)
        if not row:
            return {"ok": True, "state": "no_pairing_user"}
        if str(row.get("role") or "").lower() == "admin":
            return {"ok": True, "state": "already_admin"}
        uid = row.get("user_id") or row.get("id")
        self._admin_command(ingress_user, "auth/user/update",
                            user_id=uid, role="admin")
        return {"ok": True, "state": "promoted", "user_id": uid,
                "was": row.get("role")}

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

    def recommendations(self) -> list:
        """THE ENGINE'S OWN DISCOVER (Dave, 9 Aug 2026).

        ProOS built its Discover page by hand out of library queries. Two faults
        fell out of that, and Dave hit both: the content never matched what the
        music engine actually recommends (Stations For You, Discovery Station,
        Random Albums, New Albums …), and the tiles carried `library://` rows —
        which stop being playable the moment a service is re-linked and the old
        rows lose their provider mapping ("No playable items found", nothing
        happens on tap, 9 Aug).

        This is the engine's real recommendations feed — the same call its own
        Discover renders — returning folders of PROVIDER-native items that play.
        One mechanism, not a home-made approximation of one.
        """
        with self._client() as c:
            return c.command("music/recommendations") or []

    def browse(self, path: str | None = None) -> list:
        """Browse the MA tree by service/provider. path=None → the root folders
        (Library + each provider: Spotify, Apple Music, RadioBrowser …); a folder's
        own path drills in. Mirrors MA's own Browse."""
        with self._client() as c:
            return c.command("music/browse", path=path) or []

    # ── Stage 1 Music Mirror: Core is the only music API ──────────────────────
    # (Audit 2026-08-09, built 10 Aug 2026.) ProOS re-implemented the engine by
    # hand and played through Home Assistant's media_player.play_media — which
    # raised "No playable items found" before the engine was ever asked, then
    # ProOS swallowed it into "answering slowly". That cost a full day.
    #
    # These are the fix expressed as a rule: every method hands the engine ITS
    # OWN command (the same one its UI uses), passes arguments through UNCHANGED,
    # returns the payload untouched, and RAISES on failure so the route reports
    # the engine's own reason. ProOS renders the engine's answer; it never
    # invents or reshapes one.
    #
    # Media types and player commands are ALLOWLISTED so this passthrough can
    # never become a path to arbitrary or admin engine commands — one mechanism
    # per question, with a guard.

    # The seven library pages the engine offers (its own Library tabs).
    # NOTE the plural "radios": every engine media controller is plural, and
    # `music/radio/library_items` is rejected by the engine itself ("Invalid
    # command: music/radio/") — measured live 10 Aug 2026. Getting this one
    # name wrong is why the Radio page was empty while the engine's own Radio
    # page listed stations. (The engine's SEARCH result key is the singular
    # "radio" — the two are not the same word; do not "tidy" them to match.)
    LIBRARY_MEDIA_TYPES = ("artists", "albums", "tracks", "playlists",
                           "radios", "podcasts", "audiobooks")

    def _check_media_type(self, media_type: str) -> None:
        if media_type not in self.LIBRARY_MEDIA_TYPES:
            raise MaUnavailable(f"Unknown music library type: {media_type!r}")

    def library_items(self, media_type: str, **filters) -> list:
        """One library page (music/<type>/library_items). filters (limit, offset,
        order_by, search, favorite, provider …) ride to the engine unchanged."""
        self._check_media_type(media_type)
        with self._client() as c:
            return c.command(f"music/{media_type}/library_items", **filters) or []

    def library_count(self, media_type: str, **filters):
        """How many items in a library page (music/<type>/count). Returns the
        engine's number as-is (0 is a real answer, never coerced away)."""
        self._check_media_type(media_type)
        with self._client() as c:
            return c.command(f"music/{media_type}/count", **filters)

    def item(self, **args) -> dict:
        """A single item's full record (music/item). Args (media_type, item_id,
        provider …) are passed through exactly as the caller sends them, so the
        mirror never has to guess the engine's argument names."""
        with self._client() as c:
            return c.command("music/item", **args) or {}

    def item_by_uri(self, **args) -> dict:
        """A single item resolved from its uri (music/item_by_uri)."""
        with self._client() as c:
            return c.command("music/item_by_uri", **args) or {}

    def recently_played(self, **args) -> list:
        """The engine's Recently Played feed (music/recently_played_items)."""
        with self._client() as c:
            return c.command("music/recently_played_items", **args) or []

    def recently_added(self, **args) -> list:
        """The engine's Recently Added feed (music/recently_added_tracks)."""
        with self._client() as c:
            return c.command("music/recently_added_tracks", **args) or []

    def in_progress(self, **args) -> list:
        """In-progress items — resume points for podcasts/audiobooks
        (music/in_progress_items)."""
        with self._client() as c:
            return c.command("music/in_progress_items", **args) or []

    def favorite_remove(self, uri: str) -> dict:
        """Remove an item from favourites by its uri (music/favorites/remove_item).
        The partner of favorite_add (register: favourites add/remove)."""
        with self._client() as c:
            return c.command("music/favorites/remove_item", item=uri)

    def queue_play_media(self, queue_id: str, media, **opts) -> dict:
        """PLAY content on a player's queue (player_queues/play_media) — the
        engine's own play call. THIS REPLACES the Home Assistant
        media_player.play_media path that raised 'No playable items found' and
        got swallowed (register 35). A failure raises with the engine's reason so
        the route surfaces it verbatim — the whole point of Stage 2. `media` is a
        uri or a list of uris; `opts` (option: play/replace/next/add, radio_mode
        …) ride to the engine unchanged."""
        with self._client() as c:
            return c.command("player_queues/play_media",
                             queue_id=queue_id, media=media, **opts)

    # Transport/volume the dashboard drives (players/cmd/*). Deliberately the
    # play/pause/seek/volume/power set only — grouping, config and admin
    # commands are NOT here, so the passthrough cannot reach them.
    PLAYER_COMMANDS = ("play", "pause", "play_pause", "stop", "resume", "next",
                       "previous", "seek", "volume_set", "volume_up",
                       "volume_down", "volume_mute", "power")

    def player_command(self, cmd: str, player_id: str, **args) -> dict:
        """Run one transport/volume command against a player (players/cmd/<cmd>).
        cmd is allowlisted; player_id and any command args (volume_level,
        position, powered, muted …) ride to the engine unchanged."""
        if cmd not in self.PLAYER_COMMANDS:
            raise MaUnavailable(f"Unknown player command: {cmd!r}")
        with self._client() as c:
            return c.command(f"players/cmd/{cmd}", player_id=player_id, **args)

    # ── Stage 6 Music Mirror: Genres — the whole subsystem ────────────────────
    # (Audit 2026-08-09.) ProOS has never touched genres; the engine has a full
    # genre controller — a browsable library plus curator admin (merge, aliases,
    # media mappings, exclusions). All of it mirrors one-for-one: hand the engine
    # its own music/genres/<cmd>, args passed through unchanged, raise on failure.
    # The command is ALLOWLISTED (reads vs writes) so the passthrough can never
    # reach an arbitrary engine command; the WRITES are curator actions and the
    # route gates them to the installer.
    GENRE_READS = ("library_items", "count", "media_counts",
                   "genres_for_media_item", "genre_exclusions_for_media_item",
                   "global_exclusions", "scanner_status", "radio_mode_base_tracks")
    GENRE_WRITES = ("add", "merge", "add_alias", "remove_alias", "promote_alias",
                    "add_media_mapping", "remove_media_mapping", "restore_defaults",
                    "scan_mappings", "exclude_genre_from_media_item",
                    "remove_genre_exclusion", "remove_global_exclusion")

    def genre(self, cmd: str, **args):
        """Mirror one engine genre command (music/genres/<cmd>). cmd is
        allowlisted (reads + writes); args ride to the engine unchanged. Returns
        the engine's payload untouched; raises on failure so the route reports
        the reason."""
        if cmd not in self.GENRE_READS and cmd not in self.GENRE_WRITES:
            raise MaUnavailable(f"Unknown genre command: {cmd!r}")
        with self._client() as c:
            return c.command(f"music/genres/{cmd}", **args)

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

    # ── Assist gateway: search + create ───────────────────────────────────────
    def search(self, query: str, media_types=None, limit: int = 8) -> dict:
        """Global MA search across every configured provider. media_types is a
        list of MA type strings ('artist','album','track','playlist','radio');
        None = all. Returns MA's SearchResults (artists/albums/tracks/…)."""
        args = {"search_query": query, "limit": int(limit)}
        if media_types:
            args["media_types"] = list(media_types)
        with self._client() as c:
            return c.command("music/search", **args) or {}

    def create_playlist(self, name: str, provider=None) -> dict:
        """Create a new (library) playlist. provider omitted → MA's builtin
        provider, which accepts tracks from any source (perfect for a
        model-curated mix). Returns the created Playlist (carrying item_id)."""
        args = {"name": name}
        if provider:
            args["provider_instance_or_domain"] = provider
        with self._client() as c:
            return c.command("music/playlists/create_playlist", **args) or {}

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
