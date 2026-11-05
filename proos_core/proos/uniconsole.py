"""ProOS Core — the UniFi console connection: one sign-in for the whole UniFi suite.

REGISTER 1215 (stage 1 of the UniFi suite; rulings in register 1214). Dave, 5 Nov 2026: UniFi is the flagship
integration, supported "with everything it has to offer"; one suite in Pro with one console connection; a dedicated
LOCAL USER on the console alongside Ubiquiti's official API keys ("1. Yes 2. Yes"). This module is that connection.

WHAT IT DOES
  signin()     the installer's local user, checked against the console's own sign-in (POST /api/auth/login — UniFi
               OS; the same door the published uiprotect library uses). Kept ONLY when the console accepted it.
  create_key() ProOS's own API key, created from that sign-in (POST /proxy/users/api/v2/user/self/keys — uiprotect's
               create_api_key; Chowmain's driver does the same). The same key opens the official Network and Protect
               interfaces. When ProOS holds no Protect key yet, this one is given to the Protect side too.
  set_access() the Access app's own token (Access has its own; it is pasted, never created here).
  apps()       which UniFi apps the console runs, as the console ANSWERED: each app's address under the console is
               asked and its HTTP answer is shown as it came back; versions come from the apps' official info calls.
  status()     everything above, masked: never a password, never a key — only whether each is set and its last 4.

WHAT IT NEVER DOES (kept rulings)
  * Register 695: the platform's saved logins are never read. The local user here is ProOS's own, typed into Pro.
  * Nothing secret is printed, logged or returned. The console's certificate is self-signed, so TLS is unverified
    (as unifi.py has always done for the same console).
  * The console's ADDRESS: when the platform's UniFi integration knows it, that is used and said (register 439);
    the installer's typed address is used only when no integration has one.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

SVC_LOGIN = "unifi_console"          # token = the local user's password; meta.user = the username
SVC_KEY = "unifi_console_key"        # token = ProOS's own UniFi OS API key
SVC_ACCESS = "unifi_access"          # token = the Access app's own API token
SVC_PROTECT = "unifi_protect"        # unifi.py's existing Protect key — filled only when empty

KEY_NAME = "ProOS"
SESSION_SECONDS = 600                # a sign-in is reused for ten minutes, then asked for again
TIMEOUT = 8

# The apps a UniFi console can run, the address each answers under, and (where Ubiquiti publishes one) the official
# call that states its version. Access's official interface lives on its own port with its own token.
APPS = (
    ("network", "UniFi Network", "/proxy/network/", "/proxy/network/integration/v1/info"),
    ("protect", "UniFi Protect", "/proxy/protect/", "/proxy/protect/integration/v1/meta/info"),
    ("access", "UniFi Access", "/proxy/access/", None),
    ("connect", "UniFi Connect", "/proxy/connect/", None),
    ("talk", "UniFi Talk", "/proxy/talk/", None),
)
ACCESS_PORT = 12445
ACCESS_DOORS = "/api/v1/developer/doors"   # the Access API reference's doors call (stage 5 re-reads it on the box)

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE

_SESSION = {"host": None, "cookie": None, "csrf": None, "at": 0.0}


def _http(method, url, body=None, headers=None, timeout=TIMEOUT):
    """(status, headers dict, bytes). Never raises for an HTTP answer; status 0 = no answer at all."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:                                        # noqa: BLE001
            raw = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, raw
    except Exception as e:                                       # noqa: BLE001
        return 0, {}, str(e)[:200].encode()


HTTP = _http                                                     # benches swap this


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "ignore")) if raw else None
    except Exception:                                            # noqa: BLE001
        return None


def host(creds, platform_host=None):
    """(address, where it came from). The platform's UniFi integration first (439), else the installer's."""
    if platform_host:
        return platform_host, "your UniFi integration"
    h = ((creds.meta(SVC_LOGIN) or {}).get("host") or "").strip()
    return (h, "typed in Pro") if h else ("", "")


def _base(h):
    return "https://%s" % h if not h.startswith("http") else h.rstrip("/")


def _why_refused(status, raw):
    """The console's refusal in plain words — from what it answered, nothing invented."""
    txt = (raw or b"").decode("utf-8", "ignore").lower()
    if status == 0:
        return "the console did not answer at that address (%s)" % txt[:80]
    if "mfa" in txt or "2fa" in txt or "two-factor" in txt or status == 499:
        return "the console asks this user for a second sign-in step — turn two-factor off for this local user"
    if status in (401, 403):
        return "the console refused that username and password"
    if status == 429:
        return "the console is refusing sign-ins for a while after too many attempts — wait a few minutes"
    return "the console answered HTTP %s" % status


def _login(h, user, pw, http=None):
    http = http or HTTP
    st, hd, raw = http("POST", _base(h) + "/api/auth/login",
                       {"username": user, "password": pw, "rememberMe": False})
    if st != 200:
        return False, _why_refused(st, raw), None
    return True, None, {"cookie": (hd.get("set-cookie") or "").split(";")[0], "csrf": hd.get("x-csrf-token")}


def signin(creds, address, username, password, platform_host=None, http=None):
    """Check the local user against the console; keep it only when the console accepted it."""
    username = (username or "").strip()
    if not username or not password:
        return {"ok": False, "why": "enter the local user's username and password"}
    h = platform_host or (address or "").strip()
    if not h:
        return {"ok": False, "why": "enter the console's address"}
    ok, why, sess = _login(h, username, password, http)
    if not ok:
        return {"ok": False, "why": why}
    creds.put(SVC_LOGIN, password, name="UniFi console local user",
              host=None if platform_host else h, port=443, kind="login", extra={"user": username})
    _SESSION.update({"host": h, "cookie": sess["cookie"], "csrf": sess["csrf"], "at": time.time()})
    return {"ok": True}


def _session(creds, h, http=None):
    """A live sign-in, reused for SESSION_SECONDS; (headers, why-not)."""
    if _SESSION["host"] == h and _SESSION["cookie"] and time.time() - _SESSION["at"] < SESSION_SECONDS:
        return {"cookie": _SESSION["cookie"], "x-csrf-token": _SESSION["csrf"] or ""}, None
    pw = creds.get(SVC_LOGIN)
    user = (creds.meta(SVC_LOGIN) or {}).get("user")
    if not (pw and user):
        return None, "no local user is saved yet"
    ok, why, sess = _login(h, user, pw, http)
    if not ok:
        return None, why
    _SESSION.update({"host": h, "cookie": sess["cookie"], "csrf": sess["csrf"], "at": time.time()})
    return {"cookie": sess["cookie"], "x-csrf-token": sess["csrf"] or ""}, None


def create_key(creds, platform_host=None, http=None):
    """ProOS's own API key, created from the saved sign-in. Returns masked status only."""
    http = http or HTTP
    h, _ = host(creds, platform_host)
    if not h:
        return {"ok": False, "why": "the console's address is not known yet"}
    hd, why = _session(creds, h, http)
    if hd is None:
        return {"ok": False, "why": why}
    st, _h, raw = http("POST", _base(h) + "/proxy/users/api/v2/user/self/keys", {"name": KEY_NAME}, hd)
    d = _json(raw) or {}
    key = ((d.get("data") or {}) if isinstance(d, dict) else {}).get("full_api_key")
    if st != 200 or not key:
        return {"ok": False, "why": "the console did not create a key (%s)" % _why_refused(st, raw)}
    out = creds.put(SVC_KEY, key, name="ProOS API key (created %s)" % time.strftime("%d %b %Y"), kind="api_key")
    gave_protect = False
    if not creds.get(SVC_PROTECT):
        creds.put(SVC_PROTECT, key, name="UniFi Protect key (from the console connection)", kind="api_key")
        gave_protect = True
    return {"ok": True, "key": out, "protect_too": gave_protect}


def set_access(creds, token):
    token = (token or "").strip()
    if not token:
        return {"ok": False, "why": "paste the Access API token"}
    return {"ok": True, "token": creds.put(SVC_ACCESS, token, name="UniFi Access API token", kind="api_key")}


def forget(creds):
    """Remove ProOS's own console sign-in and key. The Protect key and the Access token are left alone."""
    a = creds.delete(SVC_LOGIN)
    b = creds.delete(SVC_KEY)
    _SESSION.update({"host": None, "cookie": None, "csrf": None, "at": 0.0})
    return {"ok": True, "removed": bool(a or b)}


def _answer_words(st):
    if st == 0:
        return "no answer"
    return "answered HTTP %s" % st


def apps(creds, h, http=None):
    """Each app as the console answered for it. found = the console answered at that app's address with anything
    but 'not here' (404) or 'no app behind this' (502); version only from an official info call."""
    http = http or HTTP
    hd, why = _session(creds, h, http)
    key = creds.get(SVC_KEY) or creds.get(SVC_PROTECT)
    out = []
    for app, name, root, info in APPS:
        row = {"app": app, "name": name}
        if hd is not None:
            st, _hh, _raw = http("GET", _base(h) + root, None, hd)
            row["answer"] = _answer_words(st)
            row["found"] = st not in (0, 404, 502)
        else:
            row["answer"] = "not asked — " + (why or "not signed in")
            row["found"] = None
        if info and key:
            st, _hh, raw = http("GET", _base(h) + info, None, {"X-API-KEY": key})
            v = (_json(raw) or {}).get("applicationVersion") if st == 200 else None
            if v:
                row["version"] = str(v)
                row["found"] = True
        out.append(row)
    return out


def counts(creds, h, http=None):
    """What the official interfaces report, counted: switches/APs/gateways, cameras, doors. Absent = not read."""
    http = http or HTTP
    key = creds.get(SVC_KEY) or creds.get(SVC_PROTECT)
    out = {}
    if key:
        st, _hd, raw = http("GET", _base(h) + "/proxy/network/integration/v1/sites", None, {"X-API-KEY": key})
        sites = ((_json(raw) or {}).get("data") or []) if st == 200 else []
        if sites:
            sid = sites[0].get("id")
            st, _hd, raw = http("GET", _base(h) + "/proxy/network/integration/v1/sites/%s/devices?limit=200" % sid,
                                None, {"X-API-KEY": key})
            d = _json(raw) or {}
            if st == 200:
                out["network_devices"] = int(d.get("totalCount") or len(d.get("data") or []))
        st, _hd, raw = http("GET", _base(h) + "/proxy/protect/integration/v1/cameras", None, {"X-API-KEY": key})
        if st == 200 and isinstance(_json(raw), list):
            out["cameras"] = len(_json(raw))
    tok = creds.get(SVC_ACCESS)
    if tok:
        st, _hd, raw = http("GET", "https://%s:%d%s" % (h, ACCESS_PORT, ACCESS_DOORS), None,
                            {"Authorization": "Bearer %s" % tok})
        d = _json(raw) or {}
        if st == 200 and isinstance(d.get("data"), list):
            out["doors"] = len(d["data"])
    return out


# REGISTER 1217 — the apps' own logos (Dave, 5 Nov: "would be good if you could use the brand logos instead of icons").
# ProOS never draws Ubiquiti's marks: it shows the ones the console itself serves. Each app keeps its icon behind the
# console's sign-in (measured on Dave's console: /proxy/<app>/favicon.svg answers 401 without one), so they are asked
# for with ProOS's own session; the console's own mark (its front-page icon) needs none. Only real images are kept —
# an app that is not there answers with the console's web page, which is refused for not being an image.
#
# FIRST INSTALL (1.0.1314, measured on the box): only the console's mark came back. The apps do not keep a favicon at
# the guessed addresses; each app's own page DECLARES its icon (a <link rel="icon">), and that page is behind the
# sign-in. So each app's page is read with ProOS's session and the icon it declares is the one fetched — what a
# browser does. The guessed addresses stay as the fallback, and where each logo came from (or why none did) is kept
# in icons_from so the box can say it.
ICON_PATHS = ("favicon.svg", "apple-touch-icon.png", "favicon.png", "favicon.ico")
ICON_MAX = 96 * 1024
ICON_SECONDS = 24 * 3600
ICON_RETRY_SECONDS = 600                                         # no app logo at all: ask again in ten minutes
_ICONS = {}                                                      # host -> {"at": t, "ttl": s, "icons": {}, "from": {}}
_LINK_RE = None


def _declared_icons(raw):
    """The icon addresses an app's page declares, best first (an SVG icon, then apple-touch, then any icon)."""
    global _LINK_RE
    import re
    if _LINK_RE is None:
        _LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
    html = (raw or b"").decode("utf-8", "ignore")
    found = []
    for tag in _LINK_RE.findall(html):
        rel = re.search(r"""rel\s*=\s*["']([^"']+)["']""", tag, re.I)
        href = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, re.I)
        if not (rel and href):
            continue
        r = rel.group(1).lower()
        if "icon" not in r:
            continue
        hv = href.group(1).strip()
        rank = 0 if hv.lower().split("?")[0].endswith(".svg") else (1 if "apple-touch" in r else 2)
        found.append((rank, hv))
    return [hv for _r, hv in sorted(found, key=lambda x: x[0])]


def _resolve(h, root, href):
    """An app page's href as a URL on THIS console only (an address elsewhere is never fetched)."""
    if href.startswith("data:"):
        return None
    if href.startswith("//") or "://" in href:
        tail = href.split("://", 1)[-1].lstrip("/")
        if tail.split("/", 1)[0] != h:
            return None
        return _base(h) + "/" + (tail.split("/", 1)[1] if "/" in tail else "")
    if href.startswith("/"):
        return _base(h) + href
    return _base(h) + root + href.lstrip("./")


def _as_image(st, hd, raw):
    import base64
    ct = (hd.get("content-type") or "").split(";")[0].strip().lower()
    if st != 200 or not ct.startswith("image/") or not raw or len(raw) > ICON_MAX:
        return None
    return "data:%s;base64,%s" % (ct, base64.b64encode(raw).decode("ascii"))


def icons(creds, h, found=None, http=None):
    """{app: data URI} for the apps whose logo the console served, plus 'console' for the console's own mark.
    Cached a day per console; an app missing from the answer simply has no logo (Pro keeps its plain symbol)."""
    http = http or HTTP
    c = _ICONS.get(h)
    if c and time.time() - c["at"] < c.get("ttl", ICON_SECONDS):
        return dict(c["icons"])
    out, came = {}, {}
    for p in ("favicon.svg", "apple-touch-icon.png"):
        img = _as_image(*http("GET", _base(h) + "/" + p, None, {"Accept": "image/*"}))
        if img:
            out["console"], came["console"] = img, "/" + p
            break
    hd, why = _session(creds, h, http)
    for app, _name, root, _info in APPS:
        if found is not None and found.get(app) is False:
            came[app] = "not asked: the console has no %s" % app
            continue
        if hd is None:
            came[app] = "not asked: " + (why or "not signed in")
            continue
        st, phd, page = http("GET", _base(h) + root, None, dict(hd, Accept="text/html"))
        tried = []
        for href in _declared_icons(page if st == 200 else b"")[:4]:
            url = _resolve(h, root, href)
            if not url:
                continue
            r = http("GET", url, None, dict(hd, Accept="image/*"))
            img = _as_image(*r)
            path = url.split(h, 1)[-1].split("?")[0]
            if img and img == out.get("console"):
                # MEASURED (1.0.1315 on the box): every app's page declares /favicon.svg — the console's own "U".
                # That is the console's mark, not the app's logo; showing it on every tile would say nothing.
                tried.append("%s is the console's own mark" % path)
                continue
            if img:
                out[app], came[app] = img, "declared by its page: " + path
                break
            tried.append("%s %s" % (path, r[0]))
        if app in out:
            continue
        for p in ICON_PATHS:
            r = http("GET", _base(h) + root + p, None, dict(hd, Accept="image/*"))
            img = _as_image(*r)
            if img and img != out.get("console"):
                out[app], came[app] = img, root + p
                break
            tried.append("%s%s %s" % (root, p, r[0]))
        if app not in out:
            came[app] = "no logo: its page answered %s; tried %s" % (st, ", ".join(tried[:6]) or "nothing declared")
    apps_with = [a for a in out if a != "console"]
    _ICONS[h] = {"at": time.time(), "ttl": ICON_SECONDS if apps_with else ICON_RETRY_SECONDS,
                 "icons": out, "from": came}
    return out


def icons_from(h):
    """Where each logo came from, or why none did — plain words, nothing secret (paths and HTTP answers only)."""
    c = _ICONS.get(h) or {}
    return dict(c.get("from") or {})


def status(creds, platform_host=None, http=None, deep=True):
    """The console card, masked. deep=False skips asking the console (for quick page loads)."""
    h, frm = host(creds, platform_host)
    login = creds.status(SVC_LOGIN)
    out = {"host": h or None, "host_from": frm or None,
           "user_set": bool(login.get("set")), "key": creds.status(SVC_KEY),
           "access_token": creds.status(SVC_ACCESS), "protect_key_set": bool(creds.get(SVC_PROTECT))}
    if not (deep and h):
        cached = _ICONS.get(h) if h else None
        if cached:
            out["icons"] = dict(cached["icons"])                 # what the console already served — no asking
        return out
    hd, why = _session(creds, h, http) if login.get("set") else (None, "no local user is saved yet")
    out["signed_in"] = hd is not None
    if why:
        out["why"] = why
    out["apps"] = apps(creds, h, http)
    out["counts"] = counts(creds, h, http)
    out["icons"] = icons(creds, h, {a["app"]: a.get("found") for a in out["apps"]}, http)
    out["icons_from"] = icons_from(h)
    return out
