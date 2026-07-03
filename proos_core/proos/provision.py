"""
ProOS Core - first-boot provisioning.

Runs once, with no user present, the first time a freshly-flashed ProOS
image powers on. Every image is byte-identical, so this routine is what
makes each box unique and safe before an installer ever touches it:

  1. detect first boot        (no valid /data/provisioned.json for this box)
  2. mint a per-unit identity (site_id + claim_secret)
  3. kill the shared secret   (rotate/clear the baked owner login)
  4. stamp host identity      (hostname proos-<short>)
  5. write the provision flag  (LAST - so an interrupted run just retries)

After this the box sits "provisioned, idle" until the Pro app claims it for
commissioning. Home Assistant onboarding is NOT done here: it was satisfied
once at image-build time, and first boot never touches it.

Design notes (verify on real hardware before trusting in production):
  * ProCore talks to Core with SUPERVISOR_TOKEN, so it needs no baked token
    of its own. The only shared secret to deal with is the baked owner's
    password - and the cleanest factory image bakes NO homeassistant
    credential for the owner at all, which makes step 3 a no-op. Set
    PROOS_OWNER_HAS_PW=0 for that image and this module does zero Core I/O.
  * admin_change_password over the supervisor-proxied WS connection needs
    owner rights. Confirm it on a scratch box before relying on it.
  * /proc/cpuinfo serial is only readable if the add-on container can see
    it; otherwise a persisted random id is used (clone-detection is then
    best-effort, which is fine - the factory image ships with no flag).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import secrets
import urllib.request
import uuid

_LOG = logging.getLogger("proos.provision")

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
FLAG_PATH = os.path.join(DATA_DIR, "provisioned.json")
HWID_PATH = os.path.join(DATA_DIR, "hardware_id")

SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Username of the headless owner baked into the factory image.
FACTORY_OWNER_USERNAME = os.environ.get("PROOS_OWNER_USER", "proos")
# Set to "1" ONLY in a factory image build where the owner has a shared baked
# password to rotate. Default OFF so this never touches a real owner account
# (e.g. your personal login) on a live box.
FACTORY_OWNER_HAS_PASSWORD = os.environ.get("PROOS_OWNER_HAS_PW", "0") == "1"

HOSTNAME_PREFIX = "proos"


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def is_provisioned() -> bool:
    """True if this exact box has already been provisioned."""
    flag = _load_flag()
    return bool(flag) and flag.get("hardware_id") == _hardware_id()


def ensure_provisioned(ws_call=None) -> dict:
    """Idempotent. Safe to call on every boot - only acts once per box.

    ``ws_call`` is an optional ``ws_call(msg_type, **fields) -> result``
    adapter around ProCore's existing Home Assistant websocket client. It
    is only used to rotate the baked owner password; pass None (or bake an
    image with no owner password) to skip all Core I/O.

    Never raises - a provisioning failure must not stop Core from booting.
    Returns a status dict describing the box.
    """
    try:
        existing = _load_flag()
        if existing and existing.get("hardware_id") == _hardware_id():
            existing["provisioned"] = True
            return existing

        _LOG.info("provision - first boot, self-provisioning this box")
        hardware_id = _hardware_id()
        site_id = uuid.uuid4().hex
        claim_secret = secrets.token_urlsafe(24)

        # kill the shared baked login (best-effort; no-op if none was baked)
        owner_rotated = _harden_owner(ws_call)

        # stamp host identity (best-effort)
        hostname = "%s-%s" % (HOSTNAME_PREFIX, site_id[:8])
        hostname_set = _set_hostname(hostname)

        # write the flag LAST, only once the steps above have completed
        flag = {
            "provisioned": True,
            "site_id": site_id,
            "hardware_id": hardware_id,
            "hostname": hostname,
            "claim_secret": claim_secret,
            "claimed": False,
            "owner_rotated": owner_rotated,
            "hostname_set": hostname_set,
            "image_version": os.environ.get("PROOS_IMAGE_VERSION", ""),
            "provisioned_at": _now(),
        }
        _write_flag(flag)
        _LOG.info("provision - done - site=%s host=%s", site_id[:8], hostname)
        return flag
    except Exception as exc:  # never let provisioning stop boot
        _LOG.warning("provision - failed, will retry next boot: %s", exc)
        return {"provisioned": False, "error": str(exc)}


DASH_HELPER_ENTITY = "input_text.proos_dashboard_page"


def ensure_dashboard_helper(ws_call=None) -> bool:
    """Idempotently ensure the dashboard-goto helper exists. Safe to call every
    boot. Activities (or any automation) set this input_text to a page/room
    token to drive the ProOS dashboard. Never raises."""
    if ws_call is None:
        return False
    try:
        states = ws_call("get_states") or []
        for s in states:
            if isinstance(s, dict) and s.get("entity_id") == DASH_HELPER_ENTITY:
                return True  # already present
        ws_call("input_text/create", name="ProOS Dashboard Page", max=255)
        _LOG.info("provision - created dashboard helper %s", DASH_HELPER_ENTITY)
        return True
    except Exception as exc:  # never let this stop boot
        _LOG.warning("provision - dashboard helper ensure failed: %s", exc)
        return False


def provision_status() -> dict:
    """Cheap, side-effect-free read for the Pro app to see a box's state."""
    flag = _load_flag() or {}
    return {
        "provisioned": bool(flag.get("provisioned")),
        "claimed": bool(flag.get("claimed")),
        "site_id": flag.get("site_id"),
        "hostname": flag.get("hostname"),
    }


def mark_claimed(site_name: str | None = None) -> dict:
    """Called by the commissioning handshake once the Pro app has claimed
    this box. Records the claim; the claim_secret stays out of status."""
    flag = _load_flag()
    if not flag:
        raise RuntimeError("box is not provisioned yet")
    flag["claimed"] = True
    flag["claimed_at"] = _now()
    if site_name:
        flag["site_name"] = site_name
    _write_flag(flag)
    return provision_status()


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------
def _hardware_id() -> str:
    """A value stable for this physical box and, ideally, unique to it - so a
    cloned /data on new hardware re-triggers provisioning. Falls back to a
    persisted random id when no hardware serial is legible."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("serial"):
                    val = line.split(":", 1)[1].strip()
                    if val and set(val) != {"0"}:
                        return "cpu:" + val
    except Exception:
        pass
    try:
        if os.path.exists(HWID_PATH):
            return open(HWID_PATH, encoding="utf-8").read().strip()
        val = "rnd:" + uuid.uuid4().hex
        _atomic_write(HWID_PATH, val)
        return val
    except Exception:
        return "rnd:" + uuid.uuid4().hex


# --------------------------------------------------------------------------
# kill the shared baked secret
# --------------------------------------------------------------------------
def _harden_owner(ws_call) -> bool:
    """Rotate the baked owner password to a discarded per-unit value so the
    shared image secret is dead. ProCore itself uses SUPERVISOR_TOKEN, so no
    one needs the new value. Returns True if a rotation happened."""
    if not FACTORY_OWNER_HAS_PASSWORD:
        return False  # cleanest image bakes no credential - nothing to rotate
    if ws_call is None:
        _LOG.info("provision - no ws_call, skipping owner rotation")
        return False
    try:
        users = ws_call("config/auth/list") or []
        owner = next(
            (u for u in users if u.get("username") == FACTORY_OWNER_USERNAME),
            None,
        )
        if not owner:
            _LOG.info("provision - baked owner '%s' not found, skipping rotation", FACTORY_OWNER_USERNAME)
            return False
        ws_call(
            "config/auth_provider/homeassistant/admin_change_password",
            user_id=owner["id"],
            password=secrets.token_urlsafe(32),  # generated, used, discarded
        )
        _LOG.info("provision - baked owner password rotated")
        return True
    except Exception as exc:
        _LOG.warning("provision - owner rotation failed: %s", exc)
        return False


# --------------------------------------------------------------------------
# host identity
# --------------------------------------------------------------------------
def _set_hostname(hostname: str) -> bool:
    # Off by default so deploying onto a live box never renames it. Enable
    # (PROOS_SET_HOSTNAME=1) only in the factory image / true zero-touch boot.
    if os.environ.get("PROOS_SET_HOSTNAME", "0") != "1":
        return False
    try:
        _sup("POST", "/host/options", {"hostname": hostname})
        return True
    except Exception as exc:
        _LOG.warning("provision - hostname set failed: %s", exc)
        return False


def _sup(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SUPERVISOR + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + SUPERVISOR_TOKEN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


# --------------------------------------------------------------------------
# flag persistence
# --------------------------------------------------------------------------
def _load_flag():
    try:
        with open(FLAG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_flag(flag: dict):
    _atomic_write(FLAG_PATH, json.dumps(flag, indent=2))


def _atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
