ARG BUILD_FROM
FROM $BUILD_FROM

# The same base and package route as ProOS Core. ffmpeg carries each camera's picture and sound from Protect to the
# iPhone (SRTP, as Home Assistant's own Apple Home cameras do). HAP-python is Home Assistant's own Apple Home library
# (Apache-2.0); homekit-audio-proxy is the piece Home Assistant uses to correct Opus timing for Apple (Apache-2.0).
RUN apk add --no-cache python3 py3-pip py3-cryptography py3-orjson ffmpeg
RUN pip install --no-cache-dir --break-system-packages \
      "HAP-python==5.0.0" "homekit-audio-proxy==1.2.1" "zeroconf>=0.36.2" h11 chacha20poly1305-reuseable

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh
WORKDIR /app
CMD [ "/run.sh" ]
