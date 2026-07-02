#!/usr/bin/env python3
"""
Add ProOS user-management + provisioning routes to ProOS Core's server.py.

Additive and safe: every edit is anchored with assert count == 1, so if your
server.py has drifted the script stops before touching the file. Idempotent -
running it twice is a no-op. Writes a .bak, then byte-compiles the result.

Usage:
    python3 apply_users_routes.py /addons/proos_core/server.py

Prereq: copy provision.py and users.py into the same proos/ package first.
"""
import py_compile
import shutil
import sys


def patch(text: str) -> str:
    if "def _ws_call(" in text:
        print("already patched - nothing to do")
        return text

    def once(s, anchor, new):
        assert s.count(anchor) == 1, "anchor not found exactly once:\n%s" % anchor[:80]
        return s.replace(anchor, new, 1)

    # A) guarded import
    text = once(text, "from proos import sync\n",
                "from proos import sync\n"
                "try:\n"
                "    from proos import provision, users\n"
                "except Exception:  # optional - missing modules must not stop boot\n"
                "    provision = users = None\n")

    # B) ws_call adapter
    text = once(text, "_cfg: dict | None = None\n",
                "_cfg: dict | None = None\n\n\n"
                "def _ws_call(msg_type, **fields):\n"
                '    """Adapter: run one HA websocket command over Core\'s supervisor\n'
                '    connection. Passed into provision/users so they stay decoupled."""\n'
                "    from proos.ha_ws import ws_command\n"
                '    return ws_command(_cfg["base_url"], _cfg["token"], msg_type, **fields)\n')

    # C) do_GET routes
    text = once(
        text,
        '            if len(parts) == 3 and parts[0] == "backups" and parts[2] == "download":\n'
        '                return self._stream_download(parts[1])\n'
        '            return self._send(404, {"error": "not found"})\n'
        '        except MaUnavailable as e:',
        '            if len(parts) == 3 and parts[0] == "backups" and parts[2] == "download":\n'
        '                return self._stream_download(parts[1])\n'
        '            if parts == ["users"]:\n'
        '                if not users:\n'
        '                    return self._send(200, {"users": [], "can_manage": False})\n'
        '                try:\n'
        '                    return self._send(200, {"users": users.list_users(_ws_call), "can_manage": True})\n'
        '                except Exception as e:\n'
        '                    return self._send(200, {"users": [], "can_manage": False, "error": str(e)})\n'
        '            if parts == ["provision"]:\n'
        '                return self._send(200, provision.provision_status() if provision else {"provisioned": False})\n'
        '            return self._send(404, {"error": "not found"})\n'
        '        except MaUnavailable as e:')

    # D) do_POST routes
    text = once(
        text,
        '                return\n'
        '            return self._send(404, {"error": "not found"})\n'
        '        except MaAuthFailed as e:',
        '                return\n'
        '            if parts == ["users"]:\n'
        '                if not users:\n'
        '                    return self._send(503, {"error": "user module not loaded"})\n'
        '                b = self._body()\n'
        '                name = (b.get("name") or "").strip()\n'
        '                if not name:\n'
        '                    return self._send(400, {"error": "name required"})\n'
        '                try:\n'
        '                    return self._send(200, users.create_user(\n'
        '                        _ws_call, name=name, role=b.get("role") or "homeowner",\n'
        '                        password=(b.get("password") or None)))\n'
        '                except Exception as e:\n'
        '                    return self._send(400, {"error": str(e)})\n'
        '            if len(parts) == 3 and parts[0] == "users" and parts[2] == "password":\n'
        '                if not users:\n'
        '                    return self._send(503, {"error": "user module not loaded"})\n'
        '                try:\n'
        '                    pw = users.set_password(_ws_call, unquote(parts[1]),\n'
        '                                            (self._body().get("password") or None))\n'
        '                    return self._send(200, {"ok": True, "password": pw})\n'
        '                except Exception as e:\n'
        '                    return self._send(400, {"error": str(e)})\n'
        '            if len(parts) == 3 and parts[0] == "users" and parts[2] == "role":\n'
        '                if not users:\n'
        '                    return self._send(503, {"error": "user module not loaded"})\n'
        '                try:\n'
        '                    return self._send(200, users.set_role(\n'
        '                        _ws_call, unquote(parts[1]), self._body().get("role") or ""))\n'
        '                except Exception as e:\n'
        '                    return self._send(400, {"error": str(e)})\n'
        '            if len(parts) == 3 and parts[0] == "users" and parts[2] == "delete":\n'
        '                if not users:\n'
        '                    return self._send(503, {"error": "user module not loaded"})\n'
        '                try:\n'
        '                    return self._send(200, users.delete_user(_ws_call, unquote(parts[1])))\n'
        '                except Exception as e:\n'
        '                    return self._send(400, {"error": str(e)})\n'
        '            if parts == ["provision", "claim"]:\n'
        '                b = self._body()\n'
        '                out = provision.mark_claimed((b.get("site_name") or "").strip() or None) if provision else {}\n'
        '                name = (b.get("homeowner") or "").strip()\n'
        '                if name and users:\n'
        '                    try:\n'
        '                        out["homeowner"] = users.create_homeowner(\n'
        '                            _ws_call, name=name, password=(b.get("password") or None))\n'
        '                    except Exception as e:\n'
        '                        out["homeowner_error"] = str(e)\n'
        '                return self._send(200, out)\n'
        '            return self._send(404, {"error": "not found"})\n'
        '        except MaAuthFailed as e:')

    # E) boot wiring
    text = once(
        text,
        '    print("  watcher running (interval 5s) -> GET /watchers")\n',
        '    print("  watcher running (interval 5s) -> GET /watchers")\n'
        '    if provision:\n'
        '        try:\n'
        '            _pv = provision.ensure_provisioned(ws_call=_ws_call)\n'
        '            print(f"  provision \\u00b7 site={_pv.get(\'site_id\',\'n/a\')} host={_pv.get(\'hostname\',\'\')}")\n'
        '        except Exception as _e:\n'
        '            print(f"  provision skipped: {_e}")\n'
        '    if users:\n'
        '        try:\n'
        '            _chk = users.manage_check(_ws_call)\n'
        '            print(f"  users \\u00b7 {_chk[\'hint\']}")\n'
        '        except Exception as _e:\n'
        '            print(f"  users check skipped: {_e}")\n')

    return text


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    original = open(path, encoding="utf-8").read()
    patched = patch(original)
    if patched == original:
        return
    shutil.copy2(path, path + ".bak")
    open(path, "w", encoding="utf-8").write(patched)
    py_compile.compile(path, doraise=True)
    print("patched %s (backup at %s.bak) - routes added, byte-compiles clean" % (path, path))


if __name__ == "__main__":
    main()
