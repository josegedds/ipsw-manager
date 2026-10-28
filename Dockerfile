# Alpine 3.24, pinned by digest so rebuilds use the same base until it's
# bumped deliberately.
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    coreutils \
    ca-certificates \
    python3 \
    supercronic \
    su-exec \
    tini \
    tzdata

COPY --chmod=755 ipsw-manager.sh webui.py /usr/local/bin/
COPY --chmod=755 entrypoint.sh /entrypoint.sh

# Default schedule. Override at run time with -e CRON_SCHEDULE=...;
# the build arg still works for baking in a different default.
ARG CRON_SCHEDULE="0 18 * * *"
ENV CRON_SCHEDULE=${CRON_SCHEDULE} \
    TZ=UTC \
    IPSW_DOWNLOAD_DIR=/downloads \
    IPSW_CHECKSUM_ALGO=md5 \
    WEBUI_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080
VOLUME ["/downloads"]

HEALTHCHECK --interval=1m --timeout=5s --start-period=15s \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${WEBUI_PORT}/" || exit 1

# tini reaps orphaned processes and forwards `docker stop` to the entrypoint,
# which runs the web UI and the scheduler side by side.
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]

ARG VERSION=dev
LABEL org.opencontainers.image.title="ipsw-manager" \
      org.opencontainers.image.description="Keeps a folder of the latest signed iPhone firmware (IPSW) files, with a web dashboard" \
      org.opencontainers.image.source="https://github.com/josegedds/ipsw-manager" \
      org.opencontainers.image.version="${VERSION}"
