"""APNs push sender for ProOS Core -- zero Python dependencies.

Apple's push service is HTTP/2-only and authenticates with an ES256 (ECDSA
P-256) JSON Web Token. Python's stdlib has neither, so rather than pull in
`httpx`/`cryptography` and break Core's "nothing to pin" property, this module
shells out to two system tools already trivial to `apk add`:

  * openssl  -- signs the ES256 JWT (Apple token-based auth, one .p8 key)
  * curl     -- POSTs to APNs over HTTP/2

The transport is deliberately isolated to `_sign_es256()` and `_apns_post()`.
If you ever prefer the vetted-library route, swapping those two functions to
PyJWT + httpx is a contained change; nothing else in the file moves.

Statelessness: the device's APNs token arrives inside every notification HA
POSTs to Core, so this module stores nothing per-device. The only secret it
needs is the account-wide .p8 key, which the credential store holds.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time

APNS_PROD = "https://api.push.apple.com"
APNS_DEV = "https://api.sandbox.push.apple.com"

# ES256 JWTs are reused up to ~1h by APNs; refresh well inside that window.
_JWT_TTL = 2400  # 40 minutes
_jwt_cache: dict = {}
_jwt_lock = threading.Lock()


class PushError(Exception):
    """Raised when a notification could not be signed or delivered."""


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _der_to_raw(der: bytes) -> bytes:
    """Convert openssl's DER ECDSA signature (SEQUENCE{INTEGER r, INTEGER s})
    to the raw 64-byte r||s form a JWS ES256 signature requires."""
    if not der or der[0] != 0x30:
        raise PushError("malformed ECDSA signature")
    idx = 2  # skip SEQUENCE tag + (short-form) length; r/s ints are < 128 bytes
    if der[1] & 0x80:  # long-form length, unusual for P-256 but handle it
        idx = 2 + (der[1] & 0x7F)
    if der[idx] != 0x02:
        raise PushError("bad r integer")
    rlen = der[idx + 1]
    r = der[idx + 2: idx + 2 + rlen]
    idx = idx + 2 + rlen
    if der[idx] != 0x02:
        raise PushError("bad s integer")
    slen = der[idx + 1]
    s = der[idx + 2: idx + 2 + slen]
    r = r.lstrip(b"\x00").rjust(32, b"\x00")
    s = s.lstrip(b"\x00").rjust(32, b"\x00")
    return r + s


def _sign_es256(signing_input: bytes, p8_pem: str) -> bytes:
    """Sign `signing_input` with the .p8 key via openssl, return raw r||s."""
    fd, path = tempfile.mkstemp(prefix="apns_", suffix=".p8")
    try:
        os.write(fd, p8_pem.encode())
        os.close(fd)
        os.chmod(path, 0o600)
        p = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=signing_input, capture_output=True, timeout=10,
        )
        if p.returncode != 0:
            raise PushError("openssl sign failed: " + p.stderr.decode()[:200])
        return _der_to_raw(p.stdout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _jwt(key_id: str, team_id: str, p8_pem: str) -> str:
    now = int(time.time())
    with _jwt_lock:
        cached = _jwt_cache.get(key_id)
        if cached and now - cached[1] < _JWT_TTL:
            return cached[0]
    header = _b64url(json.dumps({"alg": "ES256", "kid": key_id}).encode())
    claims = _b64url(json.dumps({"iss": team_id, "iat": now}).encode())
    signing_input = header + b"." + claims
    sig = _b64url(_sign_es256(signing_input, p8_pem))
    token = (signing_input + b"." + sig).decode()
    with _jwt_lock:
        _jwt_cache[key_id] = (token, now)
    return token


def build_apns_payload(body: dict) -> bytes:
    """Translate HA's mobile_app notify POST into an APNs payload.

    HA sends {title, message, data:{...}}. iOS-specific options (sound, badge,
    interruption level, thread/tag, custom keys) ride under `data`, mirroring
    the companion app's documented notification schema.
    """
    data = body.get("data") or {}
    alert = {"body": body.get("message", "")}
    if body.get("title"):
        alert["title"] = body["title"]
    if data.get("subtitle"):
        alert["subtitle"] = data["subtitle"]
    aps: dict = {"alert": alert}

    sound = data.get("sound", "default")
    if sound and sound != "none":
        aps["sound"] = sound
    if "badge" in data:
        try:
            aps["badge"] = int(data["badge"])
        except (TypeError, ValueError):
            pass
    if data.get("push", {}).get("interruption-level"):
        aps["interruption-level"] = data["push"]["interruption-level"]
    if data.get("tag"):
        aps["thread-id"] = str(data["tag"])
    if data.get("mutable-content") or data.get("attachment") or data.get("image"):
        aps["mutable-content"] = 1
    if data.get("content-available"):
        aps["content-available"] = 1

    payload = {"aps": aps}
    # Pass through non-reserved custom keys so the app can act on them.
    for k, v in data.items():
        if k not in ("sound", "badge", "push", "tag", "subtitle",
                     "mutable-content", "content-available"):
            payload[k] = v
    return json.dumps(payload, separators=(",", ":")).encode()


def _apns_post(host: str, token: str, jwt: str, topic: str,
               payload: bytes) -> tuple:
    """POST one payload to APNs over HTTP/2. Returns (status_code, body_str)."""
    url = f"{host}/3/device/{token}"
    push_type = "background" if b'"content-available":1' in payload else "alert"
    try:
        p = subprocess.run(
            ["curl", "--http2", "-s", "-w", "\n%{http_code}", "-X", "POST",
             "-H", f"authorization: bearer {jwt}",
             "-H", f"apns-topic: {topic}",
             "-H", f"apns-push-type: {push_type}",
             "--data-binary", "@-", url],
            input=payload, capture_output=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise PushError("APNs request timed out")
    out = p.stdout.decode(errors="replace")
    status_str, _, rest = out.rpartition("\n")
    try:
        status = int(rest.strip())
    except ValueError:
        raise PushError("no status from APNs (curl: %s)" % p.stderr.decode()[:200])
    return status, status_str


def send(cred: dict, body: dict) -> dict:
    """Sign, build and deliver one notification.

    `cred` = {p8, key_id, team_id, topic, env}. Returns a result dict:
      {"ok": True, "status": 200}
      {"ok": False, "status": 4xx, "reason": "...", "unregistered": bool}
    `unregistered` flags a dead device token (APNs 410) so the caller can prune.
    """
    for k in ("p8", "key_id", "team_id", "topic"):
        if not cred.get(k):
            raise PushError(f"APNs not configured: missing {k}")
    token = body.get("push_token")
    if not token:
        raise PushError("no push_token in notification")
    host = APNS_DEV if cred.get("env") == "sandbox" else APNS_PROD
    jwt = _jwt(cred["key_id"], cred["team_id"], cred["p8"])
    payload = build_apns_payload(body)
    status, resp = _apns_post(host, token, jwt, cred["topic"], payload)
    if status == 200:
        return {"ok": True, "status": 200}
    reason = ""
    try:
        reason = (json.loads(resp) or {}).get("reason", "")
    except Exception:
        reason = resp[:120]
    return {"ok": False, "status": status, "reason": reason,
            "unregistered": status == 410 or reason == "Unregistered"}
