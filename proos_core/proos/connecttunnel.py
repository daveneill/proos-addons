"""ProOS Core — remote access through ProOS Connect (register 969v, 21 Oct 2026).

Dave, 21 Oct 12:16: "go ahead and build as I would like to test remote access into the apps today", on the structure
he agreed at 12:13: homes, people, who belongs to which home and as what, and the subscription on the home; a
homeowner door and an installer door; installers only with the homeowner's permission.

WHAT THE BOX DOES — and only this:
  * it makes its own key pair once (the platform-standard OpenSSH tool already in Core's image), keeps the private
    half in Core's own store (/data, mode 600, never shown), and gives Connect the public half when it asks to join.
  * while Connect says this box's line is allowed (approved, remote access on, home not suspended), it keeps ONE
    outbound line open to Connect, over which Connect may reach ONE thing: Core's own front door on this box — the
    ProOS pages and the platform data paths those pages use, nothing else (proos/frontdoor.py, register 808). The
    person still signs in to the box exactly as at home. No port is opened on the home's router.
  * the line goes the moment Connect stops listing it (revoked, switched off, suspended).

Connect's host key comes from Connect itself over HTTPS and is pinned; a server that does not hold it is refused.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
KEY_PATH = os.path.join(DATA_DIR, "connect_ssh_ed25519")
KNOWN_PATH = os.path.join(DATA_DIR, "connect_known_hosts")
LOCAL_PORT = 8770                   # Core's own front door inside this box

_lock = threading.Lock()
_state = {"proc": None, "want": None, "since": None, "last_exit": None, "tries": 0}


def public_key() -> str | None:
    """The box's public key for Connect — made once with the platform-standard ssh-keygen."""
    try:
        if not os.path.exists(KEY_PATH):
            os.makedirs(DATA_DIR, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "proos-box", "-f", KEY_PATH],
                           check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(KEY_PATH, 0o600)
        with open(KEY_PATH + ".pub", encoding="utf-8") as fh:
            return " ".join(fh.read().split()[:2])
    except Exception:  # noqa: BLE001
        return None


def command(t: dict, local_port: int = LOCAL_PORT) -> list:
    return ["ssh", "-N", "-T", "-i", KEY_PATH,
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "UserKnownHostsFile=" + KNOWN_PATH, "-o", "StrictHostKeyChecking=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=15",
            "-p", str(int(t.get("ssh_port") or 22)),
            "-R", "%d:127.0.0.1:%d" % (int(t["listen"]), int(local_port)),
            "%s@%s" % (t.get("user") or "tunnel", t["host"])]


def _pin(t: dict) -> bool:
    hk = t.get("host_key")
    if not hk:
        return False
    port = int(t.get("ssh_port") or 22)
    host = t["host"] if port == 22 else "[%s]:%d" % (t["host"], port)
    line = "%s %s\n" % (host, hk)
    try:
        with open(KNOWN_PATH, encoding="utf-8") as fh:
            if fh.read() == line:
                return True
    except Exception:  # noqa: BLE001
        pass
    with open(KNOWN_PATH, "w", encoding="utf-8") as fh:
        fh.write(line)
    return True


def _stop_locked():
    p = _state.get("proc")
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    _state.update({"proc": None, "since": None})


def keep(t: dict | None, popen=subprocess.Popen) -> dict:
    """Converge the line to Connect's answer: open (and reopen) while a tunnel is given, closed when it is not."""
    with _lock:
        if not t or not t.get("listen") or not t.get("host"):
            if _state.get("proc"):
                _stop_locked()
            _state.update({"want": None, "tries": 0})
            return {"line": "off"}
        if t != _state.get("want"):
            _stop_locked()
            _state.update({"want": dict(t), "tries": 0})
        p = _state.get("proc")
        if p and p.poll() is None:
            return {"line": "open", "since": _state.get("since")}
        if p is not None:
            _state["last_exit"] = p.returncode
            _state["proc"] = None
        if not _pin(t) or not os.path.exists(KEY_PATH):
            return {"line": "waiting", "why": "no host key from Connect" if not t.get("host_key") else "no box key"}
        _state["tries"] += 1
        _state["proc"] = popen(command(t), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        _state["since"] = int(time.time())
        return {"line": "opening", "tries": _state["tries"]}


def wanted(status: str | None, tunnel: dict | None) -> dict | None:
    """The line Connect wants: its answer when it answered; when Connect could not be reached, the line it last gave
    (an internet blip must not close a working line; Connect itself refuses a revoked key at the door)."""
    if status == "unreachable":
        with _lock:
            return _state.get("want")
    return tunnel if status == "approved" else None


def reading() -> dict:
    with _lock:
        p = _state.get("proc")
        w = _state.get("want") or {}
        return {"line": "open" if (p and p.poll() is None) else ("off" if not w else "down"),
                "port": w.get("listen"), "host": w.get("host"), "since": _state.get("since"),
                "app_url": w.get("app_url"),
                "last_exit": _state.get("last_exit"), "tries": _state.get("tries")}
