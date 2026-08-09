# ProOS Music — OUR build (Dave's rule, 9 Aug 2026): the proven 2.9.5 engine
# (streams and decodes on this fleet) carrying upstream's own auth fix from
# 2.9.11 — the system-user allowlist, their exact code, our image. Built
# locally by the Supervisor; no external tag to drift underneath us.
#
# proos.2 (9 Aug 2026): + upstream's Apple rate-limit fixes (PRs #5333/#5391,
# merged 5-6 Aug, in no upstream release yet): a throttled request now retries
# in ~1s instead of stalling 15s (the "starting... slowly" taps and mid-stream
# aborts), and library sync uses half the requests, so Apple's limiter is hit
# less in the first place.
FROM ghcr.io/music-assistant/server:2.9.5
COPY auth_middleware.py /tmp/proos_patch/auth_middleware.py
COPY websocket_client.py /tmp/proos_patch/websocket_client.py
COPY api_client.py /tmp/proos_patch/api_client.py
RUN set -e; \
    PKG=$(find / -path '*/site-packages/music_assistant/controllers/webserver' -type d 2>/dev/null | head -1); \
    test -n "$PKG"; \
    cp /tmp/proos_patch/auth_middleware.py "$PKG/helpers/auth_middleware.py"; \
    cp /tmp/proos_patch/websocket_client.py "$PKG/websocket_client.py"; \
    PRV=$(find / -path '*/site-packages/music_assistant/providers/apple_music' -type d 2>/dev/null | head -1); \
    test -n "$PRV"; \
    cp /tmp/proos_patch/api_client.py "$PRV/api_client.py"; \
    rm -rf /tmp/proos_patch
