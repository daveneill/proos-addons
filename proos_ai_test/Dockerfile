# ProOS AI Test (register 976): the llama.cpp project's own image with the Vulkan (graphics chip) build of its tools.
FROM ghcr.io/ggml-org/llama.cpp:full-vulkan
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates mesa-vulkan-drivers vulkan-tools \
    && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
RUN chmod +x /run.sh
ENTRYPOINT []
CMD ["/run.sh"]
