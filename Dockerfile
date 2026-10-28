FROM alpine:3.19

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    coreutils \
    ca-certificates \
    python3

COPY ipsw-manager.sh /usr/local/bin/ipsw-manager.sh
RUN chmod +x /usr/local/bin/ipsw-manager.sh

ARG CRON_SCHEDULE="0 18 * * *"
RUN mkdir -p /etc/crontabs \
  && echo "${CRON_SCHEDULE} /usr/local/bin/run-ipsw-manager.sh >> /proc/1/fd/1 2>&1" \
     > /etc/crontabs/root \
  && chmod 0600 /etc/crontabs/root

COPY run-ipsw-manager.sh /usr/local/bin/run-ipsw-manager.sh
RUN chmod +x /usr/local/bin/run-ipsw-manager.sh

COPY webui.py /usr/local/bin/webui.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV IPSW_DOWNLOAD_DIR=/downloads
ENV IPSW_CHECKSUM_ALGO=md5
ENV WEBUI_PORT=8080

EXPOSE 8080
VOLUME ["/downloads"]

ENTRYPOINT ["/entrypoint.sh"]
