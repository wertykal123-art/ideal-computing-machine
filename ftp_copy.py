#!/usr/bin/env python3
"""Move folders from one FTP server to another, driven by a JSON config.

For every folder found in the source directory the script:
  1. copies the whole folder (recursively) to the destination server,
  2. moves the folder into an archive directory on the source server,
     stamped with the time it was archived.

Archived folders older than the configured retention are deleted on every
run. The script can run once (e.g. from cron) or keep running and repeat
on an interval — both controlled by the config file.

Usage:
    python ftp_copy.py --config config.json
    python ftp_copy.py --config config.json --dry-run

See config.example.json for the config format.
"""

import argparse
import json
import posixpath
import re
import sys
import time
from datetime import datetime
from ftplib import FTP, FTP_TLS, error_perm

ARCHIVE_STAMP_FORMAT = "%Y%m%d-%H%M%S"
ARCHIVE_STAMP_RE = re.compile(r"__(\d{8}-\d{6})$")

CONFIG_DEFAULTS = {
    "source": {"port": 21, "user": "anonymous", "password": "", "tls": False,
               "dir": "/", "archive_dir": "/archive"},
    "destination": {"port": 21, "user": "anonymous", "password": "",
                    "tls": False, "dir": "/"},
    "archive_retention_hours": 168,
    "poll_interval_seconds": 0,
    "chunk_size": 65536,
    "timeout": 30,
}


def load_config(path):
    """Read the JSON config and fill in defaults."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    cfg = {}
    for section in ("source", "destination"):
        if section not in raw or "host" not in raw[section]:
            raise ValueError(f"config must define {section}.host")
        cfg[section] = {**CONFIG_DEFAULTS[section], **raw[section]}
        cfg[section]["dir"] = cfg[section]["dir"].rstrip("/") or "/"
    cfg["source"]["archive_dir"] = (
        cfg["source"]["archive_dir"].rstrip("/") or "/")

    for key in ("archive_retention_hours", "poll_interval_seconds",
                "chunk_size", "timeout"):
        cfg[key] = raw.get(key, CONFIG_DEFAULTS[key])
    return cfg


def connect(server, timeout):
    """Open an FTP(S) connection described by a config section."""
    ftp = FTP_TLS(timeout=timeout) if server["tls"] else FTP(timeout=timeout)
    ftp.connect(server["host"], server["port"])
    ftp.login(server["user"], server["password"])
    if server["tls"]:
        ftp.prot_p()  # encrypt the data channel too
    ftp.set_pasv(True)
    return ftp


def is_directory(ftp, name):
    """Check whether *name* is a directory on the server."""
    current = ftp.pwd()
    try:
        ftp.cwd(name)
        ftp.cwd(current)
        return True
    except error_perm:
        return False


def ensure_remote_dir(ftp, path):
    """Create *path* on the server, including parents, if missing."""
    current = ftp.pwd()
    for part in path.split("/"):
        if not part:
            ftp.cwd("/")
            continue
        try:
            ftp.cwd(part)
        except error_perm:
            ftp.mkd(part)
            ftp.cwd(part)
    ftp.cwd(current)


def list_dir(ftp, path):
    """List entry names in *path*, tolerating empty-directory errors."""
    try:
        entries = ftp.nlst(path)
    except error_perm as exc:
        # Some servers reply 550 for an empty directory
        if str(exc).startswith("550"):
            return []
        raise
    names = []
    for entry in entries:
        name = posixpath.basename(entry.rstrip("/"))
        if name not in (".", ".."):
            names.append(name)
    return names


class _CountingReader:
    """File-like wrapper that counts the bytes read through it."""

    def __init__(self, fp):
        self._fp = fp
        self.bytes_read = 0

    def read(self, size):
        chunk = self._fp.read(size)
        self.bytes_read += len(chunk)
        return chunk


def copy_file(src, dst, src_path, dst_path, chunk_size):
    """Stream a single file from the source server to the destination.

    The source download and the destination upload run over the same
    pipe, so only one chunk is held in memory at a time.
    """
    src.voidcmd("TYPE I")
    conn = src.transfercmd(f"RETR {src_path}")
    try:
        with conn.makefile("rb") as fp:
            reader = _CountingReader(fp)
            dst.storbinary(f"STOR {dst_path}", reader, blocksize=chunk_size)
    finally:
        conn.close()
    src.voidresp()
    return reader.bytes_read


def copy_tree(src, dst, src_dir, dst_dir, chunk_size):
    """Recursively copy src_dir on the source server to dst_dir on the
    destination server. Returns the number of files copied."""
    ensure_remote_dir(dst, dst_dir)
    copied = 0
    for name in list_dir(src, src_dir):
        src_path = posixpath.join(src_dir, name)
        dst_path = posixpath.join(dst_dir, name)
        if is_directory(src, src_path):
            copied += copy_tree(src, dst, src_path, dst_path, chunk_size)
        else:
            size = copy_file(src, dst, src_path, dst_path, chunk_size)
            print(f"  copied {src_path} -> {dst_path} ({size} bytes)")
            copied += 1
    return copied


def remove_tree(ftp, path):
    """Recursively delete *path* on the server."""
    for name in list_dir(ftp, path):
        entry = posixpath.join(path, name)
        if is_directory(ftp, entry):
            remove_tree(ftp, entry)
        else:
            ftp.delete(entry)
    ftp.rmd(path)


def archive_folder(src, folder_path, archive_dir, dry_run):
    """Move *folder_path* into the archive, stamped with the current time."""
    stamp = datetime.now().strftime(ARCHIVE_STAMP_FORMAT)
    name = posixpath.basename(folder_path)
    archived_path = posixpath.join(archive_dir, f"{name}__{stamp}")
    if dry_run:
        print(f"  would archive {folder_path} -> {archived_path}")
        return
    ensure_remote_dir(src, archive_dir)
    src.rename(folder_path, archived_path)
    print(f"  archived {folder_path} -> {archived_path}")


def clean_archive(src, archive_dir, retention_hours, dry_run):
    """Delete archived folders older than the retention period.

    Age is taken from the timestamp suffix added by archive_folder, so
    this works even on servers with unreliable directory mtimes.
    Entries without a recognizable stamp are left alone.
    """
    cutoff = time.time() - retention_hours * 3600
    removed = 0
    for name in list_dir(src, archive_dir):
        match = ARCHIVE_STAMP_RE.search(name)
        if not match:
            continue
        archived_at = datetime.strptime(
            match.group(1), ARCHIVE_STAMP_FORMAT).timestamp()
        if archived_at > cutoff:
            continue
        entry = posixpath.join(archive_dir, name)
        if dry_run:
            print(f"  would clear {entry} from archive")
        else:
            if is_directory(src, entry):
                remove_tree(src, entry)
            else:
                src.delete(entry)
            print(f"  cleared {entry} from archive")
        removed += 1
    return removed


def process_once(cfg, dry_run):
    """One full pass: move every source folder, then clean the archive."""
    src_cfg = cfg["source"]
    dst_cfg = cfg["destination"]

    print(f"connecting to source {src_cfg['host']}:{src_cfg['port']} ...")
    src = connect(src_cfg, cfg["timeout"])
    print(f"connecting to destination {dst_cfg['host']}:{dst_cfg['port']} ...")
    dst = connect(dst_cfg, cfg["timeout"])

    moved = 0
    try:
        # make sure the fixed folders exist so a fresh server needs no setup
        ensure_remote_dir(src, src_cfg["dir"])
        ensure_remote_dir(src, src_cfg["archive_dir"])
        for name in list_dir(src, src_cfg["dir"]):
            folder_path = posixpath.join(src_cfg["dir"], name)
            if folder_path == src_cfg["archive_dir"]:
                continue
            if not is_directory(src, folder_path):
                print(f"skipping non-folder entry {folder_path}")
                continue

            print(f"moving folder {folder_path}")
            dst_path = posixpath.join(dst_cfg["dir"], name)
            if dry_run:
                print(f"  would copy {folder_path} -> {dst_path}")
            else:
                files = copy_tree(src, dst, folder_path, dst_path,
                                  cfg["chunk_size"])
                print(f"  {files} file(s) copied to destination")
            archive_folder(src, folder_path, src_cfg["archive_dir"], dry_run)
            moved += 1

        cleared = clean_archive(src, src_cfg["archive_dir"],
                                cfg["archive_retention_hours"], dry_run)
    finally:
        for conn in (src, dst):
            try:
                conn.quit()
            except Exception:
                conn.close()

    print(f"pass done: {moved} folder(s) moved, "
          f"{cleared} archive entrie(s) cleared")


def main():
    parser = argparse.ArgumentParser(
        description="Move folders between FTP servers using a JSON config.")
    parser.add_argument("--config", default="config.json",
                        help="path to the JSON config (default: config.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen without changing anything")
    args = parser.parse_args()

    cfg = load_config(args.config)
    interval = cfg["poll_interval_seconds"]

    while True:
        process_once(cfg, args.dry_run)
        if interval <= 0:
            return 0
        print(f"sleeping {interval}s until next pass "
              "(press Ctrl-C to stop)")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
