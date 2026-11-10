# ipsw-manager

Keeps a folder of the latest signed iPhone firmware (IPSW) files, with a small web dashboard.

Docker image: [`josephgeddes/ipsw-manager`](https://hub.docker.com/r/josephgeddes/ipsw-manager)

## How it works

- Every 30 minutes (`CHECK_INTERVAL`), `ipsw-manager.sh --if-new` asks [ipsw.me](https://ipsw.me) for the latest signed, non-beta firmware for each selected iPhone. A run starts only when that differs from the last run: Apple has released a new iOS version, stopped signing an old one, or you changed the phone selection. Otherwise the check does nothing and writes nothing to the log.
- A run downloads whatever is new and removes what is no longer current.
- Phones that share a firmware file are de-duplicated, so each IPSW downloads once.
- Each download is checked against its MD5 or SHA-256 checksum, and the saved file is checked to be complete. Verified hashes and sizes are cached (in `.hash-cache/` in the download folder) so later runs don't re-read the files. A file whose size has changed since it was verified is downloaded again.
- Only one run happens at a time; one that starts while another is going is skipped.
- IPSWs that are no longer current, or belong to phones you've unticked, are deleted.

## Web UI

Open `http://<host>:8080` to:

- see downloaded firmware and the run log
- start a run (**Run Now**) or clear the hash cache and re-verify everything (**Re-verify**)
- choose which phones to download under **Phones to Download**
- see the **Release Calendar**: public iOS releases (from ipsw.me), betas and release candidates (from Apple's developer releases feed), and which versions your phones can install now. Apple doesn't announce release dates, so once a release candidate is out the calendar shows the public release as expected about a week later. That date is an estimate.

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
| `CHECK_INTERVAL` | `30` | Minutes between checks for new firmware, 1 to 60. `0` turns the checks off |
| `CRON_SCHEDULE` | unset | Optional: also do a full run at fixed times (cron syntax, in `TZ`), e.g. `0 18 * * *` |
| `IPSW_FALLBACK_MINUTES` | `120` | Minutes ipsw.me has to be unreachable before the checks go by Apple's own firmware feed instead. `0` switches straight away |
| `IPSW_RETRY_MINUTES` | `60` | After a run that didn't finish cleanly, minutes the checks wait (from when it ended) before trying the same firmware again |
| `TZ` | `UTC` | Time zone for `CRON_SCHEDULE`, e.g. `Europe/London` |
| `PUID` / `PGID` | unset (root) | User and group to run as, so files on the NAS aren't owned by root. `PGID` defaults to `PUID` |
| `IPSW_DOWNLOAD_DIR` | `/downloads` | Where IPSWs, the log and the selection are stored |
| `IPSW_CHECKSUM_ALGO` | `md5` | `md5` or `sha256` |
| `IPSW_SELECTION_FILE` | `$IPSW_DOWNLOAD_DIR/selected-devices.json` | Saved phone selection |
| `IPSW_HASH_CACHE_DIR` | `$IPSW_DOWNLOAD_DIR/.hash-cache` | Cache of verified checksums |
| `IPSW_RUN_TIMEOUT` | `12h` | Stop a run that takes longer than this (`timeout` syntax, e.g. `90m`). `0` turns it off |
| `WEBUI_PORT` | `8080` | Web UI port |

**When runs happen.** Apple doesn't notify anyone of a release, so "run when a new iOS version is out" means asking often. A check is one small request to ipsw.me per selected phone and doesn't touch the NAS beyond reading the phone selection. A new version is picked up within `CHECK_INTERVAL` minutes of ipsw.me listing it. The first check after the container starts always does a run. A check never notices files deleted from the download folder by hand; use **Run Now** for that, or set `CRON_SCHEDULE` for a regular full run as well. To go back to runs at fixed times only, set `CHECK_INTERVAL=0` and `CRON_SCHEDULE`.

Both are run by [supercronic](https://github.com/aptible/supercronic). The log's timestamps are always in UTC.

**Running as a user.** With `PUID` set, the download folder must be writable by that user; the container warns at startup if it isn't. If you previously ran as root, fix the existing files once on the host:

```bash
sudo chown -R 1000:1000 /path/to/ipsw
```

**Restarts and health.** If the web UI or the scheduler stops, the container exits so `--restart unless-stopped` brings it back. A health check polls the web UI every minute, and `docker ps` shows the result.

**Access.** The web UI has no login, and anyone who can reach it can start runs and change the phone list. To make it reachable only from the host itself, publish the port on localhost: `-p 127.0.0.1:8080:8080`.

**If ipsw.me is unreachable.** Lookups are retried. For any phone whose lookup still fails, the run uses Apple's own firmware feed instead (`s.mzstatic.com/version`, the list Finder reads when it restores a phone) and says so in the log. The feed has the current IPSW for every iPhone. Files already in the folder are kept, not downloaded again.

- **Run Now** and `CRON_SCHEDULE` runs switch to Apple's feed straight away. The checks wait until ipsw.me has been unreachable for two hours (`IPSW_FALLBACK_MINUTES`). Apple's feed occasionally lists an older build for a phone than ipsw.me does, and switching over and back means downloading both, so a short outage is waited out.
- Apple's feed has SHA-1 checksums only, so downloads made from it are verified with SHA-1 whatever `IPSW_CHECKSUM_ALGO` says. It has no file sizes, so the size is asked of the download server.
- In the web UI, the phone list shows identifiers without names if it has never loaded from ipsw.me, and the calendar shows only the last few months of public releases, from Apple's developer feed.
- If neither source answers for a phone, the run downloads what it can but skips the cleanup, so that phone's IPSW isn't deleted.

**Downloads come from Apple only.** A firmware URL is used only if it is an `.ipsw` file on `apple.com` or `cdn-apple.com` (or a subdomain). Anything else is treated as a failed lookup and looked up in Apple's feed instead. ipsw.me supplies both the URL and the checksum, so without this a wrong or hijacked answer could put someone else's file in the folder and have the cleanup delete the real ones.

**If a download fails.** The run carries on with the other phones but skips the cleanup, so a phone whose new firmware didn't download keeps its current IPSW. A download that stalls (under 1 KB/s for two minutes) is abandoned. The checks try again an hour later (`IPSW_RETRY_MINUTES`), or sooner if the firmware changes again, and so does **Run Now** or a `CRON_SCHEDULE` run.

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
