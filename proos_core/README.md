# ProOS Core (Home Assistant add-on)

Runs ProOS Core as a Supervisor add-on: it boots with Home Assistant, runs 24/7,
and needs **no manually-created token** — the Supervisor provides one and proxies
HA core at `http://supervisor/core`. The same `server.py` you ran on the Mac runs
here unchanged; only the HA endpoint differs.

## Install (local add-on — no GitHub needed)

You already have the Samba and SSH add-ons, so either works:

1. Copy the **`proos_core`** folder into Home Assistant's `/addons/` directory
   (via Samba: the `addons` share; or SSH: `/addons/proos_core`). The folder must
   contain `config.yaml`, `Dockerfile`, `build.yaml`, `run.sh`, `server.py`, and
   the `proos/` package.
2. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**, then
   scroll to **Local add-ons** and open **ProOS Core**.
3. Click **Install** (first build pulls the base image + python3 — a minute or two).
4. Open the **Configuration** tab, set your options (below), **Save**.
5. **Start**. Watch the **Log** tab — you should see
   `ProOS Core API  home=...  ha=http://supervisor/core` and `HA says: API running.`

## Configuration

| Option | Meaning |
|---|---|
| `area` | The room Core manages first (e.g. `Family Room`). |
| `monitor_interval` | Seconds between health checks (5–3600). |
| `auto_heal` | If `true`, Core runs the recovery ladder automatically on faults. |
| `reachability` | Per device, an independent liveness signal. List of `{entity, sensor}` (an HA `ping`/router entity — recommended) or `{entity, ip}` (Core TCP probe). |

Example:
```yaml
area: Family Room
monitor_interval: 20
auto_heal: false
reachability:
  - entity: media_player.family_room_apple_tv
    sensor: binary_sensor.192_168_1_110
```

## Connecting the dashboard

Core's API is exposed on port **8770**. Point the dashboard (or the
`proos-client.html` test page) at `http://<home-assistant-ip>:8770`.

## Security note

The API on `8770` is currently **unauthenticated on the LAN** — fine for a trusted
home network, not for direct internet exposure. The hardening step is HA **ingress**
(serve the API through HA's authenticated proxy, drop the open port); that's the
next add-on iteration.

## If the build fails pulling the base image

`build.yaml` pins `ghcr.io/home-assistant/<arch>-base:latest`. If your Supervisor
can't pull it, replace `:latest` with a current tag from the Home Assistant base
images, or switch to a `-base-python` image and drop the `apk add python3` line.
