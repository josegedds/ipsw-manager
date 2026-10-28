#!/usr/bin/env python3
"""Web dashboard for ipsw-manager. No external dependencies — stdlib only."""

import collections
import concurrent.futures
import glob
import http.server
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOWNLOAD_DIR = os.environ.get("IPSW_DOWNLOAD_DIR", "/downloads")
HASH_CACHE_DIR = os.environ.get("IPSW_HASH_CACHE_DIR", os.path.join(DOWNLOAD_DIR, ".hash-cache"))
LOG_FILE = os.path.join(DOWNLOAD_DIR, "ipsw-manager.log")
SELECTION_FILE = os.environ.get(
    "IPSW_SELECTION_FILE", os.path.join(DOWNLOAD_DIR, "selected-devices.json")
)
PORT = int(os.environ.get("WEBUI_PORT", "8080"))
API_DEVICES = "https://api.ipsw.me/v4/devices"
DEVICE_CACHE_TTL = 3600  # seconds to keep the ipsw.me device list in memory
IDENTIFIER_RE = re.compile(r"^iPhone(\d+),(\d+)$")

_run_lock = threading.Lock()
_io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ipsw-io")
IO_TIMEOUT = 5  # seconds before giving up on a CIFS read
SPEED_WINDOW = 15  # seconds of .partial size samples used to work out download speed
# Written by ipsw-manager.sh while it downloads: file name, then expected bytes.
PROGRESS_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ipsw-manager-download")

_speed_samples = {}  # .partial file name -> deque of (monotonic time, size)
_speed_lock = threading.Lock()


def _with_timeout(fn, *args, fallback):
    """Run fn(*args) in a thread pool; return fallback if it doesn't finish in IO_TIMEOUT seconds."""
    future = _io_executor.submit(fn, *args)
    try:
        return future.result(timeout=IO_TIMEOUT)
    except concurrent.futures.TimeoutError:
        print(f"WARN: {fn.__name__} timed out — CIFS mount may be unavailable", flush=True)
        return fallback


def is_manager_running():
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            data = (
                Path(cmdline_path)
                .read_bytes()
                .replace(b"\x00", b" ")
                .decode("utf-8", errors="replace")
            )
            if "ipsw-manager.sh" in data:
                return True
        except OSError:
            pass
    return False


def get_log_tail(n=200):
    try:
        lines = Path(LOG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def format_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"


def extract_version(name):
    m = re.search(r"_(\d+\.\d+[\.\d]*)_[0-9A-Z]+_Restore", name)
    return m.group(1) if m else "—"


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


def get_downloads():
    results = []
    download_path = Path(DOWNLOAD_DIR)
    cache_path = Path(HASH_CACHE_DIR)

    try:
        for path in sorted(download_path.glob("*.ipsw")):
            try:
                stat = path.stat()
            except OSError:
                continue
            cache = cache_path / (path.name + ".hash")
            results.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "size_human": format_size(stat.st_size),
                    "mtime": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "status": "complete",
                    "verified": cache.exists(),
                    "version": extract_version(path.name),
                }
            )
    except OSError:
        pass

    partials = []
    expected_name, expected_size = read_expected_size()
    try:
        for path in sorted(download_path.glob("*.ipsw.partial")):
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
            results.append(
                {
                    "name": ipsw_name,
                    "size": stat.st_size,
                    "size_human": format_size(stat.st_size),
                    "mtime": None,
                    "status": "downloading",
                    "verified": False,
                    "version": extract_version(ipsw_name),
                    "speed": speed,
                    "speed_human": format_size(speed) + "/s" if speed is not None else None,
                    "total_human": format_size(total) if total else None,
                    "percent": percent,
                    "eta_human": format_duration(max(eta, 0)) + " left" if eta is not None else None,
                }
            )
    except OSError:
        pass
    forget_speeds(partials)

    return results


def trigger_run(clear_cache=False):
    if is_manager_running():
        return False, "Already running"
    if not _run_lock.acquire(blocking=False):
        return False, "Already running"

    def run():
        try:
            if clear_cache:
                try:
                    for f in Path(HASH_CACHE_DIR).glob("*.hash"):
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
            subprocess.run(
                ["/usr/local/bin/ipsw-manager.sh"],
                start_new_session=True,
            )
        finally:
            _run_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True, "Started"


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


HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPSW Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:10}
h1{font-size:16px;font-weight:600;letter-spacing:-.3px}
#dot{width:9px;height:9px;border-radius:50%;background:#3fb950;flex-shrink:0;transition:background .3s}
#dot.running{background:#f0883e;animation:pulse 1.2s ease-in-out infinite}
#status-lbl{font-size:12px;color:#8b949e}
.spacer{flex:1}
.hdr-actions{display:flex;gap:8px}
button{padding:7px 15px;border-radius:6px;border:1px solid transparent;cursor:pointer;font-size:13px;font-weight:500;transition:opacity .15s,background .15s}
.btn-green{background:#238636;color:#fff;border-color:#238636}
.btn-green:hover:not(:disabled){background:#2ea043}
.btn-gray{background:#21262d;color:#e6edf3;border-color:#30363d}
.btn-gray:hover:not(:disabled){background:#30363d}
button:disabled{opacity:.4;cursor:not-allowed}
main{max-width:1120px;margin:0 auto;padding:24px;display:flex;flex-direction:column;gap:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.card-hdr{padding:13px 18px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between}
.card-title{font-size:13px;font-weight:600}
.card-meta{font-size:12px;color:#8b949e}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:9px 16px;text-align:left;color:#8b949e;font-weight:500;border-bottom:1px solid #21262d;font-size:12px;white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}
.mono{font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:12px}
.speed{display:block;color:#58a6ff;font-size:11px;margin-top:2px}
.bar{height:4px;width:160px;max-width:100%;background:#21262d;border-radius:2px;margin-top:5px;overflow:hidden}
.bar>span{display:block;height:100%;background:#58a6ff;transition:width .5s}
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.badge-green{background:rgba(63,185,80,.13);color:#3fb950}
.badge-blue{background:rgba(88,166,255,.13);color:#58a6ff;animation:pulse 1.5s ease-in-out infinite}
.badge-orange{background:rgba(210,153,34,.13);color:#d2a820}
.log{background:#0d1117;padding:14px 16px;font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:11px;line-height:1.65;max-height:380px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.log .ok{color:#3fb950}
.log .err{color:#f85149}
.log .warn{color:#d2a820}
.log .info{color:#58a6ff}
.log .dim{color:#484f58}
.empty{text-align:center;padding:36px;color:#484f58;font-size:13px}
.dev-tools{padding:12px 18px;border-bottom:1px solid #21262d;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dev-tools input{flex:1;min-width:160px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-size:13px}
.dev-tools button{font-size:12px;padding:5px 11px}
.dev-note{padding:10px 18px 0;font-size:12px;color:#8b949e}
.dev-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:2px 12px;padding:12px 18px 16px;max-height:420px;overflow-y:auto}
.dev-grid label{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:5px;font-size:13px;cursor:pointer}
.dev-grid label:hover{background:rgba(255,255,255,.04)}
.dev-grid input{accent-color:#238636}
.dev-grid .mono{color:#8b949e;font-size:11px;margin-left:auto}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<header>
  <div id="dot"></div>
  <h1>IPSW Manager</h1>
  <span id="status-lbl">Loading…</span>
  <span id="poll-ts" style="font-size:11px;color:#484f58;margin-left:8px"></span>
  <div class="spacer"></div>
  <div class="hdr-actions">
    <button id="btn-run" class="btn-green" onclick="doRun(false)">&#9654; Run Now</button>
    <button id="btn-verify" class="btn-gray" onclick="doRun(true)">&#8635; Re-verify</button>
  </div>
</header>
<main>
  <div class="card">
    <div class="card-hdr">
      <span class="card-title">Downloaded Firmware</span>
      <span class="card-meta" id="last-run-text"></span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Filename</th>
          <th>iOS</th>
          <th>Size</th>
          <th>Modified</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="files-body">
        <tr><td colspan="5" class="empty">Loading…</td></tr>
      </tbody>
    </table>
  </div>
  <div class="card">
    <div class="card-hdr">
      <span class="card-title">Phones to Download</span>
      <span class="card-meta" id="dev-count"></span>
    </div>
    <div class="dev-tools">
      <input id="dev-filter" type="search" placeholder="Filter, e.g. 15 Pro or iPhone8" oninput="renderDevices()">
      <button class="btn-gray" onclick="setVisible(true)">Select all</button>
      <button class="btn-gray" onclick="setVisible(false)">Select none</button>
      <button id="btn-save" class="btn-green" onclick="saveDevices()" disabled>Save</button>
    </div>
    <div class="dev-note" id="dev-note">Unticked phones' firmware is deleted on the next run. Phones Apple no longer signs firmware for are skipped.</div>
    <div class="dev-grid" id="dev-grid"><div class="empty">Loading…</div></div>
  </div>
  <div class="card">
    <div class="card-hdr">
      <span class="card-title">Log</span>
      <button class="btn-gray" style="font-size:11px;padding:4px 10px" onclick="scrollLog()">&#8595; End</button>
    </div>
    <div id="log" class="log"></div>
  </div>
</main>
<script>
const fmt = iso => iso ? new Date(iso).toLocaleString() : "—";

function colorLine(line) {
  const e = line.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  if (/✅|OK|verified|retained|Done/.test(line)) return '<span class="ok">' + e + '</span>';
  if (/❌|failed|mismatch|error/i.test(line)) return '<span class="err">' + e + '</span>';
  if (/⚠|warn|skip/i.test(line)) return '<span class="warn">' + e + '</span>';
  if (/⬇|Downloading|Fetching|fetch/i.test(line)) return '<span class="info">' + e + '</span>';
  if (/^\\s*$/.test(line)) return e;
  return '<span class="dim">' + e + '</span>';
}

async function refresh() {
  document.getElementById("poll-ts").textContent = "polling " + new Date().toLocaleTimeString();
  let d;
  try {
    const resp = await fetch("/api/status");
    d = await resp.json();
    if (d.error) throw new Error(d.error);
  } catch(e) {
    document.getElementById("status-lbl").textContent = "Error";
    document.getElementById("poll-ts").textContent = "fetch failed: " + e.message;
    document.getElementById("dot").className = "";
    return;
  }
  document.getElementById("poll-ts").textContent = "updated " + new Date().toLocaleTimeString();

  const running = d.running;
  document.getElementById("dot").className = running ? "running" : "";
  document.getElementById("status-lbl").textContent = running ? "Running…" : "Idle";
  document.getElementById("btn-run").disabled = running;
  document.getElementById("btn-verify").disabled = running;

  const tbody = document.getElementById("files-body");
  if (!d.downloads.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No IPSW files found in ' + d.download_dir + '</td></tr>';
  } else {
    tbody.innerHTML = d.downloads.map(f => {
      const badge = f.status === "downloading"
        ? '<span class="badge badge-blue">⬇ Downloading</span>'
        : f.verified
          ? '<span class="badge badge-green">✓ Verified</span>'
          : '<span class="badge badge-orange">? Unverified</span>';
      return '<tr>'
        + '<td class="mono">' + f.name + '</td>'
        + '<td>' + (f.version !== "—" ? 'iOS ' + f.version : f.version) + '</td>'
        + '<td class="mono">' + f.size_human + (f.total_human ? ' of ' + f.total_human : '') + speedLabel(f) + '</td>'
        + '<td style="color:#8b949e">' + fmt(f.mtime) + '</td>'
        + '<td>' + badge + '</td>'
        + '</tr>';
    }).join("");
  }

  const logEl = document.getElementById("log");
  const atBottom = logEl.scrollHeight - logEl.scrollTop <= logEl.clientHeight + 50;
  logEl.innerHTML = d.logs.map(colorLine).join("\\n");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  if (d.last_run) {
    document.getElementById("last-run-text").textContent = "Last run: " + d.last_run.replace(/^[^Z]+Z\\s+/, "");
  }
}

function speedLabel(f) {
  if (f.status !== "downloading") return "";
  const parts = [];
  if (f.percent != null) parts.push(Math.floor(f.percent) + "%");
  parts.push(f.speed_human || "measuring…");
  if (f.eta_human) parts.push(f.eta_human);
  const bar = f.percent != null
    ? '<div class="bar"><span style="width:' + f.percent.toFixed(1) + '%"></span></div>'
    : "";
  return bar + '<span class="speed">' + parts.join(" · ") + '</span>';
}

async function doRun(clearCache) {
  if (clearCache && !confirm(
    "Re-verify will clear the hash cache and re-download all firmware files to re-verify checksums.\\n\\nThis may take a long time. Continue?"
  )) return;

  document.getElementById("btn-run").disabled = true;
  document.getElementById("btn-verify").disabled = true;

  let d;
  try {
    d = await (await fetch("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({clear_cache: clearCache})
    })).json();
  } catch (e) {
    alert("Request failed: " + e);
    return;
  }

  if (!d.success) {
    alert(d.message);
    document.getElementById("btn-run").disabled = false;
    document.getElementById("btn-verify").disabled = false;
  } else {
    setTimeout(refresh, 800);
  }
}

function scrollLog() {
  const el = document.getElementById("log");
  el.scrollTop = el.scrollHeight;
}

const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
let allDevices = [];
let selected = new Set();
let savedSelection = "";

function selectionKey() { return [...selected].sort().join(","); }

function updateDeviceMeta() {
  document.getElementById("dev-count").textContent = selected.size + " of " + allDevices.length + " selected";
  const dirty = selectionKey() !== savedSelection;
  document.getElementById("btn-save").disabled = !dirty || selected.size === 0;
  document.getElementById("btn-save").textContent = dirty ? "Save" : "Saved";
}

function visibleDevices() {
  const q = document.getElementById("dev-filter").value.trim().toLowerCase();
  if (!q) return allDevices;
  return allDevices.filter(d => (d.name + " " + d.identifier).toLowerCase().includes(q));
}

function renderDevices() {
  const list = visibleDevices();
  const grid = document.getElementById("dev-grid");
  grid.innerHTML = list.length ? list.map(d =>
    '<label><input type="checkbox" data-id="' + esc(d.identifier) + '"'
    + (selected.has(d.identifier) ? " checked" : "") + '>'
    + '<span>' + esc(d.name) + '</span><span class="mono">' + esc(d.identifier) + '</span></label>'
  ).join("") : '<div class="empty">No phones match</div>';
  updateDeviceMeta();
}

document.getElementById("dev-grid").addEventListener("change", e => {
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
    selected = new Set(d.selected);
    savedSelection = d.saved ? selectionKey() : "";
    if (!d.saved) {
      document.getElementById("dev-note").textContent =
        "No selection saved yet, so iPhone 11 and newer are downloaded by default. Unticked phones' firmware is deleted on the next run.";
    }
    renderDevices();
  } catch (e) {
    document.getElementById("dev-grid").innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  }
}

async function saveDevices() {
  const btn = document.getElementById("btn-save");
  btn.disabled = true;
  try {
    const d = await (await fetch("/api/devices", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({devices: [...selected]})
    })).json();
    if (!d.success) throw new Error(d.message);
    savedSelection = selectionKey();
    document.getElementById("dev-note").textContent =
      "Saved. Takes effect on the next run. Unticked phones' firmware is deleted on the next run.";
  } catch (e) {
    alert("Save failed: " + e.message);
  }
  updateDeviceMeta();
}

loadDevices();
refresh();
setInterval(refresh, 5000);
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            self._handle_get()
        except Exception as exc:
            print(f"ERROR in GET {self.path}: {exc}", flush=True)
            try:
                self.send_json({"error": str(exc)}, status=500)
            except Exception:
                pass

    def _handle_get(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            running = is_manager_running()
            downloads = _with_timeout(get_downloads, fallback=[])
            logs = _with_timeout(get_log_tail, 200, fallback=["[log unavailable — mount may be offline]"])
            last_run = None
            for line in reversed(logs):
                if "Done" in line and ("retained" in line or "removed" in line):
                    last_run = line.strip()
                    break
            self.send_json(
                {
                    "running": running,
                    "downloads": downloads,
                    "logs": logs,
                    "last_run": last_run,
                    "download_dir": DOWNLOAD_DIR,
                }
            )
        elif self.path == "/api/devices":
            try:
                devices = get_all_iphones()
            except Exception as exc:
                self.send_json({"error": f"Couldn't fetch device list from ipsw.me: {exc}"}, status=502)
                return
            saved = _with_timeout(read_selection, fallback=None)
            self.send_json(
                {
                    "devices": devices,
                    "selected": saved if saved is not None else default_selection(devices),
                    "saved": saved is not None,
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        if self.path == "/api/run":
            ok, msg = trigger_run(bool(data.get("clear_cache", False)))
            self.send_json({"success": ok, "message": msg})
        elif self.path == "/api/devices":
            selected = data.get("devices") if isinstance(data, dict) else None
            if not isinstance(selected, list) or not all(
                isinstance(d, str) and IDENTIFIER_RE.match(d) for d in selected
            ):
                self.send_json({"success": False, "message": "Invalid device list"}, status=400)
                return
            if not selected:
                self.send_json({"success": False, "message": "Select at least one phone"}, status=400)
                return
            try:
                write_selection(set(selected))
            except OSError as exc:
                self.send_json({"success": False, "message": f"Couldn't save: {exc}"}, status=500)
                return
            self.send_json({"success": True, "message": f"Saved {len(set(selected))} phones"})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Web UI → http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
