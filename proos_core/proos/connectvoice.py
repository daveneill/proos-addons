"""ProOS Core — the ProOS voice, from ProOS Connect (register 969u, 21 Oct 2026).

Dave, 21 Oct: "lock in one voice as our ProOS voice not jumping around" — "No Nabu Casa". The voice is Ryan
(Azure en-GB RyanNeural, "chat" style — the voice the home unit spoke through HA Cloud as "RyanNeural||chat"),
served from ProOS Connect in Sydney.

THE PLATFORM'S OWN MACHINERY, NOT A REBUILD (the 18 Sep law):
  * the platform speaks through speech engines; an outside voice joins it through the platform's own Wyoming
    protocol — the way Piper and Whisper do. Core answers that protocol here (describe → info; synthesize →
    audio-start / audio-chunk / audio-stop) and fetches each sentence from ProOS Connect.
  * the platform adds the engine through its own Wyoming setup flow (host and port) — no hand-made entries.
  * KEEPING THE SPOKEN REPLIES FOR OFFLINE IS THE PLATFORM'S: its speech cache keeps every sentence it has made
    and answers a repeat from the cache without asking the engine. Core keeps no copy of its own.
  What only ProOS knows stays here: the box's own Connect identity, and which engine is the ProOS voice.

THE BOX'S CONNECT IDENTITY: a box id and a secret made on the box, kept only in Core's own store (/data, never
shown, never logged). The box asks ProOS Connect to join; it is PENDING until Dave approves it on Connect's admin
page — the tap is the consent. No secret ever passes through a person.

No third-party Python: sockets, threads, urllib.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "connect.json")

# THE PRODUCT'S CONNECT SERVER. Stage 1 runs on Azure's own name for the Sydney server; it moves to the ProOS
# domain when Dave names it (memory: proos-connect-website).
CONNECT_URL = os.environ.get("PROOS_CONNECT_URL", "https://proos-connect-syd.australiaeast.cloudapp.azure.com")

PORT = 10210                       # inside the box's own add-on network only — no host port is published
PROGRAM = "ProOS Voice"            # the platform titles the engine after this
VOICE_NAME = "en_GB-ryan"
VOICE_LANG = "en_GB"
WIDTH, CHANNELS = 2, 1             # 16-bit mono PCM, the rate as Connect says (X-ProOS-Rate)
DEFAULT_RATE = 24000

_lock = threading.Lock()
_engine = {"entity": None, "checked": 0.0}


# ── identity ─────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(st: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def identity() -> dict:
    """The box's Connect id and secret, made once on the box."""
    with _lock:
        st = _load()
        if not (st.get("box_id") and st.get("secret")):
            st["box_id"] = "px-" + secrets.token_hex(8)
            st["secret"] = secrets.token_urlsafe(36)
            _save(st)
        return st


def _headers(st: dict) -> dict:
    return {"X-ProOS-Box": st["box_id"], "Authorization": "Bearer " + st["secret"],
            "Content-Type": "application/json", "User-Agent": "proos-core"}


def _call(method: str, path: str, body=None, st=None, timeout=10):
    st = st or identity()
    req = urllib.request.Request(CONNECT_URL + path, method=method, headers=_headers(st),
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


HW_PREFIX = "proos-hw-v1:"


def hw_fingerprint(hwid: str | None = None) -> str:
    """REGISTER 983: a one-way fingerprint of THIS physical box — sha256 of its factory id (the cpu serial on a Pi, the
    lowest built-in network port's factory MAC on a PC: provision._hardware_id, the same id the servicing login comes
    from). It survives a reflash, so ProOS Connect knows the box when it comes back. Never the id itself; "" when the
    box has no hardware id (a random stand-in identifies nothing)."""
    import hashlib
    if hwid is None:
        try:
            from proos import provision as _prov
            hwid = _prov._hardware_id()
        except Exception:  # noqa: BLE001
            return ""
    hwid = str(hwid or "")
    if not hwid.startswith(("mac:", "cpu:")):
        return ""
    return hashlib.sha256((HW_PREFIX + hwid).encode("utf-8")).hexdigest()


def enroll(home_id: str, name: str, core: str) -> dict:
    """Ask to join (or say we are still here). Returns Connect's answer: pending / approved / revoked."""
    st = identity()
    try:
        body = {"box_id": st["box_id"], "secret": st["secret"], "home_id": home_id, "name": name, "core": core}
        try:                                         # register 969v: the box's public key for its remote-access line
            from proos import connecttunnel as _ct
            body["ssh_key"] = _ct.public_key()
        except Exception:  # noqa: BLE001
            pass
        hw = hw_fingerprint()
        if hw:                                       # register 983: the same box, known again after a reflash
            body["hw"] = hw
        got = _call("POST", "/v1/enroll", body, st)
    except urllib.error.HTTPError as e:
        got = {"status": "refused", "why": "Connect answered %s" % e.code}
    except Exception as e:  # noqa: BLE001
        got = {"status": "unreachable", "why": str(e)[:120]}
    return got


# ── the directory (register 970qq) ───────────────────────────────────────────
# Away from home the app has no address after a sign-out; ProOS Connect answers "which home is this username?" from
# one-way fingerprints each box lists — never the names, never a password (the 7 Sep design). The box still checks
# the password itself. Sent when the list changes, and again every six hours so Connect never holds a stale one long.
FP_PREFIX = "proos-directory-v1:"      # the same on Connect (relay.FP_PREFIX) and in the app
DIR_RESEND_S = 6 * 3600                # plumbing: a list that has not changed is sent again after this
_dir_sent = {"key": None, "at": 0.0}


def fingerprint(username: str) -> str:
    import hashlib
    return hashlib.sha256((FP_PREFIX + str(username or "").strip().lower()).encode("utf-8")).hexdigest()


def usernames(ws_call) -> list:
    """The box's people who sign in with a username — the platform's own user list (active, not system accounts)."""
    try:
        got = ws_call("config/auth/list") or []      # Core's adapter: (msg_type, **fields) → the result
    except Exception:  # noqa: BLE001
        return []
    users = got if isinstance(got, list) else (got.get("result") or [])
    return sorted({str(u.get("username")).strip().lower() for u in users
                   if isinstance(u, dict) and u.get("username") and u.get("is_active", True)
                   and not u.get("system_generated")})


def publish_directory(names: list, home_name: str = "", call=None, now=None) -> dict:
    """List this home's usernames on Connect as fingerprints. Only when the list changed or it is time again."""
    now = now or time.time()
    fps = sorted({fingerprint(n) for n in names or [] if str(n or "").strip()})
    key = json.dumps([fps, home_name])
    if key == _dir_sent["key"] and now - _dir_sent["at"] < DIR_RESEND_S:
        return {"sent": False, "listed": len(fps)}
    try:
        got = (call or _call)("POST", "/v1/directory", {"fingerprints": fps, "name": home_name})
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "error": str(e)[:120]}
    _dir_sent.update(key=key, at=now)
    return dict(got or {}, sent=True, listed=len(fps))


def status() -> dict:
    """Connect's own reading of this box: approved or not, and whether the voice is set up there."""
    st = identity()
    try:
        got = _call("GET", "/v1/status", None, st)
    except Exception as e:  # noqa: BLE001
        return {"status": "unreachable", "why": str(e)[:120], "box_id": st["box_id"], "url": CONNECT_URL}
    got.update({"box_id": st["box_id"], "url": CONNECT_URL})
    return got


def speak_stream(text: str, timeout=15):
    """Ryan saying `text`: yields (rate, pcm_bytes) as Connect streams it. Raises with Connect's own reason."""
    st = identity()
    req = urllib.request.Request(CONNECT_URL + "/v1/tts", method="POST", headers=_headers(st),
                                 data=json.dumps({"text": text}).encode())
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or ""
        except Exception:  # noqa: BLE001
            why = ""
        raise ConnectionError("ProOS Connect refused the voice (%s%s)" % (e.code, (": " + why) if why else ""))
    except Exception as e:  # noqa: BLE001
        raise ConnectionError("ProOS Connect could not be reached: %s" % str(e)[:120])
    with r:
        rate = int(r.headers.get("X-ProOS-Rate") or DEFAULT_RATE)
        while True:
            chunk = r.read(4800)
            if not chunk:
                break
            yield rate, chunk


# ── the platform's Wyoming protocol ──────────────────────────────────────────
def _attr():
    return {"name": "ProOS", "url": "https://protechsystems.com.au"}


def info_event() -> dict:
    return {"type": "info", "data": {
        "asr": [], "wake": [], "handle": [], "intent": [], "mic": [], "snd": [], "satellite": None,
        "tts": [{"name": PROGRAM, "description": "Ryan, the ProOS voice", "attribution": _attr(),
                 "installed": True, "version": "1", "supports_synthesize_streaming": False,
                 "voices": [{"name": VOICE_NAME, "description": "Ryan", "attribution": _attr(),
                             "installed": True, "version": None, "languages": [VOICE_LANG],
                             "speakers": None}]}]}}


def read_event(rf):
    """One Wyoming event from a binary reader: (type, data, payload) or None at end."""
    line = rf.readline()
    if not line:
        return None
    hdr = json.loads(line)
    data = dict(hdr.get("data") or {})
    if hdr.get("data_length"):
        data.update(json.loads(rf.read(int(hdr["data_length"]))))
    payload = rf.read(int(hdr["payload_length"])) if hdr.get("payload_length") else b""
    return hdr.get("type"), data, payload


def write_event(wf, etype: str, data=None, payload: bytes = b""):
    hdr = {"type": etype, "version": "1.5.0"}
    if data is not None:
        hdr["data"] = data
    if payload:
        hdr["payload_length"] = len(payload)
    wf.write((json.dumps(hdr) + "\n").encode())
    if payload:
        wf.write(payload)
    wf.flush()


class _Handler(socketserver.StreamRequestHandler):
    speak = staticmethod(speak_stream)        # swapped in benches

    def handle(self):
        while True:
            try:
                ev = read_event(self.rfile)
            except Exception:  # noqa: BLE001
                return
            if ev is None:
                return
            etype, data, _payload = ev
            if etype == "describe":
                info = info_event()
                write_event(self.wfile, info["type"], info["data"])
            elif etype == "synthesize":
                self._synth(str(data.get("text") or "").strip())
            # anything else is not ours to answer; the platform does not wait on it

    def _synth(self, text):
        if not text:
            write_event(self.wfile, "error", {"text": "nothing to say", "code": "empty"})
            return
        # 970d (Dave, 21 Oct 18:37: "a loud ticking sound over the top of the voice"). Measured: the audio the app
        # plays had a click every 0.1 s, exactly where Core cut Connect's stream into 4800-byte audio-chunks, while
        # Connect's own copy of the same sentence (read on the server) had none — the seams were added after Core,
        # by the platform joining one short chunk to the next. The platform builds the whole sentence before
        # anything plays (it hands the app a finished file), so the sentence now goes over as ONE chunk: no seams,
        # and nothing heard any later.
        pcm, rate = bytearray(), None
        try:
            for r, chunk in self.speak(text):
                rate = rate or r
                pcm += chunk
        except ConnectionError as e:
            if not pcm:
                write_event(self.wfile, "error", {"text": str(e), "code": "connect"})
                return
        if not pcm:
            write_event(self.wfile, "error", {"text": "ProOS Connect sent no audio", "code": "empty"})
            return
        if len(pcm) % WIDTH:
            pcm = pcm[:-(len(pcm) % WIDTH)]       # never half a sample
        fmt = {"rate": rate, "width": WIDTH, "channels": CHANNELS}
        write_event(self.wfile, "audio-start", fmt)
        write_event(self.wfile, "audio-chunk", fmt, bytes(pcm))
        write_event(self.wfile, "audio-stop", {})


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = PORT, host: str = "0.0.0.0") -> _Server:
    srv = _Server((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, name="proos-voice", daemon=True).start()
    return srv


# ── the platform adds the engine through its own setup flow ──────────────────
def _own_host(sup) -> str | None:
    """This add-on's hostname on the box's own network, read from the Supervisor."""
    try:
        return (sup("GET", "/addons/self/info") or {}).get("hostname") or None
    except Exception:  # noqa: BLE001
        return None


def ensure_engine(sup, ws, req) -> dict:
    """The ProOS Voice engine added to the platform (its Wyoming flow), and its entity read back."""
    try:
        entries = ws("config_entries/get", domain="wyoming") or []
    except Exception:  # noqa: BLE001
        entries = []
    mine = next((e for e in entries if e.get("title") == PROGRAM), None)
    out = {}
    if mine is None:
        host = _own_host(sup)
        if not host:
            return {"error": "this add-on's own address could not be read"}
        r = req("POST", "/api/config/config_entries/flow", {"handler": "wyoming", "show_advanced_options": False}) or {}
        if r.get("type") == "form" and r.get("flow_id"):
            r = req("POST", "/api/config/config_entries/flow/%s" % r["flow_id"], {"host": host, "port": PORT}) or {}
        out["created"] = r.get("type")
        if r.get("type") != "create_entry":
            out["error"] = "the platform's Wyoming setup said %s" % (r.get("errors") or r.get("reason") or r.get("type"))
            return out
        mine = r.get("result") or {}
    eid = mine.get("entry_id")
    try:
        ents = ws("config/entity_registry/list") or []
    except Exception:  # noqa: BLE001
        ents = []
    ent = next((e.get("entity_id") for e in ents
                if e.get("config_entry_id") == eid and str(e.get("entity_id", "")).startswith("tts.")), None)
    with _lock:
        _engine.update({"entity": ent, "checked": time.time()})
    out["engine"] = ent
    return out


def ensure_pipeline_voice(ws, engine: str, pipeline_name: str = "ProOS") -> dict:
    """The ProOS pipeline speaks with the ProOS voice (the platform's own pipeline update)."""
    lst = ws("assist_pipeline/pipeline/list") or {}
    mine = next((p for p in (lst.get("pipelines") or []) if p.get("name") == pipeline_name), None)
    if not mine:
        return {"pipeline": None}
    want = {"tts_engine": engine, "tts_language": VOICE_LANG, "tts_voice": VOICE_NAME}
    if all(mine.get(k) == v for k, v in want.items()):
        return {"current": mine.get("id")}
    body = {k: v for k, v in mine.items() if k != "id"}
    body.update(want)
    ws("assist_pipeline/pipeline/update", pipeline_id=mine.get("id"), **body)
    return {"updated": mine.get("id")}


def engine() -> str | None:
    """The ProOS voice engine's entity, once the platform has it and Connect has approved this box."""
    with _lock:
        return _engine.get("entity") if _engine.get("ready") else None


def converge(sup, ws, req, home_id: str, name: str, core: str) -> dict:
    """One pass: join Connect, and once approved with the voice set up there, give the platform the engine and
    the ProOS pipeline its voice. Every step reports; a right box gets no writes."""
    out = {"enroll": enroll(home_id, name, core)}
    stt = status()
    out["status"] = {k: stt.get(k) for k in ("status", "voice_ready", "why")}
    out["tunnel"] = stt.get("tunnel")                 # register 969v: Connect's answer for the remote-access line
    ready = stt.get("status") == "approved" and bool(stt.get("voice_ready"))
    if ready:
        out["engine"] = ensure_engine(sup, ws, req)
        eng = out["engine"].get("engine")
        if eng:
            out["pipeline"] = ensure_pipeline_voice(ws, eng)
    with _lock:
        _engine["ready"] = bool(ready and _engine.get("entity"))
        _engine["last"] = {"at": int(time.time()), **out["status"]}
    return out


def reading() -> dict:
    """What /voice/status says about the ProOS voice — never the secret."""
    st = _load()
    with _lock:
        out = {"connect_url": CONNECT_URL, "box_id": st.get("box_id"), "engine": _engine.get("entity"),
               "ready": bool(_engine.get("ready")), "last": _engine.get("last")}
    try:                                              # register 969v: the remote-access line, as Core sees it
        from proos import connecttunnel as _ct
        out["remote"] = _ct.reading()
    except Exception:  # noqa: BLE001
        pass
    return out
