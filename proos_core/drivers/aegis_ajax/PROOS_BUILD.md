# ProOS Ajax — the INTERIM driver (Aegis for Ajax 1.27.2), disposable

Register 971. Dave, 24 Oct 2026: the long-term Ajax driver is built on Ajax's OFFICIAL Enterprise API ("I need it to
be the supported way as I don't want it reverse engineered as it can break") — the application is with Dave. Until the
key arrives, this community driver rides inside Core so Dave can test on his own Ajax unit ("it does not show on
integrations so it would need to be one of ours so yes").

- Upstream: github.com/bvis/aegis-hass, tag v1.27.2 (commit fbda8247, 21 Oct 2026) — the latest non-beta tag on
  24 Oct. MIT licence, kept as LICENSE.upstream.
- Byte-for-byte upstream EXCEPT manifest.json: `name` "Aegis for Ajax" → "Ajax", `codeowners` → `@protech-proos`,
  `version` 1.27.2 → 1.27.2+proos.1. No functional change. Domain stays `aegis_ajax`.
- It emulates the Ajax phone app's gRPC protocol — NOT a supported Ajax interface; Ajax may change it at any time
  (upstream's own readme). It is DISPOSABLE: replaced underneath by the official-API build when Dave's key arrives.
- First load installs its requirements on the box (grpcio, protobuf, firebase-messaging): expect the first platform
  start after install to take minutes. Live alarm/tamper EVENTS need its push (FCM) setup — read on the box before
  anything is claimed.
