# FTP folder mover

`ftp_copy.py` moves folders from one FTP server to another. For every
subfolder it finds in the source directory it:

1. copies the whole folder (recursively) to the destination server,
2. moves the folder into an archive directory on the source server,
   stamped with the time it was archived.

On every run it also clears archived folders older than the configured
retention. The fixed folders (source dir, archive dir, destination dir)
are set once in the config and never moved — only the subfolders inside
the source dir are processed.

## Configuration

Copy `config.example.json` to `config.json` and edit it:

| key                       | meaning                                                        |
|---------------------------|----------------------------------------------------------------|
| `source` / `destination`  | FTP connection: `host`, `port`, `user`, `password`, `tls`, `dir` |
| `source.archive_dir`      | where moved folders are archived on the source server          |
| `archive_retention_hours` | archived folders older than this are deleted (age threshold)   |
| `poll_interval_seconds`   | `0` = run once and exit; `>0` = loop forever, repeating every N seconds |
| `chunk_size`              | transfer block size in bytes                                   |
| `timeout`                 | connection timeout in seconds                                  |

Run it:

```bash
python ftp_copy.py --config config.json
# preview without changing anything:
python ftp_copy.py --config config.json --dry-run
```

For a scheduler like cron or Rundeck, leave `poll_interval_seconds` at
`0` so each job execution does one pass and exits.

## Trying it locally

You can try the whole flow on your own machine with two throwaway FTP
servers — no real servers needed.

1. Install the test dependency:

   ```bash
   pip install pyftpdlib
   ```

2. In one terminal, start the sandbox. It launches a source and a
   destination FTP server on `127.0.0.1:2121` / `2122`, backed by two
   temp directories, and seeds the source with sample folders:

   ```bash
   python local_sandbox.py
   ```

   It prints the two temp directory paths — keep this terminal open.

3. In a second terminal, run the mover against the bundled local config:

   ```bash
   python ftp_copy.py --config config.local.json
   ```

4. Look at the temp directories it printed: the sample folders are now
   under `incoming/` in the destination dir and under
   `outgoing_archive/` (timestamped) in the source dir.

Press Ctrl-C in the first terminal to stop the sandbox; the temp
directories can then be deleted.
