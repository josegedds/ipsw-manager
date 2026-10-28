#!/usr/bin/env bash
# busybox crond fires jobs in a near-empty environment. The entrypoint saves
# the container's env to /env at startup; sourcing it here restores
# IPSW_DOWNLOAD_DIR, IPSW_CHECKSUM_ALGO, and anything else set in compose.yml.
set -a
# shellcheck source=/dev/null
source /env
set +a

exec /usr/local/bin/ipsw-manager.sh
