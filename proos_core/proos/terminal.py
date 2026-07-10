"""
ProOS Core - constrained diagnostic terminal (tech tier only).

Runs a small allowlist of READ-ONLY commands inside Core's own container,
with NO shell (so no pipes, redirects, or globbing), a hard timeout, and
capped output. Arguments that reach for secrets or /data are refused. Every
attempt - allowed or denied - is written to an audit log with the verified
caller. This is deliberately a diagnostic surface, not a root shell; it
widens only by editing ALLOWED. The route that calls this enforces tech
identity server-side, so this module trusts it is already gated.
"""
import json
import logging
import os
import shlex
import subprocess
import time

_LOG = logging.getLogger("proos.terminal")

AUDIT = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "terminal_audit.log")
TIMEOUT = 15
MAX_OUT = 12000

ALLOWED = {
    "ls", "cat", "head", "tail", "pwd", "whoami", "id", "uname", "hostname",
    "date", "uptime", "df", "du", "free", "ps", "stat", "which", "wc",
    "grep", "find", "echo", "ping", "nslookup", "host", "ip", "netstat", "ss",
}
# Any argument containing one of these is refused (secrets / sensitive paths).
BLOCKED_SUBSTR = ("shadow", "/data/", "token", "secret", "password", ".key", "options.json")


def _audit(user, cmd, code):
    try:
        with open(AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "user": user, "cmd": cmd, "code": code}) + "\n")
    except Exception:
        pass


def run(command: str, user: str = "?") -> dict:
    command = (command or "").strip()
    if not command:
        return {"error": "empty command"}
    try:
        parts = shlex.split(command)
    except Exception:
        return {"error": "could not parse command"}
    if not parts:
        return {"error": "empty command"}
    verb = os.path.basename(parts[0])
    if verb not in ALLOWED:
        _audit(user, command, "denied")
        return {"denied": True, "error": "'%s' isn't an allowed command" % verb,
                "allowed": sorted(ALLOWED)}
    low = command.lower()
    if any(b in low for b in BLOCKED_SUBSTR):
        _audit(user, command, "blocked-arg")
        return {"denied": True, "error": "that path or argument is blocked"}
    try:
        p = subprocess.run(parts, capture_output=True, text=True, timeout=TIMEOUT)
        out = (p.stdout or "") + (p.stderr or "")
        sup = os.environ.get("SUPERVISOR_TOKEN")
        if sup:
            out = out.replace(sup, "«redacted»")
        truncated = len(out) > MAX_OUT
        _audit(user, command, p.returncode)
        return {"ok": True, "code": p.returncode, "output": out[:MAX_OUT], "truncated": truncated}
    except subprocess.TimeoutExpired:
        _audit(user, command, "timeout")
        return {"error": "command timed out after %ss" % TIMEOUT}
    except FileNotFoundError:
        return {"error": "that command isn't available in this container"}
    except Exception as exc:
        return {"error": str(exc)}


TIMEOUT_FULL = 180


def run_shell(command: str, user: str = "?", cwd: str | None = None) -> dict:
    """Full shell for the tech tier — the calling route enforces tech/owner
    identity, so this trusts it is gated. Unlike run(), this is a real shell
    (pipes, redirects, globbing) so the installer never needs native HA's
    terminal. `cd` persists across calls via the returned cwd. Still audited,
    hard-timed-out, output-capped, and Supervisor-token-redacted.
    """
    command = (command or "").strip()
    if not command:
        return {"error": "empty command"}
    wd = cwd if (cwd and cwd.startswith("/")) else "/app"
    RS = "\x1e"  # fences the trailing cwd/exit-code markers off from real output
    script = "cd %s 2>/dev/null; %s\n__rc=$?; printf '%sPWD:%%s%sRC:%%s' \"$(pwd)\" \"$__rc\"" % (
        shlex.quote(wd), command, RS, RS)
    try:
        p = subprocess.run(["sh", "-c", script], capture_output=True,
                           text=True, timeout=TIMEOUT_FULL)
        out = (p.stdout or "") + (p.stderr or "")
        new_cwd, code = wd, p.returncode
        i = out.rfind(RS + "PWD:")
        if i != -1:
            tail = out[i:]
            out = out[:i]
            try:
                pw = tail.split(RS + "PWD:", 1)[1]
                new_cwd = pw.split(RS + "RC:", 1)[0] or wd
                code = int(pw.split(RS + "RC:", 1)[1])
            except Exception:
                pass
        sup = os.environ.get("SUPERVISOR_TOKEN")
        if sup:
            out = out.replace(sup, "«redacted»")
        truncated = len(out) > MAX_OUT
        _audit(user, "$ " + command, code)
        return {"ok": True, "code": code, "output": out[:MAX_OUT],
                "truncated": truncated, "cwd": new_cwd}
    except subprocess.TimeoutExpired:
        _audit(user, "$ " + command, "timeout")
        return {"error": "command timed out after %ss" % TIMEOUT_FULL, "cwd": wd}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "cwd": wd}


def audit_tail(n: int = 60) -> list:
    try:
        with open(AUDIT, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []
