#!/usr/bin/env bash

set -euo pipefail

# With PUID set (see entrypoint.sh), a `docker exec` run as root switches to
# that user too, so it doesn't leave root-owned files on the NAS.
if [[ -n "${PUID:-}" && "$PUID" != 0 && $(id -u) -eq 0 ]] && command -v su-exec &>/dev/null; then
  exec su-exec "${PUID}:${PGID:-$PUID}" "$0" "$@"
fi

# Stop the whole run if it takes longer than this. A NAS that hangs (rather
# than erroring) would otherwise freeze the run, and with it every later one,
# since they'd all be skipped as "already in progress". timeout signals the
# whole process group, so curl and tee go too. 0 turns it off.
RUN_TIMEOUT="${IPSW_RUN_TIMEOUT:-12h}"
if [[ -z "${_IPSW_WATCHDOG:-}" && "$RUN_TIMEOUT" != 0 ]] && command -v timeout &>/dev/null; then
  export _IPSW_WATCHDOG=1
  exec timeout -k 60 "$RUN_TIMEOUT" "$BASH" "$0" "$@"
fi

# `docker exec` starts processes with umask 0000, which would leave the log
# and hash cache world-writable. Any other umask is left as the user set it.
[[ "$(umask)" == "0000" ]] && umask 022

# Environment variables — all have sensible defaults so the script
# works both inside Docker and standalone.
DOWNLOAD_DIR="${IPSW_DOWNLOAD_DIR:-$PWD/ipsw}"
CHECKSUM_ALGO="${IPSW_CHECKSUM_ALGO:-md5}"   # md5 or sha256
API_DEVICES="https://api.ipsw.me/v4/devices"
API_BASE="https://api.ipsw.me/v4/device"
LOG_FILE="${DOWNLOAD_DIR}/ipsw-manager.log"
# Phones picked in the web UI, as a JSON array of identifiers. If it doesn't
# exist yet, every iPhone 11 and newer is downloaded.
SELECTION_FILE="${IPSW_SELECTION_FILE:-${DOWNLOAD_DIR}/selected-devices.json}"
# Hash cache — avoids re-reading whole IPSWs from the NAS on later runs.
# Kept next to the downloads by default so it survives container updates.
# Stored as <filename>.hash, one line: "<algo> <hash> <size in bytes>"
HASH_CACHE_DIR="${IPSW_HASH_CACHE_DIR:-${DOWNLOAD_DIR}/.hash-cache}"
# Marks the download folder as the real one (see check_download_dir).
MARKER_FILE="${DOWNLOAD_DIR}/.ipsw-manager"

# Kept inside the container, not on the NAS, so they still work when the
# NAS doesn't. The web UI reads the last two.
STATE_DIR="${TMPDIR:-/tmp}"
SEEN_MARKER_FILE="${STATE_DIR}/ipsw-manager-seen-marker"   # we've seen the marker before
LAST_RUN_FILE="${STATE_DIR}/ipsw-manager-last-run"         # outcome of the last run
RUN_STARTED_FILE="${STATE_DIR}/ipsw-manager-run-started"   # removed when a run ends
# The firmware URLs ipsw.me listed at the last run that finished cleanly, and
# at the last run that got as far as looking. `--if-new` compares against them.
SYNCED_FILE="${STATE_DIR}/ipsw-manager-synced"
ATTEMPTED_FILE="${STATE_DIR}/ipsw-manager-attempted"
# After a run that didn't finish cleanly, `--if-new` waits this many minutes
# before trying the same firmware again.
RETRY_MINUTES="${IPSW_RETRY_MINUTES:-360}"

timestamp() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }

log() {
  local MSG="$1"
  printf "%s  %s\n" "$(timestamp)" "$MSG" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Looking up firmware on ipsw.me. Shared by a full run and the `--if-new`
# check below.
# ---------------------------------------------------------------------------

# Sets DEVICES, and DEVICES_FROM to where the list came from.
load_devices() {
  DEVICES=()
  DEVICES_FROM=selection
  if [[ -s "$SELECTION_FILE" ]]; then
    DEVICES=($(jq -r '.[] | select(type == "string" and test("^iPhone[0-9]+,[0-9]+$"))' "$SELECTION_FILE" 2>/dev/null || true))
  fi
  if (( ${#DEVICES[@]} == 0 )); then
    DEVICES_FROM=default
    DEVICES=($(curl -sS --fail --retry 3 --retry-delay 5 --max-time 60 "$API_DEVICES" | jq -r '
      .[]
      | select(.identifier | startswith("iPhone"))
      | select(.identifier | capture("iPhone(?<num>[0-9]+),").num | tonumber >= 12)
      | .identifier
    ' || true))
  fi
}

# Writes one JSON file per device into $META_DIR:
#   $META_DIR/<identifier>  →  {"url":…,"version":…,"sha256sum":…,"filesize":…}
fetch_meta() {
  local DEVICE="$1"
  local OUT="$META_DIR/$DEVICE"

  local JSON
  # A failed lookup is recorded as $OUT.failed so the cleanup step knows the
  # device's current IPSW is unknown, rather than no longer needed.
  if ! JSON=$(curl -sS --fail --retry 3 --retry-delay 5 --max-time 60 "$API_BASE/$DEVICE?type=ipsw"); then
    echo "⚠️  $DEVICE: Couldn't fetch firmware list from ipsw.me"
    touch "$OUT.failed"
    return
  fi

  # Extract the latest signed non-beta firmware entry as a compact JSON object
  echo "$JSON" | jq -c '
    .firmwares
    | map(select(.signed == true and (.version | contains("beta") | not)))
    | sort_by(.releasedate)
    | reverse
    | .[0]
    | if . == null then null
      else { url, version, md5sum, sha256sum, filesize: (.filesize // 0) }
      end
  ' > "$OUT" || { echo "⚠️  $DEVICE: Unreadable reply from ipsw.me"; rm -f "$OUT"; touch "$OUT.failed"; }
}

# Looks up every device in DEVICES, eight at a time.
fetch_all_meta() {
  export -f fetch_meta
  export API_BASE META_DIR
  printf "%s\n" "${DEVICES[@]}" | xargs -P 8 -I {} bash -c 'fetch_meta "$@"' _ {}
}

# The firmware files the looked-up devices need: their URLs, sorted, one each.
wanted_urls() {
  { cat "$META_DIR"/* 2>/dev/null | jq -r '.url // empty' 2>/dev/null | sort -u; } || true
}

# ---------------------------------------------------------------------------
# `ipsw-manager.sh --if-new`: start a run only if ipsw.me lists different
# firmware from the last run. Apple doesn't announce releases to anyone, so
# "run when a new iOS version is out" means asking often; this is the cheap
# way to ask. It only talks to ipsw.me, reads nothing from the NAS beyond the
# phone selection, and writes nothing to the log unless it starts a run.
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--if-new" ]]; then
  if command -v flock &>/dev/null; then
    # Leave it if an earlier check is still going (hung on the NAS, say) ...
    exec 8>"${STATE_DIR}/ipsw-manager-check.lock"
    flock -n 8 || exit 0
    # ... or if a run is. The run lock is only tested here, not kept.
    exec 9>"${STATE_DIR}/ipsw-manager.lock"
    flock -n 9 || exit 0
    exec 9>&-
  fi

  load_devices 2>/dev/null
  (( ${#DEVICES[@]} > 0 )) || exit 0   # ipsw.me unreachable — ask again next time

  META_DIR=$(mktemp -d)
  trap 'rm -rf "$META_DIR"' EXIT
  fetch_all_meta >/dev/null 2>&1 || exit 0
  # With a lookup missing, the list is incomplete and proves nothing.
  if compgen -G "$META_DIR/*.failed" >/dev/null; then exit 0; fi
  WANTED=$(wanted_urls)

  if [[ ! -f "$SYNCED_FILE" ]]; then
    REASON="First check since the container started"
  elif [[ "$WANTED" != "$(cat "$SYNCED_FILE")" ]]; then
    REASON="New firmware on ipsw.me, or the phone selection changed"
  else
    exit 0
  fi
  # A run for exactly this firmware didn't finish cleanly a short while ago.
  if [[ -f "$ATTEMPTED_FILE" && "$WANTED" == "$(cat "$ATTEMPTED_FILE")" \
        && -n "$(find "$ATTEMPTED_FILE" -mmin "-${RETRY_MINUTES}" 2>/dev/null)" ]]; then
    exit 0
  fi

  rm -rf "$META_DIR"
  trap - EXIT
  exec 8>&-
  export _IPSW_REASON="$REASON"
  exec "$BASH" "$0"
fi

# ---------------------------------------------------------------------------
# Ending a run. Every run that gets past the lock records a one-line outcome
# in $LAST_RUN_FILE. If the script dies part-way (set -e, usually because
# the NAS stopped answering) the outcome says where, and goes to stderr too,
# since the log on the NAS may be unreachable.
# ---------------------------------------------------------------------------

RESULT=""          # one-line outcome, set by finish()
ABORTED_AT=""      # set by the ERR trap: the command that failed
LOG_READY=0        # the download folder checked out, so the log may be written
CURRENT_PARTIAL="" # .partial being written, removed if the run dies
POLLER_PID=""

finish() {
  local STATUS="$1"
  RESULT="$2"
  log "$RESULT"
  exit "$STATUS"
}

on_exit() {
  local STATUS=$?
  set +e        # finish cleaning up whatever fails here
  trap - ERR
  [[ -n "$POLLER_PID" ]] && kill "$POLLER_PID" 2>/dev/null
  rm -f "${TMP_FILE:-}" "${PROGRESS_FILE:-}" "${CURL_ERR:-}"
  [[ -n "${META_DIR:-}" ]] && rm -rf "$META_DIR"

  if [[ -n "$ABORTED_AT" ]]; then
    RESULT="❌ Run aborted (status $STATUS) — $ABORTED_AT"
    printf "%s  %s\n" "$(timestamp)" "$RESULT" >&2
    # Best effort, in the background, so a hung NAS can't hang the exit.
    # fd 9 is the run lock; closing it stops a stuck write from holding it.
    if (( LOG_READY )); then
      {
        [[ -n "$CURRENT_PARTIAL" ]] && rm -f "$CURRENT_PARTIAL"
        printf "%s  %s\n" "$(timestamp)" "$RESULT" >> "$LOG_FILE"
      } </dev/null >/dev/null 2>&1 9>&- &
    fi
  fi

  if [[ -n "$RESULT" ]]; then
    printf "%s  %s\n" "$(timestamp)" "$RESULT" > "$LAST_RUN_FILE" 2>/dev/null || true
    rm -f "$RUN_STARTED_FILE"
  fi
}

on_err() { ABORTED_AT="line $1: $2. Is the download folder reachable?"; }

on_signal() {
  ABORTED_AT="stopped by $1 (timed out after ${RUN_TIMEOUT}, or the container is stopping)"
  exit 143
}

set -E   # run the ERR trap inside functions too
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
trap 'on_signal SIGTERM' TERM
trap 'on_signal SIGINT' INT
trap on_exit EXIT

# ---------------------------------------------------------------------------
# Make sure the download folder is the real one. If the NAS isn't mounted
# when the container starts, Docker binds the empty folder underneath, and
# the run would fill the host's disk with IPSWs that vanish once the NAS
# is back. A marker file in the folder tells the two apart.
# ---------------------------------------------------------------------------

check_download_dir() {
  if [[ ! -f "$MARKER_FILE" ]]; then
    if compgen -G "$DOWNLOAD_DIR/*.ipsw" >/dev/null \
      || compgen -G "$HASH_CACHE_DIR/*.hash" >/dev/null \
      || [[ -f "$LOG_FILE" ]]; then
      :  # Set up before the marker existed — adopt it.
    elif [[ -e "$SEEN_MARKER_FILE" ]]; then
      # Empty, but this container has used a marked folder before.
      RESULT="❌ ${DOWNLOAD_DIR} is empty and missing its .ipsw-manager marker — is the NAS mounted? Nothing was changed."
      printf "%s  %s\n" "$(timestamp)" "$RESULT" >&2
      echo "   If this really is a new, empty folder, create the marker and run again:" >&2
      echo "   touch ${MARKER_FILE}" >&2
      exit 1
    fi
    # Otherwise a first run into a new folder.
    mkdir -p "$DOWNLOAD_DIR"
    printf '%s\n' \
      "This folder is managed by ipsw-manager. Keep this file: it tells" \
      "ipsw-manager the folder (or the NAS it's on) is really there." > "$MARKER_FILE"
  fi
  touch "$SEEN_MARKER_FILE" 2>/dev/null || true
}

check_download_dir
mkdir -p "$HASH_CACHE_DIR"
LOG_READY=1

# Only one run at a time: the schedule and the web UI's Run Now would
# otherwise download into the same .partial files and both clean up.
# The lock is local (not on the NAS), where flock is reliable.
if command -v flock &>/dev/null; then
  exec 9>"${STATE_DIR}/ipsw-manager.lock"
  if ! flock -n 9; then
    log "⏭  Another run is already in progress — skipping this one."
    exit 0
  fi
fi

# Left behind only if this run is killed outright. The web UI then reports
# that the run didn't finish.
timestamp > "$RUN_STARTED_FILE"

[[ -n "${_IPSW_REASON:-}" ]] && log "🆕 ${_IPSW_REASON} — starting a run."

load_devices
if [[ "$DEVICES_FROM" == selection ]]; then
  log "📋 Using phones selected in the web UI..."
else
  log "📡 No saved selection — using the device list (iPhone 11 and newer)..."
fi

# Without a device list the cleanup step below would delete every IPSW.
if (( ${#DEVICES[@]} == 0 )); then
  finish 1 "❌ No devices to check (ipsw.me unreachable?) — stopping without changes."
fi

log "📱 Checking ${#DEVICES[@]} iPhone devices — fetching firmware metadata..."

# ---------------------------------------------------------------------------
# Phase 1: fetch firmware metadata for every device in parallel, then
# deduplicate by URL so each unique IPSW is only downloaded once.
#
# Results land in a temp dir as one JSON file per device (see fetch_meta).
# ---------------------------------------------------------------------------

META_DIR=$(mktemp -d)
TMP_FILE=$(mktemp)
# Name and expected size of the file being downloaded, so the web UI can
# show a percentage and time remaining. Inside the container, not on the NAS.
PROGRESS_FILE="${TMPDIR:-/tmp}/ipsw-manager-download"

fetch_all_meta
# Recorded before anything is downloaded, so a run that fails isn't started
# again by every `--if-new` check (see RETRY_MINUTES).
wanted_urls > "$ATTEMPTED_FILE" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Build a deduplicated download manifest.
#
# Keyed on URL, we accumulate:
#   URL_VERSION  — iOS version string
#   URL_SHA      — expected SHA256
#   URL_SIZE     — file size in bytes
#   URL_DEVICES  — all device identifiers sharing this URL, joined with " + "
#
# Any device whose metadata is missing or has no signed firmware is skipped
# with a warning and never appears in the manifest.
# ---------------------------------------------------------------------------

declare -A URL_VERSION=() URL_SHA=() URL_SHA_ALGO=() URL_SIZE=() URL_DEVICES=()

for DEVICE in "${DEVICES[@]}"; do
  META_FILE="$META_DIR/$DEVICE"
  [[ -f "$META_FILE" ]] || continue

  ENTRY=$(cat "$META_FILE")
  [[ "$ENTRY" == "null" || -z "$ENTRY" ]] && {
    echo "⚠️  $DEVICE: No signed non-beta firmware"
    continue
  }

  URL=$(echo "$ENTRY"     | jq -r '.url')
  [[ "$URL" == "null" || -z "$URL" ]] && {
    echo "⚠️  $DEVICE: No signed non-beta firmware"
    continue
  }

  VERSION=$(echo "$ENTRY" | jq -r '.version')
  # Pick the configured algorithm; fall back to sha256 if the field is absent
  SHA=$(echo "$ENTRY" | jq -r --arg algo "$CHECKSUM_ALGO" '
    if $algo == "md5" then (.md5sum // .sha256sum // empty)
    else (.sha256sum // .md5sum // empty)
    end
  ')
  SHA_ALGO=$(echo "$ENTRY" | jq -r --arg algo "$CHECKSUM_ALGO" '
    if $algo == "md5" and .md5sum then "md5"
    elif .sha256sum then "sha256"
    else "none"
    end
  ')
  SIZE=$(echo "$ENTRY"    | jq -r '.filesize')

  URL_VERSION["$URL"]="$VERSION"
  URL_SHA["$URL"]="$SHA"
  URL_SHA_ALGO["$URL"]="$SHA_ALGO"
  URL_SIZE["$URL"]="$SIZE"

  # Accumulate device names under this URL
  if [[ -n "${URL_DEVICES[$URL]+x}" ]]; then
    URL_DEVICES["$URL"]+=" + $DEVICE"
  else
    URL_DEVICES["$URL"]="$DEVICE"
  fi
done

UNIQUE_COUNT=${#URL_DEVICES[@]}
log "🗂  Deduplicated to $UNIQUE_COUNT unique firmware files (from ${#DEVICES[@]} device identifiers)"

# Nothing signed found usually means the API calls failed — don't let the
# cleanup step wipe the download folder on that basis.
if (( UNIQUE_COUNT == 0 )); then
  finish 0 "⚠️  No signed firmware found for the selected phones — skipping download and cleanup."
fi

# Phase 2: verify / download each unique URL one at a time.
#
# A background subshell polls the .partial file size every 0.5s and redraws
# a single progress line in place using \r — no curl progress parsing needed.
# ---------------------------------------------------------------------------

verify_checksum() {
  local FILE_PATH="$1"
  local EXPECTED_HASH="$2"
  local ALGO="$3"   # "md5" or "sha256"

  if [[ -z "$EXPECTED_HASH" || "$EXPECTED_HASH" == "null" || "$ALGO" == "none" ]]; then
    echo "⚠️  No checksum available for $(basename "$FILE_PATH"), skipping verification"
    return 0
  fi

  local ACTUAL_HASH
  case "$ALGO" in
    md5)
      if command -v md5sum &>/dev/null; then
        ACTUAL_HASH=$(md5sum "$FILE_PATH" | awk '{print $1}')
      elif command -v md5 &>/dev/null; then
        ACTUAL_HASH=$(md5 -q "$FILE_PATH")   # macOS
      else
        echo "⚠️  No md5 utility found, skipping checksum verification"
        return 0
      fi
      ;;
    sha256)
      if command -v sha256sum &>/dev/null; then
        ACTUAL_HASH=$(sha256sum "$FILE_PATH" | awk '{print $1}')
      elif command -v shasum &>/dev/null; then
        ACTUAL_HASH=$(shasum -a 256 "$FILE_PATH" | awk '{print $1}')
      else
        echo "⚠️  No sha256 utility found, skipping checksum verification"
        return 0
      fi
      ;;
    *)
      echo "⚠️  Unknown checksum algorithm '$ALGO', skipping verification"
      return 0
      ;;
  esac

  if [[ "$ACTUAL_HASH" == "$EXPECTED_HASH" ]]; then
    return 0
  else
    printf "\n❌ Checksum mismatch for %s (%s)\n" "$(basename "$FILE_PATH")" "$ALGO"
    printf "   Expected: %s\n" "$EXPECTED_HASH"
    printf "   Got:      %s\n" "$ACTUAL_HASH"
    return 1
  fi
}

draw_progress() {
  local LABEL="$1"
  local VERSION="$2"
  local DOWNLOADED="$3"
  local EXPECTED_BYTES="$4"

  local PCT=0 PCT_DISPLAY="0.0"
  if (( EXPECTED_BYTES > 0 )); then
    PCT=$(awk "BEGIN { printf \"%d\", ($DOWNLOADED / $EXPECTED_BYTES) * 100 }")
    PCT_DISPLAY=$(awk "BEGIN { printf \"%.1f\", ($DOWNLOADED / $EXPECTED_BYTES) * 100 }")
  fi

  local FILLED=$(( PCT / 5 ))
  local EMPTY=$(( 20 - FILLED ))
  local BAR
  BAR="$(printf '%0.s█' $(seq 1 "$FILLED" 2>/dev/null || true))$(printf '%0.s░' $(seq 1 "$EMPTY" 2>/dev/null || true))"

  local SIZE_INFO=""
  if (( EXPECTED_BYTES > 0 )); then
    SIZE_INFO=" $(numfmt --to=iec "$DOWNLOADED" 2>/dev/null || echo "${DOWNLOADED}B") / $(numfmt --to=iec "$EXPECTED_BYTES" 2>/dev/null || echo "${EXPECTED_BYTES}B")"
  fi

  printf "\r⬇️  %s  iOS %s  [%s] %s%%%s     " \
    "$LABEL" "$VERSION" "$BAR" "$PCT_DISPLAY" "$SIZE_INFO"
}

file_size() {
  if stat --version &>/dev/null; then
    stat -c '%s' "$1"
  else
    stat -f '%z' "$1"   # macOS
  fi
}

# A download that fails keeps the phone's current IPSW: any failure skips
# the cleanup step, which would otherwise delete it.
DOWNLOAD_FAILURES=0

download_failed() {
  local LABEL="$1" VERSION="$2" REASON="$3"
  rm -f "$CURRENT_PARTIAL"
  CURRENT_PARTIAL=""
  (( DOWNLOAD_FAILURES++ )) || true
  printf "❌ [%s]: %s (iOS %s)\n" "$LABEL" "$REASON" "$VERSION"
  printf "%s  ❌ [%s]: %s (iOS %s)\n" "$(timestamp)" "$LABEL" "$REASON" "$VERSION" >> "$LOG_FILE"
}

# Give up on a download that slows to under 1 KB/s for two minutes, rather
# than waiting forever on a stalled connection.
CURL_DOWNLOAD_OPTS=(-L --fail --silent --show-error --connect-timeout 30
  --speed-limit 1024 --speed-time 120)

download_unique() {
  local URL="$1"
  local VERSION="$2"
  local EXPECTED_HASH="$3"
  local HASH_ALGO="$4"
  local FILESIZE="$5"
  local LABEL="$6"

  local FILE_NAME FILE_PATH PARTIAL_PATH
  FILE_NAME=$(basename "$URL")
  FILE_PATH="$DOWNLOAD_DIR/$FILE_NAME"
  PARTIAL_PATH="${FILE_PATH}.partial"

  echo "$FILE_NAME" >> "$TMP_FILE"

  local CACHE_FILE="$HASH_CACHE_DIR/${FILE_NAME}.hash"

  if [[ -f "$FILE_PATH" ]]; then
    # Check the cache first — avoids reading the whole file back from the NAS.
    # Cache entry format: "<algo> <hash> <size>". The hash is "-" when there
    # was none to check, and entries from older versions have no size.
    if [[ -f "$CACHE_FILE" ]]; then
      local CACHED_ALGO CACHED_HASH CACHED_SIZE ON_DISK
      read -r CACHED_ALGO CACHED_HASH CACHED_SIZE < "$CACHE_FILE"
      if [[ "$CACHED_ALGO" == "$HASH_ALGO" && "${CACHED_HASH:--}" == "${EXPECTED_HASH:--}" ]]; then
        # A size check is cheap and catches a file that was cut short on the NAS.
        ON_DISK=$(file_size "$FILE_PATH")
        if [[ -z "$CACHED_SIZE" ]]; then
          printf "%s %s %s\n" "$HASH_ALGO" "${EXPECTED_HASH:--}" "$ON_DISK" > "$CACHE_FILE"
          CACHED_SIZE="$ON_DISK"
        fi
        if [[ "$ON_DISK" == "$CACHED_SIZE" ]]; then
          printf "✅ [%s]: Already verified via cache (iOS %s)\n" "$LABEL" "$VERSION"
          printf "%s  ✅ [%s]: Already verified via cache (iOS %s)\n" "$(timestamp)" "$LABEL" "$VERSION" >> "$LOG_FILE"
          return
        fi
        printf "⚠️  [%s]: %s is %s bytes but was %s when verified — re-downloading\n" \
          "$LABEL" "$FILE_NAME" "$ON_DISK" "$CACHED_SIZE"
        printf "%s  ⚠️  [%s]: %s changed size since it was verified — re-downloading\n" \
          "$(timestamp)" "$LABEL" "$FILE_NAME" >> "$LOG_FILE"
      fi
    fi
    # No valid cache entry — file exists but we haven't seen it before
    # (e.g. first run after an upgrade, or cache was cleared).
    # Re-download rather than reading the whole file back from the NAS.
    printf "🔄 [%s]: No valid cache entry — re-downloading iOS %s\n" "$LABEL" "$VERSION"
    rm -f "$FILE_PATH" "$CACHE_FILE"
  fi

  CURRENT_PARTIAL="$PARTIAL_PATH"

  # Spawn a background poller that redraws the progress line every 0.5s by
  # reading the .partial file size. Killed as soon as curl exits.
  (
    while true; do
      sleep 0.5
      draw_progress "$LABEL" "$VERSION" "$(file_size "$PARTIAL_PATH" 2>/dev/null || echo 0)" "$FILESIZE"
    done
  ) 9>&- &   # without the run lock, which its last sleep would hold on to briefly
  POLLER_PID=$!

  draw_progress "$LABEL" "$VERSION" 0 "$FILESIZE"
  printf '%s\n%s\n' "$FILE_NAME" "$FILESIZE" > "$PROGRESS_FILE"

  # Stream curl output through tee into the hash utility simultaneously.
  # This computes the checksum during the download rather than re-reading
  # the file from the NAS afterwards — avoids a full second pass over the
  # network and eliminates the timeout issue on slow mounts.
  local HASH_CMD
  case "$HASH_ALGO" in
    md5)    HASH_CMD="md5sum" ;;
    sha256) HASH_CMD="sha256sum" ;;
    *)      HASH_CMD="" ;;
  esac

  local CURL_OK=0
  local ACTUAL_HASH=""
  # curl writes the number of bytes it received as the last line of its
  # stderr, so the file on the NAS can be checked against it.
  CURL_ERR=$(mktemp)   # global, so on_exit can remove it

  if [[ -n "$HASH_CMD" && -n "$EXPECTED_HASH" && "$EXPECTED_HASH" != "null" ]]; then
    # tee keeps passing data on after a failed write, so the hash can still
    # match; pipefail makes the write failure fail the download.
    ACTUAL_HASH=$(set -o pipefail; curl "${CURL_DOWNLOAD_OPTS[@]}" \
        --write-out '%{stderr}%{size_download}\n' "$URL" 2>"$CURL_ERR" \
      | tee "$PARTIAL_PATH" \
      | $HASH_CMD \
      | awk '{print $1}') || CURL_OK=$?
  else
    curl "${CURL_DOWNLOAD_OPTS[@]}" --write-out '%{stderr}%{size_download}\n' \
      -o "$PARTIAL_PATH" "$URL" 2>"$CURL_ERR" || CURL_OK=$?
  fi

  kill "$POLLER_PID" 2>/dev/null
  wait "$POLLER_PID" 2>/dev/null || true
  POLLER_PID=""
  rm -f "$PROGRESS_FILE"

  local RECEIVED CURL_MSG
  RECEIVED=$(tail -n 1 "$CURL_ERR")
  CURL_MSG=$(sed '$d' "$CURL_ERR")   # curl's own error message, if any
  rm -f "$CURL_ERR"
  CURL_ERR=""

  if (( CURL_OK != 0 )); then
    printf "\n"
    [[ -n "$CURL_MSG" ]] && printf "%s\n" "$CURL_MSG" >&2
    download_failed "$LABEL" "$VERSION" "Download failed, or couldn't write it to the download folder (status $CURL_OK)"
    return
  fi

  draw_progress "$LABEL" "$VERSION" "$FILESIZE" "$FILESIZE"
  printf "\n"

  # Verify the hash captured inline during download — no second read needed.
  if [[ -n "$ACTUAL_HASH" && -n "$EXPECTED_HASH" && "$EXPECTED_HASH" != "null" \
        && "$ACTUAL_HASH" != "$EXPECTED_HASH" ]]; then
    printf "   Expected: %s\n" "$EXPECTED_HASH"
    printf "   Got:      %s\n" "$ACTUAL_HASH"
    download_failed "$LABEL" "$VERSION" "Checksum mismatch ($HASH_ALGO) — file removed, will retry next run"
    return
  fi

  # The hash only covers what curl received. Flush the file to the NAS and
  # check all of it arrived, so a write the NAS lost isn't cached as verified.
  if ! sync "$PARTIAL_PATH"; then
    download_failed "$LABEL" "$VERSION" "Couldn't write the file to the download folder"
    return
  fi
  local ON_DISK
  ON_DISK=$(file_size "$PARTIAL_PATH")
  if [[ "$ON_DISK" != "$RECEIVED" ]]; then
    download_failed "$LABEL" "$VERSION" "Saved file is $ON_DISK bytes but $RECEIVED were downloaded"
    return
  fi

  mv "$PARTIAL_PATH" "$FILE_PATH"
  CURRENT_PARTIAL=""
  # Cache the hash and size so future runs skip re-reading from the NAS
  printf "%s %s %s\n" "$HASH_ALGO" "${ACTUAL_HASH:--}" "$ON_DISK" > "$CACHE_FILE"
  printf "✅ [%s]: iOS %s downloaded and verified\n" "$LABEL" "$VERSION"
  printf "%s  ✅ [%s]: iOS %s downloaded and verified\n" "$(timestamp)" "$LABEL" "$VERSION" >> "$LOG_FILE"
}

# We hold the lock, so any .partial is left over from a run that was killed.
rm -f "$DOWNLOAD_DIR"/*.ipsw.partial

log "🚀 Downloading $UNIQUE_COUNT firmware files sequentially..."

for URL in "${!URL_DEVICES[@]}"; do
  download_unique \
    "$URL" \
    "${URL_VERSION[$URL]}" \
    "${URL_SHA[$URL]}" \
    "${URL_SHA_ALGO[$URL]}" \
    "${URL_SIZE[$URL]}" \
    "${URL_DEVICES[$URL]}"
done

FAILED=("$META_DIR"/*.failed)
if [[ -e "${FAILED[0]}" ]]; then
  finish 1 "⚠️  Firmware lookup failed for ${#FAILED[@]} phone(s) — skipping cleanup so their IPSWs aren't deleted."
fi

if (( DOWNLOAD_FAILURES > 0 )); then
  finish 1 "⚠️  ${DOWNLOAD_FAILURES} download(s) failed — skipping cleanup so those phones keep their current IPSWs."
fi

log "🧹 Cleaning up old IPSWs..."

mapfile -t KEEP_FILES < <(sort -u "$TMP_FILE")

REMOVED=0
for FILE in "$DOWNLOAD_DIR"/*.ipsw; do
  [[ -e "$FILE" ]] || continue
  BASENAME=$(basename "$FILE")
  if [[ ! " ${KEEP_FILES[*]} " =~ " $BASENAME " ]]; then
    log "🗑  Removed $BASENAME"
    rm -f "$FILE" "$HASH_CACHE_DIR/${BASENAME}.hash"
    (( REMOVED++ )) || true
  fi
done

# What ipsw.me listed is now what's in the folder.
mv -f "$ATTEMPTED_FILE" "$SYNCED_FILE" 2>/dev/null || true

finish 0 "✅ Done — ${#KEEP_FILES[@]} file(s) retained, $REMOVED removed."
