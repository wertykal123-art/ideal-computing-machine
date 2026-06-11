#!/usr/bin/env python3
"""Copy files from one FTP server to another.

Files are streamed through this machine in memory chunks (no temp files),
optionally recursing into subdirectories.

Usage:
    python ftp_copy.py \
        --src-host ftp.source.com --src-user alice --src-pass secret1 \
        --dst-host ftp.dest.com   --dst-user bob   --dst-pass secret2 \
        --src-dir /outgoing --dst-dir /incoming --recursive

Passwords can also be supplied via the FTP_SRC_PASS / FTP_DST_PASS
environment variables to keep them out of shell history.
"""

import argparse
import fnmatch
import os
import posixpath
import sys
from ftplib import FTP, FTP_TLS, error_perm


def connect(host, port, user, password, use_tls, timeout):
    """Open an FTP(S) connection and log in."""
    ftp = FTP_TLS(timeout=timeout) if use_tls else FTP(timeout=timeout)
    ftp.connect(host, port)
    ftp.login(user, password)
    if use_tls:
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


def copy_tree(src, dst, src_dir, dst_dir, pattern, recursive, chunk_size,
              dry_run):
    """Copy matching files from src_dir to dst_dir, recursing if asked."""
    copied = 0
    skipped = 0

    if not dry_run:
        ensure_remote_dir(dst, dst_dir)

    try:
        entries = src.nlst(src_dir)
    except error_perm as exc:
        # Some servers reply 550 for an empty directory
        if str(exc).startswith("550"):
            return copied, skipped
        raise

    for entry in entries:
        name = posixpath.basename(entry.rstrip("/"))
        if name in (".", ".."):
            continue
        src_path = posixpath.join(src_dir, name)
        dst_path = posixpath.join(dst_dir, name)

        if is_directory(src, src_path):
            if recursive:
                sub_copied, sub_skipped = copy_tree(
                    src, dst, src_path, dst_path, pattern, recursive,
                    chunk_size, dry_run)
                copied += sub_copied
                skipped += sub_skipped
            else:
                print(f"skip dir   {src_path}")
                skipped += 1
            continue

        if not fnmatch.fnmatch(name, pattern):
            skipped += 1
            continue

        if dry_run:
            print(f"would copy {src_path} -> {dst_path}")
            copied += 1
            continue

        size = copy_file(src, dst, src_path, dst_path, chunk_size)
        print(f"copied     {src_path} -> {dst_path} ({size} bytes)")
        copied += 1

    return copied, skipped


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy files from one FTP server to another.")
    parser.add_argument("--src-host", required=True)
    parser.add_argument("--src-port", type=int, default=21)
    parser.add_argument("--src-user", default="anonymous")
    parser.add_argument("--src-pass", default=os.environ.get("FTP_SRC_PASS", ""))
    parser.add_argument("--src-dir", default="/",
                        help="directory on the source server (default: /)")
    parser.add_argument("--src-tls", action="store_true",
                        help="use FTPS (explicit TLS) for the source")

    parser.add_argument("--dst-host", required=True)
    parser.add_argument("--dst-port", type=int, default=21)
    parser.add_argument("--dst-user", default="anonymous")
    parser.add_argument("--dst-pass", default=os.environ.get("FTP_DST_PASS", ""))
    parser.add_argument("--dst-dir", default="/",
                        help="directory on the destination server (default: /)")
    parser.add_argument("--dst-tls", action="store_true",
                        help="use FTPS (explicit TLS) for the destination")

    parser.add_argument("--pattern", default="*",
                        help="glob pattern of files to copy (default: *)")
    parser.add_argument("--recursive", action="store_true",
                        help="recurse into subdirectories")
    parser.add_argument("--chunk-size", type=int, default=64 * 1024,
                        help="transfer block size in bytes (default: 65536)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="connection timeout in seconds (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be copied without transferring")
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"connecting to source {args.src_host}:{args.src_port} ...")
    src = connect(args.src_host, args.src_port, args.src_user, args.src_pass,
                  args.src_tls, args.timeout)
    print(f"connecting to destination {args.dst_host}:{args.dst_port} ...")
    dst = connect(args.dst_host, args.dst_port, args.dst_user, args.dst_pass,
                  args.dst_tls, args.timeout)

    try:
        copied, skipped = copy_tree(
            src, dst, args.src_dir.rstrip("/") or "/",
            args.dst_dir.rstrip("/") or "/", args.pattern, args.recursive,
            args.chunk_size, args.dry_run)
    finally:
        for conn in (src, dst):
            try:
                conn.quit()
            except Exception:
                conn.close()

    verb = "would be copied" if args.dry_run else "copied"
    print(f"done: {copied} file(s) {verb}, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
