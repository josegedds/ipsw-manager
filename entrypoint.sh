#!/usr/bin/env bash
set -euo pipefail

# Save the current environment to /env so the cron wrapper can restore it.
# busybox crond doesn't source /etc/environment, so this is the reliable
# alternative.
printenv | grep -v "^_=" | sed 's/=\(.*\)/="\1"/' > /env

echo "🐳 ipsw-manager container started (Alpine $(cat /etc/alpine-release))"
echo "   Download dir : ${IPSW_DOWNLOAD_DIR}"
echo "   Checksum algo: ${IPSW_CHECKSUM_ALGO}"
echo "   Cron schedule: $(awk '{print $1, $2, $3, $4, $5}' /etc/crontabs/root)"
echo "   Web UI        : http://0.0.0.0:${WEBUI_PORT:-8080}"
echo ""
echo "Logs will appear here as cron fires. To run immediately:"
echo "  docker exec -it <container> ipsw-manager.sh"

# Start the web dashboard in the background; it inherits stdout/stderr so
# its output appears in `docker logs` alongside crond output.
python3 /usr/local/bin/webui.py &

# busybox crond flags:
#   -f  foreground (keeps the container alive)
#   -L /dev/stdout  log to stdout so 'docker logs' shows cron activity
exec crond -f -L /dev/stdout
