"""
ProOS Core - full diagnostic terminal (tech tier only).

Runs commands inside Core's own container. `run_shell()` is a real shell
(pipes, redirects, globbing, persistent `cd`) so the installer never needs
native HA's terminal — there is NO command allow/deny filter: the route that
calls this enforces tech/owner identity server-side, so it is already gated and
runs whatever HA's own terminal would. `run()` is a no-shell variant kept for
API callers; it is likewise unfiltered now.

Still audited (every attempt is logged with the verified caller), hard-timed
out, output-capped for browser sanity, and the Supervisor token is redacted from
output so it can't leak into a screenshot.
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
TIMEOUT_FULL = 180
# Generous cap so real output isn't clipped (HA's terminal shows everything);
# this only guards the browser from a runaway multi-megabyte dump.
MAX_OUT = 200000


def _audit(user, cmd, code):
    try:
        with open(AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "user": user, "cmd": cmd, "code": code}) + "\n")
    except Exception:
        pass


def _redact(out):
    sup = os.environ.get("SUPERVISOR_TOKEN")
    if sup:
        out = out.replace(sup, "«redacted»")
    return out


def run(command: str, user: str = "?") -> dict:
    """Run a single command with NO shell (no pipes/redirects/globbing) and no
    allow/deny filter. Kept for API callers; the terminal uses run_shell()."""
    command = (command or "").strip()
    if not command:
        return {"error": "empty command"}
    try:
        parts = shlex.split(command)
    except Exception:
        return {"error": "could not parse command"}
    if not parts:
        return {"error": "empty command"}
    try:
        p = subprocess.run(parts, capture_output=True, text=True, timeout=TIMEOUT)
        out = _redact((p.stdout or "") + (p.stderr or ""))
        truncated = len(out) > MAX_OUT
        _audit(user, command, p.returncode)
        return {"ok": True, "code": p.returncode, "output": out[:MAX_OUT], "truncated": truncated}
    except subprocess.TimeoutExpired:
        _audit(user, command, "timeout")
        return {"error": "command timed out after %ss" % TIMEOUT}
    except FileNotFoundError:
        return {"error": "that command isn't available in this container"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def run_shell(command: str, user: str = "?", cwd: str | None = None) -> dict:
    """Full shell for the tech tier — the calling route enforces tech/owner
    identity, so this trusts it is gated. A real shell (pipes, redirects,
    globbing) so the installer never needs native HA's terminal, with NO command
    filter. `cd` persists across calls via the returned cwd. Still audited,
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
        out = _redact(out)
        truncated = len(out) > MAX_OUT
        _audit(user, "$ " + command, code)
        return {"ok": True, "code": code, "output": out[:MAX_OUT],
                "truncated": truncated, "cwd": new_cwd}
    except subprocess.TimeoutExpired:
        _audit(user, "$ " + command, "timeout")
        return {"error": "command timed out after %ss" % TIMEOUT_FULL, "cwd": wd}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "cwd": wd}


def audit_tail(n: int = 200) -> list:
    try:
        with open(AUDIT, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []
