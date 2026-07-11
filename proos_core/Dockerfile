ARG BUILD_FROM
FROM $BUILD_FROM
ARG BUILD_ARCH

# Core's Python code has ZERO third-party dependencies. Push needs two system
# tools (not Python packages): openssl signs the APNs ES256 token, curl speaks
# HTTP/2 to Apple. Still nothing to pin in Python.
RUN apk add --no-cache python3 openssl curl

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

COPY proos /app/proos
COPY server.py /app/server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
