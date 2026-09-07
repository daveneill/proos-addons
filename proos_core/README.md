# ProOS Core (Home Assistant add-on)

The operational layer above Home Assistant: state-based room control,
monitoring, self-healing, Pro Assist, and the ProOS apps. It boots with
Home Assistant, runs 24/7, and needs **no manually-created token** — the
Supervisor provides one and proxies HA core at `http://supervisor/core`.

## What an update carries

**Updating this add-on is the whole install.** Core ships the ProOS
apps inside its image and converges `www/pro.html` and
`www/dashboard.html` to its shipped copies on every start — install
when different, leave alone when identical, touch nothing else. No
files are ever hand-copied.

## The doors

- **Homeowners:** the ProOS Dashboard at `/local/dashboard.html` on
  Home Assistant's own address — pinned icons and the ProOS app open
  here. Signing in at the box's address also lands in ProOS: Core
  maintains the **ProOS** dashboard as the installation default, and
  other dashboards are visible to admins only.
- **Pros:** the **ProOS** sidebar entry (ingress — served through Home
  Assistant's authenticated front door) and `/local/pro.html`.
- **Core's API — port 8770:** the published, authenticated service
  port the apps and future native shells talk to. Every request
  requires a ProOS bearer token (`require_auth` is on by default); an
  unauthenticated request is refused. This port is not a sign-in
  surface — sign-in happens on Home Assistant's own origin.

## Configuration

| Option | Meaning |
|---|---|
| `area` | The room Core manages first (e.g. `Family Room`). |
| `monitor_interval` | Seconds between health checks (5–3600). |
| `auto_heal` | If `true`, Core runs the recovery ladder automatically on faults. |
| `require_auth` | Keep `true`: every API request needs a ProOS token. |
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

## Install (add-on repository)

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add the
   ProOS add-on repository, then **Check for updates**.
2. Open **ProOS Core** → **Install**, set options, **Start**.
3. Watch the **Log** tab for `ProOS Core API` and the
   `apps ·` line confirming the apps are current.

## If the build fails pulling the base image

`build.yaml` pins `ghcr.io/home-assistant/<arch>-base:latest`. If your
Supervisor can't pull it, replace `:latest` with a current tag from the
Home Assistant base images.
