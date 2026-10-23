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
            # 970jj: which of them are Apple's Secure Video cameras (the add-on's own option, read back)
            "secure_video": list(((info.get("options") or {}).get("secure_video")) or []) if installed else [],
            "installing": _busy["installing"], "error": _busy["error"]}


def _install_then(sv, s, cameras, sleep, secure=None):
    try:
        try:
            sv("POST", "/store/reload", None)
        except Exception:  # noqa: BLE001
            pass
        sv("POST", "/store/addons/%s/install" % s, None)
        _configure(sv, s, cameras, secure)
        _busy["error"] = None
    except Exception as e:  # noqa: BLE001
        _busy["error"] = str(e)[:300]
    finally:
        _busy["installing"] = False


def _configure(sv, s, cameras, secure=None):
    """970jj: the add-on's options are WRITTEN WHOLE by the Supervisor, so every option it already has is kept and only
    the list (and, when Pro says so, which are Secure Video) changes. Before this, saving the list wrote
    {cameras, log_level} and silently dropped `secure_video` — Front Door and Backyard went back to standard cameras.
    `secure`: None keeps the add-on's own choice; a list replaces it. Either way only cameras in the list stay in it."""
    now = (sv("GET", "/addons/%s/info" % s) or {}).get("options") or {}
    keep = (now.get("secure_video") or []) if secure is None else secure
    opts = dict(now)
    opts.update({"cameras": list(cameras), "log_level": now.get("log_level") or "info",
                 "secure_video": [c for c in dict.fromkeys(keep) if isinstance(c, str) and c in cameras]})
    sv("POST", "/addons/%s/options" % s, {"options": opts})
    info = sv("GET", "/addons/%s/info" % s) or {}
    if cameras:
        sv("POST", "/addons/%s/%s" % (s, "restart" if info.get("state") == "started" else "start"), None)
    elif info.get("state") == "started":
        sv("POST", "/addons/%s/stop" % s, None)


def apply(sv, cameras, spawn=None, sleep=None, secure=None) -> dict:
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
                lambda: _install_then(sv, st["slug"], cams, sleep, secure))
        return dict(status(sv), ok=True)
    _configure(sv, st["slug"], cams, secure)
    return dict(status(sv), ok=True)
