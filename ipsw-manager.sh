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
CHECKSUM_ALGO="${IPSW_CHECKSUM_ALGO:-sha256}"   # sha256 or md5
API_DEVICES="https://api.ipsw.me/v4/devices"
API_BASE="https://api.ipsw.me/v4/device"
LOG_FILE="${DOWNLOAD_DIR}/ipsw-manager.log"
# Phones picked in the web UI, as a JSON array of identifiers. If it doesn't
# exist yet, every iPhone 11 and newer is downloaded.
SELECTION_FILE="${IPSW_SELECTION_FILE:-${DOWNLOAD_DIR}/selected-devices.json}"
# Hash cache — a record, per IPSW, of when the copy in the download folder
# was last read back and found good, so runs in between needn't read it again.
# Kept next to the downloads by default so it survives container updates.
# Stored as <filename>.hash (see write_cache for the format).
HASH_CACHE_DIR="${IPSW_HASH_CACHE_DIR:-${DOWNLOAD_DIR}/.hash-cache}"
# A stored file is read back and checked again once its last check is this
# many days old, which catches one that has gone bad on the NAS since. 0
# turns that off; `--reverify` checks every file now.
REVERIFY_DAYS="${IPSW_REVERIFY_DAYS:-30}"
[[ "$REVERIFY_DAYS" =~ ^[0-9]+$ ]] || REVERIFY_DAYS=30
# Reads a stored file back and checks it (see the top of that file).
VERIFY_HELPER="${IPSW_VERIFY_HELPER:-$(dirname "$0")/ipsw-verify.py}"
# A read from the download folder that returns nothing for this many seconds
# is given up on.
VERIFY_STALL=300
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
# When the stored files are next due a check (Unix time), written by a clean
# run. `--if-new` starts a run then, without having to look at the NAS.
REVERIFY_DUE_FILE="${STATE_DIR}/ipsw-manager-reverify-due"
# What the helper is reading back and how far it has got, for the web UI.
VERIFY_PROGRESS_FILE="${STATE_DIR}/ipsw-manager-verify"
# Written by the web UI's Stop button just before it signals the run, so the
# run reports that it was stopped rather than that it failed.
STOP_REQUEST_FILE="${STATE_DIR}/ipsw-manager-stop-requested"
# After a run that didn't finish cleanly, `--if-new` waits this many minutes
# (counted from when the run ended) before trying the same firmware again.
RETRY_MINUTES="${IPSW_RETRY_MINUTES:-60}"

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
# It also starts one when the stored files are due to be checked again, and
# when the last run didn't finish cleanly (a download failed, say), so a
# phone left without its IPSW gets it as soon as that can work.
# ---------------------------------------------------------------------------

recheck_due() {
  local DUE
  DUE=$(cat "$REVERIFY_DUE_FILE" 2>/dev/null) || return 1
  [[ "$DUE" =~ ^[0-9]+$ ]] && (( $(date +%s) >= DUE ))
}

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
  elif recheck_due; then
    REASON="The stored firmware is due to be checked again"
  elif [[ -f "$ATTEMPTED_FILE" ]]; then
    # Only a run that finished cleanly turns it into $SYNCED_FILE.
    REASON="Trying again: the last run didn't finish cleanly"
  else
    exit 0
  fi
  # A run for exactly this firmware didn't finish cleanly a short while ago:
  # wait RETRY_MINUTES before trying again.
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

# `ipsw-manager.sh --reverify` (the web UI's Re-verify): read every stored
# file back and check it, however recently it was last checked.
FORCE_REVERIFY=0
[[ "${1:-}" == "--reverify" ]] && FORCE_REVERIFY=1

RESULT=""          # one-line outcome, set by finish()
ABORTED_AT=""      # set by the ERR trap: the command that failed
STOPPED_BY_USER=0  # set when the web UI's Stop button ended the run
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
  rm -f "${TMP_FILE:-}" "${PROGRESS_FILE:-}" "${CURL_ERR:-}" "$VERIFY_PROGRESS_FILE"
  [[ -n "${META_DIR:-}" ]] && rm -rf "$META_DIR"

  # Stop signals the run's commands too. If one of them died first, the ERR
  # trap has already called it a failure; the request file says otherwise.
  if [[ -f "$STOP_REQUEST_FILE" ]]; then
    rm -f "$STOP_REQUEST_FILE"
    [[ -n "$ABORTED_AT" ]] && STOPPED_BY_USER=1
  fi

  if [[ -n "$ABORTED_AT" ]]; then
    if (( STOPPED_BY_USER )); then
      # run_summary is defined further down, so a run stopped early may not have it yet.
      RESULT="⏹  Run stopped from the web UI.$(declare -F run_summary >/dev/null && run_summary)"
    else
      RESULT="❌ Run aborted (status $STATUS) — $ABORTED_AT"
    fi
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

  # Still here means the run didn't finish cleanly (a clean one renames it).
  # The wait before `--if-new` retries counts from now, not from the start.
  [[ -e "$ATTEMPTED_FILE" ]] && touch "$ATTEMPTED_FILE" 2>/dev/null

  if [[ -n "$RESULT" ]]; then
    printf "%s  %s\n" "$(timestamp)" "$RESULT" > "$LAST_RUN_FILE" 2>/dev/null || true
    rm -f "$RUN_STARTED_FILE"
  fi
}

on_err() { ABORTED_AT="line $1: $2. Is the download folder reachable?"; }

on_signal() {
  if [[ -f "$STOP_REQUEST_FILE" ]]; then
    rm -f "$STOP_REQUEST_FILE"
    STOPPED_BY_USER=1
    ABORTED_AT="stopped from the web UI"
  else
    ABORTED_AT="stopped by $1 (timed out after ${RUN_TIMEOUT}, or the container is stopping)"
  fi
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
      RESULT="❌ ${DOWNLOAD_DIR} is empty and missing its .ipsw-manager marker — is the drive or network share it's on connected? Nothing was changed."
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
rm -f "$STOP_REQUEST_FILE"  # left over from a Stop that came too late

# The web UI's run history starts each run at this line.
if [[ -n "${_IPSW_REASON:-}" ]]; then
  TRIGGER="$_IPSW_REASON"
elif [[ "${_IPSW_TRIGGER:-}" == web ]]; then
  TRIGGER="Started from the web UI"
else
  TRIGGER="Started by CRON_SCHEDULE or by hand"
fi
log "▶️  Run started — ${TRIGGER}."
(( FORCE_REVERIFY )) && log "🔍 Re-verify — every stored file will be read back and checked."

# Nothing can be marked verified without it, so don't start.
if [[ ! -f "$VERIFY_HELPER" ]] || ! command -v python3 &>/dev/null; then
  finish 1 "❌ Can't check files: needs python3 and ${VERIFY_HELPER} — stopping without changes."
fi

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
#   URL_SHA      — expected checksum, or "-" if ipsw.me doesn't give one
#   URL_SHA_ALGO — md5 or sha256; "zip" (test the archive) without a checksum
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
  if [[ -z "$SHA" || "$SHA" == "null" || "$SHA_ALGO" == "none" ]]; then
    SHA="-"
    SHA_ALGO="zip"
  fi
  SHA="${SHA,,}"

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

# Phase 2: check / download each unique URL one at a time.
#
# A file counts as verified only once the copy in the download folder has been
# read back and matched its checksum. Hashing the download as it arrives
# proves what curl received, not what reached the NAS: a write that is dropped
# or damaged on the way leaves a file of the right size with the wrong
# contents. So:
#
#   - a new download is hashed as it arrives, then read back and hashed again
#     before it's given its final name;
#   - a file already there with no record of such a check is read back rather
#     than downloaded again, and so is one whose check is REVERIFY_DAYS old;
#   - only a file that fails the check is deleted and downloaded again.
#
# Reading back is done by ipsw-verify.py, which bypasses this machine's cache
# and gives up on a read that stalls.
# ---------------------------------------------------------------------------

# Prints a line and adds it to the log.
note() {
  printf "%s\n" "$1"
  printf "%s  %s\n" "$(timestamp)" "$1" >> "$LOG_FILE"
}

# Records that a stored file was read back and found good. One line:
#   v2 <algo> <hash> <size in bytes> <when checked, Unix time>
# Entries from before "v2" recorded the download's hash only, so they don't
# count and the file is checked on the next run.
write_cache() {
  local CACHE_FILE="$1" ALGO="$2" HASH="$3" SIZE="$4"
  printf "v2 %s %s %s %s\n" "$ALGO" "$HASH" "$SIZE" "$(date +%s)" > "$CACHE_FILE"
  rm -f "${CACHE_FILE%.hash}.check-failed"
}

# Records that a stored file couldn't be read back to check it, for the web
# UI, and so the next run checks it again however recent its last good check
# is. One line: <when, Unix time> <what went wrong>. Removed by write_cache.
mark_check_failed() {
  local FILE_NAME="$1" REASON="$2"
  printf "%s %s\n" "$(date +%s)" "$REASON" > "$HASH_CACHE_DIR/${FILE_NAME}.check-failed" 2>/dev/null || true
}

# Deletes a stored IPSW and everything recorded about it.
forget_file() {
  rm -f "$DOWNLOAD_DIR/$1" "$HASH_CACHE_DIR/$1.hash" "$HASH_CACHE_DIR/$1.check-failed"
}

# The oldest check among the files kept, for REVERIFY_DUE_FILE.
OLDEST_CHECK=0
track_checked() {
  if (( OLDEST_CHECK == 0 || $1 < OLDEST_CHECK )); then OLDEST_CHECK="$1"; fi
}

days_ago() {
  local DAYS=$(( ($(date +%s) - $1) / 86400 ))
  if (( DAYS <= 0 )); then echo "today"
  elif (( DAYS == 1 )); then echo "yesterday"
  else echo "$DAYS days ago"
  fi
}

# Reads a file back from the download folder and checks it. Sets:
#   V_STATUS  ok, reread-ok, mismatch, unstable or unreadable
#   V_HASH    the checksum it read ("-" if none)
#   V_INFO    read speed, or what went wrong
check_on_disk() {
  local FILE_PATH="$1" NAME="$2" EXPECTED_HASH="$3" ALGO="$4"
  local OUT RC=0
  # Without the run lock (fd 9), so a helper stuck on the NAS can't hold it.
  OUT=$(python3 "$VERIFY_HELPER" --algo "$ALGO" --expect "$EXPECTED_HASH" --name "$NAME" \
        --progress "$VERIFY_PROGRESS_FILE" --stall "$VERIFY_STALL" "$FILE_PATH" 9>&-) || RC=$?
  rm -f "$VERIFY_PROGRESS_FILE"
  V_STATUS="" V_HASH="" V_INFO=""
  read -r V_STATUS V_HASH V_INFO <<< "$OUT" || true
  if [[ -z "$V_STATUS" ]]; then
    V_STATUS="unreadable"
    V_INFO="the check stopped unexpectedly (status $RC)"
  fi
}

# How a file that passed was checked, for the log.
checked_how() {
  if [[ "$1" == "zip" ]]; then
    echo "no checksum on ipsw.me, so tested as an archive, read back at $V_INFO"
  else
    echo "$1, read back at $V_INFO"
  fi
}

# One read came back wrong and the next one right: the file is fine, the
# path to it isn't.
warn_reread() {
  note "⚠️  [$1]: The first read of $2 came back wrong and the second was right — reads from the download folder are unreliable. Check the drive or network share it's on, and its connection."
}

# The redrawn progress bar is only for someone watching a terminal
# (`docker exec -it`). Scheduled runs and Run Now write to `docker logs`,
# where it would be noise, and redrawing it starts a couple of dozen
# processes a second for the whole download. The web UI shows progress
# from the .partial file instead.
SHOW_BAR=0
[[ -t 1 ]] && SHOW_BAR=1

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
# Stored files that couldn't be read back to check. They're left alone.
CHECK_FAILURES=0

# For the summary on the run's last line (see run_summary).
N_DOWNLOADED=0     # downloads that passed their checks, replacements included
N_READBACK=0       # stored files read back: OK + damaged + CHECK_FAILURES
N_READ_OK=0
N_READ_DAMAGED=0
N_WRONG_SIZE=0     # stored files no longer the size they were when checked
N_REPLACED=0       # damaged files downloaded again
N_NOT_REPLACED=0   # damaged files deleted and not (yet) downloaded again

# What the run did, appended to its last line, e.g.
# " Downloaded 1. Read back 3: 2 OK, 1 damaged. Replaced 1 damaged file(s)."
run_summary() {
  local S="" NEW=$(( N_DOWNLOADED - N_REPLACED )) PARTS=() P
  (( NEW > 0 )) && S+=" Downloaded ${NEW}."
  if (( N_READBACK > 0 )); then
    PARTS=("${N_READ_OK} OK")
    (( N_READ_DAMAGED > 0 )) && PARTS+=("${N_READ_DAMAGED} damaged")
    (( CHECK_FAILURES > 0 )) && PARTS+=("${CHECK_FAILURES} couldn't be read")
    S+=" Read back ${N_READBACK}: ${PARTS[0]}"
    for P in "${PARTS[@]:1}"; do S+=", $P"; done
    S+="."
  fi
  (( N_WRONG_SIZE > 0 )) && S+=" ${N_WRONG_SIZE} had the wrong size."
  (( N_REPLACED > 0 )) && S+=" Replaced ${N_REPLACED} damaged file(s)."
  (( N_NOT_REPLACED > 0 )) && S+=" ${N_NOT_REPLACED} damaged file(s) deleted and not downloaded again yet."
  printf '%s' "$S"
}

# When the scheduled checks will try a run that didn't finish cleanly again.
retry_note() {
  if [[ "${CHECK_INTERVAL:-0}" =~ ^[1-9][0-9]*$ ]]; then
    printf ' Trying again in about %s min.' "$RETRY_MINUTES"
  fi
}

download_failed() {
  local LABEL="$1" VERSION="$2" REASON="$3"
  rm -f "$CURRENT_PARTIAL"
  CURRENT_PARTIAL=""
  (( DOWNLOAD_FAILURES++ )) || true
  note "❌ [$LABEL]: $REASON (iOS $VERSION)"
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

  local CACHE_FILE="$HASH_CACHE_DIR/${FILE_NAME}.hash"

  CURRENT_PARTIAL="$PARTIAL_PATH"

  # On a terminal, a background poller redraws the progress line every 0.5s
  # from the .partial file's size, and is killed as soon as curl exits.
  if (( SHOW_BAR )); then
    (
      while true; do
        sleep 0.5
        draw_progress "$LABEL" "$VERSION" "$(file_size "$PARTIAL_PATH" 2>/dev/null || echo 0)" "$FILESIZE"
      done
    ) 9>&- &   # without the run lock, which its last sleep would hold on to briefly
    POLLER_PID=$!
    draw_progress "$LABEL" "$VERSION" 0 "$FILESIZE"
  else
    printf "⬇️  %s  iOS %s  downloading %s...\n" "$LABEL" "$VERSION" "$FILE_NAME"
  fi
  printf '%s\n%s\n' "$FILE_NAME" "$FILESIZE" > "$PROGRESS_FILE"

  # Stream curl output through tee into the hash utility simultaneously.
  # This shows whether the download itself arrived intact; the saved copy
  # is checked separately below.
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

  if [[ -n "$HASH_CMD" ]]; then
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

  if [[ -n "$POLLER_PID" ]]; then
    kill "$POLLER_PID" 2>/dev/null
    wait "$POLLER_PID" 2>/dev/null || true
    POLLER_PID=""
  fi
  rm -f "$PROGRESS_FILE"

  local RECEIVED CURL_MSG
  RECEIVED=$(tail -n 1 "$CURL_ERR")
  CURL_MSG=$(sed '$d' "$CURL_ERR")   # curl's own error message, if any
  rm -f "$CURL_ERR"
  CURL_ERR=""

  if (( CURL_OK != 0 )); then
    (( SHOW_BAR )) && printf "\n"
    [[ -n "$CURL_MSG" ]] && printf "%s\n" "$CURL_MSG" >&2
    download_failed "$LABEL" "$VERSION" "Download failed, or couldn't write it to the download folder (status $CURL_OK)"
    return
  fi

  if (( SHOW_BAR )); then
    draw_progress "$LABEL" "$VERSION" "$FILESIZE" "$FILESIZE"
    printf "\n"
  fi

  # The hash captured during the download: did it arrive intact?
  if [[ -n "$HASH_CMD" && "$ACTUAL_HASH" != "$EXPECTED_HASH" ]]; then
    printf "   Expected: %s\n" "$EXPECTED_HASH"
    printf "   Got:      %s\n" "$ACTUAL_HASH"
    download_failed "$LABEL" "$VERSION" "Checksum mismatch ($HASH_ALGO) in the download — file removed, will retry next run"
    return
  fi

  # That hash only covers what curl received. Flush the file to the NAS and
  # check all of it arrived ...
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

  # ... then read it back and check its contents, before it gets its final
  # name: a file that fails never appears as an IPSW.
  printf "🔍 [%s]: Checking the saved copy of %s...\n" "$LABEL" "$FILE_NAME"
  check_on_disk "$PARTIAL_PATH" "$FILE_NAME" "$EXPECTED_HASH" "$HASH_ALGO"
  case "$V_STATUS" in
    ok) ;;
    reread-ok) warn_reread "$LABEL" "$FILE_NAME" ;;
    mismatch)
      if [[ -n "$HASH_CMD" ]]; then
        download_failed "$LABEL" "$VERSION" "The download arrived intact but the copy saved to the download folder is damaged ($HASH_ALGO read back as $V_HASH) — check the drive or network share it's saved to, and its connection. File removed, will retry next run"
      else
        download_failed "$LABEL" "$VERSION" "The saved file is damaged: $V_INFO — file removed, will retry next run"
      fi
      return
      ;;
    *)
      download_failed "$LABEL" "$VERSION" "Couldn't read the saved file back to check it: $V_INFO — file removed, will retry next run"
      return
      ;;
  esac

  mv "$PARTIAL_PATH" "$FILE_PATH"
  CURRENT_PARTIAL=""
  write_cache "$CACHE_FILE" "$HASH_ALGO" "$EXPECTED_HASH" "$ON_DISK"
  track_checked "$(date +%s)"
  (( N_DOWNLOADED++ )) || true
  note "✅ [$LABEL]: iOS $VERSION downloaded and verified in the download folder ($(checked_how "$HASH_ALGO"))"
}

# Downloads a damaged file again, counting whether that worked.
replace_damaged() {
  local BEFORE="$DOWNLOAD_FAILURES"
  download_unique "$@"
  if (( DOWNLOAD_FAILURES == BEFORE )); then
    (( N_REPLACED++ )) || true
  else
    (( N_NOT_REPLACED++ )) || true
  fi
}

# A file that's already in the download folder: read it back and check it.
# Only one that's actually damaged is downloaded again; one that can't be
# read (the NAS is offline, say) is left alone and reported.
verify_existing() {
  local VERSION="$2" EXPECTED_HASH="$3" HASH_ALGO="$4" LABEL="$6"
  local FILE_NAME FILE_PATH CACHE_FILE
  FILE_NAME=$(basename "$1")
  FILE_PATH="$DOWNLOAD_DIR/$FILE_NAME"
  CACHE_FILE="$HASH_CACHE_DIR/${FILE_NAME}.hash"

  printf "🔍 [%s]: Checking %s in the download folder...\n" "$LABEL" "$FILE_NAME"
  check_on_disk "$FILE_PATH" "$FILE_NAME" "$EXPECTED_HASH" "$HASH_ALGO"
  (( N_READBACK++ )) || true
  case "$V_STATUS" in
    ok|reread-ok)
      [[ "$V_STATUS" == "reread-ok" ]] && warn_reread "$LABEL" "$FILE_NAME"
      write_cache "$CACHE_FILE" "$HASH_ALGO" "$EXPECTED_HASH" "$(file_size "$FILE_PATH")"
      track_checked "$(date +%s)"
      (( N_READ_OK++ )) || true
      note "✅ [$LABEL]: iOS $VERSION verified in the download folder ($(checked_how "$HASH_ALGO"))"
      ;;
    mismatch)
      (( N_READ_DAMAGED++ )) || true
      if [[ "$HASH_ALGO" == "zip" ]]; then
        note "❌ [$LABEL]: $FILE_NAME is damaged: $V_INFO — downloading it again"
      else
        note "❌ [$LABEL]: $FILE_NAME is damaged ($HASH_ALGO is $V_HASH, should be $EXPECTED_HASH) — downloading it again"
      fi
      forget_file "$FILE_NAME"
      replace_damaged "$@"
      ;;
    *)
      (( CHECK_FAILURES++ )) || true
      mark_check_failed "$FILE_NAME" "$V_INFO"
      note "❌ [$LABEL]: Couldn't check $FILE_NAME: $V_INFO — left as it is, will retry next run"
      ;;
  esac
}

# Decides what a wanted file needs: nothing, a check or a download. Checks
# of files already there are queued in RECHECK and done after the downloads,
# so new firmware isn't kept waiting behind them.
RECHECK=()

process_unique() {
  local URL="$1" VERSION="$2" EXPECTED_HASH="$3" HASH_ALGO="$4" LABEL="$6"
  local FILE_NAME FILE_PATH CACHE_FILE
  FILE_NAME=$(basename "$URL")
  FILE_PATH="$DOWNLOAD_DIR/$FILE_NAME"
  CACHE_FILE="$HASH_CACHE_DIR/${FILE_NAME}.hash"

  echo "$FILE_NAME" >> "$TMP_FILE"

  if [[ ! -f "$FILE_PATH" ]]; then
    download_unique "$@"
    return
  fi

  local C_VER="" C_ALGO="" C_HASH="" C_SIZE="" C_WHEN="" ON_DISK
  if [[ -f "$CACHE_FILE" ]]; then
    read -r C_VER C_ALGO C_HASH C_SIZE C_WHEN < "$CACHE_FILE" || true
  fi
  if [[ "$C_VER" == "v2" && "$C_ALGO" == "$HASH_ALGO" && "$C_HASH" == "$EXPECTED_HASH" \
        && "$C_WHEN" =~ ^[0-9]+$ ]]; then
    # A size check is cheap and catches a file that was cut short on the NAS.
    ON_DISK=$(file_size "$FILE_PATH")
    if [[ "$ON_DISK" != "$C_SIZE" ]]; then
      note "⚠️  [$LABEL]: $FILE_NAME is $ON_DISK bytes but was $C_SIZE when it was checked — downloading it again"
      (( N_WRONG_SIZE++ )) || true
      forget_file "$FILE_NAME"
      replace_damaged "$@"
      return
    fi
    if (( ! FORCE_REVERIFY )) && [[ ! -e "$HASH_CACHE_DIR/${FILE_NAME}.check-failed" ]] \
       && (( REVERIFY_DAYS == 0 || $(date +%s) - C_WHEN < REVERIFY_DAYS * 86400 )); then
      track_checked "$C_WHEN"
      note "✅ [$LABEL]: Already verified, checked $(days_ago "$C_WHEN") (iOS $VERSION)"
      return
    fi
  fi
  # Never read back (it predates that check, or the cache was cleared), the
  # checksum on ipsw.me has changed, the last check is old, or the last
  # attempt to check it couldn't read it.
  RECHECK+=("$URL")
}

# When ipsw.me didn't answer for some phones, their firmware isn't in the
# manifest, so the loop above didn't look at their files. Cleanup is skipped,
# so the files are kept; this checks the ones that are due (or all of them,
# with --reverify) against the checksum saved when they last passed. A file
# with no saved checksum has to wait for ipsw.me.
check_unlisted() {
  local FILE FILE_NAME LABEL CACHE_FILE ON_DISK UNSAVED=0
  local C_VER C_ALGO C_HASH C_SIZE C_WHEN
  for FILE in "$DOWNLOAD_DIR"/*.ipsw; do
    [[ -e "$FILE" ]] || continue
    FILE_NAME=$(basename "$FILE")
    grep -qxF "$FILE_NAME" "$TMP_FILE" && continue   # in the manifest: done above
    CACHE_FILE="$HASH_CACHE_DIR/${FILE_NAME}.hash"
    C_VER="" C_ALGO="" C_HASH="" C_SIZE="" C_WHEN=""
    if [[ -f "$CACHE_FILE" ]]; then
      read -r C_VER C_ALGO C_HASH C_SIZE C_WHEN < "$CACHE_FILE" || true
    fi
    if [[ "$C_VER" != "v2" || ! "$C_WHEN" =~ ^[0-9]+$ || -z "$C_HASH" ]]; then
      (( FORCE_REVERIFY )) && { (( UNSAVED++ )) || true; }
      continue
    fi
    if (( ! FORCE_REVERIFY )) && [[ ! -e "$HASH_CACHE_DIR/${FILE_NAME}.check-failed" ]] \
       && (( REVERIFY_DAYS == 0 || $(date +%s) - C_WHEN < REVERIFY_DAYS * 86400 )); then
      continue
    fi
    LABEL="${FILE_NAME%%_*}"
    ON_DISK=$(file_size "$FILE")
    if [[ "$ON_DISK" != "$C_SIZE" ]]; then
      note "❌ [$LABEL]: $FILE_NAME is $ON_DISK bytes but was $C_SIZE when it was checked — deleted. A run once ipsw.me answers downloads it again if it's still current."
      (( N_WRONG_SIZE++, N_NOT_REPLACED++ )) || true
      forget_file "$FILE_NAME"
      continue
    fi
    printf "🔍 [%s]: Checking %s against its saved checksum, as ipsw.me didn't answer...\n" "$LABEL" "$FILE_NAME"
    check_on_disk "$FILE" "$FILE_NAME" "$C_HASH" "$C_ALGO"
    (( N_READBACK++ )) || true
    case "$V_STATUS" in
      ok|reread-ok)
        [[ "$V_STATUS" == "reread-ok" ]] && warn_reread "$LABEL" "$FILE_NAME"
        write_cache "$CACHE_FILE" "$C_ALGO" "$C_HASH" "$ON_DISK"
        (( N_READ_OK++ )) || true
        note "✅ [$LABEL]: $FILE_NAME verified against the checksum saved when it last passed ($(checked_how "$C_ALGO"))"
        ;;
      mismatch)
        (( N_READ_DAMAGED++, N_NOT_REPLACED++ )) || true
        note "❌ [$LABEL]: $FILE_NAME is damaged ($([[ "$C_ALGO" == zip ]] && echo "$V_INFO" || echo "$C_ALGO is $V_HASH, should be $C_HASH")) — deleted. A run once ipsw.me answers downloads it again if it's still current."
        forget_file "$FILE_NAME"
        ;;
      *)
        (( CHECK_FAILURES++ )) || true
        mark_check_failed "$FILE_NAME" "$V_INFO"
        note "❌ [$LABEL]: Couldn't check $FILE_NAME: $V_INFO — left as it is, will retry next run"
        ;;
    esac
  done
  if (( UNSAVED > 0 )); then
    note "⚠️  ${UNSAVED} stored file(s) have no saved checksum, so they can't be checked until ipsw.me answers."
  fi
}

# Nothing signed found usually means the API calls failed — don't let the
# cleanup step wipe the download folder on that basis.
if (( UNIQUE_COUNT == 0 )); then
  FAILED=("$META_DIR"/*.failed)
  if [[ -e "${FAILED[0]}" ]]; then
    check_unlisted
    finish 1 "⚠️  Firmware lookup failed for ${#FAILED[@]} phone(s) — skipping download and cleanup.$(run_summary)$(retry_note)"
  fi
  finish 0 "⚠️  No signed firmware found for the selected phones — skipping download and cleanup."
fi

# We hold the lock, so any .partial is left over from a run that was killed.
rm -f "$DOWNLOAD_DIR"/*.ipsw.partial

log "🚀 Checking $UNIQUE_COUNT firmware files, downloading what's missing..."

for URL in "${!URL_DEVICES[@]}"; do
  process_unique \
    "$URL" \
    "${URL_VERSION[$URL]}" \
    "${URL_SHA[$URL]}" \
    "${URL_SHA_ALGO[$URL]}" \
    "${URL_SIZE[$URL]}" \
    "${URL_DEVICES[$URL]}"
done

if (( ${#RECHECK[@]} > 0 )); then
  log "🔍 Reading ${#RECHECK[@]} stored file(s) back to check them..."
  for URL in "${RECHECK[@]}"; do
    verify_existing \
      "$URL" \
      "${URL_VERSION[$URL]}" \
      "${URL_SHA[$URL]}" \
      "${URL_SHA_ALGO[$URL]}" \
      "${URL_SIZE[$URL]}" \
      "${URL_DEVICES[$URL]}"
  done
fi

FAILED=("$META_DIR"/*.failed)
if [[ -e "${FAILED[0]}" ]]; then
  check_unlisted
  finish 1 "⚠️  Firmware lookup failed for ${#FAILED[@]} phone(s) — skipping cleanup so their IPSWs aren't deleted.$(run_summary)$(retry_note)"
fi

if (( DOWNLOAD_FAILURES > 0 || CHECK_FAILURES > 0 )); then
  PROBLEMS=()
  (( DOWNLOAD_FAILURES > 0 )) && PROBLEMS+=("${DOWNLOAD_FAILURES} download(s) failed")
  (( CHECK_FAILURES > 0 )) && PROBLEMS+=("${CHECK_FAILURES} stored file(s) couldn't be checked")
  SUMMARY="${PROBLEMS[0]}"
  (( ${#PROBLEMS[@]} > 1 )) && SUMMARY+=" and ${PROBLEMS[1]}"
  # Cleanup would delete the IPSW a phone has while its new one is missing.
  # (A damaged file has already been deleted, so the run's summary says so.)
  finish 1 "⚠️  ${SUMMARY} — skipping cleanup, so old IPSWs are kept for now.$(run_summary)$(retry_note)"
fi

log "🧹 Cleaning up old IPSWs..."

mapfile -t KEEP_FILES < <(sort -u "$TMP_FILE")

REMOVED=0
for FILE in "$DOWNLOAD_DIR"/*.ipsw; do
  [[ -e "$FILE" ]] || continue
  BASENAME=$(basename "$FILE")
  if [[ ! " ${KEEP_FILES[*]} " =~ " $BASENAME " ]]; then
    log "🗑  Removed $BASENAME"
    forget_file "$BASENAME"
    (( REMOVED++ )) || true
  fi
done

# What ipsw.me listed is now what's in the folder.
mv -f "$ATTEMPTED_FILE" "$SYNCED_FILE" 2>/dev/null || true

# When the oldest check reaches REVERIFY_DAYS, `--if-new` starts a run.
if (( REVERIFY_DAYS > 0 && OLDEST_CHECK > 0 )); then
  echo $(( OLDEST_CHECK + REVERIFY_DAYS * 86400 )) > "$REVERIFY_DUE_FILE" 2>/dev/null || true
else
  rm -f "$REVERIFY_DUE_FILE"
fi

finish 0 "✅ Done — ${#KEEP_FILES[@]} file(s) retained, $REMOVED removed.$(run_summary)"
