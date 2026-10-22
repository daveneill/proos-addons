"""ProOS Core — the ProOS Cameras add-on, run from Pro. Register 970s.

ProOS Cameras publishes the home's UniFi cameras to Apple Home itself (plan §6). Pro chooses the cameras; Core does
what an installer would otherwise do in the platform's add-on pages — install it from the ProOS repository the box
already has, give it the list, start it — through the Supervisor's own add-on API. Nothing is kept here: the list
lives in the add-on's own options, read back from the Supervisor.
"""
from __future__ import annotations

import threading

NAME = "proos_cameras"
_busy = {"installing": False, "error": None}


def slug(sv) -> str:
    """The add-on's slug in THIS box's ProOS repository (the same prefix as Core's own slug)."""
    me = (sv("GET", "/addons/self/info") or {}).get("slug") or ""
    prefix = me.split("_", 1)[0] if "_" in me else ""
    return "%s_%s" % (prefix, NAME) if prefix else NAME


def status(sv) -> dict:
    s = slug(sv)
    try:
        info = sv("GET", "/addons/%s/info" % s) or {}
    except Exception:  # noqa: BLE001 — not in the store's view yet
        info = {}
    installed = bool(info.get("version"))
    return {"slug": s, "available": bool(info), "installed": installed, "state": info.get("state"),
            "version": info.get("version"), "latest": info.get("version_latest"),
            "cameras": list(((info.get("options") or {}).get("cameras")) or []) if installed else [],
            "installing": _busy["installing"], "error": _busy["error"]}


def _install_then(sv, s, cameras, sleep):
    try:
        try:
            sv("POST", "/store/reload", None)
        except Exception:  # noqa: BLE001
            pass
        sv("POST", "/store/addons/%s/install" % s, None)
        _configure(sv, s, cameras)
        _busy["error"] = None
    except Exception as e:  # noqa: BLE001
        _busy["error"] = str(e)[:300]
    finally:
        _busy["installing"] = False


def _configure(sv, s, cameras):
    sv("POST", "/addons/%s/options" % s, {"options": {"cameras": list(cameras), "log_level": "info"}})
    info = sv("GET", "/addons/%s/info" % s) or {}
    if cameras:
        sv("POST", "/addons/%s/%s" % (s, "restart" if info.get("state") == "started" else "start"), None)
    elif info.get("state") == "started":
        sv("POST", "/addons/%s/stop" % s, None)


def apply(sv, cameras, spawn=None, sleep=None) -> dict:
    """Make ProOS Cameras publish exactly `cameras` (camera entity ids). Installs it first if needed — that builds
    on the box and takes minutes, so it runs in the background and status() says so."""
    cams = [c for c in dict.fromkeys(cameras or []) if isinstance(c, str) and c.startswith("camera.")]
    st = status(sv)
    if not st["installed"]:
        if not cams:
            return dict(st, ok=True)
        if not _busy["installing"]:
            _busy["installing"], _busy["error"] = True, None
            (spawn or (lambda f: threading.Thread(target=f, daemon=True).start()))(
                lambda: _install_then(sv, st["slug"], cams, sleep))
        return dict(status(sv), ok=True)
    _configure(sv, st["slug"], cams)
    return dict(status(sv), ok=True)
