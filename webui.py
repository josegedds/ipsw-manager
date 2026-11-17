#!/usr/bin/env python3
"""Web dashboard for ipsw-manager. No external dependencies — stdlib only."""

import collections
import concurrent.futures
import email.utils
import glob
import http.server
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOWNLOAD_DIR = os.environ.get("IPSW_DOWNLOAD_DIR", "/downloads")
HASH_CACHE_DIR = os.environ.get("IPSW_HASH_CACHE_DIR", os.path.join(DOWNLOAD_DIR, ".hash-cache"))
LOG_FILE = os.path.join(DOWNLOAD_DIR, "ipsw-manager.log")
SELECTION_FILE = os.environ.get(
    "IPSW_SELECTION_FILE", os.path.join(DOWNLOAD_DIR, "selected-devices.json")
)
PORT = int(os.environ.get("WEBUI_PORT", "8080"))
API_DEVICES = "https://api.ipsw.me/v4/devices"
API_DEVICE = "https://api.ipsw.me/v4/device/{}?type=ipsw"
API_RELEASES = "https://api.ipsw.me/v4/releases"  # public releases of every Apple OS
APPLE_RELEASES_RSS = "https://developer.apple.com/news/releases/rss/releases.rss"  # betas and RCs
CALENDAR_CACHE_TTL = 3600  # seconds
CALENDAR_HISTORY_DAYS = 3 * 365
RC_TO_RELEASE_DAYS = 7  # public releases usually follow the release candidate by about a week
IOS_RELEASE_RE = re.compile(r"^iOS (\d+(?:\.\d+)*) \((\w+)\)$")
IOS_PRERELEASE_RE = re.compile(
    r"^iOS (\d+(?:\.\d+)*) (beta|RC|Release Candidate)(?: (\d+))?(?: \((\w+)\))?$", re.IGNORECASE
)
DEVICE_CACHE_TTL = 3600  # seconds to keep the ipsw.me device list in memory
FIRMWARE_CACHE_TTL = 3600  # seconds to keep each phone's signed firmware from ipsw.me
FIRMWARE_RETRY_AFTER = 300  # seconds before looking a phone up again after a failed lookup
IDENTIFIER_RE = re.compile(r"^iPhone(\d+),(\d+)$")
MAX_POST_BODY = 64 * 1024
# As ipsw-manager.sh reads it: a stored file is checked again once its last
# check is this many days old; 0 means only when it's downloaded.
_reverify = os.environ.get("IPSW_REVERIFY_DAYS", "30")
REVERIFY_DAYS = int(_reverify) if _reverify.isdigit() else 30
# Download links for stored IPSWs. They're public files from Apple, so the
# only cost of leaving them on is bandwidth. WEBUI_DOWNLOADS=0 turns them off.
DOWNLOAD_LINKS = os.environ.get("WEBUI_DOWNLOADS", "1").strip().lower() not in ("0", "off", "false", "no")

_run_lock = threading.Lock()
_io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="ipsw-io")
IO_TIMEOUT = 5  # seconds before giving up on a NAS read
_io_pending = {}  # function name -> Future still stuck on the NAS
_io_pending_lock = threading.Lock()
SPEED_WINDOW = 15  # seconds of .partial size samples used to work out download speed
# Written by ipsw-manager.sh while it downloads: file name, then expected bytes.
STATE_DIR = os.environ.get("TMPDIR", "/tmp")
PROGRESS_FILE = os.path.join(STATE_DIR, "ipsw-manager-download")
# Written by ipsw-verify.py while it reads a stored file back to check it:
# JSON with the file name, bytes read so far, the total and the read speed.
VERIFY_PROGRESS_FILE = os.path.join(STATE_DIR, "ipsw-manager-verify")
# Also written by ipsw-manager.sh, inside the container so they're readable
# when the NAS isn't: the last run's outcome, and the start time of a run
# that hasn't finished (left behind if it was killed).
LAST_RUN_FILE = os.path.join(STATE_DIR, "ipsw-manager-last-run")
RUN_STARTED_FILE = os.path.join(STATE_DIR, "ipsw-manager-run-started")
# Written just before Stop signals a run, so it reports being stopped, not failing.
STOP_REQUEST_FILE = os.path.join(STATE_DIR, "ipsw-manager-stop-requested")
# Log lines that end a run, for when LAST_RUN_FILE doesn't exist yet.
RUN_END_MARKERS = ("✅ Done", "Run aborted", "skipping cleanup", "stopping without changes",
                   "skipping download and cleanup", "missing its .ipsw-manager marker",
                   "Run stopped from the web UI")

_speed_samples = {}  # .partial file name -> deque of (monotonic time, size)
_speed_lock = threading.Lock()


def _with_timeout(fn, *args, fallback):
    """Run fn(*args) in a thread pool; return fallback if it doesn't finish in IO_TIMEOUT seconds.

    A call stuck on a hung mount keeps its worker thread until the mount
    answers. While one is stuck, later calls to the same function return
    fallback straight away instead of tying up another worker.
    """
    name = fn.__name__
    with _io_pending_lock:
        stuck = _io_pending.get(name)
        if stuck is not None and not stuck.done():
            return fallback
        future = _io_executor.submit(fn, *args)
        _io_pending[name] = future
    try:
        return future.result(timeout=IO_TIMEOUT)
    except concurrent.futures.TimeoutError:
        print(f"WARN: {name} timed out — the download folder isn't responding", flush=True)
        return fallback


def current_run():
    """"reverify" or "run" if ipsw-manager.sh is running, otherwise None."""
    found = None
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            args = Path(cmdline_path).read_bytes().decode("utf-8", errors="replace").split("\x00")
        except OSError:
            continue
        # `--if-new` is the scheduler's quick look at ipsw.me, not a run.
        if any(a.endswith("ipsw-manager.sh") for a in args) and "--if-new" not in args:
            if "--reverify" in args:
                return "reverify"
            found = "run"
    return found


def get_last_run(logs, running):
    """One line describing how the last run ended, or None."""
    if not running:
        try:
            started = Path(RUN_STARTED_FILE).read_text(encoding="utf-8").strip()
            return f"{started}  ❌ Run stopped before finishing (killed or timed out)"
        except OSError:
            pass
    try:
        return Path(LAST_RUN_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    for line in reversed(logs):
        if any(marker in line for marker in RUN_END_MARKERS):
            return line.strip()
    return None


LOG_TAIL_BYTES = 256 * 1024  # enough for the last 200 lines many times over
_log_tail = {"key": None, "lines": []}  # last read: (inode, size, mtime) -> lines


def get_log_tail(n=200):
    """The last n lines of the log.

    The log is never trimmed, so only its end is read, and only when it has
    changed since the last poll.
    """
    try:
        with open(LOG_FILE, "rb") as f:
            st = os.fstat(f.fileno())
            key = (st.st_ino, st.st_size, st.st_mtime_ns)
            cached = _log_tail
            if cached["key"] == key:
                return cached["lines"][-n:]
            start = max(0, st.st_size - LOG_TAIL_BYTES)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    if start > 0:
        data = data.split(b"\n", 1)[-1]  # drop the line cut in half
    lines = data.decode("utf-8", errors="replace").splitlines()[-n:]
    _log_tail.update(key=key, lines=lines)
    return lines


LOG_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$")
# The line each run starts with, and the one older versions logged for runs
# started by the scheduler's check.
RUN_START_RE = re.compile(r"^(?:▶️?\s+Run started — (.*?)\.?|🆕 (.*) — starting a run\.)$")
HISTORY_MAX = 30
HISTORY_SCAN_BYTES = 4 * 1024 * 1024  # how far back to look the first time
_history = {}
_history_lock = threading.Lock()


def _history_reset(inode, offset):
    _history.clear()
    _history.update(inode=inode, offset=offset, partial=b"", skip_first=offset > 0,
                    runs=collections.deque(maxlen=HISTORY_MAX), open=None)


def _history_line(line):
    m = LOG_LINE_RE.match(line)
    if not m:
        return
    at, msg = m.groups()
    start = RUN_START_RE.match(msg)
    if start:
        if _history["open"]:
            # Started, then nothing until the next run: it was killed outright.
            _history["runs"].append(dict(_history["open"]))
        _history["open"] = {"started": at, "ended": None, "trigger": start.group(1) or start.group(2),
                            "reverify": False, "outcome": None}
    elif msg.startswith("🔍 Re-verify") and _history["open"]:
        _history["open"]["reverify"] = True
    elif any(marker in msg for marker in RUN_END_MARKERS):
        run = _history["open"] or {"started": None, "trigger": None, "reverify": False}
        _history["runs"].append(dict(run, ended=at, outcome=msg))
        _history["open"] = None


def read_run_history():
    """(finished runs, newest first; a run the log shows as started but not ended, or None).

    Built from the log's start and end lines. Only what was added to the log
    since the last poll is read.
    """
    with _history_lock:
        try:
            with open(LOG_FILE, "rb") as f:
                st = os.fstat(f.fileno())
                if _history.get("inode") != st.st_ino or st.st_size < _history["offset"]:
                    _history_reset(st.st_ino, max(0, st.st_size - HISTORY_SCAN_BYTES))
                if st.st_size > _history["offset"]:
                    f.seek(_history["offset"])
                    data = f.read(st.st_size - _history["offset"])
                    _history["offset"] += len(data)
                    lines = (_history["partial"] + data).split(b"\n")
                    _history["partial"] = lines.pop()  # not finished being written
                    if _history["skip_first"]:
                        lines = lines[1:]  # cut in half by starting mid-file
                        _history["skip_first"] = False
                    for raw in lines:
                        _history_line(raw.decode("utf-8", errors="replace"))
        except OSError:
            pass
        if not _history:
            return [], None
        return list(reversed(_history["runs"])), dict(_history["open"]) if _history["open"] else None


def format_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"


def extract_version(name):
    m = re.search(r"_(\d+\.\d+[\.\d]*)_[0-9A-Z]+_Restore", name)
    return m.group(1) if m else "—"


FILE_IDENTIFIER_RE = re.compile(r"iPhone\d+,\d+")


def extract_identifiers(name):
    """Device identifiers at the start of an IPSW name, e.g. ["iPhone10,3", "iPhone10,6"]."""
    return FILE_IDENTIFIER_RE.findall(name.split("_", 1)[0])


def model_name(identifiers, names):
    """Marketing name for a firmware's devices, e.g. "iPhone X".

    One IPSW often covers regional variants ("iPhone X (GSM)", "iPhone X (Global)"),
    so with several identifiers the bracketed suffixes are dropped and duplicates merged.
    """
    found = [names[i] for i in identifiers if i in names]
    if not found:
        return None
    if len(identifiers) > 1:
        found = [re.sub(r"\s*\(.*?\)", "", n).strip() for n in found]
    return " / ".join(dict.fromkeys(found))


def device_names():
    """identifier -> name from the cached ipsw.me list, without blocking on the network.

    If the cache is empty or stale, refresh it in the background for the next poll.
    """
    devices = _device_cache["devices"]
    if (not devices or time.time() - _device_cache["at"] >= DEVICE_CACHE_TTL) \
            and _device_lock.acquire(blocking=False):
        _device_lock.release()

        def refresh():
            try:
                get_all_iphones()
            except Exception as exc:
                print(f"WARN: couldn't fetch device names from ipsw.me: {exc}", flush=True)

        threading.Thread(target=refresh, daemon=True).start()
    return {d["identifier"]: d["name"] for d in devices}


def download_speed(name, size):
    """Bytes per second for a .partial file, from the sizes seen as the UI polls.

    Samples from the last SPEED_WINDOW seconds are kept, so several open tabs
    polling at once don't skew the figure. None until there's enough to go on.
    """
    now = time.monotonic()
    with _speed_lock:
        samples = _speed_samples.setdefault(name, collections.deque())
        if samples and size < samples[-1][1]:
            samples.clear()  # the file shrank: the download restarted
        samples.append((now, size))
        while len(samples) > 1 and now - samples[0][0] > SPEED_WINDOW:
            samples.popleft()
        start, start_size = samples[0]
        if now - start < 2:
            return None
        return (size - start_size) / (now - start)


def read_expected_size():
    """(file name, expected bytes) of the file being downloaded, or (None, 0)."""
    try:
        name, size = Path(PROGRESS_FILE).read_text(encoding="utf-8").split()[:2]
        return name, int(size)
    except (OSError, ValueError):
        return None, 0


def read_verify_progress():
    """What ipsw-verify.py is reading back right now, or None."""
    try:
        state = json.loads(Path(VERIFY_PROGRESS_FILE).read_text(encoding="utf-8"))
        total = int(state["total"])
        done = int(state["done"])
        speed = state.get("speed")
        return {
            "name": str(state["name"]),
            # 2 while a file whose first read came back wrong is read again.
            "read_pass": int(state.get("pass", 1)),
            "percent": min(100.0, done * 100 / total) if total > 0 else None,
            "speed": speed,
            "speed_human": format_size(speed) + "/s" if isinstance(speed, (int, float)) else None,
            "eta_human": format_duration((total - done) / speed) + " left"
            if isinstance(speed, (int, float)) and speed > 0 and total > done else None,
            "total_human": None,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


_cache_entries = {}  # hash cache file name -> (its mtime, parsed entry)


def _read_cached(path, parse):
    """parse(text) of a small file in the hash cache, or None if it isn't there.

    The cache is on the NAS, so each file is only read again when it changes.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache_entries.pop(path.name, None)
        return None
    known = _cache_entries.get(path.name)
    if known and known[0] == mtime:
        return known[1]
    try:
        entry = parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry = None
    _cache_entries[path.name] = (mtime, entry)
    return entry


def _parse_hash(text):
    fields = text.split()
    return (int(fields[3]), int(fields[4])) if len(fields) >= 5 and fields[0] == "v2" else None


def _parse_check_failed(text):
    when, _, reason = text.strip().partition(" ")
    return int(when), reason


def read_cache_entry(cache):
    """(size in bytes, Unix time checked) from a hash cache file.

    None if the file isn't there or predates the read-back check: those
    entries only recorded the download's checksum, not the stored copy's.
    """
    return _read_cached(cache, _parse_hash)


def read_check_failed(path):
    """(Unix time, what went wrong) if the last attempt to check a file couldn't read it."""
    return _read_cached(path, _parse_check_failed)


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def forget_speeds(active_names):
    with _speed_lock:
        for name in set(_speed_samples) - set(active_names):
            del _speed_samples[name]


def _parse_selection(text):
    data = json.loads(text)
    return [d for d in data if isinstance(d, str)] if isinstance(data, list) else None


def scan_download_dir():
    """What's in the download folder: the firmware files, the saved phone
    selection, disk usage, and an error if the folder couldn't be listed.

    Everything here touches the NAS, so it runs under _with_timeout.
    """
    results = []
    error = None
    download_path = Path(DOWNLOAD_DIR)
    cache_path = Path(HASH_CACHE_DIR)

    verifying = read_verify_progress()

    # One listing, so a folder that's missing or can't be read is reported
    # rather than looking empty (glob hides those errors).
    try:
        # Hidden files (like macOS's ._ copies on a NAS) are skipped, as glob would.
        names = sorted(n for n in os.listdir(download_path) if not n.startswith("."))
    except OSError as exc:
        names = []
        error = f"Couldn't list the download folder: {exc.strerror or exc}"

    for path in (download_path / n for n in names if n.endswith(".ipsw")):
        try:
            stat = path.stat()
        except OSError:
            continue
        # Verified means the copy here was read back and matched, and is
        # still the size it was then.
        entry = read_cache_entry(cache_path / (path.name + ".hash"))
        verified = entry is not None and entry[0] == stat.st_size
        # The last attempt to check it couldn't read it; the next run tries again.
        failed = read_check_failed(cache_path / (path.name + ".check-failed"))
        item = {
            "name": path.name,
            "identifiers": extract_identifiers(path.name),
            "size": stat.st_size,
            "size_human": format_size(stat.st_size),
            "mtime": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "status": "complete",
            "partial": False,
            "verified": verified,
            "checked": datetime.fromtimestamp(entry[1], tz=timezone.utc).isoformat()
            if verified else None,
            # When a run will next read it back (see IPSW_REVERIFY_DAYS).
            "next_check": datetime.fromtimestamp(entry[1] + REVERIFY_DAYS * 86400, tz=timezone.utc).isoformat()
            if verified and REVERIFY_DAYS else None,
            "check_failed": {
                "at": datetime.fromtimestamp(failed[0], tz=timezone.utc).isoformat(),
                "reason": failed[1],
            } if failed else None,
            "version": extract_version(path.name),
        }
        if verifying and verifying["name"] == path.name:
            item.update(verifying, name=path.name, status="verifying")
        results.append(item)

    partials = []
    expected_name, expected_size = read_expected_size()
    for path in (download_path / n for n in names if n.endswith(".ipsw.partial")):
        try:
            stat = path.stat()
        except OSError:
            continue
        partials.append(path.name)
        ipsw_name = path.stem  # strips .partial, keeps .ipsw
        speed = download_speed(path.name, stat.st_size)
        total = expected_size if ipsw_name == expected_name and expected_size > 0 else None
        percent = min(100.0, stat.st_size * 100 / total) if total else None
        eta = (total - stat.st_size) / speed if total and speed and speed > 0 else None
        item = {
            "name": ipsw_name,
            "identifiers": extract_identifiers(ipsw_name),
            "size": stat.st_size,
            "size_human": format_size(stat.st_size),
            "mtime": None,
            "status": "downloading",
            "partial": True,  # still .partial: being downloaded, or checked before it's renamed
            "verified": False,
            "version": extract_version(ipsw_name),
            "speed": speed,
            "speed_human": format_size(speed) + "/s" if speed is not None else None,
            "total_human": format_size(total) if total else None,
            "percent": percent,
            "eta_human": format_duration(max(eta, 0)) + " left" if eta is not None else None,
        }
        # Downloaded, and now being read back before it gets its final name.
        if verifying and verifying["name"] == ipsw_name:
            item.update(verifying, name=ipsw_name, status="verifying")
        results.append(item)
    forget_speeds(partials)

    try:
        st = os.statvfs(DOWNLOAD_DIR)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    used = sum(f["size"] for f in results)
    disk = {
        "files": sum(1 for f in results if f["status"] != "downloading"),
        "used_human": format_size(used),
        "free_human": format_size(free) if free is not None else None,
    }

    return {
        "files": results,
        "error": error,
        # None until a selection is saved (or if it can't be read).
        "selection": _read_cached(Path(SELECTION_FILE), _parse_selection),
        "disk": disk,
    }


def _file_sort_key(f):
    """Newest phone first, the way the phone list is sorted."""
    newest = max((_identifier_key(i) for i in f["identifiers"]), default=(0, 0))
    return (-newest[0], -newest[1], f["name"])


def newer_firmware(identifiers, version, firmware):
    """The latest signed iOS for these phones if it's newer than version, else None."""
    latest = [firmware[i]["latest"] for i in identifiers if firmware.get(i, {}).get("latest")]
    try:
        best = max(latest, key=_version_key) if latest else None
        return best if best and _version_key(best) > _version_key(version) else None
    except ValueError:
        return None


def missing_phones(files, selection, names, firmware):
    """Selected phones with no IPSW (or download under way) in the folder.

    Returns (missing, unsigned): phones that need a download, and phones
    Apple signs no firmware for, which the run skips.
    """
    covered = {i for f in files for i in f["identifiers"]}
    missing, unsigned = [], []
    for identifier in sorted(set(selection) - covered, key=_identifier_key, reverse=True):
        info = firmware.get(identifier)
        phone = {"identifier": identifier, "model": names.get(identifier) or identifier}
        if info is not None and not info["latest"]:
            unsigned.append(phone)
            continue
        phone["latest"] = info["latest"] if info else None
        phone["checked"] = info is not None  # False until ipsw.me has been asked
        missing.append(phone)
    return missing, unsigned


def schedule_info():
    """When the scheduler next asks ipsw.me for new firmware (see entrypoint.sh)."""
    # entrypoint.sh: unset means 30, empty or 0 or "off" means no checks.
    raw = os.environ.get("CHECK_INTERVAL", "30")
    cron = os.environ.get("CRON_SCHEDULE", "").strip()
    cron = None if cron in ("", "off") else cron
    interval = int(raw) if re.fullmatch(r"[1-9][0-9]?", raw) and int(raw) <= 60 else None
    next_check = None
    if interval:
        # supercronic runs "*/N * * * *" (or "0 * * * *" for 60) in local time.
        now = datetime.now().astimezone()
        hour = now.replace(minute=0, second=0, microsecond=0)
        minutes = [0] if interval == 60 else range(0, 60, interval)
        next_check = min(
            t for t in (hour + timedelta(hours=h, minutes=m) for h in (0, 1) for m in minutes)
            if t > now
        ).isoformat()
    return {"interval": interval, "next_check": next_check, "cron": cron,
            "tz": os.environ.get("TZ") or "UTC"}


START_WAIT = 10  # seconds to wait for a run to take the run lock


def _changed_since(path, when):
    try:
        return os.stat(path).st_mtime >= when
    except OSError:
        return False


def trigger_run(reverify=False):
    """Start a run. Returns (started, message) once the run holds the run lock.

    The scheduler can start one at the same moment, so the run itself is
    the judge: one that gets the lock writes RUN_STARTED_FILE; one that
    doesn't exits 0 without writing it or LAST_RUN_FILE; one that can't
    start (the download folder looks unmounted, say) records why in
    LAST_RUN_FILE and exits non-zero.
    """
    if current_run():
        return False, "Already running"
    if not _run_lock.acquire(blocking=False):
        return False, "Already running"
    launched = time.time() - 0.05  # file times can lag the clock by a tick
    try:
        # --reverify reads every stored file back and checks it, however
        # recently it was checked; only the ones that fail are downloaded again.
        proc = subprocess.Popen(
            ["/usr/local/bin/ipsw-manager.sh"] + (["--reverify"] if reverify else []),
            start_new_session=True,
            env=dict(os.environ, _IPSW_TRIGGER="web"),  # for the run's "started" line
        )
    except OSError as exc:
        _run_lock.release()
        return False, f"Couldn't start a run: {exc}"

    def reap():
        try:
            proc.wait()
        finally:
            _run_lock.release()

    threading.Thread(target=reap, daemon=True).start()

    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        if _changed_since(RUN_STARTED_FILE, launched):
            return True, "Started"
        status = proc.poll()
        if status is not None:
            if _changed_since(LAST_RUN_FILE, launched):
                if status == 0:
                    return True, "Started"  # and already finished
                try:
                    why = Path(LAST_RUN_FILE).read_text(encoding="utf-8").strip()
                    why = re.sub(r"^\S+Z\s+", "", why)
                except OSError:
                    why = f"it stopped with status {status}"
                return False, f"Couldn't start the run: {why}"
            if status == 0:
                return False, "Another run is already in progress"
            return False, f"Couldn't start the run (status {status})"
        time.sleep(0.1)
    return True, "Started"  # still going, perhaps held up by a slow NAS


def _processes():
    """pid -> (parent pid, command line) for every process in the container."""
    procs = {}
    for stat_path in glob.glob("/proc/[0-9]*/stat"):
        pid = int(stat_path.split("/")[2])
        try:
            stat = Path(stat_path).read_text(encoding="utf-8", errors="replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])  # the name in brackets may hold spaces
            args = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace").split("\x00")
        except (OSError, ValueError, IndexError):
            continue
        procs[pid] = (ppid, args)
    return procs


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def stop_run():
    """Stop the run in progress. Returns (stopping, message).

    Every process of the run gets SIGTERM: the script, its timeout wrapper,
    and whatever they're running (curl, the read-back check). Signalling the
    script alone would leave it waiting for a download to finish first. The
    script then removes its half-written file and records that it was stopped.
    """
    procs = _processes()
    roots = [pid for pid, (_, args) in procs.items()
             if any(a.endswith("ipsw-manager.sh") for a in args) and "--if-new" not in args]
    if not roots:
        _remove(STOP_REQUEST_FILE)
        return False, "No run is in progress"
    children = collections.defaultdict(list)
    for pid, (ppid, _) in procs.items():
        children[ppid].append(pid)
    targets, stack = [], list(roots)
    while stack:
        pid = stack.pop()
        if pid not in targets and pid != os.getpid():
            targets.append(pid)
            stack.extend(children[pid])
    try:
        Path(STOP_REQUEST_FILE).write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError as exc:
        return False, f"Couldn't stop the run: {exc}"
    signalled = denied = 0
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled += 1
        except ProcessLookupError:
            pass  # already gone
        except PermissionError:
            denied += 1
    if not signalled:
        _remove(STOP_REQUEST_FILE)
        if denied:
            return False, ("Couldn't stop the run: it belongs to another user. A run started with "
                           "docker exec as root can only be stopped the same way.")
        return False, "No run is in progress"
    return True, "Stopping"


def config_info():
    """The settings this container runs with, for the web UI's Settings panel."""
    schedule = schedule_info()
    timeout = os.environ.get("IPSW_RUN_TIMEOUT", "12h")
    return {
        "version": os.environ.get("IPSW_MANAGER_VERSION", "dev"),
        "download_dir": DOWNLOAD_DIR,
        "check_interval": schedule["interval"],
        "cron": schedule["cron"],
        "tz": schedule["tz"],
        "reverify_days": REVERIFY_DAYS,
        "retry_minutes": os.environ.get("IPSW_RETRY_MINUTES", "60"),
        "run_timeout": None if timeout == "0" else timeout,
        "checksum": os.environ.get("IPSW_CHECKSUM_ALGO", "sha256"),
        "user": f"{os.getuid()}:{os.getgid()}",
        "port": PORT,
        "download_links": DOWNLOAD_LINKS,
    }


_device_cache = {"at": 0.0, "devices": []}
_device_lock = threading.Lock()


def _identifier_key(identifier):
    m = IDENTIFIER_RE.match(identifier)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def get_all_iphones():
    """Every iPhone known to ipsw.me, newest first. Cached for DEVICE_CACHE_TTL."""
    with _device_lock:
        if _device_cache["devices"] and time.time() - _device_cache["at"] < DEVICE_CACHE_TTL:
            return _device_cache["devices"]
        req = urllib.request.Request(API_DEVICES, headers={"User-Agent": "ipsw-manager"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        devices = sorted(
            (
                {"identifier": d["identifier"], "name": d.get("name") or d["identifier"]}
                for d in data
                if IDENTIFIER_RE.match(d.get("identifier", ""))
            ),
            key=lambda d: _identifier_key(d["identifier"]),
            reverse=True,
        )
        _device_cache.update(at=time.time(), devices=devices)
        return devices


def _fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "ipsw-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _version_key(version):
    return tuple(int(p) for p in version.split("."))


def _ipsw_releases(since):
    """Public iOS releases from ipsw.me, one per version and day."""
    events = {}
    for day in _fetch_json(API_RELEASES):
        for release in day.get("releases", []):
            if release.get("type") != "iOS":
                continue
            m = IOS_RELEASE_RE.match(release.get("name", ""))
            if not m:
                continue
            at = datetime.fromisoformat(release["date"].replace("Z", "+00:00"))
            if at < since:
                continue
            version, build = m.groups()
            event = events.setdefault((version, at.date()), {
                "kind": "release", "version": version, "title": f"iOS {version}",
                "at": at.isoformat(), "builds": [],
            })
            event["at"] = min(event["at"], at.isoformat())
            event["builds"].append(build)
    return list(events.values())


def _apple_prereleases(since):
    """iOS betas and release candidates from Apple's developer releases feed."""
    req = urllib.request.Request(APPLE_RELEASES_RSS, headers={"User-Agent": "ipsw-manager"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read())
    events = []
    for item in root.iter("item"):
        m = IOS_PRERELEASE_RE.match((item.findtext("title") or "").strip())
        if not m:
            continue  # other OSes, Xcode, and public releases (ipsw.me has those)
        try:
            at = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if at < since:
            continue
        version, stage, number, build = m.groups()
        kind = "beta" if stage.lower() == "beta" else "rc"
        label = "beta" if kind == "beta" else "RC"
        events.append({
            "kind": kind, "version": version,
            "title": f"iOS {version} {label}" + (f" {number}" if number else ""),
            "at": at.isoformat(), "builds": [build] if build else [],
        })
    return events


# Each phone's signed firmware, shared by the calendar (which versions can be
# installed now) and the firmware table (missing phones, newer iOS out).
_firmware = {}         # identifier -> (time fetched, {"signed": set, "latest": str or None})
_firmware_failed = {}  # identifier -> time of the last failed lookup
_firmware_lock = threading.Lock()
_firmware_refreshing = [False]


def _lookup_firmware(identifier):
    """Signed versions for a phone, and the one a run downloads.

    "latest" matches ipsw-manager.sh: the newest signed non-beta by release date.
    """
    data = _fetch_json(API_DEVICE.format(identifier))
    signed = [f for f in data.get("firmwares", []) if f.get("signed") and f.get("version")]
    public = [f for f in signed if "beta" not in f["version"]]
    latest = max(public, key=lambda f: f.get("releasedate") or "", default=None)
    return {"signed": {f["version"] for f in signed}, "latest": latest["version"] if latest else None}


def _refresh_firmware(identifiers):
    def one(identifier):
        try:
            info = _lookup_firmware(identifier)
        except Exception:
            with _firmware_lock:
                _firmware_failed[identifier] = time.time()
            return
        with _firmware_lock:
            _firmware[identifier] = (time.time(), info)
            _firmware_failed.pop(identifier, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, identifiers))


def signed_firmware(identifiers, wait=False):
    """identifier -> {"signed", "latest"} for the phones ipsw.me has answered for.

    Each phone is looked up again after FIRMWARE_CACHE_TTL. With wait, stale
    ones are looked up before returning; otherwise in the background, for the
    next poll, so a page load never waits on ipsw.me.
    """
    identifiers = list(dict.fromkeys(identifiers))
    now = time.time()
    with _firmware_lock:
        stale = [i for i in identifiers
                 if (i not in _firmware or now - _firmware[i][0] >= FIRMWARE_CACHE_TTL)
                 and now - _firmware_failed.get(i, 0) >= FIRMWARE_RETRY_AFTER]
        start_background = bool(stale) and not wait and not _firmware_refreshing[0]
        if start_background:
            _firmware_refreshing[0] = True
    if stale and wait:
        _refresh_firmware(stale)
    elif start_background:
        def background():
            try:
                _refresh_firmware(stale)
            finally:
                with _firmware_lock:
                    _firmware_refreshing[0] = False

        threading.Thread(target=background, daemon=True).start()
    with _firmware_lock:
        return {i: _firmware[i][1] for i in identifiers if i in _firmware}


_sources_cache = {"at": 0.0, "data": None}
_sources_lock = threading.Lock()


def _calendar_sources():
    """(public releases, betas and RCs, errors), cached for CALENDAR_CACHE_TTL.

    Each source can fail on its own. If both do, nothing is cached and the
    next request tries again.
    """
    with _sources_lock:
        if _sources_cache["data"] and time.time() - _sources_cache["at"] < CALENDAR_CACHE_TTL:
            return _sources_cache["data"]
        since = datetime.now(timezone.utc) - timedelta(days=CALENDAR_HISTORY_DAYS)
        errors = []
        try:
            releases = _ipsw_releases(since)
        except Exception as exc:
            releases = []
            errors.append(f"Couldn't load releases from ipsw.me: {exc}")
        try:
            prereleases = _apple_prereleases(since)
        except Exception as exc:
            prereleases = []
            errors.append(f"Couldn't load betas from Apple: {exc}")
        if not releases and not prereleases:
            raise RuntimeError("; ".join(errors))
        data = (releases, prereleases, errors)
        _sources_cache.update(at=time.time(), data=data)
        return data


def get_calendar():
    """Release calendar: public releases, betas, RCs, and an estimate where an RC is out.

    The releases are cached, and so is each phone's signed firmware, so after
    the phone selection changes only the newly ticked phones are looked up.
    """
    releases, prereleases, errors = _calendar_sources()
    errors = list(errors)
    events = [dict(e) for e in releases + prereleases]

    # Versions the selected phones can install now.
    selected = _with_timeout(read_selection, fallback=None)
    if selected is None:
        try:
            selected = default_selection(get_all_iphones())
        except Exception as exc:
            selected = []
            errors.append(f"Couldn't check which versions are signed: {exc}")
    selected = [i for i in selected if IDENTIFIER_RE.match(i)]
    firmware = signed_firmware(selected, wait=True)
    if len(firmware) < len(set(selected)):
        unknown = len(set(selected)) - len(firmware)
        errors.append(f"Couldn't check which versions are signed for {unknown} of your phones; ipsw.me didn't answer")
    signed = set().union(*(info["signed"] for info in firmware.values()))
    for event in events:
        event["signed"] = event["kind"] == "release" and event["version"] in signed

    # An RC with no public release yet: expect it about a week later.
    released = {e["version"] for e in releases}
    rcs = [e for e in events if e["kind"] == "rc" and e["version"] not in released]
    if rcs:
        latest = max(rcs, key=lambda e: (_version_key(e["version"]), e["at"]))
        expected = datetime.fromisoformat(latest["at"]) + timedelta(days=RC_TO_RELEASE_DAYS)
        events.append({
            "kind": "expected", "version": latest["version"],
            "title": f"iOS {latest['version']} expected",
            "at": max(expected, datetime.now(timezone.utc)).isoformat(),
            "builds": [], "signed": False,
        })

    events.sort(key=lambda e: e["at"])
    return {"events": events, "errors": errors, "fetched_at": datetime.now(timezone.utc).isoformat()}


def default_selection(devices):
    """Matches the script's behaviour when no selection has been saved: iPhone 11 and newer."""
    return [d["identifier"] for d in devices if _identifier_key(d["identifier"])[0] >= 12]


def read_selection():
    """Saved identifiers, or None if the user hasn't saved a selection yet."""
    try:
        data = json.loads(Path(SELECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [d for d in data if isinstance(d, str)] if isinstance(data, list) else None


def write_selection(identifiers):
    path = Path(SELECTION_FILE)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sorted(identifiers, key=_identifier_key), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# A raw string, so the JavaScript below is written exactly as the browser sees it.
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>IPSW Manager</title>
<link rel="icon" id="favicon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='3' y='1' width='10' height='14' rx='2.5' fill='%233fb950'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--surface-2:#0d1117;--border:#30363d;--border-soft:#21262d;
  --text:#e6edf3;--muted:#8b949e;--faint:#6e7681;--hover:rgba(255,255,255,.03);--other-day:rgba(0,0,0,.2);
  --green:#3fb950;--green-bg:rgba(63,185,80,.13);--btn-green:#238636;--btn-green-hover:#2ea043;
  --blue:#58a6ff;--blue-bg:rgba(88,166,255,.13);--orange:#d29922;--orange-bg:rgba(210,153,34,.13);
  --red:#f85149;--red-bg:rgba(248,81,73,.12);--gray-bg:rgba(139,148,158,.15);
  --btn-gray:#21262d;--btn-gray-hover:#30363d;
}
@media (prefers-color-scheme:light){
  :root{
    --bg:#f6f8fa;--surface:#ffffff;--surface-2:#f6f8fa;--border:#d0d7de;--border-soft:#eaeef2;
    --text:#1f2328;--muted:#59636e;--faint:#6e7781;--hover:rgba(0,0,0,.025);--other-day:rgba(0,0,0,.03);
    --green:#1a7f37;--green-bg:rgba(26,127,55,.1);--btn-green:#1f883d;--btn-green-hover:#1a7f37;
    --blue:#0969da;--blue-bg:rgba(9,105,218,.1);--orange:#9a6700;--orange-bg:rgba(154,103,0,.1);
    --red:#cf222e;--red-bg:rgba(207,34,46,.08);--gray-bg:rgba(89,99,110,.1);
    --btn-gray:#f6f8fa;--btn-gray-hover:#eaeef2;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none!important}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;position:sticky;top:0;z-index:10}
.hdr-left{display:flex;align-items:center;flex-wrap:wrap;gap:4px 10px;min-width:0}
h1{font-size:16px;font-weight:600;letter-spacing:-.3px;white-space:nowrap}
#dot{width:9px;height:9px;border-radius:50%;background:var(--green);flex-shrink:0;transition:background .3s}
#dot.running{background:#f0883e;animation:pulse 1.2s ease-in-out infinite}
#dot.error{background:var(--red)}
#status-lbl{font-size:12px;color:var(--muted)}
.hdr-meta{font-size:11px;color:var(--faint)}
.spacer{flex:1}
.hdr-actions{display:flex;gap:8px}
button{padding:7px 15px;border-radius:6px;border:1px solid transparent;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit;white-space:nowrap;transition:opacity .15s,background .15s}
.btn-green{background:var(--btn-green);color:#fff;border-color:var(--btn-green)}
.btn-green:hover:not(:disabled){background:var(--btn-green-hover)}
.btn-gray{background:var(--btn-gray);color:var(--text);border-color:var(--border)}
.btn-gray:hover:not(:disabled){background:var(--btn-gray-hover)}
.btn-sm{font-size:12px;padding:4px 10px}
button:disabled{opacity:.4;cursor:not-allowed}
button:focus-visible,input:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
main{max-width:1120px;margin:0 auto;padding:24px;display:flex;flex-direction:column;gap:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.card-hdr{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.card-title{font-size:13px;font-weight:600}
.card-meta{font-size:12px;color:var(--muted)}
.banner{padding:10px 18px;font-size:12px;border-bottom:1px solid var(--border-soft);border-left:3px solid var(--muted);display:flex;gap:4px 10px;flex-wrap:wrap;line-height:1.5}
.banner-label{font-weight:600;white-space:nowrap}
.banner-label span{font-weight:400;color:var(--muted);cursor:help}
.sev-ok{border-left-color:var(--green)}
.sev-warn{border-left-color:var(--orange);background:var(--orange-bg)}
.sev-err{border-left-color:var(--red);background:var(--red-bg)}
.sev-info{border-left-color:var(--blue)}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:9px 16px;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border-soft);font-size:12px;white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid var(--border-soft);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--hover)}
.muted{color:var(--muted)}
.mono{font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:12px}
.sub{display:block;font-size:11px;color:var(--muted);margin-top:3px;max-width:340px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub.warn{color:var(--orange)}
.ios,.ios .sub{white-space:nowrap}
.row-toggle{all:unset;cursor:pointer;font-weight:500;display:inline-flex;align-items:baseline;gap:6px;border-radius:4px}
.row-toggle:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.row-toggle .chev{display:inline-block;color:var(--faint);transition:transform .15s;width:8px}
.row-toggle[aria-expanded="true"] .chev{transform:rotate(90deg)}
tr.detail td{background:var(--surface-2);padding:10px 16px 12px 34px}
tr.detail:hover td{background:var(--surface-2)}
.detail dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:12px}
.detail dt{color:var(--muted)}
.detail dd{min-width:0;overflow-wrap:break-word}
tr.missing td{color:var(--muted)}
tr.missing td:first-child{color:var(--text);font-weight:500;padding-left:30px}
.speed{display:block;color:var(--blue);font-size:11px;margin-top:2px}
.bar{height:4px;width:160px;max-width:100%;background:var(--border-soft);border-radius:2px;margin-top:5px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--blue);transition:width .5s}
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.badge-green{background:var(--green-bg);color:var(--green)}
.badge-blue{background:var(--blue-bg);color:var(--blue);animation:pulse 1.5s ease-in-out infinite}
.badge-orange{background:var(--orange-bg);color:var(--orange)}
.badge-gray{background:var(--gray-bg);color:var(--muted)}
.card-foot{padding:10px 18px;font-size:12px;color:var(--muted);border-top:1px solid var(--border-soft)}
.log{background:var(--surface-2);padding:14px 16px;font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:11px;line-height:1.65;max-height:380px;overflow-y:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.log .ts{color:var(--faint)}
.log .ok{color:var(--green)}
.log .err{color:var(--red)}
.log .warn{color:var(--orange)}
.log .info{color:var(--blue)}
.log .dim{color:var(--muted)}
.empty{text-align:center;padding:36px;color:var(--faint);font-size:13px}
.dev-tools{padding:12px 18px;border-bottom:1px solid var(--border-soft);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dev-tools input{flex:1;min-width:160px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:13px;font-family:inherit}
.dev-tools button{font-size:12px;padding:5px 11px}
.dev-note{padding:10px 18px 0;font-size:12px;color:var(--muted)}
.dev-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:2px 12px;padding:12px 18px 16px;max-height:420px;overflow-y:auto}
.dev-grid label{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:5px;font-size:13px;cursor:pointer}
.dev-grid label:hover{background:var(--hover)}
.dev-grid input{accent-color:var(--btn-green)}
.dev-grid .mono{color:var(--muted);font-size:11px;margin-left:auto}
.cal-nav{display:flex;align-items:center;gap:6px}
.cal-month{font-size:13px;font-weight:600;min-width:130px;text-align:center}
.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
.cal-dow{padding:7px 8px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border-soft)}
.cal-day{min-height:86px;min-width:0;padding:5px 6px;border-right:1px solid var(--border-soft);border-bottom:1px solid var(--border-soft);display:flex;flex-direction:column;gap:3px}
.cal-day:nth-child(7n){border-right:none}
.cal-day.other{background:var(--other-day)}
.cal-day.other .cal-num{color:var(--faint);opacity:.6}
.cal-num{font-size:11px;color:var(--muted);align-self:flex-start;padding:0 5px;border-radius:9px}
.cal-day.today .cal-num{background:var(--blue);color:var(--surface);font-weight:600}
.ev{font-size:11px;padding:2px 6px;border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border:1px solid transparent}
.ev-release{background:var(--green-bg);color:var(--green)}
.ev-beta{background:var(--blue-bg);color:var(--blue)}
.ev-rc{background:var(--orange-bg);color:var(--orange)}
.ev-expected{border:1px dashed var(--muted);color:var(--text)}
.cal-foot{padding:12px 18px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;font-size:12px;color:var(--muted);border-bottom:1px solid var(--border-soft)}
.cal-foot .ev{display:inline-block}
.cal-err{padding:10px 18px;font-size:12px;color:var(--orange);border-bottom:1px solid var(--border-soft)}
.cal-list td:first-child{color:var(--muted);white-space:nowrap}
.cal-list .ev{display:inline-block;vertical-align:middle;margin-right:6px}
.btn-red{background:transparent;color:var(--red);border-color:var(--red)}
.btn-red:hover:not(:disabled){background:var(--red-bg)}
#btn-notify[aria-pressed="true"]{border-color:var(--blue)}
.summary{padding:10px 18px;display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--border-soft)}
.chip{font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;white-space:nowrap}
.chip-ok{background:var(--green-bg);color:var(--green)}
.chip-warn{background:var(--orange-bg);color:var(--orange)}
.chip-err{background:var(--red-bg);color:var(--red)}
.chip-info{background:var(--blue-bg);color:var(--blue)}
.chip-muted{background:var(--gray-bg);color:var(--muted)}
.dl-btn{margin-top:10px;color:var(--text)}
.dl-btn:hover{background:var(--btn-gray-hover)}
a.btn-gray{display:inline-block;text-decoration:none;font-weight:500;border:1px solid var(--border);border-radius:6px;white-space:nowrap}
.log-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;background:var(--btn-gray);color:var(--muted);font-size:12px;padding:4px 10px}
.seg button+button{border-left:1px solid var(--border)}
.seg button[aria-pressed="true"]{background:var(--btn-gray-hover);color:var(--text)}
.out-ok{color:var(--green)}
.out-warn{color:var(--orange)}
.out-err{color:var(--red)}
.out-info{color:var(--text)}
.hist td{vertical-align:top}
.hist td:first-child{white-space:nowrap;width:1%}
.hist .outcome{overflow-wrap:anywhere}
.hist .sub{max-width:none;white-space:normal}
.tag{font-size:10px;font-weight:600;padding:1px 6px;border-radius:10px;white-space:nowrap;margin-left:6px;vertical-align:1px}
.tag-stored{background:var(--green-bg);color:var(--green)}
.tag-unsigned{background:var(--gray-bg);color:var(--muted)}
.dev-grid label.unsigned>span:first-of-type{color:var(--muted)}
details.card>summary{list-style:none;cursor:pointer}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary .chev{display:inline-block;color:var(--faint);transition:transform .15s;margin-right:8px}
details.card[open]>summary .chev{transform:rotate(90deg)}
details.card:not([open])>summary{border-bottom:none}
.settings dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;padding:14px 18px;font-size:13px}
.settings dt{color:var(--muted)}
.settings dd{min-width:0;overflow-wrap:anywhere}
.settings .note{padding:0 18px 14px;font-size:12px;color:var(--muted)}
@media (max-width:640px){
  .summary,.settings dl,.settings .note{padding-left:14px;padding-right:14px}
  .settings dl{grid-template-columns:1fr;gap:0}
  .settings dd{margin-bottom:8px}
  header{padding:10px 16px}
  main{padding:16px;gap:16px}
  .hide-sm{display:none}
  th,td{padding:9px 7px}
  th:first-child,td:first-child{padding-left:12px}
  .badge{padding:3px 7px}
  tr.detail td{padding:10px 10px 12px 26px}
  tr.missing td:first-child{padding-left:26px}
  .sub{max-width:180px;white-space:normal}
  .bar{width:90px}
  .card-hdr,.banner,.dev-tools,.cal-foot,.cal-err,.card-foot{padding-left:14px;padding-right:14px}
  .cal-day{min-height:54px;padding:3px}
  .cal-day .ev{font-size:0;height:6px;padding:0;border-radius:3px}
  .cal-month{min-width:0}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <div id="dot"></div>
    <h1>IPSW Manager</h1>
    <span id="status-lbl">Loading…</span>
    <span id="next-check" class="hdr-meta"></span>
    <span id="poll-ts" class="hdr-meta hide-sm"></span>
  </div>
  <div class="spacer"></div>
  <div class="hdr-actions">
    <button id="btn-run" class="btn-green" onclick="doRun(false)" title="Download new firmware, check files that are due a check, and delete IPSWs that are no longer needed">&#9654; Run Now</button>
    <button id="btn-verify" class="btn-gray" onclick="doRun(true)" title="Run Now, and also read every stored file back and check it, however recently it was checked">&#8635; Re-verify</button>
    <button id="btn-stop" class="btn-red" onclick="doStop()" hidden title="Stop the run in progress">&#9632; Stop</button>
    <button id="btn-notify" class="btn-gray" onclick="toggleNotify()" aria-pressed="false" hidden>&#128277;</button>
  </div>
</header>
<main>
  <section class="card" aria-labelledby="files-title">
    <div class="card-hdr">
      <h2 class="card-title" id="files-title">Downloaded Firmware</h2>
      <span class="card-meta" id="disk"></span>
    </div>
    <div id="summary" class="summary" aria-live="polite" hidden></div>
    <div id="folder-err" class="banner sev-err" role="alert" hidden></div>
    <div id="last-run" class="banner" hidden></div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>iOS</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="files-body">
          <tr><td colspan="3" class="empty">Loading…</td></tr>
        </tbody>
      </table>
    </div>
    <div id="unsigned-note" class="card-foot" hidden></div>
  </section>
  <section class="card" aria-labelledby="cal-title">
    <div class="card-hdr">
      <h2 class="card-title" id="cal-title">Release Calendar</h2>
      <div class="cal-nav">
        <button class="btn-gray btn-sm" onclick="shiftMonth(-1)" aria-label="Previous month">&#8249;</button>
        <span class="cal-month" id="cal-month" aria-live="polite"></span>
        <button class="btn-gray btn-sm" onclick="shiftMonth(1)" aria-label="Next month">&#8250;</button>
        <button class="btn-gray btn-sm" onclick="shiftMonth(0)">Today</button>
      </div>
    </div>
    <div id="cal-errors"></div>
    <div class="cal-grid" id="cal-grid"></div>
    <div class="cal-foot">
      <span class="ev ev-release">Release</span>
      <span class="ev ev-beta">Beta</span>
      <span class="ev ev-rc">Release candidate</span>
      <span class="ev ev-expected">Expected</span>
      <span>✓ = signed for your phones now. Apple doesn't announce dates: "expected" is a week after the release candidate.</span>
    </div>
    <div class="table-wrap">
      <table class="cal-list">
        <thead><tr><th>Date</th><th>Release</th><th>Build</th><th></th></tr></thead>
        <tbody id="cal-list"><tr><td colspan="4" class="empty">Loading releases…</td></tr></tbody>
      </table>
    </div>
  </section>
  <section class="card" aria-labelledby="dev-title">
    <div class="card-hdr">
      <h2 class="card-title" id="dev-title">Phones to Download</h2>
      <span class="card-meta" id="dev-count"></span>
    </div>
    <div class="dev-tools">
      <input id="dev-filter" type="search" placeholder="Filter, e.g. 15 Pro or iPhone8" aria-label="Filter phones" oninput="renderDevices()">
      <button id="btn-all" class="btn-gray" onclick="setVisible(true)">Select all</button>
      <button id="btn-none" class="btn-gray" onclick="setVisible(false)">Select none</button>
      <button class="btn-gray" onclick="resetDefault()" title="Tick iPhone 11 and newer: what's downloaded until a selection is saved">Reset to default</button>
      <button id="btn-save" class="btn-green" onclick="saveDevices()" disabled>Save</button>
    </div>
    <div class="dev-note" id="dev-note">Unticked phones' firmware is deleted on the next run. Phones Apple no longer signs firmware for are skipped.</div>
    <div class="dev-grid" id="dev-grid"><div class="empty">Loading…</div></div>
  </section>
  <section class="card" aria-labelledby="hist-title">
    <div class="card-hdr">
      <h2 class="card-title" id="hist-title">Run History</h2>
      <span class="card-meta" id="hist-meta"></span>
    </div>
    <div class="table-wrap">
      <table class="hist">
        <thead><tr><th>Started</th><th>Outcome</th></tr></thead>
        <tbody id="hist-body"><tr><td colspan="2" class="empty">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="card-foot" id="hist-foot" hidden><button class="btn-gray btn-sm" id="hist-more" onclick="toggleHistory()">Show all</button></div>
  </section>
  <section class="card" aria-labelledby="log-title">
    <div class="card-hdr">
      <h2 class="card-title" id="log-title">Log</h2>
      <div class="log-tools">
        <div class="seg" id="log-filter" role="group" aria-label="Show in the log">
          <button data-filter="all" aria-pressed="true">All</button>
          <button data-filter="problems" aria-pressed="false">Problems</button>
          <button data-filter="latest" aria-pressed="false">Latest run</button>
        </div>
        <a class="btn-gray btn-sm" href="/api/log" download="ipsw-manager.log" title="Download the whole log file">&#10515; Download</a>
        <button class="btn-gray btn-sm" onclick="scrollLog()">&#8595; End</button>
      </div>
    </div>
    <div id="log" class="log" tabindex="0" aria-label="Run log"></div>
  </section>
  <details class="card settings" id="settings">
    <summary class="card-hdr">
      <h2 class="card-title"><span class="chev" aria-hidden="true">›</span>Settings</h2>
      <span class="card-meta" id="cfg-version"></span>
    </summary>
    <dl id="cfg"></dl>
    <p class="note">These come from the container's environment variables (see the README). To change one, change it and recreate the container.</p>
  </details>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const pad = n => String(n).padStart(2, "0");
const fmt = iso => iso ? new Date(iso).toLocaleString() : "—";
const fmtDate = iso => iso ? new Date(iso).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "—";
const fmtTime = iso => new Date(iso).toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"});

function fmtBytes(b) {
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (b < 1024) return b.toFixed(1) + " " + unit;
    b /= 1024;
  }
  return b.toFixed(1) + " TB";
}

// "5 min ago", "in 12 min", "3 days ago".
function rel(iso) {
  const s = Math.round((new Date(iso) - Date.now()) / 1000), a = Math.abs(s);
  if (a < 45) return s > 0 ? "in under a minute" : "just now";
  let t;
  if (a < 5400) t = Math.round(a / 60) + " min";
  else if (a < 129600) t = Math.round(a / 3600) + " h";
  else { const n = Math.round(a / 86400); t = n + (n === 1 ? " day" : " days"); }
  return s > 0 ? "in " + t : t + " ago";
}

// For things that already happened: a server clock a little ahead of the
// browser's mustn't make them look like they're still to come.
const ago = iso => new Date(iso) > Date.now() ? "just now" : rel(iso);

// Only touch the DOM when something changed, so tooltips and selections survive polls.
function setHTML(el, html) {
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
}

// How a log line or run outcome reads: by its leading emoji, else by keywords (errors first).
function severity(t) {
  const lead = t.trimStart();
  if (lead.startsWith("❌")) return "err";
  if (lead.startsWith("⚠")) return "warn";
  if (lead.startsWith("✅")) return "ok";
  if (lead.startsWith("⬇")) return "info";
  if (/❌|\b(failed|error|mismatch|damaged)\b/i.test(t)) return "err";
  if (/⚠|\bwarn|\bskip/i.test(t)) return "warn";
  if (/✅|\bverified\b|\bDone\b/.test(t)) return "ok";
  if (/⬇|\bDownloading\b|\bFetching\b/i.test(t)) return "info";
  return "";
}

// The log is written in UTC; show it in the viewer's time zone.
const TS_RE = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)(\s+)/;
function localStamp(utc) {
  const d = new Date(utc);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function colorLine(line) {
  const m = line.match(TS_RE);
  const body = m ? line.slice(m[0].length) : line;
  const cls = severity(body) || (body.trim() ? "dim" : "");
  const ts = m ? '<span class="ts" title="' + m[1] + ' UTC">' + localStamp(m[1]) + '</span>' + m[2] : "";
  return ts + (cls ? '<span class="' + cls + '">' + esc(body) + '</span>' : esc(body));
}

/* ---------- Status ---------- */

let lastStatus = null;
let polling = false;
let runPending = false;
let stopping = false;
let logKey = null;
const expanded = new Set();

function setRunButtons(disabled) {
  $("btn-run").disabled = disabled;
  $("btn-verify").disabled = disabled;
}

async function refresh() {
  if (polling) return;  // a slow download folder can make a poll outlast the interval
  polling = true;
  try {
    let d;
    try {
      const resp = await fetch("/api/status", {cache: "no-store"});
      d = await resp.json();
      if (d.error) throw new Error(d.error);
    } catch (e) {
      $("dot").className = "error";
      renderFavicon(null);
      $("status-lbl").textContent = "Can't reach the server";
      $("poll-ts").textContent = "last try " + new Date().toLocaleTimeString() + ": " + e.message;
      $("next-check").textContent = "";
      return;
    }
    lastStatus = d;
    $("poll-ts").textContent = "updated " + new Date().toLocaleTimeString();
    $("dot").className = d.running ? "running" : "";
    stopping = stopping && d.running;
    $("status-lbl").textContent = !d.running ? "Idle" : stopping ? "Stopping…" : d.reverifying ? "Re-verifying…" : "Running…";
    if (!runPending) setRunButtons(d.running);
    $("btn-stop").hidden = !d.running;
    $("btn-stop").disabled = stopping;
    renderSchedule(d);
    renderBanners(d);
    renderSummary(d);
    renderFiles();
    renderHistory(d);
    renderLog(d.logs);
    noticeRunEnd(d);
    renderFavicon(d);
    syncDeviceTags();
  } finally {
    polling = false;
  }
}

function renderSchedule(d) {
  const el = $("next-check"), s = d.schedule || {};
  const cron = s.cron ? "Also runs at fixed times: " + s.cron + " (" + s.tz + ")." : "";
  if (d.running) {
    el.textContent = "";
  } else if (s.next_check) {
    el.textContent = "· next check " + fmtTime(s.next_check) + " (" + rel(s.next_check) + ")";
    el.title = "Every " + s.interval + " min, ipsw.me is asked whether new firmware is out. A run starts only if it is, "
      + "if stored files are due a check, or if the last run didn't finish cleanly. " + cron;
  } else if (s.cron) {
    el.textContent = "· runs at fixed times";
    el.title = cron + " CHECK_INTERVAL is off, so new firmware isn't looked for in between.";
  } else {
    el.textContent = "";
  }
}

function renderBanners(d) {
  const fe = $("folder-err");
  fe.hidden = !d.folder_error;
  setHTML(fe, d.folder_error ? '<span class="banner-label">Download folder</span><span>' + esc(d.folder_error) + '</span>' : "");

  const lr = $("last-run");
  if (!d.last_run) {
    lr.hidden = true;
  } else {
    const m = d.last_run.match(/^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$/);
    const when = m ? m[1] : null, text = m ? m[2] : d.last_run;
    lr.className = "banner sev-" + (severity(text) || "info");
    setHTML(lr, '<span class="banner-label">Last run'
      + (when ? ' <span title="' + esc(fmt(when)) + '">' + esc(ago(when)) + '</span>' : "")
      + '</span><span>' + esc(text) + '</span>');
    lr.hidden = false;
  }

  const disk = d.disk;
  $("disk").textContent = disk
    ? disk.files + (disk.files === 1 ? " file · " : " files · ") + disk.used_human
      + (disk.free_human ? " · " + disk.free_human + " free" : "")
    : "";
}

// " · next in 27 days", from IPSW_REVERIFY_DAYS.
function nextCheck(f) {
  if (!f.next_check) return "";
  return new Date(f.next_check) <= Date.now() ? " · due now" : " · next " + rel(f.next_check);
}

function statusCell(f) {
  if (f.status === "downloading") return '<span class="badge badge-blue">⬇ Downloading</span>' + progress(f);
  if (f.status === "verifying") {
    return '<span class="badge badge-blue" title="' + (f.read_pass > 1
        ? "The first read didn't match its checksum. Reading it again to tell a damaged file from a bad read."
        : "Reading the stored file back and checking it against its checksum") + '">🔍 Verifying</span>'
      + progress(f, f.read_pass > 1 ? "second read" : "reading back");
  }
  if (f.check_failed) {
    return '<span class="badge badge-orange" title="' + esc("Couldn't be read back to check it on " + fmt(f.check_failed.at) + ": "
        + f.check_failed.reason + (f.verified ? ". Last verified " + fmt(f.checked) : ". Never verified") + ". The next run tries again.")
      + '">⚠ Check failed</span><span class="sub">' + esc(ago(f.check_failed.at) + ": " + f.check_failed.reason) + '</span>';
  }
  if (f.verified) {
    return '<span class="badge badge-green" title="The copy in the download folder was read back and matched its checksum on '
      + esc(fmt(f.checked)) + '">✓ Verified</span><span class="sub">' + esc(ago(f.checked) + nextCheck(f)) + '</span>';
  }
  return '<span class="badge badge-orange" title="Not yet read back from the download folder and checked. The next run, or Re-verify, will do it.">? Not checked</span>'
    + '<span class="sub">next run</span>';
}

// Bar and "38% · 12.4 MB/s · 6 min left", under a Downloading or Verifying badge.
function progress(f, label) {
  const parts = label ? [label] : [];
  if (f.percent != null) parts.push(Math.floor(f.percent) + "%");
  parts.push(f.speed_human || "measuring…");
  if (f.eta_human) parts.push(f.eta_human);
  const bar = f.percent != null ? '<div class="bar"><span style="width:' + f.percent.toFixed(1) + '%"></span></div>' : "";
  return bar + '<span class="speed">' + esc(parts.join(" · ")) + '</span>';
}

const downloadHref = f => "/download/" + encodeURIComponent(f.name);
const canDownload = f => !!(lastStatus && lastStatus.download_links && !f.partial);

function fileRow(f) {
  const open = expanded.has(f.name);
  const ios = f.version !== "—" ? "iOS " + esc(f.version) : "—";
  return '<tr>'
    + '<td><button type="button" class="row-toggle" data-name="' + esc(f.name) + '" aria-expanded="' + open + '">'
    + '<span class="chev" aria-hidden="true">›</span>' + esc(f.model || f.identifiers.join(", ") || f.name) + '</button></td>'
    + '<td class="ios">' + ios + (f.update ? '<span class="sub warn" title="Apple now signs iOS ' + esc(f.update)
      + ' for this phone. The next run downloads it, then deletes this file.">' + esc(f.update) + ' is out</span>' : "") + '</td>'
    + '<td>' + statusCell(f) + '</td></tr>';
}

function fileDetail(f) {
  const rows = [
    // Long names break only after an underscore on narrow screens.
    ["File", '<span class="mono">' + esc(f.name + (f.partial ? ".partial" : "")).replace(/_/g, "_<wbr>") + '</span>'],
    ["Phones", esc((f.model ? f.model + " · " : "") + (f.identifiers.join(", ") || "—"))],
    ["Size", esc(f.size_human + (f.total_human ? " of " + f.total_human : ""))],
    ["Modified", f.mtime ? esc(fmt(f.mtime)) : "Still downloading"],
    ["Verified", f.verified
      ? esc(fmt(f.checked) + ": read back from the download folder and matched its checksum")
      : f.partial ? "Checked once the download finishes" : "Never: not yet read back and checked"],
  ];
  if (f.verified) {
    rows.push(["Next check", f.next_check
      ? esc(fmt(f.next_check) + " (" + rel(f.next_check) + "). A run reads it back again then.")
      : "Only if it's downloaded again: IPSW_REVERIFY_DAYS is 0"]);
  }
  if (f.check_failed) {
    rows.push(["Check failed", esc(fmt(f.check_failed.at) + ": " + f.check_failed.reason + ". The next run tries again.")]);
  }
  if (f.update) {
    rows.push(["Newer iOS", esc("Apple now signs iOS " + f.update + " for this phone. The next run downloads it, then deletes this file.")]);
  }
  const download = canDownload(f)
    ? '<a class="btn-gray btn-sm dl-btn" href="' + downloadHref(f) + '" download>⤓ Download IPSW (' + esc(f.size_human) + ')</a>'
    : "";
  return '<tr class="detail"><td colspan="3"><dl>'
    + rows.map(([k, v]) => '<dt>' + k + '</dt><dd>' + v + '</dd>').join("") + '</dl>' + download + '</td></tr>';
}

function missingRow(m, running) {
  const tip = m.checked
    ? "Selected, but there's no IPSW for it in the download folder. " + (running ? "This run" : "The next run") + " downloads iOS " + m.latest + "."
    : "Selected, but there's no IPSW for it in the download folder. Asking ipsw.me what Apple signs for it…";
  return '<tr class="missing"><td title="' + esc(m.identifier) + '">' + esc(m.model) + '</td>'
    + '<td class="ios">' + (m.latest ? "iOS " + esc(m.latest) : "—") + '</td>'
    + '<td><span class="badge badge-gray" title="' + esc(tip) + '">○ Not downloaded</span>'
    + '<span class="sub">' + (!m.checked ? "checking ipsw.me…" : running ? "this run" : "next run") + '</span></td></tr>';
}

// Replace only the rows that changed. Each row is [key, "<tr>…</tr>"].
function patchRows(tbody, rows) {
  const make = ([key, html]) => {
    const t = document.createElement("tbody");
    t.innerHTML = html;
    const tr = t.firstElementChild;
    tr.dataset.key = key;
    tr._html = html;
    return tr;
  };
  const cur = [...tbody.children];
  if (cur.length === rows.length && cur.every((tr, i) => tr.dataset.key === rows[i][0])) {
    rows.forEach((row, i) => { if (cur[i]._html !== row[1]) cur[i].replaceWith(make(row)); });
  } else {
    tbody.replaceChildren(...rows.map(make));
  }
}

function renderFiles() {
  const d = lastStatus;
  if (!d) return;
  const rows = [];
  d.downloads.forEach(f => {
    rows.push([f.name, fileRow(f)]);
    if (expanded.has(f.name)) rows.push([f.name + "#detail", fileDetail(f)]);
  });
  (d.missing || []).forEach(m => rows.push(["missing:" + m.identifier, missingRow(m, d.running)]));
  if (!rows.length) {
    const msg = d.folder_error ? "Can't list the download folder right now." : "No IPSW files in " + d.download_dir + " yet.";
    rows.push(["empty", '<tr><td colspan="3" class="empty">' + esc(msg) + '</td></tr>']);
  }
  patchRows($("files-body"), rows);

  const note = $("unsigned-note"), unsigned = d.unsigned || [];
  note.hidden = !unsigned.length;
  setHTML(note, unsigned.length
    ? "Selected, but Apple signs no firmware for " + (unsigned.length === 1 ? "it" : "them") + ", so there's nothing to download: "
      + esc(unsigned.map(p => p.model).join(", ")) + "."
    : "");
}

$("files-body").addEventListener("click", e => {
  const btn = e.target.closest(".row-toggle");
  if (!btn) return;
  const name = btn.dataset.name;
  expanded.has(name) ? expanded.delete(name) : expanded.add(name);
  renderFiles();
  const again = $("files-body").querySelector('.row-toggle[data-name="' + CSS.escape(name) + '"]');
  if (again) again.focus();
});

let logFilter = "all";
let logLines = [];
// A run's first line (older versions only logged one for scheduled runs).
const RUN_START_RE = /^(▶️?\s+Run started — |🆕 .* — starting a run\.)/;

function filteredLog(lines) {
  const body = l => l.replace(TS_RE, "");
  if (logFilter === "problems") return lines.filter(l => ["err", "warn"].includes(severity(body(l))));
  if (logFilter === "latest") {
    for (let i = lines.length - 1; i >= 0; i--) if (RUN_START_RE.test(body(lines[i]))) return lines.slice(i);
  }
  return lines;
}

function renderLog(lines) {
  logLines = lines;
  const shown = filteredLog(lines);
  const key = logFilter + "\n" + shown.join("\n");
  if (key === logKey) return;
  const el = $("log");
  // Don't wipe out text someone is selecting to copy; catch up on a later poll.
  const sel = window.getSelection();
  if (logKey !== null && sel && !sel.isCollapsed && el.contains(sel.anchorNode)) return;
  const atBottom = logKey === null || el.scrollHeight - el.scrollTop <= el.clientHeight + 50;
  el.innerHTML = shown.length ? shown.map(colorLine).join("\n")
    : '<span class="dim">' + (logFilter === "problems"
      ? "No warnings or errors in the last " + lines.length + " lines. Download the log to see older ones."
      : "Nothing in the log yet.") + '</span>';
  logKey = key;
  if (atBottom) el.scrollTop = el.scrollHeight;
}

$("log-filter").addEventListener("click", e => {
  const btn = e.target.closest("button[data-filter]");
  if (!btn) return;
  logFilter = btn.dataset.filter;
  document.querySelectorAll("#log-filter button").forEach(b => b.setAttribute("aria-pressed", String(b === btn)));
  logKey = null;  // redraw, and scroll to the end
  renderLog(logLines);
});

function scrollLog() {
  const el = $("log");
  el.scrollTop = el.scrollHeight;
}

async function doRun(reverify) {
  if (reverify && !confirm(
    "Re-verify does everything Run Now does, and also reads every stored firmware file back from the download folder and checks it against its checksum, however recently it was checked.\n\n"
    + "• A file that fails is deleted and downloaded again.\n"
    + "• As with Run Now, new firmware is downloaded, and IPSWs that are no longer current or belong to phones you've unticked are deleted.\n"
    + "• If ipsw.me can't be reached, files are checked against the checksum saved when they last passed.\n\n"
    + "It takes about as long as copying every file off the download folder: a minute or two per file. Continue?"
  )) return;

  runPending = true;
  setRunButtons(true);
  let d;
  try {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reverify: reverify})
    });
    d = await resp.json();
  } catch (e) {
    d = {success: false, message: "Request failed: " + e.message};
  }
  runPending = false;
  if (!d.success) {
    alert(d.message);
    setRunButtons(false);
  }
  refresh();
}

async function doStop() {
  const retry = config && config.check_interval
    ? "\n• The scheduled checks may start it again in about " + config.retry_minutes + " min."
    : "";
  if (!confirm("Stop the run in progress?\n\n"
    + "• A download in progress is thrown away; the next run starts it again.\n"
    + "• Files already downloaded and checked are kept. If the run hadn't reached its cleanup, no old IPSWs are deleted."
    + retry)) return;
  stopping = true;
  $("btn-stop").disabled = true;
  $("status-lbl").textContent = "Stopping…";
  let d;
  try {
    const resp = await fetch("/api/stop", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    d = await resp.json();
  } catch (e) {
    d = {success: false, message: "Request failed: " + e.message};
  }
  if (!d.success) {
    stopping = false;
    alert(d.message);
  }
  refresh();
}

/* ---------- Summary ---------- */

function renderSummary(d) {
  const el = $("summary");
  const files = d.downloads, missing = (d.missing || []).length;
  const c = {verified: 0, notChecked: 0, failed: 0, active: 0};
  files.forEach(f => {
    if (f.status === "downloading" || f.status === "verifying") c.active++;
    else if (f.check_failed) c.failed++;
    else if (f.verified) c.verified++;
    else c.notChecked++;
  });
  const updates = files.filter(f => f.update).length;
  const chips = [];
  if (d.folder_error) {
    // The banner says it; counts of an unlisted folder would mislead.
  } else if (files.length && c.verified === files.length && !missing && !updates) {
    chips.push(["ok", "✓ All " + c.verified + " verified and up to date"]);
  } else {
    if (c.verified) chips.push(["ok", c.verified + " verified"]);
    if (c.active) chips.push(["info", c.active + " in progress"]);
    if (c.notChecked) chips.push(["warn", c.notChecked + " not checked"]);
    if (c.failed) chips.push(["warn", c.failed + (c.failed === 1 ? " check" : " checks") + " failed"]);
    if (missing) chips.push(["muted", missing + " not downloaded"]);
    if (updates) chips.push(["warn", updates + " with a newer iOS out"]);
  }
  el.hidden = !chips.length;
  setHTML(el, chips.map(([k, t]) => '<span class="chip chip-' + k + '">' + esc(t) + '</span>').join(""));
}

/* ---------- Run history ---------- */

let historyAll = false;
const HISTORY_SHOWN = 8;
const fmtShort = iso => new Date(iso).toLocaleString(undefined, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});

function duration(from, to) {
  const s = Math.max(0, (new Date(to) - new Date(from)) / 1000);
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  const m = Math.round((s % 3600) / 60);
  return Math.floor(s / 3600) + " h" + (m ? " " + m + " min" : "");
}

function historyRow(r) {
  const when = r.started || r.ended;
  const meta = [];
  if (r.running) meta.push("running for " + duration(r.started, new Date().toISOString()));
  else if (r.started && r.ended) meta.push("took " + duration(r.started, r.ended));
  if (r.reverify) meta.push("Re-verify");
  const outcome = r.running ? "Running…"
    : r.outcome || "❌ Didn't finish: it was killed, timed out, or the container stopped.";
  const sev = r.running ? "info" : severity(outcome) || "info";
  const trigger = r.trigger || (r.started ? "" : "How it started isn't in the log");
  return '<tr><td><span title="' + esc(fmt(when)) + '">' + esc(fmtShort(when)) + '</span>'
    + (meta.length ? '<span class="sub">' + esc(meta.join(" · ")) + '</span>' : "") + '</td>'
    + '<td class="outcome"><span class="out-' + sev + '">' + esc(outcome) + '</span>'
    + (trigger ? '<span class="sub">' + esc(trigger) + '</span>' : "") + '</td></tr>';
}

function renderHistory(d) {
  const past = d.history || [];
  const runs = (d.current_run ? [Object.assign({running: true}, d.current_run)] : []).concat(past);
  let rows;
  if (!runs.length) {
    const msg = d.history_available ? "No runs in the log yet." : "Run history is unavailable while the download folder isn't responding.";
    rows = [["empty", '<tr><td colspan="2" class="empty">' + esc(msg) + '</td></tr>']];
  } else {
    rows = (historyAll ? runs : runs.slice(0, HISTORY_SHOWN))
      .map(r => [(r.running ? "running|" : "") + (r.started || "") + "|" + (r.ended || ""), historyRow(r)]);
  }
  patchRows($("hist-body"), rows);
  $("hist-foot").hidden = runs.length <= HISTORY_SHOWN;
  $("hist-more").textContent = historyAll ? "Show fewer" : "Show all " + runs.length;
  const recent = past.slice(0, 10);
  const bad = recent.filter(r => !r.outcome || ["err", "warn"].includes(severity(r.outcome))).length;
  $("hist-meta").textContent = recent.length
    ? (bad ? bad + " of the last " + recent.length + " didn't finish cleanly" : "Last " + recent.length + " finished cleanly")
    : "";
}

function toggleHistory() {
  historyAll = !historyAll;
  if (lastStatus) renderHistory(lastStatus);
}

/* ---------- Notices: tab title, icon, notifications ---------- */

const canNotify = "Notification" in window && window.isSecureContext;
let notifyOn = false;
try { notifyOn = canNotify && Notification.permission === "granted" && localStorage.getItem("ipsw-notify") === "1"; } catch (e) {}
let prevLastRun;  // undefined until the first poll
let titleNotice = "";

const outcomeText = line => line.replace(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\s+/, "");

function updateTitle() {
  document.title = (titleNotice ? titleNotice + " · " : "") + "IPSW Manager";
}

// A new "last run" line means a run ended. If nobody's looking, say so in the
// tab title and, if turned on, with a notification.
function noticeRunEnd(d) {
  if (prevLastRun !== undefined && d.last_run && d.last_run !== prevLastRun && document.hidden) {
    const text = outcomeText(d.last_run), sev = severity(text);
    titleNotice = (sev === "err" ? "✗" : sev === "warn" ? "⚠" : "✓") + " Run finished";
    updateTitle();
    if (notifyOn && Notification.permission === "granted") {
      try { new Notification("IPSW Manager: run finished", {body: text, tag: "ipsw-manager-run"}); } catch (e) {}
    }
  }
  prevLastRun = d.last_run;
}

const FAVICON_COLORS = {ok: "#3fb950", warn: "#d29922", err: "#f85149", info: "#58a6ff"};
let faviconColor = null;

function renderFavicon(d) {
  let sev = "ok";
  if (!d || d.folder_error) sev = "err";
  else if (d.running) sev = "info";
  else {
    sev = d.last_run ? severity(outcomeText(d.last_run)) || "ok" : "ok";
    if (sev === "ok" && d.downloads.some(f => f.check_failed)) sev = "warn";
  }
  const color = FAVICON_COLORS[sev] || FAVICON_COLORS.ok;
  if (color === faviconColor) return;
  faviconColor = color;
  $("favicon").href = "data:image/svg+xml," + encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect x='3' y='1' width='10' height='14' rx='2.5' fill='" + color + "'/></svg>");
}

function renderNotifyButton() {
  const btn = $("btn-notify");
  btn.hidden = !canNotify;  // browsers only allow notifications over HTTPS or on localhost
  btn.setAttribute("aria-pressed", String(notifyOn));
  btn.textContent = notifyOn ? "🔔" : "🔕";
  btn.title = notifyOn
    ? "Notifications on: you'll get one when a run finishes while this tab is in the background. Click to turn them off."
    : "Get a notification when a run finishes while this tab is in the background";
  btn.setAttribute("aria-label", notifyOn ? "Turn run notifications off" : "Turn run notifications on");
}

async function toggleNotify() {
  if (!notifyOn && Notification.permission !== "granted") {
    if (await Notification.requestPermission() !== "granted") {
      alert("This browser is blocking notifications from this page. Allow them in its site settings, then try again.");
      return;
    }
  }
  notifyOn = !notifyOn;
  try { localStorage.setItem("ipsw-notify", notifyOn ? "1" : "0"); } catch (e) {}
  renderNotifyButton();
}

/* ---------- Settings ---------- */

let config = null;

async function loadConfig() {
  try {
    config = await (await fetch("/api/config")).json();
  } catch (e) {
    $("cfg").innerHTML = '<dt>Settings</dt><dd>' + esc("Couldn't load them: " + e.message) + '</dd>';
    return;
  }
  const c = config;
  $("cfg-version").textContent = "version " + c.version;
  const rows = [
    ["Version", c.version],
    ["Download folder", c.download_dir],
    ["New firmware", c.check_interval ? "Looked for every " + c.check_interval + " min (CHECK_INTERVAL)" : "Not looked for (CHECK_INTERVAL is off)"],
    ["Fixed-time runs", c.cron ? c.cron + " (CRON_SCHEDULE)" : "None (CRON_SCHEDULE isn't set)"],
    ["Time zone", c.tz + " (TZ)"],
    ["Stored files", c.reverify_days ? "Read back and checked every " + c.reverify_days + " days (IPSW_REVERIFY_DAYS)" : "Checked only when downloaded (IPSW_REVERIFY_DAYS is 0)"],
    ["After a run that fails", c.check_interval ? "Tried again after " + c.retry_minutes + " min (IPSW_RETRY_MINUTES)" : "Not retried until the next run"],
    ["Run time limit", c.run_timeout ? c.run_timeout + " (IPSW_RUN_TIMEOUT)" : "None (IPSW_RUN_TIMEOUT is 0)"],
    ["Checksum", c.checksum.toUpperCase() + " (IPSW_CHECKSUM_ALGO)"],
    ["Runs as", "User and group " + c.user + " (PUID, PGID)"],
    ["Download links", c.download_links ? "On (WEBUI_DOWNLOADS)" : "Off (WEBUI_DOWNLOADS=0)"],
    ["Web UI port", c.port + " (WEBUI_PORT)"],
  ];
  $("cfg").innerHTML = rows.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join("");
}

/* ---------- Phones ---------- */

let allDevices = [];
let defaultIds = [];
let tagsKey = "";
let selected = new Set();
let savedSelection = "";   // what's saved; "" if nothing has been saved yet
let baselineSelection = null;  // what the page loaded with or last saved, for the leave warning

const selectionKey = () => [...selected].sort().join(",");

function updateDeviceMeta() {
  $("dev-count").textContent = selected.size + " of " + allDevices.length + " selected";
  const dirty = selectionKey() !== savedSelection;
  $("btn-save").disabled = !dirty || selected.size === 0;
  $("btn-save").textContent = dirty ? "Save" : "Saved";
}

const filterText = () => $("dev-filter").value.trim().toLowerCase();

function visibleDevices() {
  const q = filterText();
  return q ? allDevices.filter(d => (d.name + " " + d.identifier).toLowerCase().includes(q)) : allDevices;
}

// Phones with a finished IPSW in the download folder.
const storedIds = () => new Set(((lastStatus && lastStatus.downloads) || []).filter(f => !f.partial).flatMap(f => f.identifiers));
const computeTagsKey = () => [...storedIds()].sort().join(",") + "|"
  + allDevices.filter(d => d.signed === false).map(d => d.identifier).join(",");

function renderDevices() {
  const list = visibleDevices();
  const filtered = filterText() !== "";
  const stored = storedIds();
  $("btn-all").textContent = filtered ? "Select shown" : "Select all";
  $("btn-none").textContent = filtered ? "Clear shown" : "Select none";
  $("dev-grid").innerHTML = list.length ? list.map(d =>
    '<label' + (d.signed === false ? ' class="unsigned" title="Apple signs no firmware for this phone now, so nothing is downloaded for it"' : "") + '>'
    + '<input type="checkbox" data-id="' + esc(d.identifier) + '"' + (selected.has(d.identifier) ? " checked" : "") + '>'
    + '<span>' + esc(d.name)
    + (stored.has(d.identifier) ? '<span class="tag tag-stored" title="Has an IPSW in the download folder">stored</span>' : "")
    + (d.signed === false ? '<span class="tag tag-unsigned">not signed</span>' : "")
    + '</span><span class="mono">' + esc(d.identifier) + '</span></label>'
  ).join("") : '<div class="empty">No phones match</div>';
  tagsKey = computeTagsKey();
  updateDeviceMeta();
}

// Redraw the list when a phone's tags change (a download finished, or ipsw.me answered).
function syncDeviceTags() {
  if (allDevices.length && computeTagsKey() !== tagsKey) renderDevices();
}

function resetDefault() {
  selected = new Set(defaultIds);
  renderDevices();
}

// ipsw.me is asked about every phone in the background; pick up its answers.
let hintTries = 0;
async function refreshDeviceHints() {
  if (!allDevices.some(d => d.signed == null) || hintTries++ >= 8) return;
  try {
    const d = await (await fetch("/api/devices")).json();
    if (d.error) return;
    const signed = new Map(d.devices.map(x => [x.identifier, x.signed]));
    allDevices.forEach(x => { if (signed.has(x.identifier)) x.signed = signed.get(x.identifier); });
    syncDeviceTags();
  } catch (e) {
    // try again below
  }
  setTimeout(refreshDeviceHints, 8000);
}

$("dev-grid").addEventListener("change", e => {
  const id = e.target.dataset.id;
  if (!id) return;
  e.target.checked ? selected.add(id) : selected.delete(id);
  updateDeviceMeta();
});

function setVisible(on) {
  visibleDevices().forEach(d => on ? selected.add(d.identifier) : selected.delete(d.identifier));
  renderDevices();
}

async function loadDevices() {
  try {
    const d = await (await fetch("/api/devices")).json();
    if (d.error) throw new Error(d.error);
    allDevices = d.devices;
    defaultIds = d.default || [];
    selected = new Set(d.selected);
    savedSelection = d.saved ? selectionKey() : "";
    baselineSelection = selectionKey();
    if (!d.saved) {
      $("dev-note").textContent =
        "No selection saved yet, so iPhone 11 and newer are downloaded by default. Unticked phones' firmware is deleted on the next run.";
    }
    renderDevices();
    setTimeout(refreshDeviceHints, 8000);
  } catch (e) {
    $("dev-grid").innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  }
}

// Stored firmware that no ticked phone needs: the next run deletes it.
function filesToDelete() {
  return ((lastStatus && lastStatus.downloads) || []).filter(f =>
    !f.partial && f.identifiers.length && !f.identifiers.some(i => selected.has(i)));
}

async function saveDevices() {
  const doomed = filesToDelete();
  if (doomed.length && !confirm(
    "The next run will delete " + doomed.length + (doomed.length === 1 ? " firmware file" : " firmware files")
    + " (" + fmtBytes(doomed.reduce((s, f) => s + f.size, 0)) + ") for phones you've unticked:\n\n"
    + doomed.map(f => "• " + (f.model || f.identifiers.join(", ")) + (f.version !== "—" ? ", iOS " + f.version : "")).join("\n")
    + "\n\nSave anyway?"
  )) return;

  const btn = $("btn-save");
  btn.disabled = true;
  try {
    const d = await (await fetch("/api/devices", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({devices: [...selected]})
    })).json();
    if (!d.success) throw new Error(d.message);
    savedSelection = baselineSelection = selectionKey();
    $("dev-note").textContent = "Saved. Takes effect on the next run. Unticked phones' firmware is deleted on the next run.";
    loadCalendar();  // signed versions depend on the selection
    refresh();       // and so do the missing phones
  } catch (e) {
    alert("Save failed: " + e.message);
  }
  updateDeviceMeta();
}

window.addEventListener("beforeunload", e => {
  if (baselineSelection !== null && selectionKey() !== baselineSelection) {
    e.preventDefault();
    e.returnValue = "";
  }
});

/* ---------- Calendar ---------- */

const EV_LABEL = {release: "Release", beta: "Beta", rc: "Release candidate", expected: "Expected (estimate)"};
let calEvents = [];
let calLoaded = false;
let calMonth = null;
let calLoadedAt = 0;

const dayKey = d => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());

function shiftMonth(n) {
  const now = new Date();
  calMonth = n === 0 || !calMonth
    ? new Date(now.getFullYear(), now.getMonth(), 1)
    : new Date(calMonth.getFullYear(), calMonth.getMonth() + n, 1);
  renderCalendar();
}

function eventChip(e) {
  const tip = EV_LABEL[e.kind] + ": " + e.title + (e.builds.length ? " (" + e.builds.join(", ") + ")" : "")
    + (e.signed ? " · signed" : "");
  return '<div class="ev ev-' + e.kind + '" title="' + esc(tip) + '">' + esc(e.title.replace(/^iOS /, "")) + (e.signed ? " ✓" : "") + '</div>';
}

function renderCalendar() {
  if (!calMonth) { shiftMonth(0); return; }
  const y = calMonth.getFullYear(), m = calMonth.getMonth();
  $("cal-month").textContent = calMonth.toLocaleDateString(undefined, {month: "long", year: "numeric"});

  const byDay = {};
  calEvents.forEach(e => (byDay[dayKey(e.date)] ||= []).push(e));

  // Weeks start on Monday. 1 January 2024 was a Monday.
  let html = "";
  for (let i = 0; i < 7; i++) {
    html += '<div class="cal-dow">' + new Date(2024, 0, 1 + i).toLocaleDateString(undefined, {weekday: "short"}) + '</div>';
  }
  const offset = (new Date(y, m, 1).getDay() + 6) % 7;
  const cells = Math.ceil((offset + new Date(y, m + 1, 0).getDate()) / 7) * 7;
  const today = dayKey(new Date());
  for (let i = 0; i < cells; i++) {
    const d = new Date(y, m, 1 - offset + i);
    const key = dayKey(d);
    const cls = "cal-day" + (d.getMonth() !== m ? " other" : "") + (key === today ? " today" : "");
    html += '<div class="' + cls + '"><span class="cal-num">' + d.getDate() + '</span>'
      + (byDay[key] || []).map(eventChip).join("") + '</div>';
  }
  $("cal-grid").innerHTML = html;

  if (!calLoaded) return;  // the list keeps its "Loading releases…" or error message
  const inMonth = calEvents.filter(e => e.date.getFullYear() === y && e.date.getMonth() === m);
  $("cal-list").innerHTML = inMonth.length ? inMonth.map(e =>
    '<tr><td>' + e.date.toLocaleDateString(undefined, {weekday: "short", day: "numeric", month: "short"}) + '</td>'
    + '<td><span class="ev ev-' + e.kind + '">' + esc(EV_LABEL[e.kind]) + '</span>' + esc(e.title) + '</td>'
    + '<td class="mono">' + esc(e.builds.join(", ") || "—") + '</td>'
    + '<td>' + (e.signed ? '<span class="badge badge-green">✓ Signed</span>' : "") + '</td></tr>'
  ).join("") : '<tr><td colspan="4" class="empty">No releases this month</td></tr>';
}

async function loadCalendar() {
  calLoadedAt = Date.now();
  try {
    const d = await (await fetch("/api/calendar")).json();
    if (d.error) throw new Error(d.error);
    calEvents = d.events.map(e => ({...e, date: new Date(e.at)}));
    calLoaded = true;
    $("cal-errors").innerHTML = d.errors.map(m => '<div class="cal-err">⚠ ' + esc(m) + '</div>').join("");
    renderCalendar();
  } catch (e) {
    if (!calLoaded) $("cal-list").innerHTML = '<tr><td colspan="4" class="empty">' + esc(e.message) + '</td></tr>';
    else $("cal-errors").innerHTML = '<div class="cal-err">⚠ ' + esc(e.message) + '</div>';
  }
}

renderCalendar();  // the month shows straight away; releases fill in when they arrive
renderNotifyButton();
loadConfig();
loadDevices();
loadCalendar();
refresh();
// A tab in the background polls every 30 s instead of 5: often enough to
// notice a run ending, and it catches up as soon as it's shown again.
let tick = 0;
setInterval(() => { tick++; if (!document.hidden || tick % 6 === 0) refresh(); }, 5000);
setInterval(() => { if (!document.hidden) loadCalendar(); }, 3600 * 1000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  titleNotice = "";
  updateTitle();
  refresh();
  if (Date.now() - calLoadedAt > 3600 * 1000) loadCalendar();
});
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # suppress per-request access logs

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self):  # noqa: N802
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away, e.g. a download was cancelled
        except Exception as exc:
            print(f"ERROR in GET {self.path}: {exc}", flush=True)
            try:
                self.send_json({"error": str(exc)}, status=500)
            except Exception:
                pass

    def do_HEAD(self):  # noqa: N802
        # Download managers ask for a file's size before fetching it.
        path = self._route()
        try:
            if path.startswith("/download/"):
                self._send_ipsw(urllib.parse.unquote(path[len("/download/"):]), head=True)
            else:
                self.send_response(404 if path not in ("/", "/index.html") else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_get(self):
        path = self._route()
        if path.startswith("/download/"):
            self._send_ipsw(urllib.parse.unquote(path[len("/download/"):]))
        elif path == "/api/log":
            self._send_log()
        elif path == "/api/config":
            self.send_json(config_info())
        elif path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self.send_json(self._status())
        elif path == "/api/calendar":
            try:
                self.send_json(get_calendar())
            except Exception as exc:
                self.send_json({"error": f"Couldn't load the release calendar: {exc}"}, status=502)
        elif path == "/api/devices":
            try:
                devices = get_all_iphones()
            except Exception as exc:
                self.send_json({"error": f"Couldn't fetch device list from ipsw.me: {exc}"}, status=502)
                return
            saved = _with_timeout(read_selection, fallback=None)
            # Whether Apple signs anything for each phone, as far as ipsw.me
            # has answered yet (None until it has); the rest arrive later.
            firmware = signed_firmware([d["identifier"] for d in devices])
            self.send_json(
                {
                    "devices": [
                        dict(d, signed=(firmware[d["identifier"]]["latest"] is not None)
                             if d["identifier"] in firmware else None)
                        for d in devices
                    ],
                    "selected": saved if saved is not None else default_selection(devices),
                    "default": default_selection(devices),
                    "saved": saved is not None,
                }
            )
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _status(self):
        run = current_run()
        running = run is not None
        scan = _with_timeout(scan_download_dir, fallback=None)
        if scan is None:
            folder_error = ("The download folder isn't responding, so its files can't be listed. "
                            "The drive or network share it's on may be disconnected, asleep or busy.")
            scan = {"files": [], "error": None, "selection": None, "disk": None}
        else:
            folder_error = scan["error"]
        files = scan["files"]

        names = device_names()
        selection = scan["selection"]
        if selection is None and _device_cache["devices"]:
            selection = default_selection(_device_cache["devices"])
        selection = [i for i in selection or [] if IDENTIFIER_RE.match(i)]
        firmware = signed_firmware(selection + [i for f in files for i in f["identifiers"]])

        for f in files:
            f["model"] = model_name(f["identifiers"], names)
            f["update"] = None if f["status"] == "downloading" \
                else newer_firmware(f["identifiers"], f["version"], firmware)
        files.sort(key=_file_sort_key)
        # Without a listing, every phone would look missing.
        missing, unsigned = ([], []) if folder_error else missing_phones(files, selection, names, firmware)

        logs = _with_timeout(get_log_tail, 200, fallback=["[log unavailable — the download folder isn't responding]"])

        history = _with_timeout(read_run_history, fallback=None)
        runs, open_run = history if history else ([], None)
        current = None
        if open_run and running:
            current = open_run
        elif open_run:
            runs = [open_run] + runs  # started but never ended: killed outright
        return {
            "running": running,
            "reverifying": run == "reverify",
            "downloads": files,
            "missing": missing,
            "unsigned": unsigned,
            "folder_error": folder_error,
            "disk": scan["disk"],
            "schedule": schedule_info(),
            "logs": logs,
            "last_run": get_last_run(logs, running),
            "current_run": current,
            "history": runs[:HISTORY_MAX],
            "history_available": history is not None,
            "download_links": DOWNLOAD_LINKS,
            "download_dir": DOWNLOAD_DIR,
        }

    def _send_log(self):
        """The whole log file, as a download."""
        try:
            f = open(LOG_FILE, "rb")
        except OSError as exc:
            self.send_json({"error": f"Couldn't open the log: {exc.strerror or exc}"}, status=404)
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="ipsw-manager.log"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.connection.sendfile(f, 0, size)

    def _send_ipsw(self, name, head=False):
        """A stored IPSW, with Range support so a cut-off download can resume."""
        def not_found(message, status=404):
            body = (message + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        if not DOWNLOAD_LINKS:
            not_found("Download links are turned off (WEBUI_DOWNLOADS=0).", 403)
            return
        # Only a finished IPSW directly in the download folder: no paths,
        # no hidden files, no .partial still being written or checked.
        if name != os.path.basename(name) or name.startswith(".") or not name.endswith(".ipsw"):
            not_found("No such firmware file.")
            return
        try:
            f = open(os.path.join(DOWNLOAD_DIR, name), "rb")
        except OSError:
            not_found("No such firmware file.")
            return
        with f:
            st = os.fstat(f.fileno())
            size, start, end, status = st.st_size, 0, st.st_size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # the last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head and size:
                self.connection.sendfile(f, start, end - start + 1)

    def _same_origin(self):
        """False for a request another website made from the viewer's browser.

        Browsers mark every request with Sec-Fetch-Site; older ones only send
        Origin. Requests from curl and scripts send neither and are allowed.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        netloc = urllib.parse.urlsplit(origin).netloc.lower()
        hosts = {h.strip().lower() for h in (self.headers.get("Host"), self.headers.get("X-Forwarded-Host")) if h}
        return bool(netloc) and netloc in hosts

    def do_POST(self):  # noqa: N802
        try:
            self._handle_post()
        except Exception as exc:
            print(f"ERROR in POST {self.path}: {exc}", flush=True)
            try:
                self.send_json({"success": False, "message": f"Server error: {exc}"}, status=500)
            except Exception:
                pass

    def _handle_post(self):
        path = self._route()
        if path not in ("/api/run", "/api/stop", "/api/devices"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Starting runs and changing the phones (which deletes firmware) must
        # come from this page. Requiring JSON also means another site's page
        # can't send these without a CORS preflight, which this server refuses.
        if not self._same_origin():
            self.send_json({"success": False, "message": "Refused a request from another website"}, status=403)
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self.send_json({"success": False, "message": "Expected a JSON request"}, status=415)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_POST_BODY:
            self.send_json({"success": False, "message": "Bad request size"}, status=413 if length > 0 else 400)
            return
        try:
            data = json.loads(self.rfile.read(length) if length else b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.send_json({"success": False, "message": "Expected a JSON object"}, status=400)
            return

        if path == "/api/run":
            ok, msg = trigger_run(data.get("reverify") is True)
            self.send_json({"success": ok, "message": msg})
            return
        if path == "/api/stop":
            ok, msg = stop_run()
            self.send_json({"success": ok, "message": msg})
            return

        selected = data.get("devices")
        if not isinstance(selected, list) or not all(
            isinstance(d, str) and IDENTIFIER_RE.match(d) for d in selected
        ):
            self.send_json({"success": False, "message": "Invalid device list"}, status=400)
            return
        if not selected:
            self.send_json({"success": False, "message": "Select at least one phone"}, status=400)
            return
        try:
            saved = _with_timeout(write_selection, set(selected), fallback=False)
        except OSError as exc:
            self.send_json({"success": False, "message": f"Couldn't save: {exc}"}, status=500)
            return
        if saved is False:
            self.send_json(
                {"success": False, "message": "Couldn't save: the download folder isn't responding"},
                status=503,
            )
            return
        self.send_json({"success": True, "message": f"Saved {len(set(selected))} phones"})


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Web UI → http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
