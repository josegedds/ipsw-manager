# ipsw-manager

Keeps a folder of the latest signed iPhone firmware (IPSW) files, with a small web dashboard.

Docker image: [`josephgeddes/ipsw-manager`](https://hub.docker.com/r/josephgeddes/ipsw-manager)

## How it works

- On a cron schedule (default: every day at 18:00 GMT), `ipsw-manager.sh` asks [ipsw.me](https://ipsw.me) for the latest signed, non-beta firmware for each selected iPhone.
- Phones that share a firmware file are de-duplicated, so each IPSW downloads once.
- Each download is checked against its MD5 or SHA-256 checksum. Verified hashes are cached (in `.hash-cache/` in the download folder) so later runs don't re-read the files.
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
  -p 8080:8080 \
  -v /path/to/ipsw:/downloads \
  josephgeddes/ipsw-manager:latest
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `IPSW_DOWNLOAD_DIR` | `/downloads` | Where IPSWs, the log and the selection are stored |
| `IPSW_CHECKSUM_ALGO` | `md5` | `md5` or `sha256` |
| `IPSW_SELECTION_FILE` | `$IPSW_DOWNLOAD_DIR/selected-devices.json` | Saved phone selection |
| `IPSW_HASH_CACHE_DIR` | `$IPSW_DOWNLOAD_DIR/.hash-cache` | Cache of verified checksums |
| `CRON_SCHEDULE` | `0 18 * * *` | When to run (cron syntax, UTC) |
| `WEBUI_PORT` | `8080` | Web UI port |

The schedule uses cron syntax, read by [supercronic](https://github.com/aptible/supercronic), and the container runs in UTC. To run at 06:00 instead:

```bash
docker run -d --name ipsw-manager \
  -p 8080:8080 \
  -e CRON_SCHEDULE="0 6 * * *" \
  -v /path/to/ipsw:/downloads \
  josephgeddes/ipsw-manager:latest
```

## Releasing a new version

```bash
docker build -t josephgeddes/ipsw-manager:1.2 -t josephgeddes/ipsw-manager:latest .
docker push josephgeddes/ipsw-manager:1.2
docker push josephgeddes/ipsw-manager:latest
```
