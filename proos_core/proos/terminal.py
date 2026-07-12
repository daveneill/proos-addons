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
import errno
import fcntl
import json
import logging
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import termios
import time

_LOG = logging.getLogger("proos.terminal")

# Strip terminal escape sequences (colour, cursor moves, OSC titles) that a
# real TTY makes tools emit — the browser <pre> can't render them, so without
# this they'd show up as raw junk like "^[[32m".
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _clean_tty(s):
    s = _ANSI_RE.sub("", s)
    s = s.replace("\r\n", "\n")
    # Collapse in-place line rewrites (progress spinners redraw with a bare \r):
    # keep only the final text of each line.
    return "\n".join(ln.split("\r")[-1] if "\r" in ln else ln for ln in s.split("\n"))

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
    globbing) run under a PSEUDO-TERMINAL so TTY-aware tools (the `ha` CLI,
    coloured/progress output, etc.) behave EXACTLY as in native HA's terminal —
    e.g. `ha store reload` prints "Command completed successfully." instead of
    nothing. NO command filter. `cd` persists across calls via the returned cwd.
    Still audited, hard-timed-out, output-capped, and Supervisor-token-redacted.
    """
    command = (command or "").strip()
    if not command:
        return {"error": "empty command"}
    wd = cwd if (cwd and cwd.startswith("/")) else "/app"
    RS = "\x1e"  # fences the trailing cwd/exit-code markers off from real output
    script = "cd %s 2>/dev/null; %s\n__rc=$?; printf '%sPWD:%%s%sRC:%%s' \"$(pwd)\" \"$__rc\"" % (
        shlex.quote(wd), command, RS, RS)

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except Exception:
        pass
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    try:
        p = subprocess.Popen(["sh", "-c", script], stdin=slave, stdout=slave,
                             stderr=slave, close_fds=True, start_new_session=True,
                             env=env)
    except Exception as exc:  # noqa: BLE001
        for fd in (master, slave):
            try:
                os.close(fd)
            except Exception:
                pass
        return {"error": str(exc), "cwd": wd}
    os.close(slave)

    buf = bytearray()
    deadline = time.time() + TIMEOUT_FULL
    timed_out = False
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                r, _, _ = select.select([master], [], [], min(remaining, 1.0))
            except (OSError, ValueError):
                break
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:  # slave closed → process finished
                        break
                    raise
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_OUT * 4:  # runaway guard
                    break
            elif p.poll() is not None:
                break
    finally:
        try:
            os.close(master)
        except Exception:
            pass
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass

    out = buf.decode("utf-8", "replace")
    new_cwd = wd
    code = p.returncode if p.returncode is not None else 0
    i = out.rfind(RS + "PWD:")
    if i != -1:
        tail = out[i:]
        out = out[:i]
        try:
            pw = tail.split(RS + "PWD:", 1)[1]
            new_cwd = pw.split(RS + "RC:", 1)[0].strip() or wd
            code = int(pw.split(RS + "RC:", 1)[1].strip() or code)
        except Exception:
            pass
    out = _redact(_clean_tty(out))
    if timed_out:
        _audit(user, "$ " + command, "timeout")
        return {"error": "command timed out after %ss" % TIMEOUT_FULL,
                "output": out[:MAX_OUT], "cwd": new_cwd}
    truncated = len(out) > MAX_OUT
    _audit(user, "$ " + command, code)
    return {"ok": True, "code": code, "output": out[:MAX_OUT],
            "truncated": truncated, "cwd": new_cwd}


def audit_tail(n: int = 200) -> list:
    try:
        with open(AUDIT, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []
