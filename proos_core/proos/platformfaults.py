"""THE PLATFORM'S OWN FAULT LISTS, ON HEALTH (register 1014; audit 1013 step 1).

Dave, 26 Oct 2026: "the platform seems to be the brains and ProOS the smoke and mirrors"; the audit read on his box that
the platform knew the LIFX "Bec's Lamp" integration could not connect (config entry setup_retry · cannot_connect) and
that the box had no current backup (Supervisor resolution issue no_current_backup) — and Pro said "All Systems Normal".
ProOS read none of the platform's own fault lists. Agreed 16:06 ("start with those").

WHAT THIS IS: a reader, not a judge. Each problem the platform itself states becomes one Health card in the platform's
own words; ProOS adds no rule, no timer and no guess:
  * an integration (config entry) the platform says is not loaded — setup_retry, setup_error, migration_error,
    failed_unload, not_loaded — and that nobody disabled. Its words: the platform's state and reason.
  * every open Repairs issue (the issue registry) — the integration's own title, from the platform's own translations.
  * every Supervisor resolution issue, and an unhealthy or unsupported box — the Supervisor's own type and context.
A card stands exactly as long as the platform says the problem stands; the next read after the platform clears it,
the card is gone. An integration the platform is retrying right now (setup_in_progress) after it read failed keeps
its card, saying it is retrying — that is the platform's own last word, not ProOS's.

Audience installer, severity warning: none of these are on Dave's critical list (1013b), so none reaches a phone.
Readers are injected (the server wires the real ones, the bench fakes), so every card is driven, not described.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

DATA = os.environ.get("PROOS_DATA_DIR", "/data")
ACK_PATH = os.path.join(DATA, "platform_faults_ack.json")
# PLUMBING: how often ProOS asks. It claims nothing about the house; a fault stands as long as the platform says so.
READ_EVERY_S = 60

FAILED = ("setup_retry", "setup_error", "migration_error", "failed_unload", "not_loaded")
_STATE_WORDS = {
    "setup_retry": "can't connect — the platform keeps retrying",
    "setup_error": "failed to start",
    "migration_error": "failed to update its settings",
    "failed_unload": "could not be stopped cleanly",
    "not_loaded": "is not running",
}
# The Supervisor's own issue types, in the words its own UI uses (no claim beyond what the type names).
_SUP_WORDS = {
    "no_current_backup": "No Current Backup",
    "free_space": "Low Free Space",
    "corrupt_repository": "A Software Source Is Corrupt",
    "fatal_error": "A Fatal Error",
    "trust": "A Trust Problem",
    "pwned": "A Leaked Password Is in Use",
    "reboot_required": "Restart Required",
    "detached_addon_missing": "An Add-on's Source Is Missing",
    "multiple_data_disks": "More Than One Data Disk",
    "disabled_data_disk": "A Data Disk Is Disabled",
    "dns_server_failed": "DNS Server Failing",
    "ipv4_connection_problem": "Network Connection Problem",
}

_lock = threading.Lock()
_snap = {"at": None, "entries": [], "repairs": [], "resolution": {}, "error": None}
_failing_ids = set()     # entries read failed at the last read — so a retry in progress keeps saying so
_acks = None


def _iid(*parts) -> str:
    return "pf-" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def _load_acks() -> dict:
    global _acks
    if _acks is None:
        try:
            with open(ACK_PATH, encoding="utf-8") as fh:
                _acks = json.load(fh) or {}
        except Exception:                                        # noqa: BLE001
            _acks = {}
    return _acks


def _save_acks():
    try:
        os.makedirs(os.path.dirname(ACK_PATH), exist_ok=True)
        with open(ACK_PATH + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(_acks or {}, fh)
        os.replace(ACK_PATH + ".tmp", ACK_PATH)
    except Exception:                                            # noqa: BLE001
        pass


def refresh(read, now=None) -> dict:
    """One read of the platform. `read` has entries(), repairs() and resolution(); a reader that raises leaves that
    list as last read and says why in `error` — it never empties a list (an unanswered question is not an all-clear)."""
    now = now if now is not None else time.time()
    errs = []
    with _lock:
        try:
            ents = list(read.entries() or [])
            for e in ents:
                st = e.get("state")
                if st in FAILED and not e.get("disabled_by"):
                    _failing_ids.add(e.get("entry_id"))
                elif st == "loaded" or e.get("disabled_by"):
                    _failing_ids.discard(e.get("entry_id"))
            present = {e.get("entry_id") for e in ents}
            for gone in list(_failing_ids - present):
                _failing_ids.discard(gone)
            _snap["entries"] = ents
        except Exception as e:                                   # noqa: BLE001
            errs.append("integrations: %s" % e)
        try:
            _snap["repairs"] = list(read.repairs() or [])
        except Exception as e:                                   # noqa: BLE001
            errs.append("repairs: %s" % e)
        try:
            _snap["resolution"] = dict(read.resolution() or {})
        except Exception as e:                                   # noqa: BLE001
            errs.append("supervisor: %s" % e)
        _snap["at"] = now
        _snap["error"] = "; ".join(errs) or None
        return dict(_snap)


def snapshot() -> dict:
    with _lock:
        return dict(_snap, failing=sorted(i for i in _failing_ids if i))


def _card(kind, key, title, cause, since, actions, subject):
    iid = _iid(kind, key)
    c = {"kind": kind, "room": "ProOS", "slug": "site", "severity": "warning", "audience": "installer",
         "title": title, "cause": cause, "subject": subject, "id": iid, "since": since, "last_seen": since,
         "actions": actions + [{"kind": "pf_recheck", "label": "Check Again"}], "source": "platform"}
    ack = _load_acks().get(iid)
    if ack:
        c["acknowledged"] = ack
    return c


def cards(now=None) -> list:
    """The platform's open problems as Health cards (read time, from the last read)."""
    with _lock:
        snap = dict(_snap)
        failing = set(_failing_ids)
    at = snap.get("at")
    out = []
    for e in snap.get("entries") or []:
        st, eid = e.get("state"), e.get("entry_id")
        if e.get("disabled_by"):
            continue
        retrying = st == "setup_in_progress" and eid in failing
        if st not in FAILED and not retrying:
            continue
        name = e.get("title") or e.get("domain") or "An integration"
        words = "is retrying now" if retrying else _STATE_WORDS.get(st, st)
        reason = e.get("reason")
        out.append(_card("platform_integration", eid, "%s — %s" % (name, "Not Connecting" if st in ("setup_retry",) or retrying else "Not Running"),
                         "The platform says %s (%s) %s%s." % (name, e.get("domain"), words, (" — " + str(reason)) if reason else ""),
                         at, [{"kind": "pf_reload", "label": "Reload", "entry": eid}], "entry:%s" % eid))
    for i in snap.get("repairs") or []:
        if i.get("ignored") or i.get("dismissed_version"):
            continue
        title = i.get("title") or str(i.get("issue_id") or "").replace("_", " ").strip().capitalize() or "A platform repair"
        out.append(_card("platform_repair", "%s/%s" % (i.get("domain"), i.get("issue_id")), title,
                         "The platform's Repairs list says: %s (%s, %s)." % (i.get("description") or title, i.get("domain"), i.get("severity") or "warning"),
                         at, [], "repair:%s/%s" % (i.get("domain"), i.get("issue_id"))))
    res = snap.get("resolution") or {}
    for i in res.get("issues") or []:
        t = i.get("type") or "issue"
        words = "The box's own check reports %s (%s%s)." % (t.replace("_", " "), i.get("context") or "system",
                                                            (", " + str(i.get("reference"))) if i.get("reference") else "")
        acts = []
        _auto_on = bool(_auto["since"]) and ((now or time.time()) - _auto["since"]) < AUTO_GRACE_S
        if t == "no_current_backup" and _auto_on:
            words = ("ProOS is making the box's own full backup now — the daily backups already include ProOS and "
                     "every add-on; the box's check only accepts its own full type. This clears by itself in a few "
                     "minutes.")
        elif t == "no_current_backup":
            # REGISTER 1023 (Dave, 26 Oct 21:03: "What is this no backup system"): the Supervisor's own meaning, in
            # plain words — no FULL backup (one that includes the add-ons) in the last few days — and its fix.
            words = ("The box's own check reports there is no full backup from the last few days — a backup that "
                     "includes the add-ons. ProOS Core is an add-on: its rooms, activities and settings are only safe "
                     "in a full backup.")
            acts = [{"kind": "pf_full_backup", "label": "Include ProOS in Backups"}]
        _c = _card("platform_box", "%s/%s/%s" % (t, i.get("context"), i.get("reference") or ""),
                   _SUP_WORDS.get(t, t.replace("_", " ").title()), words, at, acts, "supervisor:%s" % t)
        if t == "no_current_backup" and _auto_on:
            _c["auto_fixing"] = True      # register 1024: not an alert while ProOS's own run of the box's fix is under way
        out.append(_c)
    for k in ("unhealthy", "unsupported"):
        for why in res.get(k) or []:
            out.append(_card("platform_box", "%s/%s" % (k, why), "The Box Reports It Is %s" % k.title(),
                             "The box's own check reports it is %s: %s." % (k, str(why).replace("_", " ")),
                             at, [], "supervisor:%s:%s" % (k, why)))
    return out


# ── THE BOX'S OWN FULL BACKUP, RUN AUTOMATICALLY (register 1024; Dave, 27 Oct 08:42: "2") ──────────────────────────
# Measured 26 Oct (1023a): the Supervisor's no_current_backup clears only for a backup of ITS "full" type; the
# platform's daily automatic backups are always "partial" to it, even holding every add-on — so the warning returns
# every few days. Dave chose option 2: ProOS applies the box's own fix (create_full_backup) itself, but ONLY when the
# daily backups are already healthy and complete — so the card never shows unless something is really wrong:
#   * the Supervisor reports no_current_backup AND offers create_full_backup (the box's own remedy, nothing else);
#   * the platform's automatic backups are ON and include every add-on and the database ("full" in its settings);
#   * the last automatic backup completed within AUTO_HEALTHY_S;
#   * ProOS has not tried in the last AUTO_EVERY_S (kept on disk — "done today" in memory once took two backups a day,
#     register 662).
# While it runs (up to AUTO_GRACE_S) the card says so and does not alert; if the box's check has not cleared by then
# the card is the plain critical card again. A backup touches only the box, never the house.
AUTO_PATH = os.path.join(DATA, "auto_full_backup.json")
AUTO_HEALTHY_S = 2 * 86400      # WINDOW: the daily schedule plus a day's slack
AUTO_EVERY_S = 20 * 3600        # WINDOW: at most one automatic full backup a day
AUTO_GRACE_S = 1800             # WINDOW: how long a running full backup may take before the card speaks plainly
_auto = {"since": None}


def _iso_ts(v):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return None


def auto_last_attempt():
    try:
        with open(AUTO_PATH, encoding="utf-8") as fh:
            return float((json.load(fh) or {}).get("at") or 0) or None
    except Exception:                                            # noqa: BLE001
        return None


def auto_record_attempt(now=None):
    try:
        os.makedirs(os.path.dirname(AUTO_PATH), exist_ok=True)
        with open(AUTO_PATH + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"at": now or time.time()}, fh)
        os.replace(AUTO_PATH + ".tmp", AUTO_PATH)
    except Exception:                                            # noqa: BLE001
        pass


def auto_full_backup_due(resolution, backup_cfg, last_attempt, now):
    """(due, why) — pure. `resolution` is the Supervisor's /resolution/info, `backup_cfg` platform_backup.summarise."""
    res = resolution or {}
    if not any(i.get("type") == "no_current_backup" for i in (res.get("issues") or [])):
        return False, "the box reports no backup problem"
    if not any(g.get("type") == "create_full_backup" for g in (res.get("suggestions") or [])):
        return False, "the box offers no full-backup fix"
    c = backup_cfg or {}
    if c.get("enabled") is not True:
        return False, "the daily automatic backups are not on"
    if c.get("full") is not True:
        return False, "the daily automatic backups leave out the add-ons"
    last = _iso_ts(c.get("last_completed"))
    if not last or now - last > AUTO_HEALTHY_S:
        return False, "no daily automatic backup has completed recently"
    if last_attempt and now - last_attempt < AUTO_EVERY_S:
        return False, "ProOS already made one today"
    return True, "the daily backups are healthy and complete; the box only wants its own full type"


def set_auto_fixing(since):
    _auto["since"] = since


def acknowledge(iid):
    """Acknowledged = quiet, still listed (the same meaning as every other card, register 926). Cleared for good only
    when the platform clears it. Returns {"id"} like sweep.acknowledge, or None for a card that is not open."""
    if not any(c["id"] == iid for c in cards()):
        return None
    _load_acks()[iid] = {"at": time.time()}
    _save_acks()
    return {"id": iid}


def merge_into(other_cards, mine):
    """One problem, one card (sweep.cards' own rule: "never carded twice"). The full sweep already cards the box's
    backups; when it does, the Supervisor's own no_current_backup is not a second card — its words are added to the
    sweep's Backups card as the platform agreeing, and the subject keeps the platform's own type name."""
    backup_card = next((c for c in other_cards if str(c.get("subject", "")).startswith("sweep:8:")), None)
    out = []
    for c in mine:
        if backup_card is not None and c.get("subject") == "supervisor:no_current_backup":
            backup_card["cause"] = "%s The box's own check agrees: no current backup (supervisor:no_current_backup)." % (
                backup_card.get("cause") or "")
            continue
        out.append(c)
    return out


def prune_acks():
    """An acknowledgement belongs to one open problem: once the platform clears it, the next time it appears it is new."""
    live = {c["id"] for c in cards()}
    a = _load_acks()
    gone = [k for k in a if k not in live]
    for k in gone:
        a.pop(k, None)
    if gone:
        _save_acks()
