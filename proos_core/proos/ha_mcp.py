"""ProOS Core — HA MCP client (the mirror's toolset, spoken natively).

DAVE'S RULING (16 Aug 2026): "Our Assist should be using the HA MCP,
same as you — again, it should be a mirror." The box already runs the
Home Assistant MCP Server add-on; this client speaks the MCP protocol
to it (JSON-RPC over streamable HTTP, stdlib only) so Assist's model —
Claude or ChatGPT identically — receives the SAME tools a Developer
session gets, not a hand-built imitation of them.

Tier law rides the SERVER'S OWN TESTIMONY, not name rules: MCP tools
carry a readOnlyHint annotation; a tool the server marks read-only is a
READ, anything else — including a tool with NO annotation — is treated
as an ACT (fail-closed: an unlabelled power is not assumed harmless).
Who gets what is decided in assist.py from that annotation alone.

Discovery is a PROBE, not a guess: candidate endpoints are read from
the Supervisor's add-on records (a started add-on whose slug mentions
mcp, its published port and secret path), and only a server that
actually ANSWERS the MCP handshake is remembered (mcp_conn.json —
per-site, wiped on reset). The handshake is the identity; the name
only narrows where to knock. No answer anywhere → status says so.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

_CONN_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"),
                          "mcp_conn.json")
_TOOLS_TTL = 600         # tools relisted on this TTL; the list is the server's
_PROTOCOL = "2025-03-26"


class McpUnavailable(RuntimeError):
    """No MCP server answered the handshake."""


def _parse_body(ctype, body):
    """A streamable-HTTP server may answer plain JSON or an SSE stream —
    read whichever it actually sent."""
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in (ctype or ""):
        msgs = []
        for chunk in text.split("\n\n"):
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
        return msgs
    try:
        return [json.loads(text)]
    except ValueError:
        return []


class HaMcp:
    def __init__(self, base_url=None, http=None):
        self.base_url = base_url          # discovered or injected
        self._http = http or self._http_post
        self._session = None
        self._mid = 0
        self._lock = threading.Lock()
        self._tools = None
        self._tools_ts = 0.0
        self.last_error = None
        self.census = {}

    # ── transport ───────────────────────────────────────────────────────────
    def _http_post(self, url, payload, headers):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as r:
            return (dict(r.headers), r.headers.get("Content-Type", ""),
                    r.read())

    def _rpc(self, method, params=None, notify=False):
        self._mid += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._mid
        headers = {}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        rh, ctype, body = self._http(self.base_url, msg, headers)
        sid = {k.lower(): v for k, v in (rh or {}).items()}.get("mcp-session-id")
        if sid:
            self._session = sid
        if notify:
            return None
        for m in _parse_body(ctype, body):
            if m.get("id") == self._mid:
                if m.get("error"):
                    raise RuntimeError("MCP %s error: %s" % (
                        method, (m["error"] or {}).get("message")))
                return m.get("result")
        raise RuntimeError("MCP %s: no matching response" % method)

    # ── session ─────────────────────────────────────────────────────────────
    def connect(self):
        """Handshake this endpoint. Raises when it is not an MCP server —
        which is exactly how discovery tells a real endpoint from a name
        that merely looked right."""
        res = self._rpc("initialize", {
            "protocolVersion": _PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "proos-core", "version": "1"}})
        if not isinstance(res, dict) or "capabilities" not in res:
            raise RuntimeError("not an MCP server")
        self._rpc("notifications/initialized", {}, notify=True)
        return res

    def tools(self, force=False):
        """The server's own tool list — name, description, schema, and the
        server's read-only testimony. Cached on the TTL; a failed relist
        serves the last known list rather than inventing an empty one."""
        with self._lock:
            now = time.time()
            if (not force and self._tools is not None
                    and now - self._tools_ts < _TOOLS_TTL):
                return self._tools
            try:
                out, cursor = [], None
                while True:
                    params = {"cursor": cursor} if cursor else {}
                    res = self._rpc("tools/list", params) or {}
                    for t in res.get("tools") or []:
                        ann = t.get("annotations") or {}
                        out.append({
                            "name": t.get("name"),
                            "description": t.get("description") or "",
                            "input_schema": t.get("inputSchema")
                            or {"type": "object", "properties": {}},
                            # THE SERVER'S OWN TESTIMONY, ALL OF IT (B
                            # step 1, 18 Aug 2026). Absent = fail-closed:
                            # an unlabelled power is an ACT, and an act
                            # whose destructiveness is UNSTATED is treated
                            # as destructive. Nothing here is a list of
                            # tool names — the tier line follows what the
                            # server publishes, and when it publishes
                            # nothing we say so out loud (census below).
                            "read_only": ann.get("readOnlyHint") is True,
                            "destructive": ann.get("destructiveHint")
                            is not False,
                            "destructive_stated":
                                "destructiveHint" in ann,
                            "read_only_stated": "readOnlyHint" in ann})
                    cursor = res.get("nextCursor")
                    if not cursor:
                        break
                self._tools, self._tools_ts = out, now
                self.last_error = None
                self.census = {
                    "tools": len(out),
                    "read_only_stated": sum(1 for t in out
                                            if t["read_only_stated"]),
                    "read_only": sum(1 for t in out if t["read_only"]),
                    "destructive_stated": sum(1 for t in out
                                              if t["destructive_stated"]),
                    "safe_acts": sum(1 for t in out if not t["read_only"]
                                     and not t["destructive"])}
            except Exception as e:                               # noqa: BLE001
                self.last_error = str(e)[:200]
                if self._tools is None:
                    raise
            return self._tools

    def call(self, name, args):
        """One tool call; the result returned as the server sent it (the
        caller owns truncation — a cut must be SAID there)."""
        res = self._rpc("tools/call", {"name": name,
                                       "arguments": dict(args or {})}) or {}
        parts = []
        for c in res.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
        text = "\n".join(parts) if parts else json.dumps(res)
        if res.get("isError"):
            return {"error": text[:2000]}
        try:
            return json.loads(text)
        except ValueError:
            return {"result": text}


# ── discovery: knock where the records point, keep what ANSWERS ────────────
def _conn_load():
    try:
        with open(_CONN_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _conn_save(d):
    with open(_CONN_PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def discover(sv_get, http=None):
    """An HaMcp for the box's MCP server, or raise McpUnavailable with the
    truth. `sv_get(path)` is Core's Supervisor GET. The remembered endpoint
    is retried first; otherwise every started add-on whose slug mentions
    'mcp' contributes candidates (its published port + secret path), and
    ONLY an endpoint that answers the MCP handshake is kept."""
    tried = []
    remembered = _conn_load().get("url")
    candidates = [remembered] if remembered else []
    try:
        addons = (sv_get("/addons") or {}).get("addons", [])
    except Exception:                                            # noqa: BLE001
        addons = []
    for a in addons:
        if "mcp" not in str(a.get("slug", "")).lower():
            continue
        if a.get("state") != "started":
            continue
        try:
            info = sv_get("/addons/%s/info" % a["slug"]) or {}
        except Exception:                                        # noqa: BLE001
            continue
        host = info.get("ip_address") or info.get("hostname")
        ports = [p for p in (info.get("network") or {}).values() if p]
        secret = str((info.get("options") or {}).get("secret_path") or "")
        for port in ports:
            base = "http://%s:%s" % (host, port)
            for path in (secret + "/mcp", secret, "/mcp"):
                if path and base + path not in candidates:
                    candidates.append(base + path)
    for url in candidates:
        c = HaMcp(url, http=http)
        try:
            c.connect()
            _conn_save({"url": url})
            return c
        except Exception as e:                                   # noqa: BLE001
            tried.append("%s (%s)" % (url, str(e)[:60]))
    raise McpUnavailable("no MCP server answered the handshake"
                         + (" — tried: " + "; ".join(tried[:4]) if tried
                            else " — no candidates found"))
