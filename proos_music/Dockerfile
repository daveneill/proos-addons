# ProOS Music — OUR build (Dave's rule, 9 Aug 2026): the proven 2.9.5 engine
# (streams and decodes on this fleet) carrying upstream's own auth fix from
# 2.9.11 — the system-user allowlist, their exact code, our image. Built
# locally by the Supervisor; no external tag to drift underneath us.
FROM ghcr.io/music-assistant/server:2.9.5
COPY auth_middleware.py /tmp/proos_patch/auth_middleware.py
COPY websocket_client.py /tmp/proos_patch/websocket_client.py
RUN set -e; \
    PKG=$(find / -path '*/site-packages/music_assistant/controllers/webserver' -type d 2>/dev/null | head -1); \
    test -n "$PKG"; \
    cp /tmp/proos_patch/auth_middleware.py "$PKG/helpers/auth_middleware.py"; \
    cp /tmp/proos_patch/websocket_client.py "$PKG/websocket_client.py"; \
    rm -rf /tmp/proos_patch
