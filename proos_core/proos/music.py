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
   flow that *carries the auth token from discovery*. We just confirm it -- one
   tokenless POST. We deliberately do NOT start a user flow: on current MA
   servers a user flow redirects to browser-based auth (OAuth-style) to mint a
   token, which can't be driven from here. Confirming the discovery flow sidesteps
   that entirely. Idempotent: if it's already loaded, or the flow aborts with
   already_configured, that's success.

2. AWARENESS -- report()
   Is the integration loaded? how many players? -> /music + the dashboard
   System Awareness pill. (The add-on running/stopped flag is merged in by the
   server route, which is where the Supervisor token lives.)
"""
from __future__ import annotations

MA_DOMAIN = "music_assistant"

OK = "ok"
FAULT = "fault"
# Flow steps we can complete without input (token came from discovery).
_CONFIRMABLE = ("hassio_confirm", "discovery_confirm")


class MusicLayer:
    def __init__(self, client):
        self.client = client  # RestHAClient (live_ha)

    # ── Ownership ───────────────────────────────────────────────────────────
    def ensure_integration(self) -> dict:
        """Bring the music_assistant integration up, idempotently.

        Returns a small dict describing what happened (for logs + /music/setup):
          action: noop | created | abort | waiting | manual | error
          loaded: bool   (entities present after the call)
        """
        if self.client.integration_entities(MA_DOMAIN):
            return {"action": "noop", "loaded": True}

        try:
            flows = self.client.list_flows()
        except Exception as e:
            return {"action": "error", "loaded": False, "error": f"list flows: {e}"}

        flow = next((f for f in flows if f.get("handler") == MA_DOMAIN), None)
        if not flow:
            return {"action": "waiting", "loaded": False,
                    "detail": "No Music Assistant discovery flow yet "
                              "(is the MA add-on running?)."}

        step = flow.get("step_id")
        if step not in _CONFIRMABLE:
            # user/auth step -> needs a URL or browser login; not auto-confirmable.
            return {"action": "manual", "loaded": False, "step": step,
                    "detail": f"Discovery flow is at '{step}', which needs input."}

        try:
            res = self.client.configure_flow(flow["flow_id"], {})
        except Exception as e:
            return {"action": "error", "loaded": False, "error": f"confirm: {e}"}

        rtype = res.get("type")
        if rtype == "create_entry":
            return {"action": "created", "loaded": True, "title": res.get("title")}
        if rtype == "abort":
            # already_configured (or similar) -> re-check whether entities exist.
            return {"action": "abort", "reason": res.get("reason"),
                    "loaded": bool(self.client.integration_entities(MA_DOMAIN))}
        # Unexpected: an additional form step.
        return {"action": "form", "loaded": False, "result_type": rtype,
                "step": res.get("step_id")}

    # ── Awareness ───────────────────────────────────────────────────────────
    def report(self) -> dict:
        """Music-layer health from HA's side (loaded? how many players?)."""
        try:
            ents = self.client.integration_entities(MA_DOMAIN)
        except Exception as e:
            return {"status": FAULT, "summary": "Cannot reach Home Assistant",
                    "loaded": False, "players": 0, "entities": 0, "error": str(e)}
        players = [e for e in ents if e.startswith("media_player.")]
        if ents:
            return {"status": OK, "summary": f"{len(players)} player(s)",
                    "loaded": True, "players": len(players), "entities": len(ents)}
        return {"status": FAULT, "summary": "Music integration not set up",
                "loaded": False, "players": 0, "entities": 0}
