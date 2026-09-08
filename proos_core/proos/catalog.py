"""
ProOS Core - integration catalog sync.

Fetches the Protech-hosted master catalog (which HA integrations are Certified /
Native-allowed / Hidden and how they're supported) and caches it locally so each
site inherits Protech's central curation. Offline-safe: always falls back to the
last good cached copy; never raises to the caller.

Data flow:
  master catalog (Protech repo, versioned JSON)  ->  ProCore sync (this module)
  cached at /data/integration_catalog.json        ->  GET /catalog  ->  apps
The apps merge this against the box's own HA manifest/list to build the installer
and tech integration libraries.
"""
import json
import logging
import os
import urllib.request

_LOG = logging.getLogger("proos.catalog")

CACHE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "integration_catalog.json")
DEFAULT_URL = ("https://raw.githubusercontent.com/daveneill/proos-addons/"
               "main/proos-integration-catalog.json")


def _url() -> str:
    return (os.environ.get("PROOS_CATALOG_URL") or DEFAULT_URL).strip()


def _valid(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("integrations"), dict)


def load() -> dict:
    """Return the cached catalog (last good sync). Empty skeleton if none yet."""
    try:
        with open(CACHE, encoding="utf-8") as fh:
            d = json.load(fh)
        if _valid(d):
            return d
    except Exception:
        pass
    return {"version": 0, "integrations": {}, "defaults": {"uncatalogued_tier": "native"}, "stale": True}


def sync(timeout: int = 10) -> dict:
    """Fetch the master catalog and cache it. On any failure, keep the last good
    cache and return it (marked stale). Never raises."""
    url = _url()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ProOS-Core"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        d = json.loads(raw)
        if not _valid(d):
            raise ValueError("catalog missing 'integrations' map")
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        _LOG.info("catalog synced (v%s, %d integrations)", d.get("version"), len(d.get("integrations", {})))
        d = dict(d)
        d["synced"] = True
        return d
    except Exception as exc:
        _LOG.warning("catalog sync failed (%s); using cached copy", exc)
        cached = load()
        cached = dict(cached)
        cached["synced"] = False
        cached["sync_error"] = str(exc)
        return cached


# ── Published allowlist (per-site) ─────────────────────────────────────────
# Publish model, not block model: installers see NOTHING native by default.
# Tech/Owner publish specific native integrations to make them available to
# installers on this site. Certified integrations are always available; catalog
# 'hidden' are never. This list holds the native domains Tech has published here.
PUB = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "catalog_published.json")


def load_published() -> list:
    try:
        with open(PUB, encoding="utf-8") as fh:
            d = json.load(fh)
        return sorted({str(x) for x in d}) if isinstance(d, list) else []
    except Exception:
        return []


def save_published(domains) -> list:
    try:
        clean = sorted({str(x) for x in domains}) if isinstance(domains, list) else []
        os.makedirs(os.path.dirname(PUB), exist_ok=True)
        with open(PUB, "w", encoding="utf-8") as fh:
            json.dump(clean, fh)
        return clean
    except Exception as exc:
        _LOG.warning("published save failed: %s", exc)
        return load_published()
