#!/usr/bin/env python3
"""Checks an IPSW as it is stored in the download folder. Stdlib only.

ipsw-manager.sh hashes a download as it arrives, which proves the bytes curl
received were right. It says nothing about the copy that reached the NAS: a
write that was dropped or damaged on the way leaves a file of the right size
with the wrong contents. This reads the stored file back and checks it.

Three things make that dependable on a network mount:

- The read goes past the page cache (O_DIRECT, or dropping the file's cached
  pages where the file system has no O_DIRECT). Otherwise a file that was just
  written is "read back" from this machine's memory, not from the NAS.
- A read that makes no progress for --stall seconds ends the check, rather
  than hanging until the run's own timeout.
- A mismatch is read a second time before the file is called damaged, so one
  bad read doesn't throw away a good file.

Without a published checksum (--algo zip) the file is tested as the ZIP
archive an IPSW is: every member is read and its CRC checked.

Prints one line, "<status> <hash> <details>", and exits with:
  0  ok          the file matches
     reread-ok   the first read was wrong, the second matched
  1  mismatch    the file is damaged (read twice, same wrong result)
  2  unstable    two reads gave two different wrong results
     unreadable  the file couldn't be read, or the read stalled
"""

import argparse
import errno
import hashlib
import io
import json
import mmap
import os
import queue
import sys
import threading
import time
import zipfile

CHUNK = 8 * 1024 * 1024
BUFFERS = 3  # chunks in memory at once while a file is hashed
SPEED_WINDOW = 10  # seconds of progress used to work out the read speed

EXIT_OK, EXIT_MISMATCH, EXIT_UNREADABLE = 0, 1, 2


def finish(status, digest="-", details=""):
    """Print the result line and exit. Safe to call from the watchdog thread."""
    code = {"ok": EXIT_OK, "reread-ok": EXIT_OK, "mismatch": EXIT_MISMATCH}.get(status, EXIT_UNREADABLE)
    try:
        sys.stdout.write(f"{status} {digest} {details}".rstrip() + "\n")
        sys.stdout.flush()
    finally:
        # Not sys.exit: the main thread may be stuck in a read that never returns.
        os._exit(code)


class Progress:
    """Tracks bytes read; a background thread reports them and catches a stalled read."""

    def __init__(self, name, total, path, stall):
        self.name, self.total, self.path, self.stall = name, total, path, stall
        self.done = 0
        self.passes = 1
        self.last_progress = time.monotonic()
        self.started = self.last_progress
        self._samples = [(self.last_progress, 0)]
        threading.Thread(target=self._watch, daemon=True).start()

    def add(self, n):
        self.done += n
        self.last_progress = time.monotonic()

    def restart(self):
        self.done = 0
        self.passes += 1
        self.last_progress = time.monotonic()
        self._samples = [(self.last_progress, 0)]

    def speed(self):
        now = time.monotonic()
        self._samples.append((now, self.done))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW:
            self._samples.pop(0)
        then, was = self._samples[0]
        return (self.done - was) / (now - then) if now - then >= 1 else None

    def _watch(self):
        while True:
            time.sleep(1)
            idle = time.monotonic() - self.last_progress
            if self.stall and idle > self.stall:
                self.clear()
                finish("unreadable", details=f"no data from the download folder for {int(idle)} s")
            self._write()

    def _write(self):
        if not self.path:
            return
        state = {"name": self.name, "done": self.done, "total": self.total,
                 "speed": self.speed(), "pass": self.passes}
        try:
            tmp = f"{self.path}.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # only the web UI's progress display depends on it

    def clear(self):
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def drop_cache(fd, offset=0, length=0):
    """Forget this machine's cached copy of the file, so reads come from where it's stored."""
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def flush(fd):
    """Push anything still waiting to be written; only then can its cached pages be dropped."""
    try:
        os.fsync(fd)
    except OSError:
        pass  # some mounts refuse fsync on a file opened for reading


def open_for_check(path, direct):
    """Returns (fd, is_direct)."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if direct and hasattr(os, "O_DIRECT"):
        try:
            return os.open(path, flags | os.O_DIRECT), True
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
                raise
    return os.open(path, flags), False


def read_chunks(path, free, full):
    """Reader thread: fills buffers from `free` with the file and passes them to `full`.

    Each item put on `full` is (buffer, bytes read), with 0 bytes at the end of
    the file, or the exception that stopped the read. A None from `free` means
    stop early.
    """
    fd = None
    try:
        fd, direct = open_for_check(path, direct=True)
        flush(fd)
        if not direct:
            drop_cache(fd)
        offset = 0
        while True:
            buf = free.get()
            if buf is None:
                return
            try:
                n = os.readv(fd, [buf])
            except OSError as exc:
                # Some file systems accept the O_DIRECT open and then refuse the
                # read. Carry on from the same place without it.
                if direct and exc.errno in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
                    os.close(fd)
                    fd = None
                    fd, direct = open_for_check(path, direct=False)
                    flush(fd)
                    drop_cache(fd)
                    os.lseek(fd, offset, os.SEEK_SET)
                    free.put(buf)
                    continue
                raise
            if n and not direct:
                drop_cache(fd, offset, n)  # keeps a multi-GB file out of memory, too
            full.put((buf, n))
            if n == 0:
                return
            offset += n
    except BaseException as exc:  # handed to the hashing thread, which reports it
        full.put(exc)
    finally:
        if fd is not None:
            os.close(fd)


def hash_file(path, algo, progress):
    """Hash of the whole file as stored. Returns (hex digest, bytes read).

    A reader thread fetches the next chunk while this one hashes the last, so
    the disk or network and the CPU work at the same time instead of taking
    turns. Both os.readv and hashlib release the GIL for chunks this size.
    """
    digest = hashlib.new(algo)
    # Page-aligned, which O_DIRECT needs: one being hashed, one being read,
    # and one ready for the reader so it never waits on the hash.
    buffers = [mmap.mmap(-1, CHUNK) for _ in range(BUFFERS)]
    free, full = queue.Queue(), queue.Queue()
    for buf in buffers:
        free.put(buf)
    reader = threading.Thread(target=read_chunks, args=(path, free, full), daemon=True)
    reader.start()
    total = 0
    try:
        while True:
            item = full.get()
            if isinstance(item, BaseException):
                raise item
            buf, n = item
            if n == 0:
                break
            with memoryview(buf) as view, view[:n] as data:
                digest.update(data)
            total += n
            progress.add(n)
            free.put(buf)
    finally:
        free.put(None)  # stops the reader if it's still going
        reader.join()
        for buf in buffers:
            buf.close()
    return digest.hexdigest(), total


class CountingFile(io.FileIO):
    """A file that reports what's read from it, for the ZIP test."""

    def __init__(self, path, progress):
        super().__init__(path, "rb")
        self._progress = progress

    def readinto(self, b):
        n = super().readinto(b)
        if n:
            self._progress.add(n)
        return n


def test_zip(path, progress):
    """Returns None if the file is a sound IPSW archive, or what's wrong with it."""
    with CountingFile(path, progress) as raw:
        flush(raw.fileno())
        drop_cache(raw.fileno())
        try:
            with zipfile.ZipFile(io.BufferedReader(raw, CHUNK)) as archive:
                if "BuildManifest.plist" not in archive.namelist():
                    return "it has no BuildManifest.plist, so it isn't an IPSW"
                try:
                    bad = archive.testzip()
                except NotImplementedError:
                    return None  # a compression method Python can't read: the listing is all we can check
                if bad is not None:
                    return f"{bad} inside it is damaged"
        except (zipfile.BadZipFile, EOFError) as exc:
            return f"it isn't a valid archive ({exc})"
    return None


def rate(nbytes, seconds):
    return f"{nbytes / max(seconds, 0.001) / 1e6:.0f} MB/s"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("file")
    parser.add_argument("--algo", required=True, help="md5, sha1, sha256 or zip")
    parser.add_argument("--expect", default="-", help="the checksum the file should have")
    parser.add_argument("--name", help="file name to show in the web UI")
    parser.add_argument("--progress", help="file to write progress to, as JSON")
    parser.add_argument("--stall", type=int, default=300, help="give up after this many seconds without data; 0 = never")
    args = parser.parse_args()

    if args.algo != "zip" and args.algo not in hashlib.algorithms_available:
        finish("unreadable", details=f"unknown checksum type {args.algo}")
    expected = args.expect.strip().lower()

    # The watchdog starts before the file is touched: on a hung mount even the
    # stat can block.
    progress = Progress(args.name or os.path.basename(args.file), 0, args.progress, args.stall)
    try:
        progress.total = size = os.stat(args.file).st_size
        if size == 0:
            progress.clear()
            finish("mismatch", details="the file is empty")

        if args.algo == "zip":
            started = time.monotonic()
            problem = test_zip(args.file, progress)
            progress.clear()
            if problem:
                finish("mismatch", details=problem)
            finish("ok", details=rate(size, time.monotonic() - started))

        started = time.monotonic()
        first, nread = hash_file(args.file, args.algo, progress)
        speed = rate(nread, time.monotonic() - started)
        if nread != size:
            progress.clear()
            finish("unreadable", first, f"the file changed size while it was being read ({size} to {nread} bytes)")
        if first == expected:
            progress.clear()
            finish("ok", first, speed)

        # Wrong. Is it the file, or was it the read?
        progress.restart()
        second, _ = hash_file(args.file, args.algo, progress)
        progress.clear()
        if second == expected:
            finish("reread-ok", second, speed)
        if second == first:
            finish("mismatch", first, "read twice with the same result")
        finish("unstable", second, f"two reads gave different results ({first} then {second})")
    except OSError as exc:
        progress.clear()
        finish("unreadable", details=f"{exc.strerror or exc} ({errno.errorcode.get(exc.errno, exc.errno)})")


if __name__ == "__main__":
    main()
