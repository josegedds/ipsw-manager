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
    supercronic

COPY ipsw-manager.sh /usr/local/bin/ipsw-manager.sh
RUN chmod +x /usr/local/bin/ipsw-manager.sh

COPY webui.py /usr/local/bin/webui.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default schedule (UTC). Override at run time with -e CRON_SCHEDULE=...;
# the build arg still works for baking in a different default.
ARG CRON_SCHEDULE="0 18 * * *"
ENV CRON_SCHEDULE=${CRON_SCHEDULE}
ENV IPSW_DOWNLOAD_DIR=/downloads
ENV IPSW_CHECKSUM_ALGO=md5
ENV WEBUI_PORT=8080

EXPOSE 8080
VOLUME ["/downloads"]

ENTRYPOINT ["/entrypoint.sh"]
