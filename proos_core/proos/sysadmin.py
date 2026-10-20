"""
ProOS Core — system administration (tech tier).

Supervisor-backed operations so the Tech Tools box fully replaces the native HA
UI: add-on lifecycle (start/stop/restart/rebuild/update), system health
(versions + resources + disk), and Core actions (config check / restart /
reload). The calling route enforces tech/owner identity; this trusts it is
gated. Everything goes through ProCore's SUPERVISOR_TOKEN.
"""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _req(method: str, path: str, payload=None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SUPERVISOR + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _info(path: str) -> dict:
    try:
        d = _req("GET", path, timeout=15)
        return d.get("data") or d
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


# ── Add-on lifecycle ─────────────────────────────────────────────────────────
_ADDON_ACTIONS = {"start", "stop", "restart", "rebuild", "update"}


def addon_action(slug: str, action: str) -> dict:
    if action not in _ADDON_ACTIONS:
        return {"error": "unknown action '%s'" % action}
    if not slug:
        return {"error": "add-on slug required"}
    try:
        _req("POST", "/addons/%s/%s" % (slug, action), timeout=300)
        return {"ok": True, "slug": slug, "action": action}
    except urllib.error.HTTPError as e:  # supervisor returns a message body
        try:
            msg = json.loads(e.read().decode()).get("message")
        except Exception:
            msg = str(e)
        return {"ok": False, "slug": slug, "action": action, "error": msg}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "slug": slug, "action": action}


# ── Health ───────────────────────────────────────────────────────────────────
def _num(x):
    try:
        return round(float(x), 1)
    except Exception:
        return x


def health() -> dict:
    sup = _info("/supervisor/info")
    host = _info("/host/info")
    core = _info("/core/info")
    osx = _info("/os/info")
    core_stats = _info("/core/stats")
    sup_stats = _info("/supervisor/stats")
    return {
        "ok": True,
        "versions": {
            "core": core.get("version"),
            "core_latest": core.get("version_latest"),
            "supervisor": sup.get("version"),
            "os": osx.get("version"),
            "hostname": host.get("hostname"),
            "board": osx.get("board"),
        },
        "disk": {
            "total": host.get("disk_total"),
            "used": host.get("disk_used"),
            "free": host.get("disk_free"),
        },
        "resources": {
            "core_cpu": _num(core_stats.get("cpu_percent")),
            "core_mem_pct": _num(core_stats.get("memory_percent")),
            "core_mem_usage": core_stats.get("memory_usage"),
            "core_mem_limit": core_stats.get("memory_limit"),
            "supervisor_cpu": _num(sup_stats.get("cpu_percent")),
            "supervisor_mem_pct": _num(sup_stats.get("memory_percent")),
        },
        "state": {
            "supervisor_healthy": sup.get("healthy"),
            "supervisor_supported": sup.get("supported"),
        },
    }


# ── Self-update (Dave, 3 Aug: update Core from Pro — no HA visit) ────────────
# Ground truth read live first: this add-on is installed from the ProOS
# Add-ons GitHub repo, so an update is two Supervisor calls — store reload,
# then update-own-slug. Supervisor is a separate process: it completes the
# rebuild while this container dies mid-way. The slug is always resolved
# from /addons/self/info, never hardcoded (fleet boxes differ from dev).
def self_info(reload_store: bool = False) -> dict:
    if reload_store:
        try:
            _req("POST", "/store/reload", timeout=120)
        except Exception:  # noqa: BLE001 — stale store is survivable
            pass
    d = _info("/addons/self/info")
    if d.get("_error"):
        return {"ok": False, "error": d["_error"]}
    return {"ok": True, "name": d.get("name"), "slug": d.get("slug"),
            "version": d.get("version"),
            "version_latest": d.get("version_latest"),
            "update_available": bool(d.get("update_available"))}


def self_update() -> dict:
    """Attempt the self-update SYNCHRONOUSLY and answer honestly.

    Field truth (3 Aug, Dave's first two update attempts were silent
    no-ops): Supervisor POLICY-refuses an add-on updating itself —
    "App <slug> can't update itself!" — instantly. The old fire-and-forget
    thread swallowed that refusal. So: a refusal comes back inside a
    second and is RETURNED; a timeout can only mean the update actually
    started (a future Supervisor may allow it). Pro's Update button now
    drives hassio.addon_update through the home engine instead — that
    request carries the home engine's authority and is allowed; this
    route stays for API callers and reports the truth.
    """
    try:
        _req("POST", "/store/reload", timeout=120)
    except Exception:  # noqa: BLE001
        pass
    d = _info("/addons/self/info")
    if d.get("_error"):
        return {"ok": False, "error": d["_error"]}
    slug = d.get("slug")
    if not slug:
        return {"ok": False, "error": "could not resolve own slug"}
    if not d.get("update_available"):
        return {"ok": False, "error": "no update available",
                "version": d.get("version")}
    target = d.get("version_latest")
    try:
        _req("POST", "/store/addons/%s/update" % slug, timeout=15)
        return {"ok": True, "updating_to": target, "slug": slug,
                "from": d.get("version")}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message") or str(e)
        except Exception:  # noqa: BLE001
            msg = str(e)
        return {"ok": False, "error": msg, "slug": slug,
                "updating_to": target, "from": d.get("version")}
    except Exception:  # noqa: BLE001 — timeout: the update is RUNNING
        return {"ok": True, "updating_to": target, "slug": slug,
                "from": d.get("version"), "note": "update in progress"}


# ── Core actions ─────────────────────────────────────────────────────────────
_CORE_ACTIONS = {"restart", "stop", "start", "check", "reload", "update"}


def core_action(action: str) -> dict:
    if action not in _CORE_ACTIONS:
        return {"error": "unknown action '%s'" % action}
    try:
        _req("POST", "/core/%s" % action, timeout=120)
        return {"ok": True, "action": action}
    except urllib.error.HTTPError as e:
        # /core/check returns 400 + the config error when invalid.
        try:
            body = e.read().decode()
            msg = json.loads(body).get("message") or body
        except Exception:
            msg = str(e)
        return {"ok": False, "action": action, "error": msg}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "action": action}
