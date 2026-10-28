#!/usr/bin/env bash
set -euo pipefail

CRON_SCHEDULE="${CRON_SCHEDULE:-0 18 * * *}"
CRONTAB_FILE=/tmp/crontab

# The schedule comes from the environment, so it can be changed with
# `-e CRON_SCHEDULE=...` without rebuilding. supercronic passes the
# container's environment straight through to the job.
echo "${CRON_SCHEDULE} /usr/local/bin/ipsw-manager.sh" > "$CRONTAB_FILE"
if ! supercronic -test "$CRONTAB_FILE" >/dev/null 2>&1; then
  echo "❌ Invalid CRON_SCHEDULE: '${CRON_SCHEDULE}'" >&2
  exit 1
fi

# Optional PUID/PGID: run everything as that user so files on the NAS aren't
# owned by root. ipsw-manager.sh does the same switch for `docker exec` runs.
RUN_AS=()
if [[ -n "${PUID:-}" ]]; then
  PGID="${PGID:-$PUID}"
  if [[ ! "$PUID" =~ ^[0-9]+$ || ! "$PGID" =~ ^[0-9]+$ ]]; then
    echo "❌ PUID and PGID must be numeric (got '${PUID}:${PGID}')" >&2
    exit 1
  fi
  if (( $(id -u) != 0 )); then
    echo "❌ PUID/PGID need the container to start as root" >&2
    exit 1
  fi
  RUN_AS=(su-exec "${PUID}:${PGID}")
  mkdir -p "$IPSW_DOWNLOAD_DIR"
  if ! "${RUN_AS[@]}" test -w "$IPSW_DOWNLOAD_DIR"; then
    echo "⚠️  ${IPSW_DOWNLOAD_DIR} isn't writable by ${PUID}:${PGID} — downloads will fail." >&2
    echo "   Fix it on the host, e.g. chown -R ${PUID}:${PGID} /path/to/ipsw" >&2
  fi
fi

echo "🐳 ipsw-manager container started (Alpine $(cat /etc/alpine-release))"
echo "   Download dir : ${IPSW_DOWNLOAD_DIR}"
echo "   Checksum algo: ${IPSW_CHECKSUM_ALGO}"
echo "   Cron schedule: ${CRON_SCHEDULE} (${TZ:-UTC})"
echo "   Running as   : ${PUID:-$(id -u)}:${PGID:-$(id -g)}"
echo "   Web UI        : http://0.0.0.0:${WEBUI_PORT:-8080}"
echo ""
echo "Logs will appear here as cron fires. To run immediately:"
echo "  docker exec -it <container> ipsw-manager.sh"

# Both processes share stdout/stderr, so their output appears in `docker logs`.
# -passthrough-logs prints job output as-is instead of wrapping each line
# in supercronic's own log format.
"${RUN_AS[@]}" python3 /usr/local/bin/webui.py &
WEBUI_PID=$!
"${RUN_AS[@]}" supercronic -passthrough-logs "$CRONTAB_FILE" &
CRON_PID=$!

# tini (PID 1) delivers `docker stop` here. Interrupt the wait below and pass
# the signal on, so supercronic can let a running job finish.
STOPPING=0
trap 'STOPPING=1' TERM INT

# If either process exits on its own, stop the container so Docker's restart
# policy brings both back, rather than running on without one of them.
set +e
wait -n -p EXITED "$WEBUI_PID" "$CRON_PID"
STATUS=$?
if (( ! STOPPING )); then
  [[ "${EXITED:-}" == "$WEBUI_PID" ]] && NAME="web UI" || NAME="scheduler"
  echo "❌ The ${NAME} exited (status ${STATUS}) — stopping the container." >&2
fi
kill -TERM "$WEBUI_PID" "$CRON_PID" 2>/dev/null
wait
(( STOPPING )) && exit 0
exit "$(( STATUS == 0 ? 1 : STATUS ))"
