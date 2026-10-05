"""Backups on the platform's own schedule — Dave's ruling, 2 Oct 2026 (register 662).

"Let the platform do it." Read on his box the same day: the platform's own automatic
backup already ran daily (local + cloud, encrypted) AND ProOS's loop took a second full
2 GB backup every day — and a second one again after any Core restart past 03:30 (two on
28 Sep), because "done today" lived only in memory. ProOS keeps no schedule of its own.
Pro's Auto-backup card reads and sets the platform's backup config (WebSocket
backup/config/info and backup/config/update) — a mirror, never a copy.

Pure translation both ways, so it is benched without a platform.
"""


def summarise(info) -> dict:
    """backup/config/info → what Pro's card shows. A field the platform did not give is
    None — never a guess."""
    cfg = (info or {}).get("config") or {}
    sch = cfg.get("schedule") or {}
    ret = cfg.get("retention") or {}
    cb = cfg.get("create_backup") or {}
    rec = sch.get("recurrence")
    agents = list(cb.get("agent_ids") or [])
    return {
        "source": "platform",
        "enabled": (rec not in (None, "never")) if rec is not None else None,
        "recurrence": rec,
        "days": list(sch.get("days") or []),
        "time": sch.get("time"),                    # None = the platform picks the time itself
        "keep": ret.get("copies"),
        "keep_days": ret.get("days"),
        "full": bool(cb.get("include_all_addons")) and bool(cb.get("include_database")),
        "encrypted": bool(cb.get("password")),
        "locations": agents,
        "off_device": any(not str(a).startswith("hassio.") and a != "backup.local" for a in agents),
        "last_completed": cfg.get("last_completed_automatic_backup"),
        "last_attempted": cfg.get("last_attempted_automatic_backup"),
        "next": cfg.get("next_automatic_backup"),
    }


def update_fields(partial, current) -> dict:
    """Pro's partial edit ({enabled, time, keep, full}) → backup/config/update fields.
    Only what Pro changed is sent; the platform keeps everything else it owns
    (locations, encryption key, add-on list)."""
    out = {}
    cur = current or {}
    if "enabled" in partial or "time" in partial:
        enabled = bool(partial.get("enabled", cur.get("enabled")))
        sch = {"recurrence": "daily" if enabled else "never"}
        t = partial.get("time", cur.get("time"))
        if enabled and t:
            hh, mm = [int(x) for x in str(t).split(":")]
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError("time must be HH:MM")
            sch["time"] = "%02d:%02d" % (hh, mm)
        out["schedule"] = sch
    if "keep" in partial:
        n = int(partial["keep"])
        if not 1 <= n <= 365:
            raise ValueError("keep must be 1 to 365")
        out["retention"] = {"copies": n, "days": None}
    if "full" in partial:
        f = bool(partial["full"])
        out["create_backup"] = {"include_all_addons": f, "include_database": f}
    return out
