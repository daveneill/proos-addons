ARG BUILD_FROM
FROM $BUILD_FROM

# Core's Python code has ZERO third-party dependencies. Push needs two system
# tools (not Python packages): openssl signs the APNs ES256 token, curl speaks
# HTTP/2 to Apple. Still nothing to pin in Python.
RUN apk add --no-cache python3 openssl curl

COPY proos /app/proos
COPY server.py /app/server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
