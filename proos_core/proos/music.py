"""
ProOS Core -- Music layer (Music Assistant: ProOS-owned integration + awareness)
================================================================================

Two jobs, both kept deliberately small:

1. OWNERSHIP -- ensure_integration()
   The Music Assistant *server* runs as an add-on; the HA `music_assistant`
   integration (which ships in HA Core) is what turns MA's players into
   media_player entities the dashboard reads. ProOS owns that integration's
   setup so the installer never touches HA.

   The add-on advertises HA discovery, so HA already raises a 'hassio' config
   flow that *carries the auth token from discovery*. We just confirm it. We do
   NOT start a user flow: on current MA servers a user flow redirects to
   browser-based auth to mint a token, which can't be driven from here -- the
   add-on discovery flow already has the token, so confirming it is enough.

   Reading that in-progress flow is WebSocket-only (HA's REST flow index is
   POST-only); confirming it and detecting the resulting entry are REST. The
   call is idempotent: if it's already configured, or the flow aborts with
   already_configured, that's success.

2. AWARENESS -- report()
   Is the integration configured + loaded? how many players? -> /music + the
   dashboard System Awareness pill. (The add-on running/stopped flag is merged
   in by the server route, where the Supervisor token lives.)
"""
from __future__ import annotations

MA_DOMAIN = "music_assistant"

OK = "ok"
PENDING = "pending"
FAULT = "fault"
# Flow steps we can complete with no input (token came from add-on discovery).
_CONFIRMABLE = ("hassio_confirm", "discovery_confirm")


class MusicLayer:
    def __init__(self, client):
        self.client = client  # RestHAClient (live_ha)

    # ── Ownership ───────────────────────────────────────────────────────────
    def ensure_integration(self) -> dict:
        """Bring the music_assistant integration up, idempotently.

        Returns a small dict for logs + POST /music/setup:
          action: noop | created | abort | waiting | manual | error
          loaded: bool
        """
        entries = self._entries()
        if entries:
            state = entries[0].get("state")
            return {"action": "noop", "loaded": state == "loaded", "state": state}

        # Not configured yet -> find the add-on discovery flow (WebSocket) ...
        try:
            flows = self.client.flow_progress()
        except Exception as e:
            return {"action": "error", "loaded": False, "error": f"flow progress: {e}"}

        flow = next((f for f in flows if f.get("handler") == MA_DOMAIN), None)
        if not flow:
            return {"action": "waiting", "loaded": False,
                    "detail": "No ProOS Music discovery record yet "
                              "(is the ProOS Music add-on running?)."}

        step = flow.get("step_id")
        if step not in _CONFIRMABLE:
            return {"action": "manual", "loaded": False, "step": step,
                    "detail": f"Discovery flow is at '{step}', which needs input."}

        # ... and confirm it (REST).
        try:
            res = self.client.configure_flow(flow["flow_id"], {})
        except Exception as e:
            return {"action": "error", "loaded": False, "error": f"confirm: {e}"}

        rtype = res.get("type")
        if rtype == "create_entry":
            return {"action": "created", "loaded": True, "title": res.get("title")}
        if rtype == "abort":
            return {"action": "abort", "reason": res.get("reason"),
                    "loaded": bool(self._entries())}
        return {"action": "form", "loaded": False, "result_type": rtype,
                "step": res.get("step_id")}

    # ── Awareness ───────────────────────────────────────────────────────────
    def report(self) -> dict:
        """Music-layer health from HA's side (configured? loaded? how many players?)."""
        try:
            entries = self._entries()
        except Exception as e:
            return {"status": FAULT, "summary": "Cannot reach the system",
                    "loaded": False, "players": 0, "configured": False, "error": str(e)}

        if not entries:
            return {"status": FAULT, "summary": "Music integration not set up",
                    "loaded": False, "players": 0, "configured": False}

        state = entries[0].get("state")
        if state != "loaded":
            return {"status": PENDING, "summary": f"Music integration {state}",
                    "loaded": False, "players": 0, "configured": True, "state": state}

        try:
            players = [e for e in self.client.integration_entities(MA_DOMAIN)
                       if e.startswith("media_player.")]
        except Exception:
            players = []
        return {"status": OK, "summary": f"{len(players)} player(s)",
                "loaded": True, "players": len(players), "configured": True}

    # ── internals ───────────────────────────────────────────────────────────
    def _entries(self) -> list:
        return self.client.config_entries(MA_DOMAIN)
