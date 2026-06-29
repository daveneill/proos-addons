ARG BUILD_FROM
FROM $BUILD_FROM

# Core has ZERO third-party dependencies -- python3 is all it needs.
# That's the payoff of building it on stdlib: nothing to pin, nothing to break.
RUN apk add --no-cache python3

COPY proos /app/proos
COPY server.py /app/server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
