#!/usr/bin/env python3
"""Spin up two throwaway FTP servers for trying ftp_copy.py locally.

This starts a "source" server and a "destination" server on localhost,
backed by two temporary directories, and seeds the source with a couple
of sample folders. Leave it running in one terminal, then in another
terminal run:

    python ftp_copy.py --config config.local.json

Press Ctrl-C here to stop the servers. Requires pyftpdlib:

    pip install pyftpdlib

Each server runs as its own ``python -m pyftpdlib`` process so the two
don't share state.
"""

import atexit
import os
import subprocess
import sys
import tempfile
import time

SRC_PORT = 2121
DST_PORT = 2122


def seed_source(root):
    """Drop a couple of sample folders into the source root."""
    os.makedirs(os.path.join(root, "outgoing", "job_alpha"), exist_ok=True)
    os.makedirs(os.path.join(root, "outgoing", "job_beta", "sub"),
                exist_ok=True)
    with open(os.path.join(root, "outgoing", "job_alpha", "a.txt"), "w") as fh:
        fh.write("hello from alpha\n")
    with open(os.path.join(root, "outgoing", "job_beta", "b.txt"), "w") as fh:
        fh.write("hello from beta\n")
    with open(os.path.join(root, "outgoing", "job_beta", "sub", "c.txt"),
              "w") as fh:
        fh.write("nested file\n")


def start_server(root, port):
    """Launch an anonymous, writable FTP server process serving *root*."""
    return subprocess.Popen(
        [sys.executable, "-m", "pyftpdlib",
         "-i", "127.0.0.1", "-p", str(port), "-d", root, "-w"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    src_root = tempfile.mkdtemp(prefix="ftp_src_")
    dst_root = tempfile.mkdtemp(prefix="ftp_dst_")
    seed_source(src_root)

    procs = [start_server(src_root, SRC_PORT), start_server(dst_root, DST_PORT)]

    @atexit.register
    def _cleanup():
        for proc in procs:
            proc.terminate()

    time.sleep(1)  # give the servers a moment to bind

    print("Local FTP sandbox is running.")
    print(f"  source server      ftp://127.0.0.1:{SRC_PORT}  -> {src_root}")
    print(f"  destination server ftp://127.0.0.1:{DST_PORT}  -> {dst_root}")
    print()
    print("In another terminal run:")
    print("  python ftp_copy.py --config config.local.json")
    print()
    print("Inspect the directories above to see what moved. "
          "Press Ctrl-C to stop.")
    try:
        for proc in procs:
            proc.wait()
    except KeyboardInterrupt:
        print("\nstopping servers")


if __name__ == "__main__":
    main()
