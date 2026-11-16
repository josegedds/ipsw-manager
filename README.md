# ipsw-manager

Keeps a folder of the latest signed iPhone firmware (IPSW) files, with a small web dashboard.

Docker image: [`josephgeddes/ipsw-manager`](https://hub.docker.com/r/josephgeddes/ipsw-manager)

## How it works

- Every 30 minutes (`CHECK_INTERVAL`), `ipsw-manager.sh --if-new` asks [ipsw.me](https://ipsw.me) for the latest signed, non-beta firmware for each selected iPhone. A run starts only when that differs from the last run: Apple has released a new iOS version, stopped signing an old one, or you changed the phone selection. Otherwise the check does nothing and writes nothing to the log.
- A run downloads whatever is new and removes what is no longer current.
- Phones that share a firmware file are de-duplicated, so each IPSW downloads once.
- A file counts as verified only when the copy in the download folder has been read back and matched its checksum. See [Verification](#verification).
- Only one run happens at a time; one that starts while another is going is skipped.
- IPSWs that are no longer current, or belong to phones you've unticked, are deleted.

## Web UI

Open `http://<host>:8080` to:

- see downloaded firmware and the run log
- start a run (**Run Now**), or read every stored file back and check it now (**Re-verify**)
- choose which phones to download under **Phones to Download**
- see the **Release Calendar**: public iOS releases (from ipsw.me), betas and release candidates (from Apple's developer releases feed), and which versions your phones can install now. Apple doesn't announce release dates, so once a release candidate is out the calendar shows the public release as expected about a week later. That date is an estimate.

The selection is saved to `selected-devices.json` in the download folder. Until you save one, iPhone 11 and newer are downloaded.

## Verification

Each firmware file is checked twice against the SHA-256 (or MD5) checksum ipsw.me publishes for it:

1. **As it downloads.** This shows the download arrived intact.
2. **After it's saved.** The file is read back from the download folder and hashed again. This is the check that matters on a NAS: a write that is dropped or damaged on the way there leaves a file of the right size with the wrong contents, and only reading it back shows that. The file keeps its `.partial` name until it passes, so a damaged one never appears as an IPSW.

The read-back bypasses the computer's own file cache, so it tests what the NAS holds rather than what is still in memory from the download. A read that returns nothing for five minutes is abandoned instead of hanging the run. If the result is wrong the file is read a second time before it's called damaged, and the log says whether the file or the connection is at fault.

After that:

- Every run checks each stored file is still the size it was when verified.
- Once a file's last check is `IPSW_REVERIFY_DAYS` old (30 by default), a run starts to read it back again. This catches a file that has gone bad on the NAS since.
- **Re-verify** in the web UI, or `docker exec ipsw-manager ipsw-manager.sh --reverify`, reads every file back now.
- A file that fails is deleted and downloaded again. A file that can't be read at all (the NAS is offline, say) is left alone and reported.
- If ipsw.me publishes no checksum for a file, the file is tested as the ZIP archive an IPSW is: every member is read and its CRC checked.

Reading back takes about as long as copying the file from the NAS: over gigabit Ethernet, roughly 10 seconds per GB, so a minute or two per IPSW. Checks of files already in the folder are done after any downloads, so new firmware isn't kept waiting.

The web UI shows **Verified** only for files that have passed the read-back, with the date in the badge's tooltip. Results are kept in `.hash-cache/` in the download folder.

**Upgrading.** Earlier versions only did the first check. The first run after upgrading reads every existing file back once (they show as **Not checked** until then) and replaces any that fail. Nothing is downloaded again unless it fails.

### Docker Desktop for Windows with a NAS

Mount the NAS share as a Docker volume over SMB instead of binding a mapped drive or a `\\nas\share` path. [`compose.yml`](compose.yml) has an example to uncomment. With a volume, the Linux side of Docker talks to the NAS directly. With a bound Windows path every read and write also passes through Windows' own file sharing and its cache, so a check can be answered from the PC's memory, and writes Windows is still holding can fail after the file has been checked.

With `docker run`, create the volume first and use `-v ipsw:/downloads`:

```bash
docker volume create --driver local --opt type=cifs --opt device=//192.168.1.10/ipsw --opt o=addr=192.168.1.10,username=USER,password=PASSWORD,vers=3.0,uid=1000,gid=1000,file_mode=0664,dir_mode=0775 ipsw
```

If the log reports files damaged on the way to the NAS, or reads that differ from one attempt to the next, the fault is in the path between the PC and the NAS: the cable, a switch port, the network adapter or its driver, or the NAS's disks or memory.

## Running

```bash
docker run -d --name ipsw-manager \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PUID=1000 -e PGID=1000 \
  -e TZ=Europe/London \
  -v /path/to/ipsw:/downloads \
  josephgeddes/ipsw-manager:latest
```

Or with Docker Compose: edit the volume path and settings in [`compose.yml`](compose.yml), then run `docker compose up -d`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHECK_INTERVAL` | `30` | Minutes between checks for new firmware, 1 to 60. `0` turns the checks off |
| `CRON_SCHEDULE` | unset | Optional: also do a full run at fixed times (cron syntax, in `TZ`), e.g. `0 18 * * *` |
| `IPSW_RETRY_MINUTES` | `60` | After a run that didn't finish cleanly, minutes the checks wait (from when it ended) before trying the same firmware again |
| `TZ` | `UTC` | Time zone for `CRON_SCHEDULE`, e.g. `Europe/London` |
| `PUID` / `PGID` | unset (root) | User and group to run as, so files on the NAS aren't owned by root. `PGID` defaults to `PUID` |
| `IPSW_DOWNLOAD_DIR` | `/downloads` | Where IPSWs, the log and the selection are stored |
| `IPSW_CHECKSUM_ALGO` | `sha256` | `sha256` or `md5` |
| `IPSW_REVERIFY_DAYS` | `30` | Read a stored file back and check it again once its last check is this many days old. `0` checks a file only when it's downloaded |
| `IPSW_SELECTION_FILE` | `$IPSW_DOWNLOAD_DIR/selected-devices.json` | Saved phone selection |
| `IPSW_HASH_CACHE_DIR` | `$IPSW_DOWNLOAD_DIR/.hash-cache` | Record of which files were verified, and when |
| `IPSW_RUN_TIMEOUT` | `12h` | Stop a run that takes longer than this (`timeout` syntax, e.g. `90m`). `0` turns it off |
| `WEBUI_PORT` | `8080` | Web UI port |

**When runs happen.** Apple doesn't notify anyone of a release, so "run when a new iOS version is out" means asking often. A check is one small request to ipsw.me per selected phone and doesn't touch the NAS beyond reading the phone selection. A new version is picked up within `CHECK_INTERVAL` minutes of ipsw.me listing it. The first check after the container starts always does a run. A run also starts when stored files are due to be read back and checked again (`IPSW_REVERIFY_DAYS`). A check never notices files deleted from the download folder by hand; use **Run Now** for that, or set `CRON_SCHEDULE` for a regular full run as well. To go back to runs at fixed times only, set `CHECK_INTERVAL=0` and `CRON_SCHEDULE`.

Both are run by [supercronic](https://github.com/aptible/supercronic). The log's timestamps are always in UTC.

**Running as a user.** With `PUID` set, the download folder must be writable by that user; the container warns at startup if it isn't. If you previously ran as root, fix the existing files once on the host:

```bash
sudo chown -R 1000:1000 /path/to/ipsw
```

**Restarts and health.** If the web UI or the scheduler stops, the container exits so `--restart unless-stopped` brings it back. A health check polls the web UI every minute, and `docker ps` shows the result.

**Access.** The web UI has no login, and anyone who can reach it can start runs and change the phone list. To make it reachable only from the host itself, publish the port on localhost: `-p 127.0.0.1:8080:8080`.

**If ipsw.me is unreachable.** Lookups are retried. If any phone's lookup still fails, that run downloads what it can but skips the cleanup, so that phone's IPSW isn't deleted.

**If a download fails.** The run carries on with the other phones but skips the cleanup, so a phone whose new firmware didn't download keeps its current IPSW. The same goes for a download that fails its check, and for a stored file that can't be read back. A download that stalls (under 1 KB/s for two minutes) is abandoned. The checks try again an hour later (`IPSW_RETRY_MINUTES`), or sooner if the firmware changes again, and so does **Run Now** or a `CRON_SCHEDULE` run.

**If the NAS goes offline.** A run that can't write to the download folder stops without deleting anything. A run that hangs on it is stopped after `IPSW_RUN_TIMEOUT`, so later runs aren't blocked behind it. Either way the web UI's **Last run** says what happened, even while the NAS is unreachable, and so does `docker logs`.

**The `.ipsw-manager` marker file.** The first run puts a `.ipsw-manager` file in the download folder. If the NAS isn't mounted when the container starts, Docker gives it the empty folder underneath instead. Without this check, the run would then fill the host's disk with IPSWs. When the folder is empty and the marker is missing, the run stops and changes nothing. Don't delete the marker. If you really have emptied the folder or pointed it somewhere new, recreate the marker:

```bash
docker exec ipsw-manager touch /downloads/.ipsw-manager
```

The check relies on the container remembering that it has seen the marker, so it doesn't protect the very first run of a newly created container.

## Releasing a new version

Push a version tag from `main`:

```bash
git tag v1.2.0
```

```bash
git push origin v1.2.0
```

The [Release image](.github/workflows/release.yml) workflow builds for Intel and ARM (most NAS boxes are one or the other) and pushes `josephgeddes/ipsw-manager:1.2.0`, `:1.2` and `:latest` to Docker Hub. Pre-release tags like `v1.2.0-rc1` don't move `:latest`. Follow the run under the repository's **Actions** tab.

The workflow needs two repository secrets, `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with Read & Write permission).

To build and push by hand instead:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg VERSION=1.2.0 \
  -t josephgeddes/ipsw-manager:1.2.0 -t josephgeddes/ipsw-manager:latest \
  --push .
```

The base image is pinned by digest in the `Dockerfile`. To move to a newer Alpine, update both the tag and the digest:

```bash
docker buildx imagetools inspect alpine:3.24 --format '{{json .Manifest.Digest}}'
```
