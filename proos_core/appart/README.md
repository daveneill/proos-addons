# ProOS App Tile Pack — spec & process (v2)

App tile artwork is a PRODUCT asset with two layers, both served identically to
every dashboard, remote and widget at `/apps/art/tile/<slug>.png`:

1. **Shipped pack** — `appart/` folder in the Core add-on repo. Populate once
   centrally; every install carries it with each release.
2. **Uploaded tiles** — managed live in **Pro → Tech Tools → App Tiles**
   (tech/owner only; installers never see it). Stored in Core's data on the
   box; survives add-on updates AND factory resets. An upload with the same
   slug as a shipped tile **overrides** it on that install; deleting the
   upload restores the shipped tile.

Resolution: uploaded → shipped → clean neutral wordmark tile (app name on
dark) until a graphic exists.

## Managing tiles in Pro (the day-to-day process)

Tech Tools → **App Tiles**:

* **Grid** shows every tile currently served, with live preview, slug, and an
  origin badge — `shipped` / `uploaded` / `override`.
* **Upload** — choose one or more image files; the FILE NAME becomes the slug
  (`netflix.png` → `netflix`). Any PNG/JPEG/WebP is accepted: Pro normalises
  every upload to the spec automatically (600×360 PNG, cover-cropped), so
  drag in whatever you have.
* **Delete** removes an uploaded tile (shipped tiles can't be deleted on-box —
  upload a replacement to override, delete the override to restore).

No Samba, no file shares, no SSH — it's all in Pro, on the tech tier.

## Do tiles refresh when uploaded?

* **App Tiles manager** — instantly (the grid reloads with cache-busted
  previews after every upload/delete).
* **Dashboards / remotes** — a **new** tile (app previously a wordmark)
  appears the next time that dashboard page loads its app tiles — i.e. on the
  next open/refresh of the PWA; no deploy, no Core restart. A **replacement**
  tile (override of an existing image) propagates within ~5 minutes (tile
  responses cache for 300 s) plus the next page refresh. Nothing ever
  requires touching the devices.

## File requirements (shipped pack, and what uploads are normalised to)

* **Format** PNG, full-bleed artwork — the tile exactly as it should appear.
  Square corners (the UI applies rounding).
* **Size** 600×360 px (5:3). ≤300 KB recommended (uploads hard-capped ~1.5 MB
  pre-normalisation).
* **Name** `<slug>.png` — the app's display name AS THE DEVICE REPORTS IT,
  lowercased, every run of non-alphanumerics → `_`, trimmed:

  | App name (from device) | File name |
  |---|---|
  | Netflix | `netflix.png` |
  | Disney+ | `disney.png` |
  | Prime Video | `prime_video.png` |
  | ABC iview | `abc_iview.png` |
  | 7plus | `7plus.png` |
  | 9Now | `9now.png` |
  | SBS On Demand | `sbs_on_demand.png` |
  | F1 TV | `f1_tv.png` |
  | BINGE | `binge.png` |
  | Kayo | `kayo.png` |
  | Apple TV | `apple_tv.png` |
  | YouTube | `youtube.png` |

## aliases.json (shipped pack, optional)

Different platforms name one service differently — map variants to a single
graphic instead of duplicating files:

    { "amazon_prime_video": "prime_video",
      "disney_plus":        "disney",
      "youtube_tv":         "youtube" }

## Diagnosis

`GET /apps/art/status` on any install lists every tile served (with origin)
and the alias map — "why is this one a wordmark" is a one-request answer.
