"""THE PLATFORM'S UNIFI CONNECTIONS COME FROM THE ONE UNIFI CARD — register 1255.

Dave, 8 Nov 2026, after the platform's UniFi Network connection had been in a setup error since 6 Nov 17:42 (the console's
users were changed to build the whole suite, and the account that connection had signed in with on 25 Oct could no
longer see the site): "it needs to be all done from one spot this cant be messed up by having multiple location";
16:28 "yes but this needs to still support homes that might have only one or none of these integrations".

THE ONE SPOT is Pro › UniFi's console card: the address and ProOS's own local user (uniconsole). From it, Core sets up the
PLATFORM'S OWN UniFi Network connection through the platform's own set-up steps (the same form an installer would fill),
and answers the platform's own sign-in request when it raises one. The platform still holds its connection — ProOS never
reads it back (695); the card is the only place a UniFi sign-in is ever typed.

WHAT IT NEVER DOES
  * Nothing on a home with no card sign-in, or whose console does not run UniFi Network (none / one / all — Dave 16:28).
  * Never removes a connection by itself. A broken one is REPLACED only when the installer presses Set Up Again on the
    card or on its Health card (that press is the consent: removing a connection deletes its devices with it).
  * Never guesses a site: a console with more than one site stops and says how many.
  * Nothing secret is returned or printed — only the step names, the platform's own reason words and the entry id.

`req(method, path, body)` is the platform's REST door and `ws(type, **fields)` its WebSocket (Core's own, injected).
UniFi Protect stays on its own connection for now — it moves to this card as the next step (Dave 15:58 agreed order).
"""
from __future__ import annotations

from proos import uniconsole as _uc

DOMAIN = "unifi"
FLOW = "/api/config/config_entries/flow"
LOADED = "loaded"
_FIELDS = ("host", "username", "password", "port", "verify_ssl", "api_key")
# REGISTER 1256: the same one spot for UniFi Protect (Dave 17:33 — every certified integration the one way). The app as
# the card reads it on the console → the platform's own integration for it.
APPS = {"network": {"domain": "unifi", "name": "UniFi Network"},
        "protect": {"domain": "unifiprotect", "name": "UniFi Protect"}}


def _entries(ws, domain=DOMAIN):
    try:
        got = ws("config_entries/get", domain=domain) or []
    except Exception:                                            # noqa: BLE001
        return None
    return [e for e in got if isinstance(e, dict) and e.get("domain", domain) == domain]


def _sign_in(creds, platform_host=None):
    """(fields for the platform's form, why-not). Built from the card only. The API key is ProOS's own (created on the
    card); only a form that asks for one is given it."""
    h, _frm = _uc.host(creds, platform_host)
    pw = creds.get(_uc.SVC_LOGIN)
    user = (creds.meta(_uc.SVC_LOGIN) or {}).get("user")
    if not (h and pw and user):
        return None, "the UniFi card has no sign-in yet"
    out = {"host": h.replace("https://", "").replace("http://", "").rstrip("/"), "username": user, "password": pw,
           "port": 443, "verify_ssl": False}
    key = creds.get(_uc.SVC_KEY) or creds.get(_uc.SVC_PROTECT)
    if key:
        out["api_key"] = key
    return out, None


def _schema_names(step):
    return [str(f.get("name")) for f in (step.get("data_schema") or []) if isinstance(f, dict) and f.get("name")]


def _answer(req, step, fields):
    """Fill the platform's form with exactly the fields it asks for. Returns the platform's next step."""
    names = _schema_names(step)
    if not names:                                                # a flow from the in-progress list: read its form first
        try:
            names = _schema_names(req("GET", "%s/%s" % (FLOW, step.get("flow_id"))) or {})
        except Exception:                                        # noqa: BLE001
            names = []
    names = names or ["host", "username", "password", "port", "verify_ssl"]
    body = {k: fields[k] for k in names if k in fields}
    return req("POST", "%s/%s" % (FLOW, step.get("flow_id")), body) or {}


def _drop(req, flow_id):
    try:
        req("DELETE", "%s/%s" % (FLOW, flow_id))
    except Exception:                                            # noqa: BLE001
        pass


def _outcome(r):
    """The platform's answer in words — never its data."""
    t = r.get("type")
    if t == "create_entry":
        return {"ok": True, "entry_id": ((r.get("result") or {}).get("entry_id") if isinstance(r.get("result"), dict)
                                         else r.get("result")), "words": "set up from the UniFi card"}
    if t == "abort":
        reason = str(r.get("reason") or "")
        ok = reason in ("reauth_successful", "configuration_updated")
        return {"ok": ok, "reason": reason, "words": ("signed in again from the UniFi card" if ok
                                                      else "the platform stopped: %s" % reason.replace("_", " "))}
    errs = r.get("errors") or {}
    if errs:
        return {"ok": False, "reason": ",".join(str(v) for v in errs.values()),
                "words": "the platform refused the card's sign-in: %s" % ", ".join(str(v).replace("_", " ") for v in errs.values())}
    return {"ok": False, "reason": "step:%s" % r.get("step_id"), "words": "the platform asked for %s" % (r.get("step_id") or "more")}


def _discovered(ws, domain):
    """REGISTER 906, kept: when the platform has already FOUND the console, it holds a set-up of its own for it, and a
    second one is refused (already_in_progress). That found set-up is the one carried on."""
    try:
        flows = ws("config_entries/flow/progress") or []
    except Exception:                                            # noqa: BLE001
        return None
    for f in flows if isinstance(flows, list) else []:
        src = (f.get("context") or {}).get("source")
        if f.get("handler") == domain and src not in ("reauth", "reconfigure", "user"):
            return f
    return None


def _create(req, fields, domain=DOMAIN, ws=None):
    found = _discovered(ws, domain) if ws is not None else None
    if found:
        step = req("GET", "%s/%s" % (FLOW, found.get("flow_id"))) or {}
        step.setdefault("flow_id", found.get("flow_id"))
        step.setdefault("type", "form")
    else:
        step = req("POST", FLOW, {"handler": domain, "show_advanced_options": False}) or {}
    if step.get("type") == "abort":
        return _outcome(step)
    if step.get("type") != "form" or not step.get("flow_id"):
        return {"ok": False, "words": "the platform did not offer its UniFi set-up form"}
    r = _answer(req, step, fields)
    for _more in range(2):            # a found set-up may confirm first, then ask for the sign-in on its next form
        if r.get("type") == "form" and r.get("step_id") != "site" and not r.get("errors") and _schema_names(r):
            r = _answer(req, dict(r, flow_id=r.get("flow_id") or step.get("flow_id")), fields)
        else:
            break
    if r.get("type") == "form" and r.get("step_id") == "site":
        n = len(((r.get("data_schema") or [{}])[0] or {}).get("options") or [])
        _drop(req, r.get("flow_id") or step.get("flow_id"))
        return {"ok": False, "reason": "several_sites",
                "words": "the console has %s sites — choosing one is not built yet, so nothing was set up" % (n or "several")}
    out = _outcome(r)
    if not out.get("ok") and r.get("type") == "form":
        _drop(req, r.get("flow_id") or step.get("flow_id"))
    return out


def on_console(apps, app="network") -> bool | None:
    """True / False from the card's own reading of the console's apps; None when it could not tell."""
    for a in apps or []:
        if a.get("app") == app:
            return a.get("found")
    return None


def network_on_console(apps) -> bool | None:
    return on_console(apps, "network")


def state(creds, ws, apps=None, app="network") -> dict:
    """What the card shows: is the platform's connection for this app there, and running. Read only."""
    signed = bool(creds.get(_uc.SVC_LOGIN))
    name = APPS[app]["name"]
    on = on_console(apps, app) if apps is not None else None
    ents = _entries(ws, APPS[app]["domain"])
    out = {"signed_in": signed, "on_console": on}
    if ents is None:
        out.update(words="the platform's connection list could not be read", ok=None)
        return out
    live = [e for e in ents if not e.get("disabled_by")]
    if not live:
        out.update(entry=None, ok=None if on is False else False,
                   words=("this console does not run %s" % name if on is False
                          else "the platform has no %s connection yet" % name))
        return out
    e = live[0]
    out.update(entry=e.get("entry_id"), entry_state=e.get("state"), ok=e.get("state") == LOADED,
               words=("connected" if e.get("state") == LOADED else
                      "the platform says it %s" % {"setup_error": "failed to start", "setup_retry": "cannot connect — retrying",
                                                   "not_loaded": "is not running"}.get(e.get("state"), e.get("state"))))
    if len(live) > 1:
        out["more"] = len(live) - 1
    return out


def ensure(creds, req, ws, apps, platform_host=None, replace=False, app="network") -> dict:
    """Bring the platform's connection for this UniFi app in line with the card. Converges: a connection that is
    running gets NO writes. `replace` (only from the installer's own press) removes a broken connection and sets it up
    again from the card."""
    domain, name = APPS[app]["domain"], APPS[app]["name"]
    if not creds.get(_uc.SVC_LOGIN):
        return {"done": False, "skipped": "the UniFi card has no sign-in — nothing to do"}
    _uc.adopt_host(creds, platform_host)                        # the card keeps its own address from now on
    on = on_console(apps, app)
    if on is False:
        return {"done": True, "skipped": "this console does not run %s — nothing to set up" % name}
    if on is None:
        return {"done": False, "skipped": "the card could not tell whether the console runs %s" % name}
    fields, why = _sign_in(creds, platform_host)
    if fields is None:
        return {"done": False, "skipped": why}
    ents = _entries(ws, domain)
    if ents is None:
        return {"done": False, "error": "the platform's connection list could not be read"}
    live = [e for e in ents if not e.get("disabled_by")]
    # the platform's own sign-in request, raised for this connection, is answered from the card
    try:
        flows = ws("config_entries/flow/progress") or []
    except Exception:                                            # noqa: BLE001
        flows = []
    for f in flows if isinstance(flows, list) else []:
        if f.get("handler") == domain and (f.get("context") or {}).get("source") == "reauth":
            r = _answer(req, f, fields)
            return dict(_outcome(r), done=bool(_outcome(r).get("ok")), did="answered the platform's sign-in request")
    if not live:
        out = _create(req, fields, domain, ws)
        return dict(out, done=bool(out.get("ok")), did="set up the platform's %s connection" % name + "")
    if any(e.get("state") == LOADED for e in live):
        return {"done": True, "did": "nothing — the platform's %s connection" % name + " is running"}
    if not replace:
        e = live[0]
        return {"done": False, "needs_replace": e.get("entry_id"), "entry_state": e.get("state"),
                "words": "the platform's %s connection" % name + " is not running — Set Up Again on the UniFi card "
                         "replaces it from the card's sign-in (its devices are removed and come back with it)"}
    removed = []
    for e in live:
        req("DELETE", "/api/config/config_entries/entry/%s" % e.get("entry_id"))
        removed.append(e.get("entry_id"))
    out = _create(req, fields, domain, ws)
    return dict(out, done=bool(out.get("ok")), removed=removed, did="replaced the platform's %s connection" % name + "")
