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
import urllib.error
import urllib.parse
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

# HA's config dir, mounted via homeassistant_config:rw. Newer HA mounts it at
# /homeassistant, older at /config. The add-on bundles the factory dashboards
# under /app/www-dist; first boot drops them into <config>/www so a fresh box
# serves the ProOS UI with zero manual /config/www copy and no native HA.
HA_CONFIG = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
WWW_SRC = os.environ.get("PROOS_WWW_SRC", "/app/www-dist")


def deploy_dashboards(overwrite: bool = False) -> dict:
    """Copy bundled dashboards into <ha_config>/www. Default overwrite=False so
    an installer's/dev's own updated dashboards are never clobbered — a file is
    only written if absent (fresh box) or if overwrite is forced. Best-effort
    per file; a failure here must never block boot."""
    import shutil
    dest_dir = os.path.join(HA_CONFIG, "www")
    result = {"dest": dest_dir, "copied": [], "skipped": [], "errors": []}
    if not os.path.isdir(WWW_SRC):
        result["errors"].append("no bundled dashboards at %s" % WWW_SRC)
        return result
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["errors"].append("mkdir %s failed: %s" % (dest_dir, e))
        return result
    for name in sorted(os.listdir(WWW_SRC)):
        src = os.path.join(WWW_SRC, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest_dir, name)
        if os.path.exists(dst) and not overwrite:
            result["skipped"].append(name)
            continue
        try:
            shutil.copy2(src, dst)
            result["copied"].append(name)
        except Exception as e:  # noqa: BLE001
            result["errors"].append("%s: %s" % (name, e))
    return result


def firstboot(ws_call=None, overwrite_dashboards: bool = False) -> dict:
    """First-boot hand-off: run standard provisioning, ensure the dashboard
    helpers, and drop the factory dashboards into <ha_config>/www. Idempotent
    and safe to call on every boot — this is what makes a fresh box serve ProOS
    with no manual deploy and no native HA. ProCore calls this itself on boot,
    so the OS image only has to install + start Core."""
    out = {"ok": True}
    try:
        out["provision"] = ensure_provisioned(ws_call)
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["provision_error"] = str(e)
    try:
        out["dashboard_helper"] = ensure_dashboard_helper(ws_call)
    except Exception as e:  # noqa: BLE001
        out["dashboard_helper_error"] = str(e)
    try:
        out["services_area"] = ensure_services_area(ws_call)
    except Exception as e:  # noqa: BLE001
        out["services_area_error"] = str(e)
    out["dashboards"] = deploy_dashboards(overwrite=overwrite_dashboards)
    return out


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
DASH_CMD_ENTITY = "input_text.proos_dashboard_cmd"


def ensure_dashboard_helper(ws_call=None) -> bool:
    """Idempotently ensure the dashboard helpers exist. Safe to call every boot.
    - proos_dashboard_page: activities/automations set this to a page/room token.
    - proos_dashboard_cmd:  the Pro console pushes commands (rediscover, reload,
      goto, message) here. Never raises."""
    if ws_call is None:
        return False
    try:
        states = ws_call("get_states") or []
        have = {s.get("entity_id") for s in states if isinstance(s, dict)}
        for ent, name in ((DASH_HELPER_ENTITY, "ProOS Dashboard Page"),
                          (DASH_CMD_ENTITY, "ProOS Dashboard Cmd")):
            if ent not in have:
                ws_call("input_text/create", name=name, max=255)
                _LOG.info("provision - created dashboard helper %s", ent)
        return True
    except Exception as exc:  # never let this stop boot
        _LOG.warning("provision - dashboard helper ensure failed: %s", exc)
        return False


# The standing "global" room. An area with the dashboard_system label never shows
# as a room tile on the homeowner dashboard, but devices parked in it still surface
# on the whole-home pages (security, access, irrigation). ProOS provisions one on
# every install so infrastructure devices (UniFi, security bridges, etc.) have a
# home from the start without the installer building it each project.
# Named "Home" because this one area does both jobs: it holds the whole-home
# infrastructure devices AND its picture is what the homeowner dashboard uses as
# the Home-page background (the dashboard binds it by this exact name). The
# dashboard_system label still keeps it out of the room tiles.
SERVICES_AREA_NAME = os.environ.get("PROOS_SERVICES_AREA", "Home")
LEGACY_SERVICES_NAMES = ("services",)      # pre-1.0.244 installs
DASHBOARD_SYSTEM_LABEL = "dashboard_system"


def ensure_services_area(ws_call=None) -> dict:
    """Idempotently ensure the standing 'Home' area exists and carries the
    dashboard_system label (a global/infrastructure room whose picture is the
    dashboard's home background). Safe to call every boot and after a factory
    reset; only creates what's missing. Never raises."""
    if ws_call is None:
        return {"ok": False, "reason": "no ws_call"}
    try:
        # 1) the dashboard_system label (label_id is the slug of the name, which
        #    is what pro.html/dashboard.html key off directly).
        labels = ws_call("config/label_registry/list") or []
        lab = next((l for l in labels
                    if l.get("label_id") == DASHBOARD_SYSTEM_LABEL
                    or (l.get("name") or "").strip().lower() == DASHBOARD_SYSTEM_LABEL), None)
        if not lab:
            lab = ws_call("config/label_registry/create", name=DASHBOARD_SYSTEM_LABEL,
                          color="grey", icon="mdi:server-network") or {}
        label_id = lab.get("label_id") or DASHBOARD_SYSTEM_LABEL
        # 2) the Services area
        areas = ws_call("config/area_registry/list") or []
        area = next((a for a in areas
                     if (a.get("name") or "").strip().lower() == SERVICES_AREA_NAME.lower()), None)
        created = False
        if not area:
            # adopt a legacy "Services" area rather than creating a second one —
            # its devices, id and history carry straight over
            legacy = next((a for a in areas
                           if (a.get("name") or "").strip().lower() in LEGACY_SERVICES_NAMES), None)
            if legacy and legacy.get("area_id"):
                ws_call("config/area_registry/update",
                        area_id=legacy["area_id"], name=SERVICES_AREA_NAME)
                legacy["name"] = SERVICES_AREA_NAME
                area = legacy
            else:
                area = ws_call("config/area_registry/create", name=SERVICES_AREA_NAME) or {}
                created = True
        area_id = area.get("area_id")
        # 3) Label it dashboard_system ONLY when we just created it (fresh box /
        #    after a reset). If the area already existed we leave its labels alone,
        #    so an installer who intentionally un-excluded Services isn't overridden
        #    on the next boot.
        if created:
            cur = list(area.get("labels") or [])
            if area_id and label_id not in cur:
                ws_call("config/area_registry/update", area_id=area_id, labels=cur + [label_id])
        _LOG.info("provision - services area ensured (area=%s created=%s)", area_id, created)
        return {"ok": True, "area_id": area_id, "created": created, "label_id": label_id}
    except Exception as exc:  # never let this stop boot
        _LOG.warning("provision - ensure_services_area failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Auto-onboard: hidden Developer owner + default Installer ──────────────────
# On any box that isn't onboarded (fresh image OR post-factory-reset), ProCore
# creates HA's owner headlessly and completes onboarding, so no human ever
# touches HA setup. The owner is the DEVELOPER account (Protech servicing) with
# a PER-BOX DERIVED password (model B): HMAC(secret, hardware_id) — Protech can
# recompute it to service any box, but a leak from one box never opens the fleet.
# It then creates the default INSTALLER login (admin) the installer is forced to
# change on first commission.
CORE = "http://supervisor/core"
OWNER_USERNAME = os.environ.get("PROOS_OWNER_USER", "developer")
# Protech servicing secret — set as a masked add-on option on shipped boxes.
# The build-time fallback is for dev/bench boxes only (NOT fleet-safe).
OWNER_SECRET = os.environ.get("PROOS_OWNER_SECRET", "") or "proos-dev-bench-only"
INSTALLER_USERNAME = os.environ.get("PROOS_INSTALLER_USER", "installer")
INSTALLER_DEFAULT_PW = os.environ.get("PROOS_INSTALLER_PW", "proos-install")
ONBOARD_CLIENT = os.environ.get("PROOS_ONBOARD_CLIENT", "http://homeassistant.local:8123/")
INSTALLER_STATE = os.path.join(DATA_DIR, "installer_state.json")


def owner_password() -> str:
    """Per-box derived Developer/Owner password (model B). Deterministic from the
    box's hardware id + the Protech secret, so Protech can recompute it to service
    any box while a leak from a single box never opens the fleet."""
    import hashlib
    import hmac
    digest = hmac.new(OWNER_SECRET.encode(), _hardware_id().encode(),
                      hashlib.sha256).hexdigest()
    return "Px-" + digest[:20]


_HA_DIRECT = os.environ.get("PROOS_HA_DIRECT", "").rstrip("/")


def _onboard_bases():
    # Direct HA first — public onboarding needs no token and dodges the
    # Supervisor→Core proxy, which doesn't cleanly serve onboarding on a fresh
    # box. The proxy is the last-resort fallback.
    bases = []
    if _HA_DIRECT:
        bases.append((_HA_DIRECT, None))
    bases.append(("http://homeassistant:8123", None))
    bases.append(("http://supervisor/core", SUPERVISOR_TOKEN))
    return bases


def _http(base, path, method="GET", payload=None, token=None, form=False, timeout=20):
    if form:
        data = urllib.parse.urlencode(payload).encode() if payload is not None else None
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode() if payload is not None else None
        ctype = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


def _pick_onboard_base():
    """Find a base URL that serves HA's onboarding API. Returns
    (base, token, state) with state 'fresh'|'done'|'unknown'."""
    for base, token in _onboard_bases():
        try:
            req = urllib.request.Request(base + "/api/onboarding", method="GET")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=10) as r:
                steps = json.loads(r.read().decode() or "[]")
            u = next((s for s in steps if s.get("step") == "user"), None)
            return base, token, ("done" if (u and u.get("done")) else "fresh")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return base, token, "done"
        except Exception:
            continue
    return None, None, "unknown"


def _set_installer_must_change(user_id, on=True):
    try:
        with open(INSTALLER_STATE, "w", encoding="utf-8") as fh:
            json.dump({"user_id": user_id, "must_change": bool(on)}, fh)
    except Exception:  # noqa: BLE001
        pass


def installer_must_change(user_id) -> bool:
    try:
        with open(INSTALLER_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        return bool(st.get("must_change")) and st.get("user_id") == user_id
    except Exception:  # noqa: BLE001
        return False


def clear_installer_must_change(user_id):
    _set_installer_must_change(user_id, False)


def _ensure_installer(ws_call):
    """Create the default Installer login if it hasn't been made yet. Idempotent,
    keyed on the installer_state flag — so a box that got its owner but not its
    installer (a partial onboard) self-heals on the next boot instead of locking
    everyone out. Never raises."""
    if os.path.exists(INSTALLER_STATE):
        return {"exists": True}
    try:
        from proos import users as _users
        res = _users.create_user(ws_call, name="Installer", role="installer",
                                 username=INSTALLER_USERNAME,
                                 password=INSTALLER_DEFAULT_PW)
        _set_installer_must_change(res.get("user_id"), True)
        return {"created": res.get("username")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def auto_onboard(ws_call=None) -> dict:
    """Headless onboarding for an un-onboarded box. Creates the Developer owner
    (derived pw), completes HA onboarding, then creates the default Installer
    login (forced change on first use). Idempotent + never raises. No-op on an
    already-onboarded box."""
    out = {"ok": True}
    base = None
    try:
        base, token, state = _pick_onboard_base()
        out["state"] = state
        out["base"] = base
        if state != "fresh":
            out["already"] = (state == "done")
            if state == "unknown":
                out["ok"] = False
                out["error"] = "no onboarding endpoint reachable (tried direct + proxy)"
            elif state == "done" and ws_call:
                # onboarded already — but make sure the installer got made
                # (self-heal a partial onboard: owner created, installer skipped)
                out["installer"] = _ensure_installer(ws_call)
            return out
        r = _http(base, "/api/onboarding/users", "POST", {
            "client_id": ONBOARD_CLIENT, "name": "ProOS Developer",
            "username": OWNER_USERNAME, "password": owner_password(),
            "language": "en"}, token=token)
        out["owner_created"] = True
        code = r.get("auth_code")
        owner_token = None
        if code:
            try:
                tok = _http(base, "/auth/token", "POST", {
                    "grant_type": "authorization_code", "code": code,
                    "client_id": ONBOARD_CLIENT}, token=token, form=True)
                owner_token = tok.get("access_token")
            except Exception as e:  # noqa: BLE001
                out["token_error"] = str(e)
        if owner_token:
            for step, body in (("core_config", {}), ("analytics", {}),
                               ("integration", {"client_id": ONBOARD_CLIENT,
                                                "redirect_uri": ONBOARD_CLIENT})):
                try:
                    _http(base, "/api/onboarding/" + step, "POST", body, token=owner_token)
                except Exception:  # noqa: BLE001
                    pass
            out["onboarding_completed"] = True
        if ws_call:
            try:
                from proos import users as _users
                res = _users.create_user(ws_call, name="Installer", role="installer",
                                         username=INSTALLER_USERNAME,
                                         password=INSTALLER_DEFAULT_PW)
                _set_installer_must_change(res.get("user_id"), True)
                out["installer_created"] = res.get("username")
            except Exception as e:  # noqa: BLE001
                out["installer_error"] = str(e)
        _LOG.info("auto_onboard - provisioned owner+installer via %s", base)
        return out
    except urllib.error.HTTPError as exc:  # capture HTTP body — the real reason
        try:
            body = exc.read().decode()[:300]
        except Exception:
            body = ""
        _LOG.warning("auto_onboard HTTP %s via base=%s: %s", exc.code, base, body)
        return {"ok": False, "error": "HTTP %s: %s" % (exc.code, body), "base": base}
    except Exception as exc:  # never block boot
        _LOG.warning("auto_onboard failed via base=%s: %s", base, exc)
        return {"ok": False, "error": str(exc), "base": base}


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
