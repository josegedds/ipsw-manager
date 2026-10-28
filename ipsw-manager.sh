#!/usr/bin/env bash

set -euo pipefail

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
# Hash cache lives inside the container — avoids re-reading files from the
# NAS on subsequent runs. Stored as <filename>.hash, one line: "<algo> <hash>"
HASH_CACHE_DIR="/var/cache/ipsw-manager"
mkdir -p "$DOWNLOAD_DIR" "$HASH_CACHE_DIR"

log() {
  local MSG="$1"
  printf "%s  %s\n" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$MSG" | tee -a "$LOG_FILE"
}

DEVICES=()
if [[ -s "$SELECTION_FILE" ]]; then
  log "📋 Using phones selected in the web UI..."
  DEVICES=($(jq -r '.[] | select(type == "string" and test("^iPhone[0-9]+,[0-9]+$"))' "$SELECTION_FILE" 2>/dev/null || true))
fi

if (( ${#DEVICES[@]} == 0 )); then
  log "📡 No saved selection — fetching device list (iPhone 11 and newer)..."
  DEVICES=($(curl -s "$API_DEVICES" | jq -r '
    .[]
    | select(.identifier | startswith("iPhone"))
    | select(.identifier | capture("iPhone(?<num>[0-9]+),").num | tonumber >= 12)
    | .identifier
  ' || true))
fi

# Without a device list the cleanup step below would delete every IPSW.
if (( ${#DEVICES[@]} == 0 )); then
  log "❌ No devices to check (ipsw.me unreachable?) — stopping without changes."
  exit 1
fi

log "📱 Checking ${#DEVICES[@]} iPhone devices — fetching firmware metadata..."

# ---------------------------------------------------------------------------
# Phase 1: fetch firmware metadata for every device in parallel, then
# deduplicate by URL so each unique IPSW is only downloaded once.
#
# Results land in a temp dir as one JSON file per device:
#   $META_DIR/<identifier>  →  {"url":…,"version":…,"sha256sum":…,"filesize":…}
# ---------------------------------------------------------------------------

META_DIR=$(mktemp -d)
TMP_FILE=$(mktemp)
trap "rm -f $TMP_FILE; rm -rf $META_DIR" EXIT

fetch_meta() {
  local DEVICE="$1"
  local OUT="$META_DIR/$DEVICE"

  local JSON
  JSON=$(curl -s "$API_BASE/$DEVICE?type=ipsw")

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
  ' > "$OUT"
}

export -f fetch_meta
export API_BASE META_DIR

printf "%s\n" "${DEVICES[@]}" | xargs -P 8 -I {} bash -c 'fetch_meta "$@"' _ {}

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
  log "⚠️  No signed firmware found for the selected phones — skipping download and cleanup."
  exit 0
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
    # Check the local cache first — avoids reading the file from the NAS entirely.
    # Cache entry format: "<algo> <hash>"
    if [[ -f "$CACHE_FILE" ]]; then
      local CACHED_ALGO CACHED_HASH
      read -r CACHED_ALGO CACHED_HASH < "$CACHE_FILE"
      if [[ "$CACHED_ALGO" == "$HASH_ALGO" && "$CACHED_HASH" == "$EXPECTED_HASH" ]]; then
        printf "✅ [%s]: Already verified via cache (iOS %s)\n" "$LABEL" "$VERSION"
        printf "%s  ✅ [%s]: Already verified via cache (iOS %s)\n" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LABEL" "$VERSION" >> "$LOG_FILE"
        return
      fi
    fi
    # No valid cache entry — file exists but we haven't seen it before
    # (e.g. first run after an upgrade, or cache was cleared).
    # Re-download rather than reading the whole file back from the NAS.
    printf "🔄 [%s]: No cache entry — re-downloading iOS %s\n" "$LABEL" "$VERSION"
    rm -f "$FILE_PATH" "$CACHE_FILE"
  fi

  # Spawn a background poller that redraws the progress line every 0.5s by
  # reading the .partial file size. Killed as soon as curl exits.
  (
    while true; do
      sleep 0.5
      local DOWNLOADED=0
      if stat --version &>/dev/null 2>&1; then
        DOWNLOADED=$(stat -c '%s' "$PARTIAL_PATH" 2>/dev/null || echo 0)
      else
        DOWNLOADED=$(stat -f '%z' "$PARTIAL_PATH" 2>/dev/null || echo 0)
      fi
      draw_progress "$LABEL" "$VERSION" "$DOWNLOADED" "$FILESIZE"
    done
  ) &
  local POLLER_PID=$!

  draw_progress "$LABEL" "$VERSION" 0 "$FILESIZE"

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

  if [[ -n "$HASH_CMD" && -n "$EXPECTED_HASH" && "$EXPECTED_HASH" != "null" ]]; then
    ACTUAL_HASH=$(set -o pipefail; curl -L --fail --silent --show-error "$URL" \
  | tee "$PARTIAL_PATH" \
  | $HASH_CMD \
  | awk '{print $1}') || CURL_OK=$?
  else
    curl -L --fail --silent --show-error -o "$PARTIAL_PATH" "$URL" 2>&1 || CURL_OK=$?
  fi

  kill "$POLLER_PID" 2>/dev/null
  wait "$POLLER_PID" 2>/dev/null || true

  if (( CURL_OK != 0 )); then
    rm -f "$PARTIAL_PATH"
    printf "\n❌ [%s]: Download failed for iOS %s\n" "$LABEL" "$VERSION"
    printf "%s  ❌ [%s]: Download failed (iOS %s)\n" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LABEL" "$VERSION" >> "$LOG_FILE"
    return
  fi

  draw_progress "$LABEL" "$VERSION" "$FILESIZE" "$FILESIZE"
  printf "\n"

  # Verify the hash captured inline during download — no second read needed.
  local VERIFIED=0
  if [[ -n "$ACTUAL_HASH" && -n "$EXPECTED_HASH" && "$EXPECTED_HASH" != "null" ]]; then
    if [[ "$ACTUAL_HASH" == "$EXPECTED_HASH" ]]; then
      VERIFIED=1
    else
      printf "❌ [%s]: Checksum mismatch (%s)\n" "$LABEL" "$HASH_ALGO"
      printf "   Expected: %s\n" "$EXPECTED_HASH"
      printf "   Got:      %s\n" "$ACTUAL_HASH"
    fi
  else
    # No hash available — accept the file as-is
    VERIFIED=1
  fi

  if (( VERIFIED )); then
    mv "$PARTIAL_PATH" "$FILE_PATH"
    # Cache the hash so future runs skip re-reading from the NAS
    printf "%s %s\n" "$HASH_ALGO" "$ACTUAL_HASH" > "$CACHE_FILE"
    printf "✅ [%s]: iOS %s downloaded and verified\n" "$LABEL" "$VERSION"
    printf "%s  ✅ [%s]: iOS %s downloaded and verified\n" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LABEL" "$VERSION" >> "$LOG_FILE"
  else
    rm -f "$PARTIAL_PATH"
    printf "❌ [%s]: Checksum failed — file removed. Will retry next run.\n" "$LABEL"
    printf "%s  ❌ [%s]: Checksum failed (iOS %s)\n" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LABEL" "$VERSION" >> "$LOG_FILE"
  fi
}

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

log "✅ Done — ${#KEEP_FILES[@]} file(s) retained, $REMOVED removed."
