"""
Independent reachability -- the active liveness signal HA's passive availability
can't give in time.

Why this exists: a clean network cut gives no TCP reset, so an integration like
apple_tv waits on its own keepalive timeout (often minutes) before flipping to
'unavailable'. ProOS instead probes the device directly and catches the gap.

Two sources, both returning True / False / None(unknown):
  ip     -> TCP-connect probe from Core. Pure stdlib sockets: a connection that
            opens OR is refused means the host answered (reachable); only a
            timeout / host-unreachable means down. Port-agnostic by design.
  sensor -> read an HA entity that already tracks reachability (a `ping`
            binary_sensor, or a router/UniFi device_tracker). This is the
            production path for the add-on: HA does the ICMP, Core just reads it,
            no IPs baked into Core.
"""
from __future__ import annotations
import socket

# errno values that mean "the host answered" (so it's reachable, port aside).
_REACHABLE_ERRNOS = {
    111,  # ECONNREFUSED (Linux)
    61,   # ECONNREFUSED (macOS)
}


def tcp_reachable(ip: str, port: int = 7000, timeout: float = 1.0) -> bool:
    """True if the host answers at all (connect or refuse); False on timeout."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        err = s.connect_ex((ip, port))
        # 0 = open; ECONNREFUSED = host up, port closed -> still reachable.
        return err == 0 or err in _REACHABLE_ERRNOS
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def resolve(spec: dict, client=None) -> bool | None:
    """Resolve a reachability spec to True/False/None(unknown)."""
    if not spec:
        return None
    if "ip" in spec:
        try:
            return tcp_reachable(spec["ip"], int(spec.get("port", 7000)),
                                 float(spec.get("timeout", 1.0)))
        except Exception:
            return None
    if "sensor" in spec and client is not None:
        try:
            snap = client.snapshot([spec["sensor"]])
            st = snap.get(spec["sensor"], {}).get("state")
            if st in ("on", "home"):
                return True
            # A-6, APPLIED EVERYWHERE (Dave's ruling 16 Aug, register 155;
            # extended here in Stage 2 build 1 of the rescue). Only a witness
            # making a POSITIVE statement convicts: off/not_home means "it
            # left the network" and resolves False. `unavailable`/`unknown`
            # mean the witness itself has not testified — usually the
            # network integration reloading — and resolve None, never False.
            # Before this line, one UniFi reload turned all 111 trackers
            # `unavailable` and every witnessed device in every room was
            # accused of "not responding" in the same instant, through the
            # /health path the dashboard reads. Silence is not evidence.
            if st in ("off", "not_home"):
                return False
        except Exception:
            return None
    return None
