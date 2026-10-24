# ProOS AI Test 0.2.0 (register 976): whisper.cpp's own Vulkan (graphics chip) build, and the G-Flip mic test's takes.
FROM ghcr.io/ggml-org/whisper.cpp:main-vulkan
RUN apt-get update && apt-get install -y --no-install-recommends vulkan-tools && rm -rf /var/lib/apt/lists/*
COPY takes /takes
COPY run.sh /run.sh
RUN chmod +x /run.sh
ENTRYPOINT []
CMD ["/run.sh"]
