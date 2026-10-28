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

echo "🐳 ipsw-manager container started (Alpine $(cat /etc/alpine-release))"
echo "   Download dir : ${IPSW_DOWNLOAD_DIR}"
echo "   Checksum algo: ${IPSW_CHECKSUM_ALGO}"
echo "   Cron schedule: ${CRON_SCHEDULE} (UTC)"
echo "   Web UI        : http://0.0.0.0:${WEBUI_PORT:-8080}"
echo ""
echo "Logs will appear here as cron fires. To run immediately:"
echo "  docker exec -it <container> ipsw-manager.sh"

# Start the web dashboard in the background; it inherits stdout/stderr so
# its output appears in `docker logs` alongside the cron output.
python3 /usr/local/bin/webui.py &

# -passthrough-logs prints job output as-is instead of wrapping each line
# in supercronic's own log format.
exec supercronic -passthrough-logs "$CRONTAB_FILE"
