ARG BUILD_FROM
FROM $BUILD_FROM
ARG BUILD_ARCH

# Core's Python code has ZERO third-party dependencies. Push needs two system
# tools (not Python packages): openssl signs the APNs ES256 token, curl speaks
# HTTP/2 to Apple. Still nothing to pin in Python.
RUN apk add --no-cache python3 openssl curl \
    # Tech-terminal parity with HA's Terminal & SSH add-on: ship the everyday
    # CLI tools so a Protech user never has to jump to native HA to run them.
    bash coreutils findutils grep sed gawk jq wget nano less \
    procps iproute2 bind-tools tar gzip openssh-client ca-certificates rsync tzdata

# The REAL Home Assistant CLI (`ha`), so the Tech Tools terminal is a complete
# stand-in for native HA — a tech never needs HA's own terminal/SSH. It's the
# same static Go binary the OS ships (one per arch), and it talks to the
# Supervisor through this add-on's SUPERVISOR_TOKEN + hassio_role:manager, so
# `ha store reload`, `ha addons update …`, `ha core …` etc. behave byte-for-byte
# like native HA. Pinned; bump HA_CLI_VERSION to update.
ARG HA_CLI_VERSION=5.1.0
RUN curl -fsSL -o /usr/bin/ha \
      "https://github.com/home-assistant/cli/releases/download/${HA_CLI_VERSION}/ha_${BUILD_ARCH}" \
    && chmod +x /usr/bin/ha \
    && [ -s /usr/bin/ha ]

# REGISTER 696: libjpeg-turbo's own djpeg/cjpeg shrink each camera event picture
# (3200x1800, 661 KB from the platform) to a quarter once, so the Cameras page
# loads small pictures. Its own line, and never fatal: without it Core serves the
# platform's picture as it came (slower, never wrong).
RUN apk add --no-cache libjpeg-turbo-utils || echo "libjpeg-turbo-utils unavailable — camera pictures stay full size"

COPY proos /app/proos
# Product app tile artwork — ships with the release (see appart/README.md).
COPY appart /app/appart
# A2 Phase 1 (23 Aug 2026, reg 267): the two apps ride INSIDE the image —
# Core serves them at /app/pro and /app/dashboard, so an app update ships
# with the Core update. Ship copies of deploy/, pinned byte-identical by
# app_routes_bench.py.
COPY apps /app/apps
# The Savant profile rides INSIDE the image too (register 634): Core serves it
# at /savant/profile so the profile and the feed can never disagree. 797 shipped
# it in the zip but not in the image, so Pro said "not in this build".
COPY savant /app/savant
COPY server.py /app/server.py
# The add-on manifest rides into the image so /health can report the REAL
# version (3 Aug: it read "dev" because config.yaml wasn't in the container
# and the footer cried update-needed against a current Core).
COPY config.yaml /app/config.yaml
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
