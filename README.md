# ipsw-manager

Keeps a folder of the latest signed iPhone firmware (IPSW) files, with a small web dashboard.

Docker image: [`josephgeddes/ipsw-manager`](https://hub.docker.com/r/josephgeddes/ipsw-manager)

## How it works

- On a cron schedule (default: every day at 18:00 GMT), `ipsw-manager.sh` asks [ipsw.me](https://ipsw.me) for the latest signed, non-beta firmware for each selected iPhone.
- Phones that share a firmware file are de-duplicated, so each IPSW downloads once.
- Each download is checked against its MD5 or SHA-256 checksum, and the saved file is checked to be complete. Verified hashes and sizes are cached (in `.hash-cache/` in the download folder) so later runs don't re-read the files. A file whose size has changed since it was verified is downloaded again.
- Only one run happens at a time; a scheduled run that starts while another is going is skipped.
- IPSWs that are no longer current, or belong to phones you've unticked, are deleted.

## Web UI

Open `http://<host>:8080` to:

- see downloaded firmware and the run log
- start a run (**Run Now**) or clear the hash cache and re-verify everything (**Re-verify**)
- choose which phones to download under **Phones to Download**

The selection is saved to `selected-devices.json` in the download folder. Until you save one, iPhone 11 and newer are downloaded.

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
| `CRON_SCHEDULE` | `0 18 * * *` | When to run (cron syntax, in `TZ`) |
| `TZ` | `UTC` | Time zone for the schedule, e.g. `Europe/London` |
| `PUID` / `PGID` | unset (root) | User and group to run as, so files on the NAS aren't owned by root. `PGID` defaults to `PUID` |
| `IPSW_DOWNLOAD_DIR` | `/downloads` | Where IPSWs, the log and the selection are stored |
| `IPSW_CHECKSUM_ALGO` | `md5` | `md5` or `sha256` |
| `IPSW_SELECTION_FILE` | `$IPSW_DOWNLOAD_DIR/selected-devices.json` | Saved phone selection |
| `IPSW_HASH_CACHE_DIR` | `$IPSW_DOWNLOAD_DIR/.hash-cache` | Cache of verified checksums |
| `IPSW_RUN_TIMEOUT` | `12h` | Stop a run that takes longer than this (`timeout` syntax, e.g. `90m`). `0` turns it off |
| `WEBUI_PORT` | `8080` | Web UI port |

The schedule is read by [supercronic](https://github.com/aptible/supercronic). The log's timestamps are always in UTC.

**Running as a user.** With `PUID` set, the download folder must be writable by that user; the container warns at startup if it isn't. If you previously ran as root, fix the existing files once on the host:

```bash
sudo chown -R 1000:1000 /path/to/ipsw
```

**Restarts and health.** If the web UI or the scheduler stops, the container exits so `--restart unless-stopped` brings it back. A health check polls the web UI every minute, and `docker ps` shows the result.

**Access.** The web UI has no login, and anyone who can reach it can start runs and change the phone list. To make it reachable only from the host itself, publish the port on localhost: `-p 127.0.0.1:8080:8080`.

**If ipsw.me is unreachable.** Lookups are retried. If any phone's lookup still fails, that run downloads what it can but skips the cleanup, so that phone's IPSW isn't deleted.

**If a download fails.** The run carries on with the other phones but skips the cleanup, so a phone whose new firmware didn't download keeps its current IPSW. A download that stalls (under 1 KB/s for two minutes) is abandoned. The next run tries again.

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
