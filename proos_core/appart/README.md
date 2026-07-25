# ProOS app tile pack — PRODUCT asset, ships with every Core release

Drop each streaming app's real home-screen tile artwork here as a PNG,
landscape (16:9 / 5:3, e.g. 600×360). These are served to every install's
dashboards, remotes and app-shortcut widgets automatically — populate once in
this repo, every project gets identical correct artwork with the next release.
No installer action on site, ever.

File name = app-name slug: lowercase, runs of non-alphanumerics → `_`:

    Netflix        -> netflix.png
    Disney+        -> disney.png
    ABC iview      -> abc_iview.png
    7plus          -> 7plus.png
    Prime Video    -> prime_video.png
    SBS On Demand  -> sbs_on_demand.png
    F1 TV          -> f1_tv.png

Resolution order on every surface: this pack (exact tile, full-bleed) →
Core-cached real square icon (automatic, Apple catalogue) → styled fallback.
